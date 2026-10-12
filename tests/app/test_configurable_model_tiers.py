"""Configured routes govern actual purchases and retain transcript provenance."""

from tests.domain.builders import observed_pr
import asyncio
from dataclasses import replace

import pytest

from issue_agent.app.config import from_env
from issue_agent.app.planners.tasks import FixTaskConfig
from issue_agent.domain.agent_task import RuntimeName
from issue_agent.domain.effort import Effort
from issue_agent.domain.labels import Label
from issue_agent.domain.model_routing import parse_model_route
from issue_agent.domain.outcome import AgentResult
from issue_agent.domain.world import Comment, IssueView, PRView, Check
from tests.app.test_dispatch import FakeGateway, FakeStore, SeqRunner, FakeRunner
from tests.app.test_fix_workflow import _workflow
from tests.fakes.accounted_workflow import resolve_state, seed_resolve, pr_repair_state

ENV = {
    "REPO": "o/r", "FIXER_RUNTIME": "codex", "FIX_MODEL": "",
    "FIX_MODEL_LOW": "codex:gpt-6-luna",
    "FIX_MODEL_MED": "codex:gpt-6.1-sol",
    "FIX_MODEL_HIGH": "claude:claude-opus-5-5",
    "FIX_MODEL_XHIGH": "codex:gpt-6-astra",
}


def configured_workflow(gateway, store, runner, **overrides):
    config = from_env(ENV)
    kwargs = dict(fix=FixTaskConfig("o/r"), fixer_runtime="codex",
                  fix_model_routes=config.fix_model_routes, available_fix_runtimes=config.fix_runtimes)
    kwargs.update(overrides)
    return _workflow(gateway, store, runner, **kwargs)


@pytest.mark.parametrize("tier,runtime,model", [
    ("trivial", RuntimeName.CODEX, "gpt-6-luna"),
    ("standard", RuntimeName.CODEX, "gpt-6.1-sol"),
    ("complex", RuntimeName.CLAUDE, "claude-opus-5-5"),
    ("exceptional", RuntimeName.CODEX, "gpt-6-astra"),
    ("invalid", RuntimeName.CODEX, "gpt-6.1-sol"),
])
def test_full_resolve_purchases_configured_model_and_preserves_provider(tier, runtime, model):
    gateway = FakeGateway(issues={5: IssueView(5, "implement")})
    store = FakeStore()
    runner = SeqRunner([AgentResult(False, structured={"tier": tier}),
                        AgentResult(False, status="blocked", session_id="paid-transcript", needs="context")])
    asyncio.run(configured_workflow(gateway, store, runner).resolve(5))
    probe, implementation = runner.tasks
    assert (probe.runtime, probe.model) == (RuntimeName.CODEX, "gpt-6-luna")
    assert (implementation.runtime, implementation.model) == (runtime, model)
    session = resolve_state(store).episode.continuation.session
    assert (session.runtime, session.model, session.session_id) == (runtime.value, model, "paid-transcript")


@pytest.mark.parametrize("value", ["opus", "unknown:foo", "codex:", "codex:claude-opus-5-5", "claude:gpt-6-astra", "codex:has space", "codex:" + "x" * 201])
def test_malformed_or_cross_provider_configuration_fails_loudly(value):
    config = from_env({**ENV, "FIX_MODEL_HIGH": value})
    assert any("FIX_MODEL_HIGH" in error for error in config.validation_errors())
    with pytest.raises(ValueError):
        config.fix_model_routes


def test_blank_overrides_preserve_defaults_and_configuration_identity_changes():
    legacy = from_env({"REPO": "o/r"})
    assert legacy.fix_model_routes == ()
    assert legacy.fix_runtimes == frozenset({RuntimeName.CLAUDE})
    assert from_env(ENV).config_version != from_env({**ENV, "FIX_MODEL_MED": "codex:gpt-6-astra"}).config_version


def test_router_transport_failure_uses_configured_medium_route():
    class Runner:
        def __init__(self):
            self.tasks = []
        async def run(self, task):
            self.tasks.append(task)
            if len(self.tasks) == 1:
                raise RuntimeError("probe unavailable")
            return AgentResult(False, status="blocked", needs="context", session_id="fallback")
    runner = Runner()
    asyncio.run(configured_workflow(FakeGateway(issues={5: IssueView(5)}), FakeStore(), runner).resolve(5))
    assert (runner.tasks[1].runtime, runner.tasks[1].model) == (RuntimeName.CODEX, "gpt-6.1-sol")


def test_pinned_model_skips_probe_and_retains_its_runtime():
    runner = FakeRunner(AgentResult(False, status="blocked", needs="context"))
    asyncio.run(configured_workflow(FakeGateway(issues={5: IssueView(5)}), FakeStore(), runner,
        fix=FixTaskConfig("o/r", model="gpt-6.1-sol", runtime=RuntimeName.CODEX)).resolve(5))
    assert len(runner.tasks) == 1
    assert (runner.tasks[0].runtime, runner.tasks[0].model) == (RuntimeName.CODEX, "gpt-6.1-sol")


def test_hard_evidence_uses_configured_exceptional_ceiling():
    runner = SeqRunner([AgentResult(False, structured={"tier": "complex"}), AgentResult(False, status="blocked")])
    asyncio.run(configured_workflow(FakeGateway(issues={5: IssueView(5, labels=frozenset({Label.RECURRENCE}))}), FakeStore(), runner).resolve(5))
    assert (runner.tasks[1].runtime, runner.tasks[1].model, runner.tasks[1].effort) == (RuntimeName.CODEX, "gpt-6-astra", Effort.HIGH)


def test_resume_after_tier_reassignment_keeps_claude_transcript():
    store = FakeStore()
    seed_resolve(store, 5, session_id="claude-session", runtime="claude", model="claude-opus-5-5")
    gateway = FakeGateway(issues={5: IssueView(5, comments=(Comment("owner", "Use SQL", "2026-10-06T00:00:00Z"),))})
    runner = FakeRunner(AgentResult(False, status="blocked", session_id="claude-session", needs="more context"))
    workflow = configured_workflow(gateway, store, runner,
        fix_model_routes=tuple(parse_model_route(tier, "codex:gpt-6.1-sol") for tier in ("trivial", "standard", "complex", "exceptional")))
    asyncio.run(workflow.resume(5))
    task = runner.tasks[0]
    assert (task.runtime, task.model, task.resume) == (RuntimeName.CLAUDE, "claude-opus-5-5", "claude-session")


def test_pr_repair_handoff_and_followup_keep_original_provider():
    store = FakeStore()
    gateway = FakeGateway(issues={5: IssueView(5)})
    class OpeningRunner(SeqRunner):
        async def run(self, task):
            result = await super().run(task)
            if task.model == "claude-opus-5-5":
                gateway.prs[100] = observed_pr(100, closing_issue=5,
                    head_ref_name="fix/5-issue", head_ref_oid="a" * 40, checks=(Check("unit", bucket="fail"),))
                gateway._pr_for_issue[5] = 100
            return result
    initial = OpeningRunner([AgentResult(False, structured={"tier": "complex"}),
                             AgentResult(False, status="pr_opened", session_id="claude-session")])
    asyncio.run(configured_workflow(gateway, store, initial).resolve(5))
    runner = FakeRunner(AgentResult(False, status="pushed", session_id="claude-repaired"))
    workflow = configured_workflow(gateway, store, runner)
    asyncio.run(workflow.fix_ci(100))
    assert (runner.tasks[0].runtime, runner.tasks[0].model, runner.tasks[0].resume) == (RuntimeName.CLAUDE, "claude-opus-5-5", "claude-session")
    assert pr_repair_state(store).session.runtime is RuntimeName.CLAUDE
    runner.tasks.clear()
    workflow.cfg = replace(workflow.cfg, fix_model_routes=tuple(parse_model_route(tier, "codex:gpt-6.1-sol") for tier in ("trivial", "standard", "complex", "exceptional")))
    asyncio.run(workflow.fix_ci(100))
    assert (runner.tasks[0].runtime, runner.tasks[0].resume) == (RuntimeName.CLAUDE, "claude-repaired")


def test_split_author_and_implement_share_one_configured_provider_choice():
    runner = SeqRunner([AgentResult(False, structured={"tier": "complex"}),
                        AgentResult(False, status="tests_authored", session_id="authored"),
                        AgentResult(False, status="pr_opened", session_id="implemented")])
    asyncio.run(configured_workflow(FakeGateway(issues={5: IssueView(5)}), FakeStore(), runner,
        author_tests_split=True).resolve(5))
    assert len(runner.tasks) == 3
    assert [(task.runtime, task.model) for task in runner.tasks[1:]] == [
        (RuntimeName.CLAUDE, "claude-opus-5-5"), (RuntimeName.CLAUDE, "claude-opus-5-5")]


@pytest.mark.parametrize("value", [None, 1, [], {}])
def test_external_route_parser_rejects_non_text(value):
    with pytest.raises(ValueError, match="text"):
        parse_model_route("standard", value)


@pytest.mark.parametrize("tier,provider,model", [
    ("trivial", RuntimeName.CODEX, "gpt-6-luna"),
    ("standard", RuntimeName.CODEX, "gpt-6.1-sol"),
    ("complex", RuntimeName.CLAUDE, "claude-opus-5-5"),
    ("exceptional", RuntimeName.CODEX, "gpt-6-astra"),
])
def test_production_composition_dispatches_to_distinct_provider_and_sql_authority(tmp_path, monkeypatch, tier, provider, model):
    from issue_agent.app import roles
    from issue_agent.adapters.store import Store
    from issue_agent.domain.workflow_identity import IssueRef, WorkflowKey
    from tests.app.resolve_acceptance_harness import (
        create_deployment, isolate_authority, patch_publication_environment, seed_episode, ScriptedProvider,
    )
    deployment = create_deployment(tmp_path)
    isolate_authority(deployment, monkeypatch)
    patch_publication_environment(monkeypatch)
    seed_episode(deployment, 0, Label.AI_READY.value)
    cfg = from_env({**deployment.env(), **ENV})
    codex = ScriptedProvider([AgentResult(False, structured={"tier": tier}, session_id="probe",
        usage={"input_tokens": 100, "output_tokens": 10, "source": "rollout"}),
        AgentResult(False, status="blocked", session_id="codex-implementation", needs="context",
                    usage={"input_tokens": 1000, "output_tokens": 10, "source": "rollout"})])
    claude = ScriptedProvider([AgentResult(False, status="blocked", session_id="claude-implementation",
        needs="context", cost_usd=0.2)])
    monkeypatch.setitem(roles._FIX_RUNTIME_REGISTRY, RuntimeName.CODEX,
        lambda *_args: roles._FixRuntimeBinding(codex))
    monkeypatch.setitem(roles._FIX_RUNTIME_REGISTRY, RuntimeName.CLAUDE,
        lambda *_args: roles._FixRuntimeBinding(claude))
    remote = deployment.github()
    logs = []
    composed = roles.compose_fix_daemon(cfg, remote, Store(cfg.state_dir, runtime="codex"),
                                       deployment.worktrees(), claude, log=logs.append)
    composed.daemon.tick()
    composed.daemon.wait_for_dispatches()
    tasks = codex.tasks + claude.tasks
    selected = [(task.runtime, task.model) for task in tasks if task.model == model]
    assert selected, logs
    assert selected[-1] == (provider, model)
    assert len(codex.tasks) == (1 if provider is RuntimeName.CLAUDE else 2), "\n".join(logs)
    assert len(claude.tasks) == (1 if provider is RuntimeName.CLAUDE else 0)
    session = composed.workflow_control.load_workflow(WorkflowKey("o/r", IssueRef(1))).episode.continuation.session
    assert (session.runtime, session.model) == (provider.value, model)
    assert remote.get_issue(1).labels == frozenset({Label.AI_BLOCKED})
    assert composed.control.active_reservation_runs() == ()


def test_cli_doctor_checks_codex_selected_only_by_a_tier(monkeypatch):
    from issue_agent.app import cli
    marker = object()
    selected = []
    def report(binary):
        selected.append(binary)
        return marker
    monkeypatch.setattr(cli, "report_codex_resolution", report)
    assert cli._doctor_codex_binary({"FIXER_RUNTIME": "claude", "REVIEWER_RUNTIME": "claude",
        "FIX_MODEL_MED": "codex:gpt-6.1-sol"}) is marker
    assert selected == ["codex"]
