from dataclasses import replace
from datetime import datetime, timezone

import pytest

from issue_agent.domain.contracts import RunId, WorkflowAttemptIdentity
from issue_agent.domain.effort import Effort
from issue_agent.domain.outcome import Adjudication
from issue_agent.domain.resolve_split import (
    AdjudicationInput, AdjudicationStep, ContractBaseline, ContractResetCompleted,
    ContractResetIntent, ContractResetResult, ContractRevision, NeedsAdjudication,
    ReauthorInput, ReauthorResetIntent, SplitContext, SplitImplement, SplitReply, SplitSelection, split_context,
)
from tests.fakes.workflow_authority import resolve_snapshot


def split_example():
    binding = resolve_snapshot(7).binding
    author = WorkflowAttemptIdentity(binding, RunId("author"), "author-leg")
    context = SplitContext("fix/7-contract", SplitSelection("claude", "model"),
                           SplitReply("human", "2026-09-23T01:00:00Z", "Keep the old API."))
    return SplitImplement(author, 1, context, ContractBaseline(author, ContractRevision(context.branch, "a" * 40)))


def test_upheld_rationale_and_exact_reply_follow_the_next_implementation():
    prior = split_example()
    adjudicator = replace(prior.source, run_id=RunId("adjudicator"))
    step = AdjudicationStep(adjudicator, 3, Adjudication.UPHELD, "The public API remains stable.")
    context = replace(prior.context, trail=(step,))
    next_stage = SplitImplement(adjudicator, 3, context, prior.baseline)
    assert next_stage.context.human_reply == "Keep the old API."
    assert next_stage.context.uphold_note == "The public API remains stable."
    revised = replace(context, trail=(replace(step, verdict=Adjudication.REVISED),))
    assert revised.uphold_note == ""


@pytest.mark.parametrize("sha", [None, "", "a" * 39, "a" * 41, "A" * 40, "g" * 40])
def test_contract_baseline_requires_complete_git_identity(sha):
    with pytest.raises(ValueError):
        ContractRevision("fix/7-contract", sha)


@pytest.mark.parametrize("branch", [None, "", "dev", "fix/no-issue", "fix/7-bad\nbranch"])
def test_contract_baseline_requires_issue_owned_branch(branch):
    with pytest.raises(ValueError):
        ContractRevision(branch, "a" * 40)


def test_cursor_rejects_foreign_contract_and_future_generation():
    prior = split_example()
    foreign = replace(prior.source, binding=resolve_snapshot(8).binding)
    with pytest.raises(ValueError):
        ContractBaseline(foreign, prior.baseline.revision)
    future = replace(prior.source, binding=replace(prior.source.binding,
        fence=replace(prior.source.binding.fence, generation=prior.source.binding.fence.generation + 1)))
    with pytest.raises(ValueError):
        replace(prior, baseline=replace(prior.baseline, source=future))


@pytest.mark.parametrize("changes", [
    {"runtime": ""}, {"model": None}, {"effort": "high"},
    {"difficulty_notice": "x" * 4001}, {"difficulty_notice": "bad\0notice"},
])
def test_routing_selection_is_bounded_and_typed(changes):
    with pytest.raises(ValueError):
        replace(split_example().context.selection, **changes)


@pytest.mark.parametrize("body", [None, "", " ", "x" * 65537, "bad\0reply"])
def test_reply_is_exact_nonempty_bounded_text(body):
    with pytest.raises(ValueError):
        SplitReply("human", "reply-id", body)


def test_adjudication_trail_cannot_repeat_or_reverse_paid_attempts():
    prior = split_example()
    step = AdjudicationStep(prior.source, 1, Adjudication.UPHELD, "Keep it.")
    other = replace(step, attempt=replace(step.attempt, run_id=RunId("later")), admission_version=2)
    for trail in ((step, step), (other, step), [step], (step,) * 65):
        with pytest.raises(ValueError):
            replace(prior.context, trail=trail)


def test_reset_acknowledgement_is_bound_to_exact_intent_and_revision():
    prior = split_example()
    petition = NeedsAdjudication(prior.source, 1, prior.context, prior.baseline, "The old API cannot do this.")
    intent = ContractResetIntent(prior.source.binding, petition)
    result = ContractResetResult(prior.baseline.revision, intent.archive_ref)
    ack = ContractResetCompleted(intent, result, datetime.now(timezone.utc))
    assert split_context(AdjudicationInput(ack)) == prior.context
    assert intent.intent_id == ContractResetIntent(prior.source.binding, petition).intent_id
    with pytest.raises(ValueError):
        replace(ack, result=replace(result, revision=replace(result.revision, head_sha="c" * 40)))
    with pytest.raises(ValueError):
        replace(ack, observed_at=datetime(2026, 9, 23))


def test_reauthor_reset_is_bound_to_replacement_input_and_cannot_adjudicate():
    prior = split_example()
    intent = ReauthorResetIntent(prior.source.binding, prior.source, prior.baseline, "b" * 64)
    ack = ContractResetCompleted(intent, ContractResetResult(prior.baseline.revision, intent.archive_ref), datetime.now(timezone.utc))
    value = ReauthorInput(ack, prior.context)
    assert split_context(value) == prior.context
    assert replace(intent, input_fingerprint="c" * 64).intent_id != intent.intent_id
    with pytest.raises(ValueError):
        AdjudicationInput(ack)
    with pytest.raises(ValueError):
        replace(value, context=replace(prior.context, branch="fix/7-other"))
    step = AdjudicationStep(prior.source, 1, Adjudication.UPHELD, "Keep it.")
    with pytest.raises(ValueError):
        replace(value, context=replace(prior.context, trail=(step,)))
    with pytest.raises(ValueError):
        replace(ack, intent=replace(intent, input_fingerprint="c" * 64))


@pytest.mark.parametrize("fingerprint", [None, "", "a" * 63, "a" * 65, "Z" * 64, "a" * 100000])
def test_reauthor_reset_rejects_invalid_replacement_fingerprint(fingerprint):
    prior = split_example()
    with pytest.raises(ValueError):
        ReauthorResetIntent(prior.source.binding, prior.source, prior.baseline, fingerprint)


def test_reauthor_reset_rejects_foreign_baseline_and_ack_purpose():
    prior = split_example()
    foreign = replace(prior.source, binding=resolve_snapshot(8).binding)
    with pytest.raises(ValueError):
        ReauthorResetIntent(foreign.binding, foreign, prior.baseline, "b" * 64)
    petition = NeedsAdjudication(prior.source, 1, prior.context, prior.baseline, "Change contract.")
    intent = ContractResetIntent(prior.source.binding, petition)
    ack = ContractResetCompleted(intent, ContractResetResult(prior.baseline.revision, intent.archive_ref), datetime.now(timezone.utc))
    with pytest.raises(ValueError):
        ReauthorInput(ack, prior.context)
