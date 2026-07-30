# ADR 0001 — Trust boundary and privileged effects

Status: Accepted
Date: 2026-07-30
Governs: src/issue_agent/adapters/effects.py src/issue_agent/domain/trust.py src/issue_agent/domain/gate.py plugin/hooks/

## Decision

Agent prompts, repository text, CI output, and agent-written files are
untrusted claims. They are encoded through `adapters/untrusted.py`; they never
directly authorize publication. GitHub mutations are represented as durable,
idempotency-keyed `LifecycleCommand`s and executed by the narrow effects
adapter. An unknown remote outcome is reconciled before retry.
Commands carry an exact repository and executor-role identity. The shared
control store exposes pending work only to that scope. Publication commands
also capture the prior remote revision; a remote value that is neither the
captured prior nor the proposed revision is divergence and never executes.

The shell governor and hooks are advisory defence-in-depth. Credential
placement, expected-revision checks, trusted test receipts, and effect receipts
are the authority. Until agents run without publication credentials, supported
repositories remain trusted-inbound only.

## Consequences

- Application orchestration cannot invoke gateway write methods directly.
- Agent-authored marker files are claims, not sufficient trusted evidence.
- Screenshot reads are recorded in a harness-owned, worktree-keyed state
  receipt; a `.harness/screenshots-read` file has no gate authority.
- A later separate effect-broker process can implement the same command port.
