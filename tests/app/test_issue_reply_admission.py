"""Issue replies buy paid work only through the selected SQL continuation."""
from tests.fakes.workflow_authority import retained_issue_file, seed_retained_issue_file


import asyncio
import pytest
from dataclasses import replace

from issue_agent.adapters.control_store import AccountingDisposition, ReservationResult
from issue_agent.domain.contracts import Money
from issue_agent.domain.labels import Label
from tests.app.test_fix_workflow import _workflow
from tests.fakes.accounted_workflow import resolve_state
from test_dispatch import FakeGateway, FakeStore, FakeRunner
from issue_agent.domain.world import Comment, IssueView
from issue_agent.domain.outcome import AgentResult

REPLY_AT = "2026-10-01T00:00:00Z"


def setup_reply(body="answer"):
    gateway = FakeGateway(issues={5: IssueView(5, title="title", body="body", comments=(Comment("alice", body, REPLY_AT),))}, pr_for_issue={})
    store = FakeStore()
    runner = FakeRunner(AgentResult(is_error=False, status="blocked"))
    workflow = _workflow(gateway, store, runner, fix_bot="fix-bot")
    return workflow, gateway, store, runner


def test_budget_refusal_preserves_reply_count_branch_and_legacy_inputs(monkeypatch):
    workflow, _, store, runner = setup_reply()
    before = resolve_state(store).issue_recovery.state
    def refused(request):
        return ReservationResult(AccountingDisposition.EXHAUSTED, None, Money("USD", 0), Money("USD", 1), Money("USD", 0), "test cap")
    monkeypatch.setattr(store._workflow_db, "reserve", refused)
    asyncio.run(workflow.resume(5))
    assert runner.tasks == []
    assert resolve_state(store).issue_recovery.state == before
    assert retained_issue_file(store, 5, "resumed-at") == ""


def test_success_consumes_exact_reply_once_and_preserves_legacy_files():
    workflow, _, store, runner = setup_reply()
    before = resolve_state(store)
    asyncio.run(workflow.resume(5))
    after = resolve_state(store)
    assert len(runner.tasks) == 1
    assert after.issue_recovery.state.consumed_reply_at == REPLY_AT
    assert after.issue_recovery.state.resumes == 1
    assert after.issue_recovery.state.remembered_branch == "fix/5-title"
    assert after.episode.failures == before.episode.failures
    assert retained_issue_file(store, 5, "resumed-at") == "" and retained_issue_file(store, 5, "branch") == ""
    asyncio.run(workflow.resume(5))
    assert len(runner.tasks) == 1
    with store._workflow_db._connect() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM budget_reservations").fetchone()[0] == 1
        metadata = __import__("json").loads(db.execute("SELECT dispatch_id FROM runs").fetchone()[0])
        assert REPLY_AT in metadata["issue_reply"] and "answer" in metadata["issue_reply"]


def test_blank_reply_consumes_without_reservation_and_replays_reblock():
    workflow, gateway, store, runner = setup_reply("  ")
    asyncio.run(workflow.resume(5))
    assert runner.tasks == []
    after = resolve_state(store).issue_recovery.state
    assert after.consumed_reply_at == REPLY_AT and after.resumes == 0 and after.remembered_branch == ""
    assert ("relabel", "issue", 5, "ai-working", "ai-blocked") in gateway.calls
    with store._workflow_db._connect() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM budget_reservations").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM lifecycle_commands").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM dedupe_keys WHERE namespace='issue-reply-noop'").fetchone()[0] == 1
    asyncio.run(workflow.resume(5))
    with store._workflow_db._connect() as db:
        assert db.execute("SELECT count(*) FROM lifecycle_commands").fetchone()[0] == 1


def test_older_observation_cannot_erase_later_consumption():
    from issue_agent.domain.world import Comment
    workflow, gateway, store, runner = setup_reply()
    asyncio.run(workflow.resume(5))
    first = resolve_state(store).issue_recovery.state
    gateway.issues[5] = replace(gateway.issues[5], comments=(Comment("alice", "older", "2026-09-30T00:00:00Z"),))
    asyncio.run(workflow.resume(5))
    assert len(runner.tasks) == 1 and resolve_state(store).issue_recovery.state == first
    gateway.issues[5] = replace(gateway.issues[5], comments=(Comment("alice", "newer", "2026-10-02T00:00:00Z"),))
    asyncio.run(workflow.resume(5))
    assert len(runner.tasks) == 2
    assert resolve_state(store).issue_recovery.state.consumed_reply_at == "2026-10-02T00:00:00Z"


def test_linked_pr_issue_reply_consumes_timestamp_without_no_pr_count_or_branch():
    from issue_agent.domain.world import PRView
    from issue_agent.adapters.workflow_codec import decode_workflow
    from issue_agent.domain import continuation_components as cc
    workflow, gateway, store, runner = setup_reply()
    gateway.prs[100] = PRView(100, head_ref_name="fix/5-title", head_ref_oid="head", closing_issue=5)
    gateway._pr_for_issue[5] = 100
    from tests.fakes.accounted_workflow import attach_workflow_authority
    attach_workflow_authority(gateway, store, pr_repair=True)
    before = resolve_state(store)
    asyncio.run(workflow.resume(5))
    after = resolve_state(store)
    assert len(runner.tasks) == 1
    assert after.issue_recovery.state.consumed_reply_at == REPLY_AT
    assert after.issue_recovery.state.resumes == 0 and after.issue_recovery.state.remembered_branch == ""
    assert after.episode == before.episode
    asyncio.run(workflow.resume(5))
    assert len(runner.tasks) == 1 and retained_issue_file(store, 5, "resumed-at") == ""
    with store._workflow_db._connect() as db:
        row = db.execute("SELECT workflow_binding_json,continuations_json FROM runs").fetchone()
        from issue_agent.domain.workflow_identity import WorkflowBinding
        assert decode_workflow(row[0], WorkflowBinding).authority.family.value == "pr_repair"
        assert tuple(type(purchase) for purchase in decode_workflow(row[1], tuple)) == (cc.PRRepairPurchase, cc.IssueReplyPurchase)


def test_linked_pr_budget_refusal_leaves_issue_reply_available(monkeypatch):
    from issue_agent.domain.world import PRView
    workflow, gateway, store, runner = setup_reply()
    gateway.prs[100] = PRView(100, head_ref_name="fix/5-title", head_ref_oid="head", closing_issue=5)
    gateway._pr_for_issue[5] = 100
    from tests.fakes.accounted_workflow import attach_workflow_authority
    attach_workflow_authority(gateway, store, pr_repair=True)
    before = resolve_state(store).issue_recovery.state
    monkeypatch.setattr(store._workflow_db, "reserve", lambda request: ReservationResult(AccountingDisposition.EXHAUSTED, None, Money("USD", 0), Money("USD", 1), Money("USD", 0), "test cap"))
    asyncio.run(workflow.resume(5))
    assert runner.tasks == [] and resolve_state(store).issue_recovery.state == before
    assert retained_issue_file(store, 5, "resumed-at") == ""


def test_blank_committed_projection_recovers_after_process_loss(monkeypatch):
    import pytest
    workflow, gateway, store, runner = setup_reply("  ")
    replay = workflow.executor.replay_pending
    monkeypatch.setattr(workflow.executor, "replay_pending", lambda: (_ for _ in ()).throw(RuntimeError("parent lost")))
    with pytest.raises(RuntimeError, match="parent lost"):
        asyncio.run(workflow.resume(5))
    assert resolve_state(store).issue_recovery.state.consumed_reply_at == REPLY_AT
    assert not gateway.calls
    monkeypatch.setattr(workflow.executor, "replay_pending", replay)
    asyncio.run(workflow.resume(5))
    assert ("relabel", "issue", 5, "ai-working", "ai-blocked") in gateway.calls
    assert runner.tasks == []
    with store._workflow_db._connect() as db:
        assert db.execute("SELECT count(*) FROM lifecycle_commands").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM budget_reservations").fetchone()[0] == 0


def test_split_reply_admission_consumes_once():
    workflow, gateway, store, runner = setup_reply()
    workflow.cfg = replace(workflow.cfg, author_tests_split=True)
    gateway.issues[5] = replace(gateway.issues[5], labels=frozenset({Label.AI_BLOCKED}))
    asyncio.run(workflow.resume(5))
    first = resolve_state(store)
    asyncio.run(workflow.resume(5))
    assert first.issue_recovery.state.consumed_reply_at == REPLY_AT
    assert len(runner.tasks) == 1


def test_split_same_body_new_timestamp_consumed():
    from test_dispatch import SeqRunner
    from issue_agent.domain.outcome import AgentResult
    from issue_agent.domain.world import Comment
    workflow, gateway, store, _ = setup_reply()
    workflow.cfg = replace(workflow.cfg, author_tests_split=True)
    gateway.issues[5] = replace(gateway.issues[5], labels=frozenset({Label.AI_BLOCKED}))
    runner = SeqRunner([
        AgentResult(is_error=False, status='tests_authored', session_id='a'),
        AgentResult(is_error=False, status='blocked'),
        AgentResult(is_error=False, status='blocked'),
        AgentResult(is_error=False, status='blocked'),
    ])
    workflow.runner = runner
    asyncio.run(workflow.resume(5))
    gateway.issues[5] = replace(gateway.issues[5], comments=(*gateway.issues[5].comments, Comment('alice', 'answer', '2026-10-02T00:00:00Z')))
    asyncio.run(workflow.resume(5))
    assert resolve_state(store).issue_recovery.state.consumed_reply_at == '2026-10-02T00:00:00Z'


def test_blank_retry_requires_selected_projection_command(monkeypatch):
    workflow, gateway, store, _ = setup_reply('  ')
    gateway.issues[5] = replace(gateway.issues[5], labels=frozenset({Label.AI_WORKING}))
    replay = workflow.executor.replay_pending
    monkeypatch.setattr(workflow.executor, 'replay_pending', lambda: (_ for _ in ()).throw(RuntimeError('lost')))
    with pytest.raises(RuntimeError):
        asyncio.run(workflow.resume(5))
    with store._workflow_db.transaction() as db:
        db.execute('DELETE FROM lifecycle_commands')
    monkeypatch.setattr(workflow.executor, 'replay_pending', replay)
    with pytest.raises(ValueError):
        asyncio.run(workflow.resume(5))



def test_blank_command_insert_failure_rolls_back_consumption(monkeypatch):
    workflow, _, store, _ = setup_reply("  ")
    before = resolve_state(store)
    enqueue = store._workflow_db._enqueue_command
    def failed(db, command):
        enqueue(db, command)
        raise ValueError("command publication failed")
    monkeypatch.setattr(store._workflow_db, "_enqueue_command", failed)
    with pytest.raises(ValueError, match="command publication failed"):
        asyncio.run(workflow.resume(5))
    assert resolve_state(store) == before
    with store._workflow_db._connect() as db:
        assert db.execute("SELECT count(*) FROM lifecycle_commands").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM dedupe_keys WHERE namespace='issue-reply-noop'").fetchone()[0] == 0


def test_linked_pr_actual_issue_context_cannot_consume_another_issue_reply():
    import json
    from issue_agent.domain import continuation_components as cc
    from tests.adapters.test_continuation_admission import purchase, paid
    workflow, _, store, _ = setup_reply()
    captured = purchase(resolve_state(store), reply=REPLY_AT)
    selected = cc.IssueReplyPurchase(captured.capture, captured.reply_at)
    request = replace(paid("wrong-issue", captured), workflow=None, kind="PR_RESUME", continuations=(selected,),
                      dispatch_id=json.dumps({"repository":"o/r","role":"fix","kind":"PR_RESUME","number":100,"issue":99}))
    before = resolve_state(store)
    result = store._workflow_db.reserve(request)
    assert not result.admitted and resolve_state(store) == before
    with store._workflow_db._connect() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM budget_reservations").fetchone()[0] == 0


def test_linked_purchase_timestamp_matches_actual_run_input():
    import json
    from issue_agent.domain import continuation_components as cc
    from tests.adapters.test_workflow_admission import request
    from issue_agent.adapters.workflow_codec import encode_workflow
    workflow, gateway, store, runner = setup_reply()
    current = resolve_state(store)
    selected = cc.select_issue_recovery(current)
    capture = cc.ContinuationCapture(selected.binding, selected.revision, 'selected-leg')
    purchase = cc.IssueReplyPurchase(capture, REPLY_AT)
    metadata = {'repository': 'o/r', 'role': 'fix', 'kind': 'PR_RESUME', 'issue': 5, 'number': 100, 'leg_id': 'selected-leg',
                'issue_reply': encode_workflow(('2026-09-30T00:00:00Z', 'answer', 100))}
    forged = replace(request('forged'), workflow=None, owner_id='o/r:fix:fixture', kind='PR_RESUME',
                     dispatch_id=json.dumps(metadata), continuations=(purchase,))
    result = store._workflow_db.reserve(forged)
    assert not result.admitted


def test_blank_caught_enqueue_error_rolls_back_only_inner(monkeypatch):
    from contextlib import contextmanager
    workflow, _, store, _ = setup_reply('  ')
    control = store._workflow_db
    prior = resolve_state(store)
    original_enqueue = control._enqueue_command
    def failed(db, command):
        original_enqueue(db, command)
        raise ValueError('inner failure')
    with control.transaction() as db:
        @contextmanager
        def borrowed():
            yield db
        with monkeypatch.context() as patch:
            patch.setattr(control, 'transaction', borrowed)
            patch.setattr(control, '_enqueue_command', failed)
            with pytest.raises(ValueError, match='inner failure'):
                workflow.workflow_control.consume_blank_issue_reply(prior.binding, REPLY_AT, '  ')
        db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at) VALUES ('test','o/r','outer-marker','now')")
    assert resolve_state(store) == prior
    with control._connect() as db:
        assert db.execute('SELECT count(*) FROM lifecycle_commands').fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM dedupe_keys WHERE namespace='issue-reply-noop'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM dedupe_keys WHERE namespace='test'").fetchone()[0] == 1


def test_blank_outbox_failure_rolls_back_consumption():
    workflow, _, store, _ = setup_reply('  ')
    control = store._workflow_db
    prior = resolve_state(store)
    def failed(*args):
        raise ValueError('event failed')
    control.bind_event_outbox(failed, lambda event: None)
    with pytest.raises(ValueError, match='event failed'):
        workflow.workflow_control.consume_blank_issue_reply(prior.binding, REPLY_AT, '  ')
    assert resolve_state(store) == prior


@pytest.mark.parametrize("budget_refused", [False, True])
def test_no_pr_fork_count_and_remembered_branch_advance_only_on_admission(monkeypatch, budget_refused):
    from issue_agent.adapters.workflow_rows import persist_workflow_snapshot
    from issue_agent.domain.workflow import FreshResolve, ResolveSession
    from issue_agent.domain.contracts import RunId, WorkflowAttemptIdentity
    workflow, _, store, runner = setup_reply()
    before = resolve_state(store)
    source = WorkflowAttemptIdentity(before.binding, RunId("fixture-session"), "fixture-leg")
    component = replace(before.issue_recovery, state=replace(before.issue_recovery.state, resumes=4, remembered_branch="fix/5-retained"))
    before = replace(before, revision=replace(before.revision, version=1), issue_recovery=component,
                     episode=replace(before.episode, continuation=FreshResolve(ResolveSession(source, "session", "claude", "m"), 1)))
    with store._workflow_db.transaction() as db:
        persist_workflow_snapshot(db, before)
    if budget_refused:
        monkeypatch.setattr(store._workflow_db, "reserve", lambda request: ReservationResult(AccountingDisposition.EXHAUSTED, None, Money("USD", 0), Money("USD", 1), Money("USD", 0), "test cap"))
    asyncio.run(workflow.resume(5))
    after = resolve_state(store).issue_recovery.state
    if budget_refused:
        assert runner.tasks == [] and after == before.issue_recovery.state
    else:
        assert runner.tasks[0].resume == "session" and runner.tasks[0].fork_session
        assert after.resumes == 5 and after.remembered_branch == "fix/5-retained" and after.consumed_reply_at == REPLY_AT
    assert retained_issue_file(store, 5, "branch") == "" and retained_issue_file(store, 5, "resumed-at") == ""


@pytest.mark.parametrize("state", ["pending", "retryable_failure", "unknown_outcome", "permanent_failure"])
def test_blank_projection_orders_newer_paid_reply_and_terminal_failure_stays_terminal(state):
    from issue_agent.domain.contracts import CommandId, EffectReceipt, EffectStatus
    from issue_agent.adapters.control_store import ControlStoreError
    workflow, gateway, store, runner = setup_reply("  ")
    control = store._workflow_db
    workflow.workflow_control.consume_blank_issue_reply(resolve_state(store).binding, REPLY_AT, "  ")
    with control._connect() as db:
        command_id = CommandId(db.execute("SELECT command_id FROM lifecycle_commands").fetchone()[0])
    if state != "pending":
        control.begin_command(command_id)
        now = control._now()
        control.record_effect(EffectReceipt(command_id, workflow.workflow_control.writer.owner_id, now, now,
                                            EffectStatus(state), error="verified test failure"))
    newer = "2026-10-02T00:00:00Z"
    gateway.issues[5] = replace(gateway.issues[5], labels=frozenset({Label.AI_BLOCKED}), comments=(Comment("alice", "newer answer", newer),))
    before = resolve_state(store).issue_recovery.state
    asyncio.run(workflow.resume(5))
    if state == "permanent_failure":
        assert len(runner.tasks) == 1
        assert resolve_state(store).issue_recovery.state.consumed_reply_at == newer
        with pytest.raises(ControlStoreError):
            control.begin_command(command_id)
        assert command_id not in {item.command.command_id for item in control.runnable_commands(repository="o/r", executor_identity=workflow.workflow_control.writer.owner_id, diagnose=lambda message: None)}
        assert control.effect_receipt(command_id).status is EffectStatus.PERMANENT_FAILURE
        return
    assert runner.tasks == [] and resolve_state(store).issue_recovery.state == before
    with control._connect() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM budget_reservations").fetchone()[0] == 0
    if state == "unknown_outcome":
        now = control._now()
        control.resolve_unknown_effect(EffectReceipt(command_id, workflow.workflow_control.writer.owner_id, now, now, EffectStatus.SUCCEEDED))
    else:
        from issue_agent.adapters.effects import GitHubEffectExecutor
        workflow.executor._effect_executor = GitHubEffectExecutor(gateway, control, repository="o/r", identity=workflow.workflow_control.writer.owner_id, log=workflow.cfg.log)
        workflow.executor.replay_pending()
    assert control.effect_receipt(command_id).status is EffectStatus.SUCCEEDED
    asyncio.run(workflow.resume(5))
    assert len(runner.tasks) == 1 and resolve_state(store).issue_recovery.state.consumed_reply_at == newer
    asyncio.run(workflow.resume(5))
    assert len(runner.tasks) == 1


def test_blank_exact_retry_requires_noop_receipt_when_command_remains():
    from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
    workflow, _, store, runner = setup_reply("  ")
    binding = resolve_state(store).binding
    workflow.workflow_control.consume_blank_issue_reply(binding, REPLY_AT, "  ")
    with store._workflow_db.transaction() as db:
        db.execute("DELETE FROM dedupe_keys WHERE namespace='issue-reply-noop'")
    with pytest.raises(WorkflowAuthorityError, match="consumption receipt"):
        workflow.workflow_control.consume_blank_issue_reply(binding, REPLY_AT, "  ")
    assert runner.tasks == []


def test_blank_exact_retry_rejects_corrupt_terminal_effect_digest():
    from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
    workflow, _, store, runner = setup_reply("  ")
    asyncio.run(workflow.resume(5))
    with store._workflow_db.transaction() as db:
        db.execute("UPDATE effect_receipts SET receipt_digest='corrupt'")
    with pytest.raises(WorkflowAuthorityError, match="terminal effect"):
        asyncio.run(workflow.resume(5))
    assert runner.tasks == []


@pytest.mark.parametrize("field", ["expected_revision", "config_version"])
def test_blank_exact_retry_preserves_complete_original_command_identity(field):
    from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
    workflow, _, store, runner = setup_reply("  ")
    binding = resolve_state(store).binding
    workflow.workflow_control.consume_blank_issue_reply(binding, REPLY_AT, "  ")
    with store._workflow_db.transaction() as db:
        db.execute(f"UPDATE lifecycle_commands SET {field}='conflicting'")
    with pytest.raises(WorkflowAuthorityError, match="original projection command"):
        workflow.workflow_control.consume_blank_issue_reply(binding, REPLY_AT, "  ")
    assert runner.tasks == []


@pytest.mark.parametrize("terminal_failure", [False, True])
def test_blank_projection_settles_before_primary_rearm_and_new_generation_admission(tmp_path, terminal_failure):
    from issue_agent.adapters.control_store import SQLiteControlStore
    from issue_agent.adapters.workflow_rows import WorkflowAuthorityError, persist_workflow_snapshot
    from issue_agent.adapters.effects import GitHubEffectExecutor
    from issue_agent.domain.contracts import CommandId, EffectReceipt, EffectStatus
    from issue_agent.domain.workflow import NoLinkedPr, WorkflowSnapshot
    from issue_agent.domain.workflow_policy import reconcile_resolve_ceiling, complete_resolve_rearm
    from tests.fakes.workflow_authority import with_issue_recovery, install_workflow
    from tests.adapters.test_workflow_control import NOW, WRITER, adapter, resolve_snapshot, request, commit, prepare_workspace, observation, evidence
    from tests.adapters.test_continuation_admission import purchase, paid
    control = SQLiteControlStore(str(tmp_path / "control.sqlite"), now=lambda: NOW)
    control.initialize()
    state = with_issue_recovery(resolve_snapshot())
    install_workflow(control, state)
    authority = adapter(control)
    marker = "2026-09-01T00:00:00Z"
    authority.consume_blank_issue_reply(state.binding, marker, " ")
    with control._connect() as db:
        noop = CommandId(db.execute("SELECT command_id FROM lifecycle_commands").fetchone()[0])
    state = authority.load_workflow(state.revision.fence.key)
    assert isinstance(state, WorkflowSnapshot)
    state = reconcile_resolve_ceiling(replace(state, episode=replace(state.episode, failures=3)), ceiling=3, evidence_ids=("fixture-ceiling",)).snapshot
    with control.transaction() as db:
        persist_workflow_snapshot(db, state)
    with pytest.raises(WorkflowAuthorityError, match="re-block projection"):
        commit(control, request(state))
    assert authority.load_workflow(state.revision.fence.key) == state
    if terminal_failure:
        control.begin_command(noop)
        control.record_effect(EffectReceipt(noop, WRITER.owner_id, NOW, NOW, EffectStatus.PERMANENT_FAILURE, error="verified refusal"))
    else:
        gateway = FakeGateway(issues={1: IssueView(1, labels=frozenset({Label.AI_WORKING}))})
        GitHubEffectExecutor(gateway, control, repository="o/r", identity=WRITER.owner_id, log=lambda message: None).drain_pending_commands()
        assert control.effect_receipt(noop).status is EffectStatus.SUCCEEDED
    assert authority.consume_blank_issue_reply(state.binding, marker, " ")
    proposal = request(state)
    pending = commit(control, proposal).snapshot
    workspace = prepare_workspace(control, pending)
    projection = proposal.commands[0].command_id
    control.begin_command(projection)
    control.record_effect(EffectReceipt(projection, WRITER.owner_id, NOW, NOW, EffectStatus.SUCCEEDED))
    evidence(control, pending)
    done = complete_resolve_rearm(pending, observation(pending, frozenset({Label.AI_READY})), NoLinkedPr(pending.revision.fence.key, NOW), verified_commands=(projection,), evidence_ids=("observation:1",), now=NOW, workspace=workspace)
    completed = commit(control, done).snapshot
    assert completed.issue_recovery.state.consumed_reply_at == marker
    assert completed.binding.fence.generation > state.binding.fence.generation
    selected = purchase(completed, reply="2026-09-02T00:00:00Z")
    assert control.reserve_issue_recovery(paid("new-generation", selected), selected).admitted
