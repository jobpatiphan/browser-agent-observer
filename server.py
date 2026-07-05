"""Visual pentest dashboard backend.

Serves the dashboard UI, fans out live events (traffic flows, browser
screencast frames, agent narration) to connected dashboard tabs over
websocket, and ring-buffers recent history so a newly opened tab can
replay what it missed.

In-memory only, no auth, no DB — this is a localhost-only dev tool for a
single user watching a single live session.
"""
import json
import logging
import os
import time
from collections import deque
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

import findings as findings_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("dashboard")

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"

# All tunables come from the environment (see .env.example) so nothing is
# hardcoded to one machine.
DASH_HOST = os.environ.get("DASH_HOST", "127.0.0.1")
DASH_PORT = int(os.environ.get("DASH_PORT", "8790"))

# Only accept dashboard websocket upgrades whose Origin is one of our own
# pages. This blocks a random web page the user visits from silently opening a
# socket to the dashboard (cross-site WS hijacking). Defaults cover loopback on
# the configured port; add more via DASH_ALLOWED_ORIGINS (comma-separated).
ALLOWED_WS_ORIGINS = {
    f"{scheme}://{host}:{DASH_PORT}"
    for scheme in ("http", "https")
    for host in ("127.0.0.1", "localhost")
}
ALLOWED_WS_ORIGINS |= {
    o.strip() for o in os.environ.get("DASH_ALLOWED_ORIGINS", "").split(",") if o.strip()
}

app = FastAPI()
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

START_TIME = time.time()

flows: "deque[dict]" = deque(maxlen=500)
ws_messages: "deque[dict]" = deque(maxlen=500)
frame_history: "deque[dict]" = deque(maxlen=60)
latest_frame: dict | None = None
narration: "deque[dict]" = deque(maxlen=200)
actions: "deque[dict]" = deque(maxlen=200)
commands: "deque[dict]" = deque(maxlen=200)
findings: "deque[dict]" = deque(maxlen=300)
_finding_ids: set[str] = set()   # dedup guard so a finding isn't stored twice
pending_highlights: "deque[dict]" = deque(maxlen=50)
dashboard_clients: set[WebSocket] = set()

latest_tabs: list = []       # most recent CDP page-target list from the forwarder
selected_target: str = ""    # "" = auto-follow; else a specific CDP target id


def _origin_ok(ws: WebSocket) -> bool:
    origin = ws.headers.get("origin")
    # Non-browser clients (mitmproxy addon, screencast forwarder via the
    # `websockets` library) send no Origin header at all — allow those; only
    # reject a *present* Origin that isn't ours.
    return origin is None or origin in ALLOWED_WS_ORIGINS


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/history")
async def history():
    # Filmstrip lazily pulls recent frames here instead of us replaying the
    # whole (multi-MB) ring buffer over the websocket on every reconnect.
    return {"frames": list(frame_history)}


@app.get("/healthz")
async def healthz():
    return {
        "status": "ok",
        "flows": len(flows),
        "clients": len(dashboard_clients),
        "uptime_s": round(time.time() - START_TIME, 1),
    }


async def _emit_findings(flow: dict):
    # Passive security triage on each completed flow. New findings (deduped by
    # id) are buffered and pushed to every dashboard tab like any other event.
    for f in findings_engine.analyze(flow):
        if f["id"] in _finding_ids:
            continue
        _finding_ids.add(f["id"])
        findings.append(f)
        await _broadcast(f)


async def _broadcast(message: dict):
    dead = []
    # Snapshot: two ingest sources (mitmproxy + screencast) broadcast
    # concurrently, and a tab connecting/disconnecting mid-send mutates the set
    # — iterating the live set across the `await` would raise "Set changed size
    # during iteration".
    for ws in list(dashboard_clients):
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        dashboard_clients.discard(ws)


@app.websocket("/ws/dashboard")
async def ws_dashboard(ws: WebSocket):
    if not _origin_ok(ws):
        await ws.close(code=4003)
        return
    await ws.accept()
    # Replay history so a freshly opened tab isn't starting blank.
    for f in flows:
        await ws.send_json(f)
    for m in ws_messages:
        await ws.send_json(m)
    # Only replay the single latest frame (not all of frame_history — that can
    # be several MB); the filmstrip lazily fetches thumbnails via /history.
    if latest_tabs:
        await ws.send_json({"type": "tabs", "tabs": latest_tabs, "selected": selected_target or None})
    if latest_frame is not None:
        await ws.send_json(latest_frame)
    for n in narration:
        await ws.send_json(n)
    for a in actions:
        await ws.send_json(a)
    for c in commands:
        await ws.send_json(c)
    for f in findings:
        await ws.send_json(f)

    dashboard_clients.add(ws)
    try:
        while True:
            # One-way channel from the server's perspective; just drain
            # whatever the client sends (e.g. ping keepalives) and ignore it.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        dashboard_clients.discard(ws)


@app.websocket("/ingest/mitmproxy")
async def ingest_mitmproxy(ws: WebSocket):
    if not _origin_ok(ws):
        await ws.close(code=4003)
        return
    await ws.accept()
    try:
        while True:
            raw = await ws.receive_text()
            event = json.loads(raw)
            if event.get("type") == "ws":
                ws_messages.append(event)
            elif event.get("phase") == "response":
                flows.append(event)
                await _emit_findings(event)
            await _broadcast(event)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("ingest_mitmproxy loop failed")


@app.websocket("/ingest/screencast")
async def ingest_screencast(ws: WebSocket):
    global latest_frame, latest_tabs
    if not _origin_ok(ws):
        await ws.close(code=4003)
        return
    await ws.accept()
    try:
        while True:
            raw = await ws.receive_text()
            event = json.loads(raw)
            if event.get("type") == "tabs":
                latest_tabs = event.get("tabs", [])
                # Annotate with the current selection so the UI can mark the
                # pinned tab (the forwarder only reports which one it's mirroring).
                event["selected"] = selected_target or None
                await _broadcast(event)
                continue
            latest_frame = event
            frame_history.append(event)
            await _broadcast(event)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("ingest_screencast loop failed")


class NarrateBody(BaseModel):
    text: str
    level: str = "info"


@app.post("/narrate")
async def narrate(body: NarrateBody):
    event = {
        "type": "narration",
        "ts": int(time.time() * 1000),
        "level": body.level,
        "text": body.text,
    }
    narration.append(event)
    await _broadcast(event)
    return {"ok": True}


class Coords(BaseModel):
    x: int
    y: int


class ActionBody(BaseModel):
    type: str            # click | type | scroll | navigate | key
    target: str | None = None
    coords: Coords | None = None
    # Optional highlight box size (page px) around coords; defaults to a small
    # marker. Only used for pointer-ish actions.
    w: int = 36
    h: int = 36


@app.post("/action")
async def action(body: ActionBody):
    event = {
        "type": "action",
        "ts": int(time.time() * 1000),
        "action": body.type,
        "target": body.target,
        "coords": body.coords.model_dump() if body.coords else None,
    }
    actions.append(event)
    await _broadcast(event)
    # Queue a native in-page highlight for pointer actions with a location.
    # The screencast forwarder polls /pending-highlights and draws it with the
    # browser's own Overlay domain, so it's baked into the next frame.
    if body.coords and body.type in ("click", "type", "scroll"):
        pending_highlights.append({
            "x": body.coords.x, "y": body.coords.y, "w": body.w, "h": body.h,
        })
    return {"ok": True}


class SelectTabBody(BaseModel):
    targetId: str | None = None


@app.post("/select-tab")
async def select_tab(body: SelectTabBody):
    # UI picks which browser tab to mirror; the forwarder polls /selected-tab.
    global selected_target
    selected_target = body.targetId or ""
    return {"ok": True, "targetId": selected_target or None}


@app.get("/selected-tab")
async def get_selected_tab():
    return {"targetId": selected_target or None}


@app.get("/findings")
async def get_findings():
    # Most-severe first so the UI can render a triage-ordered list.
    return {"findings": sorted(findings, key=findings_engine.sort_key)}


@app.get("/pending-highlights")
async def get_pending_highlights():
    # Drain-on-read: the forwarder pulls whatever accumulated since last poll.
    items = list(pending_highlights)
    pending_highlights.clear()
    return {"highlights": items}


class CommandBody(BaseModel):
    cmd: str


@app.post("/command")
async def command(body: CommandBody):
    event = {"type": "command", "ts": int(time.time() * 1000), "cmd": body.cmd}
    commands.append(event)
    await _broadcast(event)
    return {"ok": True}


SENSITIVE_HEADERS = {
    "authorization", "proxy-authorization", "cookie", "set-cookie",
    "x-api-key", "x-auth-token", "x-csrf-token", "x-xsrf-token",
}
SENSITIVE_QUERY = {
    "token", "access_token", "refresh_token", "id_token", "api_key", "apikey",
    "key", "sig", "signature", "password", "passwd", "pwd", "secret",
    "session", "sessionid", "auth",
}
REDACTED = "‹redacted›"


def _redact_url(url: str) -> str:
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
    try:
        p = urlsplit(url)
        # Mask credentials embedded in the authority (user:pass@host) so a
        # shared replay can't leak basic-auth creds baked into a URL.
        if p.username or p.password:
            host = p.hostname or ""
            if p.port:
                host = f"{host}:{p.port}"
            p = p._replace(netloc=f"{REDACTED}@{host}")
        if p.query:
            q = [(k, REDACTED if k.lower() in SENSITIVE_QUERY else v)
                 for k, v in parse_qsl(p.query, keep_blank_values=True)]
            p = p._replace(query=urlencode(q))
        return urlunsplit(p)
    except Exception:
        return url


def _redact_headers(headers):
    if not isinstance(headers, list):
        return headers
    return [[k, REDACTED if str(k).lower() in SENSITIVE_HEADERS else v] for k, v in headers]


def _redact_flow(f: dict) -> dict:
    f = json.loads(json.dumps(f))  # deep copy so the live buffer is untouched
    if f.get("url"):
        f["url"] = _redact_url(f["url"])
    for side in ("request", "response"):
        if isinstance(f.get(side), dict):
            f[side]["headers"] = _redact_headers(f[side].get("headers"))
    return f


@app.get("/export")
async def export(redact: bool = False):
    # Full snapshot the UI turns into a self-contained replay HTML file.
    # redact=1 masks Authorization/Cookie/Set-Cookie headers and token-ish query
    # params so a shared session doesn't leak credentials.
    if redact:
        flows_out = [_redact_flow(f) for f in flows]
        ws_out = [{**m, "url": _redact_url(m.get("url", ""))} for m in ws_messages]
    else:
        flows_out = list(flows)
        ws_out = list(ws_messages)
    return {
        "meta": {"exported_ts": int(time.time() * 1000),
                 "uptime_s": round(time.time() - START_TIME, 1),
                 "redacted": bool(redact)},
        "flows": flows_out,
        "ws": ws_out,
        "frames": list(frame_history),
        "narration": list(narration),
        "actions": list(actions),
        "commands": list(commands),
        "findings": sorted(findings, key=findings_engine.sort_key),
    }


@app.get("/metrics")
async def metrics():
    # Minimal Prometheus-style text exposition for quick prod debugging.
    lines = [
        "# HELP dashboard_flows Captured HTTP flows in the ring buffer",
        "# TYPE dashboard_flows gauge",
        f"dashboard_flows {len(flows)}",
        "# TYPE dashboard_frames gauge",
        f"dashboard_frames {len(frame_history)}",
        "# TYPE dashboard_clients gauge",
        f"dashboard_clients {len(dashboard_clients)}",
        "# TYPE dashboard_actions gauge",
        f"dashboard_actions {len(actions)}",
        "# TYPE dashboard_findings gauge",
        f"dashboard_findings {len(findings)}",
        "# TYPE dashboard_uptime_seconds counter",
        f"dashboard_uptime_seconds {round(time.time() - START_TIME, 1)}",
    ]
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("\n".join(lines) + "\n")


if __name__ == "__main__":
    log.info("dashboard on http://%s:%d", DASH_HOST, DASH_PORT)
    uvicorn.run(app, host=DASH_HOST, port=DASH_PORT)
