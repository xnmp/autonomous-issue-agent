from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from zoneinfo import ZoneInfo

import pytest

from issue_agent.adapters.control_store import AccountingDisposition, ReservationRequest, SQLiteControlStore
from issue_agent.adapters.workflow_codec import decode_workflow, encode_workflow
from issue_agent.adapters.workflow_control import SQLiteWorkflowControl
from issue_agent.domain.contracts import BudgetScope, Money, PriceQuote, ReservationId, RunId
from issue_agent.domain.workflow import ResolveAdmission, ResolveEpisode, ResolveStage
from issue_agent.domain.workflow_identity import WorkflowWriter
from tests.fakes.workflow_authority import install_workflow, resolve_snapshot

NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)
OWNER = "o/r:fix:test"


@pytest.fixture
def control(tmp_path):
    store = SQLiteControlStore(str(tmp_path / "control.db"), now=lambda: NOW)
    store.initialize()
    install_workflow(store, resolve_snapshot())
    return store


def request(name, *, binding=None, stage="RESOLVE", ceiling=None, cap=100):
    binding = binding or resolve_snapshot().binding
    return ReservationRequest(
        ReservationId("reservation:" + name), RunId(name),
        json.dumps({"repository": "o/r", "role": "fix", "kind": stage,
                    "leg_id": "leg:" + name, "runtime": "codex",
                    "issue": binding.fence.key.target.number}),
        BudgetScope("o/r", "daily", "USD", "2026-09-14", NOW,
                    NOW + timedelta(days=1), ZoneInfo("UTC")),
        PriceQuote("model", "card", "1", NOW, Money("USD", 1)),
        Money("USD", cap), "config", "o/r", kind=stage, owner_id=OWNER,
        workflow=binding, adjudication_ceiling=ceiling,
    )


def snapshot(control):
    return SQLiteWorkflowControl(control, WorkflowWriter("o/r", "fix", OWNER), resolve_ceiling=3).load_workflow(resolve_snapshot().revision.fence.key)


def admission(control, name):
    with control._connect() as db:
        row = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-admission' AND scope='o/r' AND key=?",
            (name,),
        ).fetchone()
    return decode_workflow(row[0], ResolveAdmission) if row else None


def test_same_clock_admissions_have_durable_order_and_duplicate_does_not_advance(control):
    first = request("z-first")
    assert control.reserve(first).admitted
    assert control.reserve(first).admitted
    assert control.reserve(request("a-second")).admitted
    assert admission(control, "z-first").revision.version == 1
    assert admission(control, "a-second").revision.version == 2
    assert snapshot(control).revision.version == 2
    restarted = SQLiteControlStore(control.path, now=lambda: NOW)
    assert admission(restarted, "z-first").attempt.leg_id == "leg:z-first"
    assert admission(restarted, "a-second").stage is ResolveStage.RESOLVE


def test_concurrent_admissions_allocate_unique_order(control):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = tuple(pool.map(lambda n: control.reserve(request(f"run-{n}")), range(4)))
    assert all(result.admitted for result in results)
    assert {admission(control, f"run-{n}").revision.version for n in range(4)} == {1, 2, 3, 4}
    assert snapshot(control).revision.version == 4


def test_budget_denial_does_not_allocate_workflow_order(control):
    assert control.reserve(request("denied", cap=0)).disposition is AccountingDisposition.EXHAUSTED
    assert snapshot(control).revision.version == 0
    assert admission(control, "denied") is None
    assert control.active_reservation_runs() == ()


def test_failure_after_workflow_receipt_rolls_back_order_and_reservation(control):
    with control.transaction() as db:
        db.execute("CREATE TRIGGER reject_run BEFORE INSERT ON runs BEGIN SELECT RAISE(ABORT, 'injected crash'); END")
    assert not control.reserve(request("crashed")).admitted
    assert snapshot(control).revision.version == 0
    assert admission(control, "crashed") is None
    assert control.active_reservation_runs() == ()
    with control.transaction() as db:
        db.execute("DROP TRIGGER reject_run")
    assert control.reserve(request("crashed")).admitted
    assert admission(control, "crashed").revision.version == 1


@pytest.mark.parametrize("field,value", [("kind", "REVIEW"), ("leg_id", ""), ("leg_id", None), ("issue", True)])
def test_malformed_dispatch_cannot_buy_an_admission(control, field, value):
    original = request("bad")
    metadata = json.loads(original.dispatch_id)
    metadata[field] = value
    result = control.reserve(replace(original, dispatch_id=json.dumps(metadata)))
    assert not result.admitted
    assert snapshot(control).revision.version == 0
    assert admission(control, "bad") is None


def test_adjudication_admission_is_charged_before_crash_and_survives_new_generation(control):
    first = request("first", stage="ADJUDICATE", ceiling=1)
    assert control.reserve(first).admitted
    # No completion is needed: a provider crash cannot restore this grant.
    with control.transaction() as db:
        db.execute("UPDATE workflows SET generation=generation+1, version=version+1")
    current = snapshot(control)
    restarted = SQLiteControlStore(control.path, now=lambda: NOW)
    denied = restarted.reserve(request("second", binding=current.binding, stage="ADJUDICATE", ceiling=1))
    assert not denied.admitted
    assert "adjudication allowance is exhausted" in denied.detail
    assert admission(control, "second") is None
    assert snapshot(control).revision == current.revision
    assert admission(control, "first").adjudication_ceiling == 1


def test_new_episode_gets_new_adjudication_allowance_without_deleting_old_runs(control):
    assert control.reserve(request("old", stage="ADJUDICATE", ceiling=1)).admitted
    prior = snapshot(control)
    with control.transaction() as db:
        db.execute("UPDATE workflows SET generation=generation+1, version=version+1, episode_json=?",
                   (encode_workflow((prior.authority, ResolveEpisode("new-episode"))),))
    assert control.reserve(request("new", binding=snapshot(control).binding, stage="ADJUDICATE", ceiling=1)).admitted
    assert admission(control, "old").attempt.binding.episode_id != admission(control, "new").attempt.binding.episode_id


@pytest.mark.parametrize("ceiling", [None, 0])
def test_missing_or_zero_adjudication_allowance_cannot_admit(control, ceiling):
    result = control.reserve(request("denied", stage="ADJUDICATE", ceiling=ceiling))
    assert not result.admitted
    assert admission(control, "denied") is None
    assert snapshot(control).revision.version == 0


def test_concurrent_adjudication_admissions_share_one_allowance(control):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = tuple(pool.map(lambda n: control.reserve(request(f"adj-{n}", stage="ADJUDICATE", ceiling=1)), range(4)))
    assert sum(result.admitted for result in results) == 1
    assert snapshot(control).revision.version == 1


def test_duplicate_adjudication_cannot_change_the_accepted_ceiling(control):
    accepted = request("adj", stage="ADJUDICATE", ceiling=1)
    assert control.reserve(accepted).admitted
    assert control.reserve(accepted).admitted
    assert control.reserve(replace(accepted, adjudication_ceiling=2)).disposition is AccountingDisposition.CONFLICT
    assert snapshot(control).revision.version == 1


def test_corrupt_admission_receipt_is_not_idempotent_authority(control):
    accepted = request("run")
    assert control.reserve(accepted).admitted
    with control.transaction() as db:
        db.execute("UPDATE dedupe_keys SET payload_json='garbage' WHERE namespace='workflow-admission'")
    assert control.reserve(accepted).disposition is AccountingDisposition.UNAVAILABLE
    assert snapshot(control).revision.version == 1


@pytest.mark.parametrize("corruption", ["leg", "future-version"])
def test_well_formed_contradictory_receipt_cannot_authorize_duplicate_reservation(control, corruption):
    accepted = request("run")
    assert control.reserve(accepted).admitted
    receipt = admission(control, "run")
    bad = replace(receipt, attempt=replace(receipt.attempt, leg_id="foreign-leg")) if corruption == "leg" else replace(receipt, revision=replace(receipt.revision, version=2))
    with control.transaction() as db:
        db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='workflow-admission'", (encode_workflow(bad),))
    assert control.reserve(accepted).disposition is AccountingDisposition.CONFLICT
    assert snapshot(control).revision.version == 1
