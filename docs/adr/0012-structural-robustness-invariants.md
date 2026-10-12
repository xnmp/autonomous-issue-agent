# ADR 0012 — Structural robustness invariants (post-voyage)

Status: Accepted
Governs: src/issue_agent/domain/effect_retry.py src/issue_agent/app/cli.py

Decided 2026-08-01.
Evidence: the 2026-08-01 supervised keifu run (nine live defects; see
`docs/open/` handovers and issues #153/#154).

The run's failures reduce to four violated invariants. Each is adopted here
with an explicit *dosage* — what is built now, what is a standing rule, and
what observation would justify scaling up. The selection test applied
throughout: **prefer removing a write channel over adding a guard.** Removed
channels cannot false-positive; guards charge agents in blocked legs.

## 1. No silent forever (built: stall watchdog)

The stall watchdog (`domain/liveness.py` and the stall reporter) was deleted
in refactor slice 8a; the standing rule below remains.

Every incident shared a signature: a target stuck in a non-terminal state
with nothing bounding or reporting the stuckness (advisory-UNSTABLE merge
deadlock; visual-AC resolve dead-ended on receipts; uncounted review
deferrals). Per-path counters remain necessary, but the class is open-ended,
so stuckness is now ALSO measured at the state boundary:
`domain/liveness.py` + the fix daemon's stall reporter emit an escalating
`target_stalled` warning for any watched PR/issue whose observable signature
is unchanged past 30-minute thresholds. Telemetry only — a false positive
costs a journal line, never a blocked leg.

Standing rule for new code: any retry/defer/skip loop must carry a journal
event and a ceiling that terminates in a human-visible state. The watchdog is
the backstop for the paths this rule misses, not a substitute for it.

## 2. Single writer per mutable location (built: discard-check; rule: handoffs)

Slice 5b (2026-10-13) deleted the publish broker this section describes: the
agent pushes its own branch with git (refactor decision Q11). The handoff
rule stands; the broker's fence below is history.

The publish broker's leased force push is race-safe, not authorship-safe —
an agent rebase erased an operator commit on keifu #169 (#154). Built now:
the broker refuses a non-fast-forward publish whose new history lacks the
changes of any remote-only commit (`git cherry` patch-equivalence), with an
actionable merge-instead message. Content-preserving rebases pass.

Standing rule: PR branches are agent-owned while claimed; operator
interventions go through the reply/resume rail, not `git push`. The full
ownership-lease/handoff protocol is DEFERRED — trigger: the discard-check
refusing more than occasionally, or a second concurrent operator.

## 3. Verdict inputs must not be agent- or history-writable (built: tracked-marker distrust)

A committed `.harness/test-cmd` merged into the base and silently replaced
the trusted red-fail gate's test for every downstream worktree (keifu
#167→#156). Two channel removals now hold: diffs containing `.harness/`
paths are denied (both stacks), and BOTH stacks ignore any `.harness` marker
that is *tracked* in git — repo history is never a gate input, closing the
read against poison that predates the deny rule.

Deferred: relocating agent declarations out of the worktree entirely
requires a declaration channel agents can reach from inside their sandbox
(they cannot write STATE_DIR); design that with #153's receipt work.
Standing rule (the provenance ratchet): every NEW input consumed by a gate
or review verdict states its provenance — who can write it, in which trust
domain — and anything below harness-owned is either laundered through a
harness-side mint (pinned checkout, receipt) or treated as untrusted claim.
Existing inputs migrate when touched; no big-bang typing.

## 4. Capabilities are declared, not assumed (built: one flag + honest errors)

Trust services implemented as Claude-hook side effects silently do not exist
on other runtimes: Codex cannot mint screenshot-read receipts, so visual-AC
work dead-ended after paid legs. Built now:
`RuntimeCapabilities.mints_visual_receipts` and gate messages that state the
dead end and the way out (test-asserted ACs) instead of an unactionable
refusal. A capability manifest/routing framework is DEFERRED — trigger: a
third runtime, or a second capability gap.

## Rollback triggers

Each addition names what would remove it: the watchdog goes if its warnings
are ignored noise after a month; the discard-check goes if it blocks
legitimate agent flows the resume rail cannot absorb; tracked-marker
distrust has no rollback (it is a channel removal); the capability flag
dissolves into a manifest or stays a boolean forever, both fine.

Screenshot-read receipts and visual-evidence bookkeeping were superseded by #327
(2026-10-12): the reviewer judges screenshot evidence; the 700KB image limit remains.
