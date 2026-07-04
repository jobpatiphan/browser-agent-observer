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

# Localhost dev tool: only accept dashboard websocket upgrades whose Origin is
# one of our own loopback pages. This blocks a random web page the user visits
# from silently opening a socket to the dashboard (cross-site WS hijacking).
ALLOWED_WS_ORIGINS = {
    "http://127.0.0.1:8790", "http://localhost:8790",
    "https://127.0.0.1:8790", "https://localhost:8790",
}

app = FastAPI()
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

START_TIME = time.time()

flows: "deque[dict]" = deque(maxlen=500)
frame_history: "deque[dict]" = deque(maxlen=60)
latest_frame: dict | None = None
narration: "deque[dict]" = deque(maxlen=200)
actions: "deque[dict]" = deque(maxlen=200)
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


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8790)
