#!/usr/bin/env bash
# Fallback manual start/stop/status if systemd --user units aren't usable
# (e.g. Linger=no and the login session tears them down). Prefer the
# systemd units when they work; this exists so the dashboard is never
# blocked on that.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$DIR/run"
mkdir -p "$RUN_DIR"

start_one() {
  local name="$1"; shift
  local pidfile="$RUN_DIR/$name.pid"
  if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    echo "$name already running (pid $(cat "$pidfile"))"
    return
  fi
  setsid nohup "$@" >"$RUN_DIR/$name.log" 2>&1 &
  echo $! > "$pidfile"
  echo "started $name (pid $!)"
}

stop_one() {
  local name="$1"
  local pidfile="$RUN_DIR/$name.pid"
  if [[ -f "$pidfile" ]]; then
    local pid
    pid="$(cat "$pidfile")"
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" && echo "stopped $name (pid $pid)"
    fi
    rm -f "$pidfile"
  else
    echo "$name not running"
  fi
}

status_one() {
  local name="$1"
  local pidfile="$RUN_DIR/$name.pid"
  if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    echo "$name: running (pid $(cat "$pidfile"))"
  else
    echo "$name: stopped"
  fi
}

case "${1:-}" in
  start)
    start_one backend /usr/bin/python3 "$DIR/server.py"
    sleep 1
    start_one mitmproxy /usr/bin/mitmdump -s "$DIR/addon.py" --listen-host 127.0.0.1 --listen-port 8083
    start_one screencast /usr/bin/python3 "$DIR/screencast_forwarder.py"
    ;;
  stop)
    stop_one screencast
    stop_one mitmproxy
    stop_one backend
    ;;
  status)
    status_one backend
    status_one mitmproxy
    status_one screencast
    ;;
  restart)
    "$0" stop
    sleep 1
    "$0" start
    ;;
  *)
    echo "usage: $0 {start|stop|status|restart}"
    exit 1
    ;;
esac
