# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

The primary user is the human operator supervising one or more autonomous issue-agent deployments. They use the dashboard during live operation to find urgent state, blockers, health degradation, work in progress, and actions requiring human intervention without reconstructing lifecycle state from logs.

## Product Purpose

The dashboard is the operational surface for a headless harness that moves GitHub issues through triage, resolution, review, CI repair, and merge. Success means the operator can understand whether work is on rails, identify who or what owns the next move, inspect issue-level evidence, and intervene quickly when automation cannot proceed.

## Positioning

The product exposes the full autonomous issue lifecycle as a reconciled operational model: issue state, agent legs, retry rounds, verification, cost, and intervention history remain connected rather than appearing as unrelated CI, spend, and queue views.

## Operating Context

Operators use the dashboard as a live watch-and-intervene console alongside GitHub, daemon journals, and deployment controls. Urgent state, blockers, health, and active work dominate; trends and cost provide secondary diagnosis and efficiency context. The information density is intentional and supports repeated scanning across repositories, issue queues, and per-issue detail.

## Capabilities and Constraints

- Existing views cover deployment overview, active/done/backlog issues, per-issue task and event detail, verification, usage, and daemon controls.
- Canonical lifecycle stages are Triage, Resolve, Review, CI, and Merge, with inapplicable stages omitted and retries derived from delivered client payloads.
- The dashboard must preserve current payload contracts and features. Visual work stays in the client; Python, API contracts, snapshots, and services are outside this surface's scope.
- No network-hosted visual assets or runtime UI framework dependency is required.

## Brand Commitments

The product is named autonomous-issue-agent / Issue Agent in repository and operator-facing contexts. The dashboard is explicitly a sharp, modern, dark, data-dense operator console; its quality bar is contemporary Linear, Grafana, and Vercel operational software, without becoming a marketing surface.

## Evidence on Hand

- Existing production-shaped dashboard components and behavioral tests under `dashboard/src/`.
- Existing before/after dashboard captures and the lifecycle-node reference under `evidence-facelift/`.
- Real dashboard payload fixtures in the component and end-to-end tests.
- No customer claims, performance benchmarks, or marketing proof should be fabricated.

## Product Principles

- Put the next required human action ahead of historical analysis.
- Make lifecycle progress, ownership, retries, and failure visible at scan speed.
- Preserve dense access to evidence while revealing detail progressively.
- Keep semantic state vocabulary consistent across summary and detail views.
- Treat cost and trend data as diagnostic context, not the primary operating signal.

## Accessibility & Inclusion

Interactive state must remain keyboard-operable, visibly focused, and understandable without color alone. Motion should be brief and respect reduced-motion preferences.
