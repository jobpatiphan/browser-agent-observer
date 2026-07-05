"""Passive security analysis of captured HTTP flows.

`analyze(flow)` takes one response-phase flow event (the shape addon.py emits)
and returns a list of finding dicts — things worth a pentester's attention:
secrets on the wire, missing hardening headers, insecure cookies, reflected
input, server errors, verbose stack traces, CORS mistakes.

Pure and defensive: it never raises and never does I/O, so the ingest path can
call it inline. It's a *heuristic* triage aid, not a scanner — every finding is
a lead to verify by hand, and false positives are expected.
"""
from urllib.parse import urlsplit, parse_qsl

# Severity ranking so the UI/export can sort most-serious first.
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}

# Query-param names that should never travel in a URL (logged, cached, refererd).
SECRET_PARAMS = {
    "token", "access_token", "refresh_token", "id_token", "api_key", "apikey",
    "key", "sig", "signature", "password", "passwd", "pwd", "secret",
    "session", "sessionid", "auth", "jwt",
}

# Substrings that betray a server-side stack trace / verbose error leaking
# framework, path or query internals.
ERROR_SIGNATURES = (
    "Traceback (most recent call last)", "java.lang.", "at System.",
    "SQLSTATE", "ORA-0", "You have an error in your SQL syntax",
    "Warning: mysql_", "Microsoft OLE DB", "PHP Warning", "PHP Fatal error",
    "Exception in thread", "org.springframework", "System.Web",
    "Undefined index:", ".rb:", "goroutine ", "stack traceback:",
)

# Hardening headers we expect on an HTML document, with why they matter.
DOC_SECURITY_HEADERS = {
    "content-security-policy": "no CSP — no defence-in-depth against injected scripts",
    "x-content-type-options": "missing X-Content-Type-Options: nosniff (MIME sniffing)",
    "x-frame-options": "no X-Frame-Options / frame-ancestors (clickjacking)",
    "referrer-policy": "no Referrer-Policy (URLs may leak via Referer)",
}


def _headers_dict(side: dict) -> dict:
    """Lower-cased name -> list of values, tolerant of the [[k,v],...] shape."""
    out: dict[str, list[str]] = {}
    for pair in (side or {}).get("headers") or []:
        try:
            k, v = pair
        except (ValueError, TypeError):
            continue
        out.setdefault(str(k).lower(), []).append(v)
    return out


def _text_body(side: dict) -> str:
    if (side or {}).get("body_encoding") == "text" and isinstance(side.get("body"), str):
        return side["body"]
    return ""


def _finding(flow, sev, kind, title, detail):
    return {
        "type": "finding",
        "id": f"{flow.get('id', '?')}:{kind}",
        "flow_id": flow.get("id"),
        "ts": flow.get("ts"),
        "severity": sev,
        "kind": kind,
        "title": title,
        "detail": detail,
        "method": flow.get("method"),
        "url": flow.get("url"),
        "path": flow.get("path"),
    }


def analyze(flow: dict) -> list:
    """Return a list of finding dicts for one response-phase flow event."""
    try:
        return _analyze(flow)
    except Exception:
        return []


def _analyze(flow: dict) -> list:
    out = []
    url = flow.get("url") or ""
    status = flow.get("status")
    req = flow.get("request") or {}
    resp = flow.get("response") or {}
    rhead = _headers_dict(resp)
    body = _text_body(resp)
    ctype = (resp.get("content_type") or "").lower()

    split = urlsplit(url) if url else None
    query = parse_qsl(split.query, keep_blank_values=True) if split and split.query else []

    # --- secrets on the wire --------------------------------------------
    secret_hits = sorted({k for k, _ in query if k.lower() in SECRET_PARAMS})
    if secret_hits:
        out.append(_finding(flow, "high", "secret-in-url",
                            "Secret in URL query string",
                            f"param(s) {', '.join(secret_hits)} — URLs leak via logs, "
                            "history, Referer and caches"))
    if split and (split.username or split.password):
        out.append(_finding(flow, "high", "creds-in-url",
                            "Credentials in URL",
                            "basic-auth user:pass embedded in the request URL"))

    # --- insecure cookies -----------------------------------------------
    for raw in rhead.get("set-cookie", []):
        low = raw.lower()
        name = raw.split("=", 1)[0].strip()
        missing = [flag for flag, present in (
            ("Secure", "secure" in low),
            ("HttpOnly", "httponly" in low),
            ("SameSite", "samesite" in low),
        ) if not present]
        if missing:
            out.append(_finding(flow, "medium", "insecure-cookie",
                                f"Cookie '{name}' missing {'/'.join(missing)}",
                                "session cookies without these flags are exposed to "
                                "JS/CSRF/plaintext interception"))

    # --- CORS -----------------------------------------------------------
    acao = (rhead.get("access-control-allow-origin") or [None])[0]
    acac = (rhead.get("access-control-allow-credentials") or [None])[0]
    if acao == "*" and str(acac).lower() == "true":
        out.append(_finding(flow, "high", "cors-credentialed-wildcard",
                            "CORS allows any origin with credentials",
                            "Access-Control-Allow-Origin: * together with "
                            "Allow-Credentials: true exposes authenticated responses"))
    elif acao == "*":
        out.append(_finding(flow, "low", "cors-wildcard",
                            "CORS allows any origin",
                            "Access-Control-Allow-Origin: * — fine for public data, "
                            "risky for anything authenticated"))

    # --- transport / server-side signals --------------------------------
    is_https = url.startswith("https://")
    if is_https and not rhead.get("strict-transport-security"):
        out.append(_finding(flow, "low", "missing-hsts",
                            "No HSTS header",
                            "HTTPS response without Strict-Transport-Security"))
    if isinstance(status, int) and status >= 500:
        out.append(_finding(flow, "medium", "server-error",
                            f"Server error {status}",
                            "5xx often exposes stack traces or unhandled edge cases"))

    if body:
        sig = next((s for s in ERROR_SIGNATURES if s in body), None)
        if sig:
            out.append(_finding(flow, "medium", "verbose-error",
                                "Verbose error / stack trace in response",
                                f"body contains '{sig}' — may leak paths, queries, versions"))
        if "Index of /" in body and "<title>Index of" in body:
            out.append(_finding(flow, "low", "directory-listing",
                                "Directory listing exposed",
                                "auto-generated 'Index of /' page"))

    # --- missing hardening headers on an HTML document ------------------
    if "text/html" in ctype and isinstance(status, int) and 200 <= status < 300 and body:
        has_frame_ancestors = any(
            "frame-ancestors" in v.lower() for v in rhead.get("content-security-policy", []))
        for hdr, why in DOC_SECURITY_HEADERS.items():
            if hdr == "x-frame-options" and has_frame_ancestors:
                continue  # CSP frame-ancestors supersedes X-Frame-Options
            if not rhead.get(hdr):
                out.append(_finding(flow, "low", f"missing-{hdr}",
                                    "Missing security header",
                                    why))

    # --- reflected input (possible XSS) ---------------------------------
    if body and "html" in ctype:
        seen = set()
        for k, v in query:
            if len(v) >= 4 and k.lower() not in SECRET_PARAMS and v not in seen and v in body:
                seen.add(v)
                out.append(_finding(flow, "medium", "reflected-input",
                                    "Request parameter reflected in response",
                                    f"value of '{k}' appears verbatim in the HTML — "
                                    "check for XSS / injection"))

    return out


def sort_key(f: dict):
    """Most-severe first, then newest."""
    return (SEVERITY_ORDER.get(f.get("severity"), 9), -(f.get("ts") or 0))
