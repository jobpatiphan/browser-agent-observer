import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import findings  # noqa: E402


def _flow(**kw):
    base = {
        "type": "flow", "phase": "response", "id": "f1", "ts": 1,
        "method": "GET", "url": "https://t/x", "path": "/x", "status": 200,
        "request": {"headers": [], "content_type": ""},
        "response": {"headers": [], "content_type": "", "body": "", "body_encoding": "text"},
    }
    base.update(kw)
    return base


def _kinds(flow):
    return {f["kind"] for f in findings.analyze(flow)}


def test_secret_in_url():
    f = _flow(url="https://t/api?access_token=abc123&page=2")
    assert "secret-in-url" in _kinds(f)


def test_creds_in_url():
    f = _flow(url="https://user:pass@t/api")
    assert "creds-in-url" in _kinds(f)


def test_insecure_cookie():
    f = _flow(response={"headers": [["set-cookie", "sid=abc; Path=/"]],
                        "content_type": "text/html", "body": "", "body_encoding": "text"})
    ks = _kinds(f)
    assert "insecure-cookie" in ks


def test_secure_cookie_ok():
    f = _flow(response={"headers": [["set-cookie", "sid=abc; Secure; HttpOnly; SameSite=Lax"]],
                        "content_type": "application/json", "body": "", "body_encoding": "text"})
    assert "insecure-cookie" not in _kinds(f)


def test_cors_credentialed_wildcard():
    f = _flow(response={"headers": [["access-control-allow-origin", "*"],
                                    ["access-control-allow-credentials", "true"]],
                        "content_type": "application/json", "body": "", "body_encoding": "text"})
    assert "cors-credentialed-wildcard" in _kinds(f)


def test_server_error_and_verbose():
    f = _flow(status=500, response={
        "headers": [], "content_type": "text/html",
        "body": "Traceback (most recent call last): File x", "body_encoding": "text"})
    ks = _kinds(f)
    assert "server-error" in ks and "verbose-error" in ks


def test_missing_security_headers_on_html():
    f = _flow(response={"headers": [], "content_type": "text/html; charset=utf-8",
                        "body": "<html><title>Hi</title></html>", "body_encoding": "text"})
    ks = _kinds(f)
    assert "missing-content-security-policy" in ks
    assert "missing-x-content-type-options" in ks


def test_frame_ancestors_supersedes_xfo():
    f = _flow(response={
        "headers": [["content-security-policy", "frame-ancestors 'none'"]],
        "content_type": "text/html", "body": "<html><title>x</title></html>",
        "body_encoding": "text"})
    assert "missing-x-frame-options" not in _kinds(f)


def test_reflected_input():
    f = _flow(url="https://t/s?q=needle99",
              response={"headers": [], "content_type": "text/html",
                        "body": "<html>you searched needle99</html>", "body_encoding": "text"})
    assert "reflected-input" in _kinds(f)


def test_clean_json_response_has_no_findings():
    f = _flow(url="https://t/api/ok",
              response={"headers": [["strict-transport-security", "max-age=1"]],
                        "content_type": "application/json", "body": "{}", "body_encoding": "text"})
    assert findings.analyze(f) == []


def test_analyze_never_raises_on_garbage():
    assert findings.analyze({"garbage": True}) == []
    assert findings.analyze({"response": {"headers": "notalist"}}) == []
