"""Daemon loop — tick sequencing, crash isolation, heartbeat."""

import asyncio
import json
import threading

import pytest

from tests.fakes.legacy_journal import Journal
from issue_agent.adapters.journal import read_events
from issue_agent.adapters.github import GhUnavailable
from issue_agent.adapters.store import Store
from tests.fakes.executor import ImmediateExecutor as Executor
from issue_agent.app.loop import Daemon
from issue_agent.domain.actions import NoOp, Relabel, Target
from issue_agent.domain.events import TelemetryEvent, TickGated
from issue_agent.domain.labels import Label
from issue_agent.domain.world import WorldState
from tests.fakes.in_memory_github import InMemoryGitHub as FakeGateway


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
    from issue_agent.domain.actions import AgentKind, EnableAutoMerge, RunAgent

    ran = []
    executed = []

    class OrderGateway:
        def relabel(self, kind, number, remove, add):
            executed.append(("relabel", number))

        def enable_auto_merge(self, pr, method):
            executed.append(("automerge", pr))
            return True

    ex = Executor(OrderGateway(), run_agent=lambda a: executed.append(("run", a.number)))
    d = Daemon(
        "fix", lambda: WorldState(),
        lambda w: [
            Relabel(Target.ISSUE, 500, Label.AI_READY, Label.AI_WORKING),
            RunAgent(AgentKind.RESOLVE, 500),
            EnableAutoMerge(537, "squash"),
            RunAgent(AgentKind.REBASE, 534),
        ],
        ex,
    )
    d.tick()
    assert executed == [
        ("relabel", 500),      # the dispatch's own label move still precedes it
        ("automerge", 537),    # finished work lands before any session starts
        ("run", 500),
        ("run", 534),          # dispatch order among dispatches preserved
    ]


def test_observation_and_lifecycle_continue_while_the_only_dispatch_slot_is_busy():
    """N=1 remains serial for paid work but no longer makes the daemon blind."""
    from issue_agent.domain.actions import AgentKind, EnableAutoMerge, RunAgent

    started = threading.Event()
    release = threading.Event()
    observations = []

    class LiveGateway(FakeGateway):
        def __init__(self):
            super().__init__()
            self.arm_attempts = 0

        def enable_auto_merge(self, pr, method):
            self.arm_attempts += 1
            return self.arm_attempts > 1

    gateway = LiveGateway()

    def run_agent(_action):
        started.set()
        assert release.wait(timeout=5)

    executor = Executor(gateway, run_agent=run_agent)
    daemon = Daemon(
        "fix",
        lambda: observations.append(len(observations) + 1) or WorldState(),
        lambda _world: [
            EnableAutoMerge(24, "squash"),
            RunAgent(AgentKind.RESOLVE, 7),
        ],
        executor,
        dispatch_capacity=1,
    )

    first = daemon.tick()
    assert started.wait(timeout=1)
    second = daemon.tick()

    assert observations == [1, 2]
    assert gateway.arm_attempts == 2, "a failed arm retries on the next poll"
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


def test_rejected_dispatch_releases_exclusive_lease_and_can_retry():
    from issue_agent.app.roles import DispatchPathReservations
    from issue_agent.domain.actions import AgentKind, RunAgent

    started = threading.Event()
    release = threading.Event()
    retried = threading.Event()
    reservations = DispatchPathReservations()
    occupying = RunAgent(AgentKind.RESOLVE, 1)
    rejected = RunAgent(AgentKind.RESOLVE, 50)
    ticks = 0

    def decide(_world):
        nonlocal ticks
        ticks += 1
        if ticks == 1:
            return [occupying]
        assert reservations.reserve(rejected, 50, frozenset())
        return [rejected]

    def run_agent(action):
        if action == occupying:
            started.set()
            assert release.wait(timeout=5)
        else:
            retried.set()
            reservations.finish(action)

    daemon = Daemon(
        "fix",
        lambda: WorldState(),
        decide,
        Executor(FakeGateway(), run_agent=run_agent),
        dispatch_capacity=1,
        dispatch_rejected=reservations.finish,
    )

    daemon.tick()
    assert started.wait(timeout=1)
    assert daemon.tick() == []
    assert reservations.snapshot() == ()

    unrelated = RunAgent(AgentKind.RESOLVE, 99)
    assert reservations.reserve(unrelated, 99, frozenset({"src/z.py"}))
    reservations.finish(unrelated)

    release.set()
    daemon.wait_for_dispatches()
    assert daemon.tick() == [rejected]
    assert retried.wait(timeout=1)
    daemon.wait_for_dispatches()


def test_submit_exception_releases_decision_lease():
    from issue_agent.app.roles import DispatchPathReservations
    from issue_agent.domain.actions import AgentKind, RunAgent

    reservations = DispatchPathReservations()
    action = RunAgent(AgentKind.RESOLVE, 50)
    assert reservations.reserve(action, 50, frozenset())
    daemon = Daemon(
        "fix",
        lambda: WorldState(),
        lambda _world: [action],
        Executor(FakeGateway()),
        dispatch_capacity=1,
        dispatch_rejected=reservations.finish,
    )
    assert daemon._dispatch_pool is not None
    daemon._dispatch_pool.shutdown()

    assert daemon.tick() == []
    assert reservations.snapshot() == ()
    assert reservations.reserve(
        RunAgent(AgentKind.RESOLVE, 99),
        99,
        frozenset({"src/z.py"}),
    )


def test_hot_reload_drains_paid_work_while_observation_continues():
    from issue_agent.adapters.reload import RELOAD_EXIT_CODE
    from issue_agent.domain.actions import AgentKind, RunAgent

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    observations = []
    sleeps = 0

    def run_agent(_action):
        started.set()
        assert release.wait(timeout=5)
        finished.set()

    def sleep(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            release.set()
            assert finished.wait(timeout=1)

    daemon = Daemon(
        "fix",
        lambda: observations.append(len(observations) + 1) or WorldState(),
        lambda _world: [RunAgent(AgentKind.RESOLVE, 7)],
        Executor(FakeGateway(), run_agent=run_agent),
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

    async def dispatch(_action):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    runtime = CancellableAsyncDispatch(dispatch)
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
        assert started.wait(timeout=1)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        daemon.run_forever(sleep=interrupt, now=lambda: 0)

    assert cancelled.wait(timeout=1)
    assert daemon.active_dispatches == ()
    assert daemon.available_dispatch_slots == 0


def test_shutdown_deadline_forces_process_teardown_and_exits_transient():
    from issue_agent.domain.actions import AgentKind, RunAgent
    from issue_agent.domain.supervision import ExitCode

    started = threading.Event()
    release = threading.Event()
    forced = []

    def run_agent(_action):
        started.set()
        release.wait(timeout=5)

    daemon = Daemon(
        "review",
        lambda: WorldState(),
        lambda _world: [RunAgent(AgentKind.REVIEW, 7)],
        Executor(FakeGateway(), run_agent=run_agent),
        poll_seconds=1,
        dispatch_capacity=1,
        shutdown_timeout_seconds=0.01,
        force_kill_dispatches=lambda: forced.append(True),
    )

    with pytest.raises(SystemExit) as stopped:
        daemon.run_forever(
            sleep=lambda _seconds: None,
            now=lambda: 0,
            max_ticks=1,
        )

    assert started.is_set()
    assert stopped.value.code is ExitCode.TRANSIENT
    assert forced == [True]
    release.set()
    daemon.wait_for_dispatches()


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
        return [NoOp()]
    d, _ = _daemon(decide)
    slept = []
    d.run_forever(sleep=slept.append, now=lambda: 0, max_ticks=3)
    assert calls["n"] == 3
    assert slept == [60, 60, 60]


def test_normal_shutdown_flushes_the_finalizer_once():
    finalized = []
    d, _ = _daemon(lambda _world: [])
    d.finalize = lambda: finalized.append("flushed")

    d.run_forever(
        sleep=lambda _seconds: None,
        now=lambda: 0,
        max_ticks=1,
    )

    assert finalized == ["flushed"]


def test_failed_shutdown_finalizer_forces_transient_exit():
    from issue_agent.domain.supervision import ExitCode

    d, _ = _daemon(lambda _world: [])
    d.finalize = lambda: (_ for _ in ()).throw(RuntimeError("disk full"))

    with pytest.raises(SystemExit) as stopped:
        d.run_forever(
            sleep=lambda _seconds: None,
            now=lambda: 0,
            max_ticks=1,
        )

    assert stopped.value.code is ExitCode.TRANSIENT


def test_scheduler_progress_covers_tick_and_quiet_wait():
    progress = []

    def observe():
        assert progress == []
        return WorldState()

    d, _ = _daemon(lambda _world: [], observe=observe)
    d.run_forever(
        sleep=lambda _seconds: None,
        now=lambda: 0,
        max_ticks=1,
        scheduler_progress=lambda: progress.append("completed"),
    )

    assert progress == ["completed", "completed", "completed"]


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
            pending_actions=Store(str(tmp_path / "state")),
            retry_delays=(),
        ),
        poll_seconds=60,
        journal=journal,
        recover=lambda: recoveries.append("recovered"),
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
    assert recoveries == ["recovered"]
    assert not any(event.get("replayed") is True for event in events)
    assert not any(event["event"] == "agent_run_stall" for event in events)


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


def test_a_wake_mid_sleep_ends_the_interval_early():
    """The whole point of push: a delivery must not wait out the reconcile
    interval. Asserted on the observable — total seconds slept — not on how the
    sleep is chopped up."""
    ticks = {"n": 0}
    # Wake on the 3rd probe of the first interval, never again.
    probes = {"n": 0}

    def wake():
        probes["n"] += 1
        return probes["n"] == 3

    d = _waking_daemon(wake, poll_seconds=300)
    slept = []
    d.run_forever(sleep=slept.append, now=lambda: ticks["n"], max_ticks=2)
    assert sum(slept) == 3 + 300  # cut short once, then a full backstop interval


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


def test_a_wake_check_that_raises_leaves_the_reconciler_running():
    """Losing push latency must never wedge the loop."""
    logs = []

    def boom():
        raise RuntimeError("stat failed")

    d = _waking_daemon(boom, poll_seconds=10, logs=logs)
    d.run_forever(sleep=lambda s: None, now=lambda: 0, max_ticks=2)
    assert any("wake check failed" in m for m in logs)


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
    assert len(progress) == len(slept) + 1


def _gated_daemon(gate, logs=None, journal=None):
    """A daemon whose observe/decide would definitely act, so 'nothing happened'
    can only mean the gate stopped the tick before either ran."""
    observed = []
    decided = []
    gw = FakeGateway()

    def observe():
        observed.append(1)
        return WorldState()

    def decide(world):
        decided.append(world)
        return [Relabel(Target.ISSUE, 140, Label.AI_READY, Label.AI_WORKING)]

    d = Daemon(
        name="fix", observe=observe, decide=decide, executor=Executor(gw),
        log=(logs.append if logs is not None else (lambda m: None)),
        journal=journal, tick_gate=gate,
    )
    return d, gw, observed, decided


def test_a_gated_tick_continues_observation_and_free_lifecycle_work():
    d, gw, observed, decided = _gated_daemon(lambda: "SPEND CAP: paused")
    assert d.tick() != []
    assert observed == [1]
    assert len(decided) == 1
    assert gw.calls == [
        ("relabel", "issue", 140, "ai-ready", "ai-working")
    ]


def test_the_gate_logs_once_on_the_trip_and_once_on_the_clear():
    """A level that stays tripped for hours must not print a line per poll, and
    the resume is the edge an operator is actually waiting for."""
    logs: list[str] = []
    tripped = {"yes": True}
    d, _, _, _ = _gated_daemon(
        lambda: "SPEND CAP: paused" if tripped["yes"] else None, logs=logs
    )
    for _ in range(5):
        d.tick()
    assert [m for m in logs if "SPEND CAP" in m] == ["fix: SPEND CAP: paused"]

    tripped["yes"] = False
    d.tick()
    assert any("resuming" in m for m in logs)
    d.tick()
    assert len([m for m in logs if "resuming" in m]) == 1


def test_a_gate_that_clears_lets_the_next_tick_do_real_work():
    """Anti-vacuity: the gate must be a pause, not an off switch."""
    tripped = {"yes": True}
    d, gw, observed, _ = _gated_daemon(
        lambda: "paused" if tripped["yes"] else None
    )
    d.tick()
    tripped["yes"] = False
    d.tick()
    assert observed == [1, 1]
    assert gw.calls == [
        ("relabel", "issue", 140, "ai-ready", "ai-working"),
        ("relabel", "issue", 140, "ai-ready", "ai-working"),
    ]


def test_a_changed_gate_reason_re_announces():
    """Repo cap → global cap is a different fact and sends an operator to a
    different config file; silently reusing the first edge would hide it."""
    logs: list[str] = []
    reasons = iter(["repo cap tripped", "global cap tripped", "global cap tripped"])
    d, _, _, _ = _gated_daemon(lambda: next(reasons), logs=logs)
    for _ in range(3):
        d.tick()
    assert [m for m in logs if "tripped" in m] == [
        "fix: repo cap tripped", "fix: global cap tripped"
    ]


def test_a_gate_that_raises_fails_closed_only_for_paid_admission():
    logs: list[str] = []

    def boom():
        raise RuntimeError("ledger unreadable")

    d, gw, observed, _ = _gated_daemon(boom, logs=logs)
    d.tick()
    assert observed == [1]
    assert gw.calls == [
        ("relabel", "issue", 140, "ai-ready", "ai-working")
    ]
    assert any("ACCOUNTING UNAVAILABLE" in m for m in logs)


def test_a_gated_tick_blocks_paid_dispatch_after_free_work() -> None:
    from issue_agent.domain.actions import AgentKind, RunAgent

    calls = []
    gateway = FakeGateway()
    daemon = Daemon(
        "fix",
        lambda: WorldState(),
        lambda _world: [
            Relabel(Target.ISSUE, 1, Label.AI_READY, Label.AI_WORKING),
            RunAgent(AgentKind.RESOLVE, 1),
        ],
        Executor(gateway, run_agent=calls.append),
        tick_gate=lambda: "accounting unavailable",
    )
    taken = daemon.tick()
    assert taken == []
    assert gateway.calls == []
    assert calls == []


def test_a_gated_tick_is_journalled_once_per_trip():
    """A daemon that did nothing all day and a daemon that was paused all day are
    indistinguishable from actions alone."""
    class FakeJournal:
        def __init__(self):
            self.events = []

        def emit(self, event, **fields):
            self.events.append((event, fields))

    j = FakeJournal()
    d, _, _, _ = _gated_daemon(lambda: "SPEND CAP: paused", journal=j)
    for _ in range(4):
        d.tick()
    assert len(j.events) == 1
    event, fields = j.events[0]
    assert fields == {}
    assert isinstance(event, TelemetryEvent)
    assert event.payload == TickGated("SPEND CAP: paused")


def test_no_gate_is_the_default_and_changes_nothing():
    d, gw, observed, _ = _gated_daemon(None)
    d.tick()
    assert observed == [1] and gw.calls != []


def test_world_unchanged_skips_silently_without_deciding():
    """The probe's skip is not an incident: no decide, no execute, no log line
    (a line every quiet poll is the noise the probe exists to remove)."""
    from issue_agent.app.world_probe import WorldUnchanged

    logs = []
    decided = []

    def unchanged():
        raise WorldUnchanged()

    d, gw = _daemon(lambda w: decided.append(w) or [], observe=unchanged, logs=logs)
    assert d.tick() == []
    assert decided == []
    assert gw.calls == []
    assert logs == []
