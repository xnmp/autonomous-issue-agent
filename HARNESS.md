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
- `ISSUE_SPEND_CAP_USD`, `DAILY_SPEND_CAP_USD`, and
  `GLOBAL_DAILY_SPEND_CAP_USD` are enforced transactionally by SQLite.
  The database is fixed per operating-system service account at
  `~/.local/state/issue-agent-control/control.db`; custom per-repository state
  roots never create independent global-cap pools.
  A genuinely issue-less adopted PR is charged to repository/global scopes;
  the issue scope is omitted because no issue authority exists.
- `ISSUE_AGENT_MAX_TICKS=N` is the safe one/few-tick live canary control.

Do not put shell watch commands in `TEST_CMD`; `pytest`, `vitest run`, and
equivalent terminating commands are appropriate.

## Running unattended

After a one-tick foreground canary succeeds:

```bash
issue-agent install-service
```

The installer renders deployment-specific systemd user units on Linux or
launchd jobs on macOS, validates them, and retains the previous known-good
artifacts on failure. Rendered service definitions pin the absolute `uv`
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

On launchd, logs live under the deployment state directory rather than shared
`/tmp` names.

Uninstall with `issue-agent uninstall-service`.

## Runtime and trust model

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

For deployments with `events-v2.jsonl`, all dashboard surfaces use the one pure
`domain/projection.py::fold`. GitHub calls may enrich links/titles but do not
infer lifecycle state.
CLI and dashboard quota totals are read-only projections of settled SQLite
charges for the current budget period; the legacy spend ledger is migration
input only.

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
