"""Streams a live view of the Chromium tab Claude is driving to the dashboard.

Opens a second, independent CDP client connection to the same page target
(alongside whatever client Claude's own harness uses) over a single ordered
CDP websocket, and forwards frames to the dashboard backend.

Hybrid capture (Phase 1), to look sharp without wasting bandwidth:
  * a continuous LOW-quality screencast (small, jpeg q~38, every 2nd frame)
    that exists only to convey motion while something is happening, and
  * a one-shot HIGH-quality `Page.captureScreenshot` fired ~400ms after the
    last frame — i.e. once the screen goes quiet — so the resting image the
    user actually stares at is crisp.

Both run over the same CDP connection. CDP is a single ordered stream, so a
slow captureScreenshot only delays the next frame; it can never arrive out of
order. A `capturing` flag prevents overlapping HQ captures.
"""
import asyncio
import json
import logging
import sys
import time
import urllib.request
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).parent))
from common_ws import ReconnectingWSClient  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("screencast")

CDP_HOST = "http://localhost:9222"
BACKEND_WS = "ws://127.0.0.1:8790/ingest/screencast"

RETRY_DELAY = 2

# Low-res motion feed.
SCREENCAST_PARAMS = {
    "format": "jpeg", "quality": 38,
    "maxWidth": 640, "maxHeight": 480,
    "everyNthFrame": 2,
}
HQ_QUALITY = 88          # one-shot full-res screenshot quality
QUIET_SECONDS = 0.4      # idle gap that means "the screen settled"


def _now_ms():
    return int(time.time() * 1000)


def find_page_target():
    tabs = json.loads(urllib.request.urlopen(f"{CDP_HOST}/json", timeout=5).read())
    for t in tabs:
        if t.get("type") == "page":
            return t
    return None


async def stream_screencast(client: ReconnectingWSClient):
    tab = find_page_target()
    if tab is None:
        return False

    async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=50 * 1024 * 1024,
                                   ping_timeout=30) as ws:
        _id = 0
        pending = {}
        quiet_task = None
        capturing = False
        # Cached from Page.getLayoutMetrics (once at start / on resize). Gives
        # real page dimensions for HQ frames (captureScreenshot carries none)
        # and the visualViewport scale used for DPR-correct highlights (Phase 2).
        layout = {"width": None, "height": None, "scale": 1.0}

        async def send(method, params=None):
            nonlocal _id
            _id += 1
            mid = _id
            fut = asyncio.get_event_loop().create_future()
            pending[mid] = fut
            await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            return await asyncio.wait_for(fut, timeout=10)

        async def refresh_layout():
            try:
                m = await send("Page.getLayoutMetrics")
                vv = m.get("visualViewport", {})
                css = m.get("cssContentSize") or m.get("contentSize") or {}
                layout["scale"] = vv.get("scale", 1.0) or 1.0
                layout["width"] = css.get("width")
                layout["height"] = css.get("height")
            except Exception:
                log.exception("getLayoutMetrics failed")

        async def capture_hq():
            nonlocal capturing
            if capturing:
                return
            capturing = True
            try:
                res = await send("Page.captureScreenshot", {"format": "jpeg", "quality": HQ_QUALITY})
                client.send({
                    "type": "frame", "hq": True, "ts": _now_ms(),
                    "data": res.get("data"),
                    "width": layout["width"], "height": layout["height"],
                })
            except Exception:
                log.exception("captureScreenshot failed")
            finally:
                capturing = False

        async def fire_after_quiet():
            try:
                await asyncio.sleep(QUIET_SECONDS)
            except asyncio.CancelledError:
                return
            await capture_hq()

        async def recv_loop():
            nonlocal quiet_task
            async for raw in ws:
                msg = json.loads(raw)
                mid = msg.get("id")
                if mid and mid in pending:
                    pending.pop(mid).set_result(msg.get("result", {}))
                    continue
                method = msg.get("method")
                if method == "Page.screencastFrame":
                    params = msg["params"]
                    # Ack immediately, per CDP spec, before doing anything else,
                    # or Chrome stops sending frames.
                    await ws.send(json.dumps({
                        "id": 0, "method": "Page.screencastFrameAck",
                        "params": {"sessionId": params["sessionId"]},
                    }))
                    meta = params.get("metadata", {})
                    client.send({
                        "type": "frame", "hq": False, "ts": _now_ms(),
                        "data": params.get("data"),
                        "width": meta.get("deviceWidth"),
                        "height": meta.get("deviceHeight"),
                    })
                    # Debounce: (re)arm the quiet timer on every frame. When
                    # frames stop for QUIET_SECONDS, fire one HQ capture.
                    if quiet_task:
                        quiet_task.cancel()
                    quiet_task = asyncio.create_task(fire_after_quiet())
                elif method in ("Page.frameResized",):
                    await refresh_layout()

        recv_task = asyncio.create_task(recv_loop())
        try:
            await send("Page.enable")
            await refresh_layout()
            await send("Page.startScreencast", SCREENCAST_PARAMS)
            await capture_hq()   # crisp first paint immediately
            await recv_task
        finally:
            recv_task.cancel()
            if quiet_task:
                quiet_task.cancel()
    return True


async def main():
    client = ReconnectingWSClient(BACKEND_WS, maxsize=5, drop_oldest=True, name="screencast-ws")
    client.start()
    while True:
        try:
            await stream_screencast(client)
        except Exception:
            log.exception("screencast session ended with error")
        await asyncio.sleep(RETRY_DELAY)


if __name__ == "__main__":
    asyncio.run(main())
