# Phase 4 — remaining continuation authority inventory

Date: 2026-09-27. Read-only preparation while phase-3 review acceptance runs.
Implementation remains gated on that acceptance. The
[six-phase plan](2026-09-14-architecture-implementation.md) and ADR 0015 remain
authoritative; this inventory does not authorize live migration.

## Family order

1. PR-owned review execution/recovery: unproductive-round episode, review
   session/round continuity, bounded pin deferrals, and explicit release of
   verdict escalation. Preserve the separate rejection allowance.
2. Consumed human replies and fix repair continuation/allowances.
3. Remaining fix recovery and steward continuation families.

Each slice needs typed domain decisions, one durable authority, ordered result
receipts, shadow comparison, scoped activation, old-writer fencing, rollback
reconciliation, composed failure tests and independent acceptance. File values
cannot silently become default-zero fields of an already active workflow.

## Production inventory

| Authority | Readers | Writers/reset paths |
| --- | --- | --- |
| Review `infra-deaths` plus recovered SQL deaths | `roles._review_unproductive_rounds`, `review_runtime.unproductive_rounds_reader`, dashboard breaker reads | `ReviewWorkflow._no_progress`, dispatcher death handling, `_progress`, legacy retry clearing |
| Review `pr-N.session` and `pr-N.round` | `ReviewWorkflow.re_review` | review/re-review and `_persist_review_state`, `Store.clear_pr_review` |
| Review pin deferrals | `ReviewWorkflow._pin_deferral` | process-local `_pin_deferrals`, reset by `_progress` |
| Issue/PR `resumed-at` | `roles._fix_state`, `domain.fix` reply selection | `FixWorkflow.resume`/`resume_pr` before runtime; cleanup |
| Fix repair session/model/branch/resumes | `_fix_state`, `FixWorkflow._repair_fix_cfg`, `_resume_branch`, repair/resume workflows | `_ensure_cwd`, repair/resume result paths and counters |
| Fix CI/review/rebase allowances | `_fix_state`, fix policy and `_signals` | repair workflows, `Store.reset_ci_episode`, legacy unblock and cleanup |
| Fix infra death, machine/budget block and self-clear state | `_fix_state`, `resolve_control.external_holds`, self-clear policy | fix failure/decay/block paths, dispatcher, executor callbacks |
| PR remediation markers | `_fix_state`, `_run_pr_logged`, fix policy | executor callbacks and terminal cleanup |
| Steward triage/groom baselines and stalls | steward role closure and pure policies | `StewardWorkflow` result paths and dispatcher death handling |

## First-slice design constraints

The current PR workflow stores one `(FamilyAuthority, episode)` pair and its
binding names one family. A separate review-execution family requires explicit
composition of episodes and fences; an expansion of review authority requires
its own explicit migration provenance. Settle that shared seam before assigning
parallel implementation work.

A paid review round needs one durable classification distinguishing productive
submission, genuine paid no-progress, infrastructure death, never-launched
refusal and bounded free pin deferral. Multi-review fan-out charges its outer
round once. Productive reset belongs with submission receipt settlement, so
lost replies and crashes cannot omit or repeat it. Ordered admissions prevent
late results from replacing newer sessions or clearing newer failure episodes.

`SQLiteControlStore.abandoned_dispatch_counts` currently counts all abandoned
runs for an owner without a productive-reset boundary. Review `_progress`
clears only files. Migrating only the file counter would preserve historical
recovered deaths across successful rounds. Attribute deaths to immutable run
identities and a durable failure episode before importing them.

The new authority must remain PR-owned across issue relinking. Legacy
issue-keyed counters require explicit mapping; shared or ambiguous records
must block import rather than be copied into several PRs.

Verdict escalation needs a typed cause tied to its submission receipt/head;
unproductive exhaustion needs a separate typed cause. Recovery identifies the
selected cause and captures epoch/episode/revision, guidance and a fresh review
obligation. It preserves unrelated holds and rejection allowance, and finishes
only after its durable PR projection is verified. It does not implicitly
dismiss reviews. Default continuation after an explicit release should be a
fresh review session with the committed guidance.

## Migration and acceptance

Inventory every configured state root, runtime-tagged review session/round,
both legacy counter key spaces, admitted/abandoned runs, review receipts and
escalation projections. Shadow compares eligibility, inherited failures,
continuation choice and held/releasable causes under identical observations.
Use existing fix→review→steward barriers and full quiescence checks.

After activation retain SQL authority during rollback; never restore file
counters over later admissions/receipts. Remove selected file accessors only
after all scheduler, operator and dashboard readers share the new authority.

Cleanup candidates: `Store.get_phase`, `set_phase` and `clear_review_retry`
have no production callers after review increment 8. Their stopped files may
remain migration evidence even if their obsolete APIs are removed.

For consumed replies, retain timestamp equality unless an explicit comment-ID
migration is designed: the current `Comment` and legacy timestamp contracts do
not yet carry a durable remote comment identity.
