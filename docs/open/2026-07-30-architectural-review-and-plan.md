# Architectural review and phased implementation plan — 2026-07-30

Synthesis of five parallel deep reviews (budget/pricing seam, governor/gate
trust model, journal/event schema, process supervision, cross-cutting) run
against `dev` @ `16c6783`, **integrated with the independent codex review**
([2026-07-30-architectural-hardening-plan.md](2026-07-30-architectural-hardening-plan.md)).
Every claim below was verified in code by a reviewer; several were verified
empirically (live governor probes, live journal/ledger data — 9 journals). The
original review also stated that 303 rows reconciled to $1512.55, but retained
no source digest; that sentence is not migration evidence. The immutable
migration receipt now records the captured 303-row source
(`ffc0cfbd…`, 91,060 bytes) at $3013.659291 after per-row rounding, plus
per-scope balance, admission, deduplication, and failure comparisons. The two
sharpest codex-only claims were independently re-verified in code (X1, X3
below).

**Status: IMPLEMENTED AND VERIFIED 2026-07-31.** Phases 0–7 are present on
the working tree, with the deliberately trigger-gated credential-free
publisher in 5.7 remaining gated exactly as this plan specifies. The live
authorities are SQLite control state, durable lifecycle commands/receipts,
schema-v2 events, and the single projection fold; the bash watchers and their
suite are deleted.
Verification includes `make test`, dashboard unit/type/build/E2E checks, a
bounded live steward tick against `xnmp/autonomous-issue-agent`, and the live
probes documented in `HARNESS.md`/`docs/reference/capacity.md`. The immutable
[live release canary evidence](../reference/live-release-canary-evidence.json)
records the all-role boot, an active SIGTERM shutdown, authoritative Codex
usage settlement, durable remote cleanup, and zero residual control state.
The final repoint audit also produced schema v15: foreground snapshot
replacement now quiesces the stable deployment before mutation, while durable
retired-owner rows fence any missed or rebooted prior process from reacquiring
control-store authority.

This document remains the **record** for the maintainability refactor: it owns
sequencing, scope, and the adjudicated design decisions. The codex plan remains
a **reference annex** for
the detailed contract sketches (its §7), the money-migration mechanics (its
§11.4, §18), the proposal-to-publish protocol (its §12), and the
systemd/launchd hardening checklists (its §14). Where the two documents
disagreed, §3.9 records the adjudication — do not re-litigate from the annex.

Each phase states the metric it moves and the guardrail it must hold, per the
efficiency-PR rule in CLAUDE.md.

---

## 1. The one-sentence diagnosis

Every review independently converged on the same shape: **the codebase's best
designs were each discovered from a real incident and implemented exactly once,
in the place the incident happened — and never promoted to the seam they
belong to.** `handover.py`'s untrusted encoding, `test_gate_parity.py`'s twin
control, `OpenPrLookup`'s tri-state, the codex killpg teardown, the
write-before-act markers, `_dc_default`'s drift-proofing — all correct, all
local. The refactor below is almost entirely *generalization of patterns the
repo already contains*, not invention. That is also why it is tractable.

Second-order diagnosis: **exactly one of ~six adapter seams has an explicit
port** (`AgentRuntime`); the rest are duck-typed `object`. Everything else the
reviews found — 17 defensive `getattr` shims in production code, five
hand-rolled `FakeGateway`s, `== "codex"` capability checks, 2,878 lines of
twin runner code with no parity suite, the fold duplicated eleven times —
is downstream of that omission.

The codex review adds a third, complementary diagnosis: **several operational
seams use best-effort files, agent-local hooks, and free-form events as if
they were authoritative state.** Four concepts that must not share failure
semantics currently do: policy decisions, operational state (reservations,
runs, dedup, commands), privileged effects, and telemetry. Its sharpest
formulation, adopted wholesale here: *telemetry reports what happened but is
never the sole authority for safety, accounting, or deduplication.*

## 2. Findings inventory (condensed)

Severity: **P0** = design flaw with active or imminent incorrect behavior;
**P1** = significant structural debt; **P2** = improvement. Full evidence lives
in the review transcripts; every item here carries its anchor file so it can be
re-verified.

### 2.1 Process supervision (worst seam — two P0s)

| # | Sev | Finding | Anchor |
|---|---|---|---|
| S1 | P0 | **No SIGTERM handler.** `issue-agent stop`, `systemctl restart`, and point-at's TERM trap kill the interpreter with no teardown: no dispatch cancellation, no journal receipt, no handover, no lease release. Only Ctrl-C (KeyboardInterrupt → `BaseException` arm) tears down cleanly. | `app/main.py:307-378`, `app/loop.py:445-447` |
| S2 | P0 | **Stop orphans live codex agent trees.** `start_new_session=True` puts codex in its own session; external kills (`kill 0`, cgroup TERM) never reach it, and the in-Python kill path doesn't run on SIGTERM (S1). Restarted daemon sees no in-memory lease → dispatches a second agent into the same worktree. | `adapters/codex_runner.py:1334`, `bin/point-at.sh:424` |
| S3 | P1 | Crash recovery is 8 per-incident mechanisms; **nothing runs at boot** (no orphan kill, no worktree GC, no receipt verification; only the suspend path reconciles). | `app/execute.py:467-541`, `app/loop.py:414` |
| S4 | P1 | Dispatch leases in-memory only (`DispatchPathReservations`); mutual exclusion does not survive restart. | `app/roles.py:167-220` |
| S5 | P1 | "Exactly-once" is emergent from ~7 marker conventions in 3 storage media; no `Claim`/`Receipt` vocabulary, no per-`AgentKind` classification. | `app/execute.py:192-236,543-621` |
| S6 | P1 | Claude-path teardown kills only the CLI pid — a TEST_CMD-spawned dev server survives (the #310 class, fixed for codex only). | `adapters/agent_runner.py:362-371` |
| S7 | P1 | Five clock sources; wall-vs-monotonic suspension math implemented three times (loop, journal, dashboard), no agreement test. | `app/loop.py:326-375`, `adapters/journal.py:754-800` |
| S8 | P1 | Liveness is a log line; `pool.shutdown(wait=True)` is unbounded; no watchdog/heartbeat file. | `app/loop.py:219,419-421` |
| S9 | P2 | **Dead code: the #501 error-forensics fix sits after `return` and never runs.** | `adapters/agent_runner.py:837-846` |
| S10 | P2 | `/tmp` flock keyed `(role, repo)` only — same-repo two-checkout deployments collide; foreign lock file crashes boot; webhook forwarder unlocked and `gc_dangling_hooks` decapitates a second forwarder. | `adapters/single_instance.py:51-82`, `app/webhook.py:47-76` |
| S11 | P2 | Handover written only for budget/wall deaths that *returned*; lost on SIGKILL/cancel; stored in the worktree that gets deleted. | `adapters/handover.py:51-59,116` |
| S12 | P2 | Suspension replay can overwrite newer state written by a concurrent dispatch (no causality check). | `app/execute.py:473-541` |

### 2.2 Journal / event schema (the event-bus seam)

| # | Sev | Finding | Anchor |
|---|---|---|---|
| J1 | P0 | **No event schema.** `emit(event, **fields)` at 30 emit sites *is* the wire format: no version, no ids, no registry, `default=str` degrades type errors to reprs. 13 event types have zero consumers; `hold` has 7 consumers and zero producers. | `adapters/journal.py:80-90` |
| J2 | P0 | **The fold is reimplemented per consumer** — 11 duplications, incl. `unsettled_start_events` verbatim (breaker copy vs dashboard copy), billed-cost rule authored twice with *divergent* `estimated` predicates, divergent repo guards, divergent `hold`-key coercion. Hexagonal contract forbids the dashboard importing the journal reader, so copies were the path of least resistance. | `adapters/journal.py:597` vs `domain/dashboard.py:703` |
| J3 | P0 | **Lifecycle actions journalled as English prose, parsed back with six regexes.** Structured payload exists but only for 4 durable kinds (84/1135 live rows). A new verb silently drops events from the dashboard. (#85) | `app/execute.py:129-159`, `domain/dashboard.py:37-99` |
| J4 | P0 | **Producer↔consumer contracts untested.** Zero tests span emit→fold; 223 hand-rolled event dict literals in 19 test files; two golden assertions total. | `tests/` (audit in review) |
| J5 | P1 | No leg identity: joins by 1-second timestamp equality (15-21% of live events collide); `_model_for_leg` guesses; multi-leg reviews indistinguishable; wall-minus-monotonic "clock identity" is 4 stacked heuristics standing in for a `stall_id`. | `dashweb/snapshots.py:215-268,341-411` |
| J6 | P1 | Cross-journal order = 1s timestamp + file-concatenation tiebreak; every "newest wins" rule depends on it. | `domain/dashboard.py:613-621` |
| J7 | P1 | Corrections don't reference ids (`agent_run_stall` dedups by clustering, double-counts by construction); the correction most needed — a terminal event for a dead leg — doesn't exist (#116: phantom "running" forever). | `app/loop.py:405`, `snapshots.py:451-459` |
| J8 | P1 | Schema evolution = 11 permanent per-field compat branches; a renamed field fails silently everywhere ($0 costs, misattributed roles). | inventory in review |
| J9 | P1 | No rotation anywhere; 1 MiB dedup tail window re-fires level alerts once exceeded; `sum_today` full-reads the global never-rotated ledger **on every tick and every dispatch**; dashweb re-reads everything per 5-10s HTTP poll; atomic-append claim is size-conditional (buffered writer splits >8KiB lines; comment bodies now journalled). | `adapters/journal.py:99-110,718`, `app/spend_gate.py:106-109` |
| J10 | P2 | System-of-record blur: journal already acts as WAL + dedup store + breaker input with none of an event store's guarantees; store.py state invisible to dashboard. | table in review |

**Dashboard drift mechanisms identified (D1-D10):** phantom running legs (no
abandon event), level events without clears, stage as regex-over-prose
heuristic, step-strip inventing review loops (#143), backlog misclassification
(#144), nondeterministic newest-wins, **the journal records intent while the
dashboard needs GitHub reality** (fetched by a second differently-timed reader
with 45s/3s TTLs — the deepest source, D7/D8), liveness inference, and
subscription-runtime $0 blindness. All five open dashboard issues (#85 #96
#116 #143 #144) are fold-vs-source defects.

### 2.3 Budget / pricing

| # | Sev | Finding | Anchor |
|---|---|---|---|
| B1 | P1 | No pricing domain: rate tables/pricing arithmetic live in the codex adapter; a second learned $/token model in journal.py; a third implicit (SDK envelope). New runtime touches ~6 modules with nothing forcing consistency. | `adapters/codex_runner.py:482-740` |
| B2 | P1 | Ledger sums incommensurable dollars (billed vs synthetic vs GLM-unknown) under one `cost_usd`; no `basis`/`currency`/`schema` field; caps and the $/merge north star mix currencies. | `adapters/journal.py:501-507` |
| B3 | P1 | **Agent-controllable stderr can reach the ledger and budget verdicts** at settle (rollout-miss fallback; plausibility guard inert after 120s). | `adapters/codex_runner.py:1771-1827,1401-1423` |
| B4 | P1 | Token-convention mismatch: codex cached-input ⊂ input, Anthropic disjoint; `total_tokens` double-counts codex cache reads; cache *writes* parsed then never priced; codex runs contaminate the learned rate applied to Claude aborts. | `codex_runner.py:524-535`, `journal.py:1076-1136` |
| B5 | P1 | **No spend reconciliation after a hard death** — SIGKILLed legs never reach the ledger even though codex rollout files with exact counters survive on disk; harness reconciles seconds but not dollars. | `adapters/journal.py:501-507` (write site) |
| B6 | P1 | Two hand-synced parsers of the same `CODEX_COST_PER_MTOK` string (one reports errors, one silently drops — its docstring claims it mirrors); an unreachable fail-closed branch means the #89 cached-tier blindspot warning can never fire from real config, and its tests construct the impossible state via `dataclasses.replace`. | `app/config.py:945-1044,1041` |
| B7 | P2 | Both-rates-≤0 → $0 fail-open (warning only) while a malformed table entry is a hard startup error — same failure class, two severities; `rate_in/10` magic default in 4 places. | `codex_runner.py:511-519` |
| B8 | P2 | Budget enforcement fused to a ~350-line stderr pump; `"error_max_budget_usd"` duplicated across adapters as a comment-enforced contract. | `codex_runner.py:1425-1610` |
| B9 | P2 | Leg budgets are scattered literals; router probe hardcoded at $2.00 vs the documented ≤$0.10 guardrail; `ISSUE_SPEND_CAP_USD` checked only pre-dispatch (multi-leg overshoot unbounded). | `app/planners/tasks.py:607,625,1155,1307` |

### 2.4 Governor / gate trust model

The honest current model (correctly documented in residuals §1.2, but
under-enumerated): the governor classifies **five git/gh command shapes**;
`disallowed_tools` is the only real capability boundary and exists **only on
the Claude path**; codex fix runs `danger-full-access` with the governor as the
sole check.

| # | Sev | Finding | Anchor |
|---|---|---|---|
| G1 | P1 | Wrapper allowlist loses by enumeration: `timeout`, `sudo`, `ssh-agent`, `script -c` verified live as bypasses (`timeout 5 git push --force origin main` → ALLOW). Reachable *by accident*, unlike the documented `sh -c` class. | `adapters/governor_policy.py:157` |
| G2 | P1 | Shell functions (`g() { git "$@"; }; g commit --no-verify`) and `git -c alias.z=…` defeat classification; "couldn't tell" and "not my business" are the same value (`None`) — the exact mistake `OpenPrLookup` fixed elsewhere. | `governor_policy.py:132-149,439-443` |
| G3 | P1 | **Every `.harness/*` gate marker is agent-writable self-attestation** (`adversarial-verdict`, `screenshots-read`, `ac-posted`…); AGENT.md instructs the agent to write them. P1R-18 demoted one marker for this reason; the class remains. Not carried into residuals.md (a gap by that doc's own contract). | `adapters/store.py:648-720`, `domain/gate.py:366-557` |
| G4 | P1 | **Codex enforcement artifact lives inside the agent's write scope**: `.codex/hooks/gov.sh` in the worktree; `echo 'exit 0' > .codex/hooks/gov.sh` mid-leg disarms it; under self-hosting `GOVERNOR_PATH` itself is inside `TARGET_DIR`. | `adapters/codex_hook.py:227-233` |
| G5 | P1 | **The #63 depth bound exists at 4 exec sites; the codex `EDIT_CHECK_CMD` site has none of it** (no scrub, no depth stamp, no timeout) — highest-frequency target-command execution. Scrub-list drift test can't see missing sites. | `adapters/codex_hook.py:192-205` |
| G6 | P2 | Governor-unavailable (`uv` missing) fails **open** on codex/legacy paths where the hook *is* the boundary; missing checker script fails closed — inconsistent for the same tampering class. Tree-sitter parse `ERROR` nodes fail open silently, untested. | `governor.py:786-794`, `codex_hook.py:113-116` |
| G7 | P2 | `gh` governed as a shape (`gh pr create` at fixed token positions), not a tool; `gh alias set` ungoverned; `gh api` blocked only via Claude-only `disallowed_tools`. | `governor_policy.py:200-239` |
| G8 | P2 | Untrusted-encoding rigor is local to `handover.py` (exemplary); target-repo `CLAUDE.md` spliced into codex instructions unencoded. | `codex_hook.py:37-92` |
| G9 | P2 | Not pinned by tests: governor actually registered on a real dispatch; `FIXER_DENY` non-empty; unparseable-command behavior; marker forgery not xfail-recorded the way `sh -c` is. | review §F10 |

Praise worth preserving: `governor_policy.py` is genuinely shared between the
Python and bash entry points (no twin); gate parity suite is best-in-class;
`GateContext` unknown-fails-closed defaults; `handover.py` end-to-end.

### 2.5 Cross-cutting

| # | Sev | Finding | Anchor |
|---|---|---|---|
| C1 | P0 | **Five of six adapter seams have no port** — `gateway/store/worktrees/executor: object` everywhere; `GitHubGateway` = 33 undeclared methods. | `app/workflows/fix.py:73-79`, `app/dispatch.py:167-177` |
| C2 | P0 | **17 `getattr` capability probes in production code** exist to tolerate test doubles; `execute.py:202` silently degrades #64 durable-receipt verification (duplicate-write protection) to a no-op if a method is absent. | `app/execute.py:202-263`, `app/workflows/fix.py:87-462` |
| C3 | P1 | Runtime twins: 846 + 2032 lines, zero shared code, asymmetric retry/rate-limit behavior behind one port; contract test covers only result-mapping; nothing verifies `disallowed_tools` (containment!) is honored equivalently; 3 runtimes via 3 different mechanisms. | `adapters/agent_runner.py`, `adapters/codex_runner.py` |
| C4 | P1 | Runtime identity is a bare string; `== "codex"` in app code stands in for capabilities ("can't resume with sandbox"). | `app/workflows/review.py:143,200,268`, `app/roles.py:950-1016` |
| C5 | P1 | `roles.py` god-module: six copy-pasted reporter closures (~450 lines) + six mirror `*_keys` readers in journal.py; the restart-surviving dedup subtlety re-implemented 6×. | `app/roles.py:438-890` |
| C6 | P1 | Untrusted content: 3 conventions across 20+ prompt sites; `_fenced` doesn't neutralize its own delimiter; triage thread appended unmarked in max-weight position; the strong encoder (`handover.py`) unused on human text. Second, independent reason for the trusted-issues-only restriction — undocumented as such. | `app/planners/tasks.py:151-1347` |
| C7 | P1 | Env-file sourcing (`set -a; source; env -0` → operator shell executed as config) implemented 3× with divergent error handling and scrub behavior. | `app/main.py:30-60`, `app/config.py:1123-1169`, `app/cli.py:122-176` |
| C8 | P1 | `architecture.md` 182 commits stale, omits glm/handover/dashweb/recurrence/webhook/Jira (~3,000-line dashboard unmentioned); CLAUDE.md absorbed the spec role at per-leg token cost; key invariants prose-only. | `docs/reference/architecture.md:3` |
| C9 | P1 | Five independent `FakeGateway`s; the highest-fidelity gh fake (JSON-backed, real jq) guards only the **legacy bash suite**. | `tests/fakes/gh`, 5 test files |
| C10 | P2 | ~17,000 lines of app-layer tests misfiled under `tests/adapters/`; layout contract exists for the domain tier only. | `tests/domain/test_test_layout.py` |
| C11 | P2 | Import-linter can't see `plugin/` (which imports *up* into adapters); fastapi/daemon boundary is a comment; pure `governor_policy.py` stranded in `adapters/` outside the purity contract. | `pyproject.toml:69-95`, `plugin/hooks/bash_governor.py:53-73` |
| C12 | P2 | Config: 4 manual touch points per knob; enum knobs validated inconsistently (`IDENTITY_MODE` carefully, `MERGE_METHOD`/`ARM_ON_OPEN` not at all). | `app/config.py:1047-1071` |
| C13 | P2 | `github.py`: tri-state invariant is convention — 5 fail-soft sites swallow `GhUnavailable`; `Retry-After` matched as substring, never parsed. | `adapters/github.py:1240-1432,240-248` |
| C14 | P2 | `_run_logged` duplicated with divergent status semantics; **8 of 11 fix legs bypass it**, making journal leg attribution — the substrate of every cost metric — path-dependent. | `app/workflows/fix.py:187-196,543-882` |
| C15 | P2 | `submit_review` bypasses the one-write-funnel invariant at 3 sites — the reviewer's most consequential write has no Action, no replay, no receipt. | `app/workflows/review.py:410,477,536` |
| C16 | P2 | "leg" (350 uses) has no type and collides with `domain.dashboard.Leg`; `AgentKind` does double duty; no glossary. | `domain/actions.py:30-50` |
| C17 | P2 | `roles.py` patches `executor._run_agent` post-hoc 3×; the unbound default is a **silently-succeeding no-op**. | `app/roles.py:1058,1360,1473` |
| C18 | P2 | `domain/` is import-pure but not behaviorally pure: `_utcnow()` clock in `domain/dashboard.py`; enforcement can't see usage. | `domain/dashboard.py:663-664` |

### 2.6 Additional findings from the codex review (verified)

| # | Sev | Finding | Anchor / verification |
|---|---|---|---|
| X1 | P0 | **Armed caps fail open on accounting unavailability.** `read_events` returns `[]` on *any* `OSError` (deliberate for missing-file first-run, but it also swallows permission errors, I/O errors, truncation) — so an armed daily/issue cap over an unreadable ledger sums to **zero spend** and admits paid work. There is no distinction between missing, uninitialized, unreadable, and malformed. | `adapters/journal.py:536-545` — re-verified: `except OSError: return []` under `sum_today` |
| X2 | P0 | **Admission is check-then-act with no reservation.** Cap checks run pre-dispatch; charges land post-completion. With `FIX_DISPATCH_CONCURRENCY > 1`, concurrent workers all pass the same snapshot, and a single multi-leg dispatch can overshoot the remaining cap unboundedly (sharpens B9 into a correctness hole, not a tuning gap). | `app/dispatch.py:328-393`, `app/spend_gate.py` |
| X3 | P1 | **Red-test evidence has no failure taxonomy.** `revert_and_run` returns `bool \| None` — a test that times out, hits an infra error, or is cancelled is indistinguishable from a genuine assertion failure, so an infrastructure failure can satisfy the red-fail (anti-tautological-test) gate. | `domain/gate.py:860-894` — re-verified: `Callable[[tuple[str, ...]], bool \| None]` |
| X4 | P1 | **Free work is coupled to paid admission.** A tripped daily cap gates the whole tick, including reconciliation, reaping, recurrence handling, and cleanup — work that incurs no model cost but keeps the system consistent. | `app/spend_gate.py:106-137`, `app/loop.py:244` |
| X5 | P1 | **The daemon has no exit taxonomy.** Supervisors cannot distinguish permanent configuration errors or deliberate operator cancellation from transient failure; `Restart=always` restarts everything, including unrecoverable states (30s silent crash loops). | `ops/systemd/*.service`, `app/main.py` |
| X6 | P2 | Deployment artifacts under-specified: units lack absolute-path resolution guarantees, `Type=notify`/watchdog, restart-rate limits, resource bounds; launchd twins log to shared unrotated `/tmp` paths and have the colliding-label bug residuals already notes. | `ops/systemd/`, `ops/launchd/`, `bin/install-*.sh` |

---

## 3. Design decisions (settled by this review + the codex integration)

1. **Authority split — the codex plan's central idea, adopted.** Four concepts
   get distinct failure semantics: *policy decisions*, *operational state*,
   *privileged effects*, and *telemetry*. Telemetry (the journals) is never
   the sole authority for safety, accounting, or deduplication. This amends
   the original journal-review direction (which would have made the event log
   the system of record): instead, **a small embedded transactional control
   store (SQLite) becomes authoritative** for reservations, run registry,
   leases, idempotency/dedup keys, lifecycle commands, and effect receipts —
   and **typed events are emitted from committed state via an outbox**.
   `dashboard = fold(events)` still holds, and gets *stronger*: events derived
   from committed transitions cannot drift from operational truth by
   construction.
2. **No broker, no queue, no event daemon, no microservices.** Unchanged. The
   JSONL event transport stays (per-role append-only files; crash isolation;
   lock-free append). SQLite is an embedded library, not infrastructure. Total
   order comes from an envelope key, not physical merging. Explicit codex-plan
   non-goals adopted verbatim: no full event sourcing, no distributed
   scheduler, no Kubernetes, and **no perfect shell parser as a security
   boundary**.
3. **World-observation events are part of the bus.** `dashboard = fold(events)`
   is unachievable from harness-intent events alone (drift source D7). The
   daemons already build a `World` per poll and already detect change
   (`WorldUnchanged` probe) — emit `observed` events on change, and the
   dashboard's own gh client, TTL cache, and `_human_action` archaeology
   retire.
4. **Break the dashboard during migration; shadow-migrate the money.** (User
   decision + codex refinement.) Telemetry/dashboard compatibility shims are
   not built — pre-v2 journals are dropped by the fold. But **accounting
   authority migrates via a time-bounded shadow mode** (`CONTROL_STORE_MODE=
   legacy|shadow|primary` with discrepancy alerts and a canary), because money
   errors are not recoverable by a re-render. Dual writes are time-bounded and
   every migration flag carries an owner, promotion metric, and removal issue
   (codex §18 discipline).
5. **Ports before events.** Typed protocols (C1) are the foundation; the
   event work and the control store land as ports. Doing events first would
   rebuild them on duck typing.
6. **The trust boundary is credential placement; the governor is advisory
   defense-in-depth.** The codex plan's reframing, adopted: an agent-local
   hook can reduce accidental misuse but is not an authorization boundary —
   the *end-state* boundary is that the process executing untrusted agent
   work holds no credentials capable of publishing (worktree-scoped
   filesystem, allowlist child env, effects proposed as durable
   `LifecycleCommand`s and executed by a narrow trusted broker against a
   verified content digest). That end-state remains **trigger-gated on
   onboarding untrusted-inbound repos** (residuals §1.2 posture), but two of
   its components land unconditionally because they fix current P1/P2s:
   durable lifecycle commands + receipts for every remote mutation (closes
   C15 and generalizes #64), and the trusted-evidence rule for gates (closes
   G3, X3). The **tri-state resolver** (`Governed | Ungoverned |
   Unresolvable`, deny what can't be resolved at parse time) still ships — as
   the class fix for the *advisory* layer (G1/G2/G6), explicitly labeled
   advisory.
7. **Pricing becomes a domain, with codex's sharper value objects.**
   `Money(currency, micros)` — integer minor units, never floats; money and
   quota units are distinct types that are never summed;
   `basis ∈ {provider_reported, configured_rate_card, learned_estimate}` with
   rate-card id/version recorded on every charge; `TokenUsage` with a declared
   disjoint-counter convention and `source` provenance; unknown models follow
   an explicit configured policy, never a silent fallback rate. A rate quote
   locks to a reservation; later rate-card updates never rewrite settled
   history.
8. **Explicit result taxonomies replace ambiguous zero-values** (codex P1.5,
   adopted): accounting `AVAILABLE | EXHAUSTED | UNAVAILABLE | CORRUPT`; test
   execution `PASSED | TEST_FAILED | INFRA_ERROR | TIMED_OUT | CANCELLED`
   (only `TEST_FAILED` is red-test evidence — X3); policy `ALLOW | DENY |
   INDETERMINATE` (indeterminate never becomes allow); effects `SUCCEEDED |
   RETRYABLE_FAILURE | PERMANENT_FAILURE | UNKNOWN_OUTCOME` (unknown outcome
   → reconcile, never blind retry). This is the `OpenPrLookup` lesson applied
   as a system-wide rule.
9. **Adjudications** — where the two plans disagreed:
   - *Journal as system of record* (my original Phase 2) **vs** *control store
     + outbox* (codex): **codex wins** for operational state (reservations,
     dedup, leases, commands). The journal reviewer's own findings (torn-line
     risk, 1 MiB dedup window, tail-scan idempotency) are the evidence. The
     journal keeps everything else from the original plan: typed versioned
     events, `leg_id`/`stall_id`, corrections-by-id, total order, one fold,
     world observation.
   - *Break-everything migration* (mine) **vs** *compatibility adapters +
     shadow* (codex): **split the difference by risk class** — dashboard/
     telemetry may break (user decision stands); money and dedup authority
     migrate via shadow mode (decision 4).
   - *Tri-state resolver as the governor class fix* (mine) **vs** *governor
     demoted to advisory, credentials as the boundary* (codex): **both** —
     codex's framing is the honest end-state; the resolver still closes the
     accidental-bypass class cheaply and converts future holes into loud
     denials. What changes is the *label*: no safety claim rests on shell
     parsing (decision 6).
   - *CloudEvents/OTel alignment now* (codex): **deferred** — the envelope is
     kept *exportable* to those shapes (field naming compatible, no
     obstruction), but no exporter or spec-compliance work until an external
     consumer exists (evidence-trigger doctrine).
   - *Separate OS-user effect-broker process on day one* (codex §12.2):
     **staged** — commands/receipts + in-process narrow effect adapter first;
     process/OS-user separation when the untrusted-repo trigger fires. Merely
     deleting env vars from a child is documented as insufficient isolation
     (codex is right); the capability matrix states the residual until then.
   - *Seven ADRs* (codex P0.1): **adopted, folded to five** (trust boundary +
     effects; money/accounting semantics; control store; event envelope +
     retention; supervision/exit contract — the intent-vs-runtime-plan and
     gate-evidence decisions are recorded inside this document's phases and
     get ADRs when implemented).
10. **Respect the evidence-trigger doctrine, with one amendment**: the
    residuals §4 structural items (S2 FixLeg, S5/S6 selector-consolidation)
    were gated on "a week of real operation" — that gate has been met several
    times over. They graduate into this plan. Items still lacking triggers
    (webhook push, P4.x cost levers, OTel export) stay out.

### 3.1 Standing governance rules (drift prevention)

These outlive the refactor. The form matters more than the list: **this
codebase has already run the experiment on governance-as-prose, and it lost
every time** — the one-write-funnel rule had three silent exceptions (C15),
the "every exec site uses `run_target_command`" doctrine lived in a comment
and the fourth site forgot it (G5), architecture.md drifted 182 commits (C8),
and a comment instructing maintainers to keep two predicates in sync did not
keep them in sync (J2). Meanwhile every *mechanized* rule held: import-linter,
gate parity, `_dc_default`, the strict-xfail registry, the scrub-list drift
test. In a repo where many PRs are authored and reviewed by agents, a rule is
only real if a test, a contract, or the reviewer prompt enforces it.

**The meta-rule** (binding on all rules below and on future ones): *any new
prose invariant must name its enforcement* — the test, import-linter
contract, type, or reviewer-prompt line that would catch a violation — or it
is recorded as an aspiration, not an invariant.

Mechanically enforced (tests/contracts; most land inside plan phases and are
hereby promoted from one-off deliverables to standing rules):

- **G-1 Privileged-write boundary.** No remote mutation outside the effect
  adapter / `LifecycleCommand` path. Enforcement: the Phase 5.1 AST/
  architecture test, permanent alongside import-linter.
- **G-2 Telemetry is never authority.** App code may not read journal/event
  files for control decisions (admission, dedup, authorization, lifecycle);
  those read the control store. Enforcement: import-linter contract on the
  journal-reader module once Phase 3 lands; the Phase 2.8 registry test
  covers event-shape drift.
- **G-3 No silent no-op defaults.** No required handler or collaborator may
  default to a do-nothing implementation (the C17/C2 class). Enforcement:
  AST test over `app/` constructors + the Phase 1 protocol annotations.
- **G-4 Untrusted by type.** Agent output, agent-writable files, and inbound
  GitHub text enter prompts and gates only as `Untrusted`/claim types (5.2,
  5.5). Enforcement: signature types + the fence-escape manifest test.
- **G-5 Flags and dual paths carry their executioner.** Every feature flag,
  compat shim, and dual write names a removal issue at introduction.
  Enforcement: a tested flag manifest (flag name → issue number); the
  existing "a workaround names its executioner" CLAUDE.md rule, mechanized.
- **G-6 Accepted residuals are strict-xfails.** A known-open hole gets a
  `strict=True` xfail the day it is accepted (the residuals-§1.2 pattern,
  applied uniformly — G9's gap was exactly the places this wasn't done).

Human process, deliberately scoped (enforced via reviewer prompt + PR
template, not goodwill):

- **G-7 ADR-on-five-domains.** A change affecting *trust boundaries, money/
  accounting semantics, durable operational state, privileged effects, or
  process lifecycle* cites an existing ADR or adds one (a page, max — the
  incident-comment culture's density, not enterprise ceremony). The Phase
  0.13 ADRs seed the set. Enforcement: reviewer-prompt line + PR template
  question; steward flags PRs touching the five domains without an ADR
  reference.
- **G-8 Crash/concurrency analysis for state changes.** A PR touching the
  control store, ledger, or reservation logic includes a PR-description
  section listing the crash points it introduces/moves and the test that
  covers each (the codex P2.3 discipline, applied at review time).
  Deliberately *not* required for all PRs — universal checklists get
  rubber-stamped.
- **G-9 Per-port failure contracts, paid as-you-go.** Every *new or moved*
  port declares failure disposition (retryable/permanent/indeterminate/
  cancelled), idempotency key + scope, and fail-open vs fail-closed — in the
  port docstring, pinned by its contract test. Not a retroactive audit of
  existing adapters; Phases 1-5 redraw those seams anyway and pay this cost
  port-by-port as they land.

---

## 4. Phased implementation plan

Phases are ordered by (a) severity of active incorrectness, (b) dependency.
Each phase is independently landable and leaves the tree green. Within a
phase, items are roughly parallelizable; **shared seams (new ports, event
envelope, control-store schema, resolver core) are single-owner work** — do
not fan out agents on them concurrently.

### Phase 0 — Stop the bleeding (bug-class fixes + honest containment)

Independent, high-severity, no design risk. Every item ships with the
regression test the incident rule demands.

- **0.1 SIGTERM handler** (S1): `signal.signal(SIGTERM, raise SystemExit)` in
  `main()` so the existing `BaseException` teardown runs; systemd units gain
  `TimeoutStopSec=90`, `KillSignal=SIGTERM`, `FinalKillSignal=SIGKILL`;
  `issue-agent stop` waits and verifies. Test: SIGTERM a live daemon fake,
  assert cancellation + journal receipt.
- **0.2 Kill orphaned codex trees on teardown and boot** (S2, minimal form):
  persist spawned pgids to `STATE_DIR`; kill registered pgids in the teardown
  path and at daemon start. (Full `ProcessSupervisor` is Phase 3; this is the
  P0 stopgap.) Test: kill daemon with live child pgid file, restart, assert
  child killed before first dispatch.
- **0.3 Accounting-unavailable containment** (X1, X4 — codex P0.4): armed caps
  distinguish missing / uninitialized / unreadable / malformed ledger states;
  first use records an initialized-empty marker atomically; read failure under
  an armed cap → **block new paid admission, loudly**, while observation,
  reconciliation, reaping, and cleanup continue (decouple the free tick work
  from the paid gate). An absent ledger never silently means zero spend.
  Temporary containment — atomic reservations are Phase 4.
- **0.4 Red-test evidence taxonomy, minimal form** (X3): `revert_and_run`
  return type gains the infra/timeout distinction (full `TestExecution` value
  object in Phase 1); the red-fail gate accepts only a genuine test failure.
  Parity case added per the gate-parity rule.
- **0.5 Move the dead forensics branch above `return`** (S9) + emit-site test.
- **0.6 Route codex `_edit_check_script` through the shared guarded wrapper**
  (G5): scrub + depth + timeout, like its legacy twin already does. Extend the
  sanitization drift test into an **exec-site manifest test** (grep the tree
  for config/marker-sourced execution; fail on any site not in a declared
  manifest).
- **0.7 Relocate codex hook + config outside the worktree** (G4):
  `STATE_DIR/codex-hooks/<issue>/gov.sh`, absolute path in `config.toml`;
  refuse the leg if it can't be written. Also move hook-written markers
  (`screenshots-read`) to STATE_DIR (G3, cheap half).
- **0.8 Close the settle-time stderr-usage trust hole** (B3): tag usage with
  `source`; refuse `source="stderr"` for budget verdicts and ledger pricing
  (journal a `telemetry_warning` instead); keep the plausibility guard armed
  for the whole run as a $/minute ceiling.
- **0.9 Fix the divergent `estimated` predicate and repo-guard duplicates**
  (J2 items 6-7) — 2-line fixes now, superseded properly in Phase 2.
- **0.10 Bound `sum_today`** (J9/B6 hot path): day-bounded tail read; assert
  `sum_issue` stays off the poll path. Ledger rotation lands in Phase 2.
- **0.11 Single-parser rate table** (B6): one parser, cached tier as a
  component of the entry; make the unreachable blindspot branch reachable or
  delete it; name the `rate_in/10` constant once (B7).
- **0.12 `timeout`/`sudo` to the wrapper handling** (G1, stopgap): add the
  known wrappers now with an xfail-strict per remaining known hole (G9
  pattern), pending the Phase 5 class fix.
- **0.13 ADRs + capability/authority matrix** (codex P0.1/P0.2, folded to
  five ADRs per decision 3.9): trust boundary + effects; money/accounting;
  control store; event envelope + retention; supervision/exit contract. The
  capability matrix documents, per runtime × deployment mode: credentials
  visible to daemon vs agent children, sandbox/tool surface, hook coverage,
  evidence sources, authoritative storage — with contract tests where
  checkable.
- **0.14 Documentation honesty pass** (codex P0.3, subset): stop claiming
  every GitHub write passes through the executor (C15 exists); label the
  hooks advisory with known bypasses; label JSONL as bounded telemetry, not
  durable control state; record the `.harness` marker forgery class in
  residuals.md (G3's missing entry). Full doc refresh stays in Phase 7.
- **0.15 Bootstrap the governance rules (§3.1)**: add the G-7/G-8 lines to
  the reviewer prompt and PR template; land the two cheap standing tests
  now — G-3 (no no-op defaults, AST over `app/` constructors) and G-5 (flag
  manifest with removal-issue numbers, seeded with the §6 migration flags);
  create the strict-xfail entries G-6 requires for the residuals accepted in
  this phase (0.12's remaining wrapper holes, the marker-forgery class).
  G-1/G-2/G-4 activate with their phases (5.1, 3.5, 5.5) and are marked
  standing from day one.

*Metric: escalation rate and orphaned-process incidents (S2 class) → 0; zero
paid admissions under unavailable accounting. Guardrail: merges/day flat;
`make test` green.*

### Phase 1 — Ports and typed contracts (the foundation)

Single-owner seam work. **Prerequisite to Phases 2-6.**

- **1.1 `domain/ports.py`**: `Protocol`s for `GitHubReads`, `GitHubWrites`,
  `IssueStore`, `Worktrees`, `Journal` (→ `EventSink`), `Clock`
  (wall+monotonic+sleep), `SpendAccount` (snapshot/reserve/settle/release/
  reconcile — implemented over legacy files until Phase 4), and a
  `ProcessSupervisor` stub. They speak existing domain vocabulary
  (`IssueView`, `PRView`, `Action`). Annotate every workflow/dispatcher/
  executor collaborator (C1). Split `GitHubGateway`'s 33 methods across
  read/write protocols.
- **1.2 Value objects** (codex P1.1 merged with the pricing/journal reviews):
  `Money(currency, micros)`, `TokenUsage` (disjoint counters + `source`),
  `PriceQuote` (rate-card id/version), `RunId`/`DispatchId`/`CommandId`,
  `BudgetScope` (explicit period id/start/end/tz — "today" is not a storage
  key), `TestExecution` + `TestExecutionStatus`, `PolicyDisposition`,
  `LifecycleCommand`/`EffectReceipt`, `EventEnvelope`. Constructors reject
  negative tokens, invalid currency, naive timestamps, impossible states.
- **1.3 Delete all 17 `getattr` shims** (C2). Genuine optionality becomes
  `X | None` with a null object. The `execute.py:202` receipt-verification
  degradation becomes impossible. No required handler may default to a no-op.
- **1.4 One shared `InMemoryGitHub` fake** in `tests/fakes/` satisfying the
  protocols (promote the state model of the legacy JSON fake); delete the
  five `FakeGateway`s (C9). One contract test asserts real gateway and fake
  both satisfy the protocol.
- **1.5 Runtime enum + `RuntimeCapabilities`** on the port
  (`supports_resume`, `resume_preserves_sandbox`, `renders_images`,
  `native_budget_enforcement`); replace every `== "codex"` capability check
  in `app/` (C4). Three-branch construction in `roles.py` becomes a registry.
- **1.6 `RunIntent` / `RuntimePlan` split** (codex P1.3): domain task objects
  stop carrying SDK tool names, MCP config, env, cwd, resume tokens; a
  provider-specific `RuntimePlan` is produced by app planning. Contract test:
  provider settings cannot re-enter domain objects.
- **1.7 Runtime parity suite** (C3): extend the 135-line contract test to the
  full task-field set on the gate-parity model — each field honored
  equivalently or *declared* unsupported via `RuntimeCapabilities`.
  `disallowed_tools` equivalence is the priority case (containment).
- **1.8 Static checking as a repo contract** (codex P1.6): pyrefly over `src/`
  *and* tests in CI (resolve the current import-root diagnostics rather than
  excluding directories); missing protocol methods become build failures.
- **1.9 Enforcement-contract extensions** (C11, C18): import-linter sees
  `plugin/`; fastapi barred from daemon modules by contract; move pure
  `governor_policy.py` → `domain/`; AST purity test over `domain/` (no
  `datetime.now`/`os.environ`/`open`/`subprocess`); fix the two current
  violations.
- **1.10 Fix the wiring cycle** (C17): `Executor.bind_dispatch()` raising on
  rebind, unbound default raises instead of silently no-op'ing — or move
  dispatch routing into `Daemon`, which already special-cases `RunAgent`.

*Metric: none (enabling work). Guardrail: behavior-identical — full suite +
convergence tests green; no journal/wire change in this phase.*

### Phase 2 — Event schema v2 + the single fold (telemetry done right)

The heart of the dashboard-truth refactor. Dashboard breakage acceptable
during this phase. Scope note (decision 3.1): events are **telemetry and
projection feed**, not operational authority — dedup/idempotency authority
moves to the control store in Phase 3, and events of operational transitions
are emitted from committed state via the outbox from then on.

- **2.1 `domain/events.py`**: frozen envelope — `schema_version=2`,
  `event_id` (sortable uuid), per-writer `seq`, `occurred_at`/`observed_at`
  (ms precision) + `monotonic_ns`, producer (`deployment, role, pid,
  boot_id`), correlation block (`issue?, pr?, dispatch_id?, run_id?, leg_id?,
  session_id?, causation_id?`) — plus one frozen dataclass per event type
  with a codec registry. Names are facts (`agent.run.completed.v1` style
  acceptable but not required). Kept exportable to CloudEvents/OTel shapes;
  no exporter built (decision 3.9). Retire the 13 consumer-less types and
  producer-less `hold` in the same commit (J1; the §2.2 inventory is the
  audit). No `default=str`.
- **2.2 Convert all ~30 emit sites** to `emit(Event)`; delete `**fields` from
  the port. Mint `leg_id` per leg and `stall_id` per detected suspension.
  `action` events gain structured fields for *all* verbs (#85, J3); prose
  becomes a display string.
- **2.3 Total order + level-state rules**: sort key `(ts_ms, producer, seq)`
  everywhere (J5/J6); every level fact opens/closes on a shared `state_key`
  (the `deferred` pattern generalized); corrections carry
  `corrects: event_id`, idempotent by id — delete both suspension-partition
  algorithms and the 2.0s tolerance (J7).
- **2.4 The reaper emits `agent_run_abandoned{corrects: start_id}`** from the
  computation `roles.py:412` already performs (#116 — phantom running legs
  die here).
- **2.5 `domain/projection.py` — one `fold(events) -> ReadModel`**, consumed
  by dashweb, CLI text/HTML, the breakers, and metrics. Delete: the verbatim
  `unsettled_start_events` duplicate, `parse_ts`/`_int`/`_float`/
  `_stamp_date` copies, the `import *` shadowing block, the billed-cost
  duplicate, and the six-regex `parse_action` grammar (J2, J3). Projections
  document rebuild procedure, duplicate/late-event policy, and reconcile
  against authoritative state once Phase 3 lands (codex P4.3).
- **2.6 Ledger v2** (B2): `schema: 2`, `basis` (per decision 3.7 vocabulary),
  `currency`; `spend.jsonl` explicitly labeled a projection of run events (it
  already is, verified); caps report the basis mix they summed. (Authority
  moves to reservations in Phase 4; this makes the interim honest.)
- **2.7 Durability + retention** (J9): journal writes via `os.write` on an
  `O_APPEND` fd with a line-size assertion; size/time rotation whose
  compaction preserves open level state in a snapshot header; ledger rotates
  monthly. A slow reader can never block the scheduler.
- **2.8 Contract tests generated from producers** (J4): golden fixture per
  event type produced by the *real emit path*, full-dict equality;
  registry test — every type has a golden, every field a consumer reads is
  declared by a schema; compatibility tests: unknown additive fields
  tolerated, incompatible `schema_version` rejected explicitly. Replaces the
  223 hand-rolled literals over time.
- **2.9 `WorldReporter` extraction** (C5): one abstraction owning the
  seen-set + journal-tail dedup; the six reporter closures and six `*_keys`
  readers collapse into it (~450 lines out of `roles.py`). Its dedup
  authority migrates to the control store in Phase 3; the abstraction seam is
  the same either way.

*Metric: fold duplication count 11 → 1; dashboard issues #85 #96 #116 #143
#144 closed or unblocked. Guardrail: breaker semantics unchanged (convergence
suite); spend-cap behavior unchanged on priced runtimes.*

### Phase 3 — Control store + supervision hardening (operational truth)

Introduces the SQLite control store (decision 3.1) and makes crash
consistency one seam. Storage constraints per codex §11.1: local filesystem,
WAL where possible, busy-timeout, short transactions with no network/model/
subprocess work inside, versioned migrations, startup integrity check,
restrictive permissions, documented backup/restore. SQLite holds operational
state only — never high-volume telemetry.

- **3.1 Control-store schema v1**: `schema_migrations`, `runs`,
  `leases` (process ownership), `dedupe_keys` (namespaced, scoped, with
  explicit reuse windows), `lifecycle_commands`, `effect_receipts`,
  `event_outbox`. (`budget_reservations`/`charges` tables land in Phase 4 on
  the same substrate.) DB constraints: unique run/command/receipt/idempotency
  ids, legal state values, one terminal run outcome, command→receipt
  referential integrity.
- **3.2 `ProcessSupervisor` adapter** implementing the Phase 1 port: spawn →
  register pgid durably → `kill_tree()` → reap; both runtimes use it (S2 full
  fix, S6 — Claude legs get `start_new_session` + killpg, closing the
  dev-server-survives hole). One tested implementation, two callers; the
  codex killpg test becomes the shared suite. Child env is an allowlist;
  inherited fds closed unless needed.
- **3.3 Durable leases in the control store** (S4): `{pid, pgid, started_at,
  dispatch_id}`; dead-pid leases reclaimed after kill; live-pid leases
  authoritative. The reaper gets a truthful liveness signal.
- **3.4 One idempotent `recover()`** at boot and on suspend-wake (S3), in
  fixed order: kill registered orphans → reclaim dead leases → prune/validate
  worktrees → drain pending commands → verify recent receipts against GitHub
  (the suspend path's logic, generalized, with the S12 causality check: skip
  replay when the target moved) → reconstruct unsettled runs → emit
  `agent_run_abandoned` corrections. The suspend arm becomes recover()'s
  first caller, not a special case. Crash points modeled explicitly (codex
  P2.3 list) and each has a kill-restart test.
- **3.5 Dedup authority moves to `dedupe_keys`** (S5, codex P2.4): behavioral
  once-only decisions (lifecycle actions, flake/recurrence/alert suppression,
  grooming) stop depending on the 1 MiB journal tail. Two documented
  primitives: `claim(key)` (at-most-once) and `receipt(key, payload)`
  (at-least-once); a table declares each `AgentKind`'s semantic, pinned by
  test. Migration: shadow-compare tail-derived vs store-derived decisions for
  one deployment before cutover (decision 3.4).
- **3.6 Daemon state machine + exit taxonomy** (X5, codex §14.1/P5.4):
  `STARTING → RUNNING → DRAINING → STOPPED`, with DRAINING = stop admission →
  classify shutdown source → cancel/drain per configured policy (bounded,
  inside `TimeoutStopSec`) → persist intent → reconcile → flush outbox →
  kill remaining groups → exit. Stable exit codes: clean stop / transient /
  invalid config / duplicate instance / incompatible schema / store
  corruption / operator-required repair. Supervisors restart only transient
  categories (`RestartPreventExitStatus`); operator cancellation is never
  counted as infra death. Exit-87 reload becomes part of the taxonomy.
- **3.7 `Clock` port threaded everywhere** (S7): suspension attribution
  implemented once, pure, in `domain/`; loop, journal annotation, and the
  fold consume it (most of this falls out of 2.3's `stall_id`).
- **3.8 Liveness** (S8): heartbeat file (mtime + in-flight dispatch ids) per
  iteration; `Type=notify` readiness (sent only after config + lock + store
  integrity + adapter init succeed) + `WatchdogSec` driven by scheduler
  progress; bounded `pool.shutdown` deadline → escalate to kill-tree → exit
  non-zero.
- **3.9 Handover on cancellation** (S11): best-effort bounded write from the
  teardown path; store in STATE_DIR keyed by issue (survives worktree
  removal).
- **3.10 Lock scope + deployment identity** (S10, X6): lock keyed
  `(role, repo, target_dir)` under STATE_DIR; refuse empty `REPO`; handle
  foreign lock files distinctly; webhook forwarder gets its own lock.
  Installers resolve absolute executable paths, assign stable per-deployment
  identities (units and launchd labels cannot collide), add restart-rate
  limits and resource bounds (`MemoryMax`, `TasksMax`), render + validate
  units/plists in dry-run, and keep the previous known-good on upgrade.
  launchd logs move out of shared `/tmp`. Hot-reload: content-hash instead of
  mtime; candidate config validated completely before an atomic snapshot
  swap; in-flight work finishes under its original snapshot (codex §8.2).
- **3.11 Supervision tests** the reviews found missing: SIGTERM drain with
  0/1/many children, second-signal escalation, orphan-sweep end-to-end,
  Claude descendant teardown, SIGKILL-restart reconciliation without a
  duplicate paid dispatch, stuck-scheduler watchdog, permanent-failure
  no-restart-storm.
- **3.12 architecture.md gains a supervision section**: process tree,
  death-class table, exit taxonomy, who-kills-whom (the review's topology
  diagram is the draft).

*Metric: infra-death and duplicate-dispatch incidents → 0; dedup decisions
independent of journal size. Guardrail: issue→merge wall clock flat; shadow
comparison clean before dedup cutover.*

### Phase 4 — Pricing domain + atomic budget reservation

- **4.1 `domain/pricing.py`** (B1, B4, decision 3.7): `TokenUsage`
  normalization at the adapter boundary (codex → disjoint once, at parse
  time), `Money` micros arithmetic (property-tested: rounding, huge values),
  `RateTable`/`PriceQuote` with version + `priceable()`/`gaps()`, pure
  `price()` and `overrun()`. All four token classes priced; an unpriced class
  is a config error; unknown models follow the ADR-defined policy. Runtime
  adapters only produce `TokenUsage`.
- **4.2 Transactional reservation** (X2, codex P2.1/P2.2): admission becomes
  one control-store transaction — resolve config snapshot + period + locked
  quote → check `settled + active_reservations + requested_max <= cap` →
  insert reservation bound to run id → durable receipt. Settlement is
  idempotent by run id (repeat-identical succeeds, repeat-conflicting alerts);
  release records a reason; every reservation reaches exactly one terminal
  state. Maximum-charge semantics defined for retries, multi-leg reviews, and
  escalation runs — a dispatch reserves its maximum, closing the multi-leg
  overshoot (B9). Provider-exceeds-quote is recorded as variance + alert, not
  retroactive admission falsification. Lowering a cap below committed spend
  blocks new reservations without rewriting history.
- **4.3 `BudgetSentinel`** (B8): runtime-agnostic policy object
  (`sample() -> TokenUsage|None`, decides `Overrun`); adapter owns the kill
  mechanism. `error_max_budget_usd` becomes a `domain/outcome.py` constant
  imported by both adapters.
- **4.4 Crash spend reconciliation** (B5), wired into Phase 3's `recover()`:
  for unsettled runs, price the surviving rollout delta and **settle the
  reservation** with `basis="reconciled"`, idempotent by run id;
  unrecoverable → release with reason + visible `spend_unknown` marker.
- **4.5 `BudgetPolicy`** (B9): role × leg-kind → cap, config-backed, replacing
  scattered literals; router-probe ceiling asserted against the documented
  ≤$0.10 guardrail (or the doc updated — loudly, not silently).
- **4.6 Consistent fail-closed** (B7, X1 finalized): armed cap + unpriceable
  runtime = startup error with explicit opt-out; accounting `UNAVAILABLE |
  CORRUPT` blocks paid admission (now enforced by the store, superseding the
  0.3 containment); estimator scoped by runtime+model (B4's contamination);
  GLM gets a rate table or an explicit synthetic basis (B2).
- **4.7 Migration**: `CONTROL_STORE_MODE=legacy|shadow|primary` per decision
  3.4 — import legacy ledger with deterministic ids + source digest; shadow
  for ≥ one representative window comparing totals/dedup/failure behavior;
  canary primary on one trusted repo; keep read-only legacy export for the
  rollback window. Rollback = stop paid admission + reconcile, never restore
  an older ledger over new charges.
- **4.8 The pricing test battery**: the ten untested contracts from the
  budget review + codex's concurrency set — two processes racing the final
  available dollar, hundreds of concurrent reserve/settle/release, period
  boundaries across tz/DST/leap-day, kill at every crash point, corrupt DB /
  full disk / read-only fs, duplicate + conflicting provider receipts.

*Metric: $/merged-PR becomes trustworthy (basis-split); 100% of paid runs
have a prior reservation; ledger gap after kills → 0. Guardrail: no new
escalations from spurious cap trips; shadow totals reconcile.*

### Phase 5 — Trust: commands/receipts, authoritative gates, advisory governor

Two layers, per decision 3.6. Layer A lands unconditionally (fixes current
findings); layer B's full isolation remains trigger-gated on untrusted-repo
onboarding but is now concretely designed (codex §12 is the reference).

**Layer A — authority (unconditional):**

- **5.1 `LifecycleCommand` + `EffectReceipt` for every remote mutation**
  (C15 generalized, codex P3.1): commands are durable (control store),
  idempotency-keyed, with expected-revision guards; receipts are recorded in
  the same machinery that #64 pioneered. `SubmitReview` migrates first, then
  the full inventory (comments, labels, branches, pushes, merges, closes).
  An architectural test prevents new direct remote mutations outside the
  effect adapter. `UNKNOWN_OUTCOME` → reconcile before retry, never blind
  retry.
- **5.2 Gates take trusted evidence only** (G3 full fix, X3, codex P3.4): one
  canonical policy implementation; `TestExecution` with executor identity +
  revision replaces `bool | None` (only `TEST_FAILED` is red evidence);
  agent-written markers become *claims that schedule trusted verification* —
  each marker either re-derived from a party the agent cannot author as
  (`ac-posted` from FIX_BOT-authored GitHub comments), hook-written to
  STATE_DIR (done in 0.7), or honestly reclassified Hook→Prompt in HARNESS.md
  (`adversarial-verdict`). `PolicyDisposition.INDETERMINATE` (e.g. CI
  unreachable) never becomes allow.

**Layer B — advisory hardening + staged isolation:**

- **5.3 Tri-state resolver** (G1, G2, G6, residuals §1.2 as one class):
  `resolve(command) -> Governed(kind, argv) | Ungoverned | Unresolvable`;
  `Unresolvable` = anything deferring resolution past parse time (interpreter
  `-c`, `eval`, `$VAR`/function-name in command position, `-c alias.*`,
  unrecognized wrapper before a governed binary, parse `ERROR` node) →
  **deny with actionable message**. Invert wrapper polarity: governed-binary-
  anywhere triggers classification; explicit allowlist suppresses
  (`grep git`). The five strict-xfails flip green; each newly-accepted
  residual gets its own strict-xfail (G9 discipline). Documented as
  **advisory** — no safety claim rests on it (decision 3.6).
- **5.4 `gh` allowlist in the governor** (G7), not in runtime-specific
  `disallowed_tools` — making the policy runtime-portable is the fix for the
  entire "only enforced on Claude" class. Codex fail-open on missing `uv`
  becomes fail-closed; `doctor.sh` refuses to start instead (G6).
- **5.5 `adapters/untrusted.py`** (C6, G8): extract `handover.py`'s encoding
  (`_untrusted_json` + delimiter convention) into one seam; an `Untrusted`
  newtype makes raw-`str` interpolation a type error in `build_*_task`
  signatures; route issue bodies, PR/review threads, target `CLAUDE.md`, CI
  logs through it. Manifest test: feed every builder a body containing the
  fence terminator + backtick, assert it cannot escape.
- **5.6 Governor wiring + registry tests** (G9): production-dispatch hook
  registration pinned; `evaluate_command`'s elif chain becomes a
  kind-registry with an exhaustiveness test against `classify`.
- **5.7 Staged credential isolation** (trigger: untrusted-repo onboarding;
  design ready now, codex §12.2/P3.2-P3.3): allowlist child env without
  publication credentials; worktree-scoped filesystem; the
  proposal-to-publish protocol (candidate artifact extracted without trusting
  agent refs, reconstructed from the operator-pinned base in a sanitized
  staging repo with hooks/filters disabled, verified by credential-free test
  execution, published by the narrow effect adapter against a content digest
  with force-with-lease). The codex adversarial test suite (§12.2) is the
  acceptance bar; the capability matrix states residual risk for runtimes
  that cannot enforce filesystem/egress isolation — those stay
  trusted-inbound-only.

*Metric: governor bypass class closed by construction (xfails green); 100% of
remote mutations carry command + receipt; prompt-injection surface
single-seamed. Guardrail: fixer false-denial rate — watch one deployment for
spurious `Unresolvable` denials and allowlist deliberately.*

### Phase 6 — World observation + dashboard refold (the payoff)

- **6.1 `observed` events** (decision 3.3): the world probe emits
  `observed{issue|pr, fields, hash}` on change only (the `WorldUnchanged`
  probe already detects change). Human actions — manual relabels, merges,
  closes, replies — enter the log (drift D7).
- **6.2 Dashboard reads the fold**: `_human_action` archaeology, the
  dashboard's own gh client/TTL stack, prose-stage heuristics, liveness
  inference (D3, D8, D9) retire; gh reads demoted to enrichment
  (titles/URLs). `store.py` durable transitions (session, phase, retries,
  patch-id) get outbox events so the dashboard can finally show them (J10) —
  the control store stays authoritative; the events are its committed shadow.
- **6.3 Backlog/stage from one authority** (D5, #144): the fold's state
  machine replaces `service.py`'s set-subtraction reconciliation. Run/cost
  joins use `run_id`, never event order (codex P4.3 rule).
- **6.4 `github.py` tri-state hardening** (C13): `_try_read` helper re-raises
  `GhUnavailable` by construction at all fail-soft sites; parse `Retry-After`
  into `GhUnavailable.retry_after_s`; `Daemon._sleep_interval` honors it.

*Metric: dashboard-reality drift reports → 0; dashweb per-poll cost drops
(fold over rotated files vs 9-source fusion). Guardrail: `gh_ok=false` still
never renders as an empty world — and now returns `INDETERMINATE`, never
inferred success.*

### Phase 7 — Consolidation (structure, language, docs, flag retirement)

- **7.1 True composition root** (codex P6.1 + C17 finalized): only startup/
  wiring modules import concrete adapters; no private-attribute patching, no
  default no-op handlers, no adapter construction inside orchestration.
- **7.2 `Workflow` base / `run_leg`** (C14): one path to `runner.run` in
  `app/workflows/`; AST test forbids bypass. Leg attribution becomes
  path-independent (fixes the cost-metric substrate).
- **7.3 `Leg` as a first-class domain type** (C16, residuals S2):
  `Leg(kind, target, number, billable, counts_against)`; rename the dashboard
  row type; `AgentKind` stays the dispatch discriminant.
  `docs/reference/glossary.md` (~40 lines) pins leg/task/run/session/action/
  world/outcome.
- **7.4 Config metadata table** (C12): `field(metadata={env, coerce,
  choices})` drives `from_env` + validation + a *generated*
  `agent.env.example`; enum knobs validated by construction; 4 touch points
  → 1. Config snapshots carry a version recorded on admissions and commands
  (codex §8.2, completing 3.10's reload work).
- **7.5 One `source_env_file`** in `adapters/env_file.py` (C7); `config.py`
  stops shelling out to bash.
- **7.6 Test-tier relayout** (C10): move the ~17k misfiled lines to
  `tests/app/`; extend the layout contract to all three tiers, mirroring
  import-linter.
- **7.7 Docs refresh** (C8): architecture.md rewritten current (supervision
  section from 3.12, event schema from Phase 2, pricing/reservations from
  Phase 4, trust model + capability matrix from Phase 5) with a staleness
  test (must name every top-level module); CLAUDE.md diet — behavioral detail
  moves to `docs/reference/`, leaving routing + direction + guardrails;
  measure the per-leg token delta (this is itself an efficiency change with a
  stated metric). The codex plan's documentation-set checklist (its §21) is
  the completeness bar.
- **7.8 Remove compatibility paths + retire migration flags** (codex P6.3,
  §18): JSONL control queries, free-form `event_line`, direct privileged
  writes, agent-marker gate evidence, `CONTROL_STORE_MODE`, dual writes —
  each removed when its exit criterion passed, each with a dated removal
  issue. Keep a read-only converter for historical JSONL.
- **7.9 Legacy deletion** (residuals P6.1, now unblocked): with
  `InMemoryGitHub` promoted (1.4) and parity suites guarding
  `plugin/hooks/checks/`, the legacy bash watchers and their suites can go.
- **7.10 Benchmarks + capacity statement** (codex P6.5): admission
  transaction latency, outbox delay, projection rebuild, tick latency under
  exhausted/unavailable accounting, shutdown latency with active children, DB
  growth — and a written statement of where SQLite / single-host would need
  re-evaluation. No distributed infrastructure preemptively.

*Metric: `roles.py`/`fix.py` line counts; CLAUDE.md token cost per leg; zero
live migration flags. Guardrail: gate parity suite stays green; property #5
(incident comments move verbatim) enforced in review.*

---

## 5. What we are explicitly NOT doing

- **No message broker / queue / event daemon / microservices / Kubernetes** —
  the JSONL transport is correct for telemetry; SQLite is an embedded
  library; the effect "broker" is a code/privilege boundary, a separate
  process only if/when the untrusted-repo trigger fires.
- **No full event sourcing** — the control store is authoritative for
  operational state; events are its committed shadow plus telemetry. Do not
  make the journal more authoritative before durable state exists (codex
  dependency rule, adopted).
- **No perfect shell parser as a security boundary** — the tri-state resolver
  is advisory hardening; the authorization story is credentials + commands +
  receipts.
- **No physical merge of per-role journals** — total order via envelope key.
- **No dashboard feature work** — this plan makes the dashboard *foldable*;
  refactoring its UI is a separate effort (user's call).
- **No trigger-less backlog items** — webhook push, P4.x cost levers, OTel
  exporters, live pricing lookups (a versioned operator rate card suffices)
  keep their triggers.
- **No rewrite of `.harness/` scratch conventions** — worktree-local agent
  context stays; only *gate-state* and *enforcement* artifacts move out, and
  markers become claims rather than evidence.
- **No module splits for line count alone** — splits must establish an
  ownership boundary (codex non-goal, adopted; it is also why Phase 7 waits).

## 6. Sequencing, dependencies, and migration discipline

```
Phase 0  bleeding + containment   ~14 small PRs, parallelizable, days
Phase 1  ports + value objects    foundation, single-owner seams
Phase 2  events v2 + one fold     dashboard dark mid-phase by agreement
Phase 3  control store + superv.  SQLite substrate; recover(); exit taxonomy
Phase 4  pricing + reservations   shadow → canary → primary for money
Phase 5  trust (A then B)         commands/receipts + gates now; isolation gated
Phase 6  observation + refold     the dashboard-truth payoff
Phase 7  consolidation            cleanup + flag retirement + benchmarks
```

Dependency-critical path: **1 → 2 → 6** (dashboard) and **1 → 3 → 4**
(money). Phase 5 layer A depends on 3 (command tables); layer B is
trigger-gated. Phase 0 starts immediately.

Hard ordering rules (from the codex plan, adopted verbatim):

- Do not make the journal more authoritative before durable state exists.
- Do not remove agent credentials without a tested artifact publication path.
- Do not treat new event schemas as durable idempotency.
- Do not add supervisor retries around non-idempotent commands.
- Do not split modules before contract ownership is decided.

Migration flags (each with owner, promotion metric, abort thresholds,
rollback instructions, and a dated removal issue — codex §18):

| Flag | Values | Removal condition |
|---|---|---|
| `CONTROL_STORE_MODE` | `legacy`, `shadow`, `primary` | store primary proven; legacy control reads deleted (7.8) |
| `TRUSTED_PUBLISH` | `off`, effect-kind allowlist, `on` | all direct writes routed; (credentials removed only at layer B) |
| `EVENT_FORMAT` | `legacy`, `v2` | all producers/consumers on v2 (no `dual` — dashboard may break, decision 3.4) |

Abort a canary when: an admitted paid run lacks a reservation; accounting
totals diverge without explained provenance; a command may have duplicated a
remote effect; a gate accepts agent-authored evidence; a projection differs
from authoritative state; a deliberate restart is classified as infra death;
a child survives the shutdown deadline; a permanent error restart-loops.

A note on process, from the reviews themselves: the five load-bearing
properties in architecture.md §"The five properties" plus the gate-parity
suite are the regression surface every phase must hold. Property #5 —
*incident comments move verbatim when code moves* — is binding for Phases 2,
3, and 7 especially, since they move the most commented code in the repo.
