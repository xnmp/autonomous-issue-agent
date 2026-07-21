## Master Agent (on-demand query)

Unchanged in spirit from v1: a stateless `claude -p` you invoke to ask "what happened?" /
"what's stuck?". It reads GitHub + the state dirs + session transcripts fresh each time. Give
it read-only tools.

```bash
claude -p --permission-mode bypassPermissions \
  --tools 'Read,Bash(gh *),Bash(cat *),Bash(ls *)' --disallowed-tools 'Edit,Write' \
  --max-turns 20 \
  "Summarize agent activity in the last 24h for $REPO: issues moved to ai-done, open PRs and
their review state, ai-blocked issues, needs-human-review PRs, CI retries exhausted, and any
harness issues raised. For each blocker, say what's needed to unblock."
```

---

← Prev: [Architecture Steward](07-gardener-steward.md) · [Index](README.md) · Next: [Bootstrap, Labels, Ops, Properties](09-bootstrap-and-ops.md)  →
