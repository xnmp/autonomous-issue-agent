"""Environment → config objects (port of the agent.env vars the bash reads).

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
import subprocess
from dataclasses import dataclass, fields

from ..adapters.agent_runner import DEFAULT_MAX_BUFFER_BYTES
from ..domain.effort import EFFORT_LADDER, Effort, parse_effort
from ..domain.identity import normalize_login
from ..domain.review import IdentityMode, ReviewMode


SHELL_CMD_LOG_LIMIT = 120


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
    # Kill a run once its priced spend crosses AgentTask.max_budget_usd (#29).
    # See `HarnessConfig.codex_enforce_budget`.
    enforce_budget: bool = True

    @property
    def budget_priceable(self) -> bool:
        """Whether every configured model path has complete dollar pricing."""
        return (
            self.cost_per_mtok_in > 0
            and self.cost_per_mtok_out > 0
            and all(rate_in > 0 and rate_out > 0 for _, rate_in, rate_out in self.cost_per_mtok)
        )


@dataclass(frozen=True)
class HarnessConfig:
    repo: str
    base_branch: str = "dev"
    target_dir: str = ""
    identity_mode: IdentityMode = IdentityMode.THREE
    fix_bot: str = ""
    review_bot: str = "my-reviewer-bot[bot]"
    steward_bot: str = "my-reviewer-bot[bot]"
    state_dir: str = ""
    review_state_dir: str = ""
    steward_state_dir: str = ""
    worktree_root: str = ".claude/worktrees"
    # review S7: this literal used to disagree with from_env's own "20" literal
    # (60 here, "20" there) — from_env now reads its default off THIS field, so
    # there is exactly one source and 20 (the value that actually shipped) wins.
    poll_seconds: int = 20
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
    arm_on_open: str = "auto"
    # The probe's boot-time verdict (main.build_role fills it; never from env).
    arm_on_open_active: bool = False
    auto_merge: bool = True
    merge_method: str = "squash"
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
    # Reviewer twin of max_infra_deaths (re-review P0R-3). Counts paid review
    # rounds that advanced NOTHING — a session that died pre-result, but also a
    # fan-out whose synthesis returned None, a failed submit POST, or an errored
    # gate. Those returned-result stalls never touch the raise path, so before
    # this the multi-mode fan-out (gate + dimensions + validators) re-ran every
    # poll, forever, on a PR nobody was waiting on. 0 disables.
    max_unproductive_review_rounds: int = 5
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
    review_model: str | None = None
    steward_triage_model: str | None = "sonnet"
    fix_budget_usd: float = 10.0
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
    # Legacy REVIEW_MULTI_AGENT=1 is honoured as an alias for multi.
    review_mode: ReviewMode = ReviewMode.SINGLE
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
    # Advisory grooming is on by default; it never closes or edits issue bodies.
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
    # leg switches; reviewer/steward stay Claude (a distinct model decorrelates
    # blind spots). See docs/CODEX-RUNNER.md.
    fixer_runtime: str = "claude"
    codex_bin: str = "codex"
    codex_model: str = ""              # blank → CodexRunner default (GPT-5.5)
    codex_provider_id: str = "openai-custom"  # NOT "openai": reserved by codex-cli ≥0.142
    codex_provider_name: str = "OpenAI"
    # Blank = codex's built-in OpenAI provider + `codex login` OAuth credentials
    # (ChatGPT subscription). Set it only for an OpenAI-compatible gateway, which
    # then requires the CODEX_ENV_KEY env var to hold the key.
    codex_base_url: str = ""
    codex_env_key: str = "OPENAI_API_KEY"
    codex_wire_api: str = "responses"
    codex_sandbox: str = "danger-full-access"
    # codex exec's own default is LOW effort (live session_meta, 2026-07-27);
    # medium is the deliberate implementer default. "" = leave codex alone.
    codex_reasoning_effort: str = "medium"
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
    # Codex spend accounting (review M1/D12): roles.py prices the envelope's
    # token usage against these USD-per-million-token rates so codex runs reach
    # the ledger and both spend caps can see them. USD per MILLION tokens,
    # matching provider pricing tables (not per-token, which would need too many
    # decimal places).
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
    # `CODEX_COST_PER_MTOK='model=IN/OUT,model=IN/OUT'`, USD per MILLION tokens,
    # e.g. 'gpt-5.6-luna=1.25/10,gpt-5.6-terra=10/60'. Every leg is priced with
    # the model it ACTUALLY ran (task.model, or the runner's default when the
    # task pinned none). A model absent from the table — and the whole knob
    # when unset — falls back to the scalars above, so an existing deployment
    # prices exactly as it did before.
    codex_cost_per_mtok: tuple[tuple[str, float, float], ...] = ()
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
    # individual run stayed under budget. These caps close that. All three read the
    # one shared ledger (adapters.journal.default_ledger_path) but differ in scope:
    #   issue  — lifetime spend on one issue of THIS repo; escalates it to a human.
    #   daily  — today's spend on THIS repo (per-repo overlay knob, #32).
    #   global — today's spend across EVERY repo and role on this machine; belongs
    #            in config/shared.env. Off by default so adding it never silently
    #            tightens an existing deployment.
    # Both daily caps pause dispatch until 00:00 UTC without touching any label.
    # 0 disables any of them.
    issue_spend_cap_usd: float = 25.0
    daily_spend_cap_usd: float = 50.0
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
            enforce_budget=self.codex_enforce_budget,
        )

    def validation_errors(self) -> tuple[str, ...]:
        # M2: malformed numerics recorded by from_env come first — they are the
        # loudest class of misconfiguration (a typo that would otherwise
        # traceback three frames deep in int()/float()) and must never be lost
        # behind a range check on the substituted default.
        errors: list[str] = list(self.parse_errors)
        _require_positive(errors, "POLL_SECONDS", self.poll_seconds)
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
        _require_non_negative(
            errors, "MAX_UNPRODUCTIVE_REVIEW_ROUNDS", self.max_unproductive_review_rounds
        )
        if not (self.repo or "").strip():
            # Fail LOUD at startup, not with an opaque gh error on the first
            # poll: an empty REPO means a missing/mis-composed overlay (the
            # silent-default class, #50).
            errors.append("REPO must be set (owner/name) — is the repo overlay composed?")
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
            ("REVIEW_BUDGET_USD", self.review_budget_usd),
            ("STEWARD_TRIAGE_BUDGET_USD", self.triage_budget_usd),
            ("REVIEW_GATE_BUDGET_USD", self.review_gate_budget_usd),
            ("REVIEW_DIMENSION_BUDGET_USD", self.review_dimension_budget_usd),
            ("REVIEW_VALIDATION_BUDGET_USD", self.review_validation_budget_usd),
            ("JIRA_SYNC_BUDGET_USD", self.jira_budget_usd),
        ):
            _require_positive(errors, name, value)
        if self.fixer_runtime not in ("claude", "codex", "glm"):
            errors.append("FIXER_RUNTIME must be 'claude', 'codex' or 'glm'")
        elif self.fixer_runtime == "glm" and not self.glm_settings_file.strip():
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
        return tuple(errors)

    def validate(self) -> None:
        errors = self.validation_errors()
        if errors:
            raise ValueError("invalid harness config: " + "; ".join(errors))

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
        if self.fixer_runtime == "codex" and self.fix_budget_usd > 0:
            # Every fallback and per-model rate needs both token classes priced.
            # One zero leaves a routed model able to spend outside the meter.
            priced = self.codex_runtime.budget_priceable
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
            f"fixer={self.fixer_runtime} fix_model={model(self.fix_model)} "
            f"fix_effort={getattr(self.fix_effort, 'value', None) or '(runtime default)'} "
            f"escalate={'on' if self.escalate_on_evidence else 'off'} "
            f"{fix_budget}",
            f"review_model={model(self.review_model)} mode={self.review_mode.value} "
            f"review_budget=${self.review_budget_usd:.2f} "
            f"rejections_cap={self.max_review_rejections}",
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
            f"state={self.state_dir or '(unset)'} target={self.target_dir or '(cwd)'}",
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
_FIELD_DEFAULTS = {f.name: f.default for f in fields(HarnessConfig)}


def _dc_default(field: str):
    return _FIELD_DEFAULTS[field]


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


def _coerce_rate_table(
    env: dict, key: str, errors: list[str]
) -> tuple[tuple[str, float, float], ...]:
    """`'model=IN/OUT,model=IN/OUT'` → an ordered (model, in, out) table (#43).

    Same contract as the other coercions: empty/absent is UNSET (an empty
    table, i.e. "price everything with the scalar fallback"), and anything
    malformed is dropped AND named in `errors`, so it surfaces through
    `validation_errors()` instead of silently mispricing every run on this
    runtime. Naming the offending entry matters more here than usual — a
    typo'd model name is otherwise indistinguishable from a model the operator
    genuinely did not want to override, and both read as "the fallback price",
    which is the bug this knob exists to fix.

    Rates must be finite and >= 0 for the same reason `_coerce_float` rejects
    nan/inf: every downstream range check passes for nan, so a nan rate would
    zero out the ledger and silently disarm both spend caps.

    A model named twice keeps its FIRST entry (lookup scans in order) and the
    duplicate is reported — "which of my two prices won?" should not be a
    question an operator has to answer by reading the parser.
    """
    raw = env.get(key)
    if raw is None or not raw.strip():
        return ()
    table: list[tuple[str, float, float]] = []
    seen: set[str] = set()
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        model, sep, rates = entry.partition("=")
        rate_in, slash, rate_out = rates.partition("/")
        model = model.strip().casefold()
        if not (sep and slash and model):
            errors.append(
                f"{key} entry {entry!r} must look like 'model=IN/OUT' "
                f"(USD per million tokens) — ignoring it"
            )
            continue
        try:
            parsed = (float(rate_in.strip()), float(rate_out.strip()))
        except ValueError:
            errors.append(
                f"{key} entry {entry!r} has non-numeric rates — ignoring it"
            )
            continue
        if not all(math.isfinite(r) and r >= 0 for r in parsed):
            errors.append(
                f"{key} entry {entry!r} must have finite, non-negative rates "
                f"— ignoring it"
            )
            continue
        if model in seen:
            errors.append(f"{key} names {model!r} more than once — keeping the first")
            continue
        seen.add(model)
        table.append((model, parsed[0], parsed[1]))
    return tuple(table)


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
        proc = subprocess.run(
            ["bash", "-c", 'set -a; . "$1"; set +a; env -0', "issue-agent-env", path],
            env=dict(inherited),
            check=True,
            capture_output=True,
            timeout=30,
        )
        out: dict[str, str] = {}
        for item in proc.stdout.split(b"\0"):
            if not item or b"=" not in item:
                continue
            key, value = item.split(b"=", 1)
            name = key.decode(errors="replace")
            if name in assigned:
                out[name] = value.decode(errors="replace")
        return out
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
    e = _authoritative_env(env if env is not None else os.environ)
    # Malformed-numeric fallbacks accumulate here and ride along on the config
    # (HarnessConfig.parse_errors) so validation_errors() can report them —
    # from_env itself must never traceback on operator input (review M2).
    errors: list[str] = []
    repo = e.get("REPO", "")
    return HarnessConfig(
        repo=repo,
        base_branch=e.get("BASE_BRANCH", _dc_default("base_branch")),
        target_dir=e.get("TARGET_DIR", _dc_default("target_dir")),
        identity_mode=_coerce_identity_mode(e, errors),
        fix_bot=e.get("FIX_BOT", _dc_default("fix_bot")),
        # Default to the shared GitHub App login so an operator only needs to
        # supply the App credentials (GH_APP_*), not restate the bot name. Override
        # per-repo/shared only if running a differently-named App.
        review_bot=e.get("REVIEW_BOT", _dc_default("review_bot")),
        steward_bot=e.get("STEWARD_BOT", _dc_default("steward_bot")),
        # review M5/D7: default state dirs are now per-repo (see
        # _default_state_dir) — an explicit env value (even if it happens to
        # equal the computed default) always wins; only absent/empty falls
        # through to the per-repo default.
        state_dir=e.get("STATE_DIR") or _default_state_dir(repo, "issue-agent"),
        review_state_dir=e.get("REVIEW_STATE_DIR") or _default_state_dir(repo, "review-agent"),
        steward_state_dir=e.get("STEWARD_STATE_DIR") or _default_state_dir(repo, "steward-agent"),
        worktree_root=e.get("WORKTREE_ROOT", _dc_default("worktree_root")),
        poll_seconds=_coerce_int(e, "POLL_SECONDS", _dc_default("poll_seconds"), errors),
        webhook_wake=_b(e, "WEBHOOK_WAKE", _dc_default("webhook_wake"), errors),
        reconcile_seconds=_coerce_int(
            e, "RECONCILE_SECONDS", _dc_default("reconcile_seconds"), errors
        ),
        arm_on_open=e.get("ARM_ON_OPEN", _dc_default("arm_on_open")),
        world_probe=_b(e, "WORLD_PROBE", _dc_default("world_probe"), errors),
        world_probe_ttl_seconds=_coerce_int(
            e, "WORLD_PROBE_TTL_SECONDS", _dc_default("world_probe_ttl_seconds"), errors
        ),
        auto_merge=_b(e, "AUTO_MERGE", _dc_default("auto_merge"), errors),
        merge_method=e.get("MERGE_METHOD", _dc_default("merge_method")),
        ci_ignore_checks=e.get("CI_IGNORE_CHECKS", _dc_default("ci_ignore_checks")),
        ci_pending_max_seconds=_coerce_int(
            e, "CI_PENDING_MAX_SECONDS", _dc_default("ci_pending_max_seconds"), errors
        ),
        duplicate_pr_guard=_b(
            e, "DUPLICATE_PR_GUARD", _dc_default("duplicate_pr_guard"), errors
        ),
        ci_free_rerun=_b(e, "CI_FREE_RERUN", _dc_default("ci_free_rerun"), errors),
        close_issue_on_merge=_b(e, "CLOSE_ISSUE_ON_MERGE", _dc_default("close_issue_on_merge"), errors),
        auto_rebase_conflicts=_b(e, "AUTO_REBASE_CONFLICTS", _dc_default("auto_rebase_conflicts"), errors),
        max_rebase_retries=_coerce_int(e, "MAX_REBASE_RETRIES", _dc_default("max_rebase_retries"), errors),
        max_resolve_retries=_coerce_int(
            e, "MAX_RESOLVE_RETRIES", _dc_default("max_resolve_retries"), errors
        ),
        max_ci_retries=_coerce_int(e, "MAX_CI_RETRIES", _dc_default("max_ci_retries"), errors),
        max_infra_deaths=_coerce_int(e, "MAX_INFRA_DEATHS", _dc_default("max_infra_deaths"), errors),
        max_unproductive_review_rounds=_coerce_int(
            e, "MAX_UNPRODUCTIVE_REVIEW_ROUNDS",
            _dc_default("max_unproductive_review_rounds"), errors,
        ),
        max_review_rejections=_coerce_int(
            e, "REVIEW_MAX_REJECTIONS", _dc_default("max_review_rejections"), errors
        ),
        max_review_retries=_coerce_int(
            e, "MAX_REVIEW_RETRIES", _dc_default("max_review_retries"), errors
        ),
        review_trigger_label=e.get("REVIEW_TRIGGER_LABEL", _dc_default("review_trigger_label")),
        fix_model=e.get("FIX_MODEL") or _dc_default("fix_model"),
        fix_effort=_effort(e, "FIX_EFFORT", _dc_default("fix_effort"), errors),
        escalate_on_evidence=_b(
            e, "ESCALATE_ON_EVIDENCE", _dc_default("escalate_on_evidence"), errors
        ),
        review_model=e.get("REVIEW_MODEL") or _dc_default("review_model"),
        steward_triage_model=e.get("STEWARD_TRIAGE_MODEL", _dc_default("steward_triage_model"))
        or None,
        fix_budget_usd=_coerce_float(e, "MAX_BUDGET_USD", _dc_default("fix_budget_usd"), errors),
        review_budget_usd=_coerce_float(
            e, "REVIEW_BUDGET_USD", _dc_default("review_budget_usd"), errors
        ),
        triage_budget_usd=_coerce_float(
            e, "STEWARD_TRIAGE_BUDGET_USD", _dc_default("triage_budget_usd"), errors
        ),
        review_mode=ReviewMode.from_env(
            e.get("REVIEW_MODE", ""), _b(e, "REVIEW_MULTI_AGENT", False, errors)
        ),
        # Operator REVIEW_PROMPT override, if any (wins wholesale). Blank/unset →
        # "", and main.py loads the default from plugin/REVIEW_PROMPT.md (kept out
        # of this pure dict→config mapping, like agent_md). A whitespace-only value
        # is treated as unset so it can't strip the reviewer's whole rubric.
        review_prompt=(e.get("REVIEW_PROMPT") or "").strip(),
        review_adopt_unlabeled=_b(
            e, "REVIEW_ADOPT_UNLABELED", _dc_default("review_adopt_unlabeled"), errors
        ),
        review_gate_model=e.get("REVIEW_GATE_MODEL", _dc_default("review_gate_model")) or None,
        review_validation_model=(
            e.get("REVIEW_VALIDATION_MODEL", _dc_default("review_validation_model")) or None
        ),
        review_scout_model=e.get("REVIEW_SCOUT_MODEL", _dc_default("review_scout_model")) or None,
        review_gate_budget_usd=_coerce_float(
            e, "REVIEW_GATE_BUDGET_USD", _dc_default("review_gate_budget_usd"), errors
        ),
        review_dimension_budget_usd=_coerce_float(
            e, "REVIEW_DIMENSION_BUDGET_USD", _dc_default("review_dimension_budget_usd"), errors
        ),
        review_validation_budget_usd=_coerce_float(
            e, "REVIEW_VALIDATION_BUDGET_USD", _dc_default("review_validation_budget_usd"), errors
        ),
        steward_triage=_b(e, "STEWARD_TRIAGE", _dc_default("steward_triage"), errors),
        steward_groom=_b(e, "STEWARD_GROOM", _dc_default("steward_groom"), errors),
        steward_groom_edit=_b(e, "STEWARD_GROOM_EDIT", _dc_default("steward_groom_edit"), errors),
        steward_promote=_b(e, "STEWARD_PROMOTE", _dc_default("steward_promote"), errors),
        steward_owner=e.get("STEWARD_OWNER", _dc_default("steward_owner")),
        edit_check_cmd=e.get("EDIT_CHECK_CMD", _dc_default("edit_check_cmd")),
        author_tests_split=_b(e, "AUTHOR_TESTS_SPLIT", _dc_default("author_tests_split"), errors),
        max_contract_rounds=_coerce_int(
            e, "MAX_CONTRACT_ROUNDS", _dc_default("max_contract_rounds"), errors
        ),
        fix_mcp_config=e.get("FIX_MCP_CONFIG", _dc_default("fix_mcp_config")),
        harness_root=e.get("HARNESS_ROOT_DIR", _dc_default("harness_root")),
        jira_sync=_b(e, "JIRA_SYNC", _dc_default("jira_sync"), errors),
        jira_project_key=e.get("JIRA_PROJECT_KEY", _dc_default("jira_project_key")),
        jira_cloud_id=e.get("JIRA_CLOUD_ID", _dc_default("jira_cloud_id")),
        jira_mcp_config=e.get("JIRA_MCP_CONFIG", _dc_default("jira_mcp_config")),
        jira_mcp_tools=e.get("JIRA_MCP_TOOLS", _dc_default("jira_mcp_tools")),
        jira_budget_usd=_coerce_float(e, "JIRA_SYNC_BUDGET_USD", _dc_default("jira_budget_usd"), errors),
        fixer_runtime=e.get("FIXER_RUNTIME", _dc_default("fixer_runtime")),
        codex_bin=e.get("CODEX_BIN", _dc_default("codex_bin")),
        codex_model=e.get("CODEX_MODEL", _dc_default("codex_model")),
        codex_provider_id=e.get("CODEX_PROVIDER_ID", _dc_default("codex_provider_id")),
        codex_provider_name=e.get("CODEX_PROVIDER_NAME", _dc_default("codex_provider_name")),
        codex_base_url=e.get("CODEX_BASE_URL", _dc_default("codex_base_url")),
        codex_env_key=e.get("CODEX_ENV_KEY", _dc_default("codex_env_key")),
        codex_wire_api=e.get("CODEX_WIRE_API", _dc_default("codex_wire_api")),
        codex_sandbox=e.get("CODEX_SANDBOX", _dc_default("codex_sandbox")),
        codex_reasoning_effort=e.get(
            "CODEX_REASONING_EFFORT", _dc_default("codex_reasoning_effort")
        ),
        glm_settings_file=e.get("GLM_SETTINGS_FILE", _dc_default("glm_settings_file")),
        agent_idle_timeout=_coerce_float(
            e, "AGENT_IDLE_TIMEOUT_SECONDS", _dc_default("agent_idle_timeout"), errors
        ),
        agent_max_runtime=_coerce_float(
            e, "AGENT_MAX_RUNTIME_SECONDS", _dc_default("agent_max_runtime"), errors
        ),
        sdk_max_buffer_bytes=_coerce_int(
            e, "SDK_MAX_BUFFER_BYTES", _dc_default("sdk_max_buffer_bytes"), errors
        ),
        codex_transport_retries=_coerce_int(
            e, "CODEX_TRANSPORT_RETRIES", _dc_default("codex_transport_retries"), errors
        ),
        codex_backoff_base=_coerce_float(
            e, "CODEX_BACKOFF_BASE_SECONDS", _dc_default("codex_backoff_base"), errors
        ),
        codex_rate_limit_backoff_base=_coerce_float(
            e,
            "CODEX_RATE_LIMIT_BACKOFF_BASE_SECONDS",
            _dc_default("codex_rate_limit_backoff_base"),
            errors,
        ),
        codex_max_rate_limit_wait=_coerce_float(
            e, "CODEX_MAX_RATE_LIMIT_WAIT_SECONDS", _dc_default("codex_max_rate_limit_wait"), errors
        ),
        codex_max_rate_limit_retries=_coerce_int(
            e, "CODEX_MAX_RATE_LIMIT_RETRIES", _dc_default("codex_max_rate_limit_retries"), errors
        ),
        codex_cost_per_mtok_in=_coerce_float(
            e, "CODEX_COST_PER_MTOK_IN", _dc_default("codex_cost_per_mtok_in"), errors
        ),
        codex_cost_per_mtok_out=_coerce_float(
            e, "CODEX_COST_PER_MTOK_OUT", _dc_default("codex_cost_per_mtok_out"), errors
        ),
        codex_cost_per_mtok=_coerce_rate_table(e, "CODEX_COST_PER_MTOK", errors),
        codex_enforce_budget=_b(
            e, "CODEX_ENFORCE_BUDGET", _dc_default("codex_enforce_budget"), errors
        ),
        issue_spend_cap_usd=_coerce_float(
            e, "ISSUE_SPEND_CAP_USD", _dc_default("issue_spend_cap_usd"), errors
        ),
        daily_spend_cap_usd=_coerce_float(
            e, "DAILY_SPEND_CAP_USD", _dc_default("daily_spend_cap_usd"), errors
        ),
        global_daily_spend_cap_usd=_coerce_float(
            e, "GLOBAL_DAILY_SPEND_CAP_USD",
            _dc_default("global_daily_spend_cap_usd"), errors,
        ),
        alert_cmd=e.get("ALERT_CMD", _dc_default("alert_cmd")),
        parse_errors=tuple(errors),
    )
