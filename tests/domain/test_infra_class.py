"""`infra_class_failure` — which failures measure the harness (keifu #130).

The predicate every QUALITY counter asks. Its boundaries are load-bearing in
both directions: too narrow and a broken runtime escalates healthy issues at $0
a strike; too wide and a genuinely failing agent loops forever at full session
cost.
"""

import pytest

from issue_agent.domain.outcome import (
    ADMISSION_REFUSAL_SUBTYPES,
    AgentResult,
    ExitReason,
    infra_class_failure,
    no_session_evidence,
    PROVIDER_REFUSED_SUBTYPE,
    resumable_session,
)


def result(**kwargs) -> AgentResult:
    return AgentResult(is_error=True, **kwargs)


# ── the harness's own failures ───────────────────────────────────────────────
@pytest.mark.parametrize(
    "reason",
    [
        ExitReason.RUNTIME_UNAVAILABLE,
        ExitReason.TRANSPORT_DEATH,
        ExitReason.KILLED_EXTERNALLY,
    ],
)
def test_harness_side_endings_are_infra_class(reason):
    assert infra_class_failure(result(exit_reason=reason))


@pytest.mark.parametrize(("fields", "session"), [
    (dict(exit_reason=ExitReason.TRANSPORT_DEATH, envelope_subtype=PROVIDER_REFUSED_SUBTYPE, session_id="s"), "s"),
    (dict(exit_reason=ExitReason.AGENT_ERROR, session_id="s"), "s"),
    (dict(exit_reason=ExitReason.TRANSPORT_DEATH, session_id="s"), ""),
    (dict(exit_reason=ExitReason.BUDGET_EXHAUSTED, envelope_subtype=PROVIDER_REFUSED_SUBTYPE, session_id="s"), ""),
    (dict(exit_reason=ExitReason.TRANSPORT_DEATH, envelope_subtype=PROVIDER_REFUSED_SUBTYPE), ""),
])
def test_a_provider_refused_end_is_a_machine_failure_whose_session_stays_resumable(fields, session):
    ended = result(**fields)
    assert resumable_session(ended) == session
    if fields["exit_reason"] is ExitReason.TRANSPORT_DEATH:
        assert infra_class_failure(ended)


@pytest.mark.parametrize("subtype", sorted(ADMISSION_REFUSAL_SUBTYPES))
def test_admission_refusals_are_infra_class_despite_reporting_agent_error(subtype):
    """The accounting runtime ends the dispatch, so it reports AGENT_ERROR.
    Nothing about the ISSUE caused it, and the condition is usually
    deployment-wide — charging it sidelines the whole backlog."""
    assert infra_class_failure(
        result(envelope_subtype=subtype, exit_reason=ExitReason.AGENT_ERROR)
    )


# ── the agent's own failures ─────────────────────────────────────────────────
@pytest.mark.parametrize(
    "reason",
    [
        ExitReason.AGENT_ERROR,
        ExitReason.WALL_TIMEOUT,
        ExitReason.IDLE_TIMEOUT,
        ExitReason.NO_RESULT,
        ExitReason.BUDGET_EXHAUSTED,
        None,
    ],
)
def test_a_session_that_existed_is_never_infra_class(reason):
    """`WALL_TIMEOUT` in particular: the leg ran and used its whole budget, so
    it stays a difficulty signal. `None` (a legacy result) keeps exactly the
    behaviour it had before the taxonomy existed."""
    assert not infra_class_failure(result(exit_reason=reason))


def test_an_unknown_envelope_subtype_is_not_laundered_into_an_exemption():
    """The subtype list is HARNESS-MINTED. A runtime — or an agent — that
    reports an arbitrary subtype must not be able to buy itself out of a
    breaker by naming one."""
    assert not infra_class_failure(
        result(envelope_subtype="dispatch_claim_unavailable_lol",
               exit_reason=ExitReason.AGENT_ERROR)
    )
    assert not infra_class_failure(
        result(envelope_subtype="", exit_reason=ExitReason.AGENT_ERROR)
    )


# ── the evidence predicate ───────────────────────────────────────────────────
def test_a_dispatch_with_no_evidence_at_all_never_ran():
    assert no_session_evidence()


@pytest.mark.parametrize(
    "evidence",
    [
        {"session_id": "sess-1"},
        {"usage": {"input_tokens": 12}},
        {"cost_usd": 0.01},
        {"output": "I looked at the tests"},
        {"num_turns": 1},
    ],
)
def test_any_single_shred_of_evidence_means_the_leg_ran(evidence):
    assert not no_session_evidence(**evidence)


@pytest.mark.parametrize(
    "blank", [{"session_id": "   "}, {"output": "\n \t"}, {"usage": {}},
              {"num_turns": 0}, {"cost_usd": 0.0}],
)
def test_blank_and_zero_substrates_are_not_evidence(blank):
    assert no_session_evidence(**blank)


def test_a_subscription_run_reporting_zero_dollars_still_counts_as_run():
    """`cost_usd` alone can never carry this decision: a subscription-plan
    session reports $0.00 on a run that certainly happened."""
    assert not no_session_evidence(
        session_id="sess-1", cost_usd=0.0, usage={"input_tokens": 900_000}
    )


def test_a_huge_negative_or_absent_num_turns_is_handled():
    assert no_session_evidence(num_turns=None)
    assert no_session_evidence(num_turns=-3)
    assert not no_session_evidence(num_turns=10**9)
