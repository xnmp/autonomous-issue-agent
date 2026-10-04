import json
from datetime import datetime, timezone

import pytest

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.measurement import read_observations, record_observation
from issue_agent.domain.measurement import parse_observation
from issue_agent.domain.events import MeasurementObservationRecorded, decode_event_line

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
