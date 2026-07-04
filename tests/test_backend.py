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
