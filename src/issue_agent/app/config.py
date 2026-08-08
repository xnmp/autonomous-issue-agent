"""Environment → immutable config snapshot under ADR 0007.

Pure mapping `dict → HarnessConfig` so tests construct it without os.environ —
with one deliberate exception: an env carrying `AGENT_ENV` is re-resolved
against that FILE first (`_authoritative_env`, #12), because the inherited
environment of point-at.sh's relaunch loop cannot represent a knob's removal.
Names mirror config/agent.env.example so operators carry the same env file over.
"""

from __future__ import annotations

import math
import os
import re
import hashlib
import json
from dataclasses import MISSING, dataclass, field, fields
from enum import Enum
from types import MappingProxyType

from ..adapters.agent_runner import DEFAULT_MAX_BUFFER_BYTES
from ..adapters.control_path import machine_control_authority_root
from ..adapters.env_file import source_env_file
from ..domain.agent_task import RuntimeName
from ..domain.effect_retry import DEFAULT_EFFECT_RETRY_CEILING
from ..domain.infra_breaker import (
    DEFAULT_BREAKER_COOLDOWN_SECONDS,
    DEFAULT_BREAKER_DEATHS,
    DEFAULT_BREAKER_WINDOW_SECONDS,
)
from ..domain.effort import EFFORT_LADDER, Effort, parse_effort
from ..domain.environment import EVIDENCE_STACKS
from ..domain.identity import normalize_login
from ..domain.pricing import parse_model_rate_table
from ..domain.review import IdentityMode, ReviewMode
from ..domain.steward_stall import DEFAULT_STEWARD_ERROR_CEILING
from ..domain.repository import (
    canonical_repository,
    repository_validation_error,
)


SHELL_CMD_LOG_LIMIT = 120
REPOINT_HISTORY_ENV = "REPOINTED_FROM_DEPLOYMENTS"
MAX_REPOINT_HISTORY = 32


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
    steward_state_dir: str
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
    max_ci_retries: int
    max_review_retries: int
    auto_rebase_conflicts: bool
    max_rebase_retries: int
    max_resolve_retries: int
    max_infra_deaths: int
    author_tests_split: bool
    max_contract_rounds: int
    mcp_config: str


@dataclass(frozen=True)
class ReviewRuntimeConfig:
    bot: str
    runtime: RuntimeName
    model: str | None
    prompt: str
    mode: ReviewMode
    budget_usd: float
    max_rejections: int
    trigger_label: str
    adopt_unlabeled: bool
    gate_model: str | None
    validation_model: str | None
    scout_model: str | None
    gate_budget_usd: float
    dimension_budget_usd: float
    validation_budget_usd: float
    max_unproductive_rounds: int


@dataclass(frozen=True)
class StewardRuntimeConfig:
    bot: str
    runtime: RuntimeName
    triage_enabled: bool
    promote: bool
    owner: str
    triage_prompt: str
    triage_model: str | None
    triage_budget_usd: float
    groom_enabled: bool
    max_infra_deaths: int


@dataclass(frozen=True)
class JiraRuntimeConfig:
    sync: bool
    project_key: str
    cloud_id: str
    mcp_config: str
    mcp_tools: str
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

    @property
    def unpriced_cached_models(self) -> tuple[str, ...]:
        """Models whose cache tier prices at $0 — cached spend is invisible."""
        return tuple(model for model, rate in self.cost_per_mtok_cached_in if rate <= 0)


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
    # ADR 0014 grant-authentication key: derived key material, NOT the raw
    # token (see _derive_exemption_mac_key). repr=False keeps it out of any
    # accidental dataclass dump; describe() never prints it. "" disables
    # grants (declared PRs withhold with an accurate reason).
    exemption_mac_key: str = field(default="", repr=False, compare=False)
    steward_bot: str = "my-reviewer-bot[bot]"
    state_dir: str = ""
    review_state_dir: str = ""
    steward_state_dir: str = ""
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
    # Paid fix legs run independently of observation; values >1 admit disjoint
    # work up to this bound (the path-overlap serializer still keeps same-path
    # and path-unknown legs exclusive). Default 2 since 2026-08-02: with one
    # slot a single ADDRESS_REVIEW leg held the whole deployment while other
    # PRs' repairs queued behind it. Review and steward dispatch remain
    # synchronous. Set FIX_DISPATCH_CONCURRENCY=1 to restore serial dispatch.
    fix_dispatch_concurrency: int = 2
    # Webhook push (#1): `gh webhook forward` runs as a supervised fourth
    # process and touches each role's wake file on every delivery. Deliveries
    # are WAKE SIGNALS ONLY — never acted on as payloads — so the stateless
    # reconcile stays the single source of truth and a missed delivery costs
    # latency, not correctness. With it on, the poll loop demotes to a slow
    # backstop at `reconcile_seconds` (GitHub documents delivery as
    # best-effort, and a dead forwarder must degrade to today's behaviour).
    webhook_wake: bool = False
    reconcile_seconds: int = 300
    # Change-probe gate on review/steward world builds (app/world_probe.py):
    # 1 REST point asks "did any issue/PR update?" before paying ~14 GraphQL
    # points for a full build. The fixer is NEVER probed regardless of this
    # flag — its decide reads CI/mergeability, which the probe cannot see.
    world_probe: bool = True
    world_probe_ttl_seconds: int = 300
    # ARM_ON_OPEN knob: "auto" (default) = at fix-daemon boot, probe the base
    # branch's protection and arm auto-merge at PR creation iff it verifiably
    # requires ≥1 review and ≥1 status check; "0" = never arm early. There is
    # deliberately no "1": forcing early arming without the verified server-side
    # gate would merge unreviewed work.
    arm_on_open: str = field(
        default="auto",
        metadata={
            "env": "ARM_ON_OPEN",
            "coerce": "choice",
            "choices": ("auto", "0"),
        },
    )
    # The probe's boot-time verdict (main.build_role fills it; never from env).
    arm_on_open_active: bool = False
    auto_merge: bool = True
    merge_method: str = field(
        default="squash",
        metadata={
            "env": "MERGE_METHOD",
            "coerce": "choice",
            "choices": ("squash", "merge", "rebase"),
        },
    )
    ci_ignore_checks: str = ""
    close_issue_on_merge: bool = True
    # Bound on how long CI may sit PENDING before the PR goes to a human (#30).
    # CI_IGNORE_CHECKS was the only remedy for a wedged check, and it is a knob,
    # not a guardrail: it cannot fire on the FIRST occurrence, which is the one
    # that matters. Default 3600s; 0 disables. Generous on purpose — real
    # pipelines run well over half an hour and escalating a healthy long run
    # would move the escalation-rate guardrail the wrong way.
    ci_pending_max_seconds: int = 3600
    # Refuse to RESOLVE an issue that an OPEN, not-ours PR already closes (#39):
    # comment naming the PR, then ai-ready → ai-blocked. The evidence was a ≈$32
    # duplicate resolve of an issue the operator's own open PR had already fixed.
    # Advisory — the operator un-blocks by closing the stale PR.
    duplicate_pr_guard: bool = True
    # Give a red PR ONE free `gh run rerun --failed` per head SHA before paying
    # for a CI-fix leg (#38). ≈$80 of one $188 batch went to two repair legs that
    # both re-fixed the same pre-existing flaky test. Costs one CI cycle on a
    # genuinely broken PR; a second failure on the same head pays as before.
    ci_free_rerun: bool = True
    max_ci_retries: int = 3
    # Escalation ceiling for the reviewer: after this many REQUEST_CHANGES verdicts
    # on one PR (counting the reviewer's OWN rejections, NOT author pushes — a
    # rebase/housekeeping push that ends in APPROVE never counts), the next
    # unsatisfied review escalates to a human instead of bouncing back to the
    # fixer. Default 4 = 3 rejections bounce, the 4th escalates.
    max_review_rejections: int = 4
    max_review_retries: int = 3
    # Auto-rebase a PR that goes CONFLICTING/DIRTY after its base advances (the
    # backup plan for a conflict that slips past the pre-dispatch file-overlap
    # gate — e.g. two in-flight PRs that collide with each other, or an issue that
    # declared no paths). Default on: dispatch the fixer to rebase onto base +
    # resolve, bounded by max_rebase_retries → then needs-human-review. Set
    # AUTO_REBASE_CONFLICTS=0 to instead escalate to a human on the first conflict.
    auto_rebase_conflicts: bool = True
    max_rebase_retries: int = 3
    # Resolve breaker (T3 finding): every other paid leg had a retry ceiling;
    # resolve — the most expensive — had none, so an always-erroring resolve
    # requeued ai-ready and re-dispatched a full session every poll forever.
    max_resolve_retries: int = 3
    # Infra-death ceiling (review C3/D2c): a pre-result session death (transport
    # drop, daemon restart/OOM between dispatch and workflow tail) used to be
    # unbounded, unbilled, and invisible — the crash-isolation contract means
    # the *retry* breakers never see it (infra deaths are not attempts), so a
    # separate, higher ceiling escalates once dying itself becomes the loop.
    # 0 disables (wave P0-B wires the counter; this field/knob lands now so the
    # config surface is stable for that wave).
    max_infra_deaths: int = 5
    # Deployment-wide infra circuit breaker (keifu 2026-08-02 04:53-04:58Z): a
    # `codex` binary the backend 400'd killed every leg spawn across FOUR issues
    # in five minutes. Each burned its own per-issue infra ladder and escalated
    # separately; nothing said "the runtime is broken deployment-wide". The
    # per-issue ceiling is the wrong granularity for a repo-wide fault, so this
    # pauses PAID admission for the whole deployment once infra-class deaths
    # cross issues. Off restores per-issue ceilings as the only bound.
    infra_breaker: bool = True
    # Deaths inside INFRA_BREAKER_WINDOW_SECONDS, over ≥2 distinct issues, that
    # trip it. One issue's repeated deaths never trip it — that stays the
    # per-issue `max_infra_deaths` ceiling's job.
    infra_breaker_deaths: int = DEFAULT_BREAKER_DEATHS
    infra_breaker_window_seconds: int = DEFAULT_BREAKER_WINDOW_SECONDS
    # How long a trip holds before re-arming on its own. A trip blocks the paid
    # legs whose success would clear it, so without this the only exit is an
    # operator restart; a still-broken runtime re-trips within one window.
    infra_breaker_cooldown_seconds: int = DEFAULT_BREAKER_COOLDOWN_SECONDS
    # Reviewer twin of max_infra_deaths (re-review P0R-3). Counts paid review
    # rounds that advanced NOTHING — a session that died pre-result, but also a
    # fan-out whose synthesis returned None, a failed submit POST, or an errored
    # gate. Those returned-result stalls never touch the raise path, so before
    # this the multi-mode fan-out (gate + dimensions + validators) re-ran every
    # poll, forever, on a PR nobody was waiting on. 0 disables.
    max_unproductive_review_rounds: int = 5
    # ADR 0012 ceiling for durable GitHub writes: how many times one lifecycle
    # command may be re-executed after its readback says "not applied" before it
    # is retired to permanent_failure and escalated. Bounds the keifu incident
    # (a write-denied PAT re-attempting the same 403 every tick, forever). Counts
    # attempts, not time, and only verified refusals — infra failures receipt
    # unknown_outcome and are reconciled instead. See domain/effect_retry.py.
    effect_retry_ceiling: int = DEFAULT_EFFECT_RETRY_CEILING
    # ADR 0012 ceiling for PAID steward legs on one version of one issue. A
    # triage/groom leg that returns no usable verdict (budget cap, agent error,
    # malformed output) leaves the issue's baseline unrecorded, so the next sweep
    # re-selects it and buys the leg again — unbounded spend on an issue nothing
    # can classify. After this many consecutive failures the issue's baseline is
    # recorded anyway and an operator decision is surfaced; any later edit to the
    # issue starts a fresh series. See domain/steward_stall.py.
    steward_error_ceiling: int = DEFAULT_STEWARD_ERROR_CEILING
    review_trigger_label: str = "ai-pr-open"
    fix_model: str | None = None
    # FIX_EFFORT: base reasoning effort for the fix legs, runtime-agnostic
    # (harness #16). None = say nothing and leave each runtime on its own default
    # — CODEX_REASONING_EFFORT for codex, the SDK's own for Claude — which is
    # exactly what every deployment did before the knob existed.
    fix_effort: Effort | None = None
    # ESCALATE_ON_EVIDENCE: run a leg one rung stronger when the harness already
    # knows the previous attempt under-delivered — a recurrence label (#10), a
    # failed resolve retry, a second-and-later review round. Off pins every leg
    # to the router's verdict no matter what has happened before.
    escalate_on_evidence: bool = True
    # Codex is the default reviewer runtime, so pin its deployable default here
    # instead of leaving the task un-attributed to the Codex CLI default.
    review_model: str | None = "gpt-5.6-terra"
    steward_triage_model: str | None = "sonnet"
    fix_budget_usd: float = 10.0
    router_budget_usd: float = 1.0
    review_budget_usd: float = 8.0
    triage_budget_usd: float = 0.75
    # REVIEW_MODE: single (default) | multi | delegated.
    #   multi  — the /code-review fan-out (skip-gate → parallel dimension
    #            specialists → per-finding adversarial validation → synthesis).
    #            Gate + validators run cheaper than the dimension specialists
    #            (which use REVIEW_MODEL); each leg has its own small budget.
    #   delegated — the single-pass reviewer + a cheap code-scout subagent it
    #            delegates codebase search to (REVIEW_SCOUT_MODEL, default sonnet),
    #            so the expensive reviewer reasons over summaries not raw files.
    review_mode: ReviewMode = ReviewMode.SINGLE
    # EVIDENCE_STACK: which GUI/e2e evidence toolchain this deployment declares.
    # The boot probe (adapters/environment.py) knows exactly one — WebKitGTK/
    # tauri — and its readings are attested into every review and fixer prompt.
    # Unset (default) those readings are stated as FACTS ONLY: a repo driving
    # headless Chromium through Playwright needs none of the probed binaries, so
    # a categorical "live evidence is impossible here" would be a
    # harness-attested falsehood that lowers the evidence bar globally. Declaring
    # the stack is the operator asserting the probe covers this repo, which is
    # what makes its silence conclusive rather than merely uninformed.
    evidence_stack: str = field(
        default="",
        metadata={
            "env": "EVIDENCE_STACK",
            "coerce": "choice",
            "choices": EVIDENCE_STACKS,
        },
    )
    # Adopt rogue PRs (opened directly, no trigger label) into review. Default on:
    # a PR reviewed by nobody is the worse failure. Opt out per-PR with no-ai-review.
    review_adopt_unlabeled: bool = True
    review_gate_model: str | None = "haiku"
    review_validation_model: str | None = "haiku"
    review_scout_model: str | None = "sonnet"
    review_gate_budget_usd: float = 0.10
    review_dimension_budget_usd: float = 0.75
    review_validation_budget_usd: float = 0.20
    steward_triage: bool = True
    # Advisory grooming is opt-in; it never closes or edits issue bodies.
    steward_groom: bool = False
    # Reserved explicit opt-in for future body rewrites; current grooming only
    # comments, regardless of this setting.
    steward_groom_edit: bool = False
    steward_promote: bool = False
    # Handle triage @-mentions for OWNER-level (technical/architectural) questions —
    # e.g. "add a User to the domain model". Resolution order (roles.py): the target
    # repo's CODEOWNERS default owners, else this STEWARD_OWNER, else none (logged,
    # never silent). A user (@alice) or team (@org/maintainers).
    steward_owner: str = ""
    agent_md: str = ""
    review_prompt: str = ""
    triage_prompt: str = ""
    # Fast deterministic check run after every file edit (PostToolUse hook), e.g.
    # "npx tsc --noEmit" / "pyrefly check". Blank → no edit-time check.
    edit_check_cmd: str = ""
    # AUTHOR_TESTS_SPLIT: resolve an issue in two BLIND passes sharing one worktree
    # — a test/interface author (interfaces + failing tests, no implementation) then
    # a fresh-session implementer (makes the tests green, opens the PR). The author
    # can't anticipate the impl; the implementer can't weaken the tests (gate).
    author_tests_split: bool = False
    # Max contract-renegotiation rounds (implementer petition → fresh-author
    # adjudication) before escalating the dispute to a human. Only used in split mode.
    max_contract_rounds: int = 2
    # FIX_MCP_CONFIG: JSON map {check-name-regex: {config: <mcp.json path>, tools:
    # <glob>}}. On the CI-fix leg, servers whose regex matches a PR check are attached
    # to the fixer. Relative config paths resolve against harness_root.
    fix_mcp_config: str = ""
    harness_root: str = ""
    # Jira sync (steward mirrors each GitHub issue into a Jira ticket + transitions
    # its status as the lifecycle label moves). Default ON, but INERT until
    # jira_project_key is set — so it's enabled out of the box yet harmless on a repo
    # with no Jira project configured. Reaches Jira via the Atlassian MCP (the
    # `config/mcp/atlassian.json` spec), attached only to the steward.
    jira_sync: bool = True
    jira_project_key: str = ""
    jira_cloud_id: str = ""
    jira_mcp_config: str = "config/mcp/atlassian.json"
    jira_mcp_tools: str = "mcp__atlassian__*"
    jira_budget_usd: float = 0.50
    # Fixer runtime: "claude" (default, Claude Agent SDK), "codex" (OpenAI Codex
    # CLI, e.g. GPT-5.5), or "glm" (GLM via Z.ai's Anthropic-compatible endpoint —
    # the SAME Claude SDK runner with the profile's env injected). Only the FIXER
    # leg switches. See docs/reference/codex-runner.md.
    fixer_runtime: RuntimeName = RuntimeName.CLAUDE
    # Reviewer runtime: Codex is the deployment default; set "claude" explicitly
    # for the recommended mixed-runtime posture that preserves independent
    # implementer/reviewer failure modes.
    reviewer_runtime: RuntimeName = RuntimeName.CODEX
    # STEWARD_RUNTIME: which runtime the steward's TRIAGE/GROOM legs run on.
    # Claude stays the default so existing deployments are byte-identical after
    # this knob exists. Named `_name` because `steward_runtime` is already the
    # composed StewardRuntimeConfig property.
    steward_runtime_name: RuntimeName = RuntimeName.CLAUDE
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
    # per token; set both to 0 to opt out (spend goes untracked and the loud
    # `codex_cost_blindspot_warning` fires at startup instead).
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
        return self.reconcile_seconds if self.webhook_wake else self.poll_seconds

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
            steward_state_dir=self.steward_state_dir,
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
            max_ci_retries=self.max_ci_retries,
            max_review_retries=self.max_review_retries,
            auto_rebase_conflicts=self.auto_rebase_conflicts,
            max_rebase_retries=self.max_rebase_retries,
            max_resolve_retries=self.max_resolve_retries,
            max_infra_deaths=self.max_infra_deaths,
            author_tests_split=self.author_tests_split,
            max_contract_rounds=self.max_contract_rounds,
            mcp_config=self.fix_mcp_config,
        )

    @property
    def review_runtime(self) -> ReviewRuntimeConfig:
        return ReviewRuntimeConfig(
            bot=self.review_bot,
            runtime=self.reviewer_runtime,
            model=self.review_model,
            prompt=self.review_prompt,
            mode=self.review_mode,
            budget_usd=self.review_budget_usd,
            max_rejections=self.max_review_rejections,
            trigger_label=self.review_trigger_label,
            adopt_unlabeled=self.review_adopt_unlabeled,
            gate_model=self.review_gate_model,
            validation_model=self.review_validation_model,
            scout_model=self.review_scout_model,
            gate_budget_usd=self.review_gate_budget_usd,
            dimension_budget_usd=self.review_dimension_budget_usd,
            validation_budget_usd=self.review_validation_budget_usd,
            max_unproductive_rounds=self.max_unproductive_review_rounds,
        )

    @property
    def steward_runtime(self) -> StewardRuntimeConfig:
        return StewardRuntimeConfig(
            bot=self.steward_bot,
            runtime=self.steward_runtime_name,
            triage_enabled=self.steward_triage,
            promote=self.steward_promote,
            owner=self.steward_owner,
            triage_prompt=self.triage_prompt,
            triage_model=self.steward_triage_model,
            triage_budget_usd=self.triage_budget_usd,
            groom_enabled=self.steward_groom,
            max_infra_deaths=self.max_infra_deaths,
        )

    @property
    def jira_runtime(self) -> JiraRuntimeConfig:
        return JiraRuntimeConfig(
            sync=self.jira_sync,
            project_key=self.jira_project_key,
            cloud_id=self.jira_cloud_id,
            mcp_config=self.jira_mcp_config,
            mcp_tools=self.jira_mcp_tools,
            budget_usd=self.jira_budget_usd,
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
        # Same rule as POLL_SECONDS: 0 is not "disabled", it is a busy loop.
        _require_positive(errors, "RECONCILE_SECONDS", self.reconcile_seconds)
        _require_non_negative(errors, "MAX_CI_RETRIES", self.max_ci_retries)
        _require_non_negative(
            errors, "CI_PENDING_MAX_SECONDS", self.ci_pending_max_seconds
        )
        _require_non_negative(errors, "MAX_REVIEW_RETRIES", self.max_review_retries)
        _require_non_negative(errors, "MAX_REBASE_RETRIES", self.max_rebase_retries)
        _require_non_negative(errors, "MAX_RESOLVE_RETRIES", self.max_resolve_retries)
        _require_non_negative(errors, "MAX_INFRA_DEATHS", self.max_infra_deaths)
        # Positive, not non-negative: 0 here is not "disabled" (INFRA_BREAKER=0
        # is), it is a breaker that trips on nothing or holds for no time.
        _require_positive(
            errors, "INFRA_BREAKER_DEATHS", self.infra_breaker_deaths
        )
        _require_positive(
            errors,
            "INFRA_BREAKER_WINDOW_SECONDS",
            self.infra_breaker_window_seconds,
        )
        _require_positive(
            errors,
            "INFRA_BREAKER_COOLDOWN_SECONDS",
            self.infra_breaker_cooldown_seconds,
        )
        _require_non_negative(
            errors, "MAX_UNPRODUCTIVE_REVIEW_ROUNDS", self.max_unproductive_review_rounds
        )
        # Positive, not non-negative: 0 here does not mean "disabled", it means
        # "restore the unbounded retry loop ADR 0012 forbids".
        _require_positive(errors, "EFFECT_RETRY_CEILING", self.effect_retry_ceiling)
        _require_positive(
            errors, "STEWARD_ERROR_CEILING", self.steward_error_ceiling
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
        _require_non_negative(errors, "REVIEW_MAX_REJECTIONS", self.max_review_rejections)
        _require_non_negative(errors, "MAX_CONTRACT_ROUNDS", self.max_contract_rounds)
        for name, value in (
            ("MAX_BUDGET_USD", self.fix_budget_usd),
            ("ROUTER_BUDGET_USD", self.router_budget_usd),
            ("REVIEW_BUDGET_USD", self.review_budget_usd),
            ("STEWARD_TRIAGE_BUDGET_USD", self.triage_budget_usd),
            ("REVIEW_GATE_BUDGET_USD", self.review_gate_budget_usd),
            ("REVIEW_DIMENSION_BUDGET_USD", self.review_dimension_budget_usd),
            ("REVIEW_VALIDATION_BUDGET_USD", self.review_validation_budget_usd),
            ("JIRA_SYNC_BUDGET_USD", self.jira_budget_usd),
        ):
            _require_positive(errors, name, value)
        if self.fixer_runtime is RuntimeName.GLM and not self.glm_settings_file.strip():
            # The glm runtime IS "the Claude runner plus a profile's env"; with no
            # profile there is nothing to inject and the fixer would silently run
            # on Anthropic credentials instead. Existence of the file is a
            # build-time check (roles); emptiness is a config error.
            errors.append("GLM_SETTINGS_FILE is required when FIXER_RUNTIME=glm")
        if (
            self.reviewer_runtime is RuntimeName.CODEX
            and self.review_mode is not ReviewMode.SINGLE
        ):
            errors.append(
                "REVIEWER_RUNTIME=codex supports REVIEW_MODE=single only; "
                "multi/delegated rely on Claude subagents and model aliases"
            )
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

        # Push mode changes what the interval MEANS (notice latency → backstop
        # cadence), so it prints as a different fact rather than a smaller poll.
        cadence = (
            f"wake=webhook reconcile={self.reconcile_seconds}s"
            if self.webhook_wake else f"poll={self.poll_seconds}s"
        )

        return "\n".join([
            f"repo={self.repo or '(UNSET)'} base={self.base_branch} "
            # getattr, not IdentityMode(...): describe() is the startup diagnostic
            # and must print whatever it was handed rather than raise on it.
            f"identity={getattr(self.identity_mode, 'value', self.identity_mode)} "
            f"{cadence} "
            f"probe={f'on/{self.world_probe_ttl_seconds}s' if self.world_probe else 'off'}",
            f"fixer={self.fixer_runtime.value} fix_model={model(self.fix_model)} "
            f"fix_effort={getattr(self.fix_effort, 'value', None) or '(runtime default)'} "
            f"escalate={'on' if self.escalate_on_evidence else 'off'} "
            f"dispatch_concurrency={self.fix_dispatch_concurrency} "
            f"{fix_budget}",
            f"reviewer={self.reviewer_runtime.value} review_model={model(self.review_model)} "
            f"mode={self.review_mode.value} review_budget=${self.review_budget_usd:.2f} "
            f"rejections_cap={self.max_review_rejections}",
            f"steward={self.steward_runtime_name.value} "
            f"triage_model={model(self.steward_triage_model)} "
            f"triage_budget=${self.triage_budget_usd:.2f}",
            f"retry_caps: resolve={self.max_resolve_retries} ci={self.max_ci_retries} "
            f"review={self.max_review_retries} rebase={self.max_rebase_retries} "
            f"ci_pending="
            f"{f'{self.ci_pending_max_seconds}s' if self.ci_pending_max_seconds else 'off'}",
            f"spend_guards: dup_pr_guard={'on' if self.duplicate_pr_guard else 'off'} "
            f"ci_free_rerun={'on' if self.ci_free_rerun else 'off'}",
            f"timeouts: idle={self.agent_idle_timeout:.0f}s "
            f"wall={self.agent_max_runtime:.0f}s",
            f"spend_caps: issue=${self.issue_spend_cap_usd:.2f} "
            f"daily/{self.repo or 'repo'}=${self.daily_spend_cap_usd:.2f} "
            f"daily/global="
            f"{f'${self.global_daily_spend_cap_usd:.2f}' if self.global_daily_spend_cap_usd > 0 else 'off'} "
            f"alert={describe_command(self.alert_cmd, empty='none')}",
            f"edit_check={describe_command(self.edit_check_cmd, empty='none')}",
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
        "arm_on_open_active",
        "agent_md",
        "triage_prompt",
        "codex_cost_per_mtok_cached_in",
        "control_authority_root",
        "parse_errors",
        "repointed_from_deployments",
        "repo_input",
    }
)
_ENV_NAME_OVERRIDES = {
    "fix_budget_usd": "MAX_BUDGET_USD",
    "router_budget_usd": "ROUTER_BUDGET_USD",
    "max_review_rejections": "REVIEW_MAX_REJECTIONS",
    "triage_budget_usd": "STEWARD_TRIAGE_BUDGET_USD",
    "harness_root": "HARNESS_ROOT_DIR",
    "jira_budget_usd": "JIRA_SYNC_BUDGET_USD",
    "steward_runtime_name": "STEWARD_RUNTIME",
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
    "steward_state_dir": "state_dir",
    "fix_effort": "effort",
    "review_model": "review_model",
    "review_mode": "review_mode",
    "review_prompt": "stripped",
    "steward_triage_model": "optional_string",
    "review_gate_model": "optional_string",
    "review_validation_model": "optional_string",
    "review_scout_model": "optional_string",
    "fixer_runtime": "runtime",
    "reviewer_runtime": "runtime",
    "steward_runtime_name": "runtime",
    "codex_cost_per_mtok": "rate_table",
}
_CHOICE_OVERRIDES: dict[str, tuple[str, ...]] = {
    "identity_mode": tuple(item.value for item in IdentityMode),
    "fix_effort": tuple(item.value for item in EFFORT_LADDER),
    "review_mode": tuple(item.value for item in ReviewMode),
    "fixer_runtime": tuple(item.value for item in RuntimeName),
    "reviewer_runtime": (RuntimeName.CLAUDE.value, RuntimeName.CODEX.value),
    "steward_runtime_name": (RuntimeName.CLAUDE.value, RuntimeName.CODEX.value),
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


def _raw(
    env: dict,
    field_name: str,
    *,
    default: object = MISSING,
) -> object:
    fallback = _dc_default(field_name) if default is MISSING else default
    return env.get(_env_name(field_name), fallback)


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
    if coercion == "review_mode":
        raw = env.get(env_name)
        if raw is None or not str(raw).strip():
            return fallback
        value = str(raw).strip().casefold()
        choices = tuple(str(item) for item in definition.metadata["choices"])
        if value not in choices:
            errors.append(
                f"{env_name} must be one of {list(choices)}, got {raw!r} "
                f"— using default {getattr(fallback, 'value', fallback)!r}"
            )
            return fallback
        return ReviewMode(value)
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
    """Default STATE_DIR/REVIEW_STATE_DIR/STEWARD_STATE_DIR (review M5/D7): the
    old default was per-role but not per-repo, so two repos' fix daemons shared
    `issue-N.session` etc. — issue #5 in repo B would resume repo A's
    transcript. Nothing real is deployed yet, so the default itself moves
    per-repo; an operator who already set the env var explicitly is unaffected
    (from_env only calls this when the var is absent/empty)."""
    return os.path.expanduser(f"~/.local/state/{role_dir}/{_repo_slug(repo)}")


def default_state_dir(repo: str, role_dir: str) -> str:
    """The state dir a daemon derives when the overlay carries no explicit
    STATE_DIR/REVIEW_STATE_DIR/STEWARD_STATE_DIR — the public seam for
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


def _runtime_default_model(
    env: dict,
    field_name: str,
    *,
    claude_default: object,
    is_claude: bool,
) -> str | None:
    """Resolve a model knob whose DEFAULT depends on the selected runtime.

    Three distinct states, deliberately not two: absent (take the runtime's
    default), present-and-empty (None — "let the runtime choose"), and present
    (verbatim). Collapsing the middle state into the first is how an explicit
    `KEY=""` starts meaning something other than what it meant before.
    """
    raw = env.get(_env_name(field_name))
    if raw is not None:
        return str(raw).strip() or None
    if not is_claude:
        return None
    return str(claude_default) if claude_default is not None else None


def from_env(env: dict | None = None) -> HarnessConfig:
    """Build one immutable snapshot from the metadata-backed config table."""

    e = _authoritative_env(env if env is not None else os.environ)
    errors: list[str] = []
    special = {
        "state_dir",
        "review_state_dir",
        "steward_state_dir",
        "review_model",
        "steward_triage_model",
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
    steward_runtime = values["steward_runtime_name"]
    if not isinstance(steward_runtime, RuntimeName):
        raise RuntimeError("steward runtime metadata did not produce RuntimeName")

    values.update(
        state_dir=(
            str(_metadata_value(e, "state_dir", errors))
            or _default_state_dir(repo, "issue-agent")
        ),
        review_state_dir=(
            str(_metadata_value(e, "review_state_dir", errors))
            or _default_state_dir(repo, "review-agent")
        ),
        steward_state_dir=(
            str(_metadata_value(e, "steward_state_dir", errors))
            or _default_state_dir(repo, "steward-agent")
        ),
        review_model=(
            str(e.get(_env_name("review_model")) or "")
            or (
                "opus"
                if reviewer_runtime is RuntimeName.CLAUDE
                else _dc_default("review_model")
            )
        ),
        # An explicit STEWARD_TRIAGE_MODEL always wins, and only an ABSENT one
        # resolves per runtime. The dataclass default is a Claude alias
        # ("sonnet"), which Codex rejects outright, so under
        # STEWARD_RUNTIME=codex an unset knob means None — the Codex runner then
        # uses CODEX_MODEL, else its own built-in default.
        #
        # PRESENT-but-empty keeps its pre-existing `optional_string` meaning,
        # None ("use the runtime's own default"), on BOTH runtimes. Folding it
        # into the absent case would silently turn a deliberate
        # STEWARD_TRIAGE_MODEL="" into "sonnet".
        steward_triage_model=_runtime_default_model(
            e,
            "steward_triage_model",
            claude_default=_dc_default("steward_triage_model"),
            is_claude=steward_runtime is RuntimeName.CLAUDE,
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
    values["exemption_mac_key"] = _derive_exemption_mac_key(e)
    values["parse_errors"] = tuple(errors)
    return HarnessConfig(**values)


def _derive_exemption_mac_key(e: dict) -> str:
    """Key material for authenticating ADR 0014 grant lines.

    An explicit EXEMPTION_MAC_KEY wins; otherwise the key is DERIVED (one-way,
    versioned) from REVIEW_BOT_TOKEN, which both daemons already share via the
    env snapshot — no new secret to provision, and rotating the token rotates
    the key (voiding open grants, which a re-review re-mints). The derivation
    means the stored value cannot recover the token. Neither the raw token
    nor this key is ever placed in an agent environment or prompt; the
    HMAC is computed harness-side only (round-3 BLOCKER 1)."""
    explicit = str(e.get("EXEMPTION_MAC_KEY") or "")
    if explicit:
        return explicit
    token = str(e.get("REVIEW_BOT_TOKEN") or "")
    if not token:
        return ""
    return hashlib.sha256(
        b"issue-agent exemption-grant-mac v1:" + token.encode()
    ).hexdigest()
