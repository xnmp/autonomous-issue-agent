"""Observable, isolated SQLite workflow acceptance and recovery contracts."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from itertools import permutations
import json
import sqlite3
from zoneinfo import ZoneInfo

import pytest

from issue_agent.adapters.control_store import (
    AccountingDisposition, ControlStoreError, ReservationRequest, RetiredOwnerError,
    SettlementRequest, SQLiteControlStore,
)
from issue_agent.adapters.effects import command_for_action
from issue_agent.adapters.workflow_codec import decode_workflow, encode_workflow
from issue_agent.adapters.workflow_control import SQLiteWorkflowControl
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError, workflow_scope
from issue_agent.domain.contracts import (
    BudgetScope, CommandKind, EffectReceipt, EffectStatus, Money, PriceQuote,
    PricingBasis, ReservationId, RunId, WorkflowAttemptIdentity,
)
from issue_agent.domain.events import EventEnvelope, Producer, new_event_id
from issue_agent.domain.labels import Label
from issue_agent.domain.workflow import (
    AuthoredResolve, FreshResolve, ResolveAdmission, ResolveSession, ResolveStage,
    CancelRearm, CancelledRearm, ContinueGrantedRearm, EvidenceUnavailable,
    KeepGuidance, NoAuthorizedRemediation, NoLinkedPr, ObservedIssue, OpenGate,
    OperatorActor, PendingRearm, RemoteOutcomeUncertain, RequestRearm,
    ResolveAttemptResult, ResolveEpisode, ResolveRearmPreflight, ResolveResultKind, RetiredSettledCommand,
    RetiredUncertainCommand, SetGuidance, StopReason, WorkflowAbsent, WorkflowChanged, WorkflowCommitted,
    WorkflowConflict, WorkflowSnapshot,
)
from issue_agent.domain.workflow_identity import IssueRef, WorkflowKey, WorkflowRevision, WorkflowWriter
from issue_agent.domain.workflow_policy import (
    cancel_resolve_rearm, complete_resolve_rearm, continue_resolve_rearm,
    reconcile_resolve_ceiling, record_resolve_attempt, request_resolve_rearm,
    resolve_rearm_projection, supersede_resolve_rearm,
)
from issue_agent.domain.world import IssueView
from tests.fakes.workflow_authority import install_workflow, resolve_snapshot as initial_snapshot

NOW = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
WRITER = WorkflowWriter("o/r", "fix", "o/r:fix:deployment")
ACTOR = OperatorActor("operator:alice", "dashboard")


@pytest.fixture
def control(tmp_path):
    store = SQLiteControlStore(str(tmp_path / "control" / "db.sqlite"), now=lambda: NOW)
    store.initialize()
    return store


def resolve_snapshot(number=1, *, generation=2, version=1):
    return initial_snapshot(number, generation=generation, version=version)


def adapter(control):
    return SQLiteWorkflowControl(control, WRITER, resolve_ceiling=3)


def held(*, extra=()):
    state = replace(resolve_snapshot(), episode=ResolveEpisode("initial", 3), gates=extra)
    return reconcile_resolve_ceiling(state, ceiling=3, evidence_ids=("import",)).snapshot


def observation(state, labels=frozenset({Label.NEEDS_HUMAN_REVIEW}), at=NOW):
    return ObservedIssue(state.revision.fence.key, IssueView(
        state.revision.fence.key.target.number, labels=labels, updated_at="revision:1",
    ), at)


def enrich(proposal):
    preflight = proposal.audit.preflight
    action = resolve_rearm_projection(
        proposal.snapshot, expected_revision=preflight.issue.issue.updated_at,
        external_holds=preflight.external_holds,
    )
    command = command_for_action(action, repository="o/r", executor_identity=WRITER.owner_id, workflow=proposal.snapshot.binding)
    command = replace(command, command_id=proposal.snapshot.intent.required_commands[0], idempotency_key=f"workflow:{proposal.snapshot.intent.request_id}")
    return replace(proposal, commands=(command,))


def request(state, name="request:1", *, guidance=None, external_holds=()):
    gate = next(g for g in state.gates if getattr(g.cause, "episode_id", None) == state.episode.episode_id)
    return enrich(request_resolve_rearm(
        state, RequestRearm(name, state.revision, gate.gate_id, guidance or KeepGuidance()),
        ACTOR, observation(state), NoLinkedPr(state.revision.fence.key, NOW),
        now=NOW, active_attempts=(), external_holds=external_holds,
    ))


def commit(store, proposal):
    return adapter(store).commit_workflow(proposal.expected, (proposal.snapshot.authority,), proposal)


def evidence(control, state, name="observation:1", payload=None):
    payload = payload or ResolveRearmPreflight(observation(state, frozenset({Label.AI_READY})), NoLinkedPr(state.revision.fence.key, NOW))
    with control.transaction() as db:
        db.execute("INSERT INTO workflow_evidence VALUES (?, ?, ?, ?, NULL, 'observation', ?)",
                   (name, *workflow_scope(state.revision.fence.key), encode_workflow(payload)))


def run(control, state, *, name="run:1", status="completed", leg="leg:1", stage=ResolveStage.RESOLVE, session_id=None, runtime="codex", model="<runtime-default>"):
    """An isolated completed admission, including its immutable ordering receipt."""
    result = ResolveAttemptResult(WorkflowAttemptIdentity(state.binding, RunId(name), leg), ResolveResultKind.COUNTED_REQUEUE, "failed experiment", stage)
    control.start_run(RunId(name), json.dumps({"repository": "o/r", "issue": state.revision.fence.key.target.number, "number": state.revision.fence.key.target.number, "leg_id": leg, "kind": stage.value, "runtime": runtime}), stage.value, WRITER.owner_id, "config", state=status)
    with control.transaction() as db:
        db.execute("UPDATE runs SET workflow_binding_json=? WHERE run_id=?", (encode_workflow(state.binding), name))
        db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES ('workflow-admission', ?, ?, ?, ?)",
                   (WRITER.repository, name, NOW.isoformat(), encode_workflow(admission(state, result))))
        if session_id is not None:
            result = replace(result, session=ResolveSession(result.attempt, session_id, runtime, model))
        if status == "completed" or session_id is not None:
            receipt_id = f"{runtime}:{session_id or 'no-session'}:{name}"
            db.execute("UPDATE runs SET provider_receipt_id=? WHERE run_id=?", (receipt_id, name))
            db.execute(
                "INSERT INTO budget_reservations(reservation_id,run_id,repository,workload_repository,dimension,period_id,period_start,period_end,period_timezone,currency,maximum_micros,cap_micros,model,rate_card_id,rate_card_version,quote_effective_at,config_version,state,created_at,updated_at) "
                "VALUES (?,?,'o/r','o/r','resolve','period',?,?,'UTC','USD',1,100,?,'rate','1',?,'config','settled',?,?)",
                (f"reservation:{name}", name, NOW.isoformat(), (NOW + timedelta(days=1)).isoformat(), model, NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
            )
            db.execute("INSERT INTO charges(run_id,provider_receipt_id,amount_micros,currency,basis,settled_at) VALUES (?,?,1,'USD','provider_reported',?)",
                       (name, receipt_id, NOW.isoformat()))
    return result


def admission(state, attempt_result):
    """Captured post-admission fixture; production reads the immutable SQL receipt."""
    return ResolveAdmission(attempt_result.attempt, WorkflowRevision(attempt_result.attempt.binding.fence, state.revision.version), attempt_result.stage,
                            3 if attempt_result.stage is ResolveStage.ADJUDICATE else None)


def command_row(control, command_id):
    with closing(control._connect()) as db:
        return db.execute("SELECT * FROM lifecycle_commands WHERE command_id=?", (str(command_id),)).fetchone()


def paid_result(control, state, name, stage, *, money_scope="o/r", model="<runtime-default>", session_id=None, launch=True):
    """Use real admission and settlement for continuation integration cases."""
    attempt = WorkflowAttemptIdentity(state.binding, RunId(name), f"leg:{name}")
    session_id = f"session:{name}" if session_id is None else session_id
    session = ResolveSession(attempt, session_id, "codex", model) if session_id else None
    request = ReservationRequest(
        ReservationId(f"reservation:{name}"), attempt.run_id,
        json.dumps({"repository": "o/r", "role": "fix", "kind": stage.value, "leg_id": attempt.leg_id,
                    "runtime": "codex", "issue": state.revision.fence.key.target.number}),
        BudgetScope(money_scope, "daily", "USD", "2026-09-14", NOW, NOW + timedelta(days=1), ZoneInfo("UTC")),
        PriceQuote(model, "card", "1", NOW, Money("USD", 1)), Money("USD", 100), "config", "o/r",
        kind=stage.value, owner_id=WRITER.owner_id, workflow=state.binding,
        adjudication_ceiling=3 if stage is ResolveStage.ADJUDICATE else None,
    )
    assert control.reserve(request).admitted
    if not launch:
        released = control.release_unlaunched(attempt.run_id, "spawn never started")
        assert released.disposition is AccountingDisposition.AVAILABLE
        return ResolveAttemptResult(attempt, ResolveResultKind.STAGE_PROGRESSED, stage=stage)
    settled = control.settle(SettlementRequest(attempt.run_id, Money("USD", 1), PricingBasis.PROVIDER_REPORTED,
                                               f"codex:{session_id or 'no-session'}:{name}", None, None, None))
    assert settled.disposition is AccountingDisposition.AVAILABLE
    return ResolveAttemptResult(attempt, ResolveResultKind.STAGE_PROGRESSED, stage=stage,
                                session=None if stage is ResolveStage.ROUTE else session)


@pytest.mark.parametrize("money_scope", ["o/r", "*"])
@pytest.mark.parametrize("model", ["<runtime-default>", "selected-model"])
def test_real_paid_author_session_survives_restart_with_repository_or_global_budget(control, money_scope, model):
    before = initial_snapshot()
    install_workflow(control, before)
    result = paid_result(control, before, "author", ResolveStage.AUTHOR, money_scope=money_scope, model=model)
    recorded = adapter(control).record_attempt(result)
    continuation = recorded.snapshot.episode.continuation
    assert isinstance(continuation, AuthoredResolve)
    assert continuation.author_attempt == result.attempt and continuation.session == result.session
    assert continuation.last_applied_admission_version == 1
    assert recorded.snapshot.episode.failures == 0
    restarted = SQLiteControlStore(control.path, now=lambda: NOW)
    assert adapter(restarted).record_attempt(result).idempotent
    assert adapter(restarted).load_workflow(before.revision.fence.key) == recorded.snapshot


def test_real_admissions_reverse_delivery_counts_old_strike_without_regressing_checkpoint(control):
    before = replace(initial_snapshot(), episode=ResolveEpisode("initial", 2))
    install_workflow(control, before)
    old = replace(paid_result(control, before, "old", ResolveStage.RESOLVE), kind=ResolveResultKind.COUNTED_REQUEUE)
    newer = paid_result(control, adapter(control).load_workflow(before.revision.fence.key), "author", ResolveStage.AUTHOR)
    latest = adapter(control).record_attempt(newer).snapshot
    restarted = SQLiteControlStore(control.path, now=lambda: NOW)
    final = adapter(restarted).record_attempt(old).snapshot
    assert final.episode.continuation == latest.episode.continuation
    assert final.episode.failures == 3
    assert adapter(restarted).held_projection(before.revision.fence.key).held
    assert adapter(restarted).record_attempt(old).idempotent


def test_actual_resume_blocked_clears_session_without_losing_authored_checkpoint(control):
    before = initial_snapshot()
    install_workflow(control, before)
    author = paid_result(control, before, "author", ResolveStage.AUTHOR)
    authored = adapter(control).record_attempt(author).snapshot
    blocked = replace(paid_result(control, authored, "resume", ResolveStage.RESUME), kind=ResolveResultKind.BLOCKED)
    assert blocked.session is not None
    cleared = adapter(control).record_attempt(blocked).snapshot
    assert isinstance(cleared.episode.continuation, AuthoredResolve)
    assert cleared.episode.continuation.author_attempt == author.attempt
    assert cleared.episode.continuation.session is None
    assert cleared.episode.failures == 0


@pytest.mark.parametrize("corruption", [
    "missing_admission", "wrong_admission_scope", "malformed_admission", "admission_run", "admission_stage",
    "future_admission", "run_stage", "dispatch_stage", "dispatch_runtime", "unknown_receipt", "recovered_receipt",
    "different_session", "different_model", "missing_reservation", "foreign_workload",
])
def test_unproven_continuation_is_rejected_without_audit_or_episode_mutation(control, corruption):
    before = initial_snapshot()
    install_workflow(control, before)
    result = paid_result(control, before, "author", ResolveStage.AUTHOR)
    admitted = adapter(control).load_workflow(before.revision.fence.key)
    with control.transaction() as db:
        if corruption == "missing_admission":
            db.execute("DELETE FROM dedupe_keys WHERE namespace='workflow-admission'")
        elif corruption == "wrong_admission_scope":
            db.execute("UPDATE dedupe_keys SET scope='foreign/repo' WHERE namespace='workflow-admission'")
        elif corruption == "malformed_admission":
            db.execute("UPDATE dedupe_keys SET payload_json='{}' WHERE namespace='workflow-admission'")
        elif corruption in ("admission_run", "admission_stage", "future_admission"):
            receipt = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-admission'").fetchone()
            proof = decode_workflow(receipt[0], ResolveAdmission)
            if corruption == "admission_run":
                proof = replace(proof, attempt=replace(proof.attempt, run_id=RunId("wrong-run")))
            elif corruption == "admission_stage":
                proof = replace(proof, stage=ResolveStage.RESOLVE)
            else:
                proof = replace(proof, revision=replace(proof.revision, version=admitted.revision.version + 1))
            db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='workflow-admission'", (encode_workflow(proof),))
        elif corruption == "run_stage":
            db.execute("UPDATE runs SET kind='RESOLVE'")
        elif corruption in ("dispatch_stage", "dispatch_runtime"):
            metadata = json.loads(db.execute("SELECT dispatch_id FROM runs").fetchone()[0])
            metadata["kind" if corruption == "dispatch_stage" else "runtime"] = "wrong"
            db.execute("UPDATE runs SET dispatch_id=?", (json.dumps(metadata),))
        elif corruption in ("unknown_receipt", "recovered_receipt", "different_session"):
            receipt = {"unknown_receipt": None, "recovered_receipt": "recovered:author", "different_session": "codex:other:author"}[corruption]
            db.execute("UPDATE runs SET provider_receipt_id=?", (receipt,))
        elif corruption == "different_model":
            db.execute("UPDATE budget_reservations SET model='another-model'")
        elif corruption == "foreign_workload":
            db.execute("UPDATE budget_reservations SET workload_repository='foreign/repo'")
        else:
            db.execute("DELETE FROM reservation_limits")
            db.execute("DELETE FROM budget_reservations")
    if corruption == "future_admission":
        assert isinstance(adapter(control).record_attempt(result), WorkflowConflict)
    else:
        with pytest.raises((WorkflowAuthorityError, ValueError)):
            adapter(control).record_attempt(result)
    assert adapter(control).load_workflow(before.revision.fence.key) == admitted
    with closing(control._connect()) as db:
        assert db.execute("SELECT count(*) FROM dedupe_keys WHERE namespace='workflow-result'").fetchone()[0] == 0


def test_result_proposal_uses_durable_admission_not_a_forged_newer_order(control):
    before = initial_snapshot()
    install_workflow(control, before)
    result = paid_result(control, before, "old", ResolveStage.AUTHOR)
    latest_result = paid_result(control, adapter(control).load_workflow(before.revision.fence.key), "new", ResolveStage.RESOLVE)
    latest = adapter(control).record_attempt(latest_result).snapshot
    forged_proof = ResolveAdmission(result.attempt, latest.revision, result.stage)
    forged = record_resolve_attempt(latest, result, ceiling=3, admission=forged_proof)
    assert isinstance(forged, WorkflowChanged)
    with pytest.raises(ValueError, match="authorized state transition|pure transition"):
        commit(control, forged)
    assert adapter(control).load_workflow(before.revision.fence.key) == latest


def test_accounted_empty_session_sentinel_cannot_be_laundered_into_a_resume_handle(control):
    before = initial_snapshot()
    install_workflow(control, before)
    result = paid_result(control, before, "author", ResolveStage.AUTHOR, session_id="")
    assert result.session is None
    fabricated = replace(result, session=ResolveSession(result.attempt, "no-session", "codex", "<runtime-default>"))
    with pytest.raises(WorkflowAuthorityError, match="provenance"):
        adapter(control).record_attempt(fabricated)
    legitimate = adapter(control).record_attempt(result)
    assert isinstance(legitimate.snapshot.episode.continuation, AuthoredResolve)
    assert legitimate.snapshot.episode.continuation.session is None


def test_continuation_and_result_audit_roll_back_together(control):
    before = initial_snapshot()
    install_workflow(control, before)
    result = paid_result(control, before, "author", ResolveStage.AUTHOR)
    admitted = adapter(control).load_workflow(before.revision.fence.key)
    with control.transaction() as db:
        db.execute("CREATE TRIGGER fail_classification BEFORE INSERT ON dedupe_keys WHEN NEW.namespace='workflow-result' BEGIN SELECT RAISE(ABORT, 'classification crash'); END")
    with pytest.raises(sqlite3.IntegrityError, match="classification crash"):
        adapter(control).record_attempt(result)
    assert adapter(control).load_workflow(before.revision.fence.key) == admitted


@pytest.mark.parametrize("order", [(1, 0), (0, 1)])
def test_real_author_then_implement_converge_when_results_arrive_in_reverse_order(control, order):
    before = initial_snapshot()
    install_workflow(control, before)
    author = paid_result(control, before, "author", ResolveStage.AUTHOR)
    implement = paid_result(control, adapter(control).load_workflow(before.revision.fence.key), "implement", ResolveStage.IMPLEMENT)
    results = (author, implement)
    for index in order:
        adapter(control).record_attempt(results[index])
    restarted = SQLiteControlStore(control.path, now=lambda: NOW)
    continuation = adapter(restarted).load_workflow(before.revision.fence.key).episode.continuation
    assert isinstance(continuation, AuthoredResolve)
    assert continuation.author_attempt == author.attempt
    assert continuation.author_admission.revision.version == 1
    assert continuation.session == implement.session
    assert continuation.last_applied_admission_version == 2
    assert all(adapter(restarted).record_attempt(result).idempotent for result in results)


@pytest.mark.parametrize("order", tuple(permutations(range(4))))
def test_real_multiple_author_successes_and_blocked_resume_converge_independently(control, order):
    current = initial_snapshot()
    install_workflow(control, current)
    results = []
    for name, stage in (("author-one", ResolveStage.AUTHOR), ("author-two", ResolveStage.AUTHOR),
                        ("implement", ResolveStage.IMPLEMENT), ("resume", ResolveStage.RESUME)):
        result = paid_result(control, current, name, stage)
        results.append(replace(result, kind=ResolveResultKind.BLOCKED) if stage is ResolveStage.RESUME else result)
        current = adapter(control).load_workflow(current.revision.fence.key)
    for index in order:
        adapter(control).record_attempt(results[index])
    final = adapter(control).load_workflow(current.revision.fence.key)
    continuation = final.episode.continuation
    assert isinstance(continuation, AuthoredResolve)
    assert continuation.author_attempt == results[1].attempt
    assert continuation.author_admission.revision.version == 2
    assert continuation.last_applied_admission_version == 4
    assert continuation.session is None and final.episode.failures == 0
    assert all(adapter(control).record_attempt(result).idempotent for result in results)


@pytest.mark.parametrize("kind", [ResolveResultKind.STAGE_PROGRESSED, ResolveResultKind.COUNTED_REQUEUE,
                                  ResolveResultKind.BLOCKED, ResolveResultKind.PR_OPENED])
def test_actual_unlaunched_author_cannot_claim_progress_or_a_strike(control, kind):
    before = initial_snapshot()
    install_workflow(control, before)
    unlaunched = paid_result(control, before, "unlaunched", ResolveStage.AUTHOR, launch=False)
    admitted = adapter(control).load_workflow(before.revision.fence.key)
    with pytest.raises(WorkflowAuthorityError, match="settlement provenance"):
        adapter(control).record_attempt(replace(unlaunched, kind=kind))
    assert adapter(control).load_workflow(before.revision.fence.key) == admitted
    with closing(control._connect()) as db:
        assert db.execute("SELECT count(*) FROM dedupe_keys WHERE namespace='workflow-result'").fetchone()[0] == 0


@pytest.mark.parametrize("kind", [ResolveResultKind.INFRA_FAILURE, ResolveResultKind.BUDGET_REFUSAL])
def test_unlaunched_infrastructure_or_budget_result_remains_auditable_without_settlement(control, kind):
    before = initial_snapshot()
    install_workflow(control, before)
    result = replace(paid_result(control, before, "unlaunched", ResolveStage.AUTHOR, launch=False), kind=kind)
    admitted = adapter(control).load_workflow(before.revision.fence.key)
    recorded = adapter(control).record_attempt(result)
    assert recorded.snapshot == admitted
    assert adapter(control).record_attempt(result).idempotent


@pytest.mark.parametrize("corruption", ["cancelled", "abandoned", "released", "reconciled", "missing_charge", "foreign_charge"])
def test_sessionless_author_needs_normal_completed_settlement(control, corruption):
    before = initial_snapshot()
    install_workflow(control, before)
    result = paid_result(control, before, "author", ResolveStage.AUTHOR, session_id="")
    with control.transaction() as db:
        if corruption in ("cancelled", "abandoned"):
            db.execute("UPDATE runs SET state=?", (corruption,))
        elif corruption == "released":
            db.execute("UPDATE budget_reservations SET state='released'")
        elif corruption == "reconciled":
            db.execute("UPDATE charges SET basis='reconciled'")
        elif corruption == "missing_charge":
            db.execute("DELETE FROM charges")
        else:
            db.execute("UPDATE charges SET provider_receipt_id='foreign:receipt'")
    with pytest.raises(WorkflowAuthorityError, match="settlement provenance"):
        adapter(control).record_attempt(result)
    assert isinstance(adapter(control).load_workflow(before.revision.fence.key).episode.continuation, FreshResolve)


def test_read_requires_activation_and_distinguishes_verified_absence(control):
    key = resolve_snapshot().revision.fence.key
    with pytest.raises(WorkflowAuthorityError, match="migration"):
        adapter(control).load_workflow(key)
    install_workflow(control, resolve_snapshot())
    assert isinstance(adapter(control).load_workflow(WorkflowKey("o/r", IssueRef(999))), WorkflowAbsent)
    with pytest.raises(WorkflowAuthorityError, match="scope"):
        adapter(control).load_workflow(WorkflowKey("other/repo", IssueRef(1)))


def test_discovery_requires_explicit_activation_before_creating_authority(control):
    with pytest.raises(WorkflowAuthorityError, match="migration"):
        adapter(control).ensure_observed_workflow(observation(resolve_snapshot()))
    with closing(control._connect()) as db:
        assert db.execute("SELECT count(*) FROM workflows").fetchone()[0] == 0


def test_new_issue_discovery_is_durable_and_repeated_observation_never_resets_it(control):
    install_workflow(control, resolve_snapshot())
    observed = observation(resolve_snapshot(2))
    first = adapter(control).ensure_observed_workflow(observed)
    assert not first.idempotent
    assert first.snapshot.revision.fence.generation == first.snapshot.revision.version == 0
    assert first.snapshot.episode.failures == 0
    later = replace(observed, observed_at=NOW + timedelta(seconds=10))
    control._now = lambda: later.observed_at
    duplicate = adapter(control).ensure_observed_workflow(later)
    assert duplicate.idempotent and duplicate.snapshot == first.snapshot
    with closing(control._connect()) as db:
        rows = db.execute("SELECT payload_json FROM workflow_evidence WHERE target_number=2").fetchall()
    assert len(rows) == 1
    assert decode_workflow(rows[0]["payload_json"], ObservedIssue) == observed


def test_discovery_of_existing_exhausted_episode_preserves_gates_and_guidance(control):
    before = held()
    before = replace(before, episode=replace(before.episode, guidance="original decision", last_failure="prior failure"))
    install_workflow(control, before)
    result = adapter(control).ensure_observed_workflow(observation(before, frozenset({Label.AI_READY})))
    assert result.idempotent and result.snapshot == before
    assert adapter(control).held_projection(before.revision.fence.key).held


def test_concurrent_first_observations_create_one_episode_and_audit(control):
    install_workflow(control, resolve_snapshot())
    observed = observation(resolve_snapshot(2))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: adapter(control).ensure_observed_workflow(observed), range(2)))
    assert sum(not r.idempotent for r in results) == 1
    assert results[0].snapshot == results[1].snapshot
    with closing(control._connect()) as db:
        assert db.execute("SELECT count(*) FROM workflow_evidence WHERE target_number=2").fetchone()[0] == 1


def test_discovery_failure_rolls_back_initial_episode_and_audit(control):
    install_workflow(control, resolve_snapshot())
    observed = observation(resolve_snapshot(2))
    with control.transaction() as db:
        db.execute("CREATE TRIGGER fail_discovery BEFORE INSERT ON workflow_evidence BEGIN SELECT RAISE(ABORT, 'discovery crash'); END")
    with pytest.raises(sqlite3.IntegrityError, match="discovery crash"):
        adapter(control).ensure_observed_workflow(observed)
    assert isinstance(adapter(control).load_workflow(observed.key), WorkflowAbsent)


@pytest.mark.parametrize("existing", [False, True])
def test_corrupt_activation_cannot_create_or_reset_a_discovered_workflow(control, existing):
    install_workflow(control, resolve_snapshot())
    observed = observation(resolve_snapshot(1 if existing else 2))
    with control.transaction() as db:
        db.execute("UPDATE workflow_authority_epochs SET epoch='corrupt'")
    with pytest.raises(WorkflowAuthorityError, match="epoch"):
        adapter(control).ensure_observed_workflow(observed)


def test_corrupt_existing_workflow_is_not_treated_as_undiscovered(control):
    install_workflow(control, resolve_snapshot())
    with control.transaction() as db:
        db.execute("UPDATE workflows SET episode_json='{}'")
    with pytest.raises(WorkflowAuthorityError, match="invalid"):
        adapter(control).ensure_observed_workflow(observation(resolve_snapshot()))


@pytest.mark.parametrize("offset", [-1, 301])
def test_discovery_rechecks_observation_time_under_transaction(control, offset):
    install_workflow(control, resolve_snapshot())
    control._now = lambda: NOW + timedelta(seconds=offset)
    with pytest.raises(ValueError, match="stale or from the future"):
        adapter(control).ensure_observed_workflow(observation(resolve_snapshot(2)))


def test_discovery_checks_bound_owner_and_repository(control):
    install_workflow(control, resolve_snapshot())
    foreign = replace(observation(resolve_snapshot(2)), key=WorkflowKey("foreign/repo", IssueRef(2)))
    with pytest.raises(WorkflowAuthorityError, match="scope"):
        adapter(control).ensure_observed_workflow(foreign)
    control.adopt_legacy_owner("o/r", "fix", "o/r:fix:new", prior_owner_ids=(WRITER.owner_id,))
    with pytest.raises(RetiredOwnerError):
        adapter(control).ensure_observed_workflow(observation(resolve_snapshot(2)))


def test_discovery_ignores_stale_legacy_files_after_activation(control):
    from pathlib import Path
    install_workflow(control, resolve_snapshot())
    Path(control.path).parent.joinpath("issue-2.resolve-retries").write_text("999", encoding="utf-8")
    result = adapter(control).ensure_observed_workflow(observation(resolve_snapshot(2)))
    assert result.snapshot.episode.failures == 0


def test_acceptance_restart_and_duplicate_request_grant_one_episode(control):
    before = held()
    install_workflow(control, before)
    proposal = request(before, guidance=SetGuidance("bounded operator guidance"))
    accepted = commit(control, proposal)
    assert isinstance(accepted, WorkflowCommitted) and not accepted.idempotent
    recovered = adapter(control).load_workflow(before.revision.fence.key)
    assert recovered.episode.failures == 0
    assert recovered.episode.guidance == "bounded operator guidance"
    assert recovered.revision.fence.generation == before.revision.fence.generation + 1
    assert adapter(control).held_projection(before.revision.fence.key).held
    assert isinstance(adapter(control).lookup_rearm(before.revision.fence.key, "request:1"), PendingRearm)
    assert command_row(control, proposal.commands[0].command_id)["state"] == "pending"
    replay = commit(control, proposal)
    assert isinstance(replay, WorkflowCommitted) and replay.idempotent
    assert replay.snapshot == recovered
    with closing(control._connect()) as db:
        assert db.execute("SELECT count(*) FROM lifecycle_commands").fetchone()[0] == 1


def test_changed_request_payload_and_competing_request_do_not_renew_again(control):
    before = held()
    install_workflow(control, before)
    commit(control, request(before))
    assert isinstance(commit(control, request(before, guidance=SetGuidance("changed"))), WorkflowConflict)
    assert isinstance(commit(control, request(before, "request:other")), WorkflowConflict)
    assert adapter(control).load_workflow(before.revision.fence.key).episode.failures == 0


def test_reconstructed_request_replays_original_acceptance_after_fresh_observation(control):
    before = held()
    install_workflow(control, before)
    original = request(before, guidance=SetGuidance("same operator intent"))
    accepted = commit(control, original).snapshot
    with closing(control._connect()) as db:
        original_audit = db.execute("SELECT actor_json FROM workflow_rearms WHERE request_id='request:1'").fetchone()[0]
    later = NOW + timedelta(seconds=90)
    reconstructed = enrich(request_resolve_rearm(
        before, original.audit.command, ACTOR,
        observation(before, at=later), NoLinkedPr(before.revision.fence.key, later),
        now=later, active_attempts=(),
    ))
    reconstructed = replace(reconstructed, commands=(replace(reconstructed.commands[0], config_version="new-config"),))
    reopened_store = SQLiteControlStore(control.path, now=lambda: later)
    result = commit(reopened_store, reconstructed)
    assert isinstance(result, WorkflowCommitted) and result.idempotent
    assert result.snapshot == accepted
    with closing(control._connect()) as db:
        assert db.execute("SELECT actor_json FROM workflow_rearms WHERE request_id='request:1'").fetchone()[0] == original_audit
        assert db.execute("SELECT count(*) FROM lifecycle_commands").fetchone()[0] == 1
    assert command_row(control, original.commands[0].command_id)["config_version"] == original.commands[0].config_version


def test_request_replay_belongs_to_same_authenticated_subject(control):
    before = held()
    install_workflow(control, before)
    original = request(before)
    commit(control, original)
    other_subject = replace(original, audit=replace(original.audit, actor=OperatorActor("operator:bob", "dashboard")))
    assert isinstance(commit(control, other_subject), WorkflowConflict)
    other_channel = replace(original, audit=replace(original.audit, actor=OperatorActor(ACTOR.subject, "operator_cli")))
    assert commit(control, other_channel).idempotent


def test_request_id_reuse_for_another_workflow_conflicts(control):
    before = held()
    install_workflow(control, before)
    commit(control, request(before))
    other = replace(resolve_snapshot(2), episode=ResolveEpisode("episode-2", 3))
    other = reconcile_resolve_ceiling(other, ceiling=3, evidence_ids=("import-2",)).snapshot
    install_workflow(control, other)
    assert isinstance(commit(control, request(other)), WorkflowConflict)
    assert adapter(control).load_workflow(other.revision.fence.key).episode.failures == 3


def test_concurrent_acceptances_serialize_to_one_winning_intent(control):
    before = held()
    install_workflow(control, before)
    proposals = (request(before, "left"), request(before, "right"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda p: commit(control, p), proposals))
    assert sum(isinstance(r, WorkflowCommitted) for r in results) == 1
    assert sum(isinstance(r, WorkflowConflict) for r in results) == 1


@pytest.mark.parametrize("table", ["lifecycle_commands", "workflow_rearm_commands", "dedupe_keys"])
def test_sql_failure_rolls_back_generation_allowance_gates_intent_and_outbox(control, table):
    before = held()
    install_workflow(control, before)
    with control.transaction() as db:
        db.execute(f"CREATE TRIGGER fail_workflow BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT, 'injected crash'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected crash"):
        commit(control, request(before))
    assert adapter(control).load_workflow(before.revision.fence.key) == before
    assert adapter(control).lookup_rearm(before.revision.fence.key, "request:1") is None
    with closing(control._connect()) as db:
        assert db.execute("SELECT count(*) FROM lifecycle_commands").fetchone()[0] == 0


def test_accepted_state_and_events_survive_a_down_telemetry_sink(control):
    before = held()
    install_workflow(control, before)
    producer = Producer("deployment", "o/r", "fix", 123, "boot")
    def factory(payload, correlation):
        return EventEnvelope(payload, new_event_id(NOW), 0, NOW, NOW, 0, producer, correlation)
    def offline(_event):
        raise RuntimeError("sink offline")
    control.bind_event_outbox(factory, offline)
    commit(control, request(before))
    published = []
    control.bind_event_outbox(factory, published.append)
    assert {e.payload.entity_kind for e in published} == {"workflow", "lifecycle_command"}
    assert isinstance(adapter(control).lookup_rearm(before.revision.fence.key, "request:1"), PendingRearm)


def test_retired_owner_epoch_change_and_wrong_executor_cannot_commit(control):
    before = held()
    install_workflow(control, before)
    proposal = request(before)
    with control.transaction() as db:
        db.execute("INSERT INTO retired_owners VALUES (?, ?, 'o/r', 'fix', ?)", (WRITER.owner_id, "o/r:fix:new", NOW.isoformat()))
    with pytest.raises(RetiredOwnerError):
        commit(control, proposal)
    active = SQLiteWorkflowControl(control, replace(WRITER, owner_id="o/r:fix:new"), resolve_ceiling=3)
    with pytest.raises(WorkflowAuthorityError, match="owner"):
        active.commit_workflow(proposal.expected, (before.authority,), proposal)
    with control.transaction() as db:
        db.execute("UPDATE workflow_authority_epochs SET epoch=2")
    with pytest.raises(WorkflowAuthorityError, match="epoch"):
        active.load_workflow(before.revision.fence.key)


@pytest.mark.parametrize("status,lease", [("reserved", False), ("running", False), ("completed", True)])
def test_acceptance_checks_real_active_runs_and_leases(control, status, lease):
    before = held()
    install_workflow(control, before)
    run(control, before, status=status)
    if lease:
        with control.transaction() as db:
            db.execute("INSERT INTO leases(run_id, dispatch_id, owner_id, pid, pgid, started_at) VALUES ('run:1', 'dispatch', ?, 1, 1, ?)", (WRITER.owner_id, NOW.isoformat()))
    with pytest.raises(WorkflowAuthorityError, match="active admitted"):
        commit(control, request(before))
    assert adapter(control).load_workflow(before.revision.fence.key) == before


def test_result_exact_run_dedup_and_leg_identity_prevent_double_charge(control):
    before = resolve_snapshot()
    install_workflow(control, before)
    result = run(control, before)
    proposal = record_resolve_attempt(before, result, ceiling=3, admission=admission(before, result))
    assert commit(control, proposal).snapshot.episode.failures == 1
    assert commit(control, proposal).idempotent
    after = adapter(control).load_workflow(before.revision.fence.key)
    forged = replace(result, attempt=replace(result.attempt, leg_id="another-leg"))
    assert isinstance(commit(control, record_resolve_attempt(after, forged, ceiling=3, admission=admission(after, forged))), WorkflowConflict)
    assert adapter(control).load_workflow(before.revision.fence.key).episode.failures == 1


def test_unadmitted_or_wrong_leg_result_cannot_consume_allowance(control):
    before = resolve_snapshot()
    install_workflow(control, before)
    result = ResolveAttemptResult(WorkflowAttemptIdentity(before.binding, RunId("missing"), "leg"), ResolveResultKind.COUNTED_REQUEUE)
    with pytest.raises(WorkflowAuthorityError, match="admitted"):
        commit(control, record_resolve_attempt(before, result, ceiling=3, admission=admission(before, result)))
    result = run(control, before)
    forged = replace(result, attempt=replace(result.attempt, leg_id="forged-leg"))
    with pytest.raises(WorkflowAuthorityError, match="disagrees"):
        commit(control, record_resolve_attempt(before, forged, ceiling=3, admission=admission(before, forged)))


def test_projection_preserves_other_gates_and_explicit_external_holds(control):
    other = OpenGate("dependency", 2, EvidenceUnavailable("linked_issue", NOW, 1, "offline"), NoAuthorizedRemediation("read required"))
    before = held(extra=(other,))
    install_workflow(control, before)
    proposal = request(before)
    assert proposal.commands[0].payload["remove"] is None
    assert proposal.commands[0].payload["add"] == Label.NEEDS_HUMAN_REVIEW
    malicious = replace(proposal.commands[0], payload={**proposal.commands[0].payload, "remove": Label.NEEDS_HUMAN_REVIEW.value, "add": Label.AI_READY.value})
    with pytest.raises(ValueError, match="unrelated"):
        commit(control, replace(proposal, commands=(malicious,)))
    commit(control, proposal)
    assert other in adapter(control).load_workflow(before.revision.fence.key).gates


def test_stale_preflight_is_rechecked_inside_acceptance_transaction(control):
    before = held()
    install_workflow(control, before)
    proposal = request(before)
    control._now = lambda: NOW + timedelta(minutes=6)
    with pytest.raises(ValueError, match="authorized state"):
        commit(control, proposal)


def test_cancel_pending_obligation_is_audited_and_never_reexecutes(control):
    before = held()
    install_workflow(control, before)
    proposal = request(before)
    pending = commit(control, proposal).snapshot
    stopped = cancel_resolve_rearm(pending, CancelRearm("cancel", pending.revision, "request:1", "operator cancelled"), ACTOR)
    state = commit(control, stopped).snapshot
    assert isinstance(state.intent, CancelledRearm)
    assert state.episode == pending.episode
    row = command_row(control, proposal.commands[0].command_id)
    assert isinstance(decode_workflow(row["workflow_reconciliation_json"], RetiredSettledCommand), RetiredSettledCommand)
    with pytest.raises(ControlStoreError, match="retired"):
        control.begin_command(proposal.commands[0].command_id)
    continued = enrich(continue_resolve_rearm(
        state, ContinueGrantedRearm("continue", state.revision, "request:1"), ACTOR,
        observation(state), NoLinkedPr(state.revision.fence.key, NOW), now=NOW, active_attempts=(),
    ))
    assert commit(control, continued).snapshot.episode == pending.episode
    assert isinstance(adapter(control).lookup_rearm(before.revision.fence.key, "request:1"), CancelledRearm)


def test_cancel_unknown_retains_uncertainty_receipt_and_stops_new_writes(control):
    before = held()
    install_workflow(control, before)
    proposal = request(before)
    pending = commit(control, proposal).snapshot
    command_id = proposal.commands[0].command_id
    control.begin_command(command_id)
    receipt = EffectReceipt(command_id, WRITER.owner_id, NOW, NOW, EffectStatus.UNKNOWN_OUTCOME, error="connection lost")
    control.record_effect(receipt)
    command = CancelRearm("cancel", pending.revision, "request:1", "operator cancelled")
    with pytest.raises(WorkflowAuthorityError, match="uncertainty gate"):
        commit(control, cancel_resolve_rearm(pending, command, ACTOR))
    stopped = cancel_resolve_rearm(pending, command, ACTOR, uncertainty=(RemoteOutcomeUncertain(command_id, NOW, 3, "read unavailable"),))
    state = commit(control, stopped).snapshot
    assert state.episode == pending.episode
    assert control.effect_receipt(command_id) == receipt
    retired = decode_workflow(command_row(control, command_id)["workflow_reconciliation_json"], RetiredUncertainCommand)
    assert retired.uncertainty.history.failed_reads == 3
    assert adapter(control).held_projection(before.revision.fence.key).held
    with pytest.raises(ControlStoreError, match="retired"):
        control.begin_command(command_id)


def test_completion_requires_durable_evidence_and_success_receipt(control):
    before = held()
    install_workflow(control, before)
    proposal = request(before)
    pending = commit(control, proposal).snapshot
    command_id = proposal.commands[0].command_id
    complete = complete_resolve_rearm(
        pending, observation(pending, frozenset({Label.AI_READY})), NoLinkedPr(pending.revision.fence.key, NOW),
        verified_commands=(command_id,), evidence_ids=("observation:1",), now=NOW,
    )
    with pytest.raises(WorkflowAuthorityError, match="evidence"):
        commit(control, complete)
    evidence(control, pending)
    with pytest.raises(WorkflowAuthorityError, match="receipt"):
        commit(control, complete)
    control.begin_command(command_id)
    control.record_effect(EffectReceipt(command_id, WRITER.owner_id, NOW, NOW, EffectStatus.SUCCEEDED))
    completed = commit(control, complete).snapshot
    assert not adapter(control).held_projection(before.revision.fence.key).held
    assert completed.episode == pending.episode
    assert control.effect_receipt(command_id).status is EffectStatus.SUCCEEDED


def test_corrupt_workflow_payload_never_falls_back_to_empty_authority(control):
    before = held()
    install_workflow(control, before)
    with control.transaction() as db:
        db.execute("UPDATE workflows SET episode_json='{}'")
    with pytest.raises(WorkflowAuthorityError, match="invalid"):
        adapter(control).load_workflow(before.revision.fence.key)


def test_well_formed_arbitrary_replacement_count_does_not_become_authority(control):
    before = resolve_snapshot()
    install_workflow(control, before)
    attempted = run(control, before)
    proposal = record_resolve_attempt(before, attempted, ceiling=3, admission=admission(before, attempted))
    assert isinstance(proposal, WorkflowChanged)
    forged = replace(proposal, snapshot=replace(proposal.snapshot, episode=replace(proposal.snapshot.episode, failures=10)))
    with pytest.raises(ValueError, match="pure transition"):
        commit(control, forged)
    assert adapter(control).load_workflow(before.revision.fence.key).episode.failures == 0


def test_missing_required_outbox_does_not_commit_partial_intent(control):
    before = held()
    install_workflow(control, before)
    proposal = request(before)
    with pytest.raises(ValueError, match="outbox obligations"):
        commit(control, replace(proposal, commands=()))
    assert adapter(control).load_workflow(before.revision.fence.key) == before


def test_completed_receipt_cannot_override_fresh_remote_hold_evidence(control):
    before = held()
    install_workflow(control, before)
    proposal = request(before)
    pending = commit(control, proposal).snapshot
    command_id = proposal.commands[0].command_id
    control.begin_command(command_id)
    control.record_effect(EffectReceipt(command_id, WRITER.owner_id, NOW, NOW, EffectStatus.SUCCEEDED))
    complete = complete_resolve_rearm(
        pending, observation(pending, frozenset({Label.AI_READY})), NoLinkedPr(pending.revision.fence.key, NOW),
        verified_commands=(command_id,), evidence_ids=("observation:1",), now=NOW,
    )
    evidence(control, pending, payload=ResolveRearmPreflight(observation(pending), NoLinkedPr(pending.revision.fence.key, NOW)))
    with pytest.raises(ValueError, match="authorized state"):
        commit(control, complete)
    assert adapter(control).held_projection(before.revision.fence.key).held


def test_unchanged_remote_revision_is_not_material_decision_supersession(control):
    before = held()
    install_workflow(control, before)
    pending = commit(control, request(before)).snapshot
    evidence(control, pending, payload=observation(pending))
    proposal = supersede_resolve_rearm(pending, StopReason.DECISION_CHANGED, "observation:1")
    with pytest.raises(ValueError, match="materially changed"):
        commit(control, proposal)
    changed = replace(observation(pending), issue=replace(observation(pending).issue, body="new requirements"))
    evidence(control, pending, "changed", changed)
    accepted = commit(control, supersede_resolve_rearm(pending, StopReason.DECISION_CHANGED, "changed"))
    assert accepted.snapshot.episode == pending.episode
    assert adapter(control).held_projection(before.revision.fence.key).held


def test_terminal_supersession_requires_verified_closed_issue(control):
    before = held()
    install_workflow(control, before)
    pending = commit(control, request(before)).snapshot
    evidence(control, pending, payload=observation(pending))
    with pytest.raises(ValueError, match="terminal issue"):
        commit(control, supersede_resolve_rearm(pending, StopReason.TARGET_TERMINAL, "observation:1"))
    closed = replace(observation(pending), issue=replace(observation(pending).issue, state="closed"))
    evidence(control, pending, "closed", closed)
    assert commit(control, supersede_resolve_rearm(pending, StopReason.TARGET_TERMINAL, "closed")).snapshot.episode == pending.episode


def test_required_effect_rejection_cannot_be_faked_by_an_observation(control):
    before = held()
    install_workflow(control, before)
    proposal = request(before)
    pending = commit(control, proposal).snapshot
    evidence(control, pending, payload=observation(pending))
    stopped = supersede_resolve_rearm(pending, StopReason.REQUIRED_EFFECT_REJECTED, "observation:1")
    with pytest.raises(WorkflowAuthorityError, match="exact required"):
        commit(control, stopped)
    command_id = proposal.commands[0].command_id
    control.begin_command(command_id)
    receipt = EffectReceipt(command_id, WRITER.owner_id, NOW, NOW, EffectStatus.PERMANENT_FAILURE, error="stale revision")
    control.record_effect(receipt)
    assert commit(control, stopped).snapshot.episode == pending.episode
    assert control.effect_receipt(command_id) == receipt


def test_external_holds_are_persisted_in_acceptance_and_cannot_be_relabelled_away(control):
    before = held()
    install_workflow(control, before)
    proposal = request(before, external_holds=("budget authority unavailable",))
    assert proposal.commands[0].payload["remove"] is None
    assert proposal.commands[0].payload["add"] == Label.NEEDS_HUMAN_REVIEW
    commit(control, proposal)
    with closing(control._connect()) as db:
        row = db.execute("SELECT actor_json FROM workflow_rearms WHERE request_id='request:1'").fetchone()
    from issue_agent.domain.workflow import AuthorizedOperatorCommand
    audit = decode_workflow(row["actor_json"], AuthorizedOperatorCommand)
    assert audit.preflight.external_holds == ("budget authority unavailable",)


@pytest.mark.parametrize("kind", [
    ResolveResultKind.INFRA_FAILURE, ResolveResultKind.BUDGET_REFUSAL,
    ResolveResultKind.BLOCKED, ResolveResultKind.PR_OPENED,
])
def test_uncounted_results_are_durable_and_conflicting_reclassification_never_charges(control, kind):
    before = resolve_snapshot()
    install_workflow(control, before)
    admitted = run(control, before)
    result = replace(admitted, kind=kind)
    recorded = adapter(control).record_attempt(result)
    assert recorded.snapshot.episode.failures == before.episode.failures
    assert recorded.snapshot.episode.continuation.session is None
    assert adapter(control).record_attempt(result).idempotent
    assert isinstance(adapter(control).record_attempt(admitted), WorkflowConflict)
    assert isinstance(commit(control, record_resolve_attempt(before, admitted, ceiling=3, admission=admission(before, admitted))), WorkflowConflict)
    assert adapter(control).load_workflow(before.revision.fence.key).episode.failures == 0


def test_old_generation_result_is_audited_but_never_charges_renewed_allowance(control):
    before = held()
    install_workflow(control, before)
    late = run(control, before)
    current = commit(control, request(before)).snapshot
    recorded = adapter(control).record_attempt(late)
    assert recorded.snapshot.episode == current.episode
    assert recorded.snapshot.revision == current.revision
    assert adapter(control).record_attempt(late).idempotent
    conflict = adapter(control).record_attempt(replace(late, kind=ResolveResultKind.BLOCKED))
    assert isinstance(conflict, WorkflowConflict)


def test_counted_record_attempt_and_commit_share_one_run_receipt(control):
    before = resolve_snapshot()
    install_workflow(control, before)
    result = run(control, before)
    assert adapter(control).record_attempt(result).snapshot.episode.failures == 1
    replay = commit(control, record_resolve_attempt(before, result, ceiling=3, admission=admission(before, result)))
    assert replay.idempotent
    assert replay.snapshot.episode.failures == 1


def test_result_dedupe_crash_rolls_back_its_counter_and_gate(control):
    before = replace(resolve_snapshot(), episode=ResolveEpisode("initial", 2))
    install_workflow(control, before)
    result = run(control, before)
    with control.transaction() as db:
        db.execute("CREATE TRIGGER fail_result BEFORE INSERT ON dedupe_keys BEGIN SELECT RAISE(ABORT, 'result crash'); END")
    with pytest.raises(sqlite3.IntegrityError, match="result crash"):
        adapter(control).record_attempt(result)
    assert adapter(control).load_workflow(before.revision.fence.key) == before
    assert not adapter(control).held_projection(before.revision.fence.key).held


@pytest.mark.parametrize("status", [EffectStatus.SUCCEEDED, EffectStatus.PERMANENT_FAILURE, EffectStatus.RETRYABLE_FAILURE, EffectStatus.UNKNOWN_OUTCOME])
def test_workflow_progression_refuses_foreign_persisted_receipt_executor(control, status):
    before = held()
    install_workflow(control, before)
    proposal = request(before)
    pending = commit(control, proposal).snapshot
    command_id = proposal.commands[0].command_id
    control.begin_command(command_id)
    control.record_effect(EffectReceipt(command_id, WRITER.owner_id, NOW, NOW, status,
                                       error=None if status is EffectStatus.SUCCEEDED else "failed"))
    # Corrupt persistence is independent of the now-hardened record_effect
    # boundary; workflow authority must not bless an existing foreign row.
    with control.transaction() as db:
        db.execute("UPDATE effect_receipts SET executor_identity='foreign:executor' WHERE command_id=?", (command_id,))
    if status is EffectStatus.SUCCEEDED:
        evidence(control, pending)
        transition = complete_resolve_rearm(pending, observation(pending, frozenset({Label.AI_READY})),
            NoLinkedPr(pending.revision.fence.key, NOW), verified_commands=(command_id,),
            evidence_ids=("observation:1",), now=NOW)
    elif status is EffectStatus.PERMANENT_FAILURE:
        evidence(control, pending, payload=observation(pending))
        transition = supersede_resolve_rearm(pending, StopReason.REQUIRED_EFFECT_REJECTED, "observation:1")
    else:
        uncertainty = (RemoteOutcomeUncertain(command_id, NOW, 0, "unknown"),) if status is EffectStatus.UNKNOWN_OUTCOME else ()
        transition = cancel_resolve_rearm(pending, CancelRearm("cancel", pending.revision, "request:1", "cancelled"), ACTOR, uncertainty=uncertainty)
    with pytest.raises(WorkflowAuthorityError, match="receipt|rejection"):
        commit(control, transition)
    assert isinstance(adapter(control).lookup_rearm(before.revision.fence.key, "request:1"), PendingRearm)


@pytest.mark.parametrize("status", [EffectStatus.SUCCEEDED, EffectStatus.PERMANENT_FAILURE])
def test_terminal_receipt_remains_valid_after_audited_command_owner_adoption(control, status):
    before = held()
    install_workflow(control, before)
    proposal = request(before)
    pending = commit(control, proposal).snapshot
    command_id = proposal.commands[0].command_id
    control.begin_command(command_id)
    receipt = EffectReceipt(command_id, WRITER.owner_id, NOW, NOW, status,
                            error=None if status is EffectStatus.SUCCEEDED else "stale revision")
    control.record_effect(receipt)
    successor = replace(WRITER, owner_id="o/r:fix:successor")
    control.adopt_legacy_owner("o/r", "fix", successor.owner_id, prior_owner_ids=(WRITER.owner_id,))
    if status is EffectStatus.SUCCEEDED:
        evidence(control, pending)
        transition = complete_resolve_rearm(pending, observation(pending, frozenset({Label.AI_READY})),
            NoLinkedPr(pending.revision.fence.key, NOW), verified_commands=(command_id,),
            evidence_ids=("observation:1",), now=NOW)
    else:
        evidence(control, pending, payload=observation(pending))
        transition = supersede_resolve_rearm(pending, StopReason.REQUIRED_EFFECT_REJECTED, "observation:1")
    row = command_row(control, command_id)
    assert row["executor_identity"] == row["attempt_owner"] == WRITER.owner_id
    new_control = SQLiteWorkflowControl(control, successor, resolve_ceiling=3)
    accepted = new_control.commit_workflow(transition.expected, (pending.authority,), transition)
    assert isinstance(accepted, WorkflowCommitted)
    assert accepted.snapshot.episode == pending.episode
    assert control.effect_receipt(command_id) == receipt


def test_unknown_receipt_retains_original_executor_after_active_command_adoption(control):
    before = held()
    install_workflow(control, before)
    proposal = request(before)
    pending = commit(control, proposal).snapshot
    command_id = proposal.commands[0].command_id
    control.begin_command(command_id)
    receipt = EffectReceipt(command_id, WRITER.owner_id, NOW, NOW, EffectStatus.UNKNOWN_OUTCOME, error="lost connection")
    control.record_effect(receipt)
    successor = replace(WRITER, owner_id="o/r:fix:successor")
    control.adopt_legacy_owner("o/r", "fix", successor.owner_id, prior_owner_ids=(WRITER.owner_id,))
    row = command_row(control, command_id)
    assert row["executor_identity"] == successor.owner_id
    assert row["attempt_owner"] == WRITER.owner_id
    stopped = cancel_resolve_rearm(pending, CancelRearm("cancel", pending.revision, "request:1", "cancelled"), ACTOR,
        uncertainty=(RemoteOutcomeUncertain(command_id, NOW, 0, "unavailable"),))
    new_control = SQLiteWorkflowControl(control, successor, resolve_ceiling=3)
    assert isinstance(new_control.commit_workflow(stopped.expected, (pending.authority,), stopped), WorkflowCommitted)
    assert control.effect_receipt(command_id) == receipt
    assert isinstance(decode_workflow(command_row(control, command_id)["workflow_reconciliation_json"], RetiredUncertainCommand), RetiredUncertainCommand)
