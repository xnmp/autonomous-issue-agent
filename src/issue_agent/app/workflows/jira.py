"""Jira lifecycle sync workflow dispatch."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..planners import tasks as task_builders
from ..planners.tasks import JiraTaskConfig
from ...adapters.runtime_plan import AgentRuntime
from ...domain.jira import jira_sync_outcome, plan_jira_sync
from ...domain.ports import WorkflowGitHubReads, WorkflowState
from .base import run_leg


@dataclass(frozen=True)
class JiraSyncWorkflowConfig:
    jira: JiraTaskConfig | None
    log: Callable[[str], None]


@dataclass
class JiraSyncWorkflow:
    cfg: JiraSyncWorkflowConfig
    gateway: WorkflowGitHubReads
    store: WorkflowState
    runner: AgentRuntime

    async def sync(self, issue: int) -> None:
        if self.cfg.jira is None:
            return
        iss = self.gateway.get_issue(issue)
        key = self.store.get_jira_key(issue)
        plan = plan_jira_sync(iss.labels, key, self.store.get_jira_phase(issue))
        if not plan.should_run or plan.target is None:
            return
        result = await run_leg(
            self.runner,
            self.cfg.log,
            "jira-sync",
            issue,
            task_builders.build_jira_sync_task(
                issue, iss.title, iss.body, plan.target, plan.existing_key, self.cfg.jira
            ),
        )
        outcome = jira_sync_outcome(
            is_error=result.is_error,
            status=result.status,
            returned_ticket_key=result.field("ticket_key"),
            existing_key=plan.existing_key,
            target=plan.target,
        )
        if not outcome.should_persist:
            if outcome.reason == "missing_ticket_key":
                self.cfg.log(f"jira-sync #{issue}: {result.status} but no ticket_key returned — will retry")
                return
            self.cfg.log(f"jira-sync #{issue}: failed ({result.summary or 'see logs'}) — will retry")
            return
        self.store.set_jira_key(issue, outcome.ticket_key)
        self.store.set_jira_phase(issue, outcome.phase)
        self.cfg.log(f"jira-sync #{issue}: {result.status} {outcome.ticket_key} → {outcome.phase}")
