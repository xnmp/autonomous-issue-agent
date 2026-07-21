## bootstrap.sh — new-repo setup

```bash
#!/usr/bin/env bash
set -uo pipefail
REPO="${REPO:?}"

# 1. Labels.
for l in ai-triage ai-ready ai-working ai-blocked ai-pr-open ai-done ai-suggestion \
         trivial minor major needs-human-review needs-design invariant P1 P2 P3 harness; do
  gh label create "$l" --repo "$REPO" 2>/dev/null || true
done

# 2. Gitignore the harness artifacts. evidence/ is NEVER committed (v1 3.5). Worktrees too.
{ echo ""; echo "# Agent harness"; echo ".claude/worktrees/"; echo "evidence/";
  echo ".harness/ac-posted"; echo ".harness/test-plan-posted"; echo ".harness/adversarial-verdict"; } >> .gitignore

# 3. ARCHITECTURE.md (read-only generation).
if [ ! -f ARCHITECTURE.md ]; then
  claude -p --permission-mode bypassPermissions --tools 'Read,Bash(git *),Bash(ls *),Bash(cat *)' \
    --disallowed-tools 'Edit,Write' --max-turns 30 \
    "Analyze this codebase and output an ARCHITECTURE.md: module map, key dependencies, data
flow, patterns in use, and boundaries that should not be crossed. Concise reference doc." \
    > ARCHITECTURE.md 2>/dev/null && echo "  ✓ ARCHITECTURE.md"
fi

# 4. Seed the steward's durable priors (committed). The steward proposes changes to these via
#    issues; humans approve. They start minimal and grow.
[ -f INVARIANTS.md ] || cat > INVARIANTS.md <<'EOF'
# Invariants

Checkable rules the fix and review agents MUST obey. Each is a one-line, falsifiable statement.
The steward proposes additions (label `invariant`); a human approves before they land here.

<!-- e.g. "All writes to the Order aggregate use version-checked updates; a bare UPDATE is a bug." -->
EOF
mkdir -p docs/adr
[ -f docs/adr/0000-template.md ] || cat > docs/adr/0000-template.md <<'EOF'
# NNNN. <title>
Status: Proposed | Accepted | Superseded
Context: <forces at play>
Decision: <what we chose>
Consequences: <trade-offs, what this rules out>
EOF

# 5. Verify the THREE bot identities are distinct and authenticated.
: "${FIX_BOT:?}"; : "${REVIEW_BOT:?}"; : "${STEWARD_BOT:?}"
[ "$FIX_BOT" != "$REVIEW_BOT" ] && [ "$FIX_BOT" != "$STEWARD_BOT" ] && [ "$REVIEW_BOT" != "$STEWARD_BOT" ] \
  || { echo "FIX_BOT, REVIEW_BOT, STEWARD_BOT must all differ"; exit 1; }

echo "✓ Bootstrap complete. Configure launchd/systemd units for the fix watcher, review watcher, and steward."
```

The v1 bootstrap's per-directory `CLAUDE.md` seeding is optional and can be added back; it's
orthogonal to the corrections here.

## Label Taxonomy

| Label | Meaning | Who sets it |
|-------|---------|-------------|
| `ai-triage` | Steward has triaged it: AC posted, or awaiting the author's reply to a blocking gap (`STEWARD_TRIAGE=1`) | Steward |
| `ai-ready` | Fixer should pick this up | Human, or the steward when `STEWARD_PROMOTE=1` (else steward only posts AC + `ai-triage`) |
| `ai-working` | Fixer is running | Watcher; **reaper resets if the fixer died** |
| `ai-blocked` | Needs human input | Fix scripts |
| `ai-pr-open` | PR open, awaiting review/CI | Fix scripts |
| `ai-done` | Merged to dev | cleanup.sh |
| `ai-suggestion` + `trivial`/`minor`/`major` | Steward output (never auto-promoted) | Steward |
| `needs-design` | Implies a new abstraction/boundary — needs an approved ADR first | Steward |
| `invariant` | Proposed addition to `INVARIANTS.md`/`AGENT.md` (human approves) | Steward |
| `needs-human-review` | Review agent escalated at round 5 | Review agent |
| `P1`/`P2`/`P3` | Priority (P1 first; unlabeled = P4) | Human |
| `harness` | Issue with the harness itself | Any agent |

Dropped from v1: `area/*` (no longer needed — single sequential fixer) and
`flaky-test-exemption` (the flaky path is simplified, see below).

Three identities: `fix-bot` (write code/PRs), `review-bot` (review only), `steward-bot`
(read + comment only). All three must be distinct logins.

## Operational Safeguards

**1. Concurrency.** Single sequential fixer + global run-lock + reaper (in `watcher.sh`). No
area labels, no cross-machine mutex. Conflict-free by construction because two PRs never build
concurrently from the same agent, and `cleanup.sh` re-queues any PR closed unmerged.

**2. Session bloat.** After N resumes, fork with history preserved using the **real** flag:
`claude -p --resume "$id" --fork-session …` (v1's "fork" actually started cold). Track a
per-issue `.resumes` counter in `lib.sh` and switch to `--fork-session` past the threshold.

**3. Flaky tests.** Simplified: if CI fails, retry once; if the same test also fails on the
`dev` branch's latest run, treat it as infra/flaky — comment, label `ai-blocked`, and open a
separate `fix` issue (human-promoted, not auto-`ai-ready`). No self-committing `.flaky-tests`
mutation by the agent (v1 wrote to it mid-run, polluting the diff).

**4. Cost & budget.** Every `claude` call carries `--max-budget-usd` (fixer `$MAX_BUDGET`,
reviewer `$REVIEW_BUDGET_USD`, steward `$STEWARD_BUDGET_USD`, adversarial `$ADVERSARIAL_BUDGET`).
The single-fixer model also bounds parallel spend to one fix session + one review at a time. The
steward wakes on a slow interval and reads compact file-derived context, so its standing cost is
low despite being a long-lived session (memory is in files, not an accumulating transcript).

**5. Security / trust boundary.**
- Three least-privilege GitHub App tokens (fix: write; review: review; steward: read+comment);
  never an org-admin PAT.
- All agents run with restricted `--tools` and `--disallowed-tools Edit,Write` where they
  shouldn't write (review, steward, master). The steward writes only to its own state dir and to
  issue comments — never to code or to `INVARIANTS.md`/`docs/adr/` (humans gate those).
- Issue/PR/comment text is always framed as **DATA** in prompts (see AGENT.md / REVIEW_PROMPT /
  STEWARD_PROMPT). This does not make prompt injection impossible, so: run the fixer in a sandbox
  with restricted network egress, and scope the token so a successful injection can't reach
  secrets or other repos. This is the residual risk to accept consciously, not paper over.

**6. Conceptual integrity (v3).** The steward is the homeostatic organ: it encodes invariants
(attacking same-model blind spots), watches drift metrics as a non-gating signal, mines recurring
review feedback into durable defaults, and curates docs. Without it the harness is a throughput
engine that accrues well-tested debt; with it, quality compounds. See
[Architecture Steward](07-gardener-steward.md). Pair it with a **different-model reviewer** for
decorrelated judgement.

## State Layout

```
~/.local/state/issue-agent/
  issue-<n>.session        fix session id
  issue-<n>.ci-retries     CI attempt counter
  issue-<n>.resumes        resume counter (→ --fork-session past threshold)
  locks/fixer.run          global single-fixer PID lock

~/.local/state/review-agent/
  pr-<n>.session  pr-<n>.last-sha  pr-<n>.round
  wt-pr-<n>/                throwaway checkout when REVIEWER_RUNS_TESTS=1

~/.local/state/steward-agent/        (the steward's MEMORY — read every iteration)
  journal.md               what it observed/decided/rejected, with rationale
  metrics.jsonl            drift time-series (the fitness instrument)
  feedback-clusters.json   mined recurring review feedback + counts

# Committed to the repo (the durable priors that steer fixer + reviewer):
docs/adr/NNNN-*.md         decision records (human-approved)
INVARIANTS.md              checkable rules the fixer + reviewer must read

~/.claude/projects/<hash>/<session-id>.jsonl   transcripts (managed by Claude Code)
```

Single-machine by default. The "separate machines" claim from v1 is dropped: `--resume` needs
the local transcript and the state dirs are local, so each agent's lifecycle is pinned to one
host. The fix and review *watchers* can still run on different machines (they share nothing but
GitHub), but a given issue's fix sessions stay on one machine.

## Properties (corrected)

- **Always headless**, GitHub as the only inter-agent bus.
- **Three identities** (`fix-bot` ≠ `review-bot` ≠ `steward-bot`) — required for approvals, for
  the agents to see each other, and to keep steward comments out of the fixer's reply logic.
- **Single sequential fixer** — conflict-free by construction; no fan-out, no area locks.
- **A steward owns conceptual integrity** — stateful, memory-in-files, encodes invariants,
  watches drift as signal-not-gate, compounds review feedback into durable defaults, curates docs.
- **Outcome from ground truth** — `.structured_output` + GitHub queries, never model prose.
- **Crash-safe orchestration** — no `set -e` death; isolated poll passes; supervised restart;
  a reaper that un-strands `ai-working`.
- **Non-circumventable gate** — tree-sitter governor blocks `--no-verify`/`core.hooksPath`
  bypass even inside compound/nested commands; PR gate re-derives truth from git, not prefixes.
- **Tests in CI, not the commit hook** — the hook stays fast (lint/typecheck/bypass-check).
- **Human owns promotion to prod** — everything here targets `dev`; merging `dev` → prod is a
  human decision outside this harness.
- **Plugin-shaped** — committed `.claude/{settings.json,hooks/,AGENT.md}` is the enforcement;
  orchestration scripts run on the operator's machine.

## Open risks to accept consciously

1. **Prompt injection** is mitigated (data-framing, restricted tools, scoped tokens, sandbox)
   but not eliminated. Anyone who can file an issue can attempt it.
2. **Two LLMs negotiating** can still converge prematurely or oscillate; the round cap +
   `--reviewer-runs-tests` + human escalation are the backstops, not a proof of independence.
3. **Shared-model blind spots.** Separate contexts decorrelate *information* (catching slips)
   but not *priors* — a false belief both fixer and reviewer hold survives a fresh context. The
   two real mitigations: the steward's `INVARIANTS.md` (moves the knowledge outside the model
   into a checkable rule) and a **different-model reviewer** (different training → smaller
   blind-spot intersection). A human reviewer remains the strongest and slots in unchanged.
4. **Steward latency.** The steward reacts on a slow cadence, so drift can land before it's
   noticed; the always-on `INVARIANTS.md`/ADR checks in the PR gate are the fast complement to
   the steward's slow holistic pass.
5. **GitHub rate limits** under heavy polling — batch calls, raise `POLL_SECONDS`, or move to
   webhooks if the 60s loop proves too chatty.

---

← Prev: [Master Agent](08-master-agent.md) · [Index](README.md)
