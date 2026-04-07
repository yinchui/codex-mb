#!/usr/bin/env python3
import json
import mimetypes
import os
import signal
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from codex_common import (
    BotState,
    CodexRunner,
    RunningPromptRegistry,
    SessionStore,
    chunk_text,
    env,
    fetch_provider_account_info,
    group_sessions_by_workspace,
    list_provider_models,
    log,
    load_codex_default_model,
    parse_bool_env,
    parse_dangerous_bypass_level,
    parse_non_negative_int,
    resolve_codex_bin,
)


MAX_TELEGRAM_TEXT = 4096
BOT_COMMANDS: List[Dict[str, str]] = [
    {"command": "start", "description": "开始使用"},
    {"command": "help", "description": "查看帮助"},
    {"command": "model", "description": "切换模型"},
    {"command": "account", "description": "查看额度"},
    {"command": "sessions", "description": "查看最近会话"},
    {"command": "use", "description": "切换会话"},
    {"command": "history", "description": "查看会话历史"},
    {"command": "new", "description": "新建会话模式"},
    {"command": "status", "description": "查看当前会话"},
    {"command": "ask", "description": "在当前会话提问"},
]

def parse_allowed_user_ids(raw: Optional[str]) -> Optional[Set[int]]:
    if not raw:
        return None
    result: Set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.add(int(part))
        except ValueError:
            raise ValueError(f"invalid user id in ALLOWED_TELEGRAM_USER_IDS: {part}")
    return result

class TelegramAPI:
    def __init__(
        self,
        token: str,
        ca_bundle: Optional[str] = None,
        insecure_skip_verify: bool = False,
    ):
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.file_base_url = f"https://api.telegram.org/file/bot{token}"
        self.ssl_context: Optional[ssl.SSLContext] = None
        if insecure_skip_verify:
            self.ssl_context = ssl._create_unverified_context()
        elif ca_bundle:
            self.ssl_context = ssl.create_default_context(cafile=ca_bundle)

    def _request(self, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url=f"{self.base_url}/{method}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=80, context=self.ssl_context) as resp:
            raw = resp.read().decode("utf-8")
        parsed = json.loads(raw)
        if not parsed.get("ok"):
            raise RuntimeError(f"telegram api error for {method}: {raw}")
        return parsed["result"]

    def get_updates(self, offset: Optional[int], timeout: int = 30) -> List[Dict[str, Any]]:
        payload: Dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        return self._request("getUpdates", payload)

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to: Optional[int] = None,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> None:
        for part in chunk_text(text, size=min(3800, MAX_TELEGRAM_TEXT)):
            self.send_message_with_result(
                chat_id=chat_id,
                text=part,
                reply_to=reply_to,
                reply_markup=reply_markup,
            )

    def send_message_with_result(
        self,
        chat_id: int,
        text: str,
        reply_to: Optional[int] = None,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_to is not None:
            payload["reply_to_message_id"] = reply_to
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self._request("sendMessage", payload)

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
    ) -> None:
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        self._request("editMessageText", payload)

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        self._request("sendChatAction", {"chat_id": chat_id, "action": action})

    def set_my_commands(self, commands: List[Dict[str, str]]) -> None:
        self._request("setMyCommands", {"commands": commands})

    def set_chat_menu_button_commands(self) -> None:
        self._request("setChatMenuButton", {"menu_button": {"type": "commands"}})

    def answer_callback_query(
        self,
        callback_query_id: str,
        text: Optional[str] = None,
        show_alert: bool = False,
    ) -> None:
        payload: Dict[str, Any] = {
            "callback_query_id": callback_query_id,
            "show_alert": show_alert,
        }
        if text:
            payload["text"] = text
        self._request("answerCallbackQuery", payload)

    def get_file(self, file_id: str) -> Dict[str, Any]:
        return self._request("getFile", {"file_id": file_id})

    def download_file_bytes(self, file_path: str) -> bytes:
        quoted_path = urllib.parse.quote(file_path.lstrip("/"), safe="/")
        req = urllib.request.Request(url=f"{self.file_base_url}/{quoted_path}", method="GET")
        with urllib.request.urlopen(req, timeout=120, context=self.ssl_context) as resp:
            return resp.read()


def normalize_audio_filename(file_name: Optional[str], mime_type: Optional[str]) -> Tuple[str, str]:
    name = (file_name or "").strip() or "telegram-voice.ogg"
    suffix = Path(name).suffix.lower()
    if suffix == ".oga":
        name = f"{Path(name).stem}.ogg"
        suffix = ".ogg"
    if not suffix:
        guessed_suffix = mimetypes.guess_extension(mime_type or "") or ".ogg"
        if guessed_suffix == ".oga":
            guessed_suffix = ".ogg"
        name = f"{name}{guessed_suffix}"
    content_type = mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
    if content_type == "audio/x-wav":
        content_type = "audio/wav"
    return name, content_type


def fetch_telegram_audio(
    api: TelegramAPI,
    *,
    file_id: str,
    file_name: Optional[str],
    mime_type: Optional[str],
    file_size: Optional[int],
    max_bytes: int,
) -> Tuple[bytes, str, str]:
    if file_size and file_size > max_bytes:
        raise RuntimeError(f"语音文件过大（{file_size} bytes），超过当前限制 {max_bytes} bytes。")

    file_meta = api.get_file(file_id)
    file_path = str(file_meta.get("file_path") or "").strip()
    if not file_path:
        raise RuntimeError("Telegram 未返回可下载的 file_path。")

    audio_bytes = api.download_file_bytes(file_path)
    if not audio_bytes:
        raise RuntimeError("下载到的语音文件为空。")
    if len(audio_bytes) > max_bytes:
        raise RuntimeError(f"语音文件过大（{len(audio_bytes)} bytes），超过当前限制 {max_bytes} bytes。")

    normalized_name, content_type = normalize_audio_filename(
        file_name or Path(file_path).name,
        mime_type,
    )
    return audio_bytes, normalized_name, content_type


class AudioTranscriber:
    def transcribe_telegram_audio(
        self,
        api: TelegramAPI,
        *,
        file_id: str,
        file_name: Optional[str],
        mime_type: Optional[str],
        file_size: Optional[int],
    ) -> str:
        raise NotImplementedError


class OpenAIAudioTranscriber(AudioTranscriber):
    def __init__(
        self,
        api_key: str,
        model: str,
        api_base: str = "https://api.openai.com/v1",
        timeout_sec: int = 180,
        max_bytes: int = 25 * 1024 * 1024,
    ):
        self.api_key = api_key
        self.model = model
        self.api_base = api_base.rstrip("/")
        self.timeout_sec = max(30, int(timeout_sec))
        self.max_bytes = max(1, int(max_bytes))

    @staticmethod
    def _build_multipart_body(
        *,
        fields: Dict[str, str],
        file_field: str,
        filename: str,
        content: bytes,
        content_type: str,
    ) -> Tuple[bytes, str]:
        boundary = f"----CodexTgBoundary{uuid.uuid4().hex}"
        body = bytearray()
        for key, value in fields.items():
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode("utf-8")
            )
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            (
                f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8")
        )
        body.extend(content)
        body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode("utf-8"))
        return bytes(body), boundary

    def transcribe_telegram_audio(
        self,
        api: TelegramAPI,
        *,
        file_id: str,
        file_name: Optional[str],
        mime_type: Optional[str],
        file_size: Optional[int],
    ) -> str:
        audio_bytes, normalized_name, content_type = fetch_telegram_audio(
            api,
            file_id=file_id,
            file_name=file_name,
            mime_type=mime_type,
            file_size=file_size,
            max_bytes=self.max_bytes,
        )
        body, boundary = self._build_multipart_body(
            fields={"model": self.model},
            file_field="file",
            filename=normalized_name,
            content=audio_bytes,
            content_type=content_type,
        )
        req = urllib.request.Request(
            url=f"{self.api_base}/audio/transcriptions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"转写请求失败: HTTP {e.code} {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"转写请求失败: {e}") from e

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError("转写接口返回了无法解析的响应。") from e

        text = str(parsed.get("text") or "").strip()
        if not text:
            raise RuntimeError("转写成功，但没有返回文本。")
        return text


class LocalWhisperAudioTranscriber(AudioTranscriber):
    def __init__(
        self,
        model_name: str,
        ffmpeg_bin: Optional[str] = None,
        device: Optional[str] = None,
        language: Optional[str] = None,
        max_bytes: int = 25 * 1024 * 1024,
    ):
        self.model_name = model_name
        self.ffmpeg_bin = ffmpeg_bin
        self.device = device
        self.language = language
        self.max_bytes = max(1, int(max_bytes))
        self._model = None
        self._lock = threading.Lock()

    def validate_environment(self) -> None:
        try:
            import whisper  # noqa: F401
        except Exception as e:
            raise RuntimeError("本地转写需要安装 whisper Python 包。") from e
        self._resolve_ffmpeg_bin()

    def _resolve_ffmpeg_bin(self) -> str:
        configured = (self.ffmpeg_bin or "").strip()
        if configured:
            if Path(configured).exists():
                return configured
            raise RuntimeError(f"找不到 ffmpeg: {configured}")
        found = shutil.which("ffmpeg")
        if found:
            return found
        try:
            import imageio_ffmpeg

            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception as e:
            raise RuntimeError("本地转写需要 ffmpeg，可安装系统 ffmpeg 或提供 TG_VOICE_FFMPEG_BIN。") from e

    def _load_model(self):
        with self._lock:
            if self._model is not None:
                return self._model
            try:
                import whisper
            except Exception as e:
                raise RuntimeError("本地转写需要安装 whisper Python 包。") from e
            try:
                self._model = whisper.load_model(self.model_name, device=self.device)
            except Exception as e:
                raise RuntimeError(f"加载本地 Whisper 模型失败: {e}") from e
            return self._model

    def _decode_audio(self, file_path: str):
        ffmpeg_bin = self._resolve_ffmpeg_bin()
        try:
            import numpy as np
            import whisper.audio as whisper_audio
        except Exception as e:
            raise RuntimeError("本地转写缺少 numpy/whisper 依赖。") from e

        cmd = [
            ffmpeg_bin,
            "-nostdin",
            "-threads",
            "0",
            "-i",
            file_path,
            "-f",
            "s16le",
            "-ac",
            "1",
            "-acodec",
            "pcm_s16le",
            "-ar",
            str(whisper_audio.SAMPLE_RATE),
            "-",
        ]
        try:
            out = subprocess.run(cmd, capture_output=True, check=True).stdout
        except subprocess.CalledProcessError as e:
            detail = e.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"ffmpeg 解码失败: {detail}") from e
        return np.frombuffer(out, np.int16).flatten().astype("float32") / 32768.0

    def transcribe_telegram_audio(
        self,
        api: TelegramAPI,
        *,
        file_id: str,
        file_name: Optional[str],
        mime_type: Optional[str],
        file_size: Optional[int],
    ) -> str:
        audio_bytes, normalized_name, _ = fetch_telegram_audio(
            api,
            file_id=file_id,
            file_name=file_name,
            mime_type=mime_type,
            file_size=file_size,
            max_bytes=self.max_bytes,
        )
        suffix = Path(normalized_name).suffix or ".ogg"
        model = self._load_model()
        with tempfile.NamedTemporaryFile(prefix="codex-tg-voice-", suffix=suffix, delete=True) as tmp:
            tmp.write(audio_bytes)
            tmp.flush()
            audio = self._decode_audio(tmp.name)
        try:
            result = model.transcribe(
                audio,
                language=self.language or None,
                fp16=False,
                verbose=False,
            )
        except Exception as e:
            raise RuntimeError(f"本地 Whisper 转写失败: {e}") from e
        text = str((result or {}).get("text") or "").strip()
        if not text:
            raise RuntimeError("本地 Whisper 没有返回文本。")
        return text


class TypingStatus:
    def __init__(self, api: TelegramAPI, chat_id: int, interval_sec: float = 4.0):
        self.api = api
        self.chat_id = chat_id
        self.interval_sec = interval_sec
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=0.5)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.api.send_chat_action(self.chat_id, "typing")
            except Exception:
                pass
            self._stop_event.wait(self.interval_sec)


class TgCodexService:
    def __init__(
        self,
        api: TelegramAPI,
        sessions: SessionStore,
        state: BotState,
        codex: CodexRunner,
        audio_transcriber: Optional[AudioTranscriber],
        default_cwd: Path,
        allowed_user_ids: Optional[Set[int]],
        stream_enabled: bool,
        stream_edit_interval_ms: int,
        stream_min_delta_chars: int,
        thinking_status_interval_ms: int,
    ):
        self.api = api
        self.sessions = sessions
        self.state = state
        self.codex = codex
        self.audio_transcriber = audio_transcriber
        self.default_cwd = default_cwd
        self.allowed_user_ids = allowed_user_ids
        self.stream_enabled = stream_enabled
        self.stream_edit_interval_ms = max(200, stream_edit_interval_ms)
        self.stream_min_delta_chars = max(1, stream_min_delta_chars)
        self.thinking_status_interval_ms = max(400, thinking_status_interval_ms)
        self.running_prompts = RunningPromptRegistry()
        self.offset: Optional[int] = None

    def run_forever(self) -> None:
        while True:
            try:
                updates = self.api.get_updates(self.offset, timeout=30)
                for update in updates:
                    self.offset = update["update_id"] + 1
                    self._handle_update(update)
            except urllib.error.URLError as e:
                print(f"[warn] telegram network error: {e}", file=sys.stderr)
                time.sleep(2)
            except Exception as e:
                print(f"[warn] loop error: {e}", file=sys.stderr)
                traceback.print_exc()
                time.sleep(2)

    def setup_bot_menu(self) -> None:
        self.api.set_my_commands(BOT_COMMANDS)
        try:
            self.api.set_chat_menu_button_commands()
        except Exception:
            # Non-critical; setMyCommands already provides slash-menu commands.
            pass

    def _handle_update(self, update: Dict[str, Any]) -> None:
        callback_query = update.get("callback_query")
        if callback_query:
            self._handle_callback_query(callback_query)
            return

        msg = update.get("message")
        if not msg:
            return
        text = (msg.get("text") or "").strip()
        caption = (msg.get("caption") or "").strip()
        voice = msg.get("voice") if isinstance(msg.get("voice"), dict) else None
        audio = msg.get("audio") if isinstance(msg.get("audio"), dict) else None

        chat_id = msg["chat"]["id"]
        message_id = msg["message_id"]
        user = msg.get("from") or {}
        user_id = user.get("id")

        if user_id is None:
            return
        log(
            f"update received: user_id={user_id} chat_id={chat_id} "
            f"text={text[:80]!r} voice={bool(voice)} audio={bool(audio)}"
        )

        if self.allowed_user_ids is not None and int(user_id) not in self.allowed_user_ids:
            log(f"blocked by allowlist: user_id={user_id}")
            self.api.send_message(chat_id, "没有权限使用这个 bot。", reply_to=message_id)
            return

        if not text:
            if voice:
                self._handle_audio_message(
                    chat_id=chat_id,
                    reply_to=message_id,
                    user_id=int(user_id),
                    media=voice,
                    caption=caption,
                    kind="voice",
                )
            elif audio:
                self._handle_audio_message(
                    chat_id=chat_id,
                    reply_to=message_id,
                    user_id=int(user_id),
                    media=audio,
                    caption=caption,
                    kind="audio",
                )
            return
        if not text.startswith("/"):
            if self._try_handle_quick_model_pick(chat_id, message_id, int(user_id), text):
                return
            if self._try_handle_quick_workspace_pick(chat_id, message_id, int(user_id), text):
                return
            if self._try_handle_quick_session_pick(chat_id, message_id, int(user_id), text):
                return
            self.state.clear_model_picker(int(user_id))
            self._clear_session_pickers(int(user_id))
            self._handle_chat_message(chat_id, message_id, int(user_id), text)
            return

        cmd, arg = self._parse_command(text)
        log(f"command: /{cmd} arg={arg[:80]!r}")
        if cmd != "model":
            self.state.clear_model_picker(int(user_id))
        if cmd != "sessions":
            self._clear_session_pickers(int(user_id))
        if cmd in ("start", "help"):
            self._send_help(chat_id, message_id)
            return
        if cmd == "model":
            self._handle_model(chat_id, message_id, int(user_id), arg)
            return
        if cmd == "account":
            self._handle_account(chat_id, message_id)
            return
        if cmd == "sessions":
            self._handle_sessions(chat_id, message_id, arg, int(user_id))
            return
        if cmd == "use":
            self._handle_use(chat_id, message_id, int(user_id), arg)
            return
        if cmd == "status":
            self._handle_status(chat_id, message_id, int(user_id))
            return
        if cmd == "new":
            self._handle_new(chat_id, message_id, int(user_id), arg)
            return
        if cmd == "history":
            self._handle_history(chat_id, message_id, int(user_id), arg)
            return
        if cmd == "ask":
            self._handle_ask(chat_id, message_id, int(user_id), arg)
            return

        self.api.send_message(chat_id, f"未知命令: /{cmd}\n发送 /help 查看说明。", reply_to=message_id)

    def _handle_callback_query(self, callback_query: Dict[str, Any]) -> None:
        cq_id = callback_query.get("id")
        data = (callback_query.get("data") or "").strip()
        msg = callback_query.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        reply_to = msg.get("message_id")
        user = callback_query.get("from") or {}
        user_id = user.get("id")

        if not cq_id or user_id is None:
            return
        if self.allowed_user_ids is not None and int(user_id) not in self.allowed_user_ids:
            self.api.answer_callback_query(cq_id, text="没有权限。", show_alert=True)
            return
        if not isinstance(chat_id, int):
            self.api.answer_callback_query(cq_id, text="无法解析聊天上下文。", show_alert=True)
            return

        if data.startswith("workspace:"):
            raw_index = data[10:]
            self.api.answer_callback_query(cq_id, text="正在打开工作区...")
            self._handle_workspace_callback(chat_id, reply_to, int(user_id), raw_index)
            return

        if data.startswith("session_pick:"):
            raw_index = data[13:]
            self.api.answer_callback_query(cq_id, text="正在处理选择...")
            self._handle_session_pick_callback(chat_id, reply_to, int(user_id), raw_index)
            return

        if data.startswith("use:"):
            session_id = data[4:]
            self.api.answer_callback_query(cq_id, text="正在切换会话...")
            self._switch_to_session(chat_id, reply_to, int(user_id), session_id)
            return

        self.api.answer_callback_query(cq_id, text="不支持的操作。", show_alert=True)

    @staticmethod
    def _parse_command(text: str) -> Tuple[str, str]:
        parts = text.split(" ", 1)
        cmd = parts[0][1:]
        cmd = cmd.split("@", 1)[0].strip().lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        return cmd, arg

    def _send_help(self, chat_id: int, reply_to: int) -> None:
        self.api.send_message(
            chat_id,
            "\n".join(
                [
                    "可用命令:",
                    "/model - 查看可切换模型，并用编号切换",
                    "/account - 查看今天剩余 API 额度",
                    "/sessions [N] - 先选工作区，再选最近会话",
                    "/use <编号|session_id> - 切换当前会话",
                    "/history [编号|session_id] [N] - 查看会话最近 N 条消息",
                    "/new [cwd] - 进入新会话模式（下一条普通消息会新建 session）",
                    "/status - 查看当前绑定会话",
                    "/ask <内容> - 手动提问（可选）",
                    "执行 /model 后，可直接发送编号切换模型",
                    "执行 /sessions 后，先选工作区，再选会话",
                    "执行 /sessions 后，也可点击按钮逐级切换",
                    "后台执行时仍可发送 /use /sessions /status",
                    "直接发普通消息即可对话（会自动续聊当前 session）",
                    "已配置转写时，也可直接发送 Telegram 语音或音频消息",
                ]
            ),
            reply_to=reply_to,
        )

    def _clear_session_pickers(self, user_id: int) -> None:
        self.state.clear_workspace_picker(user_id)
        self.state.clear_session_picker(user_id)

    def _handle_sessions(self, chat_id: int, reply_to: int, arg: str, user_id: int) -> None:
        limit = 10
        if arg:
            try:
                limit = max(1, min(30, int(arg)))
            except ValueError:
                self.api.send_message(chat_id, "参数错误，示例: /sessions 10", reply_to=reply_to)
                return
        items = self.sessions.list_recent(limit=limit)
        if not items:
            self.api.send_message(chat_id, "未找到本地会话记录。", reply_to=reply_to)
            return
        workspaces = group_sessions_by_workspace(items)
        lines = ["最近工作区（先选工作区，再选会话）:"]
        keyboard_rows: List[List[Dict[str, str]]] = []
        for i, workspace in enumerate(workspaces, start=1):
            count = len(workspace["session_ids"])
            lines.append(f"{i}. {workspace['label']} | {count} 条会话")
            keyboard_rows.append(
                [
                    {
                        "text": f"工作区 {i}",
                        "callback_data": f"workspace:{i}",
                    }
                ]
            )
        lines.append("先发送工作区编号（例如发送: 1）")
        self.api.send_message(
            chat_id,
            "\n".join(lines),
            reply_to=reply_to,
            reply_markup={"inline_keyboard": keyboard_rows},
        )
        self.state.set_last_session_ids(user_id, [])
        self._clear_session_pickers(user_id)
        self.state.set_workspace_picker(user_id, workspaces)

    def _show_workspace_sessions(self, chat_id: int, reply_to: int, user_id: int, workspace: Dict[str, Any]) -> None:
        session_ids = workspace.get("session_ids")
        if not isinstance(session_ids, list):
            self.state.set_last_session_ids(user_id, [])
            self.state.clear_session_picker(user_id)
            self.api.send_message(chat_id, "工作区列表已失效，请重新发送 /sessions。", reply_to=reply_to)
            return
        cwd = str(workspace.get("cwd") or "").strip()
        label = str(workspace.get("label") or "当前工作区")
        lines = [f"最近会话（工作区: {label}）:"]
        available_session_ids: List[str] = []
        keyboard_rows: List[List[Dict[str, str]]] = []
        picker_options: List[Dict[str, str]] = []
        if cwd:
            lines.append("1. 新建会话")
            picker_options.append({"kind": "new", "cwd": cwd})
            keyboard_rows.append(
                [
                    {
                        "text": "新建会话",
                        "callback_data": "session_pick:1",
                    }
                ]
            )
        for raw_session_id in session_ids:
            session_id = str(raw_session_id or "").strip()
            if not session_id:
                continue
            meta = self.sessions.find_by_id(session_id)
            if not meta:
                continue
            available_session_ids.append(meta.session_id)
            picker_options.append(
                {
                    "kind": "session",
                    "cwd": meta.cwd,
                    "session_id": meta.session_id,
                }
            )
            short_id = meta.session_id[:8]
            index = len(picker_options)
            lines.append(f"{index}. {meta.title} | {short_id}")
            keyboard_rows.append(
                [
                    {
                        "text": f"会话 {index}",
                        "callback_data": f"session_pick:{index}",
                    }
                ]
            )
        if not picker_options:
            self.state.set_last_session_ids(user_id, [])
            self.state.clear_session_picker(user_id)
            self.api.send_message(chat_id, "该工作区下没有可用会话了，请重新发送 /sessions。", reply_to=reply_to)
            return
        lines.append("再发送会话编号即可切换（例如发送: 1）")
        self.api.send_message(
            chat_id,
            "\n".join(lines),
            reply_to=reply_to,
            reply_markup={"inline_keyboard": keyboard_rows},
        )
        self.state.set_last_session_ids(user_id, available_session_ids)
        self.state.set_session_picker(user_id, picker_options)

    def _handle_workspace_callback(self, chat_id: int, reply_to: int, user_id: int, raw_index: str) -> None:
        if not raw_index.isdigit():
            self.api.send_message(chat_id, "工作区编号无效。请发送 /sessions 重新查看列表。", reply_to=reply_to)
            return
        self._open_workspace_sessions(chat_id, reply_to, user_id, int(raw_index))

    def _handle_session_pick_callback(self, chat_id: int, reply_to: int, user_id: int, raw_index: str) -> None:
        if not raw_index.isdigit():
            self.api.send_message(chat_id, "编号无效。请发送 /sessions 重新查看列表。", reply_to=reply_to)
            return
        self._open_session_picker_option(chat_id, reply_to, user_id, int(raw_index))

    def _open_workspace_sessions(self, chat_id: int, reply_to: int, user_id: int, idx: int) -> bool:
        picker = self.state.get_workspace_picker(user_id)
        workspaces = picker.get("workspaces")
        if not isinstance(workspaces, list) or idx <= 0 or idx > len(workspaces):
            self.api.send_message(chat_id, "工作区编号无效。请发送 /sessions 重新查看列表。", reply_to=reply_to)
            return False
        self.state.clear_workspace_picker(user_id)
        self._show_workspace_sessions(chat_id, reply_to, user_id, workspaces[idx - 1])
        return True

    def _open_session_picker_option(self, chat_id: int, reply_to: int, user_id: int, idx: int) -> bool:
        picker = self.state.get_session_picker(user_id)
        options = picker.get("options")
        if isinstance(options, list) and options:
            if idx <= 0 or idx > len(options):
                self.api.send_message(chat_id, "编号无效。请发送 /sessions 重新查看列表。", reply_to=reply_to)
                return False
            option = options[idx - 1]
            kind = str(option.get("kind") or "").strip()
            if kind == "new":
                cwd = str(option.get("cwd") or "").strip()
                self._handle_new(chat_id, reply_to, user_id, cwd)
                return True
            session_id = str(option.get("session_id") or "").strip()
            if not session_id:
                self.api.send_message(chat_id, "编号无效。请发送 /sessions 重新查看列表。", reply_to=reply_to)
                return False
            self._switch_to_session(chat_id, reply_to, user_id, session_id)
            return True
        recent_ids = self.state.get_last_session_ids(user_id)
        if idx <= 0 or idx > len(recent_ids):
            self.api.send_message(chat_id, "编号无效。请发送 /sessions 重新查看列表。", reply_to=reply_to)
            return False
        self._switch_to_session(chat_id, reply_to, user_id, recent_ids[idx - 1])
        return True

    def _handle_use(self, chat_id: int, reply_to: int, user_id: int, arg: str) -> None:
        selector = arg.strip()
        if not selector:
            self.api.send_message(chat_id, "示例: /use 1 或 /use <session_id>", reply_to=reply_to)
            return
        session_id, err = self._resolve_session_selector(user_id, selector)
        if err:
            self.api.send_message(chat_id, err, reply_to=reply_to)
            return
        if not session_id:
            self.api.send_message(chat_id, "无效的会话选择参数。", reply_to=reply_to)
            return
        self._switch_to_session(chat_id, reply_to, user_id, session_id)

    def _switch_to_session(self, chat_id: int, reply_to: int, user_id: int, session_id: str) -> None:
        meta = self.sessions.find_by_id(session_id)
        if not meta:
            self.api.send_message(chat_id, f"未找到 session: {session_id}", reply_to=reply_to)
            return
        self.state.set_active_session(user_id, meta.session_id, meta.cwd)
        self._clear_session_pickers(user_id)
        self.api.send_message(
            chat_id,
            f"已切换到:\n{meta.title}\nsession: {meta.session_id}\ncwd: {meta.cwd}\n现在可直接发消息对话。",
            reply_to=reply_to,
        )

    def _try_handle_quick_workspace_pick(self, chat_id: int, reply_to: int, user_id: int, text: str) -> bool:
        if not self.state.is_pending_workspace_pick(user_id):
            return False
        raw = text.strip()
        if not raw.isdigit():
            return False
        return self._open_workspace_sessions(chat_id, reply_to, user_id, int(raw))

    def _try_handle_quick_session_pick(self, chat_id: int, reply_to: int, user_id: int, text: str) -> bool:
        if not self.state.is_pending_session_pick(user_id):
            return False
        raw = text.strip()
        if not raw.isdigit():
            return False
        return self._open_session_picker_option(chat_id, reply_to, user_id, int(raw))

    def _handle_model(self, chat_id: int, reply_to: int, user_id: int, arg: str) -> None:
        if arg.strip():
            self.api.send_message(chat_id, "当前 /model 不需要参数，直接发送 /model 即可。", reply_to=reply_to)
            return
        models = list_provider_models()
        current_model = self.state.get_selected_model(user_id) or load_codex_default_model() or models[0]
        lines = [
            f"当前模型: {current_model}",
            "可切换模型（回复编号即可切换）:",
        ]
        for idx, model in enumerate(models, start=1):
            marker = " (当前)" if model == current_model else ""
            lines.append(f"{idx}. {model}{marker}")
        self.api.send_message(chat_id, "\n".join(lines), reply_to=reply_to)
        self.state.clear_model_picker(user_id)
        self._clear_session_pickers(user_id)
        self.state.set_model_picker(user_id, models)

    def _try_handle_quick_model_pick(self, chat_id: int, reply_to: int, user_id: int, text: str) -> bool:
        if not self.state.is_pending_model_pick(user_id):
            return False
        raw = text.strip()
        if not raw.isdigit():
            return False
        idx = int(raw)
        picker = self.state.get_model_picker(user_id)
        models = picker.get("models")
        if not isinstance(models, list) or idx <= 0 or idx > len(models):
            self.api.send_message(
                chat_id,
                "模型编号无效。请发送 /model 重新查看列表。",
                reply_to=reply_to,
            )
            return True
        selected_model = str(models[idx - 1])
        self.state.set_selected_model(user_id, selected_model)
        self.state.clear_model_picker(user_id)
        self.api.send_message(chat_id, f"已切换模型为: {selected_model}", reply_to=reply_to)
        return True

    def _handle_account(self, chat_id: int, reply_to: int) -> None:
        payload = fetch_provider_account_info()
        quota = payload.get("quota") if isinstance(payload.get("quota"), dict) else {}
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        lines = [
            "账户额度:",
            f"服务: {payload.get('sub_service_type_name') or payload.get('service_type') or 'unknown'}",
            f"计费方式: {payload.get('billing_type') or 'unknown'}",
            f"今日总额度: {quota.get('daily_quota', '-')}",
            f"今日已花: {quota.get('daily_spent', '-')}",
            f"今日剩余: {quota.get('daily_remaining', '-')}",
            f"今日请求数: {usage.get('daily_request_count', '-')}",
            f"重置时间: {quota.get('next_reset_at', '-')}",
        ]
        self.api.send_message(chat_id, "\n".join(lines), reply_to=reply_to)

    def _handle_history(self, chat_id: int, reply_to: int, user_id: int, arg: str) -> None:
        tokens = [x for x in arg.split() if x]
        limit = 10
        session_id: Optional[str] = None

        if not tokens:
            session_id, _ = self.state.get_active(user_id)
            if not session_id:
                self.api.send_message(
                    chat_id,
                    "当前无 active session。先 /use 选择会话，或直接对话后再查看历史。",
                    reply_to=reply_to,
                )
                return
        else:
            session_id, err = self._resolve_session_selector(user_id, tokens[0])
            if err:
                self.api.send_message(chat_id, err, reply_to=reply_to)
                return
            if not session_id:
                self.api.send_message(chat_id, "无效的会话选择参数。", reply_to=reply_to)
                return
            if len(tokens) >= 2:
                try:
                    limit = int(tokens[1])
                except ValueError:
                    self.api.send_message(chat_id, "N 必须是数字，示例: /history 1 20", reply_to=reply_to)
                    return

        limit = max(1, min(50, limit))
        meta, messages = self.sessions.get_history(session_id, limit=limit)
        if not meta:
            self.api.send_message(chat_id, f"未找到 session: {session_id}", reply_to=reply_to)
            return
        if not messages:
            self.api.send_message(chat_id, "该会话暂无可展示历史消息。", reply_to=reply_to)
            return

        lines = [
            f"会话历史: {meta.title}",
            f"session: {meta.session_id}",
            f"显示最近 {len(messages)} 条消息:",
        ]
        for i, (role, message) in enumerate(messages, start=1):
            role_zh = "用户" if role == "user" else "助手"
            lines.append(f"{i}. [{role_zh}] {SessionStore.compact_message(message)}")
        self.api.send_message(chat_id, "\n".join(lines), reply_to=reply_to)

    def _resolve_session_selector(self, user_id: int, selector: str) -> Tuple[Optional[str], Optional[str]]:
        raw = selector.strip()
        if not raw:
            return None, "示例: /use 1 或 /use <session_id>"
        if raw.isdigit():
            idx = int(raw)
            recent_ids = self.state.get_last_session_ids(user_id)
            if idx <= 0 or idx > len(recent_ids):
                return None, "编号无效。先执行 /sessions，再用编号。"
            return recent_ids[idx - 1], None
        return raw, None

    def _handle_status(self, chat_id: int, reply_to: int, user_id: int) -> None:
        session_id, cwd = self.state.get_active(user_id)
        running_count = self.running_prompts.count(user_id)
        if not session_id:
            message = "当前没有绑定会话。可先 /sessions + /use，或 /new 后直接发消息。"
            if running_count > 0:
                message += f"\n后台仍有 {running_count} 个任务运行，可继续 /use 切线程。"
            self.api.send_message(
                chat_id,
                message,
                reply_to=reply_to,
            )
            return
        title = f"session {session_id[:8]}"
        meta = self.sessions.find_by_id(session_id)
        if meta:
            title = meta.title
        lines = [
            "当前会话:",
            title,
            f"session: {session_id}",
            f"cwd: {cwd or str(self.default_cwd)}",
            "支持与本地 Codex 客户端交替续聊。",
        ]
        if running_count > 0:
            lines.append(f"后台运行中: {running_count} 个任务（可继续 /use 切线程）")
        self.api.send_message(
            chat_id,
            "\n".join(lines),
            reply_to=reply_to,
        )

    def _handle_ask(self, chat_id: int, reply_to: int, user_id: int, arg: str) -> None:
        prompt = arg.strip()
        if not prompt:
            self.api.send_message(chat_id, "示例: /ask 帮我总结当前仓库结构", reply_to=reply_to)
            return
        self._run_prompt(chat_id, reply_to, user_id, prompt)

    def _handle_new(self, chat_id: int, reply_to: int, user_id: int, arg: str) -> None:
        cwd_raw = arg.strip()
        _, current_cwd = self.state.get_active(user_id)
        target_cwd = Path(current_cwd).expanduser() if current_cwd else self.default_cwd
        if cwd_raw:
            candidate = Path(cwd_raw).expanduser()
            if not candidate.exists() or not candidate.is_dir():
                self.api.send_message(chat_id, f"cwd 不存在或不是目录: {candidate}", reply_to=reply_to)
                return
            target_cwd = candidate
        self.state.clear_active_session(user_id, str(target_cwd))
        self._clear_session_pickers(user_id)
        self.api.send_message(
            chat_id,
            f"已进入新会话模式，cwd: {target_cwd}\n下一条普通消息会创建一个新 session。",
            reply_to=reply_to,
        )

    def _handle_chat_message(self, chat_id: int, reply_to: int, user_id: int, text: str) -> None:
        self._run_prompt(chat_id, reply_to, user_id, text)

    def _handle_audio_message(
        self,
        chat_id: int,
        reply_to: int,
        user_id: int,
        media: Dict[str, Any],
        caption: str,
        kind: str,
    ) -> None:
        if self.audio_transcriber is None:
            self.api.send_message(
                chat_id,
                "当前未配置语音转写。设置 OPENAI_API_KEY 后，可直接发送 Telegram 语音或音频消息。",
                reply_to=reply_to,
            )
            return

        file_id = str(media.get("file_id") or "").strip()
        if not file_id:
            self.api.send_message(chat_id, "无法读取这条语音消息的文件 ID。", reply_to=reply_to)
            return

        active_id, active_cwd = self.state.get_active(user_id)
        cwd = Path(active_cwd).expanduser() if active_cwd else self.default_cwd
        if not cwd.exists():
            cwd = self.default_cwd
        if not self.running_prompts.try_start(user_id, active_id):
            busy_session = active_id[:8] if active_id else "当前线程"
            self.api.send_message(
                chat_id,
                f"会话 {busy_session} 已有任务运行中。可先 /use 切到其他线程，或等待当前回复完成。",
                reply_to=reply_to,
            )
            return

        session_label = self._session_label(active_id, cwd)
        log(
            f"queue audio prompt: user_id={user_id} kind={kind} cwd={cwd} "
            f"session={active_id} caption_len={len(caption)}"
        )
        if not self.stream_enabled:
            self.api.send_message(
                chat_id,
                "已开始处理。\n可继续发送 /use、/sessions、/status。",
                reply_to=reply_to,
            )
        worker = threading.Thread(
            target=self._run_audio_prompt_worker,
            args=(chat_id, reply_to, user_id, active_id, cwd, session_label, media, caption, kind),
            daemon=True,
        )
        try:
            worker.start()
        except Exception:
            self.running_prompts.finish(user_id, active_id)
            raise

    def _session_label(self, session_id: Optional[str], cwd: Path) -> str:
        resolved_cwd = cwd
        if session_id:
            meta = self.sessions.find_by_id(session_id)
            title = meta.title if meta else f"session {session_id[:8]}"
            if meta and meta.cwd:
                resolved_cwd = Path(meta.cwd)
        else:
            title = "新会话"
        cwd_name = resolved_cwd.name or str(resolved_cwd)
        if session_id:
            return f"{title} | {session_id[:8]} | {cwd_name}"
        return f"{title} | {cwd_name}"

    def _initial_prompt_status(self, session_label: str, active_id: Optional[str], elapsed: Optional[int] = None) -> str:
        body = "思考中..."
        if elapsed is not None:
            body = f"{body}\n\n已等待 {elapsed}s"
        return self._format_prompt_response(session_label, body)

    @staticmethod
    def _format_prompt_response(session_label: str, text: str) -> str:
        return (text or "Codex 没有返回可展示内容。").strip() or "Codex 没有返回可展示内容。"

    @staticmethod
    def _stream_preview_text(text: str) -> str:
        raw = text.strip() or "..."
        suffix = "\n\n[生成中...]"
        max_size = min(3800, MAX_TELEGRAM_TEXT)
        if len(raw) + len(suffix) <= max_size:
            return raw + suffix
        keep = max_size - len(suffix) - 1
        if keep <= 0:
            return raw[:max_size]
        return raw[:keep] + "…" + suffix

    def _finalize_stream_reply(
        self,
        chat_id: int,
        reply_to: int,
        stream_message_id: Optional[int],
        text: str,
        progressive_replay: bool = False,
    ) -> None:
        parts = chunk_text(text or "Codex 没有返回可展示内容。", size=min(3800, MAX_TELEGRAM_TEXT))
        if not parts:
            parts = ["Codex 没有返回可展示内容。"]

        first_sent = False
        if stream_message_id is not None:
            try:
                self.api.edit_message_text(chat_id, stream_message_id, parts[0])
                first_sent = True
            except Exception as e:
                log(f"stream final edit failed: {e}")

        if not first_sent:
            self.api.send_message(chat_id, parts[0], reply_to=reply_to)
            stream_message_id = None

        if progressive_replay and stream_message_id is not None and len(parts) == 1 and len(parts[0]) > 240:
            full = parts[0]
            step = 120
            interval_sec = 0.12
            for end in range(step, len(full), step):
                partial = full[:end].rstrip()
                if not partial:
                    continue
                preview = f"{partial}\n\n[生成中...]"
                try:
                    self.api.edit_message_text(chat_id, stream_message_id, preview)
                except Exception:
                    stream_message_id = None
                    break
                time.sleep(interval_sec)
            if stream_message_id is not None:
                try:
                    self.api.edit_message_text(chat_id, stream_message_id, full)
                except Exception:
                    stream_message_id = None

        for part in parts[1:]:
            self.api.send_message(chat_id, part)

    def _run_prompt(self, chat_id: int, reply_to: int, user_id: int, prompt: str) -> None:
        active_id, active_cwd = self.state.get_active(user_id)
        cwd = Path(active_cwd).expanduser() if active_cwd else self.default_cwd
        if not cwd.exists():
            cwd = self.default_cwd
        if not self.running_prompts.try_start(user_id, active_id):
            busy_session = active_id[:8] if active_id else "当前线程"
            self.api.send_message(
                chat_id,
                f"会话 {busy_session} 已有任务运行中。可先 /use 切到其他线程，或等待当前回复完成。",
                reply_to=reply_to,
            )
            return

        session_label = self._session_label(active_id, cwd)
        mode = "继续当前会话" if active_id else "新建会话"
        log(f"queue prompt: user_id={user_id} mode={mode} cwd={cwd} session={active_id}")
        if not self.stream_enabled:
            self.api.send_message(
                chat_id,
                "已开始处理。\n可继续发送 /use、/sessions、/status。",
                reply_to=reply_to,
            )

        worker = threading.Thread(
            target=self._run_prompt_worker,
            args=(chat_id, reply_to, user_id, prompt, active_id, cwd, session_label),
            daemon=True,
        )
        try:
            worker.start()
        except Exception:
            self.running_prompts.finish(user_id, active_id)
            raise

    def _run_prompt_worker(
        self,
        chat_id: int,
        reply_to: int,
        user_id: int,
        prompt: str,
        active_id: Optional[str],
        cwd: Path,
        session_label: str,
    ) -> None:
        stream_message_id: Optional[int] = None
        stream_lock = threading.Lock()
        thinking_stop = threading.Event()
        first_output = threading.Event()
        thinking_thread: Optional[threading.Thread] = None
        stream_state: Dict[str, Any] = {
            "last_preview": "",
            "last_emit_at_ms": 0,
            "content_updates": 0,
        }
        run_started_at = time.time()
        first_output_at: List[float] = []

        def edit_stream_message(text: str) -> bool:
            nonlocal stream_message_id
            if stream_message_id is None:
                return False
            with stream_lock:
                current_id = stream_message_id
                if current_id is None:
                    return False
                try:
                    self.api.edit_message_text(chat_id, current_id, text)
                    return True
                except Exception as e:
                    log(f"stream edit failed: {e}")
                    stream_message_id = None
                    return False

        if self.stream_enabled:
            try:
                sent = self.api.send_message_with_result(
                    chat_id,
                    self._initial_prompt_status(session_label, active_id),
                    reply_to=reply_to,
                )
                msg_id = sent.get("message_id")
                if isinstance(msg_id, int):
                    stream_message_id = msg_id
            except Exception as e:
                log(f"stream placeholder send failed: {e}")

        def thinking_loop() -> None:
            phases = ["思考中", "思考中.", "思考中..", "思考中..."]
            start_ts = time.time()
            i = 0
            while not thinking_stop.wait(self.thinking_status_interval_ms / 1000.0):
                if first_output.is_set():
                    return
                elapsed = int(time.time() - start_ts)
                status_text = self._format_prompt_response(
                    session_label,
                    f"{phases[i % len(phases)]}\n\n已等待 {elapsed}s",
                )
                i += 1
                if not edit_stream_message(status_text):
                    return

        if stream_message_id is not None:
            thinking_thread = threading.Thread(target=thinking_loop, daemon=True)
            thinking_thread.start()

        def on_update(live_text: str) -> None:
            first_output.set()
            if not first_output_at:
                first_output_at.append(time.time())
            if stream_message_id is None:
                return
            preview = self._format_prompt_response(
                session_label,
                self._stream_preview_text(live_text),
            )
            now_ms = int(time.time() * 1000)
            last_preview = str(stream_state.get("last_preview") or "")
            last_emit_at_ms = int(stream_state.get("last_emit_at_ms") or 0)
            if preview == last_preview:
                return
            # Throttle edit frequency to avoid Telegram 429.
            delta_chars = abs(len(preview) - len(last_preview))
            if now_ms - last_emit_at_ms < self.stream_edit_interval_ms and delta_chars < self.stream_min_delta_chars:
                return
            ok = edit_stream_message(preview)
            if not ok:
                return
            stream_state["last_preview"] = preview
            stream_state["last_emit_at_ms"] = now_ms
            stream_state["content_updates"] = int(stream_state.get("content_updates") or 0) + 1

        typing = TypingStatus(self.api, chat_id)
        typing.start()
        try:
            selected_model = self.state.get_selected_model(user_id)
            thread_id, answer, stderr_text, return_code = self.codex.run_prompt(
                prompt=prompt,
                cwd=cwd,
                session_id=active_id,
                model=selected_model,
                on_update=on_update if stream_message_id is not None else None,
            )
        except Exception as e:
            thinking_stop.set()
            if thinking_thread is not None:
                thinking_thread.join(timeout=0.3)
            err_msg = self._format_prompt_response(
                session_label,
                f"调用 Codex 时出现异常: {e}",
            )
            if stream_message_id is not None:
                self._finalize_stream_reply(chat_id, reply_to, stream_message_id, err_msg, progressive_replay=False)
            else:
                self.api.send_message(chat_id, err_msg, reply_to=reply_to)
            return
        finally:
            thinking_stop.set()
            if thinking_thread is not None:
                thinking_thread.join(timeout=0.3)
            typing.stop()
            self.running_prompts.finish(user_id, active_id)

        elapsed_sec = round(time.time() - run_started_at, 2)
        first_output_sec = round(first_output_at[0] - run_started_at, 2) if first_output_at else None
        log(
            "prompt finished: "
            f"user_id={user_id} session={active_id} thread={thread_id} exit={return_code} "
            f"elapsed_sec={elapsed_sec} first_output_sec={first_output_sec}"
        )

        final_session_id = thread_id or active_id
        final_session_label = self._session_label(final_session_id, cwd)
        session_updated = False
        if thread_id:
            session_updated = self.state.update_active_session_if_unchanged(
                user_id,
                active_id,
                thread_id,
                str(cwd),
            )

        if return_code != 0:
            msg = f"Codex 执行失败 (exit={return_code})\n{answer}"
            if stderr_text:
                msg += f"\n\nstderr:\n{stderr_text[-1200:]}"
            msg = self._format_prompt_response(final_session_label, msg)
            if stream_message_id is not None:
                self._finalize_stream_reply(chat_id, reply_to, stream_message_id, msg, progressive_replay=False)
            else:
                self.api.send_message(chat_id, msg, reply_to=reply_to)
            return

        if thread_id and not session_updated:
            current_active_id, _ = self.state.get_active(user_id)
            if current_active_id != thread_id:
                note = "当前活动线程未变；这是后台线程的回复。"
                if not active_id:
                    note = "新线程已创建，但你已经切到别的线程，当前活动线程未变。"
                answer = f"{note}\n\n{answer}"

        answer = self._format_prompt_response(final_session_label, answer)
        if stream_message_id is not None:
            replay = int(stream_state.get("content_updates") or 0) == 0
            self._finalize_stream_reply(chat_id, reply_to, stream_message_id, answer, progressive_replay=replay)
            return

        self.api.send_message(chat_id, answer, reply_to=reply_to)

    def _run_audio_prompt_worker(
        self,
        chat_id: int,
        reply_to: int,
        user_id: int,
        active_id: Optional[str],
        cwd: Path,
        session_label: str,
        media: Dict[str, Any],
        caption: str,
        kind: str,
    ) -> None:
        if self.audio_transcriber is None:
            self.running_prompts.finish(user_id, active_id)
            self.api.send_message(
                chat_id,
                "当前未配置语音转写。设置 OPENAI_API_KEY 后，可直接发送 Telegram 语音或音频消息。",
                reply_to=reply_to,
            )
            return

        file_id = str(media.get("file_id") or "").strip()
        file_name = media.get("file_name")
        mime_type = media.get("mime_type")
        file_size_raw = media.get("file_size")
        file_size = file_size_raw if isinstance(file_size_raw, int) else None

        typing = TypingStatus(self.api, chat_id)
        typing.start()
        try:
            transcript = self.audio_transcriber.transcribe_telegram_audio(
                self.api,
                file_id=file_id,
                file_name=str(file_name).strip() if file_name else None,
                mime_type=str(mime_type).strip() if mime_type else None,
                file_size=file_size,
            )
        except Exception as e:
            log(f"audio transcription failed: user_id={user_id} kind={kind} error={e}")
            self.api.send_message(chat_id, f"语音转写失败: {e}", reply_to=reply_to)
            self.running_prompts.finish(user_id, active_id)
            return
        finally:
            typing.stop()

        transcript = transcript.strip()
        if not transcript:
            self.api.send_message(chat_id, "语音转写结果为空，未继续发送给 Codex。", reply_to=reply_to)
            self.running_prompts.finish(user_id, active_id)
            return

        prompt = transcript
        if caption:
            prompt = f"附加说明:\n{caption}\n\n语音转写:\n{transcript}"
        log(
            f"audio transcription finished: user_id={user_id} kind={kind} "
            f"session={active_id} transcript_len={len(transcript)}"
        )
        self._run_prompt_worker(
            chat_id=chat_id,
            reply_to=reply_to,
            user_id=user_id,
            prompt=prompt,
            active_id=active_id,
            cwd=cwd,
            session_label=session_label,
        )

def build_service() -> TgCodexService:
    token = env("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("missing TELEGRAM_BOT_TOKEN")

    allowed_user_ids = parse_allowed_user_ids(env("ALLOWED_TELEGRAM_USER_IDS"))
    require_allowlist = parse_bool_env(env("TG_REQUIRE_ALLOWLIST"), True)
    session_root = Path(env("CODEX_SESSION_ROOT", "~/.codex/sessions")).expanduser()
    state_path = Path(env("STATE_PATH", "./bot_state.json"))
    codex_bin = resolve_codex_bin(env("CODEX_BIN"))
    codex_sandbox_mode = env("CODEX_SANDBOX_MODE")
    codex_approval_policy = env("CODEX_APPROVAL_POLICY")
    codex_dangerous_bypass_level = parse_dangerous_bypass_level(env("CODEX_DANGEROUS_BYPASS", "0"))
    codex_idle_timeout_sec = parse_non_negative_int(
        env("CODEX_IDLE_TIMEOUT_SEC", env("CODEX_EXEC_TIMEOUT_SEC", "3600")),
        3600,
    )
    openai_api_key = env("OPENAI_API_KEY")
    openai_api_base = env("OPENAI_BASE_URL", "https://api.openai.com/v1")
    tg_voice_enabled_raw = env("TG_VOICE_TRANSCRIBE_ENABLED")
    tg_voice_enabled = True if tg_voice_enabled_raw is None else tg_voice_enabled_raw == "1"
    tg_voice_backend = env("TG_VOICE_TRANSCRIBE_BACKEND", "local-whisper")
    tg_voice_model = env("TG_VOICE_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")
    tg_voice_timeout_sec = parse_non_negative_int(env("TG_VOICE_TRANSCRIBE_TIMEOUT_SEC", "180"), 180)
    tg_voice_max_bytes = parse_non_negative_int(env("TG_VOICE_MAX_BYTES", "26214400"), 26214400)
    tg_voice_local_model = env("TG_VOICE_LOCAL_MODEL", "base")
    tg_voice_local_device = env("TG_VOICE_LOCAL_DEVICE", "cpu")
    tg_voice_local_language = env("TG_VOICE_LOCAL_LANGUAGE")
    tg_voice_ffmpeg_bin = env("TG_VOICE_FFMPEG_BIN")
    default_cwd = Path(env("DEFAULT_CWD", os.getcwd())).expanduser()
    ca_bundle = env("TELEGRAM_CA_BUNDLE")
    insecure_skip_verify = env("TELEGRAM_INSECURE_SKIP_VERIFY", "0") == "1"
    tg_stream_enabled = env("TG_STREAM_ENABLED", "1") == "1"
    tg_stream_edit_interval_ms = parse_non_negative_int(env("TG_STREAM_EDIT_INTERVAL_MS", "300"), 300)
    tg_stream_min_delta_chars = parse_non_negative_int(env("TG_STREAM_MIN_DELTA_CHARS", "8"), 8)
    tg_thinking_status_interval_ms = parse_non_negative_int(env("TG_THINKING_STATUS_INTERVAL_MS", "700"), 700)

    if require_allowlist and not allowed_user_ids:
        raise RuntimeError(
            "ALLOWED_TELEGRAM_USER_IDS is required by default for safety. "
            "Set your Telegram numeric user ID, or set TG_REQUIRE_ALLOWLIST=0 to override."
        )
    if insecure_skip_verify:
        log("warn: TELEGRAM_INSECURE_SKIP_VERIFY=1 disables TLS certificate verification")
    if codex_dangerous_bypass_level > 0:
        log(f"warn: CODEX_DANGEROUS_BYPASS={codex_dangerous_bypass_level} expands local machine risk")
    if default_cwd == Path.home() or str(default_cwd) == "/":
        log(f"warn: DEFAULT_CWD points to a broad directory: {default_cwd}")

    api = TelegramAPI(
        token=token,
        ca_bundle=ca_bundle,
        insecure_skip_verify=insecure_skip_verify,
    )
    sessions = SessionStore(session_root)
    state = BotState(state_path)
    codex = CodexRunner(
        codex_bin=codex_bin,
        sandbox_mode=codex_sandbox_mode,
        approval_policy=codex_approval_policy,
        dangerous_bypass_level=codex_dangerous_bypass_level,
        idle_timeout_sec=codex_idle_timeout_sec,
    )
    audio_transcriber: Optional[AudioTranscriber] = None
    voice_backend_label = "disabled"
    if tg_voice_enabled:
        backend = (tg_voice_backend or "auto").strip().lower()
        if backend == "local-whisper":
            try:
                local_transcriber = LocalWhisperAudioTranscriber(
                    model_name=tg_voice_local_model,
                    ffmpeg_bin=tg_voice_ffmpeg_bin,
                    device=tg_voice_local_device,
                    language=tg_voice_local_language,
                    max_bytes=tg_voice_max_bytes,
                )
                local_transcriber.validate_environment()
                audio_transcriber = local_transcriber
                voice_backend_label = f"local-whisper:{tg_voice_local_model}"
            except Exception as e:
                voice_backend_label = f"local-whisper-unavailable:{e}"
        elif backend == "openai":
            if openai_api_key:
                audio_transcriber = OpenAIAudioTranscriber(
                    api_key=openai_api_key,
                    model=tg_voice_model,
                    api_base=openai_api_base,
                    timeout_sec=tg_voice_timeout_sec,
                    max_bytes=tg_voice_max_bytes,
                )
                voice_backend_label = f"openai:{tg_voice_model}"
            else:
                voice_backend_label = "openai-missing-key"
        else:
            try:
                local_transcriber = LocalWhisperAudioTranscriber(
                    model_name=tg_voice_local_model,
                    ffmpeg_bin=tg_voice_ffmpeg_bin,
                    device=tg_voice_local_device,
                    language=tg_voice_local_language,
                    max_bytes=tg_voice_max_bytes,
                )
                local_transcriber.validate_environment()
                audio_transcriber = local_transcriber
                voice_backend_label = f"local-whisper:{tg_voice_local_model}"
            except Exception:
                if openai_api_key:
                    audio_transcriber = OpenAIAudioTranscriber(
                        api_key=openai_api_key,
                        model=tg_voice_model,
                        api_base=openai_api_base,
                        timeout_sec=tg_voice_timeout_sec,
                        max_bytes=tg_voice_max_bytes,
                    )
                    voice_backend_label = f"openai:{tg_voice_model}"
                else:
                    voice_backend_label = "auto-unavailable"
    if codex_dangerous_bypass_level == 1:
        log("[warn] CODEX_DANGEROUS_BYPASS=1, enabling sandbox_mode=danger-full-access and approval_policy=never")
    elif codex_dangerous_bypass_level >= 2:
        log("[warn] CODEX_DANGEROUS_BYPASS=2, approvals and sandbox are fully bypassed")
    if tg_stream_enabled:
        log(
            "[info] TG streaming enabled "
            f"(edit interval: {tg_stream_edit_interval_ms}ms, "
            f"min delta: {tg_stream_min_delta_chars}, "
            f"thinking interval: {tg_thinking_status_interval_ms}ms)"
        )
    else:
        log("[info] TG streaming disabled")
    if codex_idle_timeout_sec > 0:
        log(f"[info] Codex idle timeout enabled ({codex_idle_timeout_sec}s)")
    else:
        log("[warn] Codex idle timeout disabled")
    if tg_voice_enabled and audio_transcriber is not None:
        log(
            "[info] Telegram voice transcription enabled "
            f"(backend: {voice_backend_label}, max bytes: {tg_voice_max_bytes})"
        )
    elif tg_voice_enabled:
        log(f"[warn] Telegram voice transcription requested, but backend is unavailable ({voice_backend_label})")
    else:
        log("[info] Telegram voice transcription disabled")

    return TgCodexService(
        api=api,
        sessions=sessions,
        state=state,
        codex=codex,
        audio_transcriber=audio_transcriber,
        default_cwd=default_cwd,
        allowed_user_ids=allowed_user_ids,
        stream_enabled=tg_stream_enabled,
        stream_edit_interval_ms=tg_stream_edit_interval_ms,
        stream_min_delta_chars=tg_stream_min_delta_chars,
        thinking_status_interval_ms=tg_thinking_status_interval_ms,
    )


def main() -> None:
    service = build_service()
    try:
        service.setup_bot_menu()
        log("bot command menu configured")
    except Exception as e:
        log(f"bot command menu setup failed: {e}")
    log("tg-codex service started")
    service.run_forever()


if __name__ == "__main__":
    main()
