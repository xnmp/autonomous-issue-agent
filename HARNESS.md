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

`issue-agent start` creates or reuses a repo overlay, discovers `REPO` and
`TARGET_DIR`, runs the preflight, and
starts the selected fix/review daemons in the foreground. Ctrl-C sends
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
- `FIX_BOT` must differ from `REVIEW_BOT`.
- `TEST_CMD` (optional) is only `bin/point-at.sh`'s base-branch probe; no fixer
  gate runs it. It must terminate.
- `FIXER_RUNTIME=claude|codex|glm`;
  `REVIEWER_RUNTIME=claude|codex`.
- `MAX_BUDGET_USD` / `REVIEW_BUDGET_USD` bound a leg.
- The fixer runs one configured model and effort: `FIX_MODEL` and
  `FIX_EFFORT` (blank keeps the runtime default). There is no difficulty
  router or per-tier model ladder (refactor decision Q12). Review model
  settings are independent.
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
- `ISSUE_AGENT_MAX_TICKS=N` is the safe one/few-tick live canary control.

The reviewer approves on its model verdict for the exact PR head. Merge
requires a passing CI report: targets must have CI checks, and a PR with no
reported checks does not merge. Changes under `.github/**`
escalate through the existing human-review path. Review legs do not execute
operator test commands.

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

The daemon observes GitHub, derives each item's state with a pure function,
and maps it to `Action` values. The fix role executes them directly
(`adapters/act.py`): every write is naturally idempotent (a label set, a merge
or update-branch bound to the decided head, a PR opened only when the branch
has no open one) or claims an idempotency key in SQLite and confirms it with a
GitHub read-back. The fix agent pushes its own `fix/<issue>-` branch with `git
push` and returns; the harness opens the PR from that branch (when it is ahead
of base, and has no PR or was pushed after a human closed its PR), using the
agent's summary as the description. An agent's question is recorded with its
leg and carried by the `needs-human` stop comment; reply on that thread to
answer it. Harness comments are recognised by the marker and a harness login
together. The review role writes through the same `act` step: one GitHub
review per head under the review bot, keyed in SQLite and read back before
submission (slice 5c).

There is no command governor and no fixer publication gate (slice 6a). Current
runtime children still inherit publication capability, so use only trusted inbound
issues/reviews/CI text. The credential-free artifact publisher is deliberately
trigger-gated on untrusted-repo onboarding; see
`docs/reference/capability-authority-matrix.md`.

Agent-written files are claims. Remote facts are re-derived by trusted code; CI
and the independent reviewer judge the pushed head.

## State, recovery, and accounting

The per-account SQLite database at
`~/.local/state/issue-agent-control/control.db` is authoritative for:

- paid reservations and charges;
- run/lease ownership;
- deduplication keys;
- lifecycle commands and effect receipts.

Every paid leg reserves its maximum charge before the runtime starts, including
when operator caps are disabled. Concurrent final-cap races are serialized.
On first startup after upgrade, current and rotated legacy spend ledgers are
imported into the live repository/global/issue scopes before admission. Any
malformed or unattributed legacy row exits with operator-repair-required status
instead of resetting a cap.
Use `issue-agent install-service` for this cutover: the installers stop every
legacy role before starting any SQLite-authoritative role, preventing a late
legacy-ledger append from racing the import.
Upgrading past the steward removal: before reinstalling, stop/disable/remove any
`issue-agent-steward-<name>` systemd unit, or unload/remove the steward launchd plist;
the installers no longer manage it.
Settlement is idempotent and records currency, integer micros, pricing basis,
usage provenance, and rate-card identity.

At boot and after a suspension gap, recovery runs in a fixed order: kill known
orphan groups, reclaim dead leases, reconcile unsettled spend, release only
reservations durably proven never launched, drain commands through receipt
verification. A run that may have reached
a paid provider remains fail-closed until positive settlement evidence exists.
Completed runs that returned without trusted usage or a provider charge are a
separate liveness case: recovery retries provider reconciliation through the
reservation grace window, then atomically releases the retained maximum and
records `spend_unmeasured_released`. This keeps repeated usage-blind results
from permanently exhausting admission capacity while preserving an explicit
operator-visible accounting gap.
An explicit same-repository repoint first reaps the recorded prior checkout
registries and atomically adopts their run, lease, claim, and command
authority; unrelated sibling checkouts remain isolated.
Historical JSONL is never queried for these decisions.

Each role's `journal.jsonl` is a plain JSON-lines log only. Metrics derive
from the SQLite legs/money tables (runs, charges) plus GitHub.

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

The board reads each role's `journal.jsonl` plus GitHub. GitHub calls may
enrich links/titles but do not infer lifecycle state.
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
offered only when this API reports the family `inactive`.

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
For a resolve ceiling, send:

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
per command over its life (the last one reaches the ceiling and no further grant is possible). A
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
unwitnessed, corrupt or outdated databases; there is no activation procedure
(deleted in slice 5a).

### Review role

The review role has no operator API since slice 5c; the review rejection
family, its ceiling gates, re-arm, retry grants and dashboard review panel are
deleted. The role owes one REVIEW leg when CI is green on a harness PR's head
and the review bot has no standing verdict on that head. It submits the parsed
verdict as one review on that commit, with the bot's previous review of the
pull request in the prompt. A submission GitHub did not answer is resubmitted
after the machine backoff without buying the review again; one GitHub refused
(a 4xx, such as a token without pull-request write) counts as an attempt, as
does a head whose diff is empty. Everything else is the fix role's: a bot
change request is answered by an `ADDRESS_REVIEW` leg, and three attempts on
one head (unusable verdicts, refusals, empty diffs), or repeated bot
change-request rounds, stop the pull request at `needs-human`. To buy a fresh
review of the same head, dismiss the bot's review on GitHub. The role refuses
to start without `TARGET_DIR`, or when its credential's login is not
`REVIEW_BOT`.

### Family activation (deleted)

Offline resolve, review and review-execution activation, their
`*-activation` CLI subcommands and legacy import were deleted in slice 5a. An
inactive family stays inactive; there is no activation procedure.

## Testing

```bash
make test
make typecheck
make test-domain
make test-adapters
make test-app
```

`make test` is the required submission gate. The suite includes import/purity
contracts, convergence, transactional accounting
races, recovery/supervision, and service artifact tests.
`config/agent.env.example` is generated from `HarnessConfig` metadata; update
the field definition and regenerate with
`uv run python scripts/generate_config_example.py > config/agent.env.example`.

## Safe live canary

Use a throwaway repository with a self-authored trivial issue:

```bash
ISSUE_AGENT_MAX_TICKS=1 issue-agent start
```

Confirm:

- all roles reach readiness and write heartbeats;
- the control database is created with restrictive
  permissions;
- the issue produces one reservation before a runtime starts;
- Ctrl-C/SIGTERM leaves no registered process groups;
- the dashboard renders the observed issue.

Only then install the unattended services.

On Linux, `issue-agent install-service` verifies every systemd unit reaches
`active (running)` before reporting success and rolls the installation back if
any role does not. The earlier foreground release canary did not exercise
systemd; verify the service-manager path separately for each host.
