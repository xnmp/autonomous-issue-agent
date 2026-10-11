# Architecture — how the harness works now

Current as of 2026-10-06. This supersedes the numbered `0N-*.md` specs in
`docs/archive/`, which describe the **original bash design**: the scripts they
document have been deleted. Only the shared `plugin/hooks/checks/` gate stack
remains because Codex uses it on the live Python path.

The [phase-4 simplification roadmap](../open/2026-09-30-phase-4-simplification-roadmap.md)
and [ADR 0016](../adr/0016-phase-4-current-state-authority.md) govern selected
continuation authority. Current SQL state and a bounded unresolved suffix
replace archive certification, lifetime verification and selected Store
continuity writers. A2 whole-system acceptance and Phase 6's declared policy/
independent-execution contracts are complete. Phase 5's offline contracts and
live cutover are verified; actual unattended windows and readout remain pending.
Current evidence is in the [execution record](../open/2026-10-05-phase-5-live-execution.md).
Jira's explicitly retained legacy counter path is the sole
runtime exception; its mirror context remains advisory.

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
effects   effect adapters            narrow privileged mutation boundaries
```

The layering is hexagonal and **machine-enforced** (import-linter contracts
in `make test`): `domain/` is pure — no I/O, no env, no clock — `adapters/` is
I/O, `app/` is wiring. New pure logic goes in `domain/`; new I/O goes behind an
existing adapter or an injected callable.
`domain/model_routing.py` validates immutable difficulty-tier provider/model
selections. `adapters/runtime_selection.py` selects the existing governed
provider stack before paid admission; each stack retains its own accounting
runtime under one journaling wrapper. This adds no lifecycle state or worker.
The Pyrefly seam gate structurally assigns every production adapter to each
domain port it implements. An AST coverage contract requires every newly
declared Protocol to join that checked set or an explicit documented exemption,
so the global annotation-backlog suppressions cannot hide a new port drift.
Pyrefly checks the explicit `src` and `tests` inputs without applying its default
hidden-directory exclusions or consulting ambient Git ignore files: linked issue
worktrees live below the hidden `.claude/worktrees` scratch path and that path is
also excluded by the primary checkout's shared `.git/info/exclude`, but their type
gate must be identical to the primary checkout's.

Triage returns one of three dispositions: `ready`, `blocked`, or `close`.
Closing is gated by `STEWARD_PROMOTE`, the same authority switch that gates
promotion to `ai-ready`: a deployment that keeps a human on lifecycle decisions
does not acquire autonomous closing by upgrading. A
`close` verdict must carry both a concrete reason and a reopen condition; the
steward then comments, closes the issue (`completed` only when the work actually
shipped, otherwise `not_planned`), and applies the terminal `ai-closed` label —
deliberately not `ai-done`, which means "a harness PR merged" and drives the
reopen-based recurrence metric. A close missing either field, or a close on an
issue whose `stateReason` is `reopened`, is refused and left open under
`ai-triage` for a maintainer. Because `ai-closed` is not a lifecycle label, a
human reopening a steward-closed issue puts it back into ordinary triage, and
the next non-close disposition retires the stale marker. Existing deployments
must create the `ai-closed` label (`issue-agent prepare-remote`) before enabling
closing; the relabel fails otherwise and the issue re-enters triage until the
ADR 0012 stall ceiling escalates it.

The steward optionally grooms open issue bodies (`STEWARD_GROOM=1`): it emits
advisory comments and the orthogonal `maybe-resolved` label for verified recent
merge matches or stale references. A structured `duplicate_of` claim may close
an issue only when `STEWARD_PROMOTE=1` and a separate harness-owned GitHub read
proves the target is a different, closed issue. The close travels through the
same durable `CloseIssue` rail as triage and always uses the harness-selected
`completed` reason. Grooming is capped at five candidates per changed tick and
persists a title/body signature after a successful delivery, preventing harness
comments from self-amplifying spend.
It also performs a free reconciliation over open PR metadata and posts one
advisory flag when a changed path is governed by an ADR that the PR description
does not cite. The shell governor is advisory defense-in-depth; durable
commands, captured-revision guards, SQLite ownership, and trusted readback
are the publication authority.

## The five properties worth preserving

These are what adversarial review and live deployments identified as
load-bearing. A refactor that breaks one of them is a regression even if the
tests pass.

1. **Decisions are data.** `decide_*` returns `Action` tuples; nothing in
   `domain/` performs a write. This is why adding escalation alerting and the
   journal were one-place changes.
2. **One privileged write funnel.** Harness lifecycle mutations become durable
   `LifecycleCommand`s and are executed by `GitHubEffectExecutor` or
   `GitHubOperatorEffects` with an idempotency key, expected revision, and
   receipt. `DurableEffectRunner` alone owns the command transition/receipt
   protocol, including reconcile-first async effects. Command/receipt identities
   may be subsystem-namespaced for recovery and attribution, but authority is
   always checked against the base deployment owner. Deployment-scoped effects
   hold shared `DeploymentAuthority`; a repoint takes the same repo/role lock
   exclusively, so an old mutation either completes before retirement or fails
   its owner check after it. Peer effects remain concurrent and no SQLite
   transaction spans network I/O. This includes
   CLI/dashboard labels, bootstrap settings, base-branch creation, Jira sync,
   and webhook relay lifecycle. Paid agents publish branches and open PRs
   through `issue-agent-publish`; `AgentPublicationBroker` commits
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

## Independent review execution

Reviewer children remain diff-only. After source review and before an ordinary
approval, the harness independently executes the frozen operator `TEST_CMD`
against the exact PR checkout. Its stored receipt binds rejection/execution
authority, head, patch and configuration, with pre/post tracked-tree proof and
bounded supervised teardown. Durable writers recheck that evidence before an
absent approval write; changed revisions or configuration cannot reuse a pass.
Compatible rounds reuse completed results. Failure requests changes; timeout,
interruption and unavailable execution follow bounded evidence holds. An explicit
operator waiver is recorded separately. Behavioral contracts cover normal and
copied approvals, unavailable execution, mutation, cancellation and restart.
See [ADR 0011](../adr/0011-trusted-execution-and-gate-evidence.md).

## Enforcement: authority plus two advisory stacks

The fixer is governed at two levels:

- **The advisory governor hook** (`adapters/governor.py` +
  `plugin/hooks/bash_governor.py`)
  inspects every Bash call the agent makes. Its parsing core
  (`domain/governor_policy.py`) is *shared* between the Python and bash entry points,
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
The 2026-07-31 migration rehearsal replayed a 98.531-hour, 303-row historical
window twice; matched source digest, per-scope balances, count, amount, and
failure decisions; and exercised ordinary, exhausted, duplicate, and
conflicting-receipt admissions on the pre-existing live primary database.
Fresh databases use the v9 adoption baseline. The exact digest emitted by the
previously shipped v8 initializer remains an accepted immutable predecessor,
so deployed v8 databases upgrade through v9/v10/v11/v12/v13/v14/v15/v16/v17
without rewritten history. V13 canonicalizes GitHub's case-insensitive `owner/name`
repository identity across commands, reservations, limits, and dispatch claims;
startup also adopts both historical mixed-case owner formats so in-flight
authority is not stranded by the cutover.
V14 binds each coarse dispatch claim to every admitted run in that workflow.
Claim reaping treats any bound nonterminal run as authoritative until its owner
recovers and terminalizes it, closing the crash window where a sibling checkout
could start duplicate paid work while the original child or descendant still
ran.
V15 records retired deployment owners and their successors. Repoint adoption
transfers active ownership and retires the complete recorded lineage in one
transaction. Every role store is then bound to its current owner: all later
transactions recheck that durable fence, fresh claims and reservations from a
retired owner fail closed, and a rebooted old service exits cleanly at startup.
V16 classifies every active reservation as spawn-pending, running, or
reconcile-only. Recovery can release only spawn-pending work while atomically
rechecking that no lease registered and the run never started; any reservation
that may have reached a paid provider remains unavailable until positive
settlement evidence or explicit operator resolution.
V17 separates workload repository attribution from the budget scope that
admitted a run, so global-only reservations still charge the correct
repository. It also restores pre-v16 reserved/no-lease rows to spawn-pending
when the old lease-before-exec contract proves they never launched. Paid child
registration carries the launcher's immutable expected owner and checks it in
the lease transaction, while a normal completion that races dead-lease spend
recovery converges with an equal recovered charge and corrects the false
abandonment. Read-only spend projections validate the current migration
history, SQLite quick check, foreign keys, and semantic invariants before
showing authoritative current-day values.
Completed usage-blind runs use a narrower bounded adjudication path: recovery
first retries exact provider reconciliation for the reservation grace window,
then atomically releases only a lease-free `completed/spend_unreconciled`
reservation and records `spend_unmeasured_released`. Migrated ambiguous rows,
dead leases, and running work cannot enter this path.
The coordinator feeds lease, reconciliation, and compare-and-set results into
the pure `domain/reservation_recovery.py` state machine. That model owns action
ordering and terminal reporting; SQLite remains responsible for atomically
proving that a requested release is still eligible. New runtimes or evidence
sources therefore extend one decision table rather than adding recovery loops.
V20 indexes the workflow episode on existing admitted runs. Adjudication allowance
reads are bounded and episode-scoped; generation changes do not renew them. A
strict never-launched release carries a distinct terminal proof, while ambiguous
execution continues counting. A launched PR repair (REBASE, ADDRESS_REVIEW,
FIX_CI, linked reply) that ends without a captured `pr-repair-result` is
acknowledged by `reconcile_pr_repair_returns` only once its run is terminal,
lease- and claim-free, has no unresolved publication command, and its reservation
is already released or settled; an
active reservation keeps the hold until an explicit accounting disposition. The
acknowledgment writes a `PRRepairUnreturned` receipt (no output, session, model
or settlement), charges the paired issue allowance one attempt so existing
breakers still bound retries, and changes only the exact current admissions.
Existing session/model provenance and financial evidence remain unchanged; a
late normal result cannot overwrite this acknowledgment. Recovery shares the
existing ceiling of 32 completions per reconciliation pass.

PR-repair acknowledgment crash/concurrency matrix (ADR 0003, ADR 0016):
selection is read-only and releases its transaction before acknowledgment.
The mutation transaction re-proves eligibility; SQLite's writer serialization
orders competing capture against that recheck and the paired state/receipt/outbox
writes. No process or network operation runs inside either transaction.

| Interruption or competing writer | Durable outcome and retry | Behavioral test in `tests/app/` |
|---|---|---|
| After candidate selection, before mutation | No state changed; the next reconciliation reselects and acknowledges once. | `test_pr_repair_recovery_interleavings.py::test_interrupted_acknowledgment_retries_without_duplicate_debt[after-selection]` |
| During paired state/receipt/outbox writes | The transaction/savepoint rolls back both components and receipts; retry completes once. | `test_pr_repair_terminal_recovery_guards.py::test_acknowledgment_rolls_back_both_components_and_receipts_on_write_failure` |
| Commit succeeds but the caller never receives the return | Both components and the distinct acknowledgment remain committed; retry adds no debt or receipt. | `test_pr_repair_recovery_interleavings.py::test_interrupted_acknowledgment_retries_without_duplicate_debt[after-commit]` |
| Normal result captured after selection, before acknowledgment's recheck | Capture wins; acknowledgment returns false without mutation. The next ordinary pass applies the real result/session exactly once. | `test_pr_repair_recovery_interleavings.py::test_capture_between_selection_and_acknowledgment_wins` |
| Acknowledgment commits before normal capture | The late capture is refused, retaining prior genuine session/model and financial records. | `test_pr_repair_terminal_recovery_guards.py::test_settled_recovery_preserves_prior_session_and_money_and_refuses_late_capture` |
| Newer generation or admission supersedes the selected run | Exact admission guards preserve the newer components. | `test_pr_repair_terminal_recovery.py::test_stale_unreturned_repair_cannot_rewrite_a_newer_generation`; `test_pr_repair_terminal_recovery_guards.py::test_recovered_run_cannot_clear_newer_pending_admission` |

Migration validates immutable run/admission/budget
provenance atomically before deriving the index; it does not activate a workflow
family. This migration has been exercised only against isolated test databases.
Every migration is digest-validated and individually transactional; restart
and concurrent-role tests interrupt both sides of every migration commit
boundary and serialize fresh/v8/v9 boot. Machine-readable
evidence is in `control-store-migration-evidence.json`.
V12 removes the run-level active-dispatch uniqueness constraint: one review
workflow may legitimately run several validator sublegs concurrently. The
coarser paid-dispatch authority is instead the durable target/path claim made
before the workflow starts and released on every completion, rejection, or
cancellation path. Process liveness is snapshotted before the claim
transaction, so OS inspection never holds SQLite's cross-process write lock.
The snapshot fingerprint includes bound run state and lease PID/start identity;
a concurrent bind or lease transition is conservatively retained and retried.
The bounded production-shaped boot, active SIGTERM shutdown, isolated Codex
accounting run, cleanup receipt, and post-canary invariants are captured in
`live-release-canary-evidence.json`.
JSONL is bounded
schema-v2 telemetry and a projection feed; losing it cannot authorize a write,
deduplicate work, or admit spend.
Every lifecycle command and paid run is scoped to repository, role, and stable
checkout deployment identity in the shared store. Cross-process dispatch/path
claims remain repository/role-global, so two intentionally allowed checkouts
cannot both admit the same target or overlapping path set. A daemon queries
only its exact ownership scope, so sibling deployments and roles cannot cancel,
recover, or receipt each other's work.
The observed-world target used to acquire a dispatch claim is carried through
workflow routing and paid admission. A fresh PR read may validate that target
but cannot replace it; changed linkage defers to a new observation. An issueless
PR is claimed by negative PR number without manufacturing a per-issue spend
scope (ADR 0008).
Review execution allowance belongs to the SQL PR execution episode, so a
later closing-link edit cannot erase an already-paid unproductive round.
Current review authority stores a generation-local checkpoint and an ordered,
bounded unresolved suffix. Immutable round, child admission/launch/return,
money settlement and submission receipts retain exact retry and recovery
evidence outside that aggregate. Late older-generation results settle their
actual spend without resetting current allowance or reviving a session.
An unresolved round whose existing recovery observations reach their limit
holds new rounds and paid work; settlement and exact retries remain available.

Stopped execution activation publishes one complete immutable plan, including
eligible empty targets and explicit source-specific blocked targets. Terminal
targets do not gain work; ambiguous historical counters, sessions or ownership
are retained under migration holds. Shadow equivalence applies to supported
empty inputs; blocked history is reported separately. Copied rehearsal preserves
source databases, WAL and files. Exact original-plan retry reads only selected
SQL receipts, even after later legitimate activity.

Execution renewal and selected confirmed verdict release use the existing
pending operator intent and durable effect rail. Renewal requires complete
old-episode quiescence and carries a compact origin/session reference, preserving
rejection allowance and unrelated holds. The dashboard projects this SQL state
and daemon-captured facts through its existing transport; polls never grant work.
Store continuity/session/model/reply/phase and steward baseline APIs are retired.
Current selected issue/PR components retain exact paid admission and outcome
sources, while free remediation holds the existing command identity until its
exact head/run/base receipt settles. Machine and budget recoveries share one
SQL count and retire only after verified projection. Current issue pending
admissions recover classification even when monetary settlement and newer PR
continuity have already committed. Schema39's fixed indexes select these owed
sources independently of settled history. The copied rollback requires an
explicit stopped index-only39→38 schema step before the archived F14 reader;
later money/holds/replies survive, and forward migration restores39.
Their raw files remain available to stopped import and cannot regain runtime
authority after restart or rollback; the existing schema fence refuses an older
binary before it can mutate the current database.

The supported exception is an explicit same-repository checkout repoint:
`issue-agent start --write-config` records a bounded lineage of exact prior
checkout identities. The first boot reaps only those process registries and
adopts their run/lease/claim/command and projection-feed ownership. The marker
persists across repeated repoints so an interrupted intermediate boot cannot
strand an older reservation. Before the overlay changes, the CLI stops and
verifies all foreground and service-owned processes carrying its stable name;
failure preserves the old snapshot. Schema v15 permanently fences the adopted
owners, covering processes missed by OS discovery and old service definitions
started after reboot. Cross-repository overlay reuse is refused.
Process-group, process-tree, and launch-intent receipts use the same stable
checkout deployment identity beneath each role state root. Boot and shutdown
therefore reap only that deployment's children even when sibling checkouts
intentionally share the repo-level default state root. Upgrade recovery inspects
the former unscoped registry conservatively: a live recorded launcher or live
external parent remains authoritative and is never signalled.
At the deployment-identity cutover, the first role deployment atomically adopts
the former `<repository>:<role>` run, lease, and nonterminal command scope.
Reconcile-before-write then prevents a changed owner identity from duplicating
an ambiguous legacy effect.

The control database is rooted at the operating-system service account's
`~/.local/state/issue-agent-control/control.db`, independently of every
repository's `STATE_DIR` values. Custom repository state roots therefore cannot
split the host-global reservation pool. Startup fails closed if it finds the
former deployment-local database at a custom root; that authority must be
migrated into the canonical database before the deployment can restart.
CLI status and dashboard usage endpoints project current-period settled charges
directly from this SQLite authority. The historical JSONL spend ledger is read
only during idempotent migration and cannot make current spend appear as zero.

The dashboard is a read-only projection consumer. Its canonical operational
model is rebuilt by the pure `domain/projection.py::fold` over schema-v2
events; GitHub reads may enrich titles and URLs but never infer lifecycle
success or turn an unavailable observation into an empty world.
The fold retains the first start for a run, never lets a duplicate start erase
its terminal state, and preserves a completion even when its start aged out of
the bounded event window, keeping visible cost and completion metrics intact.
Per-role scheduler heartbeats supply scheduler-health freshness when an
unchanged world correctly emits no new observation event; they do not infer
process liveness.
If process discovery is unavailable, dashboard liveness is `unknown`; recent
activity is never promoted into proof that a role is alive or dead.
Event streams retain eight rotated files plus the current file. Rotation writes
the active level envelopes, permanent feed-retirement fences, and the latest
observation for every `(feed, world subject)` into the new current file, so
bounded retention and restart do not resurrect a cleared level, discard an
active one, or make an unchanged issue disappear. A stable per-path
interprocess lock covers append, replacement, authoritative on-disk reseeding,
and pruning; multiple daemon/publisher writers cannot rotate stale in-memory
state over one another. `EventLogSegments` is the sole segment locator and
accepts only the writer's timestamp-plus-UUID suffix, so backup or unrelated
siblings are never projected or deleted.

Every paid run is bounded by layered breakers, because the failure mode
that matters is not "wrong answer" but "loop that bills forever":

- per-leg retry counters (`ci`, `review`, `rebase`, `resolve`) → escalate at cap
- `MAX_INFRA_DEATHS` / `MAX_UNPRODUCTIVE_REVIEW_ROUNDS` — a *separate, higher*
  ceiling for sessions that die before returning a result, which deliberately do
  not count as attempts. Refusals minted before admission
  (`return_authority_unavailable`: no run, so no episode receipt) spend the same
  ceiling through a per-issue `level_state` row scoped to the issue-recovery
  episode and re-arm generation; the next admitted run or a generation/episode
  change (the operator's re-arm) clears it, a corrupt row holds only that issue
  until that re-arm deletes it, and nothing prunes it on observation (a partial
  fetch must not reset a live ceiling). Every refusal counts, whatever its
  cause, so each issue is bounded. Refusals are also infra-class, so they reach
  `INFRA_BREAKER` like any infra death: in a deployment outage the breaker trips
  once two or more issues fail and gates dispatch, and issues escalate
  individually only after repeated re-probes. If the
  ledger write itself fails, the issue fails closed from an in-memory marker.
  Bounded residual: the marker is lost on every restart and every hot reload
  (re-exec), costing at most one uncounted refusal per lost write.
- `INFRA_BREAKER` — the deployment-wide twin of the per-issue infra ceiling.
  `INFRA_BREAKER_DEATHS` infra-class deaths over ≥2 distinct issues inside
  `INFRA_BREAKER_WINDOW_SECONDS` cannot be about any one issue, so paid
  admission pauses for the whole fix role, once, with a cause and the
  remediation already attempted (`infra.breaker.tripped.v1`). It re-arms on the
  next leg that returns a result, or after `INFRA_BREAKER_COOLDOWN_SECONDS`
  (`infra.breaker.cleared.v1`). One issue dying repeatedly stays
  `MAX_INFRA_DEATHS`'s business. State is in-memory: a restart re-arms.
- `MAX_BUDGET_USD` per-leg spend cap — native on the Claude SDK; on codex the
  harness prices streamed rollout counters and kills the process group on
  crossing when `CODEX_ENFORCE_BUDGET=1` and a captured rate card or positive
  legacy token prices are available
- `AGENT_MAX_RUNTIME_SECONDS` wall cap, checked between events
- repository/global/issue caps over atomic SQLite reservations; an armed cap
  blocks admission if accounting is unavailable or the runtime is unpriceable

An adopted PR that genuinely has no closing issue omits only the inapplicable
issue scope; its review legs still reserve against repository and global caps.
A missing issue on issue-scoped fix work remains a fail-closed attribution
error.

Provider-billed runtimes settle their reported cost. Subscription runtimes
whose provider reports zero use the configured versioned rate card as an
explicit synthetic token meter, so sequential Claude/GLM work cannot release
its reservation back to a nominally armed cap at zero. If the final usage
envelope is missing, an armed cap conservatively prices the observed stream
accumulator; if neither trustworthy usage nor pricing authority exists, the
full reservation remains active instead of becoming new headroom.

Codex 0.160 native review children share their parent's run and reservation.
The run-scoped observer joins durable spawn call/output receipts to canonical
child metadata, then prices each child's own response usage at its actual model
using the same captured card as the parent. Copied fork prefixes and referenced
ancestor history are excluded. The configured card also prices root-only work;
legacy scalar rates cannot override its cap decision. Missing lineage, model,
usage or completion returns `ACCOUNTING_UNAVAILABLE` and retains the full
obligation.
A rollout `token_count` whose cumulative `total_token_usage` and `last_token_usage`
both equal the previous one in the same rollout is an idempotent Codex
re-emission (for example after a rate-limit refresh) that adds no tokens, so it
is not counted against the response receipts. Equal totals with different last
usage, decreasing totals, or a missing or malformed total stay ambiguous and
fail closed; the receipt equality check is unchanged.
Spend evidence freezes the meter's failure text. Recovery parses it once into
`NativeFailure`; for the ambiguous-token-count class (fully determined by the
retained rollouts) it re-runs the meter over `codex_home` and, when that now
measures, settles through the normal reconcile path and journals
`accounting.native_usage.remetered.v1` (once per run and class, in the settlement transaction). Every recovery read
(root cumulative usage and all child events) is bounded by the harness-owned
`runs.completed_at`, so a later leg resuming the same session is never billed to
the ended run; an event without a parseable timestamp fails closed, and a run
with no `completed_at` is not re-metered; a root token_count after the end also fails closed. A still-failing, missing or malformed
re-read keeps the unavailable result and its retained obligation, and every
other frozen failure class stays final without reading rollouts. Recovery requires the complete version-2 invocation/card evidence.
Unsettled legacy version-1 recovery evidence stays reserved because it cannot establish child
accounting. JSONL remains evidence for the authoritative SQLite settlement.
Native accounting uncertainty cannot expire through the legacy completed-run
grace release; only actual reconciliation or an explicit accounting disposition
can remove its obligation.
The publishing user task explicitly authorizes step 8's bounded fresh reviewers,
scoped verifiers and required evidence capture within this same leg's budget.

A Claude leg that declares `blocked` while a delegated agent task it started is still
non-terminal (tracked from SDK `task_started` / `task_notification` /
`task_updated`) cannot ever receive that task's notification. The runner
reclassifies it as an infra-class `BACKGROUND_CHILD_PENDING` error instead of a
human-input block; resume parks the issue back to `ai-ready`, and the ordinary
consecutive-infra ceiling bounds repeats. Only `blocked` is rewritten.

Counters decay on any leg that returns a result, so the ceilings bound
*consecutive* failure rather than accumulating over a healthy issue's lifetime.

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
A transport retry can replace that run's process lease only under the same
active owner and dispatch, after the kernel proves the prior process group is
absent and the new identity is live. It revalidates continuation admission and
journals the replacement. Attached review children keep their immutable launch
receipt and cannot use this replacement path.

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
stale reservations without touching processes, worktrees, or commands. Legacy,
explicitly repointed, and current-deployment process registries are reaped in
the boot trigger's pre-adapter phase. The remaining current-state
reconciliation runs explicitly after graph construction and legacy-spend
migration but before readiness; a missing production trigger hook fails
startup. Network fail-soft behavior and operation-local reconcile-first effects
are not recovery triggers.
`SIGTERM` moves the daemon through `RUNNING → DRAINING → STOPPED`: paid
admission stops first, active work receives a bounded cancellation window,
handover and receipts are persisted, the outbox is flushed, and remaining
groups are killed. Before cancellation, owned active runs receive a durable
cancellation intent. Forced teardown then reconciles measured rollout usage; a
still-indeterminate paid run retains its reservation and evidence for boot
recovery instead of manufacturing a zero charge or releasing unknown capacity.
State-owned handovers and the resulting outbox transitions are then flushed.
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
every reservation and lifecycle command.

Untrusted repository, issue, review, CI, and Jira text crosses into planners as
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
starts. Advisory leg-role and durable-publication markers remain visible to the
hook. The Codex hook receives the operator base branch as a quoted, hook-local
value, so PR validation does not require exporting deployment configuration to
target commands. The authority is carried as a compact bundle so the same
ancestor check works through `/proc` on Linux and `ps` on macOS; its
GitHub host is captured by the daemon, and both push remotes and `gh --repo`
scope are pinned to that host rather than child-controlled `GH_HOST`.
Publication credentials are nevertheless still
recoverable in the child process/filesystem topology; the governor and broker
do not constitute credential isolation. All runtimes therefore remain
trusted-inbound-only until the trigger-gated separately privileged artifact
publisher is deployed.

Visual evidence read by the Claude SDK is recorded as a daemon-signed receipt
bound to the exact Git HEAD and image digest. Agent-written or stale records
have no authority. Codex PostToolUse hooks execute inside the untrusted child,
so Codex cannot mint a trusted receipt and visual-acceptance publication fails
closed on that runtime. Service upgrades likewise stage complete
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
`domain/ci_history.py`
(the bounded per-issue record of what each FIX_CI leg was pointed at and what
was red, so attempt N can see the pattern across attempts 1..N-1 instead of
re-diagnosing one tick's failure blind),
`domain/contracts.py`, `domain/dashboard.py`, `domain/effect_retry.py`,
`domain/eligibility.py`
(action-specific allowed/held/invalid results and immutable completion facts;
shared by lifecycle policy and durable action payloads without an import cycle),
`domain/effort.py`, `domain/environment.py`
(the harness-attested capability manifest: what the boot probe saw, and the
narrow conditions under which it may state a verdict rather than facts),
`domain/exemptions.py`
(the #124 reviewer-granted gate exemptions: declaration/grant grammar, the
exemptable-vs-security rule classification, and the merge-withholding rule),
`domain/events.py`, `domain/fix.py`, `domain/gate.py`,
`domain/measurement.py` (validated incident/intervention observations),
`domain/cohort.py` (pure admission-cohort arithmetic and explicit coverage),
`domain/governor_policy.py`, `domain/git_remote.py`, `domain/identity.py`,
`domain/infra_breaker.py`, `domain/jira.py`,
`domain/labels.py`, `domain/leg.py`, `domain/lifecycle.py`,
`domain/liveness.py`,
`domain/outcome.py`, `domain/ports.py`, `domain/pricing.py`,
`domain/run_usage.py` (immutable measured/unavailable per-model run usage),
`domain/projection.py`, `domain/refs.py`, `domain/repository.py`,
`domain/recovery.py`, `domain/reservation_recovery.py`,
`domain/review.py`, `domain/self_clear.py`
(level-triggered retirement of machine-checkable blocks — #180 publication/checks
blockers and #168 budget headroom — with a per-issue self-clear ceiling),
`domain/steward.py`, `domain/steward_stall.py`,
`domain/supervision.py`, `domain/triage.py`, `domain/trust.py`, and
`domain/world.py`, `domain/workflow_identity.py`, `domain/workflow.py`,
`domain/continuation_components.py` (fixed secondary component variants, current target-fence selection and ordered continuity watermarks),
`domain/continuation_import.py` (closed stopped fix/steward source fields and typed import decisions),
`domain/workflow_ports.py`, `domain/workflow_policy.py`,
`domain/workflow_reconciliation.py`, `domain/workflow_retry.py`, `domain/resolve_return.py`, `domain/resolve_split.py`,
`domain/resolve_workspace.py`, `domain/resolve_rearm.py`, `domain/resolve_activation.py`,
`domain/review_activation.py`, `domain/review_exemptions.py`, `domain/review_linkage.py`,
`domain/review_execution.py` (pure current execution authority and bounded ordered unresolved-suffix fold),
`domain/review_execution_activation.py` (typed stopped import identity, ownership and selected raw source observations),
`domain/review_execution_counter_mapping.py` (offline attribution of legacy issue and negated-PR counter files from complete open-PR linkage observations; shared issue keys block),
`domain/review_execution_death_import.py` (observational whole-set attribution of abandoned owner runs and current counter files; ambiguous or cross-PR deaths block),
`domain/review_execution_history_import.py` (pure per-PR classification of inherited review evidence; empty observations remain nonauthoritative and historical sources retain specific blockers),
`domain/review_execution_import_plan.py` (pure source-bound zero-history screening and fixed mixed whole-repository import proposals; no authority writer),
`domain/review_execution_migration.py` (immutable sourced blockers and canonical blocked migration decisions; no execution grant),
`domain/review_execution_session_import.py` (fail-closed classification of legacy session files; real handles retain source evidence but cannot yet grant reuse),
`domain/review_rounds.py` (pure outer-round plans, verified evidence and finalization),
`domain/review_receipt_attribution.py` (pure selected receipt shape checks and exact round/command references),
`domain/review_preflight.py` (pure round selection and current paid-work eligibility under captured execution authority),
`domain/review_return.py` (bounded, unclassified review child result evidence),
`domain/review_rejection.py`, `domain/review_workflow.py`, `domain/review_workflow_policy.py`,
`domain/review_rearm.py`, `domain/review_control.py`, `domain/review_dispatch_record.py`,
`domain/review_evidence.py`, `domain/review_test_evidence.py` (revision/configuration-bound independent execution facts), `domain/deployment.py`
(cycle-free typed target, generation, epoch and episode identities for the
incremental gate/re-arm authority migration; the PR-owned review-rejection
family has its own snapshot, continuation and handoff-free intents, sharing only
the gate values and transition envelopes with resolve, and its pure policy counts
rejections with one selector and authorizes dismissal of exactly the gate's
observed set; workspace handoffs are explicit
preparation obligations, not AUTHOR evidence or independent execution authority;
family activation plans legacy imports offline and refuses rather than defaulting;
an operator retry authorization buys exactly one execution of an existing
reconciliation-only command after harness-proven absence; an unreadable
exemption-trailer, review-history or linked-issue read backs off to a visible,
never-approving hold; linked issue lifecycle and SQL holds are joined into review
admission, operator facts and applicable effects; the review daemon's plan records its own per-pull-request dispatch
decision, with the facts and the read it came from and an as-of time, which the
operator view renders instead of deciding; the view offers each gate's commands
or refuses them with the reason the snapshot or that recorded decision already
proves, refusing commands on a recorded merged pull request while a closed
pull request retains its lever subject to fresh acceptance reads).

`adapters/` — filesystem, process, provider, and remote-I/O implementations:
`adapters/accounting_runtime.py`, `adapters/agent_publish.py`,
`adapters/measurement.py` (atomic observation recording and read-only SQL export),
`adapters/agent_runner.py`, `adapters/app_token.py`,
`adapters/child_env.py`, `adapters/claude_profile.py`,
`adapters/codex_hook.py`, `adapters/codex_runner.py`,
`adapters/codex_child_usage.py` (bounded native lineage, own-response observation and frozen-card recovery),
`adapters/control_path.py`, `adapters/control_store.py`,
`adapters/deployment_authority.py`, `adapters/durable_effects.py`,
`adapters/effects.py`,
`adapters/env_file.py`, `adapters/environment.py`,
`adapters/event_log.py`, `adapters/github.py`,
`adapters/governor.py`, `adapters/gate_transport.py` (trusted shell gate composition), `adapters/handover.py`,
`adapters/heartbeat.py`, `adapters/journal.py`,
`adapters/operator_effects.py`, `adapters/process_supervisor.py`,
`adapters/reload.py`,
`adapters/repository_guidance.py`, `adapters/resolve_activation.py`, `adapters/review_activation.py`, `adapters/review_linkage.py`,
`adapters/continuation_activation.py` (bounded raw continuation roots and atomic existing aggregate publication),
`adapters/continuation_run_import.py` (bounded stopped SQL attribution; historical executions supply migration holds only),
`adapters/review_execution_activation.py` (bounded read-only stopped file and SQL identity/ownership sources; ambiguous history retains source-specific blockers),
`adapters/review_execution_import_observation.py` (read-only complete configured SQL import source collection with a cumulative source budget; no cursor advancement or activation authority),
`adapters/review_execution_transition_proof.py` (bounded selected generation-transition receipt source; no lifetime replay),
`adapters/sql_read_budget.py` (opt-in transaction result-byte/row meter and temporary SQLite value bound; page and point queries bound engine work separately),
`adapters/review_execution_counter_links.py` (read-only complete open-PR and linked-issue observations with repository-bound revalidation for counter import),
`adapters/review_execution_import_sources.py` (barrier-held legacy file/PR-link and SQL source revalidation, including current registration/lifecycle command membership before counter attribution),
`adapters/review_execution_import_commit.py` (atomic mixed eligible/blocked whole-scope publication; blocked workflow identity grants no execution),
`adapters/review_execution_import_replay.py` (bounded SQL-only original-plan retry against selected epoch, decision, origin and blocker receipts; later activity cannot change that accepted request),
`adapters/review_execution_workflow_census.py` (stopped activation's bounded retained target inventory against its complete open listing; terminal identities remain visible),
`adapters/review_effects.py` (guarded review dismissal and PR projection
commands executed by the review owner),
`adapters/review_dispatch_holds.py` (transaction-local review submission and release holds),
`adapters/review_execution_claims.py` (exact parent claim and lifecycle receipt validation),
`adapters/review_test_execution.py` (supervised nonpaid operator tests and frozen-input proofs),
`adapters/review_execution_control.py` (review-round preflight, source-backed registration and abandoned-round recovery),
`adapters/review_round_sources.py` (transaction-local reconstruction of child, planning and submission receipts),
`adapters/review_workflow_control.py` (the review owner's generation-fenced
commit boundary: store-minted facts, barrier-ordered acceptance, bounded
re-arm reconciliation and the ceiling-release projection fact),
`adapters/runtime_plan.py`, `adapters/single_instance.py`,
`adapters/spend_recovery.py`, `adapters/store.py`,
`adapters/supervised_exec.py`, `adapters/systemd_notify.py`,
`adapters/typed_journal.py`, `adapters/untrusted.py`,
`adapters/wake.py`, `adapters/worktree.py`, `adapters/workflow_codec.py`,
`adapters/workflow_rows.py` (family-scoped authority reads),
`adapters/workflow_commit.py` (family-neutral CAS receipt helpers),
`adapters/workflow_control.py`, and
`adapters/workspace_admission.py`. Dashboard-specific read
adapters live under `adapters/dashweb/`.

`app/` — composition and use-case orchestration:
`app/cli.py`, `app/codex_resolution.py`, `app/config.py`, `app/dashboard.py`,
`app/dispatch.py`, `app/gate_check.py` (absolute advisory gate entry),
`app/execute.py`, `app/identity_pools.py`, `app/infra_breaker.py`,
`app/launchd_main.py`,
`app/loop.py`, `app/main.py`,
`app/measurement.py` (local observation/export transport and cohort specification),
`app/mcp.py`, `app/recovery.py`, `app/review_identity.py`, `app/roles.py`,
`app/resolve_control.py`, `app/resolve_activation.py`, `app/review_activation.py`,
`app/continuation_activation.py` (configured fixed fix/steward offline import, original-plan retry and copied rehearsal),
`app/review_execution_import.py` (configuration-bound legacy source checks and observational whole-repository zero-history import planning),
`app/review_execution_import_remote.py` (configured complete PR, review and trusted-hold rereads under the import coordinator's barriers),
`app/review_execution_activation.py` (configuration-bound offline planning and activation under fix/review/steward barriers, source and quiescence rechecks, atomic publication and SQL-only exact retry; operator transport and historical import remain pending),
`app/review_runtime.py` (the active review family's dispatch plan, bound
submission authority, trusted external holds and fix-side guidance read, and the
one review-control composition the daemon and the operator transport share),
`app/review_control.py` (the review owner's operator composition from the
launch snapshot for the PR-scoped dashboard API),
`app/service_doctor.py`, `app/webhook.py`,
`app/world_probe.py`, and `app/world_reporter.py`.
Provider-neutral task construction is isolated in `app/planners/`; paid
workflow sequencing and the single `run_leg` path are in `app/workflows/`;
its shared completion helper revalidates current facts between confirmed
effects, while the durable executor retains those facts across command retries;
the web composition/read-model boundary is in `app/dashweb/`.

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
