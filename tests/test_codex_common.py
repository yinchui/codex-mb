import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from codex_common import (
    BotState,
    CodexRunner,
    ProviderLookupError,
    extract_attachment_name_hints,
    extract_local_attachment_candidates,
    fetch_provider_account_info,
    is_allowed_home_attachment_path,
    is_attachment_send_intent,
    list_provider_models,
    load_codex_api_key,
    load_codex_base_url,
)


class FakeHTTPResponse:
    def __init__(self, payload, status: int = 200) -> None:
        self.status = status
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class ProviderConfigTests(unittest.TestCase):
    def test_load_codex_base_url_reads_active_custom_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text(
                "\n".join(
                    [
                        'model_provider = "custom"',
                        "",
                        "[model_providers.custom]",
                        'base_url = "https://yunyi.rdzhvip.com/codex"',
                    ]
                ),
                encoding="utf-8",
            )

            base_url = load_codex_base_url(config_path=config_path)

            self.assertEqual(base_url, "https://yunyi.rdzhvip.com/codex")

    def test_load_codex_api_key_reads_openai_auth_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            auth_path = Path(tmpdir) / "auth.json"
            auth_path.write_text(json.dumps({"OPENAI_API_KEY": "sk-test-123"}), encoding="utf-8")

            api_key = load_codex_api_key(auth_path=auth_path)

            self.assertEqual(api_key, "sk-test-123")


class ProviderApiTests(unittest.TestCase):
    def test_list_provider_models_returns_model_ids(self) -> None:
        payload = {
            "object": "list",
            "data": [
                {"id": "gpt-5.4", "object": "model"},
                {"id": "gpt-5.3", "object": "model"},
            ],
        }

        with patch("urllib.request.urlopen", return_value=FakeHTTPResponse(payload)):
            models = list_provider_models(
                base_url="https://yunyi.rdzhvip.com/codex",
                api_key="sk-test",
            )

        self.assertEqual(models, ["gpt-5.4", "gpt-5.3"])

    def test_fetch_provider_account_info_keeps_daily_quota_fields(self) -> None:
        payload = {
            "service_type": "codex",
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

        with patch("urllib.request.urlopen", return_value=FakeHTTPResponse(payload)):
            info = fetch_provider_account_info(
                base_url="https://yunyi.rdzhvip.com/codex",
                api_key="sk-test",
            )

        self.assertEqual(info["quota"]["daily_remaining"], 8760)
        self.assertEqual(info["quota"]["daily_quota"], 9000)
        self.assertEqual(info["usage"]["daily_request_count"], 40)

    def test_list_provider_models_wraps_http_errors(self) -> None:
        error = HTTPError(
            url="https://yunyi.rdzhvip.com/codex/v1/models",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=None,
        )

        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(ProviderLookupError):
                list_provider_models(
                    base_url="https://yunyi.rdzhvip.com/codex",
                    api_key="sk-test",
                )


class BotStateModelTests(unittest.TestCase):
    def test_bot_state_persists_selected_model_per_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = BotState(Path(tmpdir) / "state.json")

            state.set_selected_model("user-1", "gpt-5.4")

            self.assertEqual(state.get_selected_model("user-1"), "gpt-5.4")
            self.assertIsNone(state.get_selected_model("user-2"))

    def test_bot_state_tracks_pending_model_picker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = BotState(Path(tmpdir) / "state.json")

            state.set_model_picker("user-1", ["gpt-5.4", "gpt-5.3"])

            self.assertTrue(state.is_pending_model_pick("user-1"))
            self.assertEqual(state.get_model_picker("user-1")["models"], ["gpt-5.4", "gpt-5.3"])

            state.clear_model_picker("user-1")

            self.assertFalse(state.is_pending_model_pick("user-1"))
            self.assertEqual(state.get_model_picker("user-1"), {})


class AttachmentHelpersTests(unittest.TestCase):
    def test_is_allowed_home_attachment_path_accepts_supported_file_under_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            target = home / "Desktop" / "paper.pdf"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("ok", encoding="utf-8")

            with patch("codex_common.Path.home", return_value=home):
                allowed, reason = is_allowed_home_attachment_path(target)

            self.assertTrue(allowed)
            self.assertIsNone(reason)

    def test_is_allowed_home_attachment_path_rejects_unsupported_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            target = home / "Desktop" / "archive.bin"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("no", encoding="utf-8")

            with patch("codex_common.Path.home", return_value=home):
                allowed, reason = is_allowed_home_attachment_path(target)

            self.assertFalse(allowed)
            self.assertEqual(reason, "unsupported_type")

    def test_is_allowed_home_attachment_path_rejects_outside_home(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir, tempfile.TemporaryDirectory() as outside_dir:
            home = Path(home_dir)
            target = Path(outside_dir) / "a.pdf"
            target.write_text("x", encoding="utf-8")

            with patch("codex_common.Path.home", return_value=home):
                allowed, reason = is_allowed_home_attachment_path(target)

            self.assertFalse(allowed)
            self.assertEqual(reason, "outside_home")

    def test_is_allowed_home_attachment_path_rejects_sensitive_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            samples = [
                home / ".ssh" / "config.txt",
                home / ".gnupg" / "a.txt",
                home / ".aws" / "credentials.txt",
                home / "Desktop" / ".env.txt",
                home / "Desktop" / "id_rsa.txt",
                home / "Desktop" / "id_ed25519.md",
                home / "Desktop" / "keychain.txt",
                home / "Desktop" / "private_key.txt",
                home / "Desktop" / "secret_notes.txt",
                home / "Desktop" / "token_store.txt",
                home / "Desktop" / "credential_dump.txt",
            ]
            for path in samples:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x", encoding="utf-8")
                with patch("codex_common.Path.home", return_value=home):
                    allowed, reason = is_allowed_home_attachment_path(path)
                self.assertFalse(allowed)
                self.assertEqual(reason, "sensitive_path")

    def test_extract_attachment_name_hints_finds_supported_file_names(self) -> None:
        text = "Kimi_Attention_Residuals_2603.15031.pdf，把这个发给我"
        self.assertEqual(
            extract_attachment_name_hints(text),
            ["Kimi_Attention_Residuals_2603.15031.pdf"],
        )

    def test_extract_attachment_name_hints_matches_when_adjacent_to_chinese_text(self) -> None:
        self.assertEqual(
            extract_attachment_name_hints("把Kimi_Attention_Residuals_2603.15031.pdf发给我"),
            ["Kimi_Attention_Residuals_2603.15031.pdf"],
        )
        self.assertEqual(
            extract_attachment_name_hints("文件Kimi_Attention_Residuals_2603.15031.pdf"),
            ["Kimi_Attention_Residuals_2603.15031.pdf"],
        )

    def test_extract_local_attachment_candidates_keeps_existing_absolute_supported_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            image_path = root / "screen.png"
            note_path = root / "notes.md"
            unsupported_path = root / "raw.bin"
            relative_path = Path("local.txt")
            missing_path = root / "missing.pdf"

            image_path.write_text("png", encoding="utf-8")
            note_path.write_text("hello", encoding="utf-8")
            unsupported_path.write_text("bin", encoding="utf-8")
            (root / relative_path).write_text("rel", encoding="utf-8")

            text = "\n".join(
                [
                    f"image: {image_path}",
                    f"note: `{note_path}`",
                    f"unsupported: {unsupported_path}",
                    f"relative: {relative_path}",
                    f"missing: {missing_path}",
                ]
            )

            candidates = extract_local_attachment_candidates(text)

            self.assertEqual(
                candidates,
                [
                    {"path": str(image_path), "kind": "image", "name": "screen.png"},
                    {"path": str(note_path), "kind": "file", "name": "notes.md"},
                ],
            )

    def test_is_attachment_send_intent_matches_explicit_phrases(self) -> None:
        self.assertTrue(is_attachment_send_intent("发给我"))
        self.assertTrue(is_attachment_send_intent("把图片发我"))
        self.assertTrue(is_attachment_send_intent("把文件发来"))
        self.assertTrue(is_attachment_send_intent("作为附件发送"))
        self.assertFalse(is_attachment_send_intent("你好，今天天气怎么样"))

    def test_extract_local_attachment_candidates_deduplicates_same_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            image_path = root / "same.png"
            image_path.write_text("png", encoding="utf-8")
            text = f"{image_path}\nagain: `{image_path}`"

            candidates = extract_local_attachment_candidates(text)

            self.assertEqual(
                candidates,
                [{"path": str(image_path), "kind": "image", "name": "same.png"}],
            )

    def test_extract_local_attachment_candidates_accepts_trailing_colon(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            image_path = root / "shot.png"
            image_path.write_text("png", encoding="utf-8")

            candidates = extract_local_attachment_candidates(f"path: {image_path}:")

            self.assertEqual(
                candidates,
                [{"path": str(image_path), "kind": "image", "name": "shot.png"}],
            )

    def test_bot_state_persists_recent_attachments_and_picker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = BotState(Path(tmpdir) / "state.json")
            key = "chat-1::user-1"
            attachments = [
                {"path": "/Users/aa/Desktop/a.png", "kind": "image", "name": "a.png"},
                {"path": "/Users/aa/Desktop/b.pdf", "kind": "file", "name": "b.pdf"},
            ]

            state.set_recent_attachments(key, attachments)
            state.set_attachment_picker(key, attachments)

            self.assertEqual(state.get_recent_attachments(key), attachments)
            self.assertTrue(state.is_pending_attachment_pick(key))
            self.assertEqual(state.get_attachment_picker(key).get("attachments"), attachments)

            state.clear_attachment_picker(key)

            self.assertFalse(state.is_pending_attachment_pick(key))
            self.assertEqual(state.get_attachment_picker(key), {})

    def test_bot_state_empty_attachment_picker_does_not_set_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = BotState(Path(tmpdir) / "state.json")
            key = "chat-1::user-2"

            state.set_attachment_picker(
                key,
                [
                    {},
                    {"path": "", "kind": "image", "name": "bad.png"},
                ],
            )

            self.assertFalse(state.is_pending_attachment_pick(key))
            self.assertEqual(state.get_attachment_picker(key), {})


class _FakePipe:
    def __iter__(self):
        return iter(())

    def close(self) -> None:
        return None


class _FakeProcess:
    def __init__(self) -> None:
        self.stdout = _FakePipe()
        self.stderr = _FakePipe()
        self.pid = 999

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


class CodexRunnerModelFlagTests(unittest.TestCase):
    def test_run_prompt_includes_model_flag_for_new_session(self) -> None:
        runner = CodexRunner(codex_bin="codex")

        with patch("subprocess.Popen", return_value=_FakeProcess()) as popen:
            runner.run_prompt(
                prompt="hello",
                cwd=Path("/tmp"),
                model="gpt-5.4",
            )

        cmd = popen.call_args.kwargs if popen.call_args and popen.call_args.kwargs else {}
        argv = popen.call_args.args[0]
        self.assertIn("-m", argv)
        self.assertIn("gpt-5.4", argv)
        self.assertLess(argv.index("-m"), argv.index("hello"))

    def test_run_prompt_includes_model_flag_for_resume(self) -> None:
        runner = CodexRunner(codex_bin="codex")

        with patch("subprocess.Popen", return_value=_FakeProcess()) as popen:
            runner.run_prompt(
                prompt="continue",
                cwd=Path("/tmp"),
                session_id="sess-1",
                model="gpt-5.3",
            )

        argv = popen.call_args.args[0]
        self.assertIn("resume", argv)
        self.assertIn("-m", argv)
        self.assertIn("gpt-5.3", argv)
        self.assertLess(argv.index("-m"), argv.index("sess-1"))


if __name__ == "__main__":
    unittest.main()
