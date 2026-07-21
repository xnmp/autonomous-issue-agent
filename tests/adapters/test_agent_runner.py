"""AgentRunner pure mapping — AgentTask→options kwargs and ResultMessage→AgentResult.

No live SDK call: build_option_kwargs and result_to_agent_result are pure, and
result mapping is duck-typed so a stand-in object stands in for ResultMessage.
"""

from dataclasses import dataclass

from issue_agent.adapters.agent_runner import (
    DEFAULT_PERMISSION_MODE,
    build_option_kwargs,
    result_to_agent_result,
)
from issue_agent.domain.agent_task import AgentTask, McpServer, SubagentDef


def test_default_permission_mode_is_bypass():
    assert DEFAULT_PERMISSION_MODE == "bypassPermissions"


def test_schema_maps_to_output_format():
    schema = {"type": "object"}
    kw = build_option_kwargs(AgentTask(prompt="x", schema=schema))
    assert kw["output_format"] == {"type": "json_schema", "schema": schema}


def test_append_system_prompt_uses_claude_code_preset():
    kw = build_option_kwargs(AgentTask(prompt="x", append_system_prompt="rules"))
    assert kw["system_prompt"] == {
        "type": "preset",
        "preset": "claude_code",
        "append": "rules",
    }


def test_no_append_still_uses_preset():
    # The SDK defaults to a MINIMAL prompt; we must opt into the claude_code preset.
    kw = build_option_kwargs(AgentTask(prompt="x"))
    assert kw["system_prompt"] == {"type": "preset", "preset": "claude_code"}


def test_subagents_map_to_agents_dict_and_add_task_tool():
    task = AgentTask(
        prompt="x",
        allowed_tools=("Read",),
        subagents=(
            SubagentDef(
                name="code-scout",
                description="cheap scout",
                prompt="search the code",
                tools=("Read", "Grep"),
                model="sonnet",
            ),
        ),
    )
    kw = build_option_kwargs(task)
    # Task tool auto-added so the parent can spawn the subagent.
    assert "Task" in kw["allowed_tools"]
    # agents emitted as a plain dict spec (SDK-free; run() materialises it).
    assert kw["agents"]["code-scout"] == {
        "description": "cheap scout",
        "prompt": "search the code",
        "tools": ["Read", "Grep"],
        "model": "sonnet",
    }


def test_no_subagents_means_no_agents_key_and_no_task_tool():
    kw = build_option_kwargs(AgentTask(prompt="x", allowed_tools=("Read",)))
    assert "agents" not in kw
    assert "Task" not in kw.get("allowed_tools", [])


def test_tools_model_budget_cwd_resume_map():
    task = AgentTask(
        prompt="x",
        allowed_tools=("Read", "Bash(git *)"),
        disallowed_tools=("Edit", "Write"),
        model="opus",
        max_budget_usd=1.5,
        cwd="/wt/issue-5",
        resume="sess-123",
        fork_session=True,
    )
    kw = build_option_kwargs(task)
    assert kw["allowed_tools"] == ["Read", "Bash(git *)"]
    assert kw["disallowed_tools"] == ["Edit", "Write"]
    assert kw["model"] == "opus"
    assert kw["max_budget_usd"] == 1.5
    assert kw["cwd"] == "/wt/issue-5"
    assert kw["resume"] == "sess-123"
    assert kw["fork_session"] is True
    assert kw["setting_sources"] == ["project"]


def test_mcp_servers_and_tool_globs():
    task = AgentTask(
        prompt="x",
        allowed_tools=("Read",),
        mcp_servers=(McpServer("sonarqube", {"command": "npx"}, "mcp__sonarqube__*"),),
    )
    kw = build_option_kwargs(task)
    assert kw["mcp_servers"] == {"sonarqube": {"command": "npx"}}
    assert kw["allowed_tools"] == ["Read", "mcp__sonarqube__*"]


def test_hooks_passed_through():
    sentinel = {"PreToolUse": ["matcher"]}
    kw = build_option_kwargs(AgentTask(prompt="x"), extra_hooks=sentinel)
    assert kw["hooks"] is sentinel


def test_permission_mode_override():
    kw = build_option_kwargs(AgentTask(prompt="x"), permission_mode="auto")
    assert kw["permission_mode"] == "auto"


# ── ResultMessage → AgentResult ────────────────────────────────────────────────
@dataclass
class FakeResult:
    is_error: bool = False
    structured_output: object = None
    result: str = ""
    total_cost_usd: float = 0.0
    session_id: str = ""


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


def test_result_falls_back_to_result_text_when_no_structured():
    msg = FakeResult(structured_output=None, result="free text")
    r = result_to_agent_result(msg)
    assert r.summary == "free text"


# ── AgentRunner.run stream handling ─────────────────────────────────────────────
# The SDK raises the CLI's trailing ProcessError when Claude Code exits non-zero
# AFTER delivering its ResultMessage (e.g. a benign post-`gh pr create` exit, or
# an is_error result). We read the ENVELOPE, never the exit code, so a result we
# already captured must survive a post-result raise. Only a crash with NO result
# is a genuine transport failure.
import asyncio

from issue_agent.adapters.agent_runner import AgentRunner


class _ResultMessage(FakeResult):
    """Stand-in recognised by run() via the isinstance(message, ResultMessage)
    check — we monkeypatch the SDK symbols run() lazily imports."""


def _install_fake_sdk(monkeypatch, messages, raise_after=False):
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

    fake = types.ModuleType("claude_agent_sdk")
    fake.ClaudeAgentOptions = lambda **kw: object()
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
    r = asyncio.run(AgentRunner().run(AgentTask(prompt="x")))
    # The post-result ProcessError must NOT clobber the captured envelope.
    assert r.status == "pr_opened"
    assert r.session_id == "s1"
    assert r.is_error is False


def test_run_reraises_when_crash_before_any_result(monkeypatch):
    _install_fake_sdk(monkeypatch, [], raise_after=True)
    try:
        asyncio.run(AgentRunner().run(AgentTask(prompt="x")))
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
        asyncio.run(AgentRunner(log=logged.append, debug=True).run(AgentTask(prompt="x")))
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
        asyncio.run(AgentRunner(log=logged.append, debug=False).run(AgentTask(prompt="x")))
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
        AgentRunner(log=logged.append, idle_timeout=0.2).run(AgentTask(prompt="x"))
    )
    assert r.is_error and r.status == "timeout"
    assert any("IDLE" in m for m in logged)


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
        ).run(AgentTask(prompt="x"))
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

    r = asyncio.run(AgentRunner(idle_timeout=0.2).run(AgentTask(prompt="x")))
    assert r.is_error is False and r.status == "pr_opened"  # completed, never tripped


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
