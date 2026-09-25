# ADR 0009 — Workflow continuation authority

Status: Accepted; partially superseded by [ADR 0015](0015-generation-fenced-gate-rearm.md)
for the resolve family only (accepted 2026-09-25 at `6905184`): in a repository whose
resolve family is activated, resolve continuation, ceiling gates and re-arm authority
live in the SQLite workflow tables, not Store files. Review continuation is unchanged
until the ADR 0015 review slice is accepted.
Date: 2026-08-01
Governs: src/issue_agent/adapters/store.py src/issue_agent/app/workflows/fix.py src/issue_agent/app/workflows/review.py src/issue_agent/adapters/worktree.py

## Context

Blocked replies, CI repair, review repair, rebase, and re-review must continue
the right transcript and branch without treating a local marker as remote
lifecycle truth. Runtime changes and closed/reopened pull requests make a
single undifferentiated “resume state” unsafe.

## Decision

Continuation authority is deliberately split by fact type:

- GitHub is authoritative for current issue/PR state, human replies, linkage,
  and the head branch of an existing pull request.
- The role `Store` owns local workflow continuity: runtime-tagged session ids,
  model and phase, consumed-reply timestamps, branch memory, and bounded retry
  counters.
- Git worktrees and commits own implementation content; `.harness/` files are
  claims and never lifecycle or authorization receipts.
- SQLite remains the authority for paid admission, run ownership, commands,
  and effects under ADRs 0002, 0003, and 0008.

Session ids may be resumed only by the runtime that created them. A runtime
change cold-starts a resume-shaped leg rather than passing an incompatible
session. Existing PR work uses GitHub's head branch; otherwise continuation
uses the remembered branch, then a discovered issue worktree branch, then a
deterministic fallback. The chosen branch is persisted whenever a worktree is
prepared.

A human reply is marked consumed before a paid resume starts. A crash can be
recovered or requeued, but an unchanged reply cannot purchase one session per
poll. Cleanup distinguishes terminal lifecycle reset from closed-unmerged
requeue, preserving the counters that bound repeated failure while clearing
markers that would resume a dead lifecycle.

## Consequences

- There is no stateful resume manager that also owns remote state or money.
- Adding a runtime requires an explicit session-compatibility policy.
- Continuation tests must cover blocked-to-resume, existing-PR branch reuse,
  cold start after runtime change, crash deduplication, and lifecycle cleanup.
