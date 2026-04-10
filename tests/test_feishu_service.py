import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Optional
from uuid import uuid4
from unittest.mock import MagicMock, patch

from codex_common import BotState, RunningPromptRegistry, SessionStore
from feishu_longconn_service import FeishuCodexService, FeishuOwnerStateStore, build_service


def write_session_file(root: Path, session_id: str, cwd: str, title_prompt: str) -> None:
    day_dir = root / "2026" / "03" / "22"
    day_dir.mkdir(parents=True, exist_ok=True)
    payloads = [
        {
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "timestamp": "2026-03-22T00:00:00Z",
                "cwd": cwd,
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": title_prompt,
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "message": "done",
            },
        },
    ]
    target = day_dir / f"{session_id}.jsonl"
    target.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in payloads), encoding="utf-8")


def write_codex_global_state(home_root: Path, workspace_roots) -> Path:
    codex_dir = home_root / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)
    target = codex_dir / ".codex-global-state.json"
    target.write_text(
        json.dumps({"electron-saved-workspace-roots": [str(path) for path in workspace_roots]}, ensure_ascii=False),
        encoding="utf-8",
    )
    return target


class FakeFeishuAPI:
    def __init__(self) -> None:
        self.level = "INFO"
        self.rich_message_enabled = False
        self.sent_messages = []

    def send_message(self, chat_id: str, text: str) -> bool:
        self.sent_messages.append(("text", chat_id, text))
        return True

    def send_agent_message(self, chat_id: str, text: str, title: str = "") -> bool:
        self.sent_messages.append(("agent", chat_id, text, title))
        return True

    def send_agent_message_with_id(self, chat_id: str, text: str, title: str = ""):
        self.sent_messages.append(("agent_with_id", chat_id, text, title))
        return "msg-1"

    def patch_agent_message(self, message_id: str, text: str, title: str = "") -> bool:
        self.sent_messages.append(("patch", message_id, text, title))
        return True


class FakeCodexRunner:
    def __init__(self) -> None:
        self.calls = []

    def run_prompt(self, prompt, cwd, session_id=None, on_update=None):
        self.calls.append((prompt, str(cwd), session_id))
        return ("thread-123", f"answer:{prompt}", "", 0)


class FeishuEventBuilder:
    @staticmethod
    def message(
        *,
        chat_id: str,
        chat_type: str,
        text: str,
        open_id: str,
        user_id: str = "",
        event_id: Optional[str] = None,
        message_id: Optional[str] = None,
        message_type: str = "text",
        create_time: Optional[int] = None,
    ):
        event_id = event_id or f"evt-{uuid4().hex}"
        message_id = message_id or f"msg-{uuid4().hex}"
        sender_id = SimpleNamespace(open_id=open_id, user_id=user_id)
        sender = SimpleNamespace(sender_type="human", sender_id=sender_id)
        message = SimpleNamespace(
            chat_id=chat_id,
            chat_type=chat_type,
            message_type=message_type,
            message_id=message_id,
            content=json.dumps({"text": text}, ensure_ascii=False),
            create_time=create_time if create_time is not None else int(time.time() * 1000),
        )
        event = SimpleNamespace(message=message, sender=sender)
        header = SimpleNamespace(event_id=event_id, event_type="im.message.receive_v1")
        return SimpleNamespace(header=header, event=event)


class FeishuServiceHarness(FeishuCodexService):
    def __init__(self, *args, **kwargs):
        self.api = kwargs["api"]
        self.sessions = kwargs["sessions"]
        self.state = kwargs["state"]
        self.codex = kwargs["codex"]
        self.default_cwd = kwargs["default_cwd"]
        self.allowed_open_ids = kwargs.get("allowed_open_ids")
        self.enable_p2p = kwargs.get("enable_p2p", True)
        self.ignore_old_message_seconds = kwargs.get("ignore_old_message_seconds", 0)
        runtime_dir = kwargs.get("runtime_dir")
        if runtime_dir is None:
            runtime_dir = Path(self.default_cwd) / ".runtime" / "feishu"
        self.runtime_dir = Path(runtime_dir)
        self._owner_state_store_impl = FeishuOwnerStateStore(self.runtime_dir)
        self.owner_mode_enabled = kwargs.get("owner_mode_enabled", True)
        self.stream_enabled = False
        self.stream_edit_interval_ms = 400
        self.stream_min_delta_chars = 12
        self.thinking_status_interval_ms = 900
        self.running_prompts = RunningPromptRegistry()
        self.startup_time_ms = int(time.time() * 1000)
        self.seen_event_ids = set()
        self.seen_message_ids = set()
        self.prompt_calls = []

    def _run_prompt(self, chat_id: str, actor_id: str, prompt: str) -> None:
        self.prompt_calls.append((chat_id, actor_id, prompt))


class FeishuOwnerModeTests(unittest.TestCase):
    def build_service(self, root: Path, *, enable_p2p: bool = True):
        api = FakeFeishuAPI()
        sessions_root = root / "sessions"
        write_session_file(sessions_root, "sess-1", str(root), "first prompt")
        state = BotState(root / ".runtime" / "feishu_bot_state.json")
        service = FeishuServiceHarness(
            api=api,
            sessions=SessionStore(sessions_root),
            state=state,
            codex=FakeCodexRunner(),
            default_cwd=root,
            runtime_dir=root / ".runtime" / "feishu",
            enable_p2p=enable_p2p,
        )
        return service, api, state

    def pair_owner_and_restart(
        self,
        root: Path,
        *,
        owner_open_id: str = "ou-owner",
        enable_p2p: bool = True,
    ):
        pairing_service, pairing_api, _ = self.build_service(root, enable_p2p=enable_p2p)
        pair_code = pairing_service.issue_pair_code(ttl_seconds=60)
        self.send_message(
            pairing_service,
            chat_id="chat-p2p",
            chat_type="p2p",
            open_id=owner_open_id,
            text=f"/pair {pair_code}",
        )
        restarted_service, restarted_api, _ = self.build_service(root, enable_p2p=enable_p2p)
        return pairing_service, pairing_api, restarted_service, restarted_api, pair_code

    def send_message(self, service: FeishuCodexService, *, chat_id: str, chat_type: str, open_id: str, text: str):
        service._on_message_receive(
            FeishuEventBuilder.message(
                chat_id=chat_id,
                chat_type=chat_type,
                text=text,
                open_id=open_id,
            )
        )

    def test_build_service_enables_p2p_by_default_for_owner_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            dispatcher_builder = MagicMock()
            dispatcher_builder.register_p2_im_message_receive_v1.return_value = dispatcher_builder
            dispatcher_builder.register_p2_im_chat_access_event_bot_p2p_chat_entered_v1.return_value = dispatcher_builder
            dispatcher_builder.register_p2_im_chat_member_bot_added_v1.return_value = dispatcher_builder
            dispatcher_builder.register_p2_im_chat_member_bot_deleted_v1.return_value = dispatcher_builder
            dispatcher_builder.register_p2_customized_event.return_value = dispatcher_builder
            dispatcher_builder.build.return_value = object()

            with patch.dict(
                "os.environ",
                {
                    "FEISHU_APP_ID": "app-id",
                    "FEISHU_APP_SECRET": "app-secret",
                    "CODEX_SESSION_ROOT": str(root / "sessions"),
                    "DEFAULT_CWD": str(root),
                    "STATE_PATH": str(root / ".runtime" / "feishu_bot_state.json"),
                },
                clear=True,
            ), patch("feishu_longconn_service.FeishuAPI", return_value=FakeFeishuAPI()), patch(
                "feishu_longconn_service.CodexRunner",
                return_value=FakeCodexRunner(),
            ), patch(
                "feishu_longconn_service.resolve_codex_bin",
                return_value="codex",
            ), patch(
                "feishu_longconn_service.lark.EventDispatcherHandler.builder",
                return_value=dispatcher_builder,
            ), patch(
                "feishu_longconn_service.lark.ws.Client",
                return_value=object(),
            ):
                service = build_service()

            self.assertTrue(service.owner_mode_enabled)
            self.assertTrue(service.enable_p2p)

    def test_constructor_uses_injected_runtime_dir_for_owner_state_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime_dir = root / ".custom-runtime" / "feishu"
            api = FakeFeishuAPI()

            dispatcher_builder = MagicMock()
            dispatcher_builder.register_p2_im_message_receive_v1.return_value = dispatcher_builder
            dispatcher_builder.register_p2_im_chat_access_event_bot_p2p_chat_entered_v1.return_value = dispatcher_builder
            dispatcher_builder.register_p2_im_chat_member_bot_added_v1.return_value = dispatcher_builder
            dispatcher_builder.register_p2_im_chat_member_bot_deleted_v1.return_value = dispatcher_builder
            dispatcher_builder.register_p2_customized_event.return_value = dispatcher_builder
            dispatcher_builder.build.return_value = object()

            with patch("feishu_longconn_service.lark.EventDispatcherHandler.builder", return_value=dispatcher_builder), patch(
                "feishu_longconn_service.lark.ws.Client",
                return_value=object(),
            ):
                service = FeishuCodexService(
                    api=api,
                    sessions=SessionStore(root / "sessions"),
                    state=BotState(root / ".runtime" / "feishu_bot_state.json"),
                    codex=FakeCodexRunner(),
                    default_cwd=root,
                    app_id="app-id",
                    app_secret="app-secret",
                    allowed_open_ids=None,
                    enable_p2p=True,
                    ignore_old_message_seconds=0,
                    stream_enabled=False,
                    stream_edit_interval_ms=400,
                    stream_min_delta_chars=12,
                    thinking_status_interval_ms=900,
                    runtime_dir=runtime_dir,
                )

            self.assertEqual(service.owner_state_path, runtime_dir / "owner_state.json")
            self.assertTrue(service.owner_state_path.parent.exists())

    def test_invalid_or_expired_pair_codes_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service, api, _ = self.build_service(root, enable_p2p=True)

            pair_code = service.issue_pair_code(ttl_seconds=60)

            self.send_message(
                service,
                chat_id="chat-p2p",
                chat_type="p2p",
                open_id="ou-owner",
                text="/pair WRONG-CODE",
            )

            self.assertTrue(
                any(
                    ("invalid" in text.lower() or "无效" in text or "pair code" in text.lower())
                    for kind, _, text in api.sent_messages
                    if kind == "text"
                )
            )
            api.sent_messages.clear()

            payload = json.loads(service.owner_state_path.read_text(encoding="utf-8"))
            payload["pairing_window"]["pair_code_expires_at"] = int(time.time() * 1000) - 1
            service.owner_state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

            self.send_message(
                service,
                chat_id="chat-p2p",
                chat_type="p2p",
                open_id="ou-owner",
                text=f"/pair {pair_code}",
            )

            self.assertTrue(
                any("expired" in text.lower() or "过期" in text for kind, _, text in api.sent_messages if kind == "text"),
            )
            self.assertIsNone(service.get_current_owner())

    def test_pairing_in_group_chat_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service, api, _ = self.build_service(root, enable_p2p=True)
            pair_code = service.issue_pair_code(ttl_seconds=60)

            self.send_message(
                service,
                chat_id="chat-group",
                chat_type="group",
                open_id="ou-owner",
                text=f"/pair {pair_code}",
            )

            self.assertTrue(
                any("私聊" in text or "pair" in text.lower() for kind, _, text in api.sent_messages if kind == "text")
            )
            self.assertIsNone(service.get_current_owner())
            self.assertIsNotNone(service.get_current_pairing_window())

    def test_valid_pair_code_in_p2p_binds_sender_as_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pairing_service, pairing_api, restarted_service, _, pair_code = self.pair_owner_and_restart(root)
            self.assertTrue(
                any("paired" in msg[2].lower() or "owner" in msg[2].lower() for msg in pairing_api.sent_messages if msg[0] == "text"),
            )
            self.assertEqual(pairing_service.get_current_owner()["open_id"], "ou-owner")
            self.assertIsNone(pairing_service.get_current_pairing_window())
            self.assertTrue(pair_code)
            self.assertEqual(restarted_service.get_current_owner()["open_id"], "ou-owner")


class FeishuServiceAuthorizationTests(unittest.TestCase):
    def build_service(self, root: Path, *, enable_p2p: bool = True):
        api = FakeFeishuAPI()
        sessions_root = root / "sessions"
        write_session_file(sessions_root, "sess-1", str(root), "first prompt")
        state = BotState(root / ".runtime" / "feishu_bot_state.json")
        service = FeishuServiceHarness(
            api=api,
            sessions=SessionStore(sessions_root),
            state=state,
            codex=FakeCodexRunner(),
            default_cwd=root,
            runtime_dir=root / ".runtime" / "feishu",
            enable_p2p=enable_p2p,
        )
        return service, api, state

    def pair_owner_and_restart(self, root: Path, *, owner_open_id: str = "ou-owner", enable_p2p: bool = True):
        pairing_service, _, _ = self.build_service(root, enable_p2p=enable_p2p)
        pair_code = pairing_service.issue_pair_code(ttl_seconds=60)
        self.send_message(
            pairing_service,
            chat_id="chat-p2p",
            chat_type="p2p",
            open_id=owner_open_id,
            text=f"/pair {pair_code}",
        )
        restarted_service, restarted_api, _ = self.build_service(root, enable_p2p=enable_p2p)
        return restarted_service, restarted_api

    def send_message(self, service: FeishuCodexService, *, chat_id: str, chat_type: str, open_id: str, text: str):
        service._on_message_receive(
            FeishuEventBuilder.message(
                chat_id=chat_id,
                chat_type=chat_type,
                text=text,
                open_id=open_id,
            )
        )

    def test_non_owner_requests_are_rejected_after_pairing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service, api = self.pair_owner_and_restart(root)

            self.send_message(
                service,
                chat_id="chat-p2p",
                chat_type="p2p",
                open_id="ou-stranger",
                text="hello from non-owner",
            )

            self.assertTrue(any("owner" in text.lower() or "权限" in text or "pair" in text.lower() for _, _, text in api.sent_messages))
            self.assertEqual(service.prompt_calls, [])

    def test_owner_can_still_use_sessions_and_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            write_codex_global_state(root, [root])
            service, api = self.pair_owner_and_restart(root)

            with patch.dict("os.environ", {"HOME": str(root)}, clear=False):
                self.send_message(
                    service,
                    chat_id="chat-p2p",
                    chat_type="p2p",
                    open_id="ou-owner",
                    text="/sessions",
                )
                self.send_message(
                    service,
                    chat_id="chat-p2p",
                    chat_type="p2p",
                    open_id="ou-owner",
                    text="/history sess-1 2",
                )

            self.assertTrue(any("工作区列表" in text for kind, _, text in api.sent_messages if kind == "text"))
            self.assertTrue(any("会话历史" in text for kind, _, text in api.sent_messages if kind == "text"))

    def test_owner_can_choose_workspace_then_start_new_session_in_that_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            other_project = root / "project-beta"
            other_project.mkdir(parents=True, exist_ok=True)
            write_codex_global_state(root, [root, other_project])

            service, api = self.pair_owner_and_restart(root)

            with patch.dict("os.environ", {"HOME": str(root)}, clear=False):
                self.send_message(service, chat_id="chat-p2p", chat_type="p2p", open_id="ou-owner", text="/sessions")
                self.send_message(service, chat_id="chat-p2p", chat_type="p2p", open_id="ou-owner", text="2")
                self.send_message(service, chat_id="chat-p2p", chat_type="p2p", open_id="ou-owner", text="0")

            session_messages = [text for kind, _, text in api.sent_messages if kind == "text"]
            self.assertTrue(any("0. 新建会话" in text for text in session_messages))
            self.assertTrue(any(f"工作区: {other_project}" in text for text in session_messages))
            self.assertTrue(any("下一条普通消息会新建 session" in text for text in session_messages))

            active_session, active_cwd = service.state.get_active("ou-owner")
            self.assertIsNone(active_session)
            self.assertEqual(active_cwd, str(other_project))


class FeishuOwnerCliTests(unittest.TestCase):
    def run_script(
        self,
        root: Path,
        *args: str,
        env_overrides: Optional[Dict[str, str]] = None,
        include_runtime_dir: bool = True,
    ) -> subprocess.CompletedProcess:
        script_path = Path(__file__).resolve().parents[1] / "run_feishu.sh"
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "PYTHON_BIN": os.environ.get("PYTHON_BIN", "python3"),
            "STATE_PATH": str(root / ".runtime" / "feishu_bot_state.json"),
        }
        if include_runtime_dir:
            env["FEISHU_RUNTIME_DIR"] = str(root / ".runtime" / "feishu")
        if env_overrides:
            env.update(env_overrides)
        return subprocess.run(
            ["bash", str(script_path), *args],
            cwd=script_path.parent,
            env=env,
            capture_output=True,
            text=True,
        )

    def test_pair_status_reports_unpaired_owner_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            result = self.run_script(root, "pair-status")
            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            self.assertIn("owner mode: on", result.stdout)
            self.assertIn("paired: no", result.stdout)

    def test_pair_code_and_reset_manage_local_owner_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pair_code_result = self.run_script(root, "pair-code")
            self.assertEqual(pair_code_result.returncode, 0, msg=pair_code_result.stderr or pair_code_result.stdout)
            self.assertIn("/pair ", pair_code_result.stdout)

            owner_state_path = root / ".runtime" / "feishu" / "owner_state.json"
            self.assertTrue(owner_state_path.exists())
            payload = json.loads(owner_state_path.read_text(encoding="utf-8"))
            self.assertIsNotNone(payload.get("pairing_window"))

            reset_result = self.run_script(root, "pair-reset")
            self.assertEqual(reset_result.returncode, 0, msg=reset_result.stderr or reset_result.stdout)
            reset_payload = json.loads(owner_state_path.read_text(encoding="utf-8"))
            self.assertEqual(reset_payload, {})

    def test_pair_status_loads_local_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            alt_runtime_dir = root / ".alt-runtime" / "feishu"
            env_file = root / "feishu.env"
            env_file.write_text(
                "\n".join(
                    [
                        'FEISHU_OWNER_MODE="0"',
                        f'FEISHU_RUNTIME_DIR="{alt_runtime_dir}"',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            result = self.run_script(
                root,
                "pair-status",
                env_overrides={"FEISHU_ENV_FILE": str(env_file)},
                include_runtime_dir=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            self.assertIn("owner mode: off", result.stdout)
            self.assertIn(f"runtime_dir: {alt_runtime_dir}", result.stdout)


class FeishuLoggingTests(unittest.TestCase):
    def build_service(self, root: Path):
        api = FakeFeishuAPI()
        sessions_root = root / "sessions"
        write_session_file(sessions_root, "sess-1", str(root), "first prompt")
        state = BotState(root / ".runtime" / "feishu_bot_state.json")
        service = FeishuServiceHarness(
            api=api,
            sessions=SessionStore(sessions_root),
            state=state,
            codex=FakeCodexRunner(),
            default_cwd=root,
            runtime_dir=root / ".runtime" / "feishu",
            enable_p2p=True,
        )
        return service

    def test_freeform_logs_only_text_length_not_prompt_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service = self.build_service(root)

            with patch("feishu_longconn_service.log") as log_mock:
                service._on_message_receive(
                    FeishuEventBuilder.message(
                        chat_id="chat-p2p",
                        chat_type="p2p",
                        text="deploy the secret production token abc123",
                        open_id="ou-owner",
                    )
                )

            logged = "\n".join(str(call.args[0]) for call in log_mock.call_args_list)
            self.assertIn("text_len=", logged)
            self.assertNotIn("secret production token", logged)

    def test_slash_command_logs_command_name_without_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service = self.build_service(root)

            with patch("feishu_longconn_service.log") as log_mock:
                service._on_message_receive(
                    FeishuEventBuilder.message(
                        chat_id="chat-p2p",
                        chat_type="p2p",
                        text="/ask inspect ~/.ssh/id_rsa for me",
                        open_id="ou-owner",
                    )
                )

            logged = "\n".join(str(call.args[0]) for call in log_mock.call_args_list)
            self.assertIn("command=/ask", logged)
            self.assertNotIn("~/.ssh/id_rsa", logged)


if __name__ == "__main__":
    unittest.main()
