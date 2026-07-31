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

Gate decisions are pure policy over explicit evidence. The Python and shell
gate implementations share a behavioral contract enforced by parity cases;
intentional capability differences are enumerated rather than silently
drifting. Indeterminate evidence denies a governed commit, push, or PR.

An operator `TEST_CMD` has precedence over repository-local declarations.
Tests run through the harness-owned execution boundary with recursion limits,
timeouts, process-group teardown, and a child environment that removes
deployment, accounting, event, publication, and test-policy controls. A target
command receives workload inputs, not authority to select its base, cap,
credentials, state database, or evidence verdict.

Screenshot reads and red/revert verification produce harness-owned typed
receipts bound to the worktree and invocation. Ordinary gate tests run through
the same trusted execution boundary and return a harness-owned verdict rather
than a durable typed receipt. Agent-created `.harness/` markers may request
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
