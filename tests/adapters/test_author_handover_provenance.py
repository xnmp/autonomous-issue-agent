"""Scratch enrichment cannot change durable author-checkpoint applicability."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest

from issue_agent.adapters import accounting_runtime
from issue_agent.adapters.accounting_runtime import AccountingRuntime
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.journal import JournalingRuntime
from issue_agent.adapters.repository_guidance import RepositoryGuidanceRuntime
from issue_agent.adapters.runtime_plan import RuntimePlan
from issue_agent.adapters.workflow_codec import decode_workflow
from issue_agent.adapters.workflow_control import SQLiteWorkflowControl
from issue_agent.domain.agent_task import run_input_fingerprint
from issue_agent.domain.contracts import Money
from issue_agent.domain.outcome import AgentResult, ExitReason
from issue_agent.domain.pricing import BudgetPolicy, ModelRates, RateTable
from issue_agent.domain.resolve_return import ResolveInputs
from issue_agent.domain.resolve_split import ContractRevision, ContractResetCompleted, ContractResetResult, ReauthorInput, SplitContext, SplitSelection
from issue_agent.domain.workflow import (
    AuthoredResolve, NoLinkedPr, ObservedIssue, ResolveAdmission, ResolveStage,
    WorkflowCommitted, WorkflowSnapshot,
)
from issue_agent.domain.workflow_identity import WorkflowWriter
from tests.domain.builders import issue
from tests.fakes.legacy_journal import Journal
from tests.fakes.workflow_authority import install_workflow, resolve_snapshot, with_issue_recovery


@pytest.mark.parametrize("authoritative_change", ["prompt", "system_instruction", "schema"])
@pytest.mark.parametrize("repository_guidance", [False, True])
def test_author_checkpoint_fingerprints_authoritative_inputs_not_delivered_handover(
    tmp_path, monkeypatch, authoritative_change, repository_guidance,
):
    control = SQLiteControlStore(str(tmp_path / "control.db"))
    control.initialize()
    snapshot = with_issue_recovery(resolve_snapshot(1))
    install_workflow(control, snapshot)
    owner = "o/r:fix:test"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.setattr(
        accounting_runtime, "_fix_publication_context",
        lambda _role, _cwd: accounting_runtime._FixPublicationContext(
            str(worktree), "fix/1-test", "/usr/bin/git", "/usr/bin/gh", "/usr/bin",
        ),
    )
    monkeypatch.setattr(accounting_runtime, "capture_contract_revision",
                        lambda _cwd, _issue, _capture, branch: ContractRevision(branch, "a" * 40))
    runtime_name = "codex" if repository_guidance else "claude"
    original = RuntimePlan(
        "Author the current issue contract", cwd=str(worktree), model="test-model",
        append_system_prompt="Preserve public behavior", schema={"type": "object"},
        context_prefix="Existing scratch context\n\n", context_suffix="\n\nExisting trailing context", max_budget_usd=2,
        resolve_inputs=ResolveInputs("title", "body", "alice"),
        split_input=SplitContext("fix/1-test", SplitSelection(runtime_name, "test-model")),
    )
    changes = {
        "prompt": "Author the newly corrected requirements",
        "system_instruction": "Preserve public behavior and the documented API",
        "schema": {"type": "object", "required": ["contract"]},
    }
    revised = replace(original, intent=replace(
        original.intent, **{authoritative_change: changes[authoritative_change]},
    ))
    planned = (original, original, revised)
    notes = ("First exhausted experiment", "A different failed experiment", "A different failed experiment")
    admissions = []
    supplied = []

    class Provider:
        async def run(self, task):
            index = len(supplied)
            run_id = task.env["ISSUE_AGENT_RUN_ID"]
            with control._connect() as db:
                row = db.execute(
                    "SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-admission' "
                    "AND scope='o/r' AND key=?", (run_id,),
                ).fetchone()
            receipt = decode_workflow(row[0], ResolveAdmission)
            assert receipt.stage is ResolveStage.AUTHOR
            expected = (original, revised)[index]
            assert receipt.input_fingerprint == run_input_fingerprint(expected.intent)
            assert task.intent is expected.intent
            admissions.append(receipt)
            supplied.append(task)
            return AgentResult(False, status="tests_authored", cost_usd=1, session_id=f"author-session-{index}")

    provider = RepositoryGuidanceRuntime(Provider()) if repository_guidance else Provider()
    accounting = AccountingRuntime(
        provider, control, "o/r", "fix", Money("USD", 10_000_000), None, None,
        "config", runtime_name, BudgetPolicy({("fix", "AUTHOR"): Money("USD", 2_000_000)}),
        rate_table=RateTable("fixture", "1", "USD", {"test-model": ModelRates(1, 1, 1, 1)}),
        owner_id=owner,
    )
    runtime = JournalingRuntime(
        accounting, Journal(str(tmp_path / "journal.jsonl")), repo="o/r", role="fix",
        runtime=runtime_name, state_dir=str(tmp_path / "state"),
    )
    workflow = SQLiteWorkflowControl(control, WorkflowWriter("o/r", "fix", owner), resolve_ceiling=3)
    handover_path = tmp_path / "state" / "handovers" / "issue-1.md"
    handover_path.parent.mkdir(parents=True)
    for index, task in enumerate(planned):
        handover = f"<!-- issue-agent-handover role=fix issue=1 -->\n{notes[index]}\n"
        handover_path.write_text(handover, encoding="utf-8")
        guidance = f"Repository context revision {index}"
        if repository_guidance:
            (worktree / "AGENTS.md").write_text(guidance, encoding="utf-8")
        runtime.set_context("fix", "AUTHOR", 1, 1, workflow=snapshot.binding)

        if index == 2:
            intent = workflow.reauthor_reset_intent(snapshot.binding, run_input_fingerprint(task.intent))
            assert intent is not None
            with workflow.contract_reset_authority(intent):
                ack = workflow.complete_contract_reset(ContractResetCompleted(
                    intent, ContractResetResult(intent.baseline.revision, intent.archive_ref), datetime.now(timezone.utc),
                ))
            assert isinstance(task.split_input, SplitContext)
            task = replace(task, split_input=ReauthorInput(ack, task.split_input))

        result = asyncio.run(runtime.run(task))

        if index == 1:
            assert result.workflow_attempt is None
            assert result.exit_reason is ExitReason.RUNTIME_UNAVAILABLE
            assert len(supplied) == 1
            current = workflow.load_workflow(snapshot.binding.fence.key)
            assert isinstance(current, WorkflowSnapshot)
            assert isinstance(current.episode.continuation, AuthoredResolve)
            assert current.episode.continuation.author_admission == admissions[0]
            with control._connect() as db:
                assert db.execute("SELECT count(*) FROM charges").fetchone()[0] == 1
            continue
        assert result.workflow_attempt is not None
        delivered = supplied[-1]
        encoded = delivered.prompt.split("<untrusted_handover_json>\n", 1)[1].split(
            "\n</untrusted_handover_json>", 1,
        )[0]
        assert json.loads(encoded) == handover
        expected_task = task.context_prefix + task.intent.prompt + task.context_suffix
        assert delivered.prompt.count(expected_task) == 1
        assert delivered.prompt.count("<untrusted_handover_json>") == 1
        assert delivered.prompt.count("Existing scratch context") == 1
        if repository_guidance:
            assert delivered.prompt.count(guidance) == 1
            assert delivered.prompt.index(guidance) > delivered.prompt.index(expected_task)
            assert guidance not in delivered.intent.prompt
        else:
            assert delivered.prompt.endswith(expected_task)
        assert task.context_prefix == "Existing scratch context\n\n"
        assert task.context_suffix == "\n\nExisting trailing context"
        assert not handover_path.exists()
        returned = workflow.returned_result(result.workflow_attempt)
        assert returned is not None
        observed_at = datetime.now(timezone.utc)
        key = result.workflow_attempt.binding.fence.key
        recorded = workflow.finalize_return(returned, ObservedIssue(
            key, issue(1, title="title", body="body", author="alice", updated_at="revision"), observed_at,
        ), NoLinkedPr(key, observed_at))
        assert isinstance(recorded, WorkflowCommitted)
        checkpoint = recorded.snapshot.episode.continuation
        assert isinstance(checkpoint, AuthoredResolve)
        assert checkpoint.author_admission == admissions[-1]

    assert len(supplied) == len(admissions) == 2
    assert supplied[0].prompt != supplied[1].prompt
    assert admissions[0].input_fingerprint != admissions[1].input_fingerprint
    assert original.intent.prompt == "Author the current issue contract"
