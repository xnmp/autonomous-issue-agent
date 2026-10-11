# ADR 0002 — Money and accounting semantics

Status: Accepted
Date: 2026-07-30
Governs: src/issue_agent/domain/pricing.py src/issue_agent/adapters/accounting_runtime.py src/issue_agent/adapters/control_store.py

## Decision

Money is an integer number of currency micros. Token counters are disjoint and
carry source provenance. Charges declare their basis:
`provider_reported`, `configured_rate_card`, `estimate`, `reconciled`,
`synthetic`, or `learned_estimate`; quota is separate from money.

Every paid run reserves its quoted maximum transactionally before execution.
Repository, global, and issue limits are checked in the same SQLite transaction.
A returned leg always settles from usable receipts and cumulative usage at the
captured rate card, otherwise zero for an interrupted or unusable session.
A cumulative or incomplete contribution makes the charge
`basis=estimate`, with an accounting journal event naming the evidence gap.
The reserved maximum is a budget ceiling, never an estimate. Caps count settled
estimates just like other charges; the per-leg ceiling still bounds execution.
Live metering faults stop the owned provider group when usage cannot establish
that the leg is under its cap. This execution stop does not prevent terminal
estimated settlement from releasing the reservation.

For each Codex root/child session and disjoint token category, usage is the maximum
of observed cumulative counters and the sum of receipts deduplicated by response
identity, after excluding copied ancestor history and subtracting pre-launch
usage. Receipt arrival order cannot erase cumulative spend or charge it twice.
Each request contributes its usable provider-reported dollars, otherwise its tokens
priced at frozen rates. For each session and token category, the cumulative remainder
is `max(0, cumulative usage - sum of receipt tokens)`; reported dollars never erase
another request's charge. The live cap watchdog uses the same estimate. Corroborated native
children share the parent's reservation and retain their own model prices.
Usable USD monetary receipts do not require a token rate card. The legacy runner
captures its declared model/scalar prices as a frozen card for mixed receipts.
The observed usage carries that card through the normal return, so live, recovered
and normal settlement prices agree even when the caller has no card or newer rates.
The unreceipted remainder uses the highest applicable frozen rate for each token
category among the session's observed models. Cumulative lower bounds cannot
establish complete model boundaries or justify a cheaper allocation for live caps.
Sessions without either receipt or usage contribute zero and require a gap event.
Malformed
records do not discard other usable request evidence. A complete terminal JSON record
is usable without a trailing newline; only an unparseable tail is interrupted.
Rollout files are runtime
observations, never review verdict authority; harness-owned invocation coordinates
and locked rates bound their financial interpretation (ADR 0012).

Codex persists rollout coordinates, pre-run baselines and locked rates before
launch. Boot/wake recovery reads only the harness-owned invocation window and
settles terminal, lease-free legacy `accounting_unavailable` rows through the
normal transaction. Missing/unusable recovery evidence settles zero as an
estimate, with a journaled gap. Version-1 evidence prices usable parent usage at
its frozen scalar rates even without native-child metadata or a model name.
Repeated recovery neither adds another charge nor
rewrites an immutable return. Running work, live leases and reservations proven
never launched retain their existing recovery rules. Forced shutdown still reaps
provider process groups before settlement.

Claude's positive result dollars remain the legacy cumulative session estimate.
Its per-message input/cache counts can be deduplicated by `message_id`, but
per-message output counts are placeholders, result usage excludes subagents,
and background text forwarding is optional. Correcting resumed/forked Claude cost
(#287) requires whole-tree per-request evidence; this change does not claim that
correction. Missing Claude accounting nevertheless settles and releases capacity.
A normal return supersedes a recovery estimate, including zero recovery after a
PR-repair unreturned acknowledgment. Charge origin (`recovery_estimate` or
`normal_return`) is typed and persisted independently of pricing basis. A normal
return is final even when its pricing basis is `estimate`; stale normal or recovery
receipts cannot replace it. An acknowledged repair captures the real
output and corrects money atomically,
without reapplying attempt debt or overwriting newer workflow state. Ordinary
uncaptured acknowledgment cannot discard the late return.
The accounting correction journal preserves the prior receipt and amount; signed
`SpendRecorded` contributions reverse its prior basis and add the final charge.
SQLite retains one charge, and replay of the normal return is idempotent.
Gap metadata travels in the settlement command and its event commits with money
and output. Legacy commands explicitly decode an absent gap as empty, and omit
that empty field on encoding to preserve immutable receipt bytes.
Corrections without a normal return remain deferred; no correction hold or retry
mechanism is introduced.

Admission still fails closed on unavailable/corrupt SQLite authority and missing
required pre-launch pricing. Missing terminal accounting evidence cannot create
an accounting hold or a human-review escalation.

## Consequences

- Rate-card identity/version is historical evidence and cannot be rewritten.
- Lowering a cap blocks future admission; it does not alter settled history.
- JSONL spend output is a projection, never admission authority.
