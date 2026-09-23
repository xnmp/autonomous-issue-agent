"""Scratch enrichment cannot change durable author-checkpoint applicability."""

import asyncio
from dataclasses import replace
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
from issue_agent.domain.outcome import AgentResult
from issue_agent.domain.pricing import BudgetPolicy, ModelRates, RateTable
from issue_agent.domain.workflow import (
    AuthoredResolve, ResolveAdmission, ResolveAttemptResult, ResolveResultKind,
    ResolveSession, ResolveStage,
)
from issue_agent.domain.workflow_identity import WorkflowWriter
from tests.fakes.legacy_journal import Journal
from tests.fakes.workflow_authority import install_workflow, resolve_snapshot


@pytest.mark.parametrize("authoritative_change", ["prompt", "system_instruction", "schema"])
@pytest.mark.parametrize("repository_guidance", [False, True])
def test_author_checkpoint_fingerprints_authoritative_inputs_not_delivered_handover(
    tmp_path, monkeypatch, authoritative_change, repository_guidance,
):
    control = SQLiteControlStore(str(tmp_path / "control.db"))
    control.initialize()
    snapshot = resolve_snapshot(1)
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
    original = RuntimePlan(
        "Author the current issue contract", cwd=str(worktree), model="test-model",
        append_system_prompt="Preserve public behavior", schema={"type": "object"},
        context_prefix="Existing scratch context\n\n", context_suffix="\n\nExisting trailing context", max_budget_usd=2,
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
    runtime_name = "codex" if repository_guidance else "claude"

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
            assert receipt.input_fingerprint == run_input_fingerprint(planned[index].intent)
            assert task.intent is planned[index].intent
            admissions.append(receipt)
            supplied.append(task)
            return AgentResult(False, status="ok", cost_usd=1, session_id=f"author-session-{index}")

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

        result = asyncio.run(runtime.run(task))

        assert result.workflow_attempt is not None
        delivered = supplied[index]
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
        recorded = workflow.record_attempt(ResolveAttemptResult(
            result.workflow_attempt, ResolveResultKind.STAGE_PROGRESSED, stage=ResolveStage.AUTHOR,
            session=ResolveSession(result.workflow_attempt, result.session_id, runtime_name, "test-model"),
        ))
        checkpoint = recorded.snapshot.episode.continuation
        assert isinstance(checkpoint, AuthoredResolve)
        assert checkpoint.author_admission == admissions[index]

    assert supplied[0].prompt != supplied[1].prompt
    assert admissions[0].input_fingerprint == admissions[1].input_fingerprint
    assert admissions[2].input_fingerprint != admissions[1].input_fingerprint
    assert original.intent.prompt == "Author the current issue contract"
