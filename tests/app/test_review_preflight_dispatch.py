"""The real dispatch claim spelling reaches unpaid review execution preflight."""

from dataclasses import replace
from pathlib import Path

import pytest

from issue_agent.app.roles import DispatchPathReservations
from issue_agent.domain.actions import AgentKind, RunAgent
from issue_agent.domain.review import ReviewMode
from tests.adapters.test_review_execution_control import OWNER, assert_unpaid, current, rig


@pytest.mark.parametrize("kind", [AgentKind.REVIEW, AgentKind.RE_REVIEW])
def test_dispatch_path_reservation_authorizes_its_exact_unpaid_review_round(rig, kind):
    store, snapshot, _, selection, control = rig
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    reservations = DispatchPathReservations(store, repository="o/r", role="review", owner_id=OWNER)
    assert reservations.reserve(RunAgent(kind, 9), -9, frozenset())
    claim = store.active_dispatch_claims("o/r", "review")[0]
    plan = control.preflight(replace(selection, claim_occurrence_id=claim.claim_key))
    assert plan.round.sequence == 1 and plan.round.binding == selection.binding
    after = current(store, selection)
    assert after.revision.version == snapshot.revision.version + 1
    assert after.execution.last_sequence == 1 and after.execution.state == snapshot.execution.state
    assert_unpaid(store)


def test_rejection_only_activation_cannot_purchase_review(tmp_path, monkeypatch):
    from tests.app.review_acceptance_harness import (
        compose_review, create_review_deployment, install_clock, isolate_authority, verdicts,
    )
    from tests.app.acceptance_support import ScriptedProvider

    deployment = create_review_deployment(tmp_path)
    isolate_authority(deployment, monkeypatch)
    install_clock(deployment, monkeypatch)
    deployment.activate()
    client = compose_review(deployment, provider=ScriptedProvider(verdicts("APPROVE")))
    client.boot()
    client.tick()
    assert client.provider.tasks == []
    assert client.charges() == [] and client.submissions() == []
    with client.review.control.transaction() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM budget_reservations").fetchone()[0] == 0


def test_composed_single_review_settles_one_durable_execution_round(tmp_path, monkeypatch):
    from issue_agent.adapters.store import Store
    from issue_agent.domain.review_execution import CurrentReviewExecution
    from issue_agent.domain.review_workflow import ReviewedBaseline
    from issue_agent.domain.outcome import ExitReason
    from tests.app.review_acceptance_harness import (
        KEY, compose_review, create_review_deployment, install_clock, isolate_authority, register_review_launch, verdicts,
    )
    from tests.app.acceptance_support import ScriptedProvider

    deployment = create_review_deployment(tmp_path, ceiling=5)
    isolate_authority(deployment, monkeypatch)
    install_clock(deployment, monkeypatch)
    deployment.activate()
    results = [replace(result, exit_reason=ExitReason.OK) for result in verdicts("REQUEST_CHANGES", "REQUEST_CHANGES", "APPROVE")]
    results[1] = replace(results[1], session_id="")
    client = compose_review(deployment, provider=ScriptedProvider(results, during=register_review_launch))
    client.activate_execution(monkeypatch)
    legacy = Store(client.cfg.review_state_dir)
    Path(client.cfg.review_state_dir).mkdir(parents=True, exist_ok=True)
    Path(legacy.pr_path(7, "session")).write_text("claude:obsolete-file-session")
    Path(legacy.pr_path(7, "round")).write_text("91")
    Path(legacy.issue_path(-7, "infra-deaths")).write_text("1")
    legacy_paths = (Path(legacy.pr_path(7, "session")), Path(legacy.pr_path(7, "round")),
                    Path(legacy.issue_path(-7, "infra-deaths")))
    legacy_bytes = {path: path.read_bytes() for path in legacy_paths if path.is_file()}

    client.boot()
    client.tick()
    assert len(client.provider.tasks) == 1, client.log
    assert len(client.charges()) == 1 and len(client.submissions()) == 1
    state = client.workflow.load_workflow(KEY)
    assert isinstance(state.execution, CurrentReviewExecution)
    assert state.execution.state.failures == 0 and state.execution.state.displayed_round == 1, client.log
    assert isinstance(state.episode.continuation, ReviewedBaseline)
    with client.review.control.transaction() as db:
        assert db.execute("SELECT count(*) FROM review_execution_rounds WHERE outcome_json IS NOT NULL").fetchone()[0] == 1
    client.tick()
    assert len(client.provider.tasks) == 1 and len(client.submissions()) == 1
    client.remote.external_pr_change(7, head_ref_oid=deployment.heads[1])
    client.tick()
    assert len(client.provider.tasks) == 2 and len(client.charges()) == 2 and len(client.submissions()) == 2, client.log
    assert client.provider.tasks[1].resume == "review-0"
    state = client.workflow.load_workflow(KEY)
    assert state.execution.state.displayed_round == 2 and state.execution.state.failures == 0
    from issue_agent.domain.review_execution import NoReviewSession
    assert isinstance(state.execution.state.session, NoReviewSession)
    client.remote.external_pr_change(7, head_ref_oid=deployment.heads[2])
    client.tick()
    assert len(client.provider.tasks) == 3 and len(client.charges()) == 3 and len(client.submissions()) == 3, client.log
    assert client.provider.tasks[2].resume is None
    assert client.state().execution.state.displayed_round == 3
    from tests.app.review_acceptance_harness import diff_text
    client.remote.set_diff(deployment.heads[3], diff_text("change2", offset=9))
    client.remote.external_pr_change(7, head_ref_oid=deployment.heads[3])
    client.tick(2)
    assert len(client.provider.tasks) == 3 and len(client.charges()) == 3 and len(client.submissions()) == 4, client.log
    assert client.state().episode.continuation.head_sha == deployment.heads[3]
    assert client.state().execution.state.displayed_round == 3
    with client.review.control.transaction() as db:
        assert db.execute("SELECT count(*) FROM review_execution_rounds WHERE outcome_json IS NOT NULL").fetchone()[0] == 4
    assert {path: path.read_bytes() for path in legacy_bytes} == legacy_bytes


@pytest.mark.parametrize("mode,gate_only,findings", [(ReviewMode.DELEGATED, False, False),
                                                   (ReviewMode.MULTI, False, False),
                                                   (ReviewMode.MULTI, True, False),
                                                   (ReviewMode.MULTI, False, True)])
def test_composed_review_modes_have_one_productive_outer_round(tmp_path, monkeypatch, mode, gate_only, findings):
    from issue_agent.domain.outcome import AgentResult, ExitReason
    from issue_agent.domain.review_execution import CurrentReviewExecution
    from tests.app.review_acceptance_harness import (
        compose_review, create_review_deployment, install_clock, isolate_authority, register_review_launch, verdicts,
    )
    from tests.app.acceptance_support import ScriptedProvider

    deployment = create_review_deployment(tmp_path)
    isolate_authority(deployment, monkeypatch)
    install_clock(deployment, monkeypatch)
    deployment.activate()
    if mode is ReviewMode.MULTI:
        results = [AgentResult(is_error=False, cost_usd=0.1, exit_reason=ExitReason.OK, structured={"reviewable": not gate_only})]
        if not gate_only:
            from issue_agent.app.planners.tasks import REVIEW_DIMENSIONS
            candidates = [{"location": "a.py:1", "claim": "bug", "severity": "blocking"}] if findings else []
            results += [AgentResult(is_error=False, cost_usd=0.1, exit_reason=ExitReason.OK, structured={"findings": candidates})
                        for _ in REVIEW_DIMENSIONS]
            if findings:
                results += [AgentResult(is_error=False, cost_usd=0.1, exit_reason=ExitReason.OK,
                                        structured={"confirmed": False, "verdict": "refuted"})
                            for _ in REVIEW_DIMENSIONS]
    else:
        results = [replace(result, exit_reason=ExitReason.OK) for result in verdicts("APPROVE")]
    client = compose_review(deployment, provider=ScriptedProvider(results, during=register_review_launch), mode=mode)
    client.activate_execution(monkeypatch)
    client.boot()
    client.tick()
    assert len(client.provider.tasks) == len(results), client.log
    assert len(client.charges()) == len(results) and len(client.submissions()) == 1
    state = client.state()
    assert isinstance(state.execution, CurrentReviewExecution)
    assert state.execution.state.failures == 0 and state.execution.state.displayed_round == 1, client.log
    with client.review.control.transaction() as db:
        assert db.execute("SELECT count(*) FROM review_execution_rounds WHERE outcome_json IS NOT NULL").fetchone()[0] == 1
    client.tick()
    assert len(client.provider.tasks) == len(results) and len(client.submissions()) == 1


def test_composed_pin_deferrals_are_spaced_by_the_machine_backoff_without_a_strike(tmp_path, monkeypatch):
    """An unavailable pinned checkout is a machine failure: never a strike or a
    paid admission, and a minute-by-minute poll re-plans the round only on the
    doubling backoff (deferrals at minutes 1, 2, 4, 8, 16), not every tick."""
    from tests.app.review_acceptance_harness import (
        compose_review, create_review_deployment, install_clock, isolate_authority, register_review_launch, verdicts,
    )
    from issue_agent.domain.outcome import ExitReason
    from tests.app.acceptance_support import ScriptedProvider

    deployment = create_review_deployment(tmp_path)
    isolate_authority(deployment, monkeypatch)
    install_clock(deployment, monkeypatch)
    deployment.activate()
    results = [replace(result, exit_reason=ExitReason.OK) for result in verdicts("APPROVE")]
    client = compose_review(deployment, provider=ScriptedProvider(results, during=register_review_launch))
    client.activate_execution(monkeypatch)
    workflow = client.review.dispatcher.review_workflow
    original_checkout = workflow.cfg.review_checkout

    def refuse_pin(*_):
        raise OSError("isolated pin failure")

    workflow.cfg = replace(workflow.cfg, review_checkout=refuse_pin)
    client.boot()
    client.tick(20)
    state = client.state()
    assert state.execution.state.pin_deferrals == 5 and state.execution.state.failures == 0, client.log
    assert client.provider.tasks == [] and client.charges() == [] and client.submissions() == []
    with client.review.control.transaction() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM review_execution_rounds WHERE outcome_json IS NOT NULL").fetchone()[0] == 5
    workflow.cfg = replace(workflow.cfg, review_checkout=original_checkout)
    client.tick(minutes=16)
    assert len(client.provider.tasks) == len(client.charges()) == len(client.submissions()) == 1, client.log
    assert client.state().execution.state.pin_deferrals == client.state().execution.state.failures == 0


@pytest.mark.parametrize("mode,failed_stage", [(ReviewMode.SINGLE, "reviewer"), (ReviewMode.MULTI, "gate"),
                                             (ReviewMode.MULTI, "dimensions"), (ReviewMode.SINGLE, "post")])
def test_composed_failed_review_debits_once_for_all_launched_children(tmp_path, monkeypatch, mode, failed_stage):
    from issue_agent.domain.outcome import AgentResult, ExitReason
    from tests.app.review_acceptance_harness import (
        compose_review, create_review_deployment, install_clock, isolate_authority, register_review_launch,
    )
    from tests.app.acceptance_support import ScriptedProvider

    deployment = create_review_deployment(tmp_path)
    isolate_authority(deployment, monkeypatch)
    install_clock(deployment, monkeypatch)
    deployment.activate()
    failure = AgentResult(is_error=True, cost_usd=0.1, exit_reason=ExitReason.AGENT_ERROR)
    results = [failure]
    if failed_stage == "dimensions":
        from issue_agent.app.planners.tasks import REVIEW_DIMENSIONS
        results = [AgentResult(is_error=False, cost_usd=0.1, exit_reason=ExitReason.OK, structured={"reviewable": True})]
        results += [failure for _ in REVIEW_DIMENSIONS]
    elif failed_stage == "post":
        results = [AgentResult(is_error=False, cost_usd=0.1, exit_reason=ExitReason.OK,
                               structured={"decision": "APPROVE", "body": "refused transport"})]
    client = compose_review(deployment, provider=ScriptedProvider(results, during=register_review_launch), mode=mode)
    client.activate_execution(monkeypatch)
    if failed_stage == "post":
        client.remote.soft_failures.add("submit_review")
    client.boot()
    client.tick()
    assert len(client.provider.tasks) == len(results) and len(client.charges()) == len(results), client.log
    assert client.bot_reviews() == []
    assert client.state().execution.state.failures == 1, client.log
    with client.review.control.transaction() as db:
        assert db.execute("SELECT count(*) FROM review_execution_rounds WHERE outcome_json IS NOT NULL").fetchone()[0] == 1
    success = {"reviewable": False} if mode is ReviewMode.MULTI else {"decision": "APPROVE", "body": "recovered"}
    client.remote.soft_failures.discard("submit_review")
    client.provider.results.append(AgentResult(is_error=False, cost_usd=0.1, exit_reason=ExitReason.OK, structured=success))
    client.tick()
    assert len(client.charges()) == len(results) + 1 and len(client.bot_reviews()) == 1, client.log
    assert client.state().execution.state.failures == 0 and client.state().execution.state.displayed_round == 1


@pytest.mark.parametrize("reason", ["budget_exhausted", "transport_death", "runtime_unavailable", "admission_refused"])
def test_composed_budget_and_infrastructure_stops_preserve_failure_allowance(tmp_path, monkeypatch, reason):
    from issue_agent.domain.outcome import AgentResult, ExitReason
    from tests.app.review_acceptance_harness import (
        compose_review, create_review_deployment, install_clock, isolate_authority, register_review_launch,
    )
    from tests.app.acceptance_support import ScriptedProvider

    deployment = create_review_deployment(tmp_path)
    isolate_authority(deployment, monkeypatch)
    install_clock(deployment, monkeypatch)
    deployment.activate()
    if reason == "admission_refused":
        cfg = replace(deployment.config(), issue_spend_cap_usd=0.01)
        monkeypatch.setattr(type(deployment), "config", lambda _: cfg)
        results = []
    else:
        results = [AgentResult(is_error=True, cost_usd=0.1, exit_reason=ExitReason(reason))]
    client = compose_review(deployment, provider=ScriptedProvider(results, during=register_review_launch))
    client.activate_execution(monkeypatch)
    client.boot()
    client.tick()
    assert len(client.provider.tasks) == len(results) and len(client.charges()) == len(results), client.log
    assert client.submissions() == [] and client.state().execution.state.failures == 0
    with client.review.control.transaction() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == len(results)
        assert db.execute("SELECT count(*) FROM review_execution_rounds WHERE outcome_json IS NOT NULL").fetchone()[0] == 1


def test_a_launched_provider_exception_retains_money_and_unresolved_evidence(tmp_path, monkeypatch):
    from tests.app.review_acceptance_harness import (
        compose_review, create_review_deployment, install_clock, isolate_authority, register_review_launch,
    )
    from tests.app.acceptance_support import ScriptedProvider

    deployment = create_review_deployment(tmp_path)
    isolate_authority(deployment, monkeypatch)
    install_clock(deployment, monkeypatch)
    deployment.activate()

    def interrupted(task):
        raise RuntimeError("isolated provider died without a returned envelope")

    client = compose_review(deployment, provider=ScriptedProvider(during=interrupted))
    client.activate_execution(monkeypatch)
    client.boot()
    client.tick()
    assert len(client.provider.tasks) == 1 and client.charges() == [] and client.submissions() == []
    assert client.state().execution.state.failures == 0
    with client.review.control.transaction() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM budget_reservations WHERE state='active'").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM review_execution_rounds WHERE outcome_json IS NULL").fetchone()[0] == 1
    assert not list(Path(client.cfg.review_state_dir).glob("*.infra-deaths"))


@pytest.mark.parametrize("relink", [False, True])
@pytest.mark.parametrize("ceiling", [0, 3])
@pytest.mark.parametrize("mode", ["single", "multi"])
def test_current_failure_ceiling_bounds_purchases_across_polls_and_issue_relink(tmp_path, monkeypatch, relink, ceiling, mode):
    from issue_agent.domain.outcome import AgentResult, ExitReason
    from issue_agent.domain.world import IssueView
    from tests.app.review_acceptance_harness import (
        compose_review, create_review_deployment, install_clock, isolate_authority, register_review_launch,
    )
    from tests.app.acceptance_support import ScriptedProvider

    deployment = create_review_deployment(tmp_path)
    isolate_authority(deployment, monkeypatch)
    install_clock(deployment, monkeypatch)
    deployment.activate()
    cfg = replace(deployment.config(), max_unproductive_review_rounds=ceiling)
    monkeypatch.setattr(type(deployment), "config", lambda _: cfg)
    from issue_agent.domain.review import ReviewMode
    from issue_agent.app.planners.tasks import REVIEW_DIMENSIONS
    selected_mode = ReviewMode(mode)
    children = 1 if selected_mode is ReviewMode.SINGLE else 1 + len(REVIEW_DIMENSIONS)
    round_results = ([AgentResult(is_error=True, cost_usd=0.1, exit_reason=ExitReason.AGENT_ERROR)]
                     if selected_mode is ReviewMode.SINGLE else
                     [AgentResult(is_error=False, cost_usd=0.1, exit_reason=ExitReason.OK, structured={"reviewable": True})]
                     + [AgentResult(is_error=True, cost_usd=0.1, exit_reason=ExitReason.AGENT_ERROR) for _ in REVIEW_DIMENSIONS])
    results = round_results * 10
    client = compose_review(deployment, provider=ScriptedProvider(results, during=register_review_launch), mode=selected_mode)
    client.activate_execution(monkeypatch)
    client.boot()
    client.tick()
    assert client.state().execution.state.failures == 1
    if relink:
        client.remote.seed(IssueView(2, state="open"))
        client.remote.external_pr_change(7, closing_issue=2, head_ref_name="fix/2-thing")
    client.tick(9)
    rounds = ceiling or 10
    assert len(client.provider.tasks) == len(client.charges()) == rounds * children, client.log
    assert client.submissions() == [] and client.state().execution.state.failures == rounds
    assert not list(Path(client.cfg.review_state_dir).glob("*.infra-deaths"))


def test_a_paid_re_review_failure_preserves_display_then_productivity_resets_it(tmp_path, monkeypatch):
    from issue_agent.domain.outcome import AgentResult, ExitReason
    from tests.app.review_acceptance_harness import (
        compose_review, create_review_deployment, install_clock, isolate_authority, verdicts,
    )
    from tests.app.acceptance_support import ScriptedProvider

    deployment = create_review_deployment(tmp_path, ceiling=5)
    isolate_authority(deployment, monkeypatch)
    install_clock(deployment, monkeypatch)
    deployment.activate()
    success, recovered = verdicts("REQUEST_CHANGES", "APPROVE")
    failure = AgentResult(is_error=True, cost_usd=0.1, exit_reason=ExitReason.AGENT_ERROR)
    client = compose_review(deployment, provider=ScriptedProvider([success, failure, recovered]))
    client.activate_execution(monkeypatch)
    client.boot()
    client.tick()
    client.remote.external_pr_change(7, head_ref_oid=deployment.heads[1])
    client.tick()
    assert len(client.charges()) == 2 and len(client.submissions()) == 1
    assert client.provider.tasks[1].resume == "review-0"
    assert client.state().execution.state.failures == 1 and client.state().execution.state.displayed_round == 1
    client.tick()
    assert len(client.charges()) == 3 and len(client.submissions()) == 2
    assert client.state().execution.state.failures == 0 and client.state().execution.state.displayed_round == 2
    assert not list(Path(client.cfg.review_state_dir).glob("*.infra-deaths"))
