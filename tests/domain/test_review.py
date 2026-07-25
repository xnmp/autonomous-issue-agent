"""Review-watcher decision behaviour."""

from builders import pr, world

from issue_agent.domain.actions import AgentKind, Relabel, RunAgent, Target
from issue_agent.domain.labels import Label
from issue_agent.domain.review import (
    Finding,
    ReviewConfig,
    ReviewMode,
    ReviewState,
    count_prior_rejections,
    decide_review,
    gate_submission,
    needs_initial_review,
    needs_re_review,
    parse_findings,
    review_submission,
    synthesize_review,
)
from builders import Review

CFG = ReviewConfig(review_bot="review-bot")


# ── ReviewMode resolution (REVIEW_MODE + legacy REVIEW_MULTI_AGENT alias) ──
def test_review_mode_default_is_single():
    assert ReviewMode.from_env("", False) is ReviewMode.SINGLE


def test_review_mode_explicit_wins():
    assert ReviewMode.from_env("delegated", False) is ReviewMode.DELEGATED
    assert ReviewMode.from_env("multi", False) is ReviewMode.MULTI
    assert ReviewMode.from_env("single", True) is ReviewMode.SINGLE  # explicit beats alias


def test_review_mode_multi_agent_alias():
    # legacy REVIEW_MULTI_AGENT=1 with no REVIEW_MODE → MULTI
    assert ReviewMode.from_env("", True) is ReviewMode.MULTI


def test_review_mode_unknown_falls_back_to_single():
    # fail-safe: a typo'd mode never silently disables review
    assert ReviewMode.from_env("delegted", False) is ReviewMode.SINGLE


def test_review_mode_case_insensitive():
    assert ReviewMode.from_env("DELEGATED", False) is ReviewMode.DELEGATED


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
    assert Relabel(Target.PR, 200, None, Label.AI_PR_OPEN) in acts
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
    assert Relabel(Target.PR, 200, None, Label.AI_PR_OPEN) in acts
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


def test_adopt_skips_closed_pr():
    p = pr(200, head_ref_oid="abc", author="human", state="MERGED")
    assert decide_review(world(prs=[p]), ADOPT, ReviewState()) == []


def test_needs_helpers():
    p = pr(100, "ai-pr-open", head_ref_oid="abc")
    assert needs_initial_review(p, ReviewState()) is True
    st = ReviewState(sessions=frozenset({100}), last_sha={100: "abc"})
    assert needs_initial_review(p, st) is False
    assert needs_re_review(p, st) is False


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


# ── review_submission: verdict → effect, with the rejection ceiling ───────────
# The verdict under test is the (prior_rejections + 1)-th rejection; cap here is 4.
def test_submission_approve_wins_even_at_ceiling():
    # A rebase/housekeeping push that ends in APPROVE never escalates, whatever the
    # prior rejection count.
    sub = review_submission("APPROVE", "lgtm", prior_rejections=3, max_rejections=4)
    assert sub.event == "APPROVE" and sub.escalate is False


def test_submission_request_changes_below_ceiling_bounces_back():
    # 3rd rejection (2 prior) with a cap of 4 → still bounces back to the fixer.
    sub = review_submission("REQUEST_CHANGES", "fix x", prior_rejections=2, max_rejections=4)
    assert sub.event == "REQUEST_CHANGES" and sub.escalate is False


def test_submission_request_changes_at_ceiling_escalates():
    # The breaker: the 4th rejection (3 prior) can no longer bounce the PR back —
    # it escalates to a human regardless of the model verdict.
    sub = review_submission("REQUEST_CHANGES", "still wrong", prior_rejections=3, max_rejections=4)
    assert sub.event == "COMMENT" and sub.escalate is True


def test_submission_explicit_escalate_always_escalates():
    sub = review_submission("ESCALATE", "deadlocked", prior_rejections=0, max_rejections=4)
    assert sub.escalate is True


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
    sub = synthesize_review(fs, prior_rejections=0, max_rejections=4)
    assert sub.event == "APPROVE" and sub.escalate is False


def test_synthesis_empty_approves():
    sub = synthesize_review([], prior_rejections=0, max_rejections=4)
    assert sub.event == "APPROVE"
    assert "LGTM" in sub.body


def test_synthesis_confirmed_blocking_requests_changes():
    fs = [
        Finding("security", "x.py:9", "SQL injection", "blocking", confirmed=True,
                verdict="user input concatenated"),
        Finding("correctness", "y.py:2", "nit", "advisory", confirmed=True),
    ]
    sub = synthesize_review(fs, prior_rejections=1, max_rejections=4)
    assert sub.event == "REQUEST_CHANGES"
    assert "x.py:9" in sub.body and "Must fix" in sub.body
    assert "SQL injection" in sub.body


def test_synthesis_only_advisory_confirmed_approves_with_notes():
    fs = [Finding("architecture", "z.py:4", "consider extracting", "advisory", confirmed=True)]
    sub = synthesize_review(fs, prior_rejections=0, max_rejections=4)
    assert sub.event == "APPROVE"
    assert "z.py:4" in sub.body and "Non-blocking" in sub.body


def test_synthesis_blocking_at_ceiling_escalates():
    # The rejection ceiling still bites: a confirmed blocking finding that would be
    # the 4th rejection (3 prior) can no longer bounce the PR back — it escalates.
    fs = [Finding("correctness", "a.py:1", "real bug", "blocking", confirmed=True)]
    sub = synthesize_review(fs, prior_rejections=3, max_rejections=4)
    assert sub.event == "COMMENT" and sub.escalate is True


def test_synthesis_unconfirmed_blocking_ignored():
    # A refuted (confirmed=False) blocking candidate must NOT drive REQUEST_CHANGES —
    # the false-positive killer working end to end.
    fs = [
        Finding("security", "a.py:1", "false alarm", "blocking", confirmed=False),
        Finding("correctness", "b.py:2", "real advisory", "advisory", confirmed=True),
    ]
    sub = synthesize_review(fs, prior_rejections=0, max_rejections=4)
    assert sub.event == "APPROVE"


# ── completeness guard: an incomplete review must not silently APPROVE ─────────
def test_synthesis_incomplete_and_no_finding_returns_none():
    # A dimension/validator leg errored (complete=False) and nothing blocking
    # survived → return None so the caller submits NOTHING and retries, rather than
    # approving a PR whose defect-detecting lens may never have run.
    assert synthesize_review([], prior_rejections=0, max_rejections=4, complete=False) is None


def test_synthesis_incomplete_but_blocking_still_reports():
    # A confirmed blocking finding is reported even when incomplete — a real bug
    # doesn't wait on the flaky leg.
    fs = [Finding("security", "a.py:1", "real", "blocking", confirmed=True)]
    sub = synthesize_review(fs, prior_rejections=0, max_rejections=4, complete=False)
    assert sub is not None and sub.event == "REQUEST_CHANGES"


def test_synthesis_complete_no_finding_approves():
    # The default (complete=True) still approves on a clean review.
    sub = synthesize_review([], prior_rejections=0, max_rejections=4, complete=True)
    assert sub is not None and sub.event == "APPROVE"


def test_gate_submission_approves():
    sub = gate_submission(prior_rejections=0, max_rejections=4)
    assert sub.event == "APPROVE" and sub.escalate is False
    assert "docs/formatting/config" in sub.body
