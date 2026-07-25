"""Event journal, cost ledger, spend circuit breakers, alert hook.

Two properties dominate here and both are about NOT making things worse:
observability never raises (a read-only state dir must not kill a daemon), and
readers tolerate a truncated tail (a crash mid-write must not poison the file).
The rest verifies the caps actually gate dispatch — that the handler is never
called — rather than merely that a label moved.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.adapters.journal import (
    Journal,
    JournalingRuntime,
    alert,
    default_ledger_path,
    events_within,
    read_events,
    recent_runs,
    repeated_dispatch_warning,
    sum_issue,
    sum_today,
)
from issue_agent.app.dispatch import AgentDispatcher, DispatchConfig
from issue_agent.app.execute import Executor
from issue_agent.app.planners.tasks import FixTaskConfig
from issue_agent.domain.actions import AgentKind, Relabel, RunAgent, Target
from issue_agent.domain.agent_task import AgentRuntime, AgentTask
from issue_agent.domain.labels import Label
from issue_agent.domain.outcome import AgentResult, ExitReason
from issue_agent.domain.world import IssueView, PRView

NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)


def _at(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _write(path: str, *lines: str) -> str:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def _spend(dt: datetime, cost: float, repo="o/r", issue=5, kind="RESOLVE") -> str:
    return json.dumps({"ts": _at(dt), "event": "spend", "repo": repo,
                       "issue": issue, "kind": kind, "cost_usd": cost})


# ── Journal.emit ──────────────────────────────────────────────────────────────
def test_emit_appends_one_parseable_json_line_per_event(tmp_path):
    j = Journal(str(tmp_path / "sub" / "journal.jsonl"))
    j.emit("agent_run", kind="RESOLVE", number=5)
    j.emit("action", desc="relabel issue #5")

    events = read_events(j.path)
    assert [e["event"] for e in events] == ["agent_run", "action"]
    assert events[0]["kind"] == "RESOLVE" and events[0]["number"] == 5
    # A UTC stamp at seconds resolution, so the file stays greppable by date.
    assert events[0]["ts"].endswith("+00:00") and "." not in events[0]["ts"]


def test_emit_never_raises_on_an_unwritable_path(tmp_path, capsys):
    # A journal whose parent is a FILE can never be created. Observability must
    # degrade to a stderr line, never take the daemon down with it.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    Journal(str(blocker / "journal.jsonl")).emit("agent_run", number=1)
    assert "journal" in capsys.readouterr().err


def test_emit_serialises_unjsonable_values_instead_of_dropping_the_event(tmp_path):
    j = Journal(str(tmp_path / "j.jsonl"))
    j.emit("agent_run", exit_reason=ExitReason.WALL_TIMEOUT)
    assert read_events(j.path)[0]["exit_reason"].endswith("wall_timeout")


def test_an_event_with_a_multiline_field_is_still_exactly_one_line(tmp_path):
    # review T6: lock-free appending by three daemons rests entirely on POSIX
    # O_APPEND atomicity for ONE write() of ONE line. A field carrying embedded
    # newlines — an agent summary, a traceback, a diff — must be escaped into
    # that single line; the moment an event spans two writes, a concurrent
    # appender interleaves between them and BOTH records become unreadable.
    j = Journal(str(tmp_path / "j.jsonl"))
    body = "summary line one\nline two\r\n\nline four"
    j.emit("agent_run", summary=body)
    j.emit("action", desc="a second event lands after it")

    with open(j.path, encoding="utf-8") as fh:
        raw = fh.read()
    assert raw.count("\n") == 2, "one newline per event and not one more"
    assert len(raw.splitlines()) == 2
    # Anti-vacuity: one line because it was ESCAPED, not because it was mangled.
    assert json.loads(raw.splitlines()[0])["summary"] == body
    assert read_events(j.path)[1]["event"] == "action"


def test_an_event_is_appended_with_exactly_one_write_call(tmp_path, monkeypatch):
    # review T6, the other half: POSIX guarantees only that a SINGLE write() to
    # an O_APPEND fd is not interleaved with another appender's. `write(line)`
    # then `write("\n")` yields byte-identical output single-threaded and a
    # shredded ledger the moment the review daemon appends between the two — so
    # the contract here is the write COUNT, which no assertion on the resulting
    # file can see. (Rewriting `emit` on top of a different single-shot writer is
    # fine; splitting one event across two writes is not.)
    import builtins

    real_open = builtins.open
    writes: list[str] = []

    class _CountingHandle:
        def __init__(self, fh):
            self._fh = fh

        def write(self, text):
            writes.append(text)
            return self._fh.write(text)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._fh.__exit__(*exc)

    def spying_open(file, mode="r", *args, **kwargs):
        fh = real_open(file, mode, *args, **kwargs)
        return _CountingHandle(fh) if "a" in mode else fh

    j = Journal(str(tmp_path / "j.jsonl"))
    monkeypatch.setattr(builtins, "open", spying_open)
    try:
        j.emit("agent_run", summary="one\ntwo", number=5)
    finally:
        monkeypatch.undo()

    assert len(writes) == 1, "one event must reach the fd as one write()"
    assert writes[0].endswith("\n") and writes[0].count("\n") == 1
    assert read_events(j.path)[0]["number"] == 5  # and it really landed


# ── readers ───────────────────────────────────────────────────────────────────
def test_read_events_skips_malformed_and_truncated_lines(tmp_path):
    path = _write(
        str(tmp_path / "j.jsonl"),
        json.dumps({"event": "a"}),
        "{not json at all",
        "",
        '{"event": "b", "cost_usd": 1.0',  # truncated by a crash mid-write
        json.dumps({"event": "c"}),
        json.dumps(["not", "an", "object"]),
    )
    assert [e["event"] for e in read_events(path)] == ["a", "c"]


def test_read_events_of_a_missing_file_is_an_empty_history(tmp_path):
    assert read_events(str(tmp_path / "nope.jsonl")) == []


def test_sum_today_counts_only_todays_utc_spend(tmp_path):
    path = _write(
        str(tmp_path / "spend.jsonl"),
        _spend(NOW - timedelta(days=1), 99.0),          # yesterday
        _spend(NOW - timedelta(hours=11), 1.50),        # today, earlier UTC
        _spend(NOW, 2.25),
        json.dumps({"ts": _at(NOW), "event": "agent_run", "cost_usd": 100.0}),  # not spend
    )
    assert sum_today(path, lambda: NOW) == pytest.approx(3.75)


def test_sum_today_of_a_missing_ledger_is_zero(tmp_path):
    assert sum_today(str(tmp_path / "none.jsonl"), lambda: NOW) == 0.0


def test_sum_issue_is_lifetime_and_scoped_to_repo_and_issue(tmp_path):
    path = _write(
        str(tmp_path / "spend.jsonl"),
        _spend(NOW - timedelta(days=30), 4.0, issue=5),   # old, still counts
        _spend(NOW, 1.0, issue=5),
        _spend(NOW, 7.0, issue=6),                        # other issue
        _spend(NOW, 9.0, repo="other/repo", issue=5),     # other repo
    )
    assert sum_issue(path, "o/r", 5) == pytest.approx(5.0)


def test_recent_runs_returns_the_last_n_agent_runs_oldest_first(tmp_path):
    path = _write(
        str(tmp_path / "j.jsonl"),
        *[json.dumps({"ts": _at(NOW), "event": "agent_run", "number": n}) for n in range(6)],
        json.dumps({"ts": _at(NOW), "event": "action", "desc": "x"}),
    )
    assert [r["number"] for r in recent_runs(path, 3)] == [3, 4, 5]


def test_events_within_drops_old_and_unstamped_events():
    events = [
        {"ts": _at(NOW - timedelta(hours=2)), "event": "old"},
        {"ts": _at(NOW - timedelta(minutes=5)), "event": "fresh"},
        {"event": "unstamped"},
        {"ts": "not-a-timestamp", "event": "garbled"},
    ]
    assert [e["event"] for e in events_within(events, 3600, lambda: NOW)] == ["fresh"]


def test_repeated_dispatch_warning_fires_on_a_non_converging_loop():
    events = [
        {"ts": _at(NOW - timedelta(minutes=m)), "event": "agent_run",
         "kind": "RESOLVE", "number": 5}
        for m in (40, 30, 20, 10)
    ]
    warning = repeated_dispatch_warning(events, 3600, 4)
    assert "RESOLVE" in warning and "#5" in warning


def test_repeated_dispatch_warning_silent_when_spread_beyond_the_window():
    events = [
        {"ts": _at(NOW - timedelta(hours=h)), "event": "agent_run",
         "kind": "RESOLVE", "number": 5}
        for h in (4, 3, 2, 1)
    ]
    assert repeated_dispatch_warning(events, 3600, 4) == ""


def test_repeated_dispatch_warning_does_not_conflate_different_targets():
    # Four runs in the window, but each a different issue — that is a busy
    # harness, not a loop, and warning about it would train operators to ignore it.
    events = [
        {"ts": _at(NOW - timedelta(minutes=m)), "event": "agent_run",
         "kind": "RESOLVE", "number": n}
        for m, n in ((40, 1), (30, 2), (20, 3), (10, 4))
    ]
    assert repeated_dispatch_warning(events, 3600, 4) == ""


def test_repeated_dispatch_warning_sees_a_loop_made_only_of_starts():
    # review C3: a session that dies before any ResultMessage never produces a
    # settled `agent_run`, so a detector that reads only settled runs reported a
    # healthy harness while it bought a full session every other poll, forever.
    events = [
        {"ts": _at(NOW - timedelta(minutes=m)), "event": "agent_run_start",
         "kind": "RESOLVE", "number": 5}
        for m in (40, 30, 20, 10)
    ]
    warning = repeated_dispatch_warning(events, 3600, 4)
    assert "RESOLVE" in warning and "#5" in warning


def test_repeated_dispatch_warning_counts_a_start_and_its_settlement_once():
    # Anti-vacuity twin of the above: every HEALTHY run now emits both halves,
    # so counting events rather than dispatches would halve every operator's
    # threshold overnight and warn about four ordinary runs as if they were eight.
    events = [
        {"ts": _at(NOW - timedelta(minutes=m)), "event": ev,
         "kind": "RESOLVE", "number": 5}
        for m in (40, 30, 20, 10)
        for ev in ("agent_run_start", "agent_run")
    ]
    assert repeated_dispatch_warning(events, 3600, 5) == "", "8 events, 4 dispatches"
    assert "dispatched 4x" in repeated_dispatch_warning(events, 3600, 4)


def test_repeated_dispatch_warning_still_reads_a_journal_with_no_start_events():
    # Journals written before `agent_run_start` existed (and any run whose start
    # line was lost) must keep tripping the detector on settled runs alone.
    events = [
        {"ts": _at(NOW - timedelta(minutes=m)), "event": "agent_run",
         "kind": "REVIEW", "number": 7}
        for m in (40, 30, 20, 10)
    ]
    assert "#7" in repeated_dispatch_warning(events, 3600, 4)


def test_default_ledger_path_is_global_and_honours_xdg(monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", "/xdg")
    assert default_ledger_path() == "/xdg/issue-agent/spend.jsonl"


# ── JournalingRuntime ─────────────────────────────────────────────────────────
class FakeInner:
    def __init__(self, result):
        self.result = result
        self.tasks = []

    async def run(self, task):
        self.tasks.append(task)
        return self.result


def _runtime(tmp_path, result, repo="o/r"):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    ledger = str(tmp_path / "spend.jsonl")
    return JournalingRuntime(FakeInner(result), journal, ledger, repo, "fix"), journal, ledger


def _settled(journal_path) -> list[dict]:
    """The settled halves only — every dispatch also writes an `agent_run_start`
    before the await (review C3), so a positional index is meaningless."""
    return [e for e in read_events(journal_path) if e["event"] == "agent_run"]


def test_journaling_runtime_satisfies_the_agent_runtime_port(tmp_path):
    rt, _, _ = _runtime(tmp_path, AgentResult(is_error=False))
    assert isinstance(rt, AgentRuntime)


def test_agent_run_records_context_cost_and_usage(tmp_path):
    result = AgentResult(
        is_error=False, status="pr_opened", cost_usd=1.25, session_id="s5",
        exit_reason=ExitReason.OK,
        usage={"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 30,
               "cache_creation_input_tokens": 40, "ignored": "extra"},
    )
    rt, journal, ledger = _runtime(tmp_path, result)
    rt.set_context("fix", "RESOLVE", 5, 5)
    asyncio.run(rt.run(AgentTask(prompt="p", model="opus")))

    run = _settled(journal.path)[0]
    assert run["event"] == "agent_run"
    assert (run["role"], run["kind"], run["number"], run["issue"]) == ("fix", "RESOLVE", 5, 5)
    assert run["model"] == "opus" and run["cost_usd"] == 1.25
    assert run["status"] == "pr_opened" and run["is_error"] is False
    assert run["exit_reason"] == "ok" and run["session_id"] == "s5"
    assert run["usage"] == {"input_tokens": 10, "output_tokens": 20,
                            "cache_read_input_tokens": 30, "cache_creation_input_tokens": 40}
    assert isinstance(run["duration_s"], float)

    spend = read_events(ledger)[0]
    assert spend == {**spend, "event": "spend", "repo": "o/r", "issue": 5,
                     "kind": "RESOLVE", "cost_usd": 1.25}


def test_missing_usage_stays_distinct_from_zero_tokens(tmp_path):
    rt, journal, _ = _runtime(tmp_path, AgentResult(is_error=False, usage=None))
    asyncio.run(rt.run(AgentTask(prompt="p")))
    assert _settled(journal.path)[0]["usage"] is None


def test_a_free_run_is_journalled_but_never_hits_the_ledger(tmp_path):
    # $0.00 is what a subscription plan and Codex both report; a ledger full of
    # zero lines is noise in the file the caps scan on every dispatch.
    rt, journal, ledger = _runtime(tmp_path, AgentResult(is_error=False, cost_usd=0.0))
    asyncio.run(rt.run(AgentTask(prompt="p")))
    assert len(_settled(journal.path)) == 1
    assert read_events(ledger) == []


def test_each_leg_of_a_multi_leg_dispatch_gets_its_own_attributed_line(tmp_path):
    rt, journal, ledger = _runtime(tmp_path, AgentResult(is_error=False, cost_usd=0.5))
    rt.set_context("review", "REVIEW", 100, 5)
    for _ in range(3):
        asyncio.run(rt.run(AgentTask(prompt="leg")))
    assert len(_settled(journal.path)) == 3
    assert sum_issue(ledger, "o/r", 5) == pytest.approx(1.5)


def test_result_is_returned_unchanged_and_inner_still_sees_the_task(tmp_path):
    expected = AgentResult(is_error=False, status="done")
    rt, _, _ = _runtime(tmp_path, expected)
    task = AgentTask(prompt="p")
    assert asyncio.run(rt.run(task)) is expected
    assert rt.inner.tasks == [task]


# ── JournalingRuntime: the pre-result death (review C3) ───────────────────────
class SnoopingInner:
    """Reads the journal from INSIDE the run — i.e. at exactly the moment a
    transport death would strike."""

    def __init__(self, journal_path, result):
        self.journal_path = journal_path
        self.result = result
        self.seen: list[dict] = []

    async def run(self, task):
        self.seen = read_events(self.journal_path)
        return self.result


class RaisingInner:
    """A runtime that dies before delivering any result — `AgentRunner`'s
    deliberate pre-result behaviour (`agent_runner.py:522-531`)."""

    def __init__(self, exc):
        self.exc = exc
        self.tasks = []

    async def run(self, task):
        self.tasks.append(task)
        raise self.exc


class PoisonedJournal:
    """A journal whose sink is gone (read-only state dir, full disk). `Journal`
    swallows this itself; this fake proves the RUNTIME survives a journal that
    does not."""

    path = "/dev/null/nope"

    def emit(self, event, **fields):
        raise OSError("state dir went read-only")


def test_the_dispatch_is_journalled_before_the_await(tmp_path):
    # review C3: every attempt-recording write used to happen strictly AFTER the
    # await, so a session that died mid-run left no trace anywhere — no journal
    # line, no ledger line — while the reaper requeued the issue and the fixer
    # re-dispatched it next poll. The start line is the only durable evidence
    # that this dispatch ever happened.
    journal = Journal(str(tmp_path / "journal.jsonl"))
    inner = SnoopingInner(journal.path, AgentResult(is_error=False))
    rt = JournalingRuntime(inner, journal, "", "o/r", "fix")
    rt.set_context("fix", "RESOLVE", 5, 5)

    asyncio.run(rt.run(AgentTask(prompt="p", model="opus")))

    assert [e["event"] for e in inner.seen] == ["agent_run_start"]
    start = inner.seen[0]
    assert (start["role"], start["kind"], start["number"], start["issue"]) == (
        "fix", "RESOLVE", 5, 5)
    assert start["model"] == "opus"


def test_a_run_that_raises_is_settled_as_a_transport_death_and_re_raises(tmp_path):
    # review C3: the raise IS the crash-isolation contract (Daemon.tick catches,
    # the issue keeps ai-working, the reaper requeues). It must reach the caller
    # untouched — the journal only witnesses it.
    boom = RuntimeError("Stream closed unexpectedly")
    journal = Journal(str(tmp_path / "journal.jsonl"))
    ledger = str(tmp_path / "spend.jsonl")
    rt = JournalingRuntime(RaisingInner(boom), journal, ledger, "o/r", "fix")
    rt.set_context("fix", "RESOLVE", 5, 5)

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(rt.run(AgentTask(prompt="p", model="opus")))
    assert raised.value is boom, "the exception object must propagate unchanged"

    start, settled = read_events(journal.path)
    assert start["event"] == "agent_run_start" and settled["event"] == "agent_run"
    # The two halves correlate: same dispatch, same target.
    for key in ("role", "kind", "number", "issue", "model"):
        assert start[key] == settled[key]
    assert settled["exit_reason"] == "transport_death"
    assert settled["status"] == "raised" and settled["is_error"] is True
    assert settled["cost_usd"] == 0.0
    assert "RuntimeError" in settled["error"] and "Stream closed" in settled["error"]
    # A session that never delivered a result costs nothing to attribute.
    assert read_events(ledger) == []


def test_a_normal_run_is_never_settled_as_a_transport_death(tmp_path):
    # Anti-vacuity twin: the raise path must not be reachable from a run that
    # merely returned an error result — those still count as attempts.
    rt, journal, _ = _runtime(
        tmp_path, AgentResult(is_error=True, status="failed", exit_reason=ExitReason.AGENT_ERROR))
    rt.set_context("fix", "RESOLVE", 5, 5)
    asyncio.run(rt.run(AgentTask(prompt="p")))

    events = read_events(journal.path)
    assert [e["event"] for e in events] == ["agent_run_start", "agent_run"]
    assert events[1]["exit_reason"] == "agent_error" and events[1]["status"] == "failed"
    assert "error" not in events[1]


def test_a_journal_that_cannot_write_never_fails_a_run(tmp_path, capsys):
    # Observability is never a failure mode (module rule 1) — and that has to
    # hold for the two NEW writes as well, on both paths.
    expected = AgentResult(is_error=False, status="done")
    ok = JournalingRuntime(FakeInner(expected), PoisonedJournal())
    assert asyncio.run(ok.run(AgentTask(prompt="p"))) is expected

    boom = RuntimeError("Stream closed unexpectedly")
    dead = JournalingRuntime(RaisingInner(boom), PoisonedJournal())
    with pytest.raises(RuntimeError) as raised:
        asyncio.run(dead.run(AgentTask(prompt="p")))
    # The journal's own failure must not stand in for the run's failure.
    assert raised.value is boom
    assert "journal" in capsys.readouterr().err


# ── JournalingRuntime: the decorator keeps the inner surface (review S7) ──────
class InnerWithSurface:
    def __init__(self):
        self.bot = "fix-bot"

    async def run(self, task):
        return AgentResult(is_error=False)

    def preflight(self):
        return "codex binary not found"


def test_wrapping_a_runtime_does_not_hide_its_own_surface(tmp_path):
    # review S7: `roles.py` calls `fix_runner.preflight()` for the codex
    # readiness banner. A decorator without passthrough turns that into an
    # AttributeError — or, behind a `getattr(..., None)` probe, a silent no-op
    # that reports "ready" for a runtime with no binary.
    rt = JournalingRuntime(InnerWithSurface(), Journal(str(tmp_path / "j.jsonl")))
    assert rt.preflight() == "codex binary not found"
    assert rt.bot == "fix-bot"


def test_passthrough_does_not_invent_attributes_neither_object_has(tmp_path):
    # Anti-vacuity twin: passthrough must not turn every typo into a live object.
    rt = JournalingRuntime(InnerWithSurface(), Journal(str(tmp_path / "j.jsonl")))
    with pytest.raises(AttributeError):
        rt.no_such_attribute
    with pytest.raises(AttributeError):
        JournalingRuntime.__new__(JournalingRuntime).preflight  # half-built, no inner


def test_the_wrappers_own_attributes_win_over_the_inners(tmp_path):
    class Shadowing(InnerWithSurface):
        role = "steward"

    journal = Journal(str(tmp_path / "j.jsonl"))
    rt = JournalingRuntime(Shadowing(), journal, "", "o/r", "fix")
    assert rt.role == "fix" and rt.journal is journal


# ── alert hook ────────────────────────────────────────────────────────────────
def test_alert_pipes_the_event_json_to_the_shell_command(tmp_path):
    sink = tmp_path / "alerts.txt"
    alert(f"cat >> {sink}", '{"event": "escalation"}')
    assert json.loads(sink.read_text())["event"] == "escalation"


def test_alert_is_a_noop_when_unset(tmp_path):
    sink = tmp_path / "never.txt"
    alert("", f"echo written > {sink}")
    alert("   ", "{}")
    assert not sink.exists()


def test_alert_swallows_a_failing_command(tmp_path):
    alert("exit 7", "{}")  # must not raise — a dead pager can't wedge the loop


# ── Executor: escalation + action events ──────────────────────────────────────
class FakeGateway:
    def __init__(self):
        self.calls = []

    def relabel(self, kind, number, remove, add):
        self.calls.append(("relabel", kind, number, remove, add))

    def comment(self, kind, number, body):
        self.calls.append(("comment", kind, number, body))


def test_executor_journals_every_non_noop_action(tmp_path):
    journal = Journal(str(tmp_path / "j.jsonl"))
    ex = Executor(FakeGateway(), journal=journal)
    ex.execute(Relabel(Target.ISSUE, 5, Label.AI_READY, Label.AI_WORKING))
    actions = [e for e in read_events(journal.path) if e["event"] == "action"]
    assert len(actions) == 1 and "relabel issue #5" in actions[0]["desc"]


def test_relabel_to_needs_human_review_emits_an_escalation_and_alerts(tmp_path):
    journal = Journal(str(tmp_path / "j.jsonl"))
    sink = tmp_path / "alerts.jsonl"
    ex = Executor(FakeGateway(), journal=journal, alert_cmd=f"cat >> {sink}")
    ex.execute(Relabel(Target.PR, 100, Label.AI_PR_OPEN, Label.NEEDS_HUMAN_REVIEW))

    esc = [e for e in read_events(journal.path) if e["event"] == "escalation"]
    assert esc == [{**esc[0], "event": "escalation", "target": "pr", "number": 100}]
    assert json.loads(sink.read_text())["event"] == "escalation"


def test_an_ordinary_relabel_is_not_an_escalation(tmp_path):
    journal = Journal(str(tmp_path / "j.jsonl"))
    ex = Executor(FakeGateway(), journal=journal)
    ex.execute(Relabel(Target.ISSUE, 5, Label.AI_WORKING, Label.AI_PR_OPEN))
    assert [e for e in read_events(journal.path) if e["event"] == "escalation"] == []


# ── dispatcher spend caps ─────────────────────────────────────────────────────
class CapGateway(FakeGateway):
    """Minimal gateway: issue lookups for the PR-keyed kinds + write capture."""

    def __init__(self, issue_for_pr=None):
        super().__init__()
        self._issue_for_pr = issue_for_pr or {}

    def get_issue(self, n):
        return IssueView(n, title="t", body="b")

    def get_pr(self, n):
        return PRView(n)

    def pr_for_issue(self, issue):
        return None

    def issue_for_pr(self, pr):
        return self._issue_for_pr.get(pr)


class RecordingRunner:
    """Fails the test if a run is ever started — a blocked dispatch must never
    reach the model (the whole point of a cap is spending nothing)."""

    def __init__(self):
        self.runs = 0

    async def run(self, task):
        self.runs += 1
        return AgentResult(is_error=False, status="pr_opened")


def _cap_dispatcher(tmp_path, ledger, *, issue_cap=0.0, daily_cap=0.0,
                    alert_cmd="", gw=None, logs=None):
    gw = gw or CapGateway()
    runner = RecordingRunner()
    journal = Journal(str(tmp_path / "j.jsonl"))
    cfg = DispatchConfig(
        fix=FixTaskConfig("o/r", agent_md="AM"),
        role="fix", repo="o/r", spend_ledger_path=ledger,
        issue_spend_cap_usd=issue_cap, daily_spend_cap_usd=daily_cap,
        alert_cmd=alert_cmd,
        log=(logs.append if logs is not None else (lambda _m: None)),
    )
    ex = Executor(gw, journal=journal, alert_cmd=alert_cmd)  # as roles.py wires it

    class _WT:
        def ensure(self, issue, branch):
            return f"/wt/{issue}"

        def remove(self, issue):
            pass

        def reset(self, issue):
            pass

    d = AgentDispatcher(cfg, gw, _FakeStore(), _WT(), runner, ex, journal=journal)
    return d, gw, runner, journal


class _FakeStore:
    def __init__(self):
        self.data = {}

    def has_session(self, i):
        return False

    def get_session(self, i):
        return ""

    def save_session(self, i, s):
        self.data[i] = s

    def get_phase(self, i):
        return ""

    def set_phase(self, i, p):
        pass

    def get_resolve_retries(self, i):
        return 0

    def set_resolve_retries(self, i, n):
        pass

    def get_ci_retries(self, i):
        return 0

    def set_ci_retries(self, i, n):
        pass


def test_issue_cap_escalates_to_needs_human_review_and_never_dispatches(tmp_path):
    ledger = _write(str(tmp_path / "spend.jsonl"), _spend(NOW, 30.0, issue=5))
    d, gw, runner, journal = _cap_dispatcher(tmp_path, ledger, issue_cap=25.0)

    asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, 5)))

    assert runner.runs == 0, "a capped issue must not buy another session"
    assert ("relabel", "issue", 5, "ai-working", "needs-human-review") in gw.calls
    assert any(c[0] == "comment" and "cap" in c[3].lower() for c in gw.calls)
    block = [e for e in read_events(journal.path) if e["event"] == "spend_block"]
    assert block[0]["scope"] == "issue" and block[0]["issue"] == 5
    assert block[0]["cap"] == 25.0 and block[0]["spent"] == 30.0


def test_daily_cap_skips_dispatch_without_touching_any_label(tmp_path):
    # Level-triggered: the money returns at midnight UTC, so relabelling would
    # leave a human to unwind a dozen labels for a condition that self-heals.
    ledger = _write(str(tmp_path / "spend.jsonl"), _spend(datetime.now(timezone.utc), 60.0))
    logs: list[str] = []
    d, gw, runner, journal = _cap_dispatcher(tmp_path, ledger, daily_cap=50.0, logs=logs)

    asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, 5)))

    assert runner.runs == 0
    assert gw.calls == [], "the daily cap must change no GitHub state at all"
    block = [e for e in read_events(journal.path) if e["event"] == "spend_block"]
    assert block[0]["scope"] == "daily" and block[0]["cap"] == 50.0
    assert any("SPEND CAP" in line for line in logs), "the pause must be loud"


def test_dispatch_proceeds_when_spend_is_under_both_caps(tmp_path):
    ledger = _write(str(tmp_path / "spend.jsonl"),
                    _spend(datetime.now(timezone.utc), 3.0, issue=5))
    d, gw, runner, _ = _cap_dispatcher(tmp_path, ledger, issue_cap=25.0, daily_cap=50.0)
    asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, 5)))
    assert runner.runs == 1


def test_zero_disables_a_cap(tmp_path):
    ledger = _write(str(tmp_path / "spend.jsonl"),
                    _spend(datetime.now(timezone.utc), 999.0, issue=5))
    d, gw, runner, _ = _cap_dispatcher(tmp_path, ledger, issue_cap=0.0, daily_cap=0.0)
    asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, 5)))
    assert runner.runs == 1


def test_pr_keyed_kinds_resolve_their_issue_for_the_per_issue_cap(tmp_path):
    ledger = _write(str(tmp_path / "spend.jsonl"), _spend(NOW, 30.0, issue=5))
    gw = CapGateway(issue_for_pr={100: 5})
    d, gw, runner, _ = _cap_dispatcher(tmp_path, ledger, issue_cap=25.0, gw=gw)
    asyncio.run(d.dispatch(RunAgent(AgentKind.FIX_CI, 100)))
    assert runner.runs == 0
    assert ("relabel", "issue", 5, "ai-working", "needs-human-review") in gw.calls


def test_an_unresolvable_issue_costs_attribution_not_a_block(tmp_path):
    # A Jira-keyed branch with no closing ref: issue_for_pr → None. An unknown
    # issue must not be treated as a capped one — unknown spend is not over-spend,
    # and blocking here would strand every PR the harness cannot back-link.
    ledger = _write(str(tmp_path / "spend.jsonl"), _spend(NOW, 30.0, issue=5))
    d, gw, _, journal = _cap_dispatcher(
        tmp_path, ledger, issue_cap=25.0, gw=CapGateway(issue_for_pr={}))
    asyncio.run(d.dispatch(RunAgent(AgentKind.REBASE, 100)))
    assert [e for e in read_events(journal.path) if e["event"] == "spend_block"] == []
    assert gw.calls == []


def test_spend_block_fires_the_alert_hook(tmp_path):
    ledger = _write(str(tmp_path / "spend.jsonl"), _spend(NOW, 30.0, issue=5))
    sink = tmp_path / "alerts.jsonl"
    d, _, _, _ = _cap_dispatcher(tmp_path, ledger, issue_cap=25.0,
                                 alert_cmd=f"cat >> {sink}")
    asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, 5)))
    payloads = [json.loads(line) for line in sink.read_text().splitlines() if line.strip()]
    # Both the block itself and the escalation it triggers are worth waking for.
    assert {p["event"] for p in payloads} == {"spend_block", "escalation"}


def test_caps_are_inert_without_a_ledger_path(tmp_path):
    d, _, runner, _ = _cap_dispatcher(tmp_path, "", issue_cap=25.0, daily_cap=50.0)
    asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, 5)))
    assert runner.runs == 1


def test_dispatcher_tags_the_runtime_with_the_dispatch_context(tmp_path):
    ledger = str(tmp_path / "spend.jsonl")
    journal = Journal(str(tmp_path / "j.jsonl"))
    inner = FakeInner(AgentResult(is_error=False, status="pushed", cost_usd=0.5))
    runtime = JournalingRuntime(inner, journal, ledger, "o/r", "fix")
    gw = CapGateway(issue_for_pr={100: 5})
    cfg = DispatchConfig(fix=FixTaskConfig("o/r", agent_md="AM"), role="fix", repo="o/r")

    class _WT:
        def ensure(self, issue, branch):
            return f"/wt/{issue}"

        def remove(self, issue):
            pass

        def reset(self, issue):
            pass

    d = AgentDispatcher(cfg, gw, _FakeStore(), _WT(), runtime, Executor(gw), journal=journal)
    asyncio.run(d.dispatch(RunAgent(AgentKind.FIX_CI, 100)))

    run = [e for e in read_events(journal.path) if e["event"] == "agent_run"][0]
    assert (run["role"], run["kind"], run["number"], run["issue"]) == ("fix", "FIX_CI", 100, 5)
    assert read_events(ledger)[0]["issue"] == 5


# ── Daemon: GhUnavailable is a countable event ────────────────────────────────
def test_daemon_journals_a_skipped_tick_when_github_is_unavailable(tmp_path):
    from issue_agent.adapters.github import GhUnavailable
    from issue_agent.app.loop import Daemon

    journal = Journal(str(tmp_path / "j.jsonl"))

    def observe():
        raise GhUnavailable("rate-limit", "API rate limit exceeded")

    d = Daemon(name="fix", observe=observe, decide=lambda w: [],
               executor=Executor(FakeGateway()), journal=journal)
    assert d.tick() == []
    ev = read_events(journal.path)[0]
    assert ev["event"] == "gh_unavailable" and ev["kind"] == "rate-limit"
    assert "rate limit" in ev["detail"]


# ── CLI status summary ────────────────────────────────────────────────────────
def test_status_summary_is_silent_with_no_history(tmp_path):
    from issue_agent.app.cli import spend_status_lines

    assert spend_status_lines(str(tmp_path / "j.jsonl"), str(tmp_path / "s.jsonl")) == []


def test_status_summary_reports_spend_runs_and_warnings(tmp_path):
    from issue_agent.app.cli import spend_status_lines

    ledger = _write(str(tmp_path / "spend.jsonl"), _spend(NOW, 12.5))
    journal = str(tmp_path / "j.jsonl")
    _write(
        journal,
        *[json.dumps({"ts": _at(NOW - timedelta(minutes=m)), "event": "agent_run",
                      "kind": "RESOLVE", "number": 5, "cost_usd": 1.0,
                      "exit_reason": "agent_error", "is_error": True})
          for m in (40, 30, 20, 10)],
        json.dumps({"ts": _at(NOW), "event": "spend_block", "scope": "issue", "issue": 5}),
        json.dumps({"ts": _at(NOW - timedelta(hours=3)), "event": "escalation",
                    "target": "issue", "number": 5}),
    )
    out = "\n".join(spend_status_lines(journal, ledger, 50.0, lambda: NOW))

    assert "$12.50" in out and "$50.00 cap" in out
    assert "RESOLVE" in out and "#5" in out and "agent_error" in out
    assert "non-converging retry loop" in out
    assert "1 spend block(s) today" in out
    assert "1 escalation(s) to needs-human-review in 24h" in out


def test_status_journal_path_prefers_the_overlay_state_dir(tmp_path, monkeypatch):
    from issue_agent.app import cli

    monkeypatch.setattr(cli, "REPOS_DIR", str(tmp_path))
    (tmp_path / "myapp.env").write_text('STATE_DIR="/custom/state"\n')
    assert cli.journal_path("myapp") == "/custom/state/journal.jsonl"
    # No overlay → the layout `issue-agent start` writes.
    assert cli.journal_path("other").endswith(
        os.path.join(".local", "state", "issue-agent-other", "journal.jsonl")
    )
