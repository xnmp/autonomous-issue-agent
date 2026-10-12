"""Episode allowance reads are bounded and cannot decode unrelated history."""

from dataclasses import replace
import json
import sqlite3

import pytest

from issue_agent.adapters.control_store import (
    IncompatibleSchema, SQLiteControlStore,
    workflow_episode_key,
)
from issue_agent.adapters.workflow_codec import decode_workflow, encode_workflow
from issue_agent.domain.contracts import RunId
from issue_agent.domain.workflow import ResolveAdmission
from tests.adapters.test_workflow_admission import (
    control, request, snapshot,
)
from tests.fakes.workflow_authority import resolve_snapshot


def test_episode_index_uses_stable_wire_discriminator_and_ignores_generation():
    binding = resolve_snapshot().binding
    assert json.loads(workflow_episode_key(binding))[1] == "issue"
    assert workflow_episode_key(replace(binding, fence=replace(binding.fence, generation=binding.fence.generation + 1))) == workflow_episode_key(binding)


def _downgrade_fixture_to_v19(store):
    # Isolated historical fixture construction, never a supported rollback.
    from tests.adapters.test_control_store import _historical_fixture
    with store.transaction() as db:
        _historical_fixture(db, 19)


@pytest.mark.parametrize("corruption", ["missing", "wrong_binding", "wrong_run"])
def test_v19_projection_refuses_inconsistent_immutable_admission(control, corruption):
    assert control.reserve(request("legacy")).admitted
    _downgrade_fixture_to_v19(control)
    with sqlite3.connect(control.path) as db:
        row = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-admission' AND key='legacy'").fetchone()
        admitted = decode_workflow(row[0], ResolveAdmission)
        if corruption == "missing":
            db.execute("DELETE FROM dedupe_keys WHERE namespace='workflow-admission' AND key='legacy'")
        else:
            attempt = replace(admitted.attempt, run_id=RunId("another")) if corruption == "wrong_run" else replace(admitted.attempt, binding=replace(admitted.attempt.binding, episode_id="another"))
            db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='workflow-admission' AND key='legacy'", (encode_workflow(replace(admitted, attempt=attempt)),))
    with pytest.raises(IncompatibleSchema):
        SQLiteControlStore(control.path).initialize()
    with sqlite3.connect(control.path) as db:
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 19


def test_invalid_v19_binding_rolls_back_entire_projection_migration(control):
    assert control.reserve(request("legacy")).admitted
    _downgrade_fixture_to_v19(control)
    with sqlite3.connect(control.path) as db:
        db.execute("UPDATE runs SET workflow_binding_json='invalid' WHERE run_id='legacy'")
    with pytest.raises(IncompatibleSchema):
        SQLiteControlStore(control.path).initialize()
    with sqlite3.connect(control.path) as db:
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 19
        assert "workflow_episode_key" not in {row[1] for row in db.execute("PRAGMA table_info(runs)")}


def test_binding_projection_cannot_be_omitted_or_deleted_by_an_old_writer(control):
    assert control.reserve(request("bound")).admitted
    with control.transaction() as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE runs SET workflow_episode_key=NULL WHERE run_id='bound'")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO runs(run_id,dispatch_id,kind,owner_id,state,config_version,workflow_binding_json) VALUES ('old','old','RESOLVE','o/r:fix','reserved','old',?)", (encode_workflow(snapshot(control).binding),))


def test_generic_completion_cannot_mint_never_launched_evidence(control):
    assert control.reserve(request("reserved")).admitted
    with pytest.raises(ValueError, match="minted"):
        control.complete_run(RunId("reserved"), state="cancelled", outcome="unlaunched_released")
