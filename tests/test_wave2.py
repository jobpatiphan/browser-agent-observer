import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402

client = TestClient(server.app)


def test_search_across_traffic():
    with client.websocket_connect("/ingest/mitmproxy") as ws:
        ws.send_json({
            "type": "flow", "phase": "response", "id": "srch1", "ts": 1,
            "method": "GET", "url": "https://t/uniqueNeedle42?a=1", "path": "/uniqueNeedle42",
            "status": 200, "request": {"headers": []}, "response": {"headers": []},
        })
    r = client.get("/search", params={"q": "uniqueNeedle42"}).json()
    assert r["count"] >= 1
    assert any(res["kind"] == "flow" for res in r["results"])


def test_search_empty_query_returns_nothing():
    r = client.get("/search", params={"q": ""}).json()
    assert r["count"] == 0


def test_snapshot_queue_and_result():
    assert client.post("/snapshot", json={"label": "login"}).json()["ok"]
    pend = client.get("/pending-snapshots").json()["snapshots"]
    assert any(s["label"] == "login" for s in pend)
    # draining is one-shot
    assert client.get("/pending-snapshots").json()["snapshots"] == []
    client.post("/snapshot-result", json={"label": "login", "url": "https://t/login",
                                          "title": "Login", "html": "<html>x</html>"})
    snaps = client.get("/snapshots").json()["snapshots"]
    assert any(s["title"] == "Login" for s in snaps)
    # snapshot posts a timeline marker
    assert any("snapshot" in n["text"] for n in client.get("/export").json()["narration"])


def test_sessions_empty_without_persistence():
    assert client.get("/sessions").json()["sessions"] == []
    assert client.get("/sessions/whatever.jsonl").status_code == 404


def test_auth_gate(monkeypatch):
    monkeypatch.setattr(server, "DASH_TOKEN", "sekret")
    # UI shell + healthz stay open so the page can bootstrap
    assert client.get("/healthz").status_code == 200
    # API is gated
    assert client.get("/export").status_code == 401
    assert client.get("/export", headers={"authorization": "Bearer sekret"}).status_code == 200
    assert client.get("/export", params={"token": "sekret"}).status_code == 200
    assert client.get("/export", params={"token": "wrong"}).status_code == 401
