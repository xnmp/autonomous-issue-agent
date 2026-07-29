# Handover — post-batch-3 stewardship (2026-07-29, ~18:00–22:10Z)

Batch 3 itself is CLOSED — authoritative record in
[2026-07-29-handover-self-hosting-batch-3.md](2026-07-29-handover-self-hosting-batch-3.md)
(6/6 merged, readout in §§8–10). This doc covers what a third manager session
did AFTER the batch drained, so the next session starts from reality, not from
the batch-3 end state (two things have changed since: daemons are UP, and the
reviewer is now CODEX).

## 1. Current state (22:06Z)

- **Daemons: UP (6 processes)** — relaunched ~18:30Z via background
  `make point-at REPO=self` after the launcher bash segfaulted at 18:25Z.
  The batch-3 handover's "daemons down, stopped clean" is superseded: the
  "clean stop" actually coredumped mid-trap. Queue empty (`ai-ready`: none);
  steward grooming continues on its cadence.
- **The reviewer is now codex/terra, not Opus** — #97 landed
  `reviewer_runtime` default `"codex"` (`config.py:349`), no env file pins
  `REVIEWER_RUNTIME`, and the relaunched review daemon logs
  `reviewer=codex review_model=gpt-5.6-terra mode=single review_budget=$8.00`.
  Every batch-3 review round was Opus; the NEXT review will be codex unless
  the operator sets `REVIEWER_RUNTIME=claude` in `config/repos/self.env`
  (+ recompose). Scout note: on codex there is NO code-scout —
  `REVIEW_MODE=single` is enforced (config validation), delegated mode and
  `REVIEW_SCOUT_MODEL` are claude-runtime-only. Decide the posture before the
  next batch; the first codex review round is a watch-list item either way.
- **dev is 4 commits past the batch**: a4f7c24 + ab95f08 (manager.md duties,
  below), d88fb96 (operator PR #105, dashboard state/tables/quotas — merged
  on operator request 22:05Z), plus this handover. **dev→main promotion
  pending, operator-only.**
- `MAX_BUDGET_USD="80"` workaround still live in self.env — restore `"20"`
  when #89 merges (comment in the file marks it; #89 still open).

## 2. Segfault root cause (issue #108)

Three identical coredumps on 2026-07-29 — `bash bin/point-at.sh self`,
SIGSEGV in `push_source` (bash+0x51c7f), at 07:08Z / 10:57Z / 18:25Z; the
18:25Z stack shows `run_pending_traps` beneath it. The long-running launcher
bash corrupts its input stack under hours of trap traffic — NOT fixer
activity, and not load (earlyoom shows 88% mem free). Daemons die with the
launcher (same process group). #108 has the class-fix ladder: prefer
`install-service` (units exist) → move the watch loop out of bash →
minimum `setsid` per role. Consider recommending systemd units to the
operator for the next deployment; also worth an upstream Arch/bash report.

## 3. Manager-manual changes landed this session (commit to dev, batch over)

- **New batch-end duty** (a4f7c24): when the pending list drains, write the
  manager handover and commit it directly to dev in one docs-only commit —
  uncommitted handovers die with sessions (batch 3 proved it).
- **Disambiguation** (ab95f08): manager handover
  (`docs/open/<date>-handover-*.md`, committed) vs the fixer's exhausted-leg
  handover (`.harness/handover.md`, worktree scratch, never committed).

## 4. Issues filed this session (all with journal/coredump evidence)

- **#102** ROUTE events lack the routing decision (probe model journaled
  instead) — blocks recurrence-by-routed-tier.
- **#103** ledger rows lack pricing basis — pre-#89 codex rows (~6–7×
  inflated) will be indistinguishable after the fix; stamp rates/source.
- **#104** budget kills count as agent strikes — #69's park was
  infra-caused; make escalation exit_reason-aware (links #82, #98).
- **#108** launcher bash segfault (see §2).
- **#109** fixer subagents inherit the routed model — sol does exploration
  and evidence capture; mirror `REVIEW_SCOUT_MODEL` with fixer-side knobs
  (SDK plumbing already supports per-subagent models; codex can't be routed
  — document, don't fake). Operator-requested.
- Plus a live-evidence comment on **#98** (the 15:04Z→15:12Z silent re-arm
  bounce on #69, with the state-dependence observation).

## 5. Loose ends (ordered)

1. **Reviewer-runtime decision** (§1) — before anything else dispatches.
2. dev→main promotion (operator).
3. Close #46, #67 (operator-only; classifier blocks assistant closes).
4. Restore `MAX_BUDGET_USD="20"` when #89 merges.
5. Owed live evidence: #82 wind-down (deliberate low-cap leg), #71
   resume-a-killed-session half, #79/#84 per->64KB-line stderr (524KB-total
   near-miss noted in the ledger row), #62 ceiling=true firing.
6. Next-batch spine candidates: #89 (top), #92, #98, #102–#104, #108, #109.
7. Two-manager hygiene: batch 3 had three manager sessions and one real
   collision (#69) — check `CronList`-equivalents and `ps` for a rival
   babysitter before intervening; #11 (manager heartbeat/lock) is the fix.
