
from tests.domain.builders import observed_pr
import pytest

from issue_agent.domain.actions import (
    DiscardSelfClearMarker,
    FinalizeBudgetBlockRetirement,
    FinalizeMachineBlockRetirement,
    Relabel,
    ReportSelfClearCeiling,
    Target,
)
from issue_agent.domain.labels import Label
from issue_agent.domain.self_clear import (
    BudgetBlock,
    BudgetLimitObservation,
    BudgetWorld,
    MachineBlock,
    MachineBlocker,
    self_clear_actions,
)
from issue_agent.domain.world import Check, IssueView, PRView, WorldState


def _machine(kind=MachineBlocker.PUBLICATION) -> MachineBlock:
    return MachineBlock(7, Target.PR, 100, 100, kind, "blocked-head")


def _pr(*, checks=(), checks_known=True, head="blocked-head") -> PRView:
    return observed_pr(
        100,
        head_ref_oid=head,
        labels=frozenset({Label.AI_PR_OPEN, Label.AI_BLOCKED}),
        checks=checks,
        checks_known=checks_known,
        closing_issue=7,
    )


def _decide(world, *, machine=None, budget=None, budgets=None, count=0):
    return self_clear_actions(
        world,
        {7: machine} if machine else {},
        {7: budget} if budget else {},
        {7: budgets} if budgets else {},
        {7: count},
        frozenset(),
    )


def test_budget_recovery_does_not_release_uncleared_machine_on_same_pr():
    budget = BudgetBlock(7, Target.PR, 100, 2_000_000, "issue:7", 5_000_000, 100_000)
    pr = _pr(head="different-head")
    world = WorldState(prs=(pr,))
    assert _decide(world, machine=_machine(), budget=budget,
        budgets=BudgetWorld((BudgetLimitObservation("issue:7", "available", 0, 5_000_000),), True)) == []


def test_sibling_external_discards_are_selected_one_per_snapshot():
    from dataclasses import replace
    budget = BudgetBlock(7, Target.PR, 100, 2_000_000, "issue:7", 5_000_000, 100_000)
    world = WorldState(prs=(replace(_pr(), labels=frozenset({Label.AI_PR_OPEN})),))
    assert _decide(world, machine=_machine(), budget=budget) == [DiscardSelfClearMarker(7, "machine")]
    assert _decide(world, budget=budget) == [DiscardSelfClearMarker(7, "budget")]


def test_sibling_ceiling_reports_are_selected_once_per_snapshot():
    budget = BudgetBlock(7, Target.PR, 100, 2_000_000, "issue:7", 5_000_000, 100_000)
    actions = _decide(WorldState(prs=(_pr(),)), machine=_machine(), budget=budget,
        budgets=BudgetWorld((BudgetLimitObservation("issue:7", "available", 0, 5_000_000),), True), count=3)
    assert actions == [ReportSelfClearCeiling(7, Target.PR, 100, "publication", 3)]


@pytest.mark.parametrize("field,value", [
    ("committed_micros", True), ("committed_micros", None), ("cap_micros", 2**63),
    ("cap_micros", True), ("scope", ""), ("disposition", None), ("currency", "usd"),
])
def test_budget_observation_boundary_refuses_untrusted_projection_amounts(field, value):
    facts = dict(scope="issue:7", disposition="available", committed_micros=0, cap_micros=5_000_000, currency="USD")
    facts[field] = value
    with pytest.raises(ValueError):
        BudgetLimitObservation(**facts)


def test_publication_block_retires_only_on_the_recorded_open_pr_head():
    actions = _decide(WorldState(prs=(_pr(),)), machine=_machine())

    assert actions == [
        Relabel(
            Target.PR,
            100,
            Label.AI_BLOCKED,
            Label.AI_PR_OPEN,
            "blocked-head",
        ),
        FinalizeMachineBlockRetirement(
            7,
            Target.PR,
            100,
            "publication",
            "blocked-head",
            "blocked-head",
        ),
    ]


def test_publication_block_does_not_clear_a_different_head():
    assert _decide(
        WorldState(prs=(_pr(head="new-head"),)), machine=_machine()
    ) == []


@pytest.mark.parametrize(
    ("world", "expected"),
    [
        (WorldState(), []),
        (WorldState(prs=(_pr(head=""),)), []),
        (
            WorldState(prs=(_pr(checks=(Check("unit", bucket="pass"),)),)),
            "clear",
        ),
        (
            WorldState(prs=(_pr(
                checks=(Check("unit", bucket="pass"),), checks_known=False
            ),)),
            [],
        ),
        (
            WorldState(prs=(_pr(checks=(Check("unit", bucket="pending"),)),)),
            [],
        ),
    ],
)
def test_check_block_requires_present_pr_known_checks_and_green(world, expected):
    actions = _decide(world, machine=_machine(MachineBlocker.CHECKS))

    if expected == "clear":
        assert isinstance(actions[-1], FinalizeMachineBlockRetirement)
    else:
        assert actions == expected


def test_check_block_does_not_clear_green_checks_on_a_different_head():
    pr = _pr(
        head="new-head",
        checks=(Check("unit", bucket="pass"),),
    )

    assert _decide(
        WorldState(prs=(pr,)), machine=_machine(MachineBlocker.CHECKS)
    ) == []


def test_absent_or_unrecognized_machine_marker_never_clears_clarification():
    assert _decide(WorldState(prs=(_pr(),))) == []
    with pytest.raises(ValueError):
        MachineBlock(7, Target.ISSUE, 7, 100, MachineBlocker.PUBLICATION, "head")


def test_observed_manual_unblock_discards_stale_machine_marker_without_counting():
    pr = observed_pr(100, head_ref_oid="head", labels=frozenset({Label.AI_PR_OPEN}))

    assert _decide(WorldState(prs=(pr,)), machine=_machine()) == [
        DiscardSelfClearMarker(7, "machine")
    ]


def test_budget_block_clears_at_exact_admission_boundary():
    issue = IssueView(
        7,
        labels=frozenset({Label.AI_BLOCKED, Label.NEEDS_BUDGET_REVIEW}),
        updated_at="rev-1",
    )
    block = BudgetBlock(
        7, Target.ISSUE, 7, 2_000_000, "issue:7", 5_000_000, 500_000
    )
    budget = BudgetWorld((
        BudgetLimitObservation("issue:7", "available", 3_000_000, 5_000_000),
    ), complete=True)

    actions = _decide(WorldState(issues=(issue,)), budget=block, budgets=budget)

    assert actions[:1] == [
        Relabel(
            Target.ISSUE,
            7,
            None,
            Label.AI_READY,
            "rev-1",
            removes=(Label.NEEDS_BUDGET_REVIEW, Label.AI_BLOCKED),
        ),
    ]
    assert actions[-1] == FinalizeBudgetBlockRetirement(
        7,
        Target.ISSUE,
        7,
        "issue:7",
        500_000,
        2_000_000,
        2_000_000,
        5_000_000,
        "USD",
    )


@pytest.mark.parametrize(
    "budget",
    [
        BudgetWorld(),
        BudgetWorld((), complete=True),
        BudgetWorld((
            BudgetLimitObservation("issue:7", "unavailable", 0, 5_000_000),
        ), complete=False),
        BudgetWorld((
            BudgetLimitObservation("issue:7", "available", 3_000_001, 5_000_000),
        ), complete=True),
    ],
)
def test_budget_block_does_not_clear_on_absent_partial_or_over_cap_input(budget):
    issue = IssueView(
        7,
        labels=frozenset({Label.AI_BLOCKED, Label.NEEDS_BUDGET_REVIEW}),
    )
    block = BudgetBlock(7, Target.ISSUE, 7, 2_000_000, "issue:7", 5_000_000)

    assert _decide(WorldState(issues=(issue,)), budget=block, budgets=budget) == []


def test_fourth_self_clear_is_stopped_and_journal_edge_is_one_shot():
    world = WorldState(prs=(_pr(),))
    actions = _decide(world, machine=_machine(), count=3)

    assert actions == [
        ReportSelfClearCeiling(7, Target.PR, 100, "publication", 3)
    ]
    assert self_clear_actions(
        world,
        {7: _machine()},
        {},
        {},
        {7: 3},
        frozenset({7}),
    ) == []


def test_malformed_budget_values_are_rejected_at_the_boundary():
    with pytest.raises(ValueError):
        BudgetBlock(7, Target.ISSUE, 7, -1, "issue:7", 5_000_000)
    with pytest.raises(ValueError):
        BudgetLimitObservation("issue:7", "available", -1, 1)
    with pytest.raises(ValueError):
        MachineBlock(7, Target.PR, 100, 100, "publication", "head")
