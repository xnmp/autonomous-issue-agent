# autonomous-issue-agent

Headless harness that resolves GitHub issues end-to-end: issue → branch → TDD
implementation → PR → CI → independent review → merge to `dev`. Two Python
daemons (`fix`, `review`) reconcile GitHub and drive Claude, Codex,
or GLM runtimes.

Start with:

- [architecture](docs/reference/architecture.md) — current design and authority;
- [operator manual](HARNESS.md) — setup, operation, recovery, live canary;
- [residuals](docs/open/residuals.md) — accepted risk and trigger-gated work;
- [documentation index](docs/README.md) — which other documents are current.

The numbered documents under `docs/archive/` describe the deleted bash
orchestration. Do not implement from them.

## Direction and guardrails

The north star is lower cost per merged PR without weakening quality. An
efficiency change must name the metric it moves, the guardrail it must not move,
and the next measurement that adjudicates it.

Primary metrics, derived from the SQLite legs/money tables (runs, charges)
plus GitHub:

- cost per merged PR, split by pricing basis;
- cost mix by `Leg` (resolve/review/repair/router/triage);
- issue-to-merge wall time.

Guardrails:

- first-pass approval rate and rounds per PR;
- escalation and infra-death rate;
- merges per deployment day for a comparable difficulty mix;
- recurrence rate (reopened or refiled defects after a harness merge),
  measured from GitHub reopen/refile events (no harness emitter).

Every incident adds a behavioral regression test. Fix the generator/class, not
only the observed instance. A temporary workaround names the issue or condition
that removes it.

## Fixes must not grow the surface

The rule most often broken, and how fix cycles spiral. A fix removes a
mechanism, state, knob, or operator intervention, or at worst leaves the count
unchanged; name what it removes before writing it.

- A new counter, hold, ledger, flag, knob, status, label, retry/defer loop, or
  one-state special case is presumptively wrong. If unavoidable, the PR says
  why no existing mechanism can carry it, and gives its owner, ceiling, and
  exit (automatic or typed operator re-arm).
- Check earlier rather than recover later; unify parallel mechanisms instead
  of adding another.
- Fix PRs and agent reports state net `src/` lines and mechanisms removed and
  added; reviewers reject unjustified additions. Incident history:
  [manager reference](docs/reference/manager.md) surface-area test.

## Architecture rules

- `domain/` is pure: no I/O, ambient environment, wall clock, or adapter/app
  imports.
- `adapters/` owns filesystem, process, network, SQLite, runtime, and dashboard
  transport I/O. It cannot import `app/`.
- `app/` is the composition/orchestration layer.
- Decisions return immutable `Action` values. Remote lifecycle mutations go
  through the idempotent act layer (keyed or read-back).
- Make broken lifecycle states unrepresentable where practical: use explicit
  typed variants/discriminated unions instead of independent booleans, status
  strings, or optional fields that permit contradictions. Parse and validate
  untrusted inputs before constructing a trusted variant; retain runtime
  validation at external boundaries.
- SQLite is authority for money, leases, legs/attempts and idempotency keys.
  JSONL is the daemon log only.
- Tests assert public behavior and observable outputs. Business rules include
  malformed, absent, boundary, and huge inputs.
- Comments explain constraints or incident history, never narrate the next line.
- New gate/review verdict inputs state their provenance (who can write them,
  in which trust domain); below harness-owned means laundered through a
  harness-side mint or treated as an untrusted claim. Retry/defer/skip loops
  carry a log line and a ceiling (or are spaced by the machine backoff).
  ([minimal-harness design](docs/open/2026-10-12-minimal-harness-design.md);
  existing code migrates when touched.)

Import-linter and AST contracts enforce these boundaries in `make test`.

## Running it

```bash
make install
cd /path/to/target
issue-agent start
issue-agent ready 42
```

`issue-agent start` composes an immutable per-repo env snapshot, runs the
preflight, and starts all selected roles in the foreground. `make point-at
REPO=<name>` is the lower-level equivalent. After a one-tick canary, use
`issue-agent install-service` for systemd/launchd supervision.

Configuration layers:

- `config/shared.env` — credentials and shared runtime/pricing defaults;
- `config/repos/<name>.env` — repo, checkout, state dirs, overrides;
- `config/.active.<name>.env` — generated launch snapshot.

`TEST_CMD` is only `bin/point-at.sh`'s base-branch probe; no fixer gate runs
it. It must terminate.

## Operational model

Each role performs:

```text
observe GitHub → pure decide → execute Actions → dispatch paid Legs
                         ↓
                  SQLite control store
                         ↓
                  dashboard (SQLite + GitHub + journal.jsonl log)
```

Every paid leg receives a `run_id`/`leg_id`, reserves its maximum charge in one
transaction, then settles or is recovered. Completed legs settle usable request evidence,
with streamed usage marked as an estimate and interrupted requests charged zero.
Unavailable/corrupt SQLite authority or missing required pre-launch pricing blocks
paid admission while observation and reconciliation continue.

`SIGTERM` stops admission, cancels/drains active work within a bound, writes
handover/receipts, kills remaining groups, and exits with a
stable taxonomy. Boot and suspension recovery kill orphans, reclaim leases,
and reconcile unsettled spend.

There is no command governor. Current agent children still inherit publication
capability, so the supported posture is trusted inbound content only. See the
capability matrix before changing runtime/tool/credential boundaries.

## Package and test commands

Use `uv` only; never pip, pipenv, or poetry. Type-check with pyrefly.

```bash
make test
make typecheck
make test-domain
make test-adapters
make test-app
```

`make test` must be green before any commit. The daemons hot-reload from this
checkout between ticks, so commit only coherent tested states.

`make test` creates private scratch storage and removes it on exit. It prefers
a writable `XDG_RUNTIME_DIR`, then `/tmp`; an explicit `TMPDIR` overrides that
choice. Leave `TMPDIR` unset for routine runs so a prior disk-backed workaround
does not slow every SQLite test. The focused Python targets retain pytest
scratch for debugging.

Before a PR, include required evidence and run an unbiased adversarial review of
the complete diff in a subagent.

## Repository map

- `src/issue_agent/domain/` — value objects, policies, events, ports.
- `src/issue_agent/adapters/` — GitHub/runtime/process/store/event/dashboard I/O.
- `src/issue_agent/app/` — config, role wiring, the shared tick, CLI, dashboard.
- `plugin/` — fixer (`AGENT.md`) and reviewer prompt guidance.
- `ops/` / `bin/` — service templates/installers and launch tooling.
- `tests/domain`, `tests/adapters`, `tests/app` — mirrored test tiers.
- `docs/adr/` — authority, money, events, supervision decisions.
- `docs/reference/` — current operational and architectural references.
