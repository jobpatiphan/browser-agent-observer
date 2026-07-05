import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402

client = TestClient(server.app)


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_narrate_and_export_shape():
    client.post("/narrate", json={"text": "hello test", "level": "warn"})
    data = client.get("/export").json()
    assert set(data) >= {"meta", "flows", "frames", "narration", "actions", "commands"}
    assert any(n["text"] == "hello test" and n["level"] == "warn" for n in data["narration"])


def test_command_lands():
    client.post("/command", json={"cmd": "whoami"})
    assert any(c["cmd"] == "whoami" for c in client.get("/export").json()["commands"])


def test_click_action_queues_highlight_then_drains():
    client.get("/pending-highlights")  # clear
    client.post("/action", json={"type": "click", "target": "b#x",
                                 "coords": {"x": 10, "y": 20}, "w": 40, "h": 30})
    hl = client.get("/pending-highlights").json()["highlights"]
    assert hl and hl[-1] == {"x": 10, "y": 20, "w": 40, "h": 30}
    # drain-on-read
    assert client.get("/pending-highlights").json()["highlights"] == []


def test_navigate_action_queues_no_highlight():
    client.get("/pending-highlights")  # clear
    client.post("/action", json={"type": "navigate", "target": "https://t/"})
    assert client.get("/pending-highlights").json()["highlights"] == []


def test_metrics_exposition():
    text = client.get("/metrics").text
    assert "dashboard_flows" in text
    assert "dashboard_uptime_seconds" in text


def test_ws_bad_origin_rejected():
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/dashboard", headers={"origin": "http://evil.example"}):
            pass


def test_ws_no_origin_allowed():
    # Non-browser clients (addon, forwarder) send no Origin — must be allowed.
    with client.websocket_connect("/ws/dashboard"):
        pass


def test_export_redaction():
    # Push a flow with a secret header + token in the URL via the ingest socket.
    with client.websocket_connect("/ingest/mitmproxy") as ws:
        ws.send_json({
            "type": "flow", "phase": "response", "id": "sec1",
            "ts": 1, "method": "GET", "url": "https://t/api?access_token=SECRET123&page=2",
            "path": "/api", "status": 200, "size": 0, "duration_ms": 1,
            "request": {"headers": [["authorization", "Bearer TOPSECRET"], ["accept", "*/*"]]},
            "response": {"headers": [["set-cookie", "sid=abc; HttpOnly"]]},
        })
    raw = client.get("/export?redact=0").json()
    red = client.get("/export?redact=1").json()
    rf = next(f for f in raw["flows"] if f["id"] == "sec1")
    df = next(f for f in red["flows"] if f["id"] == "sec1")
    # raw keeps secrets
    assert "TOPSECRET" in dict((k, v) for k, v in rf["request"]["headers"])["authorization"]
    assert "SECRET123" in rf["url"]
    # redacted masks them but keeps non-sensitive
    rheaders = dict((k, v) for k, v in df["request"]["headers"])
    assert rheaders["authorization"] == "‹redacted›"
    assert rheaders["accept"] == "*/*"
    assert "SECRET123" not in df["url"] and "page=2" in df["url"]
    assert dict((k, v) for k, v in df["response"]["headers"])["set-cookie"] == "‹redacted›"
    assert red["meta"]["redacted"] is True
