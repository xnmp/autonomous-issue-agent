# ADR 0008 — Control authority scope and deployment fencing

Status: Accepted
Date: 2026-08-01
Governs: src/issue_agent/adapters/control_path.py src/issue_agent/adapters/control_store.py src/issue_agent/adapters/deployment_authority.py src/issue_agent/adapters/single_instance.py src/issue_agent/adapters/accounting_runtime.py src/issue_agent/adapters/journal.py src/issue_agent/adapters/runtime_plan.py src/issue_agent/app/roles.py src/issue_agent/app/fix.py src/issue_agent/app/cli.py

## Context

Repository state directories can be customized and multiple checkouts can run
under one service account. Per-checkout databases, locks, or owner identities
would split spend caps and allow old and new deployments to mutate the same
remote target concurrently.

## Decision

The operating-system account has one canonical SQLite control authority at
`~/.local/state/issue-agent-control/control.db`, independent of repository
`STATE_DIR` paths. A discovered legacy deployment-local database is a startup
conflict, not an empty replacement authority. The authority witness makes a
deleted or substituted database fail closed instead of silently recreating
zero state.

Repository names are canonicalized and every role/check-out has a stable
deployment owner identity. Commands, runs, leases, projection feeds, and
receipts are queried and mutated only within their exact ownership scope.
Repository/role-global dispatch and path claims prevent intentionally parallel
checkouts from buying duplicate or overlapping work.

A dispatch target is derived once from the observed world before the claim is
acquired. The reservation carries that identity through workflow routing and
paid admission. A later PR read may validate that its linkage still agrees, but
cannot replace the reserved target; disagreement defers the dispatch. Claim
identity and spend scope remain distinct: an issue-linked PR uses its positive
issue number for both, while an issueless PR claims its negative PR number and
has no per-issue spend scope.

Retry identity answers a different question from claim and spend identity.
Review convergence is always keyed by negative PR number, so closing-link
changes cannot erase paid failed rounds. Fix and steward retry ceilings remain
issue-keyed because their work and lifecycle are issue-owned.

A supported checkout repoint carries an explicit bounded lineage. Adoption
transfers its live durable ownership transactionally and permanently retires
the prior owners. Remote effects share a repository/role deployment barrier;
lineage retirement takes that barrier exclusively, so an old effect either
finishes before retirement or fails the subsequent owner fence.

Single-instance locks prevent duplicate role schedulers for one deployment,
but SQLite owner fences remain authoritative across reboot, missed process
discovery, and stale service definitions.

## Consequences

- Configuring a different repository state directory cannot create new spend
  headroom or a second command authority.
- Repoint is an explicit migration, not permission to adopt arbitrary sibling
  checkouts or repositories.
- A transient or changing PR-linkage read cannot move an admitted dispatch to
  a different claim or breaker key after decision.
- No SQLite transaction spans a remote call; deployment fencing orders the
  call against retirement while command receipts handle remote ambiguity.
