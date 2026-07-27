"""AgentDispatcher — turns a RunAgent(kind, n) into: gather inputs → build the
AgentTask → run it → apply the pure follow-up Actions + persist session/counters.

This is the impure glue the executor's `run_agent` calls. It owns the GitHub
reads the follow-up needs (notably the ground-truth "did a PR actually open?"
check) and the worktree/session plumbing, but every DECISION is delegated to a
pure domain function (resolve_actions / resume_actions / address_review_actions /
fix_ci_actions / review_submission / triage.actions_for_verdict). So the
dispatcher stays thin and the logic stays tested in tests/domain.

Async because AgentRunner.run is async (the SDK query is a coroutine).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from collections.abc import Callable

from ..adapters.journal import (
    COST_BLINDSPOT_MIN_RUNS,
    alert,
    cost_blindspot_warning,
    event_line,
    read_events_tail,
    spend_blocked_keys,
    sum_issue,
    sum_today,
)
from ..domain import review as review_dom, triage as triage_dom
from ..domain.actions import AgentKind, RunAgent
from ..domain.agent_task import AgentRuntime
from ..domain.labels import Label
from ..domain.lifecycle import issue_escalation, pr_escalation
from .planners.tasks import FixTaskConfig, JiraTaskConfig, ReviewTaskConfig, TriageTaskConfig
from .workflows.fix import FixWorkflow, FixWorkflowConfig
from .workflows.jira import JiraSyncWorkflow, JiraSyncWorkflowConfig
from .workflows.review import ReviewWorkflow, ReviewWorkflowConfig
from .workflows.steward import StewardWorkflow, StewardWorkflowConfig


@dataclass
class DispatchConfig:
    base_branch: str = "dev"
    fix: FixTaskConfig | None = None
    review: ReviewTaskConfig | None = None
    triage: TriageTaskConfig | None = None
    triage_cfg: triage_dom.TriageConfig | None = None
    jira: JiraTaskConfig | None = None
    fork_threshold: int = 5
    max_review_rejections: int = 4
    author_tests_split: bool = False
    max_contract_rounds: int = 2
    # REVIEW_MODE: which reviewer strategy to run —
    #   SINGLE    — one session-resumed agent reads diff + files itself (default).
    #   MULTI     — the /code-review fan-out (skip-gate → dimension specialists →
    #               per-finding adversarial validation → pure synthesis).
    #   DELEGATED — SINGLE + a cheap code-scout subagent the reviewer delegates
    #               codebase search to (expensive model reasons over summaries).
    review_mode: review_dom.ReviewMode = review_dom.ReviewMode.SINGLE
    # Reviewer bot login — used to count the reviewer's OWN prior REQUEST_CHANGES
    # verdicts on a PR (the rejection ceiling), distinguishing them from any human
    # or other-bot reviews.
    review_bot: str = ""
    fix_mcp_config: str = ""
    harness_root: str = ""
    # PR label removed when a review escalates to needs-human-review. Matches the
    # reviewer's trigger label (default ai-pr-open; ai-review for an external repo).
    review_trigger_label: str = "ai-pr-open"
    # Diagnostic logger (stderr in production; the steward passes its tagged logger).
    log: Callable[[str], None] = lambda _m: None
    # Fixer login, threaded to FixWorkflow so its resume path filters comments
    # with the same rule the decide side uses (see FixWorkflowConfig.fix_bot).
    fix_bot: str = ""
    # Fixer runtime ("claude" | "codex"), threaded to FixWorkflow so difficulty
    # routing picks from the runtime's own model family (see MODEL_TIERS).
    fixer_runtime: str = "claude"
    # ── spend circuit breakers ────────────────────────────────────────────────
    # Which daemon this dispatcher belongs to, and the repo it targets: journal
    # attribution only, but without them a shared ledger cannot answer "who spent
    # this?".
    role: str = ""
    repo: str = ""
    # The GLOBAL ledger (see adapters.journal.default_ledger_path). Blank → the
    # caps are inert, because a cap that cannot read the spend would either block
    # everything or nothing depending on how the read failed.
    spend_ledger_path: str = ""
    # 0 disables either cap. Defaults here are 0 (not the operator defaults in
    # HarnessConfig) so a hand-built DispatchConfig — every test, every caller
    # that predates this — keeps exactly its old behavior.
    issue_spend_cap_usd: float = 0.0
    daily_spend_cap_usd: float = 0.0
    alert_cmd: str = ""

    def fix_workflow_config(self) -> FixWorkflowConfig:
        return FixWorkflowConfig(
            base_branch=self.base_branch,
            fix=self.fix,
            fork_threshold=self.fork_threshold,
            author_tests_split=self.author_tests_split,
            max_contract_rounds=self.max_contract_rounds,
            fix_mcp_config=self.fix_mcp_config,
            harness_root=self.harness_root,
            log=self.log,
            fix_bot=self.fix_bot,
            fixer_runtime=self.fixer_runtime,
        )

    def review_workflow_config(self) -> ReviewWorkflowConfig:
        return ReviewWorkflowConfig(
            review=self.review,
            review_mode=self.review_mode,
            max_review_rejections=self.max_review_rejections,
            review_bot=self.review_bot,
            review_trigger_label=self.review_trigger_label,
            log=self.log,
        )

    def steward_workflow_config(self) -> StewardWorkflowConfig:
        return StewardWorkflowConfig(
            triage=self.triage,
            triage_cfg=self.triage_cfg,
            log=self.log,
            # The steward reads the FIXER's merged PRs (harness #10): "already
            # shipped by this harness" means shipped by the fix bot, not by the
            # steward's own identity.
            fix_bot=self.fix_bot,
            repo=self.repo,
        )

    def jira_workflow_config(self) -> JiraSyncWorkflowConfig:
        return JiraSyncWorkflowConfig(jira=self.jira, log=self.log)


class AgentDispatcher:
    """Runs one RunAgent action end-to-end. `executor` applies the follow-up
    Actions (so all writes funnel through one place); `runner` is the SDK
    AgentRunner; `gateway`/`store`/`worktrees` are the adapters. `run` is
    `runner.run` (async). All collaborators are injected → fully fakeable."""

    def __init__(self, cfg: DispatchConfig, gateway, store, worktrees,
                 runner: AgentRuntime, executor, prepare_cwd=None, journal=None):
        self.cfg = cfg
        self.gw = gateway
        self.store = store
        self.runner = runner
        self.executor = executor
        self.journal = journal
        # PR → issue answers this process has actually received. A dying
        # gateway must not silently move a counter into a key space nobody
        # reads (review P1R-1); see `_spend_target`.
        self._issue_memo: dict[int, int] = {}
        # `block_key`s already journalled/alerted this process — the in-memory
        # half of the spend cap's edge trigger (review P1R-2).
        self._spend_blocked: set[str] = set()
        self._cost_blindspot_checked = False
        self.fix_workflow = FixWorkflow(
            cfg.fix_workflow_config(), gateway, store, worktrees, runner, executor, prepare_cwd
        )
        self.review_workflow = ReviewWorkflow(
            cfg.review_workflow_config(), gateway, store, runner, executor,
            self._run_logged,
        )
        self.steward_workflow = StewardWorkflow(
            # The journal reaches the dispatcher as a constructor argument, not
            # through DispatchConfig, so it is folded in here rather than being
            # duplicated as a second config field that could disagree with it.
            replace(cfg.steward_workflow_config(), journal=journal),
            gateway, store, runner, executor,
        )
        self.jira_workflow = JiraSyncWorkflow(cfg.jira_workflow_config(), gateway, store, runner)

    async def _run_logged(self, label: str, issue: int, task):
        """Run one agent leg, bracketing it with start/done breadcrumbs so a long
        in-session TDD run isn't a silent gap in the log. The per-commit milestones
        (red:/green:/docs:) are logged live by the governor hook as they happen."""
        self.cfg.log(f"agent #{issue}: {label} — running")
        result = await self.runner.run(task)
        self.cfg.log(
            f"agent #{issue}: {label} — done "
            f"(status={result.status or ('error' if result.is_error else 'ok')}"
            f"{f', ${result.cost_usd:.2f}' if result.cost_usd else ''})"
        )
        return result

    # Kinds keyed by PR number rather than issue number. The per-issue spend cap
    # needs the ISSUE behind the work, so these are resolved through the gateway.
    _PR_KEYED = frozenset({
        AgentKind.ADDRESS_REVIEW, AgentKind.FIX_CI, AgentKind.REBASE,
        AgentKind.REVIEW, AgentKind.RE_REVIEW,
    })

    def _lookup_issue(self, pr: int) -> tuple[int | None, bool]:
        """`(issue, resolved)` from the gateway. `resolved` is False iff the
        lookup RAISED — which is a different fact from a clean `None`.

        Review P1R-1: those two used to collapse into `None`. A clean None means
        "this PR genuinely has no issue", whose breaker key IS `-pr`
        (`domain.fix.breaker_key`). A raised lookup means "unknown", and
        answering it with `-pr` moves the infra-death counter into a key space
        the fix role's decide side never reads. The coupling is the trap: the
        flakiness that kills sessions is the flakiness that fails this lookup,
        so the ceiling would fail exactly when it is needed."""
        try:
            issue = self.gw.issue_for_pr(pr)
        except Exception as exc:  # noqa: BLE001 — a lookup failure ≠ a blocked run
            self.cfg.log(f"spend: could not resolve issue for PR #{pr}: {exc}")
            return None, False
        if issue is not None:
            self._issue_memo[pr] = issue
        return issue, True

    def _spend_target(self, action: RunAgent) -> tuple[int | None, bool]:
        """`(issue, resolved)` for the dispatch as a whole. `issue` is None only
        when it is genuinely unknowable; unknown is tolerated — it costs
        per-issue attribution, never a dispatch."""
        if action.kind not in self._PR_KEYED:
            return action.number, True
        issue, resolved = self._lookup_issue(action.number)
        if resolved:
            return issue, True
        # The lookup failed. The last answer this process got for the SAME PR is
        # a far better key than a silent switch to the negated-PR space.
        return self._issue_memo.get(action.number), False

    def _issue_number(self, action: RunAgent) -> int | None:
        """The issue this dispatch spends against (see `_spend_target`)."""
        return self._spend_target(action)[0]

    def _death_key(self, action: RunAgent, issue: int | None, resolved: bool) -> int:
        """The `infra-deaths` key this dead dispatch is charged to.

        It MUST be a key the decide side reads: the issue number for issue legs
        and for any PR whose issue is resolvable, and `-pr` ONLY for a PR that
        genuinely has none (`domain.fix.breaker_key`'s convention). When the
        pre-handler lookup raised, `-pr` is a guess and not a fact, so:

          1. retry the lookup once — the transient that killed the session is
             frequently over by the time the exception unwinds, and one extra gh
             call on a path that just burned a whole session is free;
          2. fall back to whatever this process last resolved for that PR;
          3. only then use `-pr`, and say so loudly, because at that point the
             counter genuinely may not be read by the fix role's `_fix_state`
             (the review role already reads the union — see `roles._review_state`).
        """
        if issue is not None:
            return issue
        if action.kind not in self._PR_KEYED:
            return action.number
        if not resolved:
            retried, ok = self._lookup_issue(action.number)
            if retried is not None:
                return retried
            memo = self._issue_memo.get(action.number)
            if memo is not None:
                return memo
            if not ok:
                self.cfg.log(
                    f"infra-death for PR #{action.number} charged to the negated "
                    f"key -{action.number}: its issue could not be resolved on "
                    "either attempt, so the per-issue ceiling may not see it"
                )
        return -action.number

    def _pr_escalation_label(self, kind: AgentKind) -> Label:
        """Which lifecycle label a PR escalation removes. The reviewer's trigger
        label is configurable (ai-review on a repo where another bot owns
        ai-pr-open) and removing the wrong one leaves the PR pickable forever
        (review M10/D4); the fixer's PR legs are always gated on ai-pr-open. An
        unknown string is a misconfiguration — fall back loudly rather than
        crash mid-escalation, exactly as `ReviewWorkflow._trigger_label` does."""
        if kind not in (AgentKind.REVIEW, AgentKind.RE_REVIEW):
            return Label.AI_PR_OPEN
        try:
            return Label(self.cfg.review_trigger_label)
        except ValueError:
            self.cfg.log(
                f"spend: REVIEW_TRIGGER_LABEL '{self.cfg.review_trigger_label}' is not "
                f"a known lifecycle label — escalating from '{Label.AI_PR_OPEN.value}'"
            )
            return Label.AI_PR_OPEN

    def _blocked_on_spend(self, action: RunAgent, issue: int | None) -> bool:
        """Two circuit breakers, checked before a single token is bought.

        **Per-issue (lifetime)** — a one-shot escalation. Work that has already
        cost the cap is not going to converge on the next retry, so it moves
        <lifecycle label> → needs-human-review, which structurally removes it
        from the pickable set. Uses the domain's `issue_escalation` /
        `pr_escalation` so the comment+relabel pair matches every other breaker.

        Escalating the ISSUE is only one-shot for issue-keyed legs (review
        P1R-2). A PR-keyed leg is picked from the PR's OWN labels, so relabelling
        the issue behind it left the PR ai-pr-open, pickable, and hitting the cap
        again on the very next poll: 10 polls, 10 "Circuit breaker" comments, 10
        spend_block events and 10 ALERT_CMD runs — ~4,300 a day at
        POLL_SECONDS=20. The escalation therefore targets whatever the DISPATCH
        is gated on, and the journal/alert pair is edge-triggered on top of that
        (`_spend_block`), so a mechanism that ever regresses degrades to one
        noisy line per poll instead of one alert per poll.

        **Daily (global, all repos)** — deliberately NOT a label change. It is a
        LEVEL-triggered condition, not an event: the money comes back at midnight
        UTC, and marking each blocked issue needs-human-review would leave a
        human to hand-unwind a dozen labels the next morning for a condition that
        healed itself. Instead the dispatch is skipped loudly, the reaper requeues
        the issue, and the next tick re-evaluates. The cost is churn — one
        no-op dispatch attempt per issue per poll until the day rolls over —
        which is cheap precisely because no agent is ever started.
        """
        path = self.cfg.spend_ledger_path
        if not path:
            return False
        cap = self.cfg.issue_spend_cap_usd
        if cap > 0 and issue is not None:
            spent = sum_issue(path, self.cfg.repo, issue)
            if spent >= cap:
                reason = (
                    f"Spend on this issue reached ${spent:.2f}, at or over the "
                    f"ISSUE_SPEND_CAP_USD of ${cap:.2f}."
                )
                if action.kind in self._PR_KEYED:
                    target, number = "pr", action.number
                    escalation = pr_escalation(
                        action.number, reason,
                        from_label=self._pr_escalation_label(action.kind),
                    )
                else:
                    target, number = "issue", issue
                    escalation = issue_escalation(issue, reason, Label.AI_WORKING)
                self._spend_block(
                    f"issue #{issue} has spent ${spent:.2f} of its ${cap:.2f} cap "
                    f"— escalating {target} #{number}",
                    block_key=f"issue:{self.cfg.repo}:{issue}",
                    scope="issue", issue=issue, spent=round(spent, 4), cap=cap,
                    target=target, number=number,
                )
                self.executor.execute_all(escalation.actions())
                return True
        cap = self.cfg.daily_spend_cap_usd
        if cap > 0:
            spent = sum_today(path)
            if spent >= cap:
                self._spend_block(
                    f"TODAY'S GLOBAL SPEND ${spent:.2f} is at or over the "
                    f"${cap:.2f} DAILY_SPEND_CAP_USD — all dispatch paused until "
                    "00:00 UTC (no labels changed; work resumes by itself)",
                    block_key=f"daily:{datetime.now(timezone.utc).date().isoformat()}",
                    scope="daily", spent=round(spent, 4), cap=cap,
                )
                return True
        return False

    def _spend_block(self, message: str, *, block_key: str, **fields) -> None:
        """Log every time; journal + alert ONCE per blocked key (review P1R-2).

        A cap is a level, not an event: it stays tripped on every subsequent
        poll, so an un-deduped emit is a pager that fires until it is ignored.
        The identity travels IN the event (`block_key`), so the dedup survives a
        process restart by reading the journal this daemon already owns — the
        flood shape is a wedged daemon under `Restart=always`, where an
        in-process set alone would be empty again every few seconds. The set is
        kept as a cache so the steady state costs no file read.

        The LOG line is deliberately not deduped: it is local, free, and "why is
        this PR not moving?" must be answerable from the log of any single poll.
        """
        self.cfg.log(f"SPEND CAP: {message}")
        if block_key in self._spend_blocked:
            return
        self._spend_blocked.add(block_key)
        if self.journal is not None:
            if block_key in spend_blocked_keys(read_events_tail(self.journal.path)):
                return
            self.journal.emit("spend_block", block_key=block_key, **fields)
        alert(self.cfg.alert_cmd, event_line("spend_block", block_key=block_key, **fields))

    def _warn_if_the_caps_are_blind(self) -> None:
        """One loud line, once per process, when a spend cap is armed but every
        settled run so far cost $0.00 (review P1R-5) — i.e. the ledger both caps
        scan is empty and neither can ever fire, for any loop shape.

        Evidence-based rather than config-based, because the cause is usually
        the PLAN (a subscription-plan gateway reports no per-run cost) and no
        amount of harness config reveals that. Checked on the dispatch path
        rather than at startup for the same reason: at startup there is no
        evidence yet. The check stops for good as soon as there are enough
        settled runs to judge, so it is not a per-dispatch file read.
        """
        if self._cost_blindspot_checked or self.journal is None:
            return
        if not self.cfg.spend_ledger_path:
            return  # caps already inert by construction — DispatchConfig says so
        if self.cfg.issue_spend_cap_usd <= 0 and self.cfg.daily_spend_cap_usd <= 0:
            self._cost_blindspot_checked = True
            return
        runs = [
            e for e in read_events_tail(self.journal.path)
            if e.get("event") == "agent_run"
        ]
        if len(runs) < COST_BLINDSPOT_MIN_RUNS:
            return  # not enough evidence yet — ask again on the next dispatch
        self._cost_blindspot_checked = True
        warning = cost_blindspot_warning(
            runs,
            issue_spend_cap_usd=self.cfg.issue_spend_cap_usd,
            daily_spend_cap_usd=self.cfg.daily_spend_cap_usd,
        )
        if warning is None:
            return
        self.cfg.log(warning)
        self.journal.emit(
            "cost_blindspot", runs=len(runs),
            issue_cap=self.cfg.issue_spend_cap_usd,
            daily_cap=self.cfg.daily_spend_cap_usd,
        )

    async def dispatch(self, action: RunAgent) -> None:
        handler = {
            AgentKind.RESOLVE: self.fix_workflow.resolve,
            AgentKind.RESUME: self.fix_workflow.resume,
            AgentKind.ADDRESS_REVIEW: self.fix_workflow.address_review,
            AgentKind.FIX_CI: self.fix_workflow.fix_ci,
            AgentKind.REBASE: self.fix_workflow.rebase,
            AgentKind.REVIEW: self.review_workflow.review,
            AgentKind.RE_REVIEW: self.review_workflow.re_review,
            AgentKind.TRIAGE: self.steward_workflow.triage,
            AgentKind.JIRA_SYNC: self.jira_workflow.sync,
        }.get(action.kind)
        if handler is None:
            return
        issue, resolved = self._spend_target(action)
        if self._blocked_on_spend(action, issue):
            return
        self._warn_if_the_caps_are_blind()
        # Tag every leg this dispatch runs (a multi-agent review runs six) with the
        # same context, so the ledger attributes each one to the work that caused it.
        set_context = getattr(self.runner, "set_context", None)
        if set_context is not None:
            set_context(self.cfg.role, action.kind.name, action.number, issue)
        try:
            await handler(action.number)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            # Review P1R-3. These three are the OPERATOR stopping the daemon, not
            # infrastructure failing: `issue-agent start` runs in the foreground
            # and Ctrl-C is the documented way to stop it. Counting them meant
            # five ordinary Ctrl-Cs escalated a healthy issue to
            # needs-human-review, while a SIGTERM (systemd's stop signal, which
            # raises no Python exception at all) counted nothing — the asymmetry
            # was exactly backwards.
            #
            # CancelledError is excluded WITH them, deliberately: under
            # `asyncio.run` — how `roles.py` drives every dispatch — a Ctrl-C
            # delivered while the loop is awaiting arrives here as a
            # CancelledError, because `asyncio.run` cancels the task in its
            # cleanup and only then re-raises KeyboardInterrupt to the caller.
            # Excluding only KeyboardInterrupt would leave the repro open on its
            # MORE likely path. Nothing inside the harness cancels a dispatch:
            # both runners convert their own wall/idle timeouts into returned
            # results, and the review fan-out gathers with
            # `return_exceptions=True`, so a CancelledError here means shutdown.
            # Re-raised untouched — a swallowed KeyboardInterrupt is a daemon
            # that ignores Ctrl-C, and a swallowed CancelledError is a task that
            # refuses to die.
            raise
        except BaseException:
            # Review C3. A session that dies BEFORE `runner.run` returns RAISES
            # (agent_runner.py, deliberately), so the whole workflow tail —
            # save_session, the retry-counter bump, the follow-up actions — is
            # skipped. The death was therefore unbounded, unbilled and invisible:
            # `Daemon.tick` swallowed, the reaper put the issue back to ai-ready,
            # and the next poll paid for a full session again, forever. Wave A
            # made it VISIBLE (agent_run_start + a settled transport_death
            # journal line); this counter BOUNDS it (decide_fix escalates at
            # cfg.max_infra_deaths). Bump then re-raise UNCHANGED — the crash
            # isolation the raise buys is the contract, not a bug to fix here.
            #
            # `_death_key` (not a bare `-action.number` fallback) picks the key:
            # a failed pre-handler lookup must not silently relocate the counter
            # into a key space the decide side does not read — review P1R-1.
            key = self._death_key(action, issue, resolved)
            try:
                self.store.bump_infra_deaths(key)
            except Exception as exc:  # noqa: BLE001
                # A counter we could not persist must not replace the death we
                # are trying to report (the traceback is the diagnosis).
                self.cfg.log(f"infra-death counter not persisted for #{key}: {exc}")
            raise
