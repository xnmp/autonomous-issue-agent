"""Assemble the three role daemons from config + adapters.

Each `build_*_daemon` binds:
  * observe  → gateway.build_world (the GitHub slice the role needs),
  * decide   → the bound pure decision function (decide_fix / decide_review /
               decide_steward-triage),
  * executor → applies the leaf Actions; its `run_agent` drives the (async)
               AgentDispatcher for RunAgent actions.

The dispatcher is async (SDK query is a coroutine), but Daemon.tick is sync, so
`run_agent` runs the coroutine to completion with asyncio.run — one agent at a
time, matching the bash single-fixer / sequential model.
"""

from __future__ import annotations

import asyncio
import os
from functools import partial

from ..adapters.agent_runner import AgentRunner
from ..adapters.github import GitHubGateway, Identity
from ..adapters.journal import Journal, JournalingRuntime, default_ledger_path
from ..adapters.store import Store
from ..adapters.worktree import Worktrees
from ..domain.fix import FixConfig, FixState, breaker_key, decide_fix
from ..domain.labels import Label
from ..domain.review import ReviewConfig, ReviewState, decide_review
from .planners.tasks import FixTaskConfig, JiraTaskConfig, ReviewTaskConfig, TriageTaskConfig
from ..domain.triage import (
    TriageConfig,
    TriageState,
    parse_codeowners_default,
    triage_candidates,
)
from ..domain.jira import jira_phase, jira_sync_needed
from ..domain.actions import AgentKind, RunAgent
from .config import HarnessConfig
from .dispatch import AgentDispatcher, DispatchConfig
from .execute import Executor
from .loop import Daemon
from .mcp import mcp_servers_from_file


def _log(name: str):
    import sys

    def log(msg: str) -> None:
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()  # unbuffered: a crash right after must not eat the line

    return log


def _journal(store: Store) -> Journal:
    """Each role journals beside its own state, because each role already has an
    isolated STATE_DIR — so three daemons never contend on one file. The GLOBAL
    ledger is the deliberate exception: cost is the one quantity that must be
    summed across every role and repo, and POSIX append atomicity makes sharing
    it safe without a lock."""
    return Journal(os.path.join(store.state_dir, "journal.jsonl"))


def _spend_cfg(cfg: HarnessConfig, role: str, ledger: str) -> dict:
    """The spend-breaker slice of DispatchConfig, identical for all three roles."""
    return {
        "role": role,
        "repo": cfg.repo,
        "spend_ledger_path": ledger,
        "issue_spend_cap_usd": cfg.issue_spend_cap_usd,
        "daily_spend_cap_usd": cfg.daily_spend_cap_usd,
        "alert_cmd": cfg.alert_cmd,
    }


def _fix_state(store: Store, world) -> FixState:
    """Read the per-issue/PR orchestration state the fix decision needs."""
    sessions = frozenset(i.number for i in world.issues if store.has_session(i.number))
    resumed = {i.number: store.get_resumed_at(i.number) for i in world.issues}
    automerge = frozenset(p.number for p in world.prs if store.automerge_enabled(p.number))
    # Retry counters are keyed by breaker_key(pr): the linked issue number, or the
    # NEGATED PR number when a PR has no resolvable issue (Jira-keyed branch, no
    # closing-ref). Must match the workflow's increment key exactly, else the
    # breaker reads 0 forever and the retry loop never escalates (was PR #112).
    keys = {breaker_key(p) for p in world.prs}
    ci_retries = {k: store.get_ci_retries(k) for k in keys}
    # Full (review_at, count) tuple: the domain compares the stored timestamp
    # against the CURRENT review's submitted_at so a superseding review reads
    # as count 0 (see FixState.review_retries).
    review_retries = {k: store.get_review_retry(k) for k in keys}
    rebase_retries = {k: store.get_rebase_retries(k) for k in keys}
    resolve_retries = {i.number: store.get_resolve_retries(i.number) for i in world.issues}
    # Infra deaths (review C3) span BOTH key spaces: the issue legs (resolve /
    # resume) escalate on the issue number, the PR legs on breaker_key(pr) —
    # and the dispatcher writes under whichever one the dying leg used. Read the
    # union, or a leg escalates on a counter nobody reads.
    infra_deaths = {
        k: store.get_infra_deaths(k)
        for k in keys | {i.number for i in world.issues}
    }
    return FixState(
        sessions=sessions, resumed_at=resumed, automerge_enabled=automerge,
        ci_retries=ci_retries, review_retries=review_retries,
        rebase_retries=rebase_retries, resolve_retries=resolve_retries,
        infra_deaths=infra_deaths,
    )


def build_fix_daemon(
    cfg: HarnessConfig,
    gateway: GitHubGateway,
    store: Store,
    worktrees: Worktrees,
    runner: AgentRunner,
    fixer_busy=lambda: False,
) -> Daemon:
    repo_cfg = cfg.repository
    fix_runtime = cfg.fix_runtime
    codex_runtime = cfg.codex_runtime
    fix_cfg = FixConfig(
        fix_bot=fix_runtime.bot,
        identity_mode=repo_cfg.identity_mode,
        auto_merge=cfg.auto_merge,
        merge_method=cfg.merge_method,
        ci_ignore_checks=cfg.ci_ignore_checks,
        close_issue_on_merge=cfg.close_issue_on_merge,
        max_ci_retries=fix_runtime.max_ci_retries,
        max_review_retries=fix_runtime.max_review_retries,
        auto_rebase_conflicts=fix_runtime.auto_rebase_conflicts,
        max_rebase_retries=fix_runtime.max_rebase_retries,
        max_resolve_retries=fix_runtime.max_resolve_retries,
        max_infra_deaths=fix_runtime.max_infra_deaths,
    )
    journal = _journal(store)
    ledger = default_ledger_path()
    dcfg = DispatchConfig(
        base_branch=repo_cfg.base_branch,
        fix=FixTaskConfig(
            repo_cfg.repo, repo_cfg.base_branch, cfg.agent_md,
            fix_runtime.budget_usd, fix_runtime.model,
        ),
        max_review_rejections=cfg.max_review_rejections,
        author_tests_split=fix_runtime.author_tests_split,
        max_contract_rounds=fix_runtime.max_contract_rounds,
        fix_mcp_config=fix_runtime.mcp_config,
        harness_root=cfg.runtime_paths.harness_root,
        log=_log("fix"),
        fix_bot=fix_runtime.bot,
        **_spend_cfg(cfg, "fix", ledger),
    )
    # Fixer runtime: Codex (GPT-5.5) swaps the runner AND installs the governor as
    # a Codex hook in each worktree (the Claude runner gets its governor via SDK
    # hooks=, so prepare_cwd stays None there). Reviewer/steward always use Claude.
    fix_runner = runner
    prepare_cwd = None
    if cfg.fixer_runtime == "codex":
        from ..adapters.codex_runner import CodexRunner, CodexProvider
        from ..adapters.codex_hook import write_hook_config

        fix_runner = CodexRunner(
            codex_bin=codex_runtime.binary,
            provider=CodexProvider(
                provider_id=codex_runtime.provider_id,
                name=codex_runtime.provider_name,
                base_url=codex_runtime.base_url,
                env_key=codex_runtime.env_key,
                wire_api=codex_runtime.wire_api,
            ),
            sandbox=codex_runtime.sandbox,
            # AGENT.md reaches GPT-5.5 via the worktree AGENTS.md (below), so the
            # task's append_system_prompt must NOT also be prepended to the prompt
            # (that would duplicate the whole contract). Suppress it for codex.
            inject_system_prompt=False,
            log=_log("fix"),
            debug=os.environ.get("ISSUE_AGENT_DEBUG", "0") not in ("", "0", "false", "False"),
            idle_timeout=(codex_runtime.idle_timeout if codex_runtime.idle_timeout > 0 else None),
            max_runtime=(codex_runtime.max_runtime if codex_runtime.max_runtime > 0 else None),
            transport_retries=codex_runtime.transport_retries,
            backoff_base=codex_runtime.backoff_base,
            rate_limit_backoff_base=codex_runtime.rate_limit_backoff_base,
            max_rate_limit_wait=codex_runtime.max_rate_limit_wait,
            max_rate_limit_retries=codex_runtime.max_rate_limit_retries,
            **({"default_model": codex_runtime.model} if codex_runtime.model else {}),
        )
        # Surface a codex misconfig (missing bin/key) at STARTUP, not on the first
        # dispatch — a clear one-liner instead of an opaque per-issue exit 1.
        _reason = fix_runner.preflight()
        if _reason is not None:
            _log("fix")(f"codex: NOT READY at startup — {_reason}")
        else:
            _log("fix")("codex: runtime ready (bin + key present)")
        # prepare_cwd installs the governor hook AND writes AGENTS.md (= AGENT.md)
        # into each worktree, Codex's native per-project instructions file. It also
        # installs the PostToolUse edit-check when EDIT_CHECK_CMD is set.
        prepare_cwd = partial(
            write_hook_config, agents_md=cfg.agent_md, edit_check_cmd=cfg.edit_check_cmd
        )

    executor = Executor(
        gateway, remove_worktree=worktrees.remove, log=_log("fix"),
        mark_automerge=store.mark_automerge, clear_issue_state=store.cleanup_pr,
        journal=journal, alert_cmd=cfg.alert_cmd,
    )
    # Wrapped LAST, so the decorator sees whichever runtime actually ran (Claude or
    # the Codex swap above) and the ledger is runtime-agnostic.
    fix_runner = JournalingRuntime(fix_runner, journal, ledger, cfg.repo, "fix")
    dispatcher = AgentDispatcher(
        dcfg, gateway, store, worktrees, fix_runner, executor,
        prepare_cwd=prepare_cwd, journal=journal,
    )
    executor._run_agent = lambda a: asyncio.run(dispatcher.dispatch(a))  # noqa: SLF001

    def decide(world):
        return decide_fix(world, fix_cfg, _fix_state(store, world), fixer_busy())

    return Daemon("fix", gateway.build_world, decide, executor, cfg.poll_seconds,
                  _log("fix"), journal=journal)


def _review_state(store: Store, world) -> ReviewState:
    sessions = frozenset(p.number for p in world.prs if store.get_pr_session(p.number))
    last = {p.number: store.get_last_sha(p.number) for p in world.prs}
    return ReviewState(sessions=sessions, last_sha=last)


def build_review_daemon(
    cfg: HarnessConfig, gateway: GitHubGateway, store: Store, runner: AgentRunner
) -> Daemon:
    repo_cfg = cfg.repository
    review_runtime = cfg.review_runtime
    journal = _journal(store)
    ledger = default_ledger_path()
    rcfg = ReviewConfig(
        review_bot=review_runtime.bot, identity_mode=repo_cfg.identity_mode,
        trigger_label=Label(review_runtime.trigger_label),
        adopt_unlabeled=review_runtime.adopt_unlabeled,
    )
    dcfg = DispatchConfig(
        base_branch=repo_cfg.base_branch,
        review=ReviewTaskConfig(
            repo_cfg.repo, repo_cfg.base_branch, review_runtime.prompt,
            review_runtime.budget_usd, review_runtime.model,
            gate_model=review_runtime.gate_model,
            validation_model=review_runtime.validation_model,
            gate_budget_usd=review_runtime.gate_budget_usd,
            dimension_budget_usd=review_runtime.dimension_budget_usd,
            validation_budget_usd=review_runtime.validation_budget_usd,
            scout_model=review_runtime.scout_model,
        ),
        max_review_rejections=review_runtime.max_rejections,
        review_bot=review_runtime.bot,
        review_trigger_label=review_runtime.trigger_label,
        review_mode=review_runtime.mode,
        log=_log("review"),
        **_spend_cfg(cfg, "review", ledger),
    )
    # Reviewer has no worktree/issue side; pass a no-op Worktrees-like None usage.
    executor = Executor(gateway, log=_log("review"), journal=journal,
                        alert_cmd=cfg.alert_cmd)
    dispatcher = AgentDispatcher(
        dcfg, gateway, store, None,
        JournalingRuntime(runner, journal, ledger, cfg.repo, "review"),
        executor, journal=journal,
    )
    executor._run_agent = lambda a: asyncio.run(dispatcher.dispatch(a))  # noqa: SLF001

    def decide(world):
        return decide_review(world, rcfg, _review_state(store, world))

    return Daemon("review", gateway.build_review_world, decide, executor,
                  cfg.poll_seconds, _log("review"), journal=journal)


_CODEOWNERS_PATHS = ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS")


def _resolve_owner_handle(cfg: HarnessConfig, log) -> str:
    """Owner @-mention for triage's technical/architectural questions. Order:
    the target repo's CODEOWNERS `*` default owners → STEWARD_OWNER env → none.
    Never fails silently: logs which source won (or that none was found)."""
    base = cfg.target_dir or os.getcwd()
    for rel in _CODEOWNERS_PATHS:
        path = os.path.join(base, rel)
        try:
            with open(path, encoding="utf-8") as fh:
                owners = parse_codeowners_default(fh.read())
        except OSError:
            continue
        if owners:
            log(f"triage owner: {owners} (from {rel})")
            return owners
    if cfg.steward_owner:
        log(f"triage owner: {cfg.steward_owner} (from STEWARD_OWNER)")
        return cfg.steward_owner
    log(
        "triage owner: NONE — no CODEOWNERS default rule and STEWARD_OWNER unset; "
        "owner-level questions will be tagged generically ('a maintainer'). Set "
        "STEWARD_OWNER or add a CODEOWNERS '*' rule to route them."
    )
    return ""


def build_steward_daemon(
    cfg: HarnessConfig, gateway: GitHubGateway, store: Store, runner: AgentRunner
) -> Daemon:
    steward_log = _log("steward")
    repo_cfg = cfg.repository
    journal = _journal(store)
    ledger = default_ledger_path()
    steward_runtime = cfg.steward_runtime
    jira_runtime = cfg.jira_runtime
    tcfg_dom = TriageConfig(
        steward_bot=steward_runtime.bot, enabled=steward_runtime.triage_enabled,
        promote=steward_runtime.promote, triage_label=Label.AI_TRIAGE,
        owner_handle=_resolve_owner_handle(cfg, steward_log),
    )
    # Jira mirror: enabled by default, but INERT without a project key (so it's safe
    # out of the box). The Atlassian MCP spec is loaded from cfg.jira_mcp_config and
    # attached only to the steward's jira-sync task.
    jira_on = jira_runtime.sync and bool(jira_runtime.project_key)
    if jira_runtime.sync and not jira_runtime.project_key:
        steward_log("JIRA_SYNC is on but JIRA_PROJECT_KEY is unset — Jira sync inert")
    jira_cfg = None
    if jira_on:
        mcp = mcp_servers_from_file(
            jira_runtime.mcp_config, jira_runtime.mcp_tools,
            cfg.runtime_paths.harness_root, log=steward_log,
        )
        if not mcp:
            steward_log(
                f"JIRA_SYNC on but no MCP server loaded from {jira_runtime.mcp_config} — "
                "Jira sync inert (is the Atlassian MCP spec present?)"
            )
            jira_on = False
        else:
            jira_cfg = JiraTaskConfig(
                project_key=jira_runtime.project_key, cloud_id=jira_runtime.cloud_id,
                repo=repo_cfg.repo, mcp_servers=mcp, max_budget_usd=jira_runtime.budget_usd,
                model=steward_runtime.triage_model,
    )
    dcfg = DispatchConfig(
        base_branch=repo_cfg.base_branch,
        triage=TriageTaskConfig(
            steward_runtime.bot, steward_runtime.triage_prompt,
            steward_runtime.triage_budget_usd, steward_runtime.triage_model,
        ),
        triage_cfg=tcfg_dom,
        jira=jira_cfg,
        log=steward_log,
        **_spend_cfg(cfg, "steward", ledger),
    )
    executor = Executor(gateway, log=steward_log, journal=journal, alert_cmd=cfg.alert_cmd)
    dispatcher = AgentDispatcher(
        dcfg, gateway, store, None,
        JournalingRuntime(runner, journal, ledger, cfg.repo, "steward"),
        executor, journal=journal,
    )
    executor._run_agent = lambda a: asyncio.run(dispatcher.dispatch(a))  # noqa: SLF001

    def decide(world):
        state = TriageState(seen_at={i.number: store.get_triage_at(i.number) for i in world.issues})
        actions = [RunAgent(AgentKind.TRIAGE, n) for n in triage_candidates(world, tcfg_dom, state)]
        if jira_on:
            for i in world.issues:
                target = jira_phase(i.labels)
                if target is not None and jira_sync_needed(
                    target, store.get_jira_key(i.number), store.get_jira_phase(i.number)
                ):
                    actions.append(RunAgent(AgentKind.JIRA_SYNC, i.number))
        return actions

    # Jira needs closed ai-done issues too (for the final Done transition); triage
    # alone needs just open issues.
    observe = gateway.build_steward_world if jira_on else gateway.build_triage_world
    return Daemon("steward", observe, decide, executor, cfg.poll_seconds, steward_log,
                  journal=journal)
