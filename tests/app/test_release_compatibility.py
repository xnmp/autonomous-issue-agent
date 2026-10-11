"""Release transport must retain the pre-deployment operator contract."""

from copy import deepcopy
from dataclasses import replace
import hashlib
import os

import pytest

from issue_agent.adapters.workflow_codec import decode_workflow, encode_workflow
from issue_agent.app.dashweb.review_contracts import ReviewExpected
from issue_agent.domain import workflow as w
from issue_agent.domain.contracts import CommandKind
from tests.adapters.test_review_workflow_control import PR, Rig, expire_absent
from tests.app.test_review_operator_api import Api, dump


@pytest.fixture
def operator(tmp_path):
    rig = Rig(tmp_path)
    rig.gated()
    api = Api(tmp_path, rig.store.path, rig.store.now, lambda _name, **_kwargs: rig.control())
    path = f"/api/repos/demo/pulls/{PR}/review-control"
    return rig, api, path


def rearm(api, path, *, text=" scope "):
    control = api.get(path=path).json()
    return {"operation": "request_rearm", "expected": control["expected"], "request_id": "same-id",
            "gate_id": control["gates"][0]["gate_id"], "guidance": {"kind": "set", "text": text}}


def test_retry_authorization_and_rearm_have_independent_id_namespaces(operator):
    rig, api, path = operator
    body = rearm(api, path)
    assert api.client.post(path + "/commands", json=body).status_code == 202
    dismissal = rig.commands(CommandKind.DISMISS_REVIEW)[0]
    gate = expire_absent(rig, dismissal)
    observed = api.get(path=path).json()
    retry = {"operation": "authorize_retry", "expected": observed["expected"],
             "authorization_id": body["request_id"], "gate_id": gate.gate_id,
             "command_id": str(dismissal), "evidence_id": gate.cause.absence_evidence_id}
    accepted = api.client.post(path + "/commands", json=retry)
    assert accepted.status_code == 202, accepted.text
    assert rig.query("SELECT authorization_id FROM workflow_retry_authorizations")[0][0] == body["request_id"]
    rig.drain()
    assert rig.receipt(dismissal)[0] == "succeeded"


def test_response_actions_remain_readable_by_pre_deployment_clients(operator):
    _, api, path = operator
    response = api.get(path=path)
    assert response.status_code == 200
    actions = response.json()["gates"][0]["actions"]
    assert actions
    assert {action["operation"] for action in actions} == {"request_rearm"}


def test_guidance_normalization_preserves_replay_identity(operator):
    rig, api, path = operator
    body = rearm(api, path)
    accepted = api.client.post(path + "/commands", json=body)
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["control"]["guidance"] == "scope"
    saved = dump(rig.store.path)
    normalized = deepcopy(body)
    normalized["guidance"]["text"] = "scope"
    replay = api.client.post(path + "/commands", json=normalized)
    assert replay.status_code == 200 and replay.json()["outcome"] == "replayed", replay.text
    assert dump(rig.store.path) == saved


def test_accepted_audit_and_digest_use_the_original_domain_command(operator):
    rig, api, path = operator
    body = rearm(api, path)
    assert api.client.post(path + "/commands", json=body).status_code == 202
    expected = ReviewExpected.model_validate(body["expected"])
    command = w.RequestRearm(body["request_id"], expected.revision(), body["gate_id"], w.SetGuidance("scope"),
                             expected_binding=expected.binding())
    row = rig.query("SELECT actor_json,request_digest FROM workflow_rearms")[0]
    audit = decode_workflow(row["actor_json"], w.AuthorizedOperatorCommand)
    assert row["actor_json"] == encode_workflow(replace(audit, command=command))
    assert row["request_digest"] == hashlib.sha256(
        encode_workflow((f"local-user:{os.getuid()}", command)).encode()
    ).hexdigest()


def test_replaced_request_replay_is_rejected_without_writes(operator):
    rig, api, path = operator
    original = rearm(api, path)
    accepted = api.client.post(path + "/commands", json=original)
    assert accepted.status_code == 202, accepted.text
    cancelled = api.client.post(path + "/commands", json={
        "operation": "cancel_rearm", "expected": accepted.json()["control"]["expected"],
        "request_id": original["request_id"], "cancellation_id": "cancel-a", "reason": "stop"})
    assert cancelled.status_code == 202, cancelled.text
    continued = api.client.post(path + "/commands", json={
        "operation": "continue_rearm", "expected": cancelled.json()["control"]["expected"],
        "request_id": "request-b", "stopped_request_id": original["request_id"]})
    assert continued.status_code == 202, continued.text
    saved = dump(rig.store.path)
    calls = list(rig.remote.calls)
    replay = api.client.post(path + "/commands", json=original)
    assert replay.status_code == 422 and replay.json()["outcome"] == "rejected", replay.text
    assert dump(rig.store.path) == saved and rig.remote.calls == calls
