# Phase 3 — review-rejection slice plan

**Accepted 2026-09-27 at `a7214bf`.** All eight increments are implemented,
independently reviewed and fully tested; see the
[verification record](2026-09-27-review-slice-verification.md). This completes
phase 3 alongside the accepted resolve slice. No real family activation or
deployment restart was performed. The design decisions below remain binding;
the initial-code facts are historical.

Date: 2026-09-25, grounded at `f559282`; resolve slice accepted at `6905184`. Companion to
[ADR 0015](../adr/0015-generation-fenced-gate-rearm.md) and the
[gate design](2026-09-14-phase-3-gate-design.md). Implementation starts only
after the resolve slice is accepted (composed fault-matrix acceptance plus the
absence-authorized retry control).

## Facts from the resolve-first code

- One family per target in phase 3: resolve is issue-owned, review rejection is
  PR-owned. `workflows.episode_json` holds one `(FamilyAuthority, episode)`; v19
  tables already allow `target_kind='pr'`. No core schema change is needed.
- Resolve-only assumptions that break once a PR row exists:
  `load_resolve_authority` treats any retained workflow/gate/rearm/run row as
  resolve authority; `load_workflow_row` requires resolve activation;
  `_validate_workflow_reservation`/`_validate_workflow_command` demand the fix
  owner for any bound request; `_record_workflow_admission` always builds a
  `ResolveAdmission`.
- Resolve intents embed workspace handoffs; generalizing them would weaken
  resolve invariants.
- The ceiling escalation is a COMMENT review with `ceiling-1` counted
  rejections; `count_prior_rejections` and `stale_rejections` currently use
  different reset rules.
- Legacy `operator_effects.dismiss_review` treats an absent review as dismissed,
  which the ADR forbids.
- After dismissal the head is unchanged; a fresh review today depends on the
  `_reset_review_baseline` file reset.

## Shared-seam decisions (parent-owned)

- Keep `WorkflowSnapshot` byte-stable as the resolve/issue snapshot. Add
  `ReviewWorkflowSnapshot(revision, authority, episode: ReviewEpisode, gates,
  intent: ReviewRearmIntent | None)` requiring `PullRequestRef` and
  `REVIEW_REJECTIONS`; `AnyWorkflowSnapshot` widens transition envelopes.
- New values: `WorkflowFamily.REVIEW_REJECTIONS`; `CountedReview`;
  `ReviewRejectionCeiling(episode_id, head_sha, review_bot, counted, ceiling,
  non_resetting_commits)`; `FreshReviewRequired | ReviewedBaseline` continuation
  with typed baseline provenance (bound submission receipt or import evidence);
  `RequireFreshReview`, `DismissCountedReview`; review intent variants without
  handoffs; `WorkflowMutation.RECORD_REVIEW`; family-bearing
  `WorkflowMigrationRequired`; complete-review/linkage observation variants
  where partial pagination can never disprove a cause.
- Family-scoped authority reads; shared CAS/commit helpers extracted from
  `SQLiteWorkflowControl` without behaviour change; a `ReviewWorkflowControl`
  port; admission and command validation split by binding family;
  `DISMISS_REVIEW` owned by the review executor with read/verify.
- Operator API: PR-scoped `/api/repos/{name}/pulls/{pr}/review-control[/commands]`
  with distinct contract names; resolve contracts stay byte-stable.

## Increments

1. Pure types and codec.
2. Family-scoped authority, shared commit helpers, admission/command split.
3. Pure review policy (`domain/review_workflow_policy.py`).
4. Gateway + executor `DISMISS_REVIEW`, bound `SUBMIT_REVIEW`.
5. `SQLiteReviewWorkflowControl`.
6. Runtime switch in the review role/workflow; composed review acceptance;
   delete `last-sha`/`patch-id`/`operator-guidance` Store authority.
7. Operator API and dashboard review panel.
8. Review activation/import; `legacy_unblock_authority` refuses when any family
   is active; delete `_rearm_rejections`, `_reset_review_baseline`,
   `_apply_operator_guidance`, `operator_effects.dismiss_review`,
   `stale_rejections`. Annotate ADR 0015/0009.

Each increment: behavioural tests (including review fault-matrix rows),
independent adversarial review, full `make test` before commit.

## Decisions taken (ADR-consistent defaults; flag for user review)

The user asked for uninterrupted implementation; these defaults follow the ADR
and can be revisited before live activation, which remains separately paused.

1. Resolve acceptance precedes review implementation (ADR). Enforced by order.
2. Lever gap: once resolve is active, the legacy unblock refuses the repository,
   so PR rejection ceilings cannot be re-armed until review is activated.
   Decision: activate both families together operationally; no narrow legacy
   exception (ADR: never two authorities, no fallback). Documented in HARNESS.
3. Shared `needs-human-review` from ESCALATE verdicts and unproductive rounds is
   an external hold the review projection never removes; its release path is
   phase 4.
4. Counted set: one selector (`count_prior_rejections` semantics with the
   non-resetting commit set captured in the cause); `stale_rejections` deleted.
   Parity property tests guard the change.
5. Fix-role repairs read review guidance read-only from SQLite; an unavailable
   read defers the repair (fail closed, consistent with resolve).
6. Missing dismissal permission under branch protection is a visible refusal,
   never a credential switch (ADR).
7. Head change while a review re-arm is pending supersedes it (ADR).
8. Repositories without an active review family cannot review after the slice is
   accepted (migration-required); acceptable while the deployment pause holds
   (ADR: runtime may remain unstartable for live scopes).
9. PRs without a linked issue are controllable through the API; the dashboard
   panel mounts on linked PRs in the issue page. Closed PRs' legacy files are
   recorded as evidence and not imported.
10. No v23 index unless query-plan tests show the quiescence probe needs one.
11. Authorization bound (deliberate deviation from ADR wording "bounded by the
    configured ceiling"): the operator authorizes exactly the observed counted set
    captured in the gate's cause, which can exceed the ceiling when the bot keeps
    rejecting after escalation. The bound is `MAX_COUNTED_REVIEWS` (63, one intent's
    command budget); `REVIEW_MAX_REJECTIONS` is validated ≤ 63 at config load. The
    ADR's intent holds: only counted bot reviews, never human or later reviews, and
    no unbounded fan-out. A larger counted set gets a typed visible refusal, and a
    ceiling whose cause is later disproven is closed by ceiling reconciliation.
12. Releasing a disproven rejection ceiling (e.g. a human dismissed the counted
    reviews on GitHub, or the ceiling was lowered/disabled) is a distinct observed
    transition, `RELEASE_CEILING`, that advances the generation, marks a fresh
    review owed and projects the PR back to the trigger label (unless other holds
    remain). Advancing the generation fences any in-flight review submission of the
    old generation, so a stale baseline can never leave a released PR unreviewed
    (the keifu #173 dead-state class). This is the only observed transition that
    advances a generation; operator re-arms remain the only commanded ones.
