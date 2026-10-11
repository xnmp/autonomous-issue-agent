"""Shutdown deadlines are measured on the daemon's injected clock."""

import threading

import pytest

from issue_agent.app.loop import Daemon
from issue_agent.domain.actions import AgentKind, RunAgent
from issue_agent.domain.supervision import ExitCode
from issue_agent.domain.world import WorldState
from tests.fakes.executor import ImmediateExecutor as Executor
from tests.fakes.in_memory_github import InMemoryGitHub as FakeGateway
from tests.fakes.manual_clock import ManualClock


def test_deadline_expiry_is_decided_by_the_injected_clock():
    clock = ManualClock()
    finalized = []
    daemon = Daemon(
        "fix",
        lambda: WorldState(),
        lambda _world: [],
        Executor(FakeGateway()),
        dispatch_capacity=1,
        shutdown_timeout_seconds=1,
        monotonic=clock,
        cancel_active_dispatches=lambda: clock.advance(10),
        finalize=lambda: finalized.append(True),
    )

    assert daemon.shutdown_dispatches(cancel=True) is False

    assert finalized == []


def test_clock_expiry_while_killed_worker_unwinds_hard_exits_transient():
    clock = ManualClock()
    release = threading.Event()
    started = threading.Event()
    calls = []

    def run_agent(_action):
        started.set()
        release.wait(timeout=5)

    def interrupt(_seconds):
        assert started.wait(timeout=1)
        raise KeyboardInterrupt

    daemon = Daemon(
        "fix",
        lambda: WorldState(),
        lambda _world: [RunAgent(AgentKind.RESOLVE, 7)],
        Executor(FakeGateway(), run_agent=run_agent),
        poll_seconds=1,
        dispatch_capacity=1,
        shutdown_timeout_seconds=30,
        monotonic=clock,
        cancel_active_dispatches=lambda: clock.advance(20),
        force_kill_dispatches=lambda: clock.advance(2),
        finalize=lambda: calls.append("finalize"),
        terminalize=lambda: calls.append("terminalize"),
    )

    try:
        with pytest.raises((KeyboardInterrupt, SystemExit)) as stopped:
            daemon.run_forever(sleep=interrupt, now=lambda: 0, max_ticks=1)
        assert stopped.type is SystemExit
        assert stopped.value.code is ExitCode.TRANSIENT
        assert daemon.requires_hard_exit is True
        assert calls == []
    finally:
        release.set()
        daemon.wait_for_dispatches()
