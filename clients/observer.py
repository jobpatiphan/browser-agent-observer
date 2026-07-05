"""Tiny client for browser-agent-observer — pure stdlib, zero dependencies.

Drop this next to your agent code (Claude computer-use loop, a Codex script, a
Playwright/Puppeteer driver, anything) and narrate what it's doing so the
dashboard timeline reflects it:

    from observer import obs
    obs.narrate("Logging in with test creds")
    obs.click("button#login", x=203, y=411)      # cursor + in-page highlight
    obs.command("curl -s https://target/api/me")

Calls are best-effort and never raise, so observability can't break your agent.
Point it elsewhere with DASH_URL=http://host:port or Observer(base=...).
"""
import json
import os
import urllib.request

DEFAULT_URL = os.environ.get("DASH_URL", "http://127.0.0.1:8790")


class Observer:
    def __init__(self, base: str | None = None):
        self.base = (base or DEFAULT_URL).rstrip("/")

    def _post(self, path: str, payload: dict) -> None:
        try:
            headers = {"content-type": "application/json"}
            token = os.environ.get("DASH_TOKEN", "")
            if token:
                headers["authorization"] = f"Bearer {token}"
            req = urllib.request.Request(
                self.base + path,
                data=json.dumps(payload).encode(),
                headers=headers,
                method="POST",
            )
            urllib.request.urlopen(req, timeout=2).read()
        except Exception:
            pass  # observability must never break the agent

    def narrate(self, text: str, level: str = "info") -> None:
        """level: info | warn | error"""
        self._post("/narrate", {"text": text, "level": level})

    def action(self, kind: str, target: str | None = None,
               x: int | None = None, y: int | None = None) -> None:
        """kind: click | type | scroll | navigate | key. x/y are page pixels."""
        coords = {"x": x, "y": y} if x is not None and y is not None else None
        self._post("/action", {"type": kind, "target": target, "coords": coords})

    def click(self, target: str | None = None, x: int | None = None, y: int | None = None) -> None:
        self.action("click", target, x, y)

    def command(self, cmd: str) -> None:
        self._post("/command", {"cmd": cmd})


# Module-level default so you can just `from observer import obs`.
obs = Observer()
