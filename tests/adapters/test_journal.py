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
    COST_BLINDSPOT_MIN_RUNS,
    Journal,
    JournalingRuntime,
    alert,
    cost_blindspot_warning,
    death_key,
    default_ledger_path,
    events_within,
    hold_keys,
    read_events,
    read_events_tail,
    recent_runs,
    recurrence_keys,
    repeated_dispatch_warning,
    spend_blocked_keys,
    sum_issue,
    sum_today,
    unsettled_starts,
)
from issue_agent.app.dispatch import AgentDispatcher, DispatchConfig
from issue_agent.app.execute import Executor
from issue_agent.app.planners.tasks import FixTaskConfig, ReviewTaskConfig
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


def test_one_healthy_multi_agent_review_is_not_a_retry_loop():
    """review P1R-4. A `REVIEW_MODE=multi` dispatch fans a gate + five dimension
    specialists + validators out under ONE (kind, number), so counting LEGS
    reported "dispatched 16x … likely a non-converging retry loop" for every
    healthy review. A warning that is always on is a warning that is off — and
    this is the only operator-visible signal for the process deaths of P0R-4."""
    legs = [
        {"ts": _at(NOW - timedelta(seconds=60 - i)), "event": ev,
         "kind": "REVIEW", "number": 100, "dispatch": "d1"}
        for i in range(8)
        for ev in ("agent_run_start", "agent_run")
    ]
    assert repeated_dispatch_warning(legs, 3600, 4) == "", "16 legs, ONE dispatch"


def test_five_separate_dispatches_of_one_target_still_warn():
    """Anti-vacuity twin: collapsing legs must not collapse POLLS. Five
    consecutive re-dispatches are five paid rounds and the exact shape the
    detector exists for."""
    events = [
        {"ts": _at(NOW - timedelta(minutes=m)), "event": ev,
         "kind": "REVIEW", "number": 100, "dispatch": f"d{m}"}
        for m in (50, 40, 30, 20, 10)
        for ev in ("agent_run_start", "agent_run")
    ]
    warning = repeated_dispatch_warning(events, 3600, 5)
    assert "REVIEW" in warning and "#100" in warning and "dispatched 5x" in warning


def test_a_dispatch_is_stamped_when_it_started_not_when_its_last_leg_settled():
    """The window is measured between dispatches, so a long fan-out must not
    push its own dispatch outside the span and hide a loop."""
    events = []
    for i, minutes in enumerate((59, 40, 20, 1)):
        events.append({"ts": _at(NOW - timedelta(minutes=minutes)),
                       "event": "agent_run_start", "kind": "RESOLVE",
                       "number": 5, "dispatch": f"d{i}"})
        events.append({"ts": _at(NOW - timedelta(minutes=minutes) + timedelta(minutes=5)),
                       "event": "agent_run", "kind": "RESOLVE",
                       "number": 5, "dispatch": f"d{i}"})
    assert "dispatched 4x" in repeated_dispatch_warning(events, 3600, 4)


def test_ided_and_legacy_events_for_one_target_are_each_counted_once():
    """A journal that spans the upgrade holds both shapes. Neither may be
    dropped (a missed loop) nor double counted (a phantom one)."""
    ided = [
        {"ts": _at(NOW - timedelta(minutes=m)), "event": ev,
         "kind": "RESOLVE", "number": 5, "dispatch": f"d{m}"}
        for m in (40, 30)
        for ev in ("agent_run_start", "agent_run")
    ]
    legacy = [
        {"ts": _at(NOW - timedelta(minutes=m)), "event": "agent_run",
         "kind": "RESOLVE", "number": 5}
        for m in (20, 10)
    ]
    assert repeated_dispatch_warning(ided + legacy, 3600, 5) == "", "4 dispatches"
    assert "dispatched 4x" in repeated_dispatch_warning(ided + legacy, 3600, 4)


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


# ── unsettled starts / bounded tail read (review P0R-4) ───────────────────────
# The counter for the death nobody could record: SIGKILL / OOM / systemctl
# restart mid-`await`. Every existing bump site is code that runs AFTER the
# session ends, so a death that takes the daemon with it leaves an
# `agent_run_start` and nothing else — and systemd brings the process back to
# buy the same session again.
def _start(dt, kind="RESOLVE", number=5, issue=5, dispatch=""):
    return {"ts": _at(dt), "event": "agent_run_start", "role": "fix",
            "kind": kind, "number": number, "issue": issue, "model": "opus",
            "dispatch": dispatch}


def _run(dt, kind="RESOLVE", number=5, issue=5, dispatch="", **extra):
    return {"ts": _at(dt), "event": "agent_run", "role": "fix", "kind": kind,
            "number": number, "issue": issue, "cost_usd": 0.0,
            "dispatch": dispatch, **extra}


def test_a_start_with_no_settlement_is_one_unsettled_death():
    assert unsettled_starts([_start(NOW)], now_fn=lambda: NOW) == {5: 1}


def test_a_start_and_its_settlement_are_not_a_death():
    """Anti-vacuity: the shape of EVERY healthy run. A counter that read starts
    alone would escalate every issue the harness ever worked on."""
    events = [_start(NOW - timedelta(minutes=m + 1)) for m in range(4)]
    events += [_run(NOW - timedelta(minutes=m)) for m in range(4)]
    assert unsettled_starts(events, now_fn=lambda: NOW) == {}


def test_the_raise_path_death_settles_and_so_is_not_counted_here():
    """Anti-double-count. A pre-result transport death the PROCESS SURVIVED is
    already counted once, by the dispatcher's store bump; `JournalingRuntime`
    also writes it a settled `transport_death` line. Counting it here too would
    halve `MAX_INFRA_DEATHS` for the failure mode it was tuned on."""
    events = [
        _start(NOW - timedelta(minutes=1)),
        _run(NOW, status="raised", is_error=True, exit_reason="transport_death",
             error="RuntimeError: stream closed before any result"),
    ]
    assert unsettled_starts(events, now_fn=lambda: NOW) == {}


def test_a_concurrent_fanout_of_legs_reports_no_deaths():
    """The rule that forces COUNTING over positional pairing. A
    REVIEW_MODE=multi dispatch runs its gate, 5 dimensions and N validators
    concurrently under ONE context, so its journal is a run of interleaved
    starts and settlements in arbitrary order. Pairing "a start is unsettled if
    no settlement precedes the next start" would report 7 phantom deaths for
    this — one healthy review would trip a ceiling of 5 on its own."""
    starts = [_start(NOW - timedelta(seconds=60 - i), kind="REVIEW", number=100, issue=7)
              for i in range(8)]
    settles = [_run(NOW - timedelta(seconds=30 - i), kind="REVIEW", number=100, issue=7)
               for i in range(8)]
    assert unsettled_starts(starts + settles, now_fn=lambda: NOW) == {}
    # …and the same journal WITH one leg killed reports exactly one death.
    assert unsettled_starts(starts + settles[:-1], now_fn=lambda: NOW) == {7: 1}


def test_a_dispatch_killed_mid_fanout_is_ONE_death_not_one_per_leg():
    """The money question a per-leg count gets wrong. A `systemctl restart`
    during a REVIEW_MODE=multi review kills a gate + 5 dimension legs at once;
    charging six deaths trips a ceiling of five on a single routine restart,
    which is exactly the "memory pressure must not walk healthy work to a human"
    rule (FAILURE-MODES #58) inverted."""
    legs = [_start(NOW - timedelta(seconds=60 - i), kind="REVIEW", number=100,
                   issue=7, dispatch="d1") for i in range(6)]
    settled_two = [_run(NOW - timedelta(seconds=30 - i), kind="REVIEW", number=100,
                        issue=7, dispatch="d1") for i in range(2)]
    assert unsettled_starts(legs + settled_two, now_fn=lambda: NOW) == {7: 1}


def test_separate_dispatches_are_separate_deaths():
    """Anti-vacuity twin: collapsing legs must not collapse POLLS. Three
    consecutive killed dispatches are three paid rounds, and a ceiling that saw
    one would never trip on the loop it exists for."""
    events = [
        _start(NOW - timedelta(minutes=m), kind="REVIEW", number=100, issue=7,
               dispatch=f"d{m}")
        for m in (30, 20, 10)
    ]
    assert unsettled_starts(events, now_fn=lambda: NOW) == {7: 3}


def test_a_fully_settled_dispatch_is_no_death():
    legs = [_start(NOW - timedelta(seconds=60 - i), kind="REVIEW", number=100,
                   issue=7, dispatch="d1") for i in range(6)]
    settles = [_run(NOW - timedelta(seconds=30 - i), kind="REVIEW", number=100,
                    issue=7, dispatch="d1") for i in range(6)]
    assert unsettled_starts(legs + settles, now_fn=lambda: NOW) == {}


def test_the_real_runtime_stamps_one_dispatch_id_across_every_leg(tmp_path):
    """Pins the producer half of the rule: `set_context` is called once per
    dispatch, so every leg it runs must carry the SAME id — and the next
    dispatch a DIFFERENT one, or two polls' deaths collapse into one."""
    rt, journal, _ = _runtime(tmp_path, AgentResult(is_error=False))
    rt.set_context("review", "REVIEW", 100, 7)
    asyncio.run(rt.run(AgentTask(prompt="gate")))
    asyncio.run(rt.run(AgentTask(prompt="dimension")))
    first = {e["dispatch"] for e in read_events(journal.path)}
    assert len(first) == 1 and "" not in first

    rt.set_context("review", "REVIEW", 100, 7)
    asyncio.run(rt.run(AgentTask(prompt="gate")))
    assert len({e["dispatch"] for e in read_events(journal.path)}) == 2


def test_a_journal_written_before_dispatch_ids_still_counts_deaths():
    """Backward compatibility: an upgrade must not silently zero the counter for
    every run already on disk. With no `dispatch` field the rule falls back to
    (kind, number) counting."""
    legacy = {"ts": _at(NOW), "event": "agent_run_start", "kind": "RESOLVE",
              "number": 5, "issue": 5}
    assert unsettled_starts([legacy], now_fn=lambda: NOW) == {5: 1}


def test_deaths_key_to_the_issue_so_they_meet_the_dispatchers_counter():
    """The key must be the one `AgentDispatcher.dispatch` bumps `infra-deaths`
    under, or the ceiling reads a counter nobody writes (the PR #112 class)."""
    assert unsettled_starts(
        [_start(NOW, kind="REVIEW", number=100, issue=7)], now_fn=lambda: NOW
    ) == {7: 1}


def test_an_issueless_prs_death_keys_to_the_negated_pr_number():
    """`breaker_key`'s convention for a PR with no closing ref and a Jira-keyed
    branch: the dispatcher falls back to `-action.number`, so this must too."""
    assert unsettled_starts(
        [_start(NOW, kind="RE_REVIEW", number=112, issue=None)], now_fn=lambda: NOW
    ) == {-112: 1}
    assert death_key({"number": 112, "issue": None}) == -112
    assert death_key({"number": 112, "issue": 9}) == 9
    assert death_key({}) is None


def test_different_kinds_on_one_issue_sum_into_one_ceiling_key():
    events = [_start(NOW, kind="RESOLVE"), _start(NOW, kind="RESUME")]
    assert unsettled_starts(events, now_fn=lambda: NOW) == {5: 2}


def test_deaths_older_than_the_window_are_not_counted():
    assert unsettled_starts([_start(NOW - timedelta(hours=25))], now_fn=lambda: NOW) == {}
    assert unsettled_starts(
        [_start(NOW - timedelta(hours=25))], window_s=0, now_fn=lambda: NOW
    ) == {5: 1}


def test_a_settlement_whose_start_fell_outside_the_window_invents_no_death():
    """The window can only cut a pair the wrong way round; a bound that
    over-reports would walk a healthy issue to a human on a long session."""
    assert unsettled_starts([_run(NOW)], now_fn=lambda: NOW) == {}


def test_unstamped_and_unrelated_events_are_ignored():
    events = [
        {"event": "agent_run_start", "kind": "RESOLVE", "number": 5, "issue": 5},  # no ts
        {"ts": _at(NOW), "event": "escalation", "number": 5},
        {"ts": _at(NOW), "event": "agent_run_start", "kind": "RESOLVE",
         "number": "not-a-number", "issue": None},
    ]
    assert unsettled_starts(events, now_fn=lambda: NOW) == {}


# ── read_events_tail ──────────────────────────────────────────────────────────
# The poll-loop reader must not grow with the age of the install; a byte window
# means its first line is usually torn, and a parser handed half an object must
# skip it exactly like a killed writer's tail.
def test_read_events_tail_reads_only_the_last_bytes(tmp_path):
    path = str(tmp_path / "journal.jsonl")
    lines = [json.dumps({"event": "agent_run", "number": n, "pad": "x" * 100})
             for n in range(50)]
    _write(path, *lines)
    tail = read_events_tail(path, max_bytes=500)
    assert tail, "a bounded read must still return events"
    assert [e["number"] for e in tail] == list(range(50))[-len(tail):]
    assert len(tail) < 50, "the whole file was read — the bound did nothing"
    # Every returned object parsed cleanly: the torn leading line was dropped,
    # not handed to json.loads and silently counted as garbage.
    assert all(e.get("pad") == "x" * 100 for e in tail)


def test_read_events_tail_reads_a_whole_small_file_and_tolerates_a_missing_one(tmp_path):
    path = str(tmp_path / "journal.jsonl")
    _write(path, json.dumps({"event": "agent_run", "number": 1}))
    assert read_events_tail(path) == [{"event": "agent_run", "number": 1}]
    assert read_events_tail(str(tmp_path / "nope.jsonl")) == []


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


# ── #40: one leg of a dispatch under its own kind ────────────────────────────
def test_a_leg_kind_renames_only_the_legs_inside_the_block(tmp_path):
    rt, journal, _ = _runtime(tmp_path, AgentResult(is_error=False, cost_usd=0.5))
    rt.set_context("fix", "RESOLVE", 5, 5)
    with rt.leg_kind("ROUTE"):
        asyncio.run(rt.run(AgentTask(prompt="classify")))
    asyncio.run(rt.run(AgentTask(prompt="resolve")))

    assert [r["kind"] for r in _settled(journal.path)] == ["ROUTE", "RESOLVE"]


def test_a_leg_kind_keeps_the_dispatch_id_and_the_rest_of_the_context(tmp_path):
    rt, journal, _ = _runtime(tmp_path, AgentResult(is_error=False, cost_usd=0.5))
    rt.set_context("fix", "RESOLVE", 5, 5)
    with rt.leg_kind("ROUTE"):
        asyncio.run(rt.run(AgentTask(prompt="classify")))
    asyncio.run(rt.run(AgentTask(prompt="resolve")))

    runs = _settled(journal.path)
    assert len({r["dispatch"] for r in runs}) == 1, "one dispatch, one id"
    assert all((r["role"], r["number"], r["issue"]) == ("fix", 5, 5) for r in runs)


def test_a_renamed_leg_is_still_ledgered(tmp_path):
    """A router run is real spend — the caps must still see it, under its own
    bucket."""
    rt, _, ledger = _runtime(tmp_path, AgentResult(is_error=False, cost_usd=0.07))
    rt.set_context("fix", "RESOLVE", 5, 5)
    with rt.leg_kind("ROUTE"):
        asyncio.run(rt.run(AgentTask(prompt="classify")))

    spend = read_events(ledger)
    assert [(s["kind"], s["cost_usd"]) for s in spend] == [("ROUTE", 0.07)]
    assert sum_issue(ledger, "o/r", 5) == pytest.approx(0.07)


def test_a_leg_kind_unwinds_when_the_leg_raises(tmp_path):
    class Boom:
        async def run(self, task):
            raise RuntimeError("router transport died")

    journal = Journal(str(tmp_path / "journal.jsonl"))
    rt = JournalingRuntime(Boom(), journal, "", "o/r", "fix")
    rt.set_context("fix", "RESOLVE", 5, 5)
    with pytest.raises(RuntimeError):
        with rt.leg_kind("ROUTE"):
            asyncio.run(rt.run(AgentTask(prompt="classify")))
    assert rt.context.kind == "RESOLVE", "a dead leg must not leave the dispatch renamed"


# ── #43: the journal records the model that RAN ──────────────────────────────
def test_the_model_the_run_reports_wins_over_the_one_the_task_asked_for(tmp_path):
    """An unpinned task carries model=None and the runtime supplies its own
    default; without this the journal recorded nothing for a leg that certainly
    ran on something, and cost could not be split by routed tier."""
    rt, journal, _ = _runtime(
        tmp_path, AgentResult(is_error=False, cost_usd=1.0, model="gpt-5.6-terra")
    )
    asyncio.run(rt.run(AgentTask(prompt="p")))
    assert _settled(journal.path)[0]["model"] == "gpt-5.6-terra"


def test_a_runtime_that_reports_no_model_still_falls_back_to_the_task(tmp_path):
    rt, journal, _ = _runtime(tmp_path, AgentResult(is_error=False, cost_usd=1.0))
    asyncio.run(rt.run(AgentTask(prompt="p", model="opus")))
    assert _settled(journal.path)[0]["model"] == "opus"


def test_configured_model_precedes_runtime_model_in_every_journal_receipt(tmp_path):
    """Configured model attribution stays consistent across journal and spend records."""
    rt, journal, ledger = _runtime(
        tmp_path,
        AgentResult(
            is_error=False,
            cost_usd=1.0,
            model="claude-opus-4-1",
            telemetry_warning="missing usage",
        ),
    )

    asyncio.run(rt.run(AgentTask(prompt="p", model="opus")))

    assert {
        event["event"]: event["model"]
        for event in read_events(journal.path)
    } == {
        "agent_run_start": "opus",
        "agent_run": "opus",
        "telemetry_warning": "opus",
    }
    assert read_events(ledger)[0]["model"] == "opus"


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


def test_review_with_a_configured_model_journals_it_at_start_and_settlement(tmp_path):
    """A configured REVIEW_MODEL is visible before and after the SDK session."""
    rt, journal, _ = _runtime(tmp_path, AgentResult(is_error=False))
    rt.set_context("review", "REVIEW", 100, 91)

    asyncio.run(rt.run(AgentTask(prompt="review", model="claude-opus-4-1")))

    start, settled = read_events(journal.path)
    assert start["model"] == "claude-opus-4-1"
    assert settled["model"] == "claude-opus-4-1"


def test_a_deterministic_death_is_journaled_distinctly_with_the_offending_path(tmp_path):
    # #13: `transport_death` alone cannot distinguish a loop that will never
    # converge from one a retry rescues, and the operator's first question is
    # which file killed it.
    boom = RuntimeError(
        "Fatal error in message reader: Failed to decode JSON: JSON message "
        "exceeded maximum buffer size of 1048576 bytes"
    )
    boom.harness_last_trace = "AssistantMessage → Read(evidence/ac-3-titles.png)"
    journal = Journal(str(tmp_path / "journal.jsonl"))
    rt = JournalingRuntime(RaisingInner(boom), journal, str(tmp_path / "spend.jsonl"), "o/r", "review")
    rt.set_context("review", "REVIEW", 552, 545)

    with pytest.raises(RuntimeError):
        asyncio.run(rt.run(AgentTask(prompt="p")))

    _, settled = read_events(journal.path)
    assert settled["exit_reason"] == "transport_death"
    assert settled["deterministic"] == "exceeded maximum buffer size"
    assert settled["detail"] == "evidence/ac-3-titles.png"


def test_an_ordinary_transport_death_is_not_marked_deterministic(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    rt = JournalingRuntime(
        RaisingInner(RuntimeError("Stream closed unexpectedly")),
        journal, str(tmp_path / "spend.jsonl"), "o/r", "review",
    )
    rt.set_context("review", "REVIEW", 552, 545)

    with pytest.raises(RuntimeError):
        asyncio.run(rt.run(AgentTask(prompt="p")))

    _, settled = read_events(journal.path)
    assert settled["deterministic"] is None and settled["detail"] is None


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


def test_issue_block_and_reply_resume_journal_one_human_wait_span(tmp_path):
    journal = Journal(str(tmp_path / "j.jsonl"))
    ex = Executor(FakeGateway(), journal=journal)

    ex.execute(Relabel(Target.ISSUE, 5, Label.AI_WORKING, Label.AI_BLOCKED))
    ex.execute(Relabel(Target.ISSUE, 5, Label.AI_BLOCKED, Label.AI_WORKING))

    waits = [e for e in read_events(journal.path) if e["event"] == "deferred"]
    assert [(e["state"], e["reason"], e["blocker"]) for e in waits] == [
        ("waiting", "human", "human"),
        ("cleared", "human", "human"),
    ]


def test_pr_block_does_not_create_an_issue_human_wait(tmp_path):
    journal = Journal(str(tmp_path / "j.jsonl"))
    ex = Executor(FakeGateway(), journal=journal)

    ex.execute(Relabel(Target.PR, 100, Label.AI_PR_OPEN, Label.AI_BLOCKED))

    assert [e for e in read_events(journal.path) if e["event"] == "deferred"] == []


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
                    global_daily_cap=0.0,
                    alert_cmd="", gw=None, logs=None, trigger_label="ai-pr-open"):
    gw = gw or CapGateway()
    runner = RecordingRunner()
    journal = Journal(str(tmp_path / "j.jsonl"))
    cfg = DispatchConfig(
        # Pinned model: keeps the difficulty-router leg out of these scripted
        # run counts (see test_fix_workflow's routing tests for the unpinned path).
        fix=FixTaskConfig("o/r", agent_md="AM", model="m"),
        review=ReviewTaskConfig("o/r"),
        review_trigger_label=trigger_label,
        role="fix", repo="o/r", spend_ledger_path=ledger,
        issue_spend_cap_usd=issue_cap, daily_spend_cap_usd=daily_cap,
        global_daily_spend_cap_usd=global_daily_cap,
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

    def get_model(self, i):
        return ""

    def save_model(self, i, m):
        pass

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

    def get_infra_deaths(self, key):
        return 0

    def bump_infra_deaths(self, key):
        return 1

    def clear_infra_deaths(self, key):
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
    assert block[0]["scope"] == "daily-repo" and block[0]["cap"] == 50.0
    assert any("SPEND CAP" in line for line in logs), "the pause must be loud"
    # #32: which cap paused the box must be readable off the line itself —
    # DAILY_SPEND_CAP_USD (this repo) and GLOBAL_DAILY_SPEND_CAP_USD send an
    # operator to different files.
    assert any("o/r" in line and "DAILY_SPEND_CAP_USD" in line for line in logs)
    assert not any("GLOBAL_DAILY_SPEND_CAP_USD" in line for line in logs)


def test_the_daily_cap_ignores_another_repos_spend(tmp_path):
    """#32. The ledger is shared by every deployment on the box, but
    DAILY_SPEND_CAP_USD is configured in the per-repo overlay — so summing it
    unscoped made the first deployment to spend starve every other one (keifu was
    blocked on tauri-explorer's $709 having spent $0 itself)."""
    now = datetime.now(timezone.utc)
    ledger = _write(str(tmp_path / "spend.jsonl"),
                    _spend(now, 709.55, repo="other/repo"),
                    _spend(now, 1.0, repo="o/r"))
    d, _, runner, journal = _cap_dispatcher(tmp_path, ledger, daily_cap=50.0)

    asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, 5)))

    assert runner.runs == 1
    assert [e for e in read_events(journal.path) if e["event"] == "spend_block"] == []


def test_the_global_cap_pauses_a_repo_that_has_spent_nothing(tmp_path):
    """The deliberate other half of #32: a box-wide bound must be expressible,
    and GLOBAL_DAILY_SPEND_CAP_USD is where it lives. Anti-vacuity twin of the
    test above — the same ledger, a different knob, the opposite outcome."""
    now = datetime.now(timezone.utc)
    ledger = _write(str(tmp_path / "spend.jsonl"),
                    _spend(now, 709.55, repo="other/repo"))
    logs: list[str] = []
    d, gw, runner, journal = _cap_dispatcher(
        tmp_path, ledger, daily_cap=50.0, global_daily_cap=700.0, logs=logs)

    asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, 5)))

    assert runner.runs == 0
    assert gw.calls == []
    block = [e for e in read_events(journal.path) if e["event"] == "spend_block"][0]
    assert block["scope"] == "daily-global" and block["cap"] == 700.0
    assert any("GLOBAL_DAILY_SPEND_CAP_USD" in line for line in logs)


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
    """The cap is charged to the ISSUE behind the PR — but the escalation moves
    the PR, because a PR leg is picked from the PR's own labels (review P1R-2).
    Relabelling only the issue left the PR ai-pr-open and pickable, so the very
    next poll hit the same cap again."""
    ledger = _write(str(tmp_path / "spend.jsonl"), _spend(NOW, 30.0, issue=5))
    gw = CapGateway(issue_for_pr={100: 5})
    d, gw, runner, journal = _cap_dispatcher(tmp_path, ledger, issue_cap=25.0, gw=gw)
    asyncio.run(d.dispatch(RunAgent(AgentKind.FIX_CI, 100)))
    assert runner.runs == 0
    assert ("relabel", "pr", 100, "ai-pr-open", "needs-human-review") in gw.calls
    # …and the money is still attributed to the issue that spent it.
    block = [e for e in read_events(journal.path) if e["event"] == "spend_block"][0]
    assert block["issue"] == 5 and (block["target"], block["number"]) == ("pr", 100)


def test_a_review_leg_escalates_the_pr_from_the_configured_trigger_label(tmp_path):
    """A repo where another bot owns ai-pr-open runs the reviewer on ai-review;
    removing the wrong label leaves the PR in the reviewer's pickable set
    forever (review M10/D4), which is the loop this escalation exists to end."""
    ledger = _write(str(tmp_path / "spend.jsonl"), _spend(NOW, 30.0, issue=5))
    gw = CapGateway(issue_for_pr={100: 5})
    d, gw, _, _ = _cap_dispatcher(
        tmp_path, ledger, issue_cap=25.0, gw=gw, trigger_label="ai-review")
    asyncio.run(d.dispatch(RunAgent(AgentKind.REVIEW, 100)))
    assert ("relabel", "pr", 100, "ai-review", "needs-human-review") in gw.calls


def test_an_issue_keyed_leg_still_escalates_the_issue(tmp_path):
    """Anti-vacuity twin of the two above: RESOLVE is gated on the ISSUE's
    labels, so moving the PR there would escalate nothing that stops dispatch."""
    ledger = _write(str(tmp_path / "spend.jsonl"), _spend(NOW, 30.0, issue=5))
    d, gw, _, _ = _cap_dispatcher(tmp_path, ledger, issue_cap=25.0)
    asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, 5)))
    assert ("relabel", "issue", 5, "ai-working", "needs-human-review") in gw.calls


def test_ten_polls_of_a_capped_leg_emit_one_block_and_one_alert(tmp_path):
    """review P1R-2. A cap is a LEVEL, not an event: it stays tripped on every
    later poll. Un-deduped that was 10 journal events + 10 ALERT_CMD subprocesses
    per 10 polls — ~4,300 a day at POLL_SECONDS=20, which is how a pager gets
    muted. The gateway here never actually applies the escalation (the PR keeps
    coming back), which is exactly the wedged shape that produced the flood."""
    ledger = _write(str(tmp_path / "spend.jsonl"), _spend(NOW, 30.0, issue=5))
    sink = tmp_path / "alerts.jsonl"
    gw = CapGateway(issue_for_pr={100: 5})
    d, gw, runner, journal = _cap_dispatcher(
        tmp_path, ledger, issue_cap=25.0, gw=gw, alert_cmd=f"cat >> {sink}")

    for _ in range(10):
        asyncio.run(d.dispatch(RunAgent(AgentKind.FIX_CI, 100)))

    assert runner.runs == 0
    blocks = [e for e in read_events(journal.path) if e["event"] == "spend_block"]
    assert len(blocks) == 1, "the block is an edge, not a level"
    payloads = [json.loads(ln) for ln in sink.read_text().splitlines() if ln.strip()]
    assert [p["event"] for p in payloads].count("spend_block") == 1
    comments = [call for call in gw.calls if call[0] == "comment"]
    assert len(comments) == 1, "the GitHub escalation is the same edge as the alert"


def test_the_block_dedup_survives_a_restart(tmp_path):
    """The flood shape is a wedged daemon under `Restart=always`, so an
    in-process set alone is empty again every few seconds. A SECOND dispatcher
    over the same journal — a restarted daemon — must stay silent."""
    ledger = _write(str(tmp_path / "spend.jsonl"), _spend(NOW, 30.0, issue=5))
    first, _, _, journal = _cap_dispatcher(
        tmp_path, ledger, issue_cap=25.0, gw=CapGateway(issue_for_pr={100: 5}))
    asyncio.run(first.dispatch(RunAgent(AgentKind.FIX_CI, 100)))

    restarted, _, _, _ = _cap_dispatcher(
        tmp_path, ledger, issue_cap=25.0, gw=CapGateway(issue_for_pr={100: 5}))
    asyncio.run(restarted.dispatch(RunAgent(AgentKind.FIX_CI, 100)))

    assert len([e for e in read_events(journal.path) if e["event"] == "spend_block"]) == 1


def test_duplicate_block_retries_relabel_without_reposting_comment(tmp_path):
    class RelabelFailsOnce(CapGateway):
        def __init__(self):
            super().__init__()
            self.relabel_attempts = 0

        def relabel(self, kind, number, remove, add):
            self.relabel_attempts += 1
            if self.relabel_attempts == 1:
                raise RuntimeError("transient relabel failure")
            super().relabel(kind, number, remove, add)

    ledger = _write(str(tmp_path / "spend.jsonl"), _spend(NOW, 30.0, issue=5))
    gw = RelabelFailsOnce()
    dispatcher, _, _, _ = _cap_dispatcher(
        tmp_path, ledger, issue_cap=25.0, gw=gw,
    )

    asyncio.run(dispatcher.dispatch(RunAgent(AgentKind.RESOLVE, 5)))
    asyncio.run(dispatcher.dispatch(RunAgent(AgentKind.RESOLVE, 5)))

    assert gw.relabel_attempts == 2
    assert len([call for call in gw.calls if call[0] == "comment"]) == 1
    assert ("relabel", "issue", 5, "ai-working", "needs-human-review") in gw.calls


def test_a_changed_issue_cap_may_escalate_once_again(tmp_path):
    ledger = _write(str(tmp_path / "spend.jsonl"), _spend(NOW, 30.0, issue=5))
    first, first_gw, _, journal = _cap_dispatcher(
        tmp_path, ledger, issue_cap=25.0, gw=CapGateway(issue_for_pr={100: 5}))
    asyncio.run(first.dispatch(RunAgent(AgentKind.FIX_CI, 100)))

    changed, changed_gw, _, _ = _cap_dispatcher(
        tmp_path, ledger, issue_cap=20.0, gw=CapGateway(issue_for_pr={100: 5}))
    asyncio.run(changed.dispatch(RunAgent(AgentKind.FIX_CI, 100)))

    assert len([c for c in first_gw.calls if c[0] == "comment"]) == 1
    assert len([c for c in changed_gw.calls if c[0] == "comment"]) == 1
    blocks = [e for e in read_events(journal.path) if e["event"] == "spend_block"]
    assert {e["cap"] for e in blocks} == {20.0, 25.0}


def test_a_different_capped_issue_still_gets_its_own_block(tmp_path):
    """Anti-vacuity twin: a dedup keyed too coarsely would silence every issue
    after the first, and the second issue's block is new information."""
    ledger = _write(str(tmp_path / "spend.jsonl"),
                    _spend(NOW, 30.0, issue=5), _spend(NOW, 30.0, issue=6))
    d, _, _, journal = _cap_dispatcher(tmp_path, ledger, issue_cap=25.0)

    asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, 5)))
    asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, 6)))

    blocks = [e for e in read_events(journal.path) if e["event"] == "spend_block"]
    assert {b["issue"] for b in blocks} == {5, 6}


def test_the_daily_block_is_keyed_to_the_utc_day_it_pauses(tmp_path):
    """The daily cap heals itself at 00:00 UTC, so its dedup must too — a key
    without the date would silence the pause forever after the first day."""
    ledger = _write(str(tmp_path / "spend.jsonl"),
                    _spend(datetime.now(timezone.utc), 60.0))
    d, _, _, journal = _cap_dispatcher(tmp_path, ledger, daily_cap=50.0)

    for _ in range(5):
        asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, 5)))

    blocks = [e for e in read_events(journal.path) if e["event"] == "spend_block"]
    assert len(blocks) == 1
    today = datetime.now(timezone.utc).date().isoformat()
    assert blocks[0]["block_key"] == f"daily-repo:{today}:cap:50"


def test_a_changed_daily_cap_emits_a_new_edge(tmp_path):
    ledger = _write(str(tmp_path / "spend.jsonl"),
                    _spend(datetime.now(timezone.utc), 60.0))
    first, _, _, journal = _cap_dispatcher(
        tmp_path, ledger, daily_cap=50.0,
    )
    asyncio.run(first.dispatch(RunAgent(AgentKind.RESOLVE, 5)))

    changed, _, _, _ = _cap_dispatcher(
        tmp_path, ledger, daily_cap=40.0,
    )
    asyncio.run(changed.dispatch(RunAgent(AgentKind.RESOLVE, 5)))

    blocks = [e for e in read_events(journal.path) if e["event"] == "spend_block"]
    assert {event["cap"] for event in blocks} == {40.0, 50.0}


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


# ── the caps are blind when every run costs $0 (review P1R-5) ────────────────
def _zero_runs(n, cost=0.0):
    return [{"ts": _at(NOW), "event": "agent_run", "kind": "RESOLVE",
             "number": 5, "cost_usd": cost} for _ in range(n)]


def test_a_ledger_that_can_never_receive_a_line_is_announced():
    """`_record` writes a ledger line only when cost > 0 — right, because a
    $0.00 line is noise in the file both caps scan every dispatch. But a
    subscription plan reports $0.00 for EVERY run, so the ledger stays empty,
    both caps read $0.00 forever and neither can fire for any loop shape. The
    operator has to be told the cap they configured is inert."""
    warning = cost_blindspot_warning(
        _zero_runs(COST_BLINDSPOT_MIN_RUNS),
        issue_spend_cap_usd=25.0, daily_spend_cap_usd=50.0,
    )
    assert warning is not None
    assert "$25.00" in warning and "$50.00" in warning
    assert "$0.00" in warning and "NEITHER cap can ever fire" in warning


def test_one_costed_run_proves_the_ledger_works_and_silences_the_warning():
    """Anti-vacuity twin: on a metered plan this must say nothing at all, or it
    becomes the permanently-on line operators learn to scroll past."""
    runs = _zero_runs(COST_BLINDSPOT_MIN_RUNS) + _zero_runs(1, cost=0.42)
    assert cost_blindspot_warning(
        runs, issue_spend_cap_usd=25.0, daily_spend_cap_usd=50.0) is None


def test_no_armed_cap_means_nothing_to_be_blind_about():
    assert cost_blindspot_warning(
        _zero_runs(50), issue_spend_cap_usd=0.0, daily_spend_cap_usd=0.0) is None


def test_too_few_runs_is_not_yet_evidence():
    """One free run is a session that died before billing, not a blind cap."""
    assert cost_blindspot_warning(
        _zero_runs(COST_BLINDSPOT_MIN_RUNS - 1),
        issue_spend_cap_usd=25.0, daily_spend_cap_usd=50.0) is None


def test_the_dispatcher_warns_once_when_its_caps_cannot_see_a_dollar(tmp_path):
    """End to end through the dispatcher: a journal of free runs + an armed cap
    → exactly ONE line, however many dispatches follow."""
    ledger = str(tmp_path / "spend.jsonl")
    _write(str(tmp_path / "j.jsonl"),
           *[json.dumps(r) for r in _zero_runs(COST_BLINDSPOT_MIN_RUNS)])
    logs: list[str] = []
    d, _, runner, journal = _cap_dispatcher(
        tmp_path, ledger, issue_cap=25.0, logs=logs)

    asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, 5)))
    asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, 5)))

    assert runner.runs == 2, "a blind cap must not also block work"
    blind = [ln for ln in logs if "NEITHER cap can ever fire" in ln]
    assert len(blind) == 1
    assert len([e for e in read_events(journal.path)
                if e["event"] == "cost_blindspot"]) == 1


def test_the_dispatcher_stays_silent_when_the_ledger_is_being_written(tmp_path):
    """Anti-vacuity twin: the same journal with costed runs must produce no line
    — otherwise the warning is decoration, not a signal."""
    ledger = str(tmp_path / "spend.jsonl")
    _write(str(tmp_path / "j.jsonl"),
           *[json.dumps(r) for r in _zero_runs(COST_BLINDSPOT_MIN_RUNS, cost=1.0)])
    logs: list[str] = []
    d, _, _, _ = _cap_dispatcher(tmp_path, ledger, issue_cap=100.0, logs=logs)

    asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, 5)))

    assert [ln for ln in logs if "NEITHER cap can ever fire" in ln] == []


def test_spend_blocked_keys_reads_only_keyed_blocks():
    events = [
        {"event": "spend_block", "block_key": "issue:o/r:5"},
        {"event": "spend_block", "scope": "daily"},          # pre-upgrade line
        {"event": "agent_run", "block_key": "issue:o/r:9"},  # not a block
    ]
    assert spend_blocked_keys(events) == {"issue:o/r:5"}


def test_hold_keys_reads_only_keyed_holds():
    events = [
        {"event": "hold", "block_key": "hold:o/r:522:overlap:pr#123"},
        {"event": "hold", "reason": "blocker"},                  # unkeyed → ignored
        {"event": "spend_block", "block_key": "issue:o/r:9"},    # a different level
    ]
    assert hold_keys(events) == {"hold:o/r:522:overlap:pr#123"}


def test_hold_keys_survives_a_round_trip_through_a_real_journal(tmp_path):
    """The dedup that matters is the one across a process restart, so the read
    must work off what `Journal.emit` actually wrote — not a hand-built dict."""
    journal = Journal(str(tmp_path / "j.jsonl"))
    journal.emit("hold", issue=522, reason="overlap", blocker="pr#123",
                 block_key="hold:o/r:522:overlap:pr#123")
    assert hold_keys(read_events_tail(journal.path)) == {"hold:o/r:522:overlap:pr#123"}


def test_recurrence_keys_reads_only_keyed_recurrences():
    events = [
        {"event": "recurrence", "block_key": "recurrence:o/r:522:reopen"},
        {"event": "recurrence", "kind": "reopen"},              # unkeyed → ignored
        {"event": "hold", "block_key": "hold:o/r:522:overlap"},  # a different level
    ]
    assert recurrence_keys(events) == {"recurrence:o/r:522:reopen"}


def test_recurrence_keys_survive_a_round_trip_through_a_real_journal(tmp_path):
    """The dedup that matters spans daemon restarts, so it must read what
    `Journal.emit` actually wrote — and the two KINDS must not collide."""
    journal = Journal(str(tmp_path / "j.jsonl"))
    journal.emit("recurrence", kind="reopen", issue=522, of_issue=522, pr=9,
                 block_key="recurrence:o/r:522:reopen")
    journal.emit("recurrence", kind="refiled", issue=530, of_issue=522, pr=9,
                 block_key="recurrence:o/r:530:refiled:522")
    assert recurrence_keys(read_events_tail(journal.path)) == {
        "recurrence:o/r:522:reopen", "recurrence:o/r:530:refiled:522",
    }


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
