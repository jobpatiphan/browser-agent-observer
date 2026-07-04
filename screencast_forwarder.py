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

import httpx
import websockets

sys.path.insert(0, str(Path(__file__).parent))
from common_ws import ReconnectingWSClient  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("screencast")

CDP_HOST = "http://localhost:9222"
BACKEND_HTTP = "http://127.0.0.1:8790"
BACKEND_WS = "ws://127.0.0.1:8790/ingest/screencast"

RETRY_DELAY = 2
HIGHLIGHT_POLL_SECONDS = 0.15   # imperceptible vs the 2-5fps hybrid feed
HIGHLIGHT_HOLD_SECONDS = 0.5    # how long an injected highlight stays up

# Injected into the page to draw the highlight box + a click ripple, both
# self-removing after `hold` ms so a marker can never get stuck even if this
# forwarder dies mid-highlight. All units are CSS pixels (position:fixed), which
# match the driver's click coordinates directly.
_HIGHLIGHT_JS = """(function(){
  var root=document.documentElement;
  var box=document.createElement('div');
  box.style.cssText='position:fixed;left:%(left)dpx;top:%(top)dpx;width:%(w)dpx;'+
    'height:%(h)dpx;border:2px solid rgb(245,158,11);background:rgba(245,158,11,.30);'+
    'border-radius:4px;z-index:2147483647;pointer-events:none;box-sizing:border-box;';
  var dot=document.createElement('div');
  dot.style.cssText='position:fixed;left:%(cx)dpx;top:%(cy)dpx;width:14px;height:14px;'+
    'margin:-7px 0 0 -7px;border:2px solid rgb(245,158,11);border-radius:50%%;'+
    'z-index:2147483647;pointer-events:none;box-sizing:border-box;';
  root.appendChild(box); root.appendChild(dot);
  setTimeout(function(){box.remove();dot.remove();}, %(hold)d);
  return 'ok';
})()"""

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

        async def capture_hq(force=False):
            nonlocal capturing
            if capturing and not force:
                return
            # Wait out an in-flight capture rather than dropping a forced one
            # (used by highlights, which must not be skipped).
            while capturing:
                await asyncio.sleep(0.02)
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

        async def show_highlight(h):
            # In-page highlight baked into real pixels by injecting a DOM node
            # via Runtime.evaluate.
            #
            # We deliberately do NOT use CDP's Overlay.highlightRect: verified on
            # this machine that its highlight renders on a separate compositor
            # layer that headless captureScreenshot / screencast never include
            # (the frame comes back pure white). Injecting a position:fixed
            # element instead puts the marker in the page's own content, so it
            # shows up in the very next captured frame — the effect the native
            # Overlay was supposed to give. Bonus: fixed-position CSS pixels map
            # 1:1 to the driver's click coords, so there's no visualViewport /
            # DPR scaling bug to correct (the concern that applied to the
            # device-pixel Overlay API simply doesn't arise here).
            cx = int(h["x"])
            cy = int(h["y"])
            w = int(h.get("w") or 36)
            ht = int(h.get("h") or 36)
            js = _HIGHLIGHT_JS % {
                "left": cx - w // 2, "top": cy - ht // 2, "w": w, "h": ht,
                "cx": cx, "cy": cy, "hold": int(HIGHLIGHT_HOLD_SECONDS * 1000),
            }
            try:
                await send("Runtime.evaluate", {"expression": js})
                # Proactively grab a frame *now* so the highlight is guaranteed
                # to reach the dashboard even on a static page that emits no
                # screencast frames of its own.
                await capture_hq(force=True)
                await asyncio.sleep(HIGHLIGHT_HOLD_SECONDS)
                await capture_hq(force=True)   # clean resting frame afterwards
            except Exception:
                log.exception("highlight failed")

        async def poll_highlights(http):
            while True:
                try:
                    r = await http.get(f"{BACKEND_HTTP}/pending-highlights")
                    for h in r.json().get("highlights", []):
                        await show_highlight(h)
                except Exception:
                    log.debug("highlight poll failed", exc_info=True)
                await asyncio.sleep(HIGHLIGHT_POLL_SECONDS)

        async def post_navigate(http, url):
            # Passive fallback: even with no cooperating driver, a plain page
            # navigation still lands a marker on the timeline.
            try:
                await http.post(f"{BACKEND_HTTP}/action",
                                json={"type": "navigate", "target": url})
            except Exception:
                log.debug("post_navigate failed", exc_info=True)

        http = httpx.AsyncClient(timeout=2.0)

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
                elif method == "Page.frameNavigated":
                    frame = msg["params"].get("frame", {})
                    if not frame.get("parentId"):   # main frame only
                        await refresh_layout()
                        await post_navigate(http, frame.get("url"))
                elif method == "Page.frameResized":
                    await refresh_layout()

        recv_task = asyncio.create_task(recv_loop())
        poll_task = asyncio.create_task(poll_highlights(http))
        try:
            await send("Page.enable")
            await send("Runtime.enable")
            await refresh_layout()
            await send("Page.startScreencast", SCREENCAST_PARAMS)
            await capture_hq()   # crisp first paint immediately
            await recv_task
        finally:
            recv_task.cancel()
            poll_task.cancel()
            if quiet_task:
                quiet_task.cancel()
            await http.aclose()
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
