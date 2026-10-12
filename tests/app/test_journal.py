"""Event journal, cost ledger, spend circuit breakers, alert hook.

Two properties dominate here and both are about NOT making things worse:
observability never raises (a read-only state dir must not kill a daemon), and
readers tolerate a truncated tail (a crash mid-write must not poison the file).
The rest verifies the caps actually gate dispatch — that the handler is never
called — rather than merely that a label moved.
"""

from __future__ import annotations

from issue_agent.domain.checks import Check

from tests.domain.builders import observed_pr

import asyncio
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from tests.fakes.legacy_journal import Journal
from issue_agent.adapters.journal import (
    JournalingRuntime,
    alert,
    death_key,
    default_ledger_path,
    events_within,
    hold_keys,
    read_events,
    read_events_tail,
    recent_runs,
    repeated_dispatch_warning,
    spend_blocked_keys,
    sum_issue,
    sum_today,
    unsettled_starts,
)
from issue_agent.adapters.store import Store
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.app.dispatch import AgentDispatcher, DispatchConfig
from tests.fakes.executor import ImmediateExecutor as Executor
from issue_agent.app.planners.tasks import FixTaskConfig, ReviewTaskConfig
from issue_agent.app.review_runtime import review_guidance_reader
from issue_agent.domain.actions import AgentKind, Relabel, RunAgent, Target
from issue_agent.adapters.runtime_plan import AgentRuntime, RuntimePlan
from issue_agent.domain.labels import Label
from issue_agent.domain.outcome import AgentResult, ExitReason
from issue_agent.domain.world import IssueView, PRView
from issue_agent.domain.workflow_identity import PullRequestRef, WorkflowKey
from tests.fakes.in_memory_github import InMemoryGitHub as FakeGateway
from tests.fakes.review_workflow import held as held_review
from tests.fakes.workflow_authority import install_workflow

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


def test_monthly_ledger_rotation_preserves_prior_file(tmp_path):
    path = tmp_path / "spend.jsonl"
    path.write_text('{"event":"spend","cost_usd":1}\n')
    old = datetime(2020, 1, 2, tzinfo=timezone.utc).timestamp()
    os.utime(path, (old, old))

    Journal(str(path), monthly_rotation=True).emit(
        "spend", schema=2, cost_usd=2, currency="USD"
    )

    assert json.loads(path.read_text())["cost_usd"] == 2
    assert json.loads((tmp_path / "spend.2020-01.jsonl").read_text())[
        "cost_usd"
    ] == 1


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
def _start(dt, kind="RESOLVE", number=5, issue=5, dispatch="", role="fix"):
    return {"ts": _at(dt), "event": "agent_run_start", "role": role,
            "kind": kind, "number": number, "issue": issue, "model": "opus",
            "dispatch": dispatch}


def _run(dt, kind="RESOLVE", number=5, issue=5, dispatch="", role="fix", **extra):
    return {"ts": _at(dt), "event": "agent_run", "role": role, "kind": kind,
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
    starts = [_start(NOW - timedelta(seconds=60 - i), kind="REVIEW", number=100, issue=7, role="review")
              for i in range(8)]
    settles = [_run(NOW - timedelta(seconds=30 - i), kind="REVIEW", number=100, issue=7, role="review")
               for i in range(8)]
    assert unsettled_starts(starts + settles, now_fn=lambda: NOW) == {}
    # …and the same journal WITH one leg killed reports exactly one death.
    assert unsettled_starts(starts + settles[:-1], now_fn=lambda: NOW) == {-100: 1}


def test_a_dispatch_killed_mid_fanout_is_ONE_death_not_one_per_leg():
    """The money question a per-leg count gets wrong. A `systemctl restart`
    during a REVIEW_MODE=multi review kills a gate + 5 dimension legs at once;
    charging six deaths trips a ceiling of five on a single routine restart,
    which is exactly the "memory pressure must not walk healthy work to a human"
    rule (FAILURE-MODES #58) inverted."""
    legs = [_start(NOW - timedelta(seconds=60 - i), kind="REVIEW", number=100,
                   issue=7, dispatch="d1", role="review") for i in range(6)]
    settled_two = [_run(NOW - timedelta(seconds=30 - i), kind="REVIEW", number=100,
                        issue=7, dispatch="d1", role="review") for i in range(2)]
    assert unsettled_starts(legs + settled_two, now_fn=lambda: NOW) == {-100: 1}


def test_separate_dispatches_are_separate_deaths():
    """Anti-vacuity twin: collapsing legs must not collapse POLLS. Three
    consecutive killed dispatches are three paid rounds, and a ceiling that saw
    one would never trip on the loop it exists for."""
    events = [
        _start(NOW - timedelta(minutes=m), kind="REVIEW", number=100, issue=7,
               dispatch=f"d{m}", role="review")
        for m in (30, 20, 10)
    ]
    assert unsettled_starts(events, now_fn=lambda: NOW) == {-100: 3}


def test_a_fully_settled_dispatch_is_no_death():
    legs = [_start(NOW - timedelta(seconds=60 - i), kind="REVIEW", number=100,
                   issue=7, dispatch="d1", role="review") for i in range(6)]
    settles = [_run(NOW - timedelta(seconds=30 - i), kind="REVIEW", number=100,
                    issue=7, dispatch="d1", role="review") for i in range(6)]
    assert unsettled_starts(legs + settles, now_fn=lambda: NOW) == {}


def test_the_real_runtime_stamps_one_dispatch_id_across_every_leg(tmp_path):
    """Pins the producer half of the rule: `set_context` is called once per
    dispatch, so every leg it runs must carry the SAME id — and the next
    dispatch a DIFFERENT one, or two polls' deaths collapse into one."""
    rt, journal, _ = _runtime(tmp_path, AgentResult(is_error=False))
    rt.set_context("review", "REVIEW", 100, 7)
    asyncio.run(rt.run(RuntimePlan(prompt="gate")))
    asyncio.run(rt.run(RuntimePlan(prompt="dimension")))
    first = {e["dispatch"] for e in read_events(journal.path)}
    assert len(first) == 1 and "" not in first

    rt.set_context("review", "REVIEW", 100, 7)
    asyncio.run(rt.run(RuntimePlan(prompt="gate")))
    assert len({e["dispatch"] for e in read_events(journal.path)}) == 2


def test_concurrent_dispatches_keep_task_local_journal_attribution(tmp_path):
    class YieldingRuntime:
        async def run(self, _task):
            await asyncio.sleep(0)
            return AgentResult(is_error=False, status="done")

    journal = Journal(str(tmp_path / "journal.jsonl"))
    runtime = JournalingRuntime(
        YieldingRuntime(), journal, repo="o/r", role="fix"
    )

    async def dispatch(kind, number):
        runtime.set_context("fix", kind, number, number)
        await runtime.run(RuntimePlan(prompt=kind))

    async def run_both():
        await asyncio.gather(
            dispatch("RESOLVE", 5),
            dispatch("RESUME", 7),
        )

    asyncio.run(run_both())

    events = read_events(journal.path)
    by_number = {
        number: [event for event in events if event["number"] == number]
        for number in (5, 7)
    }
    assert {
        number: {(event["kind"], event["issue"]) for event in rows}
        for number, rows in by_number.items()
    } == {
        5: {("RESOLVE", 5)},
        7: {("RESUME", 7)},
    }
    assert all(
        len({event["dispatch"] for event in rows}) == 1
        for rows in by_number.values()
    )
    assert by_number[5][0]["dispatch"] != by_number[7][0]["dispatch"]


def test_a_journal_written_before_dispatch_ids_still_counts_deaths():
    """Backward compatibility: an upgrade must not silently zero the counter for
    every run already on disk. With no `dispatch` field the rule falls back to
    (kind, number) counting."""
    legacy = {"ts": _at(NOW), "event": "agent_run_start", "kind": "RESOLVE",
              "number": 5, "issue": 5}
    assert unsettled_starts([legacy], now_fn=lambda: NOW) == {5: 1}


def test_review_deaths_key_to_the_pr_even_when_an_issue_is_linked():
    assert unsettled_starts(
        [_start(NOW, kind="REVIEW", number=100, issue=7, role="review")],
        now_fn=lambda: NOW,
    ) == {-100: 1}


def test_an_issueless_prs_death_keys_to_the_negated_pr_number():
    """`breaker_key`'s convention for a PR with no closing ref and a Jira-keyed
    branch: the dispatcher falls back to `-action.number`, so this must too."""
    assert unsettled_starts(
        [_start(NOW, kind="RE_REVIEW", number=112, issue=None, role="review")], now_fn=lambda: NOW
    ) == {-112: 1}
    assert death_key({"number": 112, "issue": None}) == -112
    assert death_key({"number": 112, "issue": 9}) == 9
    assert death_key({"role": "review", "number": 112, "issue": 9}) == -112
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
    return (
        JournalingRuntime(FakeInner(result), journal, repo=repo, role="fix"),
        journal,
        ledger,
    )


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
    asyncio.run(rt.run(RuntimePlan(prompt="p", model="opus")))

    run = _settled(journal.path)[0]
    assert run["event"] == "agent_run"
    assert (run["role"], run["kind"], run["number"], run["issue"]) == ("fix", "RESOLVE", 5, 5)
    assert run["model"] == "opus" and run["cost_usd"] == 1.25
    assert run["status"] == "pr_opened" and run["is_error"] is False
    assert run["exit_reason"] == "ok" and run["session_id"] == "s5"
    assert run["usage"] == {"input_tokens": 10, "output_tokens": 20,
                            "cache_read_input_tokens": 30, "cache_creation_input_tokens": 40}
    assert isinstance(run["duration_s"], float)

    assert read_events(ledger) == [], "legacy spend JSONL is read-only"


def test_agent_run_preserves_codex_total_only_usage(tmp_path):
    result = AgentResult(
        is_error=False,
        status="pr_opened",
        cost_usd=0.5,
        usage={"total_tokens": 50_000},
    )
    rt, journal, _ledger = _runtime(tmp_path, result)

    asyncio.run(rt.run(RuntimePlan(prompt="p")))

    assert _settled(journal.path)[0]["usage"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "total_tokens": 50_000,
    }


def test_agent_run_cost_rounds_identically_to_authoritative_money(tmp_path):
    result = AgentResult(
        is_error=False,
        status="ok",
        cost_usd=0.0065895,
    )
    rt, journal, _ledger = _runtime(tmp_path, result)

    asyncio.run(rt.run(RuntimePlan(prompt="p")))

    assert _settled(journal.path)[0]["cost_usd"] == 0.00659


def test_missing_usage_stays_distinct_from_zero_tokens(tmp_path):
    rt, journal, _ = _runtime(tmp_path, AgentResult(is_error=False, usage=None))
    asyncio.run(rt.run(RuntimePlan(prompt="p")))
    assert _settled(journal.path)[0]["usage"] is None


def test_a_free_run_is_journalled_but_never_hits_the_ledger(tmp_path):
    # $0.00 is what a subscription plan and Codex both report; a ledger full of
    # zero lines is noise in the file the caps scan on every dispatch.
    rt, journal, ledger = _runtime(tmp_path, AgentResult(is_error=False, cost_usd=0.0))
    asyncio.run(rt.run(RuntimePlan(prompt="p")))
    assert len(_settled(journal.path)) == 1
    assert read_events(ledger) == []


def test_each_leg_of_a_multi_leg_dispatch_gets_its_own_attributed_line(tmp_path):
    rt, journal, ledger = _runtime(tmp_path, AgentResult(is_error=False, cost_usd=0.5))
    rt.set_context("review", "REVIEW", 100, 5)
    for _ in range(3):
        asyncio.run(rt.run(RuntimePlan(prompt="leg")))
    assert len(_settled(journal.path)) == 3
    assert sum(
        event["cost_usd"] for event in _settled(journal.path)
    ) == pytest.approx(1.5)


# ── #40: one leg of a dispatch under its own kind ────────────────────────────
def test_a_leg_kind_renames_only_the_legs_inside_the_block(tmp_path):
    rt, journal, _ = _runtime(tmp_path, AgentResult(is_error=False, cost_usd=0.5))
    rt.set_context("fix", "RESOLVE", 5, 5)
    with rt.leg_kind("ROUTE"):
        asyncio.run(rt.run(RuntimePlan(prompt="classify")))
    asyncio.run(rt.run(RuntimePlan(prompt="resolve")))

    assert [r["kind"] for r in _settled(journal.path)] == ["ROUTE", "RESOLVE"]


def test_a_leg_kind_keeps_the_dispatch_id_and_the_rest_of_the_context(tmp_path):
    rt, journal, _ = _runtime(tmp_path, AgentResult(is_error=False, cost_usd=0.5))
    rt.set_context("fix", "RESOLVE", 5, 5)
    with rt.leg_kind("ROUTE"):
        asyncio.run(rt.run(RuntimePlan(prompt="classify")))
    asyncio.run(rt.run(RuntimePlan(prompt="resolve")))

    runs = _settled(journal.path)
    assert len({r["dispatch"] for r in runs}) == 1, "one dispatch, one id"
    assert all((r["role"], r["number"], r["issue"]) == ("fix", 5, 5) for r in runs)


def test_a_renamed_leg_keeps_its_cost_attribution(tmp_path):
    """A router run remains attributable under its own telemetry bucket."""
    rt, journal, ledger = _runtime(
        tmp_path, AgentResult(is_error=False, cost_usd=0.07)
    )
    rt.set_context("fix", "RESOLVE", 5, 5)
    with rt.leg_kind("ROUTE"):
        asyncio.run(rt.run(RuntimePlan(prompt="classify")))

    assert [
        (event["kind"], event["cost_usd"])
        for event in _settled(journal.path)
    ] == [("ROUTE", 0.07)]
    assert read_events(ledger) == []


def test_a_leg_kind_unwinds_when_the_leg_raises(tmp_path):
    class Boom:
        async def run(self, task):
            raise RuntimeError("router transport died")

    journal = Journal(str(tmp_path / "journal.jsonl"))
    rt = JournalingRuntime(Boom(), journal, repo="o/r", role="fix")
    rt.set_context("fix", "RESOLVE", 5, 5)
    with pytest.raises(RuntimeError):
        with rt.leg_kind("ROUTE"):
            asyncio.run(rt.run(RuntimePlan(prompt="classify")))
    assert rt.context.kind == "RESOLVE", "a dead leg must not leave the dispatch renamed"


# ── #43: the journal records the model that RAN ──────────────────────────────
def test_the_model_the_run_reports_wins_over_the_one_the_task_asked_for(tmp_path):
    """An unpinned task carries model=None and the runtime supplies its own
    default; without this the journal recorded nothing for a leg that certainly
    ran on something, and cost could not be split by routed tier."""
    rt, journal, _ = _runtime(
        tmp_path, AgentResult(is_error=False, cost_usd=1.0, model="gpt-5.6-terra")
    )
    asyncio.run(rt.run(RuntimePlan(prompt="p")))
    assert _settled(journal.path)[0]["model"] == "gpt-5.6-terra"


def test_a_runtime_that_reports_no_model_still_falls_back_to_the_task(tmp_path):
    rt, journal, _ = _runtime(tmp_path, AgentResult(is_error=False, cost_usd=1.0))
    asyncio.run(rt.run(RuntimePlan(prompt="p", model="opus")))
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

    asyncio.run(rt.run(RuntimePlan(prompt="p", model="opus")))

    assert {
        event["event"]: event["model"]
        for event in read_events(journal.path)
    } == {
        "agent_run_start": "opus",
        "agent_run": "opus",
        "telemetry_warning": "opus",
    }
    assert read_events(ledger) == []


def test_result_is_returned_unchanged_and_inner_still_sees_the_task(tmp_path):
    expected = AgentResult(is_error=False, status="done")
    rt, _, _ = _runtime(tmp_path, expected)
    task = RuntimePlan(prompt="p")
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
    rt = JournalingRuntime(inner, journal, repo="o/r", role="fix")
    rt.set_context("fix", "RESOLVE", 5, 5)

    asyncio.run(rt.run(RuntimePlan(prompt="p", model="opus")))

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
    rt = JournalingRuntime(
        RaisingInner(boom), journal, repo="o/r", role="fix"
    )
    rt.set_context("fix", "RESOLVE", 5, 5)

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(rt.run(RuntimePlan(prompt="p", model="opus")))
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

    asyncio.run(rt.run(RuntimePlan(prompt="review", model="claude-opus-4-1")))

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
    rt = JournalingRuntime(
        RaisingInner(boom), journal, repo="o/r", role="review"
    )
    rt.set_context("review", "REVIEW", 552, 545)

    with pytest.raises(RuntimeError):
        asyncio.run(rt.run(RuntimePlan(prompt="p")))

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
        asyncio.run(rt.run(RuntimePlan(prompt="p")))

    _, settled = read_events(journal.path)
    assert settled["deterministic"] is None and settled["detail"] is None


def test_a_normal_run_is_never_settled_as_a_transport_death(tmp_path):
    # Anti-vacuity twin: the raise path must not be reachable from a run that
    # merely returned an error result — those still count as attempts.
    rt, journal, _ = _runtime(
        tmp_path, AgentResult(is_error=True, status="failed", exit_reason=ExitReason.AGENT_ERROR))
    rt.set_context("fix", "RESOLVE", 5, 5)
    asyncio.run(rt.run(RuntimePlan(prompt="p")))

    events = read_events(journal.path)
    assert [e["event"] for e in events] == ["agent_run_start", "agent_run"]
    assert events[1]["exit_reason"] == "agent_error" and events[1]["status"] == "failed"
    assert "error" not in events[1]


def test_a_journal_that_cannot_write_never_fails_a_run(tmp_path, capsys):
    # Observability is never a failure mode (module rule 1) — and that has to
    # hold for the two NEW writes as well, on both paths.
    expected = AgentResult(is_error=False, status="done")
    ok = JournalingRuntime(FakeInner(expected), PoisonedJournal())
    assert asyncio.run(ok.run(RuntimePlan(prompt="p"))) is expected

    boom = RuntimeError("Stream closed unexpectedly")
    dead = JournalingRuntime(RaisingInner(boom), PoisonedJournal())
    with pytest.raises(RuntimeError) as raised:
        asyncio.run(dead.run(RuntimePlan(prompt="p")))
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
    rt = JournalingRuntime(Shadowing(), journal, repo="o/r", role="fix")
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


def test_dispatcher_tags_the_runtime_with_the_dispatch_context(tmp_path):
    ledger = str(tmp_path / "spend.jsonl")
    journal = Journal(str(tmp_path / "j.jsonl"))
    inner = FakeInner(AgentResult(is_error=False, status="pushed", cost_usd=0.5))
    runtime = JournalingRuntime(inner, journal, repo="o/r", role="fix")
    gw = FakeGateway(
        issues={5: IssueView(5, title="t", body="b")},
        prs={100: observed_pr(100, head_ref_name="fix/5-context", closing_issue=5,
            checks=(Check("unit", bucket="fail"),))},
    )
    gw.pr_issues[100] = 5
    control = SQLiteControlStore(str(tmp_path / "control.sqlite"))
    control.initialize()
    install_workflow(control, replace(held_review(key=WorkflowKey("o/r", PullRequestRef(100))), gates=()))
    cfg = DispatchConfig(fix=FixTaskConfig("o/r", agent_md="AM"), role="fix", repo="o/r",
                         operator_guidance=review_guidance_reader(control, "o/r"))

    class _WT:
        def ensure(self, issue, branch):
            return f"/wt/{issue}"

        def remove(self, issue):
            pass

        def reset(self, issue):
            pass

    from tests.fakes.accounted_dispatch import with_resolve_authority
    d = with_resolve_authority(AgentDispatcher(
        cfg,
        gw,
        Store(str(tmp_path / "state")),
        _WT(),
        runtime,
        Executor(gw),
        journal=journal,
    ))
    asyncio.run(d.dispatch(RunAgent(AgentKind.FIX_CI, 100)))

    run = [e for e in read_events(journal.path) if e["event"] == "agent_run"][0]
    assert (run["role"], run["kind"], run["number"], run["issue"]) == ("fix", "FIX_CI", 100, 5)
    assert run["issue"] == 5
    assert read_events(ledger) == []


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

    assert spend_status_lines(str(tmp_path / "j.jsonl")) == []


def test_status_summary_reports_sqlite_spend_without_legacy_history(
    tmp_path,
):
    from issue_agent.app.cli import spend_status_lines

    output = "\n".join(
        spend_status_lines(
            str(tmp_path / "absent-journal.jsonl"),
            50.0,
            lambda: NOW,
            repo="o/r",
            global_cap=100.0,
            repository_spent=1.25,
            all_repositories_spent=3.5,
        )
    )

    assert "today, o/r: $1.25 / $50.00 cap" in output
    assert "today, all repos: $3.50 / $100.00 cap" in output


def test_status_summary_reports_spend_runs_and_warnings(tmp_path):
    from issue_agent.app.cli import spend_status_lines

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
    out = "\n".join(
        spend_status_lines(
            journal,
            50.0,
            lambda: NOW,
            all_repositories_spent=12.5,
        )
    )

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
