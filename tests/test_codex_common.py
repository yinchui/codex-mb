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
    fetch_provider_account_info,
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
