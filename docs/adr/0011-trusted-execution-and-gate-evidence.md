# ADR 0011 — Trusted execution and gate evidence

Status: Accepted
Date: 2026-08-01
Governs: src/issue_agent/domain/gate.py src/issue_agent/adapters/child_env.py src/issue_agent/adapters/codex_hook.py src/issue_agent/adapters/governor.py src/issue_agent/adapters/worktree.py plugin/hooks/checks/

## Context

Agents can edit their worktrees and influence repository-local commands. Hook
parsing is useful feedback but cannot make an agent-written marker, inherited
environment variable, or claimed test result authoritative.

## Decision

The shell governor is advisory. Publication authorization comes from durable
commands, expected revisions, trusted execution/readback, and effect receipts
under ADR 0001.

Gate decisions are pure policy over explicit evidence. Both advisory shell and SDK transports invoke the same
Python domain/governor decisions and existing Git facts/test executor. Shell
entry paths and the isolated interpreter are harness-owned; missing code or
required facts deny without a shell policy fallback. Behavioral and mutation
cases exercise the actual transports against independently specified verdicts.

Intentional shell capabilities remain explicit: commit and commit-order are
separate checks, an unavailable open-PR lookup refuses push, and historical
screenshot read receipts retain their legacy compatibility. SDK commit combines
order with other checks, can run available checks on an indeterminate PR lookup,
and can validate keyed screenshot receipts. Both preserve two-dot commit history
and merge-base diffs. The separate bin/point-at preflight/bootstrap and its
launcher environment scrub remain outside this policy consolidation. Indeterminate evidence denies a governed commit, push, or PR.

An operator `TEST_CMD` has precedence over repository-local declarations.
Tests run through the harness-owned execution boundary with recursion limits,
timeouts, process-group teardown, and a child environment that removes
deployment, accounting, event, publication, and test-policy controls. A target
command receives workload inputs, not authority to select its base, cap,
credentials, state database, or evidence verdict.

Screenshot reads and red/revert verification produce harness-owned typed
receipts bound to the worktree and invocation. Ordinary gate tests return a harness-owned verdict. Durable review approvals
require a separately persisted ordinary-test receipt for the exact rejection
and execution binding, head, patch, and frozen operator configuration. Only
operator `TEST_CMD` authorizes that execution; repository declarations cannot
authorize approval. Compatible rounds reuse completed successes and genuine
failures. Explicit `ALLOW_UNTESTED` records a separate operator waiver.

The nonpaid executor persists intent before launch and records structured
pre/post committed, index, and tracked-tree proofs plus typed execution
outcome. It awaits bounded supervised teardown before checkout cleanup.
Unavailable execution follows subject-specific evidence backoff and ceiling;
ordinary-test holds never fall through to trailer reads. At the ceiling the
existing execution-renewal action can retire the selected test hold and grant
one new execution episode after the old round settles, preserving other
holds and rejection allowance. Durable writers validate current stored
evidence and configuration before absent approvals and never run tests;
already-present effects reconcile first. Agent-created `.harness/` markers may request
verification or describe visual acceptance criteria, but cannot assert that a
read or test passed.

Environment scrubbing is capability reduction, not a security boundary against
code running as the daemon's operating-system account. Supported inbound
content remains trusted until process/credential separation is implemented.

## Consequences

- Every new gate rule needs domain tests and a parity case for both live stacks.
- Target tests cannot mutate harness control state through inherited authority.
- Hook bypass remains possible in the current same-account posture, so gates
  cannot replace durable effect or accounting controls.
