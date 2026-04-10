#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
BOT_SCRIPT="$SCRIPT_DIR/feishu_longconn_service.py"

DEFAULT_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
DEFAULT_STATE_HOME="${XDG_STATE_HOME:-$HOME/.local/state}"
FEISHU_ENV_FILE="${FEISHU_ENV_FILE:-$DEFAULT_CONFIG_HOME/codex-tg/feishu.env}"

CONFIG_KEYS=(
  FEISHU_APP_ID
  FEISHU_APP_SECRET
  ALLOWED_FEISHU_OPEN_IDS
  FEISHU_ENABLE_P2P
  FEISHU_OWNER_MODE
  FEISHU_RUNTIME_DIR
  FEISHU_LOG_LEVEL
  FEISHU_RICH_MESSAGE
  FEISHU_STREAM_ENABLED
  FEISHU_STREAM_EDIT_INTERVAL_MS
  FEISHU_STREAM_MIN_DELTA_CHARS
  FEISHU_THINKING_STATUS_INTERVAL_MS
  FEISHU_IGNORE_OLD_MESSAGE_SECONDS
  DEFAULT_CWD
  PERSONAL_ASSISTANT_CWD
  PERSONAL_ASSISTANT_SIDE_SYNC_ENABLED
  CODEX_BIN
  CODEX_SESSION_ROOT
  CODEX_SANDBOX_MODE
  CODEX_APPROVAL_POLICY
  CODEX_DANGEROUS_BYPASS
  STATE_PATH
)

load_env_file() {
  local key flag_var value_var
  for key in "${CONFIG_KEYS[@]}"; do
    if [[ "${!key+x}" == "x" ]]; then
      export "__PRESET_${key}=${!key}"
      export "__PRESET_HAS_${key}=1"
    fi
  done
  if [[ -f "$FEISHU_ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$FEISHU_ENV_FILE"
    set +a
  fi
  for key in "${CONFIG_KEYS[@]}"; do
    flag_var="__PRESET_HAS_${key}"
    value_var="__PRESET_${key}"
    if [[ "${!flag_var:-}" == "1" ]]; then
      export "$key=${!value_var}"
      unset "$value_var" "$flag_var"
    fi
  done
}

load_env_file

RUNTIME_DIR="${FEISHU_RUNTIME_DIR:-$DEFAULT_STATE_HOME/codex-tg/feishu}"
PID_FILE="${FEISHU_PID_FILE:-$RUNTIME_DIR/feishu_bot.pid}"
LOG_FILE="${FEISHU_LOG_FILE:-$RUNTIME_DIR/feishu_bot.log}"
STATE_PATH="${STATE_PATH:-$RUNTIME_DIR/feishu_bot_state.json}"
OWNER_STATE_PATH="$RUNTIME_DIR/owner_state.json"

# ===================== Env Config =====================
FEISHU_APP_ID="${FEISHU_APP_ID:-}"
FEISHU_APP_SECRET="${FEISHU_APP_SECRET:-}"
ALLOWED_FEISHU_OPEN_IDS="${ALLOWED_FEISHU_OPEN_IDS:-}"
FEISHU_ENABLE_P2P="${FEISHU_ENABLE_P2P:-1}"
FEISHU_OWNER_MODE="${FEISHU_OWNER_MODE:-1}"
FEISHU_LOG_LEVEL="${FEISHU_LOG_LEVEL:-INFO}"
FEISHU_RICH_MESSAGE="${FEISHU_RICH_MESSAGE:-1}"
FEISHU_STREAM_ENABLED="${FEISHU_STREAM_ENABLED:-1}"
FEISHU_STREAM_EDIT_INTERVAL_MS="${FEISHU_STREAM_EDIT_INTERVAL_MS:-400}"
FEISHU_STREAM_MIN_DELTA_CHARS="${FEISHU_STREAM_MIN_DELTA_CHARS:-12}"
FEISHU_THINKING_STATUS_INTERVAL_MS="${FEISHU_THINKING_STATUS_INTERVAL_MS:-900}"
FEISHU_IGNORE_OLD_MESSAGE_SECONDS="${FEISHU_IGNORE_OLD_MESSAGE_SECONDS:-180}"
DEFAULT_CWD="${DEFAULT_CWD:-$SCRIPT_DIR}"
PERSONAL_ASSISTANT_CWD="${PERSONAL_ASSISTANT_CWD:-}"
PERSONAL_ASSISTANT_SIDE_SYNC_ENABLED="${PERSONAL_ASSISTANT_SIDE_SYNC_ENABLED:-1}"
CODEX_BIN="${CODEX_BIN:-/Applications/Codex.app/Contents/Resources/codex}"
CODEX_SESSION_ROOT="${CODEX_SESSION_ROOT:-$HOME/.codex/sessions}"
CODEX_SANDBOX_MODE="${CODEX_SANDBOX_MODE:-}"
CODEX_APPROVAL_POLICY="${CODEX_APPROVAL_POLICY:-}"
CODEX_DANGEROUS_BYPASS="${CODEX_DANGEROUS_BYPASS:-0}"
# ======================================================

mkdir -p "$RUNTIME_DIR"

fail_if_not_configured() {
  if [[ -z "$FEISHU_APP_ID" ]]; then
    echo "[error] 缺少环境变量 FEISHU_APP_ID"
    exit 1
  fi
  if [[ -z "$FEISHU_APP_SECRET" ]]; then
    echo "[error] 缺少环境变量 FEISHU_APP_SECRET"
    exit 1
  fi
  if [[ ! -x "$CODEX_BIN" ]]; then
    echo "[error] CODEX_BIN 不存在或不可执行: $CODEX_BIN"
    exit 1
  fi
}

ensure_dependency() {
  if ! "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1
import lark_oapi
PY
  then
    echo "[info] 安装依赖 lark-oapi..."
    "$PYTHON_BIN" -m pip install --user lark-oapi
  fi
}

is_running() {
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "${pid}" ]] && kill -0 "$pid" >/dev/null 2>&1; then
      return 0
    fi
    rm -f "$PID_FILE"
  fi
  local existing_pid
  existing_pid="$(pgrep -f "$BOT_SCRIPT" 2>/dev/null | head -n 1 || true)"
  if [[ -n "${existing_pid}" ]]; then
    echo "$existing_pid" >"$PID_FILE"
    return 0
  fi
  return 1
}

pair_status() {
  "$PYTHON_BIN" - "$RUNTIME_DIR" "$OWNER_STATE_PATH" "$FEISHU_OWNER_MODE" <<'PY'
import json
import sys
from pathlib import Path

runtime_dir = Path(sys.argv[1])
owner_state_path = Path(sys.argv[2])
owner_mode_raw = str(sys.argv[3]).strip().lower()
owner_mode = owner_mode_raw not in {"0", "false", "no", "off"}

payload = {}
if owner_state_path.exists():
    try:
        parsed = json.loads(owner_state_path.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            payload = parsed
    except Exception:
        payload = {}

owner = payload.get("owner") if isinstance(payload.get("owner"), dict) else None
window = payload.get("pairing_window") if isinstance(payload.get("pairing_window"), dict) else None

print(f"owner mode: {'on' if owner_mode else 'off'}")
print(f"runtime_dir: {runtime_dir}")
print(f"owner_state_path: {owner_state_path}")
print(f"paired: {'yes' if owner else 'no'}")
if owner:
    print(f"owner_open_id: {owner.get('open_id', '')}")
print(f"pairing_window: {'active' if window else 'none'}")
PY
}

pair_code() {
  "$PYTHON_BIN" - "$OWNER_STATE_PATH" <<'PY'
import json
import secrets
import sys
import time
from pathlib import Path

owner_state_path = Path(sys.argv[1])
owner_state_path.parent.mkdir(parents=True, exist_ok=True)
payload = {}
if owner_state_path.exists():
    try:
        parsed = json.loads(owner_state_path.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            payload = parsed
    except Exception:
        payload = {}

pair_code = secrets.token_hex(3).upper()
payload["pairing_window"] = {
    "pair_code": pair_code,
    "issued_at": int(time.time() * 1000),
    "pair_code_expires_at": int(time.time() * 1000) + 600 * 1000,
}
owner_state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"/pair {pair_code}")
PY
}

pair_reset() {
  "$PYTHON_BIN" - "$OWNER_STATE_PATH" <<'PY'
import json
import sys
from pathlib import Path

owner_state_path = Path(sys.argv[1])
owner_state_path.parent.mkdir(parents=True, exist_ok=True)
owner_state_path.write_text(json.dumps({}, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"[ok] owner state reset: {owner_state_path}")
PY
}

start() {
  fail_if_not_configured
  ensure_dependency

  if is_running; then
    echo "[info] 服务已运行，PID=$(cat "$PID_FILE")"
    exit 0
  fi

  echo "[info] 启动飞书长连接服务..."
  nohup env \
    FEISHU_APP_ID="$FEISHU_APP_ID" \
    FEISHU_APP_SECRET="$FEISHU_APP_SECRET" \
    ALLOWED_FEISHU_OPEN_IDS="$ALLOWED_FEISHU_OPEN_IDS" \
    FEISHU_ENABLE_P2P="$FEISHU_ENABLE_P2P" \
    FEISHU_OWNER_MODE="$FEISHU_OWNER_MODE" \
    FEISHU_RUNTIME_DIR="$RUNTIME_DIR" \
    FEISHU_LOG_LEVEL="$FEISHU_LOG_LEVEL" \
    FEISHU_RICH_MESSAGE="$FEISHU_RICH_MESSAGE" \
    FEISHU_STREAM_ENABLED="$FEISHU_STREAM_ENABLED" \
    FEISHU_STREAM_EDIT_INTERVAL_MS="$FEISHU_STREAM_EDIT_INTERVAL_MS" \
    FEISHU_STREAM_MIN_DELTA_CHARS="$FEISHU_STREAM_MIN_DELTA_CHARS" \
    FEISHU_THINKING_STATUS_INTERVAL_MS="$FEISHU_THINKING_STATUS_INTERVAL_MS" \
    FEISHU_IGNORE_OLD_MESSAGE_SECONDS="$FEISHU_IGNORE_OLD_MESSAGE_SECONDS" \
    DEFAULT_CWD="$DEFAULT_CWD" \
    PERSONAL_ASSISTANT_CWD="$PERSONAL_ASSISTANT_CWD" \
    PERSONAL_ASSISTANT_SIDE_SYNC_ENABLED="$PERSONAL_ASSISTANT_SIDE_SYNC_ENABLED" \
    CODEX_BIN="$CODEX_BIN" \
    CODEX_SESSION_ROOT="$CODEX_SESSION_ROOT" \
    CODEX_SANDBOX_MODE="$CODEX_SANDBOX_MODE" \
    CODEX_APPROVAL_POLICY="$CODEX_APPROVAL_POLICY" \
    CODEX_DANGEROUS_BYPASS="$CODEX_DANGEROUS_BYPASS" \
    STATE_PATH="$STATE_PATH" \
    "$PYTHON_BIN" -u "$BOT_SCRIPT" >>"$LOG_FILE" 2>&1 &

  local pid=$!
  echo "$pid" >"$PID_FILE"
  sleep 2

  if kill -0 "$pid" >/dev/null 2>&1; then
    echo "[ok] 已启动，PID=$pid"
    echo "[ok] 日志: $LOG_FILE"
  else
    rm -f "$PID_FILE"
    echo "[error] 启动失败，最近日志："
    tail -n 80 "$LOG_FILE" || true
    exit 1
  fi
}

stop() {
  if is_running; then
    local pid
    pid="$(cat "$PID_FILE")"
    kill "$pid" >/dev/null 2>&1 || true
    rm -f "$PID_FILE"
    echo "[ok] 已停止，PID=$pid"
  else
    echo "[info] 服务未运行"
  fi
}

status() {
  if is_running; then
    echo "[ok] 运行中，PID=$(cat "$PID_FILE")"
  else
    echo "[info] 未运行"
  fi
}

logs() {
  touch "$LOG_FILE"
  tail -f "$LOG_FILE"
}

restart() {
  stop
  start
}

usage() {
  cat <<EOF
用法: ./run_feishu.sh [start|stop|restart|status|logs|pair-status|pair-code|pair-reset]
默认: start

推荐把配置写到:
  $FEISHU_ENV_FILE

至少需要:
FEISHU_APP_ID="cli_xxx"
FEISHU_APP_SECRET="xxx"
FEISHU_OWNER_MODE=1

可选：
ALLOWED_FEISHU_OPEN_IDS="ou_xxx,ou_yyy"
FEISHU_ENABLE_P2P=1
FEISHU_RUNTIME_DIR="$RUNTIME_DIR"
FEISHU_LOG_LEVEL=INFO
FEISHU_RICH_MESSAGE=1
FEISHU_STREAM_ENABLED=1
FEISHU_STREAM_EDIT_INTERVAL_MS=400
FEISHU_STREAM_MIN_DELTA_CHARS=12
FEISHU_THINKING_STATUS_INTERVAL_MS=900
FEISHU_IGNORE_OLD_MESSAGE_SECONDS=180

# Codex command execution policy
# 0: no extra permission args (default)
# 1: defaults to sandbox_mode=danger-full-access + approval_policy=never
# 2: append --dangerously-bypass-approvals-and-sandbox
CODEX_SANDBOX_MODE=""    # optional override for level=1
CODEX_APPROVAL_POLICY="" # optional override for level=1
CODEX_DANGEROUS_BYPASS=0
EOF
}

cmd="${1:-start}"
case "$cmd" in
start) start ;;
stop) stop ;;
restart) restart ;;
status) status ;;
logs) logs ;;
pair-status) pair_status ;;
pair-code) pair_code ;;
pair-reset) pair_reset ;;
help|-h|--help) usage ;;
*)
  usage
  exit 1
  ;;
esac
