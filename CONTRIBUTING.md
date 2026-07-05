# Contributing

Thanks for helping improve browser-agent-observer!

## Dev setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt   # runtime deps + pytest
```

Run it: `./run.sh up` (see the README quickstart).

## Tests

```bash
pytest -q
```

CI (`.github/workflows/ci.yml`) compiles every source file and runs the suite on
Python 3.13 for each push/PR. Please add or update tests for behaviour changes —
especially the backend (`tests/test_backend.py`) and anything touching the
header/redaction logic.

## Verifying by hand

This is a *visual* tool, so a green test suite isn't the whole story. For UI or
capture changes, run the stack, drive a browser through the proxy, and confirm
the dashboard actually shows what you expect (a screenshot in the PR helps).

## Style

- Match the surrounding code; no build step, no framework — vanilla HTML/CSS/JS
  on the front end, stdlib-first on the back end.
- Keep the observer's failure mode *silent*: capture/observability must never
  break the agent or the proxied traffic.
- Localhost, single-user, in-memory by design. Please don't add auth/DB/network
  exposure without discussing it first (open an issue).

## Pull requests

Keep them focused and describe what you changed and how you verified it. Small,
reviewable commits are appreciated.
