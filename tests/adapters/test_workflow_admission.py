from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
import json
import hashlib
from zoneinfo import ZoneInfo

import pytest

from issue_agent.adapters.control_store import AccountingDisposition, ReservationRequest, SQLiteControlStore, admitted_adjudications
from issue_agent.adapters.workflow_codec import decode_workflow, encode_workflow
from issue_agent.adapters.workflow_control import SQLiteWorkflowControl
from issue_agent.domain.agent_task import RunIntent, run_input_fingerprint
from issue_agent.domain.contracts import BudgetScope, Money, PriceQuote, ReservationId, RunId
from issue_agent.domain.workflow import ResolveAdmission, ResolveEpisode, ResolveStage
from issue_agent.domain.workflow_identity import WorkflowWriter
from issue_agent.domain.resolve_split import AdjudicationInput, SplitContext, SplitSelection
from issue_agent.domain.resolve_return import ResolveInputs
from tests.adapters.test_workflow_control import NOW
from tests.fakes.workflow_authority import install_workflow, resolve_snapshot

OWNER = "o/r:fix:test"


@pytest.fixture
def control(tmp_path):
    store = SQLiteControlStore(str(tmp_path / "control.db"), now=lambda: NOW)
    store.initialize()
    install_workflow(store, resolve_snapshot())
    return store


def request(name, *, binding=None, stage="RESOLVE", ceiling=None, cap=100, intent=None, split_input=None):
    binding = binding or resolve_snapshot().binding
    intent = intent or RunIntent(f"Resolve issue {binding.fence.key.target.number}")
    if stage == "AUTHOR" and split_input is None:
        split_input = SplitContext(f"fix/{binding.fence.key.target.number}-contract", SplitSelection("codex", "model"))
    encoded = encode_workflow(split_input) if split_input is not None else None
    return ReservationRequest(
        ReservationId("reservation:" + name), RunId(name),
        json.dumps({"repository": "o/r", "role": "fix", "kind": stage,
                    "leg_id": "leg:" + name, "runtime": "codex",
                    "input_fingerprint": run_input_fingerprint(intent),
                    "split_input": encoded, "split_fingerprint": hashlib.sha256(encoded.encode()).hexdigest() if encoded else None,
                    "resolve_inputs": encode_workflow(ResolveInputs("title", "body", "alice")),
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


def adjudication_input(control):
    from tests.adapters.test_split_workflow_control import petition, reset_ack
    return AdjudicationInput(reset_ack(control, petition(control)))


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


def test_author_admission_retains_supplied_inputs_and_replay_cannot_change_them(control):
    intent = RunIntent("Author issue 7 contract", system_instruction="Preserve public behavior",
                       schema={"type": "object", "required": ["tests"]})
    accepted = request("author", stage="AUTHOR", intent=intent)
    assert control.reserve(accepted).admitted
    restarted = SQLiteControlStore(control.path, now=lambda: NOW)
    assert admission(restarted, "author").input_fingerprint == run_input_fingerprint(intent)
    assert restarted.reserve(accepted).admitted
    changed = request("author", stage="AUTHOR", intent=replace(intent, prompt="Changed requirements"))
    assert restarted.reserve(changed).disposition is AccountingDisposition.CONFLICT
    assert snapshot(restarted).revision.version == 1


@pytest.mark.parametrize("fingerprint", [None, "", True, 123, "a" * 63, "a" * 65, "G" * 64, "a" * 63 + "\n"])
def test_author_without_valid_input_provenance_cannot_buy_admission(control, fingerprint):
    original = request("author", stage="AUTHOR")
    metadata = json.loads(original.dispatch_id)
    if fingerprint is None:
        metadata.pop("input_fingerprint")
    else:
        metadata["input_fingerprint"] = fingerprint
    assert not control.reserve(replace(original, dispatch_id=json.dumps(metadata))).admitted
    assert admission(control, "author") is None
    assert snapshot(control).revision.version == 0
    assert control.active_reservation_runs() == ()


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
    supplied = adjudication_input(control)
    first = request("first", stage="ADJUDICATE", ceiling=1, split_input=supplied)
    assert control.reserve(first).admitted
    # No completion is needed: a provider crash cannot restore this grant.
    with control.transaction() as db:
        db.execute("UPDATE workflows SET generation=generation+1, version=version+1")
    current = snapshot(control)
    restarted = SQLiteControlStore(control.path, now=lambda: NOW)
    denied = restarted.reserve(request("second", binding=current.binding, stage="ADJUDICATE", ceiling=1, split_input=supplied))
    assert not denied.admitted
    with restarted.transaction() as db:
        assert admitted_adjudications(db, current.binding) == 1
    assert admission(control, "second") is None
    assert snapshot(control).revision == current.revision
    assert admission(control, "first").adjudication_ceiling == 1


def test_new_episode_gets_new_adjudication_allowance_without_deleting_old_runs(control):
    supplied = adjudication_input(control)
    assert control.reserve(request("old", stage="ADJUDICATE", ceiling=1, split_input=supplied)).admitted
    prior = snapshot(control)
    with control.transaction() as db:
        db.execute("UPDATE workflows SET generation=generation+1, version=version+1, episode_json=?",
                   (encode_workflow((prior.authority, ResolveEpisode("new-episode"))),))
    with control.transaction() as db:
        assert admitted_adjudications(db, snapshot(control).binding) == 0
        assert admitted_adjudications(db, prior.binding) == 1
    assert admission(control, "old").attempt.binding.episode_id != snapshot(control).binding.episode_id


@pytest.mark.parametrize("ceiling", [None, 0])
def test_missing_or_zero_adjudication_allowance_cannot_admit(control, ceiling):
    result = control.reserve(request("denied", stage="ADJUDICATE", ceiling=ceiling))
    assert not result.admitted
    assert admission(control, "denied") is None
    assert snapshot(control).revision.version == 0


def test_concurrent_adjudication_admissions_share_one_allowance(control):
    supplied = adjudication_input(control)
    before = snapshot(control).revision.version
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = tuple(pool.map(lambda n: control.reserve(request(f"adj-{n}", stage="ADJUDICATE", ceiling=1, split_input=supplied)), range(4)))
    assert sum(result.admitted for result in results) == 1
    assert snapshot(control).revision.version == before + 1


def test_duplicate_adjudication_cannot_change_the_accepted_ceiling(control):
    supplied = adjudication_input(control)
    before = snapshot(control).revision.version
    accepted = request("adj", stage="ADJUDICATE", ceiling=1, split_input=supplied)
    assert control.reserve(accepted).admitted
    assert control.reserve(accepted).admitted
    assert control.reserve(replace(accepted, adjudication_ceiling=2)).disposition is AccountingDisposition.CONFLICT
    assert snapshot(control).revision.version == before + 1


def test_proven_unlaunched_adjudication_reuses_its_reset_and_single_round_allowance(control):
    from tests.adapters.test_split_workflow_control import authority
    supplied = adjudication_input(control)
    first = request("never-launched", stage="ADJUDICATE", ceiling=1, split_input=supplied)
    assert control.reserve(first).admitted
    assert control.release_unlaunched(first.run_id, "test spawn failure").disposition is AccountingDisposition.AVAILABLE
    with control.transaction() as db:
        assert admitted_adjudications(db, snapshot(control).binding) == 0
    with authority(control, ceiling=1).contract_reset_authority(supplied.reset.intent) as recovered:
        assert recovered == supplied.reset
    second = request("exact-retry", stage="ADJUDICATE", ceiling=1, split_input=supplied)
    assert control.reserve(second).admitted
    with control.transaction() as db:
        assert admitted_adjudications(db, snapshot(control).binding) == 1
    assert not control.reserve(request("unknown-third", stage="ADJUDICATE", ceiling=1, split_input=supplied)).admitted


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
