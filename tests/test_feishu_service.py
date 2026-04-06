import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import feishu_longconn_service as service_module
from codex_common import BotState, SessionStore
from feishu_longconn_service import FeishuAPI, FeishuCodexService


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
        self.sent_images = []
        self.sent_files = []

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

    def send_image_path(self, chat_id: str, path: Path) -> bool:
        self.sent_images.append((chat_id, str(path)))
        return True

    def send_file_path(self, chat_id: str, path: Path) -> bool:
        self.sent_files.append((chat_id, str(path)))
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


class _FakeLarkResponse:
    def __init__(self, *, ok: bool = True, data=None) -> None:
        self._ok = ok
        self.data = data
        self.code = 0 if ok else 500
        self.msg = "ok" if ok else "error"

    def success(self) -> bool:
        return self._ok

    def get_log_id(self) -> str:
        return "log-1"


class _FakeFeishuClient:
    class _ImageService:
        def __init__(self, outer) -> None:
            self.outer = outer

        def create(self, request):
            self.outer.image_create_requests.append(request)
            data = type("ImageData", (), {"image_key": "img-key-1"})()
            return _FakeLarkResponse(ok=True, data=data)

    class _FileService:
        def __init__(self, outer) -> None:
            self.outer = outer

        def create(self, request):
            self.outer.file_create_requests.append(request)
            data = type("FileData", (), {"file_key": "file-key-1"})()
            return _FakeLarkResponse(ok=True, data=data)

    class _MessageService:
        def __init__(self, outer) -> None:
            self.outer = outer

        def create(self, request):
            self.outer.message_create_requests.append(request)
            return _FakeLarkResponse(ok=True)

    class _V1Service:
        def __init__(self, outer) -> None:
            self.image = _FakeFeishuClient._ImageService(outer)
            self.file = _FakeFeishuClient._FileService(outer)
            self.message = _FakeFeishuClient._MessageService(outer)

    class _ImService:
        def __init__(self, outer) -> None:
            self.v1 = _FakeFeishuClient._V1Service(outer)

    def __init__(self) -> None:
        self.image_create_requests = []
        self.file_create_requests = []
        self.message_create_requests = []
        self.im = _FakeFeishuClient._ImService(self)


class FeishuApiAttachmentTests(unittest.TestCase):
    @staticmethod
    def _build_api_with_fake_client(fake_client: _FakeFeishuClient) -> FeishuAPI:
        api = object.__new__(FeishuAPI)
        api.client = fake_client
        api.level = "INFO"
        api.rich_message_enabled = True
        return api

    def test_send_image_path_uses_feishu_image_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "preview.png"
            image_path.write_bytes(b"fake-image")
            fake_client = _FakeFeishuClient()
            api = self._build_api_with_fake_client(fake_client)

            ok = api.send_image_path("chat-1", image_path)

            self.assertTrue(ok)
            self.assertEqual(len(fake_client.image_create_requests), 1)
            self.assertEqual(len(fake_client.message_create_requests), 1)
            message_req = fake_client.message_create_requests[-1]
            self.assertEqual(message_req.request_body.msg_type, "image")
            self.assertEqual(json.loads(message_req.request_body.content), {"image_key": "img-key-1"})

    def test_send_file_path_uses_feishu_file_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "notes.txt"
            file_path.write_text("hello", encoding="utf-8")
            fake_client = _FakeFeishuClient()
            api = self._build_api_with_fake_client(fake_client)

            ok = api.send_file_path("chat-1", file_path)

            self.assertTrue(ok)
            self.assertEqual(len(fake_client.file_create_requests), 1)
            self.assertEqual(len(fake_client.message_create_requests), 1)
            message_req = fake_client.message_create_requests[-1]
            self.assertEqual(message_req.request_body.msg_type, "file")
            self.assertEqual(json.loads(message_req.request_body.content), {"file_key": "file-key-1"})

    def test_send_image_path_logs_when_path_missing(self) -> None:
        fake_client = _FakeFeishuClient()
        api = self._build_api_with_fake_client(fake_client)

        with patch("feishu_longconn_service.log") as mock_log:
            ok = api.send_image_path("chat-1", Path("/tmp/definitely-not-exists-image.png"))

        self.assertFalse(ok)
        mock_log.assert_called_once()

    def test_send_file_path_logs_when_path_missing(self) -> None:
        fake_client = _FakeFeishuClient()
        api = self._build_api_with_fake_client(fake_client)

        with patch("feishu_longconn_service.log") as mock_log:
            ok = api.send_file_path("chat-1", Path("/tmp/definitely-not-exists-file.pdf"))

        self.assertFalse(ok)
        mock_log.assert_called_once()

    def test_send_image_path_logs_when_upload_key_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "preview.png"
            image_path.write_bytes(b"fake-image")
            fake_client = _FakeFeishuClient()
            api = self._build_api_with_fake_client(fake_client)

            def _missing_key_response(_request):
                data = type("ImageData", (), {"image_key": ""})()
                return _FakeLarkResponse(ok=True, data=data)

            fake_client.im.v1.image.create = _missing_key_response
            with patch("feishu_longconn_service.log") as mock_log:
                ok = api.send_image_path("chat-1", image_path)

            self.assertFalse(ok)
            mock_log.assert_called_once()

    def test_send_file_path_logs_when_upload_key_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "notes.txt"
            file_path.write_text("hello", encoding="utf-8")
            fake_client = _FakeFeishuClient()
            api = self._build_api_with_fake_client(fake_client)

            def _missing_key_response(_request):
                data = type("FileData", (), {"file_key": ""})()
                return _FakeLarkResponse(ok=True, data=data)

            fake_client.im.v1.file.create = _missing_key_response
            with patch("feishu_longconn_service.log") as mock_log:
                ok = api.send_file_path("chat-1", file_path)

            self.assertFalse(ok)
            mock_log.assert_called_once()


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

    def test_sessions_command_clears_pending_model_picker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service, _, state, _ = self.build_service(root)

            with patch(
                "feishu_longconn_service.list_provider_models",
                return_value=["gpt-5.4", "gpt-5.3"],
            ), patch(
                "feishu_longconn_service.load_codex_default_model",
                return_value="gpt-5.4",
            ):
                service._handle_text("chat-1", "user-1", "/model")

            service._handle_text("chat-1", "user-1", "/sessions")
            service._handle_text("chat-1", "user-1", "1")

            self.assertEqual(state.get_active("user-1")[0], "sess-1")
            self.assertIsNone(state.get_selected_model("user-1"))

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

    def test_prompt_worker_stores_recent_attachments_when_answer_has_valid_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service, _, state, _ = self.build_service(root)
            image_path = root / "preview.png"
            image_path.write_bytes(b"image-bytes")

            service._run_prompt_worker(
                chat_id="chat-1",
                actor_id="user-1",
                prompt=f"请查看这个文件：{image_path}",
                active_id=None,
                cwd=root,
                session_label="新会话 | tmp",
            )

            attachments = state.get_recent_attachments("chat-1::user-1")
            self.assertEqual(len(attachments), 1)
            self.assertEqual(attachments[0]["path"], str(image_path))
            self.assertEqual(attachments[0]["kind"], "image")
            self.assertEqual(attachments[0]["name"], "preview.png")

    def test_prompt_worker_does_not_update_recent_attachments_when_no_valid_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service, _, state, _ = self.build_service(root)
            state.set_recent_attachments(
                "chat-1::user-1",
                [{"path": "/tmp/old.txt", "kind": "file", "name": "old.txt"}],
            )

            service._run_prompt_worker(
                chat_id="chat-1",
                actor_id="user-1",
                prompt="这次回复里没有可用附件路径",
                active_id=None,
                cwd=root,
                session_label="新会话 | tmp",
            )

            self.assertEqual(
                state.get_recent_attachments("chat-1::user-1"),
                [{"path": "/tmp/old.txt", "kind": "file", "name": "old.txt"}],
            )

    def test_prompt_worker_includes_preferred_attachment_output_dir_instruction(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service, _, _, codex = self.build_service(root)

            service._run_prompt_worker(
                chat_id="chat-1",
                actor_id="user-1",
                prompt="截一张现在电脑的屏幕",
                active_id=None,
                cwd=root,
                session_label="新会话 | tmp",
            )

            prompt = codex.calls[-1]["prompt"]
            self.assertIn(str(root / "attachments"), prompt)
            self.assertIn("优先保存到这个目录", prompt)
            self.assertIn("尽量不要保存到 ~/Desktop、~/Documents、~/Downloads，除非用户明确要求。", prompt)
            self.assertIn("最终回复里请使用绝对路径 Markdown 链接", prompt)
            self.assertNotIn("必须保存到这个目录", prompt)

    def test_send_intent_auto_sends_single_recent_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service, api, state, codex = self.build_service(root)
            image_path = root / "preview.png"
            image_path.write_bytes(b"image-bytes")
            state.set_recent_attachments(
                "chat-1::user-1",
                [{"path": str(image_path), "kind": "image", "name": "preview.png"}],
            )

            with patch("codex_common.Path.home", return_value=root):
                service._handle_text("chat-1", "user-1", "发给我")

            self.assertEqual(api.sent_images, [("chat-1", str(image_path))])
            self.assertEqual(api.sent_files, [])
            self.assertEqual(codex.calls, [])

    def test_send_intent_auto_sends_single_recent_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service, api, state, codex = self.build_service(root)
            file_path = root / "notes.txt"
            file_path.write_text("hello", encoding="utf-8")
            state.set_recent_attachments(
                "chat-1::user-1",
                [{"path": str(file_path), "kind": "file", "name": "notes.txt"}],
            )

            with patch("codex_common.Path.home", return_value=root):
                service._handle_text("chat-1", "user-1", "把文件发给我")

            self.assertEqual(api.sent_images, [])
            self.assertEqual(api.sent_files, [("chat-1", str(file_path))])
            self.assertEqual(codex.calls, [])

    def test_send_intent_sends_allowed_home_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            home_dir = root / "home"
            file_path = home_dir / "Documents" / "notes.txt"
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text("hello", encoding="utf-8")
            service, api, state, codex = self.build_service(root)
            state.set_recent_attachments(
                "chat-1::user-1",
                [{"path": str(file_path), "kind": "file", "name": "notes.txt"}],
            )

            with patch("codex_common.Path.home", return_value=home_dir):
                service._handle_text("chat-1", "user-1", "把这个发给我")

            self.assertEqual(api.sent_files, [("chat-1", str(file_path))])
            self.assertEqual(api.sent_images, [])
            self.assertEqual(codex.calls, [])

    def test_send_intent_sends_managed_attachment_candidate_outside_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            managed_path = root / "attachments" / "notes.txt"
            managed_path.parent.mkdir(parents=True, exist_ok=True)
            managed_path.write_text("hello", encoding="utf-8")
            service, api, state, codex = self.build_service(root)
            state.set_recent_attachments(
                "chat-1::user-1",
                [{"path": str(managed_path), "kind": "file", "name": "notes.txt"}],
            )

            with patch("codex_common.Path.home", return_value=root / "elsewhere-home"):
                service._handle_text("chat-1", "user-1", "把这个发给我")

            self.assertEqual(api.sent_files, [("chat-1", str(managed_path))])
            self.assertEqual(api.sent_images, [])
            self.assertEqual(codex.calls, [])

    def test_send_intent_rejects_sensitive_candidate_without_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            home_dir = root / "home"
            file_path = home_dir / ".ssh" / "id_rsa.txt"
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text("secret", encoding="utf-8")
            service, api, state, codex = self.build_service(root)
            state.set_recent_attachments(
                "chat-1::user-1",
                [{"path": str(file_path), "kind": "file", "name": "id_rsa.txt"}],
            )

            with patch("codex_common.Path.home", return_value=home_dir):
                service._handle_text("chat-1", "user-1", "发给我")

            self.assertEqual(api.sent_files, [])
            self.assertEqual(api.sent_images, [])
            self.assertEqual(codex.calls, [])
            self.assertIn("安全策略", api.sent_messages[-1][1])
            self.assertIn("不允许发送", api.sent_messages[-1][1])

    def test_send_intent_rejection_message_does_not_expose_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            home_dir = root / "home"
            file_path = home_dir / ".ssh" / "id_rsa.txt"
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text("secret", encoding="utf-8")
            service, api, state, codex = self.build_service(root)
            state.set_recent_attachments(
                "chat-1::user-1",
                [{"path": str(file_path), "kind": "file", "name": "id_rsa.txt"}],
            )

            with patch("codex_common.Path.home", return_value=home_dir):
                service._handle_text("chat-1", "user-1", "发给我")

            self.assertEqual(api.sent_files, [])
            self.assertEqual(api.sent_images, [])
            self.assertEqual(codex.calls, [])
            text = api.sent_messages[-1][1]
            self.assertIn("id_rsa.txt", text)
            self.assertNotIn(str(file_path), text)

    def test_send_intent_prefers_filename_hint_over_multiple_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            home_dir = root / "home"
            image_path = home_dir / "Pictures" / "preview.png"
            file_path = home_dir / "Documents" / "notes.txt"
            image_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            image_path.write_bytes(b"image-bytes")
            file_path.write_text("hello", encoding="utf-8")
            service, api, state, codex = self.build_service(root)
            state.set_recent_attachments(
                "chat-1::user-1",
                [
                    {"path": str(image_path), "kind": "image", "name": "preview.png"},
                    {"path": str(file_path), "kind": "file", "name": "notes.txt"},
                ],
            )

            with patch("codex_common.Path.home", return_value=home_dir):
                service._handle_text("chat-1", "user-1", "notes.txt，把这个发给我")

            self.assertEqual(api.sent_files, [("chat-1", str(file_path))])
            self.assertEqual(api.sent_images, [])
            self.assertEqual(codex.calls, [])
            self.assertFalse(state.is_pending_attachment_pick("chat-1::user-1"))

    def test_send_intent_unmatched_filename_hint_does_not_fallback_to_other_allowed_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            home_dir = root / "home"
            image_path = home_dir / "Pictures" / "preview.png"
            file_path = home_dir / "Documents" / "notes.txt"
            image_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            image_path.write_bytes(b"image-bytes")
            file_path.write_text("hello", encoding="utf-8")
            service, api, state, codex = self.build_service(root)
            state.set_recent_attachments(
                "chat-1::user-1",
                [
                    {"path": str(image_path), "kind": "image", "name": "preview.png"},
                    {"path": str(file_path), "kind": "file", "name": "notes.txt"},
                ],
            )

            with patch("codex_common.Path.home", return_value=home_dir):
                service._handle_text("chat-1", "user-1", "report.pdf，把这个发给我")

            self.assertEqual(api.sent_files, [])
            self.assertEqual(api.sent_images, [])
            self.assertEqual(codex.calls, [])
            self.assertIn("没有可发送", api.sent_messages[-1][1])
            self.assertFalse(state.is_pending_attachment_pick("chat-1::user-1"))

    def test_send_intent_hint_collision_uses_allowed_match_even_when_rejected_duplicate_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            home_dir = root / "home"
            allowed_path = home_dir / "Documents" / "report.pdf"
            rejected_path = home_dir / ".ssh" / "report.pdf"
            allowed_path.parent.mkdir(parents=True, exist_ok=True)
            rejected_path.parent.mkdir(parents=True, exist_ok=True)
            allowed_path.write_text("public", encoding="utf-8")
            rejected_path.write_text("secret", encoding="utf-8")
            service, api, state, codex = self.build_service(root)
            state.set_recent_attachments(
                "chat-1::user-1",
                [
                    {"path": str(rejected_path), "kind": "file", "name": "report.pdf"},
                    {"path": str(allowed_path), "kind": "file", "name": "report.pdf"},
                ],
            )

            with patch("codex_common.Path.home", return_value=home_dir):
                service._handle_text("chat-1", "user-1", "report.pdf，把这个发给我")

            self.assertEqual(api.sent_files, [("chat-1", str(allowed_path))])
            self.assertEqual(api.sent_images, [])
            self.assertEqual(codex.calls, [])
            self.assertEqual(api.sent_messages, [])
            self.assertFalse(state.is_pending_attachment_pick("chat-1::user-1"))

    def test_send_intent_hint_to_rejected_file_does_not_fallback_to_allowed_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            home_dir = root / "home"
            allowed_path = home_dir / "Documents" / "notes.txt"
            rejected_path = home_dir / ".ssh" / "id_rsa.txt"
            allowed_path.parent.mkdir(parents=True, exist_ok=True)
            rejected_path.parent.mkdir(parents=True, exist_ok=True)
            allowed_path.write_text("hello", encoding="utf-8")
            rejected_path.write_text("secret", encoding="utf-8")
            service, api, state, codex = self.build_service(root)
            state.set_recent_attachments(
                "chat-1::user-1",
                [
                    {"path": str(allowed_path), "kind": "file", "name": "notes.txt"},
                    {"path": str(rejected_path), "kind": "file", "name": "id_rsa.txt"},
                ],
            )

            with patch("codex_common.Path.home", return_value=home_dir):
                service._handle_text("chat-1", "user-1", "id_rsa.txt，把这个发给我")

            self.assertEqual(api.sent_files, [])
            self.assertEqual(api.sent_images, [])
            self.assertEqual(codex.calls, [])
            self.assertIn("安全策略", api.sent_messages[-1][1])
            self.assertIn("不允许发送", api.sent_messages[-1][1])
            self.assertIn("id_rsa.txt", api.sent_messages[-1][1])

    def test_send_intent_without_candidate_returns_helpful_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service, api, _, codex = self.build_service(root)

            service._handle_text("chat-1", "user-1", "发给我")

            self.assertEqual(api.sent_images, [])
            self.assertEqual(api.sent_files, [])
            self.assertEqual(codex.calls, [])
            self.assertIn("没有可发送", api.sent_messages[-1][1])

    def test_send_intent_permission_error_returns_helpful_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service, api, state, codex = self.build_service(root)
            image_path = root / "preview.png"
            image_path.write_bytes(b"image-bytes")
            state.set_recent_attachments(
                "chat-1::user-1",
                [{"path": str(image_path), "kind": "image", "name": "preview.png"}],
            )

            def _raise_permission_error(_chat_id, _path):
                raise PermissionError("Operation not permitted")

            api.send_image_path = _raise_permission_error

            with patch("codex_common.Path.home", return_value=root):
                service._handle_text("chat-1", "user-1", "发给我")

            self.assertEqual(codex.calls, [])
            text = api.sent_messages[-1][1]
            self.assertIn("飞书服务缺少系统权限", text)
            self.assertIn("系统设置", text)
            self.assertNotIn(str(image_path), text)

    def test_send_intent_permission_shaped_oserror_returns_helpful_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service, api, state, codex = self.build_service(root)
            image_path = root / "preview.png"
            image_path.write_bytes(b"image-bytes")
            state.set_recent_attachments(
                "chat-1::user-1",
                [{"path": str(image_path), "kind": "image", "name": "preview.png"}],
            )

            def _raise_os_error(_chat_id, _path):
                raise OSError("Operation not permitted")

            api.send_image_path = _raise_os_error

            with patch("codex_common.Path.home", return_value=root):
                service._handle_text("chat-1", "user-1", "发给我")

            self.assertEqual(codex.calls, [])
            text = api.sent_messages[-1][1]
            self.assertIn("飞书服务缺少系统权限", text)
            self.assertIn("系统设置", text)
            self.assertNotIn("附件读取失败", text)
            self.assertNotIn(str(image_path), text)

    def test_send_intent_preflight_permission_failures_return_helpful_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service, api, state, codex = self.build_service(root)
            image_path = root / "preview.png"
            image_path.write_bytes(b"image-bytes")
            state.set_recent_attachments(
                "chat-1::user-1",
                [{"path": str(image_path), "kind": "image", "name": "preview.png"}],
            )

            for method_name in ("exists", "is_file"):
                for exc in (PermissionError("Operation not permitted"), OSError("permission denied")):
                    with self.subTest(method_name=method_name, exc_type=type(exc).__name__):
                        original_method = getattr(service_module.Path, method_name)

                        def _raise_permission_error(path_obj, _exc=exc):
                            if path_obj == image_path:
                                raise _exc
                            return original_method(path_obj)

                        api.sent_messages.clear()
                        api.sent_images.clear()
                        api.sent_files.clear()
                        codex.calls.clear()

                        with patch(
                            f"feishu_longconn_service.Path.{method_name}",
                            new=_raise_permission_error,
                        ), patch("codex_common.Path.home", return_value=root):
                            service._handle_text("chat-1", "user-1", "发给我")

                        self.assertEqual(codex.calls, [])
                        self.assertEqual(api.sent_images, [])
                        self.assertEqual(api.sent_files, [])
                        text = api.sent_messages[-1][1]
                        self.assertIn("飞书服务缺少系统权限", text)
                        self.assertIn("系统设置", text)
                        self.assertNotIn(str(image_path), text)

    def test_send_intent_with_multiple_candidates_prompts_for_number(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service, api, state, codex = self.build_service(root)
            image_path = root / "preview.png"
            file_path = root / "notes.txt"
            image_path.write_bytes(b"image-bytes")
            file_path.write_text("hello", encoding="utf-8")
            state.set_recent_attachments(
                "chat-1::user-1",
                [
                    {"path": str(image_path), "kind": "image", "name": "preview.png"},
                    {"path": str(file_path), "kind": "file", "name": "notes.txt"},
                ],
            )

            with patch("codex_common.Path.home", return_value=root):
                service._handle_text("chat-1", "user-1", "发给我")

            self.assertEqual(api.sent_images, [])
            self.assertEqual(api.sent_files, [])
            self.assertEqual(codex.calls, [])
            self.assertTrue(state.is_pending_attachment_pick("chat-1::user-1"))
            text = api.sent_messages[-1][1]
            self.assertIn("1. preview.png", text)
            self.assertIn("2. notes.txt", text)
            self.assertIn("回复编号", text)

    def test_numeric_attachment_pick_sends_selected_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service, api, state, codex = self.build_service(root)
            image_path = root / "preview.png"
            file_path = root / "notes.txt"
            image_path.write_bytes(b"image-bytes")
            file_path.write_text("hello", encoding="utf-8")
            state.set_recent_attachments(
                "chat-1::user-1",
                [
                    {"path": str(image_path), "kind": "image", "name": "preview.png"},
                    {"path": str(file_path), "kind": "file", "name": "notes.txt"},
                ],
            )

            with patch("codex_common.Path.home", return_value=root):
                service._handle_text("chat-1", "user-1", "发给我")
                service._handle_text("chat-1", "user-1", "2")

            self.assertEqual(api.sent_images, [])
            self.assertEqual(api.sent_files, [("chat-1", str(file_path))])
            self.assertEqual(codex.calls, [])
            self.assertFalse(state.is_pending_attachment_pick("chat-1::user-1"))

    def test_invalid_attachment_pick_returns_error_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service, api, state, codex = self.build_service(root)
            file_path = root / "notes.txt"
            file_path.write_text("hello", encoding="utf-8")
            state.set_attachment_picker(
                "chat-1::user-1",
                [{"path": str(file_path), "kind": "file", "name": "notes.txt"}],
            )

            service._handle_text("chat-1", "user-1", "9")

            self.assertEqual(api.sent_images, [])
            self.assertEqual(api.sent_files, [])
            self.assertEqual(codex.calls, [])
            self.assertTrue(state.is_pending_attachment_pick("chat-1::user-1"))
            self.assertIn("附件编号无效", api.sent_messages[-1][1])

    def test_unrelated_text_clears_pending_attachment_pick(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service, _, state, _ = self.build_service(root)
            file_path = root / "notes.txt"
            file_path.write_text("hello", encoding="utf-8")
            state.set_attachment_picker(
                "chat-1::user-1",
                [{"path": str(file_path), "kind": "file", "name": "notes.txt"}],
            )

            with patch.object(service, "_run_prompt") as mock_run_prompt:
                service._handle_text("chat-1", "user-1", "继续处理别的事")

            self.assertFalse(state.is_pending_attachment_pick("chat-1::user-1"))
            mock_run_prompt.assert_called_once_with("chat-1", "user-1", "继续处理别的事")


class FeishuStartupPermissionProbeTests(unittest.TestCase):
    def test_probe_home_directory_permissions_marks_blocked_folders(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home_dir = Path(tmpdir)
            for folder in ("Desktop", "Documents", "Downloads"):
                (home_dir / folder).mkdir(parents=True, exist_ok=True)

            def _raise_permission_error(path):
                raise PermissionError(f"Operation not permitted: {path}")

            with patch("feishu_longconn_service.Path.home", return_value=home_dir), patch(
                "feishu_longconn_service.os.scandir",
                side_effect=_raise_permission_error,
            ):
                result = service_module._probe_home_directory_permissions()

            self.assertEqual(result["Desktop"]["status"], "blocked")
            self.assertEqual(result["Documents"]["status"], "blocked")
            self.assertEqual(result["Downloads"]["status"], "blocked")

    def test_probe_home_directory_permissions_marks_missing_folders_without_blocked_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home_dir = Path(tmpdir)

            with patch("feishu_longconn_service.Path.home", return_value=home_dir):
                result = service_module._probe_home_directory_permissions()

            self.assertEqual(result["Desktop"]["status"], "missing")
            self.assertEqual(result["Documents"]["status"], "missing")
            self.assertEqual(result["Downloads"]["status"], "missing")
            self.assertIn("FileNotFoundError", result["Desktop"]["error"])
            self.assertIn("FileNotFoundError", result["Documents"]["error"])
            self.assertIn("FileNotFoundError", result["Downloads"]["error"])

    def test_run_forever_logs_permission_probe_result_before_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service, _, _, _ = FeishuModelAccountTests().build_service(root)

            class _FakeWsClient:
                def __init__(self) -> None:
                    self.started = False

                def start(self) -> None:
                    self.started = True

            ws_client = _FakeWsClient()
            service.ws_client = ws_client
            probe_result = {
                "Desktop": {"path": "/Users/demo/Desktop", "status": "blocked", "error": "PermissionError: denied"},
                "Documents": {"path": "/Users/demo/Documents", "status": "readable"},
                "Downloads": {"path": "/Users/demo/Downloads", "status": "blocked", "error": "PermissionError: denied"},
            }

            with patch(
                "feishu_longconn_service._probe_home_directory_permissions",
                return_value=probe_result,
            ) as mock_probe, patch("feishu_longconn_service.log") as mock_log:
                service.run_forever()

            self.assertTrue(ws_client.started)
            mock_probe.assert_called_once_with()
            self.assertTrue(
                any("folder=Desktop" in str(call) and "status=blocked" in str(call) for call in mock_log.call_args_list)
            )
            self.assertTrue(
                any("folder=Documents" in str(call) and "status=readable" in str(call) for call in mock_log.call_args_list)
            )
            self.assertTrue(
                any("folder=Downloads" in str(call) and "status=blocked" in str(call) for call in mock_log.call_args_list)
            )

    def test_run_forever_starts_ws_client_when_permission_probe_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service, _, _, _ = FeishuModelAccountTests().build_service(root)

            class _FakeWsClient:
                def __init__(self) -> None:
                    self.started = False

                def start(self) -> None:
                    self.started = True

            ws_client = _FakeWsClient()
            service.ws_client = ws_client

            with patch(
                "feishu_longconn_service._probe_home_directory_permissions",
                side_effect=RuntimeError("probe exploded"),
            ) as mock_probe, patch("feishu_longconn_service.log") as mock_log:
                service.run_forever()

            self.assertTrue(ws_client.started)
            mock_probe.assert_called_once_with()
            self.assertTrue(any("startup permission probe failed" in str(call) for call in mock_log.call_args_list))
            self.assertTrue(any("probe exploded" in str(call) for call in mock_log.call_args_list))


if __name__ == "__main__":
    unittest.main()
