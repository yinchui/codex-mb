#!/usr/bin/env python3
import json
import importlib.util
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union
from urllib.parse import unquote


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def chunk_text(text: str, size: int = 3800) -> List[str]:
    if len(text) <= size:
        return [text]
    chunks: List[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            split_at = text.rfind("\n", start, end)
            if split_at > start:
                end = split_at + 1
        chunks.append(text[start:end])
        start = end
    return chunks


def parse_dangerous_bypass_level(raw: Optional[str]) -> int:
    value = (raw or "0").strip()
    if not value:
        return 0
    try:
        level = int(value)
    except ValueError:
        raise ValueError("CODEX_DANGEROUS_BYPASS must be 0, 1, or 2")
    if level < 0:
        level = 0
    if level > 2:
        level = 2
    return level


def parse_non_negative_int(raw: Optional[str], default: int) -> int:
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except (ValueError, TypeError, AttributeError):
        return default
    return value if value >= 0 else default


def parse_bool_env(raw: Optional[str], default: bool) -> bool:
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


@dataclass
class SessionMeta:
    session_id: str
    timestamp: str
    cwd: str
    file_path: str
    title: str


@dataclass
class WorkspaceChoice:
    root: str
    name: str
    session_count: int
    latest_session: Optional[SessionMeta]


WorkspaceSnapshot = Dict[str, Tuple[int, int]]


@dataclass
class WorkspaceWriteVerification:
    required: bool
    cwd: str
    changed_paths: List[str]
    confirmed_paths: List[str]
    claimed_paths_present: bool

    @property
    def has_changes(self) -> bool:
        return bool(self.changed_paths)

    @property
    def is_verified(self) -> bool:
        if self.confirmed_paths:
            return True
        if self.claimed_paths_present:
            return False
        return self.has_changes


@dataclass
class PromptExecutionResult:
    thread_id: Optional[str]
    answer: str
    stderr_text: str
    return_code: int
    verification: WorkspaceWriteVerification
    retry_attempted: bool = False
    retry_recovered: bool = False
    initial_thread_id: Optional[str] = None


@dataclass
class PromptPreparation:
    prompt: str
    abort_message: Optional[str] = None


StateActor = Union[int, str]


WRITE_TASK_ACTION_HINTS = (
    "写入",
    "写进",
    "同步",
    "保存",
    "落库",
    "更新",
    "write to",
    "write into",
    "save to",
    "sync to",
    "sync into",
    "update",
)

WRITE_TASK_TARGET_HINTS = (
    "obsidian",
    "笔记",
    "markdown",
    ".md",
    "_index",
    "note",
    "notes",
)

IGNORED_WORKSPACE_SNAPSHOT_DIRS = {
    ".codex",
    ".git",
    ".runtime",
    "__pycache__",
}

IGNORED_WORKSPACE_SNAPSHOT_FILES = {
    ".ds_store",
    "feishu_bot_state.json",
    "state.json",
    "tg_bot_state.json",
    "wechat_bot_state.json",
}

ANSWER_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)\n]+\.md)\)")
ANSWER_BACKTICK_PATH_RE = re.compile(r"`([^`\n]+\.md)`")
ANSWER_BACKTICK_DIR_RE = re.compile(r"`([^`\n]+[\\/])`")
ANSWER_INLINE_PATH_RE = re.compile(r"([^\s`<>\"'，。；;（）()]+(?:[\\/][^\s`<>\"'，。；;（）()]+)*\.md)")
ANSWER_INLINE_FILENAME_RE = re.compile(r"([^\s`<>\"'，。；;（）()/\\]+\.md)")
DOUYIN_URL_RE = re.compile(
    r"https?://(?:v\.douyin\.com/[A-Za-z0-9_/-]+|www\.douyin\.com/[A-Za-z0-9_/%?=&.-]+)",
    re.IGNORECASE,
)
DOUYIN_AUTH_HINTS = ("cookie", "cookies", "fresh cookies", "authentication", "login", "403", "forbidden")
DOUYIN_ASR_HINTS = ("asr", "transcript 为空", "逐字稿", "字幕文本回填", "whisper")


def prompt_requires_workspace_write_verification(prompt: str) -> bool:
    normalized = " ".join((prompt or "").strip().lower().split())
    if not normalized:
        return False
    has_action = any(token in normalized for token in WRITE_TASK_ACTION_HINTS)
    has_target = any(token in normalized for token in WRITE_TASK_TARGET_HINTS)
    return has_action and has_target


def extract_single_douyin_url(prompt: str) -> Optional[str]:
    matches = [match.rstrip("，。；;）)") for match in DOUYIN_URL_RE.findall(prompt or "")]
    unique = []
    for match in matches:
        if match not in unique:
            unique.append(match)
    if len(unique) != 1:
        return None
    return unique[0]


def resolve_douyin_preprocess_script() -> Path:
    configured = env("DOUYIN_PREPROCESS_SCRIPT")
    if configured:
        return Path(configured).expanduser()
    return (
        Path.home()
        / ".codex"
        / "skills"
        / "douyin-to-jianguoyun-note"
        / "scripts"
        / "process_douyin_video.py"
    )


def build_default_douyin_asr_command() -> Optional[str]:
    explicit = env("DOUYIN_ASR_COMMAND")
    if explicit:
        return explicit
    if importlib.util.find_spec("whisper") is None:
        return None
    model = env("DOUYIN_ASR_MODEL", "base") or "base"
    language = env("DOUYIN_ASR_LANGUAGE", "zh") or "zh"
    python_bin = sys.executable or "python3"
    return (
        f"{python_bin} -m whisper {{audio_path}} "
        f"--model {model} --language {language} "
        "--output_format json --output_dir {output_dir}"
    )


def run_douyin_preprocessor(url: str, cwd: Path) -> Dict[str, Any]:
    script_path = resolve_douyin_preprocess_script()
    if not script_path.exists():
        raise RuntimeError(f"抖音预处理脚本不存在：{script_path}")

    command = [sys.executable or "python3", str(script_path), url]
    cookies_file = env("DOUYIN_COOKIES_FILE")
    if cookies_file:
        command.extend(["--cookies", str(Path(cookies_file).expanduser())])
    yt_dlp_bin = env("DOUYIN_YT_DLP_BIN")
    if yt_dlp_bin:
        command.extend(["--yt-dlp-bin", yt_dlp_bin])
    ffmpeg_bin = env("DOUYIN_FFMPEG_BIN")
    if ffmpeg_bin:
        command.extend(["--ffmpeg-bin", ffmpeg_bin])
    ffprobe_bin = env("DOUYIN_FFPROBE_BIN")
    if ffprobe_bin:
        command.extend(["--ffprobe-bin", ffprobe_bin])
    asr_command = build_default_douyin_asr_command()
    if asr_command:
        command.extend(["--asr-command", asr_command])

    timeout_sec = parse_non_negative_int(env("DOUYIN_PREPROCESS_TIMEOUT_SEC", "600"), 600)
    completed = subprocess.run(
        command,
        cwd=str(cwd.expanduser()),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=max(timeout_sec, 60),
    )
    stdout = (completed.stdout or "").strip()
    if not stdout:
        stderr = (completed.stderr or "").strip()
        raise RuntimeError(stderr or "抖音预处理没有输出结果。")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"抖音预处理输出不是有效 JSON：{stdout[:400]}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("抖音预处理返回了意外结果。")
    return payload


def format_douyin_preprocess_abort_message(
    prompt: str,
    payload: Dict[str, Any],
    *,
    runtime_error: Optional[str] = None,
) -> str:
    lines = ["抖音预处理失败，这次还没有拿到可用逐字稿，所以不会继续让 Codex 猜内容。"]
    first_error = ""
    if runtime_error:
        first_error = runtime_error.strip()
    else:
        errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
        first_error = str(errors[0]).strip() if errors else ""
    if first_error:
        lines.append(f"失败原因: {first_error}")

    warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
    warning_text = "\n".join(str(item).strip() for item in warnings if str(item).strip())
    lowered = f"{first_error}\n{warning_text}".lower()
    if any(token in lowered for token in DOUYIN_AUTH_HINTS):
        cookies_file = env("DOUYIN_COOKIES_FILE")
        if cookies_file:
            lines.append(
                "当前配置的抖音 cookies 可能已过期，请刷新后覆盖这个文件再试："
                f" {Path(cookies_file).expanduser()}"
            )
        else:
            lines.append(
                "请先准备 fresh cookies.txt，并在运行环境里设置 "
                "DOUYIN_COOKIES_FILE=/绝对路径/cookies.txt。"
            )
    if any(token in lowered for token in DOUYIN_ASR_HINTS) or ("逐字稿" in prompt and not payload.get("transcript")):
        lines.append(
            "当前环境还没有稳定拿到可用 ASR 结果。"
            "我已经支持自动调用本机 Whisper；如果仍失败，请确认运行这条服务的 Python 能导入 whisper。"
        )
    if warning_text:
        lines.extend(["", "预处理提示:", warning_text])
    return "\n".join(lines).strip()


def build_douyin_materials_prompt(original_prompt: str, payload: Dict[str, Any]) -> str:
    transcript = str(payload.get("transcript") or "").strip()
    subtitles = payload.get("detected_subtitles") if isinstance(payload.get("detected_subtitles"), list) else []
    visual_points = payload.get("key_visual_points") if isinstance(payload.get("key_visual_points"), list) else []
    warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []

    lines = [
        "下面是已经预处理好的抖音材料。你必须优先基于这些确定性材料工作，不能忽略它们，也不能虚构未提取到的逐字稿。",
        "",
        "原始用户请求:",
        (original_prompt or "").strip(),
        "",
        "抖音预处理材料:",
        f"- source_url: {str(payload.get('source_url') or '').strip()}",
        f"- resolved_url: {str(payload.get('resolved_url') or '').strip()}",
        f"- video_id: {str(payload.get('video_id') or '').strip()}",
        f"- author: {str(payload.get('author') or '').strip()}",
    ]
    duration = payload.get("duration")
    if isinstance(duration, (int, float)):
        lines.append(f"- duration_seconds: {duration}")
    if warnings:
        lines.extend(["", "warnings:", *[f"- {str(item).strip()}" for item in warnings if str(item).strip()]])
    if transcript:
        lines.extend(["", "transcript:", transcript])
    if subtitles:
        lines.extend(
            ["", "detected_subtitles:", *[f"- {str(item).strip()}" for item in subtitles if str(item).strip()][:40]]
        )
    if visual_points:
        lines.extend(
            ["", "key_visual_points:", *[f"- {str(item).strip()}" for item in visual_points if str(item).strip()][:24]]
        )
    lines.extend(
        [
            "",
            "执行要求:",
            "1. 优先采用 transcript；只有 transcript 缺字时，才参考 detected_subtitles。",
            "2. 如果用户要求写入 Obsidian 或生成笔记，直接基于这些材料落盘，不要再口头说“我会先去抓内容”。",
            "3. 如果材料里仍有不确定项，要明确标注，不要补造原视频里不存在的细节。",
        ]
    )
    return "\n".join(line for line in lines if line is not None).strip()


def prepare_prompt_with_douyin_materials(prompt: str, cwd: Path) -> PromptPreparation:
    if not parse_bool_env(env("DOUYIN_PREPROCESS_ENABLED"), True):
        return PromptPreparation(prompt=prompt)
    douyin_url = extract_single_douyin_url(prompt)
    if not douyin_url:
        return PromptPreparation(prompt=prompt)

    log(f"douyin preprocess started: url={douyin_url} cwd={cwd.expanduser()}")
    try:
        payload = run_douyin_preprocessor(douyin_url, cwd)
    except Exception as exc:
        log(f"douyin preprocess failed: url={douyin_url} error={exc}")
        return PromptPreparation(
            prompt=prompt,
            abort_message=format_douyin_preprocess_abort_message(prompt, {}, runtime_error=str(exc)),
        )

    errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
    transcript = str(payload.get("transcript") or "").strip()
    if errors or not transcript:
        log(
            "douyin preprocess incomplete: "
            f"url={douyin_url} errors={len(errors)} transcript_len={len(transcript)}"
        )
        return PromptPreparation(
            prompt=prompt,
            abort_message=format_douyin_preprocess_abort_message(prompt, payload),
        )

    log(
        "douyin preprocess ready: "
        f"url={douyin_url} video_id={payload.get('video_id')} transcript_len={len(transcript)}"
    )
    return PromptPreparation(
        prompt=build_douyin_materials_prompt(prompt, payload),
        abort_message=None,
    )


def _snapshot_workspace(root: Path) -> WorkspaceSnapshot:
    snapshot: WorkspaceSnapshot = {}
    if not root.exists() or not root.is_dir():
        return snapshot
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            relative_path = path.relative_to(root)
            relative_parts = relative_path.parts
            if any(part in IGNORED_WORKSPACE_SNAPSHOT_DIRS for part in relative_parts[:-1]):
                continue
            if relative_parts and relative_parts[-1].lower() in IGNORED_WORKSPACE_SNAPSHOT_FILES:
                continue
            stat = path.stat()
            relative = relative_path.as_posix()
        except OSError:
            continue
        snapshot[relative] = (int(stat.st_mtime_ns), int(stat.st_size))
    return snapshot


def capture_workspace_snapshot_for_prompt(prompt: str, cwd: Path) -> Optional[WorkspaceSnapshot]:
    if not prompt_requires_workspace_write_verification(prompt):
        return None
    return _snapshot_workspace(cwd.expanduser())


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _clean_answer_path(raw: str) -> str:
    cleaned = unquote((raw or "").strip())
    return cleaned.rstrip(".,，。；;）)")


def _resolve_answer_path(raw: str, cwd: Path) -> Optional[Path]:
    cleaned = _clean_answer_path(raw)
    if not cleaned or not cleaned.lower().endswith(".md"):
        return None
    candidate = Path(cleaned).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    candidate = candidate.resolve(strict=False)
    root = cwd.resolve(strict=False)
    if not _is_relative_to(candidate, root):
        return None
    return candidate


def _extract_existing_note_paths_from_answer(answer: str, cwd: Path, prompt: str) -> List[str]:
    text = answer or ""
    if not text:
        return []

    resolved_cwd = cwd.resolve(strict=False)
    direct_candidates: List[Path] = []
    bare_filenames: List[str] = []
    dir_candidates: List[Path] = []

    for regex in (ANSWER_MARKDOWN_LINK_RE, ANSWER_BACKTICK_PATH_RE, ANSWER_INLINE_PATH_RE):
        for match in regex.findall(text):
            candidate = _resolve_answer_path(match, resolved_cwd)
            if candidate is not None:
                direct_candidates.append(candidate)

    for match in ANSWER_INLINE_FILENAME_RE.findall(text):
        filename = _clean_answer_path(match)
        if filename and "/" not in filename and "\\" not in filename:
            bare_filenames.append(filename)

    for match in ANSWER_BACKTICK_DIR_RE.findall(text):
        cleaned = _clean_answer_path(match).rstrip("/\\")
        if not cleaned:
            continue
        directory = (resolved_cwd / cleaned).resolve(strict=False)
        if _is_relative_to(directory, resolved_cwd):
            dir_candidates.append(directory)

    if dir_candidates and bare_filenames:
        for directory in dir_candidates:
            for filename in bare_filenames:
                candidate = _resolve_answer_path(str(directory / filename), resolved_cwd)
                if candidate is not None:
                    direct_candidates.append(candidate)

    prompt_lower = (prompt or "").lower()
    index_required = "_index" in prompt_lower or "索引" in prompt
    confirmed: List[str] = []
    seen: Set[str] = set()
    for candidate in direct_candidates:
        if not candidate.exists() or not candidate.is_file():
            continue
        index_path = candidate.parent / "_index.md"
        if index_path.exists():
            try:
                index_text = index_path.read_text(encoding="utf-8")
            except OSError:
                continue
            if f"[[{candidate.name}]]" not in index_text:
                continue
        elif index_required:
            continue
        relative = candidate.relative_to(resolved_cwd).as_posix()
        if relative in seen:
            continue
        seen.add(relative)
        confirmed.append(relative)
    return confirmed


def answer_claims_note_path(answer: str, cwd: Path) -> bool:
    text = answer or ""
    if not text:
        return False
    if ANSWER_MARKDOWN_LINK_RE.search(text) or ANSWER_BACKTICK_PATH_RE.search(text) or ANSWER_INLINE_PATH_RE.search(text):
        return True
    has_filename = bool(ANSWER_INLINE_FILENAME_RE.search(text))
    has_directory = bool(ANSWER_BACKTICK_DIR_RE.search(text))
    if has_filename and has_directory:
        return True
    text_lower = text.lower()
    return ("文件路径" in text or "存放在" in text or "写入" in text) and (".md" in text_lower or has_filename)


def verify_workspace_snapshot_for_prompt(
    prompt: str,
    cwd: Path,
    before_snapshot: Optional[WorkspaceSnapshot],
    answer: str = "",
) -> WorkspaceWriteVerification:
    resolved_cwd = str(cwd.expanduser())
    if not prompt_requires_workspace_write_verification(prompt):
        return WorkspaceWriteVerification(
            required=False,
            cwd=resolved_cwd,
            changed_paths=[],
            confirmed_paths=[],
            claimed_paths_present=False,
        )
    if before_snapshot is None:
        return WorkspaceWriteVerification(
            required=True,
            cwd=resolved_cwd,
            changed_paths=[],
            confirmed_paths=[],
            claimed_paths_present=answer_claims_note_path(answer, cwd.expanduser()),
        )

    after_snapshot = _snapshot_workspace(cwd.expanduser())
    changed_paths = sorted(
        path
        for path in set(before_snapshot) | set(after_snapshot)
        if before_snapshot.get(path) != after_snapshot.get(path)
    )
    claimed_paths_present = answer_claims_note_path(answer, cwd.expanduser())
    confirmed_paths: List[str] = []
    if not changed_paths or claimed_paths_present:
        confirmed_paths = _extract_existing_note_paths_from_answer(answer, cwd.expanduser(), prompt)
    return WorkspaceWriteVerification(
        required=True,
        cwd=resolved_cwd,
        changed_paths=changed_paths,
        confirmed_paths=confirmed_paths,
        claimed_paths_present=claimed_paths_present,
    )


def format_missing_workspace_write_warning(answer: str, verification: WorkspaceWriteVerification) -> str:
    lines = [
        "未检测到当前工作区内有任何文件改动，这次不能确认已同步成功。",
        f"检查目录: {verification.cwd}",
    ]
    raw_answer = (answer or "").strip()
    if raw_answer:
        lines.extend(["", "模型原始回复:", raw_answer])
    return "\n".join(lines).strip()


def build_workspace_write_retry_prompt(prompt: str, verification: WorkspaceWriteVerification, answer: str) -> str:
    lines = [
        "这是一次针对 Obsidian/笔记同步失败的自动重试，请在全新的会话里重新执行，不要沿用上一个会话的口头结论。",
        f"当前工作区: {verification.cwd}",
        "上一次尝试未能在文件系统中确认同步成功。",
        "你必须直接检查并实际写入文件，而不是只回复“已经同步”。",
        "",
        "执行要求:",
        "1. 先检查目标笔记是否存在，以及对应目录下的 _index.md 是否包含该笔记链接。",
        "2. 如果笔记不存在或内容不完整，就实际创建/更新该 .md 文件。",
        "3. 如果任务需要索引，就同步更新 _index.md。",
        "4. 完成后再次用文件系统命令核对目标 .md 文件和 _index.md 的最终状态。",
        "5. 只有在核对成功后，才在最终答复里说明已同步，并给出真实文件路径。",
        "",
        "原始任务:",
        prompt.strip(),
    ]
    raw_answer = (answer or "").strip()
    if raw_answer:
        lines.extend(["", "上一次会话的口头回复（仅供参考，不能当作事实）:", raw_answer])
    return "\n".join(lines).strip()


def format_workspace_write_retry_status(verification: WorkspaceWriteVerification) -> str:
    return (
        "首次落盘核对未通过，正在新会话里重新检查并补写笔记文件。"
        f"\n\n工作区: {verification.cwd}"
    )


def run_prompt_with_workspace_write_recovery(
    codex: Any,
    *,
    prompt: str,
    cwd: Path,
    session_id: Optional[str] = None,
    on_update: Optional[Callable[[str], None]] = None,
    on_retry_status: Optional[Callable[[str], None]] = None,
    prompt_preparer: Optional[Callable[[str, Path], PromptPreparation]] = None,
) -> PromptExecutionResult:
    prompt_preparer = prompt_preparer or prepare_prompt_with_douyin_materials
    prepared = prompt_preparer(prompt, cwd.expanduser())
    if prepared.abort_message:
        return PromptExecutionResult(
            thread_id=session_id,
            answer=prepared.abort_message,
            stderr_text="",
            return_code=0,
            verification=WorkspaceWriteVerification(
                required=False,
                cwd=str(cwd.expanduser()),
                changed_paths=[],
                confirmed_paths=[],
                claimed_paths_present=False,
            ),
        )

    effective_prompt = prepared.prompt
    write_snapshot = capture_workspace_snapshot_for_prompt(prompt, cwd)
    thread_id, answer, stderr_text, return_code = codex.run_prompt(
        prompt=effective_prompt,
        cwd=cwd,
        session_id=session_id,
        on_update=on_update,
    )
    verification = verify_workspace_snapshot_for_prompt(prompt, cwd, write_snapshot, answer=answer)
    if return_code != 0 or not verification.required or verification.is_verified:
        return PromptExecutionResult(
            thread_id=thread_id,
            answer=answer,
            stderr_text=stderr_text,
            return_code=return_code,
            verification=verification,
        )

    if on_retry_status is not None:
        try:
            on_retry_status(format_workspace_write_retry_status(verification))
        except Exception:
            pass

    retry_snapshot = capture_workspace_snapshot_for_prompt(prompt, cwd)
    retry_prompt = build_workspace_write_retry_prompt(effective_prompt, verification, answer)
    retry_thread_id, retry_answer, retry_stderr_text, retry_return_code = codex.run_prompt(
        prompt=retry_prompt,
        cwd=cwd,
        session_id=None,
        on_update=on_update,
    )
    retry_verification = verify_workspace_snapshot_for_prompt(prompt, cwd, retry_snapshot, answer=retry_answer)
    return PromptExecutionResult(
        thread_id=retry_thread_id or thread_id,
        answer=retry_answer,
        stderr_text=retry_stderr_text,
        return_code=retry_return_code,
        verification=retry_verification,
        retry_attempted=True,
        retry_recovered=(retry_return_code == 0 and retry_verification.is_verified),
        initial_thread_id=thread_id,
    )


class SessionStore:
    def __init__(self, root: Path):
        self.root = root.expanduser()

    def list_recent(self, limit: int = 10) -> List[SessionMeta]:
        if not self.root.exists():
            return []
        files = sorted(self.root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        sessions: List[SessionMeta] = []
        for path in files:
            meta = self._parse_session_meta(path)
            if not meta:
                continue
            sessions.append(meta)
            if limit > 0 and len(sessions) >= limit:
                break
        return sessions

    def find_by_id(self, session_id: str) -> Optional[SessionMeta]:
        if not self.root.exists():
            return None
        for path in self.root.rglob("*.jsonl"):
            meta = self._parse_session_meta(path)
            if meta and meta.session_id == session_id:
                return meta
        return None

    def mark_as_desktop_session(self, session_id: str) -> bool:
        meta = self.find_by_id(session_id)
        if not meta:
            return False
        path = Path(meta.file_path)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            if not lines:
                return False
            first = json.loads(lines[0])
            if first.get("type") != "session_meta":
                return False
            payload = first.get("payload") or {}
            changed = False
            if payload.get("source") != "vscode":
                payload["source"] = "vscode"
                changed = True
            if payload.get("originator") != "Codex Desktop":
                payload["originator"] = "Codex Desktop"
                changed = True
            if not changed:
                return True
            first["payload"] = payload
            lines[0] = json.dumps(first, ensure_ascii=False, separators=(",", ":"))
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return True
        except Exception:
            return False

    def get_history(
        self,
        session_id: str,
        limit: int = 10,
    ) -> Tuple[Optional[SessionMeta], List[Tuple[str, str]]]:
        meta = self.find_by_id(session_id)
        if not meta:
            return None, []
        path = Path(meta.file_path)
        messages: List[Tuple[str, str]] = []
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if evt.get("type") != "event_msg":
                        continue
                    payload = evt.get("payload") or {}
                    msg_type = payload.get("type")
                    if msg_type not in ("user_message", "agent_message"):
                        continue
                    message = (payload.get("message") or "").strip()
                    if not message:
                        continue
                    role = "user" if msg_type == "user_message" else "assistant"
                    messages.append((role, message))
        except Exception:
            return meta, []
        if limit > 0:
            messages = messages[-limit:]
        return meta, messages

    @staticmethod
    def _parse_session_meta(path: Path) -> Optional[SessionMeta]:
        try:
            with path.open("r", encoding="utf-8") as f:
                first_line = f.readline()
            parsed = json.loads(first_line)
            payload = parsed.get("payload") or {}
            if parsed.get("type") != "session_meta":
                return None
            session_id = payload.get("id")
            if not session_id:
                return None
            title = SessionStore._extract_title(path)
            return SessionMeta(
                session_id=session_id,
                timestamp=payload.get("timestamp", "unknown"),
                cwd=payload.get("cwd", "unknown"),
                file_path=str(path),
                title=title or f"session {session_id[:8]}",
            )
        except Exception:
            return None

    @staticmethod
    def _extract_title(path: Path) -> Optional[str]:
        try:
            with path.open("r", encoding="utf-8") as f:
                for _ in range(240):
                    line = f.readline()
                    if not line:
                        break
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if evt.get("type") != "event_msg":
                        continue
                    payload = evt.get("payload") or {}
                    if payload.get("type") != "user_message":
                        continue
                    message = (payload.get("message") or "").strip()
                    if not message:
                        continue
                    return SessionStore._compact_title(message)
        except Exception:
            return None
        return None

    @staticmethod
    def _compact_title(text: str, limit: int = 46) -> str:
        one_line = " ".join(text.split())
        if len(one_line) <= limit:
            return one_line
        return one_line[: limit - 1] + "…"

    @staticmethod
    def compact_message(text: str, limit: int = 320) -> str:
        one_line = " ".join(text.split())
        if len(one_line) <= limit:
            return one_line
        return one_line[: limit - 1] + "…"


class BotState:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: Dict[str, Any] = {"users": {}}
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        with self._lock:
            if not self.path.exists():
                return
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self.data = {"users": {}}

    @staticmethod
    def _normalize_session_id(value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _save_unlocked(self) -> None:
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def save(self) -> None:
        with self._lock:
            self._save_unlocked()

    def _get_user_unlocked(self, user_id: StateActor) -> Dict[str, Any]:
        users = self.data.setdefault("users", {})
        key = str(user_id)
        if key not in users:
            users[key] = {}
        return users[key]

    def set_active_session(self, user_id: StateActor, session_id: str, cwd: str) -> None:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            user_data["active_session_id"] = session_id
            user_data["active_cwd"] = cwd
            self._save_unlocked()

    def clear_active_session(self, user_id: StateActor, cwd: str) -> None:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            user_data["active_session_id"] = None
            user_data["active_cwd"] = cwd
            self._save_unlocked()

    def get_active(self, user_id: StateActor) -> Tuple[Optional[str], Optional[str]]:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            session_id = self._normalize_session_id(user_data.get("active_session_id"))
            cwd = str(user_data.get("active_cwd") or "").strip() or None
            return session_id, cwd

    def set_last_session_ids(self, user_id: StateActor, session_ids: List[str]) -> None:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            user_data["last_session_ids"] = session_ids
            self._save_unlocked()

    def get_last_session_ids(self, user_id: StateActor) -> List[str]:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            values = user_data.get("last_session_ids")
            if not isinstance(values, list):
                return []
            return [str(v) for v in values]

    def set_pending_session_pick(self, user_id: StateActor, enabled: bool) -> None:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            user_data["pending_session_pick"] = bool(enabled)
            if not enabled:
                user_data.pop("session_picker", None)
            self._save_unlocked()

    def is_pending_session_pick(self, user_id: StateActor) -> bool:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            return bool(user_data.get("pending_session_pick"))

    def update_active_session_if_unchanged(
        self,
        user_id: StateActor,
        expected_session_id: Optional[str],
        next_session_id: str,
        cwd: str,
    ) -> bool:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            current_session_id = self._normalize_session_id(user_data.get("active_session_id"))
            if current_session_id != self._normalize_session_id(expected_session_id):
                return False
            user_data["active_session_id"] = next_session_id
            user_data["active_cwd"] = cwd
            self._save_unlocked()
            return True

    def set_workspace_picker(
        self,
        user_id: StateActor,
        workspaces: List[str],
    ) -> None:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            user_data["pending_session_pick"] = True
            user_data["session_picker"] = {
                "mode": "workspace",
                "workspace_roots": [str(item) for item in workspaces],
            }
            self._save_unlocked()

    def set_workspace_session_picker(
        self,
        user_id: StateActor,
        workspace_root: str,
        session_ids: List[str],
    ) -> None:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            user_data["pending_session_pick"] = True
            user_data["last_session_ids"] = [str(item) for item in session_ids]
            user_data["session_picker"] = {
                "mode": "session",
                "workspace_root": str(workspace_root),
                "session_ids": [str(item) for item in session_ids],
            }
            self._save_unlocked()

    def get_session_picker(self, user_id: StateActor) -> Dict[str, Any]:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            picker = user_data.get("session_picker")
            return dict(picker) if isinstance(picker, dict) else {}

    def clear_session_picker(self, user_id: StateActor) -> None:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            user_data["pending_session_pick"] = False
            user_data.pop("session_picker", None)
            self._save_unlocked()


def resolve_codex_global_state_path() -> Path:
    return Path("~/.codex/.codex-global-state.json").expanduser()


def normalize_workspace_path(path: Union[str, Path]) -> str:
    return os.path.abspath(os.path.expanduser(str(path)))


def load_saved_workspace_roots(state_path: Optional[Path] = None) -> List[str]:
    target = (state_path or resolve_codex_global_state_path()).expanduser()
    if not target.exists():
        return []
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return []
    values = payload.get("electron-saved-workspace-roots")
    if not isinstance(values, list):
        return []
    roots: List[str] = []
    seen: Set[str] = set()
    for item in values:
        normalized = normalize_workspace_path(str(item))
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        roots.append(normalized)
    return roots


def workspace_name(workspace_root: str) -> str:
    path = Path(workspace_root)
    return path.name or workspace_root


def session_matches_workspace(session: SessionMeta, workspace_root: Union[str, Path]) -> bool:
    session_cwd = normalize_workspace_path(session.cwd)
    workspace = normalize_workspace_path(workspace_root)
    if session_cwd == workspace:
        return True
    prefix = workspace.rstrip(os.sep) + os.sep
    return session_cwd.startswith(prefix)


def find_workspace_root_for_cwd(cwd: Union[str, Path], workspace_roots: List[str]) -> Optional[str]:
    normalized_cwd = normalize_workspace_path(cwd)
    candidates: List[str] = []
    for root in workspace_roots:
        normalized_root = normalize_workspace_path(root)
        if normalized_cwd == normalized_root:
            candidates.append(normalized_root)
            continue
        prefix = normalized_root.rstrip(os.sep) + os.sep
        if normalized_cwd.startswith(prefix):
            candidates.append(normalized_root)
    if not candidates:
        return None
    return max(candidates, key=len)


def build_workspace_choices(
    session_store: SessionStore,
    *,
    workspace_roots: Optional[List[str]] = None,
) -> List[WorkspaceChoice]:
    roots = workspace_roots if workspace_roots is not None else load_saved_workspace_roots()
    if not roots:
        return []
    all_sessions = session_store.list_recent(limit=0)
    choices: List[WorkspaceChoice] = []
    for root in roots:
        matching = [session for session in all_sessions if session_matches_workspace(session, root)]
        choices.append(
            WorkspaceChoice(
                root=root,
                name=workspace_name(root),
                session_count=len(matching),
                latest_session=matching[0] if matching else None,
            )
        )
    return choices


def list_workspace_sessions(
    session_store: SessionStore,
    workspace_root: Union[str, Path],
    *,
    limit: int = 20,
) -> List[SessionMeta]:
    matching = [
        session
        for session in session_store.list_recent(limit=0)
        if session_matches_workspace(session, workspace_root)
    ]
    if limit > 0:
        return matching[:limit]
    return matching


def format_workspace_choices_message(choices: List[WorkspaceChoice]) -> str:
    if not choices:
        return "未找到工作区。请先在 Codex 左侧边栏添加工作区。"
    lines = ["工作区列表（发送编号查看该工作区的会话）:"]
    for index, item in enumerate(choices, start=1):
        summary = f"{item.session_count} 个会话"
        if item.latest_session is not None:
            summary = f"{summary} | 最近: {item.latest_session.title}"
        lines.append(f"{index}. {item.name}")
        lines.append(f"   路径: {item.root}")
        lines.append(f"   {summary}")
    return "\n".join(lines)


def format_workspace_sessions_message(workspace_root: Union[str, Path], sessions: List[SessionMeta]) -> str:
    normalized_root = normalize_workspace_path(workspace_root)
    lines = [
        f"工作区: {normalized_root}",
        "发送编号切换会话，或发送 0 新建会话。",
        "0. 新建会话",
    ]
    if not sessions:
        lines.append("当前工作区下还没有历史会话。")
        return "\n".join(lines)
    for index, item in enumerate(sessions, start=1):
        lines.append(f"{index}. {item.title} | {item.session_id[:8]}")
    return "\n".join(lines)


class RunningPromptRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._running_counts: Dict[str, int] = {}
        self._running_sessions: Dict[str, Set[str]] = {}

    @staticmethod
    def _actor_key(actor: StateActor) -> str:
        return str(actor)

    def try_start(self, actor: StateActor, session_id: Optional[str]) -> bool:
        actor_key = self._actor_key(actor)
        normalized_session_id = BotState._normalize_session_id(session_id)
        with self._lock:
            if normalized_session_id:
                sessions = self._running_sessions.setdefault(actor_key, set())
                if normalized_session_id in sessions:
                    return False
                sessions.add(normalized_session_id)
            self._running_counts[actor_key] = self._running_counts.get(actor_key, 0) + 1
            return True

    def finish(self, actor: StateActor, session_id: Optional[str]) -> None:
        actor_key = self._actor_key(actor)
        normalized_session_id = BotState._normalize_session_id(session_id)
        with self._lock:
            current_count = self._running_counts.get(actor_key, 0)
            if current_count <= 1:
                self._running_counts.pop(actor_key, None)
            elif current_count > 1:
                self._running_counts[actor_key] = current_count - 1

            if normalized_session_id:
                sessions = self._running_sessions.get(actor_key)
                if sessions is not None:
                    sessions.discard(normalized_session_id)
                    if not sessions:
                        self._running_sessions.pop(actor_key, None)

    def count(self, actor: StateActor) -> int:
        actor_key = self._actor_key(actor)
        with self._lock:
            return self._running_counts.get(actor_key, 0)


class CodexRunner:
    def __init__(
        self,
        codex_bin: str,
        sandbox_mode: Optional[str] = None,
        approval_policy: Optional[str] = None,
        dangerous_bypass_level: int = 0,
        idle_timeout_sec: int = 3600,
    ):
        self.codex_bin = codex_bin
        self.sandbox_mode = sandbox_mode
        self.approval_policy = approval_policy
        self.dangerous_bypass_level = max(0, min(2, int(dangerous_bypass_level)))
        self.idle_timeout_sec = max(0, int(idle_timeout_sec))

    @staticmethod
    def _to_toml_string(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    @staticmethod
    def _terminate_process_tree(proc: subprocess.Popen[str], force: bool = False) -> None:
        sig = signal.SIGKILL if force else signal.SIGTERM
        try:
            os.killpg(proc.pid, sig)
            return
        except Exception:
            pass
        try:
            if force:
                proc.kill()
            else:
                proc.terminate()
        except Exception:
            pass

    @staticmethod
    def _close_process_pipes(proc: subprocess.Popen[str]) -> None:
        for pipe in (proc.stdout, proc.stderr):
            if pipe is None:
                continue
            try:
                pipe.close()
            except Exception:
                pass

    def run_prompt(
        self,
        prompt: str,
        cwd: Path,
        session_id: Optional[str] = None,
        on_update: Optional[Callable[[str], None]] = None,
    ) -> Tuple[Optional[str], str, str, int]:
        config_flags: List[str] = []
        if self.dangerous_bypass_level == 1:
            sandbox_mode = self.sandbox_mode or "danger-full-access"
            approval_policy = self.approval_policy or "never"
            config_flags.extend(["-c", f"sandbox_mode={self._to_toml_string(sandbox_mode)}"])
            config_flags.extend(["-c", f"approval_policy={self._to_toml_string(approval_policy)}"])

        exec_flags: List[str] = ["--json", "--skip-git-repo-check"]
        if self.dangerous_bypass_level >= 2:
            exec_flags.append("--dangerously-bypass-approvals-and-sandbox")

        if session_id:
            cmd = [
                self.codex_bin,
                "exec",
                "resume",
                *config_flags,
                *exec_flags,
                session_id,
                prompt,
            ]
        else:
            cmd = [
                self.codex_bin,
                "exec",
                *config_flags,
                *exec_flags,
                prompt,
            ]

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except FileNotFoundError as e:
            return None, f"找不到 codex 可执行文件: {self.codex_bin}", str(e), 127

        stdout_lines: List[str] = []
        stderr_chunks: List[str] = []
        activity_lock = threading.Lock()
        last_output_at = [time.monotonic()]

        def mark_output() -> None:
            with activity_lock:
                last_output_at[0] = time.monotonic()

        def _collect_stderr() -> None:
            if proc.stderr is None:
                return
            try:
                for line in proc.stderr:
                    mark_output()
                    stderr_chunks.append(line)
            except Exception:
                return

        stderr_thread: Optional[threading.Thread] = None
        if proc.stderr is not None:
            stderr_thread = threading.Thread(target=_collect_stderr, daemon=True)
            stderr_thread.start()

        timed_out = threading.Event()

        def _watchdog() -> None:
            if self.idle_timeout_sec <= 0:
                return
            while proc.poll() is None:
                time.sleep(5)
                with activity_lock:
                    idle_for_sec = time.monotonic() - last_output_at[0]
                if idle_for_sec < self.idle_timeout_sec:
                    continue
                timed_out.set()
                log(
                    "codex exec idle timed out: "
                    f"pid={proc.pid} idle_timeout_sec={self.idle_timeout_sec} "
                    f"idle_for_sec={int(idle_for_sec)} cwd={cwd}"
                )
                try:
                    self._terminate_process_tree(proc, force=False)
                    proc.wait(timeout=5)
                    self._close_process_pipes(proc)
                    return
                except subprocess.TimeoutExpired:
                    pass
                except Exception:
                    return
                try:
                    self._terminate_process_tree(proc, force=True)
                    proc.wait(timeout=2)
                except Exception:
                    return
                finally:
                    self._close_process_pipes(proc)
                return

        watchdog_thread: Optional[threading.Thread] = None
        if self.idle_timeout_sec > 0:
            watchdog_thread = threading.Thread(target=_watchdog, daemon=True)
            watchdog_thread.start()

        thread_id: Optional[str] = None
        messages: List[str] = []
        current_agent_text = ""
        current_agent_phase: Optional[str] = None
        last_emitted = ""

        if proc.stdout is not None:
            try:
                for raw_line in proc.stdout:
                    mark_output()
                    stdout_lines.append(raw_line.rstrip("\n"))
                    line = raw_line.strip()
                    if not line or not line.startswith("{"):
                        continue
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    evt_thread_id, messages, current_agent_text, current_agent_phase, changed = self._consume_exec_event(
                        evt,
                        messages,
                        current_agent_text,
                        current_agent_phase,
                    )
                    if evt_thread_id and not thread_id:
                        thread_id = evt_thread_id
                    if on_update and changed:
                        live_text = self._compose_agent_text(messages, current_agent_text)
                        if live_text and live_text != last_emitted:
                            try:
                                on_update(live_text)
                            except Exception:
                                pass
                            last_emitted = live_text
            except Exception:
                pass

        return_code = proc.wait()
        if watchdog_thread is not None:
            watchdog_thread.join(timeout=0.2)
        if stderr_thread is not None:
            stderr_thread.join(timeout=2.0)
        stderr_text = "".join(stderr_chunks).strip()

        if current_agent_text.strip():
            final_piece = current_agent_text.strip()
            if not messages or messages[-1] != final_piece:
                messages.append(final_piece)

        agent_text = self._compose_agent_text(messages, "")
        stdout_text = "\n".join(stdout_lines)
        if not thread_id or not agent_text:
            parsed_thread_id, parsed_text = self._parse_exec_json(stdout_text)
            if not thread_id:
                thread_id = parsed_thread_id
            if not agent_text:
                agent_text = parsed_text
        if not agent_text:
            merged = (stdout_text + "\n" + stderr_text).strip()
            if merged:
                agent_text = merged[-3500:]
            else:
                agent_text = "Codex 没有返回可展示内容。"
        if timed_out.is_set():
            timeout_text = (
                f"Codex 长时间无输出（>{self.idle_timeout_sec}s），"
                "进程已被终止。通常是卡在外部命令、网络请求、远端连接或等待输入。"
            )
            if agent_text and agent_text != "Codex 没有返回可展示内容。":
                agent_text = f"{timeout_text}\n\n{agent_text}"
            else:
                agent_text = timeout_text
        return thread_id, agent_text, stderr_text, return_code

    @staticmethod
    def _parse_exec_json(stdout: str) -> Tuple[Optional[str], str]:
        thread_id: Optional[str] = None
        messages: List[str] = []
        current_agent_text = ""
        current_agent_phase: Optional[str] = None
        for line in stdout.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            evt_thread_id, messages, current_agent_text, current_agent_phase, _ = CodexRunner._consume_exec_event(
                evt,
                messages,
                current_agent_text,
                current_agent_phase,
            )
            if evt_thread_id and not thread_id:
                thread_id = evt_thread_id
        text = CodexRunner._compose_agent_text(messages, current_agent_text)
        return thread_id, text

    @staticmethod
    def _compose_agent_text(messages: List[str], current_agent_text: str) -> str:
        parts = [m.strip() for m in messages if isinstance(m, str) and m.strip()]
        if current_agent_text.strip():
            parts.append(current_agent_text.strip())
        return "\n\n".join(parts).strip()

    @staticmethod
    def _consume_exec_event(
        evt: Dict[str, Any],
        messages: List[str],
        current_agent_text: str,
        current_agent_phase: Optional[str],
    ) -> Tuple[Optional[str], List[str], str, Optional[str], bool]:
        thread_id: Optional[str] = None
        changed = False
        event_type = str(evt.get("type") or "").strip().lower()

        if event_type == "thread.started":
            thread_id = str(evt.get("thread_id") or "").strip() or None
            if not thread_id:
                thread = evt.get("thread")
                if isinstance(thread, dict):
                    thread_id = str(thread.get("id") or "").strip() or None

        item = evt.get("item") if isinstance(evt.get("item"), dict) else {}
        item_type = str(item.get("type") or "").strip().lower()
        is_agent_item = item_type in ("agent_message", "assistant_message")
        event_phase = CodexRunner._extract_phase(item) or CodexRunner._extract_phase(evt)

        if event_type in ("item.delta", "response.output_text.delta", "assistant_message.delta", "message.delta"):
            delta = (
                CodexRunner._extract_text_fragment(evt.get("delta"))
                or CodexRunner._extract_text_fragment(evt.get("text_delta"))
                or CodexRunner._extract_text_fragment(evt.get("text"))
                or CodexRunner._extract_text_fragment(item.get("delta"))
                or CodexRunner._extract_text_fragment(item.get("text_delta"))
            )
            effective_phase = event_phase or current_agent_phase
            if delta and effective_phase != "commentary":
                if not current_agent_text:
                    current_agent_text = delta
                elif delta.startswith(current_agent_text):
                    current_agent_text = delta
                elif not current_agent_text.endswith(delta):
                    current_agent_text += delta
                current_agent_phase = effective_phase
                changed = True

        if event_type in ("item.updated", "item.completed") and is_agent_item:
            if event_phase == "commentary":
                if event_type == "item.completed":
                    current_agent_text = ""
                    current_agent_phase = None
                return thread_id, messages, current_agent_text, current_agent_phase, changed
            full_text = (
                CodexRunner._extract_text_fragment(item.get("text"))
                or CodexRunner._extract_text_fragment(item.get("content"))
                or CodexRunner._extract_text_fragment(item.get("message"))
            ).strip()
            if full_text:
                current_agent_text = full_text
                current_agent_phase = event_phase
                changed = True
            if event_type == "item.completed" and current_agent_text.strip():
                finalized = current_agent_text.strip()
                if not messages or messages[-1] != finalized:
                    messages.append(finalized)
                    changed = True
                current_agent_text = ""
                current_agent_phase = None

        if event_type in ("turn.completed", "response.completed", "thread.completed"):
            fallback_text = (
                CodexRunner._extract_text_fragment(evt.get("output_text"))
                or CodexRunner._extract_text_fragment(evt.get("text"))
            ).strip()
            if event_phase != "commentary" and fallback_text and (not messages or messages[-1] != fallback_text):
                messages.append(fallback_text)
                changed = True
            if current_agent_text.strip():
                finalized = current_agent_text.strip()
                if not messages or messages[-1] != finalized:
                    messages.append(finalized)
                    changed = True
                current_agent_text = ""
                current_agent_phase = None

        return thread_id, messages, current_agent_text, current_agent_phase, changed

    @staticmethod
    def _extract_text_fragment(node: Any) -> str:
        if node is None:
            return ""
        if isinstance(node, str):
            return node
        if isinstance(node, list):
            return "".join(CodexRunner._extract_text_fragment(x) for x in node)
        if isinstance(node, dict):
            for key in ("text", "delta", "text_delta", "content", "message", "output_text"):
                if key in node:
                    value = CodexRunner._extract_text_fragment(node.get(key))
                    if value:
                        return value
            return "".join(CodexRunner._extract_text_fragment(v) for v in node.values())
        return ""

    @staticmethod
    def _extract_phase(node: Any) -> Optional[str]:
        if not isinstance(node, dict):
            return None
        phase = str(node.get("phase") or "").strip().lower()
        if phase:
            return phase
        payload = node.get("payload")
        if isinstance(payload, dict):
            payload_phase = str(payload.get("phase") or "").strip().lower()
            if payload_phase:
                return payload_phase
        return None


def resolve_codex_bin(configured: Optional[str]) -> str:
    if configured:
        return configured
    found = shutil.which("codex")
    if found:
        return found
    app_path = "/Applications/Codex.app/Contents/Resources/codex"
    if Path(app_path).exists():
        return app_path
    return "codex"
