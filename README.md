# Visual Pentest Dashboard

Live 3-pane view of a Claude-driven web pentest: browser screencast, live
intercepted HTTP traffic (Burp/ZAP-style), and an agent narration log.

## Ports

| Port | Service |
|---|---|
| 8790 | dashboard backend (UI + websocket + ingest) |
| 8083 | mitmproxy traffic capture proxy |
| 9222 | Chromium CDP (Claude's existing browser harness) |

Retired: 8081/8082 (old mitmweb, replaced). ZAP (8090/8282) is a separate
tool — not part of this dashboard.

## Start / stop

Preferred (systemd user units):

```
systemctl --user start pentest-dashboard.target
systemctl --user status pentest-dashboard-backend pentest-dashboard-mitmproxy pentest-dashboard-screencast
journalctl --user -u pentest-dashboard-mitmproxy -f
systemctl --user stop pentest-dashboard.target
```

Fallback (if systemd --user units don't survive/behave, e.g. due to `Linger=no`):

```
./launcher.sh start|stop|status|restart
```

## Is it already running?

```
curl -s localhost:8790/healthz
```

`{"status":"ok",...}` means it's up — don't start it again.

## One-time persistence fix

This user has `Linger=no` and no passwordless sudo, so systemd `--user`
services may be torn down when the last login session ends. If
`/healthz` stops responding after a full logout, run once (interactive,
needs password):

```
sudo loginctl enable-linger kali
```

## Pointing Claude's browser at the proxy

Chromium's `--proxy-server` is launch-time only — to route Claude's
traffic through the dashboard, (re)launch it with:

```
chromium --headless=new --remote-debugging-port=9222 \
  --user-data-dir=/tmp/cdp_demo \
  --proxy-server=127.0.0.1:8083 \
  --ignore-certificate-errors --ignore-certificate-errors-spki-list
```

Keeping the same `--user-data-dir` preserves cookies/session across a
relaunch. Then have the CDP harness re-navigate to the target — the
screencast forwarder will pick up the (only) page target automatically.

## Feeding the timeline (HTTP API)

Anything (Claude, a shell wrapper, a custom driver) can push events into the
dashboard over plain HTTP — no websocket needed:

```
# Narration line (info | warn | error)
curl -sX POST localhost:8790/narrate \
  -H 'content-type: application/json' \
  -d '{"text":"Logging in with test creds...","level":"info"}'

# Action marker — shows a cursor/ripple + native in-page highlight, and a
# marker on the filmstrip timeline. coords are page pixels (viewport-relative).
curl -sX POST localhost:8790/action \
  -H 'content-type: application/json' \
  -d '{"type":"click","target":"button#login","coords":{"x":203,"y":411}}'

# Command line (rendered monospace in the secondary log tab)
curl -sX POST localhost:8790/command \
  -H 'content-type: application/json' \
  -d '{"cmd":"curl -s https://target/api/me -H \"Auth: ...\""}'
```

`navigate` actions also appear automatically (no driver needed): the screencast
forwarder watches CDP `Page.frameNavigated` and posts them for you.

## Dashboard UI

Open `http://127.0.0.1:8790/` in a browser.
