# Runtime capability and authority matrix

This is the deployment truth, not a security claim inferred from hooks.

| Runtime | Sandbox/tool surface | Resume | Budget evidence | Governor coverage | Agent publication credentials | Supported inbound trust |
|---|---|---|---|---|---|---|
| Claude | SDK tool allow/deny; worktree cwd; broad fixer Bash | yes; sandbox preserved | provider result/usage; native limit | in-process PreToolUse + gates | publication env keys are scrubbed from supervised commands; same-account home/filesystem access remains | trusted inbound only |
| Codex | `workspace-write`/`read-only` selection; child process group | supported, but resume cannot preserve every exec-only sandbox flag | rollout counters priced by configured card | state-owned absolute hook; fail-closed resolver + gates | publication env keys are scrubbed; operator HOME and same-account filesystem access remain | trusted inbound only |
| GLM | Claude SDK surface with a validated profile/environment | Claude semantics | provider result where supplied; otherwise explicit unknown | Claude hook path | publication env keys are scrubbed from supervised commands; same-account home/filesystem access remains | trusted inbound only |

| Concern | Authority | Failure posture |
|---|---|---|
| Paid admission | SQLite reservations and charges | unavailable/corrupt/unpriceable with an armed cap blocks new paid work |
| Remote mutation | lifecycle command + effect receipt + expected revision | unknown outcome reconciles; stale revision is permanent failure |
| Run/process ownership | SQLite lease plus PID/PGID/start-identity receipts | boot recovery kills/reclaims before admission |
| Deduplication | namespaced SQLite keys/command idempotency | conflicts are loud; journal tails are never authority |
| Gate evidence | trusted test execution receipt, harness-owned screenshot-read receipt, GitHub state, operator config | indeterminate is deny; worktree marker forgery has no authority |
| Telemetry/dashboard | schema-v2 JSONL and deterministic projection | may be incomplete; never changes control decisions |
| Shell governor | advisory classifier | unresolvable governed shapes deny, but authorization does not rely on parsing |

All paid runtimes receive the durable `issue-agent-publish` command. Raw
`git push` and `gh pr create` are denied on the supported hook paths; the broker
records `PUBLISH_BRANCH`/`OPEN_PULL_REQUEST` commands and trusted receipts and
reconciles executing/unknown commands on boot. Because the model still runs as
the daemon's OS account, filesystem or local-socket access may recover
publication authority despite environment scrubbing. Bypassing the advisory
hook is therefore still possible and keeps every runtime in the trusted-inbound
posture.

The trigger-gated end state for untrusted inbound repositories removes
publication credentials from agent children and publishes verified artifacts
through a separately privileged effect process. Environment scrubbing alone is
not sufficient isolation because filesystem and local-socket access may recover
credentials.
