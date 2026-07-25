# Architectural Review & Improvement Plan — autonomous-issue-agent

Date: 2026-07-25. Scope: full audit of the SDK harness (domain, adapters, app
wiring, enforcement payload, ops scripts, docs) against the 103-entry failure
catalogue in [FAILURE-MODES.md](FAILURE-MODES.md), aimed at making this harness
the primary workhorse across all of the operator's repositories.

Method: five parallel code audits (domain decision logic; adapters + app
wiring; enforcement payload + gates; docs vs reality; token/cost flows), each
grounding claims in `file:line` evidence, followed by direct verification of
every headline claim. The 103 failure modes collapse into ten systemic causes
(§2); the plan (§4) attacks causes, not symptoms.

> **STATUS (2026-07-25, evening): LEAN CORE IMPLEMENTED.** The findings below
> are preserved as the audit record, but most are now FIXED — see git log
> (`8fa32a5`→HEAD). Landed: all of Phase 0 (V1–V10 closures: resume dedup
> write-side, ADDRESS_REVIEW supersession gate + review-keyed breaker,
> ExitReason taxonomy + infra-death dispatch policy, wall-clock caps on both
> runners, gate test-run timeouts, fail-closed check-push + fail-closed
> TEST_CMD w/ ALLOW_UNTESTED, list-truncation warnings, pgrep anchor,
> token-mint retry), fixer deny-list (P3.1), AGENT.md resume-skip +
> validator diff-scoping (P4.5/P4.2-lite), screenshot-per-AC evidence rule
> (P3.6-lite), rate-limit/GhUnavailable detection (V10-lite), systemd-first +
> logs out of /tmp (P2.6), resolved-config print + fail-loud REPO (P5.1-lite),
> T3 loop-convergence property test, journal/ledger/spend-caps (P1-lite).
> **T3 uncovered and we fixed three NEW bugs beyond the audit:** the RESOLVE
> leg had NO circuit breaker (unbounded full-session loop — now
> MAX_RESOLVE_RETRIES), the automerge marker was dead code (re-armed every
> poll), and an empty-body reply stranded a blocked issue into the resolve
> loop. Also fixed: per-repo systemd unit collision in install-systemd.sh.
> **Everything else below remains trigger-gated backlog** (§4 table) —
> notably GraphQL world query, reconciliation/leases, governor policy tiers,
> model routing, review-cost items (P4.9–P4.11), config schema, contract
> cassettes. Open questions in Appendix G still stand except Q1 (fixer deny
> now includes background-network tools) and Q6 (bounded-retry: superseded by
> the review-keyed breaker).

---

## 1. Verdict

The core is genuinely good: a pure hexagonal domain (`domain/` has zero I/O,
enforced by import-linter), mostly level-triggered decision functions, atomic
store writes, flock single-instancing, per-action crash isolation, $0 idle
polls, and a real tree-sitter governor instead of regex. This is a sound
foundation worth investing in — not a rewrite candidate.

But it is not yet workhorse-grade. The audit found **one live correctness
regression**, several confirmed-open loop/waste bugs, a fail-open enforcement
posture with real bypass surfaces, near-zero observability (most past failures
were diagnosed by transcript archaeology, not by the system reporting itself),
no GitHub API hygiene (N+1 subprocess calls, no rate-limit handling), and a
token model that pays for the same context many times over.

### Verified-now findings (highest priority)

| # | Finding | Evidence |
|---|---------|----------|
| V1 | **`Store.set_resumed_at` is never called in `src/`** — the write side of the resume-dedup fix (FAILURE-MODES #13) is missing. A blocked issue with one unanswered human reply re-fires a RESUME agent **every poll tick** (real spend each time). | `store.py:92` (definition), call sites only in `tests/adapters/test_store.py`; read side `fix.py:132`, `roles.py:57` |
| V2 | **ADDRESS_REVIEW breaker can never trip** (#64 confirmed, worse than documented): re-dispatch fires whenever the latest review is CHANGES_REQUESTED with no new-commit gate (`fix.py:368-381`), and any successful push sets `clear_retry=True` (`outcome.py:242`, applied `workflows/fix.py:168`) — resetting the counter each round. If the review daemon is down, this loops at full session cost forever. | `fix.py:368-381`, `outcome.py:229-242` |
| V3 | **No total-runtime cap on either runner** — idle-only watchdogs (`agent_runner.py:330-363`, 900s idle default) mean a "limping" stream (#60) or a steadily-chatty runaway session runs unbounded. | `agent_runner.py:24-28, 330-363`; `codex_runner.py` same class |
| V4 | **Gate test-runs have no timeout** — `run_tests`/`revert_and_run` (`worktree.py:158`) shell out with no timeout; a hung `TEST_CMD` wedges the governor hook (and the whole agent turn) indefinitely. Same class as #24, unfixed at the adapter layer. | `worktree.py:153-158` |
| V5 | **The fixer session has no tool deny-list at all.** `plugin/settings.json`'s deny rules (curl, wget, `gh secret`, `gh auth`, `.env`/`.pem` reads) are dead code on the SDK path — never loaded (`agent_runner.py:134-136` sets only `setting_sources=["project"]`); fixer tasks set no `allowed_tools`/`disallowed_tools` (`planners/tasks.py:59-65`). Under `bypassPermissions`, prompt-injected exfiltration via `curl`/`gh api` is ungated. | `agent_runner.py:68, 134-136`, `tasks.py:13-14` |
| V6 | **The difficulty router is dead code** — `build_router_task` (`tasks.py:320`) has zero call sites. Every issue, trivial or complex, gets the same `FIX_MODEL` and the same $10 cap. | `tasks.py:320`, no callers |
| V7 | **Exit-status taxonomy missing**: an externally killed session is indistinguishable from an agent failure (#65, #58) — infra kills increment breakers and relabel `ai-ready`, racing manual `ai-blocked`; resolve-ERROR falls back to `ai-ready` but resume-ERROR to `ai-blocked` (undocumented divergence). | `agent_runner.py:398-452`, `outcome.py:59-114, 205-222` |
| V8 | **Fail-open gates**: red-fail/tests gate silently no-ops when `TEST_CMD` empty (stderr warning only, `_pr_common.sh:61-67,136-138`); `check-push.sh:28` fail-opens on any `gh` network error; red-fail fail-opens on unresolvable base ref or failed revert checkout. Gate markers (`ac-posted`, `adversarial-verdict`) are self-attested local files, forgeable by `touch`. | `_pr_common.sh`, `check-push.sh:28`, `check-pr.sh:66-67,93-95` |
| V9 | **World-building caps at `--limit 200`** with no pagination and no warning — a repo with >200 open issues silently drops the oldest from consideration (plausible root cause of #66's "11 candidates → 0"). | `github.py:412,427` |
| V10 | **No rate-limit handling anywhere**; a 403/429 returns `""` and is indistinguishable from "nothing to do" (`github.py:548-568`). Combined with ~5+O(issues)+2·O(PRs) subprocess `gh` calls per tick per repo, this fails silently exactly when scaled to many repos. | `github.py:352-436, 538-568` |

FAILURE-MODES.md corrections discovered: **#78 is FIXED** (FIX_MCP_CONFIG fully
wired: `config.py` → `workflows/fix.py:181-182` → `agent_runner.py:115-116`);
**#13 has regressed** (V1); **#61 is half-true** — the reaper *does* run every
tick (`fix.py:174-186, 272-274`) and self-heals in-process; the real gap is
process-level (foreground `point-at` has no restart-on-death; only the optional
systemd units do). `docs/issues/review-no-resultmessage-background-tasks.md` is
stale — the `run_in_background` denial it proposes is live
(`governor.py:343-350`), though Monitor/Agent denial appears to cover only
review/steward tasks (`REVIEW_DENY`), not the fixer — worth closing that gap.

---

## 2. Systemic diagnosis

The 103 failure modes reduce to ten causes. Every fix in §4 traces to one.

1. **Edge-triggered logic over shared mutable state.** Decisions keyed on
   "newer than last-seen" markers whose write side can be missed (V1), whose
   own writes feed back into triggers (#14), and whose local storage drifts
   from GitHub truth with no reconciliation sweep (confirmed: none exists).
2. **No unified subprocess supervision.** Each of #1/#2/#3/#5/#6/#10/#48/#52/
   #58/#59/#60/#62 was hand-patched at a different layer. Missing: one
   supervised-run abstraction with pgroup lifecycle, idle+wall+total caps, and
   a classified `ExitReason` consumed by dispatch (V3, V4, V7).
3. **Fail-open enforcement.** Absence of enforcement is indistinguishable from
   passing it (V8, #23, #32, #88). Markers are self-attested; deny rules are
   dead code on the live path (V5).
4. **Fake/real fidelity gaps.** #30/#46/#47: hand-written fakes encode the
   author's misunderstanding of `gh`/`git`; live-only failures ensued.
5. **Config sprawl with silent defaults.** ~70 env knobs (`config.py:401-481`),
   overlay files that silently miss lines (#50), booleans accumulating in
   `FixConfig`, no schema-driven validation of the *composition*.
6. **Token waste by architecture.** Full diff paid 6+× per review round; AGENT.md
   (~2.9k tok) re-appended even on resumed legs; O(rounds) transcript replay on
   resume; dead router (V6); uncapped scout riding the parent budget; budget
   caps that kill completed work pre-PR (#49) instead of degrading.
7. **Auth lifecycle as afterthought.** 1h App tokens (#21), UTC parse (#22),
   missing scopes discovered at runtime (#74), mint failures with no retry.
8. **GitHub semantics learned by incident.** Default-branch-only auto-close
   (#34/#35), BEHIND-branch automerge (#38), stale `--json files` (#41) — each
   encoded post-hoc rather than centralized and contract-tested.
9. **Adversarial-agent gaming.** Tautological tests (#28), coverage suppression
   (#29), wrong-side rebase (#69), no-op review pushes (#70) — the fixer
   optimizes gate-pass, and several gates measure forgeable proxies (V8).
10. **Single-laptop ops fragility.** Sleep/jetsam (#58-#62), foreground process
    trees with no restart, duplicate daemons (#12), logs in world-readable
    /tmp, an unanchored `pgrep` guard (`point-at.sh:160`).

Cross-cutting: **the system cannot see itself.** Plain stderr writes are the
only telemetry (`roles.py:44-51`); a swallowed action failure, a rate-limited
tick, or a $4.79 retry loop (#39) is invisible unless someone tails the right
log at the right time.

---

## 3. What to preserve (explicitly)

- Hexagonal split + import-linter guard; pure `domain/` with injected impure
  callables (`gate.py:342-447` is a model seam).
- Level-triggered `decide_*` as the default idiom; `Executor` action funnel.
- Atomic store writes (tmp+`os.replace`), flock single-instancing (crash-safe
  by construction), per-action crash isolation in `Daemon.tick`.
- $0 idle polls (pure decisions; LLM spend only on `RunAgent`).
- Tree-sitter AST governor (right approach — extend, don't replace), fail-open
  *on its own bugs* but fail-closed on recognized violations (`governor.py:151-153,
  357-365`) — correct asymmetry.
- Session-id threading via `Store` for resume; `_safe_get_pr` isolation;
  `_gather_legs` exception coercion in multi-review.
- Per-leg review budgets with cheap models on gate/validation legs.

---

## 4. The plan

### Execution strategy: lean core first, evidence-triggered backlog

The phases below remain the full reference design, but **only the lean core is
committed up front**. Every other item is backlog, each with an explicit
adoption trigger — an observation from the journal/ledger that proves the
need. Complexity is added only when the running system demands it.

**Lean core (~2–3 weeks total):**

1. All of Phase 0 — the ten verified bugs (days).
2. Telemetry lite (P1.1–P1.5 trimmed): JSONL journal with run_id + ExitReason,
   cost ledger (cache metrics come free from `ResultMessage.usage`),
   stuck-state warnings in `status`, one alert hook. Defer fingerprint DB,
   debug bundles, auto-postmortem.
3. Systemd-first deployment; logs out of world-readable /tmp (P2.6).
4. Rate-limit *detection* only — distinguish `ApiError` from empty, log
   loudly (the 20-line version of P2.5; batching deferred).
5. Fixer deny-list (P3.1) — wiring `disallowed_tools` is small and closes the
   worst security hole.
6. Fail-closed `TEST_CMD`: empty → dispatch refused unless the repo overlay
   explicitly opts out (`ALLOW_UNTESTED=1`) — extends P0.6/V8.
7. Spend caps from the ledger: per-issue lifetime + global daily (lean
   P4.7/P5.3). The one guard that must exist before pointing at all repos.
8. Cheap token wins: skip AGENT.md re-append on resumed legs (P4.5-lite);
   validators get finding-scoped context instead of the full diff (P4.2-lite).
9. Lean evidence rule: UI-affecting PRs require a screenshot per AC item,
   posted to the PR; reviewer must verify it shows the behavior (P3.6-lite —
   no manifest machinery yet).
10. Loop-convergence property test (T3) — guards exactly the bug class Phase 0
    fixes.
11. Config lite: print resolved config at startup; fail loud on empty critical
    values (lean P5.1).

**Backlog — adopt only when the trigger fires:**

| Backlog item | Adoption trigger |
|---|---|
| GraphQL world query (P2.4) | Rate-limit events or slow ticks appear in the journal |
| Reconciliation sweep, GitHub-anchored markers, leases (P2.1–P2.3) | A stranded-label or state-drift incident recurs despite systemd + reaper |
| Governor policy tiers, recursive `-c` parsing (P3.2) | A bypass observed in a transcript, or before onboarding repos with untrusted inbound issues |
| Marker attestation + evidence manifest (P3.3, P3.6 full) | Forged or stale evidence actually observed |
| Deterministic skip-gate (P3.4), anti-gaming diff checks (P3.5) | A gaming incident recurs (suppression, no-op push) |
| Model routing + escalation ladder (P4.1) | Ledger shows trivial issues costing ≳$1, or spend concentrated on easy issues |
| Cross-leg cache sharing (P4.9), finding dedup (P4.10) | Review cost per round is a top-2 ledger line |
| Persistent review sessions (P4.3) | Re-review replay cost visible in ledger |
| Review-once-per-green-SHA (P4.11) | Wasted rounds on red-CI pushes observed |
| Budget soft-landing (P4.4) | Another budget-kill-with-completed-work-lost incident |
| Within-repo concurrent fixes (P4.12) | Cross-repo parallelism saturated AND issue wait-time actually hurts — note this also requires conflict supervision (see P4.12 caveat) |
| Full config schema + generated doctor (P5.1) | Another silent-default incident |
| Onboarding scorecard (P5.4) | An onboarding failure doctor didn't catch |
| Webhooks (P5.6) | ~30+ repos or sustained API-budget pressure |
| Contract cassettes + chaos suite (T1, T2) | Next fake-vs-real or crash-recovery regression |
| Legacy bash deletion, gate single-sourcing, docs reconciliation (P6.1–P6.3) | Opportunistic — do when touching adjacent code |
| Steward port (P6.4) | Descoped now; revisit only if review quality plateaus |

### Phase 0 — Stop the bleeding (correctness, days, low risk)

Each item is small, independently shippable, and closes a verified-live hole.

- **P0.1** Wire `store.set_resumed_at(issue, last_reply_at)` after RESUME
  dispatch in the fix workflow; regression test: a dispatched resume whose
  outcome remains `ai-blocked` must not re-dispatch on an unchanged thread. (V1)
- **P0.2** Gate ADDRESS_REVIEW on evidence of supersession: dispatch only if
  the CHANGES_REQUESTED review's commit is behind the PR head **and** no fix
  push has already happened since that review (compare review `submitted_at` /
  commit OID vs head). Change `clear_retry` semantics: clear only when a *new
  review* supersedes the old one, not on push success. (V2, #64)
- **P0.3** Introduce `ExitReason` enum on `AgentResult`: `ok | agent_error |
  budget_exhausted | idle_timeout | wall_timeout | killed_externally |
  transport_death | no_result`. Dispatch policy: `killed_externally`/
  `transport_death` → retry **without** breaker increment and **without**
  relabeling to `ai-ready` (leave `ai-working` for the reaper/lease to settle);
  unify and document the resolve-vs-resume ERROR fallback divergence as an
  explicit transition table in `outcome.py`. (V7, #58, #65)
- **P0.4** Add wall-clock total-runtime caps: `AGENT_MAX_RUNTIME_S` (default
  ~2700s) on `AgentRunner` and `CodexRunner`, alongside the idle watchdog. (V3)
- **P0.5** Add timeouts to `run_tests`/`revert_and_run` (default 600s,
  configurable) — a timed-out gate run **denies** with a clear message. (V4)
- **P0.6** Fail-closed `check-push`: on `gh pr list` failure, retry once then
  deny with an explanatory message instead of skipping the gate. (V8)
- **P0.7** Paginate world-building or hard-warn when any list hits `--limit`;
  bump limit + log count. (V9)
- **P0.8** Anchor the `pgrep` guard (`pgrep -f "point-at\.sh ${NAME}\b"` or
  match on the flock instead); fix is two lines. (#12 adjacent)
- **P0.9** App-token mint: 3 retries with backoff before raising. (#21 adjacent)
- **P0.10** Correct FAILURE-MODES.md statuses (#78 fixed, #13 regressed→then
  fixed by P0.1, #61 clarified) and mark
  `docs/issues/review-no-resultmessage-background-tasks.md` resolved; extend
  Monitor/Agent-tool background denial to the fixer task config if absent.

### Phase 1 — Make it observable (before any big refactor)

You cannot tune or trust what you cannot see; this phase is a prerequisite for
measuring every later phase's impact.

- **P1.1 Structured event journal.** Append-only JSONL per repo
  (`STATE_DIR/journal.jsonl`): every decide-tick summary, action executed,
  agent run (task kind, model, duration, `ExitReason`, cost, session id,
  issue/PR), gate verdict, breaker change, rate-limit event. One schema, one
  writer in `Executor`/`AgentDispatcher`. Keep stderr logs as human mirror.
- **P1.2 Cost ledger.** Fold `ResultMessage.usage` (incl. cache_read /
  cache_creation tokens) into the journal; derive per-issue lifetime spend,
  per-repo daily spend, cache hit-rate. This confirms whether prompt caching
  is working at all (currently unknown) and arms P4 budget features.
- **P1.3 `issue-agent status` v2.** Read the journal + GitHub to render: active
  leases, per-issue phase + spend, breaker states, last N failures, stuck-state
  warnings (`ai-working` with no live session, approved-but-unmerged > 30min,
  same decision repeated N ticks with unchanged world — a structural
  loop detector that would have caught #14, #39, #64, V1).
- **P1.4 Alerting hook.** Optional command/webhook fired on: breaker trip,
  stuck-state detection, daily-spend threshold, repeated-decision loop. Even
  `notify-send`/ntfy is enough to end transcript archaeology.
- **P1.5 Run-id correlation + trace.** Every dispatch mints a `run_id`; every
  journal event (gh call, gate verdict, governor decision, label change, agent
  result) carries it plus issue/PR. `issue-agent trace <issue>` reconstructs
  the full causal timeline in seconds — the direct replacement for mining 81MB
  of transcripts. Retain SDK session transcripts (or pointers) keyed by run_id.
- **P1.6 Failure fingerprinting + self-reporting.** Classify each failure into
  the `ExitReason` taxonomy plus known-signature matches (patterns distilled
  from FAILURE-MODES.md); recurring fingerprints increment counters, a *novel*
  fingerprint alerts. On breaker trip or stuck-state, the harness posts a
  GitHub comment on the issue/PR with the diagnosis, journal excerpt, and
  runbook link — the error report lives where humans (and rescue agents) look,
  instead of requiring a supervising agent to notice silence.
- **P1.7 Debug bundles + auto-postmortem.** `issue-agent debug-bundle <issue>`
  tars the journal slice, transcript tail, resolved config, and world snapshot
  — so a "go fix it" session starts with context pre-packaged instead of
  re-explored. A nightly cheap-model pass scans the journal for anomalies and
  drafts candidate FAILURE-MODES entries automatically.

### Phase 2 — Reconciliation & leases (the state-machine fix)

- **P2.1 GitHub as sole source of truth; store as cache.** Add a per-tick
  cheap reconciliation: drop store entries (sessions, counters, markers) whose
  issue/PR is closed/merged; log discrepancies. Local state loss must cost at
  most one redundant-but-idempotent action, never a loop or a strand.
- **P2.2 Idempotency markers on GitHub, not disk.** Dedup keys that currently
  live only locally (resumed-at, triage baseline) get GitHub-anchored
  equivalents — e.g. an HTML comment marker (`<!-- ia:resumed:2026-07-25T… -->`)
  appended to the bot's own comment, or a check-run/label. The daemon can then
  be re-homed to another machine with zero state migration.
- **P2.3 Working leases.** `ai-working` becomes a lease: dispatch records a
  lease timestamp (marker comment or journal+heartbeat); any daemon instance
  reaps leases older than TTL regardless of who created them. This closes the
  "process died in foreground mode, issue stranded" gap (#61) properly.
- **P2.4 One GraphQL world query.** Replace the N+1 `gh` subprocess fan-out
  with a single GraphQL call per role per tick (issues + labels + comments-meta
  + PRs + reviews + statusCheckRollup). Order-of-magnitude fewer calls, faster
  ticks, and one place to implement **P2.5 rate-limit handling**: parse
  remaining/reset, back off with jitter, emit a journal event, and make gateway
  return types distinguish `ApiError` from empty. (V10)
- **P2.6 Systemd-first deployment.** Promote the existing template units to the
  documented default (`Restart=always`, `RestartSec=30`); `point-at` foreground
  becomes the explicit dev mode. Solves the whole suspend/jetsam class (#58–
  #62) more honestly than any in-process mitigation. Move logs from world-
  readable `/tmp` to `$XDG_STATE_HOME/issue-agent/`.

### Phase 3 — Enforcement hardening (before scaling blast radius)

- **P3.1 Deny-list the fixer.** Port `plugin/settings.json` deny semantics into
  `build_option_kwargs` as `disallowed_tools`/permission rules for **all**
  tasks: `gh auth`, `gh secret`, `curl`, `wget`, raw `gh api` mutations
  (POST/PATCH/DELETE except an allow-list), reads of `.env`/`*.pem`. (V5)
- **P3.2 Governor policy tiers.** Extend `governor_policy` from "classify 5
  families, allow the rest" to: governed (current) / **denied** (network exfil,
  `.git/hooks` and `.git/config` writes — including via Edit/Write hooks, `gh
  api` repo mutations) / **opaque-execution** (`sh -c`, `bash -c`, `python -c`,
  `xargs`, exec-of-written-file): recursively parse `-c` string payloads with
  tree-sitter (cheap, closes the biggest bypass), and treat still-opaque
  invocations as governed-deny in fixer context. Detect `export HUSKY=…`-style
  hook-disable in *earlier statements* of the same script, not just
  command-prefixed env. All are extensions of the existing AST machinery.
- **P3.3 Attested markers.** Gate checks verify ground truth, not local files:
  AC/test-plan checks query GitHub for the actual comments; the adversarial
  verdict is written by the harness (hook-side, after observing the subagent's
  structured result), not by the agent. Self-attested `.harness/*` files remain
  only as caches.
- **P3.4 Deterministic review skip-gate.** Replace the LLM skip-gate leg with
  numstat/path-glob rules (#79): removes an injection surface and an LLM call
  per round.
- **P3.5 Anti-gaming diff checks (deterministic, cheap).** In the PR/push gate:
  flag no-op pushes (identical tree hash → deny address-review success, #70);
  flag suppression patterns (coverage exclusions, `skip`/`only` markers,
  deleted assertions, CI config edits) for mandatory reviewer attention (#29);
  post-rebase, require the reviewer to see conflict-hunk files explicitly (#69).
- **P3.6 Evidence contract (proof-of-work made verifiable).** Today the
  reviewer's proof-of-work dimension has little concrete to inspect: the AC /
  test-plan / adversarial-verdict markers are self-attested local files, and
  nothing requires a runtime demonstration. Define a per-change-type evidence
  matrix, enforced by the PR gate and consumed by the reviewer:
  - UI/feature → screenshot or short recording **per AC item**, posted to the
    PR; the (multimodal) reviewer must verify each image actually shows the AC
    behavior, not merely that files exist.
  - API/backend → captured request/response transcript or integration-test
    output; CLI → terminal transcript; bugfix → failing-test-first proof
    (red-fail gate, already present); perf → before/after benchmark output.
  - **Attested capture:** a PostToolUse hook (extending the existing
    evidence-image recorder, `governor.py:264-286`) writes a harness-side
    manifest (file hash, mtime, producing command) for artifacts created
    in-session; the gate cross-checks PR-posted evidence against the manifest
    so stale or fabricated evidence fails deterministically.
  - **Reviewer reproduces, not just reads:** for feature PRs the reviewer
    re-runs the scoped test command and, where defined, a `.harness/demo-cmd`
    (analog of `.harness/test-cmd`) and compares observed output to the AC.
  Scale evidence strictness with risk (paths touched, diff size, priority).
- **P3.7 Doctor completeness.** Verify App permission scopes via API probe
  (#74), `TEST_CMD` exits within timeout (run it once at onboarding),
  commitlint detection, LLM-gateway reachability, and identity-vs-operator
  distinctness (#75 warning). Generate checks from the config schema (P5.1).

### Phase 4 — Token & cost efficiency

Ordered by estimated $ impact; P1.2's ledger turns these from guesses into
measured wins.

- **P4.1 Model routing at dispatch.** Restructure `FixTaskConfig` construction
  to per-dispatch (currently built once at daemon startup — `roles.py:97-109`),
  then route: deterministic heuristics first (diff-of-issue size, label
  `trivial|minor|major` from triage, file count), optional Haiku classify
  (revive `build_router_task`) as tie-breaker. Tiers: trivial → cheap model +
  $2 cap; standard → mid; complex/escalated → strong model. **Escalation
  ladder**: a retry after `agent_error` upgrades model rather than repeating
  the same configuration. (V6)
- **P4.2 Scope review context.** Validators receive the finding's hunk ±
  context, not the full diff (`tasks.py:673-699`); dimension specialists get
  per-file-group chunks above a size threshold. On a 1k-line diff this is the
  single biggest review saving (#80). Pair with **P4.3**: adopt the
  persistent-review-session design (docs/issues) or, cheaper interim, extend
  the delegated-mode "fresh session + prior-review-body" pattern to all modes
  so re-reviews stop replaying transcripts.
- **P4.4 Budget soft-landing.** At ~80% of budget, inject a harness nudge
  ("finalize now: commit, push, open PR / post verdict") instead of letting the
  hard cap vaporize completed work (#49/#50: real cost $0.42-$1.62 sessions
  killed with zero output). Mechanically: track cost from stream messages;
  one-shot system-reminder injection.
- **P4.5 Prompt diet.** Don't re-append AGENT.md on resumed legs (short delta
  reminder instead — it's already in-session); deduplicate the anti-tautology
  text stated 3× across AGENT.md/REVIEW_PROMPT; keep static-prefix ordering
  (already cache-friendly); summarize long comment threads once (Haiku), cache
  the summary in the store keyed by latest-comment-id (triage + resume both
  reuse it).
- **P4.6 Cap the scout.** Give the delegated-review scout its own sub-budget
  instead of riding the parent's (#51); journal per-leg attribution.
- **P4.7 Spend circuit breakers.** Per-issue lifetime cap (e.g. $25 across all
  retries → `needs-human-review`) and per-day global cap across repos (P5.3)
  — several past incidents burned $4-7 in silent loops; V1/V2 could again.
- **P4.8 Codex context balloon** (#71): cap AGENTS.md/ADR injection (retrieval
  pointer instead of dump), alert from the ledger when a session's input tokens
  exceed a threshold.
- **P4.9 Cross-leg prompt-cache sharing in multi-review.** Structure every
  dimension/validator prompt as `[shared static preamble][diff block]
  [leg-specific suffix]` on the same model, and run one leg first to warm the
  cache before fanning out the rest: the diff — the dominant token block —
  then cache-hits (~90% input discount) across the remaining 4 dimension legs
  and all validators instead of being paid at full price 6+ times.
- **P4.10 Dedup findings before validating.** Merge/dedup candidate findings
  (deterministic key or one Haiku pass) before spawning validators, and cap
  validators at top-N by severity — M is currently unbounded.
- **P4.11 Review once per green SHA.** Debounce review dispatch until CI is
  green for the head SHA (configurable): reviewing a red-CI push burns a full
  round that a fix-CI push immediately invalidates. Saves whole rounds, not
  just tokens per round.
- **P4.12 Concurrent fixes under leases (time, not tokens) — BACKLOG.**
  Caveat acknowledged: K>1 issues in flight *within* a repo requires conflict
  supervision — predicting file overlap from issue text (unreliable),
  serializing merges into the base branch, and absorbing extra rebase churn,
  which amplifies the single riskiest agent operation on record (#69,
  wrong-side conflict resolution). The design docs chose a single sequential
  fixer deliberately (05-fix-watcher.md, no area-locks). The free substitute
  is **parallelism across repos** — separate daemons share no files and need
  no supervisor. Revisit only if cross-repo parallelism is saturated and
  per-issue wait time demonstrably hurts; the minimal safe version is K=2
  with a hard path-disjointness check and merge serialization.

### Phase 5 — Multi-repo workhorse operations

- **P5.1 Typed config schema.** Pydantic (or dataclass+validators) schema for
  the ~70 knobs: fail loudly on unknown keys and missing required per-repo
  values, print the resolved config at startup, generate `doctor` checks and
  `agent.env.example` from the same schema. Kills the silent-default class
  (#16, #49, #50, #78) structurally.
- **P5.2 Process model: systemd template units per repo** (keep failure
  isolation; avoid a bespoke multi-repo scheduler) plus shared infrastructure:
  one App-token cache (already file-based), and —
- **P5.3 Global spend governor.** A small shared ledger file (flock-guarded)
  that every dispatch consults: daily/weekly $ cap across all repos, per-repo
  fair-share, hard stop + alert at the cap. This is the single most important
  guard before pointing the harness at *all* projects.
- **P5.4 Onboarding scorecard.** `issue-agent adopt`: probe the target (tests?
  CI? branch protection? commitlint? monorepo layout?), emit a readiness score
  + generated overlay with scoped `TEST_CMD` guidance (#77), and refuse
  `ai-ready` dispatch below a floor unless overridden (loud, not silent).
- **P5.5 Runbooks.** One page each: stuck-state recovery, breaker reset,
  identity/token rotation, "daemon dead, labels stranded", multi-repo status
  sweep. FAILURE-MODES.md stays the incident log; runbooks are the procedures.
- **P5.6 Consider webhooks only after P2.4/P2.5.** With one GraphQL call per
  tick and rate-limit awareness, 20s polling across ~10 repos is well within
  budget; webhooks (smee/cloudflared) add infra fragility for latency you
  don't need yet. Revisit at ~30+ repos.

### Phase 6 — Debt removal & single-sourcing

- **P6.1 Delete the legacy bash surface** (~1,635 LOC: `agent/`, `review/`,
  `steward/`, plus `tests/fakes/` ~18K and `tests/test_orchestration.sh`),
  `bin/bootstrap.sh` legacy mode, and `read_record.sh`/`dispatch.sh` if the
  codex path doesn't need them — per REFACTOR-SDK.md's own Phase-5 exit
  criteria, which ARCHITECTURE-STATUS.md prematurely declared met.
- **P6.2 Single-source the gate.** `domain/gate.py` becomes authoritative; the
  bash `checks/*.sh` either call `python -m issue_agent.gate …` or are
  generated — ending the manually-synced duplication (bash comments literally
  say "keep in sync with domain/gate.py").
- **P6.3 Docs reconciliation.** Mark 02–04 superseded by HARNESS.md; fix the
  HARNESS.md-vs-CLAUDE.md bootstrap and config-layout contradictions; document
  the multi-mode reviewer, CodexRunner, and Jira sync (currently invisible in
  design docs); one diagram toolchain (pick d2, delete drawio + root svgs).
- **P6.4 Decide the steward's fate explicitly.** The designed memory-bearing
  steward (journal, drift mining, invariant promotion — 07-gardener-steward.md)
  exists only in dead bash; the Python steward is triage-only (69 lines).
  Either descope the design docs to match, or schedule the port — the current
  state silently drops the harness's designed defense against same-model
  blind spots. Recommendation: descope now, revisit after Phases 1-4 prove out.

### Phase 7 — Testing strategy upgrades

- **T1 Contract tests against recorded reality.** Record real `gh`/`git`
  outputs (sanitized cassettes) for every adapter behavior the fakes model;
  run the fake suite against the cassettes so fidelity gaps (#30, #46, #47)
  fail in CI instead of in production.
- **T2 Chaos suite.** Fault-inject at the seams: kill the daemon between
  actions, wipe the state dir mid-lifecycle, expire the App token, return 403
  rate-limit from `gh`, hang `TEST_CMD`, SIGKILL the agent subprocess.
  Assert the invariants: no label stranded beyond one reap cycle, no duplicate
  dispatch, no repeated-decision loop, breaker states monotone until
  supersession.
- **T3 Loop-convergence property test.** For the pure domain: from any
  reachable world state, repeated `decide_*` with an unchanged world must
  reach a fixed point (no action) within N ticks. This structurally catches
  the entire feedback-loop class (#13, #14, #39, #64, V1, V2).
- **T4 Nightly e2e.** Drive `examples/demo-target` through the full lifecycle
  (issue → PR → review → merge) under `ISSUE_AGENT_MAX_TICKS` in CI with a
  cheap model; assert on GitHub end-state, not logs.

---

## 5. Sequencing & effort

**Committed work = the lean core in §4 (≈2–3 weeks).** The table below is the
full-plan reference for if/when backlog items are triggered; it is not a
commitment.

| Phase | Content | Effort | Depends on |
|-------|---------|--------|-----------|
| 0 | Correctness fixes V1-V10 | 2-4 days | — |
| 1 | Journal, ledger, status v2, alerts | 3-5 days | — |
| 2 | Reconciler, leases, GraphQL, systemd-first | 1-2 weeks | 1 (measure) |
| 3 | Enforcement hardening | 1-2 weeks | 0 |
| 4 | Token efficiency | 1-2 weeks | 1 (ledger) |
| 5 | Multi-repo ops, spend governor, schema | 1 week | 1, 2 |
| 6 | Debt deletion, single-sourcing, docs | 2-4 days | 2 (SDK-only) |
| 7 | Contract/chaos/property/e2e tests | ongoing, start in 0 | — |

Rationale: observability (1) precedes the refactors it must measure;
correctness (0) precedes efficiency (4); enforcement (3) precedes scaling to
all repos (5) because blast radius grows with adoption; T3's loop-convergence
test should land with Phase 0 since it guards exactly the bugs being fixed.

## 6. Success metrics (from the P1 ledger)

- $ per merged PR (median + p90) and tokens per review round — trend down.
- Cache hit-rate on system-prompt blocks — confirm >0, then optimize.
- Wedge incidents (session alive >wall-cap, daemon frozen) — target zero.
- Stranded-label incidents and repeated-decision loop detections — target zero.
- Breaker trips by `ExitReason` — infra kills must stop counting (P0.3).
- GitHub API calls per tick — order-of-magnitude drop after P2.4.
- Human interventions per merged issue — the real workhorse KPI.

---

## Appendix A — Implementation pointers for the lean core

| Core item | Where to change | Notes |
|---|---|---|
| P0.1 `set_resumed_at` | write side: `app/workflows/fix.py` resume path; read: `domain/fix.py:118-134`, `app/roles.py:57`; def: `store.py:92` | Regression test: resume dispatched, outcome stays `ai-blocked`, unchanged thread → no re-dispatch |
| P0.2 ADDRESS_REVIEW gate | `domain/fix.py:368-381`; `domain/outcome.py:229-242` (`clear_retry=True` at 242); applied `app/workflows/fix.py:168` | Gate on review commit vs head OID / `submitted_at` vs last push; clear counter only on superseding review |
| P0.3 ExitReason enum | `adapters/agent_runner.py:140-167,352-357,398-452`; `codex_runner.py:180-186`; `domain/outcome.py:59-114` (classify), `205-222` (resume divergence) | resolve-ERROR→`ai-ready` vs resume-ERROR→`ai-blocked` divergence: document as transition table |
| P0.4 wall-clock cap | `agent_runner.py:330-363` (`_poll_slice` loop, 30s slices — do NOT cancel pending `__anext__`, see comment 311-329); `codex_runner.py:478-521` | New knob `AGENT_MAX_RUNTIME_S`; idle default 900s at `config.py:222` |
| P0.5 test-run timeouts | `adapters/worktree.py:153-158` (`run_tests`/`revert_and_run`, currently no timeout); parity: `governor.py:210-217` (edit-check already 120s) | Timed-out gate run must DENY |
| P0.6 check-push fail-closed | `plugin/hooks/checks/check-push.sh:28`; Python mirror `domain/gate.py` push gate | Both copies until P6.2 single-sources |
| P0.7 pagination | `github.py:352-436`; `--limit 200` at 412, 427 | At minimum: loud warn when count == limit |
| P0.8 pgrep anchor | `bin/point-at.sh:160` | Substring match; flock in `single_instance.py` is authoritative |
| P0.9 token-mint retry | `adapters/app_token.py:88-96` (raises on failure; refresh margin 300s) | 3 retries + backoff |
| Journal + ledger | new `adapters/journal.py`; hook `app/execute.py:56-107`, `app/dispatch.py`, agent-result path, `loop.py:35-50`; surface in `cli.py` status (~:515) | Cost/cache tokens from `ResultMessage.usage` (`agent_runner.py:140-167`) |
| Rate-limit detection | `github.py:548-568` (`_subprocess_runner` — errors currently → `""`) | Return `ApiError` sentinel distinct from empty |
| Fixer deny-list | `agent_runner.py:71-137` (`build_option_kwargs`); fixer task builders `app/planners/tasks.py:59-65` (no allowed/disallowed by design note :13-14); review/steward pattern to copy: `tasks.py:335-339,703-706` | Port deny semantics from `plugin/settings.json:32-41` (dead on SDK path) |
| TEST_CMD fail-closed | `plugin/hooks/checks/_pr_common.sh:61-67,136-138` (warn-only today); `domain/gate.py`; new `ALLOW_UNTESTED` knob in `config.py` | |
| Spend caps | ledger sum + check in `app/dispatch.py` before `RunAgent` | Per-issue lifetime + global daily |
| AGENT.md skip on resume | `app/planners/tasks.py:62,104,154,197,213,238,283,316` (all legs re-append `cfg.agent_md`) | Resumed legs already hold it in-session |
| Validator scoping | `app/planners/tasks.py:673-699` (validators get full diff) | Finding hunk ± context |
| Screenshot rule | new check in `plugin/hooks/checks/check-pr.sh` (§5 area, 71-81); reviewer side `plugin/REVIEW_PROMPT.md` proof-of-work dim; agent side `plugin/AGENT.md` | Lean version: no manifest |
| T3 loop-convergence test | `tests/domain/` — repeated `decide_*` on frozen world must reach fixed point | Guards #13/#14/#39/#64/V1/V2 class |
| Config print + fail-loud | `config.py:332-363` (validate), `401-481` (from_env, ~70 knobs); called `main.py:110` | |

## Appendix B — Runner / adapter evidence (audit dossier)

- `AgentRunner.run` outcome paths: (a) ResultMessage → `result_to_agent_result` (`agent_runner.py:140-167`; budget subtype folded into `status` at 150-151); (b) idle-timeout → `is_error, status="timeout"` (352-357); (c) **pre-result exception re-raised** (398-415) — coerced to AgentResult only by `_gather_legs` (`workflows/review.py:128-141`); all other callers rely on `Daemon.tick`'s generic per-action catch (`loop.py:46-49`), masking cause; (d) clean end, no ResultMessage → `is_error` "no ResultMessage" (416-452).
- Resume: `build_option_kwargs` passes `resume=session_id` (109-110); persisted via `Store.save_session`/`save_pr_session` (`store.py:74-86`); fork after 5 resumes (`fix.py:143`, `dispatch.py:38`). Every run is still a cold subprocess.
- Multi-review orchestration: `workflows/review.py:143-249`; dims gathered 186-191, validators 209-214; budgets `config.py:160-162` (gate $0.10 haiku / dim $0.75 / validation $0.20 haiku).
- Codex: spawn `start_new_session=True`, stdin DEVNULL (`codex_runner.py:452-466`); `os.killpg` teardown (669-698); stderr pump + idle watchdog (478-521); `post_hang_drain_timeout` 30s (544-557); transport retries=4 (414-436), 429 retries=5 honoring Retry-After (381-411); `preflight()` checks binary+key only, no gateway probe (288-309).
- GitHub: one subprocess per `gh` call, 120s timeout (`github.py:538-547`); non-zero exit → `""` (548-568); world builds 352-436 ≈ base 5 + O(issues) + 2·O(PRs) calls; `_safe_get_pr` isolates bad PRs (319-335); no GraphQL/ETag/rate-limit anywhere.
- Store: atomic tmp+`os.replace` (48-53); reads degrade silently to defaults (41-46, 61-68). flock per (role, repo-hash) (`single_instance.py:51-82`), crash-released, exit(3) on conflict.
- Loop: both tick phases crash-isolated (`loop.py:35-50`); heartbeat 900s + suspend detection >4× poll (52-80); logging = raw stderr (`roles.py:44-51`); action log in `execute.py:56-107` is the only observability surface.
- Governor adapter: `build_hooks` (`governor.py:377-404`); fail-open on own exceptions (357-365) but violations checked first/fail-closed (151-153); `run_in_background` denied unconditionally (343-350).

## Appendix C — Enforcement evidence (audit dossier)

- `governor_policy.classify()` covers ONLY: `git commit`, `checkout -b`/`switch -c`, `git config` (hookspath), `git push`, `gh pr create` (`governor_policy.py:200-239`). Everything else → unconditional allow.
- Confirmed bypasses: `gh api` PR/repo mutations (unclassified); `python3 -c`/`node -e` subprocess indirection; `sh -c`/`bash -c` (not in `_PASSTHROUGH_WRAPPERS` :157, payload string never re-parsed); write-then-exec (heredoc/redirect bodies intentionally ignored, :90); `xargs git …`; `export HUSKY=0` in an **earlier statement** (env only attaches when command-prefixed, :72-91); alias indirection; `arg_value` drops `command_substitution` → `git commit -m "$(cmd)"` reads as empty msg and skips prefix/order checks (:56-59 + `check-commit.sh:44`); Edit/Write to `.git/hooks|config` unrouted (Bash-only matcher, `governor.py:401`). Handled correctly: `git -C`, `env HUSKY=0 git commit` (:140, :160-197).
- `plugin/settings.json:32-41` deny rules (curl/wget/gh secret/gh auth/.env/.pem) are **dead on the SDK path** (`agent_runner.py:134-136` loads only target-project settings).
- `check-pr.sh` map: §0 body/base + `--web` reject (30-38) · §0b base==BASE_BRANCH (46-49) · §1 tests (52) · §2 AC section (55-56) · §3 closing keyword (60-61) · §4 `.harness/ac-posted`,`test-plan-posted` (66-67) · §5 branch-type test file in diff (71-81) · §6/7/7b hygiene/evidence/test-immutability (84-86) · §7c red-fail (89-90; `_pr_common.sh:135-189`) · §8 adversarial PASS marker (92-95) · §9 ADR ref (97-113). `check-push.sh` re-runs §1/6/7/7b when an open PR exists (28-29).
- Fail-opens: TEST_CMD/`.harness/test-cmd` empty → stderr warn only (`_pr_common.sh:61-67,136-138`); red-fail opens on unresolvable base (145), failed revert checkout (172), pure-test diff (160); `check-push.sh:28` opens on `gh pr list` failure; `check-commit-order.sh:64-67` degrades without BASE_BRANCH. All `.harness/` markers self-attested/forgeable.
- Ops: `point-at.sh:160` unanchored pgrep; `trap 'kill 0'` (190); logs world-readable `/tmp`. `doctor.sh` misses App scopes / TEST_CMD-exits / commitlint / gateway reachability (58-79); has governor self-test (17-23).
- Prompt sizes: AGENT.md 11,915 B ≈ 2.9–3.0k tok; REVIEW_PROMPT.md 5,755 B ≈ 1.4–1.5k tok; only `$ROUND_NUMBER` varies per-invocation (near end, line 79); anti-tautology text duplicated 3×.

## Appendix D — Cost evidence (audit dossier)

Per-operation map (model / cap / inputs / sessions):
- fix resolve|resume|fix-ci|address-review|rebase: `FIX_MODEL` (blank→SDK default) / $10 (`config.py:141`) / AGENT.md + issue or thread or review body or last-150-line CI log (`tasks.py:200-317`) / 1 session, resumed legs replay prior transcript server-side (O(rounds) cost until 5-resume fork).
- triage: sonnet / $0.75 (`config.py:143`) / ~800-tok instructions + issue body + **full unfiltered thread** (`workflows/steward.py:33`, `tasks.py:717-775`) / 1 stateless.
- review single|delegated: `REVIEW_MODEL` / $8 / full unbounded `gh pr diff` (`github.py:461-462`) + REVIEW_PROMPT; delegated adds sonnet scout whose spend **rides the parent budget uncapped** (`tasks.py:357-362`; incident #51).
- review multi, per round: gate haiku $0.10 + 5 dims `REVIEW_MODEL` $0.75 each + M validators haiku $0.20 each — **each leg gets the full diff** (`tasks.py:370-404, 626-699`); all fresh/stateless every round.
- Jira sync: separate session per lifecycle transition, full Atlassian MCP attached, $0.50 (`roles.py:262-279`).
- Idle polls: $0 (pure decisions; LLM only on `RunAgent`).
Key facts: `build_router_task` dead (`tasks.py:320-330`, zero callers); `FixTaskConfig` built ONCE at daemon startup (`roles.py:97-109`) — per-issue routing requires per-dispatch construction; delegated re-review once blew $6 (~$7/round) via resume-replay, fixed by fresh-session + prior-review-body (`tasks.py:577-583`) — same pattern still applies to all fixer resumed legs; no `cache_control` anywhere, ordering favorable (static system prefix), hit-rate unmeasured. Observed per-fix costs: $0.29–$4.79 (FAILURE-MODES #7, #39, #49).

## Appendix E — Domain evidence (audit dossier)

- `decide_fix` priority (fix.py:255-411): reap orphaned `ai-working` → ONE dispatch (RESOLVE > RESUME > REBASE/ADDRESS_REVIEW/FIX_CI per PR) → lock-free EnableAutoMerge/UpdateBranch → cleanup. ~155-line function.
- Edge-triggered inventory: `resumed_at` (read fix.py:118-134 / roles.py:57; **write missing** — V1); `last_sha` re-review (review.py:82-88, correct); triage `seen_at` equality (triage.py:113-120; written post-refetch `workflows/steward.py:69` — #14 fix intact); retry counters by `breaker_key` (bump `workflows/fix.py:190,204` + `outcome.py:253-254`; clear-on-success 241-242 — V2); automerge one-shot marker (fix.py:143).
- Breakers: caps in `FixConfig` (fix.py:54-57) ← `roles.py:92-95`; `breaker_tripped`/`retry_breaker` (fix.py:242-245, lifecycle.py:135-148; cap≤0 = unbounded); escalation → `needs-human-review` (lifecycle.py:151-168).
- Reaper: `reap_actions` (fix.py:174-186) runs every tick (fixer_busy always False, roles.py:80) — in-process self-heal works; the gap is process death without restart (foreground point-at; systemd units already have Restart=always).
- ERROR fallback divergence: resolve→`ai-ready` (outcome.py:80-114) vs resume→`ai-blocked` (205-222) — intentional but undocumented.
- Action execution non-atomic (per-action try/except, loop.py:45-49); follow-up relabels run inside the workflow's nested `executor.execute_all`; no store↔GitHub reconciliation sweep exists; no guard against two roles sharing a STATE_DIR.
- `domain/` verified pure (stdlib-only; impure seams injected, `gate.py:342-447`); no shared transition table encodes legal label moves.

## Appendix F — Docs / dead-weight evidence (audit dossier)

- Python steward = triage-only stub (`workflows/steward.py`, 69 lines); designed memory-bearing steward (journal/metrics/feedback-clusters, drift mining, invariant promotion — 07-gardener-steward.md) exists only in dead `steward/steward.sh` (401 LOC). Master agent (08) = `bin/master.sh` (28 lines), never ported.
- Dead weight: `agent/` 948 + `review/` 294 + `steward/` 401 LOC; `tests/fakes/` ~18K; `tests/test_orchestration.sh` 493 lines; `bin/bootstrap.sh` 134; diagrams tripled (root svgs + `d2/` + `drawio/` incl. 3 generator scripts). `make lint` still shellchecks all of it.
- Contradictions: HARNESS.md Quickstart still says `make bootstrap` + commit `.claude/` into target (vs CLAUDE.md's no-mutation point-at) and `cp config/agent.env.example config/agent.env` (vs overlay layout); 02–04 docs frozen pre-HARNESS.md's 31-row constraint table; ARCHITECTURE-STATUS.md declares all phases done while REFACTOR-SDK.md Phase-5 deletions are undone.
- Undocumented-but-live: multi-mode reviewer (`REVIEW_MODE=single|multi|delegated`), CodexRunner, Jira sync.
- `docs/issues/review-no-resultmessage-background-tasks.md` is stale: `run_in_background` denial landed (`governor.py:343-350`).

## Appendix G — Open questions (do not re-derive; verify)

1. Does anything deny Monitor/Agent (background-capable) tools for **fixer** tasks? Review/steward have `REVIEW_DENY`; fixer appears to have none — if so, the #2 no-ResultMessage drop can still occur on the fix path.
2. #66 (triage candidates suppressed) root cause never confirmed; the `--limit 200` cap (V9) is the leading candidate.
3. Prompt-cache hit-rate is unknown until the ledger lands — do not assume caching works.
4. #71 Codex 2.1M-token balloon cause unconfirmed (suspected ADR/AGENTS.md dumps + accumulation).
5. #74: does the GitHub App now have Checks:Read + Commit-statuses:Read? Reviewer currently degrades to empty checks.
6. Whether the interim bounded-retry mitigation from `persistent-review-session.md` was ever landed.
