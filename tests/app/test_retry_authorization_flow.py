"""Operator retry grant composed through the loopback API and the daemon drain.

Fault-matrix rows (docs/open/2026-09-14-phase-3-gate-design.md):
- expiry -> verified absence -> authorized retry -> restart before/after
  token consumption;
- ordinary completion command -> expiry -> current absence -> authorized retry.
Both run against real SQLite authority and a persistent GitHub double that
survives process death.
"""

from contextlib import closing
from datetime import timedelta
import multiprocessing
import os
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from asgi_client import ASGITestClient
from issue_agent.adapters.agent_publish import AgentPublicationBroker
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.effects import GitHubEffectExecutor
from issue_agent.adapters.workflow_control import SQLiteWorkflowControl
from issue_agent.app.dashweb import server
from issue_agent.app.dashweb.service import Backend
from issue_agent.app.roles import EffectRecovery
from issue_agent.app.workflows.resolve_rearm import ResolveRearmWorkflow
from issue_agent.domain.contracts import EffectStatus
from issue_agent.domain.labels import Label
from issue_agent.domain.world import IssueView
from tests.adapters.test_retry_authorization import _expire
from tests.adapters.test_return_command_recovery import finalized
from tests.adapters.test_workflow_control import NOW, WRITER
from tests.fakes.persistent_github import PersistentGitHub

BASE = "http://127.0.0.1:8321"
PATH = "/api/repos/demo/issues/1/resolve-control"
HEADERS = {"X-Issue-Agent-Dashboard": "1"}


class NoWorkspace:
    def __getattr__(self, name):
        raise AssertionError(f"retry authorization touched Git: {name}")


def _recovery(store, remote, clock, logs):
    """The fix daemon's effect recovery, rebuilt as a restarted process would."""
    effects = GitHubEffectExecutor(remote, store, repository="o/r", identity=WRITER.owner_id, log=logs.append)
    effects.effects.now = lambda: clock[0]
    return EffectRecovery(effects, AgentPublicationBroker(
        store, repository="o/r", base_branch="dev", identity=f"{WRITER.owner_id}:publication",
        owner=WRITER.owner_id,
    ))


@pytest.fixture
def world(tmp_path, monkeypatch):
    clock = [NOW]
    store = SQLiteControlStore(str(tmp_path / "control.sqlite"), now=lambda: clock[0])
    store.initialize()
    snapshot, command = finalized(store)
    remote = PersistentGitHub(tmp_path / "remote.sqlite")
    remote.seed(IssueView(1, title="title", body="body", author="alice", updated_at="revision:1",
                          labels=frozenset({Label.AI_WORKING})))
    # The completion's first write outlived its reconciliation ceiling.
    _expire(store, snapshot, command)
    logs = []

    def factory(name, **kwargs):
        assert name == "demo" and kwargs["control_db_path"] == store.path
        control = SQLiteWorkflowControl(SQLiteControlStore(store.path, now=lambda: clock[0]), WRITER, resolve_ceiling=3)
        return ResolveRearmWorkflow(control, remote, NoWorkspace(), lambda: clock[0], logs.append)

    backend = Backend(harness_root=str(tmp_path), control_db_path=store.path,
                      resolve_factory=factory, now_fn=lambda: clock[0])
    monkeypatch.setattr(backend, "names", lambda: ("demo",))
    monkeypatch.setattr(backend, "_operator_effects", lambda *a, **kw: pytest.fail("authorization wrote remotely"))
    client = ASGITestClient(server.create_app(backend=backend, harness_root=str(tmp_path), offload_backend=False),
                            base_url=BASE, headers=HEADERS)
    return store, remote, clock, logs, client, command


def _gate(state):
    return next(gate for gate in state["gates"] if gate["gate_id"] == "uncertain")


def _prove_absence(world):
    store, remote, clock, logs, client, command = world
    assert _recovery(store, remote, clock, logs).drain_pending_commands() == 0
    assert remote.calls == []
    state = client.get(PATH).json()
    gate = _gate(state)
    assert gate["cause"]["kind"] == "retry_authorization_required"
    assert gate["available_commands"] == ["authorize_retry"]
    return state, gate


def _body(state, gate, authorization_id="grant-1"):
    return {"operation": "authorize_retry", "expected": state["expected"], "authorization_id": authorization_id,
            "gate_id": gate["gate_id"], "command_id": gate["cause"]["command_id"],
            "evidence_id": gate["cause"]["absence_evidence_id"]}


def _no_new_allowance(store, before, after):
    assert after["expected"]["generation"] == before["expected"]["generation"]
    assert after["expected"]["episode_id"] == before["expected"]["episode_id"]
    assert after["failures"] == before["failures"] and after["current_intent"] is None
    with closing(store._connect()) as db:
        assert db.execute("SELECT count(*) FROM workflow_rearms").fetchone()[0] == 0


def _die_before_consumption(control_path, remote_path, now):
    store = SQLiteControlStore(control_path, now=lambda: now)
    store.begin_authorized_retry = lambda *a, **kw: os._exit(72)
    _recovery(store, PersistentGitHub(Path(remote_path)), [now], []).drain_pending_commands()


def _die_after_consumption(control_path, remote_path, now, fault):
    store = SQLiteControlStore(control_path, now=lambda: now)

    def die(point):
        if point == fault:
            os._exit(71)

    _recovery(store, PersistentGitHub(Path(remote_path), fault=die), [now], []).drain_pending_commands()


def _run(target, *args):
    process = multiprocessing.get_context("spawn").Process(target=target, args=args)
    process.start()
    try:
        process.join(timeout=30)
        return process.exitcode
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)


def test_completion_command_retry_survives_restart_before_consumption_and_writes_once(world):
    store, remote, clock, logs, client, command = world
    before, gate = _prove_absence(world)
    body = _body(before, gate)
    accepted = client.post(PATH + "/commands", json=body)
    assert accepted.status_code == 202 and accepted.json()["outcome"] == "accepted"
    after = accepted.json()["control"]
    assert _gate(after)["available_commands"] == [] and _gate(after)["cause"]["kind"] == "retry_authorization_required"
    # The open grant is visible as pending, not as an action-less gate.
    assert _gate(after)["pending_retry"] == {"authorization_id": body["authorization_id"], "command_id": body["command_id"]}
    assert _gate(before)["pending_retry"] is None
    _no_new_allowance(store, before, after)
    assert client.post(PATH + "/commands", json=body).status_code == 200
    second = client.post(PATH + "/commands", json=_body(after, gate, "grant-2"))
    assert second.status_code == 409 and second.json()["outcome"] == "conflict"
    assert remote.calls == []
    clock[0] += timedelta(seconds=10)
    assert _run(_die_before_consumption, store.path, str(remote.path), clock[0]) == 72
    assert remote.calls == []
    with closing(store._connect()) as db:
        assert db.execute("SELECT consumed_attempt FROM workflow_retry_authorizations").fetchone()[0] is None
    restarted = SQLiteControlStore(store.path, now=lambda: clock[0])
    # The killed visit spent the grant's one schedule bypass; the grant itself
    # survives and is spent at the next scheduled read.
    assert _recovery(restarted, PersistentGitHub(remote.path), clock, logs).drain_pending_commands() == 0
    assert remote.calls == []
    with closing(store._connect()) as db:
        assert db.execute("SELECT disposition FROM workflow_retry_authorizations").fetchone()[0] is None
    clock[0] += timedelta(minutes=31)
    assert _recovery(restarted, PersistentGitHub(remote.path), clock, logs).drain_pending_commands() == 1
    assert [call[0] for call in remote.calls] == ["relabel"]
    assert remote.get_issue(1).labels == frozenset({Label.AI_READY})
    assert restarted.effect_receipt(command.command_id).status is EffectStatus.SUCCEEDED
    final = client.get(PATH).json()
    assert all(item["gate_id"] != "uncertain" for item in final["gates"])
    _no_new_allowance(store, before, final)
    for _ in range(3):
        clock[0] += timedelta(hours=1)
        _recovery(restarted, PersistentGitHub(remote.path), clock, logs).drain_pending_commands()
    assert client.post(PATH + "/commands", json=body).status_code == 200
    assert len(remote.calls) == 1 and logs == []


def test_death_after_consumption_reconciles_read_only_and_needs_a_fresh_grant(world):
    store, remote, clock, logs, client, command = world
    before, gate = _prove_absence(world)
    body = _body(before, gate)
    assert client.post(PATH + "/commands", json=body).status_code == 202
    original = store.command_reconciliation(command.command_id)
    clock[0] += timedelta(seconds=10)
    # The spent attempt added one label, then the process died mid-write.
    assert _run(_die_after_consumption, store.path, str(remote.path), clock[0], "label_added") == 71
    assert [call[0] for call in remote.calls] == ["relabel"]
    with closing(store._connect()) as db:
        assert db.execute("SELECT consumed_attempt FROM workflow_retry_authorizations").fetchone()[0] == 2
    restarted = SQLiteControlStore(store.path, now=lambda: clock[0])
    for _ in range(3):
        clock[0] += timedelta(hours=1)
        _recovery(restarted, PersistentGitHub(remote.path), clock, logs).drain_pending_commands()
    assert len(remote.calls) == 1  # Replay of the spent grant never buys another write.
    later = restarted.command_reconciliation(command.command_id)
    assert later.history.unresolved_since == original.history.unresolved_since
    state = client.get(PATH).json()
    fresh = _gate(state)
    assert fresh["cause"]["absence_evidence_id"] != gate["cause"]["absence_evidence_id"]
    assert fresh["available_commands"] == ["authorize_retry"]
    assert client.post(PATH + "/commands", json=body).status_code == 200
    spent = client.post(PATH + "/commands", json={**_body(state, gate, "grant-2")})
    assert spent.status_code == 422
    # The partial write is this command's own effect: the original decision
    # still applies (ADR 0015 semantic fence), so the fresh grant repairs it
    # with exactly one more write instead of retiring the completion.
    assert client.post(PATH + "/commands", json=_body(state, fresh, "grant-2")).status_code == 202
    clock[0] += timedelta(seconds=10)
    _recovery(restarted, PersistentGitHub(remote.path), clock, logs).drain_pending_commands()
    assert len(remote.calls) == 2
    assert remote.get_issue(1).labels == frozenset({Label.AI_READY})
    assert restarted.effect_receipt(command.command_id).status is EffectStatus.SUCCEEDED
    final = client.get(PATH).json()
    assert all(item["gate_id"] != "uncertain" for item in final["gates"])
    _no_new_allowance(store, before, final)


def test_death_after_remote_success_settles_without_spending_a_second_write(world):
    store, remote, clock, logs, client, command = world
    before, gate = _prove_absence(world)
    assert client.post(PATH + "/commands", json=_body(before, gate)).status_code == 202
    clock[0] += timedelta(seconds=10)
    assert _run(_die_after_consumption, store.path, str(remote.path), clock[0], "relabel_applied") == 71
    restarted = SQLiteControlStore(store.path, now=lambda: clock[0])
    clock[0] += timedelta(hours=1)
    assert _recovery(restarted, PersistentGitHub(remote.path), clock, logs).drain_pending_commands() == 1
    assert len(remote.calls) == 1
    assert restarted.effect_receipt(command.command_id).status is EffectStatus.SUCCEEDED
    assert all(item["gate_id"] != "uncertain" for item in client.get(PATH).json()["gates"])


@pytest.mark.parametrize("mutate, status", [
    (lambda body: body.update(unexpected="field"), 422),
    (lambda body: body.update(authorization_id=""), 422),
    (lambda body: body.update(authorization_id="x" * 513), 422),
    (lambda body: body.update(evidence_id="bad\x00id"), 422),
    (lambda body: body.pop("evidence_id"), 422),
    (lambda body: body.update(command_id=7), 422),
    (lambda body: body["expected"].update(version=-1), 422),
    (lambda body: body.update(evidence_id="fabricated-proof"), 422),
    (lambda body: body.update(gate_id="another-gate"), 422),
    (lambda body: body["expected"].update(version=body["expected"]["version"] + 5), 409),
    (lambda body: body["expected"].update(episode_id="other-episode"), 409),
])
def test_wire_contract_and_status_map(world, mutate, status):
    store, remote, clock, logs, client, command = world
    state, gate = _prove_absence(world)
    body = _body(state, gate)
    mutate(body)
    response = client.post(PATH + "/commands", json=body)
    assert response.status_code == status
    with closing(store._connect()) as db:
        assert db.execute("SELECT count(*) FROM workflow_retry_authorizations").fetchone()[0] == 0
    assert remote.calls == []
