"""PR-scoped review-rejection operator API over real review authority (ADR 0015 review slice, increment 7).

Composed cases run the production review daemon (``review_acceptance_harness``)
beside the dashboard's real FastAPI app. Every request opens a fresh
``open_review_operator`` from the deployment's launch snapshot; only the GitHub
read transport is injected. Assertions read HTTP responses, GitHub writes and
durable authority; nothing here executes an effect except the daemon's drain.
"""

from contextlib import closing
from dataclasses import replace
from functools import partial
from concurrent.futures import ThreadPoolExecutor
import json
import shlex
import sqlite3

import pytest

pytest.importorskip("fastapi")

from asgi_client import ASGITestClient
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.store import Store
from issue_agent.adapters.review_workflow_control import SQLiteReviewWorkflowControl
from issue_agent.app import review_runtime, roles
from issue_agent.app.dashweb import server
from issue_agent.app.dashweb.service import Backend
from issue_agent.app.review_control import open_review_operator
from issue_agent.domain.labels import Label
from issue_agent.domain.review_rejection import MAX_COUNTED_REVIEWS
from issue_agent.domain.workflow_identity import WorkflowFamily
from issue_agent.domain.world import IssueView, PRView, WorldState
from tests.adapters.test_review_workflow_control import Rig
from tests.fakes.persistent_github import PersistentGitHub
from tests.fakes.review_workflow import HEAD
from tests.app.resolve_acceptance_harness import ScriptedProvider
from tests.app.review_acceptance_harness import (
    BOT, FIX_BOT, NHR, PR, REPO, TRIGGER, ReviewComposition, compose_review, create_review_deployment, install_clock,
    isolate_authority, verdicts, register_review_launch,
)
from tests.app.test_review_acceptance import escalate, progress_until
from tests.adapters.test_review_execution_control import rig as current_execution_rig

BASE = "http://127.0.0.1:8321"
HEADERS = {"X-Issue-Agent-Dashboard": "1"}
DEPLOYMENT = "demo"
PATH = f"/api/repos/{DEPLOYMENT}/pulls/{PR}/review-control"
COMMANDS = PATH + "/commands"
RC, OK = "REQUEST_CHANGES", "APPROVE"


def write_snapshot(deployment, **overrides: str) -> None:
    """The launch snapshot the operator transport loads (``issue-agent start``)."""
    config = deployment.root / "config"
    config.mkdir(exist_ok=True)
    env = {**deployment.env(), **overrides}
    (config / f".active.{DEPLOYMENT}.env").write_text("".join(f"{k}={shlex.quote(v)}\n" for k, v in env.items()))


class Api:
    """The real dashboard app over one deployment; `opened` records every operator composition."""

    def __init__(self, root, control_path: str, now, factory):
        self.opened: list[str] = []

        def counting(name, **kwargs):
            self.opened.append(name)
            return factory(name, **kwargs)
        backend = Backend(harness_root=str(root), control_db_path=control_path, review_factory=counting, now_fn=now)
        backend.names = lambda: (DEPLOYMENT,)
        self.app = server.create_app(backend=backend, harness_root=str(root), offload_backend=False)
        self.client = ASGITestClient(self.app, base_url=BASE, headers=HEADERS)

    def get(self, request_id: str | None = None, *, path: str = PATH):
        return self.client.get(path, params={} if request_id is None else {"request_id": request_id})

    def read(self, request_id: str | None = None) -> dict:
        response = self.get(request_id)
        assert response.status_code == 200, response.text
        return response.json()

    def post(self, body, **kwargs):
        return self.client.post(COMMANDS, json=body, **kwargs)


def http(deployment, *, factory=None) -> Api:
    """Production ``open_review_operator`` from the launch snapshot; only GitHub reads are injected."""
    return Api(deployment.root, deployment.control_path, deployment.clock.now,
               factory or partial(open_review_operator, gateway=lambda _cfg, _env: deployment.github()))


def dump(path: str) -> tuple[str, ...]:
    with closing(sqlite3.connect(path)) as db:
        return tuple(db.iterdump())


def gate_of(control: dict, kind: str) -> dict:
    return next(gate for gate in control["gates"] if gate["cause"]["kind"] == kind)


def request_body(control: dict, request_id: str = "http:request", *, guidance: dict | None = None) -> dict:
    return {"operation": "request_rearm", "expected": control["expected"], "request_id": request_id,
            "gate_id": gate_of(control, "review_rejection_ceiling")["gate_id"],
            "guidance": guidance or {"kind": "keep"}}


@pytest.fixture
def bare(tmp_path, monkeypatch):
    deployment = create_review_deployment(tmp_path)
    isolate_authority(deployment, monkeypatch)
    install_clock(deployment, monkeypatch)
    write_snapshot(deployment)
    return deployment


@pytest.fixture
def deployment(bare, monkeypatch):
    bare.activate()
    compose_review(bare).activate_execution(monkeypatch)
    return bare


@pytest.fixture
def escalated(deployment):
    daemon = compose_review(deployment, provider=ScriptedProvider(verdicts(RC, RC, OK)))
    escalate(daemon)
    return daemon


# -- reads -------------------------------------------------------------------------------------------


def test_current_execution_http_poll_stale_renewal_and_original_request_replay_are_bound_and_inert(
    tmp_path, current_execution_rig,
):
    from issue_agent.domain.review_dispatch_record import DecisionBasis, DecisionRead, ReviewNotAdmitted
    from tests.adapters.test_review_execution_renewal_transactions import workflow_control

    store, before, gateway, selection, _ = current_execution_rig
    control = workflow_control(current_execution_rig)
    control.record_dispatch_decision(selection.observation.key, ReviewNotAdmitted(("execution exhausted",)),
        DecisionBasis(gateway.view.head_ref_oid, selection.observation.state, gateway.view.labels, ()),
        DecisionRead.REVIEW_READ)
    api = Api(tmp_path, store.path, store.now, lambda _name, **_kwargs: workflow_control(current_execution_rig))
    path = "/api/repos/demo/pulls/9/review-control"
    saved = dump(store.path)
    gateway.calls.clear()
    inspected = api.get(path=path).json()
    assert inspected["execution"]["kind"] == "current"
    assert inspected["execution"]["failures"] == 3
    ((choice),) = inspected["execution"]["recovery_choices"]
    assert choice["actions"][0]["operation"] == "renew_execution"
    for _ in range(3):
        assert api.get(path=path).json() == inspected
    assert dump(store.path) == saved and gateway.calls == []
    body = {"operation": "renew_execution", "expected": inspected["expected"], "request_id": "http:renew",
        "gate_id": choice["gate_id"], "guidance": {"kind": "keep"},
        "execution_epoch": inspected["execution"]["execution_epoch"],
        "execution_episode": inspected["execution"]["execution_episode"]}
    stale = {**body, "expected": {**body["expected"], "version": body["expected"]["version"] + 1}}
    assert api.client.post(path + "/commands", json=stale).status_code == 409
    assert dump(store.path) == saved
    accepted = api.client.post(path + "/commands", json=body)
    assert accepted.status_code == 202, accepted.text
    after = accepted.json()["control"]
    assert after["execution"]["failures"] == 0
    assert after["execution"]["execution_episode"] != before.execution.binding.execution_episode
    assert after["current_intent"]["authorized_reviews"] == []
    saved = dump(store.path)
    gateway.calls.clear()
    replay = api.client.post(path + "/commands", json=body)
    assert replay.status_code == 200 and replay.json()["outcome"] == "replayed"
    assert replay.json()["control"] == after
    assert dump(store.path) == saved and gateway.calls == []

def test_an_escalated_ceiling_is_projected_read_only_with_its_counted_reviews_and_lever(escalated):
    deployment = escalated.deployment
    api = http(deployment)
    before, writes = dump(deployment.control_path), escalated.remote.calls
    control = api.read()
    assert control["status"] == "available" and control["workflow_held"]
    assert control["expected"]["pull_request"] == PR and control["expected"]["family"] == "review_rejections"
    gate = gate_of(control, "review_rejection_ceiling")
    reviews = escalated.remote.pull_request_reviews(PR).reviews
    rejections = [r.review_id for r in reviews if r.author == BOT and r.state.value == "CHANGES_REQUESTED"]
    assert rejections
    assert [item["review_id"] for item in gate["cause"]["counted"]] == rejections
    assert all(item["author"] == BOT for item in gate["cause"]["counted"])
    assert gate["cause"]["counted_total"] == len(rejections)
    assert gate["cause"]["authorization_bound"] == MAX_COUNTED_REVIEWS
    assert gate["cause"]["hold"] == {"state": "restoring", "restores": 0, "restore_limit": 3}
    (action,) = gate["actions"]
    assert action["state"] == "offered" and action["operation"] == "request_rearm"
    # The escalation swapped the trigger for the gate's projection: the daemon records the gate, not a label hold.
    daemon = control["daemon"]
    assert daemon["state"] == "recorded"
    assert daemon["dispatch"] == {"kind": "held", "gate_ids": [gate["gate_id"]]}
    assert daemon["pull_request"]["state"] == "open" and daemon["pull_request"]["head_sha"] == deployment.heads[1]
    assert daemon["pull_request"]["labels"] == [NHR] and daemon["pull_request"]["external_holds"] == []
    assert control["current_intent"] is None
    # Inspection is SQLite-only: no GitHub write, no authority change.
    assert dump(deployment.control_path) == before and escalated.remote.calls == writes


def test_a_rejection_only_family_with_no_observed_pull_request_is_absent(bare):
    bare.activate()
    deployment = bare
    response = http(deployment).get()
    assert response.status_code == 404
    assert response.json() == {"status": "absent", "repository": REPO, "pull_request": PR, "epoch": 1}


def test_an_inactive_family_offers_no_lever_and_refuses_commands(bare):
    compose_review(bare).boot()
    api = http(bare)
    before = dump(bare.control_path)
    response = api.get()
    assert response.status_code == 200
    assert response.json() == {"status": "migration_required", "repository": REPO,
                               "pull_request": PR, "active_families": []}
    body = {"operation": "request_rearm", "request_id": "r", "gate_id": "g", "guidance": {"kind": "keep"},
            "expected": {"repository": REPO, "pull_request": PR, "family": "review_rejections", "epoch": 1,
                         "generation": 0, "version": 1, "episode_id": "e"}}
    refused = api.post(body)
    assert refused.status_code == 422 and refused.json()["outcome"] == "rejected"
    assert "not active" in refused.json()["reason"]
    assert dump(bare.control_path) == before


def test_another_active_family_makes_review_control_migration_required(bare):
    store = SQLiteControlStore(bare.control_path, now=bare.clock.now)
    store.initialize()
    with store.transaction() as db:
        db.execute("INSERT INTO workflow_authority_epochs VALUES (?, ?, 1, 'test', 'test', ?)",
                   (REPO, WorkflowFamily.RESOLVE.value, bare.clock.now().isoformat()))
    response = http(bare).get()
    assert response.status_code == 200
    assert response.json()["status"] == "migration_required"
    assert response.json()["active_families"] == ["resolve"]


def test_corrupt_or_lost_authority_is_a_typed_unavailable_never_a_crash_or_inactive(escalated):
    deployment = escalated.deployment
    api = http(deployment)
    body = request_body(api.read())
    with closing(sqlite3.connect(deployment.control_path)) as db, db:
        db.execute("UPDATE workflows SET episode_json='{}' WHERE target_number=?", (PR,))
    before = dump(deployment.control_path)
    broken = api.get()
    assert broken.status_code == 503 and broken.json()["status"] == "unavailable" and broken.json()["reason"]
    refused = api.post(body)
    assert refused.status_code == 503 and refused.json()["outcome"] == "unavailable"
    assert refused.json()["control"]["status"] == "unavailable"
    assert dump(deployment.control_path) == before
    with closing(sqlite3.connect(deployment.control_path)) as db, db:
        db.execute("DELETE FROM workflow_authority_epochs")
    lost = api.get()
    assert lost.status_code == 503 and lost.json()["status"] == "unavailable"


def test_an_unopenable_operator_is_unavailable_for_reads_and_commands(escalated):
    deployment = escalated.deployment
    body = request_body(http(deployment).read())

    def failing(_name, **_kwargs):
        raise sqlite3.OperationalError("database is locked")
    api = http(deployment, factory=failing)
    read = api.get()
    assert read.status_code == 503 and read.json() == {"status": "unavailable", "reason": "database is locked"}
    command = api.post(body)
    assert command.status_code == 503 and command.json()["outcome"] == "unavailable"


@pytest.mark.parametrize("overrides,reason", [
    ({"ROLES": "fix steward"}, "review role"),
    ({"REPO": ""}, "repository"),
])
def test_a_snapshot_without_review_authority_never_opens_an_operator(escalated, overrides, reason):
    write_snapshot(escalated.deployment, **overrides)
    response = http(escalated.deployment).get()
    assert response.status_code == 503 and reason in response.json()["reason"]


def test_an_operator_bound_to_another_control_database_is_refused(escalated, tmp_path):
    deployment = escalated.deployment
    opened = partial(open_review_operator, gateway=lambda _c, _e: deployment.github())
    api = http(deployment, factory=lambda name, **kwargs: opened(
        name, **{**kwargs, "control_db_path": str(deployment.root / "other.sqlite")}))
    response = api.get()
    assert response.status_code == 503 and "control authority" in response.json()["reason"]


def test_a_pull_request_closed_during_a_rearm_reads_terminal_before_its_open_hold(escalated):
    api = http(escalated.deployment)
    assert api.post(request_body(api.read())).status_code == 202
    escalated.remote.external_pr_change(PR, state="CLOSED")
    escalated.tick(3)
    control = api.read()
    assert control["current_intent"]["state"] == "superseded"
    assert control["workflow_held"] and control["daemon"]["pull_request"]["state"] == "closed"
    assert control["daemon"]["dispatch"] == {"kind": "terminal", "state": "closed"}
    assert offered(control) == ["continue_rearm"]
    (action,) = [action for gate in control["gates"] for action in gate["actions"]]
    assert "acceptance re-reads GitHub and refuses while it stays closed" in action["effect"]
    refused = api.post({"operation": "continue_rearm", "expected": control["expected"], "request_id": "http:closed",
                        "stopped_request_id": control["current_intent"]["request_id"]})
    assert refused.status_code == 422 and refused.json()["reason"] == "the pull request is terminal"


# -- commands ----------------------------------------------------------------------------------------

def test_a_rearm_is_accepted_durably_replayed_and_completed_only_by_the_review_drain(escalated):
    deployment = escalated.deployment
    api = http(deployment)
    control = api.read()
    gate = gate_of(control, "review_rejection_ceiling")
    body = request_body(control, guidance={"kind": "set", "text": "Focus on the failing edge case."})
    writes = escalated.remote.calls
    accepted = api.post(body, headers={"User-Agent": "forged-identity"})
    assert accepted.status_code == 202 and accepted.json()["outcome"] == "accepted"
    pending = accepted.json()["control"]
    assert pending["requested_intent"]["state"] == "pending"
    assert pending["requested_intent"]["authorized_reviews"] == [c["review_id"] for c in gate["cause"]["counted"]]
    obligations = [command["obligation"] for command in pending["requested_intent"]["commands"]]
    assert [o["review_id"] for o in obligations if o["kind"] == "dismiss_review"] == \
        [c["review_id"] for c in gate["cause"]["counted"]]
    assert {"kind": "projection", "remove": NHR, "add": TRIGGER} in obligations
    assert {command["status"] for command in pending["requested_intent"]["commands"]} == {"pending"}
    (rearming,) = pending["gates"]
    assert rearming["state"] == "rearming" and [a["operation"] for a in rearming["actions"]] == ["cancel_rearm"]
    # The command decides nothing about dispatch: the daemon's recorded decision is unchanged until it acts.
    assert pending["daemon"] == control["daemon"]
    # Acceptance is durable intent only: the HTTP request wrote nothing to GitHub.
    assert escalated.remote.calls == writes
    before = dump(deployment.control_path)
    replay = api.post(body)
    assert replay.status_code == 200 and replay.json()["outcome"] == "replayed"
    assert dump(deployment.control_path) == before
    reused = api.post({**body, "guidance": {"kind": "clear"}})
    assert reused.status_code == 409 and reused.json()["outcome"] == "conflict"
    assert dump(deployment.control_path) == before
    progress_until(escalated, lambda c: len(c.provider.tasks) == 3)
    done = api.read(body["request_id"])
    assert done["requested_intent"]["state"] == "completed"
    assert sorted(done["requested_intent"]["verified_commands"]) == \
        sorted(c["command_id"] for c in pending["requested_intent"]["commands"])
    assert done["gates"] == [] and not done["workflow_held"]
    assert done["continuation"]["kind"] == "reviewed_baseline"
    # The daemon's plan bought that review; its record says so until its next plan settles it.
    assert done["daemon"]["dispatch"] == {"kind": "owed", "review": "re_review"}
    escalated.tick()
    assert api.read()["daemon"]["dispatch"] == {"kind": "not_owed", "reason": "the reviewed baseline covers this head"}
    assert TRIGGER in escalated.labels() and NHR not in escalated.labels()


def test_cancel_stops_the_request_and_leaves_a_decision_gate_offering_its_continuation(escalated):
    api = http(escalated.deployment)
    body = request_body(api.read())
    pending = api.post(body).json()["control"]
    cancel = {"operation": "cancel_rearm", "expected": pending["expected"], "cancellation_id": "http:cancel",
              "request_id": body["request_id"], "reason": "wrong reviews"}
    response = api.post(cancel)
    assert response.status_code == 202 and response.json()["outcome"] == "accepted"
    cancelled = response.json()["control"]
    assert cancelled["requested_intent"]["state"] == "cancelled"
    assert cancelled["requested_intent"]["reason"] == "wrong reviews"
    (gate,) = cancelled["gates"]
    (action,) = gate["actions"]
    assert gate["cause"] == {"kind": "rearm_needs_decision", "stopped_request_id": body["request_id"],
                             "obligations": gate["cause"]["obligations"], "reason": "operator_cancelled"}
    assert (action["state"], action["operation"]) == ("offered", "continue_rearm")
    assert cancelled["daemon"] == pending["daemon"]
    assert api.post(cancel).status_code == 200
    escalated.tick(3)
    assert [c for c in escalated.remote.calls if c[0] == "dismiss_review"] == []
    assert NHR in escalated.labels()


def test_a_superseded_request_is_continued_through_its_decision_gate(escalated):
    deployment = escalated.deployment
    api = http(deployment)
    body = request_body(api.read())
    assert api.post(body).status_code == 202
    escalated.remote.external_pr_change(PR, head_ref_oid=deployment.heads[2])
    escalated.tick(3)
    stopped = api.read()
    assert stopped["current_intent"]["state"] == "superseded"
    decision = gate_of(stopped, "rearm_needs_decision")
    (action,) = decision["actions"]
    assert action == {"state": "offered", "operation": "continue_rearm", "effect": action["effect"]}
    continuation = {"operation": "continue_rearm", "expected": stopped["expected"], "request_id": "http:continue",
                    "stopped_request_id": body["request_id"]}
    response = api.post(continuation)
    assert response.status_code == 202 and response.json()["control"]["requested_intent"]["state"] == "pending"
    progress_until(escalated, lambda c: len(c.provider.tasks) == 3)
    assert api.read("http:continue")["requested_intent"]["state"] == "completed"


@pytest.mark.parametrize("field", ["version", "generation"])
def test_a_stale_revision_conflicts_without_changing_authority(escalated, field):
    api = http(escalated.deployment)
    body = request_body(api.read())
    body["expected"][field] += 1
    before = dump(escalated.deployment.control_path)
    response = api.post(body)
    assert response.status_code == 409 and response.json()["outcome"] == "conflict"
    assert dump(escalated.deployment.control_path) == before


@pytest.mark.parametrize("field,value", [("epoch", 2), ("episode_id", "forged-episode")])
def test_a_command_naming_another_epoch_or_episode_conflicts(escalated, field, value):
    api = http(escalated.deployment)
    body = request_body(api.read())
    body["expected"][field] = value
    before = dump(escalated.deployment.control_path)
    response = api.post(body)
    assert response.status_code == 409 and "epoch or episode" in response.json()["reason"]
    assert dump(escalated.deployment.control_path) == before


@pytest.mark.parametrize("field,value", [("repository", "o/other"), ("pull_request", PR + 1)])
def test_a_command_for_another_target_is_rejected(escalated, field, value):
    api = http(escalated.deployment)
    body = request_body(api.read())
    body["expected"][field] = value
    response = api.post(body)
    assert response.status_code == 422 and response.json()["outcome"] == "rejected"


def test_a_lost_submission_is_retried_exactly_once_by_an_http_grant(deployment):
    lost = [True]

    def fault(point):
        if point == "review_requested" and lost[0]:
            raise TimeoutError("request lost")
    daemon = compose_review(deployment, fault=fault, provider=ScriptedProvider(verdicts(RC)))
    daemon.boot()
    daemon.tick()
    progress_until(daemon, lambda c: "RetryAuthorizationRequired" in c.gate_causes(), limit=8, minutes=30)
    lost[0] = False
    api = http(deployment)
    control = api.read()
    gate = gate_of(control, "retry_authorization_required")
    assert gate["cause"]["retry_authorizable"] is True
    assert [(a["state"], a["operation"]) for a in gate["actions"]] == [("offered", "authorize_retry")]
    assert control["daemon"]["dispatch"] == {"kind": "held", "gate_ids": [gate["gate_id"]]}
    grant = {"operation": "authorize_retry", "expected": control["expected"], "authorization_id": "http:grant",
             "gate_id": gate["gate_id"], "command_id": gate["cause"]["command_id"],
             "evidence_id": gate["cause"]["absence_evidence_id"]}
    accepted = api.post(grant)
    assert accepted.status_code == 202 and accepted.json()["outcome"] == "accepted"
    waiting = gate_of(accepted.json()["control"], "retry_authorization_required")
    assert waiting["pending_retry"] == {"authorization_id": "http:grant", "command_id": grant["command_id"]}
    assert waiting["cause"]["retry_authorizable"] is False
    (refused,) = waiting["actions"]
    assert refused["state"] == "refused" and "http:grant" in refused["reason"]
    assert daemon.submissions() == []
    assert api.post(grant).status_code == 200
    daemon.tick(3)
    assert len(daemon.submissions()) == 1 and len(daemon.provider.tasks) == 1
    assert api.read()["gates"] == []


def test_a_retry_grant_on_a_closed_pull_request_is_refused_and_never_spent(deployment):
    lost = [True]

    def fault(point):
        if point == "review_requested" and lost[0]:
            raise TimeoutError("request lost")
    daemon = compose_review(deployment, fault=fault, provider=ScriptedProvider(verdicts(RC)))
    daemon.boot()
    daemon.tick()
    progress_until(daemon, lambda c: "RetryAuthorizationRequired" in c.gate_causes(), limit=8, minutes=30)
    lost[0] = False
    api = http(deployment)
    gate = gate_of(api.read(), "retry_authorization_required")
    daemon.remote.external_pr_change(PR, state="CLOSED")
    daemon.tick()
    closed = api.read()
    assert closed["daemon"]["dispatch"] == {"kind": "terminal", "state": "closed"}
    (action,) = gate_of(closed, "retry_authorization_required")["actions"]
    assert action["state"] == "offered" and "acceptance re-reads GitHub and refuses while it stays closed" in \
        action["effect"]
    grant = {"operation": "authorize_retry", "expected": closed["expected"], "authorization_id": "http:closed-grant",
             "gate_id": gate["gate_id"], "command_id": gate["cause"]["command_id"],
             "evidence_id": gate["cause"]["absence_evidence_id"]}
    before = dump(deployment.control_path)
    refused = api.post(grant)
    assert refused.status_code == 422 and refused.json()["reason"] == "the pull request is terminal"
    assert dump(deployment.control_path) == before  # no grant stored, so none can be spent on a certain refusal
    daemon.remote.external_pr_change(PR, state="OPEN")
    accepted = api.post(grant)
    assert accepted.status_code == 202 and accepted.json()["outcome"] == "accepted"


# -- boundary ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("change", [
    {"actor": {"subject": "admin"}}, {"reviews": ["r1"]}, {"operation": "dismiss_everything"},
    {"request_id": None}, {"request_id": "x" * 513}, {"request_id": "\ud800"},
    {"guidance": {"kind": "set", "text": "x" * 4001}}, {"guidance": {"kind": "keep", "text": "no"}},
    {"expected": None}, {"gate_id": ""},
])
def test_forged_or_oversized_commands_never_open_authority(escalated, change):
    api = http(escalated.deployment)
    body = request_body(api.read())
    api.opened.clear()
    before = dump(escalated.deployment.control_path)
    response = api.client.post(COMMANDS, content=json.dumps({**body, **change}),
                               headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert api.opened == [] and dump(escalated.deployment.control_path) == before


@pytest.mark.parametrize("field,value", [
    ("epoch", 0), ("epoch", True), ("generation", -1), ("version", 2**63), ("family", "resolve"),
    ("repository", "O/R"), ("pull_request", 0), ("episode_id", "x" * 513),
])
def test_malformed_expected_revisions_never_open_authority(escalated, field, value):
    api = http(escalated.deployment)
    body = request_body(api.read())
    body["expected"][field] = value
    api.opened.clear()
    assert api.post(body).status_code == 422 and api.opened == []


def test_an_oversized_cancellation_reason_is_refused(escalated):
    api = http(escalated.deployment)
    control = api.read()
    cancel = {"operation": "cancel_rearm", "expected": control["expected"], "cancellation_id": "c",
              "request_id": "r", "reason": "x" * 16385}
    api.opened.clear()
    assert api.post(cancel).status_code == 422 and api.opened == []


@pytest.mark.parametrize("path,params,status", [
    (PATH.replace(f"/pulls/{PR}/", "/pulls/0/"), {}, 422),
    (PATH.replace(f"/pulls/{PR}/", f"/pulls/{2**63}/"), {}, 422),
    (PATH.replace(f"/pulls/{PR}/", "/pulls/seven/"), {}, 422),
    (PATH, {"request_id": "x" * 513}, 422),
    (PATH, {"request_id": "bad\x00id"}, 422),
    (PATH.replace("/demo/", "/unknown/"), {}, 404),
])
def test_malformed_reads_are_refused_before_opening_authority(escalated, path, params, status):
    api = http(escalated.deployment)
    assert api.client.get(path, params=params).status_code == status
    assert api.opened == []


@pytest.mark.parametrize("headers,base,status", [
    ({}, BASE, 403), ({**HEADERS, "Origin": "http://hostile.test"}, BASE, 403),
    (HEADERS, "http://rebound.test:8321", 421),
])
def test_the_http_guard_refuses_cross_origin_commands_before_authority(escalated, headers, base, status):
    api = http(escalated.deployment)
    body = request_body(api.read())
    api.opened.clear()
    response = ASGITestClient(api.app, headers=headers, base_url=base).post(COMMANDS, json=body)
    assert response.status_code == status and api.opened == []


# -- the authorization bound (seeded authority: 70 counted rejections) ------------------------------

def test_a_ceiling_beyond_the_authorization_bound_is_shown_with_its_refusal_and_refused_on_submit(tmp_path):
    rig = Rig(tmp_path, reviews=0)
    for index in range(70):
        rig.remote.seed_review(9, f"r{index}", author=BOT, commit=HEAD,
                               submitted_at=f"2026-09-25T11:{index // 60:02d}:{index % 60:02d}+00:00")
    rig.gated()

    api = Api(tmp_path, rig.store.path, lambda: rig.clock[0], lambda _name, **_kwargs: rig.control())
    path = "/api/repos/demo/pulls/9/review-control"
    control = api.client.get(path).json()
    gate = gate_of(control, "review_rejection_ceiling")
    assert gate["cause"]["counted_total"] == len(gate["cause"]["counted"]) == 70
    (action,) = gate["actions"]
    assert action["state"] == "refused" and str(MAX_COUNTED_REVIEWS) in action["reason"]
    before = dump(rig.store.path)
    body = request_body(control)
    response = api.client.post(path + "/commands", json=body)
    assert response.status_code == 422 and response.json()["outcome"] == "rejected"
    assert str(MAX_COUNTED_REVIEWS) in response.json()["reason"]
    assert dump(rig.store.path) == before
    assert [c for c in rig.remote.calls if c[0] == "dismiss_review"] == []
    assert Label.NEEDS_HUMAN_REVIEW in rig.remote.get_pr(9).labels


# -- holds that only reconciliation or a human change ends ------------------------------------------

def test_a_contested_ceiling_hold_is_shown_with_its_spent_restorations_and_keeps_its_lever(escalated):
    api = http(escalated.deployment)
    control = api.read()
    for _ in range(60):
        hold = gate_of(control, "review_rejection_ceiling")["cause"]["hold"]
        if hold["state"] == "contested":
            break
        escalated.remote.external_pr_change(PR, labels=[TRIGGER])  # an automation keeps removing the hold
        escalated.tick(minutes=5)
        control = api.read()
    gate = gate_of(control, "review_rejection_ceiling")
    assert gate["cause"]["hold"] == {"state": "contested", "restores": 3, "restore_limit": 3}
    assert control["daemon"]["dispatch"] == {"kind": "held", "gate_ids": [gate["gate_id"]]}
    assert [(a["state"], a["operation"]) for a in gate["actions"]] == [("offered", "request_rearm")]
    assert len(escalated.provider.tasks) == 2


def test_an_exhausted_escalation_hold_is_a_visible_gate_without_an_operator_command(deployment):
    war = [True]

    def fault(point):
        if point == "reviews_page:0" and war[0] and daemon.submissions():
            labels = {label.value for label in daemon.remote.get_pr(PR).labels}
            daemon.remote.external_pr_change(PR, labels=sorted(labels ^ {"no-ai-review"}))
    daemon = compose_review(deployment, fault=fault, provider=ScriptedProvider(verdicts("ESCALATE", OK)))
    daemon.boot()
    daemon.tick()
    progress_until(daemon, lambda c: "ReviewProjectionRefused" in c.gate_causes(), limit=40, minutes=10)
    war[0] = False
    control = http(deployment).read()
    gate = gate_of(control, "review_projection_refused")
    assert gate["cause"]["projection"] == "escalation_hold" and gate["actions"] == []
    assert control["daemon"]["dispatch"] == {"kind": "held", "gate_ids": [gate["gate_id"]]}


def test_disabling_the_ceiling_releases_an_open_ceiling_gate_at_its_next_reconciliation(escalated):
    """HARNESS.md: REVIEW_MAX_REJECTIONS=0 releases every open ceiling on its next reconciliation."""
    disabled = replace(escalated.deployment, ceiling=0)
    write_snapshot(disabled)
    daemon = compose_review(disabled, provider=ScriptedProvider(verdicts(OK)))
    api = http(disabled)
    held = api.read()
    assert gate_of(held, "review_rejection_ceiling")
    progress_until(daemon, lambda c: c.gate_causes() == [], limit=12, minutes=30)
    released = api.read()
    assert released["gates"] == [] and not released["workflow_held"]
    assert released["expected"]["generation"] == held["expected"]["generation"] + 1
    progress_until(daemon, lambda c: len(c.provider.tasks) == 1, limit=6, minutes=5)
    assert TRIGGER in daemon.labels() and NHR not in daemon.labels()


# -- the review daemon's own decision, as of its last read ---------------------------------------------

class TriggerSlice(PersistentGitHub):
    """The production review read (``GitHubAdapter.build_review_world``): trigger-labelled pull requests only."""

    def build_review_world(self) -> WorldState:
        world = super().build_review_world()
        return replace(world, prs=tuple(pr for pr in world.prs if Label.AI_PR_OPEN in pr.labels))


def compose_sliced(deployment, provider: ScriptedProvider, *, fault=lambda _: None) -> ReviewComposition:
    """`compose_review` over the trigger-labelled review read, so a held or closed pull request leaves it."""
    cfg = deployment.config()
    remote = TriggerSlice(deployment.github_path, fault=fault, clock=deployment.clock.now)
    provider.launch = register_review_launch
    log: list[str] = []
    review = roles.compose_review_daemon(cfg, remote, Store(cfg.review_state_dir), provider, log=log.append)
    return ReviewComposition(deployment, cfg, review, remote, provider, log)


def reviewed_once(deployment) -> ReviewComposition:
    daemon = compose_review(deployment, provider=ScriptedProvider(verdicts(OK, OK)))
    daemon.boot()
    daemon.tick(2)
    assert len(daemon.provider.tasks) == 1
    return daemon


def offered(control: dict) -> list[str]:
    return [action["operation"] for gate in control["gates"] for action in gate["actions"]
            if action["state"] == "offered"]


def test_a_push_reads_as_the_daemons_last_decision_until_its_plan_owes_a_review_then_settles(deployment):
    daemon = reviewed_once(deployment)
    api = http(deployment)
    reviewed = api.read()["daemon"]
    assert reviewed["dispatch"] == {"kind": "not_owed", "reason": "the reviewed baseline covers this head"}
    assert reviewed["pull_request"]["head_sha"] == deployment.heads[0]
    daemon.remote.external_pr_change(PR, head_ref_oid=deployment.heads[1])
    # The view never re-derives: until the daemon reads the push it shows the last decision, dated.
    assert api.read()["daemon"] == reviewed
    daemon.tick()
    owed = api.read()["daemon"]
    assert owed["dispatch"] == {"kind": "owed", "review": "re_review"}
    assert owed["pull_request"]["head_sha"] == deployment.heads[1] and owed["decided_at"] > reviewed["as_of"]
    assert len(daemon.provider.tasks) == 2
    daemon.tick()
    settled = api.read()["daemon"]
    assert settled["dispatch"] == reviewed["dispatch"] and settled["decided_at"] > owed["as_of"]
    assert len(daemon.provider.tasks) == 2


def test_an_unchanged_decision_is_dated_by_its_last_bounded_confirmation(escalated):
    api = http(escalated.deployment)
    first = api.read()["daemon"]
    escalated.tick(minutes=1)
    assert api.read()["daemon"] == first  # confirmed unchanged within the bound: not rewritten
    escalated.tick(minutes=5)
    confirmed = api.read()["daemon"]
    assert confirmed["decided_at"] == first["decided_at"] and confirmed["as_of"] > first["as_of"]
    assert confirmed["dispatch"] == first["dispatch"]
    escalated.deployment.advance(minutes=90)  # a stopped daemon: the view keeps its last read's date
    assert api.read()["daemon"] == confirmed


@pytest.mark.parametrize("change,holds", [
    ({"labels": [NHR]}, [NHR]),
    ({"labels": ["ai-parked"]}, ["ai-parked"]),
    ({"labels": [], "is_draft": True}, ["draft"]),  # unlabelled, and not adoptable
    ({"labels": [TRIGGER, "ai-parked"]}, None),
])
def test_an_externally_held_pull_request_is_recorded_not_admitted_and_bought_no_review(deployment, change, holds):
    daemon = reviewed_once(deployment)
    daemon.remote.external_pr_change(PR, head_ref_oid=deployment.heads[1], **change)
    daemon.tick(2)
    dispatch = http(deployment).read()["daemon"]["dispatch"]
    assert dispatch["kind"] == "not_admitted"
    if holds is not None:
        assert dispatch["holds"] == holds
    else:
        assert any("ai-parked" in hold for hold in dispatch["holds"]), dispatch
    assert len(daemon.provider.tasks) == 1


@pytest.mark.parametrize("labels", [[NHR], ["ai-parked"], [NHR, "no-ai-review"]])
def test_a_hold_outside_the_review_workflow_is_named_even_when_no_review_is_owed(deployment, labels):
    daemon = reviewed_once(deployment)
    daemon.remote.external_pr_change(PR, labels=labels)
    daemon.tick()
    assert http(deployment).read()["daemon"]["dispatch"] == {"kind": "not_admitted", "holds": labels}


def test_an_unlabelled_pull_request_the_daemon_adopts_reads_owed_and_is_reviewed(deployment):
    daemon = reviewed_once(deployment)
    daemon.remote.external_pr_change(PR, head_ref_oid=deployment.heads[1], labels=[])
    daemon.tick()
    assert http(deployment).read()["daemon"]["dispatch"] == {"kind": "owed", "review": "re_review"}
    assert len(daemon.provider.tasks) == 2


def test_a_rearm_under_an_external_hold_promises_no_review(escalated):
    escalated.remote.external_pr_change(PR, labels=[NHR, "ai-parked"])
    escalated.tick()
    control = http(escalated.deployment).read()
    assert control["daemon"]["dispatch"]["kind"] == "held"
    assert control["daemon"]["pull_request"]["external_holds"] == ["ai-parked"]
    (action,) = gate_of(control, "review_rejection_ceiling")["actions"]
    assert "buys no review" in action["effect"] and "ai-parked" in action["effect"]
    assert "one fresh review is owed" not in action["effect"]


def test_linked_issue_hold_is_the_daemons_recorded_reason_and_the_rearm_keeps_it(escalated, monkeypatch):
    escalated.remote.relabel("issue", 1, None, Label.AI_PARKED.value)
    escalated.tick()
    api = http(escalated.deployment)
    control = api.read()
    reason = "linked issue #1: ai-parked"
    assert control["daemon"]["pull_request"]["external_holds"] == [reason]
    (action,) = gate_of(control, "review_rejection_ceiling")["actions"]
    assert "buys no review" in action["effect"] and reason in action["effect"]
    paid = len(escalated.provider.tasks)
    request = request_body(control, "linked-hold:rearm")
    response = api.post(request)
    assert response.status_code == 202, response.text
    reads = []

    def unavailable(self, number):
        reads.append(number)
        raise TimeoutError("linked issue unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(PersistentGitHub, "get_issue", unavailable)
        replay = api.post(request)
    assert replay.status_code == 200 and replay.json()["outcome"] == "replayed" and reads == []
    escalated.tick(3)
    assert len(escalated.provider.tasks) == paid
    assert Label.AI_PARKED in escalated.remote.get_issue(1).labels


def test_a_linked_issue_hold_is_visible_for_a_departed_pull_request(deployment):
    daemon = ceiling_held_outside_the_review_read(deployment)
    daemon.remote.relabel("issue", 1, None, Label.NEEDS_BUDGET_REVIEW.value)
    daemon.tick(minutes=31)
    control = http(deployment).read()
    assert control["daemon"]["read"] == "departed_read"
    assert control["daemon"]["pull_request"]["external_holds"] == ["linked issue #1: needs-budget-review"]


def test_a_terminal_departed_pr_is_recorded_when_its_linked_issue_is_unavailable(deployment, monkeypatch):
    daemon = ceiling_held_outside_the_review_read(deployment)
    daemon.remote.external_pr_change(PR, state="CLOSED")
    paid = len(daemon.provider.tasks)
    monkeypatch.setattr(PersistentGitHub, "get_issue", _raise(TimeoutError("linked issue unavailable")))
    daemon.tick(minutes=31)
    recorded = http(deployment).read()["daemon"]
    assert recorded["read"] == "departed_read"
    assert recorded["dispatch"] == {"kind": "terminal", "state": "closed"}
    assert len(daemon.provider.tasks) == paid


def departed_read_journal(deployment) -> list[str]:
    with closing(sqlite3.connect(deployment.control_path)) as db:
        return [row[0] for row in db.execute(
            "SELECT event_json FROM event_outbox WHERE event_json LIKE '%review_departed_read%' ORDER BY rowid")]


def hold_fix_claim(deployment) -> None:
    """A live fix-role dispatch claim on the pull request's issue, as the fix daemon takes one (#176)."""
    store = SQLiteControlStore(deployment.control_path, now=deployment.clock.now)
    assert store.claim_dispatch(REPO, "fix", 1, "ADDRESS_REVIEW", "fix-owner", frozenset({"thing.py"}))


def ceiling_held_outside_the_review_read(deployment, *, fault=lambda _: None) -> ReviewComposition:
    """An escalated pull request the trigger-labelled review read no longer returns."""
    daemon = compose_sliced(deployment, ScriptedProvider(verdicts(RC, RC, OK)), fault=fault)
    escalate(daemon)
    daemon.tick()
    return daemon


def test_a_ceiling_held_pull_request_is_recorded_held_by_its_gate_and_its_rearm_returns_it_to_review(deployment):
    daemon = ceiling_held_outside_the_review_read(deployment)
    control = http(deployment).read()
    gate = gate_of(control, "review_rejection_ceiling")
    assert control["daemon"]["dispatch"] == {"kind": "held", "gate_ids": [gate["gate_id"]]}
    assert control["daemon"]["read"] == "departed_read"
    (action,) = gate["actions"]
    assert "one fresh review is owed" in action["effect"] and "buys no review" not in action["effect"]
    reviews = len(daemon.provider.tasks)
    daemon.tick(3, minutes=5)
    assert len(daemon.provider.tasks) == reviews


def test_a_closed_pull_request_keeps_its_lever_and_acceptance_refuses_while_it_stays_closed(deployment):
    daemon = ceiling_held_outside_the_review_read(deployment)
    api = http(deployment)
    held = api.read()
    assert offered(held) == ["request_rearm"]
    daemon.remote.external_pr_change(PR, state="CLOSED")
    daemon.tick(minutes=1)
    stale = api.read()
    # Not yet re-read: the view keeps the dated decision rather than guessing the pull request's state.
    assert stale["daemon"] == held["daemon"] and offered(stale) == ["request_rearm"]
    refused_live = api.post(request_body(stale))
    assert refused_live.status_code == 422 and refused_live.json()["reason"] == "the pull request is terminal"
    daemon.tick(minutes=15)
    closed = api.read()
    assert closed["daemon"]["dispatch"] == {"kind": "terminal", "state": "closed"}
    assert closed["daemon"]["pull_request"]["state"] == "closed" and closed["workflow_held"]
    assert offered(closed) == ["request_rearm"]
    (action,) = gate_of(closed, "review_rejection_ceiling")["actions"]
    assert "closed (as of " in action["effect"] and "refuses while it stays closed" in action["effect"]
    before = dump(deployment.control_path)
    refused = api.post(request_body(closed, "http:after-close"))
    assert refused.status_code == 422 and refused.json()["reason"] == "the pull request is terminal"
    assert dump(deployment.control_path) == before


def test_a_closed_ceiling_is_never_retired_and_its_gate_is_re_read_on_the_short_cadence(deployment):
    daemon = ceiling_held_outside_the_review_read(deployment)
    api = http(deployment)
    def held_gates(control):
        return [(gate["gate_id"], gate["state"], gate["cause"]) for gate in control["gates"]]
    gates = held_gates(api.read())
    daemon.remote.external_pr_change(PR, state="CLOSED")
    reads = []
    for _ in range(16):  # eight hours closed
        daemon.tick(minutes=30)
        reads.append(api.read()["daemon"]["as_of"])
    closed = api.read()
    assert held_gates(closed) == gates and closed["workflow_held"]
    assert closed["daemon"]["dispatch"] == {"kind": "terminal", "state": "closed"}
    assert len(set(reads)) >= 15  # re-read every tick past 15 minutes, never parked for six hours


def test_a_pull_request_closed_for_over_a_day_is_re_read_on_the_slow_cadence_and_keeps_its_lever(deployment):
    daemon = ceiling_held_outside_the_review_read(deployment)
    api = http(deployment)
    daemon.remote.external_pr_change(PR, state="CLOSED")
    daemon.tick(26, minutes=60)
    assert api.read()["daemon"]["dispatch"] == {"kind": "terminal", "state": "closed"}
    reads = set()
    for _ in range(12 * 4):  # twelve more hours of fifteen-minute ticks
        daemon.tick(minutes=15)
        reads.add(api.read()["daemon"]["as_of"])
    assert len(reads) <= 3  # every six hours, not every fifteen minutes
    assert offered(api.read()) == ["request_rearm"]


def test_a_reopened_pull_request_is_seen_promptly_and_its_rearm_is_accepted(deployment):
    daemon = ceiling_held_outside_the_review_read(deployment)
    api = http(deployment)
    daemon.remote.external_pr_change(PR, state="CLOSED")
    daemon.tick(minutes=16)
    assert api.read()["daemon"]["dispatch"] == {"kind": "terminal", "state": "closed"}
    daemon.remote.external_pr_change(PR, state="OPEN")
    daemon.tick(minutes=16)
    reopened = api.read()
    gate = gate_of(reopened, "review_rejection_ceiling")
    assert reopened["daemon"]["dispatch"] == {"kind": "held", "gate_ids": [gate["gate_id"]]}
    assert reopened["daemon"]["pull_request"]["state"] == "open"
    (action,) = gate["actions"]
    assert action["state"] == "offered" and "closed" not in action["effect"]
    accepted = api.post(request_body(reopened, "http:reopened"))
    assert accepted.status_code == 202 and accepted.json()["outcome"] == "accepted"


def failed_reads(daemon_view: dict) -> int:
    return (daemon_view["read_failure"] or {}).get("failed_reads", 0)


def test_a_failed_departed_read_keeps_the_last_good_facts_and_backs_off_to_a_journalled_ceiling(deployment):
    outage = [False]

    def fault(point):
        if point == "observation_read" and outage[0]:
            raise TimeoutError("GitHub unreachable")
    daemon = ceiling_held_outside_the_review_read(deployment, fault=fault)
    api = http(deployment)
    held = api.read()["daemon"]
    assert held["dispatch"]["kind"] == "held" and held["read"] == "departed_read" and held["read_failure"] is None
    outage[0] = True
    daemon.tick(minutes=16)
    failing = api.read()["daemon"]
    # The last good read's decision and facts stand, as of that read; the failures annotate them.
    assert {k: failing[k] for k in ("dispatch", "pull_request", "decided_at", "as_of", "read")} == {
        k: held[k] for k in ("dispatch", "pull_request", "decided_at", "as_of", "read")}
    assert failing["read_failure"]["failed_reads"] == 1 and "own read failed" in failing["read_failure"]["reason"]
    daemon.tick(minutes=5)
    assert api.read()["daemon"] == failing  # not re-read before its schedule
    attempts = []
    for _ in range(3 * 24 * 4):  # three days of fifteen-minute ticks, GitHub still failing
        daemon.tick(minutes=15)
        attempts.append(failed_reads(api.read()["daemon"]))
    assert max(attempts) <= 20  # exponential backoff to a six-hour ceiling, not a read every tick
    journal = departed_read_journal(deployment)
    assert [event for event in journal if "read_failed" in event] == [journal[0]]
    assert [event for event in journal if "read_backoff_ceiling" in event] == [journal[1]] and len(journal) == 2
    outage[0] = False
    daemon.tick(minutes=6 * 60)
    recovered = api.read()["daemon"]
    assert recovered["dispatch"] == held["dispatch"] and recovered["read_failure"] is None
    assert recovered["as_of"] > held["as_of"]


def test_a_pull_request_whose_first_own_read_fails_is_deferred_without_facts(deployment):
    outage = [False]

    def fault(point):
        if point == "observation_read" and outage[0]:
            raise TimeoutError("GitHub unreachable")
    daemon = compose_sliced(deployment, ScriptedProvider(verdicts(OK)), fault=fault)
    daemon.boot()
    daemon.tick(2)
    daemon.remote.external_pr_change(PR, labels=[])  # leaves the review read; that read's decision is stale now
    outage[0] = True
    daemon.tick()
    deferred = api_daemon = http(deployment).read()["daemon"]
    assert api_daemon["dispatch"]["kind"] == "deferred" and "own read failed" in deferred["dispatch"]["reason"]
    assert deferred["pull_request"] is None and deferred["read"] == "departed_read" and failed_reads(deferred) == 1


OTHER_PR = 8


def owed_beside_a_departing_pull_request(deployment) -> ReviewComposition:
    """PR 7, reviewed, leaves the review read (due its own read at once) as PR 8 opens, owed its first review."""
    daemon = compose_sliced(deployment, ScriptedProvider(verdicts(OK, OK, OK, OK)))
    daemon.boot()
    daemon.tick(2)
    assert len(daemon.provider.tasks) == 1
    daemon.remote.external_pr_change(PR, labels=[])
    daemon.remote.seed(IssueView(2, title="Add other", state="open"))
    daemon.remote.seed_pr(PRView(
        OTHER_PR, head_ref_name="fix/2-other", head_ref_oid=deployment.heads[2], base_ref_oid=deployment.base,
        title="Add other", body="Adds the other thing.", labels=frozenset({Label.AI_PR_OPEN}), state="OPEN",
        closing_issue=2, author=FIX_BOT, base_ref_name="dev"))
    return daemon


def review_claims(deployment) -> tuple:
    return SQLiteControlStore(deployment.control_path, now=deployment.clock.now).active_dispatch_claims(REPO, "review")


def _raise(error):
    def fail(*_args, **_kwargs):
        raise error
    return fail


_RECORDING_FAULTS = {
    # Before admission: the departed pull requests' own reads.
    "departed listing": (SQLiteReviewWorkflowControl, "departed_pull_requests",
                         sqlite3.OperationalError("database is locked"), True),
    "departed read": (review_runtime, "read_pull_request", sqlite3.OperationalError("disk I/O error"), True),
    "departed facts": (SQLiteReviewWorkflowControl, "external_holds", RuntimeError("hold source bug"), False),
    # After admission: pure construction, then the one write.
    "construction": (review_runtime, "admitted_decision", RuntimeError("construction bug"), True),
    "write": (SQLiteReviewWorkflowControl, "record_dispatch_decisions", sqlite3.OperationalError("database is locked"),
              True),
}


@pytest.mark.parametrize("fault", list(_RECORDING_FAULTS))
def test_a_failure_while_recording_decisions_never_drops_or_strands_an_admitted_review(deployment, monkeypatch, fault):
    owner, name, error, reported = _RECORDING_FAULTS[fault]
    daemon = owed_beside_a_departing_pull_request(deployment)
    original = getattr(owner, name)

    def departing_target_only(*args, **kwargs):
        target = args[1].target if name == "read_pull_request" else args[1].key.target
        if target.number == PR:
            raise error
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, departing_target_only if fault in {"departed read", "departed facts"}
                        else _raise(error))
    daemon.tick()
    # The owed review is dispatched that tick, and its reservation is released when it finishes.
    assert len(daemon.provider.tasks) == 2 and review_claims(deployment) == ()
    reports = [line for line in daemon.log if "review dispatch decisions not recorded" in line]
    assert len(reports) == (1 if reported else 0), daemon.log
    if not reported:  # a failed departed read is that pull request's own recorded failure
        departed = http(deployment).read()["daemon"]
        assert departed["read_failure"]["failed_reads"] == 1 and "unavailable" in departed["read_failure"]["reason"]


def test_a_persistent_recording_failure_is_reported_once_per_interval_with_what_it_suppressed(deployment, monkeypatch):
    daemon = reviewed_once(deployment)
    monkeypatch.setattr(SQLiteReviewWorkflowControl, "record_dispatch_decisions",
                        _raise(sqlite3.OperationalError("database is locked")))
    for _ in range(3):
        daemon.tick(minutes=5)
    reports = [line for line in daemon.log if "review dispatch decisions not recorded" in line]
    assert len(reports) == 1 and "(0 earlier failures unreported)" in reports[0]
    daemon.tick(minutes=16)
    reports = [line for line in daemon.log if "review dispatch decisions not recorded" in line]
    assert len(reports) == 2 and "(2 earlier failures unreported)" in reports[1]
    with closing(sqlite3.connect(deployment.control_path)) as db:
        journalled = db.execute("SELECT COUNT(*) FROM event_outbox WHERE event_json LIKE '%review_dispatch_record%' "
                                "AND event_json LIKE '%record_failed%'").fetchone()[0]
    assert journalled == 2


def test_a_review_owed_behind_another_deployments_claim_is_recorded_withheld_reserved(deployment):
    daemon = reviewed_once(deployment)
    daemon.remote.external_pr_change(PR, head_ref_oid=deployment.heads[1])
    store = SQLiteControlStore(deployment.control_path, now=deployment.clock.now)
    assert store.claim_dispatch(REPO, "review", 1, "RE_REVIEW", "other-deployment", frozenset({"pr:7"}))
    daemon.tick()
    dispatch = http(deployment).read()["daemon"]["dispatch"]
    assert dispatch["kind"] == "withheld" and dispatch["review"] == "re_review" and dispatch["reason"] == "reserved"
    assert "already reserved" in dispatch["detail"] and len(daemon.provider.tasks) == 1


def test_failed_batch_admission_releases_its_claims_and_the_next_tick_can_review(deployment, monkeypatch):
    daemon = owed_beside_a_departing_pull_request(deployment)
    daemon.remote.external_pr_change(PR, labels=[TRIGGER], head_ref_oid=deployment.heads[1])
    original = SQLiteControlStore.claim_dispatch
    calls = 0

    def fail_second(store, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError("database is locked")
        return original(store, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(SQLiteControlStore, "claim_dispatch", fail_second)
        with pytest.raises(sqlite3.OperationalError):
            daemon.review.daemon.decide(daemon.review.daemon.observe())
    assert review_claims(deployment) == ()
    assert len(daemon.provider.tasks) == 1
    daemon.tick(3)
    assert len(daemon.provider.tasks) == 3
    assert review_claims(deployment) == ()


def test_local_authority_failure_does_not_back_off_a_departed_pull_request(deployment, monkeypatch):
    daemon = owed_beside_a_departing_pull_request(deployment)
    with monkeypatch.context() as fault:
        fault.setattr(SQLiteReviewWorkflowControl, "external_holds", _raise(sqlite3.OperationalError("disk I/O error")))
        daemon.tick()
    # A healthy subsequent tick must read the departed PR immediately, without
    # a remote-failure schedule caused by the local database outage.
    daemon.remote.external_pr_change(PR, state="CLOSED")
    daemon.tick()
    decision = http(deployment).read()["daemon"]["dispatch"]
    assert decision == {"kind": "terminal", "state": "closed"}
    assert any("control unavailable" in line for line in daemon.log)


def test_a_review_that_is_already_running_is_recorded_in_flight_not_withheld(deployment):
    seen: dict = {}
    composed: dict = {}

    def during(_task):
        daemon = composed.get("daemon")
        if daemon is None or "view" in seen:
            return
        review = daemon.review.daemon
        review.decide(review.observe())  # a tick while this pull request's own review still runs
        with ThreadPoolExecutor(1) as pool:
            seen["view"] = pool.submit(lambda: http(deployment).read()["daemon"]["dispatch"]).result()
    daemon = compose_review(deployment, provider=ScriptedProvider(verdicts(OK, OK), during=during))
    daemon.boot()
    daemon.tick(2)
    composed["daemon"] = daemon
    daemon.remote.external_pr_change(PR, head_ref_oid=deployment.heads[1])
    daemon.tick(2)
    assert seen["view"] == {"kind": "in_flight", "review": "re_review"}
    assert len(daemon.provider.tasks) == 2


def test_a_review_owed_behind_a_fix_run_is_recorded_withheld_and_bought_no_review(deployment):
    daemon = reviewed_once(deployment)
    daemon.remote.external_pr_change(PR, head_ref_oid=deployment.heads[1])
    hold_fix_claim(deployment)
    daemon.tick()
    dispatch = http(deployment).read()["daemon"]["dispatch"]
    assert dispatch["kind"] == "withheld" and dispatch["review"] == "re_review"
    assert dispatch["reason"] == "fix_claim" and "fix run" in dispatch["detail"]
    assert len(daemon.provider.tasks) == 1


def test_a_closed_pull_request_with_an_unresolved_submission_reads_terminal_first(deployment):
    def fault(point):
        if point == "review_requested":
            raise TimeoutError("request lost")
    daemon = compose_review(deployment, fault=fault, provider=ScriptedProvider(verdicts(RC)))
    daemon.boot()
    daemon.tick()
    api = http(deployment)
    progress_until(daemon, lambda _: api.read()["daemon"]["dispatch"]["kind"] == "submission_unresolved",
                   limit=6, minutes=1)
    daemon.remote.external_pr_change(PR, state="CLOSED")
    daemon.tick()
    closed = api.read()
    assert closed["daemon"]["dispatch"] == {"kind": "terminal", "state": "closed"}
    assert offered(closed) == []


# -- command refusals the view must match ------------------------------------------------------------

def test_a_rearm_against_a_moved_head_is_refused_without_authority_change(escalated):
    api = http(escalated.deployment)
    body = request_body(api.read())
    escalated.remote.external_pr_change(PR, head_ref_oid=escalated.deployment.heads[2])
    before = dump(escalated.deployment.control_path)
    response = api.post(body)
    assert response.status_code == 422 and response.json()["outcome"] == "rejected"
    assert "head moved" in response.json()["reason"]
    assert dump(escalated.deployment.control_path) == before
    assert [c for c in escalated.remote.calls if c[0] == "dismiss_review"] == []


def test_a_rearm_of_a_pull_request_closed_since_the_view_is_refused(escalated):
    api = http(escalated.deployment)
    body = request_body(api.read())
    escalated.remote.external_pr_change(PR, state="CLOSED")
    before = dump(escalated.deployment.control_path)
    response = api.post(body)
    assert response.status_code == 422 and response.json()["reason"] == "the pull request is terminal"
    assert dump(escalated.deployment.control_path) == before


def test_a_continuation_is_refused_for_an_unknown_request_and_conflicts_on_a_reused_id(escalated):
    deployment = escalated.deployment
    api = http(deployment)
    body = request_body(api.read())
    assert api.post(body).status_code == 202
    escalated.remote.external_pr_change(PR, head_ref_oid=deployment.heads[2])
    escalated.tick(3)
    stopped = api.read()
    continuation = {"operation": "continue_rearm", "expected": stopped["expected"], "request_id": "http:continue",
                    "stopped_request_id": body["request_id"]}
    before = dump(deployment.control_path)
    unknown = api.post({**continuation, "stopped_request_id": "http:never-requested"})
    assert unknown.status_code == 422 and unknown.json()["outcome"] == "rejected"
    assert unknown.json()["reason"] == "continuation requires the current stopped review intent"
    assert dump(deployment.control_path) == before
    reused_stopped = api.post({**continuation, "request_id": body["request_id"]})
    assert reused_stopped.status_code == 422 and api.opened[-1:] == [DEPLOYMENT]
    assert dump(deployment.control_path) == before
    assert api.post(continuation).status_code == 202
    after = dump(deployment.control_path)
    conflicting = api.post({**continuation, "stopped_request_id": "http:other"})
    assert conflicting.status_code == 409 and conflicting.json()["outcome"] == "conflict"
    assert "reused" in conflicting.json()["reason"] and dump(deployment.control_path) == after


# -- repository family status (the legacy unblock's authority) ---------------------------------------

def test_the_family_status_names_the_active_families(bare):
    families = "/api/repos/demo/workflow-families"
    api = http(bare)
    # The legacy unblock refuses an uninitialized authority too: an unread activation never reads as none.
    response = api.client.get(families)
    assert response.status_code == 503 and response.json()["status"] == "unavailable"
    compose_review(bare).boot()
    inactive = api.client.get(families)
    assert inactive.status_code == 200 and inactive.json()["active_families"] == []
    bare.activate()
    active = api.client.get(families)
    assert active.status_code == 200
    assert active.json() == {"status": "available", "repository": REPO, "active_families": ["review_rejections"]}


def test_closed_operator_transport_captures_fixed_execution_identity_and_returns_accepted_state(current_execution_rig):
    from issue_agent.app.dashweb.review_contracts import ReviewRequestExecutionRenewal
    from issue_agent.app.dashweb.review_control import submit_review_control

    from tests.adapters.test_review_execution_renewal_transactions import renewal_request, workflow_control

    rig = current_execution_rig
    request = renewal_request(rig)
    binding = request.expected_binding
    body = ReviewRequestExecutionRenewal.model_validate({
        "operation": "renew_execution", "request_id": request.request_id, "gate_id": request.gate_id,
        "guidance": {"kind": "keep"}, "execution_epoch": request.execution_epoch,
        "execution_episode": request.execution_episode,
        "expected": {"repository": "o/r", "pull_request": 9, "family": "review_rejections",
                     "epoch": binding.authority.epoch, "generation": binding.fence.generation,
                     "version": request.expected.version, "episode_id": binding.episode_id},
    })
    assert body.command() == request
    result = submit_review_control(workflow_control(rig), 9, body)
    assert result.outcome == "accepted"
    assert result.control.current_intent.authorized_reviews == []
    assert result.control.gates[-1].cause.kind == "review_execution_ceiling"
