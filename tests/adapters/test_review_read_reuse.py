"""Read optimizations preserve canonical receipts and reject changed authority."""

import json

import pytest

from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from issue_agent.adapters.workflow_codec import MAX_WORKFLOW_PAYLOAD_BYTES
from issue_agent.domain.workflow import WorkflowCommitted
from tests.adapters.test_review_execution_control import current, rig
from tests.adapters.test_review_productive_settlement import confirmed_copy, workflow_for


@pytest.mark.parametrize("column", ["closure_json", "outcome_json"])
@pytest.mark.parametrize("encoding", ["canonical", "pretty", "duplicate", "wrong_format"])
def test_current_read_preserves_encoding_validation(rig, column, encoding):
    store, gateway, selection, _, plan, command = confirmed_copy(rig)
    assert isinstance(workflow_for(store, gateway).settle_submission(command.command_id), WorkflowCommitted)
    before = current(store, selection)
    with store.transaction() as db:
        raw = db.execute(f"SELECT {column} FROM review_execution_rounds WHERE round_id=?",
                         (plan.round.round_id,)).fetchone()[0]
        if encoding == "pretty":
            raw = json.dumps(json.loads(raw), indent=2)
        elif encoding == "duplicate":
            raw = raw.replace('{"format":1,', '{"format":1,"format":1,', 1)
        elif encoding == "wrong_format":
            raw = raw.replace('{"format":1,', '{"format":2,', 1)
        db.execute(f"UPDATE review_execution_rounds SET {column}=? WHERE round_id=?",
                   (raw, plan.round.round_id))
    if encoding in {"canonical", "pretty"}:
        assert current(store, selection) == before
    else:
        with pytest.raises(WorkflowAuthorityError):
            current(store, selection)


def test_reused_round_receipt_still_requires_unchanged_actual_effect_evidence(rig):
    store, gateway, selection, _, _, command = confirmed_copy(rig)
    assert isinstance(workflow_for(store, gateway).settle_submission(command.command_id), WorkflowCommitted)
    assert current(store, selection) is not None
    with store.transaction() as db:
        db.execute("UPDATE effect_receipts SET remote_object_id='changed' WHERE command_id=?",
                   (str(command.command_id),))
    with pytest.raises(WorkflowAuthorityError):
        current(store, selection)


@pytest.mark.parametrize("column,value", [
    pytest.param("payload_json", None, id="absent"),
    pytest.param("payload_json", b"blob", id="blob"),
    pytest.param("payload_json", "", id="empty"),
    pytest.param("payload_json", "x" * (MAX_WORKFLOW_PAYLOAD_BYTES + 1), id="oversized"),
    pytest.param("claimed_at", "invalid", id="invalid-time"),
    pytest.param("claimed_at", "2026-10-05T00:00:00", id="naive-time"),
    pytest.param("reusable_after", "2026-10-05T00:00:00+00:00", id="mutable"),
])
def test_combined_receipt_read_preserves_immutable_evidence_bounds(rig, column, value):
    store, gateway, selection, _, plan, command = confirmed_copy(rig)
    assert isinstance(workflow_for(store, gateway).settle_submission(command.command_id), WorkflowCommitted)
    with store.transaction() as db:
        db.execute(f"UPDATE dedupe_keys SET {column}=? WHERE namespace='review-round-outcome' AND key=?",
                   (value, plan.round.round_id))
    with pytest.raises(WorkflowAuthorityError):
        current(store, selection)
