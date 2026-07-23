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

from ..domain import review as review_dom, triage as triage_dom
from ..domain.actions import AgentKind, RunAgent
from ..domain.agent_task import AgentRuntime
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
                 runner: AgentRuntime, executor, prepare_cwd=None):
        self.cfg = cfg
        self.gw = gateway
        self.store = store
        self.runner = runner
        self.fix_workflow = FixWorkflow(
            cfg.fix_workflow_config(), gateway, store, worktrees, runner, executor, prepare_cwd
        )
        self.review_workflow = ReviewWorkflow(
            cfg.review_workflow_config(), gateway, store, runner, self._run_logged
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
        if handler is not None:
            await handler(action.number)
