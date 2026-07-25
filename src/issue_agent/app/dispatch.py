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

from dataclasses import dataclass

from collections.abc import Callable

from ..adapters.journal import alert, event_line, sum_issue, sum_today
from ..domain import review as review_dom, triage as triage_dom
from ..domain.actions import AgentKind, RunAgent
from ..domain.agent_task import AgentRuntime
from ..domain.labels import Label
from ..domain.lifecycle import issue_escalation
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
        self.fix_workflow = FixWorkflow(
            cfg.fix_workflow_config(), gateway, store, worktrees, runner, executor, prepare_cwd
        )
        self.review_workflow = ReviewWorkflow(
            cfg.review_workflow_config(), gateway, store, runner, executor,
            self._run_logged,
        )
        self.steward_workflow = StewardWorkflow(
            cfg.steward_workflow_config(), gateway, store, runner, executor
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

    def _issue_number(self, action: RunAgent) -> int | None:
        """The issue this dispatch spends against, or None if unknowable (a PR
        with no closing ref and an unparseable branch — a Jira-keyed branch, say).
        Unknown is tolerated: it costs per-issue attribution, never a dispatch."""
        if action.kind not in self._PR_KEYED:
            return action.number
        try:
            return self.gw.issue_for_pr(action.number)
        except Exception as exc:  # noqa: BLE001 — a lookup failure ≠ a blocked run
            self.cfg.log(f"spend: could not resolve issue for PR #{action.number}: {exc}")
            return None

    def _blocked_on_spend(self, action: RunAgent, issue: int | None) -> bool:
        """Two circuit breakers, checked before a single token is bought.

        **Per-issue (lifetime)** — a one-shot escalation. An issue that has
        already cost the cap is not going to converge on the next retry, so the
        issue moves ai-working → needs-human-review, which structurally removes it
        from the pickable set. Uses the domain's `issue_escalation` so the
        comment+relabel pair matches every other breaker in the harness.

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
                self._spend_block(
                    f"issue #{issue} has spent ${spent:.2f} of its ${cap:.2f} cap",
                    scope="issue", issue=issue, spent=round(spent, 4), cap=cap,
                )
                reason = (
                    f"Spend on this issue reached ${spent:.2f}, at or over the "
                    f"ISSUE_SPEND_CAP_USD of ${cap:.2f}."
                )
                self.executor.execute_all(
                    issue_escalation(issue, reason, Label.AI_WORKING).actions()
                )
                return True
        cap = self.cfg.daily_spend_cap_usd
        if cap > 0:
            spent = sum_today(path)
            if spent >= cap:
                self._spend_block(
                    f"TODAY'S GLOBAL SPEND ${spent:.2f} is at or over the "
                    f"${cap:.2f} DAILY_SPEND_CAP_USD — all dispatch paused until "
                    "00:00 UTC (no labels changed; work resumes by itself)",
                    scope="daily", spent=round(spent, 4), cap=cap,
                )
                return True
        return False

    def _spend_block(self, message: str, **fields) -> None:
        self.cfg.log(f"SPEND CAP: {message}")
        if self.journal is not None:
            self.journal.emit("spend_block", **fields)
        alert(self.cfg.alert_cmd, event_line("spend_block", **fields))

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
        issue = self._issue_number(action)
        if self._blocked_on_spend(action, issue):
            return
        # Tag every leg this dispatch runs (a multi-agent review runs six) with the
        # same context, so the ledger attributes each one to the work that caused it.
        set_context = getattr(self.runner, "set_context", None)
        if set_context is not None:
            set_context(self.cfg.role, action.kind.name, action.number, issue)
        try:
            await handler(action.number)
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
            # `issue` is already resolved above: for the non-PR-keyed kinds it IS
            # action.number, and the -action.number fallback is exactly
            # domain.fix.breaker_key's negated-PR convention, so the decide side
            # reads the counter under the key the write side used.
            key = issue if issue is not None else -action.number
            try:
                self.store.bump_infra_deaths(key)
            except Exception as exc:  # noqa: BLE001
                # A counter we could not persist must not replace the death we
                # are trying to report (the traceback is the diagnosis).
                self.cfg.log(f"infra-death counter not persisted for #{key}: {exc}")
            raise
