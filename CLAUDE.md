# autonomous-issue-agent

Headless harness that resolves GitHub issues end-to-end: issue → branch → TDD
implementation → PR → CI → independent review → merge to `dev`. Three Python
daemons (`fix`, `review`, `steward`) reconcile GitHub and drive Claude, Codex,
or GLM runtimes.

Start with:

- [architecture](docs/reference/architecture.md) — current design and authority;
- [operator manual](HARNESS.md) — setup, operation, recovery, live canary;
- [residuals](docs/open/residuals.md) — accepted risk and trigger-gated work;
- [documentation index](docs/README.md) — which other documents are current.

The numbered documents under `docs/archive/` describe the deleted bash
orchestration. Do not implement from them. `plugin/hooks/checks/` remains live
because the Python Codex runtime uses the shared gate stack.

## Direction and guardrails

The north star is lower cost per merged PR without weakening quality. An
efficiency change must name the metric it moves, the guardrail it must not move,
and the next measurement that adjudicates it.

Primary metrics, derived from schema-v2 events and the SQLite-backed spend
projection:

- cost per merged PR, split by pricing basis;
- cost mix by `Leg` (resolve/review/repair/router/triage);
- probe hit rate and patch-id re-review skips;
- predicted-path hit rate;
- issue-to-merge wall time.

Guardrails:

- first-pass approval rate and rounds per PR;
- escalation and infra-death rate;
- merges per deployment day for a comparable difficulty mix;
- recurrence rate (reopened or refiled defects after a harness merge).

Every incident adds a behavioral regression test. Fix the generator/class, not
only the observed instance. A temporary workaround names the issue or condition
that removes it.

## Architecture rules

- `domain/` is pure: no I/O, ambient environment, wall clock, or adapter/app
  imports.
- `adapters/` owns filesystem, process, network, SQLite, runtime, and dashboard
  transport I/O. It cannot import `app/`.
- `app/` is the composition/orchestration layer.
- Decisions return immutable `Action` values. Remote lifecycle mutations go
  through durable commands and effect receipts.
- SQLite is authority for admission, spend, leases, runs, deduplication,
  lifecycle commands, and receipts. JSONL is telemetry/projection input only.
- Tests assert public behavior and observable outputs. Business rules include
  malformed, absent, boundary, and huge inputs.
- Comments explain constraints or incident history, never narrate the next line.

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
- `config/repos/<name>.env` — repo, checkout, state dirs, test command, overrides;
- `config/.active.<name>.env` — generated launch snapshot.

`TEST_CMD` must terminate. Trusted execution scrubs harness controls, bounds
recursion and time, and kills the process group on timeout.

## Operational model

Each role performs:

```text
observe GitHub → pure decide → execute Actions → dispatch paid Legs
                         ↓
                  SQLite control store
                         ↓ committed outbox
                  schema-v2 event log → one fold → dashboard
```

Every paid leg receives a `run_id`/`leg_id`, reserves its maximum charge in one
transaction, then settles or is recovered. A configured cap plus unavailable,
corrupt, or unpriceable accounting blocks paid admission while observation and
reconciliation continue.

`SIGTERM` stops admission, cancels/drains active work within a bound, writes
handover/receipts, flushes the outbox, kills remaining groups, and exits with a
stable taxonomy. Boot and suspension recovery kill orphans, reclaim leases,
reconcile unsettled spend, drain commands reconcile-first, and emit abandoned
run corrections.

The governor is advisory. Current agent children still inherit publication
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
make test-hooks
make test-governor
```

`make test` must be green before any commit. The daemons hot-reload from this
checkout between ticks, so commit only coherent tested states.

Before a PR, include required evidence and run an unbiased adversarial review of
the complete diff in a subagent.

## Repository map

- `src/issue_agent/domain/` — value objects, policies, events, projection, ports.
- `src/issue_agent/adapters/` — GitHub/runtime/process/store/event/dashboard I/O.
- `src/issue_agent/app/` — config, roles, workflows, daemon loop, CLI.
- `plugin/` — advisory governor integration and live gate checks.
- `ops/` / `bin/` — service templates/installers and launch tooling.
- `tests/domain`, `tests/adapters`, `tests/app` — mirrored test tiers.
- `docs/adr/` — authority, money, events, supervision decisions.
- `docs/reference/` — current operational and architectural references.
