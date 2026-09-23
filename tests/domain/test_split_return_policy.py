"""Split return decisions preserve exact contract and predecessor provenance."""

from dataclasses import replace
from itertools import permutations

import pytest

from issue_agent.domain.contracts import RunId, WorkflowAttemptIdentity
from issue_agent.domain.outcome import Adjudication, ExitReason
from issue_agent.domain.resolve_return import ResolveInputs, ReturnedResolve
from issue_agent.domain.resolve_split import (
    AdjudicationInput, ContractBaseline, ContractResetCompleted, ContractResetIntent,
    ContractResetResult, ContractRevision, NeedsAdjudication, SplitContext,
    SplitImplement, SplitReply, SplitSelection, UnavailableContractRevision,
)
from issue_agent.domain.workflow import (
    AuthoredResolve, LinkedPrPresent, NoLinkedPr, ResolveAdmission, ResolveAttemptResult,
    ResolveResultKind, ResolveStage, WorkflowChanged,
)
from issue_agent.domain.workflow_identity import WorkflowRevision
from issue_agent.domain.workflow_policy import clear_return_split, decide_return, record_resolve_attempt, split_return_continuation
from tests.domain.test_workflow_policy import NOW, completion_observation, snapshot


def split_return(stage, **changes):
    state = snapshot()
    author = WorkflowAttemptIdentity(state.binding, RunId("author"), "author-leg")
    implement = WorkflowAttemptIdentity(state.binding, RunId("implement"), "implement-leg")
    context = SplitContext("fix/7-contract", SplitSelection("codex", "model"), SplitReply("alice", "reply:1", "exact human answer"))
    revision = ContractRevision(context.branch, "a" * 40)
    baseline = ContractBaseline(author, revision)
    ready = SplitImplement(author, 1, context, baseline)
    petition = NeedsAdjudication(implement, 2, context, baseline, "test pins wrong observable")
    intent = ContractResetIntent(state.binding, petition)
    ack = ContractResetCompleted(intent, ContractResetResult(revision, intent.archive_ref), NOW)
    attempt = {ResolveStage.AUTHOR: author, ResolveStage.IMPLEMENT: implement,
               ResolveStage.ADJUDICATE: WorkflowAttemptIdentity(state.binding, RunId("adjudicate"), "adjudicate-leg")}[stage]
    values = dict(attempt=attempt, stage=stage, inputs=ResolveInputs("title", "body", "alice"),
                  is_error=False, status="tests_authored" if stage is ResolveStage.AUTHOR else "contract_change" if stage is ResolveStage.IMPLEMENT else "",
                  summary="summary", needs="test pins wrong observable", envelope_subtype="", exit_reason=None,
                  session=None, returned_at=NOW,
                  split_input={ResolveStage.AUTHOR: context, ResolveStage.IMPLEMENT: ready, ResolveStage.ADJUDICATE: AdjudicationInput(ack)}[stage],
                  contract_revision=revision if stage is ResolveStage.AUTHOR else None)
    values.update(changes)
    return ReturnedResolve(**values)


def admission(returned, version=None):
    version = version or {ResolveStage.AUTHOR: 1, ResolveStage.IMPLEMENT: 2, ResolveStage.ADJUDICATE: 3}[returned.stage]
    return ResolveAdmission(returned.attempt, WorkflowRevision(returned.attempt.binding.fence, version), returned.stage,
                            2 if returned.stage is ResolveStage.ADJUDICATE else None, "a" * 64, "b" * 64)


def classified(returned, **kwargs):
    result, actions = decide_return(returned, completion_observation(returned), NoLinkedPr(returned.attempt.binding.fence.key, NOW), now=NOW, **kwargs)
    return replace(result, split=split_return_continuation(returned, admission(returned), result, issue=completion_observation(returned), **kwargs)), actions


def test_author_success_establishes_exact_baseline_reply_and_selection():
    returned = split_return(ResolveStage.AUTHOR)
    result, actions = classified(returned)
    assert result.kind is ResolveResultKind.STAGE_PROGRESSED and actions == ()
    assert isinstance(result.split, SplitImplement)
    assert result.split.baseline == ContractBaseline(returned.attempt, returned.contract_revision)
    assert result.split.context.human_reply == "exact human answer"


def test_missing_author_baseline_never_fabricates_a_checkpoint():
    returned = split_return(ResolveStage.AUTHOR, contract_revision=UnavailableContractRevision("Git read failed"))
    result, _ = decide_return(returned, completion_observation(returned), NoLinkedPr(returned.attempt.binding.fence.key, NOW), now=NOW)
    with pytest.raises(ValueError, match="baseline is unavailable"):
        split_return_continuation(returned, admission(returned), result, issue=completion_observation(returned))
    captured = ContractRevision("fix/7-contract", "b" * 40)
    cursor = split_return_continuation(returned, admission(returned), result, issue=completion_observation(returned), contract_revision=captured)
    assert cursor.baseline.revision == captured


def test_contract_petition_is_progress_without_a_strike_and_retains_exact_objection():
    result, actions = classified(split_return(ResolveStage.IMPLEMENT))
    assert result.kind is ResolveResultKind.STAGE_PROGRESSED and actions == ()
    assert isinstance(result.split, NeedsAdjudication)
    assert result.split.objection == "test pins wrong observable"


@pytest.mark.parametrize("verdict,error,expected", [("upheld", False, Adjudication.UPHELD), ("revised", False, Adjudication.REVISED), ("revised", True, Adjudication.UPHELD), ("malformed", False, Adjudication.UPHELD)])
def test_adjudication_preserves_or_replaces_baseline_and_derives_uphold(verdict, error, expected):
    revised = ContractRevision("fix/7-contract", "b" * 40)
    returned = split_return(ResolveStage.ADJUDICATE, verdict=verdict, is_error=error, rationale="observable is correct", contract_revision=revised if expected is Adjudication.REVISED else ContractRevision("fix/7-contract", "a" * 40))
    result, actions = classified(returned)
    assert isinstance(result.split, SplitImplement) and actions == ()
    assert result.split.context.trail[-1].verdict is expected
    assert result.split.baseline.revision == (revised if expected is Adjudication.REVISED else returned.split_input.petition.baseline.revision)
    assert result.split.context.uphold_note == ("" if expected is Adjudication.REVISED else "observable is correct")


@pytest.mark.parametrize("revision", [None, UnavailableContractRevision("read failed"), ContractRevision("fix/7-contract", "b" * 40)])
def test_upheld_verdict_never_accepts_an_unknown_or_changed_contract(revision):
    returned = split_return(ResolveStage.ADJUDICATE, verdict="upheld", contract_revision=revision)
    with pytest.raises(ValueError, match="unchanged captured contract"):
        classified(returned)


def test_revised_verdict_requires_a_changed_captured_contract():
    returned = split_return(ResolveStage.ADJUDICATE, verdict="revised", contract_revision=ContractRevision("fix/7-contract", "a" * 40))
    with pytest.raises(ValueError, match="changed captured contract"):
        classified(returned)


def test_verified_pr_wins_over_contract_petition():
    returned = split_return(ResolveStage.IMPLEMENT)
    result, actions = decide_return(returned, completion_observation(returned), LinkedPrPresent(returned.attempt.binding.fence.key, 99, "head", NOW), now=NOW)
    assert result.kind is ResolveResultKind.PR_OPENED
    assert split_return_continuation(returned, admission(returned), result, issue=completion_observation(returned)) is None
    assert len(actions) == 2


def test_changed_requirements_do_not_continue_a_stale_dispute():
    returned = split_return(ResolveStage.IMPLEMENT)
    result, actions = decide_return(returned, completion_observation(returned, body="new body"), NoLinkedPr(returned.attempt.binding.fence.key, NOW), now=NOW)
    assert result.kind is ResolveResultKind.COUNTED_REQUEUE and actions
    assert split_return_continuation(returned, admission(returned), result, issue=completion_observation(returned, body="new body")) is None


@pytest.mark.parametrize("status,error,kind", [("blocked", False, ResolveResultKind.BLOCKED), ("failed", True, ResolveResultKind.COUNTED_REQUEUE)])
def test_implementation_retry_keeps_applicable_contract_without_rebuying_author(status, error, kind):
    returned = split_return(ResolveStage.IMPLEMENT, status=status, is_error=error)
    result, _ = classified(returned)
    assert result.kind is kind
    assert isinstance(result.split, SplitImplement)
    assert result.split.source == returned.attempt
    assert result.split.admission_version == admission(returned).revision.version
    assert result.split.context == returned.split_input.context
    assert result.split.baseline == returned.split_input.baseline


def test_contract_ceiling_escalates_without_budget_review_or_resolve_strike():
    result, actions = classified(split_return(ResolveStage.IMPLEMENT), adjudications=2, adjudication_ceiling=2)
    assert result.kind is ResolveResultKind.STAGE_PROGRESSED
    assert result.split is None
    assert "Contract dispute unresolved" in actions[0].comment
    assert "needs-human-review" in {label.value for label in actions[0].added_labels}
    assert "needs-budget-review" not in {label.value for label in actions[0].added_labels}


@pytest.mark.parametrize("reason,kind", [(ExitReason.BUDGET_EXHAUSTED, ResolveResultKind.BUDGET_REFUSAL), (ExitReason.KILLED_EXTERNALLY, ResolveResultKind.INFRA_FAILURE)])
def test_infrastructure_and_budget_returns_do_not_advance_cursor(reason, kind):
    returned = split_return(ResolveStage.ADJUDICATE, is_error=True, exit_reason=reason)
    result, _ = classified(returned)
    assert result.kind is kind
    if kind is ResolveResultKind.INFRA_FAILURE:
        assert isinstance(result.split, NeedsAdjudication)
        assert result.split.source == returned.attempt
        assert result.split.baseline == returned.split_input.petition.baseline
    else:
        assert result.split is None


def test_exhausted_infra_adjudication_escalates_contract_without_quality_strike():
    returned = split_return(ResolveStage.ADJUDICATE, is_error=True, exit_reason=ExitReason.KILLED_EXTERNALLY)
    result, actions = classified(returned, adjudications=2, adjudication_ceiling=2)
    assert result.kind is ResolveResultKind.INFRA_FAILURE
    assert result.split is None
    assert "Contract dispute unresolved" in actions[0].comment


def test_infra_implementation_continues_partial_work_without_reauthoring():
    returned = split_return(ResolveStage.IMPLEMENT, is_error=True, exit_reason=ExitReason.KILLED_EXTERNALLY)
    result, _ = classified(returned)
    assert result.kind is ResolveResultKind.INFRA_FAILURE
    assert isinstance(result.split, SplitImplement)
    assert result.split.source == returned.attempt
    assert result.split.context == returned.split_input.context
    assert result.split.baseline == returned.split_input.baseline


def test_late_cursor_cannot_resurrect_a_petition_cleared_by_newer_result():
    author = split_return(ResolveStage.AUTHOR)
    author_result, _ = classified(author)
    state = replace(snapshot(), revision=WorkflowRevision(author.attempt.binding.fence, 10))
    state = record_resolve_attempt(state, author_result, ceiling=3, admission=admission(author)).snapshot
    petition = split_return(ResolveStage.IMPLEMENT)
    petition_result, _ = classified(petition)
    terminal = ResolveAttemptResult(WorkflowAttemptIdentity(state.binding, RunId("terminal"), "leg"), ResolveResultKind.PR_OPENED, stage=ResolveStage.IMPLEMENT)
    terminal_admission = ResolveAdmission(terminal.attempt, WorkflowRevision(state.binding.fence, 4), ResolveStage.IMPLEMENT)
    for sequence in permutations(((petition_result, admission(petition)), (terminal, terminal_admission))):
        current = state
        for result, receipt in sequence:
            current = record_resolve_attempt(current, result, ceiling=3, admission=receipt).snapshot
        assert isinstance(current.episode.continuation, AuthoredResolve)
        assert current.episode.continuation.split is None


def test_infra_cursor_and_terminal_clear_do_not_advance_the_session_watermark():
    author = split_return(ResolveStage.AUTHOR)
    result, _ = classified(author)
    current = replace(snapshot(), revision=WorkflowRevision(author.attempt.binding.fence, 10))
    current = record_resolve_attempt(current, result, ceiling=3, admission=admission(author)).snapshot
    petition = split_return(ResolveStage.IMPLEMENT)
    result, _ = classified(petition)
    current = record_resolve_attempt(current, result, ceiling=3, admission=admission(petition)).snapshot
    original_watermark = current.episode.continuation.last_applied_admission_version
    infra = split_return(ResolveStage.ADJUDICATE, is_error=True, exit_reason=ExitReason.KILLED_EXTERNALLY)
    result, _ = classified(infra)
    current = record_resolve_attempt(current, result, ceiling=3, admission=admission(infra)).snapshot
    assert current.episode.continuation.last_applied_admission_version == original_watermark
    cleared = clear_return_split(current, admission(infra, version=4))
    assert cleared.episode.continuation.last_applied_admission_version == original_watermark
    replay = record_resolve_attempt(cleared, result, ceiling=3, admission=admission(infra)).snapshot
    assert replay.episode.continuation.split is None
    assert replay.episode.continuation.split_applied_admission_version == 4
