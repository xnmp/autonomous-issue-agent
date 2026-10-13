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
(written beside the path, then renamed); an empty or unreadable file is
`CorruptControlStore`, never replaced.

A pre-5d-2 database (legacy `schema_migrations` table) is cut over once, at
open, to a new file carrying every settled charge as a `carried` ended leg and
the `fix-comment` keys; everything else is dropped and re-derived from GitHub.
The cutover refuses (`CutoverRefused`) while the legacy store holds unsettled
runs, active reservations or leases, or while another process holds it open.
It verifies the carried count and sum, keeps the old file under
`control.db.retired-v<N>-<stamp>`, and swaps the new file in with one atomic
rename; a crash leaves the old file intact or the new one complete.

No network, model, or subprocess work occurs inside a transaction. The JSONL
journal is never read to make an authorization, accounting, or idempotency
decision.

## Operations

Stop all role daemons and the dashboard before copying `control.db` and its
`-wal`/`-shm` files.
Restore the complete set to the same path with mode `0600`, then start one role
and require its integrity check/recovery to finish before starting the others.
Never roll an old copy over charges created after the backup. A retired
pre-cutover file is never read again; keep it as evidence or delete it once the
new store has run.
