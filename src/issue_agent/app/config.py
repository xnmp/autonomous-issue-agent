"""Environment → config objects (port of the agent.env vars the bash reads).

Pure mapping `dict → HarnessConfig` so tests construct it without os.environ.
Names mirror config/agent.env.example so operators carry the same env file over.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..domain.review import ReviewMode


@dataclass(frozen=True)
class RepositoryConfig:
    repo: str
    base_branch: str
    target_dir: str
    identity_mode: str


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


@dataclass(frozen=True)
class StewardRuntimeConfig:
    bot: str
    triage_enabled: bool
    promote: bool
    owner: str
    triage_prompt: str
    triage_model: str | None
    triage_budget_usd: float


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


@dataclass(frozen=True)
class HarnessConfig:
    repo: str
    base_branch: str = "dev"
    target_dir: str = ""
    identity_mode: str = "three"
    fix_bot: str = ""
    review_bot: str = "my-reviewer-bot[bot]"
    steward_bot: str = "my-reviewer-bot[bot]"
    state_dir: str = ""
    review_state_dir: str = ""
    steward_state_dir: str = ""
    worktree_root: str = ".claude/worktrees"
    poll_seconds: int = 60
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
        )

    def validation_errors(self) -> tuple[str, ...]:
        errors: list[str] = []
        _require_positive(errors, "POLL_SECONDS", self.poll_seconds)
        _require_non_negative(errors, "MAX_CI_RETRIES", self.max_ci_retries)
        _require_non_negative(errors, "MAX_REVIEW_RETRIES", self.max_review_retries)
        _require_non_negative(errors, "MAX_REBASE_RETRIES", self.max_rebase_retries)
        _require_non_negative(errors, "MAX_RESOLVE_RETRIES", self.max_resolve_retries)
        if not (self.repo or "").strip():
            # Fail LOUD at startup, not with an opaque gh error on the first
            # poll: an empty REPO means a missing/mis-composed overlay (the
            # silent-default class, #50).
            errors.append("REPO must be set (owner/name) — is the repo overlay composed?")
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
            f"identity={self.identity_mode} poll={self.poll_seconds}s",
            f"fixer={self.fixer_runtime} fix_model={model(self.fix_model)} "
            f"fix_budget=${self.fix_budget_usd:.2f}",
            f"review_model={model(self.review_model)} mode={self.review_mode.value} "
            f"review_budget=${self.review_budget_usd:.2f} "
            f"rejections_cap={self.max_review_rejections}",
            f"retry_caps: resolve={self.max_resolve_retries} ci={self.max_ci_retries} "
            f"review={self.max_review_retries} rebase={self.max_rebase_retries}",
            f"timeouts: idle={self.agent_idle_timeout:.0f}s "
            f"wall={self.agent_max_runtime:.0f}s",
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


def _b(env: dict, key: str, default: bool) -> bool:
    v = env.get(key)
    if v is None:
        return default
    return v not in ("0", "false", "no", "off", "")


def _require_non_negative(errors: list[str], name: str, value: int | float) -> None:
    if value < 0:
        errors.append(f"{name} must be >= 0")


def _require_positive(errors: list[str], name: str, value: int | float) -> None:
    if value <= 0:
        errors.append(f"{name} must be > 0")


def from_env(env: dict | None = None) -> HarnessConfig:
    e = env if env is not None else os.environ
    return HarnessConfig(
        repo=e.get("REPO", ""),
        base_branch=e.get("BASE_BRANCH", "dev"),
        target_dir=e.get("TARGET_DIR", ""),
        identity_mode=e.get("IDENTITY_MODE", "three"),
        fix_bot=e.get("FIX_BOT", ""),
        # Default to the shared GitHub App login so an operator only needs to
        # supply the App credentials (GH_APP_*), not restate the bot name. Override
        # per-repo/shared only if running a differently-named App.
        review_bot=e.get("REVIEW_BOT", "my-reviewer-bot[bot]"),
        steward_bot=e.get("STEWARD_BOT", "my-reviewer-bot[bot]"),
        state_dir=e.get("STATE_DIR", os.path.expanduser("~/.local/state/issue-agent")),
        review_state_dir=e.get("REVIEW_STATE_DIR", os.path.expanduser("~/.local/state/review-agent")),
        steward_state_dir=e.get("STEWARD_STATE_DIR", os.path.expanduser("~/.local/state/steward-agent")),
        worktree_root=e.get("WORKTREE_ROOT", ".claude/worktrees"),
        poll_seconds=int(e.get("POLL_SECONDS", "20")),
        auto_merge=_b(e, "AUTO_MERGE", True),
        merge_method=e.get("MERGE_METHOD", "squash"),
        ci_ignore_checks=e.get("CI_IGNORE_CHECKS", ""),
        close_issue_on_merge=_b(e, "CLOSE_ISSUE_ON_MERGE", True),
        auto_rebase_conflicts=_b(e, "AUTO_REBASE_CONFLICTS", True),
        max_rebase_retries=int(e.get("MAX_REBASE_RETRIES", "3")),
        max_resolve_retries=int(e.get("MAX_RESOLVE_RETRIES", "3")),
        max_ci_retries=int(e.get("MAX_CI_RETRIES", "3")),
        max_review_rejections=int(e.get("REVIEW_MAX_REJECTIONS", "4")),
        max_review_retries=int(e.get("MAX_REVIEW_RETRIES", "3")),
        review_trigger_label=e.get("REVIEW_TRIGGER_LABEL", "ai-pr-open"),
        fix_model=e.get("FIX_MODEL") or None,
        review_model=e.get("REVIEW_MODEL") or None,
        steward_triage_model=e.get("STEWARD_TRIAGE_MODEL", "sonnet") or None,
        fix_budget_usd=float(e.get("MAX_BUDGET_USD", "10.00")),
        review_budget_usd=float(e.get("REVIEW_BUDGET_USD", "8.00")),
        triage_budget_usd=float(e.get("STEWARD_TRIAGE_BUDGET_USD", "0.75")),
        review_mode=ReviewMode.from_env(
            e.get("REVIEW_MODE", ""), _b(e, "REVIEW_MULTI_AGENT", False)
        ),
        # Operator REVIEW_PROMPT override, if any (wins wholesale). Blank/unset →
        # "", and main.py loads the default from plugin/REVIEW_PROMPT.md (kept out
        # of this pure dict→config mapping, like agent_md). A whitespace-only value
        # is treated as unset so it can't strip the reviewer's whole rubric.
        review_prompt=(e.get("REVIEW_PROMPT") or "").strip(),
        review_adopt_unlabeled=_b(e, "REVIEW_ADOPT_UNLABELED", True),
        review_gate_model=e.get("REVIEW_GATE_MODEL", "haiku") or None,
        review_validation_model=e.get("REVIEW_VALIDATION_MODEL", "haiku") or None,
        review_scout_model=e.get("REVIEW_SCOUT_MODEL", "sonnet") or None,
        review_gate_budget_usd=float(e.get("REVIEW_GATE_BUDGET_USD", "0.10")),
        review_dimension_budget_usd=float(e.get("REVIEW_DIMENSION_BUDGET_USD", "0.75")),
        review_validation_budget_usd=float(e.get("REVIEW_VALIDATION_BUDGET_USD", "0.20")),
        steward_triage=_b(e, "STEWARD_TRIAGE", True),
        steward_promote=_b(e, "STEWARD_PROMOTE", False),
        steward_owner=e.get("STEWARD_OWNER", ""),
        edit_check_cmd=e.get("EDIT_CHECK_CMD", ""),
        author_tests_split=_b(e, "AUTHOR_TESTS_SPLIT", False),
        max_contract_rounds=int(e.get("MAX_CONTRACT_ROUNDS", "2")),
        fix_mcp_config=e.get("FIX_MCP_CONFIG", ""),
        harness_root=e.get("HARNESS_ROOT_DIR", ""),
        jira_sync=_b(e, "JIRA_SYNC", True),
        jira_project_key=e.get("JIRA_PROJECT_KEY", ""),
        jira_cloud_id=e.get("JIRA_CLOUD_ID", ""),
        jira_mcp_config=e.get("JIRA_MCP_CONFIG", "config/mcp/atlassian.json"),
        jira_mcp_tools=e.get("JIRA_MCP_TOOLS", "mcp__atlassian__*"),
        jira_budget_usd=float(e.get("JIRA_SYNC_BUDGET_USD", "0.50")),
        fixer_runtime=e.get("FIXER_RUNTIME", "claude"),
        codex_bin=e.get("CODEX_BIN", "codex"),
        codex_model=e.get("CODEX_MODEL", ""),
        codex_provider_id=e.get("CODEX_PROVIDER_ID", "openai-custom"),
        codex_provider_name=e.get("CODEX_PROVIDER_NAME", "OpenAI"),
        codex_base_url=e.get("CODEX_BASE_URL", ""),
        codex_env_key=e.get("CODEX_ENV_KEY", "OPENAI_API_KEY"),
        codex_wire_api=e.get("CODEX_WIRE_API", "responses"),
        codex_sandbox=e.get("CODEX_SANDBOX", "danger-full-access"),
        agent_idle_timeout=float(e.get("AGENT_IDLE_TIMEOUT_SECONDS", "900") or 0),
        agent_max_runtime=float(e.get("AGENT_MAX_RUNTIME_SECONDS", "2700") or 0),
        codex_transport_retries=int(e.get("CODEX_TRANSPORT_RETRIES", "4") or 0),
        codex_backoff_base=float(e.get("CODEX_BACKOFF_BASE_SECONDS", "2") or 0),
        codex_rate_limit_backoff_base=float(
            e.get("CODEX_RATE_LIMIT_BACKOFF_BASE_SECONDS", "60") or 0
        ),
        codex_max_rate_limit_wait=float(e.get("CODEX_MAX_RATE_LIMIT_WAIT_SECONDS", "180") or 0),
        codex_max_rate_limit_retries=int(e.get("CODEX_MAX_RATE_LIMIT_RETRIES", "5") or 0),
    )
