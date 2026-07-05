#!/usr/bin/env bash
# One-command launcher for browser-agent-observer.
#
#   ./run.sh up         start backend + proxy + screencast forwarder (opens the UI)
#   ./run.sh down       stop them
#   ./run.sh status     show what's running
#   ./run.sh logs [name] tail a service log (backend|proxy|screencast|browser)
#   ./run.sh browser    launch a Chromium pointed at the proxy + CDP for you
#   ./run.sh open       open the dashboard UI in your default browser
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
# Dedicated profile so our browser never attaches to (and gets swallowed by) an
# already-running Chrome on the default profile — the #1 reason
# --remote-debugging-port silently does nothing.
BROWSER_USER_DATA_DIR="${BROWSER_USER_DATA_DIR:-/tmp/bao-browser}"
# Auto-open the dashboard UI on `up` (set OPEN_DASH=0 for headless/CI).
OPEN_DASH="${OPEN_DASH:-1}"
DASH_URL_FULL="http://$DASH_HOST:$DASH_PORT"

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

# --- small HTTP helpers (curl if present, else python stdlib) --------------
http_ok() {   # http_ok URL  -> 0 if it answers 2xx within ~2s
  local url="$1"
  if command -v curl >/dev/null 2>&1; then
    curl -fsS --max-time 2 "$url" >/dev/null 2>&1
  else
    "$PYTHON" - "$url" <<'PY' >/dev/null 2>&1
import sys, urllib.request
urllib.request.urlopen(sys.argv[1], timeout=2).read()
PY
  fi
}

have_cdp() { http_ok "$CDP_URL/json/version"; }

wait_for() {  # wait_for URL TRIES(0.5s each)
  local url="$1" tries="${2:-30}" i
  for ((i = 0; i < tries; i++)); do http_ok "$url" && return 0; sleep 0.5; done
  return 1
}

port_open() {  # port_open HOST PORT  -> 0 if something is accepting there
  "$PYTHON" - "$1" "$2" <<'PY' 2>/dev/null
import socket, sys
s = socket.socket(); s.settimeout(0.5)
try:
    s.connect((sys.argv[1], int(sys.argv[2]))); s.close()
except Exception:
    sys.exit(1)
PY
}

wait_for_port() {  # wait_for_port HOST PORT TRIES(0.25s each)
  local host="$1" port="$2" tries="${3:-40}" i
  for ((i = 0; i < tries; i++)); do port_open "$host" "$port" && return 0; sleep 0.25; done
  return 1
}

open_url() {
  local url="$1"
  # Graphical session only; headless boxes / CI stay silent.
  if [[ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" && "$(uname)" != "Darwin" ]]; then return 0; fi
  local o opener=""
  for o in xdg-open open; do command -v "$o" >/dev/null 2>&1 && { opener="$o"; break; }; done
  [[ -n "$opener" ]] || { echo "  (open $url manually — no xdg-open/open found)"; return 0; }
  nohup "$opener" "$url" >/dev/null 2>&1 &
  disown 2>/dev/null || true
}

find_browser() {
  local b
  for b in chromium chromium-browser google-chrome google-chrome-stable chrome brave-browser microsoft-edge; do
    command -v "$b" >/dev/null 2>&1 && { echo "$b"; return; }
  done
}

# Proxy address the *browser* should dial. If the proxy binds 0.0.0.0 the
# browser still needs a routable host, so fall back to loopback.
browser_proxy_host() { [[ "$PROXY_HOST" == "0.0.0.0" ]] && echo "127.0.0.1" || echo "$PROXY_HOST"; }

# Populate global BROWSER_ARGV (array, so paths with spaces survive) + BROWSER_BIN.
build_browser_argv() {
  local bin; bin="$(find_browser)"
  [[ -n "$bin" ]] || return 1
  BROWSER_BIN="$bin"
  BROWSER_ARGV=(
    "$bin"
    "--remote-debugging-port=$CDP_PORT"
    "--proxy-server=$(browser_proxy_host):$PROXY_PORT"
    "--user-data-dir=$BROWSER_USER_DATA_DIR"
    "--no-first-run"
    "--no-default-browser-check"
    "--ignore-certificate-errors"
    "about:blank"
  )
}

print_browser_cmd() {
  if build_browser_argv; then printf '%q ' "${BROWSER_ARGV[@]}"; echo; else
    echo "(no Chromium/Chrome found — install one, e.g. 'sudo apt install chromium')"; fi
}

launch_browser() {
  if have_cdp; then
    echo "  CDP already live at $CDP_URL — reusing it (not spawning a second browser)"
    return 0
  fi
  if ! build_browser_argv; then
    echo "  no Chromium/Chrome on PATH. install one (e.g. 'sudo apt install chromium')" >&2
    return 1
  fi
  # We only get here with no live CDP, so any lock on our dedicated profile is
  # stale (a previous browser that exited). Clearing it lets a fresh one start.
  rm -f "$BROWSER_USER_DATA_DIR/SingletonLock" 2>/dev/null || true
  echo "  launching $BROWSER_BIN -> CDP :$CDP_PORT, proxy $(browser_proxy_host):$PROXY_PORT"
  nohup "${BROWSER_ARGV[@]}" >"$RUN_DIR/browser.log" 2>&1 &
  local pid=$!
  disown "$pid" 2>/dev/null || true
  echo "$pid" > "$RUN_DIR/browser.pid"
  if wait_for "$CDP_URL/json/version" 30; then
    echo "  browser ready (pid $pid) — CDP up at $CDP_URL"
    echo "  trust the mitmproxy CA or keep --ignore-certificate-errors for HTTPS"
  else
    echo "  ⚠ browser started (pid $pid) but CDP never came up on :$CDP_PORT" >&2
    echo "    likely: a Chrome/Chromium is already running (close it first)," >&2
    echo "    port $CDP_PORT is taken, or the profile is locked." >&2
    echo "    see: ./run.sh logs browser" >&2
    return 1
  fi
}

case "${1:-help}" in
  up)
    echo "starting services…"
    start_one backend "$PYTHON" "$DIR/server.py"
    if wait_for "$DASH_URL_FULL/healthz" 40; then
      start_one proxy mitmdump -s "$DIR/addon.py" --listen-host "$PROXY_HOST" --listen-port "$PROXY_PORT" -q
      start_one screencast "$PYTHON" "$DIR/screencast_forwarder.py"
      # Don't report ready until the proxy is actually accepting connections —
      # otherwise traffic fired right after `up` fails with a connection refused
      # and the tool looks flaky.
      if wait_for_port "$(browser_proxy_host)" "$PROXY_PORT" 40; then
        echo
        echo "  ✓ ready — dashboard: $DASH_URL_FULL   proxy: $(browser_proxy_host):$PROXY_PORT"
      else
        echo "  ⚠ proxy slow to bind on :$PROXY_PORT — check ./run.sh logs proxy" >&2
      fi
      [[ "$OPEN_DASH" == "1" ]] && open_url "$DASH_URL_FULL"
      echo
      echo "  Now point your agent's browser at us — either:"
      echo "    ./run.sh browser        (we launch a wired-up Chromium)"
      echo "    $(print_browser_cmd)"
    else
      echo "  ⚠ backend never answered $DASH_URL_FULL/healthz — check ./run.sh logs backend" >&2
      exit 1
    fi
    ;;
  down)
    echo "stopping services…"
    stop_one screencast; stop_one proxy; stop_one backend
    ;;
  status)
    for s in backend proxy screencast; do
      if is_up "$s"; then echo "  $s: running (pid $(cat "$RUN_DIR/$s.pid"))"; else echo "  $s: stopped"; fi
    done
    if have_cdp; then echo "  browser CDP: up at $CDP_URL"; else echo "  browser CDP: down"; fi
    ;;
  logs)
    logf="$RUN_DIR/${2:-backend}.log"
    if [[ -f "$logf" ]]; then tail -f "$logf"; else
      echo "no log yet: $logf — has '${2:-backend}' been started?" >&2; exit 1; fi
    ;;
  browser)
    launch_browser
    ;;
  open)
    echo "opening $DASH_URL_FULL"
    open_url "$DASH_URL_FULL"
    ;;
  export)
    DASH_URL="$DASH_URL_FULL" "$PYTHON" "$DIR/tools/export_session.py" "${@:2}"
    ;;
  *)
    grep -E '^#( |$)' "$0" | sed -E 's/^# ?//' | head -12
    ;;
esac
