"""AgentRunner pure mapping — RuntimePlan→options kwargs and ResultMessage→AgentResult.

No live SDK call: build_option_kwargs and result_to_agent_result are pure, and
result mapping is duck-typed so a stand-in object stands in for ResultMessage.
"""

import asyncio
from dataclasses import dataclass

from issue_agent.adapters import agent_runner
from issue_agent.adapters.agent_runner import (
    DEFAULT_MAX_BUFFER_BYTES,
    DEFAULT_PERMISSION_MODE,
    build_option_kwargs,
    resolve_claude_cli,
    result_to_agent_result,
)
from issue_agent.adapters.runtime_plan import RuntimePlan
from issue_agent.domain.outcome import ExitReason


def test_default_permission_mode_is_bypass():
    assert DEFAULT_PERMISSION_MODE == "bypassPermissions"


def test_schema_maps_to_output_format():
    schema = {"type": "object"}
    kw = build_option_kwargs(RuntimePlan(prompt="x", schema=schema))
    assert kw["output_format"] == {"type": "json_schema", "schema": schema}


def test_append_system_prompt_uses_claude_code_preset():
    kw = build_option_kwargs(RuntimePlan(prompt="x", append_system_prompt="rules"))
    assert kw["system_prompt"] == {
        "type": "preset",
        "preset": "claude_code",
        "append": "rules",
    }


def test_no_append_still_uses_preset():
    # The SDK defaults to a MINIMAL prompt; we must opt into the claude_code preset.
    kw = build_option_kwargs(RuntimePlan(prompt="x"))
    assert kw["system_prompt"] == {"type": "preset", "preset": "claude_code"}


def test_no_agents_key_and_no_task_tool():
    kw = build_option_kwargs(RuntimePlan(prompt="x", allowed_tools=("Read",)))
    assert "agents" not in kw
    assert "Task" not in kw.get("allowed_tools", [])


def test_tools_model_budget_cwd_resume_map():
    task = RuntimePlan(
        prompt="x",
        allowed_tools=("Read", "Bash(git *)"),
        disallowed_tools=("Edit", "Write"),
        model="opus",
        max_budget_usd=1.5,
        cwd="/wt/issue-5",
        resume="sess-123",
    )
    kw = build_option_kwargs(task)
    assert kw["allowed_tools"] == ["Read", "Bash(git *)"]
    assert kw["disallowed_tools"] == ["Edit", "Write"]
    assert kw["model"] == "opus"
    assert kw["max_budget_usd"] == 1.5
    assert kw["cwd"] == "/wt/issue-5"
    assert kw["resume"] == "sess-123"
    assert "fork_session" not in kw
    assert kw["setting_sources"] == []


def test_target_project_settings_are_never_loaded_as_provider_authority():
    kw = build_option_kwargs(RuntimePlan(prompt="x", cwd="/target"))
    assert kw["setting_sources"] == []


def test_permission_mode_override():
    kw = build_option_kwargs(RuntimePlan(prompt="x"), permission_mode="auto")
    assert kw["permission_mode"] == "auto"


# ── message buffer ceiling (#13) ──────────────────────────────────────────────
def test_default_buffer_survives_a_five_megabyte_read():
    # The SDK's own default is 1MB and a tool result is base64'd (x4/3), so the
    # bound has to clear ~6.7MB for a 5MB file to come back alive.
    assert DEFAULT_MAX_BUFFER_BYTES >= 5 * 1024 * 1024 * 4 // 3


def test_buffer_knob_reaches_the_options():
    kw = build_option_kwargs(RuntimePlan(prompt="x"), max_buffer_size=4096)
    assert kw["max_buffer_size"] == 4096


def test_no_buffer_knob_leaves_the_sdk_default_alone():
    kw = build_option_kwargs(RuntimePlan(prompt="x"))
    assert "max_buffer_size" not in kw
    assert "max_buffer_size" not in build_option_kwargs(RuntimePlan(prompt="x"), max_buffer_size=0)


def test_a_runner_applies_the_default_buffer_to_every_session(monkeypatch):
    seen = {}
    _install_fake_sdk(monkeypatch, [_ResultMessage(is_error=False)], capture=seen)
    asyncio.run(AgentRunner().run(RuntimePlan(prompt="x")))
    assert seen["options"]["max_buffer_size"] == DEFAULT_MAX_BUFFER_BYTES


def test_the_installed_sdk_accepts_max_buffer_size():
    """Anti-vacuity: every test above stubs the SDK, so nothing else would notice
    the pinned build dropping the field and the fix becoming a no-op. Skipped
    where the SDK is absent — `make test-adapters` runs without it by design."""
    import dataclasses

    import pytest

    sdk = pytest.importorskip("claude_agent_sdk")
    assert "max_buffer_size" in {
        f.name for f in dataclasses.fields(sdk.ClaudeAgentOptions)
    }


def test_bundled_claude_cli_supports_configured_opus_55(tmp_path):
    import subprocess
    from pathlib import Path

    import pytest

    sdk = pytest.importorskip("claude_agent_sdk")
    # SDK releases can lag the CLI required by a configured model. Check the
    # executable we actually launch, without making a paid provider request.
    executable = Path(sdk.__file__).parent / "_bundled" / "claude"
    completed = subprocess.run(
        [str(executable), "--version"], cwd=tmp_path, capture_output=True,
        text=True, check=True, timeout=10,
    )
    version = tuple(int(part) for part in completed.stdout.split()[0].split("."))
    assert version >= (2, 1, 280), completed.stdout


# ── reasoning effort (#16) ────────────────────────────────────────────────────
def test_effort_reaches_the_options_as_the_sdk_word():
    from issue_agent.domain.effort import Effort

    kw = build_option_kwargs(RuntimePlan(prompt="x", effort=Effort.HIGH))
    assert kw["effort"] == "high"


def test_a_task_with_no_effort_says_nothing():
    """Adding the field must not pin every existing dispatch to the harness's
    idea of an effort — a task with no opinion keeps the SDK's own default."""
    assert "effort" not in build_option_kwargs(RuntimePlan(prompt="x"))


def test_effort_survives_to_the_session_on_a_build_that_has_the_field(monkeypatch):
    from issue_agent.domain.effort import Effort

    seen = {}
    _install_fake_sdk(monkeypatch, [_ResultMessage(is_error=False)], capture=seen)
    asyncio.run(AgentRunner().run(RuntimePlan(prompt="x", effort=Effort.LOW)))
    assert seen["options"]["effort"] == "low"


def test_the_installed_sdk_accepts_every_effort_word_the_domain_can_emit():
    """Anti-vacuity twin of the max_buffer_size check: the stubs above would not
    notice the SDK renaming the option or narrowing its Literal."""
    import dataclasses
    import typing

    import pytest

    from issue_agent.domain.effort import EFFORT_LADDER

    sdk = pytest.importorskip("claude_agent_sdk")
    field = {f.name: f for f in dataclasses.fields(sdk.ClaudeAgentOptions)}["effort"]
    accepted = set()
    for arg in typing.get_args(field.type):
        accepted.update(typing.get_args(arg))
    assert {e.value for e in EFFORT_LADDER} <= accepted


def test_a_transport_death_carries_the_last_trace_on_the_exception(monkeypatch):
    # The journal and the dispatcher both need the offending path, and the
    # exception must still propagate unchanged (crash isolation).
    blk = type("Blk", (), {})()
    blk.name = "Read"
    blk.input = {"file_path": "evidence/shot.png"}
    carrier = type("Carrier", (), {})()
    carrier.content = [blk]
    _install_fake_sdk(monkeypatch, [carrier], raise_after=True)
    try:
        asyncio.run(AgentRunner().run(RuntimePlan(prompt="x")))
    except RuntimeError as exc:
        assert "Read(evidence/shot.png)" in getattr(exc, "harness_last_trace", "")
    else:
        raise AssertionError("expected the pre-result crash to propagate")


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
    msg = FakeResult(
        structured_output={"status": "pr_opened", "summary": "opened", "needs": ""},
        total_cost_usd=0.42,
        session_id="s9",
    )
    r = result_to_agent_result(msg)
    assert r.is_error is False
    assert r.status == "pr_opened"
    assert r.summary == "opened"
    assert r.cost_usd == 0.42
    assert r.session_id == "s9"


def test_result_blocked_carries_needs():
    msg = FakeResult(structured_output={"status": "blocked", "needs": "which endpoint?"})
    r = result_to_agent_result(msg)
    assert r.status == "blocked"
    assert r.needs == "which endpoint?"


def test_result_error_envelope():
    r = result_to_agent_result(FakeResult(is_error=True))
    assert r.is_error is True


def test_review_decision_becomes_the_run_status():
    # The review schema names its terminal `decision`, not `status`, so a clean
    # review used to journal status="" and the verdict lived only on the PR.
    r = result_to_agent_result(
        FakeResult(subtype="success",
                   structured_output={"decision": "APPROVE", "body": "LGTM"})
    )
    assert r.status == "APPROVE"
    assert r.is_error is False
    assert r.exit_reason is ExitReason.OK


def test_explicit_status_wins_over_decision():
    r = result_to_agent_result(
        FakeResult(structured_output={"status": "pr_opened", "decision": "APPROVE"})
    )
    assert r.status == "pr_opened"


def test_errored_review_still_reports_the_envelope_error_kind():
    # No terminal of either name → the SDK's own error subtype, unchanged.
    r = result_to_agent_result(
        FakeResult(is_error=True, subtype="error_max_turns", structured_output={"body": ""})
    )
    assert r.status == "error_max_turns"


def test_non_string_terminal_is_ignored_rather_than_propagated():
    # A malformed envelope must not put a non-str into the str-typed field; on an
    # errored run the subtype fallback still fills it.
    ok = result_to_agent_result(FakeResult(structured_output={"status": 7, "decision": None}))
    bad = result_to_agent_result(
        FakeResult(is_error=True, subtype="error_during_execution",
                   structured_output={"decision": ["APPROVE"]})
    )
    assert ok.status == ""
    assert bad.status == "error_during_execution"


def test_result_falls_back_to_result_text_when_no_structured():
    msg = FakeResult(structured_output=None, result="free text")
    r = result_to_agent_result(msg)
    assert r.summary == "free text"


# ── exit_reason: WHY the session ended, read off the envelope ─────────────────
def test_successful_envelope_classifies_ok():
    r = result_to_agent_result(
        FakeResult(is_error=False, subtype="success",
                   structured_output={"status": "pr_opened"})
    )
    assert r.exit_reason is ExitReason.OK


def test_budget_subtype_is_distinguished_from_a_plain_agent_error():
    # A spend ceiling is an operator-knob outcome, not a failure of the agent's
    # reasoning — the ledger and any future policy must be able to tell them apart.
    budget = result_to_agent_result(
        FakeResult(is_error=True, subtype="error_max_budget_usd")
    )
    turns = result_to_agent_result(FakeResult(is_error=True, subtype="error_max_turns"))
    assert budget.exit_reason is ExitReason.BUDGET_EXHAUSTED
    assert turns.exit_reason is ExitReason.AGENT_ERROR


def test_structured_error_status_classifies_as_agent_error():
    # is_error false on the envelope but status=="error" inside it still counts as
    # the agent reporting failure (the existing is_error coercion).
    r = result_to_agent_result(
        FakeResult(is_error=False, structured_output={"status": "error"})
    )
    assert r.is_error is True
    assert r.exit_reason is ExitReason.AGENT_ERROR


def test_non_error_envelope_without_subtype_is_ok():
    # An SDK build that omits `subtype` must not be classified as a failure.
    r = result_to_agent_result(FakeResult(is_error=False, structured_output={"status": "blocked"}))
    assert r.exit_reason is ExitReason.OK


def test_usage_block_is_captured_verbatim_for_the_cost_ledger():
    # cost_usd is 0.00 on a subscription plan, so tokens (incl. the cache counters
    # nothing else exposes) are the only cost substrate that always exists.
    usage = {"input_tokens": 12, "output_tokens": 3,
             "cache_read_input_tokens": 900, "cache_creation_input_tokens": 40}
    r = result_to_agent_result(
        FakeResult(is_error=False, subtype="success", total_cost_usd=0.0, usage=usage)
    )
    assert r.usage == usage


def test_non_dict_usage_is_dropped_rather_than_stored():
    r = result_to_agent_result(FakeResult(is_error=False, usage="not-a-dict"))
    assert r.usage is None


# ── AgentRunner.run stream handling ─────────────────────────────────────────────
# The SDK raises the CLI's trailing ProcessError when Claude Code exits non-zero
# AFTER delivering its ResultMessage (e.g. a benign post-`gh pr create` exit, or
# an is_error result). We read the ENVELOPE, never the exit code, so a result we
# already captured must survive a post-result raise. Only a crash with NO result
# is a genuine transport failure.
import asyncio

import pytest

from issue_agent.adapters.agent_runner import AgentRunner


class _ResultMessage(FakeResult):
    """Stand-in recognised by run() via the isinstance(message, ResultMessage)
    check — we monkeypatch the SDK symbols run() lazily imports."""


def _install_fake_sdk(monkeypatch, messages, raise_after=False, capture=None):
    async def fake_query(prompt, options):
        for m in messages:
            yield m
        if raise_after:
            raise RuntimeError("Claude Code returned an error result: success")

    # run() does `from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query`
    # at call time, so injecting a fake module into sys.modules is the whole
    # mechanism — there is no import cache on the runner to reset.
    import sys
    import types

    def _options(**kw):
        if capture is not None:
            capture["options"] = kw
        return object()

    fake = types.ModuleType("claude_agent_sdk")
    fake.ClaudeAgentOptions = _options
    fake.ResultMessage = _ResultMessage
    fake.query = fake_query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)


def test_run_honours_result_despite_post_result_raise(monkeypatch):
    msg = _ResultMessage(
        is_error=False,
        structured_output={"status": "pr_opened", "summary": "opened"},
        session_id="s1",
    )
    _install_fake_sdk(monkeypatch, [msg], raise_after=True)
    r = asyncio.run(AgentRunner().run(RuntimePlan(prompt="x")))
    # The post-result ProcessError must NOT clobber the captured envelope.
    assert r.status == "pr_opened"
    assert r.session_id == "s1"
    assert r.is_error is False


def test_unpinned_sdk_legs_journal_the_init_models_resolved_value(monkeypatch, tmp_path):
    """Default-model review receipts name the concrete SDK model."""
    from tests.fakes.legacy_journal import Journal
    from issue_agent.adapters.journal import JournalingRuntime
    from tests.fakes.legacy_journal import read_events

    for role, kind in (("review", "REVIEW"),):
        init = type("InitMessage", (), {
            "subtype": "init", "data": {"model": "claude-opus-4-1"},
        })()
        result = _ResultMessage(
            is_error=False, structured_output={"decision": "APPROVE"}, session_id="s1",
        )
        _install_fake_sdk(monkeypatch, [init, result])

        journal = Journal(str(tmp_path / f"{role}-{kind}.jsonl"))
        runner = JournalingRuntime(AgentRunner(), journal, role=role)
        runner.set_context(role, kind, 100, 91)

        asyncio.run(runner.run(RuntimePlan(prompt="unconfigured SDK leg")))

        start, settled = read_events(journal.path)
        assert start["model"] == ""
        assert settled["model"] == "claude-opus-4-1"


def test_pinned_sdk_review_journal_keeps_the_configured_model(monkeypatch, tmp_path):
    """Configured aliases remain the journal's model across the SDK session."""
    from tests.fakes.legacy_journal import Journal
    from issue_agent.adapters.journal import JournalingRuntime
    from tests.fakes.legacy_journal import read_events

    init = type("InitMessage", (), {
        "subtype": "init", "data": {"model": "claude-opus-4-1"},
    })()
    result = _ResultMessage(
        is_error=False, structured_output={"decision": "APPROVE"}, session_id="s1",
    )
    _install_fake_sdk(monkeypatch, [init, result])
    journal = Journal(str(tmp_path / "review-journal.jsonl"))
    runner = JournalingRuntime(AgentRunner(), journal, role="review")
    runner.set_context("review", "REVIEW", 100, 91)

    asyncio.run(runner.run(RuntimePlan(prompt="configured SDK leg", model="opus")))

    start, settled = read_events(journal.path)
    assert start["model"] == "opus"
    assert settled["model"] == "opus"


def test_unpinned_sdk_no_result_journals_the_init_models_resolved_value(monkeypatch, tmp_path):
    """A dropped terminal envelope retains the SDK model in its settled receipt."""
    from tests.fakes.legacy_journal import Journal
    from issue_agent.adapters.journal import JournalingRuntime
    from tests.fakes.legacy_journal import read_events

    init = type("InitMessage", (), {
        "subtype": "init", "data": {"model": "claude-opus-4-1"},
    })()
    _install_fake_sdk(monkeypatch, [init])
    journal = Journal(str(tmp_path / "review-journal.jsonl"))
    runner = JournalingRuntime(AgentRunner(), journal, role="review")
    runner.set_context("review", "REVIEW", 100, 91)

    asyncio.run(runner.run(RuntimePlan(prompt="unconfigured SDK leg")))

    start, settled = read_events(journal.path)
    assert start["model"] == ""
    assert settled["exit_reason"] == "no_result"
    assert settled["model"] == "claude-opus-4-1"


def test_unpinned_sdk_timeout_journals_the_init_models_resolved_value(monkeypatch, tmp_path):
    """An idle SDK leg keeps its init model when its timeout settles it."""
    from tests.fakes.legacy_journal import Journal
    from issue_agent.adapters.journal import JournalingRuntime
    from tests.fakes.legacy_journal import read_events

    init = type("InitMessage", (), {
        "subtype": "init", "data": {"model": "claude-opus-4-1"},
    })()
    _install_hanging_sdk(monkeypatch, [init])
    journal = Journal(str(tmp_path / "review-journal.jsonl"))
    runner = JournalingRuntime(
        AgentRunner(idle_timeout=0.05, poll_slice=0.01), journal, role="review"
    )
    runner.set_context("review", "REVIEW", 100, 91)

    asyncio.run(runner.run(RuntimePlan(prompt="unconfigured SDK leg")))

    _, settled = read_events(journal.path)
    assert settled["exit_reason"] == "idle_timeout"
    assert settled["model"] == "claude-opus-4-1"


def test_run_skips_task_notification_followup_result(monkeypatch):
    """A background task finishing injects a synthetic follow-up whose ResultMessage
    carries origin={"kind":"task-notification"}. It must NOT be taken as our result —
    the runner keeps the ResultMessage that answers our prompt. Here the follow-up
    (a bare/empty result) arrives BEFORE the real verdict; the runner must skip the
    former and return the latter, not clobber it."""
    followup = _ResultMessage(is_error=False, structured_output={"status": "ignored"})
    followup.origin = {"kind": "task-notification"}
    real = _ResultMessage(
        is_error=False, structured_output={"status": "pr_opened", "summary": "ok"},
        session_id="real",
    )
    _install_fake_sdk(monkeypatch, [followup, real])
    r = asyncio.run(AgentRunner().run(RuntimePlan(prompt="x")))
    assert r.status == "pr_opened"
    assert r.session_id == "real"


def test_run_reraises_when_crash_before_any_result(monkeypatch):
    _install_fake_sdk(monkeypatch, [], raise_after=True)
    try:
        asyncio.run(AgentRunner().run(RuntimePlan(prompt="x")))
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected the pre-result crash to propagate")


def test_transport_death_is_logged_with_type_and_last_message(monkeypatch):
    """The wedge we couldn't diagnose: a crash before any ResultMessage now logs
    the exception type AND the last SDK message seen (the tool that preceded it),
    always — not gated on debug — so the fix log alone explains a Stream closed."""
    tool = _Msg = type("Blk", (), {})  # noqa: N806
    blk = _Msg()
    blk.name = "Bash"
    blk.input = {"command": "uv run pytest"}
    carrier = type("Carrier", (), {})()
    carrier.content = [blk]
    _install_fake_sdk(monkeypatch, [carrier], raise_after=True)  # raise, no result
    logged: list[str] = []
    try:
        asyncio.run(AgentRunner(log=logged.append, debug=True).run(RuntimePlan(prompt="x")))
    except RuntimeError:
        pass
    joined = "\n".join(logged)
    assert "transport died before any ResultMessage" in joined
    assert "RuntimeError" in joined
    assert "Bash(uv run pytest)" in joined  # last-seen tool surfaced


def test_transport_death_logged_even_without_debug(monkeypatch):
    _install_fake_sdk(monkeypatch, [], raise_after=True)
    logged: list[str] = []
    try:
        asyncio.run(AgentRunner(log=logged.append, debug=False).run(RuntimePlan(prompt="x")))
    except RuntimeError:
        pass
    assert any("transport died before any ResultMessage" in m for m in logged)


# ── idle watchdog: hang detection without false-positives on hard tasks ───────
def _install_hanging_sdk(monkeypatch, messages, hang_after=True):
    """Fake query that yields `messages` fast, then (hang_after) blocks forever —
    the silent-hang shape (no more events, never returns/raises)."""
    async def fake_query(prompt, options):
        for m in messages:
            yield m
        if hang_after:
            await asyncio.Event().wait()  # never set → hang

    import sys
    import types

    fake = types.ModuleType("claude_agent_sdk")
    fake.ClaudeAgentOptions = lambda **kw: object()
    fake.ResultMessage = _ResultMessage
    fake.query = fake_query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)


def test_idle_timeout_trips_on_hang(monkeypatch):
    """A stream that goes silent (no event) past idle_timeout → timeout result,
    not a permanent wedge. This is the #56 daemon-freeze made recoverable."""
    _install_hanging_sdk(monkeypatch, [], hang_after=True)
    logged: list[str] = []
    r = asyncio.run(
        AgentRunner(log=logged.append, idle_timeout=0.2).run(RuntimePlan(prompt="x"))
    )
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

    import sys
    import types

    fake = types.ModuleType("claude_agent_sdk")
    fake.ClaudeAgentOptions = lambda **kw: object()
    fake.ResultMessage = _ResultMessage
    fake.query = fake_query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)

    async def scenario():
        task = asyncio.create_task(
            AgentRunner(idle_timeout=60.0, poll_slice=0.01).run(
                RuntimePlan(prompt="x")
            )
        )
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
    r = asyncio.run(AgentRunner().run(RuntimePlan(prompt="x")))
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
    ticks = iter([1000.0, 4600.0, 4600.0, 4600.0])
    clock = {"t": 1000.0}

    def wall():
        try:
            clock["t"] = next(ticks)
        except StopIteration:
            pass
        return clock["t"]

    logged: list[str] = []
    # idle_timeout large (900) but poll_slice tiny so real slices are fast; the trip
    # must come from the wall-clock jump, not from 900s of real waiting.
    r = asyncio.run(
        AgentRunner(
            log=logged.append, idle_timeout=900.0, poll_slice=0.05, wall_clock=wall
        ).run(RuntimePlan(prompt="x"))
    )
    assert r.is_error and r.status == "timeout"
    assert any("IDLE" in m for m in logged)


def test_idle_timeout_does_not_trip_on_steady_stream(monkeypatch):
    """A task that keeps emitting events (each gap < idle_timeout) must NOT trip —
    the false-positive guard. Events arrive faster than the idle window; a final
    ResultMessage ends the run cleanly."""
    async def fake_query(prompt, options):
        for _ in range(5):
            await asyncio.sleep(0.02)  # gap well under idle_timeout
            yield object()  # non-result event resets the clock
        yield _ResultMessage(is_error=False,
                             structured_output={"status": "pr_opened"}, session_id="s")

    import sys
    import types
    fake = types.ModuleType("claude_agent_sdk")
    fake.ClaudeAgentOptions = lambda **kw: object()
    fake.ResultMessage = _ResultMessage
    fake.query = fake_query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)

    r = asyncio.run(AgentRunner(idle_timeout=0.2).run(RuntimePlan(prompt="x")))
    assert r.is_error is False and r.status == "pr_opened"  # completed, never tripped


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
        yield object()                    # early event
        await asyncio.sleep(0.15)         # gap > poll_slice (0.05), < idle_timeout (5.0)
        yield object()                    # event after a multi-slice pause
        await asyncio.sleep(0.15)         # another long-ish pause before the result
        yield _ResultMessage(is_error=False,
                             structured_output={"status": "pr_opened"}, session_id="s")

    import sys
    import types
    fake = types.ModuleType("claude_agent_sdk")
    fake.ClaudeAgentOptions = lambda **kw: object()
    fake.ResultMessage = _ResultMessage
    fake.query = fake_query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)

    logged: list[str] = []
    r = asyncio.run(
        AgentRunner(log=logged.append, idle_timeout=5.0, poll_slice=0.05).run(
            RuntimePlan(prompt="x")
        )
    )
    # The result survives the > poll_slice gaps; no idle trip, no drop.
    assert r.is_error is False and r.status == "pr_opened"
    assert not any("IDLE" in m for m in logged)
    assert not any("NO ResultMessage" in m for m in logged)


# ── total-runtime (wall) cap: the bound the idle watchdog structurally can't give ─
def _jumping_wall(ticks):
    """Injected wall clock that walks `ticks`, then holds the last value. Lets a
    test simulate hours of elapsed time without waiting for them."""
    it = iter(ticks)
    held = {"t": ticks[0]}

    def wall():
        try:
            held["t"] = next(it)
        except StopIteration:
            pass
        return held["t"]

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

    r = asyncio.run(
        AgentRunner(
            log=logged.append, idle_timeout=None, max_runtime=900.0,
            poll_slice=0.05, wall_clock=wall,
        ).run(RuntimePlan(prompt="x"))
    )

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

    import sys
    import types
    fake = types.ModuleType("claude_agent_sdk")
    fake.ClaudeAgentOptions = lambda **kw: object()
    fake.ResultMessage = _ResultMessage
    fake.query = fake_query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)

    wall = _jumping_wall([1000.0, 1000.0, 1000.0, 4600.0])
    logged: list[str] = []

    r = asyncio.run(
        AgentRunner(
            log=logged.append, idle_timeout=900.0, max_runtime=900.0, wall_clock=wall
        ).run(RuntimePlan(prompt="x"))
    )

    assert r.exit_reason is ExitReason.WALL_TIMEOUT
    assert not any("IDLE" in m for m in logged)  # the idle watchdog never saw it


def test_wall_cap_honours_a_result_already_captured(monkeypatch):
    """The envelope always wins over a watchdog. If the agent delivered its verdict
    and only THEN the cap expires, the run must return that verdict — aborting it
    into a timeout would discard finished, paid-for work."""
    msg = _ResultMessage(
        is_error=False, subtype="success",
        structured_output={"status": "pr_opened", "summary": "opened"}, session_id="s1",
    )
    _install_fake_sdk(monkeypatch, [msg])
    wall = _jumping_wall([1000.0, 1000.0, 1000.0, 4600.0])

    r = asyncio.run(
        AgentRunner(max_runtime=900.0, wall_clock=wall).run(RuntimePlan(prompt="x"))
    )

    assert r.status == "pr_opened"
    assert r.is_error is False
    assert r.exit_reason is ExitReason.OK


def test_healthy_short_run_is_not_capped(monkeypatch):
    """False-positive guard: a run well inside the cap completes normally, and the
    cap leaves no trace on the result."""
    msg = _ResultMessage(
        is_error=False, subtype="success", structured_output={"status": "pr_opened"}
    )
    _install_fake_sdk(monkeypatch, [msg])
    logged: list[str] = []

    r = asyncio.run(
        AgentRunner(log=logged.append, idle_timeout=900.0, max_runtime=2700.0).run(
            RuntimePlan(prompt="x")
        )
    )

    assert r.status == "pr_opened" and r.is_error is False
    assert not any("WALL" in m or "IDLE" in m for m in logged)


# ── debug tracing: make a mid-session "Stream closed" wedge diagnosable ────────
from issue_agent.adapters.agent_runner import _trace_message  # noqa: E402


@dataclass
class _ToolUseBlock:
    name: str
    input: dict


@dataclass
class _Msg:
    content: list


def test_trace_message_surfaces_tool_name_and_command():
    m = _Msg(content=[_ToolUseBlock("Bash", {"command": "pytest -q\nsecond line"})])
    t = _trace_message(m)
    assert "_Msg" in t and "Bash(pytest -q)" in t  # first line only, name shown


def test_trace_message_uses_file_path_when_no_command():
    m = _Msg(content=[_ToolUseBlock("Read", {"file_path": "/a/b/c.py"})])
    assert "Read(/a/b/c.py)" in _trace_message(m)


def test_trace_message_never_raises_on_garbage():
    assert _trace_message(object()) != ""  # returns the type name, no crash


def test_trace_message_bare_kind_when_no_tooluse():
    @dataclass
    class _Result:
        is_error: bool = False

    assert _trace_message(_Result()) == "_Result"


# ── running token total, so an aborted run knows what it spent ───────────────
# Cost only arrives on the final ResultMessage, so a wall-cap / idle / dropped-
# result abort reported usage=None and ledgered $0.00 however long it ran (first
# real deployment: 906s, four commits, $0.00). AssistantMessage.usage is the API
# call's OWN per-turn block, so summing across turns gives billed totals.
def test_merge_usage_sums_per_turn_blocks():
    from issue_agent.adapters.agent_runner import merge_usage

    acc = merge_usage(None, {"input_tokens": 10, "output_tokens": 2})
    acc = merge_usage(acc, {"input_tokens": 30, "output_tokens": 5})
    assert acc == {"input_tokens": 40, "output_tokens": 7}


def test_merge_usage_distinguishes_no_usage_from_zero_tokens():
    from issue_agent.adapters.agent_runner import merge_usage

    assert merge_usage(None, None) is None          # nothing ever reported
    assert merge_usage(None, {}) == {}              # reported, and it was empty
    assert merge_usage(None, "not-a-dict") is None


def test_merge_usage_ignores_non_numeric_and_boolean_fields():
    from issue_agent.adapters.agent_runner import merge_usage

    acc = merge_usage(None, {"input_tokens": 5, "cached": True, "model": "opus"})
    assert acc == {"input_tokens": 5}


def test_merge_usage_does_not_mutate_the_accumulator_it_is_given():
    from issue_agent.adapters.agent_runner import merge_usage

    first = merge_usage(None, {"input_tokens": 1})
    second = merge_usage(first, {"input_tokens": 1})
    assert first == {"input_tokens": 1} and second == {"input_tokens": 2}


def test_wall_timeout_result_carries_the_tokens_spent_before_the_abort():
    """The regression: this result used to be built with no usage at all, so the
    journal had nothing to price and the run ledgered $0.00."""
    from issue_agent.adapters.agent_runner import AgentRunner

    r = AgentRunner(max_runtime=900)
    res = r._wall_timeout(906.0, "Bash(pytest)", {"output_tokens": 22349})

    assert res.exit_reason is ExitReason.WALL_TIMEOUT
    assert res.usage == {"output_tokens": 22349}


# ---- commit-attribution identity (git author = "Harness - <model>") ----


def test_env_carries_git_identity_when_model_set():
    kw = build_option_kwargs(RuntimePlan(prompt="p", model="claude-sonnet-5"))
    assert kw["env"]["GIT_AUTHOR_NAME"] == "Harness - claude-sonnet-5"
    assert kw["env"]["GIT_COMMITTER_NAME"] == "Harness - claude-sonnet-5"


def test_task_env_override_beats_identity():
    kw = build_option_kwargs(
        RuntimePlan(prompt="p", model="m", env={"GIT_AUTHOR_NAME": "me"})
    )
    assert kw["env"]["GIT_AUTHOR_NAME"] == "me"
    assert kw["env"]["GIT_COMMITTER_NAME"] == "Harness - m"


def test_no_model_emits_no_git_identity():
    kw = build_option_kwargs(RuntimePlan(prompt="p"))
    assert "GIT_AUTHOR_NAME" not in kw["env"]
    assert "GIT_COMMITTER_NAME" not in kw["env"]


# ── CLI resolution (keifu 2026-08-03: venv rebuilt under a live daemon) ───────
def test_claude_cli_pin_wins_even_when_bundled_exists(tmp_path):
    bundled = tmp_path / "claude"
    bundled.write_text("#!/bin/sh\n")
    bundled.chmod(0o755)
    assert resolve_claude_cli(
        " /pinned/claude ", bundled, which=lambda _: "/path/claude"
    ) == "/pinned/claude"


def test_claude_cli_bundled_then_path_when_unpinned(tmp_path):
    bundled = tmp_path / "claude"
    bundled.write_text("#!/bin/sh\n")
    bundled.chmod(0o755)
    assert resolve_claude_cli("", bundled) == str(bundled)
    # The stale-venv shape: the bundled path no longer exists on disk.
    gone = tmp_path / "deleted-venv" / "claude"
    assert resolve_claude_cli("", gone, which=lambda _: "/local/claude") == "/local/claude"


def test_claude_cli_unresolvable_returns_none(tmp_path):
    assert resolve_claude_cli("", tmp_path / "missing", which=lambda _: None) is None
