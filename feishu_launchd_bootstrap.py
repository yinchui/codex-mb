#!/usr/bin/env python3
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, Mapping, Optional


def load_env_file_via_bash(env_file: Path, base_env: Mapping[str, str]) -> Dict[str, str]:
    loaded = {key: str(value) for key, value in dict(base_env).items()}
    if not env_file.is_file():
        return loaded

    command = 'set -a\nsource "$1"\nenv -0\n'
    completed = subprocess.run(
        ["/bin/bash", "-lc", command, "bash", str(env_file)],
        check=True,
        capture_output=True,
        env=loaded,
    )

    sourced: Dict[str, str] = {}
    for entry in completed.stdout.split(b"\0"):
        if not entry:
            continue
        key, sep, value = entry.partition(b"=")
        if not sep:
            continue
        sourced[key.decode("utf-8", errors="replace")] = value.decode("utf-8", errors="replace")

    # Keep launchctl-provided overrides authoritative.
    sourced.update(loaded)
    return sourced


def build_service_environment(environ: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    base_env = {key: str(value) for key, value in dict(environ or os.environ).items()}
    home_dir = Path(base_env.get("HOME") or str(Path.home()))
    default_config_home = Path(base_env.get("XDG_CONFIG_HOME") or str(home_dir / ".config"))
    default_state_home = Path(base_env.get("XDG_STATE_HOME") or str(home_dir / ".local" / "state"))
    env_file = Path(base_env.get("FEISHU_ENV_FILE") or str(default_config_home / "codex-tg" / "feishu.env"))

    loaded = load_env_file_via_bash(env_file, base_env)
    runtime_dir = Path(loaded.get("FEISHU_RUNTIME_DIR") or str(default_state_home / "codex-tg" / "feishu"))
    loaded.setdefault("FEISHU_RUNTIME_DIR", str(runtime_dir))
    loaded.setdefault("STATE_PATH", str(runtime_dir / "feishu_bot_state.json"))
    loaded.setdefault("PYTHONUNBUFFERED", "1")
    return loaded


def resolve_repo_dir(script_path: Path, environ: Mapping[str, str]) -> Path:
    configured = str(environ.get("CODEX_TG_REPO_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return script_path.resolve().parent


def main() -> None:
    script_path = Path(__file__).resolve()
    service_env = build_service_environment()
    repo_dir = resolve_repo_dir(script_path, service_env)
    service_script = repo_dir / "feishu_longconn_service.py"
    if not service_script.is_file():
        raise SystemExit(f"Missing feishu_longconn_service.py: {service_script}")

    runtime_dir = Path(service_env["FEISHU_RUNTIME_DIR"])
    runtime_dir.mkdir(parents=True, exist_ok=True)

    python_bin = str(Path(sys.executable or "python3").resolve())
    os.chdir(repo_dir)
    os.execvpe(python_bin, [python_bin, "-u", str(service_script)], service_env)


if __name__ == "__main__":
    main()
