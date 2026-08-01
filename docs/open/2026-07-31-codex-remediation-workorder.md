# Remediation work order — maiden-voyage blockers (for codex)

**Date:** 2026-07-31 · **Base:** `dev` @ `51cd8d2` · **Source of findings:**
`docs/open/2026-07-31-implementation-review-for-fable.md`, corroborated by a
second independent verification pass (all line numbers below re-checked at
`51cd8d2`). **Owner of this order:** whoever picks up the codex leg.

This is a fix-list, not a design doc. Every item names the root cause, the exact
locations, the fix, the test that proves it, and the acceptance bar. Do not
close an item without its regression test.

## Ground rules (do not skip)

1. `make test` and `make typecheck` must be green at every commit. Commit only
   coherent, tested states — the daemons hot-reload from this checkout between
   ticks.
2. **Every fix adds a behavioral regression test that fails before the fix and
   passes after.** Fix the class, not the instance (project rule). Where a bug
   exists because a test called the wrong seam (B2), add the test against the
   *real* seam.
3. Do not weaken or delete existing tests to make the suite green. If a test
   encoded the buggy behavior, rewrite it to encode the correct contract.
4. Correct the over-claimed docs (§ "Documentation corrections") in the **same
   change** as the Tier-1 fixes, so "IMPLEMENTED AND VERIFIED" stops being a
   warrant while it is false.
5. Run the full adversarial diff review in a subagent before the PR (project
   rule).

---

## Tier 1 — blocks any run against a repo you care about

### B1 — Governor resolver fails OPEN on `RecursionError`

**Root cause.** `domain/governor_policy.py`: `visit()` (`:108`) and `arg_value`
(`:51`, self-recursive at `:71`) have no depth bound; `resolve()` (`:361`) has no
`except`. A deeply nested `$( … )` payload raises `RecursionError`. Both
enforcement points then fail open: the in-process hook
(`adapters/governor.py:939-947`) catches `Exception` and returns `{}` (ALLOW, by
design); the codex/bash hook `plugin/hooks/bash_governor.py` `main()` (`:134`)
has no top-level guard, so an unhandled exception exits **1**, and only exit
**2** blocks (documented at `bash_governor.py:15`). Net: one nested wrapper walks
`gh pr create` / `git push --force` past every gate. Falsifies `residuals.md:21`.

**Fix.**
- Depth-bound the parser: thread a depth counter through `visit`/`arg_value`;
  past a fixed limit (e.g. 64) return `Unresolvable("nesting depth exceeds
  governor bound")` rather than recursing.
- In `evaluate_command` (the caller of `resolve`/`find_violations`), wrap the
  resolve call so **any** exception maps to a DENY verdict, not a bypass.
- Add `except BaseException:` at the top of `bash_governor.main()` that prints the
  block reason and exits **2**.

**Tests.**
- `resolve(nested_payload)` returns an `Unresolvable`/deny verdict, does not raise.
- Subprocess test: `bash_governor.py` fed the pathological payload exits **2**.
- Property-ish: a resolver exception injected into `evaluate_command` yields DENY.

**Acceptance.** Nested `$()` around `git push --no-verify` is denied on both
paths; no code path turns a resolver exception into ALLOW.

### B2 — `bash_governor.py` drops `durable_wrapper` → publication-authority override unblocked on codex path

**Root cause.** `plugin/hooks/bash_governor.py:190` calls
`bypass_reason(g.full_words, g.kind, env, g.extra_env)` — 4 args. The 5th,
`durable_wrapper`, defaults `False` (`governor_policy.py:712-715`) and is the
**sole** trigger for the `_PUBLICATION_AUTHORITY_ENV` guard
(`governor_policy.py:724-735`). The in-process `find_violations`
(`governor_policy.py:807-814`) passes `g.durable_wrapper`; the hook does not. So
`ISSUE_AGENT_RUN_ID=evil issue-agent-publish git push …` DENYs in-process but
returns **rc=0 (ALLOW)** through the hook. Existing tests pass because they call
`find_violations`, never the hook.

**Fix.** Pass `g.durable_wrapper` as the 5th arg at `bash_governor.py:190`.

**Tests.** Subprocess exit-code test **per authority key** (`ISSUE_AGENT_RUN_ID`,
control-DB path, and any other `_PUBLICATION_AUTHORITY_ENV` member): the hook
exits **2** when the key is set on a publish. This is the seam the current tests
miss — assert against the hook binary, not `find_violations`.

**Acceptance.** `ISSUE_AGENT_RUN_ID=evil issue-agent-publish git push …` → rc=2
via the hook.

### B3 — `control.db` silently self-heals to empty → spend/caps reset to zero

**Root cause.** `adapters/control_store.py:3835-3846` `_connect()` uses plain
`sqlite3.connect(self.path)` (create-on-open) and always runs
`PRAGMA journal_mode=WAL`, which materializes a 4096-byte schemaless file from
nothing. The DB lives machine-wide at `~/.local/state/issue-agent-control/…`
(`adapters/control_path.py:36`) — outside any repo, exactly where a stray
cleanup/rsync/restore hits it. On next boot the empty file defeats both guards:
`_database_exists` checks only size > 0 (`:776-781`) and the witness checks only
that the file exists + format/ownership (`:737-741`, `:791-820`) — neither checks
content. Migration replay runs from empty (`:745-752`) and `initialize()` reports
success with every charge/reservation/cap at **zero**. (Mid-run reads are safe —
`snapshot()` catches `DatabaseError` and returns CORRUPT, `:2526-2533`; the
silent zero-reset lands at the next boot.)

**Fix.** Open the DB read/write-only everywhere except `initialize()`: use the
`?mode=rw` + `uri=True` form (the read-only helper at `:138-147` already models
this with `mode=ro`). A missing DB then raises on connect → map to
`CORRUPT`/`UNAVAILABLE` so paid admission blocks and the witness guard fires,
instead of a silent recreate.

**Tests.** Delete the DB file, then a read path (`snapshot`) must raise/return
UNAVAILABLE, not recreate an empty DB. Boot with the DB absent must **not** land
in a zero-spend "success" state.

**Acceptance.** A removed `control.db` blocks paid admission; it is never silently
recreated as empty by a read path.

### B4 — Retained reservations never reclaimed while running → armed cap self-deadlocks

**Root cause.** `adapters/accounting_runtime.py:341-356` (usage-blind:
`usage is None` + zero provider amount) and `:357-366` (usage but no pricing
authority) deliberately **retain** a reservation without settling. A Claude leg
returning `cost_usd=0, usage=None` is a documented real outcome. The only
reclaimer is `RecoveryCoordinator.recover()` (`app/recovery.py:39-61`), wired at
boot (`app/roles.py:480-497`) and on suspend-stall only (`app/loop.py:696-697`) —
**no tick-cadence sweep**. Each usage-blind leg holds its full reserved maximum
against the daily cap for the daemon lifetime; a handful drive the deployment to
`EXHAUSTED` with no operator-visible cause and no remedy but a restart nobody
knows to perform.

**Fix.**
- Run the reservation-sweep arm of `recover()` on a bounded tick cadence. It is
  idempotent and its lease-liveness probes already run outside the write txn, so
  it is safe to call periodically. Keep the orphan-kill arm boot/suspend-only (do
  **not** run that on every tick — see B5).
- Emit a dashboard-visible signal on both retain-without-settle branches so a
  stuck reservation is observable.

**Tests.** A usage-blind leg's reservation is released by the periodic sweep
within N ticks; the released amount returns to available cap. A dashboard signal
is emitted on the retain branch.

**Acceptance.** A run of usage-blind legs does not monotonically consume the cap;
the retained reservations are visible and reclaimed without a restart.

### B5 — Suspend-wake `recover()` kills own in-flight paid agents + releases live reservations

**Root cause.** The stall branch fires at `app/loop.py:664`
(`elapsed >= poll*4 and suspended > 0`, `suspended` = wall−monotonic via
`suspension_between` `:658-663`) — an ordinary laptop lid-close satisfies both.
The wake call (`:696-697`) runs `RecoveryCoordinator.recover()`
(`app/recovery.py`), whose `recover_orphans()` (`:40`) →
`process_supervisor._reap_registered` (`:372-402`) `kill_tree`s **every**
registration with **no liveness/ownership check** — unlike the sibling
`recover_abandoned_orphans` (`:287-356`), which skips registrations matching a
live launcher (`:309-316`) and externally-owned trees (`:345-356`). Live legs
register in the same directory (`register_prepared_launch`, `:175`), so their
sessions are reaped while worker threads still execute them.

The reservation side compounds it: the kill runs **first** (`recovery.py:40`),
then the reservation-release step (`:51-59`) — its lease-liveness guard
(`:52-53`) now sees the just-killed pid as dead and releases the reservation as
`spend_unknown`. The subsequent `settle()` on a non-active reservation returns
CONFLICT (`control_store.py:2935-2940`), which raises
`RuntimeError("accounting settlement failed closed: …")`
(`accounting_runtime.py:436-438`), losing the charge and killing the leg, plus a
false `agent_run_abandoned`.

**Fix.**
- The wake caller must use the ownership-checked reaper: reap only registrations
  whose launcher is **not** this live process (the predicate already exists in
  `recover_abandoned_orphans`). Do not call the unconditional `recover_orphans`
  arm on suspend-wake.
- Add a lease/grace window so a reservation still in its spawn window is never
  swept.
- Order matters: verify liveness before, not after, any kill.

**Tests.** Pin both directions:
- A live sibling/owned session survives a simulated suspend-wake `recover()`
  (mirror the existing `recover_abandoned_orphans` live-skip test).
- A reservation backed by a live lease is not released on wake; `settle()` still
  returns AVAILABLE, no `RuntimeError`, no false `agent_run_abandoned`.

**Acceptance.** A suspend/wake during an in-flight paid leg does not kill the leg
or lose its charge.

### B6 — Telemetry not isolated from operational state (two paths, one class)

The plan's core rule ("telemetry is never the sole authority") is violated where
a telemetry write can abort or corrupt operational work.

**(a) Encoding/size failure rolls back the authoritative control-store txn.**
`record_effect` opens `transaction()` (`control_store.py:3383-3393`);
`_insert_effect` enqueues the event via `_event`→`enqueue_event`
(`:3486-3493`), which raises `ValueError` when the encoded line exceeds
`MAX_EVENT_LINE_BYTES` (64 KiB, `domain/events.py:23`) — **inside** the txn, which
rolls back on any exception (`:1339-1348`). `EffectReceipt.error` is unbounded
(`domain/contracts.py:334-345` never checks length) and `effects.py:339` embeds
`f"{type(exc).__name__}: {exc}"` where `GhUnavailable.detail` carries the full gh
stderr (`adapters/github.py:1888, 1920-1927`). So a large gh failure body aborts
the receipt commit for a mutation that may already have hit GitHub.
*Consequence note:* the double-apply tail is **conditional** — `begin_command`
commits separately (`effects.py:319`) so the next `execute()` takes the
reconcile-first branch (`:276-290`); a double-apply occurs only if `_reconcile`
cannot verify the landed mutation. Still fix it: a lost receipt is a real
integrity hole.

**(b) A telemetry emit failure aborts `recover()` every boot.**
`TypedJournal.abandon_run` calls `self.events.emit(...)` **unguarded** at
`adapters/typed_journal.py:80` and `:90` — unlike `TypedJournal.emit` (`:43-54`),
which swallows. `EventLog.emit` (`event_log.py:71-84`) appends raw, so an
OSError/over-limit propagates. `app/recovery.py:60-61` loops `abandon_run` with no
`try`, **after** orphan kills (`:40`) and lease reclaim (`:41`), so a full disk or
over-limit line raises out of `recover()` with recovery half-applied. Because
`_recovery` calls `recover()` at construction (`roles.py:501`), this can also fail
boot. Violates the port contract at `domain/ports.py:283-288` ("telemetry failure
never changes operational recovery state").

**Fix.**
- Bound every free-text field at the source (`EffectReceipt.error`,
  `GhUnavailable.detail`, any operator/gh stderr passthrough) — truncate to a safe
  cap in `__post_init__` or at the embed site.
- Make `_event`/`_transition` (the in-transaction enqueue) never raise into the
  transaction: on encode/size failure, enqueue a bounded `TelemetryWarning` or
  bump a counter instead. State/receipt commits must be unconditional.
- Wrap the two `abandon_run` emits in the same swallow guard as `emit`.

**Tests.**
- A 70 KB receipt error commits the receipt row (telemetry degraded, state
  intact) — assert rows committed, not zero.
- `recover()` completes with orphan-kill + lease-reclaim applied even when the
  `abandon_run` emit raises (disk-full/over-limit simulated).

**Acceptance.** No telemetry write can roll back a control-store state/receipt
commit or abort `recover()`.

---

## Tier 2 — blocks an *unattended* run

### B7 — Dashboard broken under v2 (degraded + crashable)

**(a) Drill-down features dead.** `build_deployment_v2` stores drill-down events
as v2 envelopes (`domain/dashboard.py:563`, keys `type`/`data`/`occurred_at`)
while `app/dashweb/snapshots.py` reads them with legacy journal keys (~30-40
sites: `.get("event")` ×18, `.get("ts")` ×9, plus `kind`/`turns`/`usage`/`model`/
`blocker_*`), all returning `None` → no-match branches. Row-level rendering
(stage, legs, model, cost, escalation flags via `_v2_issue_row`,
`domain/dashboard.py:440-565`) **survives**; genuinely dead are start/elapsed
clocks, turns/usage, deferred/hold badges, stall corrections, human-action,
escalation detail, blocked reasons, model fallback.
*Fix:* convert `snapshots.py` to consume typed payloads off the read model
instead of legacy dict keys.

**(b) One bad line crashes the whole dashboard.** `decode_event` is called
unguarded at `app/dashboard.py:111` and `adapters/dashweb/system.py:171`; only
`json.JSONDecodeError` is caught. A valid-JSON line with wrong/absent
`schema_version` or unknown `type` raises `ValueError` (`events.py:525-531`) and
crashes `dashboard --text/--html` and the HTTP snapshot path. Feed selection
globs `events-v2.jsonl*` (`app/dashboard.py:50/:97`,
`adapters/dashweb/system.py:85/:156`), so any sibling rotation/`.bak` is ingested.
*Fix:* guard `decode_event` per line into a counted `warnings` list; skip and
count bad lines instead of raising.

**(c) Legacy parallel fold still selected by file-existence.** The v2 fold is
chosen by `any(glob(f"{path}*"))` else the legacy regex-over-prose
`build_deployment` (`app/dashboard.py:50-90`; regexes at `domain/dashboard.py:54-64`,
`build_deployment` at `:967`). A **fresh deployment before its first v2 write
renders through the legacy fold.** The claimed "fold 11→1" did not happen —
duplicate helpers survive (`_int` ×3, `parse_ts` ×2, an `import *` at
`app/dashboard.py:15`).
*Fix:* select v2 unconditionally and delete the legacy fold once `snapshots.py`
is converted; collapse the duplicate helpers.

**Tests.** Emit→fold→`issue_card` through the real path renders the previously-
blind fields; a stray pre-v2 / unknown-type line increments a warning counter and
does not crash text/HTML/HTTP; fresh-deployment (no v2 file yet) path is defined
and tested.

**Acceptance.** Dashboard renders all documented fields from v2 events and never
hard-crashes on one bad line.

### B8 — pyrefly type gate disabled

**Root cause.** `pyproject.toml:65` sets `preset = "basic"`. `make typecheck`
reports 0 errors; removing that line surfaces **686** (verified — the review's
"254" understated it ~2.7×). The suppressed set includes real port-conformance
drift: `WorkflowState`↔`Store` parameter-name mismatches (e.g.
`Store.set_ci_retries` `n` vs `retries`), `flush_outbox` signature drift, and two
daemon builds passing `None` for non-optional `worktrees: FixWorktreePort`
(`roles.py:2178` review build, `:2415` steward build; fix passes real worktrees at
`:1867`). These drifts are currently **latent** (Store methods called
positionally; the None-worktree daemons don't do worktree ops) — but the gate that
was supposed to hold the typed seams is off, so the next drift is invisible.

**Fix.** Delete the preset. Clear the port-conformance subset (the mismatches
above). If the full 686 cannot clear pre-launch, raise the preset with per-rule
suppressions that each carry a removal issue — no blanket `basic`.

**Acceptance.** `make typecheck` enforces protocol conformance; a new port drift
becomes a build failure.

### B9 — `isinstance(x, <runtime_checkable Protocol>)` probes preserve silent-skip

**Root cause.** 18 `isinstance`-against-`runtime_checkable`-Protocol probes in
`app/` behave as byte-for-byte `hasattr` name checks. Only **two are write-side**
and matter for silent corruption: `app/workflows/fix.py:113`
(`isinstance(self.store, BranchState)` → skips `save_branch`) and `:508`
(`PredictionWriter` → skips `save_predicted_paths`). Combined with B8 (no
typechecker to catch a name drift) a drifted adapter silently skips a durable
write. The other probes are reads with inert fallbacks (`fix.py:122`,
`roles.py:1396/1718/2458`, `steward.py:81/roles.py:1733`) — lower priority.

**Fix.** For the two write-side probes: delete the probe and rely on the declared
port, or make the dependency explicit `X | None` with a null object. Add an
architecture test forbidding `isinstance(_, <…Reads|State|Writer|Runtime>)` in
`app/`.

**Acceptance.** No write path is gated on an `isinstance`-vs-Protocol probe;
architecture test enforces it.

### G-6 — restore the strict-xfail residual registry

**Root cause.** The plan named `xfail(strict=True)` as the drift-prevention
mechanism "that held every time," but it was removed, not extended:
`grep -rn xfail tests/` → one stale comment (`tests/test_governor.py:376`), zero
`xfail(strict=True)`, no `tests/test_residuals.py`. Every accepted residual
(agent-reachable credentials, `visual-ac` forgeability, the open governor holes)
has no signal if it silently worsens.

**Fix.** Add `tests/test_residuals.py` with one `xfail(strict=True)` per accepted
hole, each asserting the *current* (accepted) behavior so that a fix or a
regression flips the strict-xfail and forces a decision. This is the single most
important governance fix — do it in the Tier-2 batch.

**Acceptance.** Each residual in `residuals.md` §1.1/§1.2 has a strict-xfail; the
suite fails if a residual changes state unannounced.

---

## Tier 2b — systemd only

### B10 — All three systemd units fail to start

**Root cause.** `ops/systemd/` units use `Type=notify` + `NotifyAccess=main` with
`ExecStart=@UV_BIN@ run … python -m issue_agent.app.main <role>`. `uv run` spawns
python as a child (no exec), so systemd's MainPID is `uv` and the python
grandchild's `READY=1` is rejected under `NotifyAccess=main`. The unit times out
at `TimeoutStartSec`, `StartLimitBurst=3` / `StartLimitIntervalSec=300` puts it in
permanent `failed` within ~5 min; `WatchdogSec=120` compounds it (watchdog pings
also come from the grandchild).

**Fix.** Either `NotifyAccess=all` (minimal), or resolve the venv interpreter
absolutely and `ExecStart` it directly so the daemon is MainPID (cleaner). Assert
`active (running)` at install time.

**Acceptance.** `issue-agent install-service` yields `active (running)` for all
three units, verified at install time.

*(Not a blocker if the maiden voyage uses `point-at`/foreground.)*

---

## Documentation corrections (same change as Tier 1)

- Plan-of-record + `residuals.md`: **Phase 4.7 `CONTROL_STORE_MODE`
  (shadow/canary/rollback) does not exist** — `CONTROL_STORE_MODE` appears in zero
  src files; `migration-flags.json` is `{}`; money was hard-cut over, `spend.jsonl`
  is no longer written (`typed_journal.py:25-26`), there is no rollback path. An
  offline comparator (`scripts/verify_control_migration.py`) does exist. Change
  "IMPLEMENTED AND VERIFIED" to reflect the hard cutover + document the
  backup+digest mitigation.
- Phase 5 "governor bypass class closed by construction" is **overstated** — the
  *enumerated* wrapper class closed, but script-file execution (`bash file.sh`,
  `./x.sh`), `script -c`, `node/perl/ruby -e`, and `git rebase --exec` remain open
  (`Ungoverned`/ALLOW). Record these as accepted residuals with strict-xfails
  (G-6), do not imply closed. Fix `residuals.md:21`.
- `residuals.md` §1.3: the canary is **real but narrow** — one repo, one runtime
  (codex), one self-authored issue, no cap ever hit, no concurrency, no fault
  injection, systemd never booted. B3/B4/B5/B7/B10 were invisible to it. Keep the
  hedge; remove the upstream "VERIFIED" overreach.
- `PolicyDisposition` (`domain/contracts.py:265`) has **zero call sites**;
  `HARNESS.md:144` claims enforcement through it. Either wire it into the real
  fail-closed path (`OpenPrLookup`/`GateResult`) or delete the doc claim.

## Post-voyage backlog (do NOT do in this change — file as issues)

Two unasserted `fold` silent-drop paths (`domain/projection.py:72-73` second
start overwrites a completed run; `:75-78` completion whose start rotated out
vanishes) — both distort the `$/merge` metric; add asserting tests.
`roles.py` (2586 lines) C5 reporter closures; duplicate `PlannedRun`
(`app/planners/run.py:13`) / `RuntimePlan` (`adapters/runtime_plan.py:39`);
`ReviewWorkflow.log_run` run_leg bypass (`app/workflows/review.py:82,153`);
`control_store.py` (3994-line god-object); G-5 flag manifest; G-1/G-2/G-3
name-keyed contracts.

## Definition of done

- [ ] B1–B6 fixed, each with a red→green regression test.
- [ ] Docs corrected (CONTROL_STORE_MODE, governor residuals, canary scope,
      PolicyDisposition).
- [ ] `make test` + `make typecheck` green.
- [ ] (For unattended) B7, B8, B9, G-6 done.
- [ ] (For systemd) B10 done, `active (running)` asserted at install.
- [ ] Adversarial diff review run in a subagent; findings addressed.
