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
from ..adapters.journal import (
    Journal,
    JournalingRuntime,
    default_ledger_path,
    hold_keys,
    read_events_tail,
    unsettled_starts,
)
from ..adapters.store import Store
from ..adapters.worktree import Worktrees
from ..domain.fix import FixConfig, FixState, breaker_key, decide_fix, issue_holds
from ..domain.labels import Label
from ..domain.lifecycle import retry_breaker
from ..domain.review import (
    DEFAULT_MAX_UNPRODUCTIVE_ROUNDS,
    ReviewConfig,
    ReviewState,
    decide_review,
)
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


def _process_deaths(store: Store) -> dict[int, int]:
    """Paid runs this role's journal shows starting and never settling, keyed
    the way the `infra-deaths` counter is (review P0R-4).

    A session the DAEMON did not outlive — SIGKILL, OOM, `systemctl restart`
    mid-run — reaches none of the bump sites, because every one of them is code
    that runs after the death. The `agent_run_start` line written before the
    await is the only durable trace, so the decide side reconstructs the count
    from it. Read-side only: nothing new is written on the hot path, and the
    scan is a bounded tail of a file this role already owns."""
    return unsettled_starts(read_events_tail(_journal(store).path))


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
    #
    # The store counter only ever sees deaths the PROCESS SURVIVED. Deaths that
    # took the daemon with them (SIGKILL/OOM/restart) left an unsettled
    # `agent_run_start` in the journal and nothing else, so they are added here
    # — the one place where both halves can be summed without either write side
    # knowing about the other (review P0R-4). A raise-path death is in the store
    # counter AND has a settled `transport_death` line, so `unsettled_starts`
    # nets it to zero and it is counted exactly once.
    #
    # A PR's entry SUMS both key spaces (review P1R-1, matching `_review_state`).
    # When the dispatcher's pre-handler `issue_for_pr` lookup fails — and the
    # network flakiness that drops a session is exactly the flakiness that fails
    # that lookup — the death lands under `-pr` while the decide side asks for
    # `breaker_key(pr)`, splitting one loop across two counters so the ceiling
    # never trips. Summing folds the orphaned half back into the key the domain
    # actually reads. `alt` is skipped when it IS the breaker key (an issueless
    # PR, where breaker_key already == -pr), so nothing is counted twice.
    deaths = _process_deaths(store)

    def _infra_for_pr(p) -> int:
        k = breaker_key(p)
        alt = -p.number
        total = store.get_infra_deaths(k) + deaths.get(k, 0)
        if alt != k:
            total += store.get_infra_deaths(alt) + deaths.get(alt, 0)
        return total

    infra_deaths = {breaker_key(p): _infra_for_pr(p) for p in world.prs}
    for i in world.issues:
        infra_deaths.setdefault(
            i.number, store.get_infra_deaths(i.number) + deaths.get(i.number, 0)
        )
    return FixState(
        sessions=sessions, resumed_at=resumed, automerge_enabled=automerge,
        ci_retries=ci_retries, review_retries=review_retries,
        rebase_retries=rebase_retries, resolve_retries=resolve_retries,
        infra_deaths=infra_deaths,
    )


def _hold_reporter(journal: Journal | None, repo: str, log):
    """Build the per-poll reporter that makes a DELIBERATE hold visible.

    `pick_ready_issue` skips an ai-ready issue whose files overlap an in-flight
    PR, or that has an open GitHub dependency — silently, which is
    indistinguishable from a fixer that is simply broken (harness #6: a hold on
    #522 cost ~30 minutes of live investigation before anyone could tell which
    it was).

    Same two-channel shape as `AgentDispatcher._spend_block`, for the same
    reasons: the LOG line fires every poll (local, free, and "why is this issue
    not moving?" must be answerable from any single poll's log), while the
    JOURNAL line fires once per (issue, reason, blocker) — a hold is a level,
    not an event, so an un-deduped emit is one line every 20s for as long as
    the blocking PR is open. The identity travels IN the event as `block_key`,
    so the dedup survives the process restarts a hot reload produces; the
    in-process set is the cache that keeps the steady state free of file reads.
    """
    seen: set[str] = set()

    def report(world) -> None:
        for hold in issue_holds(world):
            waiting_on = f" ({hold.blocker})" if hold.blocker else ""
            log(
                f"HOLD: issue #{hold.issue} is ai-ready but held — "
                f"{hold.reason}{waiting_on}; it dispatches once that clears"
            )
            key = f"hold:{repo}:{hold.issue}:{hold.reason}:{hold.blocker}"
            if key in seen:
                continue
            seen.add(key)
            if journal is None:
                continue
            if key in hold_keys(read_events_tail(journal.path)):
                continue
            journal.emit(
                "hold", issue=hold.issue, reason=hold.reason,
                blocker=hold.blocker, block_key=key,
            )

    return report


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
        arm_on_open=cfg.arm_on_open_active,
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
        fixer_runtime=cfg.fixer_runtime,
        **_spend_cfg(cfg, "fix", ledger),
    )
    # Fixer runtime: Codex (GPT-5.5) swaps the runner AND installs the governor as
    # a Codex hook in each worktree (the Claude runner gets its governor via SDK
    # hooks=, so prepare_cwd stays None there). Reviewer/steward always use Claude.
    fix_runner = runner
    prepare_cwd = None
    if cfg.fixer_runtime == "codex":
        from ..adapters.codex_runner import (
            CodexRunner,
            CodexProvider,
            codex_cost_blindspot_warning,
        )
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
            cost_per_mtok_in=codex_runtime.cost_per_mtok_in,
            cost_per_mtok_out=codex_runtime.cost_per_mtok_out,
            **({"default_model": codex_runtime.model} if codex_runtime.model else {}),
        )
        # Surface a codex misconfig (missing bin/key) at STARTUP, not on the first
        # dispatch — a clear one-liner instead of an opaque per-issue exit 1.
        _reason = fix_runner.preflight()
        if _reason is not None:
            _log("fix")(f"codex: NOT READY at startup — {_reason}")
        else:
            _log("fix")("codex: runtime ready (bin + key present)")
        # Review M1 / D12: ONE loud line at build time (never per dispatch) when a
        # spend cap is armed but no codex price table is set — those caps cannot see
        # a single codex dollar, and codex has no per-session budget flag either.
        # Structurally unreachable on FIXER_RUNTIME=claude (this whole block is).
        _blindspot = codex_cost_blindspot_warning(
            cost_per_mtok_in=codex_runtime.cost_per_mtok_in,
            cost_per_mtok_out=codex_runtime.cost_per_mtok_out,
            issue_spend_cap_usd=cfg.issue_spend_cap_usd,
            daily_spend_cap_usd=cfg.daily_spend_cap_usd,
            max_runtime=codex_runtime.max_runtime,
        )
        if _blindspot is not None:
            _log("fix")(_blindspot)
        # prepare_cwd installs the governor hook AND writes AGENTS.md (= AGENT.md)
        # into each worktree, Codex's native per-project instructions file. It also
        # installs the PostToolUse edit-check when EDIT_CHECK_CMD is set.
        prepare_cwd = partial(
            write_hook_config, agents_md=cfg.agent_md, edit_check_cmd=cfg.edit_check_cmd
        )
    elif cfg.fixer_runtime == "glm":
        # GLM speaks Anthropic's wire protocol, so the SAME Claude runner is used
        # with the profile's env injected (base URL + model aliases + auth token);
        # prepare_cwd stays None because that runner gets its governor via SDK
        # hooks, exactly as on FIXER_RUNTIME=claude.
        from ..adapters.claude_profile import (
            EnvInjectingRuntime,
            claude_profile_env,
            endpoint_host,
        )

        try:
            _glm_env = claude_profile_env(cfg.glm_settings_file)
        except ValueError as exc:
            # Same contract as the codex preflight: a misconfig is ONE loud line at
            # startup, not an opaque failure on the first dispatch. Unwrapped means
            # the fixer runs on plain Claude — visibly wrong beats silently wrong.
            _log("fix")(f"glm: NOT READY at startup — {exc}")
        else:
            fix_runner = EnvInjectingRuntime(runner, env=_glm_env)
            _log("fix")(
                f"glm: runtime ready (endpoint {endpoint_host(_glm_env)}, "
                f"profile {_glm_env['CLAUDE_CONFIG_DIR']})"
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

    report_holds = _hold_reporter(journal, cfg.repo, _log("fix"))

    def decide(world):
        # Report BEFORE deciding: if decide_fix raises, the tick still leaves a
        # record of what it was holding, which is the state hardest to diagnose.
        report_holds(world)
        return decide_fix(world, fix_cfg, _fix_state(store, world), fixer_busy())

    return Daemon("fix", gateway.build_world, decide, executor, cfg.poll_seconds,
                  _log("fix"), journal=journal)


def _review_state(store: Store, world) -> ReviewState:
    """Read the per-PR state `decide_review` needs — including the ceiling
    counter that stops the reviewer paying for a PR that never converges.

    `unproductive_rounds` is PR-keyed in the domain but reconciled from TWO
    store key spaces here, because the two write sides disagree by construction
    (review P0R-3):

      * `AgentDispatcher.dispatch` bumps `infra-deaths` under
        `gateway.issue_for_pr(pr)`, falling back to `-pr` when that lookup
        returns None *or raises* — and the flakiness that kills sessions is
        exactly the flakiness that fails the lookup, so a PR's deaths can land
        in BOTH key spaces across a run of polls;
      * `ReviewWorkflow` bumps the same counter under `breaker_key(pr)` (pure,
        from the PRView it already fetched) for a round that ran and persisted
        nothing.

    Summing the union is what makes the ceiling see every paid round regardless
    of which key the writer chose. `breaker_key(pr) == -pr` for an issueless PR,
    and a set literal dedupes it, so nothing is double counted."""
    sessions = frozenset(p.number for p in world.prs if store.get_pr_session(p.number))
    last = {p.number: store.get_last_sha(p.number) for p in world.prs}
    deaths = _process_deaths(store)  # review P0R-4 — see _fix_state
    unproductive = {
        p.number: sum(
            store.get_infra_deaths(k) + deaths.get(k, 0)
            for k in {breaker_key(p), -p.number}
        )
        for p in world.prs
    }
    return ReviewState(
        sessions=sessions, last_sha=last, unproductive_rounds=unproductive
    )


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
        # getattr, not a plain attribute read: the env knob
        # (MAX_UNPRODUCTIVE_REVIEW_ROUNDS) is wired into ReviewRuntimeConfig
        # separately from this ceiling. Until it lands the domain default
        # applies — a missing knob must never mean "no ceiling", which is the
        # unbounded state P0R-3 describes.
        max_unproductive_rounds=getattr(
            review_runtime, "max_unproductive_rounds", DEFAULT_MAX_UNPRODUCTIVE_ROUNDS
        ),
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

    # Same ceiling shape as the reviewer's (review P0R-3), for the same reason:
    # the steward's "already handled" markers — `triage-N.at` and the jira phase
    # — are written ONLY on success, so a session that dies leaves the issue in
    # the candidate set and the next poll buys it again. Unlike the other two
    # roles the steward escalates NOTHING: an untriaged issue is not work anyone
    # promised to finish, and moving it to needs-human-review would be the
    # steward inventing lifecycle state for an issue it never triaged. It stops
    # spending and says so, once per change in the suppressed set (not once per
    # poll — a line repeated 4,300 times a day is a line nobody reads).
    max_deaths = getattr(
        steward_runtime, "max_infra_deaths", DEFAULT_MAX_UNPRODUCTIVE_ROUNDS
    )
    suppressed_last: set[int] = set()

    def decide(world):
        state = TriageState(seen_at={i.number: store.get_triage_at(i.number) for i in world.issues})
        deaths = _process_deaths(store)

        def exhausted(n: int) -> bool:
            spent = store.get_infra_deaths(n) + deaths.get(n, 0)
            return retry_breaker(spent, max_deaths).tripped

        actions = [
            RunAgent(AgentKind.TRIAGE, n)
            for n in triage_candidates(world, tcfg_dom, state)
            if not exhausted(n)
        ]
        if jira_on:
            for i in world.issues:
                target = jira_phase(i.labels)
                if target is not None and not exhausted(i.number) and jira_sync_needed(
                    target, store.get_jira_key(i.number), store.get_jira_phase(i.number)
                ):
                    actions.append(RunAgent(AgentKind.JIRA_SYNC, i.number))
        suppressed = {i.number for i in world.issues if exhausted(i.number)}
        if suppressed != suppressed_last:
            if suppressed:
                steward_log(
                    f"steward: no longer dispatching for issue(s) "
                    f"{sorted(suppressed)} — {max_deaths} sessions died before "
                    "returning a result. Check the runtime/daemon; clear the "
                    "issue-N.infra-deaths marker in the steward STATE_DIR to retry."
                )
            suppressed_last.clear()
            suppressed_last.update(suppressed)
        return actions

    # Jira needs closed ai-done issues too (for the final Done transition); triage
    # alone needs just open issues.
    observe = gateway.build_steward_world if jira_on else gateway.build_triage_world
    return Daemon("steward", observe, decide, executor, cfg.poll_seconds, steward_log,
                  journal=journal)
