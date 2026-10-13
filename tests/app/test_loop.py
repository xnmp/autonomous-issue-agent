"""Daemon loop — tick sequencing, crash isolation, heartbeat."""

from tests.domain.builders import observed_pr

import asyncio
import json
import threading

import pytest

from tests.fakes.legacy_journal import Journal
from issue_agent.adapters.journal import read_events
from issue_agent.adapters.github import GhUnavailable
from tests.fakes.executor import ImmediateExecutor as Executor
from issue_agent.app.tick import Daemon
from issue_agent.domain.actions import PostComment, Relabel, Target
from issue_agent.domain.labels import Label
from issue_agent.domain.recovery import RecoveryTrigger
from issue_agent.domain.world import WorldState
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
    ex = Executor(gw)
    d = Daemon(
        name="fix",
        observe=observe or (lambda: WorldState()),
        decide=decide,
        executor=ex,
        poll_seconds=60,
        log=(logs.append if logs is not None else (lambda m: None)),
    )
    return d, gw


def test_tick_observes_decides_executes():
    d, gw = _daemon(lambda w: [Relabel(Target.ISSUE, 5, Label.AI_READY, Label.AI_WORKING)])
    actions = d.tick()
    assert len(actions) == 1
    assert gw.calls == [("relabel", "issue", 5, "ai-ready", "ai-working")]


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
    from issue_agent.domain.actions import AgentKind, RunAgent

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
        "fix", lambda: WorldState(),
        lambda w: [
            Relabel(Target.ISSUE, 500, Label.AI_READY, Label.AI_WORKING),
            RunAgent(AgentKind.RESOLVE, 500),
            PostComment(Target.PR, 537, "done"),
            RunAgent(AgentKind.REBASE, 534),
        ],
        ex,
        dispatch_capacity=2,
    )
    taken = d.tick()
    d.wait_for_dispatches()
    assert executed[:2] == [
        ("relabel", 500),      # the dispatch's own label move still precedes it
        ("comment", 537),      # finished work lands before any session starts
    ]
    assert sorted(executed[2:]) == [("run", 500), ("run", 534)]
    # Dispatch order among dispatches is preserved at submission.
    assert [a.number for a in taken if isinstance(a, RunAgent)] == [500, 534]


def test_observation_and_lifecycle_continue_while_the_only_dispatch_slot_is_busy():
    """N=1 remains serial for paid work but no longer makes the daemon blind."""
    from issue_agent.domain.actions import AgentKind, RunAgent

    started = threading.Event()
    release = threading.Event()
    observations = []

    class LiveGateway(FakeGateway):
        def __init__(self):
            super().__init__(
                prs={24: observed_pr(24, head_ref_oid="head-24")}
            )
            self.arm_attempts = 0

        def comment(self, kind, number, body):
            self.arm_attempts += 1
            if self.arm_attempts == 1:
                raise RuntimeError("comment not posted")
            super().comment(kind, number, body)

    gateway = LiveGateway()

    def run_agent(_action):
        started.set()
        assert release.wait(timeout=5)

    executor = Executor(gateway, run_agent=run_agent)
    daemon = Daemon(
        "fix",
        lambda: observations.append(len(observations) + 1) or WorldState(),
        lambda _world: [
            PostComment(Target.PR, 24, "ready"),
            RunAgent(AgentKind.RESOLVE, 7),
        ],
        executor,
        dispatch_capacity=1,
    )

    first = daemon.tick()
    assert started.wait(timeout=1)
    second = daemon.tick()

    assert observations == [1, 2]
    assert gateway.arm_attempts == 2, "a failed write retries on the next poll"
    assert [a for a in first + second if isinstance(a, RunAgent)] == [
        RunAgent(AgentKind.RESOLVE, 7)
    ], "the same issue is never double-dispatched"

    release.set()
    daemon.wait_for_dispatches()


def test_background_dispatch_is_bounded_and_admits_independent_targets():
    from issue_agent.domain.actions import AgentKind, RunAgent

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
        dispatch_capacity=2,
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
    from issue_agent.adapters.reload import RELOAD_EXIT_CODE
    from issue_agent.domain.actions import AgentKind, RunAgent

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
        lambda _world: [RunAgent(AgentKind.RESOLVE, 7)] + (
            [PostComment(Target.PR, 100, "rerun")]
            if len(observations) == 2 else []
        ),
        Executor(gateway, run_agent=run_agent),
        poll_seconds=1,
        dispatch_capacity=1,
    )

    with pytest.raises(SystemExit) as stopped:
        daemon.run_forever(
            sleep=sleep,
            now=lambda: 0,
            max_ticks=4,
            reload_check=lambda: True,
        )

    assert started.is_set()
    assert stopped.value.code == RELOAD_EXIT_CODE
    assert observations == [1, 2, 3]


def test_operator_interrupt_cancels_async_dispatch_and_joins_pool():
    from issue_agent.app.roles import CancellableAsyncDispatch
    from issue_agent.domain.actions import AgentKind, RunAgent

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
    daemon = Daemon(
        "fix",
        lambda: WorldState(),
        lambda _world: [RunAgent(AgentKind.RESOLVE, 7)],
        Executor(FakeGateway(), run_agent=runtime),
        poll_seconds=1,
        dispatch_capacity=1,
        prepare_shutdown=prepared.set,
        cancel_active_dispatches=runtime.cancel_all,
        terminalize=terminalize,
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
    from issue_agent.domain.actions import AgentKind, RunAgent
    from issue_agent.domain.supervision import ExitCode

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

    daemon = Daemon(
        "review",
        lambda: WorldState(),
        lambda _world: [RunAgent(AgentKind.REVIEW, 7)],
        Executor(FakeGateway(), run_agent=run_agent),
        poll_seconds=1,
        dispatch_capacity=1,
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
            daemon.run_forever(
                sleep=before_shutdown,
                now=lambda: 0,
                max_ticks=1,
            )
        assert stopped.type is SystemExit
        assert stopped.value.code is ExitCode.TRANSIENT
        assert forced == ([] if exhausted else [True])
        assert daemon.requires_hard_exit is True
    finally:
        release.set()
        daemon.wait_for_dispatches()


def test_shutdown_reaps_killed_worker_before_final_spend_reconciliation():
    from issue_agent.domain.actions import AgentKind, RunAgent

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

    daemon = Daemon(
        "fix",
        lambda: WorldState(),
        lambda _world: [RunAgent(AgentKind.RESOLVE, 7)],
        Executor(
            FakeGateway(),
            run_agent=run_agent,
        ),
        poll_seconds=1,
        dispatch_capacity=1,
        shutdown_timeout_seconds=_SHUTDOWN_TIMEOUT,
        monotonic=clock,
        cancel_active_dispatches=lambda: clock.advance(_GRACEFUL_WINDOW),
        force_kill_dispatches=kill,
        terminalize=terminalize,
    )

    def interrupt(_seconds):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        daemon.run_forever(sleep=interrupt, now=lambda: 0, max_ticks=1)

    assert order == ["kill", "worker_accounted", "terminalize"]
    daemon.wait_for_dispatches()


def test_shutdown_retry_finishes_cleanup_after_dispatch_owner_settles():
    from issue_agent.domain.actions import AgentKind, RunAgent

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

    daemon = Daemon(
        "fix",
        lambda: WorldState(),
        lambda _world: [RunAgent(AgentKind.RESOLVE, 7)],
        Executor(FakeGateway(), run_agent=run_agent),
        dispatch_capacity=1,
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


def test_shutdown_deadline_preserves_indeterminate_spend_for_boot_recovery(
    tmp_path,
):
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from issue_agent.adapters.control_store import (
        BudgetLimit,
        ReservationRequest,
        SQLiteControlStore,
    )
    from issue_agent.domain.actions import AgentKind, RunAgent
    from issue_agent.domain.contracts import (
        Money,
        RunId,
        daily_budget_scope,
    )

    control = SQLiteControlStore(str(tmp_path / "control.db"))
    control.initialize()
    now = datetime.now(timezone.utc)
    run_id = RunId("deadline-run")
    assert control.lease("o/r", "fix", 7, "o/r:fix")
    request = ReservationRequest(
        run_id, "o/r", "fix", "RESOLVE", "o/r:fix", Money("USD", 1_000_000),
        (BudgetLimit(daily_budget_scope("o/r", "repository", "USD", now, ZoneInfo("UTC")),
                     Money("USD", 2_000_000)),),
        7, issue=7, number=7,
    )
    assert control.reserve(request).admitted
    control.register_process_lease(
        run_id,
        expected_owner_id=request.owner_id,
        pid=123,
        pgid=123,
        process_start_ticks=99,
    )
    clock = ManualClock()
    started = threading.Event()
    release = threading.Event()

    def run_agent(_action):
        started.set()
        release.wait(timeout=5)

    daemon = Daemon(
        "fix",
        lambda: WorldState(),
        lambda _world: [RunAgent(AgentKind.RESOLVE, 7)],
        Executor(FakeGateway(), run_agent=run_agent),
        poll_seconds=1,
        dispatch_capacity=1,
        shutdown_timeout_seconds=_SHUTDOWN_TIMEOUT,
        monotonic=clock,
        cancel_active_dispatches=lambda: clock.advance(_GRACEFUL_WINDOW),
        prepare_shutdown=lambda: control.record_owned_cancellation_intents(
            "o/r:fix",
            "shutdown_deadline_spend_unknown",
        ),
        force_kill_dispatches=lambda: clock.advance(_KILL_WINDOW),
    )

    def interrupt(_seconds):
        assert started.wait(timeout=1)
        raise KeyboardInterrupt

    with pytest.raises((KeyboardInterrupt, SystemExit)) as stopped:
        daemon.run_forever(sleep=interrupt, now=lambda: 0, max_ticks=1)
    assert stopped.type is SystemExit

    assert started.is_set()
    assert control.active_reservation_runs("o/r:fix") == (run_id,)
    assert tuple(
        lease.run_id for lease in control.leases()
    ) == (run_id,)
    assert control.has_cancellation_intent(run_id)
    release.set()
    daemon.wait_for_dispatches()


def test_application_shutdown_deadline_fits_inside_every_systemd_hard_stop():
    from pathlib import Path
    import re

    from issue_agent.app.tick import DEFAULT_SHUTDOWN_TIMEOUT_SECONDS

    root = Path(__file__).parents[2]
    units = tuple((root / "ops" / "systemd").glob("issue-agent-*.service"))
    assert units
    hard_stops = []
    for path in units:
        match = re.search(
            r"^TimeoutStopSec=(\d+(?:\.\d+)?)$",
            path.read_text(),
            re.M,
        )
        assert match is not None
        hard_stops.append(float(match.group(1)))
    assert all(DEFAULT_SHUTDOWN_TIMEOUT_SECONDS + 5 < stop for stop in hard_stops)


def test_interrupt_before_worker_registration_is_sticky():
    from issue_agent.app.roles import CancellableAsyncDispatch
    from issue_agent.domain.actions import AgentKind, RunAgent

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
    daemon = Daemon(
        "fix",
        lambda: WorldState(),
        lambda _world: [RunAgent(AgentKind.RESOLVE, 7)],
        Executor(FakeGateway(), run_agent=runtime),
        poll_seconds=1,
        dispatch_capacity=1,
        cancel_active_dispatches=runtime.cancel_all,
    )

    def interrupt(_seconds):
        assert registering.wait(timeout=1)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        daemon.run_forever(sleep=interrupt, now=lambda: 0)

    assert daemon.active_dispatches == ()


def test_tick_isolated_from_action_crash():
    logs = []
    class BadGateway:
        def relabel(self, *a):
            raise RuntimeError("gh down")
    d = Daemon("fix", lambda: WorldState(),
               lambda w: [Relabel(Target.ISSUE, 5, Label.AI_READY, None)],
               Executor(BadGateway()), log=logs.append)
    d.tick()  # must not raise
    assert any("action" in m and "failed" in m for m in logs)


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
        return [Relabel(Target.ISSUE, 5, Label.AI_READY, Label.AI_WORKING)]

    d, gw = _daemon(decide, observe=rate_limited, logs=logs)
    assert d.tick() == []
    assert decided == []          # never asked the domain about a world we can't see
    assert gw.calls == []         # and never touched the repo


def test_gh_unavailable_logs_one_concise_loud_line():
    """An operator scanning the log needs the cause and the consequence on ONE
    grep-able line — not a generic 'poll pass failed' dump that reads like a bug."""
    logs = []
    def unavailable():
        raise GhUnavailable(
            "rate-limit", "HTTP 403: API rate limit exceeded\n  for user ID 42\n  see docs"
        )
    d, _ = _daemon(lambda w: [], observe=unavailable, logs=logs)
    d.tick()
    assert len(logs) == 1
    line = logs[0]
    assert "\n" not in line
    assert "world build failed" in line and "rate-limited" in line
    assert "skipping tick" in line
    assert "API rate limit exceeded" in line          # the cause survives
    assert "poll pass failed" not in line             # not the generic arm


def test_non_infra_observe_failure_still_uses_the_generic_arm():
    """Only infra gets the special line; a real bug in observe must stay
    recognisable as a bug rather than be dressed up as a rate limit."""
    logs = []
    def boom():
        raise KeyError("headRefName")
    d, _ = _daemon(lambda w: [], observe=boom, logs=logs)
    assert d.tick() == []
    assert any("poll pass failed" in m for m in logs)
    assert not any("world build failed" in m for m in logs)


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


def test_run_forever_bounded_by_max_ticks():
    calls = {"n": 0}
    def decide(world):
        calls["n"] += 1
        return []
    d, _ = _daemon(decide)
    slept = []
    d.run_forever(sleep=slept.append, now=lambda: 0, max_ticks=3)
    assert calls["n"] == 3
    assert slept == [60, 60, 60]


def test_normal_shutdown_persists_stopped_heartbeat(tmp_path):
    heartbeat = tmp_path / "heartbeat.json"
    d, _ = _daemon(lambda _world: [])
    d.heartbeat_path = str(heartbeat)

    d.run_forever(
        sleep=lambda _seconds: None,
        now=lambda: 0,
        max_ticks=1,
    )

    payload = json.loads(heartbeat.read_text())
    assert payload["state"] == "stopped"
    assert payload["in_flight"] == []


def test_scheduler_progress_covers_tick_and_quiet_wait():
    progress = []

    def observe():
        assert progress == ["completed"]
        return WorldState()

    d, _ = _daemon(lambda _world: [], observe=observe)
    d.run_forever(
        sleep=lambda _seconds: None,
        now=lambda: 0,
        max_ticks=1,
        scheduler_progress=lambda: progress.append("completed"),
    )

    assert progress == ["completed"] * 5


def test_failed_shutdown_deadline_overrides_clean_signal_exit():
    from issue_agent.domain.supervision import ExitCode

    d, _ = _daemon(lambda _world: [])
    d.shutdown_dispatches = lambda *, cancel: False

    with pytest.raises(SystemExit) as stopped:
        d.run_forever(
            sleep=lambda _seconds: (_ for _ in ()).throw(
                SystemExit(ExitCode.CLEAN)
            ),
            now=lambda: 0,
        )

    assert stopped.value.code is ExitCode.TRANSIENT


def test_run_forever_warns_on_suspend_stall():
    logs = []
    d, _ = _daemon(lambda w: [], logs=logs)
    # now() jumps far beyond poll*4 between ticks → suspend-stall warning.
    times = iter([0, 100_000, 100_060])
    d.run_forever(sleep=lambda s: None, now=lambda: next(times), max_ticks=2)
    assert any("suspended/stalled" in m for m in logs)


def test_suspend_recovery_waits_for_active_dispatch_owner():
    from issue_agent.domain.actions import AgentKind, RunAgent

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
        dispatch_capacity=1,
        log=log,
    )

    def reconcile(trigger):
        assert daemon.active_dispatches == ()
        recoveries.append(trigger)

    daemon.reconcile = reconcile
    wall_times = iter([0, 0, 240, 300])
    active_times = iter([0, 0, 60, 120])
    daemon.run_forever(
        sleep=lambda _seconds: None,
        now=lambda: next(wall_times),
        active_now=lambda: next(active_times),
        max_ticks=3,
    )

    assert started.is_set()
    assert started_actions == [7]
    assert recoveries == [RecoveryTrigger.WAKE]


def test_suspend_recovery_never_replays_actions_from_telemetry(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    rows = [
        {
            "ts": "1970-01-01T00:00:10+00:00",
            "event": "action",
            "desc": "comment issue #5 (7 chars)",
            "action": {
                "type": "comment",
                "target": "ISSUE",
                "number": 5,
                "body": "blocked",
            },
        },
        {
            "ts": "1970-01-01T00:00:30+00:00",
            "event": "agent_run_start",
            "role": "fix",
            "kind": "RESOLVE",
            "number": 5,
            "issue": 5,
            "dispatch": "dispatch-5",
        },
        {
            "ts": "1970-01-01T00:04:00+00:00",
            "event": "agent_run",
            "role": "fix",
            "kind": "RESOLVE",
            "number": 5,
            "issue": 5,
            "dispatch": "dispatch-5",
            "duration_s": 30,
        },
    ]
    tmp_path.joinpath("journal.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows)
    )

    class CommentGateway(FakeGateway):
        def __init__(self):
            super().__init__()
            self.visible = False

        def comment(self, kind, number, body):
            self.calls.append(("comment", number, body))
            self.visible = True

        def has_comment(self, kind, number, body):
            return self.visible

    gateway = CommentGateway()
    recoveries = []
    daemon = Daemon(
        "fix",
        lambda: WorldState(),
        lambda _world: [],
        Executor(
            gateway,
            journal=journal,
            retry_delays=(),
        ),
        poll_seconds=60,
        journal=journal,
        reconcile=recoveries.append,
    )

    times = iter([0, 240])
    active_times = iter([0, 60])
    daemon.run_forever(
        sleep=lambda _seconds: None,
        now=lambda: next(times),
        active_now=lambda: next(active_times),
        max_ticks=1,
    )

    events = read_events(journal.path)
    stall = [event for event in events if event["event"] == "stall"]
    assert stall == [
        {
            **stall[0],
            "event": "stall",
            "started_at": "1970-01-01T00:01:00+00:00",
            "woke_at": "1970-01-01T00:04:00+00:00",
            "elapsed_seconds": 240,
            "suspended_seconds": 180,
        }
    ]
    assert gateway.calls == []
    assert recoveries == [RecoveryTrigger.WAKE]
    assert not any(event.get("replayed") is True for event in events)
    assert not any(event["event"] == "agent_run_stall" for event in events)


def test_reservation_sweep_runs_on_bounded_tick_cadence():
    sweeps = []
    daemon = Daemon(
        "fix",
        lambda: WorldState(),
        lambda _world: [],
        Executor(FakeGateway()),
        poll_seconds=1,
        reconcile=sweeps.append,
        reservation_sweep_ticks=2,
    )

    daemon.run_forever(
        sleep=lambda _seconds: None,
        now=lambda: 0,
        max_ticks=2,
    )

    assert sweeps == [RecoveryTrigger.PERIODIC]


def test_heartbeat_below_stall_threshold_does_not_reconcile_or_journal(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    receipt = {
        "ts": "1970-01-01T00:00:10+00:00",
        "event": "action",
        "desc": "relabel issue #5: +ai-blocked",
        "action": {
            "type": "relabel",
            "target": "ISSUE",
            "number": 5,
            "remove": None,
            "add": "ai-blocked",
        },
    }
    tmp_path.joinpath("journal.jsonl").write_text(json.dumps(receipt) + "\n")
    gateway = FakeGateway()
    daemon = Daemon(
        "fix",
        lambda: WorldState(),
        lambda _world: [],
        Executor(gateway, journal=journal),
        poll_seconds=60,
        journal=journal,
    )

    times = iter([0, 239])
    daemon.run_forever(
        sleep=lambda _seconds: None,
        now=lambda: next(times),
        max_ticks=1,
    )

    events = read_events(journal.path)
    assert events[0] == receipt
    assert not any(event["event"] in {"stall", "agent_run_stall"} for event in events)
    assert gateway.calls == []


def test_long_active_late_heartbeat_does_not_stall_or_reconcile(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    receipt = {
        "ts": "1970-01-01T00:00:10+00:00",
        "event": "action",
        "desc": "relabel issue #5: +ai-blocked",
        "action": {
            "type": "relabel",
            "target": "ISSUE",
            "number": 5,
            "remove": None,
            "add": "ai-blocked",
        },
    }
    tmp_path.joinpath("journal.jsonl").write_text(json.dumps(receipt) + "\n")
    gateway = FakeGateway()
    daemon = Daemon(
        "fix",
        lambda: WorldState(),
        lambda _world: [],
        Executor(gateway, journal=journal),
        poll_seconds=60,
        journal=journal,
    )
    wall_times = iter([0, 240])
    active_times = iter([0, 240])

    daemon.run_forever(
        sleep=lambda _seconds: None,
        now=lambda: next(wall_times),
        active_now=lambda: next(active_times),
        max_ticks=1,
    )

    assert gateway.calls == []
    events = read_events(journal.path)
    assert events[0] == receipt
    assert not any(event["event"] in {"stall", "agent_run_stall"} for event in events)


def test_stall_diagnostic_does_not_turn_legacy_receipts_into_commands(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    receipt = {
        "ts": "1970-01-01T00:00:30+00:00",
        "event": "action",
        "desc": "relabel issue #5: +ai-blocked",
        "action": {
            "type": "relabel",
            "target": "ISSUE",
            "number": 5,
            "remove": None,
            "add": "ai-blocked",
        },
    }
    tmp_path.joinpath("journal.jsonl").write_text(json.dumps(receipt) + "\n")
    gateway = FakeGateway()
    daemon = Daemon(
        "fix",
        lambda: WorldState(),
        lambda _world: [],
        Executor(gateway, journal=journal),
        poll_seconds=60,
        journal=journal,
    )
    wall_times = iter([50, 300])
    active_times = iter([0, 10])

    daemon.run_forever(
        sleep=lambda _seconds: None,
        now=lambda: next(wall_times),
        active_now=lambda: next(active_times),
        max_ticks=1,
    )

    events = read_events(journal.path)
    assert gateway.calls == []
    assert any(
        event["event"] == "stall"
        and event["started_at"] == "1970-01-01T00:01:00+00:00"
        and event["stall_clock_offset_seconds"] == 290
        for event in events
    )


def test_legacy_run_rows_do_not_synthesize_control_corrections(
    tmp_path,
):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    rows = [
        {
            "ts": "1970-01-01T00:00:10+00:00",
            "event": "agent_run_start",
            "role": "fix",
            "kind": "RESOLVE",
            "number": 5,
            "issue": 5,
            "dispatch": "crossed-gap",
        },
        {
            "ts": "1970-01-01T00:01:40+00:00",
            "event": "agent_run",
            "role": "fix",
            "kind": "RESOLVE",
            "number": 5,
            "issue": 5,
            "dispatch": "crossed-gap",
            "duration_s": 30,
        },
        {
            "ts": "1970-01-01T00:02:40+00:00",
            "event": "agent_run_start",
            "role": "fix",
            "kind": "RESOLVE",
            "number": 7,
            "issue": 7,
            "dispatch": "malformed-duration",
        },
        {
            "ts": "1970-01-01T00:02:50+00:00",
            "event": "agent_run",
            "role": "fix",
            "kind": "RESOLVE",
            "number": 7,
            "issue": 7,
            "dispatch": "malformed-duration",
            "duration_s": -1,
        },
        {
            "ts": "1970-01-01T00:03:10+00:00",
            "event": "agent_run_start",
            "role": "fix",
            "kind": "RESOLVE",
            "number": 6,
            "issue": 6,
            "dispatch": "after-resume",
        },
        {
            "ts": "1970-01-01T00:03:40+00:00",
            "event": "agent_run",
            "role": "fix",
            "kind": "RESOLVE",
            "number": 6,
            "issue": 6,
            "dispatch": "after-resume",
            "duration_s": 30,
        },
    ]
    tmp_path.joinpath("journal.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows)
    )
    daemon = Daemon(
        "fix",
        lambda: WorldState(),
        lambda _world: [],
        Executor(FakeGateway(), journal=journal),
        poll_seconds=60,
        journal=journal,
    )
    wall_times = iter([0, 240])
    active_times = iter([0, 180])

    daemon.run_forever(
        sleep=lambda _seconds: None,
        now=lambda: next(wall_times),
        active_now=lambda: next(active_times),
        max_ticks=1,
    )

    annotations = [
        event for event in read_events(journal.path)
        if event["event"] == "agent_run_stall"
    ]
    assert annotations == []


def _waking_daemon(wake_check, poll_seconds=60, logs=None):
    d, _ = _daemon(lambda w: [], logs=logs)
    d.poll_seconds = poll_seconds
    d.wake_check = wake_check
    return d


def test_without_a_wake_the_full_interval_is_slept():
    d = _waking_daemon(lambda: False, poll_seconds=5)
    slept = []
    d.run_forever(sleep=slept.append, now=lambda: 0, max_ticks=2)
    assert sum(slept) == 10


def test_wake_wiring_does_not_change_max_ticks_or_tick_count():
    """A wake shortens sleep; it must not smuggle in extra ticks."""
    calls = {"n": 0}
    d = _waking_daemon(lambda: True, poll_seconds=60)
    d.decide = lambda w: calls.__setitem__("n", calls["n"] + 1) or []
    d.run_forever(sleep=lambda s: None, now=lambda: 0, max_ticks=3)
    assert calls["n"] == 3


def test_no_wake_check_sleeps_in_one_call_as_before():
    """Regression guard on the unwired path: no slicing, no extra sleep calls."""
    d, _ = _daemon(lambda w: [])
    slept = []
    d.run_forever(sleep=slept.append, now=lambda: 0, max_ticks=2)
    assert slept == [60, 60]


def test_wake_failure_keeps_long_remainder_watchdog_safe():
    progress = []
    slept = []

    def boom():
        raise RuntimeError("stat failed")

    d = _waking_daemon(boom, poll_seconds=300)
    d.run_forever(
        sleep=slept.append,
        now=lambda: 0,
        max_ticks=1,
        scheduler_progress=lambda: progress.append(True),
    )

    assert sum(slept) == 300
    assert max(slept) <= 30
    assert len(progress) == len(slept) + 3


