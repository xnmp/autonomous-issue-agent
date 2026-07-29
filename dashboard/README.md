# dashboard — interactive operator UI

Vite + React 18 + TypeScript SPA for the harness dashboard. It is a pure
frontend: every byte of data comes from the FastAPI backend documented in
[`docs/reference/dashboard-web.md`](../docs/reference/dashboard-web.md). The
backend's Pydantic/OpenAPI schema generates `src/api/generated.ts` plus runtime
Zod validators; `src/api/types.ts` contains only semantic aliases.

No UI framework, no CSS framework, no state library — hand-rolled components
over CSS custom properties, React context-free (plain hooks +
`useSyncExternalStore`).

## Build & run

```bash
npm ci            # or: npm install
npm run build     # tsc --noEmit && vite build  ->  dashboard/dist
npm run typecheck # tsc --noEmit on its own
npm test          # Vitest component, contract and navigation tests
npm run test:e2e  # Playwright browser workflow with API fixtures
npm run generate:api # refresh OpenAPI-derived TS and runtime validators
npm run dev       # vite dev server on http://localhost:5173
```

`npm run build` emits `dashboard/dist`, which the backend mounts as static
files (`/` and any non-`/api` path, SPA fallback to `index.html`). The
harness-level target is `make dashboard-ui`.

### Dev against a running backend

The dev server proxies `/api` to `http://127.0.0.1:8321`, so the same relative
URLs work in dev and in production:

```bash
# terminal 1 — the backend
issue-agent dashboard --serve 8321

# terminal 2 — the UI with HMR
cd dashboard && npm run dev      # http://localhost:5173
```

Point the proxy elsewhere with `DASHBOARD_API=http://127.0.0.1:9000 npm run dev`
(see `vite.config.ts`).

With no backend running, the app still loads: every panel renders its own error
banner ("cannot reach the dashboard backend…") and every list its empty state.
A blank screen is always a bug.

## Layout

```
src/
  main.tsx                 entry; mounts <App/> and imports the three stylesheets
  App.tsx                  shell: sidebar + tab strip + one mounted view
  api/
    generated.ts           OpenAPI-generated compile-time contracts
    generated-zod.ts       OpenAPI-generated runtime validators
    types.ts               semantic aliases over generated contracts
    client.ts              fetch wrapper: response validation, ApiError, abort
    hooks.ts               usePolled (abort + document.hidden pause), useClock, useLiveElapsed
  routing/
    hash.ts                Route union, parse/serialize, useRoute()
  lib/
    format.ts              null-tolerant $ / % / duration / relative-time formatters
    semantics.ts           domain value -> Tone; leg families; explicit kind labels
  features/issues/
    useIssues.ts           issues polling seam, independent of presentation
  components/
    Sidebar.tsx            repo rail (active/past), self-repo treatment, start-daemons modal
    UsageBlock.tsx         claude 5h/7d + codex weekly meters + harness $ vs caps
    TabStrip.tsx           tab strip + sub-tab pills
    OverviewTab.tsx        stat tiles, cost mix, caps, daemon health, unattributed events
    IssuesTab.tsx          in-flight / pending / done / backlog tables + actions
    IssueTab.tsx           per-issue header, run table, per-run detail panes
    EventTimeline.tsx      journal events as a readable timeline with raw JSON
    VerificationTab.tsx    docs/field-verification.md ledger
    ui/                    Chip, Modal, Feedback (banners/empty), Charts (meter,
                           stat tile, stacked bar, ETA progress), StepBadge, Json
  styles/
    tokens.css             the palette + semantic aliases — the ONLY place hexes live
    base.css               reset + primitives (panel, table, chip, button)
    app.css                layout and component styles
```

## Conventions

**Routing & tabs.** The URL hash is the single source of truth for what is on
screen: `#/repo/<name>`, `#/repo/<name>/issues/<inflight|done|backlog>`,
`#/repo/<name>/issue/<n>`, `#/verification`. Which closeable tabs *exist* is
separate React state in `App` (a closeable tab must outlive navigating away from
it), and visiting an issue route always adds its tab — so a pasted deep link
materialises the tab it names.

**Polling.** One poller per mounted view via `usePolled(key, fetcher, ms)`:
repos + usage 10 s, issues 5 s, issue detail 5 s, verification 60 s. It aborts
in-flight requests on unmount and on key change, pauses while `document.hidden`,
and keeps the last good data when a refresh fails (banner over stale numbers,
never a blank pane). Elapsed timers tick locally at 1 Hz off one shared clock,
anchored to the server's `elapsed_s` plus the age of that response.

**Colour.** Hex values appear only in `styles/tokens.css`. Components speak
*tones* (`ok | running | fail | info | meta | link | neutral`), so the contract's
semantic mapping — green = ok/done/approve, yellow = running/pending,
red = failed/escalated/blocked, blue = info/queued, magenta = steward/meta and
the self-repo, cyan = links/models — holds structurally rather than by
convention. The cost-mix categorical slots (blue / bright cyan / yellow /
bright magenta, in that fixed order, never re-sorted by size) were checked with
the dataviz palette validator against the panel surface: CVD separation,
normal-vision floor and 3:1 contrast all pass. The lightness-band and
chroma-floor checks fail by construction — a desaturated terminal theme cannot
satisfy them — so every chart that uses those slots also carries a legend and
direct labels as the secondary encoding.

**Dialogs.** `window.confirm` / `window.alert` are forbidden. Every destructive
or spend-triggering action (start daemons, promote) goes through the in-app
`ConfirmModal`.

**Motion.** Running legs pulse; `prefers-reduced-motion: reduce` disables the
animation and collapses transitions to 1 ms (`--t` in tokens.css).

## Known gap

- `PendingCard.overlap` is reserved by the contract and always `null`; the
  column renders a dimmed `n/a` with an explanatory tooltip.
