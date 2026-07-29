"""Daemon loop — tick sequencing, crash isolation, heartbeat."""

import json

from issue_agent.adapters.journal import Journal, read_events
from issue_agent.adapters.github import GhUnavailable
from issue_agent.adapters.store import Store
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


def test_late_heartbeat_journals_reconciles_and_annotates_overlapping_leg(tmp_path):
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
    assert gateway.calls == [("comment", 5, "blocked")]
    assert any(
        event["event"] == "action"
        and event["desc"] == "comment issue #5 (7 chars)"
        and event.get("replayed") is True
        for event in events
    )
    assert [
        {
            key: event[key]
            for key in (
                "event",
                "dispatch",
                "issue",
                "stall_started_at",
                "suspended_seconds",
            )
        }
        for event in events
        if event["event"] == "agent_run_stall"
    ] == [
        {
            "event": "agent_run_stall",
            "dispatch": "dispatch-5",
            "issue": 5,
            "stall_started_at": "1970-01-01T00:01:00+00:00",
            "suspended_seconds": 180,
        }
    ]


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

    assert read_events(journal.path) == [receipt]
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
    assert read_events(journal.path) == [receipt]


def test_stall_reconciliation_includes_poll_interval_before_estimated_gap(tmp_path):
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
    assert gateway.calls == [("relabel", 5, None, "ai-blocked")]
    assert any(
        event["event"] == "stall"
        and event["started_at"] == "1970-01-01T00:01:00+00:00"
        and event["stall_clock_offset_seconds"] == 290
        for event in events
    )


def test_stall_annotation_uses_each_legs_clock_gap_not_shifted_start_estimate(
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
    assert [
        (event["dispatch"], event["suspended_seconds"])
        for event in annotations
    ] == [("crossed-gap", 60)]


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


def test_a_gated_tick_never_observes_decides_or_writes_a_label():
    """#27's actual cost: the daily-cap check ran AFTER the world build and after
    the claim relabel, so every 20s tick of a capped daemon burned ~14 GraphQL
    points and two label writes to accomplish nothing — and the orphaned
    ai-working label then defeated the world probe. Asserted on the side effects
    (no observe, no decide, no gh call), not on the log line: the pre-fix code
    logged the block faithfully and was still wrong."""
    d, gw, observed, decided = _gated_daemon(lambda: "SPEND CAP: paused")
    assert d.tick() == []
    assert observed == []
    assert decided == []
    assert gw.calls == []


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
    assert observed == [1]
    assert gw.calls == [("relabel", 140, "ai-ready", "ai-working")]


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


def test_a_gate_that_raises_does_not_wedge_the_daemon():
    """Fail OPEN: the gate is an optimisation in front of a breaker that is still
    enforced at dispatch time, so a broken gate must cost work, not stop it."""
    logs: list[str] = []

    def boom():
        raise RuntimeError("ledger unreadable")

    d, gw, observed, _ = _gated_daemon(boom, logs=logs)
    d.tick()
    assert observed == [1]
    assert gw.calls == [("relabel", 140, "ai-ready", "ai-working")]
    assert any("tick gate failed" in m for m in logs)


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
    assert j.events == [("tick_gated", {"reason": "SPEND CAP: paused"})]


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
