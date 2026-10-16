# ADR 0003 — Embedded control store

Status: Accepted
Date: 2026-07-30
Governs: src/issue_agent/adapters/control_store.py src/issue_agent/app/recovery.py

## Decision

A local SQLite database is authoritative for money, leases, legs (attempts)
and idempotency keys. Since refactor slice 5d-2 it holds exactly three tables
— `legs` (one row per leg: identity, lifecycle, money, result), `leases` (one
claim per repository/role/target, bound to its leg and holding process) and
`idempotency` (keyed remote writes) — versioned by `PRAGMA user_version`
(currently 1). There is no migration history, no digest pin and no authority
witness: a schema change is a new version with its own explicit step. It uses
WAL where supported, a busy timeout, restrictive permissions (`0600` file,
`0700` directory), short `BEGIN IMMEDIATE` transactions, CHECK constraints
that make contradictory leg states unrepresentable, and a `quick_check` at
open and before every reservation. An absent file is created atomically
(written beside the path, then renamed) only where no store was ever used:
evidence of a prior one — the `.initialize.lock` every open leaves, a
`-wal`/`-shm`, a `.retired-v*` file, or the previous release's `.authority`
witness — makes a missing store `CorruptControlStore`, never an empty
replacement. An empty or unreadable file is `CorruptControlStore` too.

A pre-5d-2 database (legacy `schema_migrations` table) is cut over once, at
open, to a new file carrying every settled charge as a `carried` ended leg and
the `fix-comment` keys; everything else is dropped and re-derived from GitHub.
The cutover refuses (`CutoverRefused`) while the legacy store holds unsettled
runs, active reservations or leases, or while another process has it open at
that instant (a point-in-time check; stopping every daemon first is the guard).
It verifies the carried count and sum and that every charge a legacy cap scope
(repository day, global day, issue lifetime) counted is counted by the same
scope in the new file, keeps the old file under
`control.db.retired-v<N>-<stamp>`, and swaps the new file in with one atomic
rename; a crash leaves the old file intact or the new one complete.

A leg's `leg_class` is the raw class it returned with; attempt classification
is derived on read (#441: `tick.leg_end` with `domain/attempt.py`), from the
order the item's legs ended, which is exact because the per-issue fix lease
runs one fix leg per issue at a time, so the legs ended before a leg are the
legs its dispatch saw. A counted cut-off (a per-leg cap or nonterminal return
on the free resume, or of a kind that never resumes) is stored `machine` and
read as an attempt, so operator SQL that counts infra deaths from
`leg_class = 'machine'` overcounts; count `result_json`'s `end` class instead.

No network, model, or subprocess work occurs inside a transaction. The JSONL
journal is never read to make an authorization, accounting, or idempotency
decision.

## Operations

Stop all role daemons and the dashboard before copying `control.db` and its
`-wal`/`-shm` files.
Restore the complete set to the same path with mode `0600`, then start one role
and require its integrity check/recovery to finish before starting the others.
Never roll an old copy over charges created after the backup. A retired
pre-cutover file is never read again, but keep it: if `control.db` is lost with
no backup, renaming the retired file back (any `-wal`/`-shm` moved aside) cuts
it over again with its pre-cutover spend, which the refusal message says.


Amended by #112: a routed leg returning cleanly without pushed, blocked or
failed is the existing NO_RESULT cut-off. Its first resumable end follows
Continue at the same tier; a second cut-off on that continuation counts under
cut_off_counts unchanged. A nonterminal return with no usable session remains a counted agent end;
existing wall/budget cut-off receipts retain their interrupted-evidence provenance.
Session validity uses the same receipt bound at every boundary. Light and review schemas keep their own verdict contracts.
No counter, receipt field, continuation state or retry path is added.
Measurement before the change (2026-10-16, journal since 2026-10-13): 64 tauri
RESOLVE ends, including 48 pushed and 5 blocked; zero clean nonterminal ends.
The regression still reproduces the old shape on current dev: the next task
started fresh with resume=None. The end-to-end regression verifies continuation,
its counted second cut-off, and a published branch taking precedence. Existing
lease/reserve/result/settle crash handling is unchanged.
Metric: fresh RESOLVE starts after a clean nonterminal end, adjudicated by the
next comparable journal sample. Guardrails: attempts remain bounded by the
existing rule, publication precedence and legitimate human blocks stay intact.


Amended by #129: a provider-ended turn is a machine failure under the
existing machine backoff and no longer joins the cut-off attempt rule.
Claude and Codex already mint provider_refused from their own envelopes or
rollouts. Repeated provider failures preserve the existing session and never
consume the issue's agent attempts. Per-leg caps and nonterminal agent returns
keep their bounded continuation. No provider pause/probe, threshold, reset
clock, runtime fallback, counter or state is introduced. Backoff and money
caps remain the bounds, including for a permanently invalid provider request.
The public regression drives six failed provider legs, verifies backoff and
no human hold, then observes recovery publish one PR. Existing raw receipts
and reserve/settle/recovery paths are unchanged.
Metric: issue-attempt stops caused solely by provider failure; guardrail:
ordinary agent failures and per-leg cap attempts retain their existing limits.
Adjudicate with the next provider-outage journal sample.
