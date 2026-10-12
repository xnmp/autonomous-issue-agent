"""Operator interventions per merged PR — the unattended-operation metric.

The dashboard is the only place that turns journalled operator actions into a
number, so these tests drive it the way the deployment does: synthetic
journal events in, `Metrics` out. Nothing asserts on how the counts are
stored, only on what the metric reports.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.domain.dashboard import (
    INTERVENTION_CLASSES,
    Metrics,
    build_deployment,
    classify_intervention,
    intervention_counts,
    intervention_items,
    interventions_line,
    operator_action,
)


NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
OPERATOR = "owner/repo:dashboard"


def _event(
    action_type: str,
    seq: int,
    desc: str,
    attributes: dict,
    *,
    command_id: str = "",
    issue: int | None = None,
    pr: int | None = None,
):
    stamp = NOW - timedelta(minutes=100 - seq)
    event = {
        "ts": stamp.isoformat(),
        "event": "action",
        "event_id": f"event-{seq}",
        "desc": desc,
        "command_id": command_id,
        "action": {"type": action_type, **attributes},
    }
    if issue is not None:
        event["issue"] = issue
    if pr is not None:
        event["pr"] = pr
    return event


def _unblock(seq: int, issue: int, removed=("ai-blocked",)):
    return _event(
        "operator_unblock",
        seq,
        f"operator unblock issue #{issue}: "
        + " ".join(f"-{label}" for label in removed),
        {
            "add": [],
            "remove": list(removed),
            "operator": OPERATOR,
            "labels_applied": True,
        },
        command_id=f"operator-unblock:{seq:x}",
        issue=issue,
    )


def _dismiss(seq: int, issue: int, pr: int):
    return _event(
        "operator_dismiss_review",
        seq,
        f"operator re-arm pr #{pr}: dismissed review PRR_1",
        {
            "add": [],
            "remove": [],
            "operator": OPERATOR,
            "review_id": "PRR_1",
            "dismissed": True,
        },
        command_id=f"operator-dismiss-review:{seq:x}",
        issue=issue,
        pr=pr,
    )


def _promote(seq: int, issue: int):
    return _event(
        "operator_relabel",
        seq,
        f"operator relabel issue #{issue}: +ai-ready",
        {"add": ["ai-ready"], "remove": [], "operator": OPERATOR},
        issue=issue,
    )


def _priority(seq: int, issue: int, priority: str = "P1"):
    others = [label for label in ("P1", "P2", "P3") if label != priority]
    return _event(
        "operator_relabel",
        seq,
        f"operator relabel issue #{issue}: +{priority} "
        + " ".join(f"-{label}" for label in others),
        {"add": [priority], "remove": others, "operator": OPERATOR},
        issue=issue,
    )


def _harness_relabel(seq: int, issue: int):
    """A relabel the FIX daemon applied — not an intervention."""
    return _event(
        "relabel",
        seq,
        f"relabel issue #{issue}: -ai-ready +ai-working",
        {"add": ["ai-working"], "remove": ["ai-ready"]},
        command_id=f"harness:{seq:x}",
        issue=issue,
    )


def _merged_issue(seq: int, issue: int, pr: int):
    """The minimum journal that makes `issue` count as a done (merged) issue."""
    stamp = NOW - timedelta(minutes=100 - seq)
    return [
        {
            "ts": stamp.isoformat(),
            "event": "action",
            "desc": f"clear state for pr #{pr}, issue #{issue} (merged)",
        }
    ]


def _metrics(events) -> Metrics:
    return build_deployment(
        "demo",
        "owner/repo",
        (("fix", "/j", "/l", list(events)),),
        now_fn=lambda: NOW,
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
        "operator_quarantine",
        20,
        "operator quarantine issue #7",
        {"operator": OPERATOR},
        command_id="operator-quarantine:1",
        issue=7,
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
        "operator_unblock",
        20,
        display,
        {"add": [], "remove": ["ai-blocked"], "operator": OPERATOR},
        command_id="operator-unblock:deadbeef",
        issue=7,
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


# ── itemized timeline ─────────────────────────────────────────────────────────
def _classes(metrics: Metrics) -> list[str]:
    return [item.action_class for item in metrics.interventions]


def test_the_timeline_itemizes_every_counted_intervention_oldest_first() -> None:
    metrics = _metrics(
        [
            *_merged_issue(1, 7, 70),
            _unblock(20, 7),
            _dismiss(21, 7, 70),
            _promote(22, 7),
        ]
    )

    items = metrics.interventions

    assert [item.kind for item in items] == [
        "operator_unblock",
        "operator_dismiss_review",
        "operator_relabel",
    ]
    assert [item.issue for item in items] == [7, 7, 7]
    assert [item.pr for item in items] == [None, 70, None]
    assert [item.ts for item in items] == sorted(item.ts for item in items)


def test_the_timeline_and_the_tile_never_disagree_about_the_split() -> None:
    metrics = _metrics(
        [
            *_merged_issue(1, 7, 70),
            _unblock(20, 7),
            _priority(21, 7),
            _promote(22, 7),
            _harness_relabel(23, 7),
        ]
    )

    per_class: dict[str, int] = dict.fromkeys(INTERVENTION_CLASSES, 0)
    for item in metrics.interventions:
        per_class[item.metric_class] += 1

    assert per_class == _by_class(metrics)
    assert len(metrics.interventions) == metrics.interventions_total


def test_a_rearm_that_moved_the_scope_note_reads_as_guidance() -> None:
    guided = _event(
        "operator_unblock",
        20,
        "operator unblock issue #7: -ai-blocked",
        {
            "add": [],
            "remove": ["ai-blocked"],
            "operator": OPERATOR,
            "labels_applied": True,
            "guidance": "set",
        },
        command_id="operator-unblock:beef",
        issue=7,
    )

    metrics = _metrics([*_merged_issue(1, 7, 70), guided])

    assert _classes(metrics) == ["guidance"]
    # A display refinement, never a contradiction: the guardrail still counts
    # this as the re-arm it was.
    assert metrics.interventions[0].metric_class == "rearm"
    assert _by_class(metrics)["rearm"] == 1


def test_an_operator_label_move_outside_the_grammar_reads_as_relabel() -> None:
    moved = _event(
        "operator_relabel",
        20,
        "operator relabel issue #7: +needs-design",
        {"add": ["needs-design"], "remove": [], "operator": OPERATOR},
        command_id="operator-relabel:cafe",
        issue=7,
    )

    metrics = _metrics([*_merged_issue(1, 7, 70), moved])

    assert _classes(metrics) == ["relabel"]
    assert metrics.interventions[0].metric_class == "other"


def test_a_promote_stays_policy_and_a_priority_nudge_stays_rearm() -> None:
    metrics = _metrics(
        [*_merged_issue(1, 7, 70), _promote(20, 7), _priority(21, 7)]
    )

    assert sorted(_classes(metrics)) == ["policy", "rearm"]


def test_an_undated_or_malformed_operator_event_still_yields_one_item() -> None:
    """Journal rows the fold cannot date must not vanish from the timeline —
    an intervention nobody can see is worse than one with an unknown clock."""
    rows = (
        {
            "event": "action",
            "event_id": "no-ts",
            "action": {"type": "operator_unblock", "operator": OPERATOR},
        },
        {
            "event": "action",
            "event_id": "bad-ts",
            "ts": "not-a-timestamp",
            "issue": "seven",
            "action": {"type": "operator_relabel", "operator": OPERATOR,
                       "add": None, "remove": 7},
        },
    )

    items = intervention_items((), rows)

    assert [item.ts for item in items] == [None, None]
    assert [item.action_class for item in items] == ["rearm", "relabel"]
    assert [item.issue for item in items] == [None, None]


def test_one_action_projected_onto_two_rows_is_one_timeline_item() -> None:
    shared = {
        "event": "action",
        "event_id": "shared",
        "ts": "2026-08-02T10:00:00+00:00",
        "issue": 7,
        "pr": 70,
        "action": {"type": "operator_unblock", "operator": OPERATOR},
    }

    items = intervention_items((), (dict(shared), dict(shared)))

    assert len(items) == 1
    assert (items[0].issue, items[0].pr) == (7, 70)
