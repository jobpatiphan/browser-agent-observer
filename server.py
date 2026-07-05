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
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
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

# Optional shared-token auth. Empty (default) = wide open, which is fine on
# loopback. Set DASH_TOKEN to gate the HTTP API + websockets when binding beyond
# 127.0.0.1 (the UI shell and /healthz stay reachable so the page can bootstrap
# and then supply the token via ?token= / Authorization: Bearer).
DASH_TOKEN = os.environ.get("DASH_TOKEN", "")

# Optional durability. Set PERSIST_DIR to append this session's events (minus
# bulky screencast frames) to a JSONL file so it can be reopened later; unset
# (default) keeps everything in memory only.
PERSIST_DIR = os.environ.get("PERSIST_DIR", "")

# Where the traffic proxy listens, so replayed requests can be sent back through
# it (and thus re-captured on the timeline, Burp-Repeater style).
PROXY_HOST = os.environ.get("PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "8083"))

# Headers we must not copy verbatim when replaying — httpx recomputes them from
# the actual URL/body, and forwarding stale values corrupts the request.
_HOP_BY_HOP = {"host", "content-length", "connection", "transfer-encoding",
               "keep-alive", "proxy-authorization", "proxy-connection", "te",
               "trailer", "upgrade"}

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
snapshots: "deque[dict]" = deque(maxlen=50)
pending_highlights: "deque[dict]" = deque(maxlen=50)
pending_snapshots: "deque[dict]" = deque(maxlen=20)
dashboard_clients: set[WebSocket] = set()

# Per-session JSONL sink (opened once if PERSIST_DIR is set).
_session_file: Path | None = None
if PERSIST_DIR:
    try:
        Path(PERSIST_DIR).mkdir(parents=True, exist_ok=True)
        _session_file = Path(PERSIST_DIR) / f"session-{int(START_TIME)}.jsonl"
    except Exception:
        log.exception("could not open PERSIST_DIR %s", PERSIST_DIR)


def _persist(event: dict):
    # Append every event except bulky screencast frames so a session can be
    # reopened later. Best-effort — never let disk trouble break the live feed.
    if _session_file is None or event.get("type") == "frame":
        return
    try:
        with _session_file.open("a") as fh:
            fh.write(json.dumps(event) + "\n")
    except Exception:
        log.debug("persist failed", exc_info=True)

latest_tabs: list = []       # most recent CDP page-target list from the forwarder
selected_target: str = ""    # "" = auto-follow; else a specific CDP target id


def _origin_ok(ws: WebSocket) -> bool:
    origin = ws.headers.get("origin")
    # Non-browser clients (mitmproxy addon, screencast forwarder via the
    # `websockets` library) send no Origin header at all — allow those; only
    # reject a *present* Origin that isn't ours.
    return origin is None or origin in ALLOWED_WS_ORIGINS


def _http_authed(request) -> bool:
    if not DASH_TOKEN:
        return True
    p = request.url.path
    # Let the UI shell + liveness load unauthenticated so the page can bootstrap
    # and then present the token on the API/websocket.
    if p == "/" or p == "/healthz" or p.startswith("/static/"):
        return True
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer ") and auth[7:] == DASH_TOKEN:
        return True
    return request.query_params.get("token") == DASH_TOKEN


def _ws_authed(ws: WebSocket) -> bool:
    return not DASH_TOKEN or ws.query_params.get("token") == DASH_TOKEN


@app.middleware("http")
async def _auth_mw(request, call_next):
    if not _http_authed(request):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


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
    _persist(message)
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
    if not _origin_ok(ws) or not _ws_authed(ws):
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
    if not _origin_ok(ws) or not _ws_authed(ws):
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
    if not _origin_ok(ws) or not _ws_authed(ws):
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


def _body_text(side: dict) -> str:
    b = (side or {}).get("body")
    return b if isinstance(b, str) else ""


@app.get("/search")
async def search(q: str, limit: int = 100):
    # One box across everything captured: traffic (url + bodies), WS frames,
    # narration, commands and findings. Case-insensitive substring.
    ql = q.lower().strip()
    results = []
    if ql:
        for f in flows:
            hay = f"{f.get('method','')} {f.get('url','')} {f.get('status','')}".lower()
            body = (_body_text(f.get("request")) + " " + _body_text(f.get("response"))).lower()
            if ql in hay or ql in body:
                results.append({"kind": "flow", "ts": f.get("ts"), "ref": f.get("id"),
                                "label": f"{f.get('method')} {f.get('url')} [{f.get('status')}]"})
        for m in ws_messages:
            if ql in str(m.get("payload", "")).lower() or ql in str(m.get("url", "")).lower():
                results.append({"kind": "ws", "ts": m.get("ts"), "ref": m.get("id"),
                                "label": str(m.get("payload", ""))[:120]})
        for n in narration:
            if ql in (n.get("text") or "").lower():
                results.append({"kind": "narration", "ts": n.get("ts"), "label": n.get("text")})
        for c in commands:
            if ql in (c.get("cmd") or "").lower():
                results.append({"kind": "command", "ts": c.get("ts"), "label": c.get("cmd")})
        for f in findings:
            blob = f"{f.get('title','')} {f.get('detail','')} {f.get('url','')}".lower()
            if ql in blob:
                results.append({"kind": "finding", "ts": f.get("ts"), "ref": f.get("flow_id"),
                                "label": f"{f.get('severity')}: {f.get('title')}"})
    results.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return {"query": q, "count": len(results), "results": results[:limit]}


class SnapshotBody(BaseModel):
    label: str | None = None


@app.post("/snapshot")
async def snapshot(body: SnapshotBody):
    # Queue a request the screencast forwarder drains (like highlights): it
    # grabs a crisp HQ frame + the page DOM and posts it back to /snapshot-result.
    pending_snapshots.append({"label": body.label or "", "ts": int(time.time() * 1000)})
    return {"ok": True}


@app.get("/pending-snapshots")
async def get_pending_snapshots():
    items = list(pending_snapshots)
    pending_snapshots.clear()
    return {"snapshots": items}


class SnapshotResult(BaseModel):
    label: str | None = None
    url: str | None = None
    title: str | None = None
    html: str | None = None


@app.post("/snapshot-result")
async def snapshot_result(body: SnapshotResult):
    snap = {
        "type": "snapshot", "ts": int(time.time() * 1000),
        "label": body.label or "", "url": body.url, "title": body.title,
        "html": (body.html or "")[:200_000],
    }
    snapshots.append(snap)
    # A timeline marker so the snapshot shows up in Activity live.
    marker = {"type": "narration", "ts": snap["ts"], "level": "info",
              "text": f"📸 snapshot {snap['label']}: {body.title or body.url or ''}".strip()}
    narration.append(marker)
    await _broadcast(marker)
    return {"ok": True}


@app.get("/snapshots")
async def get_snapshots():
    return {"snapshots": list(snapshots)}


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
        "snapshots": list(snapshots),
    }


def _har_headers(side: dict) -> list:
    return [{"name": k, "value": v} for k, v in ((side or {}).get("headers") or [])]


def _har_entry(f: dict) -> dict:
    req = f.get("request") or {}
    resp = f.get("response") or {}
    url = f.get("url") or ""
    ts = f.get("ts") or 0
    dur = f.get("duration_ms") or 0
    started = datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat().replace("+00:00", "Z")
    entry = {
        "startedDateTime": started,
        "time": dur,
        "request": {
            "method": f.get("method") or "GET",
            "url": url,
            "httpVersion": "HTTP/1.1",
            "headers": _har_headers(req),
            "queryString": [{"name": k, "value": v}
                            for k, v in parse_qsl(urlsplit(url).query, keep_blank_values=True)],
            "cookies": [], "headersSize": -1, "bodySize": -1,
        },
        "response": {
            "status": f.get("status") or 0,
            "statusText": "",
            "httpVersion": "HTTP/1.1",
            "headers": _har_headers(resp),
            "cookies": [],
            "content": {
                "size": resp.get("size", 0) or 0,
                "mimeType": resp.get("content_type") or "",
                **({"text": resp["body"]} if isinstance(resp.get("body"), str) else {}),
            },
            "redirectURL": "", "headersSize": -1, "bodySize": f.get("size", -1),
        },
        "cache": {},
        "timings": {"send": 0, "wait": dur, "receive": 0},
    }
    if isinstance(req.get("body"), str) and req["body"]:
        entry["request"]["postData"] = {"mimeType": req.get("content_type") or "",
                                        "text": req["body"]}
    return entry


@app.get("/export.har")
async def export_har(redact: bool = False):
    # HAR 1.2 so captured traffic drops straight into Burp/ZAP/DevTools.
    src = [_redact_flow(f) for f in flows] if redact else list(flows)
    har = {"log": {
        "version": "1.2",
        "creator": {"name": "browser-agent-observer", "version": "1.0"},
        "entries": [_har_entry(f) for f in src],
    }}
    from fastapi.responses import JSONResponse
    return JSONResponse(har, headers={"content-disposition": "attachment; filename=session.har"})


@app.get("/sessions")
async def list_sessions():
    if not _session_file:
        return {"sessions": []}
    out = []
    for p in sorted(Path(PERSIST_DIR).glob("session-*.jsonl")):
        st = p.stat()
        out.append({"name": p.name, "size": st.st_size, "mtime": int(st.st_mtime)})
    return {"sessions": out}


@app.get("/sessions/{name}")
async def get_session(name: str):
    # Read a persisted JSONL back into an /export-shaped snapshot so the same
    # replay machinery can reopen a past session.
    if not PERSIST_DIR:
        raise HTTPException(404, "persistence disabled")
    p = Path(PERSIST_DIR) / os.path.basename(name)   # basename blocks traversal
    if not p.is_file():
        raise HTTPException(404, "no such session")
    grouped: dict = {"flows": [], "ws": [], "narration": [], "actions": [],
                     "commands": [], "findings": [], "snapshots": []}
    bucket = {"ws": "ws", "narration": "narration", "action": "actions",
              "command": "commands", "finding": "findings", "snapshot": "snapshots"}
    for line in p.read_text().splitlines():
        try:
            e = json.loads(line)
        except Exception:
            continue
        t = e.get("type")
        if t == "flow" and e.get("phase") == "response":
            grouped["flows"].append(e)
        elif t in bucket:
            grouped[bucket[t]].append(e)
    grouped["meta"] = {"name": os.path.basename(name), "replayed": True}
    return grouped


class ReplayBody(BaseModel):
    method: str = "GET"
    url: str
    headers: list | None = None          # [[name, value], ...]
    body: str | None = None
    through_proxy: bool = True            # re-capture on the timeline


def _replay_headers(headers) -> dict:
    out = {}
    for pair in headers or []:
        try:
            k, v = pair
        except (ValueError, TypeError):
            continue
        if str(k).lower() in _HOP_BY_HOP:
            continue
        out[str(k)] = v
    return out


@app.post("/replay")
async def replay(body: ReplayBody):
    # Burp-Repeater-lite: resend an (optionally edited) request. Routed back
    # through the proxy by default, so it reappears as a fresh flow — and the
    # findings engine re-scores it — automatically. This makes the tool *active*
    # (it sends traffic), which is why it lives behind DASH_TOKEN when set.
    import httpx
    proxy = f"http://{PROXY_HOST}:{PROXY_PORT}" if body.through_proxy else None
    hdrs = _replay_headers(body.headers)
    content = body.body.encode() if isinstance(body.body, str) else None
    t0 = time.time()
    try:
        async with httpx.AsyncClient(proxy=proxy, verify=False, timeout=20,
                                     follow_redirects=False) as c:
            r = await c.request(body.method.upper(), body.url, headers=hdrs, content=content)
    except Exception as e:
        raise HTTPException(502, f"replay failed: {e}")
    text = r.text
    return {
        "ok": True,
        "status": r.status_code,
        "duration_ms": int((time.time() - t0) * 1000),
        "headers": [[k, v] for k, v in r.headers.items()],
        "body": text[:200_000],
        "body_truncated": len(text) > 200_000,
        "size": len(r.content),
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
