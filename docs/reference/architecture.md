# Architecture — how the harness works now

Current as of 2026-07-30. This supersedes the numbered `0N-*.md` specs in
`docs/archive/`, which describe the **original bash design**: the scripts they
document have been deleted. Only the shared `plugin/hooks/checks/` gate stack
remains because Codex uses it on the live Python path.

Operator instructions live in [HARNESS.md](../../HARNESS.md); how to point the
harness at a repo is in [CLAUDE.md](../../CLAUDE.md).

## The shape

Three role daemons — **fix**, **review**, **steward** — reconcile GitHub and
drive Claude, Codex, or GLM sessions. Each is assembled in `app/roles.py` from:

```
observe   gateway.build_world()      the GitHub slice this role needs
decide    a PURE function            world + config + state → list[Action]
execute   Executor.execute()         the ONE place writes happen
dispatch  AgentDispatcher            RunAgent → an actual paid agent session
control   SQLiteControlStore         leases, spend, dedup, commands, receipts
effects   GitHubEffectExecutor       narrow privileged mutation boundary
```

The layering is hexagonal and **machine-enforced** (import-linter contracts
in `make test`): `domain/` is pure — no I/O, no env, no clock — `adapters/` is
I/O, `app/` is wiring. New pure logic goes in `domain/`; new I/O goes behind an
existing adapter or an injected callable.

The steward optionally grooms open issue bodies (`STEWARD_GROOM=1`): it emits
advisory comments and the orthogonal `maybe-resolved` label for verified recent
merge matches or stale references, never a close action. Grooming is capped at
five candidates per changed tick and persists a title/body signature after a
successful delivery, preventing harness comments from self-amplifying spend.

## The five properties worth preserving

These are what adversarial review and live deployments identified as
load-bearing. A refactor that breaks one of them is a regression even if the
tests pass.

1. **Decisions are data.** `decide_*` returns `Action` tuples; nothing in
   `domain/` performs a write. This is why adding escalation alerting and the
   journal were one-place changes.
2. **One privileged write funnel.** Harness lifecycle mutations become durable
   `LifecycleCommand`s and are executed by `GitHubEffectExecutor` with an
   idempotency key, expected revision, and receipt. Paid agents publish branches
   and open PRs through `issue-agent-publish`; `AgentPublicationBroker` commits
   intent before the network call and reconciles remote state before retrying an
   ambiguous outcome. Credential isolation remains separately trigger-gated,
   so this is durability—not an untrusted-input security claim.
3. **Impure rules are higher-order.** `check_tests(test_cmd, run_tests, …)` is
   the template: pure policy, unit-tested, with ~3 lines of shell-out injected
   from an adapter.
4. **"I couldn't look" is not "nothing to do."** `GhUnavailable` and
   `OpenPrLookup`'s tri-state exist so an unreachable API can never be mistaken
   for an empty world. Per-item fetch isolation (`_safe_get_pr`/`_safe_get_issue`)
   drops one bad item, not the whole poll — but propagates systemic failures
   like rate limits, which would otherwise fake an empty world.
5. **Incident comments.** Comments here carry the failure they prevent, often
   with a PR number. They are the densest defence in the codebase. When you move
   code, move its comment verbatim.

## Required review guardrail — currently missing

The SDK reviewer is diff-only: its tool policy does not check out the PR branch
or run `TEST_CMD`. It therefore cannot independently reproduce the resolve
agent's reported result. On keifu PR #146, an independently executed review
exposed a confident PR-body claim about a test failure that did not exist while
confirming the code itself was correct. The current SDK path does not preserve
that protection.

Treat independent execution as a required guardrail for review-cost work, not as
a current capability. Before making review cheaper in a way that relies more on
the author's summary, either add independent branch execution or build an
equally independent contradiction check. Do not claim the guardrail is present
until a behavioral test enforces it (#36).

## Enforcement: authority plus two advisory stacks

The fixer is governed at two levels:

- **The advisory governor hook** (`adapters/governor.py` +
  `plugin/hooks/bash_governor.py`)
  inspects every Bash call the agent makes. Its parsing core
  (`governor_policy.py`) is *shared* between the Python and bash entry points,
  which is why that half provably cannot drift.
- **The gates** (`domain/gate.py` for the SDK path, `plugin/hooks/checks/*.sh`
  for codex mode) enforce the PR/push/commit contract: base branch, acceptance
  criteria, test runs, red-before-green, evidence images, ADR citations,
  lint/typecheck.

Those two gate implementations shared a *spec*, not code, and had already
drifted when the review checked. They are now pinned by
`tests/test_gate_parity.py`: 35 golden cases run through **both** stacks in real
scratch git repos, asserting identical verdicts. Three legitimate divergences
are asserted explicitly rather than dropped. **If you add a gate rule, add a
parity case** — a set-equality test over rule families will fail otherwise.

## Operational state, telemetry, and money

SQLite is authoritative for run/lease ownership, dedupe, lifecycle commands,
effect receipts, reservations, and charges. Recovery kills registered orphan
trees, reclaims dead leases, drains commands through reconcile-first effects,
settles killed Codex runs from persisted rollout coordinates and locked rates,
and makes genuinely unrecoverable spend visible before capacity is released.
At upgrade, current and rotated legacy ledgers are imported idempotently into
repository-daily, global-daily, and issue-lifetime scopes before paid
admission; malformed or unattributed rows require operator repair.
JSONL is bounded
schema-v2 telemetry and a projection feed; losing it cannot authorize a write,
deduplicate work, or admit spend.
Every lifecycle command is scoped to both repository and role identity in the
shared store. A daemon queries only its exact scope, so sibling repositories
and the fixer/reviewer/steward credentials cannot claim each other's work.

The dashboard is a read-only projection consumer. Its canonical operational
model is rebuilt by the pure `domain/projection.py::fold` over schema-v2
events; GitHub reads may enrich titles and URLs but never infer lifecycle
success or turn an unavailable observation into an empty world.
Per-role scheduler heartbeats supply liveness freshness when an unchanged world
correctly emits no new observation event.
Event streams retain eight rotated files plus the current file. Rotation writes
an open-level snapshot and re-emits the latest observation for every world
subject, so bounded retention and restart do not resurrect a cleared level,
discard an active one, or make an unchanged issue disappear.

Every paid run is bounded by layered breakers, because the failure mode
that matters is not "wrong answer" but "loop that bills forever":

- per-leg retry counters (`ci`, `review`, `rebase`, `resolve`) → escalate at cap
- `MAX_INFRA_DEATHS` / `MAX_UNPRODUCTIVE_REVIEW_ROUNDS` — a *separate, higher*
  ceiling for sessions that die before returning a result, which deliberately do
  not count as attempts
- `MAX_BUDGET_USD` per-leg spend cap — native on the Claude SDK; on codex the
  harness prices streamed rollout counters and kills the process group on
  crossing when `CODEX_ENFORCE_BUDGET=1` and both token prices are positive
- `AGENT_MAX_RUNTIME_SECONDS` wall cap, checked between events
- repository/global/issue caps over atomic SQLite reservations; an armed cap
  blocks admission if accounting is unavailable or the runtime is unpriceable

Counters decay on any leg that returns a result, so the ceilings bound
*consecutive* failure rather than accumulating over a healthy issue's lifetime.

`tests/app/test_convergence.py` is the regression guard for this whole class:
each scenario drives a real `Daemon` tick loop over the real workflow, dispatcher,
executor and store, and asserts a frozen world reaches a steady state without
buying sessions. **Any new dispatch leg or retry counter needs a row there.**

## Process supervision

Each deployment has a role/repository/checkout-scoped lock and a stable
deployment identity. Startup validates configuration, lock ownership, the
control-store schema and adapter construction before reporting readiness.
The scheduler heartbeat and systemd watchdog represent scheduler progress, not
merely process liveness.

Agent processes run in their own process groups. Both Codex and the Claude SDK
start through a state-owned registration wrapper that records the group leader
PID and start identity before executing paid work. Recovery signals a recorded
group while that exact leader identity exists. If the leader has exited, it
signals only reuse-safe identities currently observed in the recorded process
group; a reused leader PID invalidates the receipt and is never signalled.
Normal shutdown and boot recovery terminate and reap the complete tree before
leases are reclaimed.
`SIGTERM` moves the daemon through `RUNNING → DRAINING → STOPPED`: paid
admission stops first, active work receives a bounded cancellation window,
handover and receipts are persisted, the outbox is flushed, and remaining
groups are killed. Missing the shutdown deadline changes a clean request to a
transient exit; a second SIGTERM hard-exits transiently so boot recovery owns
the remaining receipts. Exit codes distinguish clean/operator stops, transient
failures, invalid configuration, duplicate instances, incompatible schemas,
and corrupt stores so service managers do not restart permanent failures.

Application planners split a provider-neutral `RunIntent` from the
adapter-layer `RuntimePlan`; SDK tool names, MCP configuration, environment,
worktree paths, and resume tokens cannot enter domain objects. Operator
configuration follows the same ownership rule: `HarnessConfig` field metadata
is the sole env/coercion/choice table, drives `from_env`, and generates
`config/agent.env.example`. The immutable config snapshot hash is recorded on
every reservation and lifecycle command.

## Where the detail lives

| Topic | File |
|---|---|
| Operating it | [HARNESS.md](../../HARNESS.md) |
| Pointing it at a repo, config layout | [CLAUDE.md](../../CLAUDE.md) |
| Codex runtime | [codex-runner.md](codex-runner.md) |
| Authority/capability matrix | [capability-authority-matrix.md](capability-authority-matrix.md) |
| Architectural decisions | [../adr/](../adr/) |
| 110 recorded failure modes | [failure-modes.md](failure-modes.md) |
| What's still open | [../open/residuals.md](../open/residuals.md) |
| How it got here | [../history/](../history/) |
