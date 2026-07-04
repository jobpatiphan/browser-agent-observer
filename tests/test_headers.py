import sys
from pathlib import Path

from mitmproxy.http import Headers

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from addon import _headers_list  # noqa: E402


def test_repeated_set_cookie_kept_separate():
    # The bug this guards: dict(headers) folds duplicate names into one
    # comma-joined value, corrupting Set-Cookie (whose Expires already has a
    # comma). _headers_list must keep each line intact.
    h = Headers([
        (b"set-cookie", b"a=1; Expires=Wed, 21 Oct 2025 07:28:00 GMT"),
        (b"set-cookie", b"b=2"),
    ])
    pairs = _headers_list(h)
    setc = [v for k, v in pairs if k.lower() == "set-cookie"]
    assert len(setc) == 2
    assert setc[0].startswith("a=1")
    assert "b=2" in setc
    # never comma-folded into a single value
    assert all(", b=2" not in v for v in setc)


def test_headers_are_list_of_pairs():
    h = Headers([(b"content-type", b"text/html")])
    pairs = _headers_list(h)
    assert pairs == [["content-type", "text/html"]]
