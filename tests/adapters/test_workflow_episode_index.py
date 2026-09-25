"""Episode allowance reads are bounded and cannot decode unrelated history."""

from dataclasses import replace
import json
import sqlite3

import pytest

from issue_agent.adapters.control_store import (
    AccountingDisposition, IncompatibleSchema, SQLiteControlStore,
    admitted_adjudications, workflow_episode_key,
)
from issue_agent.adapters.workflow_codec import decode_workflow, encode_workflow
from issue_agent.domain.contracts import RunId
from issue_agent.domain.workflow import ResolveAdmission
from tests.adapters.test_workflow_admission import (
    adjudication_input, control, request, snapshot,
)
from tests.fakes.workflow_authority import install_workflow, resolve_snapshot


def test_episode_index_uses_stable_wire_discriminator_and_ignores_generation():
    binding = resolve_snapshot().binding
    assert json.loads(workflow_episode_key(binding))[1] == "issue"
    assert workflow_episode_key(replace(binding, fence=replace(binding.fence, generation=binding.fence.generation + 1))) == workflow_episode_key(binding)


def _downgrade_fixture_to_v19(store):
    # Isolated historical fixture construction, never a supported rollback.
    with store.transaction() as db:
        db.execute("DROP TRIGGER workflow_retry_grant_insert")
        db.execute("DROP TRIGGER workflow_retry_grant_update")
        db.execute("DROP INDEX workflow_retry_open_grant")
        db.execute("ALTER TABLE workflow_retry_authorizations DROP COLUMN disposition")
        db.execute("ALTER TABLE workflow_retry_authorizations DROP COLUMN disposed_at")
        db.execute("ALTER TABLE workflow_retry_authorizations DROP COLUMN disposition_reason")
        db.execute("ALTER TABLE workflow_retry_authorizations DROP COLUMN accepted_at")
        db.execute("ALTER TABLE workflow_retry_authorizations DROP COLUMN first_visit_at")
        db.execute("ALTER TABLE workflow_retry_authorizations DROP COLUMN unavailable_reads")
        db.execute("DROP TABLE workflow_activation_evidence")
        db.execute("DROP TABLE workflow_activation_imports")
        db.execute("DROP INDEX runs_workflow_scope")
        db.execute("DROP INDEX lifecycle_publication_runs")
        db.execute("DROP TRIGGER runs_require_workflow_episode_insert")
        db.execute("DROP TRIGGER runs_require_workflow_episode_update")
        db.execute("DROP INDEX runs_adjudication_episode")
        db.execute("ALTER TABLE runs DROP COLUMN workflow_episode_key")
        db.execute("UPDATE runs SET outcome='released' WHERE outcome='unlaunched_released'")
        db.execute("DELETE FROM schema_migrations WHERE version>=20")


def test_malformed_unrelated_historical_binding_does_not_block_adjudication(control):
    foreign = resolve_snapshot(8)
    install_workflow(control, foreign)
    assert control.reserve(request("unrelated", binding=foreign.binding)).admitted
    with control.transaction() as db:
        db.execute("UPDATE runs SET kind='ADJUDICATE',workflow_binding_json='corrupt' WHERE run_id='unrelated'")
    supplied = adjudication_input(control)
    assert control.reserve(request("healthy", stage="ADJUDICATE", ceiling=1, split_input=supplied)).admitted
    with control.transaction() as db:
        assert admitted_adjudications(db, snapshot(control).binding) == 1


def test_selected_corrupt_binding_refuses_instead_of_resetting_allowance(control):
    supplied = adjudication_input(control)
    assert control.reserve(request("selected", stage="ADJUDICATE", ceiling=1, split_input=supplied)).admitted
    with control.transaction() as db:
        db.execute("UPDATE runs SET workflow_binding_json='corrupt' WHERE run_id='selected'")
        with pytest.raises(ValueError):
            admitted_adjudications(db, snapshot(control).binding)


def test_history_and_released_spawn_attempts_do_not_increase_query_work(control):
    supplied = adjudication_input(control)
    assert control.reserve(request("template", stage="ADJUDICATE", ceiling=1, split_input=supplied)).admitted
    assert control.release_unlaunched(RunId("template"), "spawn failed").disposition is AccountingDisposition.AVAILABLE
    binding = snapshot(control).binding
    foreign = replace(binding, episode_id="old-episode")
    with control.transaction() as db:
        # Retain valid terminal evidence alongside each synthetic historical run.
        for table, identity in (("runs", "run_id"), ("budget_reservations", "reservation_id"), ("reservation_recovery", "run_id"), ("reservation_limits", "run_id")):
            columns = tuple(row[1] for row in db.execute(f"PRAGMA table_info({table})"))
            template = db.execute(f"SELECT * FROM {table} WHERE run_id='template'").fetchone()
            values = []
            for index in range(4000):
                row = dict(template)
                name = f"history-{index}"
                row["run_id"] = name
                row[identity] = name
                if table == "runs" and index % 2:
                    row["workflow_binding_json"] = "unreadable unrelated history"
                    row["workflow_episode_key"] = workflow_episode_key(foreign)
                    row["outcome"] = "unknown"
                values.append(tuple(row[column] for column in columns))
            db.executemany(f"INSERT INTO {table}({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", values)
        steps = 0

        def bound_work():
            nonlocal steps
            steps += 100
            return int(steps > 2000)

        db.set_progress_handler(bound_work, 100)
        try:
            assert admitted_adjudications(db, binding) == 0
        finally:
            db.set_progress_handler(None, 0)
    assert control.reserve(request("retry", stage="ADJUDICATE", ceiling=1, split_input=supplied)).admitted


def test_allowance_count_saturates_above_the_largest_supported_ceiling(control):
    supplied = adjudication_input(control)
    assert control.reserve(request("source", stage="ADJUDICATE", ceiling=1, split_input=supplied)).admitted
    with control.transaction() as db:
        columns = tuple(row[1] for row in db.execute("PRAGMA table_info(runs)"))
        template = dict(db.execute("SELECT * FROM runs WHERE run_id='source'").fetchone())
        reservation_columns = tuple(row[1] for row in db.execute("PRAGMA table_info(budget_reservations)"))
        reservation = dict(db.execute("SELECT * FROM budget_reservations WHERE run_id='source'").fetchone())
        receipt_row = db.execute("SELECT * FROM dedupe_keys WHERE namespace='workflow-admission' AND key='source'").fetchone()
        admission = decode_workflow(receipt_row["payload_json"], ResolveAdmission)
        for index in range(8):
            name = f"historical-admission-{index}"
            run = {**template, "run_id": name}
            db.execute(f"INSERT INTO runs({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", tuple(run[column] for column in columns))
            budget = {**reservation, "run_id": name, "reservation_id": name}
            db.execute(f"INSERT INTO budget_reservations({','.join(reservation_columns)}) VALUES ({','.join('?' for _ in reservation_columns)})", tuple(budget[column] for column in reservation_columns))
            receipt = replace(admission, attempt=replace(admission.attempt, run_id=RunId(name)))
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES ('workflow-admission',?,?,?,?)", (receipt_row["scope"], name, receipt_row["claimed_at"], encode_workflow(receipt)))
        assert admitted_adjudications(db, snapshot(control).binding) == 5


def test_v19_upgrade_retains_consumed_and_proven_unlaunched_allowance(control):
    supplied = adjudication_input(control)
    assert control.reserve(request("released", stage="ADJUDICATE", ceiling=1, split_input=supplied)).admitted
    assert control.release_unlaunched(RunId("released"), "spawn failed").disposition is AccountingDisposition.AVAILABLE
    assert control.reserve(request("unknown", stage="ADJUDICATE", ceiling=1, split_input=supplied)).admitted
    binding = snapshot(control).binding
    _downgrade_fixture_to_v19(control)
    reopened = SQLiteControlStore(control.path)
    reopened.initialize()
    reopened.initialize()
    with reopened.transaction() as db:
        assert admitted_adjudications(db, binding) == 1
        assert db.execute("SELECT outcome FROM runs WHERE run_id='released'").fetchone()[0] == "unlaunched_released"


def test_interrupted_v20_backfill_rolls_back_row_reclassification_and_schema(control):
    supplied = adjudication_input(control)
    assert control.reserve(request("released", stage="ADJUDICATE", ceiling=1, split_input=supplied)).admitted
    assert control.release_unlaunched(RunId("released"), "spawn failed").disposition is AccountingDisposition.AVAILABLE
    binding = snapshot(control).binding
    _downgrade_fixture_to_v19(control)

    def interrupt(version, point):
        if version == 20 and point == "before_commit":
            raise RuntimeError("migration process interrupted")

    with pytest.raises(RuntimeError, match="interrupted"):
        SQLiteControlStore(control.path, migration_observer=interrupt).initialize()
    with sqlite3.connect(control.path) as db:
        assert db.execute("SELECT outcome FROM runs WHERE run_id='released'").fetchone()[0] == "released"
        assert "workflow_episode_key" not in {row[1] for row in db.execute("PRAGMA table_info(runs)")}
    restarted = SQLiteControlStore(control.path)
    restarted.initialize()
    with restarted.transaction() as db:
        assert admitted_adjudications(db, binding) == 0


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


def test_epoch_changes_do_not_inherit_other_epoch_allowance(control):
    supplied = adjudication_input(control)
    assert control.reserve(request("old-epoch", stage="ADJUDICATE", ceiling=1, split_input=supplied)).admitted
    binding = snapshot(control).binding
    new = replace(binding, authority=replace(binding.authority, epoch=binding.authority.epoch + 1))
    with control.transaction() as db:
        assert admitted_adjudications(db, binding) == 1
        assert admitted_adjudications(db, new) == 0


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
            db.execute("INSERT INTO runs(run_id,dispatch_id,kind,owner_id,state,config_version,workflow_binding_json) VALUES ('old','old','ADJUDICATE','o/r:fix','reserved','old',?)", (encode_workflow(snapshot(control).binding),))


def test_generic_completion_cannot_mint_never_launched_evidence(control):
    assert control.reserve(request("reserved")).admitted
    with pytest.raises(ValueError, match="minted"):
        control.complete_run(RunId("reserved"), state="cancelled", outcome="unlaunched_released")
