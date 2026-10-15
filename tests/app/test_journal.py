"""Event journal, cost ledger, spend circuit breakers.

Two properties dominate here and both are about NOT making things worse:
observability never raises (a read-only state dir must not kill a daemon), and
readers tolerate a truncated tail (a crash mid-write must not poison the file).
The rest verifies the caps actually gate dispatch — that the handler is never
called — rather than merely that a label moved.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.fakes.legacy_journal import Journal, read_events
from issue_agent.adapters.journal import JournalingRuntime
from issue_agent.adapters.runtime_plan import RuntimePlan
from issue_agent.app.cli import spend_status_lines
from issue_agent.domain.outcome import AgentResult, ExitReason


def _leg(rt, prompt="p", **plan):
    return asyncio.run(rt.run(RuntimePlan(prompt=prompt, **plan)))


def test_the_real_runtime_stamps_one_dispatch_id_across_every_leg(tmp_path):
    """Pins the producer half of the rule: `set_context` is called once per
    dispatch, so every leg it runs must carry the SAME id — and the next
    dispatch a DIFFERENT one, or two polls' deaths collapse into one."""
    rt, journal = _runtime(tmp_path, AgentResult(is_error=False))
    rt.set_context("review", "REVIEW", 100, 7)
    _leg(rt, "gate")
    _leg(rt, "dimension")
    first = {e["dispatch"] for e in read_events(journal.path)}
    assert len(first) == 1 and "" not in first

    rt.set_context("review", "REVIEW", 100, 7)
    _leg(rt, "gate")
    assert len({e["dispatch"] for e in read_events(journal.path)}) == 2


def test_concurrent_dispatches_keep_task_local_journal_attribution(tmp_path):
    class YieldingRuntime:
        async def run(self, _task):
            await asyncio.sleep(0)
            return AgentResult(is_error=False, status="done")

    journal = Journal(str(tmp_path / "journal.jsonl"))
    runtime = JournalingRuntime(YieldingRuntime(), journal, repo="o/r", role="fix")

    async def dispatch(kind, number):
        runtime.set_context("fix", kind, number, number)
        await runtime.run(RuntimePlan(prompt=kind))

    async def run_both():
        await asyncio.gather(dispatch("RESOLVE", 5), dispatch("RESUME", 7))

    asyncio.run(run_both())

    events = read_events(journal.path)
    by_number = {number: [event for event in events if event["number"] == number] for number in (5, 7)}
    assert {number: {(event["kind"], event["issue"]) for event in rows} for number, rows in by_number.items()} == {
        5: {("RESOLVE", 5)},
        7: {("RESUME", 7)},
    }
    assert all(len({event["dispatch"] for event in rows}) == 1 for rows in by_number.values())
    assert by_number[5][0]["dispatch"] != by_number[7][0]["dispatch"]


# ── JournalingRuntime ─────────────────────────────────────────────────────────
class FakeInner:
    def __init__(self, result):
        self.result = result
        self.tasks = []

    async def run(self, task):
        self.tasks.append(task)
        return self.result


def _runtime(tmp_path, inner, role="fix"):
    """A journaling runtime over ``inner`` (a bare result means a FakeInner)."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    inner = FakeInner(inner) if isinstance(inner, AgentResult) else inner
    return JournalingRuntime(inner, journal, repo="o/r", role=role), journal


def _settled(journal_path) -> list[dict]:
    """The settled halves only — every dispatch also writes an `agent_run_start`
    before the await (review C3), so a positional index is meaningless."""
    return [e for e in read_events(journal_path) if e["event"] == "agent_run"]


def test_agent_run_records_context_cost_and_usage(tmp_path):
    result = AgentResult(
        is_error=False,
        status="pr_opened",
        cost_usd=1.25,
        session_id="s5",
        exit_reason=ExitReason.OK,
        usage={
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_read_input_tokens": 30,
            "cache_creation_input_tokens": 40,
            "ignored": "extra",
        },
    )
    rt, journal = _runtime(tmp_path, result)
    rt.set_context("fix", "RESOLVE", 5, 5)
    _leg(rt, model="opus")

    run = _settled(journal.path)[0]
    assert run["event"] == "agent_run"
    assert (run["role"], run["kind"], run["number"], run["issue"]) == ("fix", "RESOLVE", 5, 5)
    assert run["model"] == "opus" and run["cost_usd"] == 1.25
    assert run["status"] == "pr_opened" and run["is_error"] is False
    assert run["exit_reason"] == "ok" and run["session_id"] == "s5"
    assert run["usage"] == {
        "input_tokens": 10,
        "output_tokens": 20,
        "cache_read_input_tokens": 30,
        "cache_creation_input_tokens": 40,
    }
    assert isinstance(run["duration_s"], float)


def test_agent_run_preserves_codex_total_only_usage(tmp_path):
    rt, journal = _runtime(
        tmp_path, AgentResult(is_error=False, status="pr_opened", cost_usd=0.5, usage={"total_tokens": 50_000})
    )

    _leg(rt)

    assert _settled(journal.path)[0]["usage"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "total_tokens": 50_000,
    }


def test_missing_usage_stays_distinct_from_zero_tokens(tmp_path):
    rt, journal = _runtime(tmp_path, AgentResult(is_error=False, usage=None))
    _leg(rt)
    assert _settled(journal.path)[0]["usage"] is None


# ── #43: the journal records the model that RAN ──────────────────────────────
def test_the_model_the_run_reports_wins_over_the_one_the_task_asked_for(tmp_path):
    """An unpinned task carries model=None and the runtime supplies its own
    default; without this the journal recorded nothing for a leg that certainly
    ran on something, and cost could not be split by routed tier."""
    rt, journal = _runtime(tmp_path, AgentResult(is_error=False, cost_usd=1.0, model="gpt-5.6-terra"))
    _leg(rt)
    assert _settled(journal.path)[0]["model"] == "gpt-5.6-terra"


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

    def emit(self, event, fields=None):
        raise OSError("state dir went read-only")


def test_the_dispatch_is_journalled_before_the_await(tmp_path):
    # review C3: every attempt-recording write used to happen strictly AFTER the
    # await, so a session that died mid-run left no trace anywhere — no journal
    # line, no ledger line — while the reaper requeued the issue and the fixer
    # re-dispatched it next poll. The start line is the only durable evidence
    # that this dispatch ever happened.
    inner = SnoopingInner(str(tmp_path / "journal.jsonl"), AgentResult(is_error=False))
    rt, _journal = _runtime(tmp_path, inner)
    rt.set_context("fix", "RESOLVE", 5, 5)

    _leg(rt, model="opus")

    assert [e["event"] for e in inner.seen] == ["agent_run_start"]
    start = inner.seen[0]
    assert (start["role"], start["kind"], start["number"], start["issue"], start["model"]) == (
        "fix",
        "RESOLVE",
        5,
        5,
        "opus",
    )


def test_a_run_that_raises_is_settled_as_a_transport_death_and_re_raises(tmp_path):
    # review C3: the raise IS the crash-isolation contract (Daemon.tick catches,
    # the issue keeps ai-working, the reaper requeues). It must reach the caller
    # untouched — the journal only witnesses it.
    boom = RuntimeError("Stream closed unexpectedly")
    rt, journal = _runtime(tmp_path, RaisingInner(boom))
    rt.set_context("fix", "RESOLVE", 5, 5)

    with pytest.raises(RuntimeError) as raised:
        _leg(rt, model="opus")
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


def test_a_deterministic_death_is_journaled_distinctly_with_the_offending_path(tmp_path):
    # #13: `transport_death` alone cannot distinguish a loop that will never
    # converge from one a retry rescues, and the operator's first question is
    # which file killed it.
    boom = RuntimeError(
        "Fatal error in message reader: Failed to decode JSON: JSON message "
        "exceeded maximum buffer size of 1048576 bytes"
    )
    boom.harness_last_trace = "AssistantMessage → Read(evidence/ac-3-titles.png)"
    rt, journal = _runtime(tmp_path, RaisingInner(boom), role="review")
    rt.set_context("review", "REVIEW", 552, 545)

    with pytest.raises(RuntimeError):
        _leg(rt)

    _, settled = read_events(journal.path)
    assert settled["exit_reason"] == "transport_death"
    assert settled["deterministic"] == "exceeded maximum buffer size"
    assert settled["detail"] == "evidence/ac-3-titles.png"


def test_a_journal_that_cannot_write_never_fails_a_run(tmp_path, capsys):
    # Observability is never a failure mode (module rule 1) — and that has to
    # hold for the two NEW writes as well, on both paths.
    expected = AgentResult(is_error=False, status="done")
    assert _leg(JournalingRuntime(FakeInner(expected), PoisonedJournal())) is expected

    boom = RuntimeError("Stream closed unexpectedly")
    with pytest.raises(RuntimeError) as raised:
        _leg(JournalingRuntime(RaisingInner(boom), PoisonedJournal()))
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


def test_passthrough_does_not_invent_attributes_neither_object_has(tmp_path):
    # Anti-vacuity twin: passthrough must not turn every typo into a live object.
    rt = JournalingRuntime(InnerWithSurface(), Journal(str(tmp_path / "j.jsonl")))
    with pytest.raises(AttributeError):
        rt.no_such_attribute
    with pytest.raises(AttributeError):
        JournalingRuntime.__new__(JournalingRuntime).preflight  # half-built, no inner


# ── CLI status summary ────────────────────────────────────────────────────────
def test_status_summary_is_silent_with_no_spend():
    assert spend_status_lines() == []


def test_status_summary_reports_sqlite_spend_per_repo_and_globally():
    output = "\n".join(spend_status_lines(50.0, "o/r", 100.0, repository_spent=1.25, all_repositories_spent=3.5))

    assert "today, o/r: $1.25 / $50.00 cap" in output
    assert "today, all repos: $3.50 / $100.00 cap" in output


def test_status_summary_without_a_repo_pairs_the_cap_with_the_global_total():
    out = "\n".join(spend_status_lines(50.0, all_repositories_spent=12.5))

    assert "$12.50" in out and "$50.00 cap" in out


@pytest.mark.parametrize("raw,expected", [
    ({"title": "Sandbox lacks libglib", "body": "$ ldd app"}, {"title": "Sandbox lacks libglib", "body": "$ ldd app"}),
    ({"title": "t" * 10_000}, None),  # malformed: not journalled
    (None, None),
])
def test_a_legs_harness_report_is_journalled_on_this_host(tmp_path, raw, expected):
    """A gap issue publishes no agent text; the report stays in the receipt and here."""
    structured = {"status": "pushed", "summary": "s", **({"harness_issue": raw} if raw is not None else {})}
    rt, journal = _runtime(tmp_path, AgentResult(False, status="pushed", structured=structured))
    rt.set_context("fix", "RESOLVE", 5, 5)
    _leg(rt)
    assert _settled(journal.path)[0].get("harness_issue") == expected
