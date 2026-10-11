"""Increment 1: the operator transport preserves existing durable authority."""

from copy import deepcopy
import json
import os

import pytest
from pydantic import TypeAdapter

from issue_agent.app.dashweb.review_contracts import ReviewCommand
from issue_agent.domain import workflow as w
from issue_agent.domain.review_workflow import CompletedReviewRearm, FreshReviewRequired
from tests.adapters.test_review_workflow_control import Rig, PR
from tests.adapters.test_review_execution_control import rig as current_execution_rig
from tests.adapters.test_review_execution_renewal_transactions import workflow_control
from tests.app.test_review_operator_api import Api, dump


def canonical(body):
    operation = body["operation"]
    if operation in ("release", "cancel_rearm"):
        return body
    expected = body["expected"]
    if operation == "continue_rearm":
        selection = {"kind": "stopped_grant", "stopped_request_id": body["stopped_request_id"]}
    elif operation == "authorize_retry":
        selection = {"kind": "gate", "gate_id": body["gate_id"], "intent": {
            "kind": "retry_absent", "command_id": body["command_id"], "evidence_id": body["evidence_id"]}}
    else:
        intent = {"kind": "resume" if operation == "request_rearm" else "renew_execution", "guidance": body["guidance"]}
        if operation != "request_rearm":
            intent.update(execution_epoch=body["execution_epoch"], execution_episode=body["execution_episode"])
        selection = {"kind": "gate", "gate_id": body["gate_id"], "intent": intent}
        if operation == "release_verdict":
            selection = {"kind": "verdict", "gate_id": body["gate_id"], "submission_command_id": body["submission_command_id"],
                         **{key: value for key, value in intent.items() if key != "kind"}}
    return {"operation": "release", "expected": expected,
            "request_id": body.get("request_id", body.get("authorization_id")), "selection": selection}


@pytest.fixture
def http_rig(tmp_path):
    rig = Rig(tmp_path)
    rig.gated()
    path = f"/api/repos/demo/pulls/{PR}/review-control"
    def api(gateway=None):
        return Api(tmp_path, rig.store.path, rig.store.now, lambda _name, **_kwargs: rig.control(gateway=gateway))
    return rig, api, path


def request(api, path, *, text=None):
    control = api.get(path=path).json()
    return {"operation": "request_rearm", "expected": control["expected"], "request_id": "release:one",
            "gate_id": control["gates"][0]["gate_id"],
            "guidance": {"kind": "keep"} if text is None else {"kind": "set", "text": text}}


@pytest.mark.parametrize("format", ["legacy", "release"])
def test_replay_after_restart_head_change_and_outage_preserves_original_bytes_and_actor(http_rig, format):
    rig, factory, path = http_rig
    api = factory()
    old = request(api, path)
    body = old if format == "legacy" else canonical(old)
    accepted = api.client.post(path + "/commands", json=body)
    assert accepted.status_code == 202, accepted.text
    original = [tuple(row) for row in rig.query("SELECT actor_json,request_digest,intent_json FROM workflow_rearms")]
    rig.restart()
    rig.remote.external_pr_change(PR, head_ref_oid="b" * 40)
    class Offline:
        def get_pr(self, _number, **_kwargs):
            raise AssertionError("exact replay must not read GitHub")
        def pull_request_reviews(self, _number):
            raise AssertionError("exact replay must not read GitHub")
    api = factory(Offline())
    saved = dump(rig.store.path)
    replay = api.client.post(path + "/commands", json=body)
    assert replay.status_code == 200 and replay.json()["outcome"] == "replayed", replay.text
    assert dump(rig.store.path) == saved
    assert [tuple(row) for row in rig.query("SELECT actor_json,request_digest,intent_json FROM workflow_rearms")] == original
    other_transport = api.client.post(path + "/commands", json=canonical(old) if format == "legacy" else old)
    assert other_transport.status_code == 200 and other_transport.json()["outcome"] == "replayed"
    command = TypeAdapter(ReviewCommand).validate_python(body).command()
    assert isinstance(rig.control(gateway=Offline()).request_rearm(command, w.OperatorActor("another-operator", "dashboard")), w.WorkflowConflict)
    assert dump(rig.store.path) == saved


@pytest.mark.parametrize("format", ["legacy", "release"])
def test_guidance_identity_is_normalized(http_rig, format):
    rig, factory, path = http_rig
    api = factory()
    old = request(api, path, text=" scope ")
    body = old if format == "legacy" else canonical(old)
    accepted = api.client.post(path + "/commands", json=body)
    assert accepted.status_code == 202
    assert accepted.json()["control"]["guidance"] == "scope"
    saved = dump(rig.store.path)
    changed = deepcopy(body)
    guidance = changed["guidance"] if format == "legacy" else changed["selection"]["intent"]["guidance"]
    guidance["text"] = "scope"
    assert api.client.post(path + "/commands", json=changed).status_code == 200
    assert dump(rig.store.path) == saved


@pytest.mark.parametrize("format", ["legacy", "release"])
def test_partial_dismissal_cancel_continue_finishes_one_fresh_review_obligation(http_rig, format):
    rig, factory, path = http_rig
    api = factory()
    adapt = (lambda body: body) if format == "legacy" else canonical
    body = adapt(request(api, path))
    accepted = api.client.post(path + "/commands", json=body)
    assert accepted.status_code == 202, accepted.text
    first = [True]
    def interrupt(point):
        if point == "dismiss_requested" and not first[0]:
            raise TimeoutError("second dismissal interrupted")
        if point == "dismiss_applied":
            first[0] = False
    rig.remote.fault = interrupt
    rig.drain()
    assert len([call for call in rig.remote.calls if call[0] == "dismiss_review"]) == 1
    pending = api.get(path=path).json()
    cancelled = api.client.post(path + "/commands", json={"operation": "cancel_rearm", "expected": pending["expected"],
        "request_id": "release:one", "cancellation_id": "cancel:one", "reason": "interrupted dismissal"})
    assert cancelled.status_code == 202, cancelled.text
    stopped = cancelled.json()["control"]
    rig.remote.fault = lambda _point: None
    rig.advance(3600)
    rig.drain()
    stopped = api.get(path=path).json()
    continued = adapt({"operation": "continue_rearm", "expected": stopped["expected"],
                      "request_id": "release:continue", "stopped_request_id": "release:one"})
    assert api.client.post(path + "/commands", json=continued).status_code == 202
    for _ in range(4):
        rig.drain()
        rig.advance(3600)
        rig.reconcile()
        if isinstance(rig.workflow().intent, CompletedReviewRearm):
            break
    state = rig.workflow()
    assert isinstance(state.intent, CompletedReviewRearm)
    assert isinstance(state.episode.continuation, FreshReviewRequired)
    assert not state.gates
    dismissals = [call[2] for call in rig.remote.calls if call[0] == "dismiss_review"]
    assert len(dismissals) == len(set(dismissals)) == 2
    saved = dump(rig.store.path)
    assert api.client.post(path + "/commands", json=body).status_code == 422
    assert api.client.post(path + "/commands", json=continued).status_code == 200
    assert dump(rig.store.path) == saved


def test_canonical_execution_renewal_survives_origin_validation_after_restart(tmp_path, current_execution_rig):
    store, before, gateway, _, _ = current_execution_rig
    path = "/api/repos/demo/pulls/9/review-control"
    from issue_agent.domain.review_dispatch_record import DecisionBasis, DecisionRead, ReviewNotAdmitted
    control = workflow_control(current_execution_rig)
    selection = current_execution_rig[3]
    control.record_dispatch_decision(selection.observation.key, ReviewNotAdmitted(("execution exhausted",)),
        DecisionBasis(gateway.view.head_ref_oid, selection.observation.state, gateway.view.labels, ()), DecisionRead.REVIEW_READ)
    api = Api(tmp_path, store.path, store.now, lambda _name, **_kwargs: workflow_control(current_execution_rig))
    observed = api.get(path=path).json()
    choice = observed["execution"]["recovery_choices"][0]
    body = canonical({"operation": "renew_execution", "expected": observed["expected"], "request_id": "canonical:renew",
        "gate_id": choice["gate_id"], "guidance": {"kind": "keep"},
        "execution_epoch": observed["execution"]["execution_epoch"], "execution_episode": observed["execution"]["execution_episode"]})
    accepted = api.client.post(path + "/commands", json=body)
    assert accepted.status_code == 202, accepted.text
    current = workflow_control(current_execution_rig).load_workflow(before.revision.fence.key)
    assert current.execution.state.failures == 0
    assert current.execution.binding.execution_episode != before.execution.binding.execution_episode
    saved = dump(store.path)
    gateway.calls.clear()
    assert api.client.post(path + "/commands", json=body).status_code == 200
    assert dump(store.path) == saved and gateway.calls == []


def test_canonical_verdict_release_preserves_execution_and_has_no_dismissals(tmp_path, current_execution_rig):
    from issue_agent.domain.review_dispatch_record import DecisionBasis, DecisionRead, ReviewNotAdmitted
    from tests.adapters.test_review_productive_settlement import confirmed_escalation
    store, gateway, selection, _, _, submission = confirmed_escalation(current_execution_rig)
    workflow = workflow_control(current_execution_rig)
    assert isinstance(workflow.settle_submission(submission.command_id), w.WorkflowCommitted)
    workflow.record_dispatch_decision(selection.observation.key, ReviewNotAdmitted(("verdict held",)),
        DecisionBasis(gateway.view.head_ref_oid, selection.observation.state, gateway.view.labels, ()), DecisionRead.REVIEW_READ)
    path = "/api/repos/demo/pulls/9/review-control"
    api = Api(tmp_path, store.path, store.now, lambda _name, **_kwargs: workflow_control(current_execution_rig))
    before = api.get(path=path).json()
    choices = before["gates"] + before["execution"]["recovery_choices"]
    choice = next(gate for gate in choices if gate["cause"]["kind"] == "review_verdict_hold")
    body = {"operation": "release", "expected": before["expected"], "request_id": "canonical:verdict",
            "selection": {"kind": "verdict", "gate_id": choice["gate_id"], "guidance": {"kind": "keep"},
                          "execution_epoch": before["execution"]["execution_epoch"],
                          "execution_episode": before["execution"]["execution_episode"],
                          "submission_command_id": str(submission.command_id)}}
    accepted = api.client.post(path + "/commands", json=body)
    assert accepted.status_code == 202, accepted.text
    after = accepted.json()["control"]
    assert after["execution"]["execution_episode"] == before["execution"]["execution_episode"]
    assert after["execution"]["failures"] == before["execution"]["failures"]
    assert after["current_intent"]["authorized_reviews"] == []
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM lifecycle_commands WHERE kind='dismiss_review'").fetchone()[0] == 0
    saved = dump(store.path)
    assert api.client.post(path + "/commands", json=body).status_code == 200
    assert dump(store.path) == saved


@pytest.mark.parametrize("change", [
    lambda b: b.pop("selection"), lambda b: b.pop("request_id"), lambda b: b.update(actor="forged"),
    lambda b: b["selection"].update(kind="fix"), lambda b: b["selection"].update(head_sha="not-a-sha"),
    lambda b: b["expected"].update(generation=True), lambda b: b["expected"].update(pull_request=2**64),
    lambda b: b["expected"].update(epoch=0), lambda b: b["expected"].update(version=-1),
    lambda b: b.update(request_id="x\x00y"), lambda b: b.update(request_id="x" * 513),
    lambda b: b["selection"]["intent"].update(guidance={"kind": "set", "text": "x" * 4001}),
    lambda b: b["selection"]["intent"].update(guidance={"kind": "set", "text": "x\x00y"}),
    lambda b: b["selection"]["intent"].update(guidance={"kind": "set", "text": "\ud800"}),
    lambda b: b["selection"]["intent"].update(guidance=None),
    lambda b: b["selection"].update(intent={"kind": "retry_absent", "command_id": "x"}),
    lambda b: b["expected"].update(repository="foreign/repository"),
    lambda b: b["expected"].update(pull_request=PR + 1),
])
def test_closed_canonical_boundary_rejects_malformed_and_foreign_inputs_without_authority_change(http_rig, change):
    rig, factory, path = http_rig
    api = factory()
    body = canonical(request(api, path))
    change(body)
    saved = dump(rig.store.path)
    calls = rig.remote.calls
    response = api.client.post(path + "/commands", content=json.dumps(body), headers={"content-type": "application/json"})
    assert response.status_code == 422, response.text
    assert dump(rig.store.path) == saved and rig.remote.calls == calls


@pytest.mark.parametrize("raw", ['[' * 25 + '0' + ']' * 25, '[' * 1100 + '0' + ']' * 1100,
                                  '{"operation":"release","padding":"' + 'x' * 262144 + '"}',
                                  '{"operation":"release","operation":"cancel_rearm"}'])
def test_raw_json_bounds_and_duplicate_fields_are_rejected_before_authority(http_rig, raw):
    rig, factory, path = http_rig
    api = factory()
    saved = dump(rig.store.path)
    assert api.client.post(path + "/commands", content=raw, headers={"content-type": "application/json"}).status_code == 422
    assert dump(rig.store.path) == saved


@pytest.mark.parametrize("format", ["legacy", "release"])
def test_proven_absent_release_grants_and_consumes_one_execution_of_the_original_command(http_rig, format):
    from issue_agent.domain.contracts import CommandKind
    from tests.adapters.test_review_workflow_control import expire_absent
    rig, factory, path = http_rig
    api = factory()
    assert api.client.post(path + "/commands", json=canonical(request(api, path))).status_code == 202
    dismissal = rig.commands(CommandKind.DISMISS_REVIEW)[0]
    gate = expire_absent(rig, dismissal)
    observed = api.get(path=path).json()
    body = {"operation": "authorize_retry", "expected": observed["expected"], "authorization_id": "release:one",
            "gate_id": gate.gate_id, "command_id": str(dismissal), "evidence_id": gate.cause.absence_evidence_id}
    body = body if format == "legacy" else canonical(body)
    command_ids = rig.commands()
    accepted = api.client.post(path + "/commands", json=body)
    assert accepted.status_code == 202, accepted.text
    original = [tuple(row) for row in rig.query("SELECT actor_json,request_digest FROM workflow_retry_authorizations")]
    rig.restart()
    assert api.client.post(path + "/commands", json=body).status_code == 200
    rig.drain()
    rig.advance(3600)
    rig.drain()
    assert rig.commands() == command_ids
    assert rig.receipt(dismissal)[0] == "succeeded"
    selected_review = next(call[2] for call in rig.remote.calls if call[0] == "dismiss_review")
    assert [call[2] for call in rig.remote.calls if call[0] == "dismiss_review"].count(selected_review) == 1
    assert [tuple(row) for row in rig.query("SELECT actor_json,request_digest FROM workflow_retry_authorizations")] == original
    saved = dump(rig.store.path)
    assert api.client.post(path + "/commands", json=body).status_code == 200
    assert dump(rig.store.path) == saved


def test_unknown_remote_outcome_cannot_be_released_with_fabricated_absence(http_rig):
    from issue_agent.domain.contracts import CommandKind, EffectReceipt, EffectStatus
    from tests.adapters.test_review_workflow_control import REVIEWER
    rig, factory, path = http_rig
    api = factory()
    assert api.client.post(path + "/commands", json=canonical(request(api, path))).status_code == 202
    dismissal = rig.commands(CommandKind.DISMISS_REVIEW)[0]
    rig.store.begin_command(dismissal)
    rig.store.record_effect(EffectReceipt(dismissal, REVIEWER, rig.clock[0], rig.clock[0], EffectStatus.UNKNOWN_OUTCOME, error="reply lost"))
    rig.points.add("reviews_page:0")
    for _ in range(12):
        rig.advance(1800)
        rig.drain()
    observed = api.get(path=path).json()
    gate = next(g for g in observed["gates"] if g["cause"]["kind"] == "remote_uncertain")
    saved = dump(rig.store.path)
    refused = api.client.post(path + "/commands", json={"operation": "release", "request_id": "retry:unknown",
        "expected": observed["expected"], "selection": {"kind": "gate", "gate_id": gate["gate_id"],
            "intent": {"kind": "retry_absent", "command_id": str(dismissal), "evidence_id": "operator-claim"}}})
    assert refused.status_code == 422
    assert dump(rig.store.path) == saved
    assert rig.query("SELECT COUNT(*) FROM workflow_retry_authorizations")[0][0] == 0


def test_guidance_and_identifier_limits_are_inclusive(http_rig):
    rig, factory, path = http_rig
    api = factory()
    body = canonical(request(api, path, text="x" * 4000))
    body["request_id"] = "r" * 512
    accepted = api.client.post(path + "/commands", json=body)
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["control"]["guidance"] == "x" * 4000
    assert api.client.post(path + "/commands", json=body).status_code == 200


def test_pre_upgrade_normalized_receipt_replays_unchanged(http_rig):
    rig, factory, path = http_rig
    api = factory()
    body = request(api, path, text=" scope ")
    historical = TypeAdapter(ReviewCommand).validate_python(body).command()
    actor = w.OperatorActor(f"local-user:{os.getuid()}", "dashboard")
    assert isinstance(rig.control().request_rearm(historical, actor), w.WorkflowCommitted)
    original = dump(rig.store.path)
    assert api.client.post(path + "/commands", json=body).status_code == 200
    assert dump(rig.store.path) == original
    assert api.client.post(path + "/commands", json=canonical(body)).status_code == 200
    assert dump(rig.store.path) == original
