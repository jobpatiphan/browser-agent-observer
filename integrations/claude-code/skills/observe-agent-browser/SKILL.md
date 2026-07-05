---
name: observe-agent-browser
description: Turn on the browser-agent-observer dashboard and live-mirror this session onto it. Use when the user types /observe-agent-browser, says "watch what you're doing", "show it on the dashboard", or wants a live visual of a browser/pentest task.
---

# observe-agent-browser — live-mirror this session to the dashboard

The global Claude Code hooks (`hooks/claude_mirror.py`) already stream your Bash
commands, prompts and file edits to the dashboard **whenever it's running** — so
your job here is to switch the dashboard on and add high-signal narration.

When invoked:

1. **Ensure the dashboard is up and open it.** Run
   `curl -s "${DASH_URL:-http://127.0.0.1:8790}/healthz"`. If it fails, start it
   from the browser-agent-observer checkout (commonly `~/pentest-dashboard`; ask
   if unknown): `./run.sh up`. `up` waits for the backend, then **auto-opens the
   dashboard** in the user's browser. If it was already running, open it with
   `./run.sh open`.
2. **Offer the wired-up browser:** tell the user they can launch a Chromium
   already pointed at the proxy + CDP with `./run.sh browser` (it reuses an
   existing CDP session instead of spawning a duplicate).
3. **Confirm mirroring is live** — you don't need to echo Bash commands yourself;
   the hooks do that. Just narrate *intent*.
4. **Narrate the story, not the mechanics.** For meaningful steps post a short
   what/why line so the Activity timeline reads well:
   ```
   python3 -c "import sys;sys.path.insert(0,'clients');from observer import obs; obs.narrate('Testing auth bypass on /admin', level='warn')"
   ```
5. **When you drive the browser**, mirror the action so it highlights in-frame:
   `obs.click('button#login', x=203, y=411)` (page pixels).
6. Keep it concise and high-signal — skip trivial reads.

**Record mode** — if the user says `/observe-agent-browser record` or asks to
save the session, run `./run.sh export` before stopping. It writes a
self-contained, redacted replay `.html` (credentials masked) that opens offline
with a frame scrubber synced to traffic + activity. Tell them the filename.

Stop watching with `./run.sh down`; the hooks auto-no-op once it's down, so
nothing lingers. `down` leaves your agent's browser running — it only stops the
observer services.
