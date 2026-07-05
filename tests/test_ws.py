import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from addon import DashboardAddon  # noqa: E402


def _addon_without_thread():
    # Bypass __init__ so we don't spin up the background ws client thread.
    a = DashboardAddon.__new__(DashboardAddon)
    sent = []
    a.client = SimpleNamespace(send=sent.append)
    a._start_ts = {}
    return a, sent


def test_websocket_text_message_event():
    a, sent = _addon_without_thread()
    msg = SimpleNamespace(content=b'{"hello":"world"}', from_client=True)
    flow = SimpleNamespace(
        id="f1",
        websocket=SimpleNamespace(messages=[msg]),
        request=SimpleNamespace(pretty_url="wss://t/socket"),
    )
    a.websocket_message(flow)
    e = sent[-1]
    assert e["type"] == "ws" and e["id"] == "f1"
    assert e["from_client"] is True
    assert e["encoding"] == "text"
    assert e["payload"] == '{"hello":"world"}'
    assert e["size"] == len(msg.content)


def test_websocket_binary_message_event():
    a, sent = _addon_without_thread()
    msg = SimpleNamespace(content=b"\xff\xfe\x00\x01", from_client=False)
    flow = SimpleNamespace(
        id="f2",
        websocket=SimpleNamespace(messages=[msg]),
        request=SimpleNamespace(pretty_url="wss://t/socket"),
    )
    a.websocket_message(flow)
    e = sent[-1]
    assert e["encoding"] == "base64"
    assert e["from_client"] is False
