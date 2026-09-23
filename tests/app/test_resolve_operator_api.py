"""Actual dashboard middleware over isolated durable resolve authority."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import asyncio
import json
import os

import pytest

pytest.importorskip("fastapi")

from asgi_client import ASGITestClient
from fastapi import HTTPException
from issue_agent.app.dashweb import server
from issue_agent.app.dashweb.service import Backend
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.workflow_control import SQLiteWorkflowControl
from issue_agent.domain.workflow import CancelledRearm
from tests.adapters.test_operator_binding import authority_dump
from tests.app.test_resolve_rearm import environment, episode_repo, _replay_client


BASE = "http://127.0.0.1:8321"
PATH = "/api/repos/demo/issues/1/resolve-control"
HEADERS = {"X-Issue-Agent-Dashboard": "1"}


@pytest.fixture
def api(environment, monkeypatch, tmp_path):
    service, store, gateway, repo, clock, logs, command = environment
    factory_calls = []
    def factory(name, **kwargs):
        assert name == "demo"
        assert kwargs["control_db_path"] == store.path
        factory_calls.append(name)
        return service()
    backend = Backend(harness_root=str(tmp_path), control_db_path=store.path,
                      resolve_factory=factory, now_fn=lambda: clock[0])
    monkeypatch.setattr(backend, "names", lambda: ("demo",))
    monkeypatch.setattr(backend, "_clear_retry_counters", lambda *a: pytest.fail("typed command used legacy reset"))
    monkeypatch.setattr(backend, "_operator_effects", lambda *a, **kw: pytest.fail("typed command directly executed remote effects"))
    app = server.create_app(backend=backend, harness_root=str(tmp_path), offload_backend=False)
    client = ASGITestClient(app, base_url=BASE, headers=HEADERS)
    initial = client.get(PATH)
    assert initial.status_code == 200
    state = initial.json()
    body = {"operation": "request_rearm", "expected": state["expected"],
            "request_id": "http-request", "gate_id": state["gates"][0]["gate_id"],
            "guidance": {"kind": "keep"}}
    factory_calls.clear()
    return client, app, backend, environment, body, factory_calls


def test_get_is_authoritative_and_read_only_without_remote_or_git_observation(api, monkeypatch):
    client, app, backend, environment, body, factory_calls = api
    service, store, gateway, repo, clock, logs, command = environment
    before = authority_dump(store)
    monkeypatch.setattr(gateway, "get_issue", lambda *a: pytest.fail("SQL control GET fetched remote state"))
    monkeypatch.setattr(repo.worktrees, "observe_and_pin_episode_base", lambda *a, **kw: pytest.fail("GET fetched Git"))
    state = client.get(PATH).json()
    assert state["status"] == "available" and state["workflow_held"]
    assert state["expected"] == body["expected"]
    assert state["gates"][0]["cause"]["kind"] == "resolve_ceiling"
    assert state["gates"][0]["available_commands"] == ["request_rearm"]
    assert state["current_intent"] is None and state["observation"] is None
    assert authority_dump(store) == before
    assert gateway.calls == [] and repo.runner.calls == []


def test_post_accepts_durable_intent_with_server_minted_actor_and_no_direct_remote_write(api):
    client, app, backend, environment, body, factory_calls = api
    service, store, gateway, repo, clock, logs, command = environment
    response = client.post(PATH + "/commands", json=body, headers={"User-Agent": "forged-browser-identity"})
    assert response.status_code == 202
    result = response.json()
    assert result["outcome"] == "accepted"
    assert result["control"]["workflow_held"]
    assert result["control"]["requested_intent"]["state"] == "pending"
    assert result["control"]["requested_intent"]["request_id"] == body["request_id"]
    audit = service().control.accepted_rearm(command.expected.fence.key, body["request_id"])
    assert audit is not None
    assert audit.actor.subject == f"local-user:{os.getuid()}"
    assert audit.actor.channel == "dashboard"
    assert gateway.calls == []
    assert not any(args[:2] == ("reset", "--hard") for args, _ in repo.runner.calls)
    assert result["control"]["observation"]["next_read_at"]
    before = authority_dump(store)
    replay = client.post(PATH + "/commands", json=body, headers={"User-Agent": "Mozilla/changed"})
    assert replay.status_code == 200 and replay.json()["outcome"] == "replayed"
    assert authority_dump(store) == before


def test_cancel_continue_and_original_replay_return_exact_history_separate_from_current(api, monkeypatch):
    client, app, backend, environment, body, factory_calls = api
    service, store, gateway, repo, clock, logs, command = environment
    first = client.post(PATH + "/commands", json=body).json()["control"]
    cancel = {"operation": "cancel_rearm", "expected": first["expected"],
              "cancellation_id": "http-cancel", "request_id": body["request_id"], "reason": "pause"}
    cancelled_response = client.post(PATH + "/commands", json=cancel)
    assert cancelled_response.status_code == 202
    cancelled = cancelled_response.json()["control"]
    assert cancelled["requested_intent"]["state"] == "cancelled"
    continuation = {"operation": "continue_rearm", "expected": cancelled["expected"],
                    "request_id": "http-continue", "stopped_request_id": body["request_id"]}
    resumed_response = client.post(PATH + "/commands", json=continuation)
    assert resumed_response.status_code == 202
    resumed = resumed_response.json()["control"]
    assert resumed["current_intent"]["state"] == "pending"
    assert resumed["current_intent"]["handoff_id"] == first["current_intent"]["handoff_id"]
    monkeypatch.setattr(gateway, "get_issue", lambda *a: pytest.fail("replay fetched GitHub"))
    prior = authority_dump(store)
    replay = client.post(PATH + "/commands", json=body)
    assert replay.status_code == 200
    read = replay.json()["control"]
    assert read["requested_intent"]["request_id"] == body["request_id"]
    assert read["requested_intent"]["state"] == "cancelled"
    assert read["current_intent"]["request_id"] == continuation["request_id"]
    assert read["current_intent"]["state"] == "pending"
    assert client.get(PATH, params={"request_id": body["request_id"]}).json() == read
    cancel_replay = client.post(PATH + "/commands", json=cancel)
    assert cancel_replay.status_code == 200
    assert cancel_replay.json()["control"]["requested_intent"]["state"] == "cancelled"
    continue_replay = client.post(PATH + "/commands", json=continuation)
    assert continue_replay.status_code == 200
    assert continue_replay.json()["control"]["requested_intent"]["request_id"] == continuation["request_id"]
    assert authority_dump(store) == prior


@pytest.mark.parametrize("field", ["epoch", "episode_id", "version", "generation"])
def test_stale_authority_conflicts_before_git_or_remote_reads(api, monkeypatch, field):
    client, app, backend, environment, body, factory_calls = api
    service, store, gateway, repo, clock, logs, command = environment
    wrong = deepcopy(body)
    wrong["expected"][field] = "other-episode" if field == "episode_id" else wrong["expected"][field] + 1
    prior = authority_dump(store)
    monkeypatch.setattr(gateway, "get_issue", lambda *a: pytest.fail("stale CAS fetched remote state"))
    response = client.post(PATH + "/commands", json=wrong)
    assert response.status_code == 409 and response.json()["outcome"] == "conflict"
    assert authority_dump(store) == prior
    assert repo.runner.calls == []


@pytest.mark.parametrize("field,value", [
    ("epoch", True), ("epoch", 1.0), ("epoch", "1"), ("epoch", None),
    ("epoch", 0), ("generation", -1), ("version", 2**63),
    ("episode_id", "x" * 513), ("episode_id", "\ud800"),
    ("repository", "O/R"), ("issue", False), ("family", "review"),
])
def test_strict_expected_fields_reject_without_opening_authority(api, field, value):
    client, app, backend, environment, body, factory_calls = api
    store = environment[1]
    wrong = deepcopy(body)
    wrong["expected"][field] = value
    before = authority_dump(store)
    response = client.post(PATH + "/commands", content=json.dumps(wrong), headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert factory_calls == []
    assert authority_dump(store) == before


@pytest.mark.parametrize("change", [
    {"actor": {"subject": "admin"}}, {"owner": "o/r:fix"}, {"evidence": "trusted"},
    {"workspace": {}}, {"operation": "reset_everything"}, {"request_id": None},
    {"request_id": "x" * 513}, {"request_id": "\ud800"},
    {"guidance": None}, {"guidance": {"kind": "unknown"}},
    {"guidance": {"kind": "keep", "text": "ignored?"}},
    {"guidance": {"kind": "clear", "text": "ignored?"}},
    {"guidance": {"kind": "set", "text": " "}},
    {"guidance": {"kind": "set", "text": "x" * 4001}},
    {"guidance": {"kind": "set", "text": "<<<escape"}},
    {"guidance": {"kind": "set", "text": "\ud800"}},
])
def test_untrusted_or_malformed_command_fields_cannot_mint_authority(api, change):
    client, app, backend, environment, body, factory_calls = api
    before = authority_dump(environment[1])
    response = client.post(PATH + "/commands", content=json.dumps({**body, **change}), headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert factory_calls == []
    assert authority_dump(environment[1]) == before


def test_invalid_utf8_body_is_controlled_refusal(api):
    client, app, backend, environment, body, factory_calls = api
    response = client.post(PATH + "/commands", content=b'{"operation":"request_rearm","request_id":"\xff"}',
                           headers={"Content-Type": "application/json"})
    assert response.status_code in (400, 422)
    assert factory_calls == []


@pytest.mark.parametrize("headers,base,status", [
    ({}, BASE, 403),
    ({**HEADERS, "Origin": "http://hostile.test"}, BASE, 403),
    (HEADERS, "http://rebound.test:8321", 421),
])
def test_real_http_guard_refuses_before_backend_authority(api, headers, base, status):
    client, app, backend, environment, body, factory_calls = api
    response = ASGITestClient(app, headers=headers, base_url=base).post(PATH + "/commands", json=body)
    assert response.status_code == status
    assert factory_calls == []
    assert environment[3].runner.calls == []


def test_absent_inactive_and_corrupt_authority_remain_distinct(api):
    client, app, backend, environment, body, factory_calls = api
    store = environment[1]
    absent = client.get(PATH.replace("issues/1/", "issues/999/"))
    assert absent.status_code == 404 and absent.json()["status"] == "absent"
    with store.transaction() as db:
        db.execute("UPDATE workflows SET episode_json='{}' WHERE target_number=1")
    before = authority_dump(store)
    broken = client.get(PATH)
    assert broken.status_code == 503 and broken.json()["status"] == "unavailable"
    assert authority_dump(store) == before
    with store.transaction() as db:
        db.execute("DELETE FROM workflow_authority_epochs")
    lost_activation = client.get(PATH.replace("issues/1/", "issues/999/"))
    assert lost_activation.status_code == 503 and lost_activation.json()["status"] == "unavailable"


def test_never_activated_authority_reports_inactive_without_initializing_workflow(api, tmp_path, monkeypatch):
    client, app, backend, environment, body, factory_calls = api
    service = environment[0]
    isolated = SQLiteControlStore(str(tmp_path / "inactive-control.sqlite"))
    isolated.initialize()
    inactive = replace(service(), control=SQLiteWorkflowControl(isolated, service().control.writer, resolve_ceiling=3))
    monkeypatch.setattr(backend, "_resolve_factory", lambda *a, **kw: inactive)
    before = authority_dump(isolated)
    response = client.get(PATH)
    assert response.status_code == 200 and response.json()["status"] == "inactive"
    assert authority_dump(isolated) == before


def test_unknown_deployment_is_refused_before_factory_or_side_effects(api):
    client, app, backend, environment, body, factory_calls = api
    unknown = PATH.replace("/demo/", "/unknown/")
    assert client.get(unknown).status_code == 404
    assert client.post(unknown + "/commands", json=body).status_code == 404
    assert factory_calls == []


def test_remote_unavailability_refuses_without_creating_a_grant(api):
    client, app, backend, environment, body, factory_calls = api
    store, gateway = environment[1:3]
    before = authority_dump(store)
    gateway.fail_on.add("pr_for_issue")
    result = client.post(PATH + "/commands", json=body)
    assert result.status_code == 503 and result.json()["outcome"] == "unavailable"
    assert authority_dump(store) == before


def test_api_reports_pending_until_actual_daemon_replay_verifies_both_obligations(api):
    client, app, backend, environment, body, factory_calls = api
    service, store, gateway, repo, clock, logs, command = environment
    response = client.post(PATH + "/commands", json=body)
    assert response.status_code == 202
    pending = response.json()["control"]
    assert pending["requested_intent"]["state"] == "pending"
    workflow, effects, executor, recovery = _replay_client(service, store, gateway, clock, logs)
    clock[0] += timedelta(minutes=1)
    assert executor.replay_pending()
    after_remote = client.get(PATH).json()
    assert after_remote["current_intent"]["state"] == "pending"
    assert after_remote["workflow_held"]
    clock[0] += timedelta(minutes=1)
    assert executor.replay_pending()
    completed = client.get(PATH, params={"request_id": body["request_id"]}).json()
    assert completed["requested_intent"]["state"] == "completed"
    assert completed["requested_intent"]["verified_commands"] == pending["requested_intent"]["required_commands"]
    assert not completed["workflow_held"]
    assert logs == []


@pytest.mark.parametrize("guidance,expected", [
    ({"kind": "set", "text": "  Preserve café compatibility  "}, "Preserve café compatibility"),
    ({"kind": "clear"}, ""),
])
def test_guidance_variants_become_durable_episode_input(api, guidance, expected):
    client, app, backend, environment, body, factory_calls = api
    response = client.post(PATH + "/commands", json={**body, "guidance": guidance})
    assert response.status_code == 202
    assert response.json()["control"]["guidance"] == expected
    assert client.get(PATH).json()["guidance"] == expected


@pytest.mark.parametrize("body", [None, [], "not a command"])
def test_missing_or_untyped_command_body_is_rejected_before_authority(api, body):
    client, app, backend, environment, request, factory_calls = api
    response = client.post(PATH + "/commands", content=json.dumps(body), headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert factory_calls == []


@pytest.mark.parametrize("request_id", ["", " ", "\x00", "line\nfeed", "x" * 513])
def test_malformed_get_request_identity_is_422_before_authority(api, request_id):
    client, app, backend, environment, body, factory_calls = api
    before = authority_dump(environment[1])
    response = client.get(PATH, params={"request_id": request_id})
    assert response.status_code == 422
    assert factory_calls == []
    assert authority_dump(environment[1]) == before


def test_get_handler_refuses_unencodable_identifier_if_supplied_in_process(api):
    client, app, backend, environment, body, factory_calls = api
    # HTTP query decoders replace invalid UTF-8; exercise the handler directly
    # for a Python surrogate that cannot be represented by an HTTP client.
    endpoint = next(route.endpoint for route in app.routes
                    if getattr(route, "path", None) == "/api/repos/{name}/issues/{number}/resolve-control")
    with pytest.raises(HTTPException) as rejected:
        asyncio.run(endpoint("demo", 1, "\ud800"))
    assert rejected.value.status_code == 422
    assert factory_calls == []


@pytest.mark.parametrize("corrupt", [False, True])
def test_get_due_or_corrupt_pending_observation_never_runs_recovery_or_updates_schedule(api, monkeypatch, corrupt):
    client, app, backend, environment, body, factory_calls = api
    service, store, gateway, repo, clock, logs, command = environment
    response = client.post(PATH + "/commands", json=body)
    assert response.status_code == 202
    clock[0] += timedelta(days=1)
    if corrupt:
        with store.transaction() as db:
            db.execute("UPDATE dedupe_keys SET payload_json='{}' WHERE namespace='workflow-rearm-observation'")
    before = authority_dump(store)
    git_before = tuple(repo.runner.calls)
    monkeypatch.setattr(gateway, "get_issue", lambda *a: pytest.fail("control GET tried fresh remote observation"))
    monkeypatch.setattr(repo.worktrees, "prepare_episode_workspace", lambda *a, **kw: pytest.fail("control GET tried workspace recovery"))
    first = client.get(PATH)
    second = client.get(PATH)
    assert first.status_code == (503 if corrupt else 200)
    assert second.json() == first.json()
    assert authority_dump(store) == before
    assert tuple(repo.runner.calls) == git_before
    assert gateway.calls == []


@pytest.mark.parametrize("field", ["issue", "epoch", "generation", "version"])
def test_expected_counter_above_json_safe_integer_refused_before_factory(api, field):
    client, app, backend, environment, body, factory_calls = api
    wrong = deepcopy(body)
    wrong["expected"][field] = 2**53
    before = authority_dump(environment[1])
    response = client.post(PATH + "/commands", json=wrong)
    assert response.status_code == 422
    assert factory_calls == []
    assert authority_dump(environment[1]) == before


@pytest.mark.parametrize("failure", ["unavailable \ud800", "unavailable\x00detail", "\ud800\x00"])
@pytest.mark.parametrize("method", ["get", "post"])
def test_unencodable_service_failure_is_typed_unavailable_not_http_500(api, monkeypatch, failure, method):
    client, app, backend, environment, body, factory_calls = api
    def unavailable(*args, **kwargs):
        raise ValueError(failure)
    monkeypatch.setattr(backend, "resolve_control" if method == "get" else "resolve_command", unavailable)
    before = authority_dump(environment[1])
    response = client.get(PATH) if method == "get" else client.post(PATH + "/commands", json=body)
    assert response.status_code == 503
    result = response.json()
    assert result["status" if method == "get" else "outcome"] == "unavailable"
    if method == "post":
        assert result["control"]["status"] == "unavailable"
    assert "\x00" not in result["reason"]
    result["reason"].encode("utf-8")
    assert factory_calls == []
    assert authority_dump(environment[1]) == before


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "localhost", "127.0.0.2", "hostile.test"])
def test_dashboard_runner_refuses_nonliteral_supported_loopback_without_listener(host, monkeypatch):
    monkeypatch.setattr(server, "create_app", lambda *a, **kw: pytest.fail("unsafe listener constructed application"))
    with pytest.raises(ValueError, match="literal loopback"):
        server.serve(8321, host=host)
