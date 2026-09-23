"""Split capture/reset/finalization use actual isolated admissions and settlements."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
import sqlite3
from zoneinfo import ZoneInfo

import pytest

from issue_agent.adapters.control_store import ReservationRequest, SettlementRequest, SQLiteControlStore
from issue_agent.adapters.workflow_codec import encode_workflow
from issue_agent.adapters.workflow_control import SQLiteWorkflowControl
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from issue_agent.domain.contracts import BudgetScope, Money, PriceQuote, PricingBasis, ReservationId, RunId, WorkflowAttemptIdentity
from issue_agent.domain.resolve_return import ResolveInputs, ReturnedResolve
from issue_agent.domain.outcome import ExitReason
from issue_agent.domain.resolve_split import (
    AdjudicationInput, ContractResetCompleted, ContractResetIntent, ContractResetResult,
    ContractRevision, NeedsAdjudication, ReauthorInput, ReauthorResetIntent, SplitContext, SplitImplement, SplitReply,
    SplitSelection, UnavailableContractRevision,
)
from issue_agent.domain.workflow import AuthoredResolve, LinkedPrPresent, ResolveStage, WorkflowCommitted, WorkflowConflict
from tests.adapters.test_workflow_control import NOW, WRITER, return_observations
from tests.fakes.workflow_authority import install_workflow, resolve_snapshot


@pytest.fixture
def store(tmp_path):
    control = SQLiteControlStore(str(tmp_path / "control.sqlite"), now=lambda: NOW)
    control.initialize()
    install_workflow(control, resolve_snapshot())
    return control


def authority(store, *, ceiling=2):
    return SQLiteWorkflowControl(store, WRITER, resolve_ceiling=3, max_contract_rounds=ceiling)


def state(store):
    return authority(store).load_workflow(resolve_snapshot().binding.fence.key)


def context():
    return SplitContext("fix/1-contract", SplitSelection("codex", "model"), SplitReply("alice", "reply:1", "exact reply"))


def capture(store, stage, stage_input, *, name=None, status=None, revision=None, verdict="", settle=True, is_error=False, exit_reason=None, reserve_only=False, fingerprint="a" * 64):
    current = state(store)
    name = name or stage.value
    attempt = WorkflowAttemptIdentity(current.binding, RunId(name), "leg:" + name)
    encoded = encode_workflow(stage_input)
    metadata = json.dumps(dict(repository="o/r", role="fix", issue=1, kind=stage.value,
                               leg_id=attempt.leg_id, runtime="codex", effort=None,
                               input_fingerprint=fingerprint, split_input=encoded,
                               resolve_inputs=encode_workflow(ResolveInputs("title", "body", "alice")),
                               split_fingerprint=hashlib.sha256(encoded.encode()).hexdigest()))
    request = ReservationRequest(ReservationId("reserve:" + name), attempt.run_id, metadata,
                                 BudgetScope("o/r", "daily", "USD", "2026-09-14", NOW, NOW + timedelta(days=1), ZoneInfo("UTC")),
                                 PriceQuote("model", "card", "1", NOW, Money("USD", 1)), Money("USD", 100), "config", "o/r",
                                 kind=stage.value, owner_id=WRITER.owner_id, workflow=current.binding,
                                 adjudication_ceiling=2 if stage is ResolveStage.ADJUDICATE else None)
    reservation = store.reserve(request)
    if reserve_only:
        return reservation
    assert reservation.admitted, reservation.detail
    returned = ReturnedResolve(attempt, stage, ResolveInputs("title", "body", "alice"), is_error,
                               status or ("tests_authored" if stage is ResolveStage.AUTHOR else "contract_change" if stage is ResolveStage.IMPLEMENT else ""),
                               "summary", "exact objection", "", exit_reason, None, NOW,
                               split_input=stage_input, verdict=verdict, rationale="the contract stands" if verdict else "", contract_revision=revision)
    if settle:
        settlement = SettlementRequest(attempt.run_id, Money("USD", 1), PricingBasis.PROVIDER_REPORTED,
                                       f"codex:no-session:{name}", None, None, None)
        authority(store).settle_return(settlement, returned)
    return returned


def finalize(store, returned, **kwargs):
    return authority(store).finalize_return(returned, *return_observations(returned), **kwargs)


def author(store):
    returned = capture(store, ResolveStage.AUTHOR, context(), revision=ContractRevision(context().branch, "a" * 40))
    result = finalize(store, returned)
    assert isinstance(result, WorkflowCommitted)
    cursor = result.snapshot.episode.continuation.split
    assert isinstance(cursor, SplitImplement)
    return cursor


def petition(store):
    ready = author(store)
    returned = capture(store, ResolveStage.IMPLEMENT, ready)
    result = finalize(store, returned)
    assert isinstance(result, WorkflowCommitted)
    cursor = result.snapshot.episode.continuation.split
    assert isinstance(cursor, NeedsAdjudication)
    return cursor


def reset_ack(store, cursor):
    intent = ContractResetIntent(state(store).binding, cursor)
    ack = ContractResetCompleted(intent, ContractResetResult(cursor.baseline.revision, intent.archive_ref), NOW)
    with authority(store).contract_reset_authority(intent) as previous:
        return previous or authority(store).complete_contract_reset(ack)


def test_missing_baseline_keeps_raw_author_held_then_recovers_without_new_paid_run(store):
    returned = capture(store, ResolveStage.AUTHOR, context(), revision=UnavailableContractRevision("pin read unavailable"))
    assert isinstance(finalize(store, returned), WorkflowConflict)
    assert authority(store).pending_returns()[0].returned == returned
    assert not isinstance(state(store).episode.continuation, AuthoredResolve)
    baseline = ContractRevision(context().branch, "a" * 40)
    with authority(store).return_effect_authority(returned):
        completed = finalize(store, returned, contract_revision=baseline)
    assert isinstance(completed, WorkflowCommitted)
    assert completed.snapshot.episode.continuation.split.baseline.revision == baseline
    assert authority(store).returned_result(returned.attempt) == returned
    assert finalize(store, returned).idempotent


def test_new_split_admission_cannot_bypass_atomic_return_with_direct_classification(store):
    from issue_agent.domain.workflow import ResolveAttemptResult, ResolveResultKind
    returned = capture(store, ResolveStage.AUTHOR, context(), settle=False)
    store.settle(SettlementRequest(returned.attempt.run_id, Money("USD", 1), PricingBasis.PROVIDER_REPORTED,
                                  "codex:no-session:AUTHOR", None, None, None))
    result = authority(store).record_attempt(ResolveAttemptResult(returned.attempt,
        ResolveResultKind.STAGE_PROGRESSED, stage=ResolveStage.AUTHOR))
    assert isinstance(result, WorkflowConflict)
    assert not isinstance(state(store).episode.continuation, AuthoredResolve)
    assert authority(store).returned_result(returned.attempt) is None


@pytest.mark.parametrize("status,error,failures", [("blocked", False, 0), ("failed", True, 1)])
def test_implementation_retry_uses_returned_predecessor_and_existing_author(store, status, error, failures):
    ready = author(store)
    returned = capture(store, ResolveStage.IMPLEMENT, ready, status=status, is_error=error)
    completed = finalize(store, returned)
    assert isinstance(completed, WorkflowCommitted)
    continuation = completed.snapshot.episode.continuation
    assert isinstance(continuation, AuthoredResolve)
    assert isinstance(continuation.split, SplitImplement)
    assert continuation.author_attempt == ready.source
    assert continuation.split.source == returned.attempt
    assert completed.snapshot.episode.failures == failures
    retry = capture(store, ResolveStage.IMPLEMENT, continuation.split, name="implement-retry", reserve_only=True)
    assert retry.admitted, retry.detail


def test_infra_implementation_advances_only_retry_predecessor_not_clean_author_baseline(store):
    ready = author(store)
    before = state(store).episode.continuation
    returned = capture(store, ResolveStage.IMPLEMENT, ready, is_error=True, exit_reason=ExitReason.KILLED_EXTERNALLY)
    completed = finalize(store, returned)
    assert isinstance(completed, WorkflowCommitted)
    after = completed.snapshot.episode.continuation
    assert isinstance(after, AuthoredResolve) and isinstance(after.split, SplitImplement)
    assert after.split.source == returned.attempt and after.split.source != after.author_attempt
    assert after.split.context == ready.context and after.split.baseline == ready.baseline
    assert after.split_applied_admission_version > before.split_applied_admission_version
    assert after.session == before.session
    assert after.last_applied_admission_version == before.last_applied_admission_version
    assert completed.snapshot.episode.failures == 0
    retry = capture(store, ResolveStage.IMPLEMENT, after.split, name="infra-implement-retry", reserve_only=True)
    assert retry.admitted, retry.detail


@pytest.mark.parametrize("ceiling", [-1, 5, True])
def test_adapter_rejects_unsupported_contract_round_capacity(store, ceiling):
    with pytest.raises(ValueError):
        authority(store, ceiling=ceiling)


@pytest.mark.parametrize("verdict", ["upheld", "revised"])
def test_returned_adjudication_survives_restart_and_cannot_reset_again(store, verdict):
    cursor = petition(store)
    ack = reset_ack(store, cursor)
    returned = capture(store, ResolveStage.ADJUDICATE, AdjudicationInput(ack), verdict=verdict,
                       revision=ContractRevision(context().branch, ("a" if verdict == "upheld" else "b") * 40))
    restarted = SQLiteControlStore(store.path, now=lambda: NOW)
    with pytest.raises(WorkflowAuthorityError, match="unheld"):
        with authority(restarted).contract_reset_authority(ack.intent):
            pytest.fail("reset was authorized after the paid return")
    completed = finalize(restarted, returned)
    assert isinstance(completed, WorkflowCommitted)
    ready = completed.snapshot.episode.continuation.split
    assert isinstance(ready, SplitImplement)
    assert ready.context.human_reply == "exact reply"
    assert ready.context.uphold_note == ("the contract stands" if verdict == "upheld" else "")
    assert ready.baseline.revision.head_sha == ("a" * 40 if verdict == "upheld" else "b" * 40)
    with pytest.raises(WorkflowAuthorityError, match="current petition"):
        with authority(restarted).contract_reset_authority(ack.intent):
            pytest.fail("reset was authorized after finalization")
    assert finalize(restarted, returned).idempotent
    assert len(state(restarted).episode.continuation.split.context.trail) == 1


def test_reset_ack_replay_preserves_original_evidence_and_no_sql_transaction_spans_io(store):
    cursor = petition(store)
    ack = reset_ack(store, cursor)
    with authority(store).contract_reset_authority(ack.intent) as completed:
        assert completed == ack
        # A different connection can commit while the caller performs worktree I/O.
        with store.transaction() as db:
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES ('test-io','o/r','marker',?,'{}')", (NOW.isoformat(),))
        replayed = authority(store).complete_contract_reset(ack)
    assert replayed == ack


def test_reset_ack_requires_durable_intent_before_any_adjudication_admission(store):
    cursor = petition(store)
    intent = ContractResetIntent(state(store).binding, cursor)
    ack = ContractResetCompleted(intent, ContractResetResult(cursor.baseline.revision, intent.archive_ref), NOW)
    with pytest.raises(WorkflowAuthorityError, match="durable intent"):
        authority(store).complete_contract_reset(ack)


def test_active_adjudication_prevents_repeating_reset(store):
    cursor = petition(store)
    ack = reset_ack(store, cursor)
    capture(store, ResolveStage.ADJUDICATE, AdjudicationInput(ack), settle=False)
    with pytest.raises(WorkflowAuthorityError, match="active"):
        with authority(store).contract_reset_authority(ack.intent):
            pytest.fail("reset was authorized while adjudication was admitted")


def test_split_finalization_rolls_back_baseline_cursor_and_disposition_together(store):
    returned = capture(store, ResolveStage.AUTHOR, context(), revision=UnavailableContractRevision("read failed"))
    before = state(store)
    with store.transaction() as db:
        db.execute("CREATE TRIGGER fail_baseline BEFORE INSERT ON dedupe_keys WHEN NEW.namespace='workflow-return-contract' BEGIN SELECT RAISE(ABORT,'storage failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="storage failure"):
        finalize(store, returned, contract_revision=ContractRevision(context().branch, "a" * 40))
    assert state(store) == before
    assert authority(store).pending_returns()[0].returned == returned


def test_zero_adjudication_allowance_escalates_without_reset_or_budget_review(store):
    ready = author(store)
    returned = capture(store, ResolveStage.IMPLEMENT, ready)
    result = authority(store, ceiling=0).finalize_return(returned, *return_observations(returned))
    assert isinstance(result, WorkflowCommitted)
    assert result.snapshot.episode.failures == 0
    command = store.pending_commands(repository="o/r")[0].command
    assert "Contract dispute unresolved" in command.payload["comment"]
    assert "needs-budget-review" not in str(command.payload)
    continuation = result.snapshot.episode.continuation
    assert continuation.split is None
    assert continuation.split_applied_admission_version > ready.admission_version
    refused_petition = NeedsAdjudication(returned.attempt, continuation.split_applied_admission_version, ready.context, ready.baseline, returned.needs)
    intent = ContractResetIntent(result.snapshot.binding, refused_petition)
    with pytest.raises(WorkflowAuthorityError, match="current petition"):
        with authority(store, ceiling=0).contract_reset_authority(intent):
            pytest.fail("exhausted allowance permitted a reset")


def test_infra_adjudication_retry_has_new_reset_identity_but_same_contract_and_allowance(store):
    original = petition(store)
    first_ack = reset_ack(store, original)
    before = state(store).episode.continuation
    returned = capture(store, ResolveStage.ADJUDICATE, AdjudicationInput(first_ack),
                       is_error=True, exit_reason=ExitReason.KILLED_EXTERNALLY)
    completed = finalize(store, returned)
    assert isinstance(completed, WorkflowCommitted)
    current = completed.snapshot.episode.continuation
    assert isinstance(current.split, NeedsAdjudication)
    assert current.split.source == returned.attempt and current.split.baseline == original.baseline
    assert current.session == before.session and current.last_applied_admission_version == before.last_applied_admission_version
    assert current.split.context.trail == original.context.trail
    second_ack = reset_ack(store, current.split)
    assert second_ack.intent.intent_id != first_ack.intent.intent_id
    assert second_ack.result.archive_ref != first_ack.result.archive_ref
    second = capture(store, ResolveStage.ADJUDICATE, AdjudicationInput(second_ack), name="ADJUDICATE-2",
                     is_error=True, exit_reason=ExitReason.KILLED_EXTERNALLY)
    exhausted = finalize(store, second)
    assert isinstance(exhausted, WorkflowCommitted) and exhausted.snapshot.episode.failures == 0
    assert "Contract dispute unresolved" in store.pending_commands(repository="o/r")[0].command.payload["comment"]
    terminal = exhausted.snapshot.episode.continuation
    assert terminal.split is None
    assert terminal.split_applied_admission_version > current.split.admission_version
    assert terminal.last_applied_admission_version == before.last_applied_admission_version
    assert terminal.session == before.session
    replay = finalize(store, returned)
    assert replay.idempotent and replay.snapshot.episode.continuation == terminal
    with pytest.raises(WorkflowAuthorityError, match="current petition"):
        with authority(store).contract_reset_authority(second_ack.intent):
            pytest.fail("infra retry replenished the adjudication allowance")


@pytest.mark.parametrize("field", ["inputs", "split_input"])
def test_return_cannot_replace_the_actual_admitted_inputs(store, field):
    returned = capture(store, ResolveStage.AUTHOR, context(), revision=ContractRevision(context().branch, "a" * 40))
    changes = dict(inputs=ResolveInputs("forged title", "body", "alice")) if field == "inputs" else dict(split_input=replace(context(), reply=SplitReply("alice", "reply:2", "forged reply")))
    with pytest.raises(WorkflowAuthorityError, match="admitted"):
        authority(store).capture_return(replace(returned, **changes))


def test_active_reset_blocks_admission_but_abandoned_window_does_not_deadlock_reauthor(store):
    cursor = petition(store)
    intent = ContractResetIntent(state(store).binding, cursor)
    with authority(store).contract_reset_authority(intent) as previous:
        assert previous is None
        refused = capture(store, ResolveStage.AUTHOR, context(), name="competing-author", reserve_only=True)
        assert not refused.admitted and "effect authority" in refused.detail
    replacement = authority(store).reauthor_reset_intent(state(store).binding, "b" * 64)
    assert isinstance(replacement, ReauthorResetIntent)
    with authority(store).contract_reset_authority(replacement):
        ack = authority(store).complete_contract_reset(ContractResetCompleted(
            replacement, ContractResetResult(replacement.baseline.revision, replacement.archive_ref), NOW,
        ))
    admitted = capture(store, ResolveStage.AUTHOR, ReauthorInput(ack, context()), name="new-author", reserve_only=True, fingerprint="b" * 64)
    assert admitted.admitted
    with pytest.raises(WorkflowAuthorityError, match="later active"):
        with authority(store).contract_reset_authority(intent):
            pytest.fail("old reset raced the newly admitted author")


def test_concurrent_reset_callers_only_one_receives_permission_for_git(store):
    cursor = petition(store)
    intent = ContractResetIntent(state(store).binding, cursor)
    ack = ContractResetCompleted(intent, ContractResetResult(cursor.baseline.revision, intent.archive_ref), NOW)
    first_entered, release_first, second_started = Event(), Event(), Event()
    effects = []

    def first():
        with authority(store).contract_reset_authority(intent) as prior:
            assert prior is None
            first_entered.set()
            assert release_first.wait(3)
            effects.append("reset Git")
            return authority(store).complete_contract_reset(ack)

    def second():
        second_started.set()
        with authority(store).contract_reset_authority(intent) as prior:
            assert prior is not None
            return prior

    with ThreadPoolExecutor(max_workers=2) as pool:
        initial = pool.submit(first)
        assert first_entered.wait(3)
        replay = pool.submit(second)
        assert second_started.wait(3)
        release_first.set()
        assert initial.result(timeout=3) == replay.result(timeout=3) == ack
    assert effects == ["reset Git"]


def test_verified_pr_on_infra_adjudication_clears_split_without_replacing_session(store):
    cursor = petition(store)
    ack = reset_ack(store, cursor)
    before = state(store).episode.continuation
    returned = capture(store, ResolveStage.ADJUDICATE, AdjudicationInput(ack), is_error=True, exit_reason=ExitReason.KILLED_EXTERNALLY)
    observed, _ = return_observations(returned)
    committed = authority(store).finalize_return(returned, observed, LinkedPrPresent(returned.attempt.binding.fence.key, 9, "head", NOW))
    assert isinstance(committed, WorkflowCommitted)
    continuation = committed.snapshot.episode.continuation
    assert continuation.split is None
    assert continuation.session == before.session
    assert continuation.last_applied_admission_version == before.last_applied_admission_version
    assert continuation.split_applied_admission_version > before.split_applied_admission_version
