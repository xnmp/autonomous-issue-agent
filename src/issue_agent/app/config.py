"""Environment → config objects (port of the agent.env vars the bash reads).

Pure mapping `dict → HarnessConfig` so tests construct it without os.environ.
Names mirror config/agent.env.example so operators carry the same env file over.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, fields

from ..domain.identity import normalize_login
from ..domain.review import IdentityMode, ReviewMode


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
    idle_timeout: float
    max_runtime: float
    transport_retries: int
    backoff_base: float
    rate_limit_backoff_base: float
    max_rate_limit_wait: float
    max_rate_limit_retries: int
    cost_per_mtok_in: float
    cost_per_mtok_out: float


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
    auto_merge: bool = True
    merge_method: str = "squash"
    ci_ignore_checks: str = ""
    close_issue_on_merge: bool = True
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
    # Fixer runtime: "claude" (default, Claude Agent SDK) or "codex" (OpenAI Codex
    # CLI, e.g. GPT-5.5). Only the FIXER leg switches; reviewer/steward stay Claude
    # (a distinct model decorrelates blind spots). See docs/CODEX-RUNNER.md.
    fixer_runtime: str = "claude"
    codex_bin: str = "codex"
    codex_model: str = ""              # blank → CodexRunner default (GPT-5.5)
    codex_provider_id: str = "openai-custom"  # NOT "openai": reserved by codex-cli ≥0.142
    codex_provider_name: str = "OpenAI"
    codex_base_url: str = ""            # REQUIRED for codex runtime: your OpenAI-compatible gateway
    codex_env_key: str = "OPENAI_API_KEY"
    codex_wire_api: str = "responses"
    codex_sandbox: str = "danger-full-access"
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
    # Codex spend accounting (review M1/D12): codex_runner.py hardcodes
    # cost_usd=0.0, so codex spend is invisible to both the issue and daily
    # caps by construction. When both are >0, roles.py (a later wave) prices
    # the envelope's token usage against these USD-per-million-token rates;
    # 0.0 (default) means codex spend stays untracked and a loud startup
    # warning fires instead. USD per MILLION tokens, matching provider pricing
    # tables (not per-token, which would need too many decimal places).
    codex_cost_per_mtok_in: float = 0.0
    codex_cost_per_mtok_out: float = 0.0
    # ── spend circuit breakers ────────────────────────────────────────────────
    # Per-agent budgets (MAX_BUDGET_USD et al) bound ONE session; nothing bounded
    # the aggregate, so a leg that retries forever spent without limit while every
    # individual run stayed under budget. These two caps close that: lifetime spend
    # on one issue, and today's spend across EVERY repo and role on this machine
    # (they share one ledger — see adapters.journal.default_ledger_path). Hitting
    # the issue cap escalates that issue to a human; hitting the daily cap pauses
    # all dispatch until 00:00 UTC without touching any label. 0 disables either.
    issue_spend_cap_usd: float = 25.0
    daily_spend_cap_usd: float = 50.0
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
            idle_timeout=self.agent_idle_timeout,
            max_runtime=self.agent_max_runtime,
            transport_retries=self.codex_transport_retries,
            backoff_base=self.codex_backoff_base,
            rate_limit_backoff_base=self.codex_rate_limit_backoff_base,
            max_rate_limit_wait=self.codex_max_rate_limit_wait,
            max_rate_limit_retries=self.codex_max_rate_limit_retries,
            cost_per_mtok_in=self.codex_cost_per_mtok_in,
            cost_per_mtok_out=self.codex_cost_per_mtok_out,
        )

    def validation_errors(self) -> tuple[str, ...]:
        # M2: malformed numerics recorded by from_env come first — they are the
        # loudest class of misconfiguration (a typo that would otherwise
        # traceback three frames deep in int()/float()) and must never be lost
        # behind a range check on the substituted default.
        errors: list[str] = list(self.parse_errors)
        _require_positive(errors, "POLL_SECONDS", self.poll_seconds)
        _require_non_negative(errors, "MAX_CI_RETRIES", self.max_ci_retries)
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
        if self.fixer_runtime not in ("claude", "codex"):
            errors.append("FIXER_RUNTIME must be 'claude' or 'codex'")
        _require_non_negative(errors, "AGENT_IDLE_TIMEOUT_SECONDS", self.agent_idle_timeout)
        _require_non_negative(errors, "AGENT_MAX_RUNTIME_SECONDS", self.agent_max_runtime)
        _require_non_negative(errors, "CODEX_TRANSPORT_RETRIES", self.codex_transport_retries)
        _require_positive(errors, "CODEX_BACKOFF_BASE_SECONDS", self.codex_backoff_base)
        _require_non_negative(errors, "CODEX_MAX_RATE_LIMIT_RETRIES", self.codex_max_rate_limit_retries)
        _require_positive(errors, "CODEX_RATE_LIMIT_BACKOFF_BASE_SECONDS", self.codex_rate_limit_backoff_base)
        _require_positive(errors, "CODEX_MAX_RATE_LIMIT_WAIT_SECONDS", self.codex_max_rate_limit_wait)
        _require_non_negative(errors, "CODEX_COST_PER_MTOK_IN", self.codex_cost_per_mtok_in)
        _require_non_negative(errors, "CODEX_COST_PER_MTOK_OUT", self.codex_cost_per_mtok_out)
        _require_non_negative(errors, "ISSUE_SPEND_CAP_USD", self.issue_spend_cap_usd)
        _require_non_negative(errors, "DAILY_SPEND_CAP_USD", self.daily_spend_cap_usd)
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

        return "\n".join([
            f"repo={self.repo or '(UNSET)'} base={self.base_branch} "
            # getattr, not IdentityMode(...): describe() is the startup diagnostic
            # and must print whatever it was handed rather than raise on it.
            f"identity={getattr(self.identity_mode, 'value', self.identity_mode)} "
            f"poll={self.poll_seconds}s",
            f"fixer={self.fixer_runtime} fix_model={model(self.fix_model)} "
            f"fix_budget=${self.fix_budget_usd:.2f}",
            f"review_model={model(self.review_model)} mode={self.review_mode.value} "
            f"review_budget=${self.review_budget_usd:.2f} "
            f"rejections_cap={self.max_review_rejections}",
            f"retry_caps: resolve={self.max_resolve_retries} ci={self.max_ci_retries} "
            f"review={self.max_review_retries} rebase={self.max_rebase_retries}",
            f"timeouts: idle={self.agent_idle_timeout:.0f}s "
            f"wall={self.agent_max_runtime:.0f}s",
            f"spend_caps: issue=${self.issue_spend_cap_usd:.2f} "
            f"daily=${self.daily_spend_cap_usd:.2f} "
            f"alert={'set' if self.alert_cmd else 'none'}",
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


def from_env(env: dict | None = None) -> HarnessConfig:
    e = env if env is not None else os.environ
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
        auto_merge=_b(e, "AUTO_MERGE", _dc_default("auto_merge"), errors),
        merge_method=e.get("MERGE_METHOD", _dc_default("merge_method")),
        ci_ignore_checks=e.get("CI_IGNORE_CHECKS", _dc_default("ci_ignore_checks")),
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
        agent_idle_timeout=_coerce_float(
            e, "AGENT_IDLE_TIMEOUT_SECONDS", _dc_default("agent_idle_timeout"), errors
        ),
        agent_max_runtime=_coerce_float(
            e, "AGENT_MAX_RUNTIME_SECONDS", _dc_default("agent_max_runtime"), errors
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
        issue_spend_cap_usd=_coerce_float(
            e, "ISSUE_SPEND_CAP_USD", _dc_default("issue_spend_cap_usd"), errors
        ),
        daily_spend_cap_usd=_coerce_float(
            e, "DAILY_SPEND_CAP_USD", _dc_default("daily_spend_cap_usd"), errors
        ),
        alert_cmd=e.get("ALERT_CMD", _dc_default("alert_cmd")),
        parse_errors=tuple(errors),
    )
