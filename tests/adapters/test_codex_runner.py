"""CodexRunner — pure argv assembly + envelope parsing, plus run() over a fake
`codex` binary. No real model call: build_codex_argv / parse_codex_envelope are
pure, and run() is exercised against a tiny shell stub that mimics `codex exec`
(writes the -o file, prints a `session id:` header).
"""

import asyncio
import json
import os
import stat
import tempfile

from issue_agent.adapters.codex_runner import (
    DEFAULT_CODEX_MODEL,
    DEFAULT_SANDBOX,
    CodexProvider,
    CodexRunner,
    _extract_retry_after_seconds,
    _is_rate_limited,
    _is_transport_drop,
    _session_id_from_lines,
    _strict_schema,
    build_codex_argv,
    parse_codex_envelope,
)
from issue_agent.domain.outcome import AgentResult
from issue_agent.domain.agent_task import AgentTask


# ── build_codex_argv (pure) ─────────────────────────────────────────────────────
def test_argv_basic_shape():
    argv = build_codex_argv(AgentTask(prompt="do it"))
    assert argv[:2] == ["codex", "exec"]
    assert argv[-1] == "do it"  # prompt is last positional
    assert "--sandbox" in argv and DEFAULT_SANDBOX in argv
    assert "--dangerously-bypass-hook-trust" in argv
    # default model when task.model is None
    assert "-m" in argv and DEFAULT_CODEX_MODEL in argv


def test_argv_provider_overrides_inline():
    prov = CodexProvider(provider_id="acme", base_url="https://gw.example/v1")
    argv = build_codex_argv(AgentTask(prompt="x"), provider=prov)
    joined = " ".join(argv)
    assert 'model_providers.acme.base_url="https://gw.example/v1"' in joined
    assert 'model_providers.acme.wire_api="responses"' in joined
    assert 'model_provider="acme"' in joined
    # never put the secret on the argv — only the env_key NAME (codex convention:
    # OPENAI_API_KEY, which behind a proxy is aliased to the gateway key)
    assert 'env_key="OPENAI_API_KEY"' in joined
    assert "OPENAI_API_KEY=" not in joined


def test_argv_model_override_wins():
    argv = build_codex_argv(AgentTask(prompt="x", model="some-other-model"))
    assert "some-other-model" in argv
    assert DEFAULT_CODEX_MODEL not in argv


def test_argv_cwd_schema_output():
    argv = build_codex_argv(
        AgentTask(prompt="x", cwd="/wt/issue-5"),
        schema_path="/tmp/s.json", output_path="/tmp/o.json",
    )
    assert argv[argv.index("-C") + 1] == "/wt/issue-5"
    assert argv[argv.index("--output-schema") + 1] == "/tmp/s.json"
    assert argv[argv.index("-o") + 1] == "/tmp/o.json"


def test_argv_resume_comes_first():
    argv = build_codex_argv(AgentTask(prompt="x", resume="sess-123"))
    # `exec resume <id>` must precede the -c/-m flags
    assert argv[1] == "exec" and argv[2] == "resume" and argv[3] == "sess-123"


def test_argv_resume_omits_exec_only_flags():
    # `codex exec resume` REJECTS --sandbox and -C (exit 2). The resume argv must
    # emit NEITHER — this is the bug that broke the gateway-drop retry live.
    argv = build_codex_argv(
        AgentTask(prompt="x", resume="sess-123", cwd="/wt/issue-59"),
        schema_path="/tmp/s.json", output_path="/tmp/o.json",
    )
    assert "--sandbox" not in argv          # forbidden on resume
    assert "-C" not in argv                  # forbidden on resume (cwd set on subprocess)
    # sandbox parity uses the resume-legal bypass flag instead
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    # -o / --output-schema ARE legal on resume and must survive (else no envelope)
    assert argv[argv.index("-o") + 1] == "/tmp/o.json"
    assert argv[argv.index("--output-schema") + 1] == "/tmp/s.json"
    assert "--dangerously-bypass-hook-trust" in argv  # governor stays on


def test_argv_nonresume_keeps_sandbox_and_cd():
    # The plain-exec path is unchanged: --sandbox + -C present, no resume bypass.
    argv = build_codex_argv(AgentTask(prompt="x", cwd="/wt/issue-5"))
    assert "--sandbox" in argv and DEFAULT_SANDBOX in argv
    assert argv[argv.index("-C") + 1] == "/wt/issue-5"
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv


def test_argv_prepends_system_prompt_to_prompt():
    argv = build_codex_argv(AgentTask(prompt="the task", append_system_prompt="RULES"))
    assert argv[-1].startswith("RULES")
    assert argv[-1].endswith("the task")


def test_argv_skips_system_prompt_when_inject_disabled():
    # When the contract is delivered via AGENTS.md, the prompt must NOT also be
    # prepended (no duplication).
    argv = build_codex_argv(
        AgentTask(prompt="the task", append_system_prompt="RULES"),
        inject_system_prompt=False,
    )
    assert argv[-1] == "the task"
    assert "RULES" not in argv[-1]


def test_argv_custom_provider_and_bin():
    prov = CodexProvider(provider_id="acme", name="Acme", base_url="https://x/y",
                         env_key="ACME_KEY", wire_api="chat")
    argv = build_codex_argv(AgentTask(prompt="x"), codex_bin="/opt/codex", provider=prov)
    assert argv[0] == "/opt/codex"
    joined = " ".join(argv)
    assert 'model_provider="acme"' in joined and 'env_key="ACME_KEY"' in joined


# ── _strict_schema ───────────────────────────────────────────────────────────────
def test_strict_schema_requires_all_and_closes():
    s = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
    out = _strict_schema(s)
    assert set(out["required"]) == {"a", "b"}
    assert out["additionalProperties"] is False
    # original untouched (shallow-copy semantics)
    assert "required" not in s


def test_strict_schema_recurses_nested_objects():
    s = {"type": "object", "properties": {
        "outer": {"type": "object", "properties": {"inner": {"type": "string"}}}}}
    out = _strict_schema(s)
    assert out["properties"]["outer"]["required"] == ["inner"]
    assert out["properties"]["outer"]["additionalProperties"] is False


def test_strict_schema_passthrough_non_object():
    assert _strict_schema({"type": "string"}) == {"type": "string"}


# ── parse_codex_envelope (pure) ──────────────────────────────────────────────────
def test_envelope_success_maps_fields():
    msg = json.dumps({"status": "pr_opened", "summary": "opened #5", "needs": ""})
    r = parse_codex_envelope(exit_code=0, last_message=msg, session_id="s1")
    assert r.is_error is False
    assert r.status == "pr_opened"
    assert r.summary == "opened #5"
    assert r.session_id == "s1"
    assert r.structured == {"status": "pr_opened", "summary": "opened #5", "needs": ""}


def test_envelope_blocked_carries_needs():
    msg = json.dumps({"status": "blocked", "summary": "stuck", "needs": "which endpoint?"})
    r = parse_codex_envelope(exit_code=0, last_message=msg)
    assert r.status == "blocked" and r.needs == "which endpoint?"
    assert r.is_error is False


def test_envelope_status_error_is_error():
    msg = json.dumps({"status": "error", "summary": "boom", "needs": ""})
    r = parse_codex_envelope(exit_code=0, last_message=msg)
    assert r.is_error is True


def test_envelope_nonzero_exit_no_json_is_error():
    r = parse_codex_envelope(exit_code=1, last_message="", stderr="provider 401")
    assert r.is_error is True
    assert "provider 401" in r.summary


def test_envelope_nonzero_exit_freetext_only_is_error():
    r = parse_codex_envelope(exit_code=3, last_message="not json output")
    assert r.is_error is True


def test_envelope_zero_exit_freetext_not_error():
    # codex printed prose but no schema (e.g. no --output-schema) and exited 0.
    r = parse_codex_envelope(exit_code=0, last_message="PONG")
    assert r.is_error is False and r.summary == "PONG"


def test_session_id_from_lines():
    out = "OpenAI Codex v0.142.3\nsession id: 019f0bdd-b4df-7c03\n--------\n"
    assert _session_id_from_lines(out) == "019f0bdd-b4df-7c03"
    assert _session_id_from_lines("no header here") == ""


# ── run() over a fake codex binary ───────────────────────────────────────────────
def _fake_codex(tmp, body: str) -> str:
    """Write an executable stub at <tmp>/codex that runs `body` (bash)."""
    path = os.path.join(tmp, "codex")
    with open(path, "w") as f:
        f.write("#!/usr/bin/env bash\n" + body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return path


def test_run_parses_output_file_and_session_stdout_fallback():
    # Session id on STDOUT exercises the FALLBACK branch only — real `codex exec`
    # emits it on stderr (covered by test_run_reads_session_id_from_stderr, the
    # primary path). Kept distinct so neither branch silently masks the other.
    tmp = tempfile.mkdtemp()
    # The stub: find the -o path in argv, write an envelope there, print a header.
    body = r'''
out=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-o" ]; then out="$a"; fi
  prev="$a"
done
echo "session id: sess-xyz"
printf '%s' '{"status":"pr_opened","summary":"done","needs":""}' > "$out"
exit 0
'''
    codex = _fake_codex(tmp, body)
    runner = CodexRunner(codex_bin=codex, env={"OPENAI_API_KEY": "test-key"})
    r = asyncio.run(runner.run(AgentTask(prompt="resolve", schema={
        "type": "object", "properties": {"status": {"type": "string"},
        "summary": {"type": "string"}, "needs": {"type": "string"}}})))
    assert r.is_error is False
    assert r.status == "pr_opened"
    assert r.session_id == "sess-xyz"


def test_run_nonzero_exit_no_output_is_error():
    tmp = tempfile.mkdtemp()
    body = 'echo "boom" >&2\nexit 1\n'
    codex = _fake_codex(tmp, body)
    runner = CodexRunner(codex_bin=codex, env={"OPENAI_API_KEY": "test-key"})
    r = asyncio.run(runner.run(AgentTask(prompt="x")))
    assert r.is_error is True


def test_run_reads_session_id_from_stderr():
    # Real `codex exec` prints `session id:` to STDERR, not stdout.
    tmp = tempfile.mkdtemp()
    body = r'''
out=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-o" ]; then out="$a"; fi
  prev="$a"
done
echo "session id: sess-from-stderr" >&2
printf '%s' '{"status":"done","summary":"ok","needs":""}' > "$out"
exit 0
'''
    codex = _fake_codex(tmp, body)
    runner = CodexRunner(codex_bin=codex, env={"OPENAI_API_KEY": "test-key"})
    r = asyncio.run(runner.run(AgentTask(prompt="x", schema={
        "type": "object", "properties": {"status": {"type": "string"},
        "summary": {"type": "string"}, "needs": {"type": "string"}}})))
    assert r.session_id == "sess-from-stderr"
    assert r.status == "done"


# ── rate-limit (HTTP 429) classification + window-aligned backoff ─────────────
def test_is_rate_limited_matches_provider_429_text():
    # A representative Azure OpenAI gateway 429 body.
    azure = ('azure-openai error: Your requests to gpt-5.5 for '
             'your-deployment in eastus2 have exceeded rate limit.')
    assert _is_rate_limited(azure) is True
    assert _is_rate_limited('"code":"rate_limit_exceeded"') is True
    assert _is_rate_limited("HTTP 429 Too Many Requests") is True
    assert _is_rate_limited("quota exceeded for this deployment") is True
    # A plain network drop is NOT a rate limit (routes to the short backoff).
    assert _is_rate_limited("stream disconnected before completion") is False
    assert _is_rate_limited("response.failed event received") is False


def test_rate_limit_is_a_transport_drop_but_network_drop_is_not_rate_limited():
    err = AgentResult(is_error=True)
    # 429 counts as a retryable transport drop…
    assert _is_transport_drop(1, "azure error: exceeded rate limit", err) is True
    # …but a captured success envelope is never retried, even with 429 text.
    ok = AgentResult(is_error=False, status="pr_opened")
    assert _is_transport_drop(0, "exceeded rate limit", ok) is False


def test_extract_retry_after_seconds_parses_common_shapes():
    assert _extract_retry_after_seconds("retry-after: 23") == 23.0
    assert _extract_retry_after_seconds("please retry in 12.5s") == 12.5
    assert _extract_retry_after_seconds("x-ratelimit-reset-tokens: 19") == 19.0
    assert _extract_retry_after_seconds("retry-after: 2000ms") == 2.0
    # No hint → None (caller uses the window-aligned default).
    assert _extract_retry_after_seconds("exceeded rate limit") is None
    # Largest hint wins (the furthest-out reset is the safe wait).
    assert _extract_retry_after_seconds("reset-requests: 0 reset-tokens: 47") == 47.0


def test_run_retries_after_rate_limit_then_succeeds():
    """A 429 on the first attempt must NOT be given up on: the runner waits (short,
    in-test) and retries, then succeeds. Regression for the 345 rebase, where the
    2s network backoff retried inside Azure's 60s window and burned all attempts.
    A stub counts invocations via an appended marker file; attempt 1 emits the
    provider 429 text + exits nonzero, attempt 2 writes a real envelope."""
    tmp = tempfile.mkdtemp()
    counter = os.path.join(tmp, "n")
    # Bash stub: increment counter; first run -> 429 to stderr + exit 1; else ok.
    body = (
        'n=$(cat "' + counter + '" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "' + counter + '"\n'
        'out=""; prev=""\n'
        'for a in "$@"; do if [ "$prev" = "-o" ]; then out="$a"; fi; prev="$a"; done\n'
        'echo "session id: sess-rl" >&2\n'
        'if [ "$n" -eq 1 ]; then\n'
        '  echo "azure-openai error: have exceeded rate limit. retry-after: 0" >&2\n'
        '  exit 1\n'
        'fi\n'
        'printf \'%s\' \'{"status":"pr_opened","summary":"done","needs":""}\' > "$out"\n'
        'exit 0\n'
    )
    codex = _fake_codex(tmp, body)
    runner = CodexRunner(
        codex_bin=codex, env={"OPENAI_API_KEY": "k"},
        rate_limit_backoff_base=0.05, max_rate_limit_wait=0.1, max_rate_limit_retries=3,
    )
    r = asyncio.run(runner.run(AgentTask(prompt="rebase", schema={
        "type": "object", "properties": {"status": {"type": "string"},
        "summary": {"type": "string"}, "needs": {"type": "string"}}})))
    assert r.is_error is False, f"rate-limited run was not retried to success: {r.summary}"
    assert r.status == "pr_opened"
    with open(counter) as f:
        assert int(f.read().strip()) == 2, "expected exactly one retry after the 429"


def test_run_gives_up_after_max_rate_limit_retries():
    """A sustained 429 (every attempt) is bounded by max_rate_limit_retries and
    surfaces a retryable timeout — the daemon re-dispatches cold rather than looping
    forever. Uses a tiny backoff so the test is fast."""
    tmp = tempfile.mkdtemp()
    body = (
        'echo "session id: sess-rl2" >&2\n'
        'echo "azure-openai error: exceeded rate limit" >&2\n'
        'exit 1\n'
    )
    codex = _fake_codex(tmp, body)
    runner = CodexRunner(
        codex_bin=codex, env={"OPENAI_API_KEY": "k"},
        rate_limit_backoff_base=0.02, max_rate_limit_wait=0.05, max_rate_limit_retries=2,
    )
    r = asyncio.run(runner.run(AgentTask(prompt="x")))
    assert r.is_error is True
    assert r.status == "timeout"
    assert "rate limit" in r.summary.lower()


# ── idle watchdog measures wall-clock so it survives laptop sleep ─────────────
def test_idle_watchdog_trips_on_wall_clock_not_monotonic():
    """Codex counterpart to the AgentRunner wall-clock fix. A stub that stays silent
    for longer than a poll slice but shorter than idle_timeout in REAL time must still
    be killed once the injected WALL clock jumps past idle_timeout — simulating the
    laptop sleeping mid-fix (macOS freezes the monotonic clock during suspend, so a
    one-shot monotonic wait would not count the sleep and recovery would stall)."""
    import time as _time

    tmp = tempfile.mkdtemp()
    # Stub prints nothing to stderr and sleeps a few seconds, then exits.
    codex = _fake_codex(tmp, "sleep 3\n")
    # Wall clock jumps +3600s after the first read, while real time stays ~seconds.
    ticks = iter([1000.0, 4600.0, 4600.0, 4600.0, 4600.0])
    held = {"t": 1000.0}

    def wall():
        try:
            held["t"] = next(ticks)
        except StopIteration:
            pass
        return held["t"]

    runner = CodexRunner(
        codex_bin=codex, env={"OPENAI_API_KEY": "k"},
        idle_timeout=900.0, poll_slice=0.1, wall_clock=wall,
    )
    start = _time.monotonic()
    r = asyncio.run(runner.run(AgentTask(prompt="x")))
    elapsed = _time.monotonic() - start
    # Tripped fast on the wall-clock jump, not after 900s of real waiting.
    assert elapsed < 20, f"did not trip on wall-clock gap (took {elapsed:.1f}s)"
    assert r.is_error is True and r.status == "timeout"


# ── idle watchdog kills the whole process tree (regression: Myapp #310) ─────────
def test_run_idle_timeout_kills_background_child_and_returns():
    """A hung run whose descendant holds the stdout pipe open must NOT wedge the
    runner. The stub spawns a background child that keeps the inherited stdout fd
    open and sleeps ~long, then the stub itself sleeps ~long with NO stderr output.
    With a tiny idle_timeout the watchdog must (a) return promptly as hung, and
    (b) kill the whole process group so the background child dies too — else
    _read_stdout()/proc.wait() would block forever (the #310 daemon wedge)."""
    import time as _time

    tmp = tempfile.mkdtemp()
    marker = os.path.join(tmp, "child-alive")
    # Background child: touch a marker, hold stdout open, sleep long. Parent also
    # sleeps long and prints NOTHING to stderr → idle watchdog fires.
    body = (
        "( while true; do touch '" + marker + "'; sleep 0.2; done ) &\n"
        "sleep 30\n"
    )
    codex = _fake_codex(tmp, body)
    runner = CodexRunner(codex_bin=codex, env={"OPENAI_API_KEY": "k"}, idle_timeout=1.0)

    start = _time.monotonic()
    r = asyncio.run(runner.run(AgentTask(prompt="x")))
    elapsed = _time.monotonic() - start

    # Returned promptly (well under the stub's 30s sleep) — not wedged.
    assert elapsed < 20, f"run did not return promptly (took {elapsed:.1f}s)"
    assert r.is_error is True
    assert r.status == "timeout"
    # The background child was killed with the group: its marker stops updating.
    mtime0 = os.path.getmtime(marker) if os.path.exists(marker) else 0
    _time.sleep(1.0)
    mtime1 = os.path.getmtime(marker) if os.path.exists(marker) else 0
    assert mtime0 == mtime1, "background child survived the idle-timeout kill (process group not killed)"


# ── long run must NOT be killed by the post-hang drain cap (regression: a5144bc) ─
def test_run_longer_than_drain_cap_completes_when_stderr_flows():
    """A healthy resolve runs for MINUTES while emitting per-tool-call stderr. The
    stdout-drain cap (post_hang_drain_timeout) must bound only the residual drain
    AFTER stderr EOF — never the live run. Regression for a5144bc, which wrapped the
    joint stderr+stdout read in one outer timeout, SIGKILLing every run past the cap
    (exit -9, status=timeout, no IDLE line). Here the stub runs well past the cap,
    emitting stderr within the idle window throughout; it must succeed, not be killed.
    """
    tmp = tempfile.mkdtemp()
    # Emit a stderr line every 0.1s for ~1.5s (15 lines), then write the envelope and
    # exit 0. Total runtime (~1.5s) exceeds post_hang_drain_timeout (0.3s below), but
    # each stderr gap (0.1s) stays under idle_timeout (5s) → NOT a hang.
    body = r'''
out=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-o" ]; then out="$a"; fi
  prev="$a"
done
echo "session id: sess-long" >&2
for i in $(seq 1 15); do echo "codex tool: step $i" >&2; sleep 0.1; done
printf '%s' '{"status":"pr_opened","summary":"done","needs":""}' > "$out"
exit 0
'''
    codex = _fake_codex(tmp, body)
    runner = CodexRunner(
        codex_bin=codex, env={"OPENAI_API_KEY": "k"},
        idle_timeout=5.0, post_hang_drain_timeout=0.3,
    )
    r = asyncio.run(runner.run(AgentTask(prompt="resolve", schema={
        "type": "object", "properties": {"status": {"type": "string"},
        "summary": {"type": "string"}, "needs": {"type": "string"}}})))
    assert r.is_error is False, f"long healthy run was killed: {r.status} / {r.summary}"
    assert r.status == "pr_opened"
    assert r.session_id == "sess-long"


# ── preflight: helpful, actionable "not set up" logging ───────────────────────
def test_preflight_missing_binary_is_actionable():
    r = CodexRunner(codex_bin="definitely-not-a-real-codex-bin-xyz",
                    env={"OPENAI_API_KEY": "k"})
    reason = r.preflight()
    assert reason and "not found on PATH" in reason and "FIXER_RUNTIME=codex" in reason


def test_preflight_missing_key_names_the_var(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    tmp = tempfile.mkdtemp()
    codex = _fake_codex(tmp, "exit 0\n")
    r = CodexRunner(codex_bin=codex)  # no env key
    reason = r.preflight()
    assert reason and "OPENAI_API_KEY is unset" in reason


def test_preflight_ok_when_bin_and_key_present():
    tmp = tempfile.mkdtemp()
    codex = _fake_codex(tmp, "exit 0\n")
    r = CodexRunner(codex_bin=codex, env={"OPENAI_API_KEY": "k"})
    assert r.preflight() is None


def test_run_returns_blocked_result_when_not_ready(monkeypatch):
    """A misconfigured codex runtime fails LOUD (blocked + reason), not opaque."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    logged: list[str] = []
    r = CodexRunner(codex_bin="nope-not-real-bin", log=logged.append)
    res = asyncio.run(r.run(AgentTask(prompt="x")))
    assert res.is_error and res.status == "blocked"
    assert "not found on PATH" in res.summary
    assert any("NOT READY" in m for m in logged)


def test_debug_dumps_codex_stderr_as_tagged_lines():
    """Live stderr streaming: milestone lines (a TEST_CMD/pytest run) are tagged
    and ALWAYS surface; other lines only under debug (`codex|`). A codex run is as
    observable as the SDK path — the visibility the operator asked for."""
    tmp = tempfile.mkdtemp()
    body = r'''
out=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-o" ]; then out="$a"; fi
  prev="$a"
done
echo "exec bash -lc pytest" >&2
echo "tokens used 1234" >&2
printf '%s' '{"status":"done","summary":"ok","needs":""}' > "$out"
exit 0
'''
    codex = _fake_codex(tmp, body)
    logged: list[str] = []
    runner = CodexRunner(codex_bin=codex, env={"OPENAI_API_KEY": "k"},
                         log=logged.append, debug=True)
    asyncio.run(runner.run(AgentTask(prompt="x")))
    joined = "\n".join(logged)
    assert "codex: exec model=" in joined
    # a pytest run is a milestone → tagged TEST_CMD, not the generic verbose tag
    assert "codex TEST_CMD: exec bash -lc pytest" in joined
    assert "codex| tokens used 1234" in joined  # non-milestone line, debug-only
    assert "codex: exit 0" in joined


def test_milestone_lines_surface_without_debug():
    """The key ask: TEST_CMD runs + commits are visible even with debug OFF."""
    tmp = tempfile.mkdtemp()
    body = r'''
out=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-o" ]; then out="$a"; fi
  prev="$a"
done
echo "exec bash -lc 'uv run pytest'" >&2
echo "exec git commit -m 'green: impl'" >&2
echo "some chatty verbose line" >&2
printf '%s' '{"status":"done","summary":"ok","needs":""}' > "$out"
exit 0
'''
    codex = _fake_codex(tmp, body)
    logged: list[str] = []
    runner = CodexRunner(codex_bin=codex, env={"OPENAI_API_KEY": "k"},
                         log=logged.append, debug=False)  # debug OFF
    asyncio.run(runner.run(AgentTask(prompt="x")))
    joined = "\n".join(logged)
    assert "codex TEST_CMD:" in joined         # pytest run surfaced
    assert "codex commit:" in joined           # commit surfaced
    assert "some chatty verbose line" not in joined  # noise stays suppressed


# ── idle watchdog: kill a hung codex subprocess without total-runtime cap ──────
def test_codex_idle_timeout_kills_and_returns_timeout():
    """A codex subprocess that emits one line then goes silent past idle_timeout
    is killed and surfaced as a retryable timeout — not an infinite wedge."""
    tmp = tempfile.mkdtemp()
    # print a line to stderr, then sleep far longer than the idle window.
    body = 'echo "exec bash -lc pytest" >&2\nsleep 30\nexit 0\n'
    codex = _fake_codex(tmp, body)
    logged: list[str] = []
    runner = CodexRunner(codex_bin=codex, env={"OPENAI_API_KEY": "k"},
                         log=logged.append, idle_timeout=0.4)
    r = asyncio.run(runner.run(AgentTask(prompt="x")))
    assert r.is_error and r.status == "timeout"
    assert any("IDLE" in m for m in logged)


def test_codex_no_idle_trip_on_steady_output():
    """Steady stderr output (gaps < idle_timeout) must complete, never trip."""
    tmp = tempfile.mkdtemp()
    body = r'''
out=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-o" ]; then out="$a"; fi
  prev="$a"
done
for i in 1 2 3; do echo "exec step $i" >&2; sleep 0.05; done
printf '%s' '{"status":"done","summary":"ok","needs":""}' > "$out"
exit 0
'''
    codex = _fake_codex(tmp, body)
    runner = CodexRunner(codex_bin=codex, env={"OPENAI_API_KEY": "k"}, idle_timeout=0.5)
    r = asyncio.run(runner.run(AgentTask(prompt="x")))
    assert r.is_error is False and r.status == "done"


# ── _is_transport_drop (pure classifier) ──────────────────────────────────────
def _res(status="error", is_error=True, sid=""):
    return AgentResult(is_error=is_error, status=status, summary="", session_id=sid)


def test_transport_drop_detects_response_failed():
    assert _is_transport_drop(1, "ERROR: stream disconnected before completion: "
                              "response.failed event received", _res()) is True


def test_transport_drop_ignores_clean_exit():
    # exit 0 is never a transport drop, even if stderr mentions the phrase.
    assert _is_transport_drop(0, "response.failed event received", _res()) is False


def test_transport_drop_ignores_real_agent_error():
    # non-zero exit but stderr is a normal test failure, not a stream drop.
    assert _is_transport_drop(1, "2 failed, 10 passed\nTypeError: ...", _res()) is False


def test_transport_drop_never_retries_captured_envelope():
    # A completed turn (blocked/pr_opened) must never be retried, even on nonzero exit.
    assert _is_transport_drop(1, "stream disconnected before completion",
                              _res(status="blocked", is_error=False)) is False
    assert _is_transport_drop(1, "response.failed event received",
                              _res(status="pr_opened", is_error=False)) is False


# ── gateway transport-drop retry loop (run over a fake codex) ─────────────────
def _flaky_codex(tmp, fail_times: int) -> str:
    """A codex stub that emits a gateway stream-drop + exits 1 for the first
    `fail_times` invocations (tracked via a counter file), then succeeds. Also
    prints a session id header so the retry can resume."""
    counter = os.path.join(tmp, "count")
    with open(counter, "w") as f:
        f.write("0")
    body = f'''
out=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-o" ]; then out="$a"; fi
  prev="$a"
done
n=$(cat "{counter}")
echo "session id: sess-$n" >&2
if [ "$n" -lt "{fail_times}" ]; then
  echo $((n+1)) > "{counter}"
  echo "ERROR: stream disconnected before completion: response.failed event received" >&2
  exit 1
fi
printf '%s' '{{"status":"pr_opened","summary":"done","needs":""}}' > "$out"
exit 0
'''
    return _fake_codex(tmp, body)


def test_transport_drop_retries_then_succeeds():
    """Two gateway drops, then success → run() recovers via the retry loop."""
    tmp = tempfile.mkdtemp()
    codex = _flaky_codex(tmp, fail_times=2)
    logged: list[str] = []
    runner = CodexRunner(codex_bin=codex, env={"OPENAI_API_KEY": "k"},
                         log=logged.append, transport_retries=4, backoff_base=0.01)
    r = asyncio.run(runner.run(AgentTask(prompt="x", schema={
        "type": "object", "properties": {"status": {"type": "string"},
        "summary": {"type": "string"}, "needs": {"type": "string"}}})))
    assert r.is_error is False and r.status == "pr_opened"
    joined = "\n".join(logged)
    assert "gateway stream-drop (attempt 1/5)" in joined
    assert "resuming sess-" in joined  # retried with resume, not cold


def test_transport_drop_exhausts_to_timeout():
    """Every attempt drops → run() gives up as a retryable timeout, not error."""
    tmp = tempfile.mkdtemp()
    codex = _flaky_codex(tmp, fail_times=99)  # never succeeds
    logged: list[str] = []
    runner = CodexRunner(codex_bin=codex, env={"OPENAI_API_KEY": "k"},
                         log=logged.append, transport_retries=2, backoff_base=0.01)
    r = asyncio.run(runner.run(AgentTask(prompt="x")))
    assert r.is_error and r.status == "timeout"
    assert "survived 3 attempts" in r.summary  # 1 + 2 retries
    assert any("giving up" in m for m in logged)


def test_real_error_is_not_retried():
    """A non-transport nonzero exit (real failure) must NOT enter the retry loop —
    it returns immediately as the parsed error envelope."""
    tmp = tempfile.mkdtemp()
    body = 'echo "boom: some real error" >&2\nexit 1\n'
    codex = _fake_codex(tmp, body)
    logged: list[str] = []
    runner = CodexRunner(codex_bin=codex, env={"OPENAI_API_KEY": "k"},
                         log=logged.append, transport_retries=4, backoff_base=0.01)
    r = asyncio.run(runner.run(AgentTask(prompt="x")))
    assert r.is_error is True
    assert not any("gateway stream-drop" in m for m in logged)  # loop never engaged


def test_transport_retries_zero_disables_loop():
    """transport_retries=0 → a single drop returns timeout immediately (no retry)."""
    tmp = tempfile.mkdtemp()
    codex = _flaky_codex(tmp, fail_times=99)
    runner = CodexRunner(codex_bin=codex, env={"OPENAI_API_KEY": "k"},
                         transport_retries=0, backoff_base=0.01)
    r = asyncio.run(runner.run(AgentTask(prompt="x")))
    assert r.is_error and r.status == "timeout"
    assert "survived 1 attempts" in r.summary
