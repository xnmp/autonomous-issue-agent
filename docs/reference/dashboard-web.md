# Dashboard web app — API contract & conventions

The interactive dashboard is a FastAPI backend (`src/issue_agent/app/dashweb/`)
serving a built React SPA (`dashboard/`). It supersedes the `--serve` mode of
the v1 text/HTML dashboard; `issue-agent dashboard` (text) and `--html` are
unchanged and stay journal-only.

Design rules:

- **Journal stays the truth for costs/stages/legs.** Aggregation and lifecycle
  decisions live in the pure `domain/dashboard.py`; journal, process, GitHub,
  usage and command I/O live under `adapters/dashweb/`. The application layer
  only composes ports and projects domain snapshots into response payloads.
- **GitHub reads are allowed, cached.** A small `gh`-CLI adapter with a
  per-(repo, query) TTL cache (default 45 s). Every gh call is read-only except
  the three explicit POST actions below. gh failures degrade: the UI renders
  journal-only data plus a "GitHub data unavailable" chip, never a 500.
- **Loopback is not an authorization boundary.** Binding 127.0.0.1 stops other
  hosts from connecting; it does not stop the operator's own browser from being
  pointed here by a page they visit. Three guards, all in one middleware:
  the `Host` header must name loopback (DNS-rebinding guard, else 421); an
  `Origin`, when sent, must be this server (else 403); and every non-GET
  request must carry `X-Issue-Agent-Dashboard` — a header a cross-site form
  cannot set and a cross-site fetch cannot send without a preflight that no
  CORS middleware will approve (else 403).
- **`name` is validated against `deployment_names()` on every route.** It
  reaches subprocess argv and filesystem paths. Unknown → 404 on reads, refusal
  on writes, before any collector runs. `start` invokes `bin/point-at.sh`
  directly and never `make`: make expands a command-line variable into the
  recipe text it hands to `/bin/sh`, so a quote in `name` became a shell
  command (found by adversarial review, verified as RCE, now regression-tested).
- **The cost-chart iframe is sandboxed** (`sandbox=""` + `Content-Security-
  Policy: sandbox`). Its HTML is generated from transcript text the agent read
  from issues, PRs and the web — untrusted input rendered on the dashboard's
  own origin.
- **Zero new runtime deps for the core.** fastapi/uvicorn live in a new
  optional extra `dash`. `import issue_agent.app.dashweb` must not be required
  by any daemon path; the import-linter contracts must keep passing.
- **Read-only failure posture.** Any collector error (missing file, bad JSON,
  gh down, no codex install) yields `null`/empty + a `warnings: [str]` field on
  the payload, never an exception to the client.

## Repo/di­rectory layout

```
src/issue_agent/domain/dashboard.py       # pure lifecycle model and aggregation
src/issue_agent/adapters/dashweb/
  system.py        # journal, /proc and git-origin readers
  ghdata.py        # cached read-only gh reads
  usage.py         # claude/codex usage and harness caps
  verification.py  # verification ledger parser
  costchart.py     # transcript chart process adapter
  actions.py       # daemon and label command adapters
src/issue_agent/app/dashweb/
  ports.py         # application-facing Protocols
  contracts.py     # Pydantic HTTP contracts / OpenAPI source
  service.py       # use-case orchestration and adapter composition
  snapshots.py     # pure API projections from domain snapshots
  server.py        # thin FastAPI transport and static SPA delivery
dashboard/         # Vite + React + TS; `npm run build` → dashboard/dist (gitignored)
```

`scripts/export_dashboard_openapi.py` exports the server schema. The frontend
runs `npm run generate:api` to regenerate TypeScript and Zod contracts; every
successful response is validated before reaching a component.

CLI: **serving is the default mode** — a bare `issue-agent dashboard` binds
`127.0.0.1:8321` (`cli.DASHBOARD_PORT`, the same target `dashboard/vite.config.ts`
proxies to). `--serve [PORT]` overrides the port; `--text` and `--html PATH`
keep the one-shot renders and take precedence over serving. dashweb is preferred
when fastapi is importable AND `dashboard/dist/index.html` exists; otherwise it
falls back to the v1 single-page server with a one-line hint (`uv sync --extra
dash && make dashboard-ui`). `make dashboard-ui` runs `npm ci && npm run build`
in `dashboard/`.

## Identifiers

- `name` = deployment name = `config/repos/<name>.env` stem.
- Issues are target-repo issue numbers. PRs are PR numbers.
- All timestamps ISO-8601 UTC strings; all costs USD floats; `estimated: true`
  marks synthetic (subscription-runtime) dollars.

## Endpoints

### GET /api/repos

```json
{ "repos": [{
    "name": "xnmp-keifu", "repo": "xnmp/keifu", "target_dir": "/path",
    "is_self": false,            // REPO matches the harness's own origin repo
    "active": true,              // bool from process scan; null when unavailable
    "roles": [{ "role": "fix", "alive": true, "stale": false,
                "last_event_ts": "...", "last_event": "agent_run",
                "events": 123, "log_path": "..." }],
    "frecency": 0.87,            // sort key, computed server-side; list arrives pre-sorted
    "last_activity_ts": "...",
    "in_flight": 2, "done": 5    // quick badge counts (journal-derived)
  }],
  "warnings": [] }
```

Liveness: scan `/proc/*/environ` for `AGENT_ENV=` ending in
`.active.<name>.env` (Linux-only). If the scan is unavailable, return
`active: null`, `roles[].alive: null`, and `"liveness": "unknown"`; event and
heartbeat freshness remain activity/health signals and are never promoted to
process-liveness authority.
Frecency = recency-weighted event count over role journals: score
`Σ events_in_window / age_weight` — implement as
`3*events(24h) + events(7d) + 0.25*events(30d)`, normalised to the max; ties
by `last_activity_ts`.

### GET /api/usage

```json
{ "claude": { "five_hour_pct": 12.0, "five_hour_resets_at": "...",
              "seven_day_pct": 43.0, "seven_day_resets_at": "...",
              "source": "oauth", "error": null },
  "codex":  { "weekly_pct": 0.0, "window_minutes": 10080,
              "resets_at": "...", "plan": "plus",
              "source": "rollout", "sampled_at": "...", "error": null },
  "harness": { "global_daily_cap_usd": null, "today_all_repos_usd": 3.2,
               "per_repo": [{ "name": "...", "today_usd": 1.1,
                              "daily_cap_usd": 20.0 }] },
  "warnings": [] }
```

- Claude: token from `~/.claude/.credentials.json` → `claudeAiOauth.accessToken`
  → `GET https://api.anthropic.com/api/oauth/usage` with
  `Authorization: Bearer <token>` and `anthropic-beta: oauth-2025-04-20`
  (stdlib urllib, 5 s timeout, 60 s cache). Parse defensively: accept both
  `five_hour`/`seven_day` objects with `utilization` (0-100) and any
  `*_pct` variants; unknown shape → `error` string, pcts null. NEVER log or
  return the token; NEVER send it anywhere but api.anthropic.com.
- Codex: newest `~/.codex/sessions/**/rollout-*.jsonl` by mtime; scan from the
  end for the last `"rate_limits"` object; report
  `primary.used_percent`, `primary.window_minutes`, `primary.resets_at`
  (epoch → ISO), plan from the same object. Missing → error string.
- Harness: reuse the cap-reading helpers in `app/cli.py` and ledger sums from
  `journal.py`.

### GET /api/verification

Parses `docs/field-verification.md` tables → `{ "rows": [{ "id", "change",
"shipped", "status" ("VERIFIED"|"MISBEHAVED"|"pending"|...), "evidence",
"reason": "…"|null, "raw": "…" }], "warnings": [] }`. Parser must survive column
drift: treat the first table column as id/change, map a case-insensitive
VERIFIED/MISBEHAVED/pending match anywhere in the row to `status`, and keep the
raw row text.

`reason` is the verdict prose with the leading verdict keyword stripped
("VERIFIED — probe stayed green" → "probe stayed green"), falling back to the
evidence column when the verdict cell says nothing beyond the keyword, and
`null` when the row offers no prose at all. It is the hover text behind the
verified/misbehaved badge; the badge itself still comes from `status`.

### GET /api/repos/{name}/overview

`Metrics` from dashboard.py verbatim, plus today's spend, caps, and cost-mix by
run family:

```json
{ "metrics": { "done_issues": 5, "in_flight_issues": 2, "total_cost_usd": ...,
   "cost_per_done_usd": ..., "first_pass_approvals": ..., "first_pass_changes": ...,
   "first_pass_rate": 0.6, "rounds_per_pr": 1.4, "escalations": 0,
   "recurrences": 0, "spend_blocks": 0, "today_usd": ...,
   "by_kind": [{ "kind": "RESOLVE", "usd": 13.2, "runs": 4 }] },
  "cost_mix": { "resolve_usd": ..., "review_usd": ..., "repair_usd": ...,
                "steward_usd": ... },
  "caps": { "issue_cap_usd": ..., "daily_cap_usd": ..., "today_usd": ... },
  "health": [ ...same role objects as /api/repos... ],
  "health_trend": [{ "day": "2026-07-29", "opened": 3, "closed": 1,
                     "net_open": 2, "recurrences": 0, "first_observation": true }],
  "spend_per_hour": [{ "hour_start_iso": "2026-07-29T11:00:00+00:00", "usd": 1.2 }],
  "unattributed": [ last 20 deployment-level events ],
  "warnings": [] }
```

Repair family = FIX_CI + ADDRESS_REVIEW + REBASE; steward = TRIAGE +
STEWARD_DRIVE + JIRA_SYNC; review = REVIEW + RE_REVIEW; resolve = the rest.

`health_trend` is advisory per browser-native local day: the SPA sends its IANA
timezone on the overview request, then GitHub issue `createdAt`/`closedAt`
provide opened/closed counts for the selected target repository, while journal
`recurrence` events provide the recurrence count. `first_observation` identifies
the selected deployment's first journalled day. Runtime and review-mode history
is not journalled, so the dashboard deliberately does not infer historical
configuration classes from the deployment's current configuration.
If GitHub is unavailable, lifecycle counts are absent and the panel still shows
journal recurrences with a warning. The reader paginates up to 1,000 issues;
reaching that bound emits a warning so a potentially incomplete long-run trend
is never presented as complete.

`spend_per_hour` is a dense 48-bucket UTC-hour series ending at the current
hour, oldest first, journal-only: every settled `agent_run` in the fold — issue
attributed legs plus deployment-level ones — bucketed by its settlement stamp.
Estimated (subscription) dollars count; unpriceable legs are skipped rather
than coerced, so the series and `metrics.by_kind` share one basis. Empty hours
are emitted as `0.0` so a client cannot re-space a quiet night into a busy one.
Charges outside the window, including future-stamped ones, are dropped, never
clamped. control.db is not read: this is the same journal projection as every
other number on the page.

### GET /api/repos/{name}/issues

One call returns all four buckets (the UI splits sub-tabs client-side):

```json
{ "in_flight": [IssueCard...], "pending": [PendingCard...],
  "done": [IssueCard...], "backlog": [BacklogCard...],
  "gh_ok": true, "warnings": [] }
```

`IssueCard` (journal-derived, gh-augmented when available):

```json
{ "issue": 7, "title": "…", "url": "https://github.com/o/r/issues/7",
  "pr": 12, "pr_url": "…", "pr_state": "open|merged|closed|null",
  "pr_review_state": "mergeable|changes_requested|null",
  "stage": "reviewing", "done": false,
  "issue_verification": "VERIFIED|MISBEHAVED|pending|null",
  "pr_verification": "…|null",
  "issue_verification_reason": "…|null", "pr_verification_reason": "…|null",
  "steps": [{ "kind": "RESOLVE", "state": "ok|failed|running",
              "status": "APPROVE|REQUEST_CHANGES|…", "cost_usd": 1.2 }],
  "cost_usd": 4.2, "estimated": false,
  "projected_cost_usd": 17.0,        // repo mean $/done issue; null if no dones
  "started_ts": "…", "last_ts": "…",
  "elapsed_s": 812,                   // server-computed; client ticks it live
  "projected_eta_s": 5400,            // mean done-issue wall clock; null if none
  "models": ["claude-sonnet-5"], "rounds": 1,
  "flags": ["escalated", "recurred", "spend-blocked", "held"] }
```

`pr_review_state` is merge readiness for the card's newest PR, from the same
cached `gh pr list` query (`reviewDecision`, `mergeable` — no extra gh call):
`changes_requested` when review asked for changes, `mergeable` when review
approved AND GitHub reports the branch mergeable, otherwise `null`. A PAT
without the scopes for those fields, or a mergeability answer still computing,
degrades to `null`; claiming mergeable on absent data is the one wrong answer.

`issue_verification`/`pr_verification` are the ledger verdicts for the card's
issue and newest PR; `*_verification_reason` carry the matching `reason` prose
from `/api/verification` for the badge's hover text, `null` when the ledger
gives none.

`steps` = the issue's settled legs in ts order collapsed by kind (latest state
per kind wins) plus any running leg last. Title/urls come from gh; when gh is
down they are null and the UI shows the number only. `models` reads both start
and settlement receipts, excluding the routing classifier, so configured models
remain visible while a task is live and when an older settlement omitted them.

`PendingCard` (gh: open issues labeled `ai-ready`, minus in-flight ones):

```json
{ "issue": 9, "title": "…", "url": "…", "priority": "P1|P2|P3|null",
  "labels": ["ai-ready", "P2"], "held": false, "hold_reason": null,
  "overlap": null }   // reserved: steward overlap prediction not built yet
```

Under schema v2, bucket membership and labels come only from the current
`world_facts` projection. `present=false` subjects and subjects removed by feed
retirement produce no card, even when historical run events remain. A current
open issue with no lifecycle label is backlog; `ai-ready` is pending; owned
lifecycle states are in flight; closed/`ai-done` issues are done. GitHub reads
may enrich presentational fields only — title, URL, and for PRs `state`,
`mergedAt`, `reviewDecision`, `mergeable` — and cannot move a card between
buckets. This keeps an
unavailable or differently timed dashboard GitHub read from overriding daemon
reality.

`held`/`hold_reason` from the newest journal `hold` event for that issue.

`BacklogCard` (gh: open issues with NONE of ai-ready/ai-working/ai-done/
ai-blocked/needs-human-review):

```json
{ "issue": 3, "title": "…", "url": "…", "body": "…", "labels": [...],
  "recurrence": false, "recurrence_count": 0,
  "age_days": 12, "comments": 4 }
```

`done` cards additionally carry `merged_ts` when gh knows it.

### GET /api/repos/{name}/issues/{n}

```json
{ "issue": 7, "title": "…", "url": "…", "stage": "…", "done": false,
  "prs": [12],
  "pull_requests": [{ "number": 12, "url": "https://github.com/o/r/pull/12",
                      "state": "open|merged|closed|null" }],
  "cost_usd": ..., "elapsed_s": ...,
  "steps": [{
     "idx": 0, "kind": "RESOLVE", "role": "fix", "ts": "…",
     "state": "ok|failed|running", "status": "…", "exit_reason": "…",
     "duration_s": 620.0, "cost_usd": 1.9, "estimated": false,
     "model": "…", "session_id": "…", "turns": 42,
     "usage": { "input_tokens": ..., "output_tokens": ...,
                "cache_read_input_tokens": ..., "cache_creation_input_tokens": ... },
     "has_transcript": true }],
  "events": [ full drill-down event list, ts order ],
  "warnings": [] }
```

Tasks here are NOT collapsed: each row represents one agent invocation
(`agent_run` or an unsettled `agent_run_start`) in timestamp order. The wire
contract retains the established `steps` field for compatibility; the UI calls
these rows “Tasks”.

### GET /api/repos/{name}/issues/{n}/steps/{idx}/events

Journal events for that step's window: everything with the same issue between
this leg's `agent_run_start` and its settlement (or now), plus the start/settle
events themselves. `{ "events": [...], "warnings": [] }`.

### GET /api/repos/{name}/issues/{n}/cost-chart?session_id=…

Locates the transcript `~/.claude/projects/*/<session_id>.jsonl` (glob), runs
`SESSION_COST_OPEN=0 TMPDIR=<private tmpdir> python
~/.claude/scripts/session_cost.py <transcript>` (script path overridable via
`CC_COST_SCRIPT`; 120 s timeout), returns the generated HTML with
`Content-Type: text/html` for the UI to iframe. Missing script/transcript →
404 with a JSON `detail` the UI shows inline. `session_id` must match
`^[A-Za-z0-9_-]+$` (path-safety).

### POST actions

All return `{ "ok": bool, "detail": str }`, 400 on validation failure. Every
action shells out as the operator; nothing writes harness state directly.

- `POST /api/repos/{name}/start` — refuse if already active (liveness check).
  Launch `bin/point-at.sh` detached:
  `setsid env REPO=<name> make -C <harness_root> point-at REPO=<name>` with
  stdout/err appended to `<LOG_DIR>/dashboard-start-<name>.log`; return the
  log path in `detail`. (Uses the same env-compose path as the CLI; no
  launch-logic reimplementation.)
- `POST /api/repos/{name}/issues/{n}/promote` — body `{}`. Runs
  `gh issue edit <n> -R <repo> --add-label ai-ready`.
- `POST /api/repos/{name}/issues/{n}/priority` — body
  `{ "priority": "P1"|"P2"|"P3"|null }`. Removes the other P-labels, adds the
  requested one (null = remove all). Labels may not exist on the target repo:
  create-on-miss with `gh label create` (idempotent, ignore exists-errors).

### GET / and static

`/` and any non-`/api` path serve `dashboard/dist` (SPA fallback to
index.html). If dist is missing, `/` returns a plain-HTML instruction page.

## Frontend conventions

- Vite + React 18 + TypeScript, no UI framework — hand-rolled components +
  CSS custom properties. State: React context + `useSyncExternalStore` or
  plain hooks; no redux. Data fetching: a single poller per mounted view
  (repos+usage every 10 s; open repo's issues every 5 s; issue detail every
  5 s), with `document.hidden` pausing. Elapsed timers tick client-side at 1 s
  from `elapsed_s` + local monotonic offset.
- Tabs: sidebar selects a repo; top-level tabs `Overview | Issues`; Issues
  sub-tabs `In flight & pending | Done | Backlog`. Clicking an in-flight/done
  row opens a closeable issue tab in the tab strip (title `#N <stage-badge>`,
  updates live). A dedicated sidebar button opens the `Verification` tab
  (harness issues table). Tab state lives in the URL hash
  (`#/repo/<name>/issue/<n>`) so reload restores it.
- Theme — wezterm "Cosmic Dusk", exposed as CSS custom properties:
  bg `#0e1330`, panel `#141a3d` (bg lightened), fg `#d8dce8`,
  muted `#8a91b4`, selection `#2a3060`,
  red `#d4607a`, green `#69db7c`, yellow `#fbbf24`, blue `#6a7acc`,
  magenta `#b09ac0`, cyan `#7aadcc`, brights `#e87898 #7eeea0 #ffd43b
  #7a8ae0 #c4b0d8 #8ac0e0`. Dark theme only (it is a terminal theme).
  Monospace numerals (`font-variant-numeric: tabular-nums`) for all metrics.
- Semantic color mapping (keep consistent everywhere): green = ok/done/approve,
  yellow = running/pending/attention, red = failed/escalated/blocked,
  blue = informational/queued, magenta = steward/meta, cyan = links/models.
  The self-repo sidebar entry gets a magenta left border + prominent
  `issue-agent` chip.
- Every list renders an explicit empty state ("no issues in flight") — never a
  blank pane. Every gh-degraded view shows the "GitHub data unavailable" chip.
- Status badges for steps: kind abbreviation (RES, REV, CI, ADR, REB, TRI) in
  the semantic color of its state; running steps pulse (CSS animation,
  `prefers-reduced-motion` honored).

## Testing

- Backend: pytest under `tests/app/test_dashweb_*.py` — pure logic (frecency,
  verification parser, codex rollout parser, steps assembly, action command
  construction) tested directly with fixture files; endpoint shape tests via
  `fastapi.testclient` (guard with `pytest.importorskip("fastapi")`).
  No network in tests: the claude usage fetcher takes an injectable
  `fetch: Callable` and tests stub it.
- Frontend: `npm run build` + `tsc --noEmit` must pass; no JS test runner in
  v1 (vitest can come later).
- `make test` must stay green without the `dash` extra installed.
