import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_common import BotState, SessionStore
from feishu_longconn_service import FeishuCodexService


def write_session_file(root: Path, session_id: str, cwd: str, title_prompt: str) -> None:
    day_dir = root / "2026" / "04" / "06"
    day_dir.mkdir(parents=True, exist_ok=True)
    payloads = [
        {
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "timestamp": "2026-04-06T00:00:00Z",
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
    ]
    target = day_dir / f"{session_id}.jsonl"
    target.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in payloads), encoding="utf-8")


class FakeFeishuAPI:
    def __init__(self) -> None:
        self.level = "INFO"
        self.rich_message_enabled = False
        self.sent_messages = []

    def send_message(self, chat_id: str, text: str) -> bool:
        self.sent_messages.append((chat_id, text))
        return True

    def send_agent_message(self, chat_id: str, text: str, title: str = "") -> bool:
        self.sent_messages.append((chat_id, text, title))
        return True

    def send_agent_message_with_id(self, chat_id: str, text: str, title: str = ""):
        self.sent_messages.append((chat_id, text, title))
        return "msg-1"

    def patch_agent_message(self, message_id: str, text: str, title: str = "") -> bool:
        self.sent_messages.append((message_id, text, title))
        return True


class FakeCodexRunner:
    def __init__(self) -> None:
        self.calls = []

    def run_prompt(self, prompt, cwd, session_id=None, model=None, on_update=None):
        self.calls.append(
            {
                "prompt": prompt,
                "cwd": str(cwd),
                "session_id": session_id,
                "model": model,
            }
        )
        return ("thread-123", f"answer:{prompt}", "", 0)


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
        self.stream_enabled = False
        self.stream_edit_interval_ms = 400
        self.stream_min_delta_chars = 12
        self.thinking_status_interval_ms = 900
        self.running_prompts = kwargs["running_prompts"]
        self.startup_time_ms = int(time.time() * 1000)
        self.seen_event_ids = set()
        self.seen_message_ids = set()


class _RunningPromptRegistryStub:
    def try_start(self, actor, session_id):
        return True

    def finish(self, actor, session_id) -> None:
        return None

    def count(self, actor) -> int:
        return 0


class FeishuModelAccountTests(unittest.TestCase):
    def build_service(self, root: Path):
        sessions_root = root / "sessions"
        write_session_file(sessions_root, "sess-1", str(root), "first prompt")
        api = FakeFeishuAPI()
        state = BotState(root / "state.json")
        codex = FakeCodexRunner()
        service = FeishuServiceHarness(
            api=api,
            sessions=SessionStore(sessions_root),
            state=state,
            codex=codex,
            default_cwd=root,
            running_prompts=_RunningPromptRegistryStub(),
            enable_p2p=True,
            ignore_old_message_seconds=0,
            allowed_open_ids=None,
        )
        return service, api, state, codex

    def test_model_command_lists_provider_models(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service, api, _, _ = self.build_service(root)

            with patch(
                "feishu_longconn_service.list_provider_models",
                return_value=["gpt-5.4", "gpt-5.3"],
            ), patch(
                "feishu_longconn_service.load_codex_default_model",
                return_value="gpt-5.4",
            ):
                service._handle_text("chat-1", "user-1", "/model")

            text = api.sent_messages[-1][1]
            self.assertIn("1. gpt-5.4", text)
            self.assertIn("2. gpt-5.3", text)
            self.assertIn("当前模型", text)

    def test_numeric_model_pick_saves_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service, api, state, _ = self.build_service(root)

            with patch(
                "feishu_longconn_service.list_provider_models",
                return_value=["gpt-5.4", "gpt-5.3"],
            ), patch(
                "feishu_longconn_service.load_codex_default_model",
                return_value="gpt-5.4",
            ):
                service._handle_text("chat-1", "user-1", "/model")

            service._handle_text("chat-1", "user-1", "2")

            self.assertEqual(state.get_selected_model("user-1"), "gpt-5.3")
            self.assertFalse(state.is_pending_model_pick("user-1"))
            self.assertIn("gpt-5.3", api.sent_messages[-1][1])

    def test_account_command_formats_quota_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service, api, _, _ = self.build_service(root)

            payload = {
                "sub_service_type_name": "Codex Core",
                "billing_type": "duration",
                "quota": {
                    "daily_quota": 9000,
                    "daily_spent": 240,
                    "daily_remaining": 8760,
                    "next_reset_at": "2026-04-07T00:00:00+08:00",
                },
                "usage": {
                    "daily_request_count": 40,
                },
            }

            with patch(
                "feishu_longconn_service.fetch_provider_account_info",
                return_value=payload,
            ):
                service._handle_text("chat-1", "user-1", "/account")

            text = api.sent_messages[-1][1]
            self.assertIn("今日剩余", text)
            self.assertIn("8760", text)
            self.assertIn("40", text)

    def test_prompt_worker_uses_selected_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service, _, state, codex = self.build_service(root)
            state.set_selected_model("user-1", "gpt-5.3")

            service._run_prompt_worker(
                chat_id="chat-1",
                actor_id="user-1",
                prompt="hello",
                active_id=None,
                cwd=root,
                session_label="新会话 | tmp",
            )

            self.assertEqual(codex.calls[-1]["model"], "gpt-5.3")


if __name__ == "__main__":
    unittest.main()
