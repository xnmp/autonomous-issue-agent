"""Jira lifecycle sync workflow dispatch."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..planners import tasks as task_builders
from ..planners.tasks import JiraTaskConfig
from ...adapters.runtime_plan import AgentRuntime
from ...adapters.deployment_authority import (
    DeploymentAuthority,
    deployment_role,
)
from ...adapters.durable_effects import DurableEffectRunner, EffectOutcome
from ...domain.contracts import (
    CommandId,
    CommandKind,
    EffectStatus,
    LifecycleCommand,
)
from ...domain.jira import (
    jira_correlation_id,
    jira_phase,
    jira_sync_outcome,
    plan_jira_sync,
    valid_jira_ticket_key,
)
from ...domain.leg import Leg, LegTarget
from ...domain.ports import WorkflowGitHubReads, WorkflowState
from ...domain.trust import as_untrusted
from .base import run_leg


@dataclass(frozen=True)
class JiraSyncWorkflowConfig:
    jira: JiraTaskConfig | None
    log: Callable[[str], None]
    repository: str = ""
    command_identity: str = ""
    config_version: str = ""


@dataclass
class JiraSyncWorkflow:
    cfg: JiraSyncWorkflowConfig
    gateway: WorkflowGitHubReads
    store: WorkflowState
    runner: AgentRuntime
    commands: Any | None = None
    effects: DurableEffectRunner | None = field(
        init=False,
        default=None,
    )

    def __post_init__(self) -> None:
        if self.commands is not None and self.cfg.command_identity:
            self.effects = DurableEffectRunner(
                self.commands,
                DeploymentAuthority(
                    self.commands,
                    self.cfg.repository,
                    deployment_role(
                        self.cfg.repository,
                        self.cfg.command_identity,
                    ),
                    self.cfg.command_identity,
                ),
                executor_identity=self.cfg.command_identity,
            )

    async def sync(self, issue: int) -> None:
        if self.cfg.jira is None:
            return
        iss = self.gateway.get_issue(issue)
        key = self.store.get_jira_key(issue)
        plan = plan_jira_sync(iss.labels, key, self.store.get_jira_phase(issue))
        if not plan.should_run or plan.target is None:
            return
        if plan.existing_key and not valid_jira_ticket_key(
            plan.existing_key,
            self.cfg.jira.project_key,
        ):
            self.cfg.log(
                f"jira-sync #{issue}: refusing invalid stored ticket key"
            )
            return
        command = self._command(
            issue,
            plan.existing_key,
            plan.target.value,
            iss.updated_at,
        )
        pending = self._latest_or_enqueue(command)
        if pending is not None and pending.state == EffectStatus.SUCCEEDED.value:
            receipt = self.commands.effect_receipt(pending.command.command_id)
            if (
                receipt is None
                or not receipt.remote_object_id
                or not valid_jira_ticket_key(
                    receipt.remote_object_id,
                    self.cfg.jira.project_key,
                )
            ):
                raise RuntimeError(
                    "durable Jira success has no valid ticket-key receipt"
                )
            self.store.set_jira_key(issue, receipt.remote_object_id)
            self.store.set_jira_phase(
                issue,
                receipt.remote_revision or plan.target.value,
            )
            return
        if (
            pending is not None
            and pending.state == EffectStatus.PERMANENT_FAILURE.value
        ):
            if (
                pending.command.expected_revision == command.expected_revision
                and dict(pending.command.payload) == dict(command.payload)
            ):
                self.cfg.log(
                    f"jira-sync #{issue}: durable command failed permanently"
                )
                return
            pending = self._effect_runner().enqueue_occurrence(command)

        reconcile_first = (
            pending is not None
            and pending.state in {
                "executing",
                EffectStatus.UNKNOWN_OUTCOME.value,
            }
        )
        if pending is not None and not reconcile_first:
            current = self.gateway.get_issue(issue)
            current_target = jira_phase(current.labels)
            if (
                current_target is not plan.target
                or (
                    pending.command.expected_revision
                    and current.updated_at
                    != pending.command.expected_revision
                )
            ):
                self._effect_runner().execute(
                    pending.command,
                    lambda: EffectOutcome(
                        None,
                        EffectStatus.PERMANENT_FAILURE,
                        error="GitHub issue revision changed before Jira effect",
                    ),
                )
                self.cfg.log(
                    f"jira-sync #{issue}: source revision changed before "
                    "execution; will re-plan"
                )
                return

        async def run_and_classify() -> tuple[Any, Any]:
            result = await run_leg(
                self.runner,
                self.cfg.log,
                Leg(
                    "JIRA_SYNC",
                    LegTarget.ISSUE,
                    issue,
                    True,
                    ("global", "repository", "issue"),
                ),
                task_builders.build_jira_sync_task(
                    issue,
                    as_untrusted(iss.title),
                    as_untrusted(iss.body),
                    plan.target,
                    plan.existing_key,
                    self.cfg.jira,
                    reconcile_first=reconcile_first,
                ),
            )
            outcome = jira_sync_outcome(
                is_error=result.is_error,
                status=result.status,
                returned_ticket_key=result.field("ticket_key"),
                existing_key=plan.existing_key,
                target=plan.target,
                project_key=self.cfg.jira.project_key,
            )
            return result, outcome

        started_at = datetime.now(timezone.utc)
        receipted = pending is not None and not reconcile_first
        if receipted:
            assert pending is not None

            async def execute_new() -> EffectOutcome[tuple[Any, Any]]:
                result, outcome = await run_and_classify()
                return EffectOutcome(
                    (result, outcome),
                    (
                        EffectStatus.SUCCEEDED
                        if outcome.should_persist
                        else EffectStatus.UNKNOWN_OUTCOME
                    ),
                    remote_object_id=(
                        outcome.ticket_key if outcome.should_persist else None
                    ),
                    remote_revision=(
                        outcome.phase if outcome.should_persist else None
                    ),
                    error=(
                        None
                        if outcome.should_persist
                        else outcome.reason or "Jira outcome is ambiguous"
                    ),
                )

            result, outcome = await self._effect_runner().execute_async(
                pending.command,
                execute_new,
            )
        else:
            try:
                result, outcome = await run_and_classify()
            except BaseException:
                self._record_unknown(
                    pending,
                    started_at,
                    "agent leg interrupted",
                )
                raise
        if not outcome.should_persist:
            if not receipted:
                self._record_unknown(
                    pending,
                    started_at,
                    outcome.reason or "Jira outcome is ambiguous",
                )
            if outcome.reason == "missing_ticket_key":
                self.cfg.log(f"jira-sync #{issue}: {result.status} but no ticket_key returned — will retry")
                return
            if outcome.reason == "invalid_ticket_key":
                self.cfg.log(
                    f"jira-sync #{issue}: refusing ticket key outside project "
                    f"{self.cfg.jira.project_key}"
                )
                return
            self.cfg.log(f"jira-sync #{issue}: failed ({result.summary or 'see logs'}) — will retry")
            return
        if pending is not None and reconcile_first:
            self._effect_runner().resolve_unknown(
                pending.command,
                EffectOutcome(
                    None,
                    EffectStatus.SUCCEEDED,
                    remote_object_id=outcome.ticket_key,
                    remote_revision=outcome.phase,
                ),
            )
        self.store.set_jira_key(issue, outcome.ticket_key)
        self.store.set_jira_phase(issue, outcome.phase)
        self.cfg.log(f"jira-sync #{issue}: {result.status} {outcome.ticket_key} → {outcome.phase}")

    def _command(
        self,
        issue: int,
        existing_key: str,
        target_phase: str,
        source_revision: str,
    ) -> LifecycleCommand | None:
        if self.commands is None:
            return None
        if not self.cfg.repository or not self.cfg.command_identity:
            raise RuntimeError(
                "Jira lifecycle commands require repository and executor identity"
            )
        assert self.cfg.jira is not None
        correlation = jira_correlation_id(
            self.cfg.repository,
            self.cfg.jira.project_key,
            issue,
        )
        if existing_key:
            logical_key = (
                f"jira:transition:{correlation}:{existing_key}:"
                f"{target_phase}:{source_revision}"
            )
            kind = CommandKind.TRANSITION_JIRA_ISSUE
        else:
            logical_key = f"jira:create:{correlation}"
            kind = CommandKind.CREATE_JIRA_ISSUE
        return LifecycleCommand(
            CommandId(hashlib.sha256(logical_key.encode()).hexdigest()),
            logical_key,
            kind,
            f"issue:{issue}",
            source_revision or None,
            {
                "correlation": correlation,
                "issue": issue,
                "project_key": self.cfg.jira.project_key,
                "target_phase": target_phase,
                "ticket_key": existing_key,
            },
            self.cfg.config_version,
            self.cfg.repository,
            self.cfg.command_identity,
        )

    def _latest_or_enqueue(self, command: LifecycleCommand | None):
        if command is None:
            return None
        pending = self.commands.latest_command(command.idempotency_key)
        return pending or self._effect_runner().enqueue(command)

    def _effect_runner(self) -> DurableEffectRunner:
        if self.effects is None:
            raise RuntimeError(
                "Jira lifecycle effects require durable command storage"
            )
        return self.effects

    def _record_unknown(
        self,
        pending,
        started_at: datetime,
        error: str,
    ) -> None:
        if pending is None or pending.state in {
            EffectStatus.UNKNOWN_OUTCOME.value,
            EffectStatus.PERMANENT_FAILURE.value,
        }:
            return
        self._effect_runner().record_existing(
            pending.command,
            EffectOutcome(
                None,
                EffectStatus.UNKNOWN_OUTCOME,
                error=error[:500],
            ),
            started=started_at,
        )
