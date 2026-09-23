import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from issue_agent.adapters import accounting_runtime
from issue_agent.adapters.accounting_runtime import AccountingRuntime
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.runtime_plan import RuntimePlan
from issue_agent.adapters.workflow_control import SQLiteWorkflowControl
from issue_agent.adapters.worktree import WorktreeUnavailable
from issue_agent.domain.contracts import Money
from issue_agent.domain.agent_task import run_input_fingerprint
from issue_agent.domain.outcome import AgentResult, ExitReason
from issue_agent.domain.pricing import BudgetPolicy
from issue_agent.domain.resolve_return import ResolveInputs
from issue_agent.domain.resolve_split import ContractRevision, ContractResetCompleted, ContractResetResult, ReauthorInput, SplitContext, SplitSelection, UnavailableContractRevision
from issue_agent.domain.workflow import NoLinkedPr, ObservedIssue, WorkflowSnapshot
from issue_agent.domain.workflow_identity import WorkflowWriter
from tests.domain.builders import issue
from tests.fakes.workflow_authority import install_workflow, resolve_snapshot


class Provider:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def run(self, task):
        self.calls += 1
        return self.result


@pytest.fixture
def split_runtime(tmp_path, monkeypatch):
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()
    state = resolve_snapshot(7)
    install_workflow(store, state)
    monkeypatch.setattr(accounting_runtime, "_fix_publication_context", lambda role, cwd:
        accounting_runtime._FixPublicationContext(str(Path(cwd).resolve()), "fix/7-contract", "/usr/bin/git", "/usr/bin/gh", "/usr/bin:/bin"))
    monkeypatch.setattr(accounting_runtime, "capture_contract_revision", lambda cwd, number, capture_id, branch:
        ContractRevision(branch, "a" * 40))
    provider = Provider(AgentResult(False, status="tests_authored", session_id="provider", cost_usd=.25))
    runtime = AccountingRuntime(provider, store, "o/r", "fix", Money("USD", 10_000_000), None, None,
        "test", "claude", BudgetPolicy({("fix", stage): Money("USD", 3_000_000)
                                       for stage in ("AUTHOR", "IMPLEMENT", "ADJUDICATE")}), max_contract_rounds=2)
    runtime.set_context("fix", "AUTHOR", 7, 7, workflow=state.binding)
    runtime.set_run_identity("author-run", "author-leg")
    task = RuntimePlan("author the contract", model="model", cwd=str(tmp_path),
        resolve_inputs=ResolveInputs("title", "body", "alice"),
        split_input=SplitContext("fix/7-contract", SplitSelection("claude", "model")))
    control = SQLiteWorkflowControl(store, WorkflowWriter("o/r", "fix", "o/r:fix"), resolve_ceiling=3)
    return runtime, provider, task, control


def test_author_is_settled_and_held_with_exact_context_before_application_observation(split_runtime):
    runtime, provider, task, control = split_runtime
    result = asyncio.run(runtime.run(task))
    assert provider.calls == 1
    assert result.workflow_attempt is not None
    returned = control.returned_result(result.workflow_attempt)
    assert returned is not None
    assert returned.split_input == task.split_input
    assert returned.contract_revision == ContractRevision("fix/7-contract", "a" * 40)
    assert len(control.pending_returns()) == 1
    state = control.load_workflow(result.workflow_attempt.binding.fence.key)
    assert isinstance(state, WorkflowSnapshot) and state.gates
    runtime.set_run_identity("duplicate-author", "duplicate-leg")
    denied = asyncio.run(runtime.run(task))
    assert denied.exit_reason is ExitReason.RUNTIME_UNAVAILABLE
    assert provider.calls == 1


def test_contract_capture_failure_keeps_the_paid_return_and_charge(split_runtime, monkeypatch):
    runtime, provider, task, control = split_runtime

    def fail_capture(*args):
        raise WorktreeUnavailable("checkout dirty")

    monkeypatch.setattr(accounting_runtime, "capture_contract_revision", fail_capture)
    result = asyncio.run(runtime.run(task))
    returned = control.returned_result(result.workflow_attempt)
    assert isinstance(returned.contract_revision, UnavailableContractRevision)
    assert len(control.pending_returns()) == 1
    with runtime.store.transaction() as db:
        assert db.execute("SELECT amount_micros FROM charges WHERE run_id='author-run'").fetchone()[0] == 250000


def _finalize_infra(split_runtime):
    runtime, provider, task, control = split_runtime
    provider.result = AgentResult(True, status="runtime_unavailable", exit_reason=ExitReason.RUNTIME_UNAVAILABLE,
                                  session_id="provider", cost_usd=.25)
    result = asyncio.run(runtime.run(task))
    returned = control.returned_result(result.workflow_attempt)
    now = datetime.now(timezone.utc)
    key = result.workflow_attempt.binding.fence.key
    control.finalize_return(returned, ObservedIssue(key, issue(7, title="title", body="body", author="alice", updated_at="revision"), now), NoLinkedPr(key, now))
    return runtime, provider, task, control


def test_infra_return_can_retry_same_phase_only_after_finalized_observation(split_runtime):
    runtime, provider, task, control = _finalize_infra(split_runtime)
    runtime.set_run_identity("infra-retry", "retry-leg")
    result = asyncio.run(runtime.run(task))
    assert result.workflow_attempt is not None
    assert provider.calls == 2


@pytest.mark.parametrize("change", ["prompt", "inputs", "selection"])
def test_infrastructure_retry_cannot_silently_change_paid_inputs(split_runtime, change):
    runtime, provider, task, control = _finalize_infra(split_runtime)
    if change == "prompt":
        task = replace(task, intent=replace(task.intent, prompt="another contract"))
    elif change == "inputs":
        task = replace(task, resolve_inputs=replace(task.resolve_inputs, title="another issue"))
    else:
        task = replace(task, split_input=replace(task.split_input,
                       selection=replace(task.split_input.selection, difficulty_notice="different guidance")))
    runtime.set_run_identity("changed-retry", "changed-leg")
    denied = asyncio.run(runtime.run(task))
    assert denied.exit_reason is ExitReason.RUNTIME_UNAVAILABLE
    assert provider.calls == 1


def test_cross_stage_admission_cannot_race_an_in_flight_implementation(split_runtime):
    runtime, provider, task, control = split_runtime
    first = asyncio.run(runtime.run(task))
    returned = control.returned_result(first.workflow_attempt)
    now = datetime.now(timezone.utc)
    key = first.workflow_attempt.binding.fence.key
    completed = control.finalize_return(returned, ObservedIssue(key,
        issue(7, title="title", body="body", author="alice", updated_at="revision"), now), NoLinkedPr(key, now))
    cursor = completed.snapshot.episode.continuation.split
    seen = []

    class InterruptedImplementation:
        async def run(self, paid_task):
            other = replace(runtime, inner=provider)
            other.set_context("fix", "AUTHOR", 7, 7, workflow=first.workflow_attempt.binding)
            other.set_run_identity("racing-author", "racing-leg")
            seen.append(await other.run(replace(task, intent=replace(task.intent, prompt="changed author inputs"))))
            return AgentResult(True, exit_reason=ExitReason.RUNTIME_UNAVAILABLE, cost_usd=.25)

    implementation = replace(runtime, inner=InterruptedImplementation())
    implementation.set_context("fix", "IMPLEMENT", 7, 7, workflow=first.workflow_attempt.binding)
    implementation.set_run_identity("implementation", "implementation-leg")
    asyncio.run(implementation.run(replace(task, split_input=cursor)))
    assert len(seen) == 1
    assert seen[0].exit_reason is ExitReason.RUNTIME_UNAVAILABLE
    assert provider.calls == 1


def test_completed_author_cannot_be_admitted_again(split_runtime):
    runtime, provider, task, control = split_runtime
    first = asyncio.run(runtime.run(task))
    returned = control.returned_result(first.workflow_attempt)
    now = datetime.now(timezone.utc)
    key = first.workflow_attempt.binding.fence.key
    control.finalize_return(returned, ObservedIssue(key, issue(7, title="title", body="body", author="alice", updated_at="revision"), now), NoLinkedPr(key, now))
    runtime.set_run_identity("same-author", "second-leg")
    denied = asyncio.run(runtime.run(task))
    assert denied.exit_reason is ExitReason.RUNTIME_UNAVAILABLE
    assert provider.calls == 1


def _replacement_author(split_runtime):
    runtime, provider, task, control = split_runtime
    first = asyncio.run(runtime.run(task))
    returned = control.returned_result(first.workflow_attempt)
    now = datetime.now(timezone.utc)
    key = first.workflow_attempt.binding.fence.key
    control.finalize_return(returned, ObservedIssue(key,
        issue(7, title="title", body="body", author="alice", updated_at="revision"), now), NoLinkedPr(key, now))
    replacement = replace(task, intent=replace(task.intent, prompt="Author updated requirements"))
    intent = control.reauthor_reset_intent(first.workflow_attempt.binding, run_input_fingerprint(replacement.intent))
    assert intent is not None
    with control.contract_reset_authority(intent):
        ack = control.complete_contract_reset(ContractResetCompleted(
            intent, ContractResetResult(intent.baseline.revision, intent.archive_ref), datetime.now(timezone.utc)))
    assert isinstance(task.split_input, SplitContext)
    return replace(replacement, split_input=ReauthorInput(ack, task.split_input))


def test_replacement_author_captures_reset_provenance_and_survives_recomposition(split_runtime):
    from issue_agent.adapters.workflow_codec import decode_workflow, encode_workflow

    replacement = _replacement_author(split_runtime)
    runtime, provider, task, control = split_runtime
    runtime.set_run_identity("replacement-author", "replacement-leg")
    result = asyncio.run(runtime.run(replacement))
    assert provider.calls == 2
    returned = control.returned_result(result.workflow_attempt)
    assert returned.split_input == replacement.split_input
    assert decode_workflow(encode_workflow(returned.split_input), ReauthorInput) == replacement.split_input
    recovered = SQLiteWorkflowControl(runtime.store, control.writer, resolve_ceiling=3)
    pending = recovered.pending_returns()
    assert len(pending) == 1
    now = datetime.now(timezone.utc)
    key = result.workflow_attempt.binding.fence.key
    done = recovered.finalize_return(returned, ObservedIssue(key,
        issue(7, title="title", body="body", author="alice", updated_at="revision"), now), NoLinkedPr(key, now))
    assert done.snapshot.episode.continuation.author_attempt == result.workflow_attempt
    assert done.snapshot.episode.continuation.split.source == result.workflow_attempt
    assert provider.calls == 2


@pytest.mark.parametrize("change", ["omit_reset", "different_input"])
def test_replacement_author_requires_reset_for_exact_replacement_input(split_runtime, change):
    replacement = _replacement_author(split_runtime)
    runtime, provider, task, control = split_runtime
    if change == "omit_reset":
        replacement = replace(replacement, split_input=task.split_input)
    else:
        replacement = replace(replacement, intent=replace(replacement.intent, prompt="Another replacement"))
    runtime.set_run_identity("invalid-replacement", "invalid-leg")
    denied = asyncio.run(runtime.run(replacement))
    assert denied.exit_reason is ExitReason.RUNTIME_UNAVAILABLE
    assert provider.calls == 1
    with runtime.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM charges").fetchone()[0] == 1


def test_failed_replacement_cannot_reuse_preexecution_reset(split_runtime):
    replacement = _replacement_author(split_runtime)
    runtime, provider, task, control = split_runtime
    provider.result = AgentResult(True, exit_reason=ExitReason.RUNTIME_UNAVAILABLE, cost_usd=.25)
    runtime.set_run_identity("failed-replacement", "failed-leg")
    result = asyncio.run(runtime.run(replacement))
    returned = control.returned_result(result.workflow_attempt)
    now = datetime.now(timezone.utc)
    key = result.workflow_attempt.binding.fence.key
    control.finalize_return(returned, ObservedIssue(key,
        issue(7, title="title", body="body", author="alice", updated_at="revision"), now), NoLinkedPr(key, now))
    runtime.set_run_identity("unsafe-retry", "unsafe-leg")
    denied = asyncio.run(runtime.run(replacement))
    assert denied.exit_reason is ExitReason.RUNTIME_UNAVAILABLE
    assert provider.calls == 2
    intent = control.reauthor_reset_intent(result.workflow_attempt.binding, run_input_fingerprint(replacement.intent))
    assert intent is not None
    assert intent.source == result.workflow_attempt
    assert intent.baseline == replacement.split_input.reset.intent.baseline
    assert intent.intent_id != replacement.split_input.reset.intent.intent_id


def test_configuration_change_cannot_bypass_a_live_split_with_ordinary_resolve(split_runtime):
    runtime, provider, task, control = split_runtime
    first = asyncio.run(runtime.run(task))
    returned = control.returned_result(first.workflow_attempt)
    now = datetime.now(timezone.utc)
    key = first.workflow_attempt.binding.fence.key
    control.finalize_return(returned, ObservedIssue(key,
        issue(7, title="title", body="body", author="alice", updated_at="revision"), now), NoLinkedPr(key, now))
    ordinary = replace(runtime, budget_policy=BudgetPolicy({("fix", "RESOLVE"): Money("USD", 3_000_000)}))
    ordinary.set_context("fix", "RESOLVE", 7, 7, workflow=first.workflow_attempt.binding)
    denied = asyncio.run(ordinary.run(replace(task, split_input=None)))
    assert denied.exit_reason is ExitReason.RUNTIME_UNAVAILABLE
    assert provider.calls == 1


@pytest.mark.parametrize("kind", ["RESOLVE", "RESUME"])
def test_split_configuration_cannot_inherit_a_finalized_ordinary_worktree(split_runtime, kind):
    runtime, provider, task, control = split_runtime
    ordinary = replace(runtime, budget_policy=BudgetPolicy({("fix", kind): Money("USD", 3_000_000)}))
    ordinary.set_context("fix", kind, 7, 7, workflow=resolve_snapshot(7).binding)
    provider.result = AgentResult(True, exit_reason=ExitReason.RUNTIME_UNAVAILABLE, cost_usd=.25)
    result = asyncio.run(ordinary.run(replace(task, split_input=None)))
    returned = control.returned_result(result.workflow_attempt)
    now = datetime.now(timezone.utc)
    key = result.workflow_attempt.binding.fence.key
    control.finalize_return(returned, ObservedIssue(key,
        issue(7, title="title", body="body", author="alice", updated_at="revision"), now), NoLinkedPr(key, now))
    runtime.set_run_identity("unsafe-split-author", "unsafe-leg")
    denied = asyncio.run(runtime.run(task))
    assert denied.exit_reason is ExitReason.RUNTIME_UNAVAILABLE
    assert provider.calls == 1
    with runtime.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM charges").fetchone()[0] == 1


def test_split_author_cannot_race_an_active_ordinary_resolve(split_runtime):
    runtime, provider, task, control = split_runtime
    denied = []

    class OrdinaryProvider:
        async def run(self, ordinary_task):
            denied.append(await runtime.run(task))
            return AgentResult(False, status="blocked", needs="clarify", cost_usd=.25)

    ordinary = replace(runtime, inner=OrdinaryProvider(), budget_policy=BudgetPolicy({("fix", "RESOLVE"): Money("USD", 3_000_000)}))
    ordinary.set_context("fix", "RESOLVE", 7, 7, workflow=resolve_snapshot(7).binding)
    asyncio.run(ordinary.run(replace(task, split_input=None)))
    assert len(denied) == 1
    assert denied[0].exit_reason is ExitReason.RUNTIME_UNAVAILABLE
    assert provider.calls == 0


def test_combined_split_context_capacity_is_checked_before_payment(split_runtime):
    runtime, provider, task, control = split_runtime
    from issue_agent.domain.resolve_split import SplitReply
    context = replace(task.split_input, reply=SplitReply("human", "reply", "😀" * 60000))
    denied = asyncio.run(runtime.run(replace(task, split_input=context)))
    assert denied.exit_reason is ExitReason.RUNTIME_UNAVAILABLE
    assert provider.calls == 0


@pytest.mark.parametrize("rounds", [0, 2, 4])
@pytest.mark.parametrize("character", ["x", "😀"])
def test_near_capacity_initial_contract_can_be_reauthored_and_implemented(split_runtime, rounds, character):
    from issue_agent.adapters.workflow_codec import encode_workflow
    from issue_agent.domain.resolve_split import SplitReply

    runtime, provider, task, control = split_runtime
    runtime = replace(runtime, max_contract_rounds=rounds)
    control = SQLiteWorkflowControl(runtime.store, control.writer, resolve_ceiling=3, max_contract_rounds=rounds)
    # Fill both issue and reply slots at rounds zero, where one text slot's
    # individual character bound is smaller than the aggregate allowance.
    inputs = replace(task.resolve_inputs, body="b" * (35000 if rounds == 0 else 0))
    context = replace(task.split_input, reply=SplitReply("human", "reply", character))
    empty_size = len(encode_workflow((inputs, context)).encode())
    unit_size = len(encode_workflow(character).encode()) - len(encode_workflow("").encode())
    count = (100000 - rounds * 20000 - empty_size - 8) // unit_size + 1
    context = replace(context, reply=replace(context.reply, body=character * count))
    task = replace(task, resolve_inputs=inputs, split_input=context)
    assert 0 <= 100000 - rounds * 20000 - len(encode_workflow((inputs, context)).encode()) < 24
    binding = resolve_snapshot(7).binding

    def finish(stage, selected_task, run_id, status):
        runtime.set_context("fix", stage, 7, 7, workflow=binding)
        runtime.set_run_identity(run_id, run_id + "-leg")
        provider.result = AgentResult(False, status=status, cost_usd=.25)
        result = asyncio.run(runtime.run(selected_task))
        assert result.workflow_attempt is not None, result.summary
        returned = control.returned_result(result.workflow_attempt)
        now = datetime.now(timezone.utc)
        return control.finalize_return(returned, ObservedIssue(binding.fence.key,
            issue(7, title=inputs.title, body=inputs.body, author=inputs.author, updated_at=run_id), now), NoLinkedPr(binding.fence.key, now))

    finish("AUTHOR", task, "large-initial", "tests_authored")
    replacement = replace(task, intent=replace(task.intent, prompt="replacement contract"))
    intent = control.reauthor_reset_intent(binding, run_input_fingerprint(replacement.intent))
    assert intent is not None
    with control.contract_reset_authority(intent):
        ack = control.complete_contract_reset(ContractResetCompleted(intent,
            ContractResetResult(intent.baseline.revision, intent.archive_ref), datetime.now(timezone.utc)))
    replacement = replace(replacement, split_input=ReauthorInput(ack, context))
    done = finish("AUTHOR", replacement, "large-replacement", "tests_authored")
    finish("IMPLEMENT", replace(task, split_input=done.snapshot.episode.continuation.split), "large-implementation", "blocked")
    assert provider.calls == 3


def test_maximum_supported_rounds_preserve_large_unicode_context_through_settlement(split_runtime):
    from issue_agent.domain.resolve_split import AdjudicationInput, ContractResetIntent, ContractResetCompleted, ContractResetResult
    runtime, provider, task, control = split_runtime
    runtime = replace(runtime, max_contract_rounds=4)
    control = SQLiteWorkflowControl(runtime.store, control.writer, resolve_ceiling=3, max_contract_rounds=4)
    inputs = ResolveInputs("title", "b" * 10000, "alice")
    selected = replace(task.split_input.selection, difficulty_notice="😀" * 600)
    task = replace(task, resolve_inputs=inputs, split_input=replace(task.split_input, selection=selected))
    binding = resolve_snapshot(7).binding
    index = 0

    def complete(stage, supplied, status, *, verdict=""):
        nonlocal index
        index += 1
        runtime.set_context("fix", stage, 7, 7, workflow=binding)
        runtime.set_run_identity(f"large-{index}", f"large-leg-{index}")
        provider.result = AgentResult(False, status=status, cost_usd=.25, session_id=f"session-{index}",
            summary="😀" * 4000, needs="😀" * 4000,
            structured={"verdict": verdict, "rationale": "😀" * 4000})
        result = asyncio.run(runtime.run(replace(task, split_input=supplied)))
        assert result.workflow_attempt is not None, result.summary
        returned = control.returned_result(result.workflow_attempt)
        now = datetime.now(timezone.utc)
        committed = control.finalize_return(returned, ObservedIssue(binding.fence.key,
            issue(7, title=inputs.title, body=inputs.body, author=inputs.author, updated_at=f"revision-{index}"), now), NoLinkedPr(binding.fence.key, now))
        assert control.pending_returns() == ()
        return committed.snapshot.episode.continuation.split

    cursor = complete("AUTHOR", task.split_input, "tests_authored")
    for _ in range(4):
        cursor = complete("IMPLEMENT", cursor, "contract_change")
        intent = ContractResetIntent(binding, cursor)
        with control.contract_reset_authority(intent):
            ack = control.complete_contract_reset(ContractResetCompleted(intent,
                ContractResetResult(cursor.baseline.revision, intent.archive_ref), datetime.now(timezone.utc)))
        cursor = complete("ADJUDICATE", AdjudicationInput(ack), "done", verdict="upheld")
    assert len(cursor.context.trail) == 4
    complete("IMPLEMENT", cursor, "contract_change")
    assert provider.calls == 10
