"""Persisted resolve authority is byte-stable across the review-family slice.

The fixture was captured from the accepted resolve slice (dd8844d) before any
review-family type existed. Widening shared unions, extracting commit helpers
or scoping readers by family must not change a single persisted resolve byte.
"""

from contextlib import closing
import json
from pathlib import Path

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.workflow_codec import encode_workflow
from issue_agent.adapters.workflow_commit import accepted_proposal
from issue_agent.domain.workflow import CancelRearm, ContinueGrantedRearm, StopReason
from issue_agent.domain.workflow_policy import (
    cancel_resolve_rearm, continue_resolve_rearm, supersede_resolve_rearm,
)
from tests.adapters.test_workflow_control import NOW, adapter, commit, held, request
from tests.domain import test_workflow_policy as policy
from tests.fakes.workflow_authority import install_workflow

FIXTURE = Path(__file__).parents[1] / "fixtures" / "workflow_resolve_golden.json"

_DUMPS = {
    "workflows": "SELECT * FROM workflows ORDER BY repository,target_kind,target_number",
    "workflow_gates": "SELECT * FROM workflow_gates ORDER BY gate_id",
    "workflow_rearms": "SELECT * FROM workflow_rearms ORDER BY request_id",
    "workflow_rearm_commands": "SELECT * FROM workflow_rearm_commands ORDER BY request_id,operation_key",
    "workflow_evidence": "SELECT * FROM workflow_evidence ORDER BY evidence_id",
    "dedupe_keys": "SELECT namespace,scope,key,claimed_at,payload_json FROM dedupe_keys "
                   "WHERE namespace LIKE 'workflow%' ORDER BY namespace,scope,key",
    "lifecycle_commands": "SELECT command_id,idempotency_key,kind,target,expected_revision,repository,"
                          "executor_identity,payload_json,state,workflow_binding_json "
                          "FROM lifecycle_commands ORDER BY command_id",
}


def domain_values() -> dict[str, str]:
    exhausted = policy.exhausted()
    started = policy.start(exhausted)
    pending = started.snapshot
    cancel = CancelRearm("cancel:1", pending.revision, pending.intent.request_id, "scope changed")
    cancelled = cancel_resolve_rearm(pending, cancel, policy.ACTOR)
    continued = continue_resolve_rearm(
        cancelled.snapshot, ContinueGrantedRearm("continued", cancelled.snapshot.revision, "request:1"),
        policy.ACTOR, policy.observed(cancelled.snapshot), policy.no_pr(cancelled.snapshot),
        now=policy.NOW, active_attempts=(),
    )
    superseded = supersede_resolve_rearm(pending, StopReason.TARGET_TERMINAL, "closed:observation")
    values = {
        "exhausted.snapshot": exhausted,
        "exhausted.episode_json": (exhausted.authority, exhausted.episode),
        "pending.snapshot": pending,
        "pending.audit": started.audit,
        "pending.intent": pending.intent,
        "pending.gates": pending.gates,
        "cancelled.snapshot": cancelled.snapshot,
        "cancelled.audit": cancelled.audit,
        "continued.snapshot": continued.snapshot,
        "superseded.snapshot": superseded.snapshot,
        "superseded.audit": superseded.audit,
    }
    encoded = {name: encode_workflow(value) for name, value in values.items()}
    for name, proposal in (("pending", started), ("cancelled", cancelled), ("continued", continued), ("superseded", superseded)):
        encoded[f"{name}.accepted_proposal"] = accepted_proposal(proposal)
    return encoded


def persisted_rows(tmp_path) -> dict[str, list[list[object]]]:
    store = SQLiteControlStore(str(tmp_path / "golden" / "db.sqlite"), now=lambda: NOW)
    store.initialize()
    state = held()
    install_workflow(store, state)
    proposal = request(state)
    committed = commit(store, proposal)
    pending = committed.snapshot
    cancelled = adapter(store).cancel_rearm(
        CancelRearm("cancel:1", pending.revision, pending.intent.request_id, "scope changed"), policy.ACTOR,
    )
    assert cancelled.snapshot.intent is not None
    with closing(store._connect()) as db:
        return {table: [list(row) for row in db.execute(query)] for table, query in _DUMPS.items()}


def current(tmp_path) -> dict[str, object]:
    return {"values": domain_values(), "rows": persisted_rows(tmp_path)}


def test_resolve_persisted_json_is_byte_identical_to_the_accepted_slice(tmp_path):
    expected = json.loads(FIXTURE.read_text())
    actual = json.loads(json.dumps(current(tmp_path)))
    assert actual["values"] == expected["values"]
    assert actual["rows"] == expected["rows"]


def test_golden_fixture_is_not_vacuous():
    expected = json.loads(FIXTURE.read_text())
    assert {"pending.snapshot", "pending.accepted_proposal", "exhausted.episode_json"} <= set(expected["values"])
    rows = expected["rows"]
    assert rows["workflows"] and rows["workflow_gates"] and rows["workflow_rearms"] and rows["lifecycle_commands"]
    assert any(row[0] == "workflow-request" for row in rows["dedupe_keys"])

