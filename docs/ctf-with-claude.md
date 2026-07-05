# Running a CTF with Claude Code + the observer

A practical, copy-paste workflow for solving a web CTF while Claude Code drives
and the dashboard watches everything — traffic, screencast, and auto security
findings — in real time.

## 0. One-time setup

```bash
cd ~/pentest-dashboard
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

Install the Claude Code integration once (hook + skill):

```bash
# hook: mirror every Bash command / prompt / edit onto the timeline
#   merge integrations/claude-code/settings.snippet.json into ~/.claude/settings.json
#   (fix the path to this checkout)
# skill: the on-switch
cp -r integrations/claude-code/skills/observe-agent-browser ~/.claude/skills/observe-agent-browser
```

The hook is safe to leave installed globally — it no-ops in ~0.15s whenever the
dashboard isn't running.

## 1. Start the observer (one command)

```bash
./run.sh up
```

It waits until the backend **and** the proxy are actually accepting connections,
prints `✓ ready`, and opens the dashboard at <http://127.0.0.1:8790>. Don't fire
traffic before you see `✓ ready`.

Optional but recommended for a CTF — scope capture to the target so the timeline
isn't buried in noise:

```bash
SCOPE_HOSTS="ctf.target.tld,*.target.tld" ./run.sh up
```

Keep the whole session for your writeup:

```bash
PERSIST_DIR=./sessions SCOPE_HOSTS="ctf.target.tld" ./run.sh up
```

## 2. Point traffic at the proxy

**Browser-based challenge** — launch the wired-up browser:

```bash
./run.sh browser        # Chromium on CDP :9222, routed through the proxy
```

**CLI-based challenge** (curl / ffuf / sqlmap / nuclei …) — send the tools
through the proxy so they show up on the dashboard too. Tell Claude to export
these at the start of the session:

```bash
export HTTP_PROXY=http://127.0.0.1:8083
export HTTPS_PROXY=http://127.0.0.1:8083
# tools that ignore the env vars take a flag instead, e.g.  curl -x $HTTP_PROXY
```

(For HTTPS interception, either keep `--ignore-certificate-errors` on the browser
or trust the mitmproxy CA — see the main README.)

## 3. Turn on mirroring in Claude Code

Open a Claude Code session **in this repo** and type:

```
/observe-agent-browser
```

From then on the hook streams every command Claude runs, and Claude narrates its
intent. Give it the target, e.g.:

> The CTF is at http://ctf.target.tld — find and exploit the auth bypass on
> /admin. Route curl through $HTTP_PROXY so I can watch the traffic.

## 4. What to watch while it works

| Pane | What it tells you during the CTF |
|---|---|
| **Browser** | live screencast of the page Claude is on (if browser-based) |
| **Traffic** | every request/response, Burp/ZAP-style — status, timing, cookies, WS |
| **Findings** | auto-flags: secrets in URLs, SQLi/XSS payloads Claude tried, reflected input, insecure cookies, missing headers, PII/keys in responses |
| **◉ Graph** | attack-surface map — every host touched, red = a high-severity finding on it |
| **Activity** | Claude's narration + the exact commands it ran, on a timeline |

## 5. Jump in by hand

- **⟳ Replay** — click any captured request → edit method/url/headers/body →
  **Send**. It goes back through the proxy and is re-scored. This is your
  Repeater for tweaking a payload Claude found.
- **🔍 search everything** — find a token, parameter or endpoint across all
  traffic + findings at once.
- **HAR** — export the traffic and open it in Burp / ZAP for deeper work.

## 6. Wrap up (writeup)

```bash
./run.sh export        # self-contained, redacted replay .html (frame scrubber)
```

Or grab the raw artifacts: `GET /export.har` (traffic) and, if you set
`PERSIST_DIR`, reopen the whole session later via `GET /sessions`.

Stop everything with `./run.sh down` (leaves your browser open; the hook
auto-no-ops afterwards).

## Troubleshooting

| Symptom | Fix |
|---|---|
| traffic not showing | is the tool/browser actually using the proxy? check `HTTP_PROXY` / `-x`, and that you're in scope (`SCOPE_HOSTS`) |
| `run.sh browser` says CDP already live | that's fine — it's reusing the browser already up on :9222 |
| HTTPS requests fail | trust the mitmproxy CA, or keep `--ignore-certificate-errors` |
| nothing on the timeline from Claude | the hook isn't installed / points at the wrong path — see `integrations/claude-code/` |
| bound beyond localhost | set `DASH_TOKEN` and open the UI as `…/?token=YOUR_TOKEN` |
