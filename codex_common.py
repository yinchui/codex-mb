#!/usr/bin/env python3
import json
import errno
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union


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


StateActor = Union[int, str]


class ProviderLookupError(RuntimeError):
    pass


SUPPORTED_ATTACHMENT_EXTENSIONS = {
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
    ".pdf": "file",
    ".md": "file",
    ".txt": "file",
    ".zip": "file",
}
ATTACHMENT_SEND_INTENT_PATTERNS = (
    "发给我",
    "把图片发我",
    "把文件发来",
    "作为附件发送",
)
ABSOLUTE_PATH_PATTERN = re.compile(r"(?<![A-Za-z0-9_./-])(/[^\s`<>\"'，。；;]+)")
ATTACHMENT_HINT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._-])([A-Za-z0-9][A-Za-z0-9._-]*\.(?:png|jpg|jpeg|webp|pdf|md|txt|zip))(?![A-Za-z0-9._-])",
    re.IGNORECASE,
)
SENSITIVE_HOME_DIR_NAMES = {".ssh", ".gnupg", ".aws"}
SENSITIVE_PATH_TOKENS = (
    ".env",
    "id_rsa",
    "id_ed25519",
    "keychain",
    "private_key",
    "secret",
    "token",
    "credential",
)


def _is_permission_shaped_os_error(exc: OSError) -> bool:
    if isinstance(exc, PermissionError):
        return True
    if getattr(exc, "errno", None) in (errno.EACCES, errno.EPERM):
        return True
    message = str(exc).strip().lower()
    if not message:
        return False
    return any(
        token in message
        for token in (
            "operation not permitted",
            "permission denied",
            "access denied",
        )
    )


def extract_local_attachment_candidates(text: str) -> List[Dict[str, str]]:
    value = str(text or "")
    candidates: List[Dict[str, str]] = []
    seen: Set[str] = set()
    for raw_match in ABSOLUTE_PATH_PATTERN.findall(value):
        raw_path = raw_match.rstrip(".,，。；;:)]}!?")
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            continue
        ext = path.suffix.lower()
        kind = SUPPORTED_ATTACHMENT_EXTENSIONS.get(ext)
        if not kind:
            continue
        try:
            exists = path.exists()
            is_file = path.is_file() if exists else False
        except OSError as exc:
            if _is_permission_shaped_os_error(exc):
                continue
            raise
        if not exists or not is_file:
            continue
        normalized = str(path)
        if normalized in seen:
            continue
        seen.add(normalized)
        candidates.append(
            {
                "path": normalized,
                "kind": kind,
                "name": path.name,
            }
        )
    return candidates


def is_attachment_send_intent(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    return any(token in value for token in ATTACHMENT_SEND_INTENT_PATTERNS)


def is_allowed_home_attachment_path(path: Path) -> Tuple[bool, Optional[str]]:
    candidate = Path(path).expanduser()
    ext = candidate.suffix.lower()
    if ext not in SUPPORTED_ATTACHMENT_EXTENSIONS:
        return False, "unsupported_type"

    home = Path.home().expanduser().resolve()
    try:
        resolved = candidate.resolve()
    except Exception:
        return False, "outside_home"

    try:
        resolved.relative_to(home)
    except ValueError:
        return False, "outside_home"

    lowered_parts = {part.lower() for part in resolved.parts}
    if SENSITIVE_HOME_DIR_NAMES & lowered_parts:
        return False, "sensitive_path"

    lowered_path = str(resolved).lower()
    if any(token in lowered_path for token in SENSITIVE_PATH_TOKENS):
        return False, "sensitive_path"

    return True, None


def extract_attachment_name_hints(text: str) -> List[str]:
    value = str(text or "")
    names: List[str] = []
    seen: Set[str] = set()
    for match in ATTACHMENT_HINT_PATTERN.findall(value):
        name = str(match or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


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
            if len(sessions) >= limit:
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

    def set_selected_model(self, user_id: StateActor, model: str) -> None:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            user_data["selected_model"] = str(model).strip() or None
            self._save_unlocked()

    def get_selected_model(self, user_id: StateActor) -> Optional[str]:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            value = str(user_data.get("selected_model") or "").strip()
            return value or None

    def set_pending_session_pick(self, user_id: StateActor, enabled: bool) -> None:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            user_data["pending_session_pick"] = bool(enabled)
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

    def set_model_picker(self, user_id: StateActor, models: List[str]) -> None:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            user_data["pending_model_pick"] = True
            user_data["model_picker"] = {
                "models": [str(model) for model in models],
            }
            self._save_unlocked()

    def is_pending_model_pick(self, user_id: StateActor) -> bool:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            return bool(user_data.get("pending_model_pick"))

    def get_model_picker(self, user_id: StateActor) -> Dict[str, Any]:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            picker = user_data.get("model_picker")
            return dict(picker) if isinstance(picker, dict) else {}

    def clear_model_picker(self, user_id: StateActor) -> None:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            user_data["pending_model_pick"] = False
            user_data.pop("model_picker", None)
            self._save_unlocked()

    def set_recent_attachments(self, user_id: StateActor, attachments: List[Dict[str, Any]]) -> None:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            normalized: List[Dict[str, str]] = []
            for item in attachments:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path") or "").strip()
                kind = str(item.get("kind") or "").strip()
                name = str(item.get("name") or "").strip()
                if not path or not kind or not name:
                    continue
                normalized.append(
                    {
                        "path": path,
                        "kind": kind,
                        "name": name,
                    }
                )
            user_data["recent_attachments"] = normalized
            self._save_unlocked()

    def get_recent_attachments(self, user_id: StateActor) -> List[Dict[str, str]]:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            values = user_data.get("recent_attachments")
            if not isinstance(values, list):
                return []
            result: List[Dict[str, str]] = []
            for item in values:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path") or "").strip()
                kind = str(item.get("kind") or "").strip()
                name = str(item.get("name") or "").strip()
                if not path or not kind or not name:
                    continue
                result.append({"path": path, "kind": kind, "name": name})
            return result

    def set_attachment_picker(self, user_id: StateActor, attachments: List[Dict[str, Any]]) -> None:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            normalized: List[Dict[str, str]] = []
            for item in attachments:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path") or "").strip()
                kind = str(item.get("kind") or "").strip()
                name = str(item.get("name") or "").strip()
                if not path or not kind or not name:
                    continue
                normalized.append({"path": path, "kind": kind, "name": name})
            if normalized:
                user_data["pending_attachment_pick"] = True
                user_data["attachment_picker"] = {"attachments": normalized}
            else:
                user_data["pending_attachment_pick"] = False
                user_data.pop("attachment_picker", None)
            self._save_unlocked()

    def is_pending_attachment_pick(self, user_id: StateActor) -> bool:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            return bool(user_data.get("pending_attachment_pick"))

    def get_attachment_picker(self, user_id: StateActor) -> Dict[str, Any]:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            picker = user_data.get("attachment_picker")
            return dict(picker) if isinstance(picker, dict) else {}

    def clear_attachment_picker(self, user_id: StateActor) -> None:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            user_data["pending_attachment_pick"] = False
            user_data.pop("attachment_picker", None)
            self._save_unlocked()


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
        model: Optional[str] = None,
        on_update: Optional[Callable[[str], None]] = None,
    ) -> Tuple[Optional[str], str, str, int]:
        config_flags: List[str] = []
        if self.dangerous_bypass_level == 1:
            sandbox_mode = self.sandbox_mode or "danger-full-access"
            approval_policy = self.approval_policy or "never"
            config_flags.extend(["-c", f"sandbox_mode={self._to_toml_string(sandbox_mode)}"])
            config_flags.extend(["-c", f"approval_policy={self._to_toml_string(approval_policy)}"])

        exec_flags: List[str] = ["--json", "--skip-git-repo-check"]
        normalized_model = str(model or "").strip()
        if normalized_model:
            exec_flags.extend(["-m", normalized_model])
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
                    evt_thread_id, messages, current_agent_text, changed = self._consume_exec_event(
                        evt,
                        messages,
                        current_agent_text,
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
        for line in stdout.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            evt_thread_id, messages, current_agent_text, _ = CodexRunner._consume_exec_event(
                evt,
                messages,
                current_agent_text,
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
    ) -> Tuple[Optional[str], List[str], str, bool]:
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

        if event_type in ("item.delta", "response.output_text.delta", "assistant_message.delta", "message.delta"):
            delta = (
                CodexRunner._extract_text_fragment(evt.get("delta"))
                or CodexRunner._extract_text_fragment(evt.get("text_delta"))
                or CodexRunner._extract_text_fragment(evt.get("text"))
                or CodexRunner._extract_text_fragment(item.get("delta"))
                or CodexRunner._extract_text_fragment(item.get("text_delta"))
            )
            if delta:
                if not current_agent_text:
                    current_agent_text = delta
                elif delta.startswith(current_agent_text):
                    current_agent_text = delta
                elif not current_agent_text.endswith(delta):
                    current_agent_text += delta
                changed = True

        if event_type in ("item.updated", "item.completed") and is_agent_item:
            full_text = (
                CodexRunner._extract_text_fragment(item.get("text"))
                or CodexRunner._extract_text_fragment(item.get("content"))
                or CodexRunner._extract_text_fragment(item.get("message"))
            ).strip()
            if full_text:
                current_agent_text = full_text
                changed = True
            if event_type == "item.completed" and current_agent_text.strip():
                finalized = current_agent_text.strip()
                if not messages or messages[-1] != finalized:
                    messages.append(finalized)
                    changed = True
                current_agent_text = ""

        if event_type in ("turn.completed", "response.completed", "thread.completed"):
            fallback_text = (
                CodexRunner._extract_text_fragment(evt.get("output_text"))
                or CodexRunner._extract_text_fragment(evt.get("text"))
            ).strip()
            if fallback_text and (not messages or messages[-1] != fallback_text):
                messages.append(fallback_text)
                changed = True
            if current_agent_text.strip():
                finalized = current_agent_text.strip()
                if not messages or messages[-1] != finalized:
                    messages.append(finalized)
                    changed = True
                current_agent_text = ""

        return thread_id, messages, current_agent_text, changed

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


def resolve_codex_config_path() -> Path:
    return Path("~/.codex/config.toml").expanduser()


def resolve_codex_auth_path() -> Path:
    return Path("~/.codex/auth.json").expanduser()


def _parse_toml_string(raw: str) -> str:
    value = (raw or "").strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return bytes(value[1:-1], "utf-8").decode("unicode_escape")
    return value


def load_codex_base_url(config_path: Optional[Path] = None) -> Optional[str]:
    target = (config_path or resolve_codex_config_path()).expanduser()
    if not target.exists():
        return None
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except Exception:
        return None

    active_provider: Optional[str] = None
    current_section: Optional[str] = None
    provider_base_urls: Dict[str, str] = {}

    for raw_line in lines:
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current_section = line[1:-1].strip()
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not current_section and key == "model_provider":
            active_provider = _parse_toml_string(value)
            continue
        if not current_section or not current_section.startswith("model_providers."):
            continue
        provider_name = current_section.split(".", 1)[1].strip()
        if key == "base_url":
            provider_base_urls[provider_name] = _parse_toml_string(value).rstrip("/")

    if not active_provider:
        return None
    return provider_base_urls.get(active_provider)


def load_codex_default_model(config_path: Optional[Path] = None) -> Optional[str]:
    target = (config_path or resolve_codex_config_path()).expanduser()
    if not target.exists():
        return None
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except Exception:
        return None
    current_section: Optional[str] = None
    for raw_line in lines:
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current_section = line[1:-1].strip()
            continue
        if current_section:
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "model":
            model = _parse_toml_string(value)
            return model or None
    return None


def load_codex_api_key(auth_path: Optional[Path] = None) -> Optional[str]:
    target = (auth_path or resolve_codex_auth_path()).expanduser()
    if not target.exists():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    api_key = str(payload.get("OPENAI_API_KEY") or "").strip()
    return api_key or None


def _resolve_provider_origin(base_url: str) -> str:
    parsed = urllib.parse.urlsplit((base_url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        raise ProviderLookupError("本地 Codex provider base_url 无效。")
    return f"{parsed.scheme}://{parsed.netloc}"


def _provider_json_get(url: str, api_key: str) -> Dict[str, Any]:
    req = urllib.request.Request(
        url=url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "ignore")
        except Exception:
            body = ""
        raise ProviderLookupError(
            f"provider 请求失败: HTTP {e.code}" + (f" {body[:200]}" if body else "")
        ) from e
    except urllib.error.URLError as e:
        raise ProviderLookupError(f"provider 网络请求失败: {e}") from e
    except json.JSONDecodeError as e:
        raise ProviderLookupError("provider 返回的不是有效 JSON。") from e
    if not isinstance(payload, dict):
        raise ProviderLookupError("provider 返回了意外数据。")
    return payload


def list_provider_models(
    *,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    config_path: Optional[Path] = None,
    auth_path: Optional[Path] = None,
) -> List[str]:
    resolved_base_url = (base_url or load_codex_base_url(config_path=config_path) or "").strip().rstrip("/")
    if not resolved_base_url:
        raise ProviderLookupError("未找到本地 Codex provider base_url。")
    resolved_api_key = (api_key or load_codex_api_key(auth_path=auth_path) or "").strip()
    if not resolved_api_key:
        raise ProviderLookupError("未找到本地 Codex API key。")

    payload = _provider_json_get(f"{resolved_base_url}/v1/models", resolved_api_key)
    data = payload.get("data")
    if not isinstance(data, list):
        raise ProviderLookupError("provider 模型列表格式不正确。")

    models: List[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        if model_id:
            models.append(model_id)
    if not models:
        raise ProviderLookupError("provider 没有返回可用模型。")
    return models


def fetch_provider_account_info(
    *,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    config_path: Optional[Path] = None,
    auth_path: Optional[Path] = None,
) -> Dict[str, Any]:
    resolved_base_url = (base_url or load_codex_base_url(config_path=config_path) or "").strip().rstrip("/")
    if not resolved_base_url:
        raise ProviderLookupError("未找到本地 Codex provider base_url。")
    resolved_api_key = (api_key or load_codex_api_key(auth_path=auth_path) or "").strip()
    if not resolved_api_key:
        raise ProviderLookupError("未找到本地 Codex API key。")

    origin = _resolve_provider_origin(resolved_base_url)
    payload = _provider_json_get(f"{origin}/user/api/v1/me", resolved_api_key)
    quota = payload.get("quota")
    usage = payload.get("usage")
    if not isinstance(quota, dict) or not isinstance(usage, dict):
        raise ProviderLookupError("provider 账户额度格式不正确。")
    return payload
