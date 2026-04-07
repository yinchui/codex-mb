import tempfile
import unittest
from pathlib import Path

from feishu_launchd_bootstrap import build_service_environment, load_env_file_via_bash


class FeishuLaunchdBootstrapTests(unittest.TestCase):
    def test_load_env_file_via_bash_preserves_preset_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / "feishu.env"
            env_file.write_text(
                "\n".join(
                    [
                        'FEISHU_APP_ID="from-file"',
                        'CODEX_BIN="$HOME/bin/codex"',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            loaded = load_env_file_via_bash(
                env_file,
                {
                    "HOME": "/Users/example",
                    "FEISHU_APP_ID": "from-env",
                    "PATH": "/usr/bin:/bin",
                },
            )

            self.assertEqual(loaded["FEISHU_APP_ID"], "from-env")
            self.assertEqual(loaded["CODEX_BIN"], "/Users/example/bin/codex")

    def test_build_service_environment_sets_runtime_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = build_service_environment(
                {
                    "HOME": tmpdir,
                    "PATH": "/usr/bin:/bin",
                }
            )

            runtime_dir = Path(tmpdir) / ".local" / "state" / "codex-tg" / "feishu"
            self.assertEqual(env["FEISHU_RUNTIME_DIR"], str(runtime_dir))
            self.assertEqual(env["STATE_PATH"], str(runtime_dir / "feishu_bot_state.json"))


if __name__ == "__main__":
    unittest.main()
