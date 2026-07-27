"""CodexRunner — runs a headless agent task on the OpenAI **Codex CLI** instead
of the Claude Agent SDK, so the *fixer* leg can use a different/cheaper model
(e.g. GPT-5.5 via an OpenAI-compatible gateway). Drop-in for `AgentRunner`: same `run(AgentTask) ->
AgentResult` contract, so the domain/orchestration layer is unchanged.

This is the SECOND runtime adapter (see docs/CODEX-RUNNER.md). It speaks to
`codex exec`, which is itself an agentic loop with hooks + sandbox — so the
governor ports as a Codex PreToolUse hook (emitted by `codex_hook` below) and
runs UNMODIFIED. The pure pieces (`build_codex_argv`, `parse_codex_envelope`)
are factored out so they unit-test with no subprocess.

Why `codex exec` and not the OpenAI SDK: the SDK is a chat API, not an agentic
runtime; reusing Codex keeps the tool loop, the pre-tool governor gate, worktree
isolation, and structured output that our fixer depends on.
"""

from __future__ import annotations

import glob
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any

from ..domain.agent_task import AgentRuntime, AgentTask
from ..domain.outcome import AgentResult, ExitReason, Outcome, classify_outcome


# Model the CodexRunner defaults to when neither CODEX_MODEL nor a task's `model`
# (FIX_MODEL) is set. Override for your gateway — behind a proxy this is often the
# provider's deployment/alias string rather than a bare model name.
DEFAULT_CODEX_MODEL = ""

# Codex hardcodes `.git` read-only under --sandbox workspace-write (no config
# knob; OpenAI issues #12280/#14338), which breaks the git-driven fixer. We use
# full-access and rely on the governor hook as the gate — the SAME posture the
# Claude fixer runs in (permission_mode=bypassPermissions + governor).
DEFAULT_SANDBOX = "danger-full-access"

# `codex exec` left alone runs at reasoning effort LOW (observed in live
# session_meta 2026-07-27) — too little thinking for an implementer writing
# tests-first code. Medium is the deliberate default; operators override per
# repo with CODEX_REASONING_EFFORT ("" = leave codex's own default alone).
DEFAULT_REASONING_EFFORT = "medium"


@dataclass(frozen=True)
class CodexProvider:
    """The `-c model_providers.<id>.*` block. `base_url` must point at your
    OpenAI-compatible endpoint; `env_key` names the env var Codex reads the key
    from (we keep the secret in the env, never in argv)."""

    # NOT "openai": codex-cli ≥0.142 reserves the built-in provider IDs (`openai`,
    # etc.) and aborts at config-load ("Built-in providers cannot be overridden")
    # if you try to redefine one under `model_providers`. The id is purely an
    # internal config-table key + `model_provider=` pointer (the model itself is
    # passed via -m), so any non-reserved name works.
    provider_id: str = "openai-custom"
    name: str = "OpenAI"
    base_url: str = ""                 # set to your OpenAI-compatible gateway
    # Codex's convention is OPENAI_API_KEY; behind a proxy you typically alias it
    # to the gateway's key in the agent env (OPENAI_API_KEY=$YOUR_GATEWAY_KEY).
    env_key: str = "OPENAI_API_KEY"
    wire_api: str = "responses"

    def config_overrides(self) -> list[str]:
        """`-c key=value` pairs defining the provider inline (avoids the legacy
        [profiles.x] table, which codex 0.142 rejects with -p).

        A BLANK base_url means "no custom gateway": emit nothing and let codex
        use its built-in OpenAI provider with whatever `codex login` stored
        (ChatGPT-subscription OAuth). Defining the custom provider block in that
        case would force API-key auth and fail preflight on a machine that is
        perfectly logged in."""
        if not self.base_url:
            return []
        p = f"model_providers.{self.provider_id}"
        return [
            f'{p}.name="{self.name}"',
            f'{p}.base_url="{self.base_url}"',
            f'{p}.env_key="{self.env_key}"',
            f'{p}.wire_api="{self.wire_api}"',
            f'model_provider="{self.provider_id}"',
        ]


def _strict_schema(schema: dict) -> dict:
    """Azure OpenAI strict structured-output requires EVERY property listed in
    `required` (no optional keys) and additionalProperties:false at each object.
    Our domain schemas mark some fields optional, so tighten a shallow copy for
    the Codex/azure path. Recurses into nested object properties."""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return schema
    props = schema.get("properties")
    if not isinstance(props, dict):
        return schema
    out = dict(schema)
    out["additionalProperties"] = False
    out["required"] = list(props.keys())
    out["properties"] = {k: _strict_schema(v) for k, v in props.items()}
    return out


def build_codex_argv(
    task: AgentTask,
    *,
    codex_bin: str = "codex",
    provider: CodexProvider | None = None,
    sandbox: str = DEFAULT_SANDBOX,
    default_model: str = DEFAULT_CODEX_MODEL,
    schema_path: str | None = None,
    output_path: str | None = None,
    inject_system_prompt: bool = True,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
) -> list[str]:
    """Pure: AgentTask → `codex exec` argv. No subprocess, no I/O (the caller
    writes the schema/output temp files and passes their paths). Unit-tested.

    `schema_path`: where the caller wrote the (strictified) JSON schema, mapped
    to --output-schema. `output_path`: where codex should write the final
    message (-o), which run() then parses into the envelope."""
    provider = provider or CodexProvider()
    argv: list[str] = [codex_bin, "exec"]

    # `codex exec resume <id>` has a NARROWER argument grammar than plain `codex
    # exec`: it REJECTS `--sandbox` and `-C/--cd` (exit 2, "unexpected argument").
    # It DOES accept -c / -m / --output-schema / -o / --dangerously-bypass-*. So
    # the resume path must not emit the exec-only flags. Sandbox parity on resume
    # is via --dangerously-bypass-approvals-and-sandbox (danger-full-access has no
    # resume flag form); cwd on resume is set on the SUBPROCESS (create_subprocess
    # _exec cwd=), not `-C`, so the resumed turn still runs in the worktree.
    is_resume = bool(task.resume)
    if is_resume:
        argv += ["resume", task.resume]

    # Provider definition + selection (inline -c overrides) — legal on both paths.
    for ov in provider.config_overrides():
        argv += ["-c", ov]

    # Only name a model when one was actually configured: `-m ""` is not "the
    # default", it is a broken flag. With no -m, codex uses the provider's own
    # default — which in OAuth (blank base_url) mode is the subscription's
    # current model, exactly what an operator with no CODEX_MODEL set wants.
    model = task.model or default_model
    if model:
        argv += ["-m", model]

    # `codex exec`'s own default effort is LOW (observed in live session_meta,
    # 2026-07-27) — an implementer tier that quietly under-thinks. Explicit on
    # both paths (-c is legal on exec and resume); "" opts out entirely and
    # leaves codex's default alone.
    #
    # The TASK's effort wins over the process-level knob when it has one (harness
    # #16): `reasoning_effort` is the default for legs with no opinion, and the
    # escalation ladder is an opinion about THIS leg specifically. Legs that
    # carry none (review, triage, the router) keep the knob's value untouched,
    # which is why adding the field changed no existing dispatch.
    effort = task.effort.value if task.effort is not None else reasoning_effort
    if effort:
        argv += ["-c", f"model_reasoning_effort={effort}"]

    if is_resume:
        # danger-full-access equivalent that resume DOES accept.
        argv += ["--dangerously-bypass-approvals-and-sandbox"]
    else:
        argv += ["--sandbox", sandbox]
        if task.cwd:
            argv += ["-C", task.cwd]

    # Headless: no interactive /hooks trust prompt is possible, so trust the
    # configured governor hook for this invocation. This KEEPS the gate on.
    # Legal on both exec and resume.
    argv += ["--dangerously-bypass-hook-trust"]

    if schema_path:
        argv += ["--output-schema", schema_path]
    if output_path:
        argv += ["-o", output_path]

    # AGENT.md parity: Codex has no system-prompt-preset append. Preferred path —
    # the contract is written to the worktree's AGENTS.md (Codex's native
    # per-project instructions; see codex_hook.write_hook_config) and
    # inject_system_prompt is False, so we DON'T also prepend it here (that would
    # duplicate the whole 131-line contract into every prompt). When AGENTS.md
    # isn't in use (inject_system_prompt=True), fall back to prepending.
    prompt = task.prompt
    if inject_system_prompt and task.append_system_prompt:
        prompt = f"{task.append_system_prompt}\n\n---\n\n{prompt}"
    argv.append(prompt)
    return argv


# ── token accounting (review M1 / plan D12) ──────────────────────────────────
# Codex reports no dollar figure — there is no `total_cost_usd` anywhere on the
# `codex exec` path — so tokens × a configured price table is the ONLY substrate
# from which codex spend can reach the ledger. Without it `cost_usd` stays 0.0,
# the ledger write is gated on `cost > 0` (journal.py), and BOTH spend caps are
# blind to every codex run by construction (review M1).
#
# Three stderr shapes seen across codex-cli versions. The first two are matched
# WITHIN one line (never spanning lines), so an "input=" from one trace line and
# an "output=" from another can never be stitched into a fake usage report:
#   "Token usage: total=52341 input=48000 (+ 12000 cached) output=4341"
#   "tokens used: 52341"           (older exec builds — total only, no split)
# The third (codex-cli 0.145.x) puts the count on the NEXT line, so it is the
# one sanctioned two-line pairing: a bare "tokens used" header followed
# IMMEDIATELY by a digits-only line — nothing else can pair across lines:
#   "tokens used\n1,820"
_TOKEN_SPLIT_RE = re.compile(
    r"input[\s_-]*(?:tokens)?[=:\s]+([\d,]+)"
    r"(?:[^\d]{0,20}\+\s*([\d,]+)\s*cached)?"
    r"[^\d]{0,40}?output[\s_-]*(?:tokens)?[=:\s]+([\d,]+)",
    re.IGNORECASE,
)
_TOKEN_TOTAL_RE = re.compile(
    r"tokens?[\s_-]*used[=:\s]+([\d,]+)|token[\s_-]*usage[^\d]{0,20}total[=:\s]+([\d,]+)",
    re.IGNORECASE,
)
_TOKEN_HEADER_RE = re.compile(r"^\s*tokens?[\s_-]*used\s*:?\s*$", re.IGNORECASE)
_TOKEN_BARE_COUNT_RE = re.compile(r"^\s*([\d,]+)\s*$")


def _int_tokens(raw: str | None) -> int:
    if not raw:
        return 0
    try:
        return int(raw.replace(",", ""))
    except (TypeError, ValueError):
        return 0


def parse_codex_usage(stderr: str) -> dict | None:
    """Pure: codex stderr → the token counters, or None when codex printed none.

    Shaped like the SDK usage block (`adapters.journal._USAGE_KEYS`) so the
    journal records codex and Claude runs identically. `total_tokens` is carried
    ONLY on the older total-only line, where no input/output split exists;
    `cache_creation_input_tokens` is always 0 (codex reports no such counter, and
    inventing one would be a fabricated receipt).

    The LAST matching line wins: codex prints its usage line once, at the end,
    and it is cumulative for the session — so a later line supersedes an earlier
    one rather than adding to it.

    Unlike the retry classifiers (see `_stderr_tail`), this deliberately reads
    the WHOLE buffer. The two have opposite worst cases: a missed transport
    signature costs one retry, whereas a missed usage line costs the accounting
    entirely — back to $0.00 and caps that cannot see codex, which is the very
    hole M1 describes. Last-match-wins already makes codex's own trailing line
    beat anything the agent echoed earlier."""
    found: dict | None = None
    header_pending = False
    for line in (stderr or "").splitlines():
        if header_pending:
            header_pending = False
            m3 = _TOKEN_BARE_COUNT_RE.match(line)
            if m3:
                found = {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "total_tokens": _int_tokens(m3.group(1)),
                }
                continue
        m = _TOKEN_SPLIT_RE.search(line)
        if m:
            found = {
                "input_tokens": _int_tokens(m.group(1)),
                "output_tokens": _int_tokens(m.group(3)),
                "cache_read_input_tokens": _int_tokens(m.group(2)),
                "cache_creation_input_tokens": 0,
            }
            continue
        m2 = _TOKEN_TOTAL_RE.search(line)
        if m2:
            found = {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "total_tokens": _int_tokens(m2.group(1) or m2.group(2)),
            }
            continue
        header_pending = bool(_TOKEN_HEADER_RE.match(line))
    return found


def read_rollout_usage(session_id: str, codex_home: str | None = None) -> dict | None:
    """Exact token counters from codex's own session rollout file, or None.

    codex-cli persists `token_count` events (with a cumulative
    `total_token_usage` split) to
    `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<session_id>.jsonl`. This is
    the PREFERRED usage source: the stderr line degraded to a total-only shape
    in codex-cli 0.145 and a total priced at max(rate_in, rate_out) counts
    cached reads at the $30/MTok output rate — a single warm resolve (~1.6M
    total, ~99% cached input) would ledger ≈$48 and spuriously trip the $50
    issue cap, where the real split prices it ≈$8.

    Last `token_count` wins: the counter is cumulative for the session, same
    convention as the stderr line (see parse_codex_usage). Any failure — no
    session id, no file, unparseable lines — reads as None so the caller falls
    back to stderr parsing; losing precision must never lose the run.
    """
    if not session_id:
        return None
    home = codex_home or os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    try:
        pattern = os.path.join(
            home, "sessions", "**", f"rollout-*-{session_id}.jsonl"
        )
        paths = glob.glob(pattern, recursive=True)
        if not paths:
            return None
        found: dict | None = None
        with open(max(paths, key=os.path.getmtime)) as f:
            for line in f:
                if '"token_count"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                payload = event.get("payload") or {}
                if payload.get("type") != "token_count":
                    continue
                total = (payload.get("info") or {}).get("total_token_usage") or {}
                if total:
                    found = total
        if not found:
            return None
        return {
            "input_tokens": int(found.get("input_tokens", 0) or 0),
            "output_tokens": int(found.get("output_tokens", 0) or 0),
            "cache_read_input_tokens": int(found.get("cached_input_tokens", 0) or 0),
            "cache_creation_input_tokens": int(
                found.get("cache_write_input_tokens", 0) or 0
            ),
        }
    except Exception:  # noqa: BLE001 — accounting fallback must never kill a run
        return None


def usage_delta(end: dict | None, start: dict | None) -> dict | None:
    """Pure: end-of-run counters minus a pre-run baseline, floored at zero.

    A follow-up leg (fix-ci/address-review/rebase) RESUMES the resolve's codex
    session, and the rollout's `total_token_usage` is cumulative for the whole
    session — so pricing the raw counter re-ledgers every token the previous
    leg already paid for (observed 2026-07-27: a 114s fix-ci ledgered $39.57
    because it carried its resolve's 6M tokens). The ledger wants per-leg
    spend; the delta is that. `None` end stays None (no usage reported); a
    missing baseline means the run started its own session — nothing to
    subtract."""
    if not isinstance(end, dict):
        return None
    if not isinstance(start, dict):
        return end
    keys = set(end) | set(start)
    return {
        k: max(0, int(end.get(k, 0) or 0) - int(start.get(k, 0) or 0)) for k in keys
    }


def codex_cost_usd(
    usage: dict | None, *, cost_per_mtok_in: float, cost_per_mtok_out: float
) -> float:
    """Pure: token counters × the configured USD-per-MILLION-token rates.

    Both rates 0 → 0.0: an operator who zeroes the price table opts out of
    codex spend tracking entirely, and gets a loud "the caps cannot see this
    runtime" warning instead (see `codex_cost_blindspot_warning`). HarnessConfig
    ships non-zero list-price defaults, so this is an explicit choice, not the
    common path.

    An older codex that printed only a TOTAL is priced at the HIGHER of the two
    rates. Caps exist to stop runaway spend, so the only safe rounding direction
    is over-estimation — an under-counted cap is a cap that never fires."""
    if not isinstance(usage, dict):
        return 0.0
    rate_in = max(float(cost_per_mtok_in or 0.0), 0.0)
    rate_out = max(float(cost_per_mtok_out or 0.0), 0.0)
    if rate_in <= 0 and rate_out <= 0:
        return 0.0
    tokens_in = int(usage.get("input_tokens", 0) or 0)
    tokens_out = int(usage.get("output_tokens", 0) or 0)
    if tokens_in or tokens_out:
        # Cached input reads are already inside `input_tokens` on the codex line
        # (it prints "input=N (+ M cached)"), so they are NOT added again.
        return (tokens_in * rate_in + tokens_out * rate_out) / 1_000_000.0
    total = int(usage.get("total_tokens", 0) or 0)
    return total * max(rate_in, rate_out) / 1_000_000.0


def codex_cost_blindspot_warning(
    *,
    cost_per_mtok_in: float,
    cost_per_mtok_out: float,
    issue_spend_cap_usd: float,
    daily_spend_cap_usd: float,
    max_runtime: float = 0.0,
) -> str | None:
    """The ONE startup line D12 requires: with a spend cap armed but no codex
    price table, every codex run costs $0.00 on the ledger and neither cap can
    ever fire. Returns None (silence) when the rates ARE configured or when no
    cap is armed — there is nothing to mislead the operator about. The shipped
    defaults are non-zero, so reaching this warning means someone zeroed them.

    Both rates must be >0 to count as configured: pricing only the input side
    still under-counts every run, which is the same blind spot in weaker form.

    The second half is D12's other half: `build_codex_argv` has no analogue of
    the SDK's per-session budget (`AgentTask.max_budget_usd`), so once dispatch
    starts, the wall cap is the only thing bounding one run's spend."""
    if cost_per_mtok_in > 0 and cost_per_mtok_out > 0:
        return None
    if issue_spend_cap_usd <= 0 and daily_spend_cap_usd <= 0:
        return None
    caps = ", ".join(
        part
        for part in (
            f"issue=${issue_spend_cap_usd:.2f}" if issue_spend_cap_usd > 0 else "",
            f"daily=${daily_spend_cap_usd:.2f}" if daily_spend_cap_usd > 0 else "",
        )
        if part
    )
    bound = (
        f"the wall cap (CODEX_MAX_RUNTIME_SECONDS={max_runtime:.0f}s)"
        if max_runtime > 0
        else "NOTHING (CODEX_MAX_RUNTIME_SECONDS is unset — set it)"
    )
    return (
        "codex: WARNING — spend caps are armed "
        f"({caps}) but CODEX_COST_PER_MTOK_IN/OUT are 0, so every codex run "
        "records $0.00 and NEITHER cap can ever fire. Set both to your gateway's "
        "USD-per-million-token prices. Note also that codex has no per-session "
        f"budget flag (MAX_BUDGET_USD is a Claude-SDK-only bound), so {bound} "
        "limits a single run. (FIXER_RUNTIME=codex)"
    )


def parse_codex_envelope(
    *,
    exit_code: int,
    last_message: str,
    session_id: str = "",
    stderr: str = "",
    cost_per_mtok_in: float = 0.0,
    cost_per_mtok_out: float = 0.0,
    usage: dict | None = None,
) -> AgentResult:
    """Pure: codex exec result → AgentResult. Mirrors result_to_agent_result —
    decision read from the ENVELOPE (the -o JSON), not the exit code. is_error
    only when there is no parseable envelope AND exit was non-zero, OR the
    envelope itself signals an error status.

    Token usage: a caller-supplied `usage` (the exact split from the session
    rollout file, see read_rollout_usage) wins; otherwise it is read off
    `stderr` and priced with the caller's rates — review M1: this is what makes
    codex spend visible to the ledger and therefore to both spend caps. A run
    that DIED still burned tokens, so usage/cost ride on every return path, not
    just the happy one."""
    structured: dict | None = None
    raw = (last_message or "").strip()
    if raw:
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                structured = obj
        except (json.JSONDecodeError, ValueError):
            structured = None

    usage = usage or parse_codex_usage(stderr)
    cost = codex_cost_usd(
        usage, cost_per_mtok_in=cost_per_mtok_in, cost_per_mtok_out=cost_per_mtok_out
    )

    if structured is None:
        # No envelope. If codex also exited non-zero, it's a genuine failure;
        # otherwise surface the free text as the summary (best-effort).
        if exit_code != 0:
            return AgentResult(
                is_error=True,
                summary=(stderr.strip() or raw or f"codex exec exit {exit_code}")[:2000],
                session_id=session_id,
                cost_usd=cost,
                usage=usage,
                exit_reason=ExitReason.AGENT_ERROR,
            )
        return AgentResult(
            is_error=False, summary=raw[:2000], session_id=session_id,
            cost_usd=cost, usage=usage,
            exit_reason=ExitReason.OK,
        )

    status = str(structured.get("status", "") or "")
    summary = str(structured.get("summary", "") or "")
    needs = str(structured.get("needs", "") or "")
    is_error = status == "error"
    return AgentResult(
        is_error=is_error,
        status=status,
        summary=summary,
        needs=needs,
        # Priced from the token counters codex prints (review M1): 0.0 only when
        # no rates are configured or codex printed no usage line.
        cost_usd=cost,
        usage=usage,
        session_id=session_id,
        structured=structured,
        # A parsed envelope means the agentic loop reached its own terminal —
        # including `blocked`, which is a normal completion, not a failure to run.
        exit_reason=ExitReason.AGENT_ERROR if is_error else ExitReason.OK,
    )


@dataclass
class CodexRunner(AgentRuntime):
    """Runs fixer tasks over `codex exec`. `provider`/`sandbox`/`model` are
    process-level config; the governor must already be installed in the task's
    cwd as a Codex hook (see `codex_hook.write_hook_config`). All subprocess/IO
    lives in `run`; `build_codex_argv` + `parse_codex_envelope` stay pure."""

    codex_bin: str = "codex"
    provider: CodexProvider = field(default_factory=CodexProvider)
    sandbox: str = DEFAULT_SANDBOX
    default_model: str = DEFAULT_CODEX_MODEL
    reasoning_effort: str = DEFAULT_REASONING_EFFORT
    env: dict[str, str] = field(default_factory=dict)
    # When the AGENT.md contract is delivered via the worktree AGENTS.md (the
    # default for the fix daemon), set False so the prompt isn't ALSO prepended
    # with it. True keeps the standalone prepend (e.g. tests / no-AGENTS.md use).
    inject_system_prompt: bool = True
    # Diagnostics: `log` is a str->None sink (stderr in prod). codex stderr is
    # streamed LIVE (not buffered to exit) — milestone lines (TEST_CMD runs,
    # commits, patches) always surface; `debug` adds the full verbose trace.
    log: Any = None
    debug: bool = False
    # Idle watchdog: max seconds between codex stderr events before we call it a
    # hang and kill the subprocess (not a total-runtime cap). None disables.
    # Measured against the WALL clock (see wall_clock) so it survives laptop sleep —
    # the monotonic clock freezes during macOS suspend, which would otherwise delay
    # post-wake recovery by a full idle window (same fix as the Claude AgentRunner).
    idle_timeout: float | None = None
    # Total-runtime ceiling, the cap the idle watchdog cannot provide: a gateway
    # stream that "limps" (a stderr line every few minutes) resets the idle clock
    # forever and runs unbounded (FAILURE-MODES #60). Only elapsed wall time bounds
    # that. Enforced in the same stderr pump, with the same killpg teardown. None
    # disables. See AGENT_MAX_RUNTIME_SECONDS.
    max_runtime: float | None = None
    # Injected wall clock (time.time) + poll slice for the idle watchdog: we wait for
    # the next stderr line in `poll_slice` chunks and re-check wall-clock elapsed each
    # chunk, so a sleep-spanning gap trips promptly on wake rather than after a fresh
    # monotonic window. Injectable for tests.
    wall_clock: Any = None
    poll_slice: float = 30.0
    # Post-stderr stdout-drain cap: once _pump_stderr has finished (EOF or idle
    # kill), stdout normally hits EOF at once — but a descendant that survived the
    # killpg and still holds the pipe could block the read forever. This bounds ONLY
    # that residual drain; it is NOT a ceiling on the live run (the idle watchdog is).
    # Active only when idle_timeout is set (matches the watchdog being enabled).
    post_hang_drain_timeout: float = 30.0
    # Gateway transport-drop retry. A `response.failed` / `stream disconnected`
    # from the LLM gateway is upstream, not an agent error; codex's own in-TCP
    # reconnects don't recover it, but a fresh `codex exec` (resumed) does. Retry
    # up to `transport_retries` times with jittered exponential backoff
    # (delay = backoff_base * 2**attempt + U[0, delay/2]). 0 disables the loop.
    transport_retries: int = 4
    backoff_base: float = 2.0
    # Rate-limit (HTTP 429) backoff — DISTINCT from the network-drop backoff above.
    # A 429 must wait for the provider's throttle window to renew (Azure GPT-5.5:
    # 60s), not the ~2s network base, or every retry lands inside the same throttled
    # window and fails (observed: 345 rebase burned all 5 retries on a 60s window).
    # We honour a parsed Retry-After when present, else wait rate_limit_backoff_base
    # * (attempt+1), capped at max_rate_limit_wait. A rate-limited attempt does NOT
    # consume the transport-retry budget (it's provider pushback, not a flaky
    # connection) — bounded instead by max_rate_limit_retries.
    rate_limit_backoff_base: float = 60.0
    max_rate_limit_wait: float = 180.0
    max_rate_limit_retries: int = 5
    # Price table for codex spend (review M1 / D12), USD per MILLION tokens. Both
    # 0 (the default) → cost_usd stays 0.0 and codex runs never reach the ledger,
    # which is exactly the blind spot `codex_cost_blindspot_warning` announces at
    # startup. Set them and every codex run becomes visible to both spend caps.
    cost_per_mtok_in: float = 0.0
    cost_per_mtok_out: float = 0.0

    def __post_init__(self) -> None:
        self._log = self.log or (lambda _m: None)
        self._wall_clock = self.wall_clock or time.time

    def _pump_slice(self) -> float:
        """How long one stderr-readline chunk blocks. The poll slice floored by
        whichever caps are armed, so a cap SHORTER than the slice still trips on
        time instead of waiting out a full slice."""
        caps = [self.poll_slice]
        if self.idle_timeout is not None:
            caps.append(self.idle_timeout)
        if self.max_runtime is not None:
            caps.append(self.max_runtime)
        return min(caps)

    def _log_codex_line(self, line: str) -> None:
        """Classify one live stderr line from `codex exec` and log it. Codex is a
        Node subprocess (no Python API), so its stderr is our only window into the
        agentic loop. MILESTONE lines (a TEST_CMD/pytest run, a commit, a patch,
        an exec) are ALWAYS surfaced — the visibility the operator actually needs;
        everything else is logged only under debug (verbose full trace)."""
        s = line.strip()
        if not s:
            return
        low = s.lower()
        # Milestones worth seeing even without debug: test runs, commits, patches.
        is_test = ("pytest" in low or "tsc -b" in low or "test_cmd" in low
                   or "uv run" in low and "test" in low)
        is_commit = "git commit" in low or low.startswith(("plan:", "red:", "green:", "docs:"))
        is_patch = "apply_patch" in low or low.startswith("exec ")
        if is_test:
            self._log(f"codex TEST_CMD: {s[:200]}")
        elif is_commit:
            self._log(f"codex commit: {s[:200]}")
        elif is_patch:
            self._log(f"codex tool: {s[:200]}")
        elif self.debug:
            self._log(f"codex| {s[:200]}")

    def preflight(self) -> str | None:
        """Return a human-readable reason the Codex runtime is NOT usable, or None
        if it looks ready. Checked once at startup so a misconfig is a clear log
        line, not an opaque per-issue `codex exec exit 1`. Non-fatal: the caller
        logs it; the daemon still starts (the operator can fix the env and the
        next dispatch works)."""
        import shutil

        if shutil.which(self.codex_bin) is None:
            return (
                f"codex binary '{self.codex_bin}' not found on PATH — install the "
                "Codex CLI or set CODEX_BIN. (FIXER_RUNTIME=codex)"
            )
        key_var = self.provider.env_key
        proc_env = {**os.environ, **self.env}
        if not self.provider.base_url:
            # OAuth mode (no custom gateway): codex authenticates from the
            # credentials `codex login` stored. An env key ALSO satisfies it
            # (codex accepts either), so only the absence of both is fatal.
            auth_file = os.path.join(os.path.expanduser("~"), ".codex", "auth.json")
            if proc_env.get(key_var) or os.path.exists(auth_file):
                return None
            return (
                f"codex has no credentials — no {auth_file} (run `codex login`) "
                f"and {key_var} is unset. (FIXER_RUNTIME=codex, default provider)"
            )
        if not proc_env.get(key_var):
            return (
                f"{key_var} is unset — the Codex fixer cannot authenticate to "
                f"{self.provider.base_url}. Export {key_var} (or ANTHROPIC_AUTH_TOKEN, "
                "which shared.env aliases) in the launching shell. (FIXER_RUNTIME=codex)"
            )
        return None

    async def run(self, task: AgentTask) -> AgentResult:
        # Fail LOUD, not opaque: if the runtime isn't set up, surface the exact
        # reason as the result summary (and log it) instead of a bare exit-1.
        reason = self.preflight()
        if reason is not None:
            self._log(f"codex: NOT READY — {reason}")
            return AgentResult(is_error=True, status="blocked", summary=reason)

        # Review M17: the per-run scratch dir used to be an un-removed mkdtemp, so
        # a long-lived daemon leaked one `codex-run-*` directory (schema + the last
        # envelope) per dispatch into /tmp. TemporaryDirectory removes it on EVERY
        # exit path below — return, raise, or cancellation. `ignore_cleanup_errors`
        # keeps a stray file codex left behind from turning teardown into a failed
        # run; a leaked temp file must never be the reason an issue doesn't resolve.
        with tempfile.TemporaryDirectory(
            prefix="codex-run-", ignore_cleanup_errors=True
        ) as tmpdir:
            return await self._run_in(task, tmpdir)

    async def _run_in(self, task: AgentTask, tmpdir: str) -> AgentResult:
        """The retry loop, given a scratch dir whose lifetime `run` owns."""
        import asyncio
        import random
        from dataclasses import replace

        schema_path = None
        if task.schema is not None:
            schema_path = os.path.join(tmpdir, "schema.json")
            with open(schema_path, "w") as f:
                json.dump(_strict_schema(task.schema), f)
        output_path = os.path.join(tmpdir, "last.json")

        # Transport-retry loop. A gateway stream drop (`response.failed` /
        # `stream disconnected before completion`) is an UPSTREAM failure, not an
        # agent error — codex's own 5× SSE reconnects fire within one dead TCP
        # session and don't help. A fresh `codex exec` gets a fresh connection, so
        # we retry the whole invocation with jittered exponential backoff. We
        # RESUME the session (via the parsed session id) rather than restart cold:
        # codex persists its rollout only at `task_complete` turn boundaries, so a
        # mid-generation drop never poisons the session — resume replays consistent
        # state, and worktree git commits already landed persist regardless. A hang
        # (idle-watchdog) is NOT retried here (different failure — see below); only
        # transport drops are. Exhausting retries surfaces status=timeout (retryable
        # → the daemon re-dispatches cold).
        # Two INDEPENDENT retry budgets: a flaky connection (network drop) and
        # provider rate-limiting (HTTP 429) are different failures with different
        # recoveries, so a 429 storm must not exhaust the network-drop budget and
        # vice versa. `net_attempts` counts network drops; `rl_attempts` counts
        # rate-limits. Both carry the session id forward so the retry RESUMES.
        resume_id = task.resume
        # Per-leg accounting baseline: tokens the resumed session had ALREADY
        # burned (and a previous leg already ledgered) before this run started.
        # Captured once — retries inside this run resume a session whose earlier
        # attempts were never ledgered, so their tokens must stay in the total.
        baseline_sid = task.resume or ""
        baseline_usage = read_rollout_usage(baseline_sid) if baseline_sid else None
        net_attempts = 0
        rl_attempts = 0
        while True:
            # Review M17: truncate the envelope BEFORE each attempt. Codex writes
            # `-o` only when a turn completes, so a retry after a mid-turn drop
            # would otherwise re-read the PREVIOUS attempt's envelope and attribute
            # it to this one — reporting a stale success (or a stale `blocked`) for
            # a run that produced nothing.
            _unlink_quietly(output_path)
            run_task = replace(task, resume=resume_id) if resume_id else task
            argv = build_codex_argv(
                run_task,
                codex_bin=self.codex_bin,
                provider=self.provider,
                sandbox=self.sandbox,
                default_model=self.default_model,
                schema_path=schema_path,
                output_path=output_path,
                inject_system_prompt=self.inject_system_prompt,
                reasoning_effort=self.reasoning_effort,
            )
            result, exit_code, stderr, stdout, hung = await self._exec_once(
                argv, run_task, output_path,
                usage_baseline=(baseline_sid, baseline_usage) if baseline_usage else None,
            )
            if hung:
                # A watchdog fired — surface as a retryable timeout, not a
                # blocked verdict (the daemon leaves the issue ai-working →
                # re-dispatch). A hang is not a transport drop; don't loop here.
                if result.exit_reason is ExitReason.WALL_TIMEOUT:
                    return AgentResult(
                        is_error=True, status="timeout",
                        exit_reason=ExitReason.WALL_TIMEOUT,
                        summary=(
                            f"codex exceeded max runtime {self.max_runtime:.0f}s; killed"
                        ),
                    )
                return AgentResult(
                    is_error=True, status="timeout",
                    exit_reason=ExitReason.IDLE_TIMEOUT,
                    summary=f"codex idle >{self.idle_timeout:.0f}s (no stderr event); killed",
                )

            if not _is_transport_drop(exit_code, stderr, result):
                return result  # success, genuine error, or blocked — never retry over output

            # Carry the session id forward so the retry RESUMES rather than restarts.
            resume_id = result.session_id or resume_id

            if _is_rate_limited(stderr):
                # Provider pushback (429): wait for the throttle window to renew.
                # Honour a parsed Retry-After; else back off aligned to the window
                # (rate_limit_backoff_base * (attempt+1)), capped. Does NOT consume
                # the network-drop budget.
                if rl_attempts >= self.max_rate_limit_retries:
                    self._log(
                        f"codex: rate limit (429) persisted after "
                        f"{rl_attempts + 1} attempt(s) — giving up (retryable timeout)"
                    )
                    return AgentResult(
                        is_error=True, status="timeout",
                        # Upstream refused to serve us at all — the agent never got a
                        # turn, so this is transport, not an agent attempt.
                        exit_reason=ExitReason.TRANSPORT_DEATH,
                        summary=(
                            "codex hit provider rate limit (HTTP 429) that survived "
                            f"{rl_attempts + 1} attempts; retryable"
                        ),
                    )
                hinted = _extract_retry_after_seconds(stderr)
                delay = hinted if hinted is not None else (
                    self.rate_limit_backoff_base * (rl_attempts + 1)
                )
                delay = min(delay, self.max_rate_limit_wait)
                delay += random.uniform(0, delay / 4)  # jitter: de-sync fleet retries
                rl_attempts += 1
                self._log(
                    f"codex: rate limited (429) — attempt {rl_attempts}/"
                    f"{self.max_rate_limit_retries}; "
                    f"{'resuming ' + resume_id if resume_id else 'restarting'} in "
                    f"{delay:.1f}s{' (Retry-After)' if hinted is not None else ' (window-aligned)'}"
                )
                await asyncio.sleep(delay)
                continue

            # Plain network drop: short jittered exponential backoff, fresh connection.
            if net_attempts >= self.transport_retries:
                self._log(
                    f"codex: gateway stream-drop persisted after "
                    f"{net_attempts + 1} attempt(s) — giving up (retryable timeout)"
                )
                return AgentResult(
                    is_error=True, status="timeout",
                    # Every attempt died on the wire before a turn completed — the
                    # textbook TRANSPORT_DEATH: no attempt was made, so no breaker.
                    exit_reason=ExitReason.TRANSPORT_DEATH,
                    summary=(
                        "codex gateway stream drop (response.failed) survived "
                        f"{net_attempts + 1} attempts; retryable"
                    ),
                )
            delay = self.backoff_base * (2 ** net_attempts)
            delay += random.uniform(0, delay / 2)  # jitter: de-sync flapping gateway
            net_attempts += 1
            self._log(
                f"codex: gateway stream-drop (attempt {net_attempts}/"
                f"{self.transport_retries + 1}); "
                f"{'resuming ' + resume_id if resume_id else 'restarting'} "
                f"in {delay:.1f}s"
            )
            await asyncio.sleep(delay)

    async def _exec_once(
        self, argv: list[str], task: AgentTask, output_path: str,
        usage_baseline: tuple[str, dict] | None = None,
    ) -> tuple[AgentResult, int, str, str, bool]:
        """One `codex exec` invocation: spawn, stream stderr live, parse the
        envelope. Returns (result, exit_code, stderr, stdout, hung) so run()'s
        retry loop can classify a transport drop. All subprocess/IO is here; the
        loop above owns only retry policy."""
        import asyncio

        proc_env = {**os.environ, **self.env, **task.env}
        self._log(f"codex: exec model={task.model or self.default_model} cwd={task.cwd or '.'}")
        # Set cwd on the subprocess too, not only via `-C`: the resume subcommand
        # rejects `-C`, so this is how a resumed run still executes in the worktree
        # (harmless on the exec path — it matches the `-C` we also pass there).
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,  # codex waits on stdin otherwise
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=proc_env,
            cwd=task.cwd or None,
            # New session → codex and ALL its descendants (the TEST_CMD it spawns,
            # e.g. `scripts/e2e-local.sh` → a vite/playwright server) share one
            # process GROUP, so the idle-watchdog can kill the whole tree. Without
            # this, killing only the codex pid orphaned a background test server
            # that inherited the stdout pipe → _read_stdout()/proc.wait() never
            # returned → the daemon wedged (observed on Myapp #310, tsc-hung e2e).
            start_new_session=True,
        )

        # Stream stderr LIVE rather than buffering to exit: codex exec emits its
        # per-tool-call trace (exec/apply_patch/tokens) to stderr, so live-reading
        # it is the only mid-run visibility we get (the whole resolve is one
        # subprocess). Each governed tool call — notably a TEST_CMD run — is logged
        # as it happens. stdout (small: the header) is drained concurrently so a
        # full pipe can't deadlock the child. The readline is bounded by the idle
        # watchdog: a gap > idle_timeout between stderr events is a hang → kill.
        err_chunks: list[str] = []
        hung = False
        wall_tripped = False
        # Wall-cap baseline, read ONLY when the cap is armed (the clock is injectable
        # and a spurious read would shift a test's fake sequence).
        started_at = self._wall_clock() if self.max_runtime is not None else 0.0

        async def _pump_stderr() -> None:
            nonlocal hung, wall_tripped
            assert proc.stderr is not None
            while True:
                # Between-lines wall check. A chatty run never times out a chunk, so
                # the in-chunk check alone would leave a runaway session that keeps
                # printing tool traces unbounded — the spend hole the cap closes.
                if self.max_runtime is not None:
                    elapsed = self._wall_clock() - started_at
                    if elapsed >= self.max_runtime:
                        hung = True
                        wall_tripped = True
                        self._log(
                            f"codex: WALL — total runtime {elapsed:.0f}s exceeded "
                            f"max {self.max_runtime:.0f}s; killing subprocess"
                        )
                        _kill_quietly(proc)
                        return
                if self.idle_timeout is None and self.max_runtime is None:
                    raw = await proc.stderr.readline()
                    if not raw:  # EOF
                        return
                    line = raw.decode("utf-8", "replace").rstrip("\n")
                    err_chunks.append(line)
                    self._log_codex_line(line)
                    continue
                # Wall-clock watchdogs: wait for the next line in poll_slice chunks,
                # re-checking elapsed WALL time each chunk. The monotonic clock
                # freezes across macOS sleep, so a one-shot wait_for spanning a
                # suspend would not count the sleep — polling wall time makes a
                # sleep-spanning silence trip on the first chunk after wake. Both caps
                # share this loop; each is checked only when armed, so arming only the
                # wall cap must not fall through to the unbounded readline above.
                last_line = self._wall_clock()
                got = False
                while True:
                    try:
                        raw = await asyncio.wait_for(
                            proc.stderr.readline(), timeout=self._pump_slice()
                        )
                        got = True
                        break
                    except asyncio.TimeoutError:
                        # ONE clock read per chunk, shared by both checks.
                        now = self._wall_clock()
                        idle = now - last_line
                        if self.idle_timeout is not None and idle >= self.idle_timeout:
                            hung = True
                            self._log(
                                f"codex: IDLE — no stderr event for {idle:.0f}s "
                                f"(>{self.idle_timeout:.0f}s); treating as hung, "
                                "killing subprocess"
                            )
                            _kill_quietly(proc)
                            return
                        elapsed = now - started_at
                        if self.max_runtime is not None and elapsed >= self.max_runtime:
                            # Limping stream: stderr keeps arriving so the idle
                            # watchdog never fires, but the run has outlived any
                            # plausible task. Same killpg teardown — the whole process
                            # GROUP, or a leaked test server holds the pipe forever.
                            hung = True
                            wall_tripped = True
                            self._log(
                                f"codex: WALL — total runtime {elapsed:.0f}s exceeded "
                                f"max {self.max_runtime:.0f}s; killing subprocess"
                            )
                            _kill_quietly(proc)
                            return
                        # chunk elapsed but still inside both windows — keep waiting
                if not got or not raw:  # EOF
                    return
                line = raw.decode("utf-8", "replace").rstrip("\n")
                err_chunks.append(line)
                self._log_codex_line(line)

        async def _read_stdout() -> str:
            assert proc.stdout is not None
            try:
                return (await proc.stdout.read()).decode("utf-8", "replace")
            except Exception:
                return ""

        # Drain stdout CONCURRENTLY with the live stderr pump — a full stdout pipe
        # would otherwise deadlock the child — but bound each read SEPARATELY:
        #
        #   - _pump_stderr self-bounds via the per-line idle watchdog: a genuine hang
        #     (no stderr for idle_timeout) → kill. A healthy resolve runs for MINUTES
        #     while emitting per-tool-call stderr, so it must NOT be capped by any
        #     total-runtime ceiling. (A single outer timeout on the joint read did
        #     exactly that — killing every run > 30s with exit -9 and no IDLE line.)
        #   - _read_stdout is capped only for the drain that REMAINS once stderr is
        #     done: normally stdout hits EOF the instant codex exits, but a descendant
        #     that survived the killpg and still holds the pipe could block the read
        #     forever and wedge the poll loop. The 30s cap bounds that leaked-fd case.
        stdout_task = asyncio.ensure_future(_read_stdout())
        await _pump_stderr()  # self-bounded by the idle watchdog; no outer cap
        try:
            stdout = await asyncio.wait_for(
                stdout_task,
                timeout=(
                    self.post_hang_drain_timeout
                    if (self.idle_timeout or self.max_runtime)
                    else None
                ),
            )
        except asyncio.TimeoutError:
            hung = True
            _kill_quietly(proc)
            stdout_task.cancel()
            try:
                await stdout_task
            except (asyncio.CancelledError, Exception):
                pass
            stdout = ""
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            pass  # reaped best-effort; the group was killed
        exit_code = proc.returncode or 0
        stderr = "\n".join(err_chunks)
        self._log(f"codex: exit {exit_code} (stderr {len(stderr)}B, out {len(stdout)}B)")
        if exit_code != 0 and stderr:
            # The size of the stderr is not a diagnosis. The 0.3s cross-runtime
            # resume failure ("no rollout found") sat invisible behind
            # "stderr 125B" until reproduced by hand — on a nonzero exit the
            # tail IS the reason, so log it.
            self._log(f"codex: stderr tail: {_stderr_tail(stderr, 3)[:500]}")

        if hung:
            # Carry WHICH watchdog fired on the result so run() can phrase the
            # summary without widening this tuple further.
            return (
                AgentResult(
                    is_error=True, status="timeout", summary="hung",
                    exit_reason=(
                        ExitReason.WALL_TIMEOUT if wall_tripped
                        else ExitReason.IDLE_TIMEOUT
                    ),
                ),
                exit_code, stderr, stdout, True,
            )

        last_message = ""
        try:
            with open(output_path) as f:
                last_message = f.read()
        except OSError:
            # codex didn't write -o (e.g. errored early) → fall back to stdout.
            last_message = stdout

        # `codex exec` prints its `session id: <uuid>` header to STDERR (the -o
        # file holds only the final message). Check stderr first, stdout second.
        session_id = _session_id_from_lines(stderr) or _session_id_from_lines(stdout)
        rollout_usage = read_rollout_usage(session_id)
        # A resumed session's rollout counter is cumulative; subtract what the
        # previous leg already ledgered (see usage_delta). Only when this run
        # really continued THAT session — a resume that fell back to a fresh
        # session has nothing pre-paid in its counter.
        if usage_baseline is not None and session_id == usage_baseline[0]:
            rollout_usage = usage_delta(rollout_usage, usage_baseline[1])
        result = parse_codex_envelope(
            exit_code=exit_code,
            last_message=last_message,
            session_id=session_id,
            stderr=stderr,
            cost_per_mtok_in=self.cost_per_mtok_in,
            cost_per_mtok_out=self.cost_per_mtok_out,
            usage=rollout_usage,
        )
        if exit_code < 0 and not hung:
            # A NEGATIVE returncode means terminated by signal -exit_code, and we did
            # not do it (our own killpg paths set `hung` and returned above). So this
            # is the OOM/jetsam kill of FAILURE-MODES #58, an operator `kill`, or a
            # Ctrl-C that reached the process group — infra death, not a failed
            # attempt. The domain reads exit_reason to skip the breaker bump and the
            # ai-ready relabel that raced a manual ai-blocked (#65).
            from dataclasses import replace as _replace

            self._log(
                f"codex: killed by signal {-exit_code} (not by our watchdog) — "
                "treating as infra death, not an agent attempt"
            )
            result = _replace(result, exit_reason=ExitReason.KILLED_EXTERNALLY)
        return result, exit_code, stderr, stdout, False


# Gateway stream-drop signatures codex prints to stderr when the upstream LLM
# connection fails mid-generation (SSE `response.failed`, or the socket dropping
# before the response completes). These are UPSTREAM/transport failures — a
# retry on a fresh connection is the right recovery, distinct from a real agent
# error (bad code, failed tests) or a deliberate `blocked`.
_TRANSPORT_DROP_SIGNATURES = (
    "stream disconnected before completion",
    "response.failed event received",
)

# Rate-limit signatures — a SUBSET of transport drops that need a DIFFERENT
# recovery. Azure/OpenAI surface HTTP 429 as `response.failed` mid-stream, so it
# trips the transport-drop path, but the network-drop backoff (base ~2s: 2→5→11→
# 22s) retries entirely INSIDE the provider's throttle window (observed: Azure
# GPT-5.5 uses a 60s renewal period — `x-ratelimit-renewalperiod-*: 60`), so all
# attempts fail. A 429 must back off ALIGNED to that window, honouring Retry-After
# when present. Matched on the provider's own text (verified against an Azure
# OpenAI gateway response body).
_RATE_LIMIT_SIGNATURES = (
    "rate limit",
    "too many requests",
    "rate_limit_exceeded",
    "too_many_requests",
    "usage limit",
    "quota exceeded",
    " 429",
    "status 429",
    "http 429",
)

# Retry-After / reset hints codex or the gateway may print, e.g.
#   "retry-after: 23", "retry after 23s", "reset-tokens: 19", "try again in 12.5s"
_RETRY_AFTER_RE = re.compile(
    r"(?:retry[\s_-]*after|retry[\s_-]*in|reset[\s_-]*(?:requests|tokens)?|try again in)"
    r"[^0-9]{0,12}?(\d+(?:\.\d+)?)\s*(m?s|sec|secs|seconds|s)?\b",
    re.IGNORECASE,
)


# Review M17: retry classification reads only the TAIL of stderr, never the whole
# accumulated trace. Codex streams the agent's entire tool output (test runs,
# diffs, greps) to stderr, so substring-matching the full buffer let the agent's
# OWN text decide retry policy — a test named `test_rate_limit_backoff` scrolling
# past was read as provider pushback, bought 15 minutes of 60s waits, and then
# laundered a genuine agent error into TRANSPORT_DEATH, the one exit reason the
# breakers exempt. A fatal transport error is the LAST thing codex prints before
# exiting, so the tail is where it lives; the window is generous enough to cover
# codex's own post-error reconnect chatter.
_CLASSIFY_TAIL_LINES = 20


def _stderr_tail(stderr: str, lines: int = _CLASSIFY_TAIL_LINES) -> str:
    """The last `lines` lines of stderr — the only window retry classification
    may look at (see _CLASSIFY_TAIL_LINES)."""
    return "\n".join((stderr or "").splitlines()[-lines:])


def _is_transport_drop(exit_code: int, stderr: str, result: AgentResult) -> bool:
    """True iff this looks like a gateway stream drop worth retrying: codex
    exited non-zero, the TAIL of its stderr carries a transport-drop signature,
    and no envelope was produced (a captured envelope means the turn actually
    completed — never retry over real output).

    Review M17: the "real output" guard defers to `classify_outcome`, the single
    place that knows the schemas' terminal vocabulary. The old hand-copied tuple
    had drifted — it listed `done` (in no schema) while MISSING `pushed`, so a
    completed force-push whose connection died on the way out was re-run on
    resume: duplicate spend on the one branch-mutating leg. Deferring means a new
    terminal status is covered the moment the schema and the domain agree on it,
    with no third list to forget.

    On this branch (exit_code != 0) a non-ERROR classification implies an envelope
    was captured: `parse_codex_envelope` returns is_error=True for every nonzero
    exit that produced no parseable envelope. So "not an error" IS "real output"."""
    if exit_code == 0:
        return False
    if classify_outcome(result) is not Outcome.ERROR:
        return False
    tail = _stderr_tail(stderr)
    low = tail.lower()
    return any(sig in low for sig in _TRANSPORT_DROP_SIGNATURES) or _is_rate_limited(tail)


def _is_rate_limited(stderr: str) -> bool:
    """True iff the failure is a provider rate limit (HTTP 429 / quota), the
    transport-drop subset that needs the LONGER window-aligned backoff. Matched
    on the stderr TAIL only — see _CLASSIFY_TAIL_LINES."""
    low = _stderr_tail(stderr).lower()
    return any(sig in low for sig in _RATE_LIMIT_SIGNATURES)


def _unlink_quietly(path: str) -> None:
    """Remove a scratch file if it exists. Best-effort: a leftover temp file must
    never be the reason a run fails."""
    try:
        os.unlink(path)
    except OSError:
        pass


def _extract_retry_after_seconds(stderr: str) -> float | None:
    """Parse a Retry-After / rate-limit-reset hint from stderr, in seconds. Returns
    None when absent (caller falls back to the window-aligned default). Picks the
    LARGEST hint found — the reset furthest out is the safe one to wait for.
    Milliseconds ('ms') are normalised to seconds; a bare number is seconds.

    Read from the stderr TAIL only, for the same reason the classifiers are
    (review M17): a number the AGENT printed must never set how long we sleep."""
    best: float | None = None
    for m in _RETRY_AFTER_RE.finditer(_stderr_tail(stderr)):
        try:
            val = float(m.group(1))
        except (TypeError, ValueError):
            continue
        if (m.group(2) or "").lower() == "ms":
            val /= 1000.0
        if best is None or val > best:
            best = val
    return best


def _kill_quietly(proc: Any) -> None:
    """Terminate a hung codex run and its ENTIRE process tree after an idle-timeout.
    The subprocess is spawned with start_new_session=True, so its pid is the leader
    of a process group that includes every descendant (the TEST_CMD it ran and any
    background server that spawned — e.g. a vite/playwright dev server). Killing only
    proc.pid would orphan those; they'd hold the inherited stdout pipe open and hang
    the run's stdout read + proc.wait() forever (→ wedged daemon). So signal the whole
    GROUP. Best-effort: never raises (the process may already be gone)."""
    import os
    import signal

    try:
        pgid = os.getpgid(proc.pid)
    except Exception:
        pgid = None
    # Kill the group if we have one; fall back to the single pid.
    for sig in (signal.SIGKILL,):
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                proc.kill()
        except ProcessLookupError:
            return
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
            return


def _session_id_from_lines(stdout: str) -> str:
    """codex exec prints `session id: <uuid>` in its header. Pull it out so the
    resume/address-review legs can continue the session (mirrors save_session)."""
    for line in stdout.splitlines():
        s = line.strip()
        if s.lower().startswith("session id:"):
            return s.split(":", 1)[1].strip()
    return ""
