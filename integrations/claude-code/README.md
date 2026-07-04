# Claude Code integration

Make Claude Code coordinate with the dashboard automatically: every Bash
command, prompt and file edit shows up on the timeline, and `/observe` turns the
whole thing on.

Two pieces:

## 1. Hooks (automatic mirroring — the harness runs these, not the model)

Merge `settings.snippet.json` into `~/.claude/settings.json` (global) or a
project `.claude/settings.json`. Fix the path to your checkout.

- `PostToolUse` (Bash/Edit/Write) → posts commands to `/command`, edits as narration
- `UserPromptSubmit` → posts the prompt as a narration marker
- `Stop` → a "turn complete" marker

**Safe to install globally.** `hooks/claude_mirror.py` probes `/healthz` first
and exits silently (~0.15s) when the dashboard isn't running — zero effect on
sessions where you're not watching.

## 2. The `/observe` skill (the on-switch)

Copy `skills/observe/` into `~/.claude/skills/observe/`:

```bash
cp -r integrations/claude-code/skills/observe ~/.claude/skills/observe
```

Then in any Claude Code session, `/observe`:
- starts the dashboard if it's down,
- points you at http://127.0.0.1:8790 (and offers `./run.sh browser`),
- switches Claude into "narrate what I'm doing" mode.

From then on the hooks stream the mechanics and Claude adds the story. Stop with
`./run.sh down` (hooks auto-no-op afterwards).
