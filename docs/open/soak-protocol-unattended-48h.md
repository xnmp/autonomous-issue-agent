# Unattended soak protocol — 48 hours, no manager

Status: approved 2026-08-02, not yet started. Start after the current keifu
batch drains and `gap-i-global-breaker` merges (clean baseline).

## Purpose

The north star is running without a manager. Supervised sessions cannot
measure that — the manager's presence changes what stops. The soak measures
the true residual dependence: run the harness for 48 hours with escalations
QUEUING and nobody answering, then read what actually halted and why.

## Protocol

1. **Baseline marker.** At soak start, note the dev commit, the open issue
   set, and the journal byte offsets (or just the wall-clock start) in this
   file. Seed the queue: promote everything triage marks ready at start; do
   not promote again until the soak ends.
2. **No-touch rule.** For 48 hours: no dashboard actions, no gh-side operator
   comments/labels/closes on the target repo, no config edits, no restarts
   (systemd may restart on crash — that is the harness's own supervision and
   counts as autonomous). Reading (dashboard, journals) is unrestricted.
   The 15-minute heartbeat monitor may run; it must only observe.
3. **Exception.** A genuine safety/cost emergency (runaway spend the caps do
   not stop, destructive behavior) ends the soak — record the reason; that
   ending IS a finding, the most important kind.
4. **End-of-soak readout**, from journals + the interventions metric
   (`metric-interventions` branch):
   - merges completed unattended; issue→merge wall time distribution;
   - every open gate / escalation at soak end: cause, age, and what a manager
     WOULD have done (classify: missing capability / missing policy dial /
     genuine judgment);
   - breaker trips (budget, rejection ceiling, infra) and whether recovery
     paths behaved as designed;
   - spend: total, per merged PR, and any spend that bought nothing
     (retries against a dead blocker are the soak's cost signature — bound?);
   - recurrence events.
5. **Adjudication.** The soak's product is the classified halt list. Each
   halt becomes: a gap fix (engineering), a dial to flip (policy), or a
   confirmed judgment gate (accepted human dependence). Re-soak after the
   fixes; the target trend is halts-per-48h falling session over session.

## Success criteria (first soak — calibrated to current state, revise upward)

- The harness survives 48h without a safety stop.
- ≥ 3 issues reach merge unattended.
- Every halt is legible: an open gate/escalation with an accurate cause —
  zero SILENT stalls (the ADR 0013 invariant; today's fixes claim this).
- Guardrails hold: first-pass approval rate and recurrence no worse than the
  supervised baseline; caps never exceeded.

## Non-goals

Throughput records; fixing anything mid-soak; judging the steward's product
decisions (triage questions queuing unanswered is EXPECTED — how gracefully
the pipeline idles behind them is what is being measured).
