#!/usr/bin/env bash
# obs-run — run a shell command AND mirror it onto the observer dashboard.
#
# Codex has no hook system, so this wrapper is how commands reach the timeline:
#   ./integrations/codex/obs-run.sh nmap -sV target
# Add it to PATH or alias it (alias obs-run=".../obs-run.sh") to make it seamless.
#
# Fire-and-forget: mirroring never blocks or fails the command, and silently
# no-ops when the dashboard is down.
DASH_URL="${DASH_URL:-http://127.0.0.1:8790}"

if [ "$#" -gt 0 ]; then
  payload="$(python3 -c 'import json,sys; print(json.dumps({"cmd":" ".join(sys.argv[1:])}))' "$@")"
  ( curl -s -m 0.4 -X POST "$DASH_URL/command" \
      -H 'content-type: application/json' -d "$payload" >/dev/null 2>&1 & )
fi

exec "$@"
