import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.measurement import read_observations, record_observation
from issue_agent.domain.measurement import parse_observation
from issue_agent.domain.events import MeasurementObservationRecorded

NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)


def record(**changes):
    raw = dict(kind="intervention", record_id="one", repository="o/r", actor="operator", provenance="operator recorded", occurred_at=NOW.isoformat(), category="mechanical", source="GitHub", description="released human hold")
    raw.update(changes)
    return parse_observation(raw)


def test_record_is_idempotent_conflict_is_visible_and_event_is_durable(tmp_path):
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()
    assert record_observation(store, record(), deployment="test", now=NOW)
    assert not record_observation(store, record(), deployment="test", now=NOW)
    with pytest.raises(ValueError, match="different"):
        record_observation(store, record(description="conflicting"), deployment="test", now=NOW)
    assert read_observations(store.path, "o/r") == (record(),)
    events = []
    assert store.flush_outbox(events.append) == 1
    assert isinstance(events[0].payload, MeasurementObservationRecorded)
    assert events[0].payload.record["description"] == "released human hold"


def test_outbox_failure_rolls_back_observation(tmp_path, monkeypatch):
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()
    def unavailable(*args):
        raise ValueError("encoding unavailable")
    monkeypatch.setattr(store, "enqueue_event", unavailable)
    with pytest.raises(ValueError, match="unavailable"):
        record_observation(store, record(), deployment="test", now=NOW)
    assert read_observations(store.path, "o/r") == ()


def seeded_admission(tmp_path):
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    from issue_agent.adapters.control_store import ReservationRequest, SettlementRequest
    from issue_agent.adapters.workflow_codec import encode_workflow
    from issue_agent.domain.contracts import BudgetScope, Money, PriceQuote, PricingBasis, ReservationId, RunId, WorkflowAttemptIdentity
    from issue_agent.domain.workflow import ResolveAdmission, ResolveStage
    from issue_agent.domain.workflow_identity import WorkflowBinding, WorkflowFence, WorkflowKey, IssueRef, FamilyAuthority, WorkflowFamily, WorkflowRevision
    store = SQLiteControlStore(str(tmp_path / "control.db"), now=lambda: NOW)
    store.initialize()
    scope = BudgetScope("o/r", "daily", "USD", "2026-10-04", NOW, NOW + timedelta(days=1), ZoneInfo("UTC"))
    request = ReservationRequest(ReservationId("reservation"), RunId("run"), json.dumps(dict(runtime="codex", effort="medium")), scope, PriceQuote("gpt", "card", "v1", NOW, Money("USD", 20)), Money("USD", 100), "v1", "o/r")
    store.reserve(request)
    store.settle(SettlementRequest(RunId("run"), Money("USD", 12), PricingBasis.CONFIGURED_RATE_CARD, "receipt", None, "card", "v1"))
    fence = WorkflowFence(WorkflowKey("o/r", IssueRef(7)), 1)
    binding = WorkflowBinding(fence, FamilyAuthority(WorkflowFamily.RESOLVE, 1), "episode")
    receipt = ResolveAdmission(WorkflowAttemptIdentity(binding, RunId("run"), "leg"), WorkflowRevision(fence, 1), ResolveStage.RESOLVE)
    with store.transaction() as db:
        db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)", ("workflow-admission", "o/r", "run", NOW.isoformat(), encode_workflow(receipt)))
    return store, binding


def test_export_reads_real_typed_admission_and_frozen_profile_without_mutation(tmp_path):
    from datetime import timedelta
    from issue_agent.adapters.measurement import export_cohort
    from issue_agent.domain.cohort import CohortWindow
    store, _binding = seeded_admission(tmp_path)
    window = CohortWindow("o/r", NOW, NOW + timedelta(hours=1), NOW + timedelta(hours=2),
                          expected_profile=(("deployment", "test"), ("runtime", "codex"),
                                            ("model", "gpt"), ("posture", "trusted inbound"),
                                            ("config_version", "v1")))
    before = open(store.path, "rb").read()
    result = export_cohort(store.path, window)
    assert result["primary_admitted_bindings"] == 1
    assert result["spend_micros_by_currency_basis"] == {"USD:configured_rate_card": 12}
    assert result["bindings"][0]["frozen_profiles"][0]["runtime"] == "codex"
    assert result["bindings"][0]["frozen_profiles"][0]["model"] == "gpt"
    assert result["comparability"] == "indeterminate"
    assert open(store.path, "rb").read() == before


def test_workflow_annotation_must_match_real_admission_pair(tmp_path):
    from issue_agent.adapters.workflow_codec import encode_workflow
    from issue_agent.domain.cohort import binding_reference
    store, binding = seeded_admission(tmp_path)
    raw = dict(kind="workflow_measurement", record_id="profile", repository="o/r", actor="operator", provenance="checked fixture", occurred_at=NOW.isoformat(), run_id="wrong", binding_ref=binding_reference(encode_workflow(binding)), evidence_refs=["fixture admission"])
    with pytest.raises(ValueError, match="match an authoritative"):
        record_observation(store, parse_observation(raw), deployment="test", now=NOW)
    raw["run_id"] = "run"
    assert record_observation(store, parse_observation(raw), deployment="test", now=NOW)


def admitted_components(tmp_path):
    from issue_agent.domain import continuation_components as cc
    from issue_agent.domain.workflow import IssueWorkflowShell
    from issue_agent.domain.workflow_identity import WorkflowFamily
    from issue_agent.adapters.workflow_codec import encode_workflow
    from issue_agent.adapters.workflow_rows import workflow_scope
    from tests.adapters.test_continuation_admission import active, install_secondary, money
    from tests.adapters.test_workflow_admission import NOW as admitted_at, request
    from tests.fakes.workflow_authority import install_workflow, resolve_snapshot

    store = SQLiteControlStore(str(tmp_path / "control.db"), now=lambda: admitted_at)
    store.initialize()
    resolve = resolve_snapshot(7)
    recovery = active(resolve.revision.fence.key, WorkflowFamily.ISSUE_RECOVERY)
    aggregate = replace(resolve, issue_recovery=recovery)
    install_workflow(store, aggregate)
    selected = cc.select_issue_recovery(aggregate)
    purchase = cc.IssueInfraPurchase(cc.ContinuationCapture(
        selected.binding, selected.revision, "leg:resolve"))
    paid = replace(request("resolve", binding=resolve.binding), continuations=(purchase,))
    assert store.reserve(paid).admitted
    assert store.settle(replace(money(paid.run_id), provider_receipt_id="codex:fixture:resolve")).disposition.value == "available"

    revision = resolve_snapshot(8).revision
    steward = active(revision.fence.key, WorkflowFamily.STEWARD)
    shell = IssueWorkflowShell(revision, steward=steward)
    with store.transaction() as db:
        db.execute("INSERT INTO workflows VALUES (?,?,?,?,?,?)",
                   (*workflow_scope(revision.fence.key), revision.fence.generation,
                    revision.version, encode_workflow(shell)))
    install_secondary(store, shell, steward)
    selected = cc.select_steward(shell)
    purchase = cc.StewardPurchase(cc.ContinuationCapture(selected.binding, selected.revision, "leg:triage"),
                                  cc.StewardLane.TRIAGE, admitted_at.isoformat(), 3)
    requested = request("triage", binding=selected.binding)
    dispatch = {**json.loads(requested.dispatch_id), "role": "steward", "kind": "TRIAGE", "number": 8}
    paid = replace(requested, workflow=selected.binding, owner_id="o/r:steward:test", kind="TRIAGE",
                   dispatch_id=json.dumps(dispatch))
    assert store.reserve_steward(paid, purchase).admitted
    assert store.settle(money(paid.run_id)).disposition.value == "available"
    return store, admitted_at


def test_component_export_conserves_all_family_spend_and_accepts_exact_workflow_annotation(tmp_path):
    from datetime import timedelta
    from issue_agent.adapters.measurement import export_cohort
    from issue_agent.domain.cohort import CohortWindow

    store, admitted_at = admitted_components(tmp_path)
    window = CohortWindow("o/r", admitted_at, admitted_at + timedelta(hours=1), admitted_at + timedelta(hours=2))
    before = open(store.path, "rb").read()
    result = export_cohort(store.path, window)
    assert result["admitted_bindings_by_family"] == {"resolve": 1, "issue_recovery": 1, "steward": 1}
    assert result["all_family_spend_micros_by_currency_basis"] == {"USD:provider_reported": 2}
    assert result["spend_micros_by_currency_basis"] == {"USD:provider_reported": 1}
    assert result["shared_binding_spend_micros"] == {"USD:provider_reported": 1}
    assert result["unassigned_spend_micros"] == {}
    assert not any("invalid admission" in gap for gap in result["coverage_gaps"])
    assert open(store.path, "rb").read() == before
    binding = next(binding for binding in result["bindings"] if binding["family"] == "steward")
    raw = dict(kind="workflow_measurement", record_id="component-profile", repository="o/r", actor="operator",
               provenance="actual steward reservation", occurred_at=admitted_at.isoformat(), run_id="triage",
               binding_ref=binding["binding_ref"], evidence_refs=["settled steward reservation"])
    assert record_observation(store, parse_observation(raw), deployment="test", now=admitted_at)


@pytest.mark.parametrize("malformation", ["empty", "reversed", "extra", "binding", "leg", "version", "run", "key"])
def test_malformed_component_receipt_is_a_coverage_gap_and_cannot_authorize_annotation(tmp_path, malformation):
    from datetime import timedelta
    from issue_agent.adapters.measurement import export_cohort
    from issue_agent.adapters.workflow_codec import decode_workflow, encode_workflow
    from issue_agent.domain.cohort import CohortWindow, binding_reference

    store, admitted_at = admitted_components(tmp_path)
    with store.transaction() as db:
        row = db.execute("SELECT key,payload_json FROM dedupe_keys WHERE namespace='continuation-admission' AND key LIKE '%:steward'").fetchone()
        purchase, admission = decode_workflow(row["payload_json"], tuple)
        if malformation == "empty":
            captured = ()
        elif malformation == "reversed":
            captured = (admission, purchase)
        elif malformation == "extra":
            captured = (purchase, admission, admission)
        elif malformation == "binding":
            capture = replace(purchase.capture, binding=replace(purchase.capture.binding, episode_id="other-episode"))
            captured = (replace(purchase, capture=capture), admission)
        elif malformation == "version":
            captured = (purchase, replace(admission, admission_version=admission.admission_version + 1))
        elif malformation == "run":
            captured = (purchase, replace(admission, attempt=replace(admission.attempt, run_id="resolve")))
        elif malformation == "key":
            captured = (purchase, admission)
        else:
            captured = (replace(purchase, capture=replace(purchase.capture, leg_id="other-leg")), admission)
        db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='continuation-admission' AND key=?", (encode_workflow(captured), row["key"]))
        if malformation == "key":
            db.execute("UPDATE dedupe_keys SET key='wrong-key' WHERE namespace='continuation-admission' AND key=?", (row["key"],))
    result = export_cohort(store.path, CohortWindow("o/r", admitted_at, admitted_at + timedelta(hours=1), admitted_at + timedelta(hours=2)))
    assert "steward" not in result["admitted_bindings_by_family"]
    assert any("invalid admission continuation-admission" in gap for gap in result["coverage_gaps"])
    raw = dict(kind="workflow_measurement", record_id="invalid-component", repository="o/r", actor="operator",
               provenance="malformed receipt", occurred_at=admitted_at.isoformat(), run_id="resolve" if malformation == "run" else "triage",
               binding_ref=binding_reference(encode_workflow(admission.attempt.binding)), evidence_refs=["invalid receipt"])
    with pytest.raises(ValueError, match="match an authoritative"):
        record_observation(store, parse_observation(raw), deployment="test", now=admitted_at)


def test_settlement_without_return_evidence_does_not_claim_complete_failure_cost(tmp_path):
    from datetime import timedelta
    from issue_agent.adapters.measurement import export_cohort
    from issue_agent.domain.cohort import CohortWindow

    store, at = admitted_components(tmp_path)
    result = export_cohort(store.path, CohortWindow("o/r", at, at + timedelta(minutes=1), at + timedelta(minutes=2)))
    assert result["failed_or_abandoned_spend_micros"] == {}
    assert not result["failed_or_abandoned_spend_coverage"]["complete"]
    assert result["failed_or_abandoned_spend_coverage"]["unknown_execution_runs"] == ["resolve"]
    assert all(row["outcome"] == "unknown" for row in result["run_execution_observations"])

