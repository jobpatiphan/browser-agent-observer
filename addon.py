"""mitmproxy addon: streams every request/response to the dashboard backend.

Run with:
    mitmdump -s addon.py --listen-host 127.0.0.1 --listen-port 8083

Never blocks the flow-processing path on network I/O — events are handed
to a bounded queue that a background-thread ReconnectingWSClient drains,
so the proxy keeps working even if the dashboard backend is down/restarting.
"""
import base64
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common_ws import ReconnectingWSClient  # noqa: E402

log = logging.getLogger(__name__)

# Where the dashboard backend lives (host:port). Overridable via env.
_BACKEND = os.environ.get("DASH_BACKEND", "127.0.0.1:8790")
BACKEND_WS = os.environ.get("DASH_INGEST_MITM_URL", f"ws://{_BACKEND}/ingest/mitmproxy")

MAX_BODY_BYTES = 200_000
PREVIEW_BYTES = 20_000
MAX_IMAGE_BYTES = 50_000
WS_PREVIEW_CHARS = 4_000

TEXT_TYPES = ("text/", "application/json", "application/xml", "application/javascript",
              "application/x-www-form-urlencoded")


def _summarize_body(content_type: str, data: bytes) -> dict:
    content_type = (content_type or "").lower()
    size = len(data)

    if content_type.startswith("image/") or content_type.startswith("font/") or \
            content_type in ("application/octet-stream",) or content_type.startswith("video/"):
        if size <= MAX_IMAGE_BYTES:
            return {"body": base64.b64encode(data).decode(), "body_truncated": False,
                    "body_encoding": "base64", "size": size}
        return {"body": None, "body_truncated": True, "body_encoding": "omitted", "size": size}

    is_text = any(content_type.startswith(t) for t in TEXT_TYPES) or content_type == ""
    if is_text:
        preview = data[:PREVIEW_BYTES]
        text = preview.decode("utf-8", errors="replace")
        if "json" in content_type:
            try:
                import json as _json
                text = _json.dumps(_json.loads(data[:MAX_BODY_BYTES].decode("utf-8", errors="replace")), indent=2)
            except Exception:
                pass
        return {"body": text, "body_truncated": size > PREVIEW_BYTES,
                "body_encoding": "text", "size": size}

    if size <= MAX_IMAGE_BYTES:
        return {"body": base64.b64encode(data).decode(), "body_truncated": False,
                "body_encoding": "base64", "size": size}
    return {"body": None, "body_truncated": True, "body_encoding": "omitted", "size": size}


def _headers_list(headers) -> list:
    """Preserve every header line, including repeated names.

    mitmproxy's ``dict(headers)`` folds duplicate names into a single
    comma-joined value, which is invalid for ``Set-Cookie`` (RFC 6265 forbids
    it, and ``Expires=Wed, 21 Oct ...`` already contains commas). Emitting an
    ordered list of ``[name, value]`` pairs keeps each line intact so the UI
    can show every cookie separately.
    """
    return [[k, v] for k, v in headers.items(multi=True)]


class DashboardAddon:
    def __init__(self):
        self.client = ReconnectingWSClient(BACKEND_WS, maxsize=5000, name="mitm-dashboard-ws")
        self.client.start()
        self._start_ts = {}

    def request(self, flow):
        self._start_ts[flow.id] = time.time()
        req = flow.request
        event = {
            "type": "flow",
            "phase": "request",
            "id": flow.id,
            "ts": int(time.time() * 1000),
            "method": req.method,
            "url": req.pretty_url,
            "path": req.path.split("?")[0] if req.path else req.path,
        }
        self.client.send(event)

    def response(self, flow):
        req = flow.request
        resp = flow.response
        started = self._start_ts.pop(flow.id, None)
        duration_ms = int((time.time() - started) * 1000) if started else None

        req_ct = req.headers.get("content-type", "")
        resp_ct = resp.headers.get("content-type", "") if resp else ""

        req_body = _summarize_body(req_ct, req.raw_content or b"")
        resp_body = _summarize_body(resp_ct, resp.raw_content or b"") if resp else {
            "body": None, "body_truncated": False, "body_encoding": "text", "size": 0
        }

        event = {
            "type": "flow",
            "phase": "response",
            "id": flow.id,
            "ts": int(time.time() * 1000),
            "method": req.method,
            "url": req.pretty_url,
            "path": req.path.split("?")[0] if req.path else req.path,
            "status": resp.status_code if resp else None,
            "size": resp_body.get("size", 0),
            "duration_ms": duration_ms,
            "request": {
                "headers": _headers_list(req.headers),
                "content_type": req_ct,
                **req_body,
            },
            "response": {
                "headers": _headers_list(resp.headers) if resp else [],
                "content_type": resp_ct,
                **resp_body,
            },
        }
        self.client.send(event)

    def error(self, flow):
        # A flow that errors out (connection reset, TLS failure, timeout) never
        # reaches response(), so its start timestamp would leak forever. Clean
        # it up and tell the UI to flip the pending row to "failed".
        self._start_ts.pop(flow.id, None)
        self.client.send({
            "type": "flow",
            "phase": "error",
            "id": flow.id,
            "ts": int(time.time() * 1000),
        })

    def websocket_message(self, flow):
        # Modern agents talk over WebSockets a lot; surface each frame so the
        # dashboard shows the live stream, not just the upgrade handshake.
        msg = flow.websocket.messages[-1]
        data = msg.content or b""
        try:
            text = data.decode("utf-8")
            is_text = True
        except UnicodeDecodeError:
            text, is_text = None, False
        payload = (text[:WS_PREVIEW_CHARS] if is_text
                   else base64.b64encode(data[:WS_PREVIEW_CHARS]).decode())
        self.client.send({
            "type": "ws",
            "id": flow.id,
            "ts": int(time.time() * 1000),
            "from_client": bool(msg.from_client),
            "encoding": "text" if is_text else "base64",
            "payload": payload,
            "truncated": len(data) > WS_PREVIEW_CHARS,
            "size": len(data),
            "url": flow.request.pretty_url,
        })


addons = [DashboardAddon()]
