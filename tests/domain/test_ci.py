"""CI-state precedence + distinct-actor review — the watcher-lib lessons."""

from tests.domain.builders import Check, Review

from issue_agent.domain.ci import (
    ci_state,
    latest_other_review,
    pending_age_seconds,
    pending_checks,
    pending_deadline_breached,
)
from issue_agent.domain.world import CIState


def test_no_checks_is_none():
    assert ci_state([]) is CIState.NONE


def test_all_pass_is_ok():
    assert ci_state([Check("build", bucket="pass")]) is CIState.OK


def test_any_fail_is_failure():
    assert ci_state([Check("a", bucket="pass"), Check("b", bucket="fail")]) is CIState.FAILURE


def test_failure_from_a_successful_workflow_run_is_advisory():
    checks = [
        Check("build", bucket="pass"),
        Check("Security Audit", bucket="fail", advisory=True),
    ]
    assert ci_state(checks) is CIState.OK


def test_advisory_is_not_inferred_without_a_successful_owning_run():
    assert ci_state([Check("Security Audit", bucket="fail")]) is CIState.FAILURE


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


# ── review C1: the self-review guard survives the `[bot]` suffix asymmetry ─────
def test_c1_suffixed_fix_bot_still_excludes_its_own_bare_login_review():
    # FIX_BOT='fix-bot[bot]' (App form) vs the bare `fix-bot` gh reports in the
    # review author field. Under a raw `==` the fixer's OWN approval survived the
    # filter and read as an independent one — the 422 self-approval shape.
    reviews = [Review("APPROVED", "fix-bot", "2026-01-01T00:00:00Z")]
    assert latest_other_review(reviews, "fix-bot[bot]") is None


def test_c1_bare_fix_bot_still_excludes_its_own_suffixed_login_review():
    # The asymmetry runs both ways.
    reviews = [Review("APPROVED", "fix-bot[bot]", "2026-01-01T00:00:00Z")]
    assert latest_other_review(reviews, "fix-bot") is None


def test_c1_suffix_tolerance_does_not_swallow_a_genuine_reviewer():
    # Anti-vacuity twin: a genuinely distinct actor is still returned, whichever
    # form either side is written in.
    reviews = [
        Review("APPROVED", "fix-bot", "2026-01-01T00:00:00Z"),        # self
        Review("APPROVED", "review-bot", "2026-01-02T00:00:00Z"),     # distinct
    ]
    latest = latest_other_review(reviews, "fix-bot[bot]")
    assert latest is not None and latest.author == "review-bot"


def test_c1_login_comparison_is_case_insensitive():
    reviews = [Review("APPROVED", "Fix-Bot", "2026-01-01T00:00:00Z")]
    assert latest_other_review(reviews, "fix-bot[bot]") is None


# ── pending deadline (#30) ───────────────────────────────────────────────────
# A check stuck IN_PROGRESS pins ci_state at PENDING, and PENDING outranks
# FAILURE, so nothing downstream ever acts. These bound the wait without
# touching that precedence.

_T0 = "2026-07-28T12:00:00Z"


def _later(seconds: int) -> str:
    from datetime import datetime, timedelta, timezone

    return (
        datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)
        + timedelta(seconds=seconds)
    ).isoformat()


def test_pending_past_the_deadline_breaches():
    checks = [Check("windows", state="IN_PROGRESS", started_at=_T0)]
    assert pending_deadline_breached(checks, _later(3601), 3600)


def test_pending_under_the_deadline_does_not_breach():
    checks = [Check("windows", state="IN_PROGRESS", started_at=_T0)]
    assert not pending_deadline_breached(checks, _later(1380), 3600)


def test_exactly_at_the_deadline_breaches():
    # `>=`, so a deadline of N fires at N — no one-second sliver where a
    # configured bound is silently not yet in force.
    checks = [Check("windows", state="IN_PROGRESS", started_at=_T0)]
    assert pending_deadline_breached(checks, _later(3600), 3600)


def test_zero_deadline_disables():
    checks = [Check("windows", state="IN_PROGRESS", started_at=_T0)]
    assert not pending_deadline_breached(checks, _later(10**6), 0)


def test_negative_deadline_disables():
    checks = [Check("windows", state="IN_PROGRESS", started_at=_T0)]
    assert not pending_deadline_breached(checks, _later(10**6), -1)


def test_missing_started_at_never_breaches():
    # THE fail-safe: an unknown start must read as unknown, never as epoch —
    # otherwise every pending check on a gh without the field escalates at once.
    checks = [Check("windows", state="IN_PROGRESS")]
    assert not pending_deadline_breached(checks, _later(10**6), 3600)


def test_malformed_started_at_never_breaches():
    for raw in ("not-a-date", "2026-13-45T99:99:99Z", "0", "   "):
        checks = [Check("windows", state="IN_PROGRESS", started_at=raw)]
        assert not pending_deadline_breached(checks, _later(10**6), 3600), raw


def test_unstamped_now_never_breaches():
    checks = [Check("windows", state="IN_PROGRESS", started_at=_T0)]
    assert not pending_deadline_breached(checks, "", 3600)
    assert not pending_deadline_breached(checks, "whenever", 3600)


def test_settled_checks_never_breach_however_old():
    checks = [Check("unit", bucket="pass", started_at=_T0),
              Check("lint", bucket="fail", started_at=_T0)]
    assert not pending_deadline_breached(checks, _later(10**6), 3600)


def test_newest_pending_start_is_what_counts():
    # A long pipeline that keeps STARTING new jobs is making progress: the newest
    # start is the clock, so a fresh job inside the window holds the deadline off.
    checks = [
        Check("old", state="IN_PROGRESS", started_at=_T0),
        Check("just-started", state="QUEUED", started_at=_later(3500)),
    ]
    assert not pending_deadline_breached(checks, _later(3600), 3600)
    assert pending_deadline_breached(checks, _later(7101), 3600)


def test_a_settled_recent_check_does_not_hold_off_the_deadline():
    # Anti-vacuity twin of the above: only PENDING starts count. A quick job that
    # finished just now must not mask a sibling wedged for hours.
    checks = [
        Check("wedged", state="IN_PROGRESS", started_at=_T0),
        Check("fast", bucket="pass", started_at=_later(3599)),
    ]
    assert pending_deadline_breached(checks, _later(3601), 3600)


def test_ignored_checks_are_excluded_from_the_deadline():
    # An operator-ignored check cannot pin ci_state, so it must not escalate
    # either — the two filters have to agree.
    checks = [Check("Format Backend", state="QUEUED", started_at=_T0)]
    assert not pending_deadline_breached(
        checks, _later(10**6), 3600, ignore_regex="Format Backend"
    )
    assert ci_state(checks, ignore_regex="Format Backend") is CIState.NONE


def test_deadline_ignores_a_start_in_the_future():
    # Clock skew between GitHub and this host must not escalate instantly.
    checks = [Check("windows", state="IN_PROGRESS", started_at=_later(10**6))]
    assert not pending_deadline_breached(checks, _T0, 3600)


def test_naive_timestamps_are_read_as_utc():
    checks = [Check("windows", state="IN_PROGRESS", started_at="2026-07-28T12:00:00")]
    assert pending_deadline_breached(checks, "2026-07-28T13:30:00", 3600)


def test_pending_checks_names_the_pinning_checks():
    checks = [
        Check("unit", bucket="pass"),
        Check("windows", state="IN_PROGRESS"),
        Check("mac", bucket="pending"),
    ]
    assert [c.name for c in pending_checks(checks)] == ["windows", "mac"]


def test_pending_age_is_none_when_nothing_is_pending():
    assert pending_age_seconds([Check("unit", bucket="pass")], _later(10)) is None


def test_the_deadline_does_not_change_ci_state_precedence():
    # The guardrail on this whole feature: pending still OUTRANKS failure, breach
    # or no breach. The deadline adds a bound; it does not reorder anything.
    checks = [Check("unit", bucket="fail", started_at=_T0),
              Check("e2e", state="IN_PROGRESS", started_at=_T0)]
    assert pending_deadline_breached(checks, _later(10**6), 3600)
    assert ci_state(checks) is CIState.PENDING


# ── #48: an unread checks fetch is UNKNOWN, never an answer ───────────────────
def test_unread_checks_are_unknown_not_none():
    # checks=() from a FAILED `gh pr checks` is not "no checks configured".
    assert ci_state([], known=False) is CIState.UNKNOWN


def test_unread_outranks_everything_even_green():
    # Whatever stale tuple rode along, an unread fetch is not an observation.
    assert ci_state([Check("unit", bucket="pass")], known=False) is CIState.UNKNOWN
    assert ci_state([Check("unit", bucket="fail")], known=False) is CIState.UNKNOWN


def test_honest_empty_read_stays_none():
    # The no-CI-repo path must not regress: a SUCCESSFUL read of zero checks
    # is still NONE (mergeable — nothing to wait on).
    assert ci_state([], known=True) is CIState.NONE
