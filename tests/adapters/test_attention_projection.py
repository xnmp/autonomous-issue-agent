"""Current producer-owned attention plans and their durable delivery contracts."""

from dataclasses import replace

import pytest

from issue_agent.adapters.effects import StaleExpectedRevision, command_for_action
from issue_agent.domain.actions import PostComment, Relabel, Target
from issue_agent.domain.contracts import EffectStatus
from issue_agent.domain.labels import Label
from issue_agent.domain.lifecycle import completion_guard, issue_escalation, pr_escalation
from issue_agent.domain.eligibility import LifecycleOperation
from issue_agent.domain.outcome import (
    AgentResult, address_review_actions, budget_actions,
    fix_ci_actions, pr_budget_actions,
    resolve_actions, resume_actions,
)
from tests.adapters.test_effects import _executor

BLOCKED = AgentResult(is_error=False, status="blocked", needs="which API?", summary="clarify API")


@pytest.mark.parametrize("produce,target,remove,add,signal,explanation,budget_review", [
    (lambda: pr_escalation(7, "CI ceiling", Label.AI_REVIEW).actions(), Target.PR,
     Label.AI_REVIEW, Label.NEEDS_HUMAN_REVIEW, False, "CI ceiling", False),
    (lambda: issue_escalation(7, "resolve ceiling", Label.AI_READY).actions(), Target.ISSUE,
     Label.AI_READY, Label.NEEDS_HUMAN_REVIEW, False, "resolve ceiling", False),
    (lambda: resolve_actions(7, BLOCKED, None), Target.ISSUE,
     Label.AI_WORKING, Label.AI_BLOCKED, False, "which API?", False),
    (lambda: resume_actions(7, BLOCKED, None), Target.ISSUE,
     Label.AI_WORKING, Label.AI_BLOCKED, False, "which API?", False),
    (lambda: fix_ci_actions(7, BLOCKED), Target.PR,
     None, Label.AI_BLOCKED, False, "which API?", False),
    (lambda: address_review_actions(7, BLOCKED).actions, Target.PR,
     None, Label.AI_BLOCKED, False, "clarify API", False),
    (lambda: budget_actions(7), Target.ISSUE,
     Label.AI_WORKING, Label.AI_BLOCKED, False, "Budget review needed", True),
    (lambda: pr_budget_actions(7), Target.PR,
     None, Label.AI_BLOCKED, False, "Budget review needed", True),
])
@pytest.mark.parametrize("delivery", ["fresh", "present", "lost_ack"])
def test_attention_producer_plan_and_receipts(
    tmp_path, produce, target, remove, add, signal, explanation, budget_review, delivery,
):
    actions = produce()
    comment, relabel, *extra = actions
    assert isinstance(comment, PostComment) and explanation in comment.body
    assert (comment.target, comment.number, comment.harness_signal) == (target, 7, signal)
    assert relabel == Relabel(target, 7, remove, add, harness_signal=signal)
    assert extra == ([Relabel(target, 7, None, Label.NEEDS_BUDGET_REVIEW)]
                     if budget_review else [])
    executor, gateway, store = _executor(tmp_path, legacy_review=True)
    kind = "pr" if target is Target.PR else "issue"
    if remove:
        gateway.labels.add((kind, 7, remove.value))
    gateway.labels.add((kind, 7, Label.AI_TRIAGE.value))
    if delivery == "present":
        gateway.comments.add((kind, 7, comment.body))
    assert executor.execute(comment) is True
    commands = [command_for_action(comment, repository="o/r", executor_identity="o/r:fix",
                                   expected_revision=None if signal else
                                   "head-1" if target is Target.PR else "issue-rev-1")]
    original = gateway.relabel
    for action in actions[1:]:
        if target is Target.ISSUE and action.remove is Label.AI_WORKING:
            action = replace(action, completion_guard=completion_guard(
                gateway.get_issue(7), LifecycleOperation.COMPLETE_RESOLVE))
        command = command_for_action(action, repository="o/r", executor_identity="o/r:fix",
                                     expected_revision=None if signal else
                                     "head-1" if target is Target.PR else "issue-rev-1")
        commands.append(command)
        store.enqueue_command(command)
        if delivery == "present":
            if action.remove:
                gateway.labels.discard((kind, 7, action.remove.value))
            gateway.labels.update((kind, 7, label.value) for label in action.added_labels)
        if delivery == "lost_ack":
            def lose_ack(*args):
                original(*args)
                raise TimeoutError("reply lost after mutation")

            gateway.relabel = lose_ack
            with pytest.raises(TimeoutError):
                executor.execute(action)
            gateway.relabel = original
        elif delivery == "present" and action.remove is Label.AI_WORKING:
            with pytest.raises(StaleExpectedRevision):
                executor.execute(action)
        else:
            assert executor.execute(action) is True
    assert executor.drain_pending_commands() == (len(actions) - 1 if delivery == "lost_ack" else 0)
    for command in commands:
        receipt = store.effect_receipt(command.command_id)
        refused = delivery == "present" and command.payload.get("remove") == Label.AI_WORKING.value
        assert receipt is not None
        assert receipt.status is (EffectStatus.PERMANENT_FAILURE if refused else EffectStatus.SUCCEEDED)
        if refused:
            assert receipt.error == "expected revision no longer matches remote target"
    assert gateway.has_comment(kind, 7, comment.body)
    assert gateway.has_label(kind, 7, add.value)
    assert gateway.has_label(kind, 7, Label.NEEDS_BUDGET_REVIEW.value) is (
        budget_review or add is Label.NEEDS_BUDGET_REVIEW)
    assert gateway.has_label(kind, 7, Label.AI_TRIAGE.value)
    if remove:
        assert not gateway.has_label(kind, 7, remove.value)
    assert [call[0] for call in gateway.calls] == ([] if delivery == "present" else
                                                  ["comment"] + ["relabel"] * (len(actions) - 1))
