"""A whole paid triage verdict survives restart before advancing progress."""

from dataclasses import replace
from contextlib import contextmanager
from datetime import timedelta
import json
import sqlite3

import pytest

from issue_agent.adapters.control_store import AccountingDisposition, ControlStoreError, Lease, RetiredOwnerError, SQLiteControlStore
from issue_agent.adapters.effects import GitHubEffectExecutor, commands_for_steward_verdict
from issue_agent.adapters.workflow_codec import encode_workflow
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError, workflow_scope
from issue_agent.domain import continuation_components as cc
from issue_agent.domain.actions import CloseIssue, PostComment, Relabel, Target
from issue_agent.domain.contracts import WorkflowAttemptIdentity
from issue_agent.domain.contracts import EffectReceipt, EffectStatus
from issue_agent.domain.eligibility import LifecycleOperation
from issue_agent.domain.events import EventEnvelope, Producer, new_event_id
from issue_agent.domain.labels import Label
from issue_agent.domain.lifecycle import completion_guard
from issue_agent.domain.workflow import IssueWorkflowShell
from issue_agent.domain.workflow_identity import WorkflowFamily
from issue_agent.domain.world import Comment, IssueView
from tests.adapters.test_continuation_admission import active, install_secondary, money
from tests.adapters.test_workflow_admission import NOW, request
from tests.fakes.in_memory_github import InMemoryGitHub
from tests.fakes.workflow_authority import resolve_snapshot


OWNER = "o/r:steward:test"


def bind_events(store):
    sequence = 0
    producer = Producer("deployment", "o/r", "steward", 123, "boot")

    def factory(payload, correlation):
        nonlocal sequence
        event = EventEnvelope(payload, new_event_id(NOW + timedelta(milliseconds=sequence)),
                              sequence, NOW, NOW, sequence, producer, correlation)
        sequence += 1
        return event

    store.bind_event_outbox(factory, lambda event: None)


def admitted(tmp_path, *, lane=cc.StewardLane.TRIAGE):
    store = SQLiteControlStore(str(tmp_path / "control.db"), now=lambda: NOW)
    store.initialize()
    bind_events(store)
    revision = resolve_snapshot(7).revision
    component = active(revision.fence.key, WorkflowFamily.STEWARD)
    shell = IssueWorkflowShell(revision, steward=component)
    with store.transaction() as db:
        db.execute("INSERT INTO workflows VALUES (?,?,?,?,?,?)",
                   (*workflow_scope(revision.fence.key), revision.fence.generation, revision.version, encode_workflow(shell)))
    install_secondary(store, shell, component)
    selection = cc.select_steward(shell)
    issue = IssueView(7, updated_at=NOW.isoformat(), labels=frozenset({Label.AI_TRIAGE}), title="Triage me")
    from issue_agent.domain.triage import grooming_content
    signature = grooming_content(issue) if lane is cc.StewardLane.GROOM else ""
    purchase = cc.StewardPurchase(cc.ContinuationCapture(selection.binding, selection.revision, "leg:triage"),
                                  lane, NOW.isoformat(), 3, signature)
    paid = replace(request("triage"), workflow=selection.binding, owner_id=OWNER, kind=lane.value.upper(),
        dispatch_id=json.dumps({"repository":"o/r", "role":"steward", "kind":lane.value.upper(), "issue":7, "number":7, "leg_id":"leg:triage"}))
    assert store.reserve_steward(paid, purchase).admitted
    assert store.settle(money(paid.run_id)).disposition is AccountingDisposition.AVAILABLE
    source = store.steward_admission(WorkflowAttemptIdentity(selection.binding, paid.run_id, "leg:triage"))
    gateway = InMemoryGitHub((issue,))
    actions = (PostComment(Target.ISSUE, 7, "Triage accepted"),
               Relabel(Target.ISSUE, 7, Label.AI_TRIAGE, Label.AI_READY))
    commands = commands_for_steward_verdict(actions, source, completion_guard(issue,
        LifecycleOperation.GROOM if lane is cc.StewardLane.GROOM else LifecycleOperation.COMPLETE_TRIAGE), identity=OWNER, writer_login="fix-bot")
    outcome = cc.StewardVerdict(source, tuple(str(command.command_id) for command in commands))
    return store, gateway, source, commands, outcome, paid.scope


@pytest.fixture
def verdict(tmp_path):
    result = admitted(tmp_path)
    store, _, _, commands, outcome, _ = result
    assert store.record_steward_outcome(outcome, commands)
    return result


def executor(store, gateway):
    return GitHubEffectExecutor(gateway, store, repository="o/r", identity=OWNER)


@pytest.mark.parametrize("lane", [cc.StewardLane.TRIAGE, cc.StewardLane.GROOM])
def test_whole_queued_verdict_recovers_after_restart_without_repurchase(tmp_path, lane):
    store, gateway, source, commands, outcome, scope = admitted(tmp_path, lane=lane)
    assert store.record_steward_outcome(outcome, commands)
    key = source.admission.attempt.binding.fence.key
    assert len(store.runnable_commands(repository="o/r", executor_identity=OWNER, diagnose=lambda message: None)) == 2
    assert getattr(store.read_steward(key).component.state, lane.value).baseline_at == ""
    assert not store.complete_steward_delivery(source, gateway.get_issue(7))
    restarted = SQLiteControlStore(store.path, now=lambda: NOW)
    bind_events(restarted)
    selection = restarted.read_steward(key)
    newer = cc.StewardPurchase(cc.ContinuationCapture(selection.binding, selection.revision, "leg:new"),
                               lane, (NOW + timedelta(minutes=1)).isoformat(), 3, source.content_signature)
    refused = replace(request("again"), workflow=selection.binding, owner_id=OWNER, kind=lane.value.upper())
    assert not restarted.reserve_steward(refused, newer).admitted
    assert executor(restarted, gateway).drain_pending_commands() == 2
    assert gateway.comments[("issue", 7)] == ["Triage accepted"]
    assert gateway.get_issue(7).labels == frozenset({Label.AI_READY})
    delivered_at = (NOW + timedelta(minutes=2)).isoformat()
    assert restarted.complete_steward_delivery(source, replace(gateway.get_issue(7), updated_at=delivered_at))
    assert not restarted.complete_steward_delivery(source, replace(gateway.get_issue(7), updated_at=delivered_at))
    assert not restarted.record_steward_outcome(outcome, commands)
    assert getattr(restarted.read_steward(key).component.state, lane.value).baseline_at == delivered_at
    assert executor(restarted, gateway).drain_pending_commands() == 0
    assert restarted.snapshot(scope).settled.micros == 1


def test_later_verdict_command_cannot_execute_before_its_exact_predecessor(verdict):
    store, gateway, source, commands, _, _ = verdict
    with pytest.raises(WorkflowAuthorityError, match="predecessor"):
        store.begin_command(commands[1].command_id)
    assert gateway.calls == []
    assert executor(store, gateway).drain_pending_commands() == 2
    assert store.complete_steward_delivery(source, gateway.get_issue(7))


@pytest.mark.parametrize("human_change", ["comment", "hold", "body"])
def test_human_change_after_confirmed_comment_defers_remaining_verdict(verdict, human_change):
    store, gateway, source, commands, _, _ = verdict
    effect = executor(store, gateway)
    pending = store.runnable_commands(repository="o/r", executor_identity=OWNER, diagnose=lambda message: None)[0]
    assert effect._execute(PostComment(Target.ISSUE, 7, "Triage accepted"), pending)
    issue = gateway.get_issue(7)
    if human_change == "comment":
        issue = replace(issue, comments=issue.comments + (Comment("alice", "Wait for me"),))
    elif human_change == "hold":
        issue = replace(issue, labels=issue.labels | {Label.AI_BLOCKED})
    else:
        issue = replace(issue, body="Changed requirements")
    gateway.issues[7] = issue
    assert effect.drain_pending_commands() == 0
    assert Label.AI_READY not in gateway.get_issue(7).labels
    assert store.complete_steward_delivery(source, gateway.get_issue(7))
    lane = store.read_steward(source.admission.attempt.binding.fence.key).component.state.triage
    assert lane.baseline_at == "" and lane.stalls == 1
    assert not store.complete_steward_delivery(source, gateway.get_issue(7))


def test_return_refuses_ordinary_unguarded_command_and_rolls_back(tmp_path):
    from issue_agent.adapters.effects import command_for_action
    store, gateway, source, _, _, _ = admitted(tmp_path)
    command = command_for_action(PostComment(Target.ISSUE, 7, "unguarded"), repository="o/r", executor_identity=OWNER)
    with store.transaction() as db:
        before = tuple(db.iterdump())
    with pytest.raises(ValueError):
        store.record_steward_outcome(cc.StewardVerdict(source, (str(command.command_id),)), (command,))
    with store.transaction() as db:
        assert tuple(db.iterdump()) == before
    assert gateway.calls == []


def test_fixed_verdict_refuses_new_occurrence(verdict):
    store, gateway, _, commands, _, _ = verdict
    first = store.runnable_commands(repository="o/r", executor_identity=OWNER, diagnose=lambda message: None)[0]
    assert executor(store, gateway)._execute(PostComment(Target.ISSUE, 7, "Triage accepted"), first)
    with pytest.raises(WorkflowAuthorityError, match="occurrence"):
        store.enqueue_command_occurrence(commands[0])


def test_terminal_receipt_retries_require_immutable_source_after_delivery(verdict):
    store, gateway, source, commands, _, _ = verdict
    assert executor(store, gateway).drain_pending_commands() == 2
    receipt = store.effect_receipt(commands[0].command_id)
    assert store.complete_steward_delivery(source, gateway.get_issue(7))
    assert store.record_effect(receipt)
    assert not store.resolve_unknown_effect(receipt)
    with store.transaction() as db:
        db.execute("DELETE FROM dedupe_keys WHERE namespace='continuation-admission'")
    with pytest.raises(WorkflowAuthorityError):
        store.record_effect(receipt)
    with pytest.raises(WorkflowAuthorityError):
        store.resolve_unknown_effect(receipt)


@pytest.mark.parametrize("status", [EffectStatus.UNKNOWN_OUTCOME, EffectStatus.RETRYABLE_FAILURE])
def test_caught_receipt_replacement_failure_preserves_evidence(verdict, status, monkeypatch):
    store, _, _, commands, _, _ = verdict
    store.begin_command(commands[0].command_id)
    original = EffectReceipt(commands[0].command_id, OWNER, NOW, NOW, status, error="lost or refused")
    assert store.record_effect(original)
    replacement = replace(original, executor_identity="o/r:steward:other",
                          status=EffectStatus.SUCCEEDED if status is EffectStatus.UNKNOWN_OUTCOME else EffectStatus.PERMANENT_FAILURE,
                          error=None if status is EffectStatus.UNKNOWN_OUTCOME else "exhausted")
    with store.transaction() as db:
        @contextmanager
        def borrowed():
            yield db
        with monkeypatch.context() as patch:
            patch.setattr(store, "transaction", borrowed)
            with pytest.raises(ControlStoreError, match="owner"):
                (store.resolve_unknown_effect if status is EffectStatus.UNKNOWN_OUTCOME else store.exhaust_command)(replacement)
    assert store.effect_receipt(commands[0].command_id) == original


@pytest.mark.parametrize("lost_reply", [False, True])
def test_same_body_human_comment_cannot_certify_own_delivery(verdict, lost_reply):
    store, gateway, source, commands, _, _ = verdict
    effect = executor(store, gateway)
    if lost_reply:
        store.begin_command(commands[0].command_id)
        store.record_effect(EffectReceipt(commands[0].command_id, OWNER, NOW, NOW, EffectStatus.UNKNOWN_OUTCOME, error="lost reply"))
    else:
        first = store.runnable_commands(repository="o/r", executor_identity=OWNER, diagnose=lambda message: None)[0]
        assert effect._execute(PostComment(Target.ISSUE, 7, "Triage accepted"), first)
    gateway.issues[7] = replace(gateway.get_issue(7), comments=(Comment("alice", "Triage accepted", "human-time"),))
    assert effect.drain_pending_commands() == 0
    assert Label.AI_READY not in gateway.get_issue(7).labels
    assert store.read_steward(source.admission.attempt.binding.fence.key).component.state.triage.baseline_at == ""


def test_later_human_input_is_not_absorbed_by_delivered_baseline(verdict):
    store, gateway, source, _, _, _ = verdict
    assert executor(store, gateway).drain_pending_commands() == 2
    gateway.issues[7] = replace(gateway.get_issue(7), body="new scope", updated_at=(NOW + timedelta(minutes=1)).isoformat())
    assert store.complete_steward_delivery(source, gateway.get_issue(7))
    current = store.read_steward(source.admission.attempt.binding.fence.key)
    assert current.component.state.triage.baseline_at == source.input_at
    assert cc.triage_admissible(current, gateway.get_issue(7).updated_at, 3)


def test_failed_suffix_retirement_rolls_back_if_caller_catches_failure(verdict, monkeypatch):
    store, gateway, source, commands, _, _ = verdict
    store.begin_command(commands[0].command_id)
    store.record_effect(EffectReceipt(commands[0].command_id, OWNER, NOW, NOW, EffectStatus.PERMANENT_FAILURE, error="refused"))
    with store.transaction() as db:
        db.execute("CREATE TRIGGER fail_suffix BEFORE INSERT ON effect_receipts WHEN NEW.command_id='" + str(commands[1].command_id) + "' BEGIN SELECT RAISE(ABORT,'suffix failed'); END")
        before = tuple(db.iterdump())
        @contextmanager
        def borrowed():
            yield db
        with monkeypatch.context() as patch:
            patch.setattr(store, "transaction", borrowed)
            with pytest.raises(sqlite3.IntegrityError, match="suffix failed"):
                store.complete_steward_delivery(source, gateway.get_issue(7))
        assert tuple(db.iterdump()) == before
    with store.transaction() as db:
        db.execute("DROP TRIGGER fail_suffix")
    assert store.complete_steward_delivery(source, gateway.get_issue(7))
    assert not store.complete_steward_delivery(source, gateway.get_issue(7))
    lane = store.read_steward(source.admission.attempt.binding.fence.key).component.state.triage
    assert lane.stalls == 1 and lane.baseline_at == "" and lane.pending_delivery == ()
    assert store.pending_commands() == () and gateway.calls == []


SUCCESSOR = "o/r:steward:successor"

def running_steward(tmp_path):
    store=SQLiteControlStore(str(tmp_path/'control.db'),now=lambda:NOW);store.initialize();bind_events(store)
    revision=resolve_snapshot(7).revision;component=active(revision.fence.key,WorkflowFamily.STEWARD)
    shell=IssueWorkflowShell(revision,steward=component)
    with store.transaction() as db:
        db.execute('INSERT INTO workflows VALUES (?,?,?,?,?,?)',(*workflow_scope(revision.fence.key),revision.fence.generation,revision.version,encode_workflow(shell)))
    install_secondary(store,shell,component)
    sel=cc.select_steward(shell)
    buy=cc.StewardPurchase(cc.ContinuationCapture(sel.binding,sel.revision,'leg:triage'),cc.StewardLane.TRIAGE,NOW.isoformat(),3)
    paid=replace(request('active-triage'),workflow=sel.binding,owner_id=OWNER,kind='TRIAGE',dispatch_id=json.dumps({'repository':'o/r','role':'steward','issue':7,'number':7,'kind':'TRIAGE','leg_id':'leg:triage'}))
    assert store.reserve_steward(paid,buy).admitted
    store.register_lease(Lease(paid.run_id,paid.dispatch_id,OWNER,123,123,NOW,456))
    return store,paid

def test_adoption_retains_immutable_purchase_and_recovers_lease_money(tmp_path):
    store,paid=running_steward(tmp_path)
    store.adopt_legacy_owner('o/r','steward',SUCCESSOR,prior_owner_ids=(OWNER,))
    assert store.leases(SUCCESSOR)[0].owner_id==SUCCESSOR
    assert store.leases(OWNER)==()
    assert store.active_reservation_runs(SUCCESSOR)==(paid.run_id,)
    with store.transaction() as db: assert db.execute('SELECT owner_id FROM runs WHERE run_id=?',(str(paid.run_id),)).fetchone()[0]==OWNER
    assert store.reclaim_dead_leases(SUCCESSOR,lambda pid,start:False)==(paid.run_id,)
    assert store.active_reservation_runs(SUCCESSOR)==(paid.run_id,)
    assert store.settle(money(paid.run_id)).disposition.value == "available"
    assert store.active_reservation_runs(SUCCESSOR)==()
    store.integrity_check()


@pytest.mark.parametrize("reclaimed_first", [False, True])
def test_steward_return_and_reclaim_charge_one_lane_once(tmp_path, reclaimed_first):
    store, paid = running_steward(tmp_path)
    key = paid.workflow.fence.key
    if reclaimed_first:
        assert store.reclaim_dead_leases(OWNER, lambda pid, start: False) == (paid.run_id,)
        state = store.read_steward(key).component.state
        assert state.triage.infra_failures == 1 and state.triage.stalls == 0
        assert state.groom == cc.StewardLaneState()
        assert store.active_reservation_runs(OWNER) == (paid.run_id,)
        with store.transaction() as db:
            assert db.execute("SELECT count(*) FROM charges").fetchone()[0] == 0
    assert store.settle(money(paid.run_id)).disposition is AccountingDisposition.AVAILABLE
    source = store.steward_admission(WorkflowAttemptIdentity(paid.workflow, paid.run_id, "leg:triage"))
    returned = cc.StewardFailed(source, cc.StewardFailureKind.INFRASTRUCTURE)
    assert store.record_steward_outcome(returned)
    assert not store.record_steward_outcome(returned)
    assert store.reclaim_dead_leases(OWNER, lambda pid, start: False) == ()
    reopened = SQLiteControlStore(store.path, now=lambda: NOW)
    reopened.initialize()
    state = reopened.read_steward(key).component.state
    assert state.triage.infra_failures == 1 and state.triage.stalls == 0
    assert state.groom == cc.StewardLaneState()
    with reopened.transaction() as db:
        assert db.execute("SELECT count(*) FROM charges").fetchone()[0] == 1


def test_late_steward_return_cannot_change_new_human_episode(tmp_path):
    store, paid = running_steward(tmp_path)
    key = paid.workflow.fence.key
    assert store.reclaim_dead_leases(OWNER, lambda pid, start: False) == (paid.run_id,)
    assert store.settle(money(paid.run_id)).disposition is AccountingDisposition.AVAILABLE
    source = store.steward_admission(WorkflowAttemptIdentity(paid.workflow, paid.run_id, "leg:triage"))
    selection = store.read_steward(key)
    purchase = cc.StewardPurchase(cc.ContinuationCapture(selection.binding, selection.revision, "leg:new"),
        cc.StewardLane.TRIAGE, (NOW + timedelta(minutes=1)).isoformat(), 3, infra_ceiling=2)
    fresh = replace(request("new-human"), workflow=selection.binding, owner_id=OWNER, kind="TRIAGE",
        dispatch_id=json.dumps({"repository":"o/r", "role":"steward", "issue":7, "number":7,
                               "kind":"TRIAGE", "leg_id":"leg:new"}))
    assert store.reserve_steward(fresh, purchase).admitted
    before = store.read_steward(key)
    assert before.component.state.triage.infra_failures == 0
    assert not store.record_steward_outcome(cc.StewardFailed(source, cc.StewardFailureKind.PAID))
    assert store.read_steward(key) == before
    assert store.pending_commands() == ()


@pytest.mark.parametrize("runtime_first", [False, True])
def test_captured_steward_death_survives_restart_and_reclaim(tmp_path, runtime_first):
    from issue_agent.domain.workflow_identity import WorkflowWriter
    from issue_agent.adapters.workflow_codec import decode_workflow
    store, paid = running_steward(tmp_path)
    writer = WorkflowWriter("o/r", "steward", OWNER)
    key = paid.workflow.fence.key
    before = store.read_steward(key)
    if runtime_first:
        assert not store.record_steward_recovery(paid.run_id, cc.StewardFailureKind.DETERMINISTIC, writer)
        assert store.read_steward(key) == before
        selected = store.read_steward(key)
        blocked = cc.StewardPurchase(cc.ContinuationCapture(selected.binding, selected.revision, "leg:blocked"),
            cc.StewardLane.TRIAGE, NOW.isoformat(), 3, infra_ceiling=2)
        with pytest.raises(ValueError, match="held, pending"):
            cc.consume_steward(selected, blocked, selected.revision.version + 1)
        with store.transaction() as db:
            row = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='continuation-return'").fetchone()
            assert decode_workflow(row[0], tuple)[1].kind is cc.StewardFailureKind.DETERMINISTIC
    reopened = SQLiteControlStore(store.path, now=lambda: NOW)
    reopened.initialize()
    bind_events(reopened)
    assert reopened.reclaim_dead_leases(OWNER, lambda pid, start: False) == (paid.run_id,)
    if not runtime_first:
        assert reopened.record_steward_recovery(paid.run_id, cc.StewardFailureKind.DETERMINISTIC, writer)
    state = reopened.read_steward(key)
    # A legacy capture without a policy conservatively contributes one death.
    assert state.component.state.triage.infra_failures == 1
    assert state.component.state.triage.stalls == 0 and state.component.state.groom == before.component.state.groom
    assert not reopened.record_steward_recovery(paid.run_id, cc.StewardFailureKind.DETERMINISTIC, writer)
    assert reopened.read_steward(key) == state
    assert reopened.active_reservation_runs(OWNER) == (paid.run_id,)
    with reopened.transaction() as db:
        assert db.execute("SELECT count(*) FROM charges").fetchone()[0] == 0


def test_caught_steward_reclaim_failure_rolls_back_lease_run_episode_and_receipt(tmp_path, monkeypatch):
    import sqlite3
    store, paid = running_steward(tmp_path)
    before = store.read_steward(paid.workflow.fence.key)
    with store.transaction() as db:
        db.execute("CREATE TRIGGER refuse_steward_event BEFORE INSERT ON event_outbox "
                   "BEGIN SELECT RAISE(ABORT,'refuse steward outcome'); END")
        original = tuple(db.iterdump())
        @contextmanager
        def borrowed():
            yield db
        with monkeypatch.context() as patch:
            patch.setattr(store, "transaction", borrowed)
            with pytest.raises(sqlite3.IntegrityError, match="refuse steward outcome"):
                store.reclaim_dead_leases(OWNER, lambda pid, start: False)
        assert tuple(db.iterdump()) == original
        db.execute("DROP TRIGGER refuse_steward_event")
    assert store.read_steward(paid.workflow.fence.key) == before
    assert store.leases(OWNER)[0].run_id == paid.run_id
    assert store.reclaim_dead_leases(OWNER, lambda pid, start: False) == (paid.run_id,)
    assert store.read_steward(paid.workflow.fence.key).component.state.triage.infra_failures == 1
    assert store.active_reservation_runs(OWNER) == (paid.run_id,)


def test_administrative_spend_release_cannot_claim_never_launched_steward_work(tmp_path):
    from issue_agent.domain.workflow_identity import WorkflowWriter
    store, paid = running_steward(tmp_path)
    assert store.record_reservation_retained(paid.run_id, "unknown launched spend")
    assert store.release_retained(paid.run_id, "operator grace expired", NOW + timedelta(days=1)).disposition is AccountingDisposition.AVAILABLE
    before = store.read_steward(paid.workflow.fence.key)
    with store.transaction() as db:
        original = tuple(db.iterdump())
    with pytest.raises(WorkflowAuthorityError, match="proven never-launched"):
        store.record_steward_recovery(paid.run_id, cc.StewardFailureKind.RECLAIMED,
                                     WorkflowWriter("o/r", "steward", OWNER))
    assert store.read_steward(paid.workflow.fence.key) == before
    with store.transaction() as db:
        assert tuple(db.iterdump()) == original

def test_retired_owner_cannot_cancel_adopted_current_run(tmp_path):
    store,paid=running_steward(tmp_path)
    store.adopt_legacy_owner('o/r','steward',SUCCESSOR,prior_owner_ids=(OWNER,))
    with store.transaction() as db: before=tuple(db.iterdump())
    try: result=store.record_owned_cancellation_intents(OWNER,'old daemon cancellation')
    except RetiredOwnerError: result=()
    assert result==()
    with store.transaction() as db: assert tuple(db.iterdump())==before

@pytest.mark.parametrize('kind',['unknown_outcome','retryable_failure'])
def test_adopted_command_receipt_recovery(verdict,kind):
    from issue_agent.domain.contracts import EffectReceipt,EffectStatus
    from issue_agent.domain.actions import PostComment,Target
    from issue_agent.adapters.effects import GitHubEffectExecutor
    store,gateway,source,commands,outcome,_=verdict
    store.begin_command(commands[0].command_id)
    store.record_effect(EffectReceipt(commands[0].command_id,OWNER,NOW,NOW,EffectStatus(kind),error='lost/refused'))
    store.adopt_legacy_owner('o/r','steward',SUCCESSOR,prior_owner_ids=(OWNER,))
    effect=GitHubEffectExecutor(gateway,store,repository='o/r',identity=SUCCESSOR)
    assert effect.drain_pending_commands()==2
    assert store.complete_steward_delivery(source,gateway.get_issue(7))
    assert not store.record_steward_outcome(outcome,commands)
    store.integrity_check()

def test_adopted_existing_process_lease_retry_is_inert(tmp_path):
    store,paid=running_steward(tmp_path)
    store.adopt_legacy_owner('o/r','steward',SUCCESSOR,prior_owner_ids=(OWNER,))
    with store.transaction() as db: before=tuple(db.iterdump())
    lease=store.register_process_lease(paid.run_id,expected_owner_id=SUCCESSOR,pid=123,pgid=123,process_start_ticks=456)
    assert lease.owner_id==SUCCESSOR
    with store.transaction() as db: assert tuple(db.iterdump())==before

def test_adopted_selected_claim_passes_restart_integrity(tmp_path):
    store,paid=running_steward(tmp_path)
    assert store.claim_dispatch('o/r','steward',7,'TRIAGE',OWNER,frozenset())
    assert store.bind_dispatch_claim('o/r','steward',7,OWNER,paid.run_id)
    store.adopt_legacy_owner('o/r','steward',SUCCESSOR,prior_owner_ids=(OWNER,))
    restarted=SQLiteControlStore(store.path,now=lambda:NOW)
    restarted.initialize()
    restarted.integrity_check()
    assert restarted.leases(SUCCESSOR)[0].run_id==paid.run_id
    with restarted.transaction() as db:
        assert db.execute('SELECT owner_id FROM runs WHERE run_id=?',(str(paid.run_id),)).fetchone()[0]==OWNER
        assert db.execute('SELECT owner_id FROM dispatch_claims').fetchone()[0]==SUCCESSOR

@pytest.mark.parametrize('wrong_owner',['elsewhere/repo:steward:old','o/r:review:old'])
def test_cross_scope_adoption_refuses_without_mutation(tmp_path,wrong_owner):
    store,paid=running_steward(tmp_path)
    with store.transaction() as db: before=tuple(db.iterdump())
    with pytest.raises(ValueError):store.adopt_legacy_owner('o/r','steward',SUCCESSOR,prior_owner_ids=(wrong_owner,))
    with store.transaction() as db:assert tuple(db.iterdump())==before


@pytest.mark.parametrize("captured", [True, False])
def test_compound_relabel_partial_identity_controls_failed_settlement(tmp_path, monkeypatch, captured):
    store, gateway, source, _, _, _ = admitted(tmp_path)
    original = gateway.get_issue(7)
    action = Relabel(Target.ISSUE, 7, Label.AI_TRIAGE, Label.AI_READY,
        expected_revision=original.updated_at, comment="Classified ready",
        completion_guard=completion_guard(original, LifecycleOperation.COMPLETE_TRIAGE))
    commands = commands_for_steward_verdict((action,), source, completion_guard(original, LifecycleOperation.COMPLETE_TRIAGE),
        identity=OWNER, writer_login="fix-bot")
    outcome = cc.StewardVerdict(source, (str(commands[0].command_id),))
    assert store.record_steward_outcome(outcome, commands)
    monkeypatch.setattr(gateway, "relabel", lambda *args: None)
    if not captured:
        store.begin_command(commands[0].command_id)
        gateway.comment("issue", 7, action.comment)
        store.record_effect(EffectReceipt(commands[0].command_id, OWNER, NOW, NOW,
            EffectStatus.UNKNOWN_OUTCOME, error="reply lost before identity capture"))
    effects = executor(store, gateway)
    effects.retry_ceiling = 1
    effects.drain_pending_commands()
    receipt = store.effect_receipt(commands[0].command_id)
    assert receipt.status is EffectStatus.RETRYABLE_FAILURE
    assert bool(receipt.remote_revision) is captured
    gateway.issues[7] = replace(gateway.get_issue(7), updated_at=(NOW + timedelta(seconds=1)).isoformat())
    effects.drain_pending_commands()
    assert store.effect_receipt(commands[0].command_id).status is EffectStatus.PERMANENT_FAILURE
    assert bool(store.effect_receipt(commands[0].command_id).remote_revision) is captured
    assert store.complete_steward_delivery(source, gateway.get_issue(7)) is captured
    lane = store.read_steward(source.admission.attempt.binding.fence.key).component.state.triage
    assert lane.baseline_at == ""
    if captured:
        assert lane.stalls == 1 and lane.attempted_at == gateway.get_issue(7).updated_at and not lane.pending_delivery
    else:
        assert lane.pending_delivery == outcome.commands and lane.attempted_at == source.input_at
        assert not cc.triage_admissible(store.read_steward(source.admission.attempt.binding.fence.key), gateway.get_issue(7).updated_at, 3)


def test_compound_relabel_cannot_reconcile_an_old_captured_comment(tmp_path):
    store, gateway, source, _, _, _ = admitted(tmp_path)
    original = replace(gateway.get_issue(7), comments=(Comment("fix-bot", "Still blocked", "original"),))
    gateway.issues[7] = original
    gateway.comments[("issue", 7)] = ["Still blocked"]
    action = Relabel(Target.ISSUE, 7, None, Label.AI_TRIAGE, expected_revision=original.updated_at,
        comment="Still blocked", completion_guard=completion_guard(original, LifecycleOperation.COMPLETE_TRIAGE))
    commands = commands_for_steward_verdict((action,), source, completion_guard(original, LifecycleOperation.COMPLETE_TRIAGE),
        identity=OWNER, writer_login="fix-bot")
    assert store.record_steward_outcome(cc.StewardVerdict(source, (str(commands[0].command_id),)), commands)
    assert executor(store, gateway).drain_pending_commands() == 1
    assert store.complete_steward_delivery(source, gateway.get_issue(7))
    assert len(gateway.get_issue(7).comments) == 2
    assert gateway.get_issue(7).comments[0] == original.comments[0]
    assert gateway.get_issue(7).comments[1].body == "Still blocked"
    assert store.read_steward(source.admission.attempt.binding.fence.key).component.state.triage.baseline_at == original.updated_at


@pytest.mark.parametrize('kind',['close','relabel'])
@pytest.mark.parametrize('point',['comment','last_write'])
@pytest.mark.parametrize("lane", [cc.StewardLane.TRIAGE, cc.StewardLane.GROOM])
def test_lost_reply_exact_compound_delivery(tmp_path,kind,point,lane):
    store,gateway,source,_,_,_=admitted(tmp_path,lane=lane); view=gateway.get_issue(7)
    guard=completion_guard(view,LifecycleOperation.CLOSE if kind=='close' else LifecycleOperation.COMPLETE_TRIAGE)
    action=CloseIssue(7,'completed','Exact selected explanation',remove_labels=(Label.AI_TRIAGE,),add_labels=(Label.AI_CLOSED,)) if kind=='close' else Relabel(Target.ISSUE,7,Label.AI_TRIAGE,Label.AI_READY,comment='Exact selected explanation',completion_guard=guard,expected_revision=NOW.isoformat())
    commands=commands_for_steward_verdict((action,),source,guard,identity=OWNER,writer_login='fix-bot')
    store.record_steward_outcome(cc.StewardVerdict(source,(str(commands[0].command_id),)),commands)
    method='comment' if point=='comment' else 'close_issue' if kind=='close' else 'relabel'
    original=getattr(gateway,method);calls=[]
    def lost(*args):
        original(*args);calls.append(args)
        if len(calls)==1: raise RuntimeError('lost reply after actual mutation')
    setattr(gateway,method,lost)
    effects=executor(store,gateway)
    effects.drain_pending_commands()
    assert store.pending_commands()[0].state=='unknown_outcome'
    effects.drain_pending_commands()
    assert store.complete_steward_delivery(source,gateway.get_issue(7))
    assert not getattr(store.read_steward(source.admission.attempt.binding.fence.key).component.state, lane.value).pending_delivery
    assert len(gateway.get_issue(7).comments)==1
    with store.transaction() as db: assert db.execute('SELECT count(*) FROM charges').fetchone()[0]==1


def test_empty_groom_input_refuses_before_money_or_authority_writes(tmp_path):
    store, _, source, _, _, _ = admitted(tmp_path)
    assert store.record_steward_outcome(cc.StewardFailed(source, cc.StewardFailureKind.PAID))
    selected = store.read_steward(source.admission.attempt.binding.fence.key)
    purchase = cc.StewardPurchase(cc.ContinuationCapture(selected.binding, selected.revision, "leg:groom"),
                                  cc.StewardLane.GROOM, "", 3, "a" * 64)
    paid = replace(request("empty-groom"), workflow=selected.binding, owner_id=OWNER, kind="GROOM",
        dispatch_id=json.dumps({"repository":"o/r", "role":"steward", "kind":"GROOM", "issue":7, "number":7, "leg_id":"leg:groom"}))
    with store.transaction() as db:
        before = tuple(db.iterdump())
    assert not store.reserve_steward(paid, purchase).admitted
    with store.transaction() as db:
        assert tuple(db.iterdump()) == before


@pytest.mark.parametrize("operation", [LifecycleOperation.GROOM, LifecycleOperation.CLOSE])
def test_groom_verdict_cannot_rebind_purchased_content(tmp_path, operation):
    store, gateway, source, _, _, _ = admitted(tmp_path, lane=cc.StewardLane.GROOM)
    changed = replace(gateway.get_issue(7), body="Human input not classified by this purchase")
    with store.transaction() as db:
        before = tuple(db.iterdump())
    with pytest.raises(ValueError, match="purchased content"):
        commands_for_steward_verdict((PostComment(Target.ISSUE, 7, "Old advice"),), source,
            completion_guard(changed, operation), identity=OWNER, writer_login="fix-bot")
    with store.transaction() as db:
        assert tuple(db.iterdump()) == before
    assert gateway.calls == []


def test_no_finding_return_rolls_back_when_outer_caller_catches_error(tmp_path, monkeypatch):
    store, _, source, _, _, _ = admitted(tmp_path, lane=cc.StewardLane.GROOM)
    outcome = cc.GroomNoFinding(source)
    with store.transaction() as db:
        db.execute("CREATE TRIGGER reject_no_finding BEFORE INSERT ON event_outbox BEGIN SELECT RAISE(ABORT,'return failed'); END")
        before = tuple(db.iterdump())
        @contextmanager
        def borrowed():
            yield db
        with monkeypatch.context() as patch:
            patch.setattr(store, "transaction", borrowed)
            with pytest.raises(sqlite3.IntegrityError, match="return failed"):
                store.record_steward_outcome(outcome)
        assert tuple(db.iterdump()) == before
        db.execute("DROP TRIGGER reject_no_finding")
        db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at) VALUES ('outer','test','marker','now')")
    assert store.record_steward_outcome(outcome)
    assert not store.record_steward_outcome(outcome)
    current = store.read_steward(source.admission.attempt.binding.fence.key)
    assert current.component.state.groom_content.successful == source.content_signature
    assert current.component.state.groom.baseline_at == source.input_at
    assert current.component.state.triage == cc.StewardLaneState()
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM dedupe_keys WHERE namespace='outer'").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM charges").fetchone()[0] == 1
