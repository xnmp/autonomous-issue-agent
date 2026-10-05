# Retained-run import prerequisite

The approved Phase 5 preflight reproduced a deployment blocker on copied live
data: fix/steward activation refuses every unbound historical run before it can
publish per-target migration holds. The target has 297 fix runs and 165 steward
runs without modern workflow bindings. Clearing three accounting reservations
and one obsolete publication does not remove this blocker.

Implement only stopped-import attribution. Validate the existing run owner,
dispatch JSON and accounting repository identities before assigning a historical
run to an existing issue/PR key. Attribution supplies a sourced migration
blocker; it grants no allowance, reset, consumed-reply watermark or session.
Genuinely new targets initialize their secondary components at the existing
canonical first-observation boundary once the family epoch is activated.
Steward-first observation creates an issue shell; later resolve discovery
preserves its components. A blocked historical shell cannot mint fresh primary
authority. Unassignable or conflicting execution evidence still
refuses activation. Settled `legacy-import` accounting rows can be distinguished
from execution only through their exact existing import contract and linked
settlement evidence.

Conventions: trusted lifecycle state remains in the existing SQLite workflows
and typed `BlockedIssueRecovery`, `BlockedPRRepair` and `BlockedSteward`
components. Reuse `ContinuationImportBlocker`, plan digests, saved-plan replay,
deployment barriers and activation events. This is one bounded stopped import;
no schema, archive census, background worker, new queue, paid diagnostic or
runtime-history scan is needed. The parent owns shared types and publication
transaction changes; one Sol implementer handles the isolated parser and its
behavioral verification. No historical run receives a fabricated binding.

Acceptance requires copied-data activation with every affected target held,
observable fresh-target admission under the existing controls, correct PR/issue
separation, malformed/conflicting/oversize input refusal, unrelated-owner
isolation, and refusal if evidence changes before publication. Retain immutable
original recovery/source snapshots. Verify compatible-code rollback and repeat
idempotence. Independent review and a green full `make test` precede integration
or any live activation. No exceptional accounting release is authorized here.

Closed historical PRs without canonical primary authority retain a deferred
blocked decision in the existing durable deduplication receipts. The ordinary
activation-import table requires a workflow through its foreign key, so it
cannot store this pre-discovery decision. A fresh explicit
closed/merged observation is required; absence from an open list is insufficient.
No primary workflow is invented. Every later canonical creation or import,
including review rejection and execution import, retains the exact blocked
repair component atomically. Genuine observation preserves it across restart.
The original ambiguous-PR constraint
applies to all paid work: the shared review eligibility check and SQL reservation
boundary must refuse paid review while that sourced migration block remains.
This reuses the existing receipt store and component types rather than adding an
authority table or scanning historical runs during ordinary operation.

The final private-copy qualification on 2026-10-05 passed with all six family
epochs active. It preserved 1,103 runs, 1,103 reservations, 1,072 charges,
3,296 limits and 1,524 lifecycle commands; all 297 fix and 165 steward histories
remain held. Original saved fix/steward retries were inert after all-family
activation, and genuine read-only observation of PR #581 preserved its exact
hold across restart. Independent review accepted the current candidate and
194 focused tests passed with both type checks clean. The initial third
preflight began before the final guard centralization; its loaded source hashes
were not captured. Final postactivation qualification used current source and
retained its hashes. The complete `make test` gate passed on 2026-10-05 at
01:35:22 UTC in 15 minutes 30 seconds: 10,529 domain/adapter/app cases, 123 hook
checks, parity, four architecture contracts and both type checks. Dashboard API
coverage executed; all 22 frozen source/test hashes matched afterward.

The existing Makefile supports two-worker execution without changing coverage.
Prepare its shared environment serially with
`uv sync --extra dev --extra sdk --extra dash --frozen`, then run
`UV_NO_SYNC=1 make -j2 test` with `TMPDIR` unset and a writable
`XDG_RUNTIME_DIR`. Do not synchronize dependencies or edit source while the
parallel gate runs. The dashboard extra ensures endpoint cases execute; the
test-local optional import guard preserves the supported minimal installation.
The isolation fixture now publishes its prior activation through the public
API, with all original rollback and peer-admission assertions unchanged.
