"""Stall watchdog behaviour: no watched target is silently stuck forever."""

from tests.domain.builders import Check, Review, issue, pr, world

from issue_agent.domain.liveness import (
    STALL_THRESHOLD_SECONDS,
    detect_stalls,
    pr_signature,
)


def _world(minutes: int, **kw):
    return world(now=f"2026-08-01T10:{minutes:02d}:00Z", **kw)


def _open_pr():
    return pr(
        100, "ai-pr-open",
        head_ref_oid="head-1",
        reviews=(Review("APPROVED", "bot", "t1", commit_id="head-1"),),
        checks=(Check("Security Audit", bucket="fail"),),
        mergeable="MERGEABLE", merge_state="UNSTABLE",
    )


def test_a_fresh_target_is_tracked_not_stalled():
    stalls, seen = detect_stalls(_world(0, prs=[_open_pr()]), {})
    assert stalls == ()
    assert seen == {"pr:100": (pr_signature(_open_pr()), "2026-08-01T10:00:00Z")}


def test_an_unchanged_target_past_the_threshold_stalls():
    """The #166 shape: an APPROVED advisory-UNSTABLE PR nothing was moving.
    Whatever the (possibly undiscovered) cause, the boundary measurement
    fires."""
    p = _open_pr()
    seen = {"pr:100": (pr_signature(p), "2026-08-01T10:00:00Z")}
    stalls, _ = detect_stalls(
        _world(35, prs=[p]), seen, threshold_seconds=STALL_THRESHOLD_SECONDS
    )
    assert len(stalls) == 1
    assert (stalls[0].kind, stalls[0].number, stalls[0].level) == ("pr", 100, 1)


def test_the_level_escalates_with_age():
    p = _open_pr()
    seen = {"pr:100": (pr_signature(p), "2026-08-01T09:00:00Z")}
    stalls, _ = detect_stalls(_world(31, prs=[p]), seen)
    assert stalls[0].level == 3  # 91 min = three whole 30-min thresholds


def test_a_pr_open_issue_does_not_stall_while_its_pr_moves():
    # #185 (tauri #482): the issue's own labels/updated_at froze for 3 hours
    # while its PR churned through pushes, verdicts and CI — six stall levels
    # of noise on a healthy issue. PR movement must reset the issue's clock.
    # Linkage here is the BRANCH-NAME fallback (closing_issue=None): GitHub's
    # closing-reference graph is default-branch-only, so on a repo whose base
    # is not the default (the self-hosting repo) it is the only linkage that
    # exists — keying on closing_issue alone made this fix a silent no-op
    # exactly there (#185 review F1).
    from dataclasses import replace

    i = issue(482, "ai-pr-open", updated_at="2026-08-01T08:00:00Z")
    p = replace(_open_pr(), head_ref_name="fix/482-backoff", closing_issue=None)
    _, seen = detect_stalls(_world(0, issues=[i], prs=[p]), {})
    moved = replace(p, head_ref_oid="head-2")
    stalls, _ = detect_stalls(_world(35, issues=[i], prs=[moved]), seen)
    assert [s for s in stalls if s.kind == "issue"] == []


def test_a_contradictory_label_pair_keeps_its_own_stall_clock():
    # The #482 stall signal was the ONLY detector of the ai-ready+ai-pr-open
    # contradiction — PR churn must not silence an issue that wears any watched
    # label BESIDES ai-pr-open, or the class this fold's own change set repairs
    # becomes invisible (#185 review F5).
    from dataclasses import replace

    i = issue(482, "ai-ready", "ai-pr-open", updated_at="2026-08-01T08:00:00Z")
    p = replace(_open_pr(), closing_issue=482)
    _, seen = detect_stalls(_world(0, issues=[i], prs=[p]), {})
    moved = replace(p, head_ref_oid="head-2")
    stalls, _ = detect_stalls(_world(35, issues=[i], prs=[moved]), seen)
    assert [s.number for s in stalls if s.kind == "issue"] == [482]


def test_a_pr_open_issue_with_a_quiet_pr_still_stalls():
    # Anti-vacuity twin: delegation is to PR PROGRESS, not to PR existence —
    # both quiet past the threshold is exactly "nothing is moving it".
    from dataclasses import replace

    i = issue(482, "ai-pr-open", updated_at="2026-08-01T08:00:00Z")
    p = replace(_open_pr(), closing_issue=482)
    _, seen = detect_stalls(_world(0, issues=[i], prs=[p]), {})
    stalls, _ = detect_stalls(_world(35, issues=[i], prs=[p]), seen)
    assert [s.number for s in stalls if s.kind == "issue"] == [482]


def test_any_observable_change_resets_the_clock():
    from dataclasses import replace

    p = _open_pr()
    seen = {"pr:100": (pr_signature(p), "2026-08-01T09:00:00Z")}
    moved = replace(p, head_ref_oid="head-2")
    stalls, next_seen = detect_stalls(_world(31, prs=[moved]), seen)
    assert stalls == ()
    assert next_seen["pr:100"] == (pr_signature(moved), "2026-08-01T10:31:00Z")


def test_an_issue_comment_counts_as_progress():
    """An ai-blocked issue waiting on a human is only stalled while nobody
    has replied — updated_at moves on comments and resets the clock."""
    i = issue(7, "ai-blocked", updated_at="2026-08-01T09:00:00Z")
    from issue_agent.domain.liveness import issue_signature

    seen = {"issue:7": (issue_signature(i), "2026-08-01T09:00:00Z")}
    from dataclasses import replace

    replied = replace(i, updated_at="2026-08-01T10:25:00Z")
    stalls, _ = detect_stalls(_world(31, issues=[replied]), seen)
    assert stalls == ()


def test_unwatched_targets_are_ignored():
    from dataclasses import replace

    from issue_agent.domain.world import IssueView

    closed = replace(_open_pr(), state="MERGED")
    unlabeled = IssueView(9, state="open")
    stalls, seen = detect_stalls(
        _world(0, prs=[closed], issues=[unlabeled]), {}
    )
    assert stalls == () and seen == {}


def test_an_unstamped_world_never_ages_anything():
    """A missing clock must not read as infinitely stalled."""
    p = _open_pr()
    seen = {"pr:100": (pr_signature(p), "2026-08-01T09:00:00Z")}
    stalls, _ = detect_stalls(world(prs=[p]), seen)  # now=""
    assert stalls == ()
