"""Which number a run leg is reported under.

2026-08-03 (keifu): the review of PR #186 for issue #185 was announced as
"PR #185 approved" and taught the PR→issue map 185→185, because the fold
overwrote the number the leg itself journalled with its correlation. These
drive the fold the way the deployment does — typed events in, event views out.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from issue_agent.domain.dashboard import build_deployment_v2, pr_issue_map
from issue_agent.domain.events import (
    AgentRunCompleted,
    AgentRunStarted,
    Correlation,
    EventEnvelope,
    Producer,
    WorldObserved,
)
from issue_agent.domain.projection import fold


NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


def _event(payload, seq: int, correlation: Correlation | None = None):
    stamp = NOW - timedelta(minutes=100 - seq)
    return EventEnvelope(
        payload,
        f"event-{seq}",
        seq,
        stamp,
        stamp,
        seq,
        Producer("deployment", "owner/repo", "review", 100, "boot"),
        correlation or Correlation(),
    )


def _review_completed(attributes, correlation: Correlation, seq: int = 2):
    return _event(
        AgentRunCompleted(
            "APPROVE",
            False,
            None,
            1_000,
            "model",
            "codex",
            kind="REVIEW",
            attributes=attributes,
        ),
        seq,
        correlation,
    )


def _views(*events):
    ordered = tuple(sorted(events, key=lambda e: e.seq))
    deployment = build_deployment_v2(
        "demo",
        "owner/repo",
        (("review", "/j", "/l", ordered),),
        fold(ordered),
        lambda: NOW,
    )
    return [
        view
        for row in deployment.metrics_issues
        for view in row.events
    ] + list(deployment.unattributed)


def _run_views(*events):
    return [
        view
        for view in _views(*events)
        if view.get("event") in ("agent_run", "agent_run_start")
    ]


REVIEW_CORRELATION = Correlation(
    issue=185,
    run_id="run-1",
    leg_id="leg-1",
    dispatch_id="dispatch-1",
)


def test_a_review_leg_is_reported_under_the_pr_it_reviewed_not_its_issue():
    started = _event(
        AgentRunStarted("model", "REVIEW", {"role": "review", "number": 186}),
        1,
        REVIEW_CORRELATION,
    )
    settled = _review_completed({"number": 186, "role": "review"}, REVIEW_CORRELATION)

    views = _run_views(started, settled)

    assert [view["number"] for view in views] == [186, 186]
    assert [view["issue"] for view in views] == [185, 185]


def test_a_review_leg_teaches_the_pr_to_issue_map_the_pr_it_reviewed():
    settled = _review_completed({"number": 186, "role": "review"}, REVIEW_CORRELATION)

    assert pr_issue_map(_views(settled)) == {186: 185}


def test_a_leg_that_journalled_no_number_still_falls_back_to_its_correlation():
    """Journals written before legs recorded their own number stay readable."""
    settled = _review_completed(None, REVIEW_CORRELATION)
    with_pr = _review_completed(
        None,
        Correlation(issue=185, pr=186, run_id="run-2", leg_id="leg-2"),
        seq=3,
    )

    assert [view["number"] for view in _run_views(settled, with_pr)] == [185, 186]


def test_an_unusable_journalled_number_falls_back_instead_of_crashing():
    cases = [
        {"number": "garbage", "role": "review"},
        {"number": None, "role": "review"},
        {"number": 0, "role": "review"},
        {"number": -1, "role": "review"},
        {"number": [186], "role": "review"},
    ]
    views = _run_views(
        *(
            _review_completed(attributes, REVIEW_CORRELATION, seq=seq)
            for seq, attributes in enumerate(cases, start=2)
        )
    )

    assert [view["number"] for view in views] == [185] * len(cases)


def test_a_resolve_leg_keeps_reporting_its_issue_number():
    settled = _event(
        AgentRunCompleted(
            "done",
            False,
            None,
            1_000,
            "model",
            "codex",
            kind="RESOLVE",
            attributes={"number": 142, "role": "fix"},
        ),
        2,
        Correlation(issue=142, run_id="run-3", leg_id="leg-3"),
    )
    world = _event(
        WorldObserved("issue", 142, "hash", {"state": "open", "labels": []}),
        1,
        Correlation(issue=142),
    )

    views = _run_views(world, settled)

    assert [(view["number"], view["issue"]) for view in views] == [(142, 142)]
