"""Shared reconnecting websocket client used by addon.py and screencast_forwarder.py.

Runs its own asyncio loop in a background thread, drains a thread-safe queue,
and forwards each item as a JSON text frame to the backend. Reconnects with
exponential backoff whenever the connection drops or the backend is down,
so producers never need to know whether the backend is currently reachable.
"""
import asyncio
import json
import queue
import threading

import websockets


class ReconnectingWSClient:
    def __init__(self, url: str, maxsize: int = 5000, drop_oldest: bool = False, name: str = "ws-client"):
        self.url = url
        self.name = name
        self.drop_oldest = drop_oldest
        self._q: "queue.Queue" = queue.Queue(maxsize=maxsize)
        self._thread = threading.Thread(target=self._run, daemon=True, name=name)
        self._started = False

    def start(self):
        if not self._started:
            self._started = True
            self._thread.start()

    def send(self, obj: dict):
        payload = json.dumps(obj)
        try:
            self._q.put_nowait(payload)
        except queue.Full:
            if self.drop_oldest:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._q.put_nowait(payload)
                except queue.Full:
                    pass
            # else: silently drop the newest item rather than block traffic capture

    def _run(self):
        asyncio.run(self._loop())

    async def _loop(self):
        backoff = 1
        loop = asyncio.get_event_loop()
        while True:
            try:
                async with websockets.connect(self.url, open_timeout=5) as ws:
                    backoff = 1
                    while True:
                        payload = await loop.run_in_executor(None, self._q.get)
                        await ws.send(payload)
            except Exception:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10)
