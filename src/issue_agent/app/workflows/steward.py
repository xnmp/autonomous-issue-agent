"""Steward triage workflow dispatch."""

from __future__ import annotations

from dataclasses import dataclass

from ...domain import triage as triage_dom
from ...domain.agent_task import AgentRuntime
from ...domain.triage import TriageConfig
from ...domain.triage import TriageVerdict
from ..planners import tasks as task_builders
from ..planners.tasks import TriageTaskConfig


@dataclass(frozen=True)
class StewardWorkflowConfig:
    triage: TriageTaskConfig | None
    triage_cfg: TriageConfig | None
    log: object


@dataclass
class StewardWorkflow:
    cfg: StewardWorkflowConfig
    gateway: object
    store: object
    runner: AgentRuntime
    executor: object

    async def triage(self, issue: int) -> None:
        self.cfg.log(f"triage #{issue}: classifying")
        iss = self.gateway.get_issue(issue)
        thread = "\n\n---\n\n".join(f"@{c.author}: {c.body}" for c in iss.comments)
        task = task_builders.build_triage_task(issue, iss.title, iss.body, thread, self.cfg.triage)
        result = await self.runner.run(task)
        if result.is_error or not isinstance(result.structured, dict):
            self.cfg.log(f"triage #{issue}: {result.status or 'error'} — no verdict, will retry")
            return
        s = result.structured
        verdict = TriageVerdict(
            verdict=s.get("verdict", ""),
            plain_summary=s.get("plain_summary", ""),
            acceptance_criteria=tuple(s.get("acceptance_criteria", []) or ()),
            creator_questions=tuple(s.get("creator_questions", []) or ()),
            owner_questions=tuple(s.get("owner_questions", []) or ()),
            blocked_by_issues=tuple(
                int(n) for n in (s.get("blocked_by_issues", []) or []) if isinstance(n, int)
            ),
            rationale=s.get("rationale", ""),
        )
        promote = bool(self.cfg.triage_cfg and self.cfg.triage_cfg.promote)
        if verdict.is_blocked:
            disp = (
                f"blocked ({len(verdict.creator_questions)} creator + "
                f"{len(verdict.owner_questions)} owner question(s)"
                f"{'; needs owner' if verdict.needs_owner else ''})"
            )
        else:
            disp = (
                "ready → ai-ready" if (promote and not verdict.blocked_by_issues)
                else "ready (awaiting human promote)"
            )
        if verdict.blocked_by_issues:
            disp += f"; blocked-by {sorted(verdict.blocked_by_issues)}"
        self.cfg.log(f"triage #{issue}: {disp}")
        self.executor.execute_all(
            triage_dom.actions_for_verdict(issue, iss.author, verdict, self.cfg.triage_cfg)
        )
        self.store.set_triage_at(issue, self.gateway.get_issue(issue).updated_at)
