# Harness operator manual

The live system is the Python package under `src/issue_agent/`. The archived
numbered documents describe the deleted bash implementation and are not an
operator reference.

## First run

Prerequisites: `git`, `gh`, `jq`, `uv`, and at least one configured runtime
(`claude`, `codex`, or a GLM Claude profile).

```bash
make install
cd /path/to/target-repository
issue-agent start
```

`issue-agent start` creates or reuses a repo overlay, discovers `REPO`,
`TARGET_DIR`, and (when omitted) a non-watch `TEST_CMD`, runs the preflight, and
starts the selected fix/review/steward daemons in the foreground. Ctrl-C sends
the daemons through their bounded drain path.

Useful commands:

```bash
issue-agent doctor
issue-agent ready 42
issue-agent status
issue-agent dashboard
issue-agent stop
```

The lower-level equivalent is:

```bash
make point-at REPO=<overlay-name>
```

## Configuration

Configuration is layered:

1. `config/shared.env` — identities, secrets, shared runtime/pricing defaults.
2. `config/repos/<name>.env` — repository, checkout, state directories, test
   command, role-specific overrides.
3. `config/.active.<name>.env` — generated immutable launch snapshot.

The repo overlay wins. The active snapshot is the daemon authority: removing a
knob from it restores the dataclass default instead of accidentally inheriting a
stale value from a relaunch shell. Each admitted run and lifecycle command
records the snapshot version that created it.

Important fields:

- `REPO=owner/name` and `TARGET_DIR=/absolute/path` are required.
- `issue-agent start --write-config` may repoint an existing overlay only to a
  new checkout of the same repository. It preserves a bounded
  `REPOINTED_FROM_DEPLOYMENTS` lineage so the first boot can reap and adopt
  those exact old deployment identities. Before replacing the overlay, the CLI
  stops and verifies every foreground or installed process carrying that
  deployment name; a stop failure leaves the old overlay unchanged. Schema v15
  then retires every prior owner identity in the shared control store. An old
  process that survived discovery loses all write authority, and an old service
  started after reboot exits cleanly instead of reacquiring work. Use a
  different `--name` for another repository; do not hand-author the lineage.
- `FIX_BOT` must differ from `REVIEW_BOT` in three-identity mode.
- `TEST_CMD` must terminate. The trusted executor bounds it, scrubs deployment
  controls, stamps recursion depth, and kills the full process group on timeout.
- `GATE_TEST_FAILURE_EXIT_CODES` defaults to `1`. Add a code only when the
  trusted test runner documents it as assertion failure; timeout, spawn, and
  signal exits can never satisfy the red-test gate.
- `FIXER_RUNTIME=claude|codex|glm`;
  `REVIEWER_RUNTIME=claude|codex` (Codex review supports single mode only);
  `STEWARD_RUNTIME=claude|codex` (triage/groom; Codex needs `TARGET_DIR` and
  cannot run Jira sync — no MCP transport. An unset `STEWARD_TRIAGE_MODEL`
  resolves to `sonnet` under claude and to the Codex runner's own default
  under codex).
- `MAX_BUDGET_USD` / `REVIEW_BUDGET_USD` bound a leg.
- Configure implementer tiers with `FIX_MODEL_LOW`, `FIX_MODEL_MED`,
  `FIX_MODEL_HIGH`, and `FIX_MODEL_XHIGH`, each as `runtime:model`. They map
  to trivial, standard, complex, and exceptional difficulty. For example:

  ```sh
  FIX_MODEL=""
  FIX_MODEL_LOW="codex:gpt-6-luna"
  FIX_MODEL_MED="codex:gpt-6.1-sol"
  FIX_MODEL_HIGH="claude:claude-opus-5-5"
  FIX_MODEL_XHIGH="codex:gpt-6-astra"
  ```

  Blank tier settings retain the built-in ladder. Partial overrides are
  supported. Malformed selections and known model/provider mismatches fail
  configuration validation. A configured ladder uses Low for classification
  and Med if classification fails. Each purchase uses its selected provider's
  hooks and accounting; Claude and Codex remain available while overrides are
  enabled so changing a tier preserves existing sessions, split stages and PR
  repairs. Clearing every tier override restores single-provider operation;
  finish existing cross-provider work first or retain an explicit override
  until its transcripts are no longer needed. Returning to a single provider
  can require a fresh PR repair session or leave an issue resume held.
  `FIX_MODEL` still pins fresh work to `FIXER_RUNTIME` and bypasses
  classification. Review and steward model settings are independent.
- Unpinned implementers route by difficulty: `complex` uses Sol/Opus;
  `exceptional` uses Astra/Fable under Codex/Claude. Recurrence or two
  completed resolve/CI failures also select that exceptional ceiling. A pinned
  `FIX_MODEL` wins, and repair legs retain the model of their existing session.
  Astra has built-in standard-context accounting estimates ($10/$50 per
  million input/output tokens, $1 cached input); existing configured model
  rates override them. These estimates are not provider invoices and do not
  model the provider's long-context surcharges.
- `ISSUE_SPEND_CAP_USD`, `DAILY_SPEND_CAP_USD`, and
  `GLOBAL_DAILY_SPEND_CAP_USD` are enforced transactionally by SQLite.
  The database is fixed per operating-system service account at
  `~/.local/state/issue-agent-control/control.db`; custom per-repository state
  roots never create independent global-cap pools.
  A genuinely issue-less adopted PR is charged to repository/global scopes;
  the issue scope is omitted because no issue authority exists.
- `ISSUE_AGENT_MAX_TICKS=N` is the safe one/few-tick live canary control.

Durable review approval requires a nonpaid test receipt from the frozen operator
`TEST_CMD` for the exact PR head, patch, execution episode and configuration.
Repository `.harness/test-cmd` cannot authorize approval. Compatible rounds reuse
completed results; genuine failures request changes through the normal rejection
ceiling. `ALLOW_UNTESTED=1` records an explicit operator waiver.

Unavailable tests back off without buying another reviewer before the next due
observation. At the evidence ceiling, settle the old round and use the existing
**Renew execution** action for the ordinary-test hold. It grants one new execution
episode and preserves unrelated holds and rejection allowance. Changing test
configuration alone does not release a hold. Writers never launch tests while
draining commands.

Do not put shell watch commands in `TEST_CMD`; `pytest`, `vitest run`, and
equivalent terminating commands are appropriate.

## Running unattended

After a one-tick foreground canary succeeds:

```bash
issue-agent install-service
```

The installer renders deployment-specific systemd user units on Linux or
launchd jobs on macOS, validates them, and retains the previous known-good
artifacts on failure. On Linux it also refreshes and restarts the host-wide
`issue-agent-dashboard.service`; the most recent repository installation owns
that singleton definition, while every repository's role units remain
independent. Rendered service definitions pin the absolute `uv`
executable found during installation; the installers' `--dry-run` mode
validates candidates without replacing live definitions. Each
role/repository/checkout owns a distinct lock and identity.
Deployment names have one canonical lowercase `[a-z0-9._-]` representation;
the CLI and both installers reject lossy aliases before they can collide.
Systemd and launchd key definitions and private snapshots by that stable name,
while allowing `TARGET_DIR` to change for a same-repository repoint. Every
prior role stops before the snapshot changes; launchd also migrates
checksum-era jobs carrying that identity in the rollback-safe cutover.
Uninstall is two-phase on both platforms: every loaded role must stop
successfully before any definition is deleted, so a failure stays loud and
recoverable.

Systemd units use SIGTERM draining, a 90-second stop deadline, readiness after
all synchronous startup checks, and a five-minute watchdog fed by scheduler
and bounded GitHub-operation progress. Restart rates and memory/task counts are
bounded, and permanent configuration/schema/store failures do not restart.
View logs with:

```bash
journalctl --user -u issue-agent-fix-watcher-<name> -f
systemctl --user status 'issue-agent-*-<name>'
```

Systemd also writes `issue_agent_service_terminal` to the role's journal after
each invocation, including failed startup or daemon termination. The
`ExecStopPost` hook `bin/service-terminal-evidence.sh` records the invocation,
the manager result and available exit code/status. It runs inside the unit's
cgroup, so it also records that cgroup's `oom`/`oom_kill` counters and
`memory_peak`. Empty manager fields and `unknown` counters are evidence gaps,
never zero. A signal status does not identify its sender. Exit 137 alone does
not establish OOM, but `result=oom-kill` with a nonzero `oom_kill` does. Join
the record to the manager's own transitions for the same invocation, including
the stop or start job type and result:

```bash
journalctl --user -o short-iso USER_INVOCATION_ID=<id> + _SYSTEMD_INVOCATION_ID=<id>
```

The journal does not record which client issued a stop job; that attribution
stays unknown unless other evidence names it. If the manager or journal cannot
write the record, or the checkout moved since installation, its absence is an
evidence gap. The `-` prefix ignores an unsuccessful hook exit; a hook spawn
failure or timeout may still affect the unit's result. Retain the original
main-process exit fields with that result.

To exercise the hook without a paid role, run throwaway transient units with
the same hook, then read their journals as above and `systemctl --user
reset-failed 'iaa-terminal-probe-*'`:

```bash
hook="-/bin/sh $PWD/bin/service-terminal-evidence.sh"
systemd-run --user --unit=iaa-terminal-probe-signal -p "ExecStopPost=$hook" \
  python3 -c 'import os, signal; os.kill(os.getpid(), signal.SIGKILL)'
systemd-run --user --unit=iaa-terminal-probe-oom -p MemoryMax=32M \
  -p MemorySwapMax=0 -p "ExecStopPost=$hook" python3 -c "b = b'x' * (256 << 20)"
```

Expect `result=signal exit_status=KILL oom_kill=0` and `result=oom-kill
exit_status=KILL oom_kill=1` respectively.

On launchd, logs live under the deployment state directory rather than shared
`/tmp` names.

Uninstall with `issue-agent uninstall-service`.

## Runtime and trust model

Repository CI can use one native local GitHub Actions runner instead of paid
GitHub-hosted minutes. See [native CI setup](docs/reference/self-hosted-ci.md)
for the dedicated account, standard runner service, limits and installation.
The runner retains all existing checks and reports genuine GitHub CI results.

The daemon observes GitHub, computes pure `Action` values, and submits remote
mutations as durable `LifecycleCommand`s. `GitHubEffectExecutor` handles daemon
actions; paid agents must use `issue-agent-publish git push …` and
`issue-agent-publish gh pr create …`. That broker commits intent before the
network call and reconciles an unknown outcome before retry. Commands are bound
to the exact repository and role identity; a different repo/role daemon cannot
claim them from the shared control store. A branch publication also refuses to
run if the remote moved after its expected revision was captured.

The shell governor is advisory defense-in-depth. It denies governed commands it
cannot resolve, but it is not the authorization boundary. Current runtime
children still inherit publication capability, so use only trusted inbound
issues/reviews/CI text. The credential-free artifact publisher is deliberately
trigger-gated on untrusted-repo onboarding; see
`docs/reference/capability-authority-matrix.md`.

Agent-written gate markers are claims. Tests and remote facts are re-derived by
trusted code. Live enforcement uses `GateResult` and `OpenPrLookup`; an
unavailable or indeterminate observation never becomes allow.

## State, recovery, and accounting

The per-account SQLite database at
`~/.local/state/issue-agent-control/control.db` is authoritative for:

- paid reservations and charges;
- run/lease ownership;
- deduplication keys;
- lifecycle commands and effect receipts;
- the transactional event outbox.

Every paid leg reserves its maximum charge before the runtime starts, including
when operator caps are disabled. Concurrent final-cap races are serialized.
On first startup after upgrade, current and rotated legacy spend ledgers are
imported into the live repository/global/issue scopes before admission. Any
malformed or unattributed legacy row exits with operator-repair-required status
instead of resetting a cap.
Use `issue-agent install-service` for this cutover: the installers stop every
legacy role before starting any SQLite-authoritative role, preventing a late
legacy-ledger append from racing the import.
Settlement is idempotent and records currency, integer micros, pricing basis,
usage provenance, and rate-card identity.

At boot and after a suspension gap, recovery runs in a fixed order: kill known
orphan groups, reclaim dead leases, reconcile unsettled spend, release only
reservations durably proven never launched, drain commands through receipt
verification, and emit abandoned-run corrections. A run that may have reached
a paid provider remains fail-closed until positive settlement evidence exists.
Completed runs that returned without trusted usage or a provider charge are a
separate liveness case: recovery retries provider reconciliation through the
reservation grace window, then atomically releases the retained maximum and
records `spend_unmeasured_released`. This keeps repeated usage-blind results
from permanently exhausting admission capacity while preserving an explicit
operator-visible accounting gap.
An explicit same-repository repoint first reaps the recorded prior checkout
registries and atomically adopts their run, lease, claim, command, outbox, and
world-feed authority; unrelated sibling checkouts remain isolated.
Historical JSONL is never queried for these decisions.

Schema-v2 JSONL is rotated projection input and telemetry only. The dashboard
folds typed events; schema-v1 readers exist solely as a read-only historical
converter.

## Dashboard

```bash
issue-agent dashboard                 # interactive server
issue-agent dashboard --text
issue-agent dashboard --html
```

The Linux service installer keeps the interactive server running as the
host-wide `issue-agent-dashboard.service`. The server watches this harness
checkout's committed HEAD and exits 87 after active requests drain so systemd
can restart it on the new code. A manually started server exits the same way
and prints the command to re-run when no supervisor is present.

For deployments with `events-v2.jsonl`, all dashboard surfaces use the one pure
`domain/projection.py::fold`. GitHub calls may enrich links/titles but do not
infer lifecycle state.
CLI and dashboard quota totals are read-only projections of settled SQLite
charges for the current budget period; the legacy spend ledger is migration
input only.

### Resolve operator API

The loopback dashboard exposes `GET /api/repos/{name}/issues/{number}/resolve-control`
and `POST` to the same path plus `/commands`. The issue page renders these as
its resolve controls; the board's "Review holds" opens that page rather than
mutating from the row. The browser saves the exact in-flight command in the
tab's session storage before sending it, so a lost reply or reload offers only
a byte-identical retry, never a new request ID. The legacy label unblock is
offered only when this API reports the family `inactive`. Live activation
remains paused under the
[architecture plan](docs/open/2026-09-14-architecture-implementation.md).

GET reads existing SQLite authority only. It distinguishes inactive family,
active-but-absent target (404), available workflow, and unavailable/corrupt
authority (503). Optional `request_id` selects historical intent separately from
the current intent and holds. `workflow_held` describes SQLite holds, not remote
readiness; submission checks external facts again. Inspection never creates or
migrates a database, performs Git recovery, or drains commands.

`release` is the single recovery command; `cancel_rearm` remains cancellation.
Copy the exact `expected` identity from GET (repository, issue or pull request,
family, epoch, generation, version, episode) and use a stable `request_id`.
Release selects one gate, registered eligible verdict, or stopped grant.
For a resolve or review rejection ceiling, send:

```json
{"operation":"release","request_id":"operator-stable-id","expected":{"...":"copy GET expected"},"selection":{"kind":"gate","gate_id":"copy GET gate_id","intent":{"kind":"resume","guidance":{"kind":"keep"}}}}
```

Guidance is `keep`, `clear`, or `set` with text. A stopped grant selection is
`{"kind":"stopped_grant","stopped_request_id":"original request"}`; it continues
existing authority. Cancellation names the pending request, cancellation ID,
and reason. Actor identity is minted by the server for its local OS account.
Release maps to the existing domain commands, with the same ID namespaces,
guidance normalization, receipts, and audit bytes. Browser journals and response
operation names keep their original shapes so tabs opened before deployment
continue to work. Original HTTP operation payloads remain accepted for those tabs.

`release` with gate intent `retry_absent` is offered only for a
`retry_authorization_required` gate: a
command whose remote outcome expired into reconciliation-only mode and whose
current postcondition the harness's own read later proved absent. The body
names `request_id`, selection `gate_id`, and intent `command_id`/`evidence_id` exactly as
GET reports them; the API never accepts proof. It permits **one** execution of
the existing command under its current generation and episode. It does not
renew allowance, create a re-arm, advance generation, reset expiry, or erase
unresolved history. It is offered and accepted only when the executor could
begin it now — the same validator decides both: a re-arm obligation needs its
original request still pending and its workspace handoff verified; a
returned-result completion needs no other gate holding the workflow; the
store's own execution-authority check must pass; retired commands are never
authorizable. The absence must postdate the command's uncertainty and its last
authorized attempt. Authorized attempts are bounded: one per grant and three
per command over its life (the last one is journaled as `ceiling_reached`). A
second grant while one is open, or a reused ID with different content,
conflicts (409); an evidence ID that already backed a grant, a command whose
original decision was judged inapplicable, or a command at its ceiling is
rejected (422). While a grant is open the gate reports it under
`pending_retry` ("one retry authorized, waiting for the executor").

Every grant ends in one journaled terminal disposition (`consumed`,
`not_needed`, `inapplicable` or `void`, entity `workflow_retry_authorization`).
The fix daemon visits a new grant once immediately; later visits follow the
read schedule. It re-reads the postcondition (stamped at read start): present
settles success (`not_needed`); unavailable keeps the grant open under the
normal backoff (`deferred`) and voids it after five unavailable reads or one
day; a restart before consumption keeps it open for the next scheduled read.
Absent re-checks the original decision's guards: a changed guard is
`inapplicable` (a standalone command retires; a re-arm obligation becomes a
verified refusal that supersedes its request at the next re-arm reconciliation,
after which `release` can select the stopped grant, and it can never be granted again). A
scope change since acceptance (other hold, generation, cancelled or retired
obligation, stale read) is `void` and records the fresh absence. Otherwise the
grant is consumed with the attempt in one transaction and the gate reports the
attempt as `remote_uncertain`; while the attempt's process is alive no other
drain reads that command. Success closes the gate; a crash or unverified write
keeps reconciliation-only with the original history, and a later verified
absence needs a new grant. Cancelling or superseding a re-arm voids any open
grant on its retired obligations.

Accepted (202) means durable intent, not completed recovery. Canonical replay
returns 200, stale identity/content conflicts 409, invalid requests 422, and
unavailable observations/authority 503. After a lost response, retry the exact
same command and ID; do not invent a new grant. Later GET reports progress while
the existing daemon recovery and durable executor establish completion.

Both typed and legacy commands use one validated active launch snapshot, without
per-field overlay fallback. Missing snapshots refuse; edits during a request do
not redirect its repository, credentials or local state writes.

The listener permits literal `127.0.0.1` or `::1` only; existing Host, same-Origin,
and `X-Issue-Agent-Dashboard: 1` write guards apply. This trusts the local operator
account; it is not a multi-user authentication service. Legacy `/unblock` refuses
activated resolve authority, including missing targets. It also refuses absent,
unwitnessed, corrupt or outdated databases; operators must use the explicit
offline migration/activation procedure when that slice is accepted.

### Review rejection operator API

The same loopback dashboard exposes the pull-request-scoped review family:
`GET /api/repos/{name}/pulls/{pr}/review-control` and `POST` to the same path
plus `/commands`. Guards, size limits, request-ID replay and the status-code
taxonomy are the resolve API's: 202 accepted durable intent, 200 canonical
replay, 409 stale revision or reused identity, 422 invalid or refused, 503
unavailable (always a typed body, never an empty gate list). Commands are
accepted by the review owner's workflow authority under the fix→review→steward
barriers; the HTTP request executes no GitHub effect. The review daemon's drain
executes every accepted obligation. GET reads SQLite only.

The issue page mounts one review panel per linked pull request that is not
merged. Its statuses are:

- `absent` (404): the review daemon has recorded no workflow for this pull
  request, so it holds no review-rejection gate and nothing can be re-armed.
  A label on GitHub is not such a hold. The panel says so.

- `migration_required`: the review family is not activated. Review dispatch
  and fix-side repairs needing its guidance defer until activation, regardless
  of other families. Activate resolve and review together operationally.
- `unavailable` (503): the authority is unreadable or corrupt.
- `available`: gates with typed causes, the continuation (fresh review owed,
  or the reviewed baseline and its provenance), the pending intent with
  per-obligation progress, and the review daemon's recorded decision.

A gate whose own SQLite facts disagree (two open retry grants, or obligation
receipts that contradict the intent) reads `facts_unavailable`, naming the
underlying cause. That gate still holds and offers nothing; every other gate,
the decision and the daemon are unaffected. A malformed hold-restoration
record reads `hold.state = unreadable` on its ceiling only, and a malformed
recorded decision reads `daemon.state = unreadable`. Neither makes the view 503.

`GET /api/repos/{name}/workflow-families` returns the repository's activated
families, read exactly as the server's legacy `/unblock` refusal reads them.
It returns 503 while the authority is unreadable or uninitialized. The
label-based legacy unblock stays disabled, with the reason shown, unless that
read shows no family active. While review control is active, the reason
points at the linked pull requests' review panels, or says that an issue with
none has no lever until resolve control is active.

Every applicable command appears on its gate, either offered with its effect
or refused with its reason. The UI never sends a refused command. Refusals
cover only facts SQLite proves:

- another request is pending;
- the counted set exceeds the 63-review bound of one re-arm;
- a review run for the pull request is still live;
- a retry grant is already waiting, or the shared grant policy refuses one.

- the review daemon recorded the pull request merged; every action on every
  gate is then refused.

A pull request the daemon recorded closed keeps its gates and its commands:
closing retires nothing, and a reopen needs the lever. `release` says that acceptance re-reads GitHub and
refuses while the pull request stays closed; a refused retry grant is never
stored, so none is spent on a certain refusal. Remote facts (a moved head, a closed pull request,
reviews counted since the gate opened) are decided by acceptance's own fresh
read, which returns 422 for a moved head or a terminal pull request.

Execution renewal uses a gate selection with `renew_execution` intent and the
execution epoch/episode from GET. A `verdict` selection names the registered
submission command, displayed gate ID, execution epoch/episode, and guidance;
it releases only that eligible verdict. Neither changes unrelated holds.
Fix, steward, and imported-hold releases are not offered in this increment.

The release selections and cancellation:

- `release` on a `review_rejection_ceiling` gate authorizes dismissal of
  exactly the counted bot reviews listed in the gate. It never dismisses a
  human review or a later review, and the review set is never on the wire.
  Tagged guidance (`keep`/`clear`/`set`) reaches exactly the next paid review.
  The re-arm then owes one fresh review and returns the pull request to review
  once every dismissal and the relabel are verified. The view and acceptance
  share one predicate for the holds a re-arm keeps
  (`request_rearm_kept_holds`): other open gates except the ones its
  generation advance ends (unreadable review evidence, pre-write reads, a
  refused projection), a stopped re-arm's decision gate it absorbs, and
  uncertainty over a retired obligation it covers; plus the trusted external
  holds (`ai-blocked`, `ai-parked`, `needs-budget-review`, the
  unproductive-round breaker) of the daemon's last read, where acceptance uses
  its own fresh read. When any remain, the effect text says the re-arm buys no
  review and names them.
  When it showed labels the relabel leaves but review dispatch skips
  (`no-ai-review`, `needs-design`, `ai-done`, `ai-working`, an untrusted
  `ai-parked`), the text says the pull request returns to review but no review
  is bought until they are removed. The ceiling's own `needs-human-review` is
  neither. A ceiling with more than
  63 counted reviews is shown refused. Dismiss some on GitHub; ceiling
  reconciliation then refreshes or releases the gate.
- `cancel_rearm` stops the pending request. Verified effects remain,
  unexecuted obligations are retired and never run, and a
  `rearm_needs_decision` gate holds the pull request.
- `release` selecting that stopped grant re-plans the stopped request's
  remaining authorized dismissals against a fresh read. It carries no new
  authorization. Its effect text says, from the same kept-holds predicate as
  its acceptance (`continue_rearm_kept_holds`), whether it buys a review.
- `release` with `retry_absent` behaves as it does for resolve. After exact-replay and stale
  binding checks, acceptance reads the pull request and refuses a closed or merged one. The review
  drain executes it.

A new command naming another family epoch or episode conflicts (409), checked
inside the acceptance transaction. An exact replay retains its original
episode even after that re-arm advanced the episode. Replay across another
activation epoch conflicts.

The view's `daemon` is the review daemon's own recorded dispatch decision; the
view never decides dispatch itself. Provenance: harness-owned. Only the review
role's plan tick writes it, from the daemon's own GitHub read, through the
review owner's workflow control (SQLite `dedupe_keys` namespace
`review-dispatch-decision`, one row per pull request). The operator transport
and the dashboard only read it. It carries:

- `dispatch`, the plan's typed decision, in the daemon's order: `terminal`
  (closed or merged, first); then the workflow's own holds: `held` by gates,
  `submission_unresolved`, `release_projection_pending` (an open gate's
  projected `needs-human-review` is recorded here, never as a label hold);
  `not_admitted` naming each hold outside the review workflow (a missing
  trigger, `needs-human-review`, `ai-parked`, `no-ai-review`, a draft or the
  bot's own pull request when adopting, lifecycle holds, the unproductive-round
  breaker, solo identity); `deferred` (an unreadable fact or a failed read);
  `owed` (it passed the tick's dispatch admission; the spend cap still decides
  when the paid leg runs); `withheld` (owed, but this tick's admission withheld
  it: `fix_claim`, a live fix run on the same issue, or `reserved`, another
  pull request's or deployment's claim on the target); `in_flight` (owed and
  already running: the pull request's own dispatch still holds its
  reservation); `not_owed`;
- `pull_request`, the facts it decided from (head, state, labels, and the
  holds a re-arm keeps), or null when no read of its own succeeded;
- `read`: `review_read` (the trigger-labelled review read) or `departed_read`
  (the pull request's own read after it left that read);
- `decided_at`, and `as_of`: when it was last confirmed unchanged;
- `read_failure`: consecutive failed departed reads (count, latest reason,
  first and latest failure), or null.

The record is the view's projection, never dispatch authority. The departed
reads run in the plan, before admission; after admission only pure
construction and one SQLite write run, and any failure there is logged and
journalled (`review_dispatch_record` `record_failed`) at most once per 15
minutes with the count it suppressed. It never changes or drops an admitted
review.

A changed decision or basis is written at once; an unchanged one refreshes
`as_of` at most every 5 minutes. A stopped or wedged daemon therefore shows an
old `as_of`, never a fresher guess. A pull request that leaves the review read
(its trigger swapped for a hold, or closed) is read on its own at once, then
every 15 minutes. A closed one is re-read every 15 minutes for the first 24
hours after its recorded close while its workflow holds gates (so a prompt
reopen is seen promptly), otherwise every 6 hours; a merged one never again; at
most 4 such reads per tick. A failed read keeps the last good facts of the
pull request's own read (a closed one stays shown closed, as of that read) and
adds `read_failure`; with no such facts it records `deferred`. Consecutive
failures back off 15 min, 30 min, 1 h, 2 h, 4 h, then every 6 h from the latest
failure. The control journal records `review_departed_read` `read_failed` at
the first failure and `read_backoff_ceiling` when the backoff reaches 6 h; a
successful read clears the failure. Only a read decides that a
pull request is closed. `daemon.state = unrecorded` means no tick has recorded
it yet.

`REVIEW_MAX_REJECTIONS=0` disables the ceiling. Relaunch the review role from a
fresh launch snapshot (`issue-agent start`); each open ceiling gate is then
released at its next ceiling reconciliation, on the held-ceiling backoff of at
most 30 minutes. The release is `RELEASE_CEILING`: it advances the generation,
owes one fresh review, and relabels the pull request back to the trigger label
unless another hold remains. A human dismissing the counted reviews on GitHub
releases the gate the same way.

Contested hold. An open ceiling's `needs-human-review` hold is restored when an
actor removes it, at most three times. After that the ceiling reads
`hold.state = contested`: the harness stops restoring the label and journals
`hold_contested` once. The operator is signalled at most once per escalation.
The gate alone still holds every review.
It exits when the gate closes, through a completed re-arm, a human dismissal
of the counted reviews, or a disabled ceiling; `hold_contest_cleared` is
journalled then. Stop the automation that removes the label first.

Exhausted escalation. A verdict escalation's `needs-human-review` projection
refused by hold facts is re-planned at most twice. After that it becomes a
`review_projection_refused` gate with `projection = escalation_hold` and one
operator signal. No operator command applies. To exit:

- apply the planned labels by hand; the next harness read verifies them;
- or push a new head, or close the pull request, which makes the escalation
  moot.

A refused ceiling-release relabel (`projection = ceiling_release`) exits when
the labels are corrected by hand or a later generation supersedes it.

Review projections preserve independent holds. Rejection-ceiling release cannot
remove an ESCALATE/human hold, renew review execution allowance or erase a fix
infrastructure episode. Each supported named release consumes its selected SQL
cause/revision and existing durable receipt. Legacy label-only unblock does not
grant unsupported fix retry allowance; requests needing such a grant refuse
before remote labels/comments. Ordinary selected state comes from SQL, with
Jira's explicitly retained counter path as the sole runtime exception.

### Resolve family activation (offline, operator-only)

A deployment's resolve family stays **inactive** until an operator activates it;
inactive resolve work is held as `resolve_migration_required` and is never read
from the legacy `issue-N.resolve-retries`/`last-error` files, which remain on
disk only as import evidence. Activation is explicit and offline:

```bash
issue-agent resolve-activation inventory            # read-only: in-flight work, legacy records, blockers
issue-agent resolve-activation shadow               # read-only: per-issue legacy vs new-authority comparison + plan digest
issue-agent resolve-activation rehearse --copy-to /tmp/empty-dir   # full activation on an isolated copy
issue-agent resolve-activation activate --confirm-plan-digest <digest from shadow>
```

Stop all roles first: `activate` refuses while any watcher process or fresh or
unreadable heartbeat exists for any deployment of the same repository, while
the control store records in-flight runs, leases, claims, non-terminal
commands or active reservations for it, and when any legacy record is
unreadable, malformed or duplicated across state roots. Add
`--legacy-state-dir` (repeatable, absolute) for older or custom state roots.
The confirmed digest covers the epoch, ceiling, sources, inventory and every
import decision; any change since the reviewed `shadow` refuses. Activation
commits the epoch, imported episodes, ceiling holds for exhausted issues,
per-issue decisions and a `workflow_activation` event in one transaction.
Re-running with the same digest reports `already_active_with_this_plan`;
activation is never repeated or reversed. Rollback after activation means
running compatible code against the retained SQLite authority (or keeping work
paused) — never re-enabling the legacy files. Older binaries fail closed on the
schema. Live activation remains paused under the architecture plan.

### Review family activation (offline, operator-only)

The review family uses the same four commands and liveness/quiescence checks:

```bash
issue-agent review-activation inventory
issue-agent review-activation shadow
issue-agent review-activation rehearse --copy-to /tmp/empty-review-rehearsal
issue-agent review-activation activate --confirm-plan-digest <digest from shadow>
```

Inventory covers every open PR and every legacy review record across the
configured role roots and repeatable `--legacy-state-dir` roots. A complete
fresh bot-review history must corroborate both valid `last-sha` and `patch-id`
before they import as a reviewed baseline. Otherwise the open PR owes a fresh
review. Guidance imports verbatim; closed PR records remain evidence only.
Fresh counting and exemption evidence may import a rejection ceiling only
where legacy review holds it, including the abandoned-at-head escalation case.
The latest bot escalation must prove the rejection-ceiling preamble; an
independent ESCALATE verdict remains a separate hold. Unknown hold provenance
or incomplete history on a potentially held PR blocks import.
Unreadable or malformed sources and ambiguous duplicate roots block activation.

The shadow report compares legacy and new dispatch, including configured
adoption and external holds, and names paid reviews activation would cause.
Linked issue lifecycle/dependency holds and SQLite gates are included in the
new dispatch prediction. A known linked issue whose read fails is unavailable,
never treated as an unlinked PR. Normal review operation preserves that same
distinction and bounds repeated failures with an evidence gate and backoff.
The confirmation digest covers sources, inventory, epoch and every decision.
Activation revalidates under all three deployment barriers and commits once;
same-digest replay reports `already_active_with_this_plan`. Import creates no
daemon dispatch record: the daemon records its first decision from its own read.

Rehearse both families on separate isolated copies first. With all roles still
stopped, shadow and activate resolve, then obtain a fresh review shadow and
activate review before restarting either role. Activating only resolve retires
legacy unblock without supplying the review lever. Review baseline, patch and
guidance files are retained as evidence; their runtime readers, writers and
legacy dismissal/reset controls have been removed. Rollback retains SQLite
authority and requires compatible code. Real activation and restart remain
subject to the architecture plan's deployment pause.

### Review execution activation (offline, operator-only)

Execution activation follows rejection activation. Keep all writers stopped.
Inventory and shadow show every open or retained target; verified empty targets
receive a current execution origin, while historical targets retain a sourced
migration hold. A hold grants no allowance or reusable session. Shadow reports
compare legacy observations with eligibility, allowance, session and holds,
and name the deliberate differences where history cannot safely import.

```bash
issue-agent review-execution-activation inventory
issue-agent review-execution-activation shadow > /tmp/execution-plan.json
issue-agent review-execution-activation rehearse --copy-to /tmp/empty-execution-rehearsal
issue-agent review-execution-activation activate --plan-file /tmp/execution-plan.json --confirm-plan-digest <digest from shadow>
```

The saved shadow report contains the complete immutable original plan. Keep it
for retry after an ambiguous command response; supply the same file and digest.
An exact committed retry checks SQL receipts without GitHub, source-file or
writer-liveness observations. A different digest refuses. First publication
revalidates the configured sources, remote observations and stopped writers.
Inactive execution reports `migration_required`. Activation creates no paid work.

Rehearsal always activates an isolated copy using the canonical control layout.
It refuses destinations overlapping source roots, the target checkout or control
authority, checks exact retry and digest conflict, and verifies original file and
database bytes remain unchanged. Repeatable `--legacy-state-dir` includes older
roots. Rehearsal plans identify their copied sources; obtain a fresh original
shadow report before a separately authorized deployment. Live activation and
restart remain paused under the architecture plan.

## Testing

```bash
make test
make typecheck
make test-domain
make test-adapters
make test-app
make test-hooks
make test-governor
```

`make test` is the required submission gate. The suite includes import/purity
contracts, runtime and gate parity, convergence, transactional accounting
races, recovery/supervision, schema-v2 projection, and service artifact tests.
`config/agent.env.example` is generated from `HarnessConfig` metadata; update
the field definition and regenerate with
`uv run python scripts/generate_config_example.py > config/agent.env.example`.

Run the local control-plane capacity probe with:

```bash
uv run python scripts/benchmark_control_plane.py --iterations 500
```

## Safe live canary

Use a throwaway repository with a self-authored trivial issue:

```bash
ISSUE_AGENT_MAX_TICKS=1 issue-agent start
```

Confirm:

- all roles reach readiness and write heartbeats;
- `events-v2.jsonl` and the control database are created with restrictive
  permissions;
- the issue produces one reservation before a runtime starts;
- Ctrl-C/SIGTERM leaves no registered process groups;
- the dashboard renders the observed issue from the v2 fold.

Only then install the unattended services.

On Linux, `issue-agent install-service` verifies every systemd unit reaches
`active (running)` before reporting success and rolls the installation back if
any role does not. The earlier foreground release canary did not exercise
systemd; verify the service-manager path separately for each host.
