## Fix Agent Plugin Structure

All enforcement is via Claude Code hooks — no GitHub Actions required for the *plugin's own*
gates (the repo's existing CI still runs and is polled separately).

```
.claude/
  settings.json            # PreToolUse hook → dispatcher (real schema)
  hooks/
    dispatch.sh            # thin shell entry: pipe stdin JSON to the parser
    bash_governor.py       # PEP 723 tree-sitter parser + router + bypass detection
    checks/
      check-branch-name.sh # governed: git checkout -b / switch -c
      check-commit.sh      # governed: git commit  (lint+typecheck, +bypass guard)
      check-pr.sh          # governed: gh pr create (full gate)
  AGENT.md                 # appended to system prompt
  agent/                   # orchestration (not committed enforcement, see note)
    lib.sh resolve-issue.sh resume-issue.sh fix-ci.sh address-review.sh cleanup.sh watcher.sh
  review/
    lib.sh review-pr.sh re-review-pr.sh review-watcher.sh
  steward.sh master.sh bootstrap.sh   # steward.sh = the stateful Architecture Steward (was gardener.sh)
```

### settings.json (verified schema)

The matcher matches the **tool name**. One hook fires on every `Bash` call; the dispatcher
decides whether the command is governed. This is the only schema that works — v1's
`{"Bash":{"pre":[...]}}` is silently ignored.

```json
{
  "permissions": {
    "allow": ["Bash", "Read", "Edit", "Write"]
  },
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          { "type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/dispatch.sh" }
        ]
      }
    ]
  }
}
```

`dispatch.sh` is a 2-line shim so the hosting repo can keep `bash_governor.py` self-contained:

```bash
#!/usr/bin/env bash
# Reads the PreToolUse JSON on stdin, hands it to the governor.
# Exit 0 = allow, exit 2 = block (stderr is shown to the agent).
exec uv run "$(dirname "$0")/bash_governor.py"
```

> **Note on enforcement vs orchestration.** The `hooks/` tree is the *enforcement* layer and
> ships inside the repo's committed `.claude/` so it governs the agent. The `agent/`,
> `review/`, steward and master scripts are *orchestration*; they run on the operator's
> machine and can live in the plugin or alongside it. Only `hooks/`, `AGENT.md`, and
> `settings.json` must be committed for the gates to bind. The steward's durable priors
> (`INVARIANTS.md`, `docs/adr/`) are also committed — they steer the fixer and reviewer.

---

← Prev: [Overview & Index](README.md) · [Index](README.md) · Next: [Hook Layer: tree-sitter Bash Governor](02-hook-governor.md)  →
