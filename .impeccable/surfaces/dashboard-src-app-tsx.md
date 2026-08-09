---
version: 1
slug: "dashboard-src-app-tsx"
primary_target: "dashboard/src/App.tsx"
related_targets: ["dashboard/src/components/Sidebar.tsx","dashboard/src/components/OverviewTab.tsx","dashboard/src/components/IssuesTab.tsx","dashboard/src/components/IssueTab.tsx","dashboard/src/styles/tokens.css","dashboard/src/styles/base.css","dashboard/src/styles/app.css"]
---

## Scope and mode

Operate mode across the React dashboard shell, overview, issue queues, and issue detail. The operator repeatedly scans for live health, blocked work, and the next intervention, then drills into execution evidence.

## Direction

Version-control topology console: calm graphite/navy elevations, a restrained periwinkle route line, crisp semantic state, and compact typographic hierarchy. Lifecycle paths are the signature. The approved composition is `.impeccable/mocks/dashboard-topology-board.png`, combined with the evidence-ledger detail structure from `.impeccable/mocks/dashboard-evidence-ledger.png`.

## Memorable moment

Every active issue reads as a small execution graph. A glance reveals completed, active, waiting, failed, and future phases; review and CI circle-backs show retry counts as part of the path rather than buried in text.

## Implementation inventory

| Visible ingredient | Fidelity commitment | Medium |
|---|---|---|
| 248px deployment rail with product mark, grouped repos, usage and verification | Distinct shell elevation, icon-led controls, selected lane, persistent compact density | Semantic React + CSS + inline SVG |
| Slim repository context header and tab strip | Strong repo identity, health/live cue, styled closeable tabs | Semantic React + CSS |
| Alert and warning surfaces | Full-width but compact, semantic icon, title/body/action hierarchy | Existing state + React/CSS |
| Lifecycle topology | Connected nodes, labels, state glyphs, active halo, retry arcs/counts, hover/focus detail | React + inline SVG/CSS |
| KPI row | Large tabular numeral, tracked label, subordinate context/delta, semantic edge cue | Existing React + CSS |
| Dense tables and issue lanes | Sticky headers, 40–44px rows, subtle separators/hover, chips, expanded evidence | Existing React + CSS |
| Issue detail execution ledger | Full lifecycle panel first, compact summary, execution rows and events below | Existing React + CSS |
| Icons | One 16px stroke vocabulary with consistent caps and joins | Local inline SVG components |
| Loading/empty/failure states | Purpose-built panels, restrained skeleton sweep, clear recovery action | Existing React + CSS |

## Constraints

Preserve all existing content, behavior, API contracts, client-side column controls, expansion, and route semantics. Dark theme only; no runtime dependencies or network assets. Keep motion to 120–150ms interaction transitions and one reduced-motion-safe active-state pulse.

## Unresolved decisions

None. The operator delegated remaining visual decisions and fixed the live watch-and-intervene posture.
