# ADR 0013 — Human gates are a ledger, not labels plus events

Governs: src/issue_agent/domain/dashboard.py

Status: Superseded by the minimal-harness refactor (slice 5d-1, 2026-10-13): the single stop is the `needs-human` label written by policy (docs/open/2026-10-12-minimal-harness-design.md).
Previously: proposed (2026-08-02). Motivating incidents: the 2026-08-02 keifu
supervised session, especially issue #25 / PR #173.

## Problem

"Blocked on a human" is currently *implied* state: an escalation event in the
journal plus an attention label on GitHub. Recovery is implied too — an
operator unblock removes the label and journals the removal. Nothing in the
system can answer the question that matters: **is the blocking cause still
true?**

That representation produced a live dead state. PR #173 tripped the rejection
ceiling; the operator unblocked; the next review re-tripped the ceiling; the
decide layer deduped the escalation (its state key already existed), so no
event, no label, no dispatch — and the dashboard showed a stale reason with
both clocks frozen. Six hours of silence, ended only by out-of-band surgery.

The same session produced the pattern's siblings:

- unblock did not execute the actions that would make the cause false
  (dismissing the counted rejections), so "unblocked" and "re-armed" diverged;
- escalations fired while a cheaper automatic remediation existed (phantom
  budget reservations, wedged CI runs, infra deaths counted as quality
  failures) — the escalation carried no record of what was attempted;
- every unblock needed a manual re-promote after it, because clearing the
  label and restoring eligibility are unrelated code paths.

The 2026-08-02 fixes (unblock-as-re-arm, at-ceiling self-healing,
reconcile-before-escalate, cancel-before-rerun, infra/quality counter split)
each close one instance inside the current representation. This ADR records
the structural change that closes the class.

## Decision (proposed shape)

SQLite — already the authority for admission, spend, leases, and lifecycle
commands — gains a `gates` table. An escalation *opens a gate row*; labels,
comments, and journal events become projections of it, never the authority.

A gate row carries:

- **cause**: a machine-checkable predicate name plus its evidence (e.g.
  `review_rejections_at_ceiling(pr=173, count=4, ceiling=4)`), not prose;
- **remediation attempted**: which automatic remediation ran before the gate
  opened, and its outcome. An escalation with no attempted remediation is
  invalid by construction unless the cause class declares none exists;
- **re-arm actions**: the durable commands that make the cause false
  (dismiss these review ids, reset these counters, cancel this run,
  re-apply this label), stored when the gate opens — when the knowledge is
  fresh — not reconstructed at unblock time;
- **generation**: monotonically bumped on every operator unblock, so
  re-escalation dedup keys on (cause, generation) and a re-trip after an
  unblock is a NEW gate, never a suppressed duplicate.

"Unblock" then means: execute the re-arm actions transactionally, re-evaluate
the cause predicate against the observed world, and close the gate only if the
predicate is now false — otherwise the gate stays open with the failure
recorded and visible. Eligibility restoration (today's separate re-promote) is
one of the re-arm actions, not a second manual step.

The dashboard reads gates directly: reason, evidence, what was attempted, what
unblock will do, and — because open/closed is explicit — the work/idle clocks
stop being reverse-engineered from journal folds.

## Invariants this buys

1. No silent dead states: an issue/PR that nothing will move MUST have an open
   gate row; a tick that observes "nothing to do, nothing dispatched, no open
   gate" for a held target is itself a defect signal.
2. Escalate only after remediate: the gate schema forces the attempted
   remediation to be named.
3. Unblock is re-arm: an operator click cannot produce the
   cleared-but-still-blocked divergence, because clearing IS executing.
4. Capability closure follows: every re-arm action stored in a gate must be a
   brokered harness operation, which surfaces capability holes (PR body edits,
   review dismissal) at design time instead of as 3am operator surgery.

## Migration

Incremental, per ADR 0012's "existing code migrates when touched": new
escalation sites open gates; the review-ceiling and budget paths (the two that
bit) migrate first; label-implied blocked states remain readable until their
sites are touched. No big-bang rewrite.

The deployment-wide infra breaker that first used this shape was deleted in
the 2026-10 subtractive refactor (slice 3): a machine failure never opens a
gate; it backs off and only the money caps bound it.

## Alternatives considered

- Keep labels+events, add more self-healing arms (the 2026-08-02 approach).
  Works per-instance; the class keeps reappearing because nothing forces new
  escalation sites to be recoverable.
- A state machine in the journal (event-sourced gates). The journal is
  telemetry/projection input by ADR; making it load-bearing for recovery
  inverts the authority decision already made for SQLite.
