"""app/roles.py — `_fix_state` round-trips (review C4).

C4: `app/roles.py` had ZERO test references, and `_fix_state` is the READ half
of EVERY circuit breaker `decide_fix` trips on. The review's mutation proved
this: replacing all four `_fix_state` reads with empty dicts left 848/848
tests green, because nothing drove a real counter through the write side, back
through `_fix_state`, into `decide_fix`.

Every test below does exactly that round trip against a REAL `Store(tmp_path)`:

    real write side (AgentDispatcher → FixWorkflow, a scripted runner)
        → roles._fix_state(store, world)
        → domain.fix.decide_fix(world, cfg, state, fixer_busy=False)

and asserts the breaker actually trips: the escalation Action appears and the
corresponding `RunAgent` is withheld. Each is written as a two-phase
(below-cap / at-cap) pair, which doubles as the anti-vacuity twin: phase one
proves the breaker does NOT fire early (a `_fix_state` that always reads 0
would pass phase one trivially but then also fail to trip in phase two).

MUTATION CHECK (done by hand, see the final report): temporarily replacing
every `_fix_state` read with `{}` was applied to `src/issue_agent/app/roles.py`,
this whole module was run, all round-trip tests FAILED (the phase-two breaker
never tripped, so `RunAgent` kept appearing where an escalation was expected),
and the mutation was then reverted. See the agent's final message for the
exact result.
"""

from __future__ import annotations

from tests.domain.builders import observed_pr, with_checks


from tests.fakes.accounted_workflow import review_retry_pair, seed_review_retry
from issue_agent.domain import continuation_components as cc

import asyncio
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from tests.fakes.legacy_journal import Journal
from issue_agent.adapters.journal import JournalingRuntime, read_events
from issue_agent.adapters.process_supervisor import ProcessSupervisor
from issue_agent.adapters.control_store import (
    AccountingDisposition,
    Lease,
    ReservationRequest,
    SQLiteControlStore,
    SettlementRequest,
)
from issue_agent.adapters.store import Store
from issue_agent.app.dispatch import AgentDispatcher, DispatchConfig
from issue_agent.app.config import HarnessConfig, from_env
from issue_agent.app.main import _recover_pre_adapter_processes
from tests.fakes.executor import ImmediateExecutor as Executor
from issue_agent.app.planners.tasks import (
    FixTaskConfig,
    REVIEW_DIMENSIONS,
    ReviewTaskConfig,
)
from issue_agent.app.roles import (
    DispatchPathReservations,
    EffectRecovery,
    _FIX_RUNTIME_REGISTRY,
    _REVIEW_RUNTIME_REGISTRY,
    _ci_flake_reporter,
    _ci_stall_reporter,
    _control_store,
    _control_store_path,
    _duplicate_pr_reporter,
    _fix_hold_reporter,
    _fix_holds,
    _fix_state as read_fix_state,
    _hold_reporter,
    _inject_predicted_paths,
    _prediction_outcome_reporter,
    _recurrence_reporter,
    _journal,
    _owner_identity,
    _shutdown_finalize,
    _shutdown_prepare,
    _shutdown_terminalize,
    _typed_journal,
    process_supervision_state_dir,
)
from issue_agent.domain.agent_task import RuntimeName
from issue_agent.domain.actions import (
    AgentKind,
    CancelWorkflowRuns,
    EscalateCiPending,
    Relabel,
    RunAgent,
    Target,
)
from issue_agent.domain.fix import (
    FixConfig,
    FixState,
    InFlightFix,
    breaker_key,
    decide_fix,
)
from issue_agent.domain.labels import Label
from issue_agent.domain.outcome import AgentResult
from issue_agent.domain.contracts import (
    BudgetScope,
    Money,
    PriceQuote,
    PricingBasis,
    ReservationId,
    RunId,
)
from issue_agent.domain.refs import issue_from_branch
from issue_agent.domain.recovery import RecoveryTrigger
from issue_agent.domain.review import (
    BoundReviewState,
    FixHold,
    ReviewConfig,
    ReviewMode,
    decide_bound_review,
    fix_hold_ceiling_seconds,
    hold_review_dispatch,
)
from issue_agent.domain.world import (
    Check,
    ClosingRef,
    IssueView,
    PRView,
    PullRequestDiff,
    Review,
    REVIEW_CHANGES_REQUESTED,
    WorldState,
)


@pytest.mark.parametrize("raw", ["[]", "null", "1", '{"sig": [], "since": {}}'])
def test_malformed_stall_advisory_does_not_stop_observation(tmp_path, raw):
    from pathlib import Path
    from issue_agent.app.roles import _stall_reporter
    from tests.domain.builders import issue, world
    store = Store(str(tmp_path))
    Path(store.issue_path(7, "stall-sig")).write_text(raw)
    observed = world(issues=[issue(7, "ai-ready")], now="2026-10-01T00:00:00Z")
    _stall_reporter(None, store, lambda _: None)(observed)
    sig, since = store.get_stall_marker("issue", 7)
    assert isinstance(sig, str) and sig
    assert since == observed.now
    assert observed.issues[0].labels == frozenset({Label.AI_READY})


def test_runtime_wiring_registries_cover_every_supported_runtime():
    assert frozenset(_FIX_RUNTIME_REGISTRY) == frozenset(RuntimeName)
    assert frozenset(_REVIEW_RUNTIME_REGISTRY) == {
        RuntimeName.CLAUDE,
        RuntimeName.CODEX,
    }


def test_process_supervision_isolated_between_checkouts_sharing_state(
    tmp_path,
) -> None:
    shared = tmp_path / "shared-state"
    checkout_a = tmp_path / "checkout-a"
    checkout_b = tmp_path / "checkout-b"
    checkout_a.mkdir()
    checkout_b.mkdir()
    common = {
        "REPO": "owner/repo",
        "STATE_DIR": str(shared),
    }
    cfg_a = from_env({**common, "TARGET_DIR": str(checkout_a)})
    cfg_b = from_env({**common, "TARGET_DIR": str(checkout_b)})
    supervisor_a = ProcessSupervisor(
        process_supervision_state_dir(cfg_a, "fix"),
        grace_seconds=0.05,
    )
    supervisor_b = ProcessSupervisor(
        process_supervision_state_dir(cfg_b, "fix"),
        grace_seconds=0.05,
    )
    assert supervisor_a.state_dir != supervisor_b.state_dir

    process = subprocess.Popen(
        ["sleep", "30"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
    )
    try:
        run_id = RunId("checkout-a-paid-run")
        supervisor_a.register(run_id, os.getpgid(process.pid))

        assert supervisor_b.recover_orphans() == ()
        assert process.poll() is None
        assert supervisor_a.recover_orphans() == (run_id,)

        deadline = time.monotonic() + 3
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_boot_preflight_recovers_current_and_repointed_but_not_siblings(
    tmp_path,
) -> None:
    shared = str(tmp_path / "shared-state")
    prior_target = str(tmp_path / "prior")
    current_target = str(tmp_path / "current")
    sibling_target = str(tmp_path / "sibling")
    for target in (prior_target, current_target, sibling_target):
        os.makedirs(target)
    base = HarnessConfig(
        repo="o/r",
        target_dir=prior_target,
        state_dir=shared,
        review_state_dir=shared,
        steward_state_dir=shared,
    )
    current = replace(
        base,
        target_dir=current_target,
        repointed_from_deployments=(("o/r", prior_target),),
    )
    sibling = replace(base, target_dir=sibling_target)
    prior_supervisor = ProcessSupervisor(
        process_supervision_state_dir(base, "fix"),
        grace_seconds=0.05,
    )
    current_supervisor = ProcessSupervisor(
        process_supervision_state_dir(current, "fix"),
        grace_seconds=0.05,
    )
    sibling_supervisor = ProcessSupervisor(
        process_supervision_state_dir(sibling, "fix"),
        grace_seconds=0.05,
    )
    prior_process = subprocess.Popen(
        ["sleep", "30"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
    )
    current_process = subprocess.Popen(
        ["sleep", "30"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
    )
    sibling_process = subprocess.Popen(
        ["sleep", "30"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
    )
    try:
        prior_run = RunId("prior-repoint-run")
        current_run = RunId("current-deployment-run")
        sibling_run = RunId("unrelated-sibling-run")
        prior_supervisor.register(prior_run, os.getpgid(prior_process.pid))
        current_supervisor.register(
            current_run, os.getpgid(current_process.pid)
        )
        sibling_supervisor.register(
            sibling_run, os.getpgid(sibling_process.pid)
        )

        assert _recover_pre_adapter_processes(
            RecoveryTrigger.WAKE, current, "fix", shared
        ) == ()
        assert prior_process.poll() is None
        assert current_process.poll() is None
        assert _recover_pre_adapter_processes(
            RecoveryTrigger.BOOT, current, "fix", shared
        ) == (prior_run, current_run)

        deadline = time.monotonic() + 3
        while (
            (prior_process.poll() is None or current_process.poll() is None)
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        assert prior_process.poll() is not None
        assert current_process.poll() is not None
        assert sibling_process.poll() is None
        assert sibling_supervisor.registrations()[0].run_id == sibling_run
    finally:
        if prior_process.poll() is None:
            prior_process.kill()
            prior_process.wait()
        if current_process.poll() is None:
            current_process.kill()
            current_process.wait()
        if sibling_process.poll() is None:
            sibling_process.kill()
            sibling_process.wait()


def test_explicit_repoint_adopts_and_releases_old_accounting_owner(
    tmp_path,
) -> None:
    state_root = tmp_path / "state"
    state = str(state_root / "fix")
    review_state = str(state_root / "review")
    steward_state = str(state_root / "steward")
    prior_target = str(tmp_path / "prior")
    current_target = str(tmp_path / "current")
    old = HarnessConfig(
        repo="o/r",
        target_dir=prior_target,
        state_dir=state,
        review_state_dir=review_state,
        steward_state_dir=steward_state,
    )
    current = replace(
        old,
        target_dir=current_target,
        repointed_from_deployments=(("o/r", prior_target),),
    )
    store = SQLiteControlStore(_control_store_path(old, state))
    store.initialize()
    now = datetime.now(timezone.utc)
    period_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    scope = BudgetScope(
        "o/r",
        "repository",
        "USD",
        now.date().isoformat(),
        period_start,
        period_start + timedelta(days=1),
        ZoneInfo("UTC"),
    )
    old_owner = _owner_identity(old, "fix")
    request = ReservationRequest(
        ReservationId("repoint-old-reservation"),
        RunId("repoint-old-run"),
        "repoint-old-dispatch",
        scope,
        PriceQuote("model", "test", "1", now, Money("USD", 60)),
        Money("USD", 100),
        "cfg",
        "o/r",
        kind="RESOLVE",
        owner_id=old_owner,
    )
    assert store.reserve(request).admitted
    store.register_lease(
        Lease(
            request.run_id,
            request.dispatch_id,
            old_owner,
            2_000_000_000,
            2_000_000_000,
            now,
            1,
        )
    )

    journal = _typed_journal(
        _journal(Store(state_dir=state)),
        current,
        "fix",
        state,
    )
    adopted = _control_store(current, state, journal)
    current_owner = _owner_identity(current, "fix")
    assert adopted.leases(current_owner)[0].run_id == request.run_id
    assert adopted.reclaim_dead_leases(
        current_owner, lambda _pid, _start: False
    ) == (request.run_id,)
    assert adopted.release(request.run_id, "spend_unknown").disposition.value == (
        "available"
    )

    next_request = replace(
        request,
        reservation_id=ReservationId("repoint-new-reservation"),
        run_id=RunId("repoint-new-run"),
        owner_id=current_owner,
    )
    assert adopted.reserve(next_request).admitted


def test_hard_shutdown_reconciles_killed_rollout_before_releasing_capacity(
    tmp_path,
):
    now = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)
    control = SQLiteControlStore(
        str(tmp_path / "control.db"), now=lambda: now
    )
    control.initialize()
    scope = BudgetScope(
        "o/r",
        "repository",
        "USD",
        "2026-07-30",
        now.replace(hour=0),
        now.replace(hour=0) + timedelta(days=1),
        ZoneInfo("UTC"),
    )
    run_id = RunId("shutdown-reconcile")
    assert control.reserve(
        ReservationRequest(
            ReservationId("shutdown-reservation"),
            run_id,
            json.dumps({"kind": "RESOLVE", "issue": 42}),
            scope,
            PriceQuote(
                "model",
                "card",
                "v1",
                now,
                Money("USD", 1_000),
            ),
            Money("USD", 2_000),
            "config-v1",
            "o/r",
            kind="RESOLVE",
            owner_id="o/r:fix",
        )
    ).admitted
    control.register_process_lease(
        run_id,
        expected_owner_id="o/r:fix",
        pid=123,
        pgid=123,
        process_start_ticks=99,
    )
    _shutdown_prepare(control, owner_id="o/r:fix")()
    assert control.has_cancellation_intent(run_id)
    order = []

    class Effects:
        def drain_pending_commands(self):
            order.append("effects")
            return 0

    _shutdown_finalize(
        control,
        Effects(),
        owner_id="o/r:fix",
        retire_projection_feed=lambda: order.append("retire-feed"),
    )()

    assert order == ["effects", "retire-feed"]
    assert control.active_reservation_runs("o/r:fix") == (run_id,)

    class Spend:
        def reconcile(self, candidate):
            result = control.settle(
                SettlementRequest(
                    candidate,
                    Money("USD", 250),
                    PricingBasis.RECONCILED,
                    "recovered-rollout",
                    None,
                    None,
                    None,
                )
            )
            return result.disposition is AccountingDisposition.AVAILABLE

    _shutdown_terminalize(
        control,
        Spend(),
        owner_id="o/r:fix",
        state_dir=str(tmp_path / "state"),
        role="fix",
    )()

    assert control.active_reservation_runs("o/r:fix") == ()
    assert control.leases("o/r:fix") == ()
    assert control.snapshot(scope).settled == Money("USD", 250)
    assert control.pending_cancellation_intents("o/r:fix") == ()
    assert (tmp_path / "state" / "handovers" / "issue-42.md").exists()


def test_effect_recovery_drains_remote_actions_and_agent_publications():
    calls = []

    class Recovery:
        def __init__(self, name, count):
            self.name = name
            self.count = count

        def drain_pending_commands(self):
            calls.append(self.name)
            return self.count

    recovery = EffectRecovery(
        Recovery("actions", 2),
        Recovery("publications", 3),
    )

    assert recovery.drain_pending_commands() == 5
    assert calls == ["actions", "publications"]

from test_dispatch import (
    FakeRunner,
    FakeWorktrees,
    RaisingRunner,
    SchemaRunner,
    _gate,
    _val,
)
from tests.fakes.in_memory_github import InMemoryGitHub as FakeGateway
from tests.fakes.accounted_dispatch import with_resolve_authority
from tests.fakes.accounted_workflow import issue_recovery_state
from tests.fakes.accounted_workflow import attach_workflow_authority, resolve_state
from tests.fakes.workflow_authority import install_workflow, resolve_snapshot

FIX_BOT = "fix-bot"


def _fix_state(store, world, *args, **kwargs):
    """Behavior fixtures supply isolated SQL authority explicitly."""
    workflows = attach_workflow_authority(FakeGateway(
        issues={i.number: i for i in world.issues},
        prs={p.number: p for p in world.prs},
    ), store, resolve_ceiling=3, pr_repair=True)
    return read_fix_state(store, world, *args, workflow_control=workflows, **kwargs)


def _control(tmp_path) -> SQLiteControlStore:
    control = SQLiteControlStore(str(tmp_path / "control.sqlite3"))
    control.initialize()
    return control


def _dispatcher(gw, store, runner, **overrides):
    kwargs = dict(
        # Pinned model: keeps the difficulty-router leg out of these scripted
        # sequences (see test_fix_workflow's routing tests for the unpinned path).
        fix=FixTaskConfig("o/r", agent_md="AM", model="m"),
        review=ReviewTaskConfig("o/r", review_prompt="p"),
        fix_bot=FIX_BOT,
    )
    kwargs.update(overrides)
    cfg = DispatchConfig(**kwargs)
    executor = Executor(gw, remove_worktree=FakeWorktrees().remove)
    return with_review_authority(with_resolve_authority(AgentDispatcher(cfg, gw, store, FakeWorktrees(), runner, executor)))


def _cfg(**overrides):
    kwargs = dict(fix_bot=FIX_BOT)
    kwargs.update(overrides)
    return FixConfig(**kwargs)


def _has_run(actions, kind) -> bool:
    return any(isinstance(a, RunAgent) and a.kind is kind for a in actions)


def test_concurrent_router_expansions_atomically_reserve_one_shared_path():
    reservations = DispatchPathReservations()
    first = RunAgent(AgentKind.RESOLVE, 1)
    second = RunAgent(AgentKind.RESOLVE, 2)
    assert reservations.reserve(first, 1, frozenset({"src/a.py"}))
    assert reservations.reserve(second, 2, frozenset({"src/b.py"}))
    assert reservations.issue_for(first) == 1
    assert reservations.issue_for(second) == 2

    gate = threading.Barrier(3)
    results: list[bool] = []

    def expand(issue):
        gate.wait()
        results.append(
            reservations.expand(issue, frozenset({"src/shared.py"}))
        )

    threads = [
        threading.Thread(target=expand, args=(1,)),
        threading.Thread(target=expand, args=(2,)),
    ]
    for thread in threads:
        thread.start()
    gate.wait()
    for thread in threads:
        thread.join(timeout=1)

    assert sorted(results) == [False, True]
    snapshots = {item.issue: item.paths for item in reservations.snapshot()}
    assert sum("src/shared.py" in paths for paths in snapshots.values()) == 1


def test_distinct_checkouts_share_durable_fix_dispatch_claims(
    tmp_path,
) -> None:
    path = str(tmp_path / "control.sqlite3")
    first_store = SQLiteControlStore(path)
    second_store = SQLiteControlStore(path)
    first_store.initialize()
    second_store.initialize()
    first = DispatchPathReservations(
        first_store,
        repository="o/r",
        role="fix",
        owner_id="o/r:fix:checkout-a",
    )
    second = DispatchPathReservations(
        second_store,
        repository="o/r",
        role="fix",
        owner_id="o/r:fix:checkout-b",
    )
    action = RunAgent(AgentKind.RESOLVE, 7)

    assert first.reserve(action, 7, frozenset({"src/a.py"}))
    assert not second.reserve(action, 7, frozenset({"src/b.py"}))
    assert not second.reserve(
        RunAgent(AgentKind.RESOLVE, 8),
        8,
        frozenset({"src/a.py"}),
    )

    first.finish(action)
    assert second.reserve(action, 7, frozenset({"src/b.py"}))


# ── resolve-retries (issue-keyed) ──────────────────────────────────────────────
def test_resolve_episode_round_trip_trips_the_gate(tmp_path):
    """Settled attempts update the same SQL episode the scheduler observes."""
    issue = 5
    gw = FakeGateway(issues={issue: IssueView(issue, title="t", body="b")})
    store = Store(state_dir=str(tmp_path))
    runner = FakeRunner(AgentResult(is_error=True))  # error, no PR opened, no infra death
    d = _dispatcher(gw, store, runner)
    cfg = _cfg(max_resolve_retries=3)

    def world():
        return WorldState(issues=(
            IssueView(issue, title="t", body="b", labels=frozenset({Label.AI_READY})),
        ))

    asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, issue)))
    assert resolve_state(store).episode.failures == 1
    actions = decide_fix(world(), cfg, _fix_state(store, world()), fixer_busy=False)
    assert _has_run(actions, AgentKind.RESOLVE)          # anti-vacuity: below cap, still dispatches
    assert Relabel(Target.ISSUE, issue, Label.AI_READY, Label.NEEDS_HUMAN_REVIEW) not in actions

    asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, issue)))
    assert resolve_state(store).episode.failures == 2
    assert _has_run(decide_fix(world(), cfg, _fix_state(store, world()), fixer_busy=False), AgentKind.RESOLVE)
    asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, issue)))
    assert resolve_state(store).episode.failures == 3
    assert resolve_state(store).gates
    actions = decide_fix(world(), cfg, _fix_state(store, world()), fixer_busy=False)
    assert not _has_run(actions, AgentKind.RESOLVE)      # at cap: withheld
    assert any(isinstance(a, Relabel) and a.add is Label.NEEDS_HUMAN_REVIEW for a in actions)


# ── ci-retries (PR-keyed via closing_issue) ────────────────────────────────────
def _ci_pr(issue):
    return observed_pr(
        100, head_ref_name="fix/5-x", closing_issue=issue,
        checks=(Check("unit", bucket="fail"),),
        labels=frozenset({Label.AI_PR_OPEN}), author=FIX_BOT,
    )


def test_ci_retries_round_trip_trips_the_breaker(tmp_path):
    """review C4. Write: FIX_CI dispatches bump ci-retries via the REAL
    FixWorkflow.fix_ci. Read: _fix_state keys ci_retries by breaker_key(pr)
    (== the closing issue here); decide_fix withholds RunAgent(FIX_CI) and
    escalates the PR once the cap is hit."""
    issue, pr = 5, 100
    gw = FakeGateway(prs={pr: _ci_pr(issue)})
    store = Store(state_dir=str(tmp_path))
    runner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="c"))
    d = _dispatcher(gw, store, runner)
    cfg = _cfg(max_ci_retries=2)

    def world():
        return WorldState(prs=(_ci_pr(issue),))

    asyncio.run(d.dispatch(RunAgent(AgentKind.FIX_CI, pr)))
    assert issue_recovery_state(store, issue).ci_failures == 1
    actions = decide_fix(world(), cfg, _fix_state(store, world()), fixer_busy=False)
    assert _has_run(actions, AgentKind.FIX_CI)

    asyncio.run(d.dispatch(RunAgent(AgentKind.FIX_CI, pr)))
    assert issue_recovery_state(store, issue).ci_failures == 2
    actions = decide_fix(world(), cfg, _fix_state(store, world()), fixer_busy=False)
    assert not _has_run(actions, AgentKind.FIX_CI)
    assert Relabel(Target.PR, pr, Label.AI_PR_OPEN, Label.NEEDS_HUMAN_REVIEW) in actions


@pytest.mark.parametrize("change", ["none", "head", "linkage", "checks", "empty-checks", "unknown-checks", "merged", "replacement", "ambiguous"])
def test_composed_fix_daemon_resets_only_the_selected_settled_green_ci(tmp_path, change):
    from issue_agent.domain.ci_history import CiAttempt
    from issue_agent.domain import continuation_components as cc
    from issue_agent.domain.actions import ResetCiRetries
    from issue_agent.domain.workflow_identity import WorkflowFamily, WorkflowKey, IssueRef, PullRequestRef
    from issue_agent.adapters.workflow_rows import load_issue_aggregate
    from tests.fakes.workflow_authority import with_issue_recovery
    from tests.fakes.review_workflow import held
    from tests.adapters.test_continuation_admission import active, install_secondary
    daemon = _codex_daemon(tmp_path)
    dispatcher = daemon.executor._run_agent._dispatch.__self__
    control = dispatcher.fix_workflow.workflow_control
    sql = control._control
    base = with_issue_recovery(resolve_snapshot(5))
    state = replace(base.issue_recovery.state, ci_failures=2, review_failures=cc.ReviewRetryState(count=3), rebase_failures=4, resumes=5)
    issue = replace(base, issue_recovery=replace(base.issue_recovery, state=state))
    install_workflow(sql, issue)
    key = WorkflowKey("o/r", PullRequestRef(100))
    pr_base = replace(held(key=key), gates=())
    install_workflow(sql, pr_base)
    component = active(key, WorkflowFamily.PR_REPAIR)
    install_secondary(sql, replace(pr_base, pr_repair=component), component)
    observed = with_checks(_ci_pr(5), head_ref_oid="a" * 40, checks=(Check("unit", bucket="pass"),))
    selected = control.read_issue_recovery(WorkflowKey("o/r", IssueRef(5)))
    source = cc.CIEpisodeReset(selected.binding, selected.revision, pr_base.revision.fence, observed.head_ref_oid)
    if change == "head":
        observed = replace(observed, head_ref_oid="b" * 40)
    elif change == "linkage":
        observed = replace(observed, closing_issue=6, head_ref_name="fix/6-other")
    elif change == "checks":
        observed = with_checks(observed, checks=(Check("unit", bucket="fail"),))
    elif change == "unknown-checks":
        observed = with_checks(observed, checks_known=False)
    elif change == "empty-checks":
        observed = with_checks(observed, checks=())
    elif change in {"merged", "replacement"}:
        observed = with_checks(observed, state="MERGED", checks=(Check("unit", bucket="fail"),))
    if change in {"replacement", "ambiguous"}:
        dispatcher.gw.prs[101] = replace(_ci_pr(5), number=101, head_ref_oid="b" * 40)
    dispatcher.gw.prs[100] = observed
    dispatcher.store.append_ci_attempt(5, CiAttempt("old-head", ("unit",)))
    from pathlib import Path
    Path(dispatcher.store.issue_path(5, "ci-retries")).write_text("-999")
    assert control.read_issue_recovery(selected.binding.fence.key).component.state.ci_failures == 2
    if change == "merged":
        from issue_agent.domain.actions import ClearIssueState
        world = WorldState(prs=(observed,), issues=(IssueView(5, labels=frozenset({Label.AI_PR_OPEN})),))
        state_view = read_fix_state(dispatcher.store, world, workflow_control=control)
        planned = decide_fix(world, _cfg(), state_view, fixer_busy=True)
        resets = [action for action in planned if isinstance(action, ResetCiRetries)]
        assert resets == [ResetCiRetries(5, source)]
        daemon.executor.execute(resets[0])
        cleanup = next(action for action in planned if isinstance(action, ClearIssueState))
        assert cleanup.source is not None
        daemon.executor.execute(cleanup)
    elif change == "none":
        daemon.executor.execute(ResetCiRetries(5, source))
    else:
        with pytest.raises(ValueError, match="settled green PR/head linkage|ambiguous"):
            daemon.executor.execute(ResetCiRetries(5, source))
    with sql.transaction() as db:
        after = load_issue_aggregate(db, selected.binding.fence.key)
        assert after.episode == issue.episode
    assert after.issue_recovery.state == (replace(state, ci_failures=0) if change in {"none", "merged"} else state)
    assert bool(dispatcher.store.get_ci_history(5)) is (change not in {"none", "merged"})
    if change == "merged":
        from issue_agent.adapters.workflow_rows import persist_workflow_snapshot
        from issue_agent.domain.workflow_identity import WorkflowRevision
        new_debt = replace(after.issue_recovery.state, ci_failures=1)
        with sql.transaction() as db:
            persist_workflow_snapshot(db, replace(after,
                revision=WorkflowRevision(after.revision.fence, after.revision.version+1),
                issue_recovery=replace(after.issue_recovery, state=new_debt)))
        dispatcher.store.append_ci_attempt(5, CiAttempt("new-head", ("unit",)))
        daemon.executor.execute(ResetCiRetries(5, source))
        assert control.read_issue_recovery(selected.binding.fence.key).component.state == new_debt
        assert len(dispatcher.store.get_ci_history(5)) == 1
        dispatcher.gw.prs[101] = replace(_ci_pr(5), number=101, head_ref_oid="b" * 40)
        with pytest.raises(ValueError, match="settled green PR/head linkage"):
            daemon.executor.execute(ResetCiRetries(5, source))
        assert control.read_issue_recovery(selected.binding.fence.key).component.state == new_debt


# ── rebase-retries (PR-keyed via closing_issue) ────────────────────────────────
def _rebase_pr(issue):
    return observed_pr(
        200, head_ref_name="fix/6-y", closing_issue=issue, mergeable="CONFLICTING",
        labels=frozenset({Label.AI_PR_OPEN}), author=FIX_BOT,
    )


def test_rebase_retries_round_trip_trips_the_breaker(tmp_path):
    from issue_agent.domain.workflow_identity import WorkflowKey, IssueRef
    """review C4. Write: REBASE dispatches bump rebase-retries via the REAL
    FixWorkflow.rebase. Read/decide as above, for the CONFLICTING-branch leg."""
    issue, pr = 6, 200
    gw = FakeGateway(prs={pr: _rebase_pr(issue)})
    store = Store(state_dir=str(tmp_path))
    runner = FakeRunner(AgentResult(is_error=True))  # a failed rebase attempt, not an infra death
    d = _dispatcher(gw, store, runner)
    cfg = _cfg(max_rebase_retries=2)

    def world():
        return WorldState(prs=(_rebase_pr(issue),))

    asyncio.run(d.dispatch(RunAgent(AgentKind.REBASE, pr)))
    assert store._workflow_authority.read_issue_recovery(WorkflowKey("o/r", IssueRef(issue))).component.state.rebase_failures == 1
    actions = decide_fix(world(), cfg, _fix_state(store, world()), fixer_busy=False)
    assert _has_run(actions, AgentKind.REBASE)

    asyncio.run(d.dispatch(RunAgent(AgentKind.REBASE, pr)))
    assert store._workflow_authority.read_issue_recovery(WorkflowKey("o/r", IssueRef(issue))).component.state.rebase_failures == 2
    actions = decide_fix(world(), cfg, _fix_state(store, world()), fixer_busy=False)
    assert not _has_run(actions, AgentKind.REBASE)
    assert Relabel(Target.PR, pr, Label.AI_PR_OPEN, Label.NEEDS_HUMAN_REVIEW) in actions


# ── review-retry (PR-keyed via closing_issue; (at, count) tuple) ──────────────
def _review_pr(issue, submitted_at="2026-01-01T00:00:01Z"):
    return observed_pr(
        300, head_ref_name="fix/7-z", closing_issue=issue,
        reviews=(Review(state=REVIEW_CHANGES_REQUESTED, author="human1", submitted_at=submitted_at),),
        labels=frozenset({Label.AI_PR_OPEN}), author=FIX_BOT,
    )


def test_review_retry_round_trip_trips_the_breaker(tmp_path):
    """review C4. Write: ADDRESS_REVIEW dispatches bump review-retry (keyed to
    the review's submitted_at) via the REAL FixWorkflow.address_review. Read/
    decide: decide_fix compares the SAME submitted_at before trusting the
    stored count (a superseding review would reset it — not exercised here)."""
    issue, pr = 7, 300
    gw = FakeGateway(prs={pr: _review_pr(issue)})
    store = Store(state_dir=str(tmp_path))
    runner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="a"))
    d = _dispatcher(gw, store, runner)
    cfg = _cfg(max_review_retries=2)

    def world():
        return WorldState(prs=(_review_pr(issue),))

    asyncio.run(d.dispatch(RunAgent(AgentKind.ADDRESS_REVIEW, pr)))
    assert review_retry_pair(store, issue) == ("2026-01-01T00:00:01Z", 1)
    actions = decide_fix(world(), cfg, _fix_state(store, world()), fixer_busy=False)
    assert _has_run(actions, AgentKind.ADDRESS_REVIEW)

    asyncio.run(d.dispatch(RunAgent(AgentKind.ADDRESS_REVIEW, pr)))
    assert review_retry_pair(store, issue) == ("2026-01-01T00:00:01Z", 2)
    actions = decide_fix(world(), cfg, _fix_state(store, world()), fixer_busy=False)
    assert not _has_run(actions, AgentKind.ADDRESS_REVIEW)
    assert Relabel(Target.PR, pr, Label.AI_PR_OPEN, Label.NEEDS_HUMAN_REVIEW) in actions


# ── machine deaths (issue-keyed) ───────────────────────────────────────────────
def test_machine_death_leaves_attempts_unchanged_and_redispatches_only_after_backoff(tmp_path):
    """review C4 / C3, and the rule that machine failures are never issue debt.
    Write: a RESOLVE session that dies pre-result (raises, never returns) is a
    reclaimed run, a machine end. Read/decide: the issue's attempts do not
    move; the next RESOLVE waits out the doubling backoff from the last death,
    then dispatches again. Five deaths in a row never escalate."""
    issue = 8
    gw = FakeGateway(issues={issue: IssueView(issue, title="t", body="b")})
    store = Store(state_dir=str(tmp_path))
    class LaunchedDeath(RaisingRunner):
        async def run(self, task):
            store._workflow_db.register_process_lease(RunId(task.env["ISSUE_AGENT_RUN_ID"]),
                expected_owner_id="o/r:fix:fixture", pid=123, pgid=123, process_start_ticks=99)
            return await super().run(task)
    d = _dispatcher(gw, store, LaunchedDeath())
    cfg = _cfg()

    def world(at):
        return WorldState(issues=(
            IssueView(issue, title="t", body="b", labels=frozenset({Label.AI_READY})),
        ), now=at.isoformat().replace("+00:00", "Z"))

    for deaths in range(1, 6):
        with pytest.raises(RuntimeError):
            asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, issue)))
        ends = store._workflow_db.fix_leg_ends("o/r", issue)
        assert [end.machine for end in ends] == [True] * deaths
        assert resolve_state(store, issue).episode.failures == 0
        due = max(end.ended_at for end in ends) + min(timedelta(seconds=60 * 2 ** (deaths - 1)), timedelta(hours=1))
        early = decide_fix(world(due - timedelta(seconds=1)), cfg, _fix_state(store, world(due)), fixer_busy=False)
        assert not _has_run(early, AgentKind.RESOLVE)
        assert not any(isinstance(a, Relabel) and a.add is Label.NEEDS_HUMAN_REVIEW for a in early)
        assert _has_run(decide_fix(world(due), cfg, _fix_state(store, world(due)), fixer_busy=False), AgentKind.RESOLVE)


# ── C4 key-agreement: breaker_key(pr) vs gateway.issue_for_pr(pr) ──────────────
def test_breaker_key_agrees_with_gateway_issue_for_pr_when_issueless(tmp_path):
    """review C4 (the PR #112 class). A PR with NO closing-ref AND a
    non-numeric branch is genuinely issueless: domain.fix.breaker_key falls
    back to -pr.number.

    FixWorkflow.fix_ci/rebase/address_review all early-return WITHOUT writing
    anything for such a PR (`if issue is None: return`) — and decide_fix's own
    routing never dispatches those kinds on an issueless PR in the first place
    (see fix.py's `issue_n is None` branch), so there is no "workflow write"
    for ci/rebase/review-retry to round-trip here at all. The ONE production
    writer that ever touches a PR's negated key is AgentDispatcher.dispatch's
    exception-bump path (`key = issue if issue is not None else -action.number`),
    which is common to every PR-keyed AgentKind — so REVIEW stands in for it.
    This proves the key FORMULA agrees; see the report for the write-side gap
    this surfaces (not a bug: decide_fix's own gating makes it moot)."""
    pr = observed_pr(400, head_ref_name="feat/ACME-2468-x")  # no closing_issue, non-numeric branch
    assert issue_from_branch(pr.head_ref_name) is None  # sanity: genuinely issueless
    assert breaker_key(pr) == -400

    gw = FakeGateway(prs={pr.number: pr})
    store = Store(state_dir=str(tmp_path))
    d = _dispatcher(gw, store, RaisingRunner())

    with pytest.raises(RuntimeError):
        asyncio.run(d.dispatch(RunAgent(AgentKind.REVIEW, pr.number)))

    state = _fix_state(store, WorldState(prs=(pr,)))
    assert breaker_key(pr) not in state.leg_ends
    assert not _has_run(decide_fix(WorldState(prs=(pr,)), _cfg(), state, False), AgentKind.FIX_CI)


def test_breaker_key_agrees_with_gateway_issue_for_pr_when_branch_numbered_no_closing_ref(tmp_path):
    """review C4. The other half of #112: NO closing-ref, but the branch DOES
    encode an issue number (the fixer's own branch-naming convention). Both
    key implementations must resolve to the SAME real issue number, never the
    negated PR number — write through FixWorkflow.fix_ci, read through
    breaker_key + _fix_state."""
    issue, pr_no = 42, 401
    pr = observed_pr(
        pr_no, head_ref_name=f"fix/{issue}-thing", closing_issue=None,
        checks=(Check("unit", bucket="fail"),),
        labels=frozenset({Label.AI_PR_OPEN}), author=FIX_BOT,
    )
    assert issue_from_branch(pr.head_ref_name) == issue
    assert breaker_key(pr) == issue  # NOT -pr_no

    gw = FakeGateway(prs={pr_no: pr})
    store = Store(state_dir=str(tmp_path))
    runner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="c"))
    d = _dispatcher(gw, store, runner)

    asyncio.run(d.dispatch(RunAgent(AgentKind.FIX_CI, pr_no)))

    # The SQL workflow charge, where `issue` comes from gateway.issue_for_pr,
    # must land under the SAME
    # key `_fix_state` reads via the domain's breaker_key.
    state = _fix_state(store, WorldState(prs=(pr,)))
    assert state.ci_retries[breaker_key(pr)] == 1
    assert issue_recovery_state(store, issue).ci_failures == 1  # explicit: the real issue number, not -401


# ══════════════════════════════════════════════════════════════════════════════
# review P0R-3 — the reviewer's ceiling, measured in PAID DISPATCHES
#
# `decide_bound_review` emitted one RunAgent per eligible PR per poll with no
# `consumed` gate and no breaker read: a review that never persists state is
# bought again on the very next poll, forever. These tests assert on the money
# (how many dispatches the loop bought over 20 polls), not on a label, and they
# feed the escalation's own relabel back into the observed world so the
# structural stop — the PR leaving `prs_with(trigger_label)` — is exercised
# rather than assumed.
# ══════════════════════════════════════════════════════════════════════════════
REVIEW_BOT = "review-bot"

from tests.fakes.review_authority import review_binding, review_state, with_review_authority


def _bound_review_state(store, world):
    recorded = review_state(store)
    errors = {}
    counts = {}
    owed = {pr.number: (AgentKind.RE_REVIEW if recorded.head(pr.number) else AgentKind.REVIEW,
                       review_binding(pr.number)) for pr in world.prs if recorded.head(pr.number) != pr.head_ref_oid}
    return BoundReviewState(owed=owed, unproductive_rounds=counts, control_errors=errors)







def _open_pr(number=500, issue=9, sha="abc"):
    return observed_pr(
        number, head_ref_name=f"fix/{issue}-x", head_ref_oid=sha, closing_issue=issue,
        title="t", body="b", labels=frozenset({Label.AI_PR_OPEN}), state="OPEN",
        author=FIX_BOT,
    )












def test_a_currently_running_dispatch_is_not_a_machine_end(tmp_path):
    issue = 8
    store = Store(state_dir=str(tmp_path))
    world = WorldState(issues=(
        IssueView(issue, title="t", body="b", labels=frozenset({Label.AI_WORKING})),
    ))
    state = _fix_state(
        store,
        world,
        (InFlightFix(issue, frozenset({"src/x.py"})),),
    )

    assert state.leg_ends.get(issue, ()) == ()
    assert state.inflight == (InFlightFix(issue, frozenset({"src/x.py"})),)


def test_a_raise_path_death_is_one_machine_end_not_two(tmp_path):
    """Anti-double-count, driven through the REAL write side. A pre-result
    transport death the process SURVIVED is reclaimed AND emits a settled
    `transport_death` journal line; the backoff reads the run table only, so
    it sees exactly one machine end."""
    issue = 8
    gw = FakeGateway(issues={issue: IssueView(issue, title="t", body="b")})
    store = Store(state_dir=str(tmp_path))
    journal = Journal(os.path.join(str(tmp_path), "journal.jsonl"))
    class LaunchedDeath(RaisingRunner):
        async def run(self, task):
            store._workflow_db.register_process_lease(RunId(task.env["ISSUE_AGENT_RUN_ID"]),
                expected_owner_id="o/r:fix:fixture", pid=123, pgid=123, process_start_ticks=99)
            return await super().run(task)
    runner = JournalingRuntime(LaunchedDeath(), journal, repo="o/r", role="fix")
    d = _dispatcher(gw, store, runner)
    world = WorldState(issues=(IssueView(issue, title="t", body="b", labels=frozenset({Label.AI_READY})),))

    with pytest.raises(RuntimeError):
        asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, issue)))

    events = read_events(journal.path)
    assert [e["event"] for e in events] == ["agent_run_start", "agent_run"]
    assert events[1]["exit_reason"] == "transport_death"
    assert [end.machine for end in _fix_state(store, world).leg_ends[issue]] == [True]


# ── steward: the third role P0R-3 names, bounded on the SAME two mechanisms ────
# `triage-N.at` (and the jira phase) are written ONLY on success, so a session
# that dies leaves the issue in `triage_candidates` forever — one paid TRIAGE
# per untriaged issue per poll, which in aggregate is larger than the reviewer's
# loop. Driven through the REAL `build_steward_daemon` closure, not a
# re-implementation of its gate: a test that restates the implementation cannot
# fail when the implementation is deleted.
def _charge_steward_infra(store, number, *, reclaim=False):
    from issue_agent.domain import continuation_components as cc
    from issue_agent.domain.workflow_identity import WorkflowKey, IssueRef
    from issue_agent.domain.contracts import WorkflowAttemptIdentity
    from tests.adapters.test_continuation_admission import money
    from tests.adapters.test_workflow_admission import request
    control = store._steward_control
    selected = control.read_steward(WorkflowKey("o/r", IssueRef(number)))
    leg = f"triage:{number}:{selected.revision.version}"
    purchase = cc.StewardPurchase(cc.ContinuationCapture(selected.binding, selected.revision, leg),
        cc.StewardLane.TRIAGE, _triaged(number).issues[0].updated_at, 3, infra_ceiling=5)
    paid = replace(request(leg), workflow=selected.binding, owner_id=store._steward_owner, kind="TRIAGE",
        dispatch_id=json.dumps({"repository":"o/r", "role":"steward", "issue":number, "number":number,
                               "kind":"TRIAGE", "leg_id":leg}))
    assert control.reserve_steward(paid, purchase).admitted
    if reclaim:
        control.register_process_lease(paid.run_id, expected_owner_id=store._steward_owner,
            pid=123, pgid=123, process_start_ticks=99)
        assert control.reclaim_dead_leases(store._steward_owner, lambda pid, start: False) == (paid.run_id,)
    else:
        assert control.settle(money(paid.run_id)).disposition is AccountingDisposition.AVAILABLE
        source = control.steward_admission(WorkflowAttemptIdentity(selected.binding, paid.run_id, leg))
        assert control.record_steward_outcome(cc.StewardFailed(source, cc.StewardFailureKind.INFRASTRUCTURE))


def _steward_decide(
    tmp_path, monkeypatch, *, process_deaths: tuple[int, ...] = (), **cfg_kw
):
    from issue_agent.app import roles
    from issue_agent.app.config import HarnessConfig

    store = Store(state_dir=str(tmp_path))
    # Pin the journal beside this store (production does exactly this; the
    # HarnessConfig default would point at the real XDG state dir).
    monkeypatch.setattr(
        roles, "_journal",
        lambda _s: Journal(os.path.join(str(tmp_path), "journal.jsonl")),
    )
    control = SQLiteControlStore(str(tmp_path / "control.db"))
    control.initialize()
    from tests.fakes.accounted_steward import install_steward
    from tests.adapters.test_steward_delivery import bind_events
    bind_events(control)
    install_steward(control, (30, 31))
    object.__setattr__(store, "_steward_control", control)
    cfg = HarnessConfig(repo="o/r", target_dir=str(tmp_path), **cfg_kw)
    owner_id = roles._owner_identity(cfg, "steward")
    object.__setattr__(store, "_steward_owner", owner_id)
    for target in process_deaths:
        _charge_steward_infra(store, target, reclaim=True)
    monkeypatch.setattr(roles, "_control_store", lambda *_args: control)
    gw = FakeGateway()
    gw.build_triage_world = lambda: WorldState()   # observe is never called here
    gw.build_steward_world = lambda: WorldState()
    daemon = roles.build_steward_daemon(cfg, gw, store, FakeRunner(AgentResult(is_error=False)))

    def decide(world):
        actions = daemon.decide(world)
        for action in actions:
            if (
                isinstance(action, RunAgent)
                and daemon.dispatch_rejected is not None
            ):
                daemon.dispatch_rejected(action)
        return actions

    return decide, store


def _triaged(number=30):
    return WorldState(issues=(IssueView(number, title="t", body="b", updated_at="2026-09-14T10:00:00+00:00"),))


def test_blocked_steward_component_holds_only_its_issue(tmp_path, monkeypatch):
    decide, store = _steward_decide(tmp_path, monkeypatch)
    from issue_agent.domain.continuation_components import BlockedSteward, ContinuationImportBlocker
    from issue_agent.domain.workflow_identity import WorkflowKey, IssueRef, FamilyAuthority, WorkflowFamily
    control = store._steward_control
    read = control.read_steward
    blocked = BlockedSteward(WorkflowKey("o/r", IssueRef(30)), FamilyAuthority(WorkflowFamily.STEWARD, 1),
        "fixture-blocked", (ContinuationImportBlocker("issue-30.steward-stalls-triage", "unknown counter"),))
    monkeypatch.setattr(control, "read_steward", lambda key: blocked if key.target.number == 30 else read(key))
    world = WorldState(issues=(_triaged(30).issues[0], _triaged(31).issues[0]))
    assert decide(world) == [RunAgent(AgentKind.TRIAGE, 31)]


def test_the_steward_stops_dispatching_for_an_issue_whose_sessions_keep_dying(
    tmp_path, monkeypatch
):
    decide, store = _steward_decide(tmp_path, monkeypatch)
    world = _triaged()

    for _ in range(5):
        assert decide(world) == [RunAgent(AgentKind.TRIAGE, 30)]  # anti-vacuity
        _charge_steward_infra(store, 30)

    assert decide(world) == [], "5 dead sessions must stop the paid loop"


def test_the_steward_still_triages_a_healthy_issue(tmp_path, monkeypatch):
    """Anti-vacuity twin: a steward that suppressed everything would look
    identical to a bounded one on the test above."""
    decide, store = _steward_decide(tmp_path, monkeypatch)
    assert decide(_triaged()) == [RunAgent(AgentKind.TRIAGE, 30)]
    from issue_agent.adapters.workflow_rows import load_issue_aggregate, persist_workflow_snapshot
    from issue_agent.domain.workflow_identity import WorkflowKey, IssueRef
    with store._steward_control.transaction() as db:
        aggregate = load_issue_aggregate(db, WorkflowKey("o/r", IssueRef(30)))
        component = aggregate.steward
        persist_workflow_snapshot(db, replace(aggregate, steward=replace(component,
            state=replace(component.state, triage=replace(component.state.triage, baseline_at=_triaged().issues[0].updated_at)))))
    assert decide(_triaged()) == [], "unchanged since last triage — not the ceiling"


def test_jira_legacy_death_gate_preserves_its_own_history(tmp_path, monkeypatch):
    from issue_agent.app import roles
    from issue_agent.adapters.runtime_plan import McpServer
    from issue_agent.domain.contracts import RunId
    monkeypatch.setattr(roles, "mcp_servers_from_file", lambda *args, **kwargs:
        (McpServer(name="atlassian", config={"type": "http"}, tools="mcp__atlassian__*"),))
    decide, store = _steward_decide(tmp_path, monkeypatch, steward_triage=False,
        jira_sync=True, jira_project_key="ABC", max_infra_deaths=2)
    world = replace(_triaged(), issues=(replace(_triaged().issues[0], labels=frozenset({Label.AI_READY})),))
    assert decide(world) == [RunAgent(AgentKind.JIRA_SYNC, 30)]
    Path(store.issue_path(30, "infra-deaths")).write_text("1")
    assert decide(world) == [RunAgent(AgentKind.JIRA_SYNC, 30)]
    control = store._steward_control
    control.start_run(RunId("legacy-jira-death"), json.dumps({"breaker_key": 30}),
        "JIRA_SYNC", store._steward_owner, "fixture", state="abandoned")
    assert decide(world) == []
    assert control.abandoned_dispatch_counts(store._steward_owner) == {30: 1}


def test_modern_steward_reclaim_does_not_feed_jira_legacy_gate(tmp_path, monkeypatch):
    from issue_agent.domain.contracts import RunId
    from issue_agent.domain.workflow_identity import WorkflowKey, IssueRef
    from tests.fakes.accounted_steward import install_steward
    from tests.app.test_steward_component_workflow import issue
    daemon = _steward_daemon(tmp_path, MAX_INFRA_DEATHS="3")
    dispatcher = _steward_dispatcher(daemon)
    accounting = dispatcher.runner.inner
    control = accounting.store
    install_steward(control, (7,))
    dispatcher.steward_workflow.gateway = FakeGateway(issues={7: issue()})
    assert control.claim_dispatch("o/r", "steward", 7, "TRIAGE", accounting.owner_id, frozenset())
    async def die(task):
        control.register_process_lease(RunId(task.env["ISSUE_AGENT_RUN_ID"]),
            expected_owner_id=accounting.owner_id, pid=123, pgid=123, process_start_ticks=99)
        assert control.reclaim_dead_leases(accounting.owner_id, lambda *_: False)
        raise RuntimeError("exceeded maximum buffer size")
    monkeypatch.setattr(accounting.inner, "run", die)
    with pytest.raises(RuntimeError):
        asyncio.run(dispatcher.steward_workflow.triage(7))
    assert control.read_steward(WorkflowKey("o/r", IssueRef(7))).component.state.triage.infra_failures == 3
    assert control.abandoned_dispatch_counts(accounting.owner_id) == {}


def test_the_stewards_ceiling_also_sees_a_process_death(tmp_path, monkeypatch):
    """Control-store abandonments bound the paid loop after daemon death."""
    decide, store = _steward_decide(
        tmp_path, monkeypatch, process_deaths=(30, 30, 30, 30, 30)
    )

    assert store.get_infra_deaths(30) == 0, "no write survived the kills"
    assert decide(_triaged()) == []


def test_the_steward_suppression_is_per_issue(tmp_path, monkeypatch):
    decide, store = _steward_decide(tmp_path, monkeypatch)
    for _ in range(5):
        _charge_steward_infra(store, 30)
    world = WorldState(issues=(
        IssueView(30, title="t", body="b", updated_at="2026-09-14T10:00:00+00:00"),
        IssueView(31, title="t", body="b", updated_at="2026-09-14T10:00:00+00:00"),
    ))

    assert decide(world) == [RunAgent(AgentKind.TRIAGE, 31)]


# ── infra-death key split (review P1R-1) ───────────────────────────────────────
def test_issueless_pr_has_no_invented_issue_machine_history(tmp_path):
    store = Store(str(tmp_path))
    pr = observed_pr(100, head_ref_name="chore/no-issue-here", closing_issue=None,
        checks=(Check("unit", bucket="fail"),), labels=frozenset({Label.AI_PR_OPEN}), author=FIX_BOT)
    w = WorldState(prs=(pr,))
    Path(store.issue_path(-100, "infra-deaths")).write_text("1")
    state = _fix_state(store, w)
    assert -100 not in state.leg_ends
    assert not _has_run(decide_fix(w, _cfg(), state, False), AgentKind.FIX_CI)


# ── hold reporting (harness #6) ───────────────────────────────────────────────
def _held_world(pr_state="OPEN"):
    """One ai-ready issue whose declared path collides with a PR's changed
    files. `pr_state="MERGED"` is the same world after the hold RELEASES."""
    return WorldState(
        issues=(
            IssueView(522, title="t", body="b", labels=frozenset({Label.AI_READY}),
                      declared_paths=frozenset({"backend/app/x.py"})),
        ),
        prs=(
            observed_pr(123, head_ref_name="fix/9-x", state=pr_state, author=FIX_BOT,
                   changed_files=frozenset({"backend/app/x.py"})),
        ),
    )


def _holds_in(journal):
    return [e for e in read_events(journal.path) if e.get("event") == "deferred"]


def test_a_defer_is_journaled_and_logged_only_when_it_begins(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    logs = []
    report = _hold_reporter(journal, "o/r", logs.append)

    for _ in range(3):
        report(_held_world())

    events = _holds_in(journal)
    assert events == [{
        "ts": events[0]["ts"], "event": "deferred", "state": "waiting",
        "issue": 522, "reason": "overlap", "blocker_pr": 123,
        "blocker_issue": None,
    }]
    assert logs == ["DEFERRED: issue #522 waiting on PR #123; dispatches once that clears"]


def test_releasing_a_defer_emits_one_clear_then_allows_a_new_begin(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    logs = []
    report = _hold_reporter(journal, "o/r", logs.append)

    report(_held_world())
    report(_held_world("MERGED"))   # released
    report(_held_world())           # same (issue, reason, blocker) again

    assert [event["state"] for event in _holds_in(journal)] == [
        "waiting", "cleared", "waiting",
    ]
    assert logs[1] == "issue #522 no longer waiting on PR #123; eligible for dispatch"


def test_restart_clears_a_persisted_hold_that_is_no_longer_active(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    control = _control(tmp_path)
    control.replace_level_state(
        "holds",
        "o/r",
        {
            "522:overlap:123:": {
                "issue": 522,
                "reason": "overlap",
                "blocker_pr": 123,
                "blocker_issue": None,
            }
        },
    )
    logs = []

    _hold_reporter(
        journal, "o/r", logs.append, control=control
    )(_held_world("MERGED"))

    assert _holds_in(journal)[-1]["state"] == "cleared"
    assert logs == [
        "issue #522 no longer waiting on PR #123; eligible for dispatch"
    ]


def test_router_predictions_are_injected_before_the_overlap_decision(tmp_path):
    store = Store(str(tmp_path))
    store.save_predicted_paths(522, frozenset({"backend/app/x.py"}))

    enriched = _inject_predicted_paths(store, _held_world())

    assert enriched.issues[0].predicted_paths == frozenset({"backend/app/x.py"})


def test_fix_daemon_reports_a_pending_repair_hold(tmp_path, monkeypatch):
    from issue_agent.app.config import HarnessConfig
    from issue_agent.app.roles import compose_fix_daemon
    from issue_agent.domain.actions import NoOp

    reason = "rebase #1019 held: waiting for the current workflow's durable rebase return"
    monkeypatch.setattr("issue_agent.app.roles.decide_fix", lambda *_args, **_kwargs: [NoOp(reason)])
    logs = []

    class QuietGateway(FakeGateway):
        def build_world(self):
            return WorldState()

    daemon = compose_fix_daemon(
        HarnessConfig(repo="o/r", fix_bot=FIX_BOT, target_dir=str(tmp_path),
                      state_dir=str(tmp_path / "state")),
        QuietGateway(), Store(str(tmp_path / "state")), FakeWorktrees(),
        FakeRunner(AgentResult(is_error=False)), log=logs.append,
    ).daemon

    daemon.tick()

    assert reason in logs


@pytest.mark.parametrize("issue_state,pr_state,held", [
    ("closed", "CLOSED", False),
    ("closed", "MERGED", False),
    ("open", "CLOSED", True),
    ("closed", "OPEN", True),
    (None, "OPEN", True),
    (None, "MERGED", False),
    (None, "", False),
])
def test_fix_daemon_reports_workflow_holds_only_for_open_targets(
    tmp_path, issue_state, pr_state, held,
):
    from issue_agent.app.roles import compose_fix_daemon

    observed = WorldState(
        issues=() if issue_state is None else (IssueView(514, state=issue_state),),
        prs=(observed_pr(678, state=pr_state, closing_issue=514),),
        now="2026-10-11T00:00:00Z",
    )

    class Gateway(FakeGateway):
        def build_world(self):
            return observed

    logs = []
    daemon = compose_fix_daemon(
        HarnessConfig(repo="o/r", fix_bot=FIX_BOT, target_dir=str(tmp_path),
                      state_dir=str(tmp_path / "state"),
                      control_authority_root=str(tmp_path / "authority")),
        Gateway(issues=observed.issues, prs={678: observed.prs[0]}),
        Store(str(tmp_path / "state")), FakeWorktrees(),
        FakeRunner(AgentResult(is_error=False)), log=logs.append,
    ).daemon

    daemon.tick()

    reason = "workflow 514 held: CI issue recovery authority is inactive or migration blocked"
    assert (reason in logs) is held
    if not held:
        assert not any(" held:" in line for line in logs)


def test_fix_daemon_still_cleans_up_a_closed_target_without_reporting_a_hold(tmp_path):
    from issue_agent.app.roles import compose_fix_daemon

    closed = IssueView(514, state="closed", labels=frozenset({Label.AI_PR_OPEN}),
                       updated_at="2026-10-11T00:00:00Z")

    class Gateway(FakeGateway):
        def build_world(self):
            return WorldState(issues=tuple(self.issues.values()), now="2026-10-11T00:00:00Z")

    gateway = Gateway(issues=(closed,))
    worktrees = FakeWorktrees()
    logs = []
    composition = compose_fix_daemon(
        HarnessConfig(repo="o/r", fix_bot=FIX_BOT, target_dir=str(tmp_path),
                      state_dir=str(tmp_path / "state"),
                      control_authority_root=str(tmp_path / "authority")),
        gateway, Store(str(tmp_path / "state")), worktrees,
        FakeRunner(AgentResult(is_error=False)), log=logs.append,
    )
    install_workflow(composition.control, resolve_snapshot(514))

    actions = composition.daemon.tick()

    assert gateway.get_issue(514).labels == frozenset({Label.AI_DONE}), (logs, actions)
    assert not any(" held:" in line for line in logs)


def test_daemon_holds_predicted_overlap_before_every_resolve_dispatch(tmp_path):
    """Persisted router hints must cross the real daemon decision seam."""
    from issue_agent.app.config import HarnessConfig
    from issue_agent.app.roles import build_fix_daemon

    issue = 522
    world = WorldState(
        issues=(IssueView(issue, labels=frozenset({Label.AI_READY})),),
        prs=(observed_pr(123, state="OPEN", changed_files=frozenset({"backend/app/x.py"})),),
    )

    class PredictionOverlapGateway(FakeGateway):
        def build_world(self):
            return world

    store = Store(str(tmp_path / "state"))
    store.save_predicted_paths(issue, ["backend/app/x.py"])
    daemon = build_fix_daemon(
        HarnessConfig(
            repo="o/r", fix_bot=FIX_BOT, target_dir=str(tmp_path),
            state_dir=str(tmp_path / "state"),
        ),
        PredictionOverlapGateway(), store, FakeWorktrees(),
        FakeRunner(AgentResult(is_error=False)),
    )

    actions = [*daemon.tick(), *daemon.tick()]

    assert not any(
        isinstance(action, RunAgent)
        and action.kind is AgentKind.RESOLVE
        and action.number == issue
        for action in actions
    )


def test_reaper_ignores_a_leg_that_finishes_during_the_world_fetch(tmp_path):
    """The relabel race (keifu #18/#25 dual labels, 2026-08-02): a leg that
    completes while build_world is in flight releases its reservation, and the
    already-fetched world still shows the issue's ai-working label with no PR.
    A reservation snapshot taken after the fetch reads "nothing active" against
    that stale world, so the reaper requeued — and the next poll paid for — an
    issue that was never orphaned. Reservations sampled BEFORE the fetch must
    protect the issue for the tick that consumes that world."""
    import sqlite3

    from issue_agent.app.config import HarnessConfig
    from issue_agent.app.roles import build_fix_daemon

    release = threading.Event()
    runner_returned = threading.Event()

    class BlockingRunner:
        def __init__(self):
            self.tasks = []

        async def run(self, task):
            self.tasks.append(task)
            await asyncio.to_thread(release.wait)
            runner_returned.set()
            return AgentResult(is_error=False)

    state_dir = tmp_path / "state"

    def dispatch_claims_released() -> bool:
        # The in-memory reservation is dropped BEFORE its durable claim row
        # (DispatchPathReservations.finish), so an empty dispatch_claims table
        # proves the in-memory release the race needs has already happened.
        for path in state_dir.rglob("*"):
            if not path.is_file():
                continue
            try:
                with sqlite3.connect(path) as db:
                    count = db.execute(
                        "SELECT count(*) FROM dispatch_claims"
                    ).fetchone()[0]
                return count == 0
            except sqlite3.Error:
                continue
        return False

    ready = WorldState(issues=(
        IssueView(8, labels=frozenset({Label.AI_READY}),
                  updated_at="2026-08-02T00:00:00Z"),
    ))
    # What GitHub served while the leg was still running: claimed, no PR yet.
    stale = WorldState(issues=(
        IssueView(8, labels=frozenset({Label.AI_WORKING}),
                  updated_at="2026-08-02T00:10:00Z"),
    ))

    class RacingGateway(FakeGateway):
        def __init__(self):
            super().__init__(issues={8: ready.issues[0]})
            self.fetches = 0

        def build_world(self):
            self.fetches += 1
            if self.fetches == 1:
                return ready
            if self.fetches == 2:
                # The fetch is "in flight" and the leg completes under it.
                release.set()
                assert runner_returned.wait(20), "leg never completed"
                deadline = time.monotonic() + 20
                while not dispatch_claims_released():
                    assert time.monotonic() < deadline, "claim never released"
                    time.sleep(0.02)
                return stale
            return stale

    cfg = HarnessConfig(
        repo="o/r", fix_bot=FIX_BOT, target_dir=str(tmp_path),
        state_dir=str(state_dir),
    )
    control = SQLiteControlStore(_control_store_path(cfg, str(state_dir)))
    control.initialize()
    from tests.fakes.workflow_authority import with_issue_recovery
    install_workflow(control, with_issue_recovery(resolve_snapshot(8)))
    daemon = build_fix_daemon(
        cfg,
        RacingGateway(), Store(str(state_dir)), FakeWorktrees(),
        BlockingRunner(),
    )

    first = daemon.tick()
    assert any(isinstance(a, RunAgent) and a.number == 8 for a in first)

    second = daemon.tick()
    stale_repairs = [
        a for a in second
        if isinstance(a, Relabel) and a.number == 8
        and a.remove is Label.AI_WORKING
    ]
    assert stale_repairs == [], (
        "the reaper repaired an issue whose leg finished during the fetch"
    )
    assert not any(isinstance(a, RunAgent) and a.number == 8 for a in second)


def test_terminal_pr_journals_router_prediction_against_actual_changed_files(tmp_path):
    store = Store(str(tmp_path))
    store.save_predicted_paths(522, frozenset({"backend/app/x.py", "wrong.py"}))
    journal = Journal(str(tmp_path / "journal.jsonl"))
    terminal = WorldState(
        issues=(IssueView(522, labels=frozenset({Label.AI_PR_OPEN})),),
        prs=(observed_pr(123, state="MERGED", closing_issue=522,
                    changed_files=frozenset({"backend/app/x.py", "actual.py"})),),
    )

    report = _prediction_outcome_reporter(journal, "o/r", store, lambda _m: None)
    report(terminal)
    report(terminal)

    events = [e for e in read_events(journal.path) if e.get("event") == "path_prediction"]
    assert events == [{
        "ts": events[0]["ts"], "event": "path_prediction", "issue": 522,
        "pr": 123, "predicted_paths": ["backend/app/x.py", "wrong.py"],
        "changed_files": ["actual.py", "backend/app/x.py"],
        "matched_paths": ["backend/app/x.py"],
        "block_key": "path-prediction:o/r:522:123",
    }]


def test_a_second_blocker_is_its_own_hold(tmp_path):
    """Anti-vacuity for the dedup: it keys on the TUPLE, so a different blocking
    PR — a genuinely new fact — is still reported."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    report = _hold_reporter(journal, "o/r", lambda _m: None)

    report(_held_world())
    w = _held_world()
    report(replace(w, prs=(replace(w.prs[0], number=124),)))

    assert [
        e.get("blocker_pr")
        for e in _holds_in(journal)
        if e["state"] == "waiting"
    ] == [123, 124]


def test_the_hold_dedup_survives_a_daemon_restart(tmp_path):
    """The flood shape is a daemon restarting (hot reload / Restart=always), so
    an in-process set alone would re-emit every boot. A FRESH reporter over the
    same journal must stay silent."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    control = _control(tmp_path)
    _hold_reporter(
        journal, "o/r", lambda _m: None, control=control
    )(_held_world())
    _hold_reporter(
        journal, "o/r", lambda _m: None, control=control
    )(_held_world())

    assert len(_holds_in(journal)) == 1


# ── CI pending-deadline reporting (#30) ──────────────────────────────────────
_STALL_CFG = FixConfig(fix_bot=FIX_BOT, ci_pending_max_seconds=3600)
_EMPTY_FIX_STATE = FixState()


def _stalled_world(head="sha-aaa", elapsed=3601, started="2026-07-28T12:00:00Z"):
    """One fixer PR with a check wedged IN_PROGRESS past the deadline."""
    return WorldState(
        prs=(
            observed_pr(144, head_ref_name="fix/9-x", head_ref_oid=head, state="OPEN",
                   author=FIX_BOT, labels=frozenset({Label.AI_PR_OPEN}),
                   checks=(Check("Test (windows-latest)", state="IN_PROGRESS",
                                 started_at=started),)),
        ),
        now=(
            datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)
            + timedelta(seconds=elapsed)
        ).isoformat(),
    )


def _stalls_in(journal):
    return [
        e for e in read_events(journal.path) if e.get("event") == "ci_pending_deadline"
    ]


def _steps_in(journal):
    return [
        e for e in read_events(journal.path) if e.get("event") == "ci_stall_step"
    ]


def test_a_wedged_check_is_journaled_once_and_logged_every_poll(tmp_path):
    """AC: the deadlock stops being invisible. The journal carries the PR, the
    head, the elapsed seconds and the pinning check names — enough to compute an
    escalation rate and to go cancel the job — once per stuck run, while the log
    keeps answering "why is PR #144 not moving?" on every poll."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    logs = []
    report = _ci_stall_reporter(journal, "o/r", _STALL_CFG, logs.append)

    for _ in range(3):
        report(_stalled_world(), _EMPTY_FIX_STATE)

    assert _stalls_in(journal) == [
        {
            "ts": _stalls_in(journal)[0]["ts"], "event": "ci_pending_deadline",
            "pr": 144, "issue": 9, "head": "sha-aaa", "seconds": 3601,
            "checks": ["Test (windows-latest)"],
            # The remediation STEP is part of the key: this PR's pending check
            # is an external status with no workflow-run identity, so the only
            # step available is the human escalation.
            "block_key": "ci-pending:o/r:144:sha-aaa:escalate",
        }
    ]
    assert len(logs) == 3 and all("#144" in ln for ln in logs)


def test_a_healthy_pending_pr_is_neither_logged_nor_journaled(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    logs = []
    _ci_stall_reporter(journal, "o/r", _STALL_CFG, logs.append)(
        _stalled_world(elapsed=23 * 60), _EMPTY_FIX_STATE
    )
    assert _stalls_in(journal) == [] and logs == []


def test_a_new_head_wedging_again_is_its_own_event(tmp_path):
    """Anti-vacuity for the dedup: it keys on the head SHA, so a push that
    produces a fresh check run which ALSO wedges is a genuinely new fact."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    report = _ci_stall_reporter(journal, "o/r", _STALL_CFG, lambda _m: None)

    report(_stalled_world(head="sha-aaa"), _EMPTY_FIX_STATE)
    report(_stalled_world(head="sha-bbb"), _EMPTY_FIX_STATE)

    assert [e["head"] for e in _stalls_in(journal)] == ["sha-aaa", "sha-bbb"]


def test_the_stall_dedup_survives_a_daemon_restart(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    control = _control(tmp_path)
    _ci_stall_reporter(
        journal, "o/r", _STALL_CFG, lambda _m: None, control
    )(_stalled_world(), _EMPTY_FIX_STATE)
    _ci_stall_reporter(
        journal, "o/r", _STALL_CFG, lambda _m: None, control
    )(_stalled_world(), _EMPTY_FIX_STATE)

    assert len(_stalls_in(journal)) == 1


def _wedged_run_world(head="sha-aaa", elapsed=3601):
    """A stalled PR whose pinning check carries a workflow-run identity, so the
    harness can cancel it instead of handing it to a human (keifu PR #177)."""
    w = _stalled_world(head=head, elapsed=elapsed)
    pr = w.prs[0]
    return replace(
        w, prs=(with_checks(pr, checks=(replace(pr.checks[0], run_id=4242),)),)
    )


def _cancelled_state(at="2026-07-28T13:00:01+00:00"):
    return FixState(ci_cancel_head={144: f"sha-aaa@{at}"})


def test_each_remediation_step_is_journaled_once_with_its_correlation(tmp_path):
    """ADR 0012: the cancel, the wait it opens and the escalation that bounds it
    are three facts about one head, each journaled once — a wait that recurs
    every poll would be noise, and a wait nothing records is invisible."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    report = _ci_stall_reporter(journal, "o/r", _STALL_CFG, lambda _m: None)

    for _ in range(3):
        report(_wedged_run_world(), _EMPTY_FIX_STATE)
    for _ in range(3):
        report(_wedged_run_world(elapsed=3700), _cancelled_state())
    report(_wedged_run_world(elapsed=7201), _cancelled_state())

    assert [(e["step"], e["block_key"]) for e in _steps_in(journal)] == [
        ("cancel", "ci-pending:o/r:144:sha-aaa:cancel"),
        ("await_cancel", "ci-pending:o/r:144:sha-aaa:await_cancel"),
    ]
    # Correlation carries both ends of the incident, not just the PR.
    assert all(
        (e["pr"], e["issue"]) == (144, 9)
        for e in _steps_in(journal) + _stalls_in(journal)
    )


def test_a_wedge_the_harness_heals_never_counts_as_an_escalation(tmp_path):
    """`ci_pending_deadline` is the escalation-rate guardrail. A rate counted
    from steps that hand nothing to a human is not a rate — the change would
    appear to worsen the guardrail it exists to protect."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    report = _ci_stall_reporter(journal, "o/r", _STALL_CFG, lambda _m: None)

    report(_wedged_run_world(), _EMPTY_FIX_STATE)
    report(_wedged_run_world(elapsed=3700), _cancelled_state())

    assert _stalls_in(journal) == []
    assert [e["step"] for e in _steps_in(journal)] == ["cancel", "await_cancel"]

    # …and the step that DOES reach a human still counts.
    report(_wedged_run_world(elapsed=7201), _cancelled_state())
    assert [e["head"] for e in _stalls_in(journal)] == ["sha-aaa"]


def test_the_journal_agrees_with_the_cancel_it_explains(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    w = _wedged_run_world()
    _ci_stall_reporter(journal, "o/r", _STALL_CFG, lambda _m: None)(
        w, _EMPTY_FIX_STATE
    )

    cancelled = {
        a.pr for a in decide_fix(w, _STALL_CFG, _EMPTY_FIX_STATE, fixer_busy=False)
        if isinstance(a, CancelWorkflowRuns)
    }
    assert cancelled == {e["pr"] for e in _steps_in(journal)} == {144}


def _green_after_cancel_world(head="sha-aaa"):
    """Current Clear CI must obsolete an earlier cancellation marker."""
    w = _stalled_world(head=head)
    pr = w.prs[0]
    return replace(
        w,
        prs=(
            with_checks(
                pr,
                checks=(
                    replace(
                        pr.checks[0], state="COMPLETED", bucket="pass", run_id=4242
                    ),
                ),
            ),
        ),
    )


@pytest.mark.parametrize(
    "world_builder,state_builder,expected",
    [
        (_stalled_world, lambda: _EMPTY_FIX_STATE, {144}),
        (_green_after_cancel_world, _cancelled_state, {144}),
    ],
    ids=["pending-deadline", "cancel-aftermath"],
)
def test_the_journal_agrees_with_the_escalation_it_explains(
    tmp_path, world_builder, state_builder, expected
):
    """The two halves read the SAME derivation, so a journaled stall is always a
    PR decide_fix escalates in that very poll — never a line about a PR nothing
    acted on, nor an escalation with no record.

    Empty-policy green after cancellation still requires operator attention.
    """
    journal = Journal(str(tmp_path / "journal.jsonl"))
    w = world_builder()
    state = state_builder()
    _ci_stall_reporter(journal, "o/r", _STALL_CFG, lambda _m: None)(w, state)

    escalated = {
        a.pr for a in decide_fix(w, _STALL_CFG, state, fixer_busy=False)
        if isinstance(a, EscalateCiPending)
    }
    assert escalated == {e["pr"] for e in _stalls_in(journal)} == expected


def test_hold_reporting_without_a_journal_still_logs(tmp_path):
    """Observability must never add a failure mode: no journal → log only."""
    logs = []
    _hold_reporter(None, "o/r", logs.append)(_held_world())
    assert len(logs) == 1


def test_a_journaled_hold_explains_the_issue_decide_fix_actually_withheld(tmp_path):
    """The two halves must agree: the issue named in the journal is exactly the
    one `decide_fix` refuses to dispatch this poll."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    w = _held_world()
    _hold_reporter(journal, "o/r", lambda _m: None)(w)

    actions = decide_fix(w, _cfg(), _fix_state(Store(str(tmp_path)), w), fixer_busy=False)
    assert not _has_run(actions, AgentKind.RESOLVE)
    assert [e["issue"] for e in _holds_in(journal)] == [522]


# ── reopen recurrences (harness #10) ─────────────────────────────────────────
class _ReopenGateway:
    """The two recurrence reads, scripted. Records every state-reason lookup so a
    test can assert the steady state costs nothing."""

    def __init__(self, reasons=None, merged=(), merges_raise=False):
        self.reasons = reasons or {}
        self.merged = merged
        self.merges_raise = merges_raise
        self.asked = []

    def issue_state_reason(self, number):
        self.asked.append(number)
        return self.reasons.get(number, "")

    def recently_merged_issues(self, author, limit=30):
        if self.merges_raise:
            raise RuntimeError("gh boom")
        return self.merged


class _Merged:
    def __init__(self, issue, pr):
        self.issue, self.pr = issue, pr


def _resolved_world(*, state="open", label=Label.AI_DONE):
    return WorldState(issues=(
        IssueView(522, title="t", body="b", labels=frozenset({label}), state=state),
    ))


def _recurrences_in(journal):
    return [e for e in read_events(journal.path) if e.get("event") == "recurrence"]


def test_a_reopened_resolved_issue_is_journaled_once_and_logged_every_poll(tmp_path):
    """AC: a reopened harness-resolved issue journals EXACTLY one recurrence
    event, however many polls the reopen survives — while the log keeps saying so
    every pass."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    gw = _ReopenGateway(reasons={522: "reopened"}, merged=(_Merged(522, 300),))
    logs = []
    report = _recurrence_reporter(journal, gw, "o/r", "fix-bot", logs.append)

    report(_resolved_world())
    report(_resolved_world())

    assert _recurrences_in(journal) == [
        {
            "ts": _recurrences_in(journal)[0]["ts"], "event": "recurrence",
            "kind": "reopen", "issue": 522, "of_issue": 522, "pr": 300,
            "block_key": "recurrence:o/r:522:reopen",
        }
    ]
    assert len(logs) == 2
    assert all("#522" in ln and "reopened" in ln for ln in logs)


def test_an_ai_done_issue_that_was_never_reopened_is_not_a_recurrence(tmp_path):
    """The harness merges to a non-default base and closes issues itself, so
    'ai-done and open' is ambiguous — only stateReason=reopened confirms it.
    Without this every ai-done issue on a close_issue_on_merge=off deployment
    would journal a recurrence."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    logs = []
    _recurrence_reporter(
        journal, _ReopenGateway(reasons={522: ""}), "o/r", "fix-bot", logs.append
    )(_resolved_world())

    assert _recurrences_in(journal) == [] and logs == []


def test_only_resolved_open_issues_cost_a_state_reason_call(tmp_path):
    """Zero extra gh calls in the steady state: a closed ai-done issue and an
    open issue the harness never resolved are both never asked about."""
    gw = _ReopenGateway(reasons={522: "reopened"})
    report = _recurrence_reporter(None, gw, "o/r", "fix-bot", lambda _m: None)

    report(_resolved_world(state="closed"))
    report(_resolved_world(label=Label.AI_READY))

    assert gw.asked == []


def test_the_reopen_dedup_survives_a_daemon_restart(tmp_path):
    """The recurrence RATE is computed by counting these events, so a duplicate
    corrupts the metric — an in-process set alone would re-emit on every boot."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    gw = _ReopenGateway(reasons={522: "reopened"})
    control = _control(tmp_path)
    _recurrence_reporter(
        journal, gw, "o/r", "fix-bot", lambda _m: None, control
    )(_resolved_world())
    _recurrence_reporter(
        journal, gw, "o/r", "fix-bot", lambda _m: None, control
    )(_resolved_world())

    assert len(_recurrences_in(journal)) == 1


def test_reopen_reporting_without_a_journal_still_logs(tmp_path):
    """Observability must never add a failure mode: no journal → log only."""
    logs = []
    _recurrence_reporter(
        None, _ReopenGateway(reasons={522: "reopened"}), "o/r", "fix-bot", logs.append
    )(_resolved_world())
    assert len(logs) == 1


def test_an_unreadable_merged_list_still_records_the_reopen(tmp_path):
    """The PR number is enrichment. Losing it must not lose the recurrence."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    gw = _ReopenGateway(reasons={522: "reopened"}, merges_raise=True)
    _recurrence_reporter(journal, gw, "o/r", "fix-bot", lambda _m: None)(_resolved_world())

    assert [e["pr"] for e in _recurrences_in(journal)] == [0]


def test_a_gateway_without_the_recurrence_reads_is_inert(tmp_path):
    """A fake or legacy adapter must yield an inert reporter, not an
    AttributeError out of the steward's decide closure."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    _recurrence_reporter(journal, object(), "o/r", "fix-bot", lambda _m: None)(
        _resolved_world()
    )
    assert _recurrences_in(journal) == []


# ── FIXER_RUNTIME=glm wiring (build_fix_daemon) ──────────────────────────────
# The wrapping itself (env injection, task-env precedence) is proven in
# tests/adapters/test_claude_profile.py; what these pin is the BUILD-TIME
# contract of the glm branch — that a profile is resolved once at startup and
# that its outcome is announced in one operator-facing line either way. The
# composed runner is not reachable from the returned Daemon without reading a
# private attribute, so it is deliberately not asserted here.


class _BuildableGateway(FakeGateway):
    """FakeGateway plus the one member `build_fix_daemon` binds at build time."""

    def build_world(self):  # pragma: no cover - never ticked in these tests
        raise AssertionError("these tests build the daemon, they do not tick it")

    def build_review_world(self):  # pragma: no cover - never ticked in these tests
        raise AssertionError("these tests build the daemon, they do not tick it")


class _BuildableReviewGateway(_BuildableGateway):
    """Complete review reads include submissions made through the durable executor."""

    def pull_request_reviews(self, pr):
        from issue_agent.domain.review_workflow import (
            CompleteReviews, SubmittedReviewFact, SubmittedReviewState, body_review_command,
        )

        states = {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED", "COMMENT": "COMMENTED"}
        return CompleteReviews(tuple(
            SubmittedReviewFact(str(index), REVIEW_BOT, SubmittedReviewState(states[event]), head,
                                datetime(2026, 9, 27, tzinfo=timezone.utc), body_review_command(body))
            for index, (number, event, body, head) in enumerate(sorted(self.submitted_reviews)) if number == pr
        ))


def _activate_review_fixture(cfg, gateway=None):
    """Seed isolated family authority before composing a real review daemon."""
    from issue_agent.domain.workflow_identity import WorkflowFamily
    control = SQLiteControlStore(_control_store_path(cfg, cfg.review_state_dir))
    control.initialize()
    with control.transaction() as db:
        db.execute("INSERT INTO workflow_authority_epochs VALUES (?, ?, 1, 'fixture', 'fixture', ?)",
                   (cfg.repo, WorkflowFamily.REVIEW_REJECTIONS.value, datetime.now(timezone.utc).isoformat()))
    if gateway is not None:
        from issue_agent.app.review_execution_activation import (
            ConfiguredReviewExecutionActivation, ReviewExecutionActivationScope, _observed_scope_sources,
        )
        from issue_agent.app.review_runtime import configured_review_control
        from issue_agent.app.config import owner_identity_for_target
        from issue_agent.domain.workflow_identity import WorkflowKey, PullRequestRef
        workflow = configured_review_control(cfg, control, gateway, Store(cfg.review_state_dir),
                                            owner_identity_for_target(cfg.repo, cfg.target_dir, "review"))
        for pr in gateway.build_review_world().prs:
            workflow.ensure_observed_workflow(WorkflowKey(cfg.repo, PullRequestRef(pr.number)))
        gateway.repo = cfg.repo
        gateway.get_review_pull_request = gateway.get_pr
        gateway._gh_json = lambda _: [[{"number": pr.number, "state": "open"}
                                      for pr in gateway.build_review_world().prs]]
        scope = ReviewExecutionActivationScope("fixture", cfg, gateway)
        files, links, sql = _observed_scope_sources(scope, control)
        activation = ConfiguredReviewExecutionActivation(cfg, control, gateway, Store(cfg.review_state_dir), liveness=lambda: ())
        activation.activate(activation.plan(files, links, sql), files, links, sql)
    return control


def _glm_profile(tmp_path, settings: str) -> str:
    d = tmp_path / ".cc-glm"
    d.mkdir()
    (d / "settings.json").write_text(settings)
    return str(d / "settings.json")


def _role_state_env(tmp_path) -> dict[str, str]:
    return {
        "STATE_DIR": str(tmp_path / "state"),
        "REVIEW_STATE_DIR": str(tmp_path / "review-state"),
        "STEWARD_STATE_DIR": str(tmp_path / "steward-state"),
    }


def _glm_daemon(tmp_path, settings_file: str):
    from issue_agent.app.config import from_env
    from issue_agent.app.roles import build_fix_daemon

    cfg = replace(
        from_env({
            "REPO": "o/r", "FIX_BOT": FIX_BOT, "FIXER_RUNTIME": "glm",
            "GLM_SETTINGS_FILE": settings_file, **_role_state_env(tmp_path),
        }),
        control_authority_root=str(tmp_path / "machine-state"),
    )
    return build_fix_daemon(
        cfg, _BuildableGateway(), Store(str(tmp_path / "state")), FakeWorktrees(),
        FakeRunner(AgentResult(is_error=False)),
    )


def test_a_glm_profile_is_resolved_once_at_startup_and_announced(tmp_path, capsys):
    settings = json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic"}})

    daemon = _glm_daemon(tmp_path, _glm_profile(tmp_path, settings))

    line = capsys.readouterr().err
    assert "glm: runtime ready" in line
    assert "api.z.ai" in line              # endpoint host: useful, not secret
    assert "NOT READY" not in line
    assert daemon.name == "fix"


def test_a_missing_glm_profile_says_so_loudly_at_startup_instead_of_dying(tmp_path, capsys):
    """A misconfig must surface as ONE startup line, not as an opaque failure on
    the first dispatch — and the daemon must still be built (visibly wrong beats
    a crash-looping fixer)."""
    daemon = _glm_daemon(tmp_path, str(tmp_path / "absent" / "settings.json"))

    err = capsys.readouterr().err
    assert "glm: NOT READY at startup" in err
    assert "settings.json" in err
    assert daemon.name == "fix"


def test_a_malformed_glm_profile_is_not_ready_rather_than_a_traceback(tmp_path, capsys):
    daemon = _glm_daemon(tmp_path, _glm_profile(tmp_path, '{"env": {,,}'))

    assert "glm: NOT READY at startup" in capsys.readouterr().err
    assert daemon.name == "fix"


# ── boot capability lines (#46) ───────────────────────────────────────────────
# The log line IS the contract here: an operator watching three healthy daemons
# reasonably concludes three things are happening, and the steward is the role
# that can be configured into doing literally nothing while looking identical to
# the two that work. So these assert on the rendered text.
def test_a_steward_with_every_feature_off_says_it_can_do_nothing():
    from issue_agent.app.roles import steward_capability_line

    line = steward_capability_line(
        triage=False, promote=False, jira_sync=False, recurrence=True
    )

    assert "steward: capabilities: NONE" in line
    assert "no work it can do" in line
    # The knobs to change, named in the line that reports the problem.
    assert "STEWARD_TRIAGE=0" in line
    assert "STEWARD_PROMOTE=0" in line
    assert "JIRA_SYNC=0" in line


def test_a_steward_with_one_feature_on_reports_the_set_not_a_verdict():
    from issue_agent.app.roles import steward_capability_line

    line = steward_capability_line(
        triage=True, promote=False, jira_sync=False, recurrence=True
    )

    assert "NONE" not in line
    assert "triage=on" in line and "promote=off" in line and "jira_sync=off" in line


def test_recurrence_watching_is_reported_inert_without_a_fix_bot():
    from issue_agent.app.roles import steward_capability_line

    inert = steward_capability_line(
        triage=True, promote=False, jira_sync=False, recurrence=False
    )
    live = steward_capability_line(
        triage=True, promote=False, jira_sync=False, recurrence=True
    )

    assert "recurrence=inert" in inert and "FIX_BOT" in inert
    assert "recurrence=watch" in live


def test_the_fix_and_review_capability_lines_name_what_the_role_can_do():
    from issue_agent.app.roles import fix_capability_line, review_capability_line

    fix = fix_capability_line(
        auto_merge=False, auto_rebase=True, escalate_on_evidence=False,

    )
    review = review_capability_line(can_approve=False, adopt_unlabeled=True)

    assert "fix: capabilities:" in fix
    assert "auto_merge=off" in fix and "auto_rebase=on" in fix
    assert "escalate_on_evidence=off" in fix
    # approve=off is solo mode: the reviewer reviews but nothing it does merges.
    assert "review: capabilities:" in review
    assert "approve=off" in review and "adopt_unlabeled=on" in review


def test_the_steward_daemon_prints_its_capability_set_at_boot(
    tmp_path, monkeypatch, capsys
):
    """Through the REAL builder, not the pure helper: a capability line nothing
    emits is a line no operator ever sees."""
    _steward_decide(
        tmp_path, monkeypatch,
        steward_triage=False, steward_promote=False, jira_sync=False,
    )

    assert "steward: capabilities: NONE" in capsys.readouterr().err


def test_a_working_steward_does_not_claim_it_has_nothing_to_do(
    tmp_path, monkeypatch, capsys
):
    _steward_decide(tmp_path, monkeypatch, steward_triage=True)

    err = capsys.readouterr().err
    assert "steward: capabilities: triage=on" in err
    assert "capabilities: NONE" not in err


# ── #29: a codex fixer announces that MAX_BUDGET_USD bounds nothing ──────────


def _codex_daemon(tmp_path, **env):
    from issue_agent.app.config import from_env
    from issue_agent.app.roles import build_fix_daemon

    cfg = replace(
        from_env({
            "REPO": "o/r", "FIX_BOT": FIX_BOT, "FIXER_RUNTIME": "codex",
            **_role_state_env(tmp_path), "MAX_BUDGET_USD": "20",
            "AGENT_MAX_RUNTIME_SECONDS": "3600", **env,
        }),
        control_authority_root=str(tmp_path / "machine-state"),
    )
    return build_fix_daemon(
        cfg, _BuildableGateway(), Store(str(tmp_path / "state")), FakeWorktrees(),
        FakeRunner(AgentResult(is_error=False)),
    )


def test_a_codex_fixer_warns_that_the_per_session_budget_is_not_enforced(tmp_path, capsys):
    """It fired for nobody before: the clause lived inside the blindspot
    warning, which goes silent as soon as pricing is configured — so the
    operators who set up pricing correctly were the ones who never saw it."""
    _codex_daemon(tmp_path)  # shipped (non-zero) rates → no blindspot warning

    err = capsys.readouterr().err
    assert "CODEX_COST_PER_MTOK_IN/OUT are 0" not in err, "pricing IS configured"
    assert "NOT ENFORCED on this runtime" in err
    assert "MAX_BUDGET_USD=$20.00" in err


def test_codex_reviewer_uses_the_review_budget_and_model(tmp_path, monkeypatch):
    """A codex review leg must use the same runner seam as fixes while carrying
    its own per-review cap and model into the task-producing dispatcher."""
    from issue_agent.adapters import codex_runner
    from issue_agent.app.config import from_env
    from issue_agent.app.roles import build_review_daemon

    seen = {}

    class CapturingCodexRunner:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def preflight(self):
            return None

        def oauth_auth_file(self):
            return ""

    monkeypatch.setattr(codex_runner, "CodexRunner", CapturingCodexRunner)
    cfg = replace(
        from_env({
            "REPO": "o/r", "FIX_BOT": FIX_BOT, "REVIEWER_RUNTIME": "codex",
            "REVIEW_MODEL": "gpt-5.6-terra", "REVIEW_BUDGET_USD": "2.5",
            **_role_state_env(tmp_path),
            "TARGET_DIR": str(tmp_path),
        }),
        control_authority_root=str(tmp_path / "machine-state"),
    )

    daemon = build_review_daemon(
        cfg, _BuildableGateway(), Store(str(tmp_path / "review-state")),
        FakeRunner(AgentResult(is_error=False)),
    )

    dispatcher = daemon.executor._run_agent._dispatch.__self__
    runtime = dispatcher.runner.inner
    assert runtime.__class__.__name__ == "AccountingRuntime"
    runtime = runtime.inner
    assert runtime.__class__.__name__ == "HookPreparingRuntime"
    assert isinstance(runtime.inner, CapturingCodexRunner)
    assert seen["default_model"] == "gpt-5.6-terra"
    assert seen["enforce_budget"] is True
    assert seen["sandbox"] == "read-only"
    assert seen["resume_on_retry"] is False
    assert dispatcher.review_workflow.cfg.execution_cwd == str(tmp_path)
    assert callable(dispatcher.review_workflow.cfg.prepare_cwd)
    # #113: the leg carries its own role, so the governor never has to infer it
    # from a hook file that a differently-roled leg may also resolve to.
    assert seen["env"]["ISSUE_AGENT_LEG_ROLE"] == "review"


# ── STEWARD_RUNTIME wiring (build_steward_daemon) ────────────────────────────


def _steward_daemon(tmp_path, **env):
    from issue_agent.app.config import from_env
    from issue_agent.app.roles import build_steward_daemon

    cfg = replace(
        from_env({
            "REPO": "o/r", "FIX_BOT": FIX_BOT, "STEWARD_BOT": "steward[bot]",
            **_role_state_env(tmp_path), **env,
        }),
        control_authority_root=str(tmp_path / "machine-state"),
    )
    gateway = _BuildableGateway()
    gateway.build_triage_world = lambda: WorldState()
    gateway.build_steward_world = lambda: WorldState()
    return build_steward_daemon(
        cfg, gateway, Store(str(tmp_path / "steward-state")),
        FakeRunner(AgentResult(is_error=False)),
    )


def _steward_dispatcher(daemon):
    return daemon.executor._run_agent._dispatch.__self__


def test_the_steward_runs_on_claude_when_no_runtime_is_selected(tmp_path):
    """Back-compat: an overlay that never heard of STEWARD_RUNTIME must keep
    dispatching triage on the injected Claude runner, priced as claude."""
    daemon = _steward_daemon(tmp_path, STEWARD_GROOM="1")

    dispatcher = _steward_dispatcher(daemon)
    assert dispatcher.runner.runtime == "claude"
    accounting = dispatcher.runner.inner
    assert accounting.__class__.__name__ == "AccountingRuntime"
    assert accounting.runtime_name == "claude"
    assert isinstance(accounting.inner, FakeRunner), (
        "the Claude steward binds the injected SDK runner unchanged"
    )
    # Claude keeps its alias models and its historical implicit cwd.
    assert dispatcher.cfg.triage.model == "sonnet"
    assert dispatcher.cfg.groom.model == "sonnet"
    assert dispatcher.cfg.triage.cwd is None


def test_steward_runtime_codex_dispatches_triage_and_groom_on_codex(
    tmp_path, monkeypatch
):
    """STEWARD_RUNTIME=codex must move the paid triage/groom legs onto the
    Codex adapter with read-only containment, and must still price and journal
    them as codex — otherwise the spend projection attributes ChatGPT-auth legs
    to the Anthropic rate card."""
    from issue_agent.adapters import codex_runner

    seen = {}

    class CapturingCodexRunner:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def preflight(self):
            return None

        def oauth_auth_file(self):
            return ""

    monkeypatch.setattr(codex_runner, "CodexRunner", CapturingCodexRunner)
    daemon = _steward_daemon(
        tmp_path,
        STEWARD_RUNTIME="codex",
        STEWARD_GROOM="1",
        TARGET_DIR=str(tmp_path),
    )

    dispatcher = _steward_dispatcher(daemon)
    assert dispatcher.runner.runtime == "codex"
    accounting = dispatcher.runner.inner
    assert accounting.__class__.__name__ == "AccountingRuntime"
    assert accounting.runtime_name == "codex"
    # The synthetic ChatGPT-auth rate card, not the Claude one.
    assert accounting.rate_table.rate_card_id == "codex-config"
    contained = accounting.inner
    assert contained.__class__.__name__ == "HookPreparingRuntime"
    assert isinstance(contained.inner, CapturingCodexRunner)
    # Triage/groom read and classify; they never edit.
    assert seen["sandbox"] == "read-only"
    assert seen["resume_on_retry"] is False
    assert seen["env"]["ISSUE_AGENT_LEG_ROLE"] == "steward"
    # Codex containment refuses a plan with no cwd, so both legs carry one.
    assert dispatcher.cfg.triage.cwd == str(tmp_path)
    assert dispatcher.cfg.groom.cwd == str(tmp_path)


def test_a_codex_steward_never_sends_a_claude_model_alias(tmp_path, monkeypatch):
    """`sonnet` is an Anthropic alias; Codex rejects it outright. An unset
    STEWARD_TRIAGE_MODEL under codex must resolve to the Codex runner's own
    default instead of leaking the Claude-flavoured default through."""
    from issue_agent.adapters import codex_runner

    seen = {}

    class CapturingCodexRunner:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def preflight(self):
            return None

        def oauth_auth_file(self):
            return ""

    monkeypatch.setattr(codex_runner, "CodexRunner", CapturingCodexRunner)
    daemon = _steward_daemon(
        tmp_path, STEWARD_RUNTIME="codex", STEWARD_GROOM="1",
        TARGET_DIR=str(tmp_path),
    )

    dispatcher = _steward_dispatcher(daemon)
    assert dispatcher.cfg.triage.model is None
    assert dispatcher.cfg.groom.model is None
    assert seen.get("default_model", "") != "sonnet"


def test_an_explicit_triage_model_reaches_the_codex_steward(tmp_path, monkeypatch):
    """Anti-vacuity twin: the fallback must not swallow an operator's choice."""
    from issue_agent.adapters import codex_runner

    seen = {}

    class CapturingCodexRunner:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def preflight(self):
            return None

        def oauth_auth_file(self):
            return ""

    monkeypatch.setattr(codex_runner, "CodexRunner", CapturingCodexRunner)
    daemon = _steward_daemon(
        tmp_path, STEWARD_RUNTIME="codex", STEWARD_TRIAGE_MODEL="gpt-5.6-terra",
        TARGET_DIR=str(tmp_path),
    )

    assert _steward_dispatcher(daemon).cfg.triage.model == "gpt-5.6-terra"
    assert seen["default_model"] == "gpt-5.6-terra"


def test_a_codex_steward_enforces_the_triage_budget_from_streamed_usage(
    tmp_path, monkeypatch
):
    """Codex has no native dollar flag: the harness kills a run once its priced
    rollout usage crosses the leg's cap. The steward must inherit that, and the
    cap it enforces is STEWARD_TRIAGE_BUDGET_USD, not the fixer's."""
    from issue_agent.adapters import codex_runner

    seen = {}

    class CapturingCodexRunner:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def preflight(self):
            return None

        def oauth_auth_file(self):
            return ""

    monkeypatch.setattr(codex_runner, "CodexRunner", CapturingCodexRunner)
    daemon = _steward_daemon(
        tmp_path, STEWARD_RUNTIME="codex", STEWARD_GROOM="1",
        STEWARD_TRIAGE_BUDGET_USD="0.75", TARGET_DIR=str(tmp_path),
    )

    assert seen["enforce_budget"] is True
    assert seen["cost_per_mtok_in"] > 0 and seen["cost_per_mtok_out"] > 0
    dispatcher = _steward_dispatcher(daemon)
    assert dispatcher.cfg.triage.max_budget_usd == 0.75
    assert dispatcher.cfg.groom.max_budget_usd == 0.75


@pytest.mark.parametrize(
    "target_dir", ["", "/nonexistent/typo-in-target-dir"], ids=["unset", "typo"]
)
def test_a_codex_steward_without_a_usable_checkout_disarms_every_capability(
    tmp_path, monkeypatch, capsys, target_dir
):
    """Codex containment is declared by a hook script at the leg's cwd, so with
    no usable TARGET_DIR there is nothing to contain the leg in.

    The daemon must therefore not merely SAY it cannot triage — it must not
    decide or dispatch triage/groom/jira at all. An earlier version logged
    triage=off while `decide` kept emitting paid TRIAGE actions that ran with no
    CODEX_HOME and no PreToolUse hook: paid, ungoverned legs.
    """
    from issue_agent.adapters import codex_runner

    class CapturingCodexRunner:
        def __init__(self, **kwargs):
            pass

        def preflight(self):
            return None

        def oauth_auth_file(self):
            return ""

    monkeypatch.setattr(codex_runner, "CodexRunner", CapturingCodexRunner)
    daemon = _steward_daemon(
        tmp_path, STEWARD_RUNTIME="codex", STEWARD_GROOM="1", JIRA_SYNC="1",
        JIRA_PROJECT_KEY="ABC", TARGET_DIR=target_dir,
    )

    dispatcher = _steward_dispatcher(daemon)
    assert dispatcher.cfg.triage_cfg.enabled is False
    assert dispatcher.cfg.groom is None
    assert dispatcher.cfg.jira is None
    # The pure decision emits nothing payable for an untriaged issue.
    assert daemon.decide(_triaged()) == []
    assert "STEWARD_RUNTIME=codex requires TARGET_DIR" in capsys.readouterr().err


def test_an_unusable_codex_steward_runtime_refuses_rather_than_improvising(
    tmp_path, monkeypatch
):
    """Defence in depth behind the capability gate: if anything ever did reach
    the runner, it must refuse — never fall back to the BARE Codex runner with
    the operator's own ~/.codex/config.toml and no governor."""
    import asyncio

    from issue_agent.adapters import codex_runner
    from issue_agent.adapters.runtime_plan import RuntimePlan

    class CapturingCodexRunner:
        def __init__(self, **kwargs):
            pass

        def preflight(self):
            return None

        def oauth_auth_file(self):
            return ""

    monkeypatch.setattr(codex_runner, "CodexRunner", CapturingCodexRunner)
    daemon = _steward_daemon(tmp_path, STEWARD_RUNTIME="codex", TARGET_DIR="")

    runtime = _steward_dispatcher(daemon).runner.inner.inner
    assert not isinstance(runtime, CapturingCodexRunner)
    with pytest.raises(RuntimeError, match="requires TARGET_DIR"):
        asyncio.run(runtime.run(RuntimePlan(prompt="x")))


def test_a_codex_steward_keeps_jira_sync_inert_rather_than_unattached(
    tmp_path, monkeypatch, capsys
):
    """The Codex adapter has no MCP transport, so a jira-sync leg dispatched
    there would run without the Atlassian server. Inert and loud, not silent."""
    from issue_agent.adapters import codex_runner

    class CapturingCodexRunner:
        def __init__(self, **kwargs):
            pass

        def preflight(self):
            return None

        def oauth_auth_file(self):
            return ""

    monkeypatch.setattr(codex_runner, "CodexRunner", CapturingCodexRunner)
    daemon = _steward_daemon(
        tmp_path, STEWARD_RUNTIME="codex", JIRA_SYNC="1",
        JIRA_PROJECT_KEY="ABC", TARGET_DIR=str(tmp_path),
    )

    err = capsys.readouterr().err
    assert "has no MCP transport — Jira sync inert" in err
    assert _steward_dispatcher(daemon).cfg.jira is None
    assert "jira_sync=off" in err


def _git_target(tmp_path):
    """A real git checkout for TARGET_DIR. The Claude review binding arms the
    per-PR pinned checkout off TARGET_DIR (0d2e295 M1), so a review leg only
    runs when the PR's head SHA actually exists in that repo — a bare tmp dir
    now (correctly) defers every round instead of reviewing it."""
    repo = tmp_path / "target"
    subprocess.run(
        ["git", "init", "-q", str(repo)], check=True, capture_output=True
    )
    subprocess.run(
        [
            "git", "-C", str(repo), "-c", "user.email=t@example.com",
            "-c", "user.name=t", "commit", "--allow-empty", "-q", "-m", "seed",
        ],
        check=True, capture_output=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return str(repo), sha


def test_review_daemon_claim_agrees_with_branch_resolved_issue(tmp_path, monkeypatch):
    """A review claim and paid admission must identify the same target.

    GitHub does not populate ``closing_issue`` for every non-default-base PR,
    but the fixer's branch still carries its issue number.  The composed
    review daemon must reach the runner and submit its verdict in that world;
    otherwise the durable claim and accounting admission disagree before any
    paid work can start.
    """
    from issue_agent.app.config import from_env
    from issue_agent.app.roles import build_review_daemon

    target_dir, head_sha = _git_target(tmp_path)
    pr = observed_pr(
        152,
        head_ref_name="fix/150-live-e2e",
        head_ref_oid=head_sha,
        closing_issue=None,
        title="dashboard health",
        body="Adds a health endpoint.",
        labels=frozenset({Label.AI_PR_OPEN}),
        state="OPEN",
        author=FIX_BOT,
    )
    world = WorldState(prs=(pr,))

    class BranchLinkedReviewGateway(_BuildableReviewGateway):
        def build_review_world(self):
            return world

    gateway = BranchLinkedReviewGateway(
        prs={pr.number: pr},
        issues={150: IssueView(150, title="dashboard health", state="open")},
        fail_on={"issue_for_pr"},
    )
    from tests.app.resolve_acceptance_harness import ScriptedProvider
    from tests.app.review_acceptance_harness import register_review_launch
    from issue_agent.domain.outcome import ExitReason
    runner = ScriptedProvider([
        AgentResult(
            is_error=False,
            exit_reason=ExitReason.OK,
            session_id="review-session",
            structured={"decision": "APPROVE", "body": "lgtm"},
        )
    ], during=register_review_launch)
    cfg = replace(
        from_env({
            "REPO": "o/r",
            "FIX_BOT": FIX_BOT,
            "REVIEW_BOT": REVIEW_BOT,
            "REVIEWER_RUNTIME": "claude",
            "TARGET_DIR": target_dir,
            **_role_state_env(tmp_path),
        }),
        control_authority_root=str(tmp_path / "machine-state"),
    )
    store = Store(str(tmp_path / "review-state"))
    control = _activate_review_fixture(cfg, gateway)
    daemon = build_review_daemon(cfg, gateway, store, runner)

    actions = daemon.tick()
    daemon.wait_for_dispatches()
    assert daemon.shutdown_dispatches(cancel=False)

    assert any(isinstance(a, RunAgent) and a.kind is AgentKind.REVIEW
               and a.number == pr.number and a.workflow is not None for a in actions)
    assert len(runner.tasks) == 1
    # M1: the Claude leg executed in the pinned per-PR checkout, not the
    # operator's live TARGET_DIR.
    assert runner.tasks[0].cwd == os.path.join(
        target_dir, ".claude", "worktrees", "review-pr-152"
    )
    assert any(c[:3] == ("review", pr.number, "APPROVE") and c[3].startswith("lgtm")
               for c in gateway.calls)
    from issue_agent.app.review_runtime import current_review_failures
    assert current_review_failures(control, "o/r", pr.number) == 0
    assert not os.path.exists(store.pr_path(pr.number, "session"))


def test_review_daemon_defers_changed_linkage_without_spend_or_retry(
    tmp_path,
):
    from issue_agent.app.config import from_env
    from issue_agent.app.roles import build_review_daemon

    observed = observed_pr(
        152,
        head_ref_name="fix/150-live-e2e",
        head_ref_oid="a" * 40,
        closing_issue=None,
        title="dashboard health",
        body="Adds a health endpoint.",
        labels=frozenset({Label.AI_PR_OPEN}),
        state="OPEN",
        author=FIX_BOT,
    )
    changed = replace(
        observed,
        head_ref_name="fix/151-relinked",
        closing_issue=151,
    )
    world = WorldState(prs=(observed,))

    class RelinkedReviewGateway(_BuildableReviewGateway):
        def build_review_world(self):
            return world

    gateway = RelinkedReviewGateway(prs={changed.number: changed}, issues={
        number: IssueView(number, title="dashboard health", state="open") for number in (150, 151)
    })
    from tests.app.resolve_acceptance_harness import ScriptedProvider
    from tests.app.review_acceptance_harness import register_review_launch
    from issue_agent.domain.outcome import ExitReason
    runner = ScriptedProvider([
        AgentResult(
            is_error=True,
            status="must-not-run",
        )
    ], during=register_review_launch)
    cfg = replace(
        from_env({
            "REPO": "o/r",
            "FIX_BOT": FIX_BOT,
            "REVIEW_BOT": REVIEW_BOT,
            "REVIEWER_RUNTIME": "claude",
            "TARGET_DIR": str(tmp_path),
            **_role_state_env(tmp_path),
        }),
        control_authority_root=str(tmp_path / "machine-state"),
    )
    store = Store(str(tmp_path / "review-state"))
    control = _activate_review_fixture(cfg, gateway)
    daemon = build_review_daemon(cfg, gateway, store, runner)

    actions = daemon.tick()
    daemon.wait_for_dispatches()
    assert daemon.shutdown_dispatches(cancel=False)

    assert any(isinstance(a, RunAgent) and a.kind is AgentKind.REVIEW
               and a.number == observed.number and a.workflow is not None for a in actions)
    assert runner.tasks == []
    assert not any(call[0] == "review" for call in gateway.calls)
    assert store.get_infra_deaths(150) == 0
    assert store.get_infra_deaths(151) == 0




def test_codex_fixer_declares_its_role_so_a_review_hook_cannot_cage_it(tmp_path, monkeypatch):
    """#113: the reviewer's hook lives at TARGET_DIR and fixer worktrees are
    nested beneath it, so a fix leg can execute the reviewer's read-only hook.
    The runner states the role in the leg's environment; the governor gives it
    precedence over the inherited flag."""
    from issue_agent.adapters import codex_runner

    seen = {}

    class CapturingCodexRunner:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def preflight(self):
            return None

        def oauth_auth_file(self):
            return ""

    monkeypatch.setattr(codex_runner, "CodexRunner", CapturingCodexRunner)
    _codex_daemon(tmp_path)

    assert seen["env"]["ISSUE_AGENT_LEG_ROLE"] == "fix"


def test_codex_fixer_composes_operator_base_into_gate_local_hook(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    daemon = _codex_daemon(
        tmp_path,
        BASE_BRANCH="canary/dashboard-e2e",
    )
    dispatcher = daemon.executor._run_agent._dispatch.__self__

    prepare_cwd = dispatcher.fix_workflow.prepare_cwd
    assert prepare_cwd is not None
    prepare_cwd(str(worktree))

    scripts = tuple(
        (tmp_path / "state" / "codex-hooks").glob("*/hooks/gov.sh")
    )
    assert len(scripts) == 1
    assert "export BASE_BRANCH=canary/dashboard-e2e" in scripts[0].read_text()


def test_codex_reviewer_defers_hook_installation_until_dispatch(tmp_path, monkeypatch):
    """Building a daemon must not mutate a fixer worktree's Codex hook."""
    from issue_agent.adapters import codex_hook, codex_runner
    from issue_agent.app.config import from_env
    from issue_agent.app.roles import build_review_daemon

    hook_calls = []

    class CapturingCodexRunner:
        def __init__(self, **_kwargs):
            pass

        def preflight(self):
            return None

        def oauth_auth_file(self):
            return ""

    monkeypatch.setattr(codex_runner, "CodexRunner", CapturingCodexRunner)
    monkeypatch.setattr(
        codex_hook, "write_hook_config", lambda *args, **kwargs: hook_calls.append((args, kwargs))
    )
    cfg = replace(
        from_env({
            "REPO": "o/r", "FIX_BOT": FIX_BOT, "REVIEWER_RUNTIME": "codex",
            **_role_state_env(tmp_path),
            "TARGET_DIR": str(tmp_path / "target"),
        }),
        control_authority_root=str(tmp_path / "machine-state"),
    )

    build_review_daemon(
        cfg, _BuildableGateway(), Store(str(tmp_path / "review-state")),
        FakeRunner(AgentResult(is_error=False)),
    )

    assert hook_calls == []


def test_codex_reviewer_names_its_unpriced_budget_warning(tmp_path, monkeypatch, capsys):
    """A review deployment must not inherit a fixer-only warning that sends the
    operator to the wrong knob while REVIEW_BUDGET_USD is unmeasurable."""
    from issue_agent.adapters import codex_runner
    from issue_agent.app.config import from_env
    from issue_agent.app.roles import build_review_daemon

    class CapturingCodexRunner:
        def __init__(self, **_kwargs):
            pass

        def preflight(self):
            return None

    monkeypatch.setattr(codex_runner, "CodexRunner", CapturingCodexRunner)
    cfg = replace(
        from_env({
            "REPO": "o/r", "FIX_BOT": FIX_BOT, "REVIEWER_RUNTIME": "codex",
            **_role_state_env(tmp_path),
            "REVIEW_BUDGET_USD": "2.5", "CODEX_COST_PER_MTOK_IN": "0",
            "CODEX_COST_PER_MTOK_OUT": "0",
        }),
        control_authority_root=str(tmp_path / "machine-state"),
    )

    build_review_daemon(
        cfg, _BuildableGateway(), Store(str(tmp_path / "review-state")),
        FakeRunner(AgentResult(is_error=False)),
    )

    err = capsys.readouterr().err
    assert "REVIEW_BUDGET_USD=$2.50 is NOT ENFORCED" in err
    assert "REVIEWER_RUNTIME=codex" in err


def test_the_warning_still_fires_with_the_price_table_zeroed(tmp_path, capsys):
    _codex_daemon(tmp_path, CODEX_COST_PER_MTOK_IN="0", CODEX_COST_PER_MTOK_OUT="0")

    err = capsys.readouterr().err
    assert "NOT ENFORCED on this runtime" in err
    assert "NEITHER cap can ever fire" in err, "the zero-price warning is unchanged"


# ── duplicate-PR guard reporting (#39) ───────────────────────────────────────
_GUARD_CFG = FixConfig(fix_bot=FIX_BOT)


def _duplicate_world(author="xnmp"):
    """One ai-ready issue an open, NOT-ours PR already closes."""
    return WorldState(
        issues=(IssueView(554, title="t", labels=frozenset({Label.AI_READY})),),
        closing_refs=(ClosingRef(pr=558, issue=554, author=author, base_ref_name="main"),),
    )


def _duplicates_in(journal):
    return [
        e for e in read_events(journal.path) if e.get("event") == "duplicate_pr_guard"
    ]


def test_a_duplicate_pr_is_journaled_once_and_logged_every_poll(tmp_path):
    """The event IS the metric #39 moves — resolves not bought — so it carries
    the issue, the PR and the author, once per pair; the log keeps answering
    "why did my ai-ready issue go to ai-blocked?" on any poll it survives."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    logs = []
    report = _duplicate_pr_reporter(journal, "o/r", _GUARD_CFG, logs.append)

    for _ in range(3):
        report(_duplicate_world())

    events = _duplicates_in(journal)
    assert len(events) == 1
    assert (events[0]["issue"], events[0]["pr"], events[0]["author"]) == (554, 558, "xnmp")
    assert events[0]["block_key"] == "duplicate-pr:o/r:554:558"
    assert len(logs) == 3 and all("#554" in ln and "#558" in ln for ln in logs)


def test_the_fixers_own_pr_is_never_reported_as_a_duplicate(tmp_path):
    # anti-vacuity twin: the normal in-flight state must produce no event at all.
    journal = Journal(str(tmp_path / "journal.jsonl"))
    logs = []
    _duplicate_pr_reporter(journal, "o/r", _GUARD_CFG, logs.append)(
        _duplicate_world(author=FIX_BOT)
    )
    assert _duplicates_in(journal) == [] and logs == []


def test_the_duplicate_dedup_survives_a_daemon_restart(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    control = _control(tmp_path)
    _duplicate_pr_reporter(
        journal, "o/r", _GUARD_CFG, lambda _m: None, control
    )(_duplicate_world())
    _duplicate_pr_reporter(
        journal, "o/r", _GUARD_CFG, lambda _m: None, control
    )(_duplicate_world())
    assert len(_duplicates_in(journal)) == 1


def test_the_reporter_and_the_decision_read_the_same_world(tmp_path):
    """Single definition: what is journaled is what decide_fix refuses. Prove it
    end-to-end rather than by re-stating the predicate."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    world = _duplicate_world()
    _duplicate_pr_reporter(journal, "o/r", _GUARD_CFG, lambda _m: None)(world)
    snapshot = resolve_snapshot(554)
    actions = decide_fix(world, _GUARD_CFG, FixState(resolve_workflows={554: snapshot}), fixer_busy=False)
    assert [a for a in actions if isinstance(a, RunAgent)] == []
    assert Relabel(Target.ISSUE, 554, Label.AI_READY, Label.AI_BLOCKED, workflow=snapshot.binding) in actions
    assert _duplicates_in(journal)[0]["pr"] == 558


# ── confirmed-flake reporting (#38) ──────────────────────────────────────────
def _rerun_world(bucket="pass", head="sha-1"):
    return WorldState(
        prs=(
            observed_pr(144, head_ref_name="fix/9-x", head_ref_oid=head, state="OPEN",
                   author=FIX_BOT, labels=frozenset({Label.AI_PR_OPEN}),
                   checks=(Check("unit", bucket=bucket),)),
        ),
    )


def _flakes_in(journal):
    return [e for e in read_events(journal.path) if e.get("event") == "ci_flake"]


def test_a_green_head_that_used_its_free_rerun_is_journaled_once(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    logs = []
    report = _ci_flake_reporter(journal, "o/r", _GUARD_CFG, logs.append)
    state = FixState(ci_rerun_head={144: "sha-1"})

    for _ in range(3):
        report(_rerun_world(), state)

    events = _flakes_in(journal)
    assert len(events) == 1
    assert (events[0]["pr"], events[0]["head"]) == (144, "sha-1")
    assert events[0]["block_key"] == "ci-flake:o/r:144:sha-1"
    assert len(logs) == 3


def test_a_green_pr_that_never_re_ran_is_not_a_flake(tmp_path):
    # anti-vacuity twin: without this the "flake rate" would be the green rate.
    journal = Journal(str(tmp_path / "journal.jsonl"))
    logs = []
    _ci_flake_reporter(journal, "o/r", _GUARD_CFG, logs.append)(
        _rerun_world(), FixState()
    )
    assert _flakes_in(journal) == [] and logs == []


# ── FixState carries the re-run marker (#38) ─────────────────────────────────
def test_fix_state_reads_the_ci_rerun_marker_the_executor_wrote(tmp_path):
    """Round trip through the REAL store: the executor's write must be the value
    the domain reads, or the free re-run repeats forever."""
    store = Store(str(tmp_path / "state"))
    world = _rerun_world(bucket="fail")
    assert _fix_state(store, world).ci_rerun_head[144] == ""   # fresh PR: unspent
    from tests.fakes.accounted_workflow import seed_verified_ci_rerun
    seed_verified_ci_rerun(store, 144, "sha-1")
    assert _fix_state(store, world).ci_rerun_head[144] == "sha-1"
    # ...and that is exactly what stops a second free re-run on the same head.
    actions = decide_fix(world, _GUARD_CFG, _fix_state(store, world), fixer_busy=False)
    assert RunAgent(AgentKind.FIX_CI, 144) in actions


@pytest.mark.parametrize("legacy_hint", [False, True])
def test_unknown_actual_ci_rerun_blocks_paid_repair_after_restart(tmp_path, legacy_hint):
    from types import SimpleNamespace
    from pathlib import Path
    from tests.app.test_control_record_isolation import workflow_control
    from tests.fakes.accounted_workflow import install_pr_repair_authority
    from tests.adapters.test_effects import Gateway
    from issue_agent.adapters.effects import GitHubEffectExecutor
    from issue_agent.domain.actions import RerunFailedChecks
    from tests.app.test_durable_workflow_composition import Crash
    control, authority = workflow_control(tmp_path, numbers=(9,))
    store = Store(str(tmp_path / "state"))
    object.__setattr__(store, "_workflow_db", control)
    object.__setattr__(store, "_workflow_authority", authority)
    gateway = Gateway()
    gateway.head = "sha-1"
    gateway.ci_state = "failure"
    world = _rerun_world(bucket="fail")
    world = replace(world, prs=(with_checks(world.prs[0], checks=(Check("unit", bucket="fail", run_id=42),)),))
    install_pr_repair_authority(SimpleNamespace(prs={144: world.prs[0]}), store)
    baseline = decide_fix(world, _GUARD_CFG, _fix_state(store, world), fixer_busy=False)
    assert RerunFailedChecks(144, "sha-1", (42,)) in baseline
    def lose_reply(*args):
        gateway.calls.append(("rerun", "sha-1", (42,)))
        raise Crash("rerun success reply is unavailable")
    gateway.rerun_failed_checks = lose_reply
    if legacy_hint:
        Path(store.pr_path(144, "ci-rerun-head")).parent.mkdir(parents=True, exist_ok=True)
        Path(store.pr_path(144, "ci-rerun-head")).write_text("sha-1")
    effect = GitHubEffectExecutor(gateway, control, repository="o/r", identity="o/r:fix:fixture")
    with pytest.raises(Crash):
        effect.execute(RerunFailedChecks(144, "sha-1", (42,)))
    restarted = _fix_state(store, world)
    actions = decide_fix(world, _GUARD_CFG, restarted, fixer_busy=False)
    assert RunAgent(AgentKind.FIX_CI, 144) not in actions
    assert RerunFailedChecks(144, "sha-1", (42,)) not in actions
    gateway.advance_run_attempts()
    restarted_effect = GitHubEffectExecutor(gateway, control, repository="o/r", identity="o/r:fix:fixture")
    assert restarted_effect.execute(RerunFailedChecks(144, "sha-1", (42,)))
    assert restarted_effect.execute(RerunFailedChecks(144, "sha-1", (42,)))
    assert gateway.calls == [("rerun", "sha-1", (42,))]
    fresh = _fix_state(store, world)
    assert RunAgent(AgentKind.FIX_CI, 144) in decide_fix(world, _GUARD_CFG, fresh, fixer_busy=False)
    next_world = replace(world, prs=(with_checks(world.prs[0], head_ref_oid="sha-2", checks=(Check("unit", bucket="fail", run_id=43),)),))
    assert RerunFailedChecks(144, "sha-2", (43,)) in decide_fix(next_world, _GUARD_CFG,
        _fix_state(store, next_world), fixer_busy=False)


def test_actual_ci_cancel_restart_wait_then_rerun_preserves_existing_progression(tmp_path):
    from tests.app.test_control_record_isolation import workflow_control
    from tests.fakes.accounted_workflow import install_pr_repair_authority
    from tests.adapters.test_effects import Gateway
    from issue_agent.adapters.effects import GitHubEffectExecutor
    from issue_agent.domain.actions import CancelWorkflowRuns, RerunFailedChecks
    from types import SimpleNamespace
    control, authority = workflow_control(tmp_path, numbers=(9,))
    store = Store(str(tmp_path / "state"))
    object.__setattr__(store, "_workflow_db", control)
    object.__setattr__(store, "_workflow_authority", authority)
    world = _wedged_run_world()
    install_pr_repair_authority(SimpleNamespace(prs={144: world.prs[0]}), store)
    gateway = Gateway()
    gateway.head = world.prs[0].head_ref_oid
    planned = decide_fix(world, _STALL_CFG, _fix_state(store, world), fixer_busy=False)
    cancels = [action for action in planned if isinstance(action, CancelWorkflowRuns)]
    assert len(cancels) == 1
    assert cancels[0].run_ids == (4242,)
    effect = GitHubEffectExecutor(gateway, control, repository="o/r", identity="o/r:fix:fixture")
    gateway.get_pr = lambda number, **kwargs: world.prs[0]
    assert effect.execute(cancels[0])
    waiting = decide_fix(world, _STALL_CFG, _fix_state(store, world), fixer_busy=False)
    assert not any(isinstance(action, (CancelWorkflowRuns, RerunFailedChecks, RunAgent)) for action in waiting)
    failed = replace(world, prs=(with_checks(world.prs[0], checks=(Check("unit", bucket="fail", run_id=4242),)),))
    gateway.run_states[4242] = "completed"
    gateway.get_pr = lambda number, **kwargs: failed.prs[0]
    reruns = [action for action in decide_fix(failed, _STALL_CFG, _fix_state(store, failed), fixer_busy=False)
              if isinstance(action, RerunFailedChecks)]
    assert reruns == [RerunFailedChecks(144, gateway.head, (4242,))]
    restarted = GitHubEffectExecutor(gateway, control, repository="o/r", identity="o/r:fix:fixture")
    assert restarted.execute(reruns[0])
    assert restarted.execute(reruns[0])
    final = decide_fix(failed, _STALL_CFG, _fix_state(store, failed), fixer_busy=False)
    assert RunAgent(AgentKind.FIX_CI, 144) in final
    assert gateway.calls == [("cancel", gateway.head, (4242,)), ("rerun", gateway.head, (4242,))]


def _held_pr(number=581, issue=500):
    return observed_pr(
        number, head_ref_name=f"fix/{issue}-x", closing_issue=issue,
        head_ref_oid="abc", labels=frozenset({Label.AI_PR_OPEN}),
        author=FIX_BOT, state="OPEN",
    )


def _review_cfg():
    return ReviewConfig(review_bot="review-bot")


def _decide(tmp_path, control, world_state, now):
    actions = decide_bound_review(
        world_state, _review_cfg(), _bound_review_state(Store(state_dir=str(tmp_path)), world_state)
    )
    return hold_review_dispatch(
        actions,
        _fix_holds(control, "o/r", world_state, now),
        ceiling_seconds=fix_hold_ceiling_seconds(2700.0),
    )


def test_an_active_fix_leg_defers_the_review_of_the_same_pr(tmp_path):
    """The 2026-08-08 tauri batch burned four complete paid review legs on PRs
    the fixer was still pushing to. The claim the fixer already holds is enough
    to know that at dispatch time."""
    control = _control(tmp_path)
    now = datetime.now(timezone.utc)
    world_state = WorldState(prs=(_held_pr(),))
    assert control.claim_dispatch(
        "o/r", "fix", 500, "ADDRESS_REVIEW", "owner-a", frozenset({"a.py"})
    )

    plan = _decide(tmp_path, control, world_state, now)

    assert plan.actions == ()
    assert [hold.pr for hold in plan.deferred] == [581]

    # The fix leg finishes between ticks; nothing persistent has to be undone.
    assert control.release_dispatch_claim("o/r", "fix", 500, "owner-a")
    assert _decide(tmp_path, control, world_state, now).actions == (
        RunAgent(AgentKind.REVIEW, 581, workflow=review_binding(581)),
    )


def test_a_fix_hold_past_the_ceiling_stops_starving_the_review(tmp_path):
    control = _control(tmp_path)
    world_state = WorldState(prs=(_held_pr(),))
    assert control.claim_dispatch(
        "o/r", "fix", 500, "ADDRESS_REVIEW", "owner-a", frozenset({"a.py"})
    )
    wedged = datetime.now(timezone.utc) + timedelta(
        seconds=fix_hold_ceiling_seconds(2700.0) + 1
    )

    plan = _decide(tmp_path, control, world_state, wedged)

    assert plan.actions == (RunAgent(AgentKind.REVIEW, 581, workflow=review_binding(581)),)
    assert [hold.pr for hold in plan.forced] == [581]


def test_a_fix_hold_leaves_every_other_prs_review_alone(tmp_path):
    control = _control(tmp_path)
    world_state = WorldState(prs=(_held_pr(), _held_pr(614, 610)))
    assert control.claim_dispatch(
        "o/r", "fix", 500, "ADDRESS_REVIEW", "owner-a", frozenset({"a.py"})
    )

    plan = _decide(tmp_path, control, world_state, datetime.now(timezone.utc))

    assert plan.actions == (RunAgent(AgentKind.REVIEW, 614, workflow=review_binding(614)),)


def test_a_dead_fix_claim_does_not_defer_the_review(tmp_path):
    control = _control(tmp_path)
    assert control.claim_dispatch(
        "o/r", "fix", 500, "ADDRESS_REVIEW", "owner-a", frozenset({"a.py"})
    )
    with control._connect() as db:
        db.execute(
            "UPDATE dispatch_claims SET owner_pid=?, owner_start_ticks=?",
            (2_000_000_000, 1),
        )

    plan = _decide(tmp_path, control, WorldState(prs=(_held_pr(),)), datetime.now(timezone.utc))

    assert plan.actions == (RunAgent(AgentKind.REVIEW, 581, workflow=review_binding(581)),)


def test_an_unreadable_control_store_fails_open_to_dispatch(tmp_path):
    """Withholding is the only thing this reader does, and the submit-time head
    fence still protects correctness — so an unreadable store must not be able
    to stop review."""
    class Broken:
        def active_dispatch_claims(self, repository, role):
            raise RuntimeError("control store unavailable")

    assert _fix_holds(
        Broken(), "o/r", WorldState(prs=(_held_pr(),)), datetime.now(timezone.utc)
    ) == {}


def _fix_holds_in(journal):
    return [
        event
        for event in read_events(journal.path)
        if event.get("event") == "deferred"
        and event.get("reason") == "fix_leg_active"
    ]


def test_a_review_deferral_is_journaled_once_per_blocking_claim(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    logs = []
    report = _fix_hold_reporter(journal, "o/r", logs.append, _control(tmp_path))
    hold = FixHold(581, "claim-1", 30.0)

    for seconds in (30.0, 50.0, 70.0):
        report((replace(hold, held_seconds=seconds),), ())

    events = _fix_holds_in(journal)
    assert [(e["state"], e["blocker_pr"], e["blocking_claim"]) for e in events] == [
        ("waiting", 581, "claim-1")
    ]
    assert len(logs) == 1


def test_the_ceiling_override_is_journaled_on_the_same_key(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    report = _fix_hold_reporter(journal, "o/r", lambda _m: None)
    report((FixHold(581, "claim-1", 30.0),), ())
    report((), (FixHold(581, "claim-1", 3000.0),))
    report((), (FixHold(581, "claim-1", 3020.0),))  # still wedged, still forcing
    report((), ())                                   # claim finally released

    # "stopped" is a CLOSING level (a forced dispatch is not an open deferral),
    # so a released claim owes no second close and a still-wedged one owes no
    # per-tick repeat.
    assert [e["state"] for e in _fix_holds_in(journal)] == ["waiting", "stopped"]


def test_a_persisted_review_deferral_clears_after_a_restart(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    control = _control(tmp_path)
    control.replace_level_state(
        "review_fix_hold", "o/r", {"581:claim-1": {"state": "waiting"}}
    )

    _fix_hold_reporter(journal, "o/r", lambda _m: None, control)((), ())

    assert [e["state"] for e in _fix_holds_in(journal)] == ["cleared"]
    assert control.load_level_state("review_fix_hold", "o/r") == {}


def test_a_malformed_persisted_deferral_still_clears(tmp_path):
    """A level-state row without a readable state must not wedge the tick; the
    reporter's only job is journalling, so it treats it as an unknown level and
    clears it."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    control = _control(tmp_path)
    control.replace_level_state("review_fix_hold", "o/r", {"581:claim-1": {}})

    _fix_hold_reporter(journal, "o/r", lambda _m: None, control)((), ())

    assert [e["state"] for e in _fix_holds_in(journal)] == ["cleared"]


def test_a_pr_with_no_resolvable_issue_is_still_deferred(tmp_path):
    """`breaker_key` falls back to -PR for a PR with no closing ref and a
    non-numeric branch; the fixer claims under that key, so the review side has
    to address it the same way."""
    control = _control(tmp_path)
    issueless = observed_pr(
        617, head_ref_name="feat/ACME-2468-x", head_ref_oid="abc",
        labels=frozenset({Label.AI_PR_OPEN}), author=FIX_BOT, state="OPEN",
    )
    assert control.claim_dispatch(
        "o/r", "fix", -617, "FIX_CI", "owner-a", frozenset({"a.py"})
    )

    plan = _decide(
        tmp_path, control, WorldState(prs=(issueless,)), datetime.now(timezone.utc)
    )

    assert plan.actions == ()
    assert [hold.pr for hold in plan.deferred] == [617]


def test_groom_dispatch_reads_sql_signature_and_preserves_triage(tmp_path, monkeypatch):
    from pathlib import Path
    from issue_agent.adapters.workflow_rows import load_issue_aggregate, persist_workflow_snapshot
    from issue_agent.domain import continuation_components as cc
    from issue_agent.domain.triage import grooming_content
    from issue_agent.domain.workflow_identity import WorkflowKey, IssueRef
    decide, store = _steward_decide(tmp_path, monkeypatch, steward_triage=False, steward_groom=True)
    world = _triaged()
    assert decide(world) == [RunAgent(AgentKind.GROOM, 30)]
    # Retired raw sources are import evidence and cannot change runtime authority.
    Path(store.state_dir, "groom-30.at").write_text("unknown stopped baseline")
    Path(store.state_dir, "groom-30.content").write_text("unknown stopped signature")
    with store._steward_control.transaction() as db:
        aggregate = load_issue_aggregate(db, WorkflowKey("o/r", IssueRef(30)))
        component = aggregate.steward
        triage = component.state.triage
        signature = grooming_content(world.issues[0])
        state = replace(component.state, groom=cc.StewardLaneState(baseline_at=world.issues[0].updated_at),
                        groom_content=cc.GroomContent(signature, signature))
        persist_workflow_snapshot(db, replace(aggregate, steward=replace(component, state=state)))
    own_comment_tick = replace(world, issues=(replace(world.issues[0], updated_at="2026-09-14T11:00:00+00:00"),))
    assert decide(own_comment_tick) == []
    human_tick = replace(world, issues=(replace(world.issues[0], body="New human body", updated_at="2026-09-14T12:00:00+00:00"),))
    assert decide(human_tick) == [RunAgent(AgentKind.GROOM, 30)]
    assert store._steward_control.read_steward(WorkflowKey("o/r", IssueRef(30))).component.state.triage == triage


def test_exhausted_groom_targets_do_not_starve_eligible_sibling(tmp_path, monkeypatch):
    from issue_agent.adapters.workflow_rows import load_issue_aggregate, persist_workflow_snapshot
    from issue_agent.domain import continuation_components as cc
    from issue_agent.domain.triage import grooming_content
    from issue_agent.domain.workflow_identity import WorkflowKey, IssueRef
    from tests.fakes.accounted_steward import install_steward
    decide, store = _steward_decide(tmp_path, monkeypatch, steward_triage=False, steward_groom=True)
    control = store._steward_control
    install_steward(control, range(30, 36))
    issues = tuple(_triaged(n).issues[0] for n in range(30, 36))
    assert decide(WorldState(issues=issues)) == [RunAgent(AgentKind.GROOM, n) for n in range(30, 35)]
    for issue in issues[:5]:
        with control.transaction() as db:
            aggregate = load_issue_aggregate(db, WorkflowKey("o/r", IssueRef(issue.number)))
            component = aggregate.steward
            state = replace(component.state, groom=cc.StewardLaneState(stalls=3, attempted_at=issue.updated_at),
                            groom_content=cc.GroomContent(attempted=grooming_content(issue)))
            persist_workflow_snapshot(db, replace(aggregate, steward=replace(component, state=state)))
    assert decide(WorldState(issues=issues)) == [RunAgent(AgentKind.GROOM, 35)]


@pytest.mark.parametrize("cancel_still_gates", [False, True])
def test_clear_current_policy_obsoletes_cancel_even_when_its_run_still_reports(tmp_path, cancel_still_gates):
    from issue_agent.domain.checks import Observed, RequiredCheck
    from issue_agent.domain.actions import CancelWorkflowRuns, EnableAutoMerge, EscalateCiPending
    from tests.app.test_control_record_isolation import workflow_control
    from tests.fakes.accounted_workflow import install_pr_repair_authority
    from tests.adapters.test_effects import Gateway
    from issue_agent.adapters.effects import GitHubEffectExecutor
    from types import SimpleNamespace
    control, authority = workflow_control(tmp_path, numbers=(9,))
    store = Store(str(tmp_path / "state"))
    object.__setattr__(store, "_workflow_db", control)
    object.__setattr__(store, "_workflow_authority", authority)
    world = _wedged_run_world()
    install_pr_repair_authority(SimpleNamespace(prs={144: world.prs[0]}), store)
    gateway = Gateway()
    gateway.head = world.prs[0].head_ref_oid
    gateway.get_pr = lambda number, **kwargs: world.prs[0]
    action = next(a for a in decide_fix(world, _STALL_CFG, _fix_state(store, world), fixer_busy=False)
        if isinstance(a, CancelWorkflowRuns))
    assert GitHubEffectExecutor(gateway, control, repository="o/r", identity="o/r:fix:fixture").execute(action)
    ci = Observed(gateway.head, gateway.head, frozenset({RequiredCheck("unit")}), (
        Check("unit", "SKIPPED" if cancel_still_gates else "SUCCESS", "pass", required=True,
            run_id=4242 if cancel_still_gates else 9999), Check("macos", "CANCELLED", "fail", run_id=4242)))
    current = replace(world.prs[0], ci=ci, merge_state="UNSTABLE",
        reviews=(Review("APPROVED", "review-bot", commit_id=gateway.head),))
    world = replace(world, prs=(current,))
    actions = decide_fix(world, _STALL_CFG, _fix_state(store, world), fixer_busy=False)
    assert any(isinstance(a, EnableAutoMerge) for a in actions)
    assert not any(isinstance(a, EscalateCiPending) for a in actions)


@pytest.mark.parametrize("cap_usd, blocked", [(5.0, True), (10.0, False), (50.0, False)])
def test_spend_gate_holds_paid_dispatch_when_one_leg_no_longer_fits_the_daily_cap(tmp_path, cap_usd, blocked):
    # Decision #7: a daily cap smaller than one leg's reservation is a reported
    # tick-gate wait, never a silent one and never an admission refusal label.
    from issue_agent.app.roles import _control_spend_gate

    control = SQLiteControlStore(str(tmp_path / "control.db"))
    control.initialize()
    cfg = HarnessConfig(repo="o/r", target_dir=str(tmp_path), daily_spend_cap_usd=cap_usd)
    gate = _control_spend_gate(cfg, control, 10.0)
    assert gate is not None
    reason = gate()
    assert (reason is not None) is blocked
    if blocked:
        assert "repository daily spend cap reached" in reason and "$10.00 leg / $5.00" in reason
