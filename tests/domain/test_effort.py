"""domain.effort — the escalation ladder (#16).

Behaviour under test: how much evidence exists, what a notch buys, and that the
ladder can only ever hand back a stance at least as strong as the one it was
given. Nothing here touches a runtime, a store, or GitHub — that wiring is
covered in tests/adapters/test_fix_workflow.py.
"""

import pytest

from issue_agent.domain.effort import (
    DEFAULT_EFFORT,
    DEFAULT_TIER,
    EFFORT_LADDER,
    TIER_LADDER,
    Effort,
    EscalationSignals,
    Stance,
    effort_rank,
    escalate,
    parse_effort,
    tier_rank,
)


# ── ordering ──────────────────────────────────────────────────────────────────
def test_effort_ladder_orders_weakest_to_strongest():
    assert [e.value for e in EFFORT_LADDER] == ["low", "medium", "high"]
    assert effort_rank(Effort.LOW) < effort_rank(Effort.MEDIUM) < effort_rank(Effort.HIGH)


def test_effort_order_is_not_the_lexicographic_order_of_its_values():
    """The trap a `str`-mixin enum would have walked into: sorted by their WORDS
    these read high < low < medium, so a `str` Enum's inherited comparisons would
    have made HIGH the weakest rung and silently inverted every escalation."""
    lexicographic = sorted(e.value for e in EFFORT_LADDER)
    assert lexicographic != [e.value for e in EFFORT_LADDER]
    assert effort_rank(Effort.HIGH) == len(EFFORT_LADDER) - 1


def test_tier_ladder_orders_cheapest_to_strongest():
    assert TIER_LADDER == ("trivial", "standard", "complex")
    assert tier_rank("trivial") < tier_rank("standard") < tier_rank("complex")


# ── totality on junk ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw", ["HIGH", " high ", "High"])
def test_parse_effort_normalises_case_and_whitespace(raw):
    assert parse_effort(raw) is Effort.HIGH


@pytest.mark.parametrize("raw", ["", "hihg", None, 7, [], "medium-high"])
def test_parse_effort_returns_the_default_on_anything_unrecognised(raw):
    assert parse_effort(raw) is None
    assert parse_effort(raw, Effort.LOW) is Effort.LOW


def test_parse_effort_passes_an_effort_through():
    assert parse_effort(Effort.MEDIUM) is Effort.MEDIUM


def test_unknown_tier_prices_as_the_default_tier():
    """Same rule route_model applies: a classifier misfire escalates from the
    same floor it would have been dispatched on, rather than raising."""
    assert tier_rank("wibble") == tier_rank(DEFAULT_TIER)
    assert tier_rank("") == tier_rank(DEFAULT_TIER)


# ── counting the evidence ─────────────────────────────────────────────────────
def test_no_evidence_is_no_notches():
    assert EscalationSignals().notches() == 0


def test_a_recurrence_is_evidence_from_the_first_occurrence():
    assert EscalationSignals(recurrence=True).notches() == 1


def test_each_failed_resolve_retry_is_a_notch():
    assert EscalationSignals(resolve_retries=2).notches() == 2


def test_the_first_review_round_is_not_evidence_but_later_ones_are():
    """One REQUEST_CHANGES is the system working as designed; it is the SECOND
    outstanding one on the PR that says the model is not converging."""
    assert EscalationSignals(review_rounds=1).notches() == 0
    assert EscalationSignals(review_rounds=3).notches() == 2


def test_signals_compose():
    assert EscalationSignals(
        recurrence=True, resolve_retries=1, review_rounds=2
    ).notches() == 3


@pytest.mark.parametrize(
    "signals",
    [
        EscalationSignals(resolve_retries=-4),
        EscalationSignals(review_rounds=-9),
    ],
)
def test_impossible_counter_values_never_produce_negative_evidence(signals):
    assert signals.notches() == 0


# ── spending the evidence ─────────────────────────────────────────────────────
def test_no_evidence_leaves_the_stance_alone():
    base = Stance(tier="trivial", effort=Effort.LOW)
    assert escalate(base, 0) == base


def test_the_first_notch_buys_effort_not_a_pricier_tier():
    """Effort is the cheap lever — same model, more thinking — so it is spent
    before the tier that costs real dollars per token."""
    assert escalate(Stance("trivial", Effort.LOW), 1) == Stance("trivial", Effort.MEDIUM)


def test_tier_climbs_only_once_effort_is_exhausted():
    assert escalate(Stance("trivial", Effort.LOW), 2) == Stance("trivial", Effort.HIGH)
    assert escalate(Stance("trivial", Effort.LOW), 3) == Stance("standard", Effort.HIGH)
    assert escalate(Stance("trivial", Effort.LOW), 4) == Stance("complex", Effort.HIGH)


def test_a_stance_already_at_high_effort_spends_its_first_notch_on_tier():
    assert escalate(Stance("standard", Effort.HIGH), 1) == Stance("complex", Effort.HIGH)


def test_escalation_saturates_at_the_ceiling():
    """Past the strongest configuration the next step up is a human, which the
    retry breakers own — extra notches must be absorbed, not overflow."""
    ceiling = Stance(TIER_LADDER[-1], EFFORT_LADDER[-1])
    assert escalate(Stance("trivial", Effort.LOW), 99) == ceiling
    assert escalate(ceiling, 1) == ceiling


@pytest.mark.parametrize("notches", [-1, -100])
def test_negative_notches_cannot_de_escalate(notches):
    base = Stance("standard", Effort.MEDIUM)
    assert escalate(base, notches) == base


def test_escalation_is_monotonic_over_every_stance_and_notch_count():
    """The anti-thrash property (#16), stated as an invariant rather than a
    handful of examples: no input can hand back a weaker configuration."""
    for tier in TIER_LADDER:
        for effort in EFFORT_LADDER:
            for n in range(0, 8):
                out = escalate(Stance(tier, effort), n)
                assert tier_rank(out.tier) >= tier_rank(tier)
                assert effort_rank(out.effort) >= effort_rank(effort)


def test_escalation_is_total_on_a_garbage_tier():
    out = escalate(Stance("not-a-tier", Effort.MEDIUM), 1)
    assert out.tier in TIER_LADDER
    assert out.effort is Effort.HIGH


def test_default_stance_is_the_middle_of_both_ladders():
    """A leg with no router verdict and no configured effort starts where an
    unrouted dispatch already lands, so the ladder never silently downgrades a
    default deployment."""
    assert Stance() == Stance(DEFAULT_TIER, DEFAULT_EFFORT)
    assert DEFAULT_TIER in TIER_LADDER
    assert DEFAULT_EFFORT in EFFORT_LADDER


# ── the sensor behind `review_rounds` (#16 P1) ────────────────────────────────
# The store's per-review attempt counter re-keys to zero on every new review, so
# it could never answer "rounds on this PR are climbing". These assert the
# GitHub-derived counter that replaced it, from the FIXER's point of view.
def test_outstanding_change_requests_counts_everyone_but_the_fixer():
    from issue_agent.domain.review import count_outstanding_change_requests
    from issue_agent.domain.world import Review

    reviews = (
        Review("CHANGES_REQUESTED", "rev-bot", "t1"),
        Review("CHANGES_REQUESTED", "a-human", "t2"),
        Review("CHANGES_REQUESTED", "fix-bot", "t3"),  # our own — not evidence
    )
    assert count_outstanding_change_requests(reviews, "fix-bot") == 2


def test_an_approve_clears_the_outstanding_count():
    from issue_agent.domain.review import count_outstanding_change_requests
    from issue_agent.domain.world import Review

    reviews = (
        Review("CHANGES_REQUESTED", "rev-bot", "t1"),
        Review("CHANGES_REQUESTED", "rev-bot", "t2"),
        Review("APPROVED", "rev-bot", "t3"),
        Review("CHANGES_REQUESTED", "rev-bot", "t4"),
    )
    assert count_outstanding_change_requests(reviews, "fix-bot") == 1


def test_no_reviews_is_no_evidence():
    from issue_agent.domain.review import count_outstanding_change_requests

    assert count_outstanding_change_requests((), "fix-bot") == 0
