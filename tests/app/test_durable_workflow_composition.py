"""Bounded crash/interleaving cases through real daemon and durable authority.

Only provider/worktree/GitHub transport is simulated. Restart recreates the
daemon, executors, Store and SQLite clients over the same two independent
databases. These are controlled restart replays, not live deployment evidence.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import multiprocessing
import os
from pathlib import Path
import sqlite3
from threading import Barrier
from zoneinfo import ZoneInfo

import pytest

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters import accounting_runtime
from issue_agent.adapters.accounting_runtime import AccountingRuntime
from issue_agent.adapters.effects import GitHubEffectExecutor
from issue_agent.adapters.event_log import EventLog, read_event_log
from issue_agent.adapters.github import GhUnavailable
from issue_agent.adapters.store import Store
from issue_agent.app import roles
from issue_agent.app.dispatch import AgentDispatcher, DispatchConfig
from issue_agent.app.execute import Executor
from issue_agent.app.loop import Daemon
from issue_agent.app.planners.tasks import FixTaskConfig, TriageTaskConfig
from issue_agent.app.workflows.steward import StewardWorkflow, StewardWorkflowConfig
from issue_agent.domain.actions import AgentKind, PostComment, Relabel, RunAgent, Target
from issue_agent.domain.contracts import EffectStatus, Money, daily_budget_scope
from issue_agent.domain.fix import FixConfig, decide_fix
from issue_agent.domain.events import ControlTransitionCommitted, Producer, SpendRecorded
from issue_agent.domain.labels import Label
from issue_agent.domain.lifecycle import LifecycleOperation, completion_guard
from issue_agent.domain.outcome import AgentResult
from issue_agent.domain.pricing import BudgetPolicy
from issue_agent.domain.triage import TriageConfig
from tests.domain.builders import issue, pr
from tests.fakes.persistent_github import PersistentGitHub


class Crash(BaseException):
    """Process loss bypasses application exception/retry handlers."""


class Worktrees:
    def __init__(self, root: Path):
        self.root = root

    def ensure(self, number: int, branch: str) -> str:
        return str(self.root)

    def branch_for_issue(self, number: int, fallback: str, *, preferred: str = "") -> str:
        return preferred or fallback


class Provider:
    def __init__(self, after_run=lambda: None, result=None):
        self.after_run = after_run
        self.result = result or AgentResult(is_error=True, cost_usd=0.25, session_id="provider-receipt")
        self.tasks = []

    async def run(self, task):
        self.tasks.append(task)
        self.after_run()
        return self.result


@dataclass
class Composition:
    daemon: Daemon
    remote: PersistentGitHub
    control: SQLiteControlStore
    effects: GitHubEffectExecutor
    provider: Provider
    log: list[str]


def compose(root: Path, *, fault=lambda _: None, provider=None, decide=None, accounted=False, role="fix") -> Composition:
    remote = PersistentGitHub(root / "github.db", fault=fault)
    control = SQLiteControlStore(str(root / "control.db"))
    control.initialize()
    events = EventLog(str(root / "events.jsonl"), Producer("test", "o/r", role, os.getpid(), "test-boot"))
    control.bind_event_outbox(events.prepare, events.append_committed)
    state = Store(state_dir=str(root / role))
    messages = []
    effects = GitHubEffectExecutor(remote, control, repository="o/r", identity=f"o/r:{role}", log=messages.append)
    provider = provider or Provider()
    runtime = AccountingRuntime(
        provider, control, "o/r", "fix", Money("USD", 5_000_000), None, None,
        "test", "claude", BudgetPolicy({("fix", "RESOLVE"): Money("USD", 3_000_000)}),
    ) if accounted else provider
    executor = Executor(remote, effect_executor=effects, retry_delays=(), log=messages.append, remove_worktree=lambda _: None, clear_issue_state=state.cleanup_pr)
    dispatcher = AgentDispatcher(
        DispatchConfig(fix=FixTaskConfig("o/r", agent_md="test", model="test-model"), fix_bot="fix-bot", log=messages.append),
        remote, state, Worktrees(root), runtime, executor,
    )
    executor._run_agent = lambda action: asyncio.run(dispatcher.dispatch(action))
    policy = decide or (lambda world: decide_fix(world, FixConfig(fix_bot="fix-bot"), roles._fix_state(state, world), fixer_busy=False))
    return Composition(Daemon("fix", remote.build_world, policy, executor, log=messages.append), remote, control, effects, provider, messages)


def seed(root: Path, *labels: str) -> None:
    PersistentGitHub(root / "github.db").seed(issue(7, *labels, updated_at="2026-09-14T00:00:00Z"))


def fail_at(point: str):
    def fault(observed: str):
        if observed == point:
            raise Crash(point)
    return fault


def _terminated_daemon(root: Path, boundary: str) -> None:
    action = PostComment(Target.ISSUE, 7, "Survives process loss.")
    client = compose(root, decide=lambda _: [action])
    if boundary == "after_begin":
        begin = client.control.begin_command

        def interrupted_begin(command_id):
            begin(command_id)
            os._exit(71)

        client.control.begin_command = interrupted_begin
    else:
        client.control.record_effect = lambda _: os._exit(71)
    client.daemon.tick()


@pytest.mark.parametrize("boundary", ["after_begin", "before_receipt"])
def test_process_death_restarts_from_committed_command_without_duplicate(tmp_path, boundary):
    seed(tmp_path, "needs-human-review")
    process = multiprocessing.get_context("spawn").Process(target=_terminated_daemon, args=(tmp_path, boundary))
    process.start()
    try:
        process.join(timeout=10)
        assert process.exitcode == 71
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
    restarted = compose(tmp_path)
    pending = restarted.control.pending_commands()
    assert len(pending) == 1
    command_id = pending[0].command.command_id
    assert pending[0].state == "executing"
    for _ in range(3):
        restarted.daemon.tick()
    assert restarted.control.pending_commands() == ()
    assert restarted.control.effect_receipt(command_id).status is EffectStatus.SUCCEEDED
    assert restarted.remote.calls == [["comment", 7, "Survives process loss."]]
    assert restarted.provider.tasks == []


@pytest.mark.parametrize("point", ["comment_applied", "receipt_commit"])
def test_remote_success_before_receipt_reconciles_once_after_restart(tmp_path, monkeypatch, point):
    seed(tmp_path, "needs-human-review")
    action = PostComment(Target.ISSUE, 7, "Waiting for operator approval.")
    first = compose(tmp_path, fault=fail_at(point), decide=lambda _: [action])
    if point == "receipt_commit":
        monkeypatch.setattr(first.control, "record_effect", lambda _: (_ for _ in ()).throw(Crash(point)))
    with pytest.raises(Crash):
        first.daemon.tick()
    pending = first.control.pending_commands()
    assert len(pending) == 1
    command = pending[0].command
    assert pending[0].state in {"executing", "unknown_outcome"}
    restarted = compose(tmp_path, decide=lambda _: [action])
    for _ in range(3):
        restarted.daemon.tick()
    assert restarted.control.pending_commands() == ()
    assert restarted.control.effect_receipt(command.command_id).status is EffectStatus.SUCCEEDED
    assert restarted.remote.calls == [["comment", 7, action.body]]
    assert restarted.remote.get_issue(7).has(Label.NEEDS_HUMAN_REVIEW)
    assert restarted.provider.tasks == []


@pytest.mark.parametrize("boundary", ["INSERT", "UPDATE"])
def test_local_commit_failure_cannot_apply_remote_mutation(tmp_path, boundary):
    seed(tmp_path, "needs-human-review")
    action = PostComment(Target.ISSUE, 7, "Durable intent first.")
    first = compose(tmp_path, decide=lambda _: [action])
    with sqlite3.connect(first.control.path) as db:
        db.execute(f"CREATE TRIGGER injected_failure AFTER {boundary} ON lifecycle_commands BEGIN SELECT RAISE(ABORT, 'injected transaction failure'); END")
    first.daemon.tick()
    assert first.remote.calls == []
    assert any("injected transaction failure" in message for message in first.log)
    if boundary == "INSERT":
        assert first.control.pending_commands() == ()
        assert read_event_log(str(tmp_path / "events.jsonl")).events == ()
    else:
        assert first.control.pending_commands()[0].state == "pending"
    with sqlite3.connect(first.control.path) as db:
        db.execute("DROP TRIGGER injected_failure")
    restarted = compose(tmp_path, decide=lambda _: [action])
    for _ in range(3):
        restarted.daemon.tick()
    assert restarted.remote.calls == [["comment", 7, action.body]]
    assert restarted.control.pending_commands() == ()


def test_unknown_remote_result_remains_visible_and_does_not_repeat_write(tmp_path):
    seed(tmp_path, "needs-human-review")
    action = PostComment(Target.ISSUE, 7, "Operator evidence.")
    first = compose(tmp_path, fault=fail_at("comment_applied"), decide=lambda _: [action])
    with pytest.raises(Crash):
        first.daemon.tick()
    command = first.control.pending_commands()[0].command

    def unavailable(point):
        if point == "read":
            raise GhUnavailable("network", "readback unavailable")

    restarted = compose(tmp_path, fault=unavailable)
    for _ in range(3):
        restarted.daemon.tick()
    assert restarted.remote.calls == [["comment", 7, action.body]]
    assert restarted.control.pending_commands()[0].command.command_id == command.command_id
    assert restarted.control.effect_receipt(command.command_id).status is EffectStatus.UNKNOWN_OUTCOME
    assert any(
        isinstance(event.payload, ControlTransitionCommitted)
        and event.payload.entity_id == command.command_id
        and event.payload.state == "unknown_outcome"
        for event in read_event_log(str(tmp_path / "events.jsonl")).events
    )
    assert any("readback unavailable" in message for message in restarted.log)
    assert restarted.provider.tasks == []


def test_partial_relabel_preserves_hold_and_never_pays_after_restart(tmp_path):
    seed(tmp_path, "ai-ready")
    hold = Relabel(Target.ISSUE, 7, Label.AI_READY, Label.NEEDS_HUMAN_REVIEW)
    first = compose(tmp_path, fault=fail_at("label_added"), decide=lambda _: [hold])
    with pytest.raises(Crash):
        first.daemon.tick()
    assert first.remote.get_issue(7).labels == frozenset({Label.AI_READY, Label.NEEDS_HUMAN_REVIEW})
    restarted = compose(tmp_path)
    for _ in range(4):
        restarted.daemon.tick()
    assert restarted.remote.get_issue(7).has(Label.NEEDS_HUMAN_REVIEW)
    assert restarted.provider.tasks == []


def test_hold_added_during_resolve_completion_survives_restart(tmp_path):
    seed(tmp_path, "ai-ready")
    human = PersistentGitHub(tmp_path / "github.db")
    provider = Provider(lambda: human.relabel("issue", 7, None, "needs-human-review"))
    first = compose(tmp_path, provider=provider)
    first.daemon.tick()
    assert len(provider.tasks) == 1
    restarted = compose(tmp_path)
    for _ in range(4):
        restarted.daemon.tick()
    held = restarted.remote.get_issue(7)
    assert held.has(Label.NEEDS_HUMAN_REVIEW)
    assert not held.has(Label.AI_READY)
    assert restarted.provider.tasks == []


def test_concurrent_rearm_deliveries_do_not_erase_a_newer_hold(tmp_path):
    seed(tmp_path, "needs-human-review")
    revision = PersistentGitHub(tmp_path / "github.db").get_issue(7).updated_at
    rearm = Relabel(Target.ISSUE, 7, Label.NEEDS_HUMAN_REVIEW, Label.AI_READY, expected_revision=revision)
    barrier = Barrier(2)

    def deliver():
        client = compose(tmp_path, decide=lambda _: [rearm])
        barrier.wait(timeout=5)
        client.daemon.tick()

    with ThreadPoolExecutor(max_workers=2) as pool:
        deliveries = [pool.submit(deliver) for _ in range(2)]
        for delivery in deliveries:
            delivery.result(timeout=10)
    human = PersistentGitHub(tmp_path / "github.db")
    assert human.get_issue(7).has(Label.AI_READY)
    human.relabel("issue", 7, "ai-ready", "needs-human-review")
    delayed = compose(tmp_path, decide=lambda _: [rearm])
    delayed.daemon.tick()
    restarted = compose(tmp_path)
    for _ in range(3):
        restarted.daemon.tick()
    assert restarted.remote.get_issue(7).labels == frozenset({Label.NEEDS_HUMAN_REVIEW})
    assert restarted.provider.tasks == []


def test_paid_attempt_settlement_is_conserved_when_completion_receipt_is_lost(tmp_path, monkeypatch):
    seed(tmp_path, "ai-ready")
    # The external worktree capability is controlled; the accounting wrapper,
    # reserve/settle transaction and workflow are the production composition.
    monkeypatch.setattr(accounting_runtime, "_fix_publication_context", lambda role, cwd: accounting_runtime._FixPublicationContext(str(tmp_path), "fix/7-test", "/usr/bin/git", "/usr/bin/gh", "/usr/bin:/bin"))
    provider = Provider(result=AgentResult(is_error=False, status="blocked", needs="Which API version?", cost_usd=0.25, session_id="provider-receipt"))
    completed_labels = 0

    def crash_after_completion(point):
        nonlocal completed_labels
        if point == "relabel_applied":
            completed_labels += 1
            if completed_labels == 2:
                raise Crash("completion applied before receipt")

    first = compose(tmp_path, provider=provider, accounted=True, fault=crash_after_completion)
    with pytest.raises(Crash):
        first.daemon.tick()
    assert len(provider.tasks) == 1
    run_id = provider.tasks[0].env["ISSUE_AGENT_RUN_ID"]
    assert run_id
    scope = daily_budget_scope("o/r", "repository", "USD", datetime.now(timezone.utc), ZoneInfo("UTC"))
    before = first.control.snapshot(scope)
    assert before.settled == Money("USD", 250_000)
    assert before.reserved == Money.zero("USD")
    assert first.control.active_reservation_runs() == ()

    command_id = first.control.pending_commands()[0].command.command_id
    restarted = compose(tmp_path, accounted=True)
    for _ in range(4):
        restarted.daemon.tick()
    assert restarted.provider.tasks == []
    assert restarted.control.snapshot(scope) == before
    assert restarted.control.active_reservation_runs() == ()
    assert restarted.control.effect_receipt(command_id).status is EffectStatus.SUCCEEDED
    assert restarted.remote.get_issue(7).labels == frozenset({Label.AI_BLOCKED})
    assert len([call for call in restarted.remote.calls if call[0] == "comment"]) == 1
    assert len([call for call in restarted.remote.calls if call[0] == "relabel"]) == 2
    with sqlite3.connect(restarted.control.path) as db:
        assert db.execute(
            "SELECT runs.run_id, runs.state, charges.run_id, "
            "charges.provider_receipt_id, charges.amount_micros "
            "FROM runs JOIN charges ON charges.run_id = runs.run_id"
        ).fetchall() == [(
            run_id, "completed", run_id,
            f"claude:provider-receipt:{run_id}", 250_000,
        )]
    settlements = [
        event for event in read_event_log(str(tmp_path / "events.jsonl")).events
        if isinstance(event.payload, SpendRecorded)
    ]
    assert len(settlements) == 1
    assert settlements[0].payload.run_id == run_id
    assert settlements[0].correlation.run_id == run_id
    assert settlements[0].correlation.issue == 7
    assert settlements[0].payload.amount_micros == 250_000


@pytest.mark.parametrize("human_hold", [False, True], ids=["repair-own-add", "preserve-new-hold"])
def test_guarded_partial_relabel_recovery_preserves_completion_authority(tmp_path, human_hold):
    seed(tmp_path, "ai-working")
    observed = PersistentGitHub(tmp_path / "github.db").get_issue(7)
    action = Relabel(
        Target.ISSUE, 7, Label.AI_WORKING, Label.AI_READY,
        expected_revision=observed.updated_at,
        completion_guard=completion_guard(observed, LifecycleOperation.COMPLETE_RESOLVE),
    )
    first = compose(tmp_path, fault=fail_at("label_added"), decide=lambda _: [action])
    with pytest.raises(Crash):
        first.daemon.tick()
    command_id = first.control.pending_commands()[0].command.command_id
    human = PersistentGitHub(tmp_path / "github.db")
    assert human.get_issue(7).labels == frozenset({Label.AI_WORKING, Label.AI_READY})
    if human_hold:
        human.relabel("issue", 7, None, "needs-human-review")
    # Exercise replay before new admission; the next fresh attempt is a separate
    # scheduler decision, not a recovery of the interrupted remote mutation.
    restarted = compose(tmp_path, decide=lambda _: [])
    for _ in range(3):
        restarted.daemon.tick()
    current = restarted.remote.get_issue(7)
    receipt = restarted.control.effect_receipt(command_id)
    if human_hold:
        assert current.has(Label.NEEDS_HUMAN_REVIEW)
        assert receipt.status is not EffectStatus.SUCCEEDED
    else:
        assert current.labels == frozenset({Label.AI_READY})
        assert receipt.status is EffectStatus.SUCCEEDED
    assert restarted.provider.tasks == []


@pytest.mark.parametrize("intervention", ["none", "hold", "reopen"], ids=["ordinary-merge", "hold-between-effects", "reopen-between-effects"])
def test_observed_merged_pr_cleanup_finishes_close_unless_human_intervenes(tmp_path, intervention):
    seed(tmp_path, "ai-pr-open")
    human = PersistentGitHub(tmp_path / "github.db")
    human.seed_pr(pr(70, "ai-pr-open", state="MERGED", closing_issue=7, head_ref_oid="merged-head"))
    interposed = False

    def hold_after_done(point):
        nonlocal interposed
        if intervention != "none" and point == "relabel_applied" and not interposed:
            interposed = True
            if intervention == "hold":
                human.relabel("issue", 7, None, "needs-human-review")
            else:
                human.reopen_issue(7)

    client = compose(tmp_path, fault=hold_after_done)
    client.daemon.tick()
    restarted = compose(tmp_path)
    for _ in range(3):
        restarted.daemon.tick()
    final = restarted.remote.get_issue(7)
    assert final.has(Label.AI_DONE)
    assert not final.has(Label.AI_PR_OPEN)
    assert not restarted.remote.get_pr(70).has(Label.AI_PR_OPEN)
    if intervention != "none":
        assert interposed
        assert final.state == "open"
        if intervention == "hold":
            assert final.has(Label.NEEDS_HUMAN_REVIEW)
        else:
            assert final.state_reason == "reopened"
        assert not any(call[0] == "close" for call in human.calls)
    else:
        assert final.state == "closed"
        assert final.state_reason == "completed"
        assert len([call for call in human.calls if call[0] == "close"]) == 1
    assert client.provider.tasks == restarted.provider.tasks == []


def test_triage_promotion_finishes_advisory_recurrence_after_own_transition(tmp_path):
    seed(tmp_path)
    remote = PersistentGitHub(tmp_path / "github.db")
    remote.seed_pr(pr(300, state="MERGED", closing_issue=42, title="Original defect"))
    provider = Provider(result=AgentResult(is_error=False, structured={
        "verdict": "ready", "plain_summary": "clear", "acceptance_criteria": ["works"],
        "creator_questions": [], "owner_questions": [], "blocked_by_issues": [],
        "recurrence_of": 42,
    }))
    client = compose(tmp_path, role="steward", provider=provider)
    state = Store(state_dir=str(tmp_path / "steward"))
    workflow = StewardWorkflow(
        StewardWorkflowConfig(
            triage=TriageTaskConfig("steward-bot", triage_prompt="test"),
            triage_cfg=TriageConfig("steward-bot", enabled=True, promote=True),
            log=client.log.append, fix_bot="fix-bot", repo="o/r",
        ),
        client.remote, state, provider, client.daemon.executor,
    )
    client.daemon.executor._run_agent = lambda action: asyncio.run(workflow.triage(action.number))
    client.daemon.decide = lambda _: [RunAgent(AgentKind.TRIAGE, 7)]
    client.daemon.tick()
    after = client.remote.get_issue(7)
    assert after.has(Label.AI_READY)
    assert after.has(Label.RECURRENCE)
    assert any("#42" in comment.body and "PR #300" in comment.body for comment in after.comments)
    assert state.get_triage_at(7) == after.updated_at
    restarted = compose(tmp_path, role="steward", decide=lambda _: [])
    restarted.daemon.tick()
    assert restarted.control.pending_commands() == ()
    assert len(provider.tasks) == 1
    assert restarted.remote.get_issue(7) == after


def test_changed_issue_body_cannot_apply_obsolete_blocked_answer_or_reset_allowance(tmp_path, monkeypatch):
    seed(tmp_path, "ai-ready")
    monkeypatch.setattr(accounting_runtime, "_fix_publication_context", lambda role, cwd: accounting_runtime._FixPublicationContext(str(tmp_path), "fix/7-test", "/usr/bin/git", "/usr/bin/gh", "/usr/bin:/bin"))
    human = PersistentGitHub(tmp_path / "github.db")
    human.edit_issue_body(7, "Which API version should be used?")
    changes = 0

    def answer_during_run():
        nonlocal changes
        changes += 1
        human.edit_issue_body(7, f"Use API version 2; ambiguity resolved. Clarification {changes}.")

    provider = Provider(
        answer_during_run,
        AgentResult(is_error=False, status="blocked", needs="Which API version?", cost_usd=0.25, session_id="obsolete-verdict"),
    )
    client = compose(tmp_path, provider=provider, accounted=True)
    client.daemon.tick()
    current = human.get_issue(7)
    assert len(provider.tasks) == 1
    assert not current.has(Label.AI_BLOCKED)
    assert not any("Which API version?" in comment.body for comment in current.comments)
    assert current.has(Label.AI_READY) or current.has(Label.AI_WORKING)
    allowance_used = Store(state_dir=str(tmp_path / "fix")).get_resolve_retries(7)
    assert allowance_used >= 1
    restarted = compose(tmp_path, accounted=True, decide=lambda _: [])
    restarted.daemon.tick()
    assert Store(state_dir=str(tmp_path / "fix")).get_resolve_retries(7) == allowance_used
    scope = daily_budget_scope("o/r", "repository", "USD", datetime.now(timezone.utc), ZoneInfo("UTC"))
    assert restarted.control.snapshot(scope).settled == Money("USD", 250_000)
    assert restarted.control.snapshot(scope).reserved == Money.zero("USD")
    resumed = compose(tmp_path, provider=provider, accounted=True)
    for _ in range(6):
        resumed.daemon.tick()
    assert len(provider.tasks) == FixConfig(fix_bot="fix-bot").max_resolve_retries
    assert human.get_issue(7).has(Label.NEEDS_HUMAN_REVIEW)
    assert not human.get_issue(7).has(Label.AI_BLOCKED)
    assert resumed.control.snapshot(scope).settled == Money("USD", 250_000 * len(provider.tasks))
    assert resumed.control.snapshot(scope).reserved == Money.zero("USD")
