import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from codex_common import BotState, SessionStore
from wechat_codex_service import (
    WechatAPI,
    WechatAccountStore,
    WechatCodexService,
    extract_text_from_item_list,
    parse_allowed_wechat_user_ids,
)


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


class FakeCodexRunner:
    def __init__(self) -> None:
        self.calls = []

    def run_prompt(self, prompt, cwd, session_id=None, on_update=None):
        self.calls.append((prompt, str(cwd), session_id))
        return ("thread-123", f"answer:{prompt}", "", 0)


class FakeCodexRunnerSyncClaim:
    def __init__(self) -> None:
        self.calls = []

    def run_prompt(self, prompt, cwd, session_id=None, on_update=None):
        self.calls.append((prompt, str(cwd), session_id))
        return ("thread-123", "已同步到 Obsidian，文件在 学习笔记/AI行业观察/note.md。", "", 0)


class FakeCodexRunnerSyncClaimExistingNote:
    def __init__(self) -> None:
        self.calls = []

    def run_prompt(self, prompt, cwd, session_id=None, on_update=None):
        self.calls.append((prompt, str(cwd), session_id))
        return (
            "thread-123",
            "已同步到 Obsidian。\n\n文件路径是 2026-03-30-近期AI革命新闻与人类重启.md，存放在 `学习笔记/AI行业观察/`。",
            "",
            0,
        )


class FakeCodexRunnerRetrySync:
    def __init__(self) -> None:
        self.calls = []

    def run_prompt(self, prompt, cwd, session_id=None, on_update=None):
        self.calls.append((prompt, str(cwd), session_id))
        root = Path(cwd)
        if len(self.calls) == 1:
            return (
                "thread-first",
                "已同步到 Obsidian。\n\n文件路径是 2026-03-30-近期AI革命新闻与人类重启.md，存放在 `学习笔记/AI行业观察/`。",
                "",
                0,
            )
        note_path = root / "学习笔记" / "AI行业观察" / "2026-03-30-近期AI革命新闻与人类重启.md"
        index_path = note_path.parent / "_index.md"
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text("# repaired sync\n", encoding="utf-8")
        index_path.write_text(
            "- [[2026-03-30-近期AI革命新闻与人类重启.md]] - repaired entry\n",
            encoding="utf-8",
        )
        return (
            "thread-retry",
            "已同步到 Obsidian。\n\n文件路径是 2026-03-30-近期AI革命新闻与人类重启.md，存放在 `学习笔记/AI行业观察/`。",
            "",
            0,
        )


class RecordingWechatAPI:
    def __init__(self) -> None:
        self.sent = []

    def send_text(self, to_user_id: str, context_token: str, text: str) -> str:
        self.sent.append((to_user_id, context_token, text))
        return f"mid-{len(self.sent)}"

    def get_config(self, ilink_user_id: str, context_token: str):
        return {"ret": 0, "typing_ticket": "ticket-1"}

    def send_typing(self, ilink_user_id: str, typing_ticket: str, status: int) -> None:
        self.sent.append((ilink_user_id, typing_ticket, f"typing:{status}"))


class RecordingWechatService(WechatCodexService):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.prompt_requests = []

    def _run_prompt(self, actor_id: str, context_token: str, prompt: str) -> None:
        self.prompt_requests.append((actor_id, context_token, prompt))


class WechatApiProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.requests = []

        requests = self.requests

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed_path = self.path.split("?", 1)[0]
                if parsed_path == "/ilink/bot/get_bot_qrcode":
                    body = {"qrcode": "qr-1", "qrcode_img_content": "https://example.com/qr-1"}
                elif parsed_path == "/ilink/bot/get_qrcode_status":
                    body = {
                        "status": "confirmed",
                        "bot_token": "bot-token",
                        "ilink_bot_id": "bot@im.bot",
                        "ilink_user_id": "user@im.wechat",
                        "baseurl": f"http://127.0.0.1:{self.server.server_port}",
                    }
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                requests.append((self.command, self.path, dict(self.headers), None))
                raw = json.dumps(body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(length).decode("utf-8")
                body = json.loads(raw_body)
                requests.append((self.command, self.path, dict(self.headers), body))
                if self.path == "/ilink/bot/getupdates":
                    resp = {
                        "ret": 0,
                        "msgs": [
                            {
                                "message_type": 1,
                                "message_id": 9,
                                "from_user_id": "user@im.wechat",
                                "context_token": "ctx-1",
                                "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
                            }
                        ],
                        "get_updates_buf": "buf-2",
                    }
                elif self.path == "/ilink/bot/sendmessage":
                    resp = {}
                elif self.path == "/ilink/bot/getconfig":
                    resp = {"ret": 0, "typing_ticket": "ticket-1"}
                elif self.path == "/ilink/bot/sendtyping":
                    resp = {"ret": 0}
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                raw = json.dumps(resp).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, format, *args):  # noqa: A003
                return

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()

    def test_wechat_api_endpoints(self) -> None:
        api = WechatAPI(self.base_url, token="bot-token")
        login = api.start_login("3")
        self.assertEqual(login["qrcode"], "qr-1")
        status = api.get_qrcode_status("qr-1")
        self.assertEqual(status["status"], "confirmed")
        updates = api.get_updates("buf-1", timeout_sec=10)
        self.assertEqual(updates["get_updates_buf"], "buf-2")
        api.send_text("user@im.wechat", "ctx-1", "hello world")
        config = api.get_config("user@im.wechat", "ctx-1")
        self.assertEqual(config["typing_ticket"], "ticket-1")
        api.send_typing("user@im.wechat", "ticket-1", 1)

        auth_posts = [req for req in self.requests if req[0] == "POST"]
        self.assertTrue(any(req[1] == "/ilink/bot/getupdates" for req in auth_posts))
        for _, path, headers, _ in auth_posts:
            normalized = {k.lower(): v for k, v in headers.items()}
            self.assertEqual(normalized.get("authorizationtype"), "ilink_bot_token", path)
            self.assertEqual(normalized.get("authorization"), "Bearer bot-token", path)
            self.assertIn("x-wechat-uin", normalized, path)


class WechatServiceTests(unittest.TestCase):
    def test_parse_allowed_ids(self) -> None:
        self.assertEqual(parse_allowed_wechat_user_ids("a,b"), {"a", "b"})
        self.assertIsNone(parse_allowed_wechat_user_ids(""))

    def test_extract_text_from_item_list(self) -> None:
        text = extract_text_from_item_list(
            [{"type": 1, "text_item": {"text": "  hi  "}}, {"type": 1, "text_item": {"text": "ignored"}}]
        )
        self.assertEqual(text, "hi")

    def test_account_store_persists_token_and_buf(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WechatAccountStore(Path(tmpdir))
            store.save_account({"token": "abc", "base_url": "https://example.com"})
            store.save_get_updates_buf("buf-1")
            self.assertTrue(store.has_token())
            self.assertEqual(store.token(), "abc")
            self.assertEqual(store.base_url(), "https://example.com")
            self.assertEqual(store.load_get_updates_buf(), "buf-1")

    def test_command_dispatch_and_session_pick(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sessions_root = root / "sessions"
            write_session_file(sessions_root, "sess-1", str(root), "first prompt")
            project_beta = root / "project-beta"
            project_beta.mkdir(parents=True, exist_ok=True)
            write_codex_global_state(root, [root, project_beta])
            api = RecordingWechatAPI()
            state = BotState(root / "state.json")
            service = RecordingWechatService(
                api=api,
                sessions=SessionStore(sessions_root),
                state=state,
                codex=FakeCodexRunner(),
                default_cwd=root,
                allowed_user_ids={"user@im.wechat"},
                poll_timeout_sec=35,
                send_typing_enabled=False,
                account_store=WechatAccountStore(root / "wechat"),
            )

            with patch.dict("os.environ", {"HOME": str(root)}, clear=False):
                service._handle_message(
                    {
                        "message_type": 1,
                        "message_id": 1,
                        "from_user_id": "user@im.wechat",
                        "context_token": "ctx-1",
                        "item_list": [{"type": 1, "text_item": {"text": "/sessions"}}],
                    }
                )
                service._handle_message(
                    {
                        "message_type": 1,
                        "message_id": 2,
                        "from_user_id": "user@im.wechat",
                        "context_token": "ctx-2",
                        "item_list": [{"type": 1, "text_item": {"text": "1"}}],
                    }
                )
                service._handle_message(
                    {
                        "message_type": 1,
                        "message_id": 3,
                        "from_user_id": "user@im.wechat",
                        "context_token": "ctx-3",
                        "item_list": [{"type": 1, "text_item": {"text": "1"}}],
                    }
                )

            self.assertTrue(any("工作区列表" in text for _, _, text in api.sent))
            self.assertTrue(any(f"工作区: {root}" in text for _, _, text in api.sent))
            self.assertTrue(any("0. 新建会话" in text for _, _, text in api.sent))

            active_id, _ = state.get_active("user@im.wechat")
            self.assertEqual(active_id, "sess-1")

            service._handle_message(
                {
                    "message_type": 1,
                    "message_id": 4,
                    "from_user_id": "user@im.wechat",
                    "context_token": "ctx-4",
                    "item_list": [{"type": 1, "text_item": {"text": "继续这个会话"}}],
                }
            )
            self.assertEqual(service.prompt_requests[-1], ("user@im.wechat", "ctx-4", "继续这个会话"))

    def test_workspace_picker_can_enter_new_session_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            project_beta = root / "project-beta"
            project_beta.mkdir(parents=True, exist_ok=True)
            write_codex_global_state(root, [root, project_beta])

            api = RecordingWechatAPI()
            state = BotState(root / "state.json")
            service = RecordingWechatService(
                api=api,
                sessions=SessionStore(root / "sessions"),
                state=state,
                codex=FakeCodexRunner(),
                default_cwd=root,
                allowed_user_ids={"user@im.wechat"},
                poll_timeout_sec=35,
                send_typing_enabled=False,
                account_store=WechatAccountStore(root / "wechat"),
            )

            with patch.dict("os.environ", {"HOME": str(root)}, clear=False):
                service._handle_message(
                    {
                        "message_type": 1,
                        "message_id": 1,
                        "from_user_id": "user@im.wechat",
                        "context_token": "ctx-1",
                        "item_list": [{"type": 1, "text_item": {"text": "/sessions"}}],
                    }
                )
                service._handle_message(
                    {
                        "message_type": 1,
                        "message_id": 2,
                        "from_user_id": "user@im.wechat",
                        "context_token": "ctx-2",
                        "item_list": [{"type": 1, "text_item": {"text": "2"}}],
                    }
                )
                service._handle_message(
                    {
                        "message_type": 1,
                        "message_id": 3,
                        "from_user_id": "user@im.wechat",
                        "context_token": "ctx-3",
                        "item_list": [{"type": 1, "text_item": {"text": "0"}}],
                    }
                )

            self.assertTrue(any("0. 新建会话" in text for _, _, text in api.sent))
            self.assertTrue(any(f"工作区: {project_beta}" in text for _, _, text in api.sent))
            self.assertTrue(any("下一条普通消息会新建 session" in text for _, _, text in api.sent))

            active_id, active_cwd = state.get_active("user@im.wechat")
            self.assertIsNone(active_id)
            self.assertEqual(active_cwd, str(project_beta))

    def test_prompt_worker_sends_final_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            api = RecordingWechatAPI()
            service = WechatCodexService(
                api=api,
                sessions=SessionStore(root / "sessions"),
                state=BotState(root / "state.json"),
                codex=FakeCodexRunner(),
                default_cwd=root,
                allowed_user_ids={"user@im.wechat"},
                poll_timeout_sec=35,
                send_typing_enabled=False,
                account_store=WechatAccountStore(root / "wechat"),
            )
            service._run_prompt("user@im.wechat", "ctx-final", "hello")
            deadline = time.time() + 2
            while time.time() < deadline:
                if any("answer:hello" in text for _, _, text in api.sent):
                    break
                time.sleep(0.05)
            self.assertFalse(
                any("已开始处理" in text for _, _, text in api.sent),
                "wechat prompt should not emit the removed ack text",
            )
            self.assertTrue(any("answer:hello" in text for _, _, text in api.sent))

    def test_prompt_worker_warns_when_obsidian_sync_has_no_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            api = RecordingWechatAPI()
            service = WechatCodexService(
                api=api,
                sessions=SessionStore(root / "sessions"),
                state=BotState(root / "state.json"),
                codex=FakeCodexRunnerSyncClaim(),
                default_cwd=root,
                allowed_user_ids={"user@im.wechat"},
                poll_timeout_sec=35,
                send_typing_enabled=False,
                account_store=WechatAccountStore(root / "wechat"),
            )

            service._run_prompt_worker(
                "user@im.wechat",
                "ctx-sync",
                "把这条内容整理成 Obsidian 笔记，写入 学习笔记/AI行业观察，并更新 _index.md。",
                None,
                root,
                "新会话 | vault",
            )

            self.assertTrue(any("未检测到当前工作区内有任何文件改动" in text for _, _, text in api.sent))
            self.assertTrue(any("模型原始回复" in text for _, _, text in api.sent))

    def test_prompt_worker_accepts_existing_note_and_index_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            note_path = root / "学习笔记" / "AI行业观察" / "2026-03-30-近期AI革命新闻与人类重启.md"
            index_path = note_path.parent / "_index.md"
            note_path.parent.mkdir(parents=True, exist_ok=True)
            note_path.write_text("# existing note\n", encoding="utf-8")
            index_path.write_text(
                "- [[2026-03-30-近期AI革命新闻与人类重启.md]] - existing entry\n",
                encoding="utf-8",
            )

            api = RecordingWechatAPI()
            service = WechatCodexService(
                api=api,
                sessions=SessionStore(root / "sessions"),
                state=BotState(root / "state.json"),
                codex=FakeCodexRunnerSyncClaimExistingNote(),
                default_cwd=root,
                allowed_user_ids={"user@im.wechat"},
                poll_timeout_sec=35,
                send_typing_enabled=False,
                account_store=WechatAccountStore(root / "wechat"),
            )

            service._run_prompt_worker(
                "user@im.wechat",
                "ctx-sync-existing",
                "3.00 复制打开抖音，帮我同步到obsidian",
                None,
                root,
                "新会话 | vault",
            )

            self.assertFalse(any("未检测到当前工作区内有任何文件改动" in text for _, _, text in api.sent))
            self.assertTrue(any("2026-03-30-近期AI革命新闻与人类重启.md" in text for _, _, text in api.sent))

    def test_prompt_worker_retries_in_fresh_session_after_unverified_sync_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            codex = FakeCodexRunnerRetrySync()
            api = RecordingWechatAPI()
            service = WechatCodexService(
                api=api,
                sessions=SessionStore(root / "sessions"),
                state=BotState(root / "state.json"),
                codex=codex,
                default_cwd=root,
                allowed_user_ids={"user@im.wechat"},
                poll_timeout_sec=35,
                send_typing_enabled=False,
                account_store=WechatAccountStore(root / "wechat"),
            )

            service._run_prompt_worker(
                "user@im.wechat",
                "ctx-sync-retry",
                "3.00 复制打开抖音，帮我同步到obsidian",
                "existing-session",
                root,
                "当前会话 | vault",
            )

            self.assertEqual(len(codex.calls), 2)
            self.assertEqual(codex.calls[0][2], "existing-session")
            self.assertIsNone(codex.calls[1][2])
            self.assertTrue(any("2026-03-30-近期AI革命新闻与人类重启.md" in text for _, _, text in api.sent))
            self.assertFalse(any("未检测到当前工作区内有任何文件改动" in text for _, _, text in api.sent))


if __name__ == "__main__":
    unittest.main()
