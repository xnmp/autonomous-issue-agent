"""Daemon loop — tick sequencing, crash isolation, heartbeat."""


import asyncio
import json
import re
import threading
from pathlib import Path

import pytest

from issue_agent.adapters.github import GhUnavailable
from issue_agent.adapters.reload import RELOAD_EXIT_CODE
from issue_agent.app.tick import DEFAULT_SHUTDOWN_TIMEOUT_SECONDS, CancellableAsyncDispatch, Daemon
from issue_agent.domain.actions import AgentKind, PostComment, Relabel, RunAgent, Target
from issue_agent.domain.labels import Label
from issue_agent.domain.recovery import RecoveryTrigger
from issue_agent.domain.supervision import ExitCode
from issue_agent.domain.world import WorldState
from tests.fakes.executor import ImmediateExecutor as Executor
from tests.fakes.in_memory_github import InMemoryGitHub as FakeGateway
from tests.fakes.manual_clock import ManualClock

# Shutdown timeout and clock steps that put a daemon at the start of its
# forced-teardown window: the 10 s escalation reserve remains, graceful
# draining is over, and the 3 s terminalize reserve leaves a 7 s kill window.
_SHUTDOWN_TIMEOUT = 30
_GRACEFUL_WINDOW = 20
# Closes the post-kill wait while leaving the terminalize reserve.
_KILL_WINDOW = 7


def _daemon(decide, observe=None, logs=None):
    gw = FakeGateway()
    d = Daemon(name="fix", observe=observe or (lambda: WorldState()), decide=decide, executor=Executor(gw),
               poll_seconds=60, log=(logs.append if logs is not None else (lambda m: None)))
    return d, gw


def _paid(run_agent, name="fix", kind=AgentKind.RESOLVE, **daemon) -> Daemon:
    """A daemon whose every tick decides one paid leg on item 7."""
    return Daemon(name, lambda: WorldState(), lambda _world: [RunAgent(kind, 7)],
                  Executor(FakeGateway(), run_agent=run_agent), **{"poll_seconds": 1, "dispatch_capacity": 1, **daemon})


def _interrupt(_seconds):
    raise KeyboardInterrupt


def test_tick_isolated_from_decide_crash():
    logs = []
    def boom(world):
        raise RuntimeError("decide blew up")
    d, gw = _daemon(boom, logs=logs)
    assert d.tick() == []                # swallowed
    assert any("poll pass failed" in m for m in logs)
    assert gw.calls == []


def test_tick_executes_cheap_actions_before_agent_dispatches():
    # A RunAgent blocks the tick for the whole session, so a merge/relabel/reap
    # sequenced after one waits up to the wall cap for a ~1s gh call (PR #533
    # sat APPROVED+green across two daemon lifetimes; RESOLVE #500 was
    # dispatched ahead of arming merge-ready #537). Cheap actions must land
    # first — but a dispatch's OWN preceding relabel keeps preceding it, and
    # dispatch order among dispatches is preserved.
    executed = []

    class OrderGateway:
        def relabel(self, kind, number, remove, add):
            executed.append(("relabel", number))

        def has_label(self, kind, number, label):
            return True

        def comment(self, kind, number, body):
            executed.append(("comment", number))

    ex = Executor(OrderGateway(), run_agent=lambda a: executed.append(("run", a.number)))
    d = Daemon("fix", lambda: WorldState(), lambda w: [
        Relabel(Target.ISSUE, 500, Label.NEEDS_HUMAN, Label.AI_READY), RunAgent(AgentKind.RESOLVE, 500),
        PostComment(Target.PR, 537, "done"), RunAgent(AgentKind.REBASE, 534),
    ], ex, dispatch_capacity=2)
    taken = d.tick()
    d.wait_for_dispatches()
    assert executed[:2] == [
        ("relabel", 500),      # the dispatch's own label move still precedes it
        ("comment", 537),      # finished work lands before any session starts
    ]
    assert sorted(executed[2:]) == [("run", 500), ("run", 534)]
    # Dispatch order among dispatches is preserved at submission.
    assert [a.number for a in taken if isinstance(a, RunAgent)] == [500, 534]


def test_background_dispatch_is_bounded_and_admits_independent_targets():
    release = threading.Event()
    started: list[int] = []
    started_two = threading.Event()
    next_issue = iter((1, 2, 3))

    def run_agent(action):
        started.append(action.number)
        if len(started) == 2:
            started_two.set()
        assert release.wait(timeout=5)

    daemon = Daemon("fix", lambda: WorldState(), lambda _world: [RunAgent(AgentKind.RESOLVE, next(next_issue))],
                    Executor(FakeGateway(), run_agent=run_agent), dispatch_capacity=2)

    daemon.tick()
    daemon.tick()
    assert started_two.wait(timeout=1)
    third = daemon.tick()

    assert set(started) == {1, 2}
    assert len(daemon.active_dispatches) == 2
    assert third == [], "the third paid leg is not accepted above the bound"

    release.set()
    daemon.wait_for_dispatches()


def test_hot_reload_drains_paid_work_while_observation_continues():
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    observations = []
    sleeps = 0
    gateway = FakeGateway()

    def run_agent(_action):
        started.set()
        assert release.wait(timeout=5)
        finished.set()

    def sleep(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            assert daemon.available_dispatch_slots == 0
            assert gateway.calls == [("comment", "pr", 100, "rerun")]
            release.set()
            assert finished.wait(timeout=1)

    daemon = Daemon(
        "fix", lambda: observations.append(len(observations) + 1) or WorldState(),
        lambda _world: [RunAgent(AgentKind.RESOLVE, 7)] + (
            [PostComment(Target.PR, 100, "rerun")] if len(observations) == 2 else []),
        Executor(gateway, run_agent=run_agent), poll_seconds=1, dispatch_capacity=1,
    )

    with pytest.raises(SystemExit) as stopped:
        daemon.run_forever(sleep=sleep, now=lambda: 0, max_ticks=4, reload_check=lambda: True)

    assert started.is_set()
    assert stopped.value.code == RELOAD_EXIT_CODE
    assert observations == [1, 2, 3]


def test_operator_interrupt_cancels_async_dispatch_and_joins_pool():
    started = threading.Event()
    cancelled = threading.Event()
    prepared = threading.Event()
    terminalized = threading.Event()

    async def dispatch(_action):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            assert prepared.is_set()
            cancelled.set()

    def terminalize():
        assert cancelled.is_set()
        terminalized.set()

    runtime = CancellableAsyncDispatch(dispatch)
    daemon = _paid(runtime, prepare_shutdown=prepared.set, cancel_active_dispatches=runtime.cancel_all,
                   terminalize=terminalize)

    def interrupt(_seconds):
        assert started.wait(timeout=1)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        daemon.run_forever(sleep=interrupt, now=lambda: 0)

    assert cancelled.wait(timeout=1)
    assert terminalized.is_set()
    assert daemon.active_dispatches == ()
    assert daemon.available_dispatch_slots == 0


@pytest.mark.parametrize("exhausted", [False, True], ids=["teardown-window", "budget-exhausted"])
def test_shutdown_deadline_forces_process_teardown_and_exits_transient(exhausted):
    clock = ManualClock()
    started = threading.Event()
    release = threading.Event()
    forced = []

    def run_agent(_action):
        started.set()
        release.wait(timeout=5)

    def kill():
        forced.append(True)
        clock.advance(_KILL_WINDOW)

    daemon = _paid(run_agent, "review", AgentKind.REVIEW, shutdown_timeout_seconds=_SHUTDOWN_TIMEOUT, monotonic=clock,
                   cancel_active_dispatches=lambda: clock.advance(_GRACEFUL_WINDOW), force_kill_dispatches=kill)

    def before_shutdown(_seconds):
        assert started.wait(timeout=1)
        if exhausted:
            # A deadline can already be exhausted by earlier shutdown work;
            # optional teardown has no remaining allocation in that case.
            daemon._shutdown_deadline = 0
        raise KeyboardInterrupt

    try:
        with pytest.raises((KeyboardInterrupt, SystemExit)) as stopped:
            daemon.run_forever(sleep=before_shutdown, now=lambda: 0, max_ticks=1)
        assert stopped.type is SystemExit
        assert stopped.value.code is ExitCode.TRANSIENT
        assert forced == ([] if exhausted else [True])
        assert daemon.requires_hard_exit is True
    finally:
        release.set()
        daemon.wait_for_dispatches()


def test_shutdown_reaps_killed_worker_before_final_spend_reconciliation():
    clock = ManualClock()
    release = threading.Event()
    worker_accounted = threading.Event()
    order = []

    def run_agent(_action):
        assert release.wait(timeout=5)
        order.append("worker_accounted")
        worker_accounted.set()

    def kill():
        order.append("kill")
        release.set()

    def terminalize():
        # Spend reconciliation cannot race the dispatch's own settlement.
        assert worker_accounted.is_set()
        order.append("terminalize")

    daemon = _paid(run_agent, shutdown_timeout_seconds=_SHUTDOWN_TIMEOUT, monotonic=clock,
                   cancel_active_dispatches=lambda: clock.advance(_GRACEFUL_WINDOW), force_kill_dispatches=kill,
                   terminalize=terminalize)

    with pytest.raises(KeyboardInterrupt):
        daemon.run_forever(sleep=_interrupt, now=lambda: 0, max_ticks=1)

    assert order == ["kill", "worker_accounted", "terminalize"]
    daemon.wait_for_dispatches()


def test_shutdown_retry_finishes_cleanup_after_dispatch_owner_settles():
    clock = ManualClock()
    started = threading.Event()
    release = threading.Event()
    settled = threading.Event()
    terminalized = []

    def run_agent(_action):
        started.set()
        assert release.wait(timeout=5)
        settled.set()

    def kill_without_worker_settling_in_window():
        # The kill window closes with the terminalize reserve still intact,
        # and the worker has not settled yet.
        clock.set(_SHUTDOWN_TIMEOUT - 3)

    daemon = _paid(run_agent, poll_seconds=60, shutdown_timeout_seconds=_SHUTDOWN_TIMEOUT, monotonic=clock,
                   cancel_active_dispatches=lambda: clock.advance(_GRACEFUL_WINDOW),
                   force_kill_dispatches=kill_without_worker_settling_in_window,
                   terminalize=lambda: terminalized.append("terminalized"))
    daemon.tick()
    assert started.wait(timeout=1)

    assert daemon.shutdown_dispatches(cancel=True) is False
    release.set()
    assert settled.wait(timeout=1)
    daemon.wait_for_dispatches()
    assert daemon.shutdown_dispatches(cancel=False) is True

    assert terminalized == ["terminalized"]


def test_application_shutdown_deadline_fits_inside_every_systemd_hard_stop():
    units = tuple((Path(__file__).parents[2] / "ops" / "systemd").glob("issue-agent-*.service"))
    assert units
    for path in units:
        match = re.search(r"^TimeoutStopSec=(\d+(?:\.\d+)?)$", path.read_text(), re.M)
        assert match is not None
        assert DEFAULT_SHUTDOWN_TIMEOUT_SECONDS + 5 < float(match.group(1))


def test_interrupt_before_worker_registration_is_sticky():
    registering = threading.Event()
    allow_registration = threading.Event()

    class PausedRegistration(CancellableAsyncDispatch):
        def _new_cancel_event(self):
            registering.set()
            assert allow_registration.wait(timeout=1)
            return super()._new_cancel_event()

        def cancel_all(self):
            super().cancel_all()
            allow_registration.set()

    async def dispatch(_action):
        await asyncio.Event().wait()

    runtime = PausedRegistration(dispatch)
    daemon = _paid(runtime, cancel_active_dispatches=runtime.cancel_all)

    def interrupt(_seconds):
        assert registering.wait(timeout=1)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        daemon.run_forever(sleep=interrupt, now=lambda: 0)

    assert daemon.active_dispatches == ()


def test_tick_skips_the_pass_when_github_is_unavailable():
    """The V10 contract at the loop layer: an unreadable GitHub must produce NO
    decide, NO execute — not a pass over an empty world. Asserted on the observable
    outcome (zero side-effects on the gateway), not on how tick branches."""
    logs = []
    def rate_limited():
        raise GhUnavailable("rate-limit", "HTTP 403: API rate limit exceeded for user 42")
    decided = []
    def decide(world):
        decided.append(world)
        return [Relabel(Target.ISSUE, 5, Label.NEEDS_HUMAN, Label.AI_READY)]

    d, gw = _daemon(decide, observe=rate_limited, logs=logs)
    assert d.tick() == []
    assert decided == []          # never asked the domain about a world we can't see
    assert gw.calls == []         # and never touched the repo


def test_action_raising_gh_unavailable_does_not_kill_the_daemon():
    """Side-effect calls share the low-level runner, so a rate limit can surface
    mid-execute. The executor's per-action isolation must absorb it: the action is
    logged as failed and the daemon keeps polling."""
    logs = []
    class ThrottledGateway:
        def relabel(self, *a):
            raise GhUnavailable("rate-limit", "HTTP 429: Too Many Requests")
    d = Daemon("fix", lambda: WorldState(),
               lambda w: [Relabel(Target.ISSUE, 5, Label.AI_READY, None)],
               Executor(ThrottledGateway()), log=logs.append)
    d.tick()  # must not raise
    assert any("action" in m and "failed" in m and "rate-limited" in m for m in logs)


def test_normal_shutdown_persists_stopped_heartbeat(tmp_path):
    heartbeat = tmp_path / "heartbeat.json"
    d, _ = _daemon(lambda _world: [])
    d.heartbeat_path = str(heartbeat)

    d.run_forever(sleep=lambda _seconds: None, now=lambda: 0, max_ticks=1)

    payload = json.loads(heartbeat.read_text())
    assert payload["state"] == "stopped"
    assert payload["in_flight"] == []


def test_scheduler_progress_covers_tick_and_quiet_wait():
    progress = []

    def observe():
        assert progress == ["completed"]
        return WorldState()

    d, _ = _daemon(lambda _world: [], observe=observe)
    d.run_forever(sleep=lambda _seconds: None, now=lambda: 0, max_ticks=1,
                  scheduler_progress=lambda: progress.append("completed"))

    assert progress == ["completed"] * 5


def test_failed_shutdown_deadline_overrides_clean_signal_exit():
    d, _ = _daemon(lambda _world: [])
    d.shutdown_dispatches = lambda *, cancel: False

    with pytest.raises(SystemExit) as stopped:
        d.run_forever(sleep=lambda _seconds: (_ for _ in ()).throw(SystemExit(ExitCode.CLEAN)), now=lambda: 0)

    assert stopped.value.code is ExitCode.TRANSIENT


def test_run_forever_warns_on_suspend_stall():
    logs = []
    d, _ = _daemon(lambda w: [], logs=logs)
    # now() jumps far beyond poll*4 between ticks → suspend-stall warning.
    times = iter([0, 100_000, 100_060])
    d.run_forever(sleep=lambda s: None, now=lambda: next(times), max_ticks=2)
    assert any("suspended/stalled" in m for m in logs)


def test_suspend_recovery_waits_for_active_dispatch_owner():
    started = threading.Event()
    release = threading.Event()
    settled = threading.Event()
    recoveries = []
    started_actions = []
    decisions = 0

    def decide(_world):
        nonlocal decisions
        decisions += 1
        if decisions == 1:
            return [RunAgent(AgentKind.RESOLVE, 7)]
        if decisions == 2:
            assert daemon.active_dispatches == ()
            assert daemon.available_dispatch_slots == 0
            return [RunAgent(AgentKind.RESOLVE, 8)]
        return []

    def run_agent(action):
        started_actions.append(action.number)
        started.set()
        assert release.wait(timeout=5)
        settled.set()

    def log(message):
        if "wake recovery deferred" in message:
            release.set()
            assert settled.wait(timeout=1)
            daemon.wait_for_dispatches()

    daemon = Daemon("fix", lambda: WorldState(), decide, Executor(FakeGateway(), run_agent=run_agent),
                    poll_seconds=60, dispatch_capacity=1, log=log)

    def reconcile(trigger):
        assert daemon.active_dispatches == ()
        recoveries.append(trigger)

    daemon.reconcile = reconcile
    wall_times = iter([0, 0, 240, 300])
    active_times = iter([0, 0, 60, 120])
    daemon.run_forever(sleep=lambda _seconds: None, now=lambda: next(wall_times),
                       active_now=lambda: next(active_times), max_ticks=3)

    assert started.is_set()
    assert started_actions == [7]
    assert recoveries == [RecoveryTrigger.WAKE]


def test_a_late_tick_without_suspension_is_productive_not_a_wake():
    # A long synchronous leg outruns the poll interval on both clocks: no
    # wall time went unexplained, so nothing was suspended.
    recoveries, logs = [], []
    d, _ = _daemon(lambda w: [], logs=logs)
    d.reconcile = recoveries.append
    wall, active = iter([0, 0, 240, 300]), iter([0, 0, 240, 300])
    d.run_forever(sleep=lambda _s: None, now=lambda: next(wall), active_now=lambda: next(active), max_ticks=3)
    assert RecoveryTrigger.WAKE not in recoveries
    assert not any("woke" in m for m in logs)


def test_a_second_dispatch_for_a_running_target_is_not_submitted():
    release, started = threading.Event(), []

    def run_agent(action):
        started.append(action.number)
        assert release.wait(timeout=5)

    d = Daemon("fix", lambda: WorldState(), lambda _w: [RunAgent(AgentKind.RESOLVE, 7)] * 2,
               Executor(FakeGateway(), run_agent=run_agent), poll_seconds=1, dispatch_capacity=2)
    try:
        assert d.tick() == [RunAgent(AgentKind.RESOLVE, 7)]
    finally:
        release.set()
        d.wait_for_dispatches()
    assert started == [7]


def test_reservation_sweep_runs_on_bounded_tick_cadence():
    sweeps = []
    daemon = Daemon("fix", lambda: WorldState(), lambda _world: [], Executor(FakeGateway()), poll_seconds=1,
                    reconcile=sweeps.append, reservation_sweep_ticks=2)

    daemon.run_forever(sleep=lambda _seconds: None, now=lambda: 0, max_ticks=2)

    assert sweeps == [RecoveryTrigger.PERIODIC]


def test_without_a_wake_the_full_interval_is_slept():
    d, _ = _daemon(lambda w: [])
    d.poll_seconds, d.wake_check = 5, lambda: False
    slept = []
    d.run_forever(sleep=slept.append, now=lambda: 0, max_ticks=2)
    assert sum(slept) == 10
    # Regression guard on the unwired path: no slicing, no extra sleep calls.
    unwired, _ = _daemon(lambda w: [])
    slept = []
    unwired.run_forever(sleep=slept.append, now=lambda: 0, max_ticks=2)
    assert slept == [60, 60]
