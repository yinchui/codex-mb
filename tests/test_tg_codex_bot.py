import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_common import BotState, SessionStore
from tg_codex_bot import BOT_COMMANDS, TgCodexService


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


class FakeTelegramAPI:
    def __init__(self) -> None:
        self.sent_messages = []
        self.chat_actions = []

    def send_message(self, chat_id: int, text: str, reply_to=None, reply_markup=None) -> None:
        self.sent_messages.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_to": reply_to,
                "reply_markup": reply_markup,
            }
        )

    def send_message_with_result(self, chat_id: int, text: str, reply_to=None, reply_markup=None):
        self.send_message(chat_id, text, reply_to=reply_to, reply_markup=reply_markup)
        return {"message_id": len(self.sent_messages)}

    def edit_message_text(self, chat_id: int, message_id: int, text: str) -> None:
        self.sent_messages.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
            }
        )

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        self.chat_actions.append((chat_id, action))

    def answer_callback_query(self, callback_query_id: str, text=None, show_alert: bool = False) -> None:
        return None

    def set_my_commands(self, commands):
        return None

    def set_chat_menu_button_commands(self) -> None:
        return None


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


class TgRunningPromptRegistryStub:
    def try_start(self, actor, session_id):
        return True

    def finish(self, actor, session_id) -> None:
        return None

    def count(self, actor) -> int:
        return 0


class TelegramModelAccountTests(unittest.TestCase):
    def build_service(self, root: Path):
        sessions_root = root / "sessions"
        write_session_file(sessions_root, "sess-1", str(root), "first prompt")
        api = FakeTelegramAPI()
        state = BotState(root / "state.json")
        codex = FakeCodexRunner()
        service = TgCodexService(
            api=api,
            sessions=SessionStore(sessions_root),
            state=state,
            codex=codex,
            audio_transcriber=None,
            default_cwd=root,
            allowed_user_ids=None,
            stream_enabled=False,
            stream_edit_interval_ms=400,
            stream_min_delta_chars=12,
            thinking_status_interval_ms=900,
        )
        service.running_prompts = TgRunningPromptRegistryStub()
        return service, api, state, codex

    @staticmethod
    def make_text_update(text: str, user_id: int = 42, chat_id: int = 100, message_id: int = 1):
        return {
            "update_id": message_id,
            "message": {
                "message_id": message_id,
                "chat": {"id": chat_id},
                "from": {"id": user_id},
                "text": text,
            },
        }

    @staticmethod
    def make_callback_update(
        data: str,
        user_id: int = 42,
        chat_id: int = 100,
        message_id: int = 900,
        callback_id: str = "cb-1",
    ):
        return {
            "update_id": message_id,
            "callback_query": {
                "id": callback_id,
                "from": {"id": user_id},
                "data": data,
                "message": {
                    "message_id": message_id,
                    "chat": {"id": chat_id},
                },
            },
        }

    def test_bot_commands_include_model_and_account(self) -> None:
        commands = {item["command"] for item in BOT_COMMANDS}
        self.assertIn("model", commands)
        self.assertIn("account", commands)

    def test_model_command_lists_provider_models(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service, api, _, _ = self.build_service(root)

            with patch(
                "tg_codex_bot.list_provider_models",
                return_value=["gpt-5.4", "gpt-5.3"],
            ), patch(
                "tg_codex_bot.load_codex_default_model",
                return_value="gpt-5.4",
            ):
                service._handle_update(self.make_text_update("/model"))

            text = api.sent_messages[-1]["text"]
            self.assertIn("1. gpt-5.4", text)
            self.assertIn("2. gpt-5.3", text)
            self.assertIn("当前模型", text)

    def test_numeric_model_pick_saves_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service, api, state, _ = self.build_service(root)

            with patch(
                "tg_codex_bot.list_provider_models",
                return_value=["gpt-5.4", "gpt-5.3"],
            ), patch(
                "tg_codex_bot.load_codex_default_model",
                return_value="gpt-5.4",
            ):
                service._handle_update(self.make_text_update("/model"))

            service._handle_update(self.make_text_update("2", message_id=2))

            self.assertEqual(state.get_selected_model(42), "gpt-5.3")
            self.assertFalse(state.is_pending_model_pick(42))
            self.assertIn("gpt-5.3", api.sent_messages[-1]["text"])

    def test_sessions_command_clears_pending_model_picker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service, api, state, _ = self.build_service(root)

            with patch(
                "tg_codex_bot.list_provider_models",
                return_value=["gpt-5.4", "gpt-5.3"],
            ), patch(
                "tg_codex_bot.load_codex_default_model",
                return_value="gpt-5.4",
            ):
                service._handle_update(self.make_text_update("/model"))

            service._handle_update(self.make_text_update("/sessions", message_id=2))
            self.assertTrue(state.is_pending_workspace_pick(42))
            self.assertFalse(state.is_pending_session_pick(42))
            self.assertIn("最近工作区", api.sent_messages[-1]["text"])

            service._handle_update(self.make_text_update("1", message_id=3))

            self.assertFalse(state.is_pending_workspace_pick(42))
            self.assertTrue(state.is_pending_session_pick(42))
            self.assertIsNone(state.get_active(42)[0])
            self.assertIn("最近会话", api.sent_messages[-1]["text"])

            service._handle_update(self.make_text_update("1", message_id=4))

            self.assertEqual(state.get_active(42)[0], "sess-1")
            self.assertIsNone(state.get_selected_model(42))

    def test_workspace_callback_shows_second_level_sessions_before_switching(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service, api, state, _ = self.build_service(root)

            service._handle_update(self.make_text_update("/sessions"))

            first_message = api.sent_messages[-1]
            self.assertEqual(
                first_message["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
                "workspace:1",
            )

            service._handle_update(self.make_callback_update("workspace:1"))

            second_message = api.sent_messages[-1]
            self.assertFalse(state.is_pending_workspace_pick(42))
            self.assertTrue(state.is_pending_session_pick(42))
            self.assertIsNone(state.get_active(42)[0])
            self.assertIn("最近会话", second_message["text"])
            self.assertEqual(
                second_message["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
                "use:sess-1",
            )

            service._handle_update(self.make_callback_update("use:sess-1", message_id=901, callback_id="cb-2"))

            self.assertEqual(state.get_active(42)[0], "sess-1")

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
                "tg_codex_bot.fetch_provider_account_info",
                return_value=payload,
            ):
                service._handle_update(self.make_text_update("/account"))

            text = api.sent_messages[-1]["text"]
            self.assertIn("今日剩余", text)
            self.assertIn("8760", text)
            self.assertIn("40", text)

    def test_prompt_worker_uses_selected_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service, _, state, codex = self.build_service(root)
            state.set_selected_model(42, "gpt-5.3")

            service._run_prompt_worker(
                chat_id=100,
                reply_to=1,
                user_id=42,
                prompt="hello",
                active_id=None,
                cwd=root,
                session_label="新会话 | tmp",
            )

            self.assertEqual(codex.calls[-1]["model"], "gpt-5.3")


if __name__ == "__main__":
    unittest.main()
