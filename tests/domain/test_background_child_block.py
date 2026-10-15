"""#255: the pure rule that strips a self-inflicted human-input block."""

from __future__ import annotations

from issue_agent.domain.outcome import (
    AgentResult,
    ExitReason,
    LegClass,
    classify_leg,
    infra_class_failure,
    reclassify_pending_background_block,
)


def _blocked() -> AgentResult:
    return AgentResult(
        is_error=False, status="blocked", needs="wait for reviewer", exit_reason=ExitReason.OK, session_id="s"
    )


def test_blocked_with_pending_children_becomes_an_agent_attempt_error():
    out = reclassify_pending_background_block(_blocked(), 2)
    assert out.is_error and classify_leg(out) is LegClass.AGENT
    assert out.exit_reason is ExitReason.BACKGROUND_CHILD_PENDING
    assert not infra_class_failure(out)
    assert (out.status, out.needs, out.session_id) == ("blocked", "wait for reviewer", "s")


def test_no_pending_children_leaves_a_genuine_block_alone():
    for pending in (0, -1):
        out = reclassify_pending_background_block(_blocked(), pending)
        assert classify_leg(out) is LegClass.ASKED
        assert not infra_class_failure(out)


def test_only_a_blocked_claim_is_rewritten():
    for status in ("pr_opened", "pushed", "", "failed"):
        result = AgentResult(is_error=False, status=status, exit_reason=ExitReason.OK)
        assert reclassify_pending_background_block(result, 1) == result


def test_an_existing_error_keeps_its_own_reason():
    result = AgentResult(is_error=True, status="blocked", exit_reason=ExitReason.WALL_TIMEOUT)
    assert reclassify_pending_background_block(result, 1) == result
