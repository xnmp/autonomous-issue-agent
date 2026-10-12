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
| Remote mutation | lifecycle command + effect receipt + expected revision | unknown outcome reconciles; stale revision is permanent failure |
| Run/process ownership | SQLite lease plus PID/PGID/start-identity receipts | boot recovery kills/reclaims before admission |
| Deduplication | namespaced SQLite keys/command idempotency | conflicts are loud; journal tails are never authority |
| Gate evidence | GitHub state, operator config | indeterminate is deny; worktree marker forgery has no authority |
| Telemetry/dashboard | schema-v2 JSONL and deterministic projection | may be incomplete; never changes control decisions |

All paid runtimes receive the durable `issue-agent-publish` command. Nothing
forces raw `git push`, `gh pr create`, or `gh pr edit` through it (residual
1.1); the broker records
`PUBLISH_BRANCH`/`OPEN_PULL_REQUEST`/`UPDATE_PULL_REQUEST_BODY` commands and
trusted receipts and reconciles executing/unknown commands on boot.
`UPDATE_PULL_REQUEST_BODY` replaces the body of the ONE pull request the
supervised run owns: the number is adjudicated against GitHub's open PRs for the
authorized branch onto the approved base, the body is a bounded literal argv
element completed with missing issue linkage and acceptance criteria before
command capture (#327), and a body that changed between capture and
execution refuses rather than overwrites. It carries no other PR mutation —
title, labels, reviewers, base, and review dismissal are outside broker
authority.
A registered supervisor ancestor
retains the compact authority bundle while provider and target-command
environments omit accounting, event, and publication authority.
Because the model still runs as
the daemon's OS account, filesystem or local-socket access may recover
publication authority despite environment scrubbing. With no command filter,
this keeps every runtime in the trusted-inbound posture.

The trigger-gated end state for untrusted inbound repositories removes
publication credentials from agent children and publishes verified artifacts
through a separately privileged effect process. Environment scrubbing alone is
not sufficient isolation because filesystem and local-socket access may recover
credentials.
