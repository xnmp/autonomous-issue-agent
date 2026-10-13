# ADR 0009 — Workflow continuation authority

Status: Superseded by the minimal-harness refactor. Since slices 5b and 5c
no role reads the typed SQL continuation components: the fix role resumes from
its run receipts (`app/fix.py`) and the review role runs one fresh leg per head
(`app/review.py`). Slice 5d deletes the retained components. Earlier history:
selected runtime continuity was superseded by
[ADR 0015](0015-generation-fenced-gate-rearm.md) and
[ADR 0016](0016-phase-4-current-state-authority.md).
Date: 2026-08-01
Governs: src/issue_agent/adapters/store.py src/issue_agent/adapters/worktree.py

## Context

Blocked replies, CI repair, review repair, rebase, and re-review must continue
the right transcript and branch without treating a local marker as remote
lifecycle truth. Runtime changes and closed/reopened pull requests make a
single undifferentiated “resume state” unsafe.

## Decision

Continuation authority is deliberately split by fact type:

- GitHub is authoritative for current issue/PR state, human replies, linkage,
  and the head branch of an existing pull request.
- Typed SQL components own selected workflow continuity: resolve and review
  execution sessions, PR repair session/model, consumed replies, branch memory,
  lane allowances, infrastructure episodes, machine/budget holds and shared
  self-clear count. Admission, classified outcome and effect receipts retain
  their exact run, owner, episode, generation and revision provenance.
- `Store` supplies bounded CI prompt history, predicted paths, stall telemetry
  and Jira mirror context. Jira alone retains its existing raw infrastructure
  counter and abandoned-run gate by explicit user decision; new SQL
  TRIAGE/GROOM lane deaths do not update that counter. Selected runtime reads
  cannot use retained control files to grant or refuse continuation.
- Git worktrees and commits own implementation content; `.harness/` files are
  claims and never lifecycle or authorization receipts.
- SQLite remains the authority for paid admission, run ownership, commands,
  and effects under ADRs 0002, 0003, and 0008.

Session ids may be resumed only by the runtime that created them. A runtime
change cold-starts a resume-shaped leg rather than passing an incompatible
session. Existing PR work uses GitHub's head branch; otherwise continuation
uses the remembered branch, then a discovered issue worktree branch, then a
deterministic fallback. The selected SQL admission owns remembered branch
changes; preparing a worktree does not write a legacy branch marker.

A qualifying human reply is consumed atomically with its exact paid admission;
blank replies use the existing durable no-op receipt and buy no work. Budget
refusal consumes neither reply nor allowance. Captured repair results survive
restart and can classify still-owed issue debt after independent monetary
settlement or PR generation advance. Superseded results settle money without
replacing current continuity or holds.

Free CI rerun/cancel use existing commands and exact head/run identities;
their pending or unknown receipts bar paid fallback. Branch refresh and
pending-CI escalation use exact head/base-bound receipts for replay and
completion while preserving their existing paid-eligibility policy.
Machine and budget recovery retire the selected cause and
increment one shared SQL count only after verified projection; external clear
retires without charging that count. Cleanup preflights its complete fixed
recipe before effects and checks the selected revision again at SQL retirement.
Closed-unmerged requeue preserves debt and unsettled evidence. Existing named
resolve/review grants remain supported; unsupported legacy fix retry grants
refuse before remote labels or comments.

## Consequences

- There is no stateful resume manager that also owns remote state or money.
- Adding a runtime requires an explicit session-compatibility policy.
- Continuation tests must cover blocked-to-resume, existing-PR branch reuse,
  cold start after runtime change, crash deduplication, and lifecycle cleanup.
