"""CodexRunner: the second runtime adapter, over the OpenAI Codex CLI.

Same `run(RuntimePlan) -> AgentResult` contract as `AgentRunner`. It speaks to
`codex exec`, itself an agentic loop with a sandbox. The pure pieces
(`build_codex_argv`, `parse_codex_envelope`) unit-test with no subprocess.

Money: Codex reports no dollars, so this leg's spend is its root session's
rollout token counters (minus the resumed session's pre-launch baseline) priced
against the configured rate card. Native sub-agents are disabled in the
CODEX_HOME config (`codex_home`), so the root counter is the whole leg. Stderr
carries agent-controlled output and is never priced.
"""

from __future__ import annotations

import asyncio
import glob
import hashlib
import json
import os
import re
import signal
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from typing import Any

from .child_env import child_process_env
from .runtime_plan import AgentRuntime, RuntimePlan, git_identity_env
from ..domain.contracts import Money, RunId, TokenUsage, UsageSource
from ..domain.pricing import ModelRates, RateTable, UnknownModelPolicy
from ..domain.run_usage import (
    EstimatedRunUsage,
    MeasuredRunUsage,
    ModelUsage,
    RunUsage,
    price_run_usage,
)
from ..domain.outcome import (
    AgentResult,
    ExitReason,
    MAX_BUDGET_STATUS,
    Outcome,
    PROVIDER_REFUSED_SUBTYPE,
    classify_outcome,
    declared_status,
    no_session_evidence,
)

# Codex hardcodes `.git` read-only under --sandbox workspace-write (OpenAI
# issues #12280/#14338), which breaks the git-driven fixer: full access is the
# same posture the Claude fixer runs in (bypassPermissions).
DEFAULT_SANDBOX = "danger-full-access"

# `codex exec` left alone runs at reasoning effort LOW (live session_meta
# 2026-07-27): too little for an implementer. A task's own effort wins.
DEFAULT_REASONING_EFFORT = "medium"

# One stderr record can be arbitrarily large (agent tool output). Let multi-MiB
# records through, retaining a bounded diagnostic beyond that.
_STDERR_STREAM_LIMIT = 4 * 1024 * 1024
_STDERR_RETAINED_LINE_BYTES = 16 * 1024
_SPEND_EVIDENCE_VERSION = 3


def spend_evidence_path(directory: str, run_id: str) -> str:
    """Stable, path-safe location for one reserved provider run."""

    digest = hashlib.sha256(str(run_id).encode()).hexdigest()
    return os.path.join(directory, f"{digest}.json")


def write_spend_evidence(directory: str, run_id: str, evidence: dict[str, Any]) -> None:
    """Atomically persist what boot recovery needs to price a killed rollout."""

    if not directory or not run_id:
        return
    os.makedirs(directory, exist_ok=True)
    path = spend_evidence_path(directory, run_id)
    temporary = f"{path}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    payload = {"version": _SPEND_EVIDENCE_VERSION, "run_id": str(run_id), **evidence}
    try:
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def remove_spend_evidence(directory: str, run_id: str) -> None:
    if not directory or not run_id:
        return
    try:
        os.unlink(spend_evidence_path(directory, str(run_id)))
    except FileNotFoundError:
        pass


def rate_table_evidence(table: RateTable | None) -> dict | None:
    """A rate card frozen into spend evidence, so recovery prices with it."""
    if table is None:
        return None
    return {"id": table.rate_card_id, "version": table.version, "currency": table.currency,
            "rates": {name: asdict(rates) for name, rates in table.rates.items()},
            "unknown_model_policy": table.unknown_model_policy.value,
            "fallback": asdict(table.fallback) if table.fallback is not None else None}


def recovered_rate_table(raw: dict | None) -> RateTable | None:
    """Parse a frozen rate card back; malformed evidence raises ValueError."""
    if raw is None:
        return None
    if (not isinstance(raw, dict) or not isinstance(raw.get("rates"), dict) or len(raw["rates"]) > 256
            or not all(isinstance(raw.get(key), str) and 0 < len(raw[key]) <= 256
                       for key in ("id", "version", "currency"))
            or not all(isinstance(name, str) and 0 < len(name) <= 256 for name in raw["rates"])):
        raise ValueError("recovery price card is malformed")

    def rates(value: dict) -> ModelRates:
        if not isinstance(value, dict) or any(type(v) is not int or v <= 0 for v in value.values()):
            raise ValueError("recovery rates require positive integers")
        return ModelRates(**value)

    return RateTable(raw["id"], raw["version"], raw["currency"],
                     {name: rates(value) for name, value in raw["rates"].items()},
                     UnknownModelPolicy(raw["unknown_model_policy"]),
                     rates(raw["fallback"]) if raw.get("fallback") is not None else None)


def leg_usage(session_id: str, model: str, usage: dict | None, rates: RateTable | None) -> RunUsage:
    """This leg's spend evidence from its root rollout counter delta.

    Counters are measured; their absence (no session, no completed turn, a
    malformed record) is an explicit estimate that charges zero for the
    interrupted requests rather than inventing a figure.
    """
    try:
        if not session_id or not isinstance(usage, dict) or usage.get("source") != UsageSource.ROLLOUT.value:
            raise ValueError("no trusted rollout counters")
        values = tuple(usage.get(key, 0) for key in (
            "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        if any(type(v) is not int or not 0 <= v <= 10**12 for v in values):
            raise ValueError("rollout counters are malformed")
        inputs, outputs, cached, written = values
        if cached + written > inputs:
            raise ValueError("cache tokens exceed inclusive input")
        tokens = TokenUsage(inputs - cached - written, outputs, cached, written, UsageSource.ROLLOUT)
        name = model.strip().casefold() or "<runtime-default>"
        return MeasuredRunUsage((ModelUsage(session_id, name, tokens),), rates)
    except (ValueError, TypeError):
        return EstimatedRunUsage("rollout usage unavailable; interrupted requests charged zero", (), rates)


@dataclass(frozen=True)
class CodexProvider:
    """The `-c model_providers.<id>.*` block for an OpenAI-compatible gateway.

    A blank `base_url` means codex's built-in provider with `codex login`
    credentials. The id must not be a built-in one (codex-cli >= 0.142 refuses
    to redefine `openai`). The key stays in the env named by `env_key`.
    """

    provider_id: str = "openai-custom"
    name: str = "OpenAI"
    base_url: str = ""
    env_key: str = "OPENAI_API_KEY"
    wire_api: str = "responses"

    def config_overrides(self) -> list[str]:
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


def _nullable(schema: dict) -> dict:
    declared = schema.get("type")
    if isinstance(declared, str):
        return schema if declared == "null" else {**schema, "type": [declared, "null"]}
    if isinstance(declared, list):
        return schema if "null" in declared else {**schema, "type": [*declared, "null"]}
    return schema


def _strict_schema(schema: dict) -> dict:
    """Strict structured output requires every property in `required` and
    additionalProperties:false at each object. A formerly optional property
    becomes nullable rather than mandatory, so a model reading untrusted text
    can still say "nothing found" instead of inventing a value."""
    if not isinstance(schema, dict):
        return schema
    out = dict(schema)
    items = schema.get("items")
    if isinstance(items, dict):
        out["items"] = _strict_schema(items)
    for keyword in ("anyOf", "oneOf", "allOf"):
        alternatives = schema.get(keyword)
        if isinstance(alternatives, list):
            out[keyword] = [_strict_schema(alternative) for alternative in alternatives]
    if schema.get("type") != "object":
        return out
    props = schema.get("properties")
    if not isinstance(props, dict):
        return out
    optional = set(props) - set(schema.get("required") or ())
    out["additionalProperties"] = False
    out["required"] = list(props.keys())
    out["properties"] = {
        k: (_nullable(_strict_schema(v)) if k in optional else _strict_schema(v))
        for k, v in props.items()
    }
    return out


def build_codex_argv(
    task: RuntimePlan,
    *,
    codex_bin: str = "codex",
    provider: CodexProvider | None = None,
    sandbox: str = DEFAULT_SANDBOX,
    default_model: str = "",
    schema_path: str | None = None,
    output_path: str | None = None,
    inject_system_prompt: bool = True,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
) -> list[str]:
    """Pure: RuntimePlan -> `codex exec` argv."""
    provider = provider or CodexProvider()
    argv: list[str] = [codex_bin, "exec"]
    # `codex exec resume <id>` rejects `--sandbox` and `-C`; the resumed turn's
    # cwd is set on the subprocess instead, and the bypass flag is the sandbox
    # posture the resume grammar accepts.
    is_resume = bool(task.resume)
    if is_resume:
        argv += ["resume", task.resume]
    for ov in provider.config_overrides():
        argv += ["-c", ov]
    # `-m ""` is a broken flag, not "the default".
    model = task.model or default_model
    if model:
        argv += ["-m", model]
    effort = task.effort.value if task.effort is not None else reasoning_effort
    if effort:
        argv += ["-c", f"model_reasoning_effort={effort}"]
    if is_resume:
        argv += ["--dangerously-bypass-approvals-and-sandbox"]
    else:
        argv += ["--sandbox", sandbox]
        if task.cwd:
            argv += ["-C", task.cwd]
    if schema_path:
        argv += ["--output-schema", schema_path]
    if output_path:
        argv += ["-o", output_path]
    # The fix contract rides CODEX_HOME's developer_instructions; without it
    # (inject_system_prompt) the system prompt is prepended to the task.
    prompt = task.rendered_prompt
    if inject_system_prompt and task.append_system_prompt:
        prompt = f"{task.append_system_prompt}\n\n---\n\n{prompt}"
    argv.append(prompt)
    return argv


# Linux caps one argv element at MAX_ARG_STRLEN (128 KiB); above this the
# prompt goes on stdin (`-`). A prompt too large for any model context is
# refused before spend (keifu #173: E2BIG on a diff-bearing prompt).
_PROMPT_ARGV_LIMIT = 100_000
_PROMPT_STDIN_HARD_LIMIT = 1_500_000


def route_oversize_prompt(argv: list[str]) -> tuple[list[str], bytes | None]:
    """Pure: move an oversize argv prompt (always the last element) onto stdin."""
    prompt = argv[-1]
    if len(prompt.encode()) <= _PROMPT_ARGV_LIMIT:
        return argv, None
    return [*argv[:-1], "-"], prompt.encode()


def _codex_home(codex_home: str | None) -> str:
    return codex_home or os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def _rollout_timestamp(raw: Any) -> float | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return stamp.timestamp() if stamp.tzinfo is not None else None


def discover_rollout_session(cwd: str, started_at: float, codex_home: str | None = None) -> str:
    """This fresh root `codex exec` session, from its rollout `session_meta`.

    Codex >= 0.146 prints no session header. Identity is the originator, the
    exact resolved cwd, `source == "exec"` (sub-agents carry an object source)
    and a start no earlier than this invocation.
    """
    if not cwd:
        return ""
    target_cwd = os.path.realpath(cwd)
    candidates: list[tuple[float, str]] = []
    try:
        pattern = os.path.join(_codex_home(codex_home), "sessions", "**", "rollout-*.jsonl")
        for path in glob.glob(pattern, recursive=True):
            try:
                with open(path, encoding="utf-8") as rollout:
                    for line in rollout:
                        if '"session_meta"' not in line:
                            continue
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if event.get("type") != "session_meta":
                            continue
                        payload = event.get("payload") or {}
                        timestamp = _rollout_timestamp(event.get("timestamp"))
                        session_id = payload.get("session_id") or payload.get("id")
                        if (
                            payload.get("originator") == "codex_exec"
                            and payload.get("source") == "exec"
                            and isinstance(payload.get("cwd"), str)
                            and os.path.realpath(payload["cwd"]) == target_cwd
                            and timestamp is not None
                            and timestamp >= started_at
                            and isinstance(session_id, str)
                            and session_id
                        ):
                            candidates.append((timestamp, session_id))
                        break
            except OSError:
                continue
    except Exception:  # noqa: BLE001 — identity recovery must not kill a run
        return ""
    return max(candidates)[1] if candidates else ""


def _rollout_path(session_id: str, codex_home: str | None) -> str | None:
    if not session_id:
        return None
    paths = glob.glob(os.path.join(_codex_home(codex_home), "sessions", "**",
                                   f"rollout-*-{session_id}.jsonl"), recursive=True)
    return max(paths, key=os.path.getmtime) if paths else None


class MalformedRolloutCounters(ValueError):
    """A complete rollout record carries token counters that do not parse:
    nothing can price the leg, so its live budget cannot hold."""


def _rollout_events(session_id: str, codex_home: str | None, marker: str, *, strict: bool = False):
    """Parsed rollout events whose line contains `marker`. An unparseable line
    skips, or under `strict` raises once complete (codex may be mid-write)."""
    path = _rollout_path(session_id, codex_home)
    if path is None:
        return
    with open(path) as f:
        for line in f:
            if marker not in line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                if strict and line.endswith("\n"):
                    raise MalformedRolloutCounters(f"unparseable {marker} record") from None
                continue
            payload = event.get("payload") if isinstance(event, dict) else None
            if isinstance(payload, dict):
                yield event, payload


# Provider refusals codex writes on `task_complete` as `error.codex_error_info`,
# from the provider's response (the counters' trusted channel; agent output
# never reaches it). "other" stays an agent error.
_PROVIDER_MACHINE_FAILURES = frozenset({"server_overloaded", "usage_limit_exceeded"})


def read_rollout_provider_failure(
    session_id: str, codex_home: str | None = None, *, since: float | None = None
) -> str | None:
    """The provider refusal that ended this run's last turn, or None.

    Only `task_complete` events at or after `since` count, so a resumed
    session's earlier overloaded turn never classifies this run.
    """
    try:
        last: object = None
        for event, payload in _rollout_events(session_id, codex_home, '"task_complete"'):
            if payload.get("type") != "task_complete":
                continue
            stamp = _rollout_timestamp(event.get("timestamp"))
            if since is not None and (stamp is None or stamp < since):
                continue
            error = payload.get("error")
            last = error.get("codex_error_info") if isinstance(error, dict) else None
        return last if isinstance(last, str) and last in _PROVIDER_MACHINE_FAILURES else None
    except Exception:  # noqa: BLE001 — classification evidence must never kill a run
        return None


def read_rollout_usage(
    session_id: str, codex_home: str | None = None, *, until: float | None = None
) -> dict | None:
    """The session's cumulative token counters from its rollout, or None.

    The last valid timestamped `token_count` wins (the counter is cumulative).
    `until` excludes counters after the run's recorded end, which a later leg
    resuming the same session may have written. A counter record that does not
    parse raises `MalformedRolloutCounters` (fail closed); a record without
    counters yet (`info: null`) is skipped.
    """
    try:
        found: dict | None = None
        for event, payload in _rollout_events(session_id, codex_home, '"token_count"', strict=True):
            if payload.get("type") != "token_count":
                continue
            stamp = _rollout_timestamp(event.get("timestamp"))
            if stamp is None or (until is not None and stamp > until):
                continue
            info = payload.get("info")
            if info is None:
                continue
            total = info.get("total_token_usage") if isinstance(info, dict) else None
            if not (isinstance(total, dict) and type(total.get("input_tokens")) is int
                    and type(total.get("output_tokens")) is int):
                raise MalformedRolloutCounters("token_count record without parseable totals")
            found = total
        if found is None:
            return None
        return {
            "input_tokens": found["input_tokens"],
            "output_tokens": found["output_tokens"],
            "cache_read_input_tokens": found.get("cached_input_tokens", 0),
            "cache_creation_input_tokens": found.get("cache_write_input_tokens", 0),
            "source": UsageSource.ROLLOUT.value,
        }
    except MalformedRolloutCounters:
        raise
    except Exception:  # noqa: BLE001 — an unreadable rollout is no evidence yet
        return None


_USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def usage_delta(end: dict | None, start: dict | None) -> dict | None:
    """Pure: end counters minus the resumed session's baseline, floored at zero.

    A follow-up leg resumes the resolve's session, whose counter is cumulative:
    pricing it raw re-ledgers every token an earlier leg already paid for
    (2026-07-27: a 114s fix-ci ledgered $39.57 of its resolve's tokens).
    """
    if not isinstance(end, dict):
        return None
    if not isinstance(start, dict):
        return end
    delta: dict[str, Any] = {
        key: max(0, int(end.get(key, 0) or 0) - int(start.get(key, 0) or 0))
        for key in _USAGE_KEYS
        if key in end or key in start
    }
    if isinstance(end.get("source"), str):
        delta["source"] = end["source"]
    return delta


# A runtime that never got a session off the ground is a machine failure
# (keifu #130: a broken binary answered with an instant HTTP 400). Classified
# only from a positive signature in the stderr TAIL, guarded by "no session
# evidence at all"; strings agent output can produce are deliberately absent.
_RUNTIME_FAULT_SIGNATURES: tuple[str, ...] = ("unexpected status 400 bad request",)
# `supervised_exec` exits 127 silently when it cannot exec the binary.
_EXEC_FAILURE_EXIT_CODE = 127


def _runtime_could_not_start(*, exit_code: int, session_id: str, usage: dict | None, raw: str, stderr: str) -> bool:
    if not no_session_evidence(session_id=session_id, usage=usage, output=raw):
        return False
    if exit_code == _EXEC_FAILURE_EXIT_CODE and not (stderr or "").strip():
        return True
    low = _stderr_tail(stderr).lower()
    return any(sig in low for sig in _RUNTIME_FAULT_SIGNATURES)


def parse_codex_envelope(
    *,
    exit_code: int,
    last_message: str,
    session_id: str = "",
    stderr: str = "",
    usage: dict | None = None,
) -> AgentResult:
    """Pure: codex exec result -> AgentResult, decided from the `-o` envelope,
    never the exit code. An error only when no envelope parsed and the exit
    was non-zero, or the envelope declares `error`. Pricing happens in `run`."""
    structured: dict | None = None
    raw = (last_message or "").strip()
    if raw:
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                structured = obj
        except ValueError:
            structured = None
    if structured is None:
        if exit_code != 0:
            stillborn = _runtime_could_not_start(
                exit_code=exit_code, session_id=session_id, usage=usage, raw=raw, stderr=stderr,
            )
            return AgentResult(
                is_error=True,
                summary=(stderr.strip() or raw or f"codex exec exit {exit_code}")[:2000],
                session_id=session_id,
                usage=usage,
                exit_reason=ExitReason.RUNTIME_UNAVAILABLE if stillborn else ExitReason.AGENT_ERROR,
            )
        return AgentResult(is_error=False, summary=raw[:2000], session_id=session_id,
                           usage=usage, exit_reason=ExitReason.OK)
    status = declared_status(structured)
    is_error = status == "error"
    return AgentResult(
        is_error=is_error,
        status=status,
        summary=str(structured.get("summary", "") or ""),
        needs=str(structured.get("needs", "") or ""),
        usage=usage,
        session_id=session_id,
        structured=structured,
        # A parsed envelope (including `blocked`) is the loop's own terminal.
        exit_reason=ExitReason.AGENT_ERROR if is_error else ExitReason.OK,
    )


@dataclass
class CodexRunner(AgentRuntime):
    """Runs tasks over `codex exec`. The task's CODEX_HOME is prepared by
    `codex_home.CodexHomeRuntime`; all subprocess I/O lives here."""

    codex_bin: str = "codex"
    provider: CodexProvider = field(default_factory=CodexProvider)
    sandbox: str = DEFAULT_SANDBOX
    default_model: str = ""
    reasoning_effort: str = DEFAULT_REASONING_EFFORT
    env: dict[str, str] = field(default_factory=dict)
    # False when the contract is delivered as CODEX_HOME developer_instructions.
    inject_system_prompt: bool = True
    log: Any = None
    debug: bool = False
    # Idle watchdog (seconds between stderr events) and total-runtime cap, both
    # on the WALL clock so a laptop suspend counts (the monotonic clock freezes
    # on macOS sleep). A limping stream defeats the idle cap; only the wall cap
    # bounds it (FAILURE-MODES #60). None disables either.
    idle_timeout: float | None = None
    max_runtime: float | None = None
    wall_clock: Any = None
    poll_slice: float = 30.0
    # Bounds only the residual stdout drain after stderr ends: a descendant that
    # survived the group kill may hold the pipe forever.
    post_hang_drain_timeout: float = 30.0
    process_supervisor: Any = None
    # Where the leg's rollout coordinates are persisted, so boot recovery can
    # settle measured usage after SIGKILL or machine loss.
    spend_evidence_dir: str = ""
    # The rate card both the mid-run budget kill and settlement price with.
    rate_table: RateTable | None = None

    def __post_init__(self) -> None:
        self._log = self.log or (lambda _m: None)
        self._wall_clock = self.wall_clock or time.time

    def _resolved_model(self, task: RuntimePlan) -> str:
        """The model the leg actually runs on (an unpinned task carries None)."""
        return task.model or self.default_model

    def _child_environment(self, task: RuntimePlan) -> dict[str, str]:
        return child_process_env(
            os.environ,
            {**git_identity_env(self._resolved_model(task)), **self.env, **task.env},
            additional_keys=(self.provider.env_key,),
        )

    @staticmethod
    def _accounting_home(environment: dict[str, str], cwd: str | None) -> str:
        # An explicit empty CODEX_HOME resets to the child's HOME; codex
        # resolves a relative home from its execution directory.
        home = environment.get("CODEX_HOME") or os.path.join(
            environment.get("HOME") or os.path.expanduser("~"), ".codex",
        )
        return os.path.realpath(os.path.join(cwd or os.getcwd(), home))

    def _pump_slice(self) -> float:
        caps = [self.poll_slice]
        if self.idle_timeout is not None:
            caps.append(self.idle_timeout)
        if self.max_runtime is not None:
            caps.append(self.max_runtime)
        return min(caps)

    def _log_codex_line(self, line: str) -> None:
        """Surface milestone stderr lines (test runs, commits, patches) always;
        everything else only under debug."""
        s = line.strip()
        if not s:
            return
        low = s.lower()
        if "pytest" in low or "tsc -b" in low or "test_cmd" in low or ("uv run" in low and "test" in low):
            self._log(f"codex TEST_CMD: {s[:200]}")
        elif "git commit" in low or low.startswith(("plan:", "red:", "green:", "docs:")):
            self._log(f"codex commit: {s[:200]}")
        elif "apply_patch" in low or low.startswith("exec "):
            self._log(f"codex tool: {s[:200]}")
        elif self.debug:
            self._log(f"codex| {s[:200]}")

    def preflight(self) -> str | None:
        """Why the runtime is NOT usable, or None. Logged at startup; a run
        that finds it unready returns RUNTIME_UNAVAILABLE (a machine failure)."""
        import shutil

        if shutil.which(self.codex_bin) is None:
            return (f"codex binary '{self.codex_bin}' not found on PATH — install the "
                    "Codex CLI or set CODEX_BIN.")
        key_var = self.provider.env_key
        if self.env.get(key_var) or os.environ.get(key_var):
            return None
        if not self.provider.base_url:
            if self.oauth_auth_file():
                return None
            auth_file = os.path.join(_codex_home(None), "auth.json")
            return f"codex has no credentials — no {auth_file} (run `codex login`) and {key_var} is unset."
        return (f"{key_var} is unset — the Codex runtime cannot authenticate to "
                f"{self.provider.base_url}. Export {key_var} in the launching shell.")

    def oauth_auth_file(self) -> str:
        """The `codex login` file the isolated CODEX_HOME links, if any."""
        if self.provider.base_url:
            return ""
        key_var = self.provider.env_key
        if self.env.get(key_var) or os.environ.get(key_var):
            return ""
        auth_file = os.path.join(self.env.get("CODEX_HOME") or _codex_home(None), "auth.json")
        return auth_file if os.path.isfile(auth_file) else ""

    async def run(self, task: RuntimePlan) -> AgentResult:
        reason = self.preflight()
        if reason is not None:
            self._log(f"codex: NOT READY — {reason}")
            return AgentResult(is_error=True, status="blocked", summary=reason,
                               exit_reason=ExitReason.RUNTIME_UNAVAILABLE,
                               model=self._resolved_model(task))
        # Removed on every exit path, including cancellation (review M17).
        with tempfile.TemporaryDirectory(prefix="codex-run-", ignore_cleanup_errors=True) as tmpdir:
            result = await self._run_in(task, tmpdir)
        return replace(result, model=result.model or self._resolved_model(task))

    async def _run_in(self, task: RuntimePlan, tmpdir: str) -> AgentResult:
        """One priced `codex exec`. A leg is one launch: a gateway drop or a 429
        ends it as a machine failure charged what it spent, and the item's
        backoff retries in a new leg with its own reservation (an in-leg retry
        loop once settled only its last attempt's session)."""
        schema_path = None
        if task.schema is not None:
            schema_path = os.path.join(tmpdir, "schema.json")
            with open(schema_path, "w") as f:
                json.dump(_strict_schema(task.schema), f)
        output_path = os.path.join(tmpdir, "last.json")
        model = self._resolved_model(task) or "<runtime-default>"
        codex_home = self._accounting_home(self._child_environment(task), task.cwd)
        # Tokens the resumed session had already burned (and an earlier leg ledgered).
        baseline_sid = task.resume or ""
        try:
            baseline = read_rollout_usage(baseline_sid, codex_home) if baseline_sid else None
        except MalformedRolloutCounters as exc:
            return _malformed_counters(str(exc), baseline_sid, None)
        argv, stdin_payload = route_oversize_prompt(build_codex_argv(
            task, codex_bin=self.codex_bin, provider=self.provider, sandbox=self.sandbox,
            default_model=self.default_model, schema_path=schema_path, output_path=output_path,
            inject_system_prompt=self.inject_system_prompt, reasoning_effort=self.reasoning_effort,
        ))
        if stdin_payload is not None and len(stdin_payload) > _PROMPT_STDIN_HARD_LIMIT:
            return AgentResult(
                is_error=True, status="prompt_oversize", exit_reason=ExitReason.AGENT_ERROR,
                summary=(f"prompt is {len(stdin_payload)} bytes (> {_PROMPT_STDIN_HARD_LIMIT}); refusing "
                         "to dispatch a leg that cannot fit a model context"),
            )
        result = await self._exec_once(
            argv, task, output_path, model, codex_home,
            (baseline_sid, baseline) if baseline else None, stdin_payload,
        )
        observed = leg_usage(result.session_id, model, result.usage, self.rate_table)
        amount = price_run_usage(observed, self.rate_table)
        result = replace(result, run_usage=observed, cost_usd=amount.micros / 1_000_000)
        if task.max_budget_usd > 0 and amount.micros >= Money.from_major("USD", task.max_budget_usd).micros:
            result = replace(result, is_error=True, status=MAX_BUDGET_STATUS,
                             envelope_subtype=MAX_BUDGET_STATUS, exit_reason=ExitReason.BUDGET_EXHAUSTED)
        return result

    async def _exec_once(
        self, argv: list[str], task: RuntimePlan, output_path: str, model: str, codex_home: str,
        baseline: tuple[str, dict] | None, stdin_payload: bytes | None,
    ) -> AgentResult:
        """One `codex exec`: spawn, pump stderr under the watchdogs and the
        budget, then read the envelope and this leg's rollout counters."""
        proc_env = self._child_environment(task)
        self._log(f"codex: exec model={model} cwd={task.cwd or '.'}")
        rollout_started_at = time.time()
        execution_cwd = task.cwd or os.getcwd()
        accounting_run_id = proc_env.get("ISSUE_AGENT_RUN_ID", "")
        evidence = {
            "cwd": os.path.realpath(execution_cwd),
            "rollout_started_at": rollout_started_at,
            "session_id": task.resume or "",
            "baseline_session_id": baseline[0] if baseline else "",
            "baseline_usage": baseline[1] if baseline else None,
            "model": model,
            "codex_home": codex_home,
            "rate_table": rate_table_evidence(self.rate_table),
        }
        write_spend_evidence(self.spend_evidence_dir, accounting_run_id, evidence)
        supervision_run_id = RunId(accounting_run_id or f"codex-{uuid.uuid4().hex}")
        spawn_argv = tuple(argv)
        if self.process_supervisor is not None:
            spawn_argv = self.process_supervisor.supervised_command(supervision_run_id, argv)
            proc_env.update(self.process_supervisor.launch_guard_env())
        # A new session puts codex and every descendant (the test servers it
        # spawns) in one process group the watchdogs kill whole; a leaked
        # child holding stdout wedged the daemon (Myapp #310).
        proc = await asyncio.create_subprocess_exec(
            *spawn_argv,
            stdin=asyncio.subprocess.PIPE if stdin_payload is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=proc_env,
            cwd=task.cwd or None,
            start_new_session=True,
            limit=_STDERR_STREAM_LIMIT,
        )
        feeder: asyncio.Task | None = None
        if stdin_payload is not None:
            # Fed concurrently: codex may emit stderr before reading stdin.
            async def _feed(payload: bytes) -> None:
                try:
                    if proc.stdin is not None:
                        proc.stdin.write(payload)
                        await proc.stdin.drain()
                except Exception:  # noqa: BLE001 — a dead child surfaces via exit
                    pass
                finally:
                    if proc.stdin is not None:
                        proc.stdin.close()

            feeder = asyncio.get_running_loop().create_task(_feed(stdin_payload))

        err_chunks: list[str] = []
        hung = False
        wall_tripped = False
        budget_stop = False
        malformed = ""
        budget_armed = task.max_budget_usd > 0 and self.rate_table is not None
        budget = Money.from_major("USD", task.max_budget_usd) if budget_armed else None
        started_at = self._wall_clock() if self.max_runtime is not None else 0.0
        last_budget_check = self._wall_clock() if budget_armed else 0.0
        session_id = task.resume or ""
        stderr_pending = b""

        def _session() -> str:
            nonlocal session_id
            if not session_id:
                session_id = discover_rollout_session(execution_cwd, rollout_started_at, codex_home)
                if session_id:
                    write_spend_evidence(self.spend_evidence_dir, accounting_run_id,
                                         {**evidence, "session_id": session_id})
            return session_id

        def _usage() -> dict | None:
            nonlocal malformed
            sid = _session()
            try:
                usage = read_rollout_usage(sid, codex_home) if sid else None
            except MalformedRolloutCounters as exc:
                malformed = str(exc)
                return None
            return usage_delta(usage, baseline[1]) if baseline is not None and sid == baseline[0] else usage

        def _over_budget(now: float) -> bool:
            """Sampled once per slice: each sample globs and reads the rollout,
            and codex's counters only advance at turn boundaries."""
            nonlocal last_budget_check
            if budget is None or now - last_budget_check < self._pump_slice():
                return False
            last_budget_check = now
            spent = price_run_usage(leg_usage(_session(), model, _usage(), self.rate_table), self.rate_table)
            if malformed:
                self._log(f"codex: COUNTERS — {malformed}; the budget cannot hold, killing subprocess")
                return True
            if spent.micros < budget.micros:
                return False
            self._log(f"codex: BUDGET — this leg has spent ${spent.micros / 1e6:.2f}, crossing "
                      f"${task.max_budget_usd:.2f}; killing subprocess")
            return True

        def _bounded(raw: bytes) -> str:
            if len(raw) <= _STDERR_RETAINED_LINE_BYTES:
                return raw.decode("utf-8", "replace").rstrip("\n")
            prefix = raw[:_STDERR_RETAINED_LINE_BYTES].decode("utf-8", "replace")
            return (f"[oversized stderr line truncated: {len(raw)} bytes; retained "
                    f"{_STDERR_RETAINED_LINE_BYTES} bytes] {prefix}")

        async def _read_stderr_line() -> bytes:
            """One logical line; an over-limit record is drained in bounded
            chunks. `stderr_pending` survives a watchdog cancellation."""
            nonlocal stderr_pending
            assert proc.stderr is not None
            prefix = stderr_pending
            pending_newline = prefix.find(b"\n")
            if pending_newline >= 0:
                stderr_pending = prefix[pending_newline + 1:]
                return prefix[:pending_newline + 1]
            try:
                raw = await proc.stderr.readuntil(b"\n")
            except asyncio.IncompleteReadError as exc:
                stderr_pending = b""
                return prefix + exc.partial
            except (asyncio.LimitOverrunError, ValueError):
                retained = bytearray(prefix[:_STDERR_RETAINED_LINE_BYTES])
                while True:
                    chunk = await proc.stderr.read(64 * 1024)
                    if not chunk:
                        break
                    newline = chunk.find(b"\n")
                    record = chunk if newline < 0 else chunk[:newline]
                    remaining = _STDERR_RETAINED_LINE_BYTES - len(retained)
                    if remaining > 0:
                        retained.extend(record[:remaining])
                    if newline >= 0:
                        stderr_pending = chunk[newline + 1:]
                        break
                return b"[oversized stderr line truncated after stream limit] " + bytes(retained)
            stderr_pending = b""
            return prefix + raw

        def _trip(now: float, idle: float | None) -> bool:
            """Check every armed cap once against one clock reading; kill on a trip."""
            nonlocal hung, wall_tripped, budget_stop
            if idle is not None and self.idle_timeout is not None and idle >= self.idle_timeout:
                hung = True
                self._log(f"codex: IDLE — no stderr event for {idle:.0f}s (>{self.idle_timeout:.0f}s); "
                          "killing subprocess")
            elif self.max_runtime is not None and now - started_at >= self.max_runtime:
                hung = wall_tripped = True
                self._log(f"codex: WALL — total runtime {now - started_at:.0f}s exceeded max "
                          f"{self.max_runtime:.0f}s; killing subprocess")
            elif _over_budget(now):
                budget_stop = True
            else:
                return False
            _kill_quietly(proc)
            return True

        armed = self.idle_timeout is not None or self.max_runtime is not None or budget_armed

        async def _pump_stderr() -> None:
            while True:
                # Checked between lines too: a chatty run never times out a slice.
                if armed and (self.max_runtime is not None or budget_armed) and _trip(self._wall_clock(), None):
                    return
                if not armed:
                    raw = await _read_stderr_line()
                else:
                    last_line = self._wall_clock()
                    while True:
                        try:
                            raw = await asyncio.wait_for(_read_stderr_line(), timeout=self._pump_slice())
                            break
                        except asyncio.TimeoutError:
                            now = self._wall_clock()
                            if _trip(now, now - last_line):
                                return
                if not raw:
                    return
                line = _bounded(raw)
                err_chunks.append(line)
                self._log_codex_line(line)

        async def _read_stdout() -> str:
            assert proc.stdout is not None
            try:
                return (await proc.stdout.read()).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                return ""

        async def _observe_exit(timeout: float) -> bool:
            # Polls returncode: CPython's Process.wait can attach after the
            # final transport callback and sleep until timeout.
            deadline = asyncio.get_running_loop().time() + timeout
            while proc.returncode is None:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return False
                await asyncio.sleep(min(0.01, remaining))
            return True

        stdout_task = asyncio.ensure_future(_read_stdout())
        try:
            await _pump_stderr()
            try:
                stdout = await asyncio.wait_for(
                    stdout_task, timeout=self.post_hang_drain_timeout if armed else None)
            except asyncio.TimeoutError:
                hung = True
                _kill_quietly(proc)
                await _cancel_quietly(stdout_task)
                stdout = ""
            if not await _observe_exit(10):
                hung = True
                self._log("codex: process exit was not observed within 10s after stream teardown")
                _kill_quietly(proc)
        except asyncio.CancelledError:
            # Post-spawn ownership: terminate the group and drain local tasks
            # before the dispatch lease is released, or an orphan keeps editing.
            _kill_quietly(proc)
            await _cancel_quietly(stdout_task)
            if not await _observe_exit(10):
                _kill_quietly(proc)
            raise
        finally:
            if feeder is not None:
                await _cancel_quietly(feeder)
            if self.process_supervisor is not None:
                # Ownership ends only after the whole group is gone.
                self.process_supervisor.kill_tree(supervision_run_id)
        exit_code = proc.returncode if proc.returncode is not None else 124
        stderr = "\n".join(err_chunks)
        self._log(f"codex: exit {exit_code} (stderr {len(stderr)}B, out {len(stdout)}B)")
        if exit_code != 0 and stderr:
            self._log(f"codex: stderr tail: {_stderr_tail(stderr, 3)[:500]}")
        sid = _session()
        usage = _usage()
        if malformed:
            # Checked first: a malformed-counter kill also sets `budget_stop`.
            return _malformed_counters(malformed, sid, usage)
        if budget_stop:
            # Checked before `hung`: the residual drain may set it after our kill.
            return AgentResult(
                is_error=True, status=MAX_BUDGET_STATUS, envelope_subtype=MAX_BUDGET_STATUS,
                exit_reason=ExitReason.BUDGET_EXHAUSTED,
                summary=f"codex spend crossed MAX_BUDGET_USD=${task.max_budget_usd:.2f} mid-run; killed",
                usage=usage, session_id=sid,
            )
        if hung:
            limit = (f"exceeded max runtime {self.max_runtime:.0f}s" if wall_tripped
                     else f"idle >{self.idle_timeout or 0:.0f}s (no stderr event)")
            return AgentResult(
                is_error=True, status="timeout", summary=f"codex {limit}; killed",
                exit_reason=ExitReason.WALL_TIMEOUT if wall_tripped else ExitReason.IDLE_TIMEOUT,
                session_id=sid, usage=usage,
            )
        try:
            with open(output_path) as f:
                last_message = f.read()
        except OSError:
            last_message = stdout
        result = parse_codex_envelope(
            exit_code=exit_code, last_message=last_message, session_id=sid, stderr=stderr, usage=usage,
        )
        provider_failure = (
            read_rollout_provider_failure(sid, codex_home, since=rollout_started_at) if result.is_error else None
        )
        if provider_failure is None and _is_transport_drop(exit_code, stderr, result):
            provider_failure = "HTTP 429 rate limit" if _is_rate_limited(stderr) else "gateway stream drop"
        if provider_failure is not None:
            # The provider ended the turn (capacity, quota, a dropped stream):
            # a machine failure whose session stays resumable. Reached only
            # after the watchdog and budget returns above, so a hung run keeps
            # its timeout class.
            self._log(f"codex: provider ended the turn ({provider_failure}) — machine failure, not an attempt")
            result = replace(result, exit_reason=ExitReason.TRANSPORT_DEATH,
                             envelope_subtype=PROVIDER_REFUSED_SUBTYPE,
                             summary=f"provider ended the turn: {provider_failure}")
        if exit_code < 0:
            # A signal we did not send (our kills set `hung`/`budget_stop`):
            # OOM, an operator kill, a Ctrl-C to the group. Not an attempt.
            self._log(f"codex: killed by signal {-exit_code} (not by our watchdog) — infra death")
            result = replace(result, exit_reason=ExitReason.KILLED_EXTERNALLY)
        return result


# Gateway stream-drop signatures codex prints when the upstream connection
# fails mid-generation.
_TRANSPORT_DROP_SIGNATURES = (
    "stream disconnected before completion",
    "response.failed event received",
)

# HTTP 429 arrives as `response.failed` too; the item's machine backoff waits
# out the provider's throttle window.
_RATE_LIMIT_SIGNATURES = (
    "rate limit", "too many requests", "rate_limit_exceeded", "too_many_requests",
    "usage limit", "quota exceeded", " 429", "status 429", "http 429",
)

# Transport classification reads only the stderr TAIL (review M17): codex streams
# the agent's whole tool output to stderr, and a test named
# `test_rate_limit_backoff` scrolling past once bought 15 minutes of waits and
# laundered an agent error into a transport death. A fatal transport error is
# the last thing codex prints.
_CLASSIFY_TAIL_LINES = 20


def _stderr_tail(stderr: str, lines: int = _CLASSIFY_TAIL_LINES) -> str:
    return "\n".join((stderr or "").splitlines()[-lines:])


def _is_transport_drop(exit_code: int, stderr: str, result: AgentResult) -> bool:
    """A non-zero exit with a drop or 429 signature in the tail and no captured
    envelope. `classify_outcome` decides "real output" so a completed turn
    (any terminal status) is never re-run."""
    if exit_code == 0 or classify_outcome(result) is not Outcome.ERROR:
        return False
    tail = _stderr_tail(stderr).lower()
    return any(sig in tail for sig in _TRANSPORT_DROP_SIGNATURES) or _is_rate_limited(tail)


def _is_rate_limited(stderr: str) -> bool:
    low = _stderr_tail(stderr).lower()
    return any(sig in low for sig in _RATE_LIMIT_SIGNATURES)


def _malformed_counters(detail: str, session_id: str, usage: dict | None) -> AgentResult:
    """Fail closed: an unpriceable leg ends as an agent attempt, so the item's
    attempt cap bounds a session whose spend nothing can see."""
    return AgentResult(is_error=True, status="error", exit_reason=ExitReason.AGENT_ERROR,
                       summary=f"codex rollout counters are malformed ({detail}); the leg cannot be priced",
                       session_id=session_id, usage=usage)


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


async def _cancel_quietly(task: asyncio.Future) -> None:
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


def _kill_quietly(proc: Any) -> None:
    """SIGKILL the run's whole process group (it leads one: start_new_session),
    so no descendant keeps the pipes open. Never raises."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        return
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
