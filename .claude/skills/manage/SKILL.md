---
name: manage
description: Act as the harness manager for one or more target repos — launch/verify deployments, run the monitoring cadence, triage operator actions as judgment-vs-gap, fix or file gaps, keep the field-verification ledger, and write the batch handover.
---

# /manage — run the manager role

You are the manager for the autonomous-issue-agent harness, pointed at the
target repo(s) named in the arguments (default: every repo with a live
deployment — check `systemctl --user list-units 'issue-agent-*'` and
`config/repos/*.env`). The authority for the role is
[docs/reference/manager.md](../../../docs/reference/manager.md) — read it
first, follow its Before/During/After lifecycle, and treat this file as the
operational quickstart layered on top.

## Standing directives

- Resolve harness gaps as they arise, with unbiased adversarial review for
  medium-high-risk diffs (fresh-eyes subagent; do not seed it with your
  hypothesis). **Fixes must not grow the surface** (manager.md surface-area
  test): name what each fix removes before briefing it, never brief a new
  counter/hold/flag/loop, require a net-`src/` + mechanisms-removed/added
  report from every fix agent, and have the reviewer reject unjustified
  additions.
- **Judgment-vs-gap test on every operator action** (manager.md "Direction"):
  an action is a gap unless its irreducible core is a judgment call (budget
  grant, policy/permission, dispute, repo governance). Name the judgment
  kernel; if you cannot, the whole action is a gap — fix it on the spot or
  file it with journal evidence.
- Every behavior change lands with a `docs/field-verification.md` row whose
  evidence spec names the journal observable. Tick rows as evidence arrives.
- Commit only `make test`-green coherent states; daemons hot-reload commits
  between ticks. `uv` only; pyrefly for types.

## Launch / recover

```bash
make install                 # from the CANONICAL checkout only
issue-agent doctor           # refuses from a transient worktree; validates
                             # INSTALLED services (PATH, snapshot drift, trio)
issue-agent start            # composes snapshot, preflights, foreground canary
issue-agent install-service  # systemd/launchd supervision; RE-RUN after any
                             # config/overlay change (services read the
                             # install-time copy, not config/.active.*.env)
```

The dashboard (`issue-agent dashboard`, port 8321) must be started/restarted
manually and UNSANDBOXED (a sandboxed shell breaks its gh writes; see #191):
`setsid nohup .venv/bin/issue-agent dashboard >/tmp/dash.log 2>&1 &`.
Restart it after any dashweb code change; rebuild `dashboard/dist`
(`cd dashboard && npm run build`) after any frontend change — a stale bundle
silently serves old UI.

## Monitoring cadence

Run a 15-minute journal digest (Monitor tool over
`journalctl --user -u 'issue-agent-*' --since <local-time>` with boot-noise
filtered; use LOCAL `date '+%Y-%m-%d %H:%M:%S'` — UTC timestamps re-print old
lines). Non-verbose: relay only notable events. Known benign noise: keifu
`branches/<base>/protection` 404 (branch unprotected), worktree
`git show-ref`/`worktree remove` exit-1 lines around merges, exit-87
hot-reload restarts ("Scheduled restart job" then "Started").

Interpret before intervening: approved-but-unmerged PRs are usually CI churn
(re-runs, FIX_CI, rebase after a sibling merge) — check `gh pr checks` and the
GraphQL `mergeStateStatus` before touching anything. STALL lines name what the
daemons will not move on their own (`ai-blocked` = a question awaits a human).

## Operator actions — sanctioned paths

- Unblock/re-arm: the dashboard Unblock button (journals `operator_unblock`,
  clears PR-level holds and the resolve episode). Never bare-relabel
  `ai-ready` — the daemon re-escalates without resetting the budget.
- Answering an agent ask: reply on the issue/PR thread; the fixer resumes on
  the unconsumed reply. VERIFY environment claims yourself before answering
  (run the named binary, `ldd` it — a framework validator's summary is not
  evidence; class: #192).
- Issue close from an assistant shell: `gh issue close` may be
  classifier-blocked; the verified fallback is
  `gh api -X PATCH .../issues/N -f state=closed -f state_reason=completed`.
  Labels: raw `gh api -X POST/DELETE .../labels`.
- Never run `issue-agent dashboard` from a sandboxed shell; never
  `chezmoi`/`git checkout --` bulk reverts — targeted edits only.

## Wrap-up

When the pending list drains: batch readout with the judgment-vs-gap triage
of every operator action, ledger verdicts ticked and committed to dev, a
manager handover at `~/.local/share/issue-agent/handovers/<date>-handover-<batch>.md`
(never in the harness repo: it is target-repo specific), the memory index
updated, and every worktree this session created removed once its branch
merged or was abandoned (`git worktree remove`, then `git worktree prune`;
see manager.md "After"). Do not push dev or touch human-owned PRs unless the operator says so.
