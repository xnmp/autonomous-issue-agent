"""Environment → immutable config snapshot under ADR 0007.

Pure mapping `dict → HarnessConfig` so tests construct it without os.environ —
with one deliberate exception: an env carrying `AGENT_ENV` is re-resolved
against that FILE first (`_authoritative_env`, #12), because the inherited
environment of point-at.sh's relaunch loop cannot represent a knob's removal.
Names mirror config/agent.env.example so operators carry the same env file over.
"""

from __future__ import annotations

import re

import math
import os
import hashlib
import json
from dataclasses import MISSING, dataclass, field, fields
from enum import Enum
from types import MappingProxyType

from ..adapters.agent_runner import DEFAULT_MAX_BUFFER_BYTES
from ..adapters.control_path import machine_control_authority_root
from ..adapters.env_file import source_env_file
from ..domain.agent_task import RuntimeName
from ..domain.effort import EFFORT_LADDER, Effort, parse_effort
from ..domain.identity import normalize_login
from ..domain.pricing import parse_model_rate_table
from ..domain.identity import IdentityMode
from ..domain.repository import (
    canonical_repository,
    repository_validation_error,
)


def fix_runtime_names_from_env(env: dict) -> frozenset[str]:
    """The fixer provider readiness must cover: the one `FIXER_RUNTIME`.

    Doctor renders malformed configuration too, so this never raises.
    """
    primary = str(env.get("FIXER_RUNTIME") or "").strip().strip("\"'").lower()
    return frozenset({primary} if primary else set())

SHELL_CMD_LOG_LIMIT = 120
REPOINT_HISTORY_ENV = "REPOINTED_FROM_DEPLOYMENTS"
MAX_REPOINT_HISTORY = 32


def deployment_identity_for_target(
    repository: str,
    target_dir: str,
    role: str,
) -> str:
    """Stable identity for one repository checkout and daemon role."""
    checkout = os.path.realpath(target_dir) if target_dir else ""
    return hashlib.sha256(
        f"{repository}:{checkout}:{role}".encode()
    ).hexdigest()[:16]


def owner_identity_for_target(
    repository: str,
    target_dir: str,
    role: str,
) -> str:
    deployment = deployment_identity_for_target(repository, target_dir, role)
    return f"{repository}:{role}:{deployment}"


def prior_deployment_identities(
    cfg: HarnessConfig, role: str,
) -> tuple[tuple[str, str], ...]:
    """Exact prior repository/deployment identities authorized by config."""
    if role not in ("fix", "review"):
        raise ValueError("prior deployment identities require configured role authority")
    candidates = list(cfg.repointed_from_deployments)
    if cfg.repo_input and cfg.repo_input != cfg.repo:
        candidates.append((cfg.repo_input, cfg.target_dir))
    current = (cfg.repo, deployment_identity_for_target(cfg.repo, cfg.target_dir, role))
    return tuple(
        identity
        for identity in dict.fromkeys(
            (repository, deployment_identity_for_target(repository, target_dir, role))
            for repository, target_dir in candidates
        )
        if identity != current
    )


def configured_prior_owner_ids(cfg: HarnessConfig, role: str) -> tuple[str, ...]:
    """Owner predecessors passed to daemon boot's adoption operation."""
    return tuple(
        owner
        for repository, deployment in prior_deployment_identities(cfg, role)
        for owner in (f"{repository}:{role}", f"{repository}:{role}:{deployment}")
    )


def parse_repoint_history(raw: str) -> tuple[tuple[str, str], ...]:
    """Decode the explicit repository/checkout lineage written by the CLI."""

    if not raw.strip():
        return ()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{REPOINT_HISTORY_ENV} must be valid JSON") from exc
    if not isinstance(value, list):
        raise ValueError(f"{REPOINT_HISTORY_ENV} must be a JSON list")
    if len(value) > MAX_REPOINT_HISTORY:
        raise ValueError(
            f"{REPOINT_HISTORY_ENV} cannot contain more than "
            f"{MAX_REPOINT_HISTORY} deployments"
        )
    result: list[tuple[str, str]] = []
    identities: set[tuple[str, str]] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(
                f"{REPOINT_HISTORY_ENV}[{index}] must be an object"
            )
        repository = canonical_repository(str(item.get("repository") or ""))
        target_dir = str(item.get("target_dir") or "")
        if repository_validation_error(repository) or not repository:
            raise ValueError(
                f"{REPOINT_HISTORY_ENV}[{index}].repository is invalid"
            )
        if not target_dir or not os.path.isabs(target_dir):
            raise ValueError(
                f"{REPOINT_HISTORY_ENV}[{index}].target_dir must be absolute"
            )
        identity = (repository, os.path.realpath(target_dir))
        if identity not in identities:
            identities.add(identity)
            result.append(identity)
    return tuple(result)


def describe_command(value: str | None, *, empty: str = "EMPTY",
                     limit: int = SHELL_CMD_LOG_LIMIT) -> str:
    """Render a shell command for a boot log line: quoted, and truncated
    DELIBERATELY rather than by accident.

    The boot log used to print `TEST_CMD=set` — the literal word "set",
    reporting presence only. For a real value like
    `TEST_CMD='set -e; cargo test …'` that output is indistinguishable from a
    value truncated at the first space (#35), and this is the one line an
    operator reads to confirm which command arms the gate. So: print the value,
    repr-quoted (whitespace and shell punctuation stay visible inside the
    quotes), cut at `limit` with an explicit ellipsis, and always state the full
    length so a truncation can never be mistaken for the whole command.
    """
    text = value or ""
    if not text:
        return empty
    shown = text if len(text) <= limit else text[:limit] + "…"
    return f"{shown!r} (len={len(text)})"


@dataclass(frozen=True)
class RepositoryConfig:
    repo: str
    base_branch: str
    target_dir: str
    identity_mode: IdentityMode


@dataclass(frozen=True)
class RuntimePathsConfig:
    state_dir: str
    review_state_dir: str
    worktree_root: str
    harness_root: str


@dataclass(frozen=True)
class FixRuntimeConfig:
    bot: str
    model: str | None
    # Base reasoning effort for the fix legs; None = leave the runtime's default
    # alone. The escalation ladder (#16) raises it from here, never below it.
    effort: Effort | None
    budget_usd: float


@dataclass(frozen=True)
class ReviewRuntimeConfig:
    bot: str
    runtime: RuntimeName
    model: str | None
    prompt: str
    budget_usd: float


@dataclass(frozen=True)
class CodexRuntimeConfig:
    binary: str
    model: str
    provider_id: str
    provider_name: str
    base_url: str
    env_key: str
    wire_api: str
    sandbox: str
    reasoning_effort: str
    idle_timeout: float
    max_runtime: float
    transport_retries: int
    backoff_base: float
    rate_limit_backoff_base: float
    max_rate_limit_wait: float
    max_rate_limit_retries: int
    cost_per_mtok_in: float
    cost_per_mtok_out: float
    # Per-model (model, USD/MTok in, USD/MTok out) overrides; the scalars above
    # remain the fallback for any model not named here. See
    # `HarnessConfig.codex_cost_per_mtok`.
    cost_per_mtok: tuple[tuple[str, float, float], ...] = ()
    cost_per_mtok_cached_in: tuple[tuple[str, float], ...] = ()
    # Kill a run once its priced spend crosses RuntimePlan.max_budget_usd (#29).
    # See `HarnessConfig.codex_enforce_budget`.
    enforce_budget: bool = True

    @property
    def budget_pricing_gap(self) -> str:
        """Which price knob leaves the dollar budget unenforceable, else "".

        Named rather than boolean so the startup warning can point at the rate
        that is actually missing: a zeroed CACHED_IN used to be reported as a
        CODEX_COST_PER_MTOK_IN/OUT problem, sending the operator to two knobs
        that were already correct."""
        if self.cost_per_mtok_in <= 0 or self.cost_per_mtok_out <= 0:
            return "both CODEX_COST_PER_MTOK_IN and CODEX_COST_PER_MTOK_OUT"
        if any(rate_in <= 0 or rate_out <= 0 for _, rate_in, rate_out in self.cost_per_mtok):
            return "every per-model IN/OUT rate in CODEX_COST_PER_MTOK"
        if any(rate <= 0 for _, rate in self.cost_per_mtok_cached_in):
            return "every per-model CACHED_IN rate in CODEX_COST_PER_MTOK"
        return ""

    @property
    def budget_priceable(self) -> bool:
        """Whether every configured model path has complete dollar pricing."""
        return not self.budget_pricing_gap


@dataclass(frozen=True)
class HarnessConfig:
    repo: str
    # Exact operator spelling retained only to adopt pre-canonical active
    # control ownership during upgrade. It does not affect equality/config IDs.
    repo_input: str = field(default="", repr=False, compare=False)
    base_branch: str = "dev"
    target_dir: str = ""
    identity_mode: IdentityMode = IdentityMode.THREE
    fix_bot: str = ""
    review_bot: str = "my-reviewer-bot[bot]"
    state_dir: str = ""
    review_state_dir: str = ""
    control_authority_root: str = ""
    # Explicit ancestry produced only by `issue-agent start --write-config`.
    # It authorizes adopting and reaping those exact prior deployments during
    # a supported checkout/repository repoint; arbitrary siblings stay isolated.
    repointed_from_deployments: tuple[tuple[str, str], ...] = field(
        default=(), repr=False, compare=False
    )
    worktree_root: str = ".claude/worktrees"
    # review S7: this literal used to disagree with from_env's own "20" literal
    # (60 here, "20" there) — from_env now reads its default off THIS field, so
    # there is exactly one source and 20 (the value that actually shipped) wins.
    poll_seconds: int = 20
    # Paid fix legs run independently of observation; values >1 admit legs on
    # different issues up to this bound (one leg per issue). Default 2 since 2026-08-02: with one
    # slot a single ADDRESS_REVIEW leg held the whole deployment while other
    # PRs' repairs queued behind it. Review dispatch remains
    # synchronous. Set FIX_DISPATCH_CONCURRENCY=1 to restore serial dispatch.
    fix_dispatch_concurrency: int = 2
    auto_merge: bool = True
    merge_method: str = field(
        default="squash",
        metadata={
            "env": "MERGE_METHOD",
            "coerce": "choice",
            "choices": ("squash", "merge", "rebase"),
        },
    )
    close_issue_on_merge: bool = True
    fix_model: str | None = None
    # FIX_EFFORT: base reasoning effort for the fix legs, runtime-agnostic
    # (harness #16). None = say nothing and leave each runtime on its own default
    # — CODEX_REASONING_EFFORT for codex, the SDK's own for Claude — which is
    # exactly what every deployment did before the knob existed.
    fix_effort: Effort | None = None
    # Codex is the default reviewer runtime, so pin its deployable default here
    # instead of leaving the task un-attributed to the Codex CLI default.
    # gpt-6.1-sol keeps the independent check at least as strong as the fixer;
    # a reviewer at the fixer's own tier approved/cycled at parity.
    review_model: str | None = "gpt-6.1-sol"
    fix_budget_usd: float = 10.0
    # $15 not $8 since 2026-08-09: the sol default reviews at ~2.5x terra's
    # token rates; $8 sized for terra would clip sol mid-review.
    review_budget_usd: float = 15.0
    agent_md: str = ""
    review_prompt: str = ""
    harness_root: str = ""
    # Fixer runtime: "claude" (default, Claude Agent SDK), "codex" (OpenAI Codex
    # CLI, e.g. GPT-5.5), or "glm" (GLM via Z.ai's Anthropic-compatible endpoint —
    # the SAME Claude SDK runner with the profile's env injected). Only the FIXER
    # leg switches. See docs/reference/codex-runner.md.
    fixer_runtime: RuntimeName = RuntimeName.CLAUDE
    # Reviewer runtime: Codex is the deployment default; set "claude" explicitly
    # for the recommended mixed-runtime posture that preserves independent
    # implementer/reviewer failure modes.
    reviewer_runtime: RuntimeName = RuntimeName.CODEX
    codex_bin: str = "codex"
    codex_model: str = ""              # blank → CodexRunner default (GPT-5.5)
    codex_provider_id: str = "openai-custom"  # NOT "openai": reserved by codex-cli ≥0.142
    codex_provider_name: str = "OpenAI"
    # Blank = codex's built-in OpenAI provider + `codex login` OAuth credentials
    # (ChatGPT subscription). Set it only for an OpenAI-compatible gateway, which
    # then requires the CODEX_ENV_KEY env var to hold the key.
    codex_base_url: str = ""
    codex_env_key: str = "OPENAI_API_KEY"
    codex_wire_api: str = field(
        default="responses",
        metadata={
            "env": "CODEX_WIRE_API",
            "coerce": "choice",
            "choices": ("responses", "chat"),
        },
    )
    codex_sandbox: str = field(
        default="danger-full-access",
        metadata={
            "env": "CODEX_SANDBOX",
            "coerce": "choice",
            "choices": ("read-only", "workspace-write", "danger-full-access"),
        },
    )
    # codex exec's own default is LOW effort (live session_meta, 2026-07-27);
    # medium is the deliberate implementer default. "" = leave codex alone.
    codex_reasoning_effort: str = field(
        default="medium",
        metadata={
            "env": "CODEX_REASONING_EFFORT",
            "coerce": "choice",
            "choices": ("", "low", "medium", "high", "xhigh"),
        },
    )
    # FIXER_RUNTIME=glm: path to a Claude Code PROFILE's settings.json (e.g.
    # ~/.cc-glm/settings.json). Its parent directory IS the profile dir, and both
    # the settings `env` block and the profile's `.env` are read at build time to
    # form the env injected into the fixer's SDK subprocess (see
    # adapters/claude_profile.py). Blank is a config error under glm.
    glm_settings_file: str = ""
    # Idle watchdog: max seconds between agent stream events before a run is deemed
    # hung and aborted (retryable). NOT a total-runtime cap — a hard task streams
    # events steadily and never trips it. The one legit long gap is a single
    # long-running tool call (no stream event until it returns); on the SDK path the
    # Bash tool caps a foreground command at ~10 min (longer work must be
    # backgrounded, which returns immediately), so 900s sits safely ABOVE that
    # ceiling while a true hang (∞) still trips. MUST exceed the longest single tool
    # invocation a target repo can run (e.g. an ML repo whose test IS a training
    # run — raise it or set 0 to disable). See the liveness-check enhancement issue
    # for a CPU-aware alternative that removes the threshold guess. 0 disables.
    agent_idle_timeout: float = 900.0
    # Total-runtime ceiling for ONE agent session — the cap the idle watchdog
    # structurally cannot provide. A stream left "limping" by a suspend/resume
    # delivers an event every few minutes: each one resets the idle clock, so a
    # review ran 58 minutes on 3.6s of CPU and never tripped (FAILURE-MODES #60).
    # Only elapsed wall time bounds that shape. 45 min sits well above a genuinely
    # hard resolve (observed p99 ~20 min) while making a limping session recoverable
    # within one poll cycle. Raise it for a repo whose test suite alone runs longer;
    # 0 disables (back to idle-watchdog-only).
    agent_max_runtime: float = 2700.0
    # Largest single decoded SDK stream message. The SDK's own 1MB default is
    # below what one Read of a hi-DPI screenshot produces once base64'd (x4/3),
    # and blowing it kills the transport for the whole session — deterministically,
    # because the retry reads the same file (#13). See
    # adapters.agent_runner.DEFAULT_MAX_BUFFER_BYTES. 0 = leave the SDK default.
    sdk_max_buffer_bytes: int = DEFAULT_MAX_BUFFER_BYTES
    # Codex gateway transport-drop retry: a `response.failed`/`stream disconnected`
    # from the LLM gateway is upstream, not agent error. Retry the whole `codex
    # exec` (resumed) up to N times with jittered exponential backoff (base * 2^n).
    # 0 disables (a single drop → retryable timeout, daemon re-dispatches cold).
    codex_transport_retries: int = 4
    codex_backoff_base: float = 2.0
    # Codex rate-limit (HTTP 429) backoff — separate budget from the network-drop
    # retry above. A 429 waits for the provider throttle window to renew (Azure
    # GPT-5.5: 60s) rather than the ~2s network base, honouring a parsed Retry-After
    # when present. Retrying inside the window just re-fails (observed: 345 rebase).
    codex_rate_limit_backoff_base: float = 60.0
    codex_max_rate_limit_wait: float = 180.0
    codex_max_rate_limit_retries: int = 5
    # Runtime spend accounting (review M1/D12): roles.py prices trusted token
    # usage against these USD-per-million-token rates whenever a provider does
    # not report a billed cost. The legacy CODEX_* names remain configuration
    # compatibility aliases, but the synthetic fallback also keeps Claude/GLM
    # subscription runs visible to issue/repository/global caps.
    #
    # The defaults are gpt-5.6-sol's LIST price ($5 in / $30 out per MTok). On a
    # ChatGPT subscription the real bill is $0 — these dollars are SYNTHETIC.
    # They exist because the quota being spent is real and exhaustible (a
    # free-tier limit killed two RESOLVE runs on 2026-07-27), and a fictional
    # price is the only thing that lets ISSUE/DAILY_SPEND_CAP_USD bound codex
    # TOKEN VOLUME at all. Override with your gateway's real rates if you pay
    # per token; set both to 0 to opt out (spend goes untracked and the caps
    # cannot bound codex).
    codex_cost_per_mtok_in: float = 5.0
    codex_cost_per_mtok_out: float = 30.0
    # PER-MODEL rates, which the two scalars above cannot express (#43). With
    # difficulty routing live (#8/#21) a deployment runs several models — the
    # router on the cheap tier, the resolves on whatever tier was classified —
    # and a flat price makes the tiers indistinguishable in the ledger: a
    # cheaper model can only show up as FEWER tokens, never as a lower rate,
    # which is exactly the axis routing moves. CLAUDE.md's routing guardrail
    # ("split by routed model tier") is unsatisfiable without this.
    #
    # `CODEX_COST_PER_MTOK='model=IN/OUT[/CACHED_IN],...'`, USD per MILLION tokens,
    # e.g. 'gpt-5.6-luna=1.25/10/0.125,gpt-5.6-terra=10/60'. Cached input
    # defaults to one tenth of the ordinary input rate when omitted, and must be
    # positive when given (a $0 cache tier under-counts a cache-heavy leg by
    # ~7x — #89). Every leg is priced with
    # the model it ACTUALLY ran (task.model, or the runner's default when the
    # task pinned none). A model absent from the table — and the whole knob
    # when unset — falls back to the scalars above, so an existing deployment
    # prices exactly as it did before.
    codex_cost_per_mtok: tuple[tuple[str, float, float], ...] = ()
    codex_cost_per_mtok_cached_in: tuple[tuple[str, float], ...] = ()
    # #29: whether MAX_BUDGET_USD actually BOUNDS a codex run. codex has no
    # per-session budget flag, so the harness enforces it by sampling the
    # session's streamed token counters and killing the run once this leg's
    # priced spend crosses the budget (ExitReason.BUDGET_EXHAUSTED, the same
    # exit the SDK's own `error_max_budget_usd` maps to). On by default: the
    # alternative shipped a startup line asserting a $20 ceiling while a resolve
    # ran to $28.74 and exited `ok`.
    #
    # Set CODEX_ENFORCE_BUDGET=0 to go back to wall-cap-only bounding — worth it
    # if your rates are synthetic enough that a threshold kill would cut real
    # work short. Doing so is REPORTED (describe() and the startup warning both
    # say NOT ENFORCED and why), which is the half of #29 that was broken:
    # the operator has to be able to tell which regime they are in.
    codex_enforce_budget: bool = True
    # ── spend circuit breakers ────────────────────────────────────────────────
    # Per-agent budgets (MAX_BUDGET_USD et al) bound ONE session; nothing bounded
    # the aggregate, so a leg that retries forever spent without limit while every
    # individual run stayed under budget. SQLite reservations enforce all three
    # atomically: lifetime issue spend, current-period repository spend, and
    # current-period host-global spend.
    # 0 disables any of them.
    issue_spend_cap_usd: float = 25.0
    daily_spend_cap_usd: float = 700.0
    global_daily_spend_cap_usd: float = 0.0
    # Shell command fed each spend_block / escalation event as JSON on stdin.
    # Blank = no alerting (the events are still journalled).
    alert_cmd: str = ""
    # Malformed-numeric fallbacks recorded by from_env (review M2): a bad
    # POLL_SECONDS/MAX_BUDGET_USD/etc must not traceback at startup — from_env
    # substitutes the field default and records what it did here, so
    # validation_errors() can surface it as a readable, fail-loud entry instead
    # of an opaque ValueError three frames deep in int()/float(). Not itself an
    # env knob; never set this directly.
    parse_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        raw = self.repo_input or self.repo
        object.__setattr__(self, "repo_input", raw.strip())
        object.__setattr__(self, "repo", canonical_repository(self.repo))

    @property
    def config_version(self) -> str:
        """Stable identity of the immutable snapshot used by one admission."""

        return hashlib.sha256(repr(self).encode()).hexdigest()[:16]

    @property
    def tick_interval_seconds(self) -> int:
        """What a daemon actually sleeps between ticks. One source of truth for
        the poll-vs-reconcile choice, so no role can wire the wrong one."""
        return self.poll_seconds

    @property
    def repository(self) -> RepositoryConfig:
        return RepositoryConfig(
            repo=self.repo,
            base_branch=self.base_branch,
            target_dir=self.target_dir,
            identity_mode=self.identity_mode,
        )

    @property
    def runtime_paths(self) -> RuntimePathsConfig:
        return RuntimePathsConfig(
            state_dir=self.state_dir,
            review_state_dir=self.review_state_dir,
            worktree_root=self.worktree_root,
            harness_root=self.harness_root,
        )

    @property
    def fix_runtime(self) -> FixRuntimeConfig:
        return FixRuntimeConfig(
            bot=self.fix_bot,
            model=self.fix_model,
            effort=self.fix_effort,
            budget_usd=self.fix_budget_usd,
        )

    @property
    def review_runtime(self) -> ReviewRuntimeConfig:
        return ReviewRuntimeConfig(
            bot=self.review_bot,
            runtime=self.reviewer_runtime,
            model=self.review_model,
            prompt=self.review_prompt,
            budget_usd=self.review_budget_usd,
        )

    @property
    def codex_runtime(self) -> CodexRuntimeConfig:
        return CodexRuntimeConfig(
            binary=self.codex_bin,
            model=self.codex_model,
            provider_id=self.codex_provider_id,
            provider_name=self.codex_provider_name,
            base_url=self.codex_base_url,
            env_key=self.codex_env_key,
            wire_api=self.codex_wire_api,
            sandbox=self.codex_sandbox,
            reasoning_effort=self.codex_reasoning_effort,
            idle_timeout=self.agent_idle_timeout,
            max_runtime=self.agent_max_runtime,
            transport_retries=self.codex_transport_retries,
            backoff_base=self.codex_backoff_base,
            rate_limit_backoff_base=self.codex_rate_limit_backoff_base,
            max_rate_limit_wait=self.codex_max_rate_limit_wait,
            max_rate_limit_retries=self.codex_max_rate_limit_retries,
            cost_per_mtok_in=self.codex_cost_per_mtok_in,
            cost_per_mtok_out=self.codex_cost_per_mtok_out,
            cost_per_mtok=self.codex_cost_per_mtok,
            cost_per_mtok_cached_in=self.codex_cost_per_mtok_cached_in,
            enforce_budget=self.codex_enforce_budget,
        )

    def validation_errors(self) -> tuple[str, ...]:
        # M2: malformed numerics recorded by from_env come first — they are the
        # loudest class of misconfiguration (a typo that would otherwise
        # traceback three frames deep in int()/float()) and must never be lost
        # behind a range check on the substituted default.
        errors: list[str] = list(self.parse_errors)
        _require_positive(errors, "POLL_SECONDS", self.poll_seconds)
        _require_positive(
            errors, "FIX_DISPATCH_CONCURRENCY", self.fix_dispatch_concurrency
        )
        if not (self.repo or "").strip():
            # Fail LOUD at startup, not with an opaque gh error on the first
            # poll: an empty REPO means a missing/mis-composed overlay (the
            # silent-default class, #50).
            errors.append("REPO must be set (owner/name) — is the repo overlay composed?")
        elif detail := repository_validation_error(self.repo):
            errors.append(f"REPO {self.repo!r} {detail}")
        if any(
            repository != self.repo
            for repository, _target_dir in self.repointed_from_deployments
        ):
            errors.append(
                f"{REPOINT_HISTORY_ENV} can only contain the current REPO; "
                "cross-repository overlay reuse requires a different --name"
            )
        if not (self.fix_bot or "").strip():
            # review P1R-7: an unset FIX_BOT used to pass validate() silently.
            # Worse, `login_eq("", "")` used to be True, so with fix_bot=="" a
            # PR whose author the adapter could not resolve (`author: null` ->
            # "", e.g. a deleted/ghost account) read as OURS — the opposite of
            # the fail-safe the authorship filter's own comment claimed.
            # login_eq now refuses to match two unknowns (see identity.py),
            # but a harness that never has a real FIX_BOT identity to compare
            # against is still misconfigured and must not start silently —
            # `install-systemd.sh` starts the unit directly and never runs
            # doctor, so this is the ONLY gate on the production path.
            errors.append(
                "FIX_BOT must be set — an unset FIX_BOT means the fixer's own "
                "PRs/reviews can never be identified, and (pre-P1R-7) let an "
                "unresolvable PR author read as the fixer's own"
            )
        if self.identity_mode == IdentityMode.THREE and normalize_login(self.fix_bot) == normalize_login(
            self.review_bot
        ):
            # review C1/D1 belt-and-braces: a raw string compare would miss
            # `fix-bot` vs `fix-bot[bot]` and let a self-approving identity
            # pair through — GitHub then 422s every approval, forever.
            errors.append(
                "FIX_BOT and REVIEW_BOT normalize to the same identity under "
                "IDENTITY_MODE=three (GitHub rejects self-approval with a 422)"
            )
        for name, value in (
            ("MAX_BUDGET_USD", self.fix_budget_usd),
            ("REVIEW_BUDGET_USD", self.review_budget_usd),
        ):
            _require_positive(errors, name, value)
        if self.fixer_runtime is RuntimeName.GLM and not self.glm_settings_file.strip():
            # The glm runtime IS "the Claude runner plus a profile's env"; with no
            # profile there is nothing to inject and the fixer would silently run
            # on Anthropic credentials instead. Existence of the file is a
            # build-time check (roles); emptiness is a config error.
            errors.append("GLM_SETTINGS_FILE is required when FIXER_RUNTIME=glm")
        _require_non_negative(errors, "AGENT_IDLE_TIMEOUT_SECONDS", self.agent_idle_timeout)
        _require_non_negative(errors, "AGENT_MAX_RUNTIME_SECONDS", self.agent_max_runtime)
        _require_non_negative(errors, "SDK_MAX_BUFFER_BYTES", self.sdk_max_buffer_bytes)
        _require_non_negative(errors, "CODEX_TRANSPORT_RETRIES", self.codex_transport_retries)
        _require_positive(errors, "CODEX_BACKOFF_BASE_SECONDS", self.codex_backoff_base)
        _require_non_negative(errors, "CODEX_MAX_RATE_LIMIT_RETRIES", self.codex_max_rate_limit_retries)
        _require_positive(errors, "CODEX_RATE_LIMIT_BACKOFF_BASE_SECONDS", self.codex_rate_limit_backoff_base)
        _require_positive(errors, "CODEX_MAX_RATE_LIMIT_WAIT_SECONDS", self.codex_max_rate_limit_wait)
        _require_non_negative(errors, "CODEX_COST_PER_MTOK_IN", self.codex_cost_per_mtok_in)
        _require_non_negative(errors, "CODEX_COST_PER_MTOK_OUT", self.codex_cost_per_mtok_out)
        _require_non_negative(errors, "ISSUE_SPEND_CAP_USD", self.issue_spend_cap_usd)
        _require_non_negative(errors, "DAILY_SPEND_CAP_USD", self.daily_spend_cap_usd)
        _require_non_negative(
            errors, "GLOBAL_DAILY_SPEND_CAP_USD", self.global_daily_spend_cap_usd
        )
        caps_armed = any(
            cap > 0
            for cap in (
                self.issue_spend_cap_usd,
                self.daily_spend_cap_usd,
                self.global_daily_spend_cap_usd,
            )
        )
        if caps_armed and not self.codex_budget_priceable:
            errors.append(
                "armed spend caps require positive input/output pricing; "
                "set CODEX_COST_PER_MTOK_IN/OUT or disable every spend cap"
            )
        return tuple(errors)

    def validate(self) -> None:
        errors = self.validation_errors()
        if errors:
            raise ValueError("invalid harness config: " + "; ".join(errors))

    @property
    def codex_budget_priceable(self) -> bool:
        """Complete pricing includes rejecting malformed configured overrides."""

        return (
            self.codex_runtime.budget_priceable
            and not any(
                error.startswith("CODEX_COST_PER_MTOK ")
                for error in self.parse_errors
            )
        )

    def describe(self) -> str:
        """Resolved-config summary printed at daemon startup (lean P5.1): the
        silent-default class (#16/#49/#50/#78) is only diagnosable when the
        EFFECTIVE values are visible somewhere. Curated, one knob-group per
        line; never includes secrets (tokens/keys live in env, not here)."""
        def model(m: str | None) -> str:
            return m or "(sdk default)"

        # #29: MAX_BUDGET_USD is the SDK's per-session bound and codex has no
        # analogue FLAG, so this number used to bound nothing on that runtime —
        # a keifu resolve ran to $28.74 against a configured $20 and exited
        # `ok`. The harness now enforces it itself (CodexRunner.enforce_budget:
        # sample the streamed counters, kill on crossing), so the honest report
        # depends on whether that check can actually fire. Printing one fixed
        # verdict either way is the startup log asserting something it hasn't
        # checked — which is the bug this line is here to not repeat.
        fix_budget = f"fix_budget=${self.fix_budget_usd:.2f}"
        if self.fixer_runtime is RuntimeName.CODEX and self.fix_budget_usd > 0:
            # Every fallback and per-model rate needs both token classes priced.
            # One zero leaves a routed model able to spend outside the meter.
            priced = self.codex_budget_priceable
            if self.codex_enforce_budget and priced:
                fallback = (
                    f"wall cap {self.agent_max_runtime:.0f}s remains the fallback"
                    if self.agent_max_runtime > 0
                    else "AGENT_MAX_RUNTIME_SECONDS is unset"
                )
                fix_budget += (
                    " (native flag NOT ENFORCED on codex; harness-enforced on "
                    "codex — the run is rejected once streamed usage crosses it; "
                    f"{fallback})"
                )
            else:
                why = (
                    "CODEX_ENFORCE_BUDGET=0"
                    if not self.codex_enforce_budget
                    else "both input and output prices must be set, so the "
                    "dollar threshold is not measurable"
                )
                bound = (
                    f"wall cap {self.agent_max_runtime:.0f}s is the only per-run bound"
                    if self.agent_max_runtime > 0
                    else "NOTHING bounds a run — AGENT_MAX_RUNTIME_SECONDS is unset"
                )
                fix_budget += f" (NOT ENFORCED on codex, {why} — {bound})"

        cadence = f"poll={self.poll_seconds}s"

        return "\n".join([
            f"repo={self.repo or '(UNSET)'} base={self.base_branch} "
            # getattr, not IdentityMode(...): describe() is the startup diagnostic
            # and must print whatever it was handed rather than raise on it.
            f"identity={getattr(self.identity_mode, 'value', self.identity_mode)} "
            f"{cadence}",
            f"fixer={self.fixer_runtime.value} fix_model={model(self.fix_model)} "
            f"fix_effort={getattr(self.fix_effort, 'value', None) or '(runtime default)'} "
            f"dispatch_concurrency={self.fix_dispatch_concurrency} "
            f"{fix_budget}",
            f"reviewer={self.reviewer_runtime.value} review_model={model(self.review_model)} "
            f"review_budget=${self.review_budget_usd:.2f}",
            f"timeouts: idle={self.agent_idle_timeout:.0f}s "
            f"wall={self.agent_max_runtime:.0f}s",
            f"spend_caps: issue=${self.issue_spend_cap_usd:.2f} "
            f"daily/{self.repo or 'repo'}=${self.daily_spend_cap_usd:.2f} "
            f"daily/global="
            f"{f'${self.global_daily_spend_cap_usd:.2f}' if self.global_daily_spend_cap_usd > 0 else 'off'} "
            f"alert={describe_command(self.alert_cmd, empty='none')}",
            f"state={self.state_dir or '(unset)'} "
            f"control={self.control_authority_root or '(deployment state parent)'} "
            f"target={self.target_dir or '(cwd)'} "
            f"repoint_ancestors={len(self.repointed_from_deployments)}",
        ])


def render_agent_md(text: str, cfg: "HarnessConfig") -> str:
    """Expand the AGENT.md template's $VARs. The bash harness never did this
    (it cat'd the file raw), so Claude saw literal `$DIAGNOSE_RULES` etc. and
    ignored them — but GPT-5.5 takes them literally (went hunting for a
    `diagnose` skill). Expand config-derived vars; strip the optional rule
    blocks (they were env-injected and are empty here). $ISSUE_NUMBER stays a
    literal — the agent fills it per-issue from the prompt."""
    repl = {
        "$BASE_BRANCH": cfg.base_branch,
        "$HARNESS_REPO": cfg.repo,
        "$DIAGNOSE_RULES": "", "$LOGGING_RULES": "",
        "$DOC_RULES": "", "$TEST_RULES": "",
    }
    for k, v in repl.items():
        text = text.replace(k, v)
    return text


# review P1R-8: the recognized true/false spellings. Deliberately explicit
# (not "anything not in the false set is true") — an unrecognized token now
# falls back to `default` + a validation-error entry instead of silently
# defaulting truthy, matching `_coerce_int`'s malformed-value contract.
_TRUE_TOKENS = frozenset({"1", "true", "yes", "on"})
_FALSE_TOKENS = frozenset({"0", "false", "no", "off"})


def _b(env: dict, key: str, default: bool, errors: list[str]) -> bool:
    v = env.get(key)
    if v is None or v.strip() == "":
        # review M2: an exported-but-empty knob (AUTO_MERGE= with nothing after
        # the `=`) must mean UNSET, not False — a raw `v not in (...)` treated
        # "" as a falsy token, silently flipping every default-ON flag off.
        return default
    # review P1R-8: compared unstripped and case-sensitively — `AUTO_MERGE=False`
    # (capital F) matched neither raw string in the old false-tuple, so it fell
    # through to the "anything else is true" branch and turned auto-merge ON
    # when the operator explicitly asked for it off. Every boolean knob shared
    # this bug. strip().casefold() first, so "False"/" FALSE "/"no"/"Off" all
    # normalize the same way "fix-bot[BOT]" does in identity.py.
    token = v.strip().casefold()
    if token in _TRUE_TOKENS:
        return True
    if token in _FALSE_TOKENS:
        return False
    errors.append(
        f"{key} must be one of {sorted(_TRUE_TOKENS | _FALSE_TOKENS)}, "
        f"got {v!r} — using default {default}"
    )
    return default


def _effort(env: dict, key: str, default: Effort | None, errors: list[str]) -> Effort | None:
    """FIX_EFFORT → Effort. Empty/unset means UNSET (leave the runtime's own
    default alone), same rule `_b` applies to booleans. A typo is reported and
    then falls back — a misspelled effort must not take the daemon down, but it
    must not be silent either, or the operator believes a knob is live that
    isn't (the #12 failure shape)."""
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    parsed = parse_effort(raw, None)
    if parsed is None:
        errors.append(
            f"{key} must be one of {[e.value for e in EFFORT_LADDER]}, "
            f"got {raw!r} — using default {getattr(default, 'value', None)}"
        )
        return default
    return parsed


def _runtime(
    env: dict,
    key: str,
    default: RuntimeName,
    allowed: frozenset[RuntimeName],
    errors: list[str],
) -> RuntimeName:
    raw = env.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        runtime = RuntimeName(str(raw).strip())
    except ValueError:
        errors.append(
            f"{key} must be one of {[item.value for item in sorted(allowed, key=lambda item: item.value)]}, "
            f"got {raw!r} — using default {default.value}"
        )
        return default
    if runtime not in allowed:
        errors.append(
            f"{key} must be one of {[item.value for item in sorted(allowed, key=lambda item: item.value)]}, "
            f"got {raw!r} — using default {default.value}"
        )
        return default
    return runtime


def _require_non_negative(errors: list[str], name: str, value: int | float) -> None:
    if value < 0:
        errors.append(f"{name} must be >= 0")


def _require_positive(errors: list[str], name: str, value: int | float) -> None:
    if value <= 0:
        errors.append(f"{name} must be > 0")


# review M2: the single source of truth for "what does an absent/empty numeric
# knob default to" is the HarnessConfig dataclass field itself — from_env reads
# it here instead of restating a literal (the drift class that produced the
# POLL_SECONDS 60-vs-20 mismatch, review S7).
_INTERNAL_CONFIG_FIELDS = frozenset(
    {
        "agent_md",
        "codex_cost_per_mtok_cached_in",
        "control_authority_root",
        "parse_errors",
        "repointed_from_deployments",
        "repo_input",
    }
)
_ENV_NAME_OVERRIDES = {
    "fix_budget_usd": "MAX_BUDGET_USD",
    "harness_root": "HARNESS_ROOT_DIR",
    "agent_idle_timeout": "AGENT_IDLE_TIMEOUT_SECONDS",
    "agent_max_runtime": "AGENT_MAX_RUNTIME_SECONDS",
    "codex_backoff_base": "CODEX_BACKOFF_BASE_SECONDS",
    "codex_rate_limit_backoff_base": "CODEX_RATE_LIMIT_BACKOFF_BASE_SECONDS",
    "codex_max_rate_limit_wait": "CODEX_MAX_RATE_LIMIT_WAIT_SECONDS",
}
_COERCE_OVERRIDES: dict[str, str] = {
    "identity_mode": "identity_mode",
    "state_dir": "state_dir",
    "review_state_dir": "state_dir",
    "fix_effort": "effort",
    "review_model": "review_model",
    "review_prompt": "stripped",
    "fixer_runtime": "runtime",
    "reviewer_runtime": "runtime",
    "codex_cost_per_mtok": "rate_table",
}
_CHOICE_OVERRIDES: dict[str, tuple[str, ...]] = {
    "identity_mode": tuple(item.value for item in IdentityMode),
    "fix_effort": tuple(item.value for item in EFFORT_LADDER),
    "fixer_runtime": tuple(item.value for item in RuntimeName),
    "reviewer_runtime": (RuntimeName.CLAUDE.value, RuntimeName.CODEX.value),
}


def _default_coercion(definition) -> str:
    default = definition.default
    if isinstance(default, bool):
        return "bool"
    if isinstance(default, int):
        return "int"
    if isinstance(default, float):
        return "float"
    if default is None:
        return "optional_string"
    return "string"


def _install_config_metadata() -> None:
    """Attach the single env/coercion table to every operator-facing field."""

    for definition in fields(HarnessConfig):
        if definition.name in _INTERNAL_CONFIG_FIELDS:
            continue
        existing = dict(definition.metadata)
        metadata = {
            "env": _ENV_NAME_OVERRIDES.get(
                definition.name, definition.name.upper()
            ),
            "coerce": _COERCE_OVERRIDES.get(
                definition.name,
                str(existing.get("coerce") or _default_coercion(definition)),
            ),
        }
        choices = existing.get("choices") or _CHOICE_OVERRIDES.get(
            definition.name
        )
        if choices is not None:
            metadata["choices"] = tuple(choices)
        definition.metadata = MappingProxyType(metadata)


_install_config_metadata()
_FIELD_DEFINITIONS = {item.name: item for item in fields(HarnessConfig)}
_FIELD_DEFAULTS = {name: item.default for name, item in _FIELD_DEFINITIONS.items()}


def _dc_default(field: str):
    return _FIELD_DEFAULTS[field]


def _env_name(field_name: str) -> str:
    return str(_FIELD_DEFINITIONS[field_name].metadata["env"])


def _metadata_choice(
    env: dict, field_name: str, errors: list[str]
) -> str:
    """Parse a choice field from its dataclass metadata.

    Adding a choice knob has one source of truth for env name, default, coercion,
    validation, and generated reference rendering.
    """

    definition = _FIELD_DEFINITIONS[field_name]
    env_name = str(definition.metadata["env"])
    choices = tuple(str(item) for item in definition.metadata["choices"])
    default = str(definition.default)
    raw = env.get(env_name)
    if raw is None or str(raw).strip() == "":
        return default
    value = str(raw).strip()
    if value not in choices:
        errors.append(
            f"{env_name} must be one of {list(choices)}, got {raw!r} "
            f"— using default {default!r}"
        )
        return default
    return value


def generated_env_reference() -> str:
    """Render the complete operator env template from field metadata."""

    lines = [
        "# Generated from HarnessConfig metadata; do not hand-edit.",
        "# Set REPO and FIX_BOT before starting a daemon.",
        "",
    ]
    for definition in fields(HarnessConfig):
        metadata = definition.metadata
        if not metadata:
            continue
        default = definition.default
        if default is MISSING or default is None or isinstance(default, tuple):
            rendered = ""
        elif isinstance(default, bool):
            rendered = "1" if default else "0"
        elif isinstance(default, Enum):
            rendered = str(default.value)
        else:
            rendered = str(default)
        choices = metadata.get("choices")
        suffix = (
            "  # choices: " + "|".join(str(item) for item in choices)
            if choices
            else f"  # {metadata['coerce']}"
        )
        lines.append(
            f"{metadata['env']}={json.dumps(rendered, ensure_ascii=False)}{suffix}"
        )
    return "\n".join(lines) + "\n"


def _metadata_value(
    env: dict,
    field_name: str,
    errors: list[str],
    *,
    default: object = MISSING,
) -> object:
    """Parse one field entirely from its metadata definition."""

    definition = _FIELD_DEFINITIONS[field_name]
    fallback = definition.default if default is MISSING else default
    coercion = str(definition.metadata["coerce"])
    env_name = _env_name(field_name)
    if coercion == "bool":
        return _b(env, env_name, bool(fallback), errors)
    if coercion == "int":
        return _coerce_int(env, env_name, int(fallback), errors)
    if coercion == "float":
        return _coerce_float(env, env_name, float(fallback), errors)
    if coercion == "choice":
        return _metadata_choice(env, field_name, errors)
    if coercion == "identity_mode":
        return _coerce_identity_mode(env, errors)
    if coercion == "effort":
        return _effort(env, env_name, fallback, errors)
    if coercion == "runtime":
        allowed = frozenset(
            RuntimeName(value) for value in definition.metadata["choices"]
        )
        return _runtime(env, env_name, fallback, allowed, errors)
    raw = env.get(env_name)
    if raw is None:
        return fallback
    if coercion == "optional_string":
        return str(raw) or None
    if coercion == "stripped":
        return str(raw).strip()
    if coercion in {"string", "state_dir", "review_model"}:
        return str(raw)
    raise RuntimeError(
        f"unsupported config coercion {coercion!r} for {field_name}"
    )


def _coerce_int(env: dict, key: str, default: int, errors: list[str]) -> int:
    """Empty/absent → `default` (unset, not an error). A malformed value also
    falls back to `default` but is recorded in `errors` so it reaches
    validation_errors() as a readable entry — config construction must never
    traceback on operator input (review M2: `POLL_SECONDS=abc` used to raise a
    bare ValueError three frames inside int())."""
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        errors.append(f"{key} must be an integer, got {raw!r} — using default {default}")
        return default


def _coerce_float(env: dict, key: str, default: float, errors: list[str]) -> float:
    """Float twin of `_coerce_int` — same empty=unset / malformed=fallback+error
    contract. Closes the ISSUE_SPEND_CAP_USD hole specifically: `float(v or 0)`
    used to make an exported-but-empty cap silently mean "disabled" instead of
    "use the default".

    review P1R-9: bare `float()` also happily parses `"nan"`/`"inf"`/`"1e400"`,
    and every range check downstream (`_require_non_negative`: `value < 0`) is
    False for `nan` — so `ISSUE_SPEND_CAP_USD=nan` disabled the cap it names
    and `AGENT_MAX_RUNTIME_SECONDS=nan` disabled the wall cap, both silently.
    `math.isfinite` rejects non-finite results the same way a genuinely
    malformed string is rejected: fall back to `default`, record why."""
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        errors.append(f"{key} must be a number, got {raw!r} — using default {default}")
        return default
    if not math.isfinite(value):
        errors.append(
            f"{key} must be a finite number, got {raw!r} — using default {default}"
        )
        return default
    return value


def _coerce_rate_tables(
    env: dict, key: str, errors: list[str]
) -> tuple[tuple[tuple[str, float, float], ...], tuple[tuple[str, float], ...]]:
    """Parse normal and cached tiers in one pass so their entries cannot drift."""

    table, cached, parse_errors = parse_model_rate_table(env.get(key, ""))
    errors.extend(f"{key} {error} — ignoring it" for error in parse_errors)
    return table, cached


def _coerce_identity_mode(env: dict, errors: list[str]) -> IdentityMode:
    """IDENTITY_MODE → `IdentityMode`, with the same contract as `_coerce_int`:
    empty/absent = unset (the field default), malformed = the default PLUS a
    readable entry in validation_errors() (review M16).

    `IDENTITY_MODE=Three` used to pass straight through as a string and land
    BETWEEN the two guards that read it — `!= "three"` disabled auto-merge while
    `== "solo"` left the reviewer enabled, so the harness reviewed, approved and
    then never merged anything, with no log line naming the cause. There is no
    safe silent fallback for this knob, so the value has to be named as wrong at
    startup where `validate()` can refuse to run on it."""
    raw = env.get("IDENTITY_MODE")
    default: IdentityMode = _dc_default("identity_mode")
    if raw is None or raw.strip() == "":
        return default
    try:
        return IdentityMode.from_env(raw)
    except ValueError:
        allowed = "|".join(m.value for m in IdentityMode)
        errors.append(
            f"IDENTITY_MODE must be exactly one of {allowed}, got {raw!r} "
            f"(case-sensitive — the bash half compares it literally) — "
            f"using default {default.value}"
        )
        return default


def _repo_slug(repo: str) -> str:
    """`owner/name` → a filesystem-safe, COLLISION-FREE per-repo state-dir
    component (review M5/D7; separator fixed under R2-10). Unset/blank REPO
    falls back to a fixed "unknown" slug rather than collapsing every
    repo-less config onto the bare role dir (which would recreate the exact
    collision this fix closes).

    R2-10: the prior `owner/name -> owner-name` join (replacing "/" with "-")
    collided whenever EITHER segment itself contained a hyphen — `acme/app-b`
    and `acme-app/b` both produced `acme-app-b`, because "-" is legal inside
    both a GitHub owner login and a repo name, so the join character wasn't
    distinguishable from a character either side could already contain.

    "_" is NOT a legal character in a GitHub owner/org login (owners are
    alphanumeric + single, non-leading/trailing, non-consecutive hyphens
    only), so joining on "__" is provably unambiguous: whatever the repo name
    contains, the FIRST "__" in the slug is always the true owner/name
    boundary, because no valid owner can contain "_" at all (so no owner-only
    prefix can itself contain "__"). Two distinct (owner, name) pairs can
    therefore never produce the same slug.

    Casefolded too — GitHub owner/repo names are case-insensitive identity
    (`Acme/Widget` and `acme/widget` are the SAME repo), so case-variant
    spellings share one state dir instead of silently splitting it in two."""
    repo = (repo or "").strip()
    if not repo:
        return "unknown"
    return repo.replace("/", "__").casefold()


def _default_state_dir(repo: str, role_dir: str) -> str:
    """Default STATE_DIR/REVIEW_STATE_DIR (review M5/D7): the
    old default was per-role but not per-repo, so two repos' fix daemons shared
    `issue-N.session` etc. — issue #5 in repo B would resume repo A's
    transcript. Nothing real is deployed yet, so the default itself moves
    per-repo; an operator who already set the env var explicitly is unaffected
    (from_env only calls this when the var is absent/empty)."""
    return os.path.expanduser(f"~/.local/state/{role_dir}/{_repo_slug(repo)}")


def default_state_dir(repo: str, role_dir: str) -> str:
    """The state dir a daemon derives when the overlay carries no explicit
    STATE_DIR/REVIEW_STATE_DIR — the public seam for
    read-side callers (status/dashboard journal discovery). Canonicalizes the
    repo the same way from_env does, so a raw overlay REPO value ("owner/name",
    URL forms, case variants) lands on the SAME directory the daemon writes.
    Any read-side fallback that derives this path differently goes blind
    exactly when the overlay is hand-written (the keifu 2026-08-02 incident:
    dashboard showed settled spend with zero runs)."""
    return _default_state_dir(canonical_repository(repo), role_dir)


# Names an env file textually ASSIGNS. Same anchor as bin/compose-env.sh's
# scan: a commented-out `# FOO=bar` is not a definition, `export FOO=` and
# leading whitespace are. `.active.env` is machine-written one-line
# `KEY=<%q-escaped>` pairs, so a per-line scan cannot straddle a value.
_ASSIGNMENT_RE = re.compile(
    r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)=", re.MULTILINE
)


def _file_env(path: str, inherited: dict) -> dict[str, str] | None:
    """`{name: value}` for every variable the env file at `path` ASSIGNS, or
    `None` when the file cannot be read or sourced (caller falls back to
    `inherited` — a broken read must never take the daemon with it, same
    fail-open rule as adapters/reload.py).

    The file is sourced by bash UNDER `inherited`, so the parameter-expansion
    idiom operators write (`FOO="${FOO:-default}"`, `$HOME/...`) resolves
    exactly as it does for bin/point-at.sh. The result is then RESTRICTED to
    the names the file itself assigns: an inherited variable the file does not
    mention is dropped, which is the whole point of #12.

    Names-plus-values (rather than a pre/post diff) for compose-env.sh's
    P0R-1 reason: a file assigning the value the environment already carries
    is invisible to a diff, and silently defaulting TEST_CMD/REPO that way
    fails every gate closed.
    """
    try:
        assigned = {
            m.group(1)
            for m in _ASSIGNMENT_RE.finditer(
                open(path, encoding="utf-8", errors="replace").read()
            )
        }
        # An empty/unreadable/garbled file assigns nothing — that reads as
        # "every knob absent", i.e. a silent full reset to defaults. Refuse it
        # and let the caller keep the inherited environment.
        if not assigned:
            return None
        return source_env_file(path, inherited, assigned_only=True)
    except Exception:  # noqa: BLE001 — config load must not die on a bad file
        return None


def _authoritative_env(e: dict) -> dict:
    """The env `from_env` maps, with AGENT_ENV's FILE as the authority (#12).

    Precedence, when AGENT_ENV names a readable file:

    1. a knob the file assigns → the file's value, always (it beats anything
       inherited);
    2. a knob the file does NOT assign → absent, so the HarnessConfig dataclass
       default applies — it does NOT fall back to the inherited value.

    Rule 2 is the fix. bin/point-at.sh's relaunch loop re-SOURCES the composed
    file into one long-lived wrapper shell, and sourcing can overwrite but
    never unset: a knob the operator DELETED from .active.env stayed exported
    from the previous reload and was inherited by every later exec (observed
    2026-07-27 — a removed FIX_MODEL kept difficulty routing disabled while the
    same reload's DAILY_SPEND_CAP_USD change did apply). The wrapper's
    environment is therefore not trustworthy for harness knobs; the file is,
    which is exactly what the mtime watch already promises the operator.

    The cost is that exporting a knob in the launching shell without putting it
    in shared.env/repos/<name>.env no longer reaches the daemon — the file is
    the only channel.

    With no AGENT_ENV, or a file that cannot be read, the passed-in environment
    is used unchanged (fail open).
    """
    path = e.get("AGENT_ENV", "")
    if not path or not os.access(path, os.R_OK):
        return e
    parsed = _file_env(path, e)
    return e if parsed is None else parsed


def from_env(env: dict | None = None) -> HarnessConfig:
    """Build one immutable snapshot from the metadata-backed config table."""

    e = _authoritative_env(env if env is not None else os.environ)
    errors: list[str] = []
    special = {
        "state_dir",
        "review_state_dir",
        "review_model",
        "codex_cost_per_mtok",
    }
    values: dict[str, object] = {}
    for definition in fields(HarnessConfig):
        if not definition.metadata or definition.name in special:
            continue
        values[definition.name] = _metadata_value(
            e,
            definition.name,
            errors,
            default=("" if definition.name == "repo" else MISSING),
        )

    repo_input = str(values["repo"]).strip()
    repo = canonical_repository(repo_input)
    values["repo"] = repo
    values["repo_input"] = repo_input
    reviewer_runtime = values["reviewer_runtime"]
    if not isinstance(reviewer_runtime, RuntimeName):
        raise RuntimeError("reviewer runtime metadata did not produce RuntimeName")

    values.update(
        state_dir=(
            str(_metadata_value(e, "state_dir", errors))
            or _default_state_dir(repo, "issue-agent")
        ),
        review_state_dir=(
            str(_metadata_value(e, "review_state_dir", errors))
            or _default_state_dir(repo, "review-agent")
        ),
        review_model=(
            str(e.get(_env_name("review_model")) or "")
            or (
                "opus"
                if reviewer_runtime is RuntimeName.CLAUDE
                else _dc_default("review_model")
            )
        ),
    )
    rates, cached_rates = _coerce_rate_tables(
        e, _env_name("codex_cost_per_mtok"), errors
    )
    values["codex_cost_per_mtok"] = rates
    values["codex_cost_per_mtok_cached_in"] = cached_rates
    try:
        values["repointed_from_deployments"] = parse_repoint_history(
            str(e.get(REPOINT_HISTORY_ENV) or "")
        )
    except ValueError as exc:
        errors.append(str(exc))
        values["repointed_from_deployments"] = ()
    values["control_authority_root"] = machine_control_authority_root()
    values["parse_errors"] = tuple(errors)
    return HarnessConfig(**values)
