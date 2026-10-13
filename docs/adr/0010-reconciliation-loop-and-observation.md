# ADR 0010 — Reconciliation loop and observation

Status: Superseded by the minimal-harness refactor (slice 5d-1, 2026-10-13): both roles run one tick, observe -> derive -> policy -> act, in `app/tick.py` (docs/open/2026-10-12-minimal-harness-design.md).
Date: 2026-08-01
Governs: src/issue_agent/app/loop.py src/issue_agent/adapters/github.py

## Context

GitHub webhook delivery can be dropped, duplicated, or reordered, while full
world reads are expensive and network failures can resemble an empty world if
collapsed into ordinary values.

## Decision

The periodic observe → decide → execute loop is the convergence mechanism.
Webhook payloads never drive decisions; a delivery only wakes sleeping roles,
which then perform the same observation and reconciliation as a poll. The
periodic interval remains the resync backstop, so a lost delivery costs latency
rather than correctness.

The world probe is an optimization, not authority. It may skip a full read only
while a non-empty signature matches a successfully committed prior build and
the role-specific safety predicate says the signature covers the relevant
state. A TTL forces full reconciliation. Probe absence, malformed output, or a
blind world shape falls back to a full build.

Systemic GitHub unavailability skips the complete tick: nothing is observed,
decided, or executed, and the next poll is the retry. Per-item failures may be
isolated only when they cannot manufacture an empty global world. Spend gates
can block new paid dispatch while lifecycle replay, observation, and free
reconciliation continue.

Crash/suspend recovery is a separate fail-closed trigger policy under ADR
0006. Network fail-soft and quota admission do not become recovery arms.

Slice 5b (2026-10-13) deleted the webhook wake and the world probe: every
tick performs the full observation, and the poll interval is the only wake.
The remaining paragraphs stand.

## Consequences

- Webhook support improves latency without creating an event-processing state
  machine or delivery cursor authority.
- New probe signatures require a proof of what they cannot see and a bounded
  full-resync path.
- An unreachable API is never interpreted as “there is no work.”
