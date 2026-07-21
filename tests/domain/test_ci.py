"""CI-state precedence + distinct-actor review — the watcher-lib lessons."""

from builders import Check, Review

from issue_agent.domain.ci import ci_state, latest_other_review
from issue_agent.domain.world import CIState


def test_no_checks_is_none():
    assert ci_state([]) is CIState.NONE


def test_all_pass_is_ok():
    assert ci_state([Check("build", bucket="pass")]) is CIState.OK


def test_any_fail_is_failure():
    assert ci_state([Check("a", bucket="pass"), Check("b", bucket="fail")]) is CIState.FAILURE


def test_pending_beats_failure():
    # THE lesson (#49): a failed check + an in-flight check is NOT yet a failure.
    checks = [Check("unit", bucket="fail"), Check("e2e", state="IN_PROGRESS")]
    assert ci_state(checks) is CIState.PENDING


def test_queued_is_pending():
    assert ci_state([Check("x", state="QUEUED")]) is CIState.PENDING


def test_ignore_regex_drops_stuck_check():
    # A check stuck QUEUED forever on a starved runner must not pin us at pending.
    checks = [Check("Format Backend", state="QUEUED"), Check("unit", bucket="pass")]
    assert ci_state(checks, ignore_regex="Format Backend") is CIState.OK


def test_ignore_all_checks_yields_none():
    checks = [Check("Format Backend", state="QUEUED")]
    assert ci_state(checks, ignore_regex="Format Backend") is CIState.NONE


def test_real_failure_on_non_ignored_check_still_counts():
    checks = [Check("Format Backend", state="QUEUED"), Check("unit", bucket="fail")]
    # The stuck check is ignored, but the real failure on `unit` still settles.
    assert ci_state(checks, ignore_regex="Format Backend") is CIState.FAILURE


def test_latest_other_review_excludes_fixer():
    reviews = [
        Review("APPROVED", "fix-bot", "2026-01-01T00:00:00Z"),
    ]
    assert latest_other_review(reviews, "fix-bot") is None


def test_latest_other_review_picks_newest_distinct_actor():
    reviews = [
        Review("CHANGES_REQUESTED", "review-bot", "2026-01-01T00:00:00Z"),
        Review("APPROVED", "review-bot", "2026-01-02T00:00:00Z"),
        Review("APPROVED", "fix-bot", "2026-01-03T00:00:00Z"),  # self — ignored
    ]
    latest = latest_other_review(reviews, "fix-bot")
    assert latest is not None and latest.state == "APPROVED"
    assert latest.submitted_at == "2026-01-02T00:00:00Z"
