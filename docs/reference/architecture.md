# Architecture — how the harness works now

Current as of 2026-10-13. This supersedes the numbered `0N-*.md` specs in
`docs/archive/`, which describe the **original bash design**: the scripts they
document have been deleted. Slice 6a (2026-10) deleted the remaining
`plugin/hooks/` gate stack, the governor and the fixer's publication gates.

The [minimal-harness design](../open/2026-10-12-minimal-harness-design.md)
governs the current shape. Each tick, both roles derive every item's state from
GitHub plus local leg facts; nothing persists a selected continuation. The
phase-4 roadmap and [ADR 0016](../archive/adr/0016-phase-4-current-state-authority.md)
continuation authority are superseded (slices 5c and 5d-1 deleted the workflow
model, the durable-command path and the continuation writers).

Operator instructions live in [HARNESS.md](../../HARNESS.md); how to point the
harness at a repo is in [CLAUDE.md](../../CLAUDE.md).

## The shape

Two role daemons — **fix** and **review** — reconcile GitHub and
drive Claude or Codex sessions. Both assemble through one path,
`tick.compose_role` (called by `fix.compose_fix_daemon` and
`review.compose_review_daemon`), from:

```
observe   gateway.build_world()      one bounded GitHub observation
derive    state.derive (PURE)        world + local SQLite facts → per-item state
policy    policy.actions (PURE)      states → list[Action]; each role keeps its own
act       GitHubAct.execute()        the ONE place writes happen (adapters/act.py)
legs      FixLegs / ReviewLegs       RunAgent → an actual paid agent session
control   SQLiteControlStore         legs (money + attempts), leases, idempotency keys
```

The layering is hexagonal and **machine-enforced** (import-linter contracts
in `make test`): `domain/` is pure — no I/O, no env, no clock — `adapters/` is
I/O, `app/` is wiring. New pure logic goes in `domain/`; new I/O goes behind an
existing adapter or an injected callable.
The fix role runs one configured model and effort (refactor decision Q12);
there is no difficulty router or per-tier provider selection.
The Pyrefly seam gate structurally assigns every production adapter to each
domain port it implements. An AST coverage contract requires every newly
declared Protocol to join that checked set or an explicit documented exemption,
so the global annotation-backlog suppressions cannot hide a new port drift.
Pyrefly checks the explicit `src` and `tests` inputs without applying its default
hidden-directory exclusions or consulting ambient Git ignore files: issue
checkouts live below the hidden `.claude/worktrees` scratch path and that path is
also excluded by the primary checkout's shared `.git/info/exclude`, but their type
gate must be identical to the primary checkout's.

The idempotent act layer (read-back writes and SQLite idempotency keys,
property 2) and SQLite ownership are the publication authority.

## The five properties worth preserving

These are what adversarial review and live deployments identified as
load-bearing. A refactor that breaks one of them is a regression even if the
tests pass.

1. **Decisions are data.** `derive` and `policy` return states and `Action`
   tuples; nothing in
   `domain/` performs a write. This is why adding escalation alerting and the
   journal were one-place changes.
2. **One idempotent write layer.** Every GitHub mutation goes through
   `adapters/act.py`: each write is naturally idempotent and read back (a label
   set, a merge or update-branch bound to the decided head, a PR opened only
   when its branch has none), or claims an idempotency key in SQLite before
   writing and confirms it only after a GitHub read-back. There is no command
   queue or effect receipt (slice 5d-1; ADR 0001 amendment). Operator writes
   (`adapters/operator_effects.py`) are label moves through the same
   `act.relabel` plus the CLI's bootstrap steps; the dashboard changes labels
   only (refactor decision Q19: park adds `needs-human`, unblock removes it and
   changes nothing else, each on a fresh read that refuses a closed issue). No SQLite transaction spans network
   I/O. Fix agents push their own branch with git; the harness opens the PR
   from it, once. Credential isolation remains separately trigger-gated, so
   this is durability—not an untrusted-input security claim.
3. **Impure rules are higher-order.** `check_tests(test_cmd, run_tests, …)` is
   the template: pure policy, unit-tested, with ~3 lines of shell-out injected
   from an adapter.
4. **"I couldn't look" is not "nothing to do."** `GhUnavailable` and
   `OpenPrLookup`'s tri-state exist so an unreachable API can never be mistaken
   for an empty world. An incomplete observation skips the item or the tick
   and propagates systemic failures like rate limits, which would otherwise
   fake an empty world.
5. **Incident comments.** Comments here carry the failure they prevent, often
   with a PR number. They are the densest defence in the codebase. When you move
   code, move its comment verbatim.

## Review quality

The reviewer submits its model verdict for the exact PR head. Targets must have CI
checks; a PR with no reported checks does not merge. A diff touching `.github/**` escalates
through the existing human-review path. Reviewer legs do not execute target tests.

## No fixer gates

The fixer has no publication gates and no command governor (slice 6a; ADR 0011
superseded). TDD is prompt guidance in `plugin/AGENT.md`. What a fixer pushes
is judged by CI and the independent reviewer.

## Operational state, telemetry, and money

SQLite is authoritative for money, leases, legs (attempts) and idempotency
keys, in three tables (`adapters/control_store.py`, `PRAGMA user_version` 1;
no migration history and no digest pins):

- `legs` — one row per leg, paid or refused: identity (repository, role, kind,
  issue, item number, head, owner, runtime), lifecycle
  `state ∈ reserved | running | ended | released` with `reserved_at`,
  `started_at`, `ended_at` and `end_reason`, the money (reserved maximum,
  charged micros, basis, origin, usage, rate card, `provider_receipt_id`
  UNIQUE, `settled_at`) and the leg's result (`leg_class`, session, result
  JSON). `leg_class` is the raw end class the leg returned with: a cut-off
  (a per-leg cap or a provider-ended turn) is stored `machine`. Whether a leg
  is an attempt is derived on read (`tick.leg_end`, `domain/attempt.py`, ADR
  0003), so SQL that counts `leg_class` is not an attempt count. CHECK constraints make contradictions unrepresentable: an `ended`
  leg has a charge and a `released` or unsettled leg has none, a charge's
  fields are all set or all null, a `reserved` leg has not started, and an
  ended or released leg has an end time. An unsettled leg with `ended_at`
  set is ended work awaiting spend recovery.
- `leases` — one claim per `(repository, role, target)`: owner, the bound
  leg (set by `reserve` in the reservation's transaction), and the holding
  process (pid, pgid, start ticks). The claiming daemon holds it until the
  leg's child registers its own process group. A lease is live while its
  bound leg is unsettled and not ended, or while its process is alive. A
  role releasing its claim leaves a running leg's lease in place, so a leg
  killed at shutdown is settled by boot recovery after the reap, from usage
  that may only then be readable, never at a premature zero. A bound leg that
  never launched is released with the claim, so a failed release on the
  runtime's error path cannot hold its reservation until restart.
- `idempotency` — keyed writes (`fix-comment:{repository}:{key}`), claimed
  before the effect and confirmed after its read-back.

Caps are sums over `legs` by scope dimension: a repository's day, every
repository's day (`global`), or one issue's lifetime (`issue:<n>`), counting
settled charges and the reserved maximum of unsettled legs. A missing store
is recreated only where none was ever used (ADR 0003). Attempts and
machine backoff derive from ended legs (`leg_runs`); ties on `ended_at`
resolve in recorded order. Recovery kills registered orphan trees, reclaims
dead leases of the daemon's repository and role (so a repointed checkout
recovers its predecessor's legs), settles killed Codex runs from persisted
rollout coordinates and locked rates, and settles genuinely unrecoverable
ended work at a zero estimate before capacity is released.

The first open of a pre-cutover database (one with the legacy
`schema_migrations` table) performs a one-way cutover. It refuses with
`CutoverRefused` (exit `OPERATOR_REPAIR_REQUIRED`) while the legacy store
holds a reserved/starting/running run, an active reservation or a lease,
naming each and its owner. Otherwise, under `BEGIN EXCLUSIVE` with rollback
journaling, it writes a new file beside the old one carrying every settled
charge as a `role='carried'` ended leg (repository, issue, reservation time,
charge, basis, rate card, receipt) and the `fix-comment` idempotency keys,
verifies the carried count and sum and that every charge each legacy cap
scope counted is still counted by that scope, hard-links the old file to
`control.db.retired-v<N>-<UTC stamp>` (never read again) and atomically
renames the new file into place. A crash leaves either the old file intact
(the next boot repeats the cutover) or the new file complete. Carried legs
count toward daily and per-issue lifetime caps; they are not attempts, so
derive restarts attempt counts from GitHub. A downgrade is refused: older
binaries do not recognise the new schema.

Completed legs, Claude and Codex alike, settle their per-model token usage at the
captured rate card; a runtime's own dollar figure is never charged (#389).
Interrupted legs price streamed usage at the same card, otherwise zero.
Partial evidence produces `basis=estimate`; malformed counters, or tokens spent
by a model the card cannot price, settle at the reserved maximum as an estimate.
Live work retains its reservation and stops its process group on a metering
fault that prevents cap enforcement. See [ADR 0002](../adr/0002-money-and-accounting.md)
for runtime provenance, Claude's resumed-session usage rules, and deferred late
corrections.
The coordinator (`app/recovery.py`) walks each stale reservation in one fixed
order: a live lease preserves it; otherwise surviving request evidence settles
it, else an ended leg settles at a zero estimate, else a never-launched hold is
released. SQLite remains responsible for atomically proving that a requested
release is still eligible.
Lease liveness is probed (OS process inspection) outside SQLite write
transactions; the write transaction revalidates the complete lease row, so a
lease replaced meanwhile survives. The pre-cutover migration and canary
records (`control-store-migration-evidence.json`,
`live-release-canary-evidence.json`) describe the retired v8–v46 schema and
are historical.
JSONL is the per-role daemon log only; losing it cannot authorize a write,
deduplicate work, or admit spend. Metrics derive from the SQLite legs/money
tables plus GitHub.
Every paid leg records its repository, role, and stable checkout deployment
owner in the shared store. Leases are repository/role-global, so two
intentionally allowed checkouts cannot both admit the same target (targets are
the only conflict; Q13 deleted the path-overlap holds). Shutdown cancels only
the daemon's exact owner's legs; recovery is scoped by repository and role and
never touches a leg whose lease is live.
The observed-world target used to acquire a lease is carried through
paid admission. A fresh PR read may validate that target
but cannot replace it; changed linkage defers to a new observation. An issueless
PR is claimed by negative PR number without manufacturing a per-issue spend
scope (ADR 0008).
The review role (slice 5c) owes a REVIEW leg when CI is green on a harness PR's
head and the review bot has no standing verdict on that head, a change request
standing only if it is no older than the PR body's last edit (GraphQL
`lastEditedAt`): a fixer may answer a review in the description alone, since a
commit would stale the CI evidence it cites (#472). An approval stands through
body edits.
The leg reads the PR body and its edit time in one read, and the
exact head: a detached checkout with the base fetched, where the agent runs
`git diff <base>...<head>` itself (an empty diff is reviewed as a claim that the
base already resolves the issue). It
is briefed with the steward's acceptance criteria as the bar (the issue body
when there are none; never the fixer-written PR description), the issue and its
human comments (bounded), the PR text, the conversation and its own prior review: the Codex reviewer's read-only sandbox
has no network, so it cannot fetch them. `GitHubAct`
submits the parsed verdict as one review on that commit, read back first
(a standing bot verdict on the head, no older than the body edit it judged,
means it landed), so a crash after
submission never posts a second review. The verdict is also kept on the leg's
result: a verdict whose submission never landed is resubmitted without
buying the review again. An unparseable verdict is an attempt, and derive caps
attempts per head; everything else the review produces (labels, stop comments,
merge) is the fix role's.

Offline family activation and legacy import were deleted (slice 5a). A family
epoch's plan evidence remains authority; a target's family state is initialized
only by its live first observation.

An overlay can be repointed (`issue-agent start --write-config`). Before the
overlay changes, the CLI stops and verifies all foreground and service-owned
processes carrying its stable name; failure preserves the old snapshot. For a
new checkout of the same repository, repository/role-scoped recovery settles the
predecessor's unsettled legs. After a repoint to another repository, the old
repository's unsettled legs stay reserved in its own scopes, never blocking the
new one, until a deployment of that repository recovers them.
Process-group and launch-intent receipts use the stable checkout deployment
identity beneath each role state root. Shutdown reaps only that deployment's
children. Boot reaps its own registry, then every other registry beneath the
deployment's state roots (a repointed predecessor, the sibling role, the former
unscoped registry), signalling only groups whose recorded launcher is dead; a
receipt without a launch intent falls back to its leader's parent, and a live
one keeps it.

The control database is rooted at the operating-system service account's
`~/.local/state/issue-agent-control/control.db`, independently of every
repository's `STATE_DIR` values. Custom repository state roots therefore cannot
split the host-global reservation pool. Startup fails closed if it finds the
former deployment-local database at a custom root; that authority must be
migrated into the canonical database before the deployment can restart.
CLI status and dashboard usage endpoints project current-period settled charges
directly from this SQLite authority (read-only; a missing or unreadable store is
reported unavailable, never zero).

The dashboard is a read-only consumer. Its board is built from each role's
plain `journal.jsonl` log, SQLite settled spend, and GitHub reads; GitHub reads
never infer lifecycle success or turn an unavailable observation into an empty
world. If process discovery is unavailable, dashboard liveness is `unknown`;
recent activity is never promoted into proof that a role is alive or dead.

Every paid run is bounded by layered breakers, because the failure mode
that matters is not "wrong answer" but "loop that bills forever":

- machine failures (provider overload or quota, OOM, a killed, cancelled or
  unreturned process, a runtime that never started, a refusal before
  admission) are never attempts and never escalate. The next fix dispatch of
  the same kind for the same issue waits `min(60s * 2^(n-1), 1h)` after the
  last machine end, `n` consecutive machine ends since the last normal return
  (`domain/backoff.py`). The input is the run table plus each run's
  `fix-infra-outcome` receipt; a run ended without a normal-return receipt is a
  machine end. Nothing is stored for it, and only the money caps bound the
  retries. Pre-admission refusals mint no run, so they retry each poll at $0.
- harness gaps (`domain/gaps.py`, `policy.gap_actions`): a fix leg's
  `harness_issue` report (bounded into its receipt and the journal, never
  published) and a live item's act whose machine-end streak reached
  `STALL_STREAK` (teardowns and provider windows aside) become a keyed
  `ReportGap` of harness facts into the harness's own repository. Act files
  it when the token has no open issue of that title (gap kind and target)
  there and otherwise comments, once per item per gap, at most
  `GAP_REPORTS_PER_DAY` new keys per UTC day (`claim_key_within`); a
  confirmed key reaches policy as a local fact (`Local.reported`) and is never
  asked for again. Judgment stops are never filed; only a human's `ai-ready`
  queues a gap issue (ADR 0001).
- derived attempt caps (`domain/state.py`): consecutive agent legs of one act
  without progress, REVIEW counted per head, and bot change-request rounds,
  each stopping at `needs-human`; nothing is stored for them.
- evidence before dispatch (tauri #1078): a FIX_CI or ADDRESS_REVIEW action
  carries what it repairs, read from the observation, and neither can be
  built empty: FIX_CI the failed checks (name, Actions job and run when it is
  one), whose logs the agent reads itself from the job-logs endpoint (which,
  unlike `gh run view --log-failed`, serves a finished job of a running run);
  ADDRESS_REVIEW the change request (reviewer, head, review id, body), whose
  inline comments the agent reads itself with `gh api`. No agent is told
  "CI failed" with nothing to read. A head's failure is handed to one FIX_CI
  agent end (its receipt records the failure keys, which name when each
  failure's CI run began: an Actions run attempt's start; for another app's
  check or a status, which have no run start, its own start or post, which
  over-counts); an attempt that was cut off (a per-leg cap or the provider,
  `domain/attempt.py`) did not finish, so it was handed nothing. A FIX_CI try is started by a leg on a new head, or handed a
  failure from a CI run that began after the head's first leg began and
  that no earlier leg was handed (a push, re-run or dispatch it or anyone
  caused), or with no readable keys, or after a capped leg on the head; a failure of a run that existed
  before, or was already handed, resumes the session at its tier, and
  ATTEMPT_CAP bounds every new run. The same evidence never dispatches: once the failing runs are
  complete with every failure handed, the harness re-runs the failed jobs
  once per head (free, read back by run attempt; a run past its first
  attempt was re-run already), and after that the item stops at
  `needs-human` (ATTEMPTS); the reply that releases it re-arms the evidence.
  No timer guards this; derive reads its own writes. A PR-stage leg's
  receipt records `end_head`, the branch's commit on the remote as the
  harness read it (`git ls-remote` from its own checkout) after the leg;
  while GitHub still shows the head the newest leg pushed away from, the
  item waits (CI_UNKNOWN) until it does or a human resets. The CI
  observation drops check runs that are jobs of an earlier attempt of their
  run (`actions/runs/<run>/jobs?filter=all`) and reads them as pending while
  the latest attempt has no jobs or unfinished ones, so it needs Actions
  read on the observing token. A refused harness re-run repeats one log line
  per poll, as a refused update-branch does, until a push or a human
  (residuals §2).
- a red base is not charged to PRs (#313) and is fixed first (#402, ADR
  0017): one base-CI issue per workflow per red streak, from the base's
  completed push runs (`policy.base_ci_actions`); a required workflow's
  (`ci.base_fix`: its check run on the tip carries a required context) is
  readied, ordered first and resolved without STEWARD, TRIAGE or a blocker,
  and its PR is charged the base's failures. Nothing is paused or stored.
- `MAX_BUDGET_USD` per-leg spend cap — native on the Claude SDK; on codex the
  harness prices the root rollout counters with the captured rate card and
  kills the process group on crossing
- `AGENT_MAX_RUNTIME_SECONDS` wall cap, checked between events
- per-leg caps and no-result returns share one rule
  (`domain/attempt.py`, #441): the receipt records the cut-off end as a
  machine end with its session; provider failures instead remain machine ends
  under the existing machine backoff. Read in order with the item's earlier legs,
  a first cut-off is a machine end and the next leg resumes that session at
  the same tier; a cut-off of that resume is an attempt, and the next leg
  runs one tier up on the same evidence. STEWARD, TRIAGE and REVIEW never
  resume, so their cut-off is an attempt at once. Derive, the fix role's
  resume choice and the dashboard all read this rule through `tick.leg_end`. A resume is bought
  while the issue cap has positive headroom. Admission reserves and enforces
  the lesser of the leg maximum and each armed cap's remaining headroom; a
  budget hit spends at least that reserved maximum.
- repository/global/issue caps over atomic SQLite reservations; an armed cap
  blocks admission if accounting is unavailable or the runtime is unpriceable

Both roles' legs are issue-scoped: the review role reviews only harness PRs,
whose branch names their issue. A missing issue remains a fail-closed
attribution error.

Provider-billed runtimes settle their reported cost. Subscription runtimes
whose provider reports zero use the configured versioned rate card as an
explicit synthetic token meter, so sequential Claude work cannot release
its reservation back to a nominally armed cap at zero. If the final usage
envelope is missing, an armed cap conservatively prices the observed stream
accumulator; if neither trustworthy usage nor pricing authority exists, the
full reservation remains active instead of becoming new headroom.

Codex runs with native sub-agents disabled in the isolated profile
(`[features] multi_agent = false`), so a leg's spend is its root rollout's
cumulative `token_count`, minus a resumed session's pre-launch baseline, priced
at the rate card captured before launch. The same reading drives the live
budget kill and terminal settlement; missing rollout usage settles an
estimate of zero for the interrupted request, never the reserved maximum.
Recovery prices the root counter up to `runs.completed_at`, so a later leg's
resumed usage cannot enter the ended run; unusable evidence settles a terminal
estimate. Repeated recovery keeps one charge.
JSONL remains evidence for authoritative SQLite settlement.
The publishing user task explicitly authorizes step 8's bounded fresh reviewers,
scoped verifiers and required evidence capture within this same leg's budget.

A Claude leg that declares `blocked` while a delegated agent task it started is still
non-terminal (tracked from SDK `task_started` / `task_notification` /
`task_updated`) cannot ever receive that task's notification. The runner
reclassifies it as an infra-class `BACKGROUND_CHILD_PENDING` error instead of a
human-input block; resume parks the issue back to `ai-ready`, and the ordinary
machine-failure backoff spaces repeats. Only `blocked` is rewritten.

The machine backoff restarts on any leg that returns normally, so it spaces
*consecutive* machine failures rather than accumulating over an issue's lifetime.

`tests/app/test_convergence.py` is the regression guard for this whole class:
each scenario drives a real `Daemon` tick loop over the real workflow, dispatcher,
executor and store, and asserts a frozen world reaches a steady state without
buying sessions. **Any new dispatch leg or retry counter needs a row there.**

## Process supervision

The accounting run ID is also the process-supervision ID. Before an agent
command executes, its child wrapper verifies the launcher's PID/start identity,
persists PID/PGID/start-ticks in the SQLite `leases` table, and verifies the
launcher again. A launcher death before registration refuses `exec`; a death
after registration is recoverable through the same authoritative run record.
A leg is one launch: a running leg's process lease is never replaced, and a
gateway failure ends the leg (the item's backoff retries in a new leg with its
own reservation).

Each deployment has a role/repository/checkout-scoped lock and a stable
deployment identity. Locks live in a private per-user host runtime directory,
not a configurable role state root, so two overlays aimed at one checkout
cannot acquire independent locks. Startup validates configuration, lock
ownership, the control-store schema and adapter construction before reporting
readiness. Schema bootstrap is serialized across role processes with a
per-database advisory lock. Recovery snapshots leases and performs OS liveness
probes outside SQLite write transactions, then revalidates the complete lease
identity before reclaiming it.
The scheduler heartbeat and systemd watchdog represent scheduler progress, not
merely process liveness. GitHub calls are individually bounded below the
watchdog deadline and report progress between calls; action execution reports
between actions, so a degraded but advancing tick is not mistaken for a hung
scheduler.

Agent processes run in their own process groups. Both Codex and the Claude SDK
start through a state-owned registration wrapper that records the group leader
PID and start identity before executing paid work. Recovery signals a recorded
group while that exact leader identity exists. If the leader has exited, it
signals only reuse-safe identities currently observed in the recorded process
group; a reused leader PID invalidates the receipt and is never signalled.
Normal shutdown and boot recovery terminate and reap the complete tree before
leases are reclaimed.
Recovery trigger policy is an immutable domain mapping consumed by the
application coordinator. Boot performs an unconditional current-deployment
reap plus lease, worktree, command, and immediate reservation reconciliation;
suspend-wake uses only the ownership-checked abandoned-process reaper and the
configured reservation grace; periodic resync reclaims dead leases and sweeps
stale reservations without touching processes, worktrees, or commands. Legacy and
current-deployment process registries are reaped in
the boot trigger's pre-adapter phase. The remaining current-state
reconciliation runs explicitly after graph construction and legacy-spend
migration but before readiness; a missing production trigger hook fails
startup. Network fail-soft behavior and operation-local reconcile-first effects
are not recovery triggers.
`SIGTERM` moves the daemon through `RUNNING → DRAINING → STOPPED`: paid
admission stops first, active work receives a bounded cancellation window,
receipts are persisted, and remaining
groups are killed. Before cancellation, owned active runs receive a durable
cancellation intent. Forced teardown then reconciles measured rollout usage; a
still-indeterminate paid run retains its reservation and evidence for boot
recovery instead of manufacturing a zero charge or releasing unknown capacity.
No handover file is written. A leg cancelled at shutdown records no
receipt and no session, so the next leg starts a fresh session in the
preserved worktree and branch, which hold its work.
Missing the shutdown deadline changes a clean request to a transient exit. Exit codes
distinguish clean/operator stops, transient
failures, invalid configuration, duplicate instances, incompatible schemas,
and corrupt stores so service managers do not restart permanent failures.

Application planners split a provider-neutral `RunIntent` from the
adapter-layer `RuntimePlan`; SDK tool names, MCP configuration, environment,
worktree paths, and resume tokens cannot enter domain objects. Operator
configuration follows the same ownership rule: `HarnessConfig` field metadata
is the sole env/coercion/choice table, drives `from_env`, and generates
`config/agent.env.example`. The immutable config snapshot hash is recorded on
every reservation and run. Knobs marked `live` (routes, dispatch concurrency,
the issue and daily caps, the issue pickup) are re-read each tick from the
same composed env file and swapped in as a new immutable snapshot
(`app/live_config.py`, ADR 0007 amendment); each leg's receipt names the
snapshot (`config`) it was admitted under.

Untrusted repository, issue, review, and CI text crosses into planners as
`domain/trust.py::Untrusted`; raw strings are rejected at the fence boundary.
Target `AGENTS.md` and `CLAUDE.md` files share one byte ceiling and one encoded
untrusted block appended only to the user task for both providers. Codex
project-document loading and Claude project setting sources are disabled, so
target files never enter developer/system authority.
Child agent processes receive an explicit environment allowlist plus the
selected provider credential. Supported branch/PR publication goes through the
durable broker, whose authority is recovered from the immutable supervised
ancestor and matched to the live SQLite run/lease instead of trusted from
child-controlled assignments. The registered process-group leader remains as
an authority-carrier ancestor while the provider runs in its child; accounting,
event, and publication variables are removed before the provider or target test
starts; no `ISSUE_AGENT_*` variable reaches the provider. The authority is carried as a compact bundle so the same
ancestor check works through `/proc` on Linux and `ps` on macOS; its
GitHub host is captured by the daemon, and both push remotes and `gh --repo`
scope are pinned to that host rather than child-controlled `GH_HOST`.
Since #413 the filesystem is the credential boundary: every leg runs under
bubblewrap with a private HOME, a private PID namespace and only the mounts
`domain/sandbox.py` plans, so neither the harness's `config/` nor the
operator's `gh`/SSH credentials nor `/proc/<daemon>/environ` is reachable. A
fix leg holds exactly `FIX_BOT_TOKEN` in its environment; a review leg holds
none. Residual exposure is in `docs/open/residuals.md` §1.1.

Visual screenshot evidence is judged by the independent reviewer (#327); image
size remains capped at 700KB. The harness opens a PR with the agent's own
description unchanged; the reviewer's bar is the steward's criteria (or the
issue). The issue's human comments are briefed as unauthenticated context: a
narrowing counts only when the reviewer can tie it to one, and a description
cannot lower the bar. Agent children still inherit direct
publication capability, so a direct `gh pr create` is possible in the current
advisory posture.
Service upgrades likewise stage complete
unit/plist-plus-private-config pairs; both systemd and launchd restore the
previous pair if activation fails. Partial-role service upgrades are rejected:
all unsuffixed, target-basename, repository-derived, and current-name legacy
writers stop before any role imports or admits against the SQLite authority.
Deployment names have one canonical lowercase representation; the CLI and
both installers reject lossy aliases before service paths are resolved.
Systemd and launchd key definitions and private snapshots by that stable name,
while treating `TARGET_DIR` as the mutable component of a same-repository
repoint. A repoint therefore replaces one three-role set atomically; launchd
also stops and removes checksum-era plists with the same embedded identity,
restores them on failure, and includes them in scoped uninstall. Both
uninstallers stop the complete resolved role set before deleting any
definition; a stop failure preserves every recoverable unit/plist and returns
nonzero.
Foreground and installed processes also carry an explicit deployment identity;
operator stop/status discovery filters by that identity and never signals an
unrelated deployment's role daemon.

## Module manifest

This list is intentionally checked against the filesystem. Adding a top-level
module requires placing it in the appropriate tier and explaining it here.

`domain/` — pure values and policy:
`domain/actions.py`, `domain/agent_task.py`, `domain/ci.py`,
`domain/architecture.py` (history-derived architecture passes through ordinary issues; ADR 0017),
`domain/checks.py` (the repository's required-check policy and the typed CI
observation/reduction: `Clear | Waiting | Failed`, or `Unavailable`; declared
policies use the test-merge revision when it has checks, otherwise the head.
Empty policies retain head report selection and escalate unverified green
after same-head cancellation until the free rerun is spent. Unavailable reads
leave durable escalations owed; settled Clear obsoletes other CI escalations),
`domain/contracts.py`, `domain/dashboard.py`,
`domain/effort.py`,
`domain/evidence.py` (what a repair is handed: FIX_CI its CI failures, keyed
by check run so each failure of a head goes to one repair; ADDRESS_REVIEW its
change request; neither constructible empty),
`domain/identity.py`,
`domain/backoff.py` (per-item machine-failure backoff derived from ended
legs),
`domain/gaps.py` (harness gaps the harness files about itself: an agent's
bounded `harness_issue` report, kept on the host, and how a report names its
target),
`domain/labels.py`, `domain/leg.py`,
`domain/attempt.py` (whether a cut-off leg is an attempt, and how the next leg starts),
`domain/outcome.py`, `domain/ports.py`, `domain/pricing.py`,
`domain/run_usage.py` (immutable measured/unavailable per-model run usage),
`domain/refs.py`, `domain/repository.py`,
`domain/recovery.py`,
`domain/routing.py` (difficulty tiers, the derived per-attempt escalation and
the `runtime:model` route spec), `domain/triage.py` (the STEWARD and TRIAGE acts' results,
parsed and laundered from the agent's claim),
`domain/sandbox.py` (the leg mount plan: what a paid leg sees under
bubblewrap, #413),
`domain/state.py` and `domain/policy.py` (the pure derive → actions core of the
minimal-harness design; the fix role runs on it since slice 5b and the review
role since slice 5c),
`domain/review.py` (a REVIEW leg's verdict: the agent's claim, parsed into an
exact APPROVE or REQUEST_CHANGES with a bounded body, or nothing),
`domain/supervision.py`,
`domain/trust.py`, and
`domain/world.py`, and `domain/deployment.py` (canonical deployment names).

`adapters/` — filesystem, process, provider, and remote-I/O implementations:
`adapters/accounting_runtime.py`, `adapters/act.py` (both roles' `act` step:
idempotent or keyed-and-read-back GitHub writes, including the review bot's
one review per head, no command queue),
`adapters/agent_runner.py`, `adapters/app_token.py`, `adapters/atomic_file.py`,
`adapters/child_env.py`,
`adapters/config_files.py` (settings file I/O for CLI and dashboard: one
assignment edited in place, process-safe serialization, `compose-env.sh`,
publish to service snapshots; ADR 0007 amendment),
`adapters/codex_home.py`, `adapters/codex_runner.py` (root-rollout usage, priced
once per leg; native sub-agents disabled in the isolated profile),
`adapters/control_path.py`, `adapters/control_store.py`,
`adapters/credentials.py` (credstore secrets delivered by `ImportCredential=` fill the three secrets at config load; fails closed),
`adapters/env_file.py`,
`adapters/github.py`,
`adapters/github_ci.py` (reads branch protection and rulesets into the required-check inventory),
`adapters/github_identity.py` (validates account type and login before authorship grants trust; ADR 0001),
`adapters/heartbeat.py`, `adapters/journal.py`,
`adapters/leg_sandbox.py` (renders the mount plan as a `bwrap` launch, carries
a fix leg's one GitHub token in its environment),
`adapters/operator_effects.py`, `adapters/process_supervisor.py`,
`adapters/reload.py`,
`adapters/repository_guidance.py`,
`adapters/runtime_plan.py`, `adapters/runtime_switch.py` (one role's
per-runtime journal/accounting chains behind one runtime, so legs route
across Claude and Codex), `adapters/signal_log.py`, `adapters/single_instance.py`,
`adapters/spend_recovery.py`,
`adapters/supervised_exec.py`, `adapters/systemd_notify.py`,
and `adapters/worktree.py`. Dashboard-specific read
adapters live under `adapters/dashweb/`.

`app/` — composition and use-case orchestration:
`app/cli.py`, `app/config.py`, `app/dashboard.py`,
`app/doctor.py` (offline doctor checks: one credential per role, the codex
binary the daemons exec, the installed services' PATH and snapshot),
`app/fix.py` (the fix role: one paid leg per dispatch, resuming the right
session),
`app/identity.py` (each role's GitHub credential, verified against its
configured login at boot; no credential refuses to start, ADR 0001),
`app/launchd_main.py`,
`app/live_config.py` (the live knobs' per-tick snapshot: hash-gated re-read,
validated, previous kept on refusal),
`app/main.py`,
`app/recovery.py`, `app/review.py` (the review role: keeps only REVIEW
dispatches, runs one leg pinned to the head, and submits the parsed verdict
once, resubmitting a stored verdict free when its submission never landed),
`app/settings.py` (the shared live-settings transaction, with explicit shared
or deployment scope; both CLI and dashboard use it, ADR 0007),
`app/tick.py` (the daemon both roles run: local facts from SQLite,
derive → policy → choose, the action executor, bounded paid dispatch, the
single `run_leg` path, the SIGTERM drain, and `compose_role`: the one
composition both roles share — control store, recovery coordinator,
accounting and journal runtimes, runtime binding, shutdown hooks).
Prompt assembly for the five acts is isolated in `app/planners/`; the web
composition/read-model boundary is in `app/dashweb/`.

## Where the detail lives

| Topic | File |
|---|---|
| Operating it | [HARNESS.md](../../HARNESS.md) |
| Pointing it at a repo, config layout | [CLAUDE.md](../../CLAUDE.md) |
| Codex runtime | [codex-runner.md](codex-runner.md) |
| Authority/capability matrix | [capability-authority-matrix.md](capability-authority-matrix.md) |
| Capacity boundary and benchmark | [capacity.md](capacity.md) |
| Prompt-size measurement | [prompt-budget.md](prompt-budget.md) |
| Control-store migration evidence | [control-store-migration-evidence.json](control-store-migration-evidence.json) |
| Live release canary evidence | [live-release-canary-evidence.json](live-release-canary-evidence.json) |
| Architectural decisions | [../adr/README.md](../adr/README.md) |
| 110 recorded failure modes | [failure-modes.md](failure-modes.md) |
| What's still open | [../open/residuals.md](../open/residuals.md) |
| How it got here | [../history/](../history/) |
