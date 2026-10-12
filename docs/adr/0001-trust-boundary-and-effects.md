# ADR 0001 — Trust boundary and privileged effects

Status: Accepted
Date: 2026-07-30
Governs: src/issue_agent/adapters/effects.py src/issue_agent/adapters/operator_effects.py src/issue_agent/adapters/act.py src/issue_agent/domain/trust.py

## Decision

Agent prompts, repository text, CI output, and agent-written files are
untrusted claims. They are encoded through `domain/trust.py`; they never
directly authorize publication. GitHub mutations are represented as durable,
idempotency-keyed `LifecycleCommand`s and executed by the narrow daemon or
operator effects adapter. An unknown remote outcome is reconciled before retry.
Commands carry an exact repository and executor-role identity. The shared
control store exposes pending work only to that scope. Publication commands
also capture the prior remote revision; a remote value that is neither the
captured prior nor the proposed revision is divergence and never executes.

The shell governor and hooks were deleted in slice 6a (2026-10). Credential
placement, expected-revision checks, and effect receipts
are the authority. Until agents run without publication credentials, supported
repositories remain trusted-inbound only.

Slice 5b (2026-10-13) amends this for the fix role. It queues no
`LifecycleCommand`s: `adapters/act.py` executes its derived Actions directly.
Each write is naturally idempotent (a label set, a merge or update-branch
bound to the decided head, a PR opened only when the branch has no open one)
or claims an idempotency key in SQLite before writing and confirms it only
after a GitHub read-back. A failed read-back raises and the next tick
re-derives from GitHub. The agent pushes its own branch with git; the publish
broker is deleted (refactor decision Q11). The review role keeps durable
commands until slice 5c.

## Consequences

- Application orchestration cannot invoke gateway write methods directly.
- Agent-authored marker files are claims, not sufficient trusted evidence.
- Screenshot reads are recorded in a harness-owned, worktree-keyed state
  receipt; a `.harness/screenshots-read` file has no gate authority.
- A later separate effect-broker process can implement the same command port.

Screenshot-read receipts and visual-evidence bookkeeping were superseded by #327
(2026-10-12): the reviewer judges screenshot evidence; the 700KB image limit remains.
