"""Review-watcher decision behaviour."""

from dataclasses import replace

import pytest

from tests.domain.builders import pr, world

from issue_agent.domain.actions import AgentKind, PostComment, Relabel, RunAgent, Target
from issue_agent.domain.labels import Label
from issue_agent.domain.review import (
    ApprovedSubmission,
    ChangesRequestedSubmission,
    EscalatedSubmission,
    Finding,
    ReviewConfig,
    ReviewMode,
    BoundReviewState,
    count_prior_rejections,
    decide_bound_review,
    gate_submission,
    parse_findings,
    patch_signature,
    review_submission,
    reviewer_verdict_on_head,
    synthesize_review,
    unproductive_escalation_reason,
    FIX_HOLD_CEILING_SECONDS,
    FIX_HOLD_SETTLE_SECONDS,
    FixHold,
    fix_hold_ceiling_seconds,
    hold_review_dispatch,
)
from tests.domain.builders import Review

from tests.domain.test_review_bound_dispatch import binding


def _owed_state(*, kind=AgentKind.REVIEW, **kwargs):
    return BoundReviewState(owed={number: (kind, binding(number)) for number in (100, 101, 200, 815)}, **kwargs)


def _run(kind, number, claim=None):
    return RunAgent(kind, number, claim, workflow=binding(number))


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
    actions = decide_bound_review(world(prs=[p]), CFG, _owed_state())
    assert _run(AgentKind.REVIEW, 100) in actions


def test_re_review_when_sha_advances():
    p = pr(100, "ai-pr-open", head_ref_oid="def")
    st = _owed_state(kind=AgentKind.RE_REVIEW)
    actions = decide_bound_review(world(prs=[p]), CFG, st)
    assert _run(AgentKind.RE_REVIEW, 100) in actions


def test_solo_mode_disables_reviewer():
    cfg = ReviewConfig(review_bot="review-bot", identity_mode="solo")
    p = pr(100, "ai-pr-open", head_ref_oid="abc")
    assert decide_bound_review(world(prs=[p]), cfg, _owed_state()) == []


# ── configurable trigger label (external-repo reviewer) ───────────────────────
def test_custom_trigger_label_fires_on_ai_review():
    # An external-repo deployment triggers on `ai-review`, not `ai-pr-open`.
    cfg = ReviewConfig(review_bot="review-bot", trigger_label=Label.AI_REVIEW)
    p = pr(815, "ai-review", head_ref_oid="abc")
    assert _run(AgentKind.REVIEW, 815) in decide_bound_review(world(prs=[p]), cfg, _owed_state())


def test_custom_trigger_label_ignores_ai_pr_open():
    # With trigger=ai-review, a plain ai-pr-open PR is NOT picked up.
    cfg = ReviewConfig(review_bot="review-bot", trigger_label=Label.AI_REVIEW)
    p = pr(100, "ai-pr-open", head_ref_oid="abc")
    assert decide_bound_review(world(prs=[p]), cfg, _owed_state()) == []


def test_default_trigger_label_is_ai_pr_open():
    # Default unchanged: an ai-review PR is ignored by the stock reviewer.
    p = pr(815, "ai-review", head_ref_oid="abc")
    assert decide_bound_review(world(prs=[p]), CFG, _owed_state()) == []


# ── rogue-PR adoption ─────────────────────────────────────────────────────────
ADOPT = ReviewConfig(review_bot="review-bot", adopt_unlabeled=True)


def test_adopts_unlabeled_open_pr():
    # A PR with no trigger label → labelled with it AND reviewed the same poll,
    # so a comment lands on the first tick (not a poll later).
    p = pr(200, head_ref_oid="abc", author="human")
    acts = decide_bound_review(world(prs=[p]), ADOPT, _owed_state())
    assert Relabel(Target.PR, 200, None, Label.AI_PR_OPEN, "abc") in acts
    assert _run(AgentKind.REVIEW, 200) in acts


def test_adopt_off_by_default_leaves_rogue_pr_alone():
    p = pr(200, head_ref_oid="abc", author="human")
    assert decide_bound_review(world(prs=[p]), CFG, _owed_state()) == []  # CFG adopt=False


def test_adopt_skips_draft():
    p = pr(200, head_ref_oid="abc", author="human", is_draft=True)
    assert decide_bound_review(world(prs=[p]), ADOPT, _owed_state()) == []


def test_adopt_skips_review_bots_own_pr():
    p = pr(200, head_ref_oid="abc", author="review-bot")
    assert decide_bound_review(world(prs=[p]), ADOPT, _owed_state()) == []


def test_c1_adopt_skips_own_pr_when_config_carries_the_bot_suffix():
    # review C1: REVIEW_BOT='review-bot[bot]' (App form) vs the bare `review-bot`
    # gh reports as the PR author. A raw `==` would ADOPT the reviewer's own PR
    # and review it — the self-review loop the guard exists to prevent.
    cfg = ReviewConfig(review_bot="review-bot[bot]", adopt_unlabeled=True)
    p = pr(200, head_ref_oid="abc", author="review-bot")
    assert decide_bound_review(world(prs=[p]), cfg, _owed_state()) == []


def test_c1_adopt_skips_own_pr_when_author_carries_the_bot_suffix():
    # The asymmetry runs both ways: bare config, suffixed author.
    p = pr(200, head_ref_oid="abc", author="review-bot[bot]")
    assert decide_bound_review(world(prs=[p]), ADOPT, _owed_state()) == []


def test_c1_adopt_self_guard_is_case_insensitive():
    p = pr(200, head_ref_oid="abc", author="Review-Bot")
    assert decide_bound_review(world(prs=[p]), ADOPT, _owed_state()) == []


def test_c1_suffix_tolerance_still_adopts_a_strangers_pr():
    # Anti-vacuity twin: the self-guard must not swallow everyone — a genuinely
    # different author's rogue PR is still adopted and reviewed.
    cfg = ReviewConfig(review_bot="review-bot[bot]", adopt_unlabeled=True)
    p = pr(200, head_ref_oid="abc", author="some-human")
    acts = decide_bound_review(world(prs=[p]), cfg, _owed_state())
    assert Relabel(Target.PR, 200, None, Label.AI_PR_OPEN, "abc") in acts
    assert _run(AgentKind.REVIEW, 200) in acts


def test_adopt_skips_opt_out_label():
    p = pr(200, "no-ai-review", head_ref_oid="abc", author="human")
    assert decide_bound_review(world(prs=[p]), ADOPT, _owed_state()) == []


def test_adopt_skips_already_labelled_pr():
    # already carries the trigger label → normal review path, not re-adopted.
    p = pr(200, "ai-pr-open", head_ref_oid="abc", author="human")
    acts = decide_bound_review(world(prs=[p]), ADOPT, _owed_state())
    assert _run(AgentKind.REVIEW, 200) in acts
    assert not any(isinstance(a, Relabel) for a in acts)


def test_adopt_skips_parked_pr():
    # An operator park removes the trigger label to take the PR OUT of the
    # pipeline; adoption re-labelling it undid the park and let the budget arm
    # re-escalate (keifu PR #179, 2026-08-03). Parked PRs stay the operator's.
    p = pr(200, "ai-parked", head_ref_oid="abc", author="human")
    assert decide_bound_review(world(prs=[p]), ADOPT, _owed_state()) == []


def test_adopt_skips_closed_pr():
    p = pr(200, head_ref_oid="abc", author="human", state="MERGED")
    assert decide_bound_review(world(prs=[p]), ADOPT, _owed_state()) == []


# ── unproductive-round ceiling (review P0R-3) ─────────────────────────────────
# The reviewer used to have NO ceiling of any kind: one RunAgent per eligible PR
# per poll, no `consumed` gate, no breaker read. A PR whose review never
# persists state (session dies pre-result, agent errors, MULTI fan-out
# synthesises nothing) was re-dispatched at full cost every poll, forever.
CAPPED = ReviewConfig(review_bot="review-bot", max_unproductive_rounds=3)


def _state(pr_number, rounds, **kw):
    return _owed_state(unproductive_rounds={pr_number: rounds}, **kw)


def test_below_the_ceiling_the_review_still_dispatches():
    """Anti-vacuity FIRST: a ceiling that fires early is a reviewer that never
    reviews. Two dead rounds out of three still buys the third."""
    p = pr(100, "ai-pr-open", head_ref_oid="abc")
    acts = decide_bound_review(world(prs=[p]), CAPPED, _state(100, 2))
    assert acts == [_run(AgentKind.REVIEW, 100)]


def test_at_the_ceiling_the_dispatch_is_withheld_and_the_pr_escalates():
    p = pr(100, "ai-pr-open", head_ref_oid="abc")
    acts = decide_bound_review(world(prs=[p]), CAPPED, _state(100, 3))
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
    st = _owed_state(kind=AgentKind.RE_REVIEW,
                     unproductive_rounds={100: 3})
    acts = decide_bound_review(world(prs=[p]), CAPPED, st)
    assert not any(isinstance(a, RunAgent) for a in acts)
    assert Relabel(Target.PR, 100, Label.AI_PR_OPEN, Label.NEEDS_HUMAN_REVIEW) in acts


def test_escalation_is_edge_triggered_on_the_needs_human_review_label():
    """P1R-2's shape, pre-empted: a PR already handed to a human emits NOTHING.
    Without this a stuck relabel converts one paid loop into a comment loop —
    one 'Circuit breaker' comment + one ALERT_CMD run per poll, ~4,300/day."""
    p = pr(100, "ai-pr-open", "needs-human-review", head_ref_oid="abc")
    assert decide_bound_review(world(prs=[p]), CAPPED, _state(100, 3)) == []


def test_a_ceiling_of_zero_disables_the_breaker():
    """cap<=0 disables, like every other breaker in the harness."""
    cfg = ReviewConfig(review_bot="review-bot", max_unproductive_rounds=0)
    p = pr(100, "ai-pr-open", head_ref_oid="abc")
    assert decide_bound_review(world(prs=[p]), cfg, _state(100, 99)) == [
        _run(AgentKind.REVIEW, 100)
    ]


def test_the_ceiling_is_per_pr_and_does_not_leak_across_prs():
    a = pr(100, "ai-pr-open", head_ref_oid="abc")
    b = pr(101, "ai-pr-open", head_ref_oid="abc")
    acts = decide_bound_review(world(prs=[a, b]), CAPPED, _state(100, 3))
    assert _run(AgentKind.REVIEW, 101) in acts
    assert _run(AgentKind.REVIEW, 100) not in acts


def test_default_ceiling_is_armed_without_any_configuration():
    """The default MUST be a finite number. A ReviewConfig built with no ceiling
    argument is what every existing caller constructs, and 'unset' meaning
    'unbounded' is exactly the state P0R-3 describes."""
    assert ReviewConfig(review_bot="r").max_unproductive_rounds == 5
    p = pr(100, "ai-pr-open", head_ref_oid="abc")
    st = _state(100, 5)
    assert not any(
        isinstance(a, RunAgent)
        for a in decide_bound_review(world(prs=[p]), ReviewConfig(review_bot="r"), st)
    )


def test_adoption_is_a_second_entrance_and_is_bounded_too():
    """A rogue PR is adopted AND reviewed the same tick, so the adoption arm
    needs the same guard — otherwise the ceiling just moves the loop."""
    p = pr(200, head_ref_oid="abc", author="human")
    assert decide_bound_review(world(prs=[p]), replace(ADOPT, max_unproductive_rounds=3),
                         _state(200, 3)) == []
    # anti-vacuity: below the cap the rogue PR is still adopted and reviewed.
    acts = decide_bound_review(world(prs=[p]), replace(ADOPT, max_unproductive_rounds=3),
                         _state(200, 2))
    assert Relabel(Target.PR, 200, None, Label.AI_PR_OPEN, "abc") in acts
    assert _run(AgentKind.REVIEW, 200) in acts


def test_an_escalated_pr_is_not_re_adopted_into_the_loop():
    """The escalation swaps the trigger label for needs-human-review, which
    drops the PR out of `prs_with(trigger_label)` — straight into the adoption
    sweep. Without the needs-human-review exclusion, an adopting deployment
    re-labels and re-dispatches the escalated PR on the very next poll and the
    ceiling buys nothing at all."""
    escalated = pr(200, "needs-human-review", head_ref_oid="abc", author="human")
    assert decide_bound_review(world(prs=[escalated]), ADOPT, _owed_state()) == []


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
def test_submission_approve_wins_even_at_ceiling():
    # A rebase/housekeeping push that ends in APPROVE never escalates, whatever the
    # prior rejection count.
    sub = review_submission(
        "APPROVE", "lgtm", prior_rejections=3, max_rejections=4,
    )
    assert sub.event == "APPROVE" and sub.escalate is False


















def test_submission_zero_disables_ceiling_for_ordinary_rejection():
    sub = review_submission(
        "REQUEST_CHANGES",
        "still wrong",
        prior_rejections=10_000,
        max_rejections=0,
    )

    assert sub.event == "REQUEST_CHANGES"
    assert sub.escalate is False




def test_submission_request_changes_below_ceiling_bounces_back():
    # 3rd rejection (2 prior) with a cap of 4 → still bounces back to the fixer.
    sub = review_submission(
        "REQUEST_CHANGES", "fix x", prior_rejections=2, max_rejections=4,
    )
    assert sub.event == "REQUEST_CHANGES" and sub.escalate is False


def test_submission_request_changes_at_ceiling_escalates():
    # The breaker: the 4th rejection (3 prior) can no longer bounce the PR back —
    # it escalates to a human regardless of the model verdict.
    sub = review_submission(
        "REQUEST_CHANGES", "still wrong", prior_rejections=3, max_rejections=4,
    )
    assert sub.event == "COMMENT" and sub.escalate is True
    assert sub.escalate_cause == "ceiling"


def test_submission_explicit_escalate_always_escalates():
    sub = review_submission(
        "ESCALATE", "deadlocked", prior_rejections=0, max_rejections=4,
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
    )
    assert sub.escalate is True and sub.escalate_cause == "verdict"


def test_submission_non_escalating_paths_carry_no_cause():
    ok = review_submission(
        "APPROVE", "lgtm", prior_rejections=0, max_rejections=4,
    )
    bounce = review_submission(
        "REQUEST_CHANGES", "fix", prior_rejections=0, max_rejections=4,
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
    )
    assert sub.event == "APPROVE" and sub.escalate is False


def test_synthesis_empty_approves():
    sub = synthesize_review(
        [], prior_rejections=0, max_rejections=4,
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
    )
    assert sub.event == "REQUEST_CHANGES"
    assert "x.py:9" in sub.body and "Must fix" in sub.body
    assert "SQL injection" in sub.body


def test_synthesis_only_advisory_confirmed_approves_with_notes():
    fs = [Finding("architecture", "z.py:4", "consider extracting", "advisory", confirmed=True)]
    sub = synthesize_review(
        fs, prior_rejections=0, max_rejections=4,
    )
    assert sub.event == "APPROVE"
    assert "z.py:4" in sub.body and "Non-blocking" in sub.body


def test_synthesis_blocking_at_ceiling_escalates():
    # The rejection ceiling still bites: a confirmed blocking finding that would be
    # the 4th rejection (3 prior) can no longer bounce the PR back — it escalates.
    fs = [Finding("correctness", "a.py:1", "real bug", "blocking", confirmed=True)]
    sub = synthesize_review(
        fs, prior_rejections=3, max_rejections=4,
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
    )
    assert sub.event == "APPROVE"


# ── completeness guard: an incomplete review must not silently APPROVE ─────────
def test_synthesis_incomplete_and_no_finding_returns_none():
    # A dimension/validator leg errored (complete=False) and nothing blocking
    # survived → return None so the caller submits NOTHING and retries, rather than
    # approving a PR whose defect-detecting lens may never have run.
    assert synthesize_review(
        [], prior_rejections=0, max_rejections=4, complete=False,
    ) is None


def test_synthesis_incomplete_but_blocking_still_reports():
    # A confirmed blocking finding is reported even when incomplete — a real bug
    # doesn't wait on the flaky leg.
    fs = [Finding("security", "a.py:1", "real", "blocking", confirmed=True)]
    sub = synthesize_review(
        fs, prior_rejections=0, max_rejections=4, complete=False,
    )
    assert sub is not None and sub.event == "REQUEST_CHANGES"


def test_synthesis_complete_no_finding_approves():
    # The default (complete=True) still approves on a clean review.
    sub = synthesize_review(
        [], prior_rejections=0, max_rejections=4, complete=True,
    )
    assert sub is not None and sub.event == "APPROVE"


def test_gate_submission_approves():
    sub = gate_submission(
        prior_rejections=0,
        max_rejections=4,
    )
    assert sub.event == "APPROVE" and sub.escalate is False
    assert "docs/formatting/config" in sub.body




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


# ── review dispatch defers while a fix leg holds the same PR (#176) ───────────
_CEILING = fix_hold_ceiling_seconds(2700.0)


def _held(pr_number: int, seconds: float, claim: str = "claim-1") -> FixHold:
    return FixHold(pr_number, claim, seconds)


def test_the_ceiling_sits_above_the_longest_leg_the_harness_permits():
    """A ceiling below the fixer's own total-runtime cap fires on healthy legs,
    which recreates the race this exists to prevent."""
    assert fix_hold_ceiling_seconds(2700.0) == 2700.0 + FIX_HOLD_SETTLE_SECONDS
    assert fix_hold_ceiling_seconds(2700.0) > 2700.0


def test_a_disabled_runtime_cap_falls_back_to_a_declared_ceiling():
    assert fix_hold_ceiling_seconds(0.0) == FIX_HOLD_CEILING_SECONDS
    assert fix_hold_ceiling_seconds(-1.0) == FIX_HOLD_CEILING_SECONDS


def test_review_dispatch_is_withheld_while_a_fix_leg_holds_the_pr():
    p = pr(100, "ai-pr-open", head_ref_oid="abc")
    actions = decide_bound_review(world(prs=[p]), CFG, _owed_state())
    plan = hold_review_dispatch(actions, {100: _held(100, 30.0)})
    assert plan.actions == ()
    assert plan.deferred == (_held(100, 30.0),)
    assert plan.forced == ()


def test_a_finished_fix_leg_releases_the_review_on_the_next_tick():
    p = pr(100, "ai-pr-open", head_ref_oid="abc")
    actions = decide_bound_review(world(prs=[p]), CFG, _owed_state())
    assert hold_review_dispatch(actions, {}).actions == tuple(actions)


def test_a_re_review_is_withheld_by_the_same_hold():
    p = pr(100, "ai-pr-open", head_ref_oid="def")
    st = _owed_state(kind=AgentKind.RE_REVIEW)
    actions = decide_bound_review(world(prs=[p]), CFG, st)
    assert hold_review_dispatch(actions, {100: _held(100, 5.0)}).actions == ()


def test_a_hold_past_the_ceiling_dispatches_anyway_and_is_reported():
    p = pr(100, "ai-pr-open", head_ref_oid="abc")
    actions = decide_bound_review(world(prs=[p]), CFG, _owed_state())
    stuck = _held(100, _CEILING)
    plan = hold_review_dispatch(actions, {100: stuck}, ceiling_seconds=_CEILING)
    assert _run(AgentKind.REVIEW, 100) in plan.actions
    assert plan.forced == (stuck,)
    assert plan.deferred == ()


def test_a_hold_never_defers_another_prs_review_in_the_same_tick():
    held = pr(100, "ai-pr-open", head_ref_oid="abc")
    free = pr(101, "ai-pr-open", head_ref_oid="abc")
    actions = decide_bound_review(world(prs=[held, free]), CFG, _owed_state())
    plan = hold_review_dispatch(actions, {100: _held(100, 30.0)})
    assert plan.actions == (_run(AgentKind.REVIEW, 101),)


def test_a_deferred_adoption_withholds_its_claim_label_too():
    # Labelling without reviewing would mark the PR in-pipeline a tick before
    # anyone looks at it; the whole adoption waits for the fix leg instead.
    p = pr(200, head_ref_oid="abc", author="human")
    actions = decide_bound_review(world(prs=[p]), ADOPT, _owed_state())
    assert hold_review_dispatch(actions, {200: _held(200, 1.0)}).actions == ()


def test_a_hold_does_not_withhold_a_non_dispatch_action():
    escalations = [Relabel(Target.PR, 9, Label.AI_PR_OPEN, Label.NEEDS_HUMAN_REVIEW)]
    plan = hold_review_dispatch(escalations, {9: _held(9, 1.0)})
    assert escalations and plan.actions == tuple(escalations)
    assert plan.deferred == ()


@pytest.mark.parametrize("body", ["", "Looks good", "gate-exemption: granted rule=tests-immutable issue=42 subject=tests/test_x.py patch=old mac=old"])
def test_approval_needs_no_test_grant_and_preserves_legacy_body_text(body):
    submitted = review_submission("APPROVE", body, 1000, 4)
    assert submitted.event == "APPROVE"
    assert submitted.body == body
    assert not submitted.escalate
