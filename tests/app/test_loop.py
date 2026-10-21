"""Daemon loop — tick sequencing, crash isolation, heartbeat."""

import asyncio
import contextlib
import json
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from issue_agent.adapters.control_store import SQLiteControlStore, StoreWake
from issue_agent.adapters.github import GhUnavailable
from issue_agent.adapters.reload import RELOAD_EXIT_CODE
from issue_agent.app.tick import DEFAULT_SHUTDOWN_TIMEOUT_SECONDS, CancellableAsyncDispatch, Daemon
from issue_agent.domain.contracts import RunId
from issue_agent.domain.actions import AgentKind, PostComment, Relabel, RunAgent, Target
from issue_agent.domain.evidence import CiFailure, CiFailures
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
    d = Daemon(
        name="fix",
        observe=observe or (lambda: WorldState()),
        decide=decide,
        executor=Executor(gw),
        poll_seconds=60,
        log=(logs.append if logs is not None else (lambda m: None)),
    )
    return d, gw


def _paid(run_agent, name="fix", kind=AgentKind.RESOLVE, **daemon) -> Daemon:
    """A daemon whose every tick decides one paid leg on item 7."""
    return Daemon(**{
        "name": name,
        "observe": lambda: WorldState(),
        "decide": lambda _world: [RunAgent(kind, 7)],
        "executor": Executor(FakeGateway(), run_agent=run_agent),
        "poll_seconds": 1,
        "dispatch_capacity": lambda: 1,
        **daemon,
    })


def _interrupt(_seconds):
    raise KeyboardInterrupt


def test_tick_isolated_from_decide_crash():
    logs = []

    def boom(world):
        raise RuntimeError("decide blew up")

    d, gw = _daemon(boom, logs=logs)
    assert d.tick() == []  # swallowed
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
    d = Daemon(
        "fix",
        lambda: WorldState(),
        lambda w: [
            Relabel(Target.ISSUE, 500, Label.NEEDS_HUMAN, Label.AI_READY),
            RunAgent(AgentKind.RESOLVE, 500),
            PostComment(Target.PR, 537, "done", key="done:537"),
            RunAgent(AgentKind.REBASE, 534),
        ],
        ex,
        dispatch_capacity=lambda: 2,
    )
    taken = d.tick()
    d.wait_for_dispatches()
    assert executed[:2] == [
        ("relabel", 500),  # the dispatch's own label move still precedes it
        ("comment", 537),  # finished work lands before any session starts
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

    daemon = Daemon(
        "fix",
        lambda: WorldState(),
        lambda _world: [RunAgent(AgentKind.RESOLVE, next(next_issue))],
        Executor(FakeGateway(), run_agent=run_agent),
        dispatch_capacity=lambda: 2,
    )

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
        "fix",
        lambda: observations.append(len(observations) + 1) or WorldState(),
        lambda _world: (
            [RunAgent(AgentKind.RESOLVE, 7)]
            + ([PostComment(Target.PR, 100, "rerun", key="rerun:100")] if len(observations) == 2 else [])
        ),
        Executor(gateway, run_agent=run_agent),
        poll_seconds=1,
        dispatch_capacity=lambda: 1,
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
    daemon = _paid(
        runtime, prepare_shutdown=prepared.set, cancel_active_dispatches=runtime.cancel_all, terminalize=terminalize
    )

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

    daemon = _paid(
        run_agent,
        "review",
        AgentKind.REVIEW,
        shutdown_timeout_seconds=_SHUTDOWN_TIMEOUT,
        monotonic=clock,
        cancel_active_dispatches=lambda: clock.advance(_GRACEFUL_WINDOW),
        force_kill_dispatches=kill,
    )

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

    daemon = _paid(
        run_agent,
        shutdown_timeout_seconds=_SHUTDOWN_TIMEOUT,
        monotonic=clock,
        cancel_active_dispatches=lambda: clock.advance(_GRACEFUL_WINDOW),
        force_kill_dispatches=kill,
        terminalize=terminalize,
    )

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

    daemon = _paid(
        run_agent,
        poll_seconds=60,
        shutdown_timeout_seconds=_SHUTDOWN_TIMEOUT,
        monotonic=clock,
        cancel_active_dispatches=lambda: clock.advance(_GRACEFUL_WINDOW),
        force_kill_dispatches=kill_without_worker_settling_in_window,
        terminalize=lambda: terminalized.append("terminalized"),
    )
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
    assert decided == []  # never asked the domain about a world we can't see
    assert gw.calls == []  # and never touched the repo


def test_action_raising_gh_unavailable_does_not_kill_the_daemon():
    """Side-effect calls share the low-level runner, so a rate limit can surface
    mid-execute. The executor's per-action isolation must absorb it: the action is
    logged as failed and the daemon keeps polling."""
    logs = []

    class ThrottledGateway:
        def relabel(self, *a):
            raise GhUnavailable("rate-limit", "HTTP 429: Too Many Requests")

    d = Daemon(
        "fix",
        lambda: WorldState(),
        lambda w: [Relabel(Target.ISSUE, 5, Label.AI_READY, None)],
        Executor(ThrottledGateway()),
        log=logs.append,
    )
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
    progress, clock = [], ManualClock()

    def observe():
        assert progress == ["completed"]
        return WorldState()

    d, _ = _daemon(lambda _world: [], observe=observe)
    d.run_forever(
        sleep=clock.advance, now=lambda: 0, max_ticks=1, scheduler_progress=lambda: progress.append("completed")
    )

    assert progress == ["completed"] * 63  # three around the tick, then every second of the 60 s wait


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

    daemon = Daemon(
        "fix",
        lambda: WorldState(),
        decide,
        Executor(FakeGateway(), run_agent=run_agent),
        poll_seconds=60,
        dispatch_capacity=lambda: 1,
        log=log,
    )

    def reconcile(trigger):
        assert daemon.active_dispatches == ()
        recoveries.append(trigger)

    daemon.reconcile = reconcile
    wall_times = iter([0, 0, 240, 300])
    active_times = iter([0, 0, 60, 120])
    daemon.run_forever(
        sleep=lambda _seconds: None, now=lambda: next(wall_times), active_now=lambda: next(active_times), max_ticks=3
    )

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

    d = Daemon(
        "fix",
        lambda: WorldState(),
        lambda _w: [RunAgent(AgentKind.RESOLVE, 7)] * 2,
        Executor(FakeGateway(), run_agent=run_agent),
        poll_seconds=1,
        dispatch_capacity=lambda: 2,
    )
    try:
        assert d.tick() == [RunAgent(AgentKind.RESOLVE, 7)]
    finally:
        release.set()
        d.wait_for_dispatches()
    assert started == [7]


def _probe(*answers: bool):
    """A `watch_store` whose nth wait probes `answers[n]` (then nothing freed)."""
    waits = iter(answers)

    def watch():
        return contextlib.nullcontext(lambda answer=next(waits, False): answer)

    return watch


def test_reservation_sweep_cadence_is_elapsed_time_not_tick_count():
    # Wakes tick every 5 s against a 10 s poll; the sweep stays once per
    # 3 poll intervals (30 s) of elapsed time, not once per 3 ticks.
    sweeps, clock = [], ManualClock()
    daemon = Daemon(
        "fix",
        lambda: WorldState(),
        lambda _world: [],
        Executor(FakeGateway()),
        poll_seconds=10,
        reconcile=sweeps.append,
        reservation_sweep_polls=3,
        watch_store=_probe(*[True] * 13),
    )

    daemon.run_forever(sleep=clock.advance, now=lambda: int(clock()), active_now=clock, max_ticks=13)

    assert clock() == 65
    assert sweeps == [RecoveryTrigger.PERIODIC] * 2


def _observed_at(clock, observed):
    return lambda: observed.append(clock()) or WorldState()


def test_without_a_trigger_the_poll_still_fires_and_nothing_busy_loops(tmp_path):
    # Commits that free nothing here (another repository's legs every second,
    # a new claim) end no wait early.
    store = SQLiteControlStore(str(tmp_path / "state" / "control.db"))
    store.initialize()
    clock, observed, logs = ManualClock(), [], []

    def sleep(seconds):
        clock.advance(seconds)
        store.record_unpaid_leg(RunId(f"other-{clock()}"), repository="other/repo", role="review", kind="REVIEW",
                                owner_id="other/repo:review", issue=1, number=1, head="", runtime="claude")
        if clock() == 30:
            assert store.lease("o/r", "review", 7, "o/r:review")

    daemon = Daemon("fix", _observed_at(clock, observed), lambda _world: [], Executor(FakeGateway()),
                    poll_seconds=60, log=logs.append,
                    watch_store=StoreWake(store.path, "o/r", "fix", log=logs.append).watch)
    daemon.run_forever(sleep=sleep, now=lambda: int(clock()), active_now=clock, max_ticks=3)

    assert observed == [0, 60, 120]
    assert not any("woke" in m for m in logs)


def test_an_unwired_daemon_waits_the_full_poll():
    clock, observed = ManualClock(), []
    d, _ = _daemon(lambda _w: [], observe=_observed_at(clock, observed))
    d.run_forever(sleep=clock.advance, now=lambda: int(clock()), active_now=clock, max_ticks=3)
    assert observed == [0, 60, 120]


def test_a_wake_starts_the_next_tick_at_the_gap_and_logs_its_reason_once():
    clock, observed, logs = ManualClock(), [], []
    d, _ = _daemon(lambda _w: [], observe=_observed_at(clock, observed), logs=logs)
    d.watch_store = _probe(True)
    d.run_forever(sleep=clock.advance, now=lambda: int(clock()), active_now=clock, max_ticks=3)
    assert observed == [0, 5, 65]
    assert [m for m in logs if "woke" in m] == ["fix: woke: the other role freed an item"]


@pytest.mark.parametrize(("retry_after", "back_off"), [(120, 120), (None, 60)])
def test_after_github_refuses_a_tick_no_trigger_shortens_the_back_off(retry_after, back_off):
    clock, observed = ManualClock(), []

    def observe():
        observed.append(clock())
        if len(observed) == 1:
            raise GhUnavailable("rate-limit", "HTTP 429", retry_after_s=retry_after)
        return WorldState()

    d, _ = _daemon(lambda w: [], observe=observe)
    d.watch_store = _probe(True, True)
    d.run_forever(sleep=clock.advance, now=lambda: int(clock()), active_now=clock, max_ticks=2)
    assert observed == [0, back_off]  # the back-off in full; the next interval is woken
    assert clock() == back_off + 5


_FREE_ITEM_ELSEWHERE = """
import sys
from issue_agent.adapters.control_store import SQLiteControlStore
assert SQLiteControlStore(sys.argv[1]).unlease("o/r", "review", 7, "o/r:review")
"""


def test_an_item_freed_in_another_process_starts_the_next_tick_before_the_poll(tmp_path):
    store = SQLiteControlStore(str(tmp_path / "state" / "control.db"))
    store.initialize()
    assert store.lease("o/r", "review", 7, "o/r:review")
    clock, observed, logs = ManualClock(), [], []

    def sleep(seconds):
        clock.advance(seconds)
        if clock() == 10:
            subprocess.run([sys.executable, "-c", _FREE_ITEM_ELSEWHERE, store.path], check=True, timeout=60)

    daemon = Daemon("fix", _observed_at(clock, observed), lambda _world: [], Executor(FakeGateway()),
                    poll_seconds=60, log=logs.append,
                    watch_store=StoreWake(store.path, "o/r", "fix", log=logs.append).watch)

    daemon.run_forever(sleep=sleep, now=lambda: int(clock()), active_now=clock, max_ticks=2)

    assert observed == [0, 10]  # the check that saw the release, not the 60 s poll
    assert "fix: woke: the other role freed an item" in logs


def test_a_freed_slot_wakes_the_next_tick_only_once_the_leg_has_left_the_pool(tmp_path):
    # The race the settle timer hid: a role releases its claim inside the
    # dispatch, before the dispatch leaves the pool. A tick woken by the
    # store's release would count the slot taken, dispatch nothing, and wait
    # the full poll. The role wakes on its pool instead, and that tick fills
    # the freed slot.
    store = SQLiteControlStore(str(tmp_path / "state" / "control.db"))
    store.initialize()
    clock, observed, started = ManualClock(), [], []
    leased, released, leave, unleased = threading.Event(), threading.Event(), threading.Event(), threading.Event()

    def run_agent(action):
        started.append(action.number)
        if len(started) > 1:
            return
        assert store.lease("o/r", "fix", 7, "o/r:fix")
        leased.set()
        assert released.wait(timeout=5)
        assert store.unlease("o/r", "fix", 7, "o/r:fix")
        unleased.set()
        assert leave.wait(timeout=5)  # still in the pool, its claim gone

    def sleep(seconds):
        assert leased.wait(timeout=5)  # the wait sees the claim held before it goes
        clock.advance(seconds)
        if clock() == 10:
            released.set()
            assert unleased.wait(timeout=5)
        if clock() == 20:
            leave.set()
            d.wait_for_dispatches()

    d = _paid(run_agent, poll_seconds=60, observe=_observed_at(clock, observed),
              watch_store=StoreWake(store.path, "o/r", "fix", log=lambda _m: None).watch)
    try:
        d.run_forever(sleep=sleep, now=lambda: int(clock()), active_now=clock, max_ticks=2)
    finally:
        released.set()
        leave.set()
        d.wait_for_dispatches()

    assert observed == [0, 20]
    assert started == [7, 7]  # the woken tick dispatched into the freed slot


def test_a_leg_that_leaves_the_pool_while_the_tick_runs_wakes_the_next_wait():
    # The count is read before the tick, so a leg ending at any point in it
    # (here inside decide) is never absorbed by the wait that follows.
    clock, observed, release = ManualClock(), [], threading.Event()

    def decide(_world):
        if len(observed) == 2:
            release.set()
            d.wait_for_dispatches()
            return []
        return [RunAgent(AgentKind.RESOLVE, 7)]

    d = _paid(lambda _a: release.wait(timeout=5), poll_seconds=60, observe=_observed_at(clock, observed))
    d.decide = decide
    try:
        d.run_forever(sleep=clock.advance, now=lambda: int(clock()), active_now=clock, max_ticks=3)
    finally:
        release.set()
        d.wait_for_dispatches()
    assert observed == [0, 60, 65]


_CRITERIA = PostComment(Target.ISSUE, 1096, "Acceptance criteria: …", key="triage:1096")


def test_a_tick_that_changed_github_observes_the_result_after_the_gap_not_the_poll():
    # #1096: the triage-criteria comment made RESOLVE dispatchable, but the
    # loop slept the full poll before observing it (83 s comment to dispatch).
    clock, decided_at, logs = ManualClock(), [], []

    def decide(_world):
        decided_at.append(clock())
        return [_CRITERIA] if len(decided_at) == 1 else []

    d, gw = _daemon(decide, logs=logs)
    d.run_forever(sleep=clock.advance, now=lambda: int(clock()), active_now=clock, max_ticks=2)

    assert decided_at == [0, 5]
    assert "fix: woke: re-tick after a GitHub change" in logs


def test_a_mutation_every_tick_repeats_costs_one_extra_observe_per_interval_never_a_loop():
    # An idempotent re-post (or a merge GitHub has not taken yet) is decided
    # on every tick: the re-tick that repeats it does not re-tick again.
    clock, observed = ManualClock(), []
    d, _ = _daemon(lambda _w: [_CRITERIA], observe=_observed_at(clock, observed))
    d.run_forever(sleep=clock.advance, now=lambda: int(clock()), active_now=clock, max_ticks=5)
    assert observed == [0, 5, 65, 70, 130]


def test_every_legs_end_wakes_the_next_wait_back_to_back_never_sooner_than_the_gap():
    # #1341: the steward's end woke the loop, the triage after it waited a full
    # poll (88 s to its comment). Each link of a chain wakes; none faster than
    # the gap, however soon its leg ends.
    clock, observed, logs = ManualClock(), [], []
    d = _paid(lambda _a: None, poll_seconds=60, observe=_observed_at(clock, observed), log=logs.append)

    def sleep(seconds):
        d.wait_for_dispatches()  # each leg has left the pool before the next check
        clock.advance(seconds)

    d.run_forever(sleep=sleep, now=lambda: int(clock()), active_now=clock, max_ticks=5)
    assert observed == [0, 5, 10, 15, 20]
    assert logs.count("fix: woke: a leg of this role ended") == 5  # one per wait, the last included


def test_a_dispatch_that_raises_every_tick_is_retried_at_the_poll_never_the_gap():
    # A worker's own GitHub read failing before its lease records no leg, so
    # nothing backs it off: its end must not wake the loop.
    clock, observed, logs = ManualClock(), [], []

    def fails(_action):
        raise RuntimeError("gh issue view timed out")

    d = _paid(fails, poll_seconds=60, observe=_observed_at(clock, observed), log=logs.append)

    def sleep(seconds):
        d.wait_for_dispatches()
        clock.advance(seconds)

    d.run_forever(sleep=sleep, now=lambda: int(clock()), active_now=clock, max_ticks=3)
    assert observed == [0, 60, 120]
    assert "fix: woke: a leg of this role ended" not in logs


def test_a_wake_by_the_other_role_does_not_spend_the_next_own_wake():
    clock, observed = ManualClock(), []
    d, _ = _daemon(lambda _w: [_CRITERIA], observe=_observed_at(clock, observed))
    d.watch_store = _probe(False, True)
    d.run_forever(sleep=clock.advance, now=lambda: int(clock()), active_now=clock, max_ticks=4)
    assert observed == [0, 5, 10, 15]  # re-tick, store wake, re-tick


def test_a_tick_that_only_dispatched_waits_for_the_leg_not_a_retick():
    # A dispatch changes nothing on GitHub; its end wakes the loop.
    clock, observed, release = ManualClock(), [], threading.Event()
    d = _paid(lambda _a: release.wait(timeout=5), poll_seconds=60, observe=_observed_at(clock, observed))
    try:
        d.run_forever(sleep=clock.advance, now=lambda: int(clock()), active_now=clock, max_ticks=2)
    finally:
        release.set()
        d.wait_for_dispatches()
    assert observed == [0, 60]


class _Refusing:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def execute(self, _action) -> None:
        raise self.error


@pytest.mark.parametrize(("error", "waits"), [
    (GhUnavailable("rate-limit", "HTTP 403: secondary rate limit", retry_after_s=90), [0, 90, 180]),
    (GhUnavailable("rate-limit", "HTTP 403: secondary rate limit"), [0, 60, 120]),
    (RuntimeError("comment not confirmed"), [0, 60, 120]),
])
def test_a_github_write_that_failed_backs_off_instead_of_reticking(error, waits):
    clock, observed = ManualClock(), []
    d = Daemon("fix", _observed_at(clock, observed), lambda _w: [_CRITERIA], _Refusing(error), poll_seconds=60,
               watch_store=_probe(True, True))
    d.run_forever(sleep=clock.advance, now=lambda: int(clock()), active_now=clock, max_ticks=3)
    if isinstance(error, GhUnavailable):  # a refusal is never woken early, by any trigger
        assert observed == waits
    else:  # a failed write is no re-tick; the store's trigger still wakes
        assert observed == [0, 5, 10]
    assert d.tick() == []  # a failed write is not a change taken


def test_the_retick_gap_never_exceeds_a_shorter_poll():
    clock, observed = ManualClock(), []
    d, _ = _daemon(lambda _w: [_CRITERIA], observe=_observed_at(clock, observed))
    d.poll_seconds = 2
    d.run_forever(sleep=clock.advance, now=lambda: int(clock()), active_now=clock, max_ticks=3)
    assert observed == [0, 2, 4]


def test_a_mutation_beside_a_dispatch_still_reticks():
    clock, observed, release = ManualClock(), [], threading.Event()
    d = _paid(lambda _a: release.wait(timeout=5), poll_seconds=60, observe=_observed_at(clock, observed))
    d.decide = lambda _w: [_CRITERIA, RunAgent(AgentKind.RESOLVE, 7)]
    try:
        d.run_forever(sleep=clock.advance, now=lambda: int(clock()), active_now=clock, max_ticks=2)
    finally:
        release.set()
        d.wait_for_dispatches()
    assert observed == [0, 5]


def test_a_shutdown_signal_interrupts_the_wait_promptly():
    # The daemon's SIGTERM handler raises SystemExit in the main thread; the
    # wait's real sleep must let it through instead of finishing the interval.
    d, _ = _daemon(lambda _w: [])

    def stop(_signum, _frame):
        raise SystemExit(0)

    previous = signal.signal(signal.SIGALRM, stop)
    signal.setitimer(signal.ITIMER_REAL, 0.2)
    started = time.monotonic()
    try:
        with pytest.raises(SystemExit):
            d.run_forever(sleep=time.sleep, now=lambda: int(time.time()))
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    assert time.monotonic() - started < 5


def _drain_daemon(decisions, run_agent, heartbeat="", capacity=1):
    """A fix daemon whose nth tick decides `decisions[n]` (then nothing)."""
    ticks = []

    def decide(_world):
        ticks.append(1)
        return list(decisions[len(ticks) - 1]) if len(ticks) <= len(decisions) else []

    return Daemon("fix", lambda: WorldState(), decide, Executor(FakeGateway(), run_agent=run_agent),
                  poll_seconds=1, dispatch_capacity=lambda: capacity, heartbeat_path=heartbeat)


def _wait_until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.01)


def test_a_reload_drain_holds_heavy_legs_but_starts_light_ones_while_a_heavy_leg_runs(tmp_path):
    # #1105: a harness merge held STEWARD/TRIAGE for 45 min behind #1078's
    # FIX_CI. Light legs keep starting while the heavy one drains; heavy ones wait.
    release, started = threading.Event(), []

    def run_agent(action):
        started.append((action.kind, action.number))
        if action.kind is AgentKind.FIX_CI:
            assert release.wait(timeout=5)

    heartbeat = tmp_path / "heartbeat.json"
    daemon = _drain_daemon([
        [RunAgent(AgentKind.FIX_CI, 1078, head="h1", evidence=CiFailures((CiFailure("check:1@9.1", "smoke", 1, 9),)))],
        [RunAgent(AgentKind.STEWARD, 1105), RunAgent(AgentKind.RESOLVE, 9)],
    ], run_agent, str(heartbeat), capacity=2)  # a heavy slot stays free: only the drain holds RESOLVE 9
    beats, slots = [], []

    def sleep(_seconds):
        beats.append(json.loads(heartbeat.read_text()))
        slots.append((daemon.available_dispatch_slots, daemon.available_light_slots))
        if len(beats) == 2:  # STEWARD was admitted on tick 2 while FIX_CI ran
            release.set()
            # The no-op sleep spins ticks faster than the released worker gets the
            # GIL; join on the drain's own fact, not a tick count.
            daemon.wait_for_dispatches()

    with pytest.raises(SystemExit) as stopped:
        daemon.run_forever(sleep=sleep, now=lambda: 0, max_ticks=10, reload_check=lambda: True)

    assert stopped.value.code == RELOAD_EXIT_CODE
    assert len(beats) == 2  # exits on the first tick after the heavy leg drains
    # Worker threads append in scheduling order, not submission order.
    assert len(started) == 2 and set(started) == {(AgentKind.FIX_CI, 1078), (AgentKind.STEWARD, 1105)}  # RESOLVE 9 held
    assert beats[0]["reload_pending"] is True
    assert beats[0]["in_flight"] == [{"kind": "FIX_CI", "number": 1078}]
    assert slots[0] == (0, 2)  # decide sees no heavy slot but the whole light lane


def test_once_no_heavy_leg_drains_no_light_leg_starts_so_the_reload_exits():
    # No livelock: a light leg decided on every tick must not defer the exit.
    release, started = threading.Event(), []

    def run_agent(action):
        started.append(action.number)
        if action.number == 1:
            assert release.wait(timeout=5)

    daemon = _drain_daemon([[RunAgent(AgentKind.TRIAGE, n)] for n in range(1, 50)], run_agent)
    sleeps = []

    def sleep(_seconds):
        sleeps.append(1)
        if len(sleeps) == 3:
            release.set()  # the one light leg in flight when the reload came ends
            _wait_until(lambda: not daemon.active_dispatches)

    with pytest.raises(SystemExit) as stopped:
        daemon.run_forever(sleep=sleep, now=lambda: 0, max_ticks=50, reload_check=lambda: len(sleeps) >= 1)

    assert stopped.value.code == RELOAD_EXIT_CODE
    assert started == [1]
    assert len(sleeps) == 3  # exits on the first tick after that leg ends


def test_light_legs_started_during_the_drain_drain_before_the_exit():
    release_heavy, release_light, started, finished = threading.Event(), threading.Event(), [], []

    def run_agent(action):
        started.append(action.number)
        assert (release_heavy if action.kind is AgentKind.RESOLVE else release_light).wait(timeout=5)
        finished.append(action.number)

    daemon = _drain_daemon([[RunAgent(AgentKind.RESOLVE, 7)], [RunAgent(AgentKind.TRIAGE, 8)]], run_agent)
    sleeps = []

    def sleep(_seconds):
        sleeps.append(1)
        if len(sleeps) == 2:
            _wait_until(lambda: 8 in started)
            release_heavy.set()
            _wait_until(lambda: 7 in finished)
        if len(sleeps) == 4:
            assert finished == [7]  # still draining the light leg: no exit yet
            release_light.set()
            _wait_until(lambda: not daemon.active_dispatches)

    with pytest.raises(SystemExit) as stopped:
        daemon.run_forever(sleep=sleep, now=lambda: 0, max_ticks=20, reload_check=lambda: True)

    assert stopped.value.code == RELOAD_EXIT_CODE
    assert finished == [7, 8]


def test_dispatch_capacity_is_read_at_each_use_so_a_live_change_applies_next_tick():
    """FIX_DISPATCH_CONCURRENCY is live (ADR 0007 amendment): raising it lets
    the next tick start more legs; lowering it admits none past the new bound."""
    release = threading.Event()
    capacity = [1]
    issues = iter(range(1, 10))

    daemon = Daemon(
        "fix", lambda: WorldState(), lambda _world: [RunAgent(AgentKind.RESOLVE, next(issues))],
        Executor(FakeGateway(), run_agent=lambda _action: release.wait(timeout=5)),
        dispatch_capacity=lambda: capacity[0],
    )
    try:
        assert len(daemon.tick()) == 1
        assert daemon.available_dispatch_slots == 0 and daemon.tick() == []
        capacity[0] = 3
        assert daemon.available_dispatch_slots == 2
        assert len(daemon.tick()) == 1 and len(daemon.tick()) == 1
        capacity[0] = 1
        assert daemon.available_dispatch_slots == 0 and daemon.tick() == []
    finally:
        release.set()
        daemon.wait_for_dispatches()
