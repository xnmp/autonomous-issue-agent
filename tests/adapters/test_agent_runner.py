"""AgentRunner pure mapping — RuntimePlan→options kwargs and ResultMessage→AgentResult.

No live SDK call: build_option_kwargs and result_to_agent_result are pure, and
result mapping is duck-typed so a stand-in object stands in for ResultMessage.
"""

import asyncio
import dataclasses
from dataclasses import dataclass
import subprocess
import sys
import types
import typing
from pathlib import Path

import pytest

from issue_agent.adapters.agent_runner import (
    DEFAULT_MAX_BUFFER_BYTES,
    AgentRunner,
    _trace_message,
    build_option_kwargs,
    merge_usage,
    resolve_claude_cli,
    result_to_agent_result,
)
from issue_agent.adapters.runtime_plan import RuntimePlan
from issue_agent.domain.effort import EFFORT_LADDER, Effort
from issue_agent.domain.outcome import ExitReason


def _run(**runner):
    """One leg through a runner built with ``runner`` options."""
    return asyncio.run(AgentRunner(**runner).run(RuntimePlan(prompt="x")))


def test_a_plan_maps_onto_the_sdk_options():
    schema = {"type": "object"}
    kw = build_option_kwargs(
        RuntimePlan(
            prompt="x",
            allowed_tools=("Read", "Bash(git *)"),
            disallowed_tools=("Edit", "Write"),
            model="opus",
            max_budget_usd=1.5,
            cwd="/wt/issue-5",
            resume="sess-123",
            schema=schema,
            append_system_prompt="rules",
            effort=Effort.HIGH,
        )
    )
    assert kw["allowed_tools"] == ["Read", "Bash(git *)"]
    assert kw["disallowed_tools"] == ["Edit", "Write"]
    assert (kw["model"], kw["max_budget_usd"], kw["cwd"], kw["resume"]) == ("opus", 1.5, "/wt/issue-5", "sess-123")
    assert "fork_session" not in kw
    # Target project settings are never loaded as provider authority.
    assert kw["setting_sources"] == []
    assert kw["output_format"] == {"type": "json_schema", "schema": schema}
    assert kw["system_prompt"] == {"type": "preset", "preset": "claude_code", "append": "rules"}
    assert kw["effort"] == "high"  # the escalation ladder's word (#16)
    # commit attribution: git author = "Harness - <model>"
    assert kw["env"]["GIT_AUTHOR_NAME"] == kw["env"]["GIT_COMMITTER_NAME"] == "Harness - opus"


def test_task_env_overrides_the_model_git_identity():
    # fix.py puts the fix-bot identity in task.env; were the model identity to
    # win, derive would read the agent's push as a human reset (attempt caps).
    kw = build_option_kwargs(RuntimePlan(prompt="p", model="m", env={"GIT_AUTHOR_NAME": "me"}))
    assert (kw["env"]["GIT_AUTHOR_NAME"], kw["env"]["GIT_COMMITTER_NAME"]) == ("me", "Harness - m")


# ── message buffer ceiling (#13) ──────────────────────────────────────────────
def test_default_buffer_survives_a_five_megabyte_read():
    # The SDK's own default is 1MB and a tool result is base64'd (x4/3), so the
    # bound has to clear ~6.7MB for a 5MB file to come back alive.
    assert DEFAULT_MAX_BUFFER_BYTES >= 5 * 1024 * 1024 * 4 // 3


def test_a_runner_applies_the_default_buffer_to_every_session(monkeypatch):
    seen = {}
    _install_fake_sdk(monkeypatch, [_ResultMessage(is_error=False)], capture=seen)
    _run()
    assert seen["options"]["max_buffer_size"] == DEFAULT_MAX_BUFFER_BYTES


def test_the_installed_sdk_accepts_the_options_we_pass():
    """Anti-vacuity: every other test stubs the SDK, so nothing else would notice
    the pinned build dropping max_buffer_size (the fix becoming a no-op) or
    renaming/narrowing `effort`. Skipped where the SDK is absent."""
    sdk = pytest.importorskip("claude_agent_sdk")
    fields = {f.name: f for f in dataclasses.fields(sdk.ClaudeAgentOptions)}
    assert "max_buffer_size" in fields
    accepted = set()
    for arg in typing.get_args(fields["effort"].type):
        accepted.update(typing.get_args(arg))
    assert {e.value for e in EFFORT_LADDER} <= accepted


def test_bundled_claude_cli_supports_configured_opus_55(tmp_path):
    sdk = pytest.importorskip("claude_agent_sdk")
    # SDK releases can lag the CLI required by a configured model. Check the
    # executable we actually launch, without making a paid provider request.
    executable = Path(sdk.__file__).parent / "_bundled" / "claude"
    completed = subprocess.run(
        [str(executable), "--version"], cwd=tmp_path, capture_output=True, text=True, check=True, timeout=10
    )
    version = tuple(int(part) for part in completed.stdout.split()[0].split("."))
    assert version >= (2, 1, 280), completed.stdout


# ── ResultMessage → AgentResult ────────────────────────────────────────────────
@dataclass
class FakeResult:
    is_error: bool = False
    structured_output: object = None
    result: str = ""
    total_cost_usd: float = 0.0
    session_id: str = ""
    subtype: str = ""
    usage: object = None


def test_result_reads_structured_output():
    r = result_to_agent_result(
        FakeResult(
            structured_output={"status": "pr_opened", "summary": "opened", "needs": ""},
            total_cost_usd=0.42,
            session_id="s9",
        )
    )
    assert (r.is_error, r.status, r.summary, r.cost_usd, r.session_id) == (False, "pr_opened", "opened", 0.42, "s9")


@pytest.mark.parametrize(
    "fake,status,reason",
    [
        (FakeResult(is_error=True), "error", ExitReason.AGENT_ERROR),
        # The review schema names its terminal `decision`, not `status`, so a clean
        # review used to journal status="" and the verdict lived only on the PR.
        (
            FakeResult(subtype="success", structured_output={"decision": "APPROVE", "body": "LGTM"}),
            "APPROVE",
            ExitReason.OK,
        ),
        # A malformed envelope must not put a non-str into the str-typed field; on an
        # errored run the subtype fallback still fills it.
        (FakeResult(structured_output={"status": 7, "decision": None}), "", ExitReason.OK),
        (
            FakeResult(is_error=True, subtype="error_during_execution", structured_output={"decision": ["APPROVE"]}),
            "error_during_execution",
            ExitReason.AGENT_ERROR,
        ),
        # A spend ceiling is an operator-knob outcome, not a failure of the agent's reasoning.
        (
            FakeResult(is_error=True, subtype="error_max_budget_usd"),
            "error_max_budget_usd",
            ExitReason.BUDGET_EXHAUSTED,
        ),
        (FakeResult(is_error=True, subtype="error_max_turns"), "error_max_turns", ExitReason.AGENT_ERROR),
        # is_error false on the envelope but status=="error" inside it is the agent reporting failure.
        (FakeResult(is_error=False, structured_output={"status": "error"}), "error", ExitReason.AGENT_ERROR),
    ],
)
def test_result_envelope_classification(fake, status, reason):
    r = result_to_agent_result(fake)
    assert (r.status, r.exit_reason, r.is_error) == (status, reason, reason is not ExitReason.OK)


def test_usage_block_is_captured_verbatim_for_the_cost_ledger():
    # cost_usd is 0.00 on a subscription plan, so tokens (incl. the cache counters
    # nothing else exposes) are the only cost substrate that always exists.
    usage = {"input_tokens": 12, "output_tokens": 3, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 40}
    assert result_to_agent_result(FakeResult(subtype="success", total_cost_usd=0.0, usage=usage)).usage == usage


# ── AgentRunner.run stream handling ─────────────────────────────────────────────
# The SDK raises the CLI's trailing ProcessError when Claude Code exits non-zero
# AFTER delivering its ResultMessage (e.g. a benign post-`gh pr create` exit, or
# an is_error result). We read the ENVELOPE, never the exit code, so a result we
# already captured must survive a post-result raise. Only a crash with NO result
# is a genuine transport failure.


class _ResultMessage(FakeResult):
    """Stand-in recognised by run() via the isinstance(message, ResultMessage)
    check — we monkeypatch the SDK symbols run() lazily imports."""


def _install_sdk(monkeypatch, fake_query, capture=None):
    # run() does `from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query`
    # at call time, so injecting a fake module into sys.modules is the whole
    # mechanism — there is no import cache on the runner to reset.
    def _options(**kw):
        if capture is not None:
            capture["options"] = kw
        return object()

    fake = types.ModuleType("claude_agent_sdk")
    fake.ClaudeAgentOptions = _options
    fake.ResultMessage = _ResultMessage
    fake.query = fake_query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)


def _install_fake_sdk(monkeypatch, messages, raise_after=False, capture=None):
    async def fake_query(prompt, options):
        for m in messages:
            yield m
        if raise_after:
            raise RuntimeError("Claude Code returned an error result: success")

    _install_sdk(monkeypatch, fake_query, capture)


def test_run_honours_result_despite_post_result_raise(monkeypatch):
    msg = _ResultMessage(structured_output={"status": "pr_opened", "summary": "opened"}, session_id="s1")
    _install_fake_sdk(monkeypatch, [msg], raise_after=True)
    r = _run()
    # The post-result ProcessError must NOT clobber the captured envelope.
    assert r.status == "pr_opened"
    assert r.session_id == "s1"
    assert r.is_error is False


def test_unpinned_sdk_no_result_journals_the_init_models_resolved_value(monkeypatch, tmp_path):
    """A dropped terminal envelope retains the SDK model in its settled receipt."""
    from issue_agent.adapters.journal import JournalingRuntime
    from tests.fakes.legacy_journal import Journal, read_events

    init = type("InitMessage", (), {"subtype": "init", "data": {"model": "claude-opus-4-1"}})()
    _install_fake_sdk(monkeypatch, [init])
    journal = Journal(str(tmp_path / "review-journal.jsonl"))
    runner = JournalingRuntime(AgentRunner(), journal, role="review")
    runner.set_context("review", "REVIEW", 100, 91)

    asyncio.run(runner.run(RuntimePlan(prompt="unconfigured SDK leg")))

    start, settled = read_events(journal.path)
    assert start["model"] == ""
    assert settled["exit_reason"] == "no_result"
    assert settled["model"] == "claude-opus-4-1"


def test_run_skips_task_notification_followup_result(monkeypatch):
    """A background task finishing injects a synthetic follow-up whose ResultMessage
    carries origin={"kind":"task-notification"}. It must NOT be taken as our result —
    the runner keeps the ResultMessage that answers our prompt. Here the follow-up
    (a bare/empty result) arrives BEFORE the real verdict; the runner must skip the
    former and return the latter, not clobber it."""
    followup = _ResultMessage(is_error=False, structured_output={"status": "ignored"})
    followup.origin = {"kind": "task-notification"}
    real = _ResultMessage(structured_output={"status": "pr_opened", "summary": "ok"}, session_id="real")
    _install_fake_sdk(monkeypatch, [followup, real])
    r = _run()
    assert r.status == "pr_opened"
    assert r.session_id == "real"


def test_run_reraises_when_crash_before_any_result(monkeypatch):
    _install_fake_sdk(monkeypatch, [], raise_after=True)
    with pytest.raises(RuntimeError):
        _run()


def test_transport_death_is_logged_with_type_and_last_message(monkeypatch):
    """The wedge we couldn't diagnose: a crash before any ResultMessage now logs
    the exception type AND the last SDK message seen (the tool that preceded it),
    always — not gated on debug — so the fix log alone explains a Stream closed."""
    _Msg = type("Blk", (), {})  # noqa: N806
    blk = _Msg()
    blk.name = "Bash"
    blk.input = {"command": "uv run pytest"}
    carrier = type("Carrier", (), {})()
    carrier.content = [blk]
    _install_fake_sdk(monkeypatch, [carrier], raise_after=True)  # raise, no result
    logged: list[str] = []
    with pytest.raises(RuntimeError):
        _run(log=logged.append, debug=True)
    joined = "\n".join(logged)
    assert "transport died before any ResultMessage" in joined
    assert "RuntimeError" in joined
    assert "Bash(uv run pytest)" in joined  # last-seen tool surfaced


# ── idle watchdog: hang detection without false-positives on hard tasks ───────
def _install_hanging_sdk(monkeypatch, messages, hang_after=True):
    """Fake query that yields `messages` fast, then (hang_after) blocks forever —
    the silent-hang shape (no more events, never returns/raises)."""

    async def fake_query(prompt, options):
        for m in messages:
            yield m
        if hang_after:
            await asyncio.Event().wait()  # never set → hang

    _install_sdk(monkeypatch, fake_query)


def test_idle_timeout_trips_on_hang(monkeypatch):
    """A stream that goes silent (no event) past idle_timeout → timeout result,
    not a permanent wedge. This is the #56 daemon-freeze made recoverable."""
    _install_hanging_sdk(monkeypatch, [], hang_after=True)
    logged: list[str] = []
    r = _run(log=logged.append, idle_timeout=0.2)
    assert r.is_error and r.status == "timeout"
    assert r.exit_reason is ExitReason.IDLE_TIMEOUT
    assert any("IDLE" in m for m in logged)


def test_dispatch_cancellation_joins_pending_watchdog_receive(monkeypatch):
    closed = asyncio.Event()

    async def fake_query(prompt, options):
        try:
            await asyncio.Event().wait()
            yield object()
        finally:
            closed.set()

    _install_sdk(monkeypatch, fake_query)

    async def scenario():
        task = asyncio.create_task(AgentRunner(idle_timeout=60.0, poll_slice=0.01).run(RuntimePlan(prompt="x")))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(scenario())


def test_clean_end_without_result_classifies_as_no_result(monkeypatch):
    """The result-delivery drop: the stream ends via StopAsyncIteration having never
    yielded a ResultMessage. It is an error, but a DIFFERENT one from the agent
    reporting failure — the taxonomy has to say which."""
    _install_fake_sdk(monkeypatch, [])  # ends cleanly, no result, no raise
    r = _run()
    assert r.is_error is True
    assert r.exit_reason is ExitReason.NO_RESULT


def test_idle_watchdog_trips_on_wall_clock_not_monotonic(monkeypatch):
    """The post-sleep wedge fix (Myapp #345). macOS freezes the monotonic clock during
    system sleep, so a one-shot monotonic wait_for spanning a suspend never counts the
    suspended time. Here the stream hangs and the injected WALL clock jumps forward by
    far more than idle_timeout on the first slice (simulating the laptop sleeping),
    while REAL elapsed time stays tiny. The watchdog must trip on the wall-clock gap —
    proving recovery is prompt after wake, not delayed by a fresh idle window."""
    _install_hanging_sdk(monkeypatch, [], hang_after=True)
    # Wall clock: starts at 1000, then jumps +3600s (a 1h "sleep") on the next read.
    wall = _jumping_wall([1000.0, 4600.0])

    logged: list[str] = []
    # idle_timeout large (900) but poll_slice tiny so real slices are fast; the trip
    # must come from the wall-clock jump, not from 900s of real waiting.
    r = _run(log=logged.append, idle_timeout=900.0, poll_slice=0.05, wall_clock=wall)
    assert r.is_error and r.status == "timeout"
    assert any("IDLE" in m for m in logged)


def test_gap_longer_than_poll_slice_still_delivers_result(monkeypatch):
    """Regression: the persistent reviewer $0.00 "no ResultMessage" drop.

    When the model pauses LONGER than one poll_slice between messages (routine for
    a reviewer thinking over a large diff), the old code — wait_for(__anext__(),
    slice) — cancelled the in-flight receive on every slice timeout, closing the
    SDK stream so the next __anext__() returned StopAsyncIteration and the run
    ended with NO ResultMessage. The watchdog must instead keep observing the SAME
    receive across slices: a gap > poll_slice but < idle_timeout must NOT drop the
    stream, and the terminal ResultMessage must still arrive."""

    async def fake_query(prompt, options):
        yield object()  # early event
        await asyncio.sleep(0.15)  # gap > poll_slice (0.05), < idle_timeout (5.0)
        yield object()  # event after a multi-slice pause
        await asyncio.sleep(0.15)  # another long-ish pause before the result
        yield _ResultMessage(structured_output={"status": "pr_opened"}, session_id="s")

    _install_sdk(monkeypatch, fake_query)

    logged: list[str] = []
    r = _run(log=logged.append, idle_timeout=5.0, poll_slice=0.05)
    # The result survives the > poll_slice gaps; no idle trip, no drop.
    assert r.is_error is False and r.status == "pr_opened"
    assert not any("IDLE" in m for m in logged)
    assert not any("NO ResultMessage" in m for m in logged)


# ── total-runtime (wall) cap: the bound the idle watchdog structurally can't give ─
def _jumping_wall(ticks):
    """Injected wall clock that walks `ticks`, then holds the last value."""
    remaining, held = iter(ticks), [ticks[0]]

    def wall():
        held[0] = next(remaining, held[0])
        return held[0]

    return wall


def test_wall_cap_trips_when_idle_timeout_is_disabled(monkeypatch):
    """With only the wall cap armed there is no idle watchdog to borrow the slice
    loop from — the runner must still observe in slices rather than falling through
    to an unbounded `await __anext__()`, or the cap would silently never fire."""
    _install_hanging_sdk(monkeypatch, [], hang_after=True)
    # started_at=1000, first between-receive check 1000 (0s elapsed), last_event
    # 1000, then the slice expires with the clock 3600s later (a suspend).
    wall = _jumping_wall([1000.0, 1000.0, 1000.0, 4600.0])
    logged: list[str] = []

    r = _run(log=logged.append, idle_timeout=None, max_runtime=900.0, poll_slice=0.05, wall_clock=wall)

    assert r.is_error and r.status == "timeout"
    assert r.exit_reason is ExitReason.WALL_TIMEOUT
    assert "900" in r.summary
    assert any("WALL" in m for m in logged)


def test_wall_cap_trips_on_a_limping_stream_the_idle_watchdog_never_catches(monkeypatch):
    """FAILURE-MODES #60. A post-suspend stream keeps delivering events, so every
    idle window is reset and the idle watchdog is structurally blind to it — the run
    burned 58 minutes on 3.6s of CPU. Here events arrive faster than a poll slice
    (so no slice ever expires) while the wall clock advances past the cap: only a
    between-receives elapsed check can stop this."""

    async def fake_query(prompt, options):
        while True:
            yield object()  # always ready → asyncio.wait returns before any timeout

    _install_sdk(monkeypatch, fake_query)

    wall = _jumping_wall([1000.0, 1000.0, 1000.0, 4600.0])
    logged: list[str] = []

    r = _run(log=logged.append, idle_timeout=900.0, max_runtime=900.0, wall_clock=wall)

    assert r.exit_reason is ExitReason.WALL_TIMEOUT
    assert not any("IDLE" in m for m in logged)  # the idle watchdog never saw it


def test_wall_cap_honours_a_result_already_captured(monkeypatch):
    """The envelope always wins over a watchdog. If the agent delivered its verdict
    and only THEN the cap expires, the run must return that verdict — aborting it
    into a timeout would discard finished, paid-for work."""
    msg = _ResultMessage(
        subtype="success", structured_output={"status": "pr_opened", "summary": "opened"}, session_id="s1"
    )
    _install_fake_sdk(monkeypatch, [msg])

    r = _run(max_runtime=900.0, wall_clock=_jumping_wall([1000.0, 1000.0, 1000.0, 4600.0]))

    assert r.status == "pr_opened"
    assert r.is_error is False
    assert r.exit_reason is ExitReason.OK


def test_healthy_short_run_is_not_capped(monkeypatch):
    """False-positive guard: a run well inside the cap completes normally, and the
    cap leaves no trace on the result."""
    _install_fake_sdk(monkeypatch, [_ResultMessage(subtype="success", structured_output={"status": "pr_opened"})])
    logged: list[str] = []

    r = _run(log=logged.append, idle_timeout=900.0, max_runtime=2700.0)

    assert r.status == "pr_opened" and r.is_error is False
    assert not any("WALL" in m or "IDLE" in m for m in logged)


# ── debug tracing: make a mid-session "Stream closed" wedge diagnosable ────────


@dataclass
class _ToolUseBlock:
    name: str
    input: dict


@dataclass
class _Msg:
    content: list


def test_trace_message_names_the_tool_and_its_first_argument_line():
    t = _trace_message(_Msg(content=[_ToolUseBlock("Bash", {"command": "pytest -q\nsecond line"})]))
    assert "_Msg" in t and "Bash(pytest -q)" in t  # first line only, name shown
    assert "Read(/a/b/c.py)" in _trace_message(_Msg(content=[_ToolUseBlock("Read", {"file_path": "/a/b/c.py"})]))
    assert _trace_message(object()) != ""  # garbage returns the type name, no crash


# ── running token total, so an aborted run knows what it spent ───────────────
# Cost only arrives on the final ResultMessage, so a wall-cap / idle / dropped-
# result abort reported usage=None and ledgered $0.00 however long it ran (first
# real deployment: 906s, four commits, $0.00). AssistantMessage.usage is the API
# call's OWN per-turn block, so summing across turns gives billed totals.
def test_merge_usage_sums_numeric_per_turn_blocks_and_keeps_absent_distinct():
    acc = merge_usage(None, {"input_tokens": 10, "output_tokens": 2})
    assert merge_usage(acc, {"input_tokens": 30, "output_tokens": 5}) == {"input_tokens": 40, "output_tokens": 7}
    assert merge_usage(None, {"input_tokens": 5, "cached": True, "model": "opus"}) == {"input_tokens": 5}
    assert merge_usage(None, None) is None  # nothing ever reported
    assert merge_usage(None, {}) == {}  # reported, and it was empty
    assert merge_usage(None, "not-a-dict") is None


def test_wall_timeout_result_carries_the_tokens_spent_before_the_abort():
    """The regression: this result used to be built with no usage at all, so the
    journal had nothing to price and the run ledgered $0.00."""
    res = AgentRunner(max_runtime=900)._wall_timeout(906.0, "Bash(pytest)", {"output_tokens": 22349})

    assert res.exit_reason is ExitReason.WALL_TIMEOUT
    assert res.usage == {"output_tokens": 22349}


# ── CLI resolution (keifu 2026-08-03: venv rebuilt under a live daemon) ───────
def test_claude_cli_resolution_prefers_the_pin_then_the_bundled_cli_then_path(tmp_path):
    bundled = tmp_path / "claude"
    bundled.write_text("#!/bin/sh\n")
    bundled.chmod(0o755)
    assert resolve_claude_cli(" /pinned/claude ", bundled, which=lambda _: "/path/claude") == "/pinned/claude"
    assert resolve_claude_cli("", bundled) == str(bundled)
    # The stale-venv shape: the bundled path no longer exists on disk.
    gone = tmp_path / "deleted-venv" / "claude"
    assert resolve_claude_cli("", gone, which=lambda _: "/local/claude") == "/local/claude"
    assert resolve_claude_cli("", tmp_path / "missing", which=lambda _: None) is None


# ── session id from the init message (#340): every end can be resumed ───────
def _init(data):
    """The SDK's generic init SystemMessage: the session id lives in `data`."""
    return type("SystemMessage", (), {"subtype": "init", "data": data})()


_WALL_TRIP = {"idle_timeout": None, "max_runtime": 900.0, "poll_slice": 0.05}


def test_a_wall_capped_stream_carries_the_session_its_init_named(monkeypatch):
    _install_hanging_sdk(monkeypatch, [_init({"session_id": "s-init", "model": "claude-opus-5-5"})])

    r = _run(**_WALL_TRIP, wall_clock=_jumping_wall([1000.0, 1000.0, 1000.0, 1000.0, 4600.0]))

    assert r.exit_reason is ExitReason.WALL_TIMEOUT
    assert (r.session_id, r.model) == ("s-init", "claude-opus-5-5")


def test_an_idle_or_resultless_end_carries_the_init_session(monkeypatch):
    _install_hanging_sdk(monkeypatch, [_init({"session_id": "s-idle"})])
    assert _run(idle_timeout=0.2, poll_slice=0.05).session_id == "s-idle"

    _install_fake_sdk(monkeypatch, [_init({"session_id": "s-drop"})])
    r = _run()
    assert (r.exit_reason, r.session_id) == (ExitReason.NO_RESULT, "s-drop")


@pytest.mark.parametrize("data", [
    None, "s-not-a-dict", {}, {"session_id": None}, {"session_id": 123}, {"session_id": "   "},
    {"session_id": "s" * 257}, {"session_id": ["s1"]},
])
def test_a_malformed_or_absent_init_leaves_a_timeout_with_no_session(monkeypatch, data):
    _install_hanging_sdk(monkeypatch, [_init(data)])

    r = _run(idle_timeout=0.2, poll_slice=0.05)

    assert r.exit_reason is ExitReason.IDLE_TIMEOUT
    assert r.session_id == ""


def test_a_stream_without_init_uses_the_first_message_that_names_a_session(monkeypatch):
    first = type("AssistantMessage", (), {"session_id": "s-main", "content": []})()
    later = type("AssistantMessage", (), {"session_id": "s-later", "content": []})()
    _install_hanging_sdk(monkeypatch, [_init({"model": "m"}), first, later])

    assert _run(idle_timeout=0.2, poll_slice=0.05).session_id == "s-main"


def test_the_result_envelope_session_wins_and_a_bare_one_falls_back_to_init(monkeypatch):
    _install_fake_sdk(monkeypatch, [_init({"session_id": "s-init"}), _ResultMessage(is_error=False, session_id="s-end")])
    assert _run().session_id == "s-end"

    _install_fake_sdk(monkeypatch, [_init({"session_id": "s-init"}), _ResultMessage(is_error=False, session_id=None)])
    assert _run().session_id == "s-init"


def test_a_resumed_leg_that_hangs_before_any_message_keeps_the_resumed_session(monkeypatch):
    _install_hanging_sdk(monkeypatch, [])

    r = asyncio.run(AgentRunner(idle_timeout=0.2, poll_slice=0.05).run(RuntimePlan(prompt="x", resume="s-prior")))

    assert (r.exit_reason, r.session_id) == (ExitReason.IDLE_TIMEOUT, "s-prior")


def test_a_transport_death_after_init_journals_the_session(monkeypatch):
    from issue_agent.adapters.journal import JournalingRuntime

    class Sink:
        canonical, path = True, ""

        def __init__(self):
            self.events = []

        def emit(self, event, fields):
            self.events.append((event, dict(fields)))

    _install_fake_sdk(monkeypatch, [_init({"session_id": "s-died"})], raise_after=True)
    sink = Sink()
    runtime = JournalingRuntime(AgentRunner(), sink, repo="o/r", role="fix")
    runtime.set_context("fix", "RESOLVE", 5, 5)

    with pytest.raises(RuntimeError):
        asyncio.run(runtime.run(RuntimePlan(prompt="x")))

    settled = [f for e, f in sink.events if e == "agent_run"]
    assert [(f["exit_reason"], f["session_id"]) for f in settled] == [("transport_death", "s-died")]
