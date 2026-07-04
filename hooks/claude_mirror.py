#!/usr/bin/env python3
"""Claude Code hook: mirror what the agent does onto the observer dashboard.

Wired into ~/.claude/settings.json for PostToolUse + UserPromptSubmit (see
integrations/claude-code/). The harness runs it — not the model — so mirroring
happens every time, reliably, without Claude having to remember.

Design goal: safe to install globally. If the dashboard isn't running it does a
~0.15s /healthz probe, finds nothing, and exits silently — zero side effects on
sessions where you're not watching. Any error also exits 0: a hook must never
break the user's tools.

Reads the hook payload as JSON on stdin; posts to the dashboard's HTTP API.
"""
import json
import os
import sys
import urllib.request

DASH_URL = os.environ.get("DASH_URL", "http://127.0.0.1:8790").rstrip("/")


def _post(path, payload, timeout=0.4):
    try:
        req = urllib.request.Request(
            DASH_URL + path,
            data=json.dumps(payload).encode(),
            headers={"content-type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=timeout).read()
    except Exception:
        pass


def _dashboard_up():
    try:
        urllib.request.urlopen(DASH_URL + "/healthz", timeout=0.15).read()
        return True
    except Exception:
        return False


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    # Fast bail-out when nobody's watching — this is what makes global install safe.
    if not _dashboard_up():
        return

    event = data.get("hook_event_name")

    if event == "UserPromptSubmit":
        prompt = (data.get("prompt") or "").strip().replace("\n", " ")
        if prompt:
            _post("/narrate", {"text": "▶ " + prompt[:200], "level": "info"})

    elif event == "PostToolUse":
        tool = data.get("tool_name")
        ti = data.get("tool_input") or {}
        if tool == "Bash" and ti.get("command"):
            _post("/command", {"cmd": ti["command"]})
            desc = (ti.get("description") or "").strip()
            if desc:
                _post("/narrate", {"text": desc, "level": "info"})
        elif tool in ("Edit", "Write", "NotebookEdit"):
            fp = ti.get("file_path") or ti.get("notebook_path") or ""
            if fp:
                _post("/narrate", {"text": f"✎ edited {os.path.basename(fp)}", "level": "info"})

    elif event == "Stop":
        _post("/narrate", {"text": "✓ turn complete", "level": "info"})


if __name__ == "__main__":
    main()
