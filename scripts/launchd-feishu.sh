#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_DIR="${CODEX_TG_REPO_DIR:-$DEFAULT_REPO_DIR}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

DEFAULT_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
DEFAULT_STATE_HOME="${XDG_STATE_HOME:-$HOME/.local/state}"
FEISHU_ENV_FILE="${FEISHU_ENV_FILE:-$DEFAULT_CONFIG_HOME/codex-tg/feishu.env}"

if [[ -f "$FEISHU_ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$FEISHU_ENV_FILE"
  set +a
fi

export FEISHU_RUNTIME_DIR="${FEISHU_RUNTIME_DIR:-$DEFAULT_STATE_HOME/codex-tg/feishu}"
export STATE_PATH="${STATE_PATH:-$FEISHU_RUNTIME_DIR/feishu_bot_state.json}"
mkdir -p "$FEISHU_RUNTIME_DIR"

cd "$REPO_DIR"
exec "$PYTHON_BIN" -u "$REPO_DIR/feishu_longconn_service.py"
