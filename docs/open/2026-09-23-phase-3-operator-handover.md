# Phase 3 operator boundary — checkpoint handover

This increment builds on `82fe5c7` on `dev`. The
[overall six-phase plan](2026-09-14-architecture-implementation.md) remains the
source of phase ordering: phases 1–2 accepted, phase 3 current, phases 4–6 gated.
The goal is active and incomplete. No live service, target checkout, GitHub
issue, paid agent, schema upgrade or family activation was operated.

## Implemented boundary

- Existing loopback dashboard now exposes resolve control GET and typed
  Request/Continue/Cancel POST. See [operator protocol](../../HARNESS.md#resolve-operator-api).
  HTTP carries exact expected repository/issue/family/epoch/generation/version/
  episode, stable IDs and explicit guidance variants. Server mints the local
  OS-account actor; request bodies cannot mint actors, receipts or workspace proof.
- Full binding is checked within existing deployment barriers and SQLite CAS.
  Canonical replay verifies the original epoch/episode/operator/content and
  returns historical request separately from current intent and holds. Accepted
  is durable intent, not completed recovery; existing daemon/executor completes it.
- Read-only coherent SQLite views distinguish inactive, absent, available and
  unavailable. GET never fetches GitHub/Git, schedules reconciliation, initializes,
  adopts, migrates or drains commands. `workflow_held` is explicitly only SQL holds,
  not remote eligibility. Missing epoch with retained authority, or missing target
  with retained rearm/gate/bound runs, is unavailable rather than safe absence.
  Retained-run probes use existing bounded indexed target-scope ranges.
- Legacy unblock requires witnessed current-schema authority and holds the same
  fix/review/steward exclusive barriers as activation for the whole operation.
  Active family refuses even for a missing target; absent/unwitnessed/corrupt/
  outdated control databases refuse without creation/adoption/migration. The
  legacy path remains only for verified inactive families, not as fallback.
- Both typed and legacy operator composition use the same single active-snapshot
  loader, returning validated config and immutable assigned-only environment.
  No overlay/per-field fallback is used for mutation. Legacy effects, counters,
  guidance, review baselines, credentials, policy and fresh admission provenance
  reuse captured scope. Journal cache
  includes repository and path, not just deployment name. A mid-action config
  change cannot redirect writes into another deployment's state files.
- Listener refuses nonliteral/non-loopback bindings. Existing Host/Origin/custom
  header protection remains; this trusts the local operator account and is not
  remote/multi-user authentication. Request counters fit exact browser JSON
  integers; malformed text/errors cannot break typed unavailable responses.
- Generated TypeScript and Zod contracts are refreshed. Export supplements JSON
  Schema `const` with equivalent one-value `enum` for the installed generator,
  preserving discriminated unions instead of crashing dashboard imports.
  No visual components or client action UX are implemented in this increment.

## Review and verification

Independent review found mixed overlay/active configuration, repository and
state-path configuration races plus
orphaned-target misclassification. Behavioral regressions reproduce these and
verify the fixes. Existing corrupt-epoch rejection preserves its typed error
contract. API tests caught NUL/surrogate failure-reporting problems; generated
client execution caught literal-schema loss. These are fixed at shared boundaries,
not by route-specific exception swallowing or generated-file hand edits.

Focused API suite: 72 cases; adapter fence/coherent-read suite: 20 cases;
legacy Backend suite: 11 cases. Existing 84 legacy-action tests remain green.
Dashboard generation/typecheck and 247 Vitest + 3 build tests pass
(`/tmp/phase3-operator-dashboard-verified.log`). First full suite passed at
`/tmp/phase3-operator-full-first.log`, but final review refinements overlapped
that run. Independent review accepted the final bounded increment after the
single-snapshot and scoped-provenance corrections. Final frozen-tree `make test`
passed at `/tmp/phase3-operator-full-verified.log`: 1,624 domain, 1,753 adapter,
2,577 application tests, hooks 123, gate/config checks, all four import contracts
and both Pyrefly checks (zero errors). The coherent checkpoint subject is
`feat: expose fenced resolve operator controls`. This accepts this bounded
backend increment, not phase 3 overall.
The intentional empty-database corruption fixture now checkpoints/truncates its
WAL first: otherwise a live connection could later restore the main file, making
that fixture nondeterministic rather than proving rejection of on-disk loss.

All four installed target services were rechecked inactive/dead, disabled,
MainPID=0. No service start or migration occurred.

Metric: fewer ambiguous operator interventions and duplicate re-arm grants.
Guardrails: allowance/accounting conservation, exact authority identity,
preservation of independent holds and displaced work, no implicit migration,
and no ordinary-path extra exclusive locks. Current evidence is isolated
composition/fault replay, not live cost or unattended-throughput measurement.

## Next work and preserved constraints

1. Build actual dashboard controls over these generated contracts: current vs
   historical intent, held/unavailable distinctions, stable retry IDs, conflict
   refresh, explicit guidance and pending/cancel/continue outcomes. Use the
   applicable frontend skills before visual work; test actual user outcomes.
2. Implement explicit offline import/activation and rollback rehearsal against
   isolated copies. Reuse exact barrier ordering; inventory/drain/map legacy
   unbound work and old-writer fencing. Do not implicitly activate from GET/POST.
3. Accept the complete resolve vertical slice before implementing/accepting the
   review-rejection gate family. Phase 3 is still incomplete; phase 4 cannot start.
4. Phases 4–6 remain as documented in the overall plan. Phase 5 live windows
   require explicit user release of the deployment pause.

Tauri decisions preserved: #696 blocked for interactive Mac qualification;
#523 branch-status badges approved; #514 Gravatar only explicit opt-in; #722 is
pasting an image from another app. #723 `trash:///` attachment-preparation gap
remains recorded, not fixed. Astra/Fable exceptional routing was already
committed as `eaa1cc5`. No target issue was closed or posted during this work.
