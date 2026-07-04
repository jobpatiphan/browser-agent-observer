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
frame_history: "deque[dict]" = deque(maxlen=60)
latest_frame: dict | None = None
narration: "deque[dict]" = deque(maxlen=200)
actions: "deque[dict]" = deque(maxlen=200)
commands: "deque[dict]" = deque(maxlen=200)
pending_highlights: "deque[dict]" = deque(maxlen=50)
dashboard_clients: set[WebSocket] = set()


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


async def _broadcast(message: dict):
    dead = []
    for ws in dashboard_clients:
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
    # Only replay the single latest frame (not all of frame_history — that can
    # be several MB); the filmstrip lazily fetches thumbnails via /history.
    if latest_frame is not None:
        await ws.send_json(latest_frame)
    for n in narration:
        await ws.send_json(n)
    for a in actions:
        await ws.send_json(a)
    for c in commands:
        await ws.send_json(c)

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
            if event.get("phase") == "response":
                flows.append(event)
            await _broadcast(event)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("ingest_mitmproxy loop failed")


@app.websocket("/ingest/screencast")
async def ingest_screencast(ws: WebSocket):
    global latest_frame
    if not _origin_ok(ws):
        await ws.close(code=4003)
        return
    await ws.accept()
    try:
        while True:
            raw = await ws.receive_text()
            event = json.loads(raw)
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


@app.get("/export")
async def export():
    # Full snapshot the UI turns into a self-contained replay HTML file.
    return {
        "meta": {"exported_ts": int(time.time() * 1000),
                 "uptime_s": round(time.time() - START_TIME, 1)},
        "flows": list(flows),
        "frames": list(frame_history),
        "narration": list(narration),
        "actions": list(actions),
        "commands": list(commands),
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
        "# TYPE dashboard_uptime_seconds counter",
        f"dashboard_uptime_seconds {round(time.time() - START_TIME, 1)}",
    ]
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("\n".join(lines) + "\n")


if __name__ == "__main__":
    log.info("dashboard on http://%s:%d", DASH_HOST, DASH_PORT)
    uvicorn.run(app, host=DASH_HOST, port=DASH_PORT)
