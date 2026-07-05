#!/usr/bin/env bash
# One-command launcher for browser-agent-observer.
#
#   ./run.sh up        start backend + proxy + screencast forwarder
#   ./run.sh down       stop them
#   ./run.sh status     show what's running
#   ./run.sh logs [name] tail a service log (backend|proxy|screencast)
#   ./run.sh browser    launch a Chromium pointed at the proxy + CDP for you
#   ./run.sh export [args]  save the current session to a replay .html
#
# Config comes from .env (copy .env.example). Every value has a default, so
# `./run.sh up` works out of the box on a fresh clone.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$DIR/run"
mkdir -p "$RUN_DIR"

# Load .env if present, exporting everything to child processes.
if [[ -f "$DIR/.env" ]]; then set -a; . "$DIR/.env"; set +a; fi

DASH_HOST="${DASH_HOST:-127.0.0.1}"
DASH_PORT="${DASH_PORT:-8790}"
PROXY_HOST="${PROXY_HOST:-127.0.0.1}"
PROXY_PORT="${PROXY_PORT:-8083}"
CDP_URL="${CDP_URL:-http://localhost:9222}"
CDP_PORT="$(printf '%s' "$CDP_URL" | sed -E 's#.*:([0-9]+).*#\1#')"
PYTHON="${PYTHON:-python3}"

is_up() { [[ -f "$RUN_DIR/$1.pid" ]] && kill -0 "$(cat "$RUN_DIR/$1.pid")" 2>/dev/null; }

start_one() {
  local name="$1"; shift
  if is_up "$name"; then echo "  $name already running (pid $(cat "$RUN_DIR/$name.pid"))"; return; fi
  # Plain nohup (no setsid): setsid forks, so $! would record the short-lived
  # wrapper instead of the real child — leaving orphans on stop. nohup + '&'
  # already detaches the child so it survives this script exiting.
  nohup "$@" >"$RUN_DIR/$name.log" 2>&1 &
  local pid=$!
  disown "$pid" 2>/dev/null || true
  echo "$pid" > "$RUN_DIR/$name.pid"
  echo "  started $name (pid $pid)"
}

stop_one() {
  local name="$1"
  if is_up "$name"; then
    local pid; pid="$(cat "$RUN_DIR/$name.pid")"
    kill "$pid" 2>/dev/null || true
    # Give it a moment to shut down cleanly, then force-kill if it lingers
    # (mitmdump in particular doesn't always exit on the first SIGTERM).
    for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$pid" 2>/dev/null || break; sleep 0.3; done
    kill -9 "$pid" 2>/dev/null || true
    echo "  stopped $name"
  fi
  rm -f "$RUN_DIR/$name.pid"
}

find_browser() {
  for b in chromium chromium-browser google-chrome google-chrome-stable chrome brave-browser microsoft-edge; do
    command -v "$b" >/dev/null 2>&1 && { echo "$b"; return; }
  done
}

browser_cmd() {
  local bin; bin="$(find_browser)"; bin="${bin:-chromium}"
  echo "$bin --remote-debugging-port=$CDP_PORT --proxy-server=$PROXY_HOST:$PROXY_PORT \\
    --user-data-dir=/tmp/bao-browser --ignore-certificate-errors about:blank"
}

case "${1:-help}" in
  up)
    echo "starting services…"
    start_one backend "$PYTHON" "$DIR/server.py"
    sleep 1
    start_one proxy mitmdump -s "$DIR/addon.py" --listen-host "$PROXY_HOST" --listen-port "$PROXY_PORT" -q
    start_one screencast "$PYTHON" "$DIR/screencast_forwarder.py"
    echo
    echo "  Dashboard: http://$DASH_HOST:$DASH_PORT"
    echo
    echo "  Now point your agent's browser at us — launch it with:"
    echo "    $(browser_cmd)"
    echo "  …or just run:  ./run.sh browser"
    ;;
  down)
    echo "stopping services…"
    stop_one screencast; stop_one proxy; stop_one backend
    ;;
  status)
    for s in backend proxy screencast; do
      if is_up "$s"; then echo "  $s: running (pid $(cat "$RUN_DIR/$s.pid"))"; else echo "  $s: stopped"; fi
    done
    ;;
  logs)
    tail -f "$RUN_DIR/${2:-backend}.log"
    ;;
  browser)
    echo "launching browser -> CDP :$CDP_PORT, proxy $PROXY_HOST:$PROXY_PORT"
    eval "$(browser_cmd)" &
    echo "  (pid $!) trust the mitmproxy CA or keep --ignore-certificate-errors for HTTPS"
    ;;
  export)
    DASH_URL="http://$DASH_HOST:$DASH_PORT" "$PYTHON" "$DIR/tools/export_session.py" "${@:2}"
    ;;
  *)
    grep -E '^#( |$)' "$0" | sed -E 's/^# ?//' | head -12
    ;;
esac
