# Implementation review — architectural hardening landing (for Fable review)

**Date:** 2026-07-31 · **Reviews:** `dev` @ `51cd8d2` (58 commits over `16c6783`,
+20.7k/−3.4k in `src/`, 43k lines total incl. tests) · **Method:** five parallel
adversarial Opus reviewers (one per seam) + independent hand-verification of the
sharpest claims. **Plan of record:** `docs/open/2026-07-30-architectural-review-and-plan.md`.

---

## 1. Verdict

**The implementation is genuinely impressive and genuinely not ready for an
unattended maiden voyage.** Both halves of that sentence are load-bearing.

The *hard cores* landed and are real, verified in code and by test: an atomic
`BEGIN IMMEDIATE` budget reservation with a true multi-process race test; a
correct event envelope + transactional outbox + idempotent-by-`event_id` fold;
careful torn-append recovery; the governor tri-state resolver that closes the
five previously-xfailed indirect-execution bypasses; the two original
supervision P0s (SIGTERM teardown, boot orphan-reaping) properly fixed;
integer-micro `Money` with disjoint `TokenUsage`; and a value-object layer
(`domain/contracts.py`) that is the highest-quality artifact in the change. The
test suite is **2,970 cases green, 0 skipped, 0 xfail**, and — with two named
exceptions — it is credible behavioral evidence, not tests written to pass.

But the landing has a **uniform pattern across all five seams: excellent cores,
half-completed integration, and half-delivered governance.** That pattern
produced **~10 maiden-voyage blockers**, several independently reproduced,
several cross-corroborated by two reviewers who did not share notes. Four of
them are silent-failure modes on exactly the surfaces the first run is meant to
validate (the dashboard, the spend caps, the governor, a laptop suspend). The
plan-of-record and `residuals.md` currently claim these phases are "IMPLEMENTED
AND VERIFIED," which is false for several and should be corrected before the
claim is used as a warrant for sailing.

**Recommendation:** do not point this at a repo you care about, or run it
unattended, until the Tier-1 blockers below are fixed. A **supervised**
foreground canary (`point-at`, not systemd, `ISSUE_AGENT_MAX_TICKS=1`, trivial
self-authored issue, all caps set before start, machine that will not suspend)
is reasonable *after* Tier-1, to gather the evidence the current canary did not.

---

## 2. How to read this report

Each blocker is tagged with its **evidence grade**:

- **[REPRODUCED]** — a reviewer (or I) triggered the failure and observed it.
- **[CORROBORATED]** — two independent reviewers found it separately.
- **[CODE]** — established by reading code + tests, not run.

I personally re-verified, with my own tools: the governor recursion fail-open
(exit code 1, not 2), the pyrefly gate being off (0 vs 254 errors), the port
param-drift being positional (latent, not a live crash), the resolver denying
the five bypasses live, and the SIGTERM handler + boot `recover()` being wired.

---

## 3. Maiden-voyage blockers

### Tier 1 — fix before any run against a repo you care about

**B1. Governor resolver crashes fail-OPEN on both enforcement paths.**
`[REPRODUCED, verified by me]`
A deeply nested `$( … )` payload makes `governor_policy.resolve()` raise
`RecursionError` (no depth bound in `extract_commands_with_env.visit` /
`arg_value`, no `except` in `resolve`). The in-process hook catches `Exception`
and returns `{}` (ALLOW, by its documented fail-open design); the bash/codex
hook (`plugin/hooks/bash_governor.py`) has no top-level guard and exits **1**,
where only exit **2** blocks. I confirmed both: `resolve(...)` → `RecursionError`;
`bash_governor.py < payload` → `exit=1`. Because this same hook is the *only*
enforcement point for the PR/push gates on the codex path, one wrapper walks a
`gh pr create` / `git push --force` past every gate. Directly falsifies
`residuals.md:22` ("the governor now denies command shapes it cannot resolve").
*Fix:* depth-bound the parser (return `Unresolvable` past N); wrap
`resolve`/`find_violations` in `evaluate_command` so a resolver exception denies;
add `except BaseException → exit 2` to `bash_governor.main()`. Add a test
asserting exit 2 on the pathological payload. **Effort: small.**

**B2. `bash_governor.py` omits `durable_wrapper` → publication-authority env
override is unblocked on the codex path.** `[REPRODUCED by reviewer]`
`bash_governor.py:191` calls `bypass_reason(...)` with 4 args, dropping the 5th
(`durable_wrapper`), which is the *only* trigger for the `_PUBLICATION_AUTHORITY_ENV`
guard. The in-process path (`find_violations`) passes it; the hook does not — the
twins diverge. Live: `ISSUE_AGENT_RUN_ID=evil issue-agent-publish git push …`
→ DENY in-process, **rc=0 ALLOW** via the hook. A paid codex child can point the
durable broker at a forged run id / control DB. The tests that "cover" this call
`find_violations`, never the hook, so they pass while the hook is broken — false
confidence. *Fix:* pass `g.durable_wrapper`; add subprocess exit-code tests per
authority key. **Effort: trivial (one arg) + tests.**

**B3. `control.db` silently self-heals to empty, resetting all spend and caps to
zero.** `[REPRODUCED by reviewer]`
`SQLiteControlStore._connect()` opens with default create-on-open. Every read
path (`snapshot`, `leases`, `flush_outbox`, …) can therefore recreate a *deleted*
DB as an empty file; the spend gate polls `snapshot()` every tick, so this is
near-certain if the DB is removed. The next boot sees a non-zero-byte file, the
authority witness validates, migrations replay from empty, and `initialize()`
reports **success with every charge, reservation, and cap at zero.** The DB now
lives at a machine-wide `~/.local/state/issue-agent-control/control.db` — outside
any repo, exactly where a stray cleanup/rsync/restore hits it. *Fix:* open
read/write-only (`?mode=rw`, `uri=True`) everywhere except `initialize()`; a
missing DB then raises → maps to `CORRUPT`/`UNAVAILABLE` (blocked) and the
witness guard fires. **Effort: small + regression test.**

**B4. Retained reservations are never reclaimed while running → an armed cap
self-deadlocks within hours.** `[CORROBORATED — both accounting reviewers]`
`AccountingRuntime` deliberately (and correctly) *retains* a reservation without
settling when a leg returns no usable usage — and a Claude leg returning
`cost_usd=0, usage=None` is a **documented real outcome** (a 906s session with
four commits). But the only reclaimer, `recover()`, runs **only at boot and on
suspend-wake**. So each usage-blind leg permanently holds its full
`FIX_BUDGET_USD` maximum against the daily cap for the daemon's lifetime. A
handful wedge the whole deployment into `EXHAUSTED` with no operator-visible
cause and no remedy but a restart nobody knows to perform. *Fix:* run the
reservation-sweep arm of `recover()` on a bounded tick cadence (it is idempotent;
lease-liveness probes are already outside the write txn), and emit a
dashboard-visible signal on the retain-without-settle branches. **Effort: medium.**

**B5. Suspend-wake `recover()` kills the daemon's own in-flight paid agents and
releases their live reservations.** `[CORROBORATED — supervision + accounting]`
The stall branch (`loop.py:696`) fires when `elapsed ≥ poll×4` and a monotonic
gap is detected — an ordinary **laptop lid-close** satisfies both (CLOCK_MONOTONIC
doesn't advance across suspend). On wake it calls `recover_orphans()`, which
reaps *every* registration with **no liveness/ownership check** (unlike its
sibling `recover_abandoned_orphans`, which does check) — killing every live
codex/claude session this process owns, while the worker threads running those
legs are still executing. The accounting reviewer found the same event from the
other side: the released reservation makes the subsequent `settle()` return
`CONFLICT`, which raises `RuntimeError("settlement failed closed")`, losing the
charge and killing the leg, plus a false `agent_run_abandoned`. This turns a
routine suspend into "all paid work destroyed mid-flight." *Fix:* the wake caller
must reap only registrations whose launcher is not this live process (the
predicate already exists in `recover_abandoned_orphans`); add a lease/grace
window so a reservation in its spawn window is never swept. Pin both directions
with tests. **Effort: medium.**

**B6. Telemetry failures are not isolated from operational state — two instances,
one class.** `[REPRODUCED + CORROBORATED across two reviewers]`
The plan's core decision ("telemetry is never the sole authority") is violated on
two paths where a telemetry write can abort or corrupt operational work:
- **(a) Encoding failure rolls back the authoritative control-store transaction.**
  Envelope validation and the outbox size-limit run **inside** `transaction()`
  (`control_store.py:678-706`). The effect-receipt path embeds
  `EffectReceipt.error` = `GhUnavailable.detail` = the *full unbounded gh stderr*;
  a large gh failure body aborts the receipt commit for a mutation that **may
  already have hit GitHub** — the harness then retries and double-applies,
  reopening #64. Reproduced: a 70 KB world-observation event → `ValueError` →
  zero rows committed.
- **(b) A telemetry emit failure aborts `recover()` every boot.**
  `TypedJournal.abandon_run` calls `self.events.emit(...)` **unguarded**
  (`typed_journal.py:80,90`) — unlike `emit`, which swallows — and
  `app/recovery.py` has no `try`. A full disk or over-limit line raises straight
  out of `RecoveryCoordinator.recover()` *after* orphan kills and lease reclaim
  have run, leaving recovery half-applied on every boot. The port's own docstring
  (`ports.py:283`) promises the opposite ("telemetry failure never changes
  operational recovery state").
*Fix:* bound every free-text field at the source; make `_event`/`_transition`
never raise into the transaction (enqueue a bounded `TelemetryWarning` / bump a
counter instead); wrap the two `abandon_run` emits in the same guard as `emit`.
State and recovery commits must be unconditional. **Effort: medium.**

### Tier 2 — fix before an *unattended* run; each is a silent-failure mode on a validation surface

**B7. The interactive dashboard is broken under v2 — blind, and crashable.**
`[REPRODUCED + CORROBORATED across two reviewers]`
Two defects on the port-8321 operator surface:
- **(a) Blindness.** `app/dashweb/snapshots.py` was never converted to v2 — it
  reads typed envelopes as legacy journal dicts at ~30-35 sites, so every `.get()`
  returns `None` and the "no match" branch fires. Silently lost: start/elapsed
  clocks, blocked reasons (#121), turns/usage, deferred/hold badges, stall
  corrections, human-action, escalation, model attribution. Both reviewers
  reproduced blank cards from a real emit→fold→`issue_card` harness.
- **(b) One bad line crashes the whole dashboard.** `decode_event` is called
  unguarded (`app/dashboard.py:111`, `dashweb/system.py:171`); the feed is chosen
  by `glob(f"{path}*")` in the same dir as `journal.jsonl`. A single stray pre-v2
  line or a newer daemon's unknown event type → `ValueError` → the CLI
  `dashboard --text/--html` and the HTTP API both hard-crash. Reproduced twice.
- **Note:** the v2 path is a *parallel* fold selected by file-existence, so a
  **fresh deployment before its first v2 write renders through the legacy
  regex-over-prose fold** — the "fold 11→1" metric did not happen (nothing was
  deleted; three copies of `_int/_float/_stamp_date`, two of `parse_ts`, the
  `import *` block, and `build_deployment` all survive).
*Fix:* convert `snapshots.py` to consume typed payloads off `ReadModel`; guard
`decode_event` per line into a counted `warnings` list; delete the legacy fold
and select v2 unconditionally; port the 222 legacy test literals to v2 fixtures
built by the real emit path. **Effort: medium-large.**

**B8. The pyrefly type gate is disabled (`preset = "basic"`), so the ports are
unenforced annotations.** `[REPRODUCED, verified by me]`
`make typecheck` reports `0 errors`; removing the single `preset = "basic"` line
surfaces **254** (I confirmed 0 → 254). Plan 1.8 ("missing protocol methods
become build failures") is delivered in name only. The suppressed set includes
real port-conformance drift: 6 `WorkflowState`↔`Store` parameter-name
mismatches, `ControlStore.flush_outbox` signature drift, and two daemon builds
passing `None` where `FixWorktreePort` is declared non-optional. **Nuance I
verified:** those specific drifts are currently *latent*, not live crashes — the
`Store` methods are called **positionally** (so `retries` vs `n` doesn't bite),
and the `None` worktrees are the review/steward daemons that don't do worktree
ops. So this is not "254 live bugs"; it is "the gate that was supposed to hold
the ports is off, so the next drift is invisible." That is still a blocker for a
system whose entire Phase-1 thesis is typed seams. *Fix:* delete the preset;
clear the port-conformance subset; if the full 254 can't clear pre-launch, raise
the preset with per-rule suppressions that each carry a removal issue. **Effort:
medium (triage-bounded).**

**B9. The 17 `getattr` shims were renamed, not deleted — 15
`isinstance(x, <runtime_checkable Protocol>)` probes preserve the C2 degradation
class.** `[CODE]`
`runtime_checkable isinstance` checks attribute *names* only — byte-for-byte
`hasattr` semantics. Nine micro-protocols exist solely as probe targets, and the
docstrings still say the motivation is "tolerate an injected fake that lacks the
method." Six are **write-side** (`BranchState` ×2, `PredictionWriter`,
`CiPendingState`, `IssueStateReasonReads`, `RecentMergesReads`): a production
adapter that drifts a name (see B8) silently *skips a durable write* instead of
failing. Combined with B8 (no typechecker to catch the drift) this is a live
silent-corruption path, not a style nit. *Fix:* per probe, delete (rely on the
declared port) or make it explicit `X | None` with a null object; add an
architecture test forbidding `isinstance(_, <…Reads|State|Writer|Runtime>)` in
`app/`. **Effort: medium.**

### Tier 2b — systemd only

**B10. All three systemd units fail to start.** `[REPRODUCED empirically by reviewer]`
`Type=notify` + `NotifyAccess=main` behind a non-exec'ing `uv run`: systemd's
MainPID is `uv`, so the daemon's `READY=1` (sent from the python grandchild) is
rejected; the unit times out at `TimeoutStartSec`, is killed, and
`StartLimitBurst=3` puts it in permanent `failed` within ~5 minutes. The reviewer
reproduced this with a transient unit of identical shape and verified
`NotifyAccess=all` fixes it. The release canary was **foreground-only** — the
systemd path has never been live-booted. *Fix:* `NotifyAccess=all`, or resolve
the venv interpreter absolutely and `ExecStart` it directly so the daemon is
MainPID; assert `active (running)` at install time. **Effort: small.**
*(Not a blocker if the maiden voyage uses `point-at`/foreground, which works.)*

---

## 4. What is genuinely good (preserve these under any refactor)

Verified by the reviewers in code and test, not taken on trust:

- **Atomic budget reservation is real.** One `BEGIN IMMEDIATE` transaction does
  balance-check + insert; the predicate is exactly `settled + active + requested
  ≤ cap`; there is *no* check-then-act window. Tested with real `spawn`
  multi-process races and a 200-reservation/32-thread settle/release test
  asserting exact final balance. DB-enforced terminal states (CHECK + UNIQUE +
  a `settled ⟺ exactly one charge` invariant). This is the strongest work in the
  change.
- **Fail-closed accounting.** Corrupt/disk-full/read-only → `UNAVAILABLE`/`CORRUPT`
  → paid admission blocked; free work (reconcile/reap/cleanup) continues. The
  original `except OSError: return []` zero-spend bug class is gone.
- **The event envelope + outbox are correct.** Full identity (schema_version,
  sortable event_id, per-writer seq, occurred/observed/monotonic, producer,
  correlation incl. `leg_id`/`stall_id`), total order by `(ts_ms, producer, seq)`,
  transactional at-least-once outbox with idempotent-by-event_id fold, and
  careful torn-append truncation with a golden-fixture registry test that emits
  *every* type through the real path.
- **Corrections reference ids (not clusters); world-observation tombstones exist.**
  #116 phantom-running legs are structurally fixed; the 1-second-collision
  heuristic and 2.0s tolerance are gone.
- **The governor tri-state resolver closed the enumerated wrapper class.** The
  five formerly-xfailed bypasses now deny as real assertions (I verified live:
  `sh -c`, `eval`, functions, `-c alias`, `find -exec` → `Unresolvable`;
  `timeout`/`sudo` correctly resolve *through* to the push gate). UNKNOWN_OUTCOME
  handling in `effects.py` never blind-retries — it reconciles first. `SubmitReview`
  is fully migrated to durable command + receipt (C15 closed), enforced by a
  non-vacuous architecture test.
- **The two original supervision P0s are properly closed** — real SIGTERM handler
  into the existing teardown; durable pid+start_ticks orphan receipts (PID-reuse
  safe) reaped before first dispatch; a generation-token protocol that makes a
  child spawned during shutdown refuse to exec; bounded drain (no more unbounded
  `pool.shutdown(wait=True)` wedge). `single_instance.py` and the installers are
  better than the plan asked.
- **The value objects** (`Money`, `TokenUsage`, `PriceQuote`, `BudgetScope`) —
  integer micros, disjoint counters with `trusted_for_accounting` excluding
  stderr, tz'd periods tested across DST/leap-day.
- **The untrusted-content encoder is airtight** (delimiter cannot be reproduced
  inside its own block; the `Untrusted` type raises on raw `str` at runtime),
  and marker demotion is real (gate readers for `ac-posted`/`adversarial-verdict`
  are gone; the screenshot receipt is daemon-HMAC-signed).

---

## 5. Test-trustworthiness verdict

**HIGH for behavior, with two structural blind spots.** Across ~45 tests read in
full, the ratio was ~40 behavioral / 3 tautological / 2 vacuous. The
discriminator is that the hard paths have *negative and positive controls*:
missing rollout must not fabricate spend; a dead launcher must leave the marker
unwritten; a short write must leave `b""`; a live sibling must survive orphan
recovery; disk-full must fail *closed*. The green is not hiding skips (0 skipped,
0 xfail). The `InMemoryGitHub` fake is a real state machine with
optimistic-concurrency guards, not a canned stub.

The two blind spots matter:
1. **The suite cannot vouch for the ports** — with the typechecker off (B8) and
   the one port contract test being name-only `isinstance`, green tells you the
   *behavior* is right and *nothing* about whether the ports hold.
2. **Two silent-drop paths in `fold` are unasserted** (`projection.py:72` a
   second start overwrites a completed run; `:74-78` a completion whose start
   rotated out vanishes) — both corrupt the `$/merge` north-star metric quietly.

Net: trust the green as evidence the *cores* work; do not trust it as evidence
the *integration* is complete — the four Tier-2 blockers are all things a green
suite passed over.

---

## 6. Documentation over-claim (correct before the docs are used as a warrant)

- The plan-of-record header and phase tables say **"IMPLEMENTED AND VERIFIED"**
  for work that diverged: **Phase 4.7 `CONTROL_STORE_MODE` (shadow/canary/rollback)
  does not exist** — money was cut over with a hard switch, `spend.jsonl` is no
  longer written, and there is **no rollback path**. The import is idempotent and
  there is an *offline* comparator (`scripts/verify_control_migration.py`), but
  the shadow window decision-3.4 called "not recoverable by a re-render" was not
  built. *Mitigation before first paid run: take and digest a `control.db` backup
  immediately after `migrate_legacy_spend` and preserve the real `spend.jsonl`
  read-only.*
- **Phase 5's "governor bypass class closed by construction" is overstated** —
  the *enumerated* wrapper class closed; **script-file execution (`bash file.sh`,
  `./x.sh`), `script -c`, `node/perl/ruby -e`, and `git rebase --exec` remain
  open** (reviewer-verified live ALLOW). These are design-inherent for an
  advisory hook and fine to *accept* — but they must be recorded, not implied
  closed.
- `residuals.md` §1.3 presents the canary as broadly validating; it is **real but
  narrow** — one repo, one runtime (codex), one issue, self-authored, **no cap
  ever hit, no concurrency, no fault injection, systemd never booted**. B3, B4,
  B5, B7, B10 would all have been invisible to it. Its own hedge is correct; the
  overreach is upstream in the "VERIFIED" claim.

---

## 7. Governance mechanization gaps (why drift will return without these)

The plan's §3.1 thesis — *a rule is only real if a test/contract/prompt enforces
it* — is the thing most at risk, because the enforcement landed weaker than the
prose:

- **G-6 (strict-xfail registry) was not delivered at all.** `grep xfail tests/`
  → one stale *comment*. The mechanism the plan named as "the one that held every
  time" was **removed, not extended**. Every residual in `residuals.md` §1.1/§1.2
  (credentials reachable from an agent child; `visual-ac` forgeability; the open
  governor holes above) has no strict-xfail, so a residual silently getting worse
  produces no signal. **This is the single most important governance fix** — add
  `tests/test_residuals.py`, one `xfail(strict=True)` per accepted hole.
- **G-5 (flag manifest)** is vacuous — `migration-flags.json` is `{}` and the
  test asserts `∅ == ∅`; a flag added tomorrow is invisible. It also does not see
  the largest surviving compat path: the **legacy prose-parsing fold**
  (`domain/dashboard.py`, ~450 live lines, selected by file-existence) has no
  removal issue, and a *fresh deployment before its first v2 write renders
  through it*.
- **G-1/G-2/G-3** are real tests but **name-keyed and narrow** — G-1 keys on the
  attribute being literally named `gateway` (rename it and the rule evaporates,
  no positive control); G-2 is an import-name AST check over 5 files, not the
  import-linter `forbidden` contract the plan specified; G-3 catches only literal
  `lambda: None`, not the `Callable | None = None` guard pattern that dominates
  this codebase. Each is *permission to drift* in its current form.
- **`PolicyDisposition` is a documented type with zero call sites** — HARNESS.md
  claims an enforcement that does not exist (the fail-closed behavior is real, but
  via `OpenPrLookup`/`GateResult`, not this type). Exactly the "prose outrunning
  code" the meta-rule forbids.

---

## 8. Recommended path to the maiden voyage

**Minimum to sail supervised (foreground/`point-at`, not systemd):**
B1, B2, B3, B4, B5, B6 (Tier 1). These are the ones that cause silent
mis-enforcement, silent spend corruption, or destroyed work under normal
operation. Most are small-to-medium; B4/B5/B6 are the medium ones.
Plus the **process mitigations**: backup+digest `control.db` after migration
(for the absent rollback), set all caps before first admission and don't change
which are armed while running (B-cap-arming, see accounting F2), and run on a
machine that will not suspend until B5 lands.

**Before an *unattended* run:** B7 (dashboard), B8 (typecheck gate), B9
(isinstance probes), and the **G-6 strict-xfail registry** — because an
unattended run with a blind dashboard and unmechanized residuals cannot be
trusted to surface its own failures.

**Before systemd deployment:** B10.

**Correct the docs** (§6) in the same change that fixes Tier 1, so the "VERIFIED"
claims stop being load-bearing.

## 9. Post-voyage backlog (real, not blocking)

`roles.py` grew 1557 → 2586 (+66%); C5's six copy-pasted reporter closures are
untouched (extract one parametrized `LevelReporter`). `PlannedRun`/`RuntimePlan`
are duplicate classes with already-divergent `__init__` (delete one). `Leg`'s
`billable`/`counts_against` are validated constants nobody reads (wire them into
scope selection or drop them). `ReviewWorkflow.log_run` is a live `run_leg`
bypass with a dead production twin. `control_store.py` is a 3,994-line god-object
(split by bounded context behind the `transaction()` seam). Per-admission
whole-DB `integrity_check()` is O(history) with no retention (move to startup +
add a retention job). The divergent `TokenUsage` normalizers (three of them) and
the float-arithmetic reconciled-settlement path should collapse onto one
`pricing.price()`. Nine declared ports (`Clock`, `SpendAccount`, …) have zero
consumers — wire or delete; `AccountingStatus` is a fake-taxonomy `class(str)`,
make it an `Enum`.

---

## 10. Bottom line for Fable

This is a large, careful, mostly-excellent landing by codex — the parts that are
hard to get right (transactional accounting, event sourcing hygiene, governor
parsing, supervision teardown) are the parts that are right. The risk is not
craftsmanship; it is **completeness**, and it clusters predictably: integration
seams that a green behavioral suite doesn't exercise (dashboard, typecheck),
recovery paths reused for two callers with different needs (boot vs wake), and
governance that was described more strongly than it was wired. Ten blockers,
~six of them small-to-medium, stand between here and a defensible first run. None
of them require rearchitecting; all are within the shape the plan already
defined. Fix Tier 1, correct the "VERIFIED" claims, restore the strict-xfail
registry, and a supervised foreground canary becomes the right next step.
