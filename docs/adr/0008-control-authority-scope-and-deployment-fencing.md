# ADR 0008 — Control authority scope and deployment fencing

Status: Accepted
Date: 2026-08-01
Governs: src/issue_agent/adapters/control_path.py src/issue_agent/adapters/control_store.py src/issue_agent/adapters/single_instance.py src/issue_agent/adapters/accounting_runtime.py src/issue_agent/adapters/journal.py src/issue_agent/adapters/runtime_plan.py src/issue_agent/app/roles.py src/issue_agent/app/fix.py src/issue_agent/app/cli.py

## Context

Repository state directories can be customized and multiple checkouts can run
under one service account. Per-checkout databases, locks, or owner identities
would split spend caps and allow old and new deployments to mutate the same
remote target concurrently.

## Decision

The operating-system account has one canonical SQLite control authority at
`~/.local/state/issue-agent-control/control.db`, independent of repository
`STATE_DIR` paths. A discovered legacy deployment-local database is a startup
conflict, not an empty replacement authority. A missing canonical store is
recreated only where none was ever used; the files a prior store leaves beside
it (its `.initialize.lock`, `-wal`/`-shm`, `.retired-v*`, the previous
release's `.authority` witness) make its loss a startup refusal naming them,
so caps never silently reset (ADR 0003). The dedicated witness file was
retired with the 3-table store in slice 5d-2.

Repository names are canonicalized and every role/check-out has a stable
deployment owner identity, recorded on each leg and lease. Shutdown cancels
only its exact owner's legs; recovery is scoped by repository and role and
never touches a leg whose lease is live. Repository/role-global leases prevent
intentionally parallel checkouts from buying duplicate work.

A dispatch target is derived once from the observed world before the lease is
acquired. The reservation carries that identity through paid admission. A later PR read may validate that its linkage still agrees, but
cannot replace the reserved target; disagreement defers the dispatch. Claim
identity and spend scope remain distinct: an issue-linked PR uses its positive
issue number for both, while an issueless PR claims its negative PR number and
has no per-issue spend scope.

Retry identity answers a different question from claim and spend identity.
Review convergence is always keyed by negative PR number, so closing-link
changes cannot erase paid failed rounds. Fix and steward retry ceilings remain
issue-keyed because their work and lifecycle are issue-owned.

A supported checkout repoint carries an explicit bounded lineage, used to reap
the prior checkouts' process registries at boot. Owner adoption and retired-
owner fencing were deleted in slice 5d-2: the predecessor's unsettled legs are
recovered by the repository/role-scoped sweep, and remote writes need no
deployment barrier since slice 5d-1 (every write is idempotent by key or
read-back, so a stale deployment's late write is at worst a duplicate the next
read observes).

Single-instance locks prevent duplicate role schedulers for one deployment;
they are the only deployment fence (refactor decision Q17).

## Consequences

- Configuring a different repository state directory cannot create new spend
  headroom or a second spend authority.
- Repoint reaps only the recorded prior checkouts' processes; it is not
  permission to reap arbitrary sibling checkouts or repositories.
- A transient or changing PR-linkage read cannot move an admitted dispatch to
  a different claim or breaker key after decision.
- No SQLite transaction spans a remote call; idempotency keys and read-back
  handle remote ambiguity.
