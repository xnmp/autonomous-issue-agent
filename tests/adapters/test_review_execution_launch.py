"""Guarded process registration retains one immutable review child launch."""

from dataclasses import replace
from datetime import datetime
import json
import os
import sqlite3
import subprocess
import sys

import pytest

from issue_agent.adapters.control_store import AccountingDisposition, ControlStoreError, Lease
from issue_agent.adapters.process_supervisor import ProcessSupervisor
from issue_agent.adapters.workflow_codec import decode_workflow, encode_workflow
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from issue_agent.domain.actions import AgentKind
from issue_agent.domain.review_execution import ReviewChildLaunchRegistration
from issue_agent.domain.review_rounds import ReviewPlanningComplete, ReviewPlanningSealed
from tests.adapters.test_review_execution_admission import planned, receipt, request
from tests.adapters.test_review_execution_control import OWNER, current, dump, rig


@pytest.fixture
def admitted(planned):
    store, selection, _, child = planned
    proposed = request(child)
    assert store.reserve(proposed).admitted
    return store, selection, proposed


def register(store, proposed, **identity):
    process = ProcessSupervisor.process_identity(os.getpid())
    assert process is not None
    return store.register_process_lease(proposed.run_id, expected_owner_id=proposed.owner_id,
                                       **({"pid": process.pid, "pgid": os.getpgrp(), "process_start_ticks": process.start_ticks} | identity))


def launch_receipt(store):
    with store.transaction() as db:
        row = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='review-execution-launch' "
                         "AND scope='o/r' AND key='run-1'").fetchone()
    return decode_workflow(row[0], ReviewChildLaunchRegistration) if row else None


def test_guarded_registration_commits_exact_launch_without_changing_allowance_or_money(admitted):
    store, selection, proposed = admitted
    snapshot = current(store, selection)
    admission = receipt(store)
    money = store.snapshot(proposed.scope)
    assert launch_receipt(store) is None and store.leases() == ()
    lease = register(store, proposed)
    process = ProcessSupervisor.process_identity(os.getpid())
    assert process is not None
    assert lease == Lease(proposed.run_id, proposed.dispatch_id, OWNER, process.pid, os.getpgrp(), store.now(), process.start_ticks)
    assert launch_receipt(store) == ReviewChildLaunchRegistration(admission, "review-execution-launch:run-1",
                                                                 proposed.dispatch_id, process.pid, os.getpgrp(), process.start_ticks, lease.started_at)
    assert current(store, selection) == snapshot
    assert store.snapshot(proposed.scope) == money
    with store.transaction() as db:
        run = db.execute("SELECT state,started_at FROM runs").fetchone()
        assert run["state"] == "running" and datetime.fromisoformat(run["started_at"]) == lease.started_at
        assert db.execute("SELECT policy FROM reservation_recovery").fetchone()[0] == "running"
        assert db.execute("SELECT COUNT(*) FROM charges").fetchone()[0] == 0


def test_existing_planned_child_can_launch_after_parent_seals_the_stage_list(planned):
    store, selection, control, child = planned
    control.seal_completed_planning(child.round.round_id, current(store, selection).revision)
    proposed = request(child)
    assert store.reserve(proposed).admitted

    lease = register(store, proposed)

    assert lease.run_id == proposed.run_id
    assert launch_receipt(store) is not None


def test_exact_registration_replay_is_inert_after_parent_release(admitted):
    store, _, proposed = admitted
    lease = register(store, proposed)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    before = dump(store)
    assert register(store, proposed) == lease
    assert dump(store) == before


@pytest.mark.parametrize("invalid", ["missing_pid", "wrong_start_ticks", "wrong_pgid"])
def test_new_launch_requires_observed_child_process_identity(admitted, invalid):
    store, _, proposed = admitted
    process = ProcessSupervisor.process_identity(os.getpid())
    assert process is not None
    changed = ({"pid": 2**31 - 1, "pgid": 2**31 - 1, "process_start_ticks": 123456789}
               if invalid == "missing_pid" else {"process_start_ticks": process.start_ticks + 1}
               if invalid == "wrong_start_ticks" else {"pgid": 2**31 - 1})
    before = dump(store)
    with pytest.raises(ControlStoreError):
        register(store, proposed, **changed)
    assert store.leases() == () and launch_receipt(store) is None and dump(store) == before


def test_exact_retained_registration_replay_is_inert_after_real_child_exit(admitted):
    store, _, proposed = admitted
    child = subprocess.run([sys.executable, "-c", """
import json, os, sys
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.process_supervisor import ProcessSupervisor
from issue_agent.domain.contracts import RunId
identity = ProcessSupervisor.process_identity(os.getpid())
assert identity is not None
facts = dict(pid=identity.pid, pgid=os.getpgrp(), process_start_ticks=identity.start_ticks)
SQLiteControlStore(sys.argv[1]).register_process_lease(RunId(sys.argv[2]), expected_owner_id=sys.argv[3], **facts)
print(json.dumps(facts))
""", store.path, str(proposed.run_id), proposed.owner_id], start_new_session=True,
                           check=True, capture_output=True, text=True)
    identity = json.loads(child.stdout)
    assert not ProcessSupervisor.matches_identity(identity["pid"], identity["process_start_ticks"])
    retained = store.leases()[0]
    original = launch_receipt(store)
    before = dump(store)
    assert register(store, proposed, **identity) == retained
    assert launch_receipt(store) == original and dump(store) == before


@pytest.mark.parametrize("field,value", [("pid", 125), ("pgid", 125), ("process_start_ticks", 100)])
def test_registered_run_cannot_be_replayed_as_another_process(admitted, field, value):
    store, _, proposed = admitted
    register(store, proposed)
    before = dump(store)
    with pytest.raises(ControlStoreError):
        register(store, proposed, **{field: value})
    assert dump(store) == before


@pytest.mark.parametrize("corruption", ["missing", "unreadable", "process", "admission", "dispatch"])
def test_registered_process_requires_its_original_launch_receipt(admitted, corruption):
    store, _, proposed = admitted
    register(store, proposed)
    original = launch_receipt(store)
    with store.transaction() as db:
        if corruption == "missing":
            db.execute("DELETE FROM dedupe_keys WHERE namespace='review-execution-launch'")
        else:
            changed = (replace(original, pid=999) if corruption == "process" else
                       replace(original, admission=replace(original.admission, claim_occurrence_id="forged"))
                       if corruption == "admission" else replace(original, dispatch_id="forged"))
            db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='review-execution-launch'",
                       ("invalid-json" if corruption == "unreadable" else encode_workflow(changed),))
    before = dump(store)
    with pytest.raises(ControlStoreError):
        register(store, proposed)
    assert dump(store) == before


@pytest.mark.parametrize("failure", ["receipt", "lease", "recovery"])
def test_registration_write_failure_rolls_back_lease_run_recovery_and_receipt(admitted, failure):
    store, _, proposed = admitted
    clause = {"receipt": "BEFORE INSERT ON dedupe_keys WHEN NEW.namespace='review-execution-launch'",
              "lease": "BEFORE INSERT ON leases", "recovery": "BEFORE UPDATE ON reservation_recovery"}[failure]
    with store.transaction() as db:
        db.execute(f"CREATE TRIGGER refuse_registration {clause} BEGIN SELECT RAISE(ABORT,'launch write refused'); END")
    before = dump(store)
    with pytest.raises(sqlite3.IntegrityError, match="launch write refused"):
        register(store, proposed)
    assert launch_receipt(store) is None and dump(store) == before
    assert store.leases() == ()
    with store.transaction() as db:
        db.execute("DROP TRIGGER refuse_registration")
    assert register(store, proposed).run_id == proposed.run_id
    assert launch_receipt(store) is not None


def test_never_launched_release_winning_first_prevents_launch_receipt(admitted):
    store, _, proposed = admitted
    assert store.release_unlaunched(proposed.run_id, "cancel before spawn").disposition is AccountingDisposition.AVAILABLE
    before = dump(store)
    with pytest.raises(ControlStoreError):
        register(store, proposed)
    assert launch_receipt(store) is None and store.leases() == () and dump(store) == before


@pytest.mark.parametrize("remove_lease", [False, True])
def test_registered_launch_can_never_be_released_as_unlaunched(admitted, remove_lease):
    store, _, proposed = admitted
    register(store, proposed)
    original = launch_receipt(store)
    if remove_lease:
        store.release_lease(proposed.run_id)
    before = dump(store)
    assert store.release_unlaunched(proposed.run_id, "too late").disposition is AccountingDisposition.CONFLICT
    assert launch_receipt(store) == original and dump(store) == before


def test_direct_lease_registration_cannot_bypass_guarded_review_launch(admitted):
    store, _, proposed = admitted
    process = ProcessSupervisor.process_identity(os.getpid())
    assert process is not None
    lease = Lease(proposed.run_id, proposed.dispatch_id, OWNER, process.pid, os.getpgrp(), store.now(), process.start_ticks)
    before = dump(store)
    with pytest.raises(ControlStoreError):
        store.register_lease(lease)
    assert launch_receipt(store) is None and store.leases() == () and dump(store) == before


@pytest.mark.parametrize("change", ["stale_binding", "sealed_round", "replaced_claim", "missing_admission", "missing_claim_link"])
def test_paid_reservation_does_not_authorize_a_stale_or_unproven_launch(admitted, change):
    store, _, proposed = admitted
    if change == "replaced_claim":
        assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
        assert store.claim_dispatch("o/r", "review", -9, AgentKind.REVIEW.name, OWNER)
    else:
        with store.transaction() as db:
            if change == "stale_binding":
                db.execute("UPDATE workflows SET generation=generation+1,version=version+1 WHERE target_kind='pr'")
            elif change == "sealed_round":
                db.execute("UPDATE review_execution_rounds SET planning_json=?",
                           (encode_workflow(ReviewPlanningSealed("sealed", ReviewPlanningComplete())),))
            elif change == "missing_admission":
                db.execute("DELETE FROM dedupe_keys WHERE namespace='review-execution-admission'")
            else:
                db.execute("DELETE FROM dispatch_claim_runs")
    before = dump(store)
    with pytest.raises((ControlStoreError, WorkflowAuthorityError)):
        register(store, proposed)
    assert launch_receipt(store) is None and store.leases() == () and dump(store) == before


@pytest.mark.parametrize("terminal", [False, True])
def test_retained_launch_receipt_survives_cleanup_and_prevents_another_registration(admitted, terminal):
    store, _, proposed = admitted
    register(store, proposed)
    original = launch_receipt(store)
    if terminal:
        assert store.complete_run(proposed.run_id, state="abandoned", outcome="crashed")
    else:
        store.release_lease(proposed.run_id)
    before = dump(store)
    assert store.leases() == () and launch_receipt(store) == original
    with pytest.raises(ControlStoreError):
        register(store, proposed)
    assert dump(store) == before
