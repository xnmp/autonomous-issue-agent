"""Operator interventions per merged PR — the unattended-operation metric.

The fold is the only place that turns journalled operator actions into a
number, so these tests drive it the way the deployment does: synthetic
schema-v2 journals in, `Metrics` out. Nothing asserts on how the counts are
stored, only on what the metric reports.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.domain.dashboard import (
    INTERVENTION_CLASSES,
    Metrics,
    build_deployment_v2,
    classify_intervention,
    intervention_counts,
    interventions_line,
    operator_action,
)
from issue_agent.domain.events import (
    ActionApplied,
    AgentRunCompleted,
    AgentRunStarted,
    Correlation,
    EventEnvelope,
    Producer,
    WorldObserved,
)
from issue_agent.domain.projection import fold


NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
OPERATOR = "owner/repo:dashboard"


def _event(payload, seq: int, correlation: Correlation | None = None):
    stamp = NOW - timedelta(minutes=100 - seq)
    return EventEnvelope(
        payload,
        f"event-{seq}",
        seq,
        stamp,
        stamp,
        seq,
        Producer("deployment", "owner/repo", "fix", 100, "boot"),
        correlation or Correlation(),
    )


def _unblock(seq: int, issue: int, removed=("ai-blocked",)):
    return _event(
        ActionApplied(
            f"operator-unblock:{seq:x}",
            "operator_unblock",
            "issue",
            f"operator unblock issue #{issue}: "
            + " ".join(f"-{label}" for label in removed),
            issue,
            {
                "add": [],
                "remove": list(removed),
                "operator": OPERATOR,
                "labels_applied": True,
            },
        ),
        seq,
        Correlation(issue=issue),
    )


def _dismiss(seq: int, issue: int, pr: int):
    return _event(
        ActionApplied(
            f"operator-dismiss-review:{seq:x}",
            "operator_dismiss_review",
            "pr",
            f"operator re-arm pr #{pr}: dismissed review PRR_1",
            pr,
            {
                "add": [],
                "remove": [],
                "operator": OPERATOR,
                "review_id": "PRR_1",
                "dismissed": True,
            },
        ),
        seq,
        Correlation(issue=issue, pr=pr),
    )


def _promote(seq: int, issue: int):
    return _event(
        ActionApplied(
            "9f55fd81a2ef20b879ddb88ff35ac43fcffb648922eccca41d3c2abd3a660869",
            "operator_relabel",
            "issue",
            f"operator relabel issue #{issue}: +ai-ready",
            issue,
            {"add": ["ai-ready"], "remove": [], "operator": OPERATOR},
        ),
        seq,
        Correlation(issue=issue),
    )


def _priority(seq: int, issue: int, priority: str = "P1"):
    others = [label for label in ("P1", "P2", "P3") if label != priority]
    return _event(
        ActionApplied(
            "c3cb5ff1060ddffdb9b267f85443967c4fd950018975cb72d6944dde2ff4dd97",
            "operator_relabel",
            "issue",
            f"operator relabel issue #{issue}: +{priority} "
            + " ".join(f"-{label}" for label in others),
            issue,
            {"add": [priority], "remove": others, "operator": OPERATOR},
        ),
        seq,
        Correlation(issue=issue),
    )


def _harness_relabel(seq: int, issue: int):
    """A relabel the FIX daemon applied — not an intervention."""
    return _event(
        ActionApplied(
            f"harness:{seq:x}",
            "relabel",
            "issue",
            f"relabel issue #{issue}: -ai-ready +ai-working",
            issue,
            {"add": ["ai-working"], "remove": ["ai-ready"]},
        ),
        seq,
        Correlation(issue=issue),
    )


def _merged_issue(seq: int, issue: int, pr: int):
    """The minimum journal that makes `issue` count as a done (merged) issue."""
    return [
        _event(
            WorldObserved(
                "issue",
                issue,
                f"hash-{issue}",
                {"state": "closed", "labels": ["ai-done"]},
            ),
            seq,
            Correlation(issue=issue),
        ),
        _event(
            AgentRunStarted("model", "RESOLVE"),
            seq + 1,
            Correlation(issue=issue, run_id=f"run-{issue}", leg_id=f"leg-{issue}"),
        ),
        _event(
            AgentRunCompleted("ok", False, 1_000, 1000, "model", "codex"),
            seq + 2,
            Correlation(issue=issue, run_id=f"run-{issue}", leg_id=f"leg-{issue}"),
        ),
    ]


def _metrics(events) -> Metrics:
    ordered = tuple(sorted(events, key=lambda e: e.seq))
    model = fold(ordered)
    return build_deployment_v2(
        "demo",
        "owner/repo",
        (("fix", "/j", "/l", ordered),),
        model,
        lambda: NOW,
    ).metrics


def _by_class(metrics: Metrics) -> dict[str, int]:
    return dict(metrics.interventions_by_class)


# ── classification ────────────────────────────────────────────────────────────
def test_mixed_operator_journal_classifies_every_intervention() -> None:
    metrics = _metrics(
        [
            *_merged_issue(1, 7, 70),
            *_merged_issue(10, 8, 80),
            _unblock(20, 7),
            _dismiss(21, 7, 70),
            _priority(22, 8),
            _promote(23, 8),
            _promote(24, 7),
            _harness_relabel(25, 8),
        ]
    )

    assert _by_class(metrics) == {"rearm": 3, "policy": 2, "other": 0}
    assert metrics.interventions_total == 5


def test_a_daemons_own_relabel_is_not_an_operator_intervention() -> None:
    metrics = _metrics([*_merged_issue(1, 7, 70), _harness_relabel(20, 7)])

    assert metrics.interventions_total == 0
    assert _by_class(metrics) == dict.fromkeys(INTERVENTION_CLASSES, 0)


def test_an_unknown_operator_verb_is_counted_as_other_not_dropped() -> None:
    invented = _event(
        ActionApplied(
            "operator-quarantine:1",
            "operator_quarantine",
            "issue",
            "operator quarantine issue #7",
            7,
            {"operator": OPERATOR},
        ),
        20,
        Correlation(issue=7),
    )

    metrics = _metrics([*_merged_issue(1, 7, 70), invented])

    assert _by_class(metrics) == {"rearm": 0, "policy": 0, "other": 1}
    assert metrics.interventions_total == 1


def test_every_class_is_reported_even_when_nothing_happened() -> None:
    metrics = _metrics(_merged_issue(1, 7, 70))

    assert [name for name, _count in metrics.interventions_by_class] == list(
        INTERVENTION_CLASSES
    )
    assert metrics.interventions_total == 0


# ── the ratio ─────────────────────────────────────────────────────────────────
def test_policy_promotes_are_excluded_from_the_headline_ratio() -> None:
    metrics = _metrics(
        [
            *_merged_issue(1, 7, 70),
            _unblock(20, 7),
            _promote(21, 7),
            _promote(22, 7),
        ]
    )

    assert _by_class(metrics) == {"rearm": 1, "policy": 2, "other": 0}
    # 1 rearm + 0 other over 1 merged issue — the two promotes do not inflate it.
    assert metrics.interventions_per_merged_pr == 1.0


def test_other_is_charged_to_the_ratio_alongside_rearm() -> None:
    metrics = Metrics(
        done_issues=2,
        in_flight_issues=0,
        total_cost_usd=0.0,
        cost_per_done_usd=0.0,
        by_kind=(),
        first_pass_approvals=0,
        first_pass_changes=0,
        rounds_per_pr=0.0,
        escalations=0,
        recurrences=0,
        spend_blocks=0,
        today_usd=None,
        interventions_by_class=(("rearm", 3), ("policy", 9), ("other", 1)),
    )

    assert metrics.interventions_per_merged_pr == 2.0
    assert metrics.interventions_total == 13


def test_zero_merged_prs_yields_no_ratio_rather_than_dividing_by_zero() -> None:
    metrics = _metrics([_unblock(20, 7), _unblock(21, 7)])

    assert metrics.done_issues == 0
    assert metrics.interventions_per_merged_pr is None
    assert _by_class(metrics)["rearm"] == 2


def test_no_interventions_and_no_merges_is_still_an_unknown_ratio() -> None:
    assert _metrics([]).interventions_per_merged_pr is None


# ── malformed and boundary input ──────────────────────────────────────────────
@pytest.mark.parametrize(
    "display",
    [
        "",
        "operator unblock issue #: ",
        "operator unblock issue #7",  # no colon — parse_action returns None
        "operator unblock issue #999999999999999999999: -ai-blocked",
        "operator unblock issue #7: " + "-x" * 5000,
        "operator unblock issue #7: \x00퟿ — ünïcödé",
    ],
)
def test_a_malformed_operator_display_string_still_counts_and_never_crashes(
    display: str,
) -> None:
    event = _event(
        ActionApplied(
            "operator-unblock:deadbeef",
            "operator_unblock",
            "issue",
            display,
            7,
            {"add": [], "remove": ["ai-blocked"], "operator": OPERATOR},
        ),
        20,
        Correlation(issue=7),
    )

    metrics = _metrics([*_merged_issue(1, 7, 70), event])

    assert _by_class(metrics) == {"rearm": 1, "policy": 0, "other": 0}


@pytest.mark.parametrize(
    "attributes",
    [
        {"operator": OPERATOR, "add": None, "remove": None},
        {"operator": OPERATOR, "add": "ai-ready", "remove": 17},
        {"operator": OPERATOR, "add": [None, "", 42], "remove": [{"x": 1}]},
        {"operator": OPERATOR},
    ],
)
def test_malformed_relabel_label_attributes_fall_back_to_other(attributes) -> None:
    """A relabel whose labels cannot be read is unclassifiable, not invisible:
    `other` is exactly the bucket that says "someone intervened, unknown how"."""
    action = {"type": "operator_relabel", **attributes}

    assert classify_intervention(action) == "other"


@pytest.mark.parametrize("action", [None, "relabel", 42, [], {"type": None}])
def test_classification_of_a_non_mapping_action_is_other(action) -> None:
    assert classify_intervention(action) == "other"


@pytest.mark.parametrize(
    "event",
    [
        None,
        "action",
        {},
        {"event": "action"},
        {"event": "action", "action": "operator_unblock"},
        {"event": "agent_run", "action": {"type": "operator_unblock"}},
        {"event": "action", "action": {"type": "relabel"}},
    ],
)
def test_a_non_operator_event_yields_no_operator_action(event) -> None:
    assert operator_action(event) is None


def test_a_command_id_prefix_alone_identifies_an_intervention() -> None:
    """Provenance can arrive on the envelope rather than the attributes: an
    older journal predating the `operator` stamp still carries the minted
    command id, and dropping those would silently under-count history."""
    event = {
        "event": "action",
        "command_id": "operator-unblock:deadbeef",
        "action": {"type": "relabel", "add": [], "remove": ["ai-blocked"]},
    }

    assert operator_action(event) is not None
    assert (
        classify_intervention(event["action"], event["command_id"]) == "rearm"
    )


def test_the_same_event_projected_twice_counts_once() -> None:
    """An action correlated to an issue AND its PR can reach the counter from
    two rows; the event id is what makes it one intervention."""
    duplicated = {
        "event": "action",
        "event_id": "event-20",
        "command_id": "operator-unblock:1",
        "action": {"type": "operator_unblock", "operator": OPERATOR},
    }

    counts = dict(intervention_counts((), (duplicated, dict(duplicated))))

    assert counts == {"rearm": 1, "policy": 0, "other": 0}


def test_events_without_an_event_id_are_each_counted() -> None:
    """Identity falls back to position, so two separate unblocks from a journal
    that never wrote event ids are still two interventions."""
    action = {
        "event": "action",
        "action": {"type": "operator_unblock", "operator": OPERATOR},
    }

    counts = dict(intervention_counts((), (dict(action), dict(action))))

    assert counts["rearm"] == 2


def test_a_huge_operator_journal_is_counted_exactly() -> None:
    events = tuple(
        {
            "event": "action",
            "event_id": f"e{index}",
            "command_id": "operator-unblock:1",
            "action": {"type": "operator_unblock", "operator": OPERATOR},
        }
        for index in range(20_000)
    )

    assert dict(intervention_counts((), events))["rearm"] == 20_000


# ── readout ───────────────────────────────────────────────────────────────────
def test_the_status_line_reports_the_split_and_the_ratio() -> None:
    metrics = _metrics(
        [*_merged_issue(1, 7, 70), _unblock(20, 7), _promote(21, 7)]
    )

    assert interventions_line(metrics) == (
        "interventions: 2 (rearm 1, policy 1, other 0) — 1.0 per merged PR"
    )


def test_the_status_line_says_so_when_there_is_nothing_to_divide_by() -> None:
    line = interventions_line(_metrics([_unblock(20, 7)]))

    assert line == "interventions: 1 (rearm 1, policy 0, other 0) — no merged PRs yet"
