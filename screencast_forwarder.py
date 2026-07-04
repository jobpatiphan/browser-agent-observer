"""Streams a live view of the Chromium tab Claude is driving to the dashboard.

Opens a second, independent CDP client connection to the same page target
(alongside whatever client Claude's own harness uses), starts a screencast,
and forwards each frame to the dashboard backend over websocket.

Reuses the connect/send/recv-loop pattern proven in /tmp/full_visual2.py's
cdp() helper.
"""
import asyncio
import base64
import json
import sys
import time
import urllib.request
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).parent))
from common_ws import ReconnectingWSClient  # noqa: E402

CDP_HOST = "http://localhost:9222"
BACKEND_WS = "ws://127.0.0.1:8790/ingest/screencast"

RETRY_DELAY = 2


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

        async def send(method, params=None):
            nonlocal _id
            _id += 1
            mid = _id
            fut = asyncio.get_event_loop().create_future()
            pending[mid] = fut
            await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            return await asyncio.wait_for(fut, timeout=10)

        async def recv_loop():
            async for raw in ws:
                msg = json.loads(raw)
                mid = msg.get("id")
                if mid and mid in pending:
                    pending.pop(mid).set_result(msg.get("result", {}))
                    continue
                if msg.get("method") == "Page.screencastFrame":
                    params = msg["params"]
                    session_id = params["sessionId"]
                    # Ack immediately, per CDP spec, before doing anything else.
                    await ws.send(json.dumps({
                        "id": 0, "method": "Page.screencastFrameAck",
                        "params": {"sessionId": session_id},
                    }))
                    meta = params.get("metadata", {})
                    client.send({
                        "type": "frame",
                        "ts": int(time.time() * 1000),
                        "data": params.get("data"),
                        "width": meta.get("deviceWidth"),
                        "height": meta.get("deviceHeight"),
                    })

        recv_task = asyncio.create_task(recv_loop())
        try:
            await send("Page.enable")
            await send("Page.startScreencast", {
                "format": "jpeg", "quality": 60,
                "maxWidth": 1024, "maxHeight": 768,
                "everyNthFrame": 1,
            })
            await recv_task
        finally:
            recv_task.cancel()
    return True


async def main():
    client = ReconnectingWSClient(BACKEND_WS, maxsize=5, drop_oldest=True, name="screencast-ws")
    client.start()
    while True:
        try:
            await stream_screencast(client)
        except Exception:
            pass
        await asyncio.sleep(RETRY_DELAY)


if __name__ == "__main__":
    asyncio.run(main())
