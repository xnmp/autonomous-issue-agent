"""Review-watcher decision behaviour."""

from dataclasses import replace

import pytest

from tests.domain.builders import pr, world

from issue_agent.domain.actions import AgentKind, PostComment, Relabel, RunAgent, Target
from issue_agent.domain.exemptions import (
    ExemptionGrant,
    authenticated_grant_pairs,
    grant_mac,
)
from issue_agent.domain.labels import Label
from issue_agent.domain.review import (
    ApprovedSubmission,
    ChangesRequestedSubmission,
    CompleteGrants,
    EscalatedSubmission,
    Finding,
    RejectedExemptions,
    ReviewConfig,
    ReviewMode,
    ReviewState,
    ReviewTarget,
    count_prior_rejections,
    decide_review,
    gate_submission,
    needs_initial_review,
    needs_re_review,
    parse_findings,
    patch_signature,
    resolve_authenticated_grants,
    review_submission,
    reviewer_verdict_on_head,
    synthesize_review,
    unproductive_ceiling_hit,
    unproductive_escalation_reason,
)
from tests.domain.builders import Review

CFG = ReviewConfig(review_bot="review-bot")


def test_latest_reviewer_verdict_must_target_the_current_head():
    reviews = (
        Review("APPROVED", "review-bot[bot]", "2026-07-27T01:00:00Z", commit_id="old"),
        Review("CHANGES_REQUESTED", "review-bot", "2026-07-27T02:00:00Z", commit_id="new"),
    )
    assert reviewer_verdict_on_head(
        pr(1, reviews=reviews, head_ref_oid="new"), "review-bot"
    )
    assert not reviewer_verdict_on_head(
        pr(1, reviews=reviews, head_ref_oid="old"), "review-bot"
    )


def test_another_reviewers_verdict_does_not_suppress_review():
    reviews = (Review("APPROVED", "someone-else", commit_id="head"),)
    assert not reviewer_verdict_on_head(
        pr(1, reviews=reviews, head_ref_oid="head"), "review-bot"
    )


def test_comment_review_is_not_a_verdict_on_head():
    reviews = (Review("COMMENTED", "review-bot", commit_id="head"),)
    assert not reviewer_verdict_on_head(
        pr(1, reviews=reviews, head_ref_oid="head"), "review-bot"
    )


# ── ReviewMode resolution ──
def test_review_mode_default_is_single():
    assert ReviewMode.from_env("") is ReviewMode.SINGLE


def test_review_mode_explicit_wins():
    assert ReviewMode.from_env("delegated") is ReviewMode.DELEGATED
    assert ReviewMode.from_env("multi") is ReviewMode.MULTI
    assert ReviewMode.from_env("single") is ReviewMode.SINGLE


def test_review_mode_unknown_falls_back_to_single():
    # fail-safe: a typo'd mode never silently disables review
    assert ReviewMode.from_env("delegted") is ReviewMode.SINGLE


def test_review_mode_case_insensitive():
    assert ReviewMode.from_env("DELEGATED") is ReviewMode.DELEGATED


def test_initial_review_for_unsessioned_pr():
    p = pr(100, "ai-pr-open", head_ref_oid="abc")
    actions = decide_review(world(prs=[p]), CFG, ReviewState())
    assert RunAgent(AgentKind.REVIEW, 100) in actions


def test_re_review_when_sha_advances():
    p = pr(100, "ai-pr-open", head_ref_oid="def")
    st = ReviewState(sessions=frozenset({100}), last_sha={100: "abc"})
    actions = decide_review(world(prs=[p]), CFG, st)
    assert RunAgent(AgentKind.RE_REVIEW, 100) in actions


def test_no_re_review_when_sha_unchanged():
    p = pr(100, "ai-pr-open", head_ref_oid="abc")
    st = ReviewState(sessions=frozenset({100}), last_sha={100: "abc"})
    assert decide_review(world(prs=[p]), CFG, st) == []


def test_solo_mode_disables_reviewer():
    cfg = ReviewConfig(review_bot="review-bot", identity_mode="solo")
    p = pr(100, "ai-pr-open", head_ref_oid="abc")
    assert decide_review(world(prs=[p]), cfg, ReviewState()) == []


# ── configurable trigger label (external-repo reviewer) ───────────────────────
def test_custom_trigger_label_fires_on_ai_review():
    # An external-repo deployment triggers on `ai-review`, not `ai-pr-open`.
    cfg = ReviewConfig(review_bot="review-bot", trigger_label=Label.AI_REVIEW)
    p = pr(815, "ai-review", head_ref_oid="abc")
    assert RunAgent(AgentKind.REVIEW, 815) in decide_review(world(prs=[p]), cfg, ReviewState())


def test_custom_trigger_label_ignores_ai_pr_open():
    # With trigger=ai-review, a plain ai-pr-open PR is NOT picked up.
    cfg = ReviewConfig(review_bot="review-bot", trigger_label=Label.AI_REVIEW)
    p = pr(100, "ai-pr-open", head_ref_oid="abc")
    assert decide_review(world(prs=[p]), cfg, ReviewState()) == []


def test_default_trigger_label_is_ai_pr_open():
    # Default unchanged: an ai-review PR is ignored by the stock reviewer.
    p = pr(815, "ai-review", head_ref_oid="abc")
    assert decide_review(world(prs=[p]), CFG, ReviewState()) == []


# ── rogue-PR adoption ─────────────────────────────────────────────────────────
ADOPT = ReviewConfig(review_bot="review-bot", adopt_unlabeled=True)


def test_adopts_unlabeled_open_pr():
    # A PR with no trigger label → labelled with it AND reviewed the same poll,
    # so a comment lands on the first tick (not a poll later).
    p = pr(200, head_ref_oid="abc", author="human")
    acts = decide_review(world(prs=[p]), ADOPT, ReviewState())
    assert Relabel(Target.PR, 200, None, Label.AI_PR_OPEN, "abc") in acts
    assert RunAgent(AgentKind.REVIEW, 200) in acts


def test_adopt_off_by_default_leaves_rogue_pr_alone():
    p = pr(200, head_ref_oid="abc", author="human")
    assert decide_review(world(prs=[p]), CFG, ReviewState()) == []  # CFG adopt=False


def test_adopt_skips_draft():
    p = pr(200, head_ref_oid="abc", author="human", is_draft=True)
    assert decide_review(world(prs=[p]), ADOPT, ReviewState()) == []


def test_adopt_skips_review_bots_own_pr():
    p = pr(200, head_ref_oid="abc", author="review-bot")
    assert decide_review(world(prs=[p]), ADOPT, ReviewState()) == []


def test_c1_adopt_skips_own_pr_when_config_carries_the_bot_suffix():
    # review C1: REVIEW_BOT='review-bot[bot]' (App form) vs the bare `review-bot`
    # gh reports as the PR author. A raw `==` would ADOPT the reviewer's own PR
    # and review it — the self-review loop the guard exists to prevent.
    cfg = ReviewConfig(review_bot="review-bot[bot]", adopt_unlabeled=True)
    p = pr(200, head_ref_oid="abc", author="review-bot")
    assert decide_review(world(prs=[p]), cfg, ReviewState()) == []


def test_c1_adopt_skips_own_pr_when_author_carries_the_bot_suffix():
    # The asymmetry runs both ways: bare config, suffixed author.
    p = pr(200, head_ref_oid="abc", author="review-bot[bot]")
    assert decide_review(world(prs=[p]), ADOPT, ReviewState()) == []


def test_c1_adopt_self_guard_is_case_insensitive():
    p = pr(200, head_ref_oid="abc", author="Review-Bot")
    assert decide_review(world(prs=[p]), ADOPT, ReviewState()) == []


def test_c1_suffix_tolerance_still_adopts_a_strangers_pr():
    # Anti-vacuity twin: the self-guard must not swallow everyone — a genuinely
    # different author's rogue PR is still adopted and reviewed.
    cfg = ReviewConfig(review_bot="review-bot[bot]", adopt_unlabeled=True)
    p = pr(200, head_ref_oid="abc", author="some-human")
    acts = decide_review(world(prs=[p]), cfg, ReviewState())
    assert Relabel(Target.PR, 200, None, Label.AI_PR_OPEN, "abc") in acts
    assert RunAgent(AgentKind.REVIEW, 200) in acts


def test_adopt_skips_opt_out_label():
    p = pr(200, "no-ai-review", head_ref_oid="abc", author="human")
    assert decide_review(world(prs=[p]), ADOPT, ReviewState()) == []


def test_adopt_skips_already_labelled_pr():
    # already carries the trigger label → normal review path, not re-adopted.
    p = pr(200, "ai-pr-open", head_ref_oid="abc", author="human")
    acts = decide_review(world(prs=[p]), ADOPT, ReviewState())
    assert RunAgent(AgentKind.REVIEW, 200) in acts
    assert not any(isinstance(a, Relabel) for a in acts)


def test_adopt_skips_parked_pr():
    # An operator park removes the trigger label to take the PR OUT of the
    # pipeline; adoption re-labelling it undid the park and let the budget arm
    # re-escalate (keifu PR #179, 2026-08-03). Parked PRs stay the operator's.
    p = pr(200, "ai-parked", head_ref_oid="abc", author="human")
    assert decide_review(world(prs=[p]), ADOPT, ReviewState()) == []


def test_adopt_skips_closed_pr():
    p = pr(200, head_ref_oid="abc", author="human", state="MERGED")
    assert decide_review(world(prs=[p]), ADOPT, ReviewState()) == []


def test_needs_helpers():
    p = pr(100, "ai-pr-open", head_ref_oid="abc")
    assert needs_initial_review(p, ReviewState()) is True
    st = ReviewState(sessions=frozenset({100}), last_sha={100: "abc"})
    assert needs_initial_review(p, st) is False
    assert needs_re_review(p, st) is False


# ── unproductive-round ceiling (review P0R-3) ─────────────────────────────────
# The reviewer used to have NO ceiling of any kind: one RunAgent per eligible PR
# per poll, no `consumed` gate, no breaker read. A PR whose review never
# persists state (session dies pre-result, agent errors, MULTI fan-out
# synthesises nothing) was re-dispatched at full cost every poll, forever.
CAPPED = ReviewConfig(review_bot="review-bot", max_unproductive_rounds=3)


def _state(pr_number, rounds, **kw):
    return ReviewState(unproductive_rounds={pr_number: rounds}, **kw)


def test_below_the_ceiling_the_review_still_dispatches():
    """Anti-vacuity FIRST: a ceiling that fires early is a reviewer that never
    reviews. Two dead rounds out of three still buys the third."""
    p = pr(100, "ai-pr-open", head_ref_oid="abc")
    acts = decide_review(world(prs=[p]), CAPPED, _state(100, 2))
    assert acts == [RunAgent(AgentKind.REVIEW, 100)]


def test_at_the_ceiling_the_dispatch_is_withheld_and_the_pr_escalates():
    p = pr(100, "ai-pr-open", head_ref_oid="abc")
    acts = decide_review(world(prs=[p]), CAPPED, _state(100, 3))
    # Set equality, not membership (review §5): the ONLY thing this poll may emit
    # is the human handoff — no RunAgent hiding beside it.
    assert acts == [
        PostComment(Target.PR, 100, "**Circuit breaker:** "
                    + unproductive_escalation_reason(3) + " Escalating to a human."),
        Relabel(Target.PR, 100, Label.AI_PR_OPEN, Label.NEEDS_HUMAN_REVIEW),
    ]


def test_the_ceiling_also_stops_a_re_review_loop():
    """A PR that was reviewed once and then advances its SHA re-enters through
    `needs_re_review` — a second, equally unbounded entrance."""
    p = pr(100, "ai-pr-open", head_ref_oid="def")
    st = ReviewState(sessions=frozenset({100}), last_sha={100: "abc"},
                     unproductive_rounds={100: 3})
    acts = decide_review(world(prs=[p]), CAPPED, st)
    assert not any(isinstance(a, RunAgent) for a in acts)
    assert Relabel(Target.PR, 100, Label.AI_PR_OPEN, Label.NEEDS_HUMAN_REVIEW) in acts


def test_escalation_is_edge_triggered_on_the_needs_human_review_label():
    """P1R-2's shape, pre-empted: a PR already handed to a human emits NOTHING.
    Without this a stuck relabel converts one paid loop into a comment loop —
    one 'Circuit breaker' comment + one ALERT_CMD run per poll, ~4,300/day."""
    p = pr(100, "ai-pr-open", "needs-human-review", head_ref_oid="abc")
    assert decide_review(world(prs=[p]), CAPPED, _state(100, 3)) == []


def test_a_ceiling_of_zero_disables_the_breaker():
    """cap<=0 disables, like every other breaker in the harness."""
    cfg = ReviewConfig(review_bot="review-bot", max_unproductive_rounds=0)
    p = pr(100, "ai-pr-open", head_ref_oid="abc")
    assert decide_review(world(prs=[p]), cfg, _state(100, 99)) == [
        RunAgent(AgentKind.REVIEW, 100)
    ]


def test_the_ceiling_is_per_pr_and_does_not_leak_across_prs():
    a = pr(100, "ai-pr-open", head_ref_oid="abc")
    b = pr(101, "ai-pr-open", head_ref_oid="abc")
    acts = decide_review(world(prs=[a, b]), CAPPED, _state(100, 3))
    assert RunAgent(AgentKind.REVIEW, 101) in acts
    assert RunAgent(AgentKind.REVIEW, 100) not in acts


def test_default_ceiling_is_armed_without_any_configuration():
    """The default MUST be a finite number. A ReviewConfig built with no ceiling
    argument is what every existing caller constructs, and 'unset' meaning
    'unbounded' is exactly the state P0R-3 describes."""
    assert ReviewConfig(review_bot="r").max_unproductive_rounds == 5
    p = pr(100, "ai-pr-open", head_ref_oid="abc")
    st = _state(100, 5)
    assert not any(
        isinstance(a, RunAgent)
        for a in decide_review(world(prs=[p]), ReviewConfig(review_bot="r"), st)
    )


def test_adoption_is_a_second_entrance_and_is_bounded_too():
    """A rogue PR is adopted AND reviewed the same tick, so the adoption arm
    needs the same guard — otherwise the ceiling just moves the loop."""
    p = pr(200, head_ref_oid="abc", author="human")
    assert decide_review(world(prs=[p]), replace(ADOPT, max_unproductive_rounds=3),
                         _state(200, 3)) == []
    # anti-vacuity: below the cap the rogue PR is still adopted and reviewed.
    acts = decide_review(world(prs=[p]), replace(ADOPT, max_unproductive_rounds=3),
                         _state(200, 2))
    assert Relabel(Target.PR, 200, None, Label.AI_PR_OPEN, "abc") in acts
    assert RunAgent(AgentKind.REVIEW, 200) in acts


def test_an_escalated_pr_is_not_re_adopted_into_the_loop():
    """The escalation swaps the trigger label for needs-human-review, which
    drops the PR out of `prs_with(trigger_label)` — straight into the adoption
    sweep. Without the needs-human-review exclusion, an adopting deployment
    re-labels and re-dispatches the escalated PR on the very next poll and the
    ceiling buys nothing at all."""
    escalated = pr(200, "needs-human-review", head_ref_oid="abc", author="human")
    assert decide_review(world(prs=[escalated]), ADOPT, ReviewState()) == []


def test_unproductive_ceiling_hit_is_the_shared_breaker_primitive():
    cfg = ReviewConfig(review_bot="r", max_unproductive_rounds=2)
    assert unproductive_ceiling_hit(_state(1, 1), cfg, 1) is False
    assert unproductive_ceiling_hit(_state(1, 2), cfg, 1) is True
    assert unproductive_ceiling_hit(_state(1, 9), cfg, 1) is True
    assert unproductive_ceiling_hit(ReviewState(), cfg, 1) is False  # unknown PR = 0


# ── count_prior_rejections: the ceiling counts rejections, not pushes ─────────
def test_count_prior_rejections_counts_only_bot_changes_requested():
    # No intervening APPROVE here — pure exclusion test: only the bot's own
    # CHANGES_REQUESTED count; human RCs, the bot's COMMENTs are excluded.
    reviews = (
        Review(state="CHANGES_REQUESTED", author="my-reviewer-bot"),
        Review(state="CHANGES_REQUESTED", author="some-human"),        # not the bot
        Review(state="COMMENTED", author="my-reviewer-bot"),      # not a rejection
        Review(state="CHANGES_REQUESTED", author="my-reviewer-bot"),
    )
    assert count_prior_rejections(reviews, "my-reviewer-bot") == 2


def test_count_prior_rejections_normalizes_bot_suffix():
    # gh returns the review author WITHOUT the [bot] suffix, but REVIEW_BOT config
    # carries it. A raw == would count zero and the ceiling would never fire.
    reviews = (Review(state="CHANGES_REQUESTED", author="my-reviewer-bot"),)
    assert count_prior_rejections(reviews, "my-reviewer-bot[bot]") == 1


def test_c1_count_prior_rejections_is_case_insensitive():
    # GitHub logins are case-insensitive; the rejection ceiling must not miss the
    # bot's own rejections because the config differs in case (review C1).
    reviews = (Review(state="CHANGES_REQUESTED", author="My-Reviewer-Bot"),)
    assert count_prior_rejections(reviews, "my-reviewer-bot[bot]") == 1


def test_c1_count_prior_rejections_still_excludes_other_actors():
    # Anti-vacuity twin: suffix/case tolerance must not count a stranger's
    # rejection toward the reviewer's own ceiling.
    reviews = (
        Review(state="CHANGES_REQUESTED", author="some-human"),
        Review(state="CHANGES_REQUESTED", author="other-bot[bot]"),
    )
    assert count_prior_rejections(reviews, "my-reviewer-bot[bot]") == 0


def test_count_prior_rejections_empty():
    assert count_prior_rejections((), "review-bot") == 0


def test_count_prior_rejections_resets_on_approve():
    # RC → RC → RC → APPROVE (fixer satisfied it) → author reopens work → RC on the
    # new revision. The 3 pre-approval rejections were RESOLVED; the ceiling must see
    # only the ONE post-approve rejection, not 4. Otherwise a satisfied cycle would
    # escalate the very next rejection.
    reviews = (
        Review(state="CHANGES_REQUESTED", author="review-bot", submitted_at="t1"),
        Review(state="CHANGES_REQUESTED", author="review-bot", submitted_at="t2"),
        Review(state="CHANGES_REQUESTED", author="review-bot", submitted_at="t3"),
        Review(state="APPROVED", author="review-bot", submitted_at="t4"),
        Review(state="CHANGES_REQUESTED", author="review-bot", submitted_at="t5"),
    )
    assert count_prior_rejections(reviews, "review-bot") == 1


def test_count_prior_rejections_all_before_approve_is_zero():
    # Approved and nothing since → a fresh push draws its first rejection at prior=0.
    reviews = (
        Review(state="CHANGES_REQUESTED", author="review-bot", submitted_at="t1"),
        Review(state="APPROVED", author="review-bot", submitted_at="t2"),
    )
    assert count_prior_rejections(reviews, "review-bot") == 0


def test_incomplete_current_head_approval_does_not_reset_rejections():
    reviews = (
        Review(
            state="CHANGES_REQUESTED",
            author="review-bot",
            submitted_at="t1",
            commit_id="head",
        ),
        Review(
            state="CHANGES_REQUESTED",
            author="review-bot",
            submitted_at="t2",
            commit_id="head",
        ),
        Review(
            state="CHANGES_REQUESTED",
            author="review-bot",
            submitted_at="t3",
            commit_id="head",
        ),
        Review(
            state="APPROVED",
            author="review-bot",
            submitted_at="t4",
            commit_id="head",
        ),
    )

    assert count_prior_rejections(
        reviews,
        "review-bot",
        non_resetting_approval_commits=frozenset({"head"}),
    ) == 3


# ── review_submission: verdict → effect, with the rejection ceiling ───────────
# The verdict under test is the (prior_rejections + 1)-th rejection; cap here is 4.
REVIEW_TARGET = ReviewTarget(42, "test-patch")
AUTHENTICATED_EMPTY = authenticated_grant_pairs(
    (), issue_number=42, patch_id="test-patch", mac_key="test-key"
)
COMPLETE_GRANTS = resolve_authenticated_grants(
    (), AUTHENTICATED_EMPTY, target=REVIEW_TARGET
)
assert isinstance(COMPLETE_GRANTS, CompleteGrants)


def test_submission_approve_wins_even_at_ceiling():
    # A rebase/housekeeping push that ends in APPROVE never escalates, whatever the
    # prior rejection count.
    sub = review_submission(
        "APPROVE", "lgtm", prior_rejections=3, max_rejections=4,
        grant_resolution=COMPLETE_GRANTS,
        target=REVIEW_TARGET,
    )
    assert sub.event == "APPROVE" and sub.escalate is False


def test_submission_approval_without_required_grants_requests_changes():
    sub = review_submission(
        "APPROVE",
        "No code findings; additive tests need no exemption.",
        prior_rejections=0,
        max_rejections=4,
        grant_resolution=RejectedExemptions(
            (("tests-immutable", "tests/existing.spec.ts"),)
        ),
        target=REVIEW_TARGET,
    )

    assert isinstance(sub, ChangesRequestedSubmission)
    assert sub.event == "REQUEST_CHANGES"
    assert sub.escalate is False
    assert sub.rejected_exemptions is not None
    assert "not a mergeable approval" in sub.body
    assert "`tests-immutable` for `tests/existing.spec.ts`" in sub.body
    assert "additive coverage belongs in a new test file" in sub.body
    assert "Reviewer rationale:" in sub.body


def test_submission_ungranted_approval_obeys_rejection_ceiling():
    sub = review_submission(
        "APPROVE",
        "refused",
        prior_rejections=3,
        max_rejections=4,
        grant_resolution=RejectedExemptions((("required-test", "*"),)),
        target=REVIEW_TARGET,
    )

    assert isinstance(sub, EscalatedSubmission)
    assert sub.event == "COMMENT"
    assert sub.escalate is True
    assert sub.escalate_cause == "ceiling"


def test_approval_variant_requires_complete_grants_at_runtime():
    with pytest.raises(TypeError, match="authenticated grant resolution"):
        CompleteGrants(None, "", (), object())

    approved = ApprovedSubmission("looks good", COMPLETE_GRANTS, REVIEW_TARGET)
    assert approved.event == "APPROVE"
    assert approved.escalate is False


def test_complete_grant_proof_is_bound_to_issue_patch_and_requests():
    request = ("tests-immutable", "tests/existing.spec.ts")
    grant = ExemptionGrant(
        request[0],
        42,
        request[1],
        "patch-42",
        grant_mac("test-key", request[0], 42, request[1], "patch-42"),
    )
    authenticated = authenticated_grant_pairs(
        (grant,), issue_number=42, patch_id="patch-42", mac_key="test-key"
    )
    resolved = resolve_authenticated_grants(
        (request,),
        authenticated,
        target=ReviewTarget(42, "patch-42"),
    )

    assert isinstance(resolved, CompleteGrants)
    assert resolved.issue_number == 42
    assert resolved.patch_id == "patch-42"
    assert resolved.requests == (request,)


def test_complete_grant_proof_fails_closed_without_patch_binding():
    request = ("tests-immutable", "tests/existing.spec.ts")
    authenticated = authenticated_grant_pairs(
        (), issue_number=42, patch_id="", mac_key="test-key"
    )
    resolved = resolve_authenticated_grants(
        (request,),
        authenticated,
        target=ReviewTarget(42, ""),
    )

    assert isinstance(resolved, RejectedExemptions)
    assert resolved.requests == (request,)


def test_approval_rejects_complete_grants_for_another_target():
    with pytest.raises(ValueError, match="different review target"):
        ApprovedSubmission(
            "looks good", COMPLETE_GRANTS, ReviewTarget(43, "other-patch")
        )


def test_complete_grants_cannot_be_rebound_with_dataclass_replace():
    with pytest.raises(TypeError):
        replace(COMPLETE_GRANTS, issue_number=43, patch_id="other-patch")


def test_submission_zero_disables_ceiling_for_ungranted_approval():
    sub = review_submission(
        "APPROVE",
        "refused",
        prior_rejections=10_000,
        max_rejections=0,
        grant_resolution=RejectedExemptions((("required-test", "*"),)),
        target=REVIEW_TARGET,
    )

    assert sub.event == "REQUEST_CHANGES"
    assert sub.escalate is False


def test_submission_zero_disables_ceiling_for_ordinary_rejection():
    sub = review_submission(
        "REQUEST_CHANGES",
        "still wrong",
        prior_rejections=10_000,
        max_rejections=0,
        grant_resolution=COMPLETE_GRANTS,
        target=REVIEW_TARGET,
    )

    assert sub.event == "REQUEST_CHANGES"
    assert sub.escalate is False


def test_submission_deduplicates_ungranted_requests_and_allows_empty_body():
    request = ("tests-immutable", "tests/existing.spec.ts")
    sub = review_submission(
        "APPROVE",
        "",
        prior_rejections=0,
        max_rejections=4,
        grant_resolution=RejectedExemptions((request, request)),
        target=REVIEW_TARGET,
    )

    assert sub.event == "REQUEST_CHANGES"
    assert sub.body.count("`tests-immutable` for `tests/existing.spec.ts`") == 1
    assert "Reviewer rationale:" not in sub.body


def test_submission_request_changes_below_ceiling_bounces_back():
    # 3rd rejection (2 prior) with a cap of 4 → still bounces back to the fixer.
    sub = review_submission(
        "REQUEST_CHANGES", "fix x", prior_rejections=2, max_rejections=4,
        grant_resolution=COMPLETE_GRANTS,
        target=REVIEW_TARGET,
    )
    assert sub.event == "REQUEST_CHANGES" and sub.escalate is False


def test_submission_request_changes_at_ceiling_escalates():
    # The breaker: the 4th rejection (3 prior) can no longer bounce the PR back —
    # it escalates to a human regardless of the model verdict.
    sub = review_submission(
        "REQUEST_CHANGES", "still wrong", prior_rejections=3, max_rejections=4,
        grant_resolution=COMPLETE_GRANTS,
        target=REVIEW_TARGET,
    )
    assert sub.event == "COMMENT" and sub.escalate is True
    assert sub.escalate_cause == "ceiling"


def test_submission_explicit_escalate_always_escalates():
    sub = review_submission(
        "ESCALATE", "deadlocked", prior_rejections=0, max_rejections=4,
        grant_resolution=COMPLETE_GRANTS,
        target=REVIEW_TARGET,
    )
    assert sub.escalate is True
    # The cause names the verdict, not the ceiling: keifu PR #177 escalated on
    # a round-3 ESCALATE but the breaker comment blamed the ceiling, reading
    # like a fencepost bug to the operator.
    assert sub.escalate_cause == "verdict"


def test_submission_escalate_verdict_at_ceiling_reports_verdict_cause():
    # When both apply, the reviewer's explicit verdict is the truer cause.
    sub = review_submission(
        "ESCALATE", "deadlocked", prior_rejections=3, max_rejections=4,
        grant_resolution=COMPLETE_GRANTS,
        target=REVIEW_TARGET,
    )
    assert sub.escalate is True and sub.escalate_cause == "verdict"


def test_submission_non_escalating_paths_carry_no_cause():
    ok = review_submission(
        "APPROVE", "lgtm", prior_rejections=0, max_rejections=4,
        grant_resolution=COMPLETE_GRANTS,
        target=REVIEW_TARGET,
    )
    bounce = review_submission(
        "REQUEST_CHANGES", "fix", prior_rejections=0, max_rejections=4,
        grant_resolution=COMPLETE_GRANTS,
        target=REVIEW_TARGET,
    )
    assert ok.escalate_cause == "" and bounce.escalate_cause == ""


# ── multi-agent review: parse_findings + synthesize_review ────────────────────
def test_parse_findings_keeps_wellformed_and_drops_junk():
    structured = {"findings": [
        {"location": "a.py:10", "claim": "off-by-one", "severity": "blocking"},
        {"location": "", "claim": "no location"},          # dropped
        {"location": "b.py:2", "claim": ""},                 # dropped
        {"location": "c.py:3", "claim": "bad", "severity": "weird"},  # sev→advisory
        "not-a-dict",                                        # dropped
    ]}
    fs = parse_findings("correctness", structured)
    assert [f.location for f in fs] == ["a.py:10", "c.py:3"]
    assert fs[0].severity == "blocking" and fs[0].is_blocking
    assert fs[1].severity == "advisory"          # coerced
    assert all(f.dimension == "correctness" for f in fs)


def test_parse_findings_tolerates_missing_envelope():
    assert parse_findings("security", None) == []
    assert parse_findings("security", {}) == []
    assert parse_findings("security", {"findings": None}) == []


def test_synthesis_no_confirmed_findings_approves():
    fs = [Finding("correctness", "a.py:1", "maybe", "blocking", confirmed=False)]
    sub = synthesize_review(
        fs, prior_rejections=0, max_rejections=4,
        grant_resolution=COMPLETE_GRANTS,
        target=REVIEW_TARGET,
    )
    assert sub.event == "APPROVE" and sub.escalate is False


def test_synthesis_empty_approves():
    sub = synthesize_review(
        [], prior_rejections=0, max_rejections=4,
        grant_resolution=COMPLETE_GRANTS,
        target=REVIEW_TARGET,
    )
    assert sub.event == "APPROVE"
    assert "LGTM" in sub.body


def test_synthesis_confirmed_blocking_requests_changes():
    fs = [
        Finding("security", "x.py:9", "SQL injection", "blocking", confirmed=True,
                verdict="user input concatenated"),
        Finding("correctness", "y.py:2", "nit", "advisory", confirmed=True),
    ]
    sub = synthesize_review(
        fs, prior_rejections=1, max_rejections=4,
        grant_resolution=COMPLETE_GRANTS,
        target=REVIEW_TARGET,
    )
    assert sub.event == "REQUEST_CHANGES"
    assert "x.py:9" in sub.body and "Must fix" in sub.body
    assert "SQL injection" in sub.body


def test_synthesis_only_advisory_confirmed_approves_with_notes():
    fs = [Finding("architecture", "z.py:4", "consider extracting", "advisory", confirmed=True)]
    sub = synthesize_review(
        fs, prior_rejections=0, max_rejections=4,
        grant_resolution=COMPLETE_GRANTS,
        target=REVIEW_TARGET,
    )
    assert sub.event == "APPROVE"
    assert "z.py:4" in sub.body and "Non-blocking" in sub.body


def test_synthesis_blocking_at_ceiling_escalates():
    # The rejection ceiling still bites: a confirmed blocking finding that would be
    # the 4th rejection (3 prior) can no longer bounce the PR back — it escalates.
    fs = [Finding("correctness", "a.py:1", "real bug", "blocking", confirmed=True)]
    sub = synthesize_review(
        fs, prior_rejections=3, max_rejections=4,
        grant_resolution=COMPLETE_GRANTS,
        target=REVIEW_TARGET,
    )
    assert sub.event == "COMMENT" and sub.escalate is True


def test_synthesis_unconfirmed_blocking_ignored():
    # A refuted (confirmed=False) blocking candidate must NOT drive REQUEST_CHANGES —
    # the false-positive killer working end to end.
    fs = [
        Finding("security", "a.py:1", "false alarm", "blocking", confirmed=False),
        Finding("correctness", "b.py:2", "real advisory", "advisory", confirmed=True),
    ]
    sub = synthesize_review(
        fs, prior_rejections=0, max_rejections=4,
        grant_resolution=COMPLETE_GRANTS,
        target=REVIEW_TARGET,
    )
    assert sub.event == "APPROVE"


# ── completeness guard: an incomplete review must not silently APPROVE ─────────
def test_synthesis_incomplete_and_no_finding_returns_none():
    # A dimension/validator leg errored (complete=False) and nothing blocking
    # survived → return None so the caller submits NOTHING and retries, rather than
    # approving a PR whose defect-detecting lens may never have run.
    assert synthesize_review(
        [], prior_rejections=0, max_rejections=4, complete=False,
        grant_resolution=COMPLETE_GRANTS,
        target=REVIEW_TARGET,
    ) is None


def test_synthesis_incomplete_but_blocking_still_reports():
    # A confirmed blocking finding is reported even when incomplete — a real bug
    # doesn't wait on the flaky leg.
    fs = [Finding("security", "a.py:1", "real", "blocking", confirmed=True)]
    sub = synthesize_review(
        fs, prior_rejections=0, max_rejections=4, complete=False,
        grant_resolution=COMPLETE_GRANTS,
        target=REVIEW_TARGET,
    )
    assert sub is not None and sub.event == "REQUEST_CHANGES"


def test_synthesis_complete_no_finding_approves():
    # The default (complete=True) still approves on a clean review.
    sub = synthesize_review(
        [], prior_rejections=0, max_rejections=4, complete=True,
        grant_resolution=COMPLETE_GRANTS,
        target=REVIEW_TARGET,
    )
    assert sub is not None and sub.event == "APPROVE"


def test_gate_submission_approves():
    sub = gate_submission(
        prior_rejections=0,
        max_rejections=4,
        grant_resolution=COMPLETE_GRANTS,
        target=REVIEW_TARGET,
    )
    assert sub.event == "APPROVE" and sub.escalate is False
    assert "docs/formatting/config" in sub.body


def test_gate_submission_cannot_approve_rejected_exemptions():
    sub = gate_submission(
        prior_rejections=3,
        max_rejections=4,
        grant_resolution=RejectedExemptions(
            (("tests-immutable", "tests/existing.spec.ts"),)
        ),
        target=REVIEW_TARGET,
    )

    assert isinstance(sub, EscalatedSubmission)
    assert sub.event == "COMMENT"
    assert sub.escalate_cause == "ceiling"


# ── rebase-invariant patch identity (harness #2) ──────────────────────────────
# The behaviour under test is a MONEY decision: equal signature means the
# reviewer may carry its verdict forward instead of buying another round, so the
# tests are about which diff pairs are the same CHANGE, not about the hashing.

_BEFORE = """diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -10,7 +10,8 @@ def handler(req):
     ctx = build(req)
-    return ctx.render()
+    if ctx.empty:
+        return None
+    return ctx.render()
"""

# The same change after a mechanical rebase: new blob hashes (the base moved),
# shifted hunk offsets (an unrelated hunk landed upstream), and a re-derived
# section heading. Not one character of the change itself differs.
_AFTER_REBASE = """diff --git a/app.py b/app.py
index 3333333..4444444 100644
--- a/app.py
+++ b/app.py
@@ -42,7 +42,8 @@ def handler(req, opts):
     ctx = build(req)
-    return ctx.render()
+    if ctx.empty:
+        return None
+    return ctx.render()
"""


def test_a_mechanical_rebase_keeps_the_patch_signature():
    # THE acceptance criterion: a force-push that only moved blob hashes and hunk
    # offsets must be recognisable as the already-reviewed patch.
    assert patch_signature(_BEFORE) == patch_signature(_AFTER_REBASE)


def test_a_changed_body_line_changes_the_signature():
    revised = _AFTER_REBASE.replace("return None", "return EMPTY")
    assert patch_signature(revised) != patch_signature(_AFTER_REBASE)


def test_an_added_line_changes_the_signature():
    extended = _AFTER_REBASE.replace(
        "+    return ctx.render()", "+    log(ctx)\n+    return ctx.render()"
    )
    assert patch_signature(extended) != patch_signature(_AFTER_REBASE)


def test_changed_context_changes_the_signature():
    # A rebase that pulled in an upstream edit ADJACENT to our change alters the
    # context lines: the reviewer is looking at different code, so this must NOT
    # be carried forward (matches `git patch-id`, which also hashes context).
    moved = _AFTER_REBASE.replace("    ctx = build(req)", "    ctx = build(req, opts)")
    assert patch_signature(moved) != patch_signature(_AFTER_REBASE)


def test_touching_a_different_file_changes_the_signature():
    other = _BEFORE.replace("app.py", "worker.py")
    assert patch_signature(other) != patch_signature(_BEFORE)


def test_a_second_file_changes_the_signature():
    assert patch_signature(_BEFORE + _BEFORE.replace("app.py", "worker.py")) != (
        patch_signature(_BEFORE)
    )


def test_a_rename_is_signature_stable_across_recomputed_similarity():
    before = (
        "diff --git a/old.py b/new.py\n"
        "similarity index 96%\n"
        "rename from old.py\n"
        "rename to new.py\n"
    )
    after = before.replace("96%", "91%")
    assert patch_signature(before) == patch_signature(after)


def test_a_rename_to_a_different_path_changes_the_signature():
    before = (
        "diff --git a/old.py b/new.py\n"
        "similarity index 96%\n"
        "rename from old.py\n"
        "rename to new.py\n"
    )
    assert patch_signature(before.replace("new.py", "other.py")) != patch_signature(before)


def test_line_endings_are_not_a_patch_change():
    assert patch_signature(_BEFORE.replace("\n", "\r\n")) == patch_signature(_BEFORE)


def test_a_trailing_newline_is_not_a_patch_change():
    assert patch_signature(_BEFORE) == patch_signature(_BEFORE.rstrip("\n"))


def test_the_signature_is_deterministic_across_calls():
    # It is compared against a value persisted by an EARLIER process, so a
    # per-process salt (e.g. hash()) would silently disable the whole feature.
    assert patch_signature(_BEFORE) == patch_signature(_BEFORE)


def test_an_empty_diff_has_a_stable_signature():
    assert patch_signature("") == patch_signature("")
    assert patch_signature("") != patch_signature(_BEFORE)


def test_malformed_input_is_total():
    # Never raises: a crash here happens mid-poll on a PR with a verdict pending.
    for bad in (None, 0, [], {"a": 1}, b"diff --git a/x b/x\n", "\x00\udcff not a diff"):
        assert isinstance(patch_signature(bad), str)
        assert len(patch_signature(bad)) == 64


def test_a_diff_with_no_trailing_content_after_the_hunk_header_is_total():
    assert isinstance(patch_signature("@@ -1 +1 @@"), str)


def test_a_removed_index_like_body_line_is_not_stripped():
    # `-index a..b 100644` is a DELETED LINE of a file that happens to look like a
    # git header; stripping it would make two different patches collide.
    with_body = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-index 1111111..2222222 100644\n+x\n"
    without = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n+x\n"
    assert patch_signature(with_body) != patch_signature(without)


# ── operator re-arm: which reviews still arm the ceiling ─────────────────────
def _sub(review_id, state="CHANGES_REQUESTED", author="review-bot"):
    from issue_agent.domain.review import SubmittedReview

    return SubmittedReview(review_id, state, author, f"2026-08-02T0{review_id}:00Z")


def test_only_the_bots_change_requests_since_its_approve_still_count():
    from issue_agent.domain.review import stale_rejections

    reviews = (
        _sub("1"),
        _sub("2", state="APPROVED"),
        _sub("3"),
        _sub("4", author="a-human"),
        _sub("5", state="COMMENTED"),
        _sub("6"),
    )
    assert [r.review_id for r in stale_rejections(reviews, "review-bot")] == ["3", "6"]


def test_an_operator_held_pr_can_rearm_rejections_before_a_trailing_approval():
    from issue_agent.domain.review import stale_rejections

    reviews = (
        _sub("1"),
        _sub("2"),
        _sub("3"),
        _sub("4", state="APPROVED"),
        _sub("5", state="COMMENTED"),
    )
    assert [
        review.review_id
        for review in stale_rejections(
            reviews, "review-bot", trailing_approvals_reset=False
        )
    ] == ["1", "2", "3"]


def test_an_already_dismissed_rejection_is_not_dismissed_again():
    from issue_agent.domain.review import stale_rejections

    reviews = (_sub("1", state="DISMISSED"), _sub("2"))
    assert [r.review_id for r in stale_rejections(reviews, "review-bot")] == ["2"]


def test_the_dismissal_set_matches_what_the_ceiling_counts():
    """The re-arm's contract: dismiss exactly these and the ceiling reads zero."""
    from issue_agent.domain.review import stale_rejections

    reviews = (_sub("1"), _sub("2"), _sub("3", state="COMMENTED"))
    counted = count_prior_rejections(
        tuple(Review(r.state, r.author, r.submitted_at) for r in reviews),
        "review-bot",
    )
    assert len(stale_rejections(reviews, "review-bot")) == counted


def test_the_dismissal_set_is_bounded_newest_first():
    from issue_agent.domain.review import stale_rejections

    reviews = tuple(_sub(str(n)) for n in range(1, 6))
    assert [r.review_id for r in stale_rejections(reviews, "review-bot", limit=2)] == [
        "4", "5",
    ]


def test_an_empty_or_foreign_review_history_dismisses_nothing():
    from issue_agent.domain.review import stale_rejections

    assert stale_rejections((), "review-bot") == ()
    assert stale_rejections((_sub("1", author="someone-else"),), "review-bot") == ()
    # An unset review bot must select NOTHING: dismissing on a guess retracts
    # verdicts that are not the harness's.
    assert stale_rejections((_sub("1"),), "") == ()


def test_a_state_the_ceiling_does_not_count_is_never_dismissed():
    """The two must select the same set, or "dismiss these and the ceiling
    reads zero" quietly stops holding. A lower-case state is not counted by
    `count_prior_rejections`, so it must not be retracted here either."""
    from issue_agent.domain.review import stale_rejections

    weird = (_sub("1", state=""), _sub("2", state="changes_requested"),
             _sub("3"))
    assert [r.review_id for r in stale_rejections(weird, "review-bot")] == ["3"]
    assert count_prior_rejections(
        tuple(Review(r.state, r.author, r.submitted_at) for r in weird),
        "review-bot",
    ) == 1


# ── self-healing: at ceiling with no attention label (keifu #173 / #25) ───────
CEILING_CFG = ReviewConfig(review_bot="review-bot", max_rejections=4)


def _abandoned_pr(number=9, *labels_, head="head1", rejections=3, escalation=True):
    reviews = tuple(
        Review("CHANGES_REQUESTED", "review-bot", f"2026-08-02T0{n}:00Z",
               commit_id="head1")
        for n in range(rejections)
    )
    if escalation:
        reviews += (
            Review("COMMENTED", "review-bot", "2026-08-02T09:00Z", commit_id=head),
        )
    return pr(number, *labels_, reviews=reviews, head_ref_oid=head)


def _escalations(actions):
    return [a for a in actions if isinstance(a, PostComment)]


def test_a_ceiling_pr_whose_attention_label_was_cleared_is_escalated_again():
    """The dead state: the ceiling escalation posted a COMMENT and swapped the
    trigger label for needs-human-review; an operator unblock removed that label
    and added none, so the reviewer would not review, the fixer would not fix,
    and no further escalation could ever be emitted."""
    actions = decide_review(
        world(prs=(_abandoned_pr(),)), CEILING_CFG, ReviewState()
    )
    comments = _escalations(actions)
    assert len(comments) == 1
    assert "rejection ceiling (4)" in comments[0].body
    relabels = [a for a in actions if isinstance(a, Relabel)]
    assert [(r.target, r.number, r.add) for r in relabels] == [
        (Target.PR, 9, Label.NEEDS_HUMAN_REVIEW)
    ]


def test_the_re_emission_happens_once_per_unblock_not_once_per_tick():
    """Escalating re-applies needs-human-review, and the label's presence is the
    dedup key: the very next poll sees a held PR and says nothing (ADR 0012)."""
    held = _abandoned_pr(9, Label.NEEDS_HUMAN_REVIEW.value)
    assert decide_review(world(prs=(held,)), CEILING_CFG, ReviewState()) == []


def test_a_pr_below_the_ceiling_is_never_re_escalated():
    below = _abandoned_pr(rejections=1)
    assert _escalations(
        decide_review(world(prs=(below,)), CEILING_CFG, ReviewState())
    ) == []


def test_an_incomplete_trailing_approval_does_not_hide_an_abandoned_ceiling():
    abandoned = _abandoned_pr(escalation=False)
    abandoned = replace(
        abandoned,
        reviews=abandoned.reviews
        + (
            Review(
                "APPROVED",
                "review-bot",
                "2026-08-02T08:00Z",
                commit_id="head1",
            ),
            Review(
                "COMMENTED",
                "review-bot",
                "2026-08-02T09:00Z",
                commit_id="head1",
            ),
        ),
    )

    assert len(
        _escalations(decide_review(world(prs=(abandoned,)), CEILING_CFG, ReviewState()))
    ) == 1


def test_a_reviewer_that_has_not_given_up_on_this_head_still_gets_its_round():
    """At 3 of 4 rejections the reviewer may still APPROVE the next revision —
    re-escalating there would cut a healthy loop a round short."""
    still_working = _abandoned_pr(escalation=False)
    assert _escalations(
        decide_review(world(prs=(still_working,)), CEILING_CFG, ReviewState())
    ) == []


def test_a_fixer_push_past_the_escalation_earns_a_re_review_not_a_re_escalation():
    moved = _abandoned_pr(head="head1")
    moved = replace(moved, head_ref_oid="head2")
    assert _escalations(
        decide_review(world(prs=(moved,)), CEILING_CFG, ReviewState())
    ) == []


def test_a_disabled_ceiling_never_re_escalates():
    cfg = replace(CEILING_CFG, max_rejections=0)
    assert _escalations(
        decide_review(world(prs=(_abandoned_pr(),)), cfg, ReviewState())
    ) == []


def test_a_closed_pr_is_left_alone():
    closed = replace(_abandoned_pr(), state="CLOSED")
    assert decide_review(world(prs=(closed,)), CEILING_CFG, ReviewState()) == []


def test_an_abandoned_pr_is_escalated_rather_than_adopted():
    """Adoption matches the same shape (no trigger label, no needs-human-review)
    and would pay for a review that can only skip — the silent half of #173."""
    cfg = replace(CEILING_CFG, adopt_unlabeled=True)
    actions = decide_review(
        world(prs=(_abandoned_pr(),)), cfg, ReviewState()
    )
    assert not [a for a in actions if isinstance(a, RunAgent)]
    assert len(_escalations(actions)) == 1


def test_an_unplaceable_escalation_comment_falls_through_to_the_round_breaker():
    """commit_id unknown ("" — the adapter could not resolve it) fails CLOSED:
    the unproductive-rounds ceiling is the backstop."""
    unplaceable = _abandoned_pr()
    unplaceable = replace(
        unplaceable,
        reviews=unplaceable.reviews[:-1]
        + (Review("COMMENTED", "review-bot", "2026-08-02T09:00Z"),),
    )
    assert _escalations(
        decide_review(world(prs=(unplaceable,)), CEILING_CFG, ReviewState())
    ) == []


def test_a_pr_still_carrying_the_trigger_label_belongs_to_the_review_loop():
    """The reviewer's own labelled arm decides whether it gets a round; acting
    on it here would be a second decision about the same PR in one tick."""
    labelled = _abandoned_pr(9, CEILING_CFG.trigger_label.value)
    assert _escalations(
        decide_review(world(prs=(labelled,)), CEILING_CFG, ReviewState())
    ) == []
