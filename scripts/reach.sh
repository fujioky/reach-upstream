#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Ensure uv is on PATH
export PATH="$HOME/.local/bin:$PATH"

set -a
if [ -f "$ROOT/.env" ]; then
  # shellcheck disable=SC1091
  source "$ROOT/.env"
fi
set +a

abs_path() {
  local value="$1"
  if [[ "$value" = /* ]]; then
    printf '%s\n' "$value"
  else
    printf '%s\n' "$ROOT/$value"
  fi
}

RUNTIME_DIR="$(abs_path "${AGENT_REACH_RUNTIME_DIR:-runtime}")"
VENV="$(abs_path "${AGENT_REACH_VENV:-runtime/venv}")"
LOCAL_BIN="$(abs_path "${AGENT_REACH_BIN_DIR:-runtime/bin}")"
LOCAL_HOME="$(abs_path "${AGENT_REACH_HOME:-runtime/home}")"
NODE_BIN="$(abs_path "${AGENT_REACH_NODE_BIN:-runtime/node/bin}")"
LOG_DIR="$(abs_path "${AGENT_REACH_LOG_DIR:-runtime/logs}")"
PID_DIR="$(abs_path "${AGENT_REACH_PID_DIR:-runtime/pids}")"

PROXY_PID_FILE="$PID_DIR/reach-proxy.pid"
PROXY_LOG_FILE="$LOG_DIR/reach-proxy.log"
PORT="${AGENT_REACH_PROXY_PORT:-18280}"
HOST="${AGENT_REACH_PROXY_HOST:-127.0.0.1}"

export AGENT_REACH_RUNTIME_DIR="$RUNTIME_DIR"
export AGENT_REACH_VENV="$VENV"
export AGENT_REACH_BIN_DIR="$LOCAL_BIN"
export AGENT_REACH_HOME="$LOCAL_HOME"
export AGENT_REACH_NODE_BIN="$NODE_BIN"
export HOME="$LOCAL_HOME"
export XDG_CONFIG_HOME="$LOCAL_HOME/.config"
export XDG_CACHE_HOME="$LOCAL_HOME/.cache"
export XDG_DATA_HOME="$LOCAL_HOME/.local/share"
export PATH="$LOCAL_BIN:$VENV/bin:$NODE_BIN:$PATH"

usage() {
  cat <<'EOF'
Usage: ./scripts/reach.sh <command>

Commands:
  setup              Create runtime/venv and install dependencies
  run                Run the proxy in the foreground
  start              Start the proxy in the background
  stop               Stop the background proxy
  restart            Restart the background proxy
  status             Show proxy status
  health             Call the local health endpoint
  logs [lines]       Show recent proxy logs
  sync-commands      Refresh runtime/bin command wrappers
  tunnel             Show Cloudflare Tunnel status
  cmd <command>      Run a command inside the isolated runtime (e.g. cmd twitter whoami)
EOF
}

ensure_dirs() {
  mkdir -p "$RUNTIME_DIR" "$LOCAL_BIN" "$LOCAL_HOME" "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME" "$NODE_BIN" "$LOG_DIR" "$PID_DIR"
}

ensure_venv() {
  if [ ! -x "$VENV/bin/python" ]; then
    echo "Missing venv: $VENV"
    echo "Run: ./scripts/reach.sh setup"
    exit 1
  fi
}

is_running() {
  [ -f "$PROXY_PID_FILE" ] && kill -0 "$(cat "$PROXY_PID_FILE")" 2>/dev/null
}

sync_local_commands() {
  ensure_dirs
  for command in twitter xhs yt-dlp; do
    if [ -x "$VENV/bin/$command" ]; then
      rm -f "$LOCAL_BIN/$command"
      {
        printf '%s\n' '#!/usr/bin/env bash'
        printf '%s\n' 'set -euo pipefail'
        printf 'export AGENT_REACH_RUNTIME_DIR=%q\n' "$RUNTIME_DIR"
        printf 'export AGENT_REACH_VENV=%q\n' "$VENV"
        printf 'export AGENT_REACH_BIN_DIR=%q\n' "$LOCAL_BIN"
        printf 'export AGENT_REACH_HOME=%q\n' "$LOCAL_HOME"
        printf 'export HOME=%q\n' "$LOCAL_HOME"
        printf 'export XDG_CONFIG_HOME=%q\n' "$XDG_CONFIG_HOME"
        printf 'export XDG_CACHE_HOME=%q\n' "$XDG_CACHE_HOME"
        printf 'export XDG_DATA_HOME=%q\n' "$XDG_DATA_HOME"
        printf 'export PATH=%q:%q:%q:${PATH:-}\n' "$LOCAL_BIN" "$VENV/bin" "$NODE_BIN"
        printf 'exec %q "$@"\n' "$VENV/bin/$command"
      } > "$LOCAL_BIN/$command"
      chmod 700 "$LOCAL_BIN/$command"
    fi
  done
}

setup() {
  ensure_dirs
  if [ ! -x "$VENV/bin/python" ]; then
    uv venv "$VENV"
  fi
  uv pip install --python "$VENV/bin/python" -r "$ROOT/requirements.txt"
  sync_local_commands
  echo "Runtime is ready under: $RUNTIME_DIR"
  echo "Local command bin: $LOCAL_BIN"
  echo "Local home: $LOCAL_HOME"
}

run_proxy() {
  ensure_dirs
  ensure_venv
  sync_local_commands
  cd "$ROOT"
  exec "$VENV/bin/python" "$ROOT/agent_reach_proxy.py"
}

start_proxy() {
  ensure_dirs
  ensure_venv
  if is_running; then
    echo "Reach proxy is already running with pid $(cat "$PROXY_PID_FILE")"
    exit 0
  fi
  setsid "$ROOT/scripts/reach.sh" run >"$PROXY_LOG_FILE" 2>&1 < /dev/null &
  echo "$!" >"$PROXY_PID_FILE"
  echo "Reach proxy started with pid $(cat "$PROXY_PID_FILE")"
  echo "Log: $PROXY_LOG_FILE"
}

stop_proxy() {
  if ! [ -f "$PROXY_PID_FILE" ]; then
    echo "No proxy pid file found"
    exit 0
  fi
  local pid
  pid="$(cat "$PROXY_PID_FILE")"
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid"
    echo "Stopped Reach proxy pid $pid"
  else
    echo "Proxy pid $pid is not running"
  fi
  rm -f "$PROXY_PID_FILE"
}

status_proxy() {
  if is_running; then
    echo "Reach proxy is running with pid $(cat "$PROXY_PID_FILE")"
  else
    echo "Reach proxy is not running"
  fi
  echo "Runtime: $RUNTIME_DIR"
  echo "Local command bin: $LOCAL_BIN"
  echo "Local home: $LOCAL_HOME"
  echo "Log: $PROXY_LOG_FILE"
}

health() {
  curl -fsS "http://$HOST:$PORT/healthz"
}

logs() {
  local lines="${1:-80}"
  if [ -f "$PROXY_LOG_FILE" ]; then
    tail -n "$lines" "$PROXY_LOG_FILE"
  else
    echo "No log file yet: $PROXY_LOG_FILE"
  fi
}

tunnel_status() {
  cloudflared tunnel list 2>&1
}

command="${1:-}"
if [ $# -gt 0 ]; then
  shift
fi

case "$command" in
  setup) setup "$@" ;;
  run) run_proxy "$@" ;;
  start) start_proxy "$@" ;;
  stop) stop_proxy "$@" ;;
  restart) stop_proxy "$@"; start_proxy "$@" ;;
  status) status_proxy "$@" ;;
  health) health "$@" ;;
  logs) logs "$@" ;;
  sync-commands) ensure_venv; sync_local_commands "$@" ;;
  tunnel) tunnel_status "$@" ;;
  cmd) exec "$@" ;;
  ""|-h|--help|help) usage ;;
  *)
    echo "Unknown command: $command" >&2
    usage >&2
    exit 2
    ;;
esac
