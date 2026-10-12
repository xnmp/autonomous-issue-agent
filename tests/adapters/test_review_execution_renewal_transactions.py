"""Fixed execution grants use the existing SQLite intent and effect rail."""

from dataclasses import replace
import sqlite3
from datetime import timedelta

import pytest

from issue_agent.adapters.review_workflow_control import SQLiteReviewWorkflowControl
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from issue_agent.domain.labels import Label
from issue_agent.domain.review_rejection import CountingRule
from issue_agent.domain.review_workflow import PendingReviewRearm
from issue_agent.domain.review_workflow_policy import review_execution_ceiling_gate_id
from issue_agent.domain.workflow import (
    CancelRearm, ContinueGrantedRearm, KeepGuidance, OperatorActor, RequestReviewExecutionRenewal,
    RequestReviewVerdictRelease, RequestRearm, WorkflowCommitted, WorkflowConflict, WorkflowRejected,
)
from issue_agent.domain.review_workflow_policy import review_verdict_release_gate_id
from issue_agent.domain.workflow_identity import WorkflowWriter
from tests.adapters.test_review_execution_control import OWNER, current, dump, rig  # noqa: F401

ACTOR = OperatorActor("operator:test", "test")
# Renewal selects the baseline's verdict, so it cannot run over the stand-in baseline.
without_baseline = pytest.mark.parametrize("rig", [False], indirect=True)


def test_repeated_public_control_polling_does_not_initialize_clear_or_rearm_execution(rig):
    store, before, gateway, selection, _ = rig
    saved = dump(store)
    gateway.calls.clear()
    control = workflow_control(rig)
    for _ in range(5):
        result = control.review_control_view(selection.observation.key)
        assert result.snapshot == before
        assert result.execution_ceiling == 3
        assert result.execution_episode_open is False
    assert gateway.calls == [] and dump(store) == saved


def workflow_control(rig):
    store, _, gateway, _, _ = rig
    return SQLiteReviewWorkflowControl(
        store, WorkflowWriter("o/r", "review", OWNER), gateway=gateway,
        ceiling=3, execution_ceiling=3, trigger_label=Label.AI_PR_OPEN, rule=CountingRule("review-bot"),
    )


def renewal_request(rig, request_id="renew:1"):
    store, _, gateway, selection, _ = rig
    snapshot = current(store, selection)
    execution = snapshot.execution
    return RequestReviewExecutionRenewal(
        request_id, snapshot.revision, review_execution_ceiling_gate_id(snapshot, gateway.view.head_ref_oid, 3),
        KeepGuidance(), execution.binding.execution_epoch, execution.binding.execution_episode,
        expected_binding=snapshot.binding,
    )


def _counts(store, *tables):
    with store.transaction() as db:
        return tuple(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables)


def exhaust_with_three_settled_returns(rig, label="later", first=1):
    from issue_agent.domain.actions import AgentKind
    from issue_agent.domain.outcome import AgentResult, ExitReason
    from tests.adapters.test_review_execution_admission import request
    from tests.adapters.test_review_execution_launch import register
    from tests.adapters.test_review_execution_return import returned, settlement
    from tests.adapters.test_review_execution_stage import stage

    store, _, _, selection, execution = rig
    for index in range(3):
        if not store.active_dispatch_claims("o/r", "review"):
            assert store.claim_dispatch("o/r", "review", -9, AgentKind.REVIEW.name, OWNER)
        occurrence = store.active_dispatch_claims("o/r", "review")[0].claim_key
        snapshot = current(store, selection)
        round = execution.preflight(replace(selection, request_id=f"{label}:failure:{index}",
            expected=snapshot.revision, binding=snapshot.execution.binding, claim_occurrence_id=occurrence,
            observation=replace(selection.observation, observed_at=store.now()), failure_ceiling=3))
        child_stage = stage(round)
        execution.register_stage(round.round.round_id, current(store, selection).revision, child_stage)
        paid = request(child_stage.children[0], index=index + first)
        assert store.reserve(paid).admitted
        register(store, paid)
        execution.seal_completed_planning(round.round.round_id, current(store, selection).revision)
        failure = returned(store, paid, result=AgentResult(True, status="error", exit_reason=ExitReason.AGENT_ERROR))
        store.settle_review_return(settlement(paid, amount=6), failure)
        execution.finalize_completed_round(round.round.round_id)
        assert store.release_dispatch_claim("o/r", "review", -9, OWNER)


@without_baseline
def test_public_renewal_resets_only_execution_and_exact_retry_is_sql_only_after_restart(rig):
    store, _, gateway, selection, _ = rig
    exhaust_with_three_settled_returns(rig)
    before = current(store, selection)
    counts = _counts(store, "lifecycle_commands", "budget_reservations", "runs")
    command = renewal_request(rig)
    result = workflow_control(rig).request_rearm(command, ACTOR)
    assert isinstance(result, WorkflowCommitted) and not result.idempotent
    after = result.snapshot
    assert isinstance(after.intent, PendingReviewRearm)
    assert after.episode.episode_id == before.episode.episode_id
    assert after.episode.guidance == before.episode.guidance
    assert after.execution.state.failures == after.execution.state.pin_deferrals == 0
    assert after.execution.binding.execution_episode != before.execution.binding.execution_episode
    assert after.execution.state.displayed_round == before.execution.state.displayed_round
    assert after.intent.authorized == after.intent.dismissals == ()
    assert _counts(store, "lifecycle_commands", "budget_reservations", "runs") == (
        counts[0] + 1, counts[1], counts[2])
    saved = dump(store)
    gateway.calls.clear()
    assert workflow_control(rig).request_rearm(command, ACTOR) == WorkflowCommitted(after, idempotent=True)
    assert gateway.calls == [] and dump(store) == saved
    assert isinstance(workflow_control(rig).request_rearm(replace(command, execution_episode="changed"), ACTOR),
                      WorkflowConflict)
    assert current(store, selection) == after and dump(store) == saved


@without_baseline
def test_current_renewal_read_does_not_traverse_the_previous_origin(rig):
    store, before, _, selection, _ = rig
    exhaust_with_three_settled_returns(rig)
    result = workflow_control(rig).request_rearm(renewal_request(rig), ACTOR)
    assert isinstance(result, WorkflowCommitted)
    with store.transaction() as db:
        db.execute("DELETE FROM dedupe_keys WHERE namespace='review-execution-origin' AND key=?",
                   (before.execution.origin.evidence_id,))
    assert current(store, selection) == result.snapshot


@without_baseline
@pytest.mark.parametrize("source", ["origin", "request"])
def test_current_renewal_refuses_a_missing_selected_origin_or_request(rig, source):
    store, _, _, selection, _ = rig
    exhaust_with_three_settled_returns(rig)
    command = renewal_request(rig)
    result = workflow_control(rig).request_rearm(command, ACTOR)
    assert isinstance(result, WorkflowCommitted)
    with store.transaction() as db:
        namespace, identity = (("review-execution-origin", result.snapshot.execution.origin.evidence_id)
            if source == "origin" else ("workflow-request", command.request_id))
        db.execute("DELETE FROM dedupe_keys WHERE namespace=? AND key=?", (namespace, identity))
    saved = dump(store)
    with pytest.raises(WorkflowAuthorityError):
        workflow_control(rig).request_rearm(command, ACTOR)
    assert dump(store) == saved


def test_unfinished_round_prevents_renewal_without_intent_money_or_commands(rig):
    store, _, _, selection, execution_control = rig
    execution_control.preflight(selection)
    command = renewal_request(rig)
    saved = dump(store)
    result = workflow_control(rig).request_rearm(command, ACTOR)
    assert isinstance(result, WorkflowRejected) and "old execution episode round" in result.reason
    assert dump(store) == saved


def test_earlier_generation_unfinished_round_blocks_even_when_current_suffix_is_terminal(rig):
    from tests.adapters.test_review_productive_settlement import confirmed_escalation

    store, snapshot, gateway, selection, execution = rig
    old = execution.preflight(selection)
    later = replace(selection, request_id="later:ceiling", expected=current(store, selection).revision)
    _, _, _, _, _, escalation = confirmed_escalation((store, snapshot, gateway, later, execution), cause="ceiling")
    workflow = workflow_control(rig)
    workflow._ceiling = 1
    assert isinstance(workflow.settle_submission(escalation.command_id), WorkflowCommitted)
    before = current(store, selection)
    accepted = workflow.request_rearm(RequestRearm("rejection:advance", before.revision,
        before.gates[0].gate_id, KeepGuidance(), expected_binding=before.binding), ACTOR)
    assert isinstance(accepted, WorkflowCommitted)
    pending = accepted.snapshot
    assert isinstance(workflow.cancel_rearm(CancelRearm("cancel:rejection", pending.revision,
        pending.intent.request_id, "stop", expected_binding=pending.binding), ACTOR), WorkflowCommitted)
    advanced = current(store, selection)
    assert advanced.execution.baseline.through_sequence == advanced.execution.last_sequence
    assert advanced.revision.fence.generation > old.round.binding.rejection.fence.generation
    saved = dump(store)
    result = workflow.request_rearm(renewal_request(rig), ACTOR)
    assert isinstance(result, WorkflowRejected) and "old execution episode round" in result.reason
    assert dump(store) == saved


def test_public_confirmed_verdict_release_preserves_execution_without_review_dismissals(rig):
    from issue_agent.adapters.control_store import load_bound_review_submission
    from tests.adapters.test_review_productive_settlement import confirmed_escalation

    store, gateway, selection, _, _, command = confirmed_escalation(rig)
    workflow = workflow_control(rig)
    assert isinstance(workflow.settle_submission(command.command_id), WorkflowCommitted)
    before = current(store, selection)
    with store.transaction() as db:
        receipt = load_bound_review_submission(db, command.command_id)
    release = RequestReviewVerdictRelease("release:1", before.revision,
        review_verdict_release_gate_id(before, receipt.submission), KeepGuidance(),
        before.execution.binding.execution_epoch, before.execution.binding.execution_episode,
        command.command_id, expected_binding=before.binding)
    result = workflow.request_rearm(release, ACTOR)
    assert isinstance(result, WorkflowCommitted)
    after = result.snapshot
    assert after.execution.binding.execution_episode == before.execution.binding.execution_episode
    assert after.execution.state.failures == before.execution.state.failures
    assert after.intent.authorized == after.intent.dismissals == ()
    saved = dump(store)
    assert workflow_control(rig).request_rearm(release, ACTOR) == WorkflowCommitted(after, idempotent=True)
    assert dump(store) == saved
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM lifecycle_commands WHERE kind='dismiss_review'").fetchone()[0] == 0


@pytest.mark.parametrize("corruption", [None, "effect_executor", "effect_digest", "command_digest"])
def test_renewal_cancel_continue_retains_exact_escalate_hold_until_selected_release(rig, corruption):
    from issue_agent.adapters.control_store import load_bound_review_submission
    from issue_agent.domain.review_rejection import ReviewVerdictHold
    from issue_agent.domain.contracts import EffectReceipt, EffectStatus
    from tests.adapters.test_review_productive_settlement import confirmed_escalation

    store, gateway, selection, _, _, command = confirmed_escalation(rig)
    workflow = workflow_control(rig)
    assert isinstance(workflow.settle_submission(command.command_id), WorkflowCommitted)
    gateway.view = replace(gateway.view, head_ref_oid="d" * 40)
    later_selection = replace(selection, observation=replace(selection.observation, head_sha="d" * 40))
    later_rig = (store, rig[1], gateway, later_selection, rig[4])
    exhaust_with_three_settled_returns(later_rig)
    with store.transaction() as db:
        receipt = load_bound_review_submission(db, command.command_id)
    renewed = workflow.request_rearm(renewal_request(rig), ACTOR)
    assert isinstance(renewed, WorkflowCommitted)
    held = renewed.snapshot
    cause = ReviewVerdictHold(command.command_id, receipt.head_sha)
    selected = next(g for g in held.gates if getattr(g, "cause", None) == cause)
    assert held.execution.state.failures == 0
    stopped = workflow.cancel_rearm(CancelRearm("cancel:retained", held.revision, held.intent.request_id,
        "pause", expected_binding=held.binding), ACTOR).snapshot
    continued = workflow.continue_rearm(ContinueGrantedRearm("continue:retained", stopped.revision,
        stopped.intent.request_id, expected_binding=stopped.binding), ACTOR).snapshot
    assert selected in continued.gates and continued.intent.grant == held.intent.grant
    projection = continued.intent.projection.command_id
    store.begin_command(projection)
    store.record_effect(EffectReceipt(projection, OWNER, store.now(), store.now(), EffectStatus.SUCCEEDED))
    gateway.view = replace(gateway.view, labels=frozenset({Label.NEEDS_HUMAN_REVIEW}))
    complete = workflow.reconcile_rearm(continued.revision).snapshot
    assert selected in complete.gates
    release = RequestReviewVerdictRelease("release:retained", complete.revision, selected.gate_id, KeepGuidance(),
        complete.execution.binding.execution_epoch, complete.execution.binding.execution_episode,
        command.command_id, expected_binding=complete.binding)
    if corruption is not None:
        with store.transaction() as db:
            if corruption == "effect_executor":
                db.execute("UPDATE effect_receipts SET executor_identity='wrong:owner' WHERE command_id=?",
                           (command.command_id,))
            elif corruption == "effect_digest":
                db.execute("UPDATE effect_receipts SET receipt_digest=? WHERE command_id=?",
                           ("x" * 64, command.command_id))
            else:
                db.execute("UPDATE lifecycle_commands SET payload_digest=? WHERE command_id=?",
                           ("x" * 64, command.command_id))
        gateway.view = replace(gateway.view, head_ref_oid=receipt.head_sha)
        before_corrupt = dump(store)
        refused = workflow.request_rearm(release, ACTOR)
        assert isinstance(refused, WorkflowRejected) and "selected verdict evidence" in refused.reason
        assert current(store, selection) == complete and dump(store) == before_corrupt
        return
    before_stale = dump(store)
    assert isinstance(workflow.request_rearm(release, ACTOR), WorkflowRejected)
    assert dump(store) == before_stale
    gateway.view = replace(gateway.view, head_ref_oid=receipt.head_sha)
    released = workflow.request_rearm(release, ACTOR)
    assert isinstance(released, WorkflowCommitted)
    assert selected not in released.snapshot.gates
    assert released.snapshot.execution.origin == held.execution.origin
    assert released.snapshot.execution.state.failures == 0
    assert released.snapshot.intent.authorized == released.snapshot.intent.dismissals == ()


def test_unknown_selected_verdict_returns_typed_refusal_without_writes(rig):
    from issue_agent.domain.contracts import CommandId

    store, _, _, selection, _ = rig
    snapshot = current(store, selection)
    command = RequestReviewVerdictRelease("release:unknown", snapshot.revision, "verdict:unknown", KeepGuidance(),
        snapshot.execution.binding.execution_epoch, snapshot.execution.binding.execution_episode,
        CommandId("unknown:command"), expected_binding=snapshot.binding)
    saved = dump(store)
    result = workflow_control(rig).request_rearm(command, ACTOR)
    assert isinstance(result, WorkflowRejected)
    assert dump(store) == saved


@without_baseline
@pytest.mark.parametrize("cancel_continue", [False, True])
def test_lost_projection_reply_restart_and_continue_never_renew_twice(rig, tmp_path, cancel_continue):
    from issue_agent.adapters.effects import GitHubEffectExecutor
    from issue_agent.domain.review_workflow import CompletedReviewRearm, SupersededReviewRearm
    from issue_agent.domain.workflow import RemoteOutcomeUncertain
    from issue_agent.domain.review import UNPRODUCTIVE_ROUND_HOLD
    from tests.fakes.persistent_github import PersistentGitHub
    from tests.adapters.test_review_workflow_control import raising

    store, _, gateway, selection, _ = rig
    exhaust_with_three_settled_returns(rig)
    reservations = _counts(store, "budget_reservations")
    clock = [store.now()]
    store._now = lambda: clock[0]
    lost = {"relabel_pr_applied"}
    remote = PersistentGitHub(tmp_path / "remote.sqlite", clock=store.now, fault=raising(set(), lost))
    remote.seed_pr(replace(gateway.view, labels=frozenset({Label.NEEDS_HUMAN_REVIEW})))

    def control():
        return SQLiteReviewWorkflowControl(store, WorkflowWriter("o/r", "review", OWNER), gateway=remote,
            ceiling=3, execution_ceiling=3, trigger_label=Label.AI_PR_OPEN, rule=CountingRule("review-bot"),
            external_holds=lambda _pr, _view: (UNPRODUCTIVE_ROUND_HOLD,)
            if current(store, selection).execution.state.failures >= 3 else ())

    command = renewal_request(rig)
    accepted = control().request_rearm(command, ACTOR)
    assert isinstance(accepted, WorkflowCommitted)
    granted = accepted.snapshot.intent.grant
    origin = accepted.snapshot.execution.origin
    executor = GitHubEffectExecutor(remote, store, repository="o/r", identity=OWNER, log=lambda _: None)
    executor.drain_pending_commands()
    pending = current(store, selection)
    assert pending.intent.grant == granted and pending.execution.origin == origin
    with store.transaction() as db:
        assert db.execute("SELECT state FROM lifecycle_commands WHERE command_id=?",
                          (pending.intent.projection.command_id,)).fetchone()[0] == "unknown_outcome"
    assert isinstance(control().request_rearm(command, ACTOR), WorkflowCommitted)
    lost.clear()
    if cancel_continue:
        cancelled = control().cancel_rearm(CancelRearm("cancel:lost", pending.revision, pending.intent.request_id,
            "pause", expected_binding=pending.binding), ACTOR)
        assert isinstance(cancelled, WorkflowCommitted)
        stopped = current(store, selection)
        continued = control().continue_rearm(ContinueGrantedRearm("continue:lost", stopped.revision,
            stopped.intent.request_id, expected_binding=stopped.binding), ACTOR)
        assert isinstance(continued, WorkflowCommitted)
        assert continued.snapshot.intent.grant == granted
    clock[0] += timedelta(hours=1)
    GitHubEffectExecutor(remote, store, repository="o/r", identity=OWNER, log=lambda _: None).drain_pending_commands()
    if cancel_continue:
        for _ in range(24):
            clock[0] += timedelta(minutes=30)
            GitHubEffectExecutor(remote, store, repository="o/r", identity=OWNER, log=lambda _: None).drain_pending_commands()
            state = current(store, selection)
            assert state.intent.grant == granted and state.execution.origin == origin
            if not any(isinstance(getattr(gate, "cause", None), RemoteOutcomeUncertain) for gate in state.gates):
                break
        assert not any(isinstance(getattr(gate, "cause", None), RemoteOutcomeUncertain) for gate in state.gates)
    after = current(store, selection)
    result = control().reconcile_rearm(after.revision)
    assert isinstance(result, WorkflowCommitted)
    if cancel_continue and isinstance(result.snapshot.intent, SupersededReviewRearm):
        stopped = result.snapshot
        result = control().continue_rearm(ContinueGrantedRearm("continue:resolved", stopped.revision,
            stopped.intent.request_id, expected_binding=stopped.binding), ACTOR)
        assert isinstance(result, WorkflowCommitted) and result.snapshot.intent.grant == granted
        GitHubEffectExecutor(remote, store, repository="o/r", identity=OWNER, log=lambda _: None).drain_pending_commands()
        result = control().reconcile_rearm(current(store, selection).revision)
    assert isinstance(result.snapshot.intent, CompletedReviewRearm)
    assert result.snapshot.intent.grant == granted and result.snapshot.execution.origin == origin
    assert result.snapshot.execution.state.failures == 0
    assert remote.get_pr(9).labels == frozenset({Label.AI_PR_OPEN})
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='review-execution-origin'").fetchone()[0] == 2
    assert _counts(store, "budget_reservations") == reservations
    if not cancel_continue:
        exhaust_with_three_settled_returns(rig, "renewed", first=4)
        exhausted = current(store, selection)
        assert exhausted.execution.state.failures == 3
        next_grant = control().request_rearm(renewal_request(rig, "renew:later-exhaustion"), ACTOR)
        assert isinstance(next_grant, WorkflowCommitted)
        assert next_grant.snapshot.execution.origin != origin
        assert next_grant.snapshot.execution.state.failures == 0
        assert next_grant.snapshot.intent.grant.previous_episode == granted.renewed_episode
        with store.transaction() as db:
            assert db.execute("SELECT SUM(amount_micros) FROM charges").fetchone()[0] == 36


@without_baseline
def test_origin_write_error_rolls_back_renewal_when_caller_catches_and_commits(rig, monkeypatch):
    store, _, _, _, _ = rig
    exhaust_with_three_settled_returns(rig)
    tables = ("workflow_rearms", "lifecycle_commands", "budget_reservations", "workflow_evidence")
    counts = _counts(store, *tables)
    control = workflow_control(rig)
    command = renewal_request(rig)
    saved = current(store, rig[3])
    original = control._commit

    def catch_inner(db, *args, **kwargs):
        db.execute("CREATE TEMP TRIGGER fail_renewal BEFORE INSERT ON dedupe_keys "
                   "WHEN NEW.namespace='review-execution-origin' BEGIN SELECT RAISE(FAIL,'origin failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match="origin failure"):
            original(db, *args, **kwargs)
        db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at) VALUES ('test','test','outer-committed',?)",
                   (store.now().isoformat(),))
        return WorkflowConflict(saved.revision, "caught origin failure")

    monkeypatch.setattr(control, "_commit", catch_inner)
    assert isinstance(control.request_rearm(command, ACTOR), WorkflowConflict)
    assert current(store, rig[3]) == saved
    with store.transaction() as db:
        assert db.execute("SELECT 1 FROM dedupe_keys WHERE namespace='test'").fetchone() is not None
    assert _counts(store, *tables) == counts
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='workflow-request'").fetchone()[0] == 0
