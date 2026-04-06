import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from codex_common import (
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


if __name__ == "__main__":
    unittest.main()
