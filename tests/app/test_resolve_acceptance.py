"""ADR 0015 resolve fault matrix through the composed fix daemon.

Every case runs the production-shaped composition from
``resolve_acceptance_harness`` (daemon + executor + effect recovery with the
re-arm reconciliation bound, SQLite control store, real Git episode workspace,
persistent GitHub double, dashboard service layer) and then restarts it over
the same databases. Assertions read public outcomes: remote labels/comments
and harness write calls, receipts, charges, ``load_workflow`` and the
dashboard projection.
"""

from datetime import timedelta
import os
import sqlite3

import pytest

from issue_agent.adapters.github import GhUnavailable
from issue_agent.domain.contracts import CommandId, EffectStatus
from issue_agent.domain.labels import Label
from issue_agent.domain.workflow import (
    CompletedRearm, PendingRearm, ReconciliationPolicy, ResolveCeiling, SupersededRearm,
)
from tests.app.resolve_acceptance_harness import (
    BODY, ISSUE, REPO as REPO_NAME, TITLE, child_environment, compose, create_deployment, fail_at, install_clock, isolate_authority,
    patch_publication_environment, run_until_exit, seed_exhausted, ScriptedProvider,
)

NHR = Label.NEEDS_HUMAN_REVIEW.value
READY = Label.AI_READY.value
BODY_EDIT = "Original requirements\n\n(typo fixed)"


@pytest.fixture
def bare(tmp_path, monkeypatch):
    patch_publication_environment(monkeypatch)
    deployment = create_deployment(tmp_path)
    isolate_authority(deployment, monkeypatch)
    install_clock(deployment, monkeypatch)
    return deployment


@pytest.fixture
def deployment(bare):
    seed_exhausted(bare)
    return bare


def advance(deployment, minutes: float = 1) -> None:
    deployment.clock.advance(timedelta(minutes=minutes))


def progress(composition, ticks: int = 3, minutes: float = 1) -> None:
    for _ in range(ticks):
        advance(composition.deployment, minutes)
        composition.tick()


def progress_until(composition, done, *, limit: int = 12, minutes: float = 1) -> None:
    """Tick on the deployment clock until `done(composition)`; bounded, never a spin."""
    for _ in range(limit):
        if done(composition):
            return
        advance(composition.deployment, minutes)
        composition.tick()
    assert done(composition), f"condition not reached within {limit} ticks"


def rearm_completed(composition) -> bool:
    return isinstance(composition.state().intent, CompletedRearm)


def projections(composition) -> list:
    """Harness writes that release or preserve the ceiling-owned hold."""
    return [call for call in composition.remote.calls
            if call[0] == "relabel" and (call[2] == NHR or call[3] == NHR) and call[3] != Label.AI_WORKING.value]


def required(control: dict) -> str:
    return control["current_intent"]["required_commands"][0]


def receipt(composition, command_id: str):
    return composition.control.effect_receipt(CommandId(command_id))


def ceiling_gates(state) -> list:
    return [gate for gate in state.gates
            if isinstance(getattr(gate, "gate", gate).cause, ResolveCeiling)]


# -- Row 1: failure before local commit / after commit before response --------

def test_rearm_failure_before_local_commit_changes_nothing_and_replay_grants_once(deployment):
    client = compose(deployment)
    client.boot()
    before = client.read()
    body = client.request_body("operator-request")
    with sqlite3.connect(client.control.path) as db:
        db.execute("CREATE TRIGGER injected_commit_failure BEFORE INSERT ON workflow_rearms "
                   "BEGIN SELECT RAISE(ABORT, 'injected re-arm commit failure'); END")
    refused = client.post(body)
    assert refused["outcome"] == "unavailable" and "injected re-arm commit failure" in refused["reason"]
    assert client.read() == before
    assert client.control.pending_commands() == ()
    progress(client, 2)
    assert client.read() == before
    assert client.remote.calls == [] and client.provider.tasks == []
    with sqlite3.connect(client.control.path) as db:
        db.execute("DROP TRIGGER injected_commit_failure")

    restarted = compose(deployment)
    restarted.boot()
    accepted = restarted.post(body)
    assert accepted["outcome"] == "accepted"
    assert accepted["control"]["expected"]["generation"] == before["expected"]["generation"] + 1
    progress(restarted, 2)
    assert isinstance(restarted.state().intent, CompletedRearm)
    assert len(projections(restarted)) == 1


def _die_after_commit_before_response(deployment, request_id: str) -> None:
    child_environment(deployment)
    client = compose(deployment)
    body = client.request_body(request_id)
    from issue_agent.app.dashweb import resolve_control

    resolve_control.read_control = lambda *args, **kwargs: os._exit(81)
    client.post(body)


def test_rearm_committed_before_lost_response_replays_same_identity_without_second_grant(deployment):
    before = compose(deployment).read()
    run_until_exit(_die_after_commit_before_response, deployment, "operator-request", code=81)

    client = compose(deployment)
    client.boot()
    committed = client.read("operator-request")
    intent = committed["requested_intent"]
    assert intent["state"] == "pending"
    assert committed["expected"]["generation"] == before["expected"]["generation"] + 1
    body = {"operation": "request_rearm", "expected": before["expected"], "request_id": "operator-request",
            "gate_id": before["gates"][0]["gate_id"], "guidance": {"kind": "keep"}}

    # Replay against the running daemon before and after it applies the projection.
    replay = client.post(body)
    assert replay["outcome"] == "replayed"
    assert replay["control"]["requested_intent"]["generation"] == intent["generation"]
    assert replay["control"]["requested_intent"]["required_commands"] == intent["required_commands"]
    progress(client, 1)
    assert client.post(body)["outcome"] == "replayed"
    progress(client, 2)
    assert isinstance(client.state().intent, CompletedRearm)

    restarted = compose(deployment)
    restarted.boot()
    final = restarted.post(body)
    assert final["outcome"] == "replayed"
    assert final["control"]["requested_intent"]["state"] == "completed"
    assert final["control"]["requested_intent"]["generation"] == intent["generation"]
    assert final["control"]["expected"]["generation"] == intent["generation"]
    assert final["control"]["requested_intent"]["verified_commands"] == intent["required_commands"]
    assert receipt(restarted, intent["required_commands"][0]).status is EffectStatus.SUCCEEDED
    assert len(projections(restarted)) == 1
    assert NHR not in restarted.labels()
    assert restarted.state().episode.episode_id == intent["episode_id"]


# -- Row 2: duplicate, competing and reused request identities ----------------

def test_same_generation_requests_renew_once_across_drain_and_restart(deployment):
    client = compose(deployment)
    client.boot()
    before = client.read()
    body = client.request_body("first-request", control=before)
    assert client.post(body)["outcome"] == "accepted"
    assert client.post(body)["outcome"] == "replayed"
    competing = {**body, "request_id": "second-request"}
    assert client.post(competing)["outcome"] == "conflict"
    changed = {**body, "guidance": {"kind": "set", "text": "Different guidance"}}
    assert client.post(changed)["outcome"] == "conflict"

    progress(client, 1)  # a daemon drain between duplicates
    assert client.post(body)["outcome"] == "replayed"
    assert client.post(competing)["outcome"] == "conflict"

    restarted = compose(deployment)
    restarted.boot()
    assert restarted.post(body)["outcome"] == "replayed"
    assert restarted.post(changed)["outcome"] == "conflict"
    # A fresh identity against the *current* revision cannot buy a second grant
    # while the first request still owns the workflow.
    fresh_id = restarted.request_body("third-request", control=restarted.read())
    assert restarted.post(fresh_id)["outcome"] in ("conflict", "rejected")
    progress(restarted, 2)
    state = restarted.state()
    assert isinstance(state.intent, CompletedRearm)
    assert state.intent.request_id == "first-request"
    assert state.revision.fence.generation == before["expected"]["generation"] + 1
    assert len(projections(restarted)) == 1
    assert restarted.post(competing)["outcome"] in ("conflict", "rejected")
    assert restarted.state().revision.fence.generation == state.revision.fence.generation


# -- Row 4: failure at each remote mutation / receipt boundary ----------------

def _accept(deployment, request_id: str = "operator-request") -> dict:
    client = compose(deployment)
    client.boot()
    accepted = client.request_rearm(request_id)
    assert accepted["outcome"] == "accepted"
    return accepted["control"]


def _die_at_projection_boundary(deployment, boundary: str) -> None:
    child_environment(deployment)
    fault = (lambda point: os._exit(82) if point == "label_added" else None) if boundary == "label_added" else (lambda _: None)
    client = compose(deployment, fault=fault)
    if boundary == "after_begin":
        begin = client.control.begin_command

        def begun_then_lost(command_id):
            begin(command_id)
            os._exit(82)
        client.control.begin_command = begun_then_lost
    elif boundary == "before_receipt":
        client.control.record_effect = lambda _receipt: os._exit(82)
    client.tick()



BOUNDARY_WRITES = {  # boundary: (writes before restart, writes after recovery)
    "after_begin": (0, 1), "label_added": (1, 2), "lost_reply": (1, 1),
    "before_receipt": (1, 1), "local_commit": (0, 1),
}


@pytest.mark.parametrize("boundary", [
    "after_begin",
    "label_added",
    "lost_reply", "before_receipt", "local_commit",
])
def test_projection_boundary_failure_reconciles_same_command_without_duplicate(deployment, boundary):
    control = _accept(deployment)
    command_id = required(control)
    advance(deployment)
    if boundary in ("after_begin", "label_added", "before_receipt"):
        run_until_exit(_die_at_projection_boundary, deployment, boundary, code=82)
    elif boundary == "lost_reply":
        client = compose(deployment, fault=fail_at("relabel_applied", GhUnavailable("network", "reply lost")))
        client.tick()
    else:
        client = compose(deployment)
        with sqlite3.connect(client.control.path) as db:
            db.execute("CREATE TRIGGER injected_begin_failure BEFORE UPDATE ON lifecycle_commands "
                       "BEGIN SELECT RAISE(ABORT, 'injected command commit failure'); END")
        client.tick()
        assert client.remote.calls == []
        with sqlite3.connect(client.control.path) as db:
            db.execute("DROP TRIGGER injected_begin_failure")

    observer = compose(deployment)
    held = observer.read()
    assert held["workflow_held"] is True
    assert held["current_intent"]["state"] == "pending"
    assert held["current_intent"]["required_commands"] == [command_id]
    assert observer.provider.tasks == []
    # Exact remote writes before the restart: none unless the call reached GitHub.
    assert len(projections(observer)) == BOUNDARY_WRITES[boundary][0]

    restarted = compose(deployment)
    restarted.boot()
    progress(restarted, 3)
    state = restarted.state()
    assert isinstance(state.intent, CompletedRearm)
    assert state.intent.verified_commands == (CommandId(command_id),)
    assert receipt(restarted, command_id).status is EffectStatus.SUCCEEDED
    writes = projections(restarted)
    # Only a half-applied relabel needs one completing call of the same
    # command; every other boundary is verified present or written once.
    assert len(writes) == BOUNDARY_WRITES[boundary][1]
    assert set(map(tuple, writes)) == {("relabel", ISSUE, NHR, READY)}
    assert NHR not in restarted.labels()


# -- Row 5: add-trigger succeeds, remove-hold fails ----------------------------

def test_partial_projection_keeps_scheduler_held_and_dashboard_pending_across_restart(deployment):
    control = _accept(deployment)
    command_id = required(control)
    removal_fails = fail_at("label_added", GhUnavailable("network", "remove-hold failed"), times=10**6)
    client = compose(deployment, fault=removal_fails)
    progress(client, 2)
    assert client.labels() >= {READY, NHR}
    assert client.provider.tasks == []
    before = client.read()
    assert before["workflow_held"] is True
    assert before["current_intent"]["state"] == "pending"
    assert before["current_intent"]["required_commands"] == [command_id]

    restarted = compose(deployment, fault=removal_fails)
    restarted.boot()
    progress(restarted, 3)
    after = restarted.read()
    assert restarted.provider.tasks == []
    assert restarted.labels() >= {READY, NHR}
    assert after["workflow_held"] is True
    assert after["gates"] == before["gates"]
    assert after["current_intent"] == before["current_intent"]

    healed = compose(deployment)
    healed.boot()
    # Repeated half-landed attempts back off exponentially; the next scheduled
    # read after healing completes the same command.
    progress_until(healed, rearm_completed, limit=6, minutes=5)
    assert receipt(healed, command_id).status is EffectStatus.SUCCEEDED
    progress(healed, 1)
    assert len(healed.provider.tasks) >= 1


# -- Row 6: human hold / terminal issue / new linked PR during re-arm ----------

def _intervene(remote, intervention: str) -> None:
    from tests.domain.builders import pr

    if intervention == "human_hold":
        remote.external_change(ISSUE, add=(Label.AI_PARKED.value,))
    elif intervention == "terminal":
        remote.external_change(ISSUE, state="closed", state_reason="not_planned")
    else:
        remote.seed_pr(pr(70, author="human", head_ref_name="feature/manual", closing_issue=ISSUE,
                          head_ref_oid="manual-head"))
        remote.link_pr(ISSUE, 70)


@pytest.mark.parametrize("intervention", ["human_hold", "terminal", "linked_pr"])
def test_intervention_during_rearm_supersedes_without_blind_promotion(deployment, intervention):
    control = _accept(deployment)
    command_id = required(control)
    client = compose(deployment)
    _intervene(client.remote, intervention)
    progress(client, 2)

    def check(composition):
        view = composition.read("operator-request")
        assert view["requested_intent"]["state"] == "superseded"
        assert view["requested_intent"]["retired_commands"] == [command_id]
        assert view["workflow_held"] is True
        issue = composition.remote.get_issue(ISSUE)
        assert Label.AI_READY not in issue.labels
        assert not any(call[0] in ("close", "comment") for call in composition.remote.calls)
        assert projections(composition) == []
        assert composition.provider.tasks == []
        if intervention == "human_hold":
            assert issue.has(Label.AI_PARKED) and issue.has(Label.NEEDS_HUMAN_REVIEW)
        if intervention == "terminal":
            assert issue.state == "closed" and issue.state_reason == "not_planned"
        receipt_now = receipt(composition, command_id)
        assert receipt_now is None or receipt_now.status is not EffectStatus.SUCCEEDED
        return view

    first = check(client)
    restarted = compose(deployment)
    restarted.boot()
    progress(restarted, 3)
    second = check(restarted)
    assert second["gates"] == first["gates"]
    assert isinstance(restarted.state().intent, SupersededRearm)


@pytest.mark.parametrize("activity", ["comment", "body_edit", "priority_label", "unrelated_pr_head"])
def test_harmless_activity_during_pending_rearm_does_not_supersede(bare, activity):
    from tests.domain.builders import pr

    seed_exhausted(bare)
    if activity == "unrelated_pr_head":
        bare.github().seed_pr(pr(71, author="human", head_ref_name="feature/unrelated", head_ref_oid="head-1"))
    control = _accept(bare)
    command_id = required(control)
    client = compose(bare)
    if activity == "comment":
        client.remote.external_change(ISSUE, comment=("alice", "Is this still planned?"))
    elif activity == "body_edit":
        client.remote.external_change(ISSUE, body=BODY_EDIT)
    elif activity == "priority_label":
        client.remote.external_change(ISSUE, add=(Label.P2.value,))
    else:
        client.remote.external_pr_change(71, head_ref_oid="head-2")
    progress_until(client, rearm_completed, limit=4)
    assert receipt(client, command_id).status is EffectStatus.SUCCEEDED
    assert projections(client) == [["relabel", ISSUE, NHR, READY]]
    assert NHR not in client.labels() and READY in client.labels()


def test_linked_pr_head_change_during_pending_rearm_supersedes(deployment):
    from tests.domain.builders import pr

    control = _accept(deployment)
    command_id = required(control)
    client = compose(deployment)
    client.remote.seed_pr(pr(72, author="human", head_ref_name="feature/manual", closing_issue=ISSUE,
                             head_ref_oid="manual-1"))
    client.remote.link_pr(ISSUE, 72)
    client.remote.external_pr_change(72, head_ref_oid="manual-2")
    progress(client, 2)
    view = client.read("operator-request")
    assert view["requested_intent"]["state"] == "superseded"
    assert view["requested_intent"]["reason"] == "decision_changed"
    assert view["requested_intent"]["retired_commands"] == [command_id]
    assert projections(client) == [] and client.provider.tasks == []


# -- Row 7: independent gates and external holds ------------------------------

def _independent_gate(deployment):
    from issue_agent.domain.workflow import EvidenceUnavailable, NoAuthorizedRemediation, OpenGate

    return OpenGate("independent-evidence-gate", 2, EvidenceUnavailable(
        "independent required fact", deployment.clock.now() - timedelta(hours=2), 3, "independent source unreadable",
    ), NoAuthorizedRemediation("an independent operator decision is required"))


@pytest.mark.parametrize("holds", ["independent_gate_only", "gate_hold_and_unrelated_pr"])
def test_rearm_renews_only_selected_allowance_and_preserves_other_gate_and_holds(bare, holds):
    from tests.domain.builders import pr

    other = _independent_gate(bare)
    labels = (NHR,) if holds == "independent_gate_only" else (NHR, Label.AI_PARKED.value)
    seed_exhausted(bare, *labels, extra_gates=(other,))
    if holds != "independent_gate_only":
        bare.github().seed_pr(pr(ISSUE, author="human", head_ref_name="feature/unrelated", head_ref_oid="unrelated"))
    client = compose(bare)
    client.boot()
    control = client.read()
    assert {g["cause"]["kind"] for g in control["gates"]} == {"resolve_ceiling", "evidence_unavailable"}
    accepted = client.request_rearm("operator-request", control=control)
    assert accepted["outcome"] == "accepted", accepted["reason"]
    progress(client, 3)

    restarted = compose(bare)
    restarted.boot()
    progress(restarted, 2)
    state = restarted.state()
    assert isinstance(state.intent, CompletedRearm)
    assert state.episode.failures == 0
    # The independent gate alone must keep the attention hold: nothing else
    # (no external label, no PR) holds the issue in the first variant.
    assert [getattr(g, "gate", g) for g in state.gates] == [other]
    assert restarted.workflow.held_projection(restarted.key).held
    assert restarted.read()["workflow_held"] is True
    assert set(labels) <= restarted.labels() and READY not in restarted.labels()
    assert restarted.provider.tasks == []
    assert all(call[0] != "relabel_pr" for call in restarted.remote.calls)
    if holds != "independent_gate_only":
        assert restarted.remote.get_pr(ISSUE).labels == frozenset()
    assert all(call[3] != READY for call in restarted.remote.calls if call[0] == "relabel")


def test_dashboard_renders_evidence_unavailable_gates(bare):
    other = _independent_gate(bare)
    seed_exhausted(bare, extra_gates=(other,))
    view = compose(bare).read()
    gate = next(g for g in view["gates"] if g["gate_id"] == other.gate_id)
    assert gate["cause"]["kind"] == "evidence_unavailable"
    assert gate["cause"]["last_error"] == "independent source unreadable"


# -- Row 8: unavailable linkage / remote revision ------------------------------

@pytest.mark.parametrize("point", ["linkage", "read"])
def test_unavailable_request_observation_is_visible_and_never_permission(deployment, point):
    client = compose(deployment)
    client.boot()
    before = client.read()
    body = client.request_body("operator-request", control=before)
    unavailable = fail_at(point, GhUnavailable("network", f"{point} unavailable"), times=10**6)
    client.operator_fault[:] = [unavailable]
    refused = client.post(body)
    assert refused["outcome"] == "unavailable"
    assert client.read() == before
    assert client.control.pending_commands() == ()

    client.operator_fault[:] = []
    assert client.post(body)["outcome"] == "accepted"
    command_id = required(client.read())
    failing = compose(deployment, fault=unavailable)
    progress(failing, 3)
    view = failing.read()
    assert view["current_intent"]["state"] == "pending"
    assert view["observation"]["failed_reads"] >= 1
    assert f"{point} unavailable" in view["observation"]["last_error"]
    assert failing.remote.calls == []
    assert failing.provider.tasks == []

    restarted = compose(deployment, fault=unavailable)
    restarted.boot()
    progress(restarted, 4, minutes=30)
    assert isinstance(restarted.state().intent, PendingRearm)
    assert "EvidenceUnavailable" in restarted.gate_causes()
    rendered = [g["cause"] for g in restarted.read()["gates"] if g["cause"]["kind"] == "evidence_unavailable"]
    assert rendered and f"{point} unavailable" in rendered[0]["last_error"]
    assert restarted.remote.calls == [] and restarted.provider.tasks == []

    healed = compose(deployment)
    healed.boot()
    progress(healed, 3, minutes=30)
    assert isinstance(healed.state().intent, CompletedRearm)
    assert receipt(healed, command_id).status is EffectStatus.SUCCEEDED
    assert "EvidenceUnavailable" not in healed.gate_causes()
    assert healed.read("operator-request")["requested_intent"]["state"] == "completed"


# -- Row 9: repeated unknown outcomes until the age/read ceiling -------------

class LostReplyThenBlindReads:
    """The projection lands remotely, its reply is lost, then reads fail."""

    def __init__(self, *, reads_fail: bool = True):
        self.lost = False
        self.reads = 0
        self.fail_reads = reads_fail

    def __call__(self, point: str) -> None:
        if point == "relabel_applied" and not self.lost:
            self.lost = True
            raise GhUnavailable("network", "projection reply lost")
        if point in ("read", "linkage"):
            self.reads += 1
            if self.lost and self.fail_reads:
                raise GhUnavailable("network", "verification read unavailable")


def _probe_skip(composition) -> None:
    from issue_agent.app.world_probe import WorldUnchanged

    def unchanged():
        raise WorldUnchanged()
    composition.daemon.observe = unchanged


def _unknown_projection(deployment, *, receipt_delay: bool = False):
    control = _accept(deployment)
    command_id = required(control)
    fault = LostReplyThenBlindReads()
    client = compose(deployment, fault=fault)
    _probe_skip(client)
    if receipt_delay:
        import time

        record = client.control.record_effect

        def scheduled_later(receipt_value):
            # Ordinary scheduling/lock-wait latency between the runner's
            # completed_at read and the store's own updated_at read.
            time.sleep(0.005)
            return record(receipt_value)
        client.control.record_effect = scheduled_later
    advance(deployment)
    client.tick()
    assert fault.lost and len(projections(client)) == 1
    return command_id, fault, client



def test_unknown_outcome_enters_the_bounded_schedule_when_it_is_recorded(deployment):
    from issue_agent.domain.workflow import ActiveReconciliation

    command_id, fault, client = _unknown_projection(deployment)
    # Recorded with the outcome itself, before any verification read ran.
    history = compose(deployment).control.command_reconciliation(CommandId(command_id))
    assert isinstance(history, ActiveReconciliation)
    assert history.failed_reads == 0
    assert history.next_read_at - history.unresolved_since == timedelta(seconds=30)
    assert receipt(client, command_id).status is EffectStatus.UNKNOWN_OUTCOME


@pytest.mark.parametrize("tick_minutes, ticks", [(10, 40), (0.5, 160)], ids=["slow-ticks", "fast-ticks"])
def test_repeatedly_lost_requests_reach_the_ceiling_instead_of_re_executing_forever(deployment, tick_minutes, ticks):
    """Unknown -> proven absent -> re-execute -> unknown backs off and ends at a ceiling."""
    control = _accept(deployment)
    command_id = required(control)
    attempts = [0]
    sent_at = []

    def always_lost(point: str) -> None:
        if point == "relabel_requested":
            attempts[0] += 1
            sent_at.append(deployment.clock.now())
            raise GhUnavailable("network", "projection request lost")

    client = compose(deployment, fault=always_lost)
    _probe_skip(client)
    for _ in range(ticks):  # 80 minutes of 30-second ticks, or over six hours of 10-minute ticks
        advance(deployment, minutes=tick_minutes)
        client.tick()
    assert projections(client) == []
    # The first attempt plus at most the policy's re-executions, whatever the tick rate.
    assert 2 <= attempts[0] <= 1 + ReconciliationPolicy().maximum_reexecutions, (
        f"{attempts[0]} write attempts: re-execution is not bounded")
    gaps = [(b - a).total_seconds() for a, b in zip(sent_at, sent_at[1:])]
    if tick_minutes < 1:
        # Fast ticks expose the schedule itself: each re-execution waits at
        # least twice as long as the one before.
        assert all(later >= 2 * earlier - 1 for earlier, later in zip(gaps, gaps[1:])), gaps
    view = client.read()
    gate = _retry_gate(view)
    assert gate is not None and gate["cause"]["command_id"] == command_id
    assert isinstance(client.state().intent, PendingRearm)
    assert client.provider.tasks == []


def test_repeated_unknown_outcome_is_bounded_and_opens_one_uncertainty_gate(deployment):
    command_id, fault, client = _unknown_projection(deployment)
    fault.reads = 0
    for _ in range(60):  # ten minutes of ten-second ticks
        advance(deployment, minutes=1 / 6)
        client.tick()
    assert fault.reads <= 16, f"{fault.reads} reads in 60 ticks: reconciliation is polling, not scheduled"
    for _ in range(8):
        advance(deployment, minutes=10)
        client.tick()
    uncertain = [getattr(g, "gate", g) for g in client.state().gates
                 if type(getattr(g, "gate", g).cause).__name__ == "RemoteOutcomeUncertain"]
    assert len(uncertain) == 1 and uncertain[0].cause.command_id == command_id


def test_unknown_outcome_never_blindly_reexecutes_and_later_evidence_resolves_same_command(deployment):
    command_id, fault, client = _unknown_projection(deployment)
    for _ in range(8):
        advance(deployment, minutes=10)
        client.tick()
    assert len(projections(client)) == 1
    pending = {str(p.command.command_id): p for p in client.control.pending_commands()}
    assert pending[command_id].refusals == 0
    assert isinstance(client.state().intent, PendingRearm)
    assert client.provider.tasks == []

    restarted = compose(deployment)
    restarted.boot()
    # Reads follow the persisted reconciliation-only schedule, not every tick.
    progress_until(restarted, rearm_completed, limit=12, minutes=10)
    assert receipt(restarted, command_id).status is EffectStatus.SUCCEEDED
    state = restarted.state()
    assert isinstance(state.intent, CompletedRearm)
    assert state.intent.verified_commands == (CommandId(command_id),)
    assert not any(type(getattr(g, "gate", g).cause).__name__ == "RemoteOutcomeUncertain" for g in state.gates)
    assert len(projections(restarted)) == 1


# -- Row 10: overlap, paid cap, probe skip, observation error ----------------

@pytest.mark.parametrize("condition", ["paid_cap", "probe_skip", "observation_error"])
def test_intent_progress_is_independent_of_paid_admission_and_world_observation(deployment, condition):
    control = _accept(deployment)
    command_id = required(control)
    client = compose(deployment)
    if condition == "paid_cap":
        client.daemon.tick_gate = lambda: "daily spend cap reached"
    elif condition == "probe_skip":
        _probe_skip(client)
    else:
        def unavailable():
            raise GhUnavailable("network", "world build unavailable")
        client.daemon.observe = unavailable
    progress(client, 3)
    state = client.state()
    assert isinstance(state.intent, CompletedRearm)
    assert receipt(client, command_id).status is EffectStatus.SUCCEEDED
    assert client.provider.tasks == []
    assert client.labels() == {READY}


def test_exclusive_acceptance_waits_for_in_flight_shared_effect_without_deadlock(bare):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from issue_agent.domain.actions import PostComment, Target
    seed_exhausted(bare)
    entered, release = threading.Event(), threading.Event()

    def hold_comment(point: str) -> None:
        if point == "comment_applied" and not release.is_set():
            entered.set()
            assert release.wait(timeout=30), "effect was never released"

    daemon = compose(bare, fault=hold_comment)
    action = PostComment(Target.ISSUE, ISSUE, "Operator evidence.", workflow=daemon.state().binding)
    daemon.daemon.decide = lambda _world: [action]
    operator = compose(bare)
    body = operator.request_body("overlap-request")
    from concurrent.futures import TimeoutError as FutureTimeout

    with ThreadPoolExecutor(max_workers=2) as pool:
        effect = pool.submit(daemon.tick)
        assert entered.wait(timeout=30)
        accepted = pool.submit(operator.post, body)
        # Acceptance's exclusive barrier waits for the shared in-flight effect.
        with pytest.raises(FutureTimeout):
            accepted.result(timeout=2)
        assert operator.read()["current_intent"] is None
        release.set()
        effect.result(timeout=60)
        result = accepted.result(timeout=60)
    assert result["outcome"] == "accepted"
    assert result["control"]["current_intent"]["state"] == "pending"
    assert [call[0] for call in daemon.remote.calls].count("comment") == 1


# -- Row 3: old completion before/after acceptance with the exhausting run ----

@pytest.mark.parametrize("timing", ["before_acceptance", "pending_during_request"])
def test_old_generation_return_settles_once_and_never_charges_new_episode(bare, timing):
    from tests.app.resolve_acceptance_harness import seed_episode

    ceiling = bare.config().max_resolve_retries
    seed_episode(bare, ceiling - 1, READY)
    paid = [False]
    provider = ScriptedProvider(during=lambda _task: paid.__setitem__(0, True))

    def post_run_unavailable(point: str) -> None:
        if timing == "pending_during_request" and paid[0] and point == "read":
            raise GhUnavailable("network", "post-run observation unavailable")

    client = compose(bare, fault=post_run_unavailable, provider=provider)
    client.boot()
    client.tick()
    assert len(provider.tasks) == 1
    assert len(client.charges()) == 1

    if timing == "pending_during_request":
        # The exhausting run is settled but its return is not finalized: the
        # ceiling is not yet claimable and no re-arm can be requested.
        pending = client.state()
        assert pending.episode.failures == ceiling - 1 and pending.gates and not ceiling_gates(pending)
        # The dashboard renders the evidence hold; selecting it for a re-arm
        # is refused because only the resolve ceiling is renewable.
        control = client.read()
        assert [g["cause"]["kind"] for g in control["gates"]] == ["evidence_unavailable"]
        refused = client.request_rearm("early-request", control=control, cause="evidence_unavailable")
        assert refused["outcome"] not in ("accepted", "replayed"), refused
        assert "paid return" in refused["reason"]
        assert client.state() == pending
        assert client.remote.calls.count(["relabel", ISSUE, NHR, READY]) == 0

    restarted = compose(bare)
    restarted.boot()
    progress(restarted, 2)
    assert restarted.workflow.pending_returns() == ()
    exhausted = restarted.state()
    assert exhausted.episode.failures == ceiling and ceiling_gates(exhausted)
    assert len(restarted.charges()) == 1
    assert NHR in restarted.labels()
    assert restarted.provider.tasks == []
    accepted = restarted.request_rearm("operator-request")
    assert accepted["outcome"] == "accepted"
    renewed = restarted.state()
    assert renewed.episode.failures == 0
    assert renewed.revision.fence.generation == exhausted.revision.fence.generation + 1

    fresh = compose(bare, provider=ScriptedProvider())
    fresh.boot()
    advance(bare)
    fresh.tick()
    advance(bare)
    fresh.tick()
    state = fresh.state()
    assert isinstance(state.intent, CompletedRearm)
    assert len(fresh.provider.tasks) >= 1
    charges = fresh.charges()
    assert len(charges) == 1 + len(fresh.provider.tasks)
    assert len({run for run, _ in charges}) == len(charges)
    assert state.episode.failures == len(fresh.provider.tasks)
    assert state.episode.episode_id == renewed.episode.episode_id


# -- Row 11: close/reopen and permanent refusal supersede ---------------------

@pytest.mark.parametrize("change", ["close_reopen", "semantic_refusal"])
def test_superseded_intent_frees_slot_without_completion_and_continuation_needs_fresh_scope(deployment, change):
    control = _accept(deployment)
    command_id = required(control)
    accepted_episode = control["current_intent"]["episode_id"]
    client = compose(deployment)
    if change == "close_reopen":
        client.remote.external_change(ISSUE, state="closed", state_reason="completed")
    else:
        # A semantic intervention (not mere activity) makes the accepted
        # projection inapplicable; the executor refuses it permanently.
        client.remote.external_change(ISSUE, add=(Label.RECURRENCE.value,))
    progress(client, 2)
    view = client.read("operator-request")
    assert view["requested_intent"]["state"] == "superseded"
    assert view["requested_intent"]["reason"] == ("target_terminal" if change == "close_reopen" else "required_effect_rejected")
    assert view["workflow_held"] is True
    assert READY not in client.labels() and client.provider.tasks == []
    assert not any(call[0] == "close" for call in client.remote.calls)

    if change == "close_reopen":
        client.remote.external_change(ISSUE, state="open", state_reason="reopened")
    restarted = compose(deployment)
    restarted.boot()
    progress(restarted, 2)
    assert projections(restarted) == []
    assert READY not in restarted.labels() and NHR in restarted.labels()
    retired = receipt(restarted, command_id)
    assert retired is None or retired.status is not EffectStatus.SUCCEEDED
    assert restarted.read("operator-request")["requested_intent"]["state"] == "superseded"
    assert restarted.provider.tasks == []

    continued = restarted.continue_rearm("operator-request", "operator-continue")
    assert continued["outcome"] == "accepted"
    fresh = continued["control"]["current_intent"]
    assert fresh["required_commands"] != [command_id]
    assert fresh["episode_id"] == accepted_episode
    progress(restarted, 2)
    state = restarted.state()
    assert isinstance(state.intent, CompletedRearm)
    assert state.intent.request_id == "operator-continue"
    assert state.episode.episode_id == accepted_episode
    assert len(projections(restarted)) == 1
    retired = receipt(restarted, command_id)
    assert retired is None or retired.status is not EffectStatus.SUCCEEDED


# -- Row 12: cancel after partial success or unknown effect, then continue ----

def test_cancel_after_verified_projection_keeps_receipt_and_continue_does_not_repeat(deployment):
    control = _accept(deployment)
    command_id = required(control)
    client = compose(deployment)
    advance(deployment)
    client.tick()
    assert receipt(client, command_id).status is EffectStatus.SUCCEEDED
    assert isinstance(client.state().intent, PendingRearm)
    handoff = client.read()["current_intent"]["handoff_id"]
    cancelled = client.cancel("operator-request")
    assert cancelled["outcome"] == "accepted"
    assert cancelled["control"]["requested_intent"]["verified_commands"] == [command_id]
    progress(client, 2)
    assert client.provider.tasks == []

    restarted = compose(deployment)
    restarted.boot()
    continued = restarted.continue_rearm("operator-request")
    assert continued["outcome"] == "accepted"
    assert continued["control"]["current_intent"]["handoff_id"] == handoff
    progress(restarted, 2)
    state = restarted.state()
    assert isinstance(state.intent, CompletedRearm)
    assert receipt(restarted, command_id).status is EffectStatus.SUCCEEDED
    assert len(projections(restarted)) == 1
    assert state.revision.fence.generation == control["expected"]["generation"] + 1



def test_cancel_after_unknown_projection_stays_read_only_and_continue_never_conflicts(deployment):
    command_id, fault, client = _unknown_projection(deployment, receipt_delay=True)
    cancelled = client.cancel("operator-request")
    assert cancelled["outcome"] == "accepted", cancelled["reason"]
    uncertain = [getattr(g, "gate", g).cause for g in client.state().gates
                 if type(getattr(g, "gate", g).cause).__name__ == "RemoteOutcomeUncertain"]
    assert [cause.command_id for cause in uncertain] == [command_id]
    continued = client.continue_rearm("operator-request")
    assert continued["outcome"] == "accepted"
    progress(client, 4, minutes=5)
    assert len(projections(client)) == 1
    assert isinstance(client.state().intent, PendingRearm)
    assert client.provider.tasks == []

    healed = compose(deployment)
    healed.boot()
    progress_until(healed, rearm_completed, limit=12, minutes=10)
    state = healed.state()
    assert isinstance(state.intent, CompletedRearm)
    # The unknown write landed once and was verified; the continuation's own
    # projection found it present and never wrote again.
    assert len(projections(healed)) == 1
    assert receipt(healed, command_id).status is EffectStatus.SUCCEEDED


def test_continuation_replacement_waits_for_the_prior_unknown_effect_to_reconcile(deployment):
    """ADR 0015: an unresolved prior effect holds a replacement that could conflict."""
    control = _accept(deployment)
    command_id = required(control)
    fault = LostReplyThenBlindReads(reads_fail=False)  # the prior write landed; only its reply was lost
    client = compose(deployment, fault=fault)
    _probe_skip(client)
    advance(deployment)
    client.tick()
    assert receipt(client, command_id).status is EffectStatus.UNKNOWN_OUTCOME
    assert client.cancel("operator-request")["outcome"] == "accepted"
    client.remote.external_change(ISSUE, add=(Label.AI_PARKED.value,))
    continued = client.continue_rearm("operator-request")
    assert continued["outcome"] == "accepted", continued["reason"]
    replacement = continued["control"]["current_intent"]["required_commands"][0]
    client.tick()  # the prior's scheduled read is not due yet
    assert receipt(client, command_id).status is EffectStatus.UNKNOWN_OUTCOME
    assert receipt(client, replacement) is None
    assert projections(client) == [["relabel", ISSUE, NHR, READY]]

    progress_until(client, rearm_completed, limit=6)
    assert receipt(client, command_id).status is EffectStatus.SUCCEEDED
    assert receipt(client, replacement).status is EffectStatus.SUCCEEDED
    assert {NHR, Label.AI_PARKED.value} <= client.labels()


# -- Row 13: retired unknown → changed guard → verified absence → continue ----

def test_retired_unknown_with_verified_current_absence_lets_fresh_continuation_advance(deployment):
    command_id, fault, client = _unknown_projection(deployment, receipt_delay=True)
    cancelled = client.cancel("operator-request")
    assert cancelled["outcome"] == "accepted", cancelled["reason"]
    client.remote.external_change(ISSUE, add=(NHR,), remove=(READY,))
    healed = compose(deployment)
    healed.boot()
    progress(healed, 3, minutes=10)
    assert not any(type(getattr(g, "gate", g).cause).__name__ == "RemoteOutcomeUncertain"
                   for g in healed.state().gates)
    retired = receipt(healed, command_id)
    assert retired is None or retired.status is not EffectStatus.SUCCEEDED
    assert len(projections(healed)) == 1
    assert healed.provider.tasks == []

    assert healed.continue_rearm("operator-request")["outcome"] == "accepted"
    progress(healed, 3)
    state = healed.state()
    assert isinstance(state.intent, CompletedRearm)
    assert len(projections(healed)) == 2
    assert NHR not in healed.labels()


# -- Row 14: timer/restart and clock skew -------------------------------------

def test_observation_schedule_survives_restart_and_clock_skew_without_tight_polling(deployment):
    _accept(deployment)
    fault = LostReplyThenBlindReads()
    fault.lost = True  # every read fails from the first reconciliation
    client = compose(deployment, fault=fault, retry_ceiling=10**12)
    _probe_skip(client)
    advance(deployment)
    client.tick()
    first = client.workflow.resolve_control_view(client.key).observation
    assert first.history.failed_reads == 1

    deployment.clock.advance(timedelta(minutes=-30))
    skewed = compose(deployment, fault=fault, retry_ceiling=10**12)
    _probe_skip(skewed)
    fault.reads = 0
    for _ in range(12):
        advance(deployment, minutes=1 / 6)
        skewed.tick()
    assert fault.reads == 0
    assert skewed.workflow.resolve_control_view(skewed.key).observation == first
    assert "EvidenceUnavailable" not in skewed.gate_causes()

    deployment.clock.advance(timedelta(hours=2))
    forward = compose(deployment, fault=fault, retry_ceiling=10**12)
    _probe_skip(forward)
    forward.tick()
    later = forward.workflow.resolve_control_view(forward.key).observation
    assert later.history.unresolved_since == first.history.unresolved_since
    assert later.history.failed_reads == first.history.failed_reads + 1
    assert later.history.next_read_at > deployment.clock.now()
    assert "EvidenceUnavailable" in forward.gate_causes()
    reads = fault.reads
    forward.tick()
    forward.tick()
    assert fault.reads == reads
    assert forward.remote.calls == [] and forward.provider.tasks == []


def test_extreme_reconciliation_parameters_are_rejected_not_tight_loops():
    from issue_agent.domain.workflow import ReconciliationPolicy

    for bad in ({"maximum_reads": 10**6}, {"maximum_age_seconds": 10**9},
                {"initial_delay_seconds": 0}, {"maximum_delay_seconds": 10**7}):
        with pytest.raises(ValueError):
            ReconciliationPolicy(**bad)


# -- Row 15: re-escalation after a completed re-arm --------------------------

BREAKER = ("**Circuit breaker:** resolve failed 3× without opening a PR. This episode is held; an explicit "
           "harness re-arm is required to grant another allowance. Escalating to a human.")


def test_reescalation_opens_new_generation_gate_despite_retained_attention_history(bare):
    from issue_agent.domain.world import Comment
    from tests.app.resolve_acceptance_harness import TITLE, BODY
    from issue_agent.domain.world import IssueView
    from tests.fakes.workflow_authority import install_workflow
    from tests.app.resolve_acceptance_harness import exhausted_snapshot

    ceiling = bare.config().max_resolve_retries
    bare.github().seed(IssueView(ISSUE, title=TITLE, body=BODY, updated_at="2026-09-14T00:00:00Z",
                                 labels=frozenset({Label.NEEDS_HUMAN_REVIEW}),
                                 comments=(Comment("fix-bot", BREAKER, "2026-09-13T00:00:00Z"),)))
    install_workflow(bare.control(), exhausted_snapshot(ceiling))
    client = compose(bare)
    client.boot()
    old_gate = client.read()["gates"][0]
    accepted = client.request_rearm()
    generation = accepted["control"]["expected"]["generation"]
    new_episode = accepted["control"]["current_intent"]["episode_id"]
    progress(client, ceiling + 3)
    assert len(client.provider.tasks) == ceiling

    restarted = compose(bare)
    restarted.boot()
    progress(restarted, 3)
    assert restarted.provider.tasks == []
    state = restarted.state()
    gates = ceiling_gates(state)
    assert len(gates) == 1
    gate = getattr(gates[0], "gate", gates[0])
    assert gate.gate_id != old_gate["gate_id"]
    assert gate.opened_generation == generation
    assert gate.cause.episode_id == new_episode and gate.cause.failures == ceiling
    assert NHR in restarted.labels() and READY not in restarted.labels()
    assert len(restarted.charges()) == ceiling
    view = restarted.read()
    assert [g["available_commands"] for g in view["gates"]] == [["request_rearm"]]
    again = restarted.request_rearm("second-request")
    assert again["outcome"] == "accepted"
    assert again["control"]["expected"]["generation"] == generation + 1


# -- Authorized retry of a re-arm obligation (expiry -> absence -> grant) -----

class LostRequestThenBlindReads:
    """The projection request is lost before GitHub applies it; reads then fail."""

    def __init__(self):
        self.lost = False
        self.blind = True

    def __call__(self, point: str) -> None:
        if point == "relabel_requested" and not self.lost:
            self.lost = True
            raise GhUnavailable("network", "projection request lost")
        if point in ("read", "linkage") and self.lost and self.blind:
            raise GhUnavailable("network", "verification read unavailable")


def _retry_gate(view: dict) -> dict | None:
    return next((g for g in view["gates"] if g["cause"]["kind"] == "retry_authorization_required"), None)


def _absent_obligation(deployment):
    """Expire an unknown re-arm projection, then prove its current absence."""
    control = _accept(deployment)
    command_id = required(control)
    fault = LostRequestThenBlindReads()
    client = compose(deployment, fault=fault)
    _probe_skip(client)
    advance(deployment)
    client.tick()
    assert fault.lost and projections(client) == []
    for _ in range(8):
        advance(deployment, minutes=10)
        client.tick()
    assert "RemoteOutcomeUncertain" in client.gate_causes()
    fault.blind = False
    progress_until(client, lambda c: _retry_gate(c.read()) is not None, limit=6, minutes=10)
    assert projections(client) == [] and client.provider.tasks == []
    view = client.read()
    gate = _retry_gate(view)
    assert gate["available_commands"] == ["authorize_retry"] and gate["cause"]["command_id"] == command_id
    return command_id, client, view, gate


def _grant(client, view: dict, gate: dict, authorization_id: str = "grant-1") -> dict:
    return client.post({
        "operation": "authorize_retry", "expected": view["expected"], "authorization_id": authorization_id,
        "gate_id": gate["gate_id"], "command_id": gate["cause"]["command_id"],
        "evidence_id": gate["cause"]["absence_evidence_id"],
    })


def test_authorized_obligation_retry_survives_restart_before_consumption_and_writes_once(deployment):
    command_id, client, view, gate = _absent_obligation(deployment)
    granted = _grant(client, view, gate)
    assert granted["outcome"] == "accepted", granted["reason"]
    assert _grant(client, view, gate)["outcome"] == "replayed"
    assert _grant(client, granted["control"], gate, "grant-2")["outcome"] == "conflict"
    assert projections(client) == []

    restarted = compose(deployment)
    restarted.boot()
    progress_until(restarted, rearm_completed, limit=8, minutes=10)
    assert len(projections(restarted)) == 1
    assert receipt(restarted, command_id).status is EffectStatus.SUCCEEDED
    assert restarted.labels() == {READY}
    # Later drains (and the paid leg the re-arm admitted) never spend the grant again.
    progress(restarted, 3, minutes=60)
    assert restarted.remote.calls.count(["relabel", ISSUE, NHR, READY]) == 1
    assert _grant(restarted, view, gate)["outcome"] == "replayed"


@pytest.mark.parametrize("activity", ["comment", "body_edit"])
def test_harmless_activity_between_grant_and_spend_keeps_the_retry_applicable(deployment, activity):
    command_id, client, view, gate = _absent_obligation(deployment)
    assert _grant(client, view, gate)["outcome"] == "accepted"
    if activity == "comment":
        client.remote.external_change(ISSUE, comment=("alice", "Any update on this?"))
    else:
        client.remote.external_change(ISSUE, body=BODY_EDIT)
    progress_until(client, rearm_completed, limit=8, minutes=10)
    assert len(projections(client)) == 1
    assert receipt(client, command_id).status is EffectStatus.SUCCEEDED
    assert client.labels() == {READY}


def test_semantic_intervention_between_grant_and_spend_declines_the_retry(deployment):
    command_id, client, view, gate = _absent_obligation(deployment)
    assert _grant(client, view, gate)["outcome"] == "accepted"
    client.remote.external_change(ISSUE, add=(Label.AI_PARKED.value,))
    progress(client, 6, minutes=10)
    assert projections(client) == []
    stopped = client.read("operator-request")["requested_intent"]
    assert stopped["state"] == "superseded" and stopped["reason"] == "decision_changed"
    assert stopped["retired_commands"] == [command_id]
    retired = receipt(client, command_id)
    assert retired is None or retired.status is not EffectStatus.SUCCEEDED
    assert {NHR, Label.AI_PARKED.value} <= client.labels() and READY not in client.labels()
    assert client.provider.tasks == []


def _die_during_retry(deployment, point: str) -> None:
    child_environment(deployment)
    client = compose(deployment, fault=lambda observed: os._exit(83) if observed == point else None)
    _probe_skip(client)
    advance(deployment)
    client.tick()


def test_death_after_retry_consumption_reconciles_read_only_and_a_fresh_grant_repairs_once(deployment):
    command_id, client, view, gate = _absent_obligation(deployment)
    assert _grant(client, view, gate)["outcome"] == "accepted"
    # The spent attempt added the trigger label, then the process died mid-write.
    run_until_exit(_die_during_retry, deployment, "label_added", code=83)
    assert len(projections(client)) == 1 and client.labels() == {NHR, READY}

    restarted = compose(deployment)
    restarted.boot()
    _probe_skip(restarted)
    progress(restarted, 4, minutes=60)
    # Replay of the spent grant never buys another write.
    assert len(projections(restarted)) == 1
    assert isinstance(restarted.state().intent, PendingRearm)
    state = restarted.read()
    fresh = _retry_gate(state)
    assert fresh is not None and fresh["available_commands"] == ["authorize_retry"]
    assert fresh["cause"]["absence_evidence_id"] != gate["cause"]["absence_evidence_id"]
    assert _grant(restarted, view, gate)["outcome"] == "replayed"
    assert _grant(restarted, state, gate, "grant-2")["outcome"] == "rejected"
    # The half-applied write is this command's own effect: the decision still
    # applies (ADR 0015 semantic fence), so one more write completes it.
    assert _grant(restarted, state, fresh, "grant-2")["outcome"] == "accepted"
    progress_until(restarted, rearm_completed, limit=6)
    assert len(projections(restarted)) == 2
    assert receipt(restarted, command_id).status is EffectStatus.SUCCEEDED
    assert NHR not in restarted.labels() and READY in restarted.labels()


def test_death_after_retry_remote_success_settles_without_a_second_write(deployment):
    command_id, client, view, gate = _absent_obligation(deployment)
    assert _grant(client, view, gate)["outcome"] == "accepted"
    run_until_exit(_die_during_retry, deployment, "relabel_applied", code=83)
    restarted = compose(deployment)
    restarted.boot()
    progress_until(restarted, rearm_completed, limit=8, minutes=10)
    assert len(projections(restarted)) == 1
    assert receipt(restarted, command_id).status is EffectStatus.SUCCEEDED
    assert _retry_gate(restarted.read()) is None
