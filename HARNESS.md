# Harness operator manual

The live system is the Python package under `src/issue_agent/`. The archived
numbered documents describe the deleted bash implementation and are not an
operator reference.

## First run

Prerequisites: `git`, `gh`, `jq`, `uv`, `bwrap` (bubblewrap 0.10 or newer, with
unprivileged user namespaces; Linux only; Ubuntu 24.04 and Debian 12 ship older
ones, so build a release as CI does), and at least one configured runtime
(`claude` or `codex`).

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
stale value from a relaunch shell. Each admitted run records the snapshot
version that created it.

Important fields:

- `REPO=owner/name` and `TARGET_DIR=/absolute/path` are required.
- `issue-agent start --write-config` regenerates an existing overlay for the
  checkout given. Before replacing the overlay, the CLI stops and verifies every
  foreground or installed process carrying that deployment name; a stop failure
  leaves the old overlay unchanged. The overlay keeps its name-derived state
  roots, so the new deployment's boot kills any process group the old one left
  behind whose launching daemon is dead, whatever its repository. Lease and
  spend recovery is scoped by repository and role: for a new checkout of the
  same repository it settles the old deployment's unsettled legs; after a
  repoint to another repository, the old repository's unsettled legs stay
  reserved in its own scopes (they never block the new one) until a deployment
  of that repository runs again. Prefer a different `--name` for another
  repository.
- `FIX_BOT` must differ from `REVIEW_BOT`.
- `TEST_CMD` (optional) is only `bin/point-at.sh`'s base-branch probe; no fixer
  gate runs it. It must terminate.
- `FIXER_RUNTIME=claude|codex`;
  `REVIEWER_RUNTIME=claude|codex`.
- `MAX_BUDGET_USD` / `REVIEW_BUDGET_USD` bound a leg. A fix leg that hits its
  budget or the wall cap resumes its session once at the same tier; a second
  hit on that resume counts as an attempt. `ISSUE_SPEND_CAP_USD` still stops
  the issue for a human, and it does so before any leg it cannot afford at
  the dearer of the two budgets. With the defaults (10 / 15 / 25), one budget
  hit already leaves no room for the resume; size the issue cap to allow it.
- Routing (refactor decisions 15, 27): a STEWARD leg writes each admitted
  issue's acceptance criteria and flags duplicates; a TRIAGE leg then grades it
  `low|med|high|xhigh`; one harness comment posts the tier and criteria. Fix
  legs run on `FIX_MODEL_<TIER>` (`runtime:model`, e.g. `claude:claude-sonnet-5-5`), one tier stronger per earlier attempt in the
  attempt run, capped at `xhigh`. Review legs run at the tier of the fix leg
  that produced the head, else the triaged tier, else `REVIEW_MODEL`. An unset
  tier spec falls back to `FIX_MODEL` on `FIXER_RUNTIME`. `TRIAGE_MODEL`
  (blank: the `low` spec) routes the tier-grading leg and `STEWARD_MODEL`
  (blank: `TRIAGE_MODEL`'s route) the leg that runs first, writing the
  acceptance criteria and flagging duplicates; `TRIAGE_BUDGET_USD` bounds each
  of the two. A suspected duplicate stops the issue with `needs-human`; a reply or label
  removal releases it. Effort stays `FIX_EFFORT` / `CODEX_REASONING_EFFORT`.
- Astra has built-in standard-context accounting estimates ($10/$50 per
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

This repository is public, so its CI runs on GitHub-hosted runners only: a
fork PR runs `make test` with its own code, and a runner on the operator's
machine would expose the harness credentials to it. A private target repository
can still use one native local runner instead of paid hosted minutes; see
[native CI setup](docs/reference/self-hosted-ci.md).

The daemon observes GitHub, derives each item's state with a pure function,
and maps it to `Action` values. The fix role executes them directly
(`adapters/act.py`): every write is naturally idempotent (a label set, a merge
or update-branch bound to the decided head, a PR opened only when the branch
has no open one) or claims an idempotency key in SQLite and confirms it with a
GitHub read-back. The fix agent pushes its own `fix/<issue>-` branch with `git
push` and returns; the harness opens the PR from that branch (when it is ahead
of base, and has no PR or was pushed after a human closed its PR), with the
agent's summary, unchanged, as the description (AGENT.md step 9 has the agent
write the whole of it). An agent's question is recorded with its
leg and carried by the `needs-human` stop comment; reply on that thread to
answer it. Harness comments are recognised by the marker and a harness login
together. The review role writes through the same `act` step: one GitHub
review per head under the review bot, keyed in SQLite and read back before
submission (slice 5c).

There is no command governor and no fixer publication gate (slice 6a). Every
leg runs under bubblewrap (#413): it sees a private HOME, its own checkout, the
toolchains and its runtime's session store, never `config/`, `~/.config/gh`,
`~/.ssh`, the harness checkout, state dirs or other repositories. A fix leg's
one GitHub credential is `FIX_BOT_TOKEN` (give it Contents and Pull requests
write on the harness repos and nothing that bypasses branch protection); a
review leg has none. Paid legs are refused when bwrap is missing or cannot
create a user namespace, and `issue-agent doctor` reports the posture. What a
sandboxed leg can still reach is listed in `docs/open/residuals.md` §1.1; see
`docs/reference/capability-authority-matrix.md`.

Agent-written files are claims. Remote facts are re-derived by trusted code; CI
and the independent reviewer judge the pushed head.

## State, recovery, and accounting

The per-account SQLite database at
`~/.local/state/issue-agent-control/control.db` is authoritative for:

- paid reservations and charges, one `legs` row per leg;
- item leases (which role holds which issue or PR, and its process);
- idempotency keys for GitHub comments.

Every paid leg reserves its maximum charge before the runtime starts, including
when operator caps are disabled, and only while its role holds the item's
lease. Concurrent final-cap races are serialized.
Upgrading past the steward removal: before reinstalling, stop/disable/remove any
`issue-agent-steward-<name>` systemd unit, or unload/remove the steward launchd plist;
the installers no longer manage it.
Settlement is idempotent and records currency, integer micros, pricing basis,
usage provenance, and rate-card identity.

At boot and after a suspension gap, recovery runs in a fixed order: kill known
orphan groups, reclaim dead leases, reconcile unsettled spend, release only
reservations whose process never registered. A leg that may have reached a
paid provider keeps its reservation while its lease is live. Once its process
is gone, recovery settles it from surviving request evidence (Codex rollouts),
or else at a zero `estimate` charge, so usage-blind ends cannot exhaust
admission capacity. Historical JSONL is never queried for these decisions.

A missing `control.db` is recreated only on a machine that never had one. If
its companions remain (`control.db.initialize.lock`, `-wal`, `-shm`,
`.retired-v*`, `.authority`), startup refuses and names them rather than
silently resetting every cap to zero. With every daemon stopped, restore the
database from a backup. If a `control.db.retired-v*` file is listed and there
is no backup, move any listed `-wal`/`-shm` aside (they belong to the lost
store) and rename the retired file back to `control.db`: the next start cuts it
over again and keeps the pre-cutover spend; only legs recorded since the
cutover are lost. Do not delete a retired file. Only when none is listed may
you delete the listed files to start deliberately with no spend history.

While the daemons run, the latest commits can sit in `control.db-wal`: each daemon's store wake holds a connection through its idle wait. Back up with `sqlite3 control.db ".backup <file>"`, or stop every daemon first. Copying `control.db` alone can miss recent spend.

### Upgrading to the 3-table store (slice 5d-2)

The first role daemon to open a pre-5d-2 `control.db` converts it in place,
once. Carried over: every settled charge (so today's repository/global spend
and each issue's lifetime spend still count against the caps) and confirmed or
pending comment keys. Dropped and re-derived from GitHub on the first tick:
workflow rows, receipts, holds and attempt history (attempt counts restart).

1. Stop every role daemon of every deployment and the dashboard
   (`systemctl --user stop 'issue-agent-*'`, or unload the launchd plists).
2. Start the new release (`issue-agent install-service` or `issue-agent start`).
3. If it exits with `control-store cutover refused`, read the message:
   - *holds N unsettled legacy run(s) … owned by …*: the old store still has
     in-flight money. Check out the previous release, start each named owning
     deployment once, let its boot recovery settle (its `issue-agent status`
     shows no in-flight legs), stop it, and go back to step 2.
   - *another process holds … open*: something had the database open at that
     instant (a daemon or the dashboard); stop it and go back to step 2. The
     check is point-in-time, so step 1 is what actually guarantees nothing of
     the previous release is left running.
4. On success the old file remains as
   `control.db.retired-v<N>-<UTC stamp>` beside the new one. Nothing reads it,
   but keep it: it is the only copy of the pre-cutover spend if `control.db`
   is ever lost (see above). The old
   `control.db.authority` witness file beside it is no longer used and may be
   deleted too.

The cutover is one-way: an older release refuses the new schema, so roll back
by restoring the retired file (with every daemon stopped) under the
`control.db` name, accepting that legs recorded since are lost.

Each role's `journal.jsonl` is a plain JSON-lines log only. Metrics derive
from the SQLite `legs` table plus GitHub.

## Dashboard

```bash
issue-agent dashboard [--port PORT]   # = python -m issue_agent.app.dashweb.main
```

The Linux service installer keeps the interactive server running as the
host-wide `issue-agent-dashboard.service`. The server watches this harness
checkout's committed HEAD and exits 87 after active requests drain so systemd
can restart it on the new code. A manually started server exits the same way
and prints the command to re-run when no supervisor is present.

The board reads each role's `journal.jsonl` plus GitHub. GitHub calls may
enrich links/titles but do not infer lifecycle state.
CLI and dashboard quota totals are read-only projections of settled SQLite
charges for the current budget period.

### Operator writes

The dashboard changes labels on GitHub and nothing else (refactor decision
Q19). `POST /api/repos/{name}/issues/{number}/park` adds `needs-human` to the
issue and to each open harness PR for it (head `fix/<issue>-…`); the fix
policy then waits until the label is removed or a human replies to the stop.
`POST …/unblock` removes `needs-human` from the issue and those PRs and
changes nothing else. Park and unblock read the issue and its open PRs fresh
from GitHub and refuse a closed issue. `POST …/promote` adds `ai-ready`. There
is no priority action. Each write reads the label first, writes
only what is missing, and reads it back; repeating a request moves nothing,
and a write whose reply was lost is observed rather than repeated. There is no
command queue, typed resolve command or retry grant (deleted in slice 5d-1).

To hold an issue until another lands, mark it "blocked by" that issue on
GitHub (issue dependencies; any repository). The harness only reads the link:
while a blocker is open, the issue gets no STEWARD, TRIAGE or RESOLVE leg and
the board shows it under Waiting as "Blocked by #N". It starts by itself once
every blocker is closed, whatever the close reason. An issue that already has
an open harness PR is not held. A `blockedBy` read the harness cannot parse is
logged and never blocks.

The steward may also propose such a link, but only for a genuine dependency:
the issue cannot be correctly done until another open issue lands. Overlap is
not a dependency. The harness treats the proposal as an untrusted claim. The
target must be an open issue the leg was offered, must not be the issue itself,
and must not itself be blocked by it (re-read when the link is written). Then
the harness links it and posts one comment with the reason, or one saying why
it refused. That comment ends the matter: delete the link on GitHub and the
harness never re-adds it.

Among ready issues, dispatch prefers the oldest whose steward-guessed paths
(`touches`, kept in the STEWARD leg's result) overlap no file that an open
harness PR changes. This changes order only. With every candidate overlapping,
or no guesses, it is plain FIFO. A paid scheduler leg is the deferred
alternative (#388).

The listener permits literal `127.0.0.1` or `::1` only; existing Host, same-Origin,
and `X-Issue-Agent-Dashboard: 1` write guards apply. This trusts the local operator
account; it is not a multi-user authentication service.

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
