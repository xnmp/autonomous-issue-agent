"""Daemon loop — tick sequencing, crash isolation, heartbeat."""

from issue_agent.adapters.github import GhUnavailable
from issue_agent.app.execute import Executor
from issue_agent.app.loop import Daemon
from issue_agent.domain.actions import NoOp, Relabel, Target
from issue_agent.domain.labels import Label
from issue_agent.domain.world import WorldState


class FakeGateway:
    def __init__(self):
        self.calls = []

    def relabel(self, kind, number, remove, add):
        self.calls.append(("relabel", number, remove, add))


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
    assert gw.calls == [("relabel", 5, "ai-ready", "ai-working")]


def test_tick_isolated_from_decide_crash():
    logs = []
    def boom(world):
        raise RuntimeError("decide blew up")
    d, gw = _daemon(boom, logs=logs)
    assert d.tick() == []                # swallowed
    assert any("poll pass failed" in m for m in logs)
    assert gw.calls == []


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


def test_run_forever_warns_on_suspend_stall():
    logs = []
    d, _ = _daemon(lambda w: [], logs=logs)
    # now() jumps far beyond poll*4 between ticks → suspend-stall warning.
    times = iter([0, 100_000, 100_060])
    d.run_forever(sleep=lambda s: None, now=lambda: next(times), max_ticks=2)
    assert any("suspended/stalled" in m for m in logs)
