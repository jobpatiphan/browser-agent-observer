# AGENTS.md — coordinating with browser-agent-observer

This project runs a live observability dashboard. If you (Codex, or any agent
reading this file) are doing browser or pentest work here **and the dashboard is
up** (`curl -s http://127.0.0.1:8790/healthz` returns ok), mirror what you do so
the dashboard timeline reflects it. Codex has no hook system, so this is
best-effort and depends on you using the helpers below.

## Narrate intent

```bash
python3 -c "import sys;sys.path.insert(0,'clients');from observer import obs; obs.narrate('Enumerating admin endpoints', level='info')"
```

## Run shell commands through the wrapper so they appear on the timeline

```bash
./integrations/codex/obs-run.sh sqlmap -u https://target/login --forms
# or:  alias obs-run="$(pwd)/integrations/codex/obs-run.sh"  then  obs-run <cmd>
```

## Mirror browser actions (they highlight in-frame)

```bash
python3 -c "import sys;sys.path.insert(0,'clients');from observer import obs; obs.click('button#login', x=203, y=411)"
```

Everything degrades gracefully: if the dashboard is down, `obs-run` just runs
the command and the `observer` calls no-op. To start the dashboard: `./run.sh up`.

> Claude Code users get this automatically via hooks (`integrations/claude-code/`);
> for Codex the wrapper + these instructions are the equivalent.
