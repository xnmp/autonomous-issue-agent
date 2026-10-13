# Runtime capability and authority matrix

This is the deployment truth, not a security claim inferred from hooks.

| Runtime | Sandbox/tool surface | Resume | Budget evidence | Command governor | Agent publication credentials | Supported inbound trust |
|---|---|---|---|---|---|---|
| Claude | SDK tool allow/deny; worktree cwd; broad fixer Bash | yes; sandbox preserved | provider result/usage; native limit | none (deleted in slice 6a) | publication env keys are scrubbed from supervised commands; same-account home/filesystem access remains | trusted inbound only |
| Codex | `workspace-write`/`read-only` selection; child process group | supported, but resume cannot preserve every exec-only sandbox flag | rollout counters priced by configured card | none (deleted in slice 6a) | publication env keys are scrubbed; operator HOME and same-account filesystem access remain | trusted inbound only |
| GLM | Claude SDK surface with a validated profile/environment | Claude semantics | provider result where supplied; otherwise explicit unknown | none (deleted in slice 6a) | publication env keys are scrubbed from supervised commands; same-account home/filesystem access remains | trusted inbound only |

| Concern | Authority | Failure posture |
|---|---|---|
| Paid admission | SQLite reservations and charges | unavailable/corrupt/unpriceable with an armed cap blocks new paid work |
| Remote mutation | idempotent act layer: naturally idempotent write plus read-back, or a SQLite idempotency key claimed before the write and confirmed after read-back | unknown outcome is read back, never blindly repeated |
| Run/process ownership | SQLite lease plus PID/PGID/start-identity receipts | boot recovery kills/reclaims before admission |
| Deduplication | namespaced SQLite idempotency keys | conflicts are loud; journal tails are never authority |
| Gate evidence | GitHub state, operator config | indeterminate is deny; worktree marker forgery has no authority |
| Logs/dashboard | plain per-role JSONL log, SQLite legs/money tables, GitHub | may be incomplete; never changes control decisions |

Fix agents push their own branch with plain `git push` (refactor decision
Q11); the publish broker and `issue-agent-publish` were deleted in slice 5b.
The harness opens the PR from a pushed `fix/<issue>-` branch owed one (ahead of base, no open PR),
once, through `adapters/act.py`. Nothing stops an agent from running
`gh pr create` or `gh pr edit` itself (it holds the daemon account's
credentials); a PR the agent opened is simply observed and the harness opens
no second one.
A registered supervisor ancestor keeps the run's accounting identity while
provider and target-command environments omit it.
Because the model still runs as
the daemon's OS account, filesystem or local-socket access may recover
publication authority despite environment scrubbing. With no command filter,
this keeps every runtime in the trusted-inbound posture.

The trigger-gated end state for untrusted inbound repositories removes
publication credentials from agent children and publishes verified artifacts
through a separately privileged effect process. Environment scrubbing alone is
not sufficient isolation because filesystem and local-socket access may recover
credentials.
