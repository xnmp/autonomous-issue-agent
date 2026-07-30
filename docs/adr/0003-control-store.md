# ADR 0003 — Embedded control store

Status: Accepted
Date: 2026-07-30
Governs: src/issue_agent/adapters/control_store.py src/issue_agent/app/recovery.py

## Decision

A local SQLite database is authoritative for runs, leases, dedupe keys,
reservations, charges, lifecycle commands, effect receipts, and the event
outbox. It uses versioned migrations, foreign keys, WAL where supported, a busy
timeout, restrictive permissions, short `BEGIN IMMEDIATE` transactions, and a
startup integrity check.

Fresh databases adopt the immutable v9 baseline. Databases created by the
previously shipped v8 initializer retain their exact v8 digest and upgrade
through v9 and v10; startup accepts both histories, validates every recorded
digest, and never rewrites an applied migration.

No network, model, or subprocess work occurs inside a transaction. Telemetry
is emitted from committed transitions via the outbox and is never read to make
an authorization, accounting, or idempotency decision.

## Operations

Stop all role daemons before copying `control.db` and its `-wal`/`-shm` files.
Restore the complete set to the same path with mode `0600`, then start one role
and require its integrity check/recovery to finish before starting the others.
Never roll an old copy over charges created after the backup.
