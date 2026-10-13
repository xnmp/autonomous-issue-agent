# Dashboard web app — contract & conventions

The dashboard is a separate, read-only consumer of the core
([minimal-harness design](../open/2026-10-12-minimal-harness-design.md)): a
FastAPI backend (`src/issue_agent/app/dashweb/`) serving a built React SPA
(`dashboard/`). It holds no authority, keeps no state of its own, and the core
never imports it (import-linter contract). `python -m issue_agent.app.dashweb.main`
serves on `127.0.0.1:8321`; `--text` / `--html PATH` render once.

## What it shows, and from where

| Concept | Source |
|---|---|
| Board: one row per harness item, worded by its derived state | `domain.state.derive` over the roles' observation (`GitHubGateway.build_world`) and the control store, per role (`app.tick.Facts`) |
| Needs you, with the stop reason | `NeedsHuman` / `needs-human` plus the harness stop comment |
| Legs: kind, runtime and model, outcome, cost and pricing basis, tokens | `legs` table (read-only connection); model from the role `journal.jsonl` `agent_run` event |
| Carried legs | `role='carried'` rows: one pre-cutover total per issue and one `carried` cost row, never legacy kind rows |
| Leases | `leases` table, joined to the bound leg |
| Caps | the store's committed snapshot (settled + reserved, UTC day) for the repository and globally, and each role's headroom — the reads `tick.headroom` makes |
| Role health | process table plus each role's `heartbeat.json` (time, scheduler state, in-flight dispatches) |
| Metrics | `legs` plus GitHub merged harness PRs (`fix/<issue>-` branches) |
| Quota meters | Claude OAuth usage, Codex rollout rate limits (dashboard-side, refactor decision Q19) |
| Verification ledger | `docs/field-verification.md` (the manager's ledger) |

Legacy files in the state dirs (`events-v2.jsonl*`, `pr-*.round`, `*.stall-sig`, …)
are not read.

## Rules

- **Writes are label changes only**: promote adds `ai-ready` to an open issue
  the last observation listed; unblock removes `needs-human` from the issue and
  its open harness PRs (`operator_effects`). Both return `{ok, detail}`, 400 on
  refusal. `test_the_only_writes_are_label_changes` pins the route list.
- **The store is opened read-only** (`adapters/dashweb/readonly_store.py`:
  `mode=ro`, `query_only`); `test_no_read_path_writes_the_control_store`
  fingerprints it across every read.
- **Read-only failure posture.** A collector failure yields empty data plus a
  `warnings` entry, never a 500. An unreadable store derives no board and says
  so (`store_error`); unknown values are `null`, never zero.
- **Loopback is not an authorization boundary.** One middleware: the `Host`
  header must name loopback (421), an `Origin` must be this server (403), and
  every non-GET carries `X-Issue-Agent-Dashboard` (403).
- **`name` is validated** against `config/repos/*.env` on every route.
- **fastapi/uvicorn live in the optional `dash` extra**; no daemon path imports
  `app.dashweb`.

## Contract

`app/dashweb/contracts.py` is the source of truth. `npm run generate:api`
exports it (`scripts/export_dashboard_openapi.py` → `dashboard/openapi.json`)
and regenerates `src/api/generated.ts` and the Zod validators every response
passes through. Endpoints: `GET /api/repos`, `/api/usage`, `/api/verification`,
`/api/repos/{name}/overview`, `/api/repos/{name}/issues`,
`/api/repos/{name}/issues/{n}`; `POST …/issues/{n}/promote|unblock`.

## Bundle

`make dashboard-ui` (`npm ci && npm run build`) publishes an immutable build
under `dashboard/dist/builds/<id>/` and points `dashboard/dist/current.json` at
it. The server serves only the pointed build, and only when its manifest's
source and output hashes match the checkout; otherwise it rebuilds once at
startup or serves build instructions. Files at the top of `dist/` (a
pre-pointer build) are never served.

## Testing

- Python: `tests/domain/test_dashboard.py`, `tests/app/test_dashweb_*.py`,
  `tests/adapters/test_dashweb_legs.py`, `tests/adapters/test_dashboard_bundle.py`.
- Frontend: `tsc --noEmit`, vitest (`npm test`), and Playwright
  (`npm run test:e2e`). The FastAPI spec runs the real `Backend` over a fixture
  world and control store (`dashboard/e2e/server.py`) and asserts derived-state
  text, leg rows, leases and caps.
