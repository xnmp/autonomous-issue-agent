"""AgentDispatcher — turns a RunAgent(kind, n) into: gather inputs → build the
RuntimePlan → run it → apply pure follow-up Actions + persist session/counters.

This is the impure glue the executor's `run_agent` calls. It owns the GitHub
reads the follow-up needs (notably the ground-truth "did a PR actually open?"
check) and the worktree/session plumbing, but every DECISION is delegated to a
pure domain function (resolve_actions / resume_actions / address_review_actions /
fix_ci_actions / review_submission). So the
dispatcher stays thin and the logic stays tested in tests/domain.

Async because AgentRunner.run is async (the SDK query is a coroutine).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from collections.abc import Callable
from functools import partial

from ..domain import review as review_dom
from ..domain.actions import AgentKind, RunAgent
from ..domain.events import Correlation, TelemetryEvent, TelemetryWarning
from ..adapters.runtime_plan import (
    AgentRuntime,
    ContextualRuntime,
)
from ..adapters.workflow_rows import WorkflowAuthorityError
from ..domain.agent_task import (
    RuntimeName,
)
from ..domain.leg import Leg, LegTarget
from ..domain.ports import ActionExecutor, EventSink, WorkflowGitHubReads, WorkflowState
from .planners.tasks import ReviewTaskConfig
from .workflows.review import ReviewAuthority, ReviewWorkflow, ReviewWorkflowConfig
from .workflows.base import run_leg


def _ignore_log(_message: str) -> None:
    pass


@dataclass
class DispatchConfig:
    base_branch: str = "dev"
    review: ReviewTaskConfig | None = None
    fork_threshold: int = 5
    max_review_rejections: int = 4
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
    reviewer_runtime: RuntimeName = RuntimeName.CLAUDE
    review_execution_cwd: str = ""
    review_prepare_cwd: Callable[[str], None] | None = None
    review_checkout: Callable[[int, str], str] | None = None
    review_checkout_cleanup: Callable[[int], None] | None = None
    # PR label removed when a review escalates to needs-human-review. Matches the
    # reviewer's trigger label (default ai-pr-open; ai-review for an external repo).
    review_trigger_label: str = "ai-pr-open"
    # Diagnostic logger (stderr in production).
    log: Callable[[str], None] = _ignore_log
    # Paid accounting is enforced by the runtime wrapper over SQLite. These two
    # fields only provide durable run attribution.
    role: str = ""
    repo: str = ""
    command_identity: str = ""
    deployment_owner_id: str = ""
    config_version: str = ""
    operator_guidance: Callable[[int], str] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.reviewer_runtime, RuntimeName):
            self.reviewer_runtime = RuntimeName(self.reviewer_runtime)

    def review_workflow_config(self) -> ReviewWorkflowConfig:
        return ReviewWorkflowConfig(
            review=self.review,
            review_mode=self.review_mode,
            max_review_rejections=self.max_review_rejections,
            review_bot=self.review_bot,
            reviewer_runtime=self.reviewer_runtime,
            execution_cwd=self.review_execution_cwd,
            prepare_cwd=self.review_prepare_cwd,
            review_checkout=self.review_checkout,
            review_checkout_cleanup=self.review_checkout_cleanup,
            review_trigger_label=self.review_trigger_label,
            log=self.log,
        )


class AgentDispatcher:
    """Runs one RunAgent action end-to-end. `executor` applies the follow-up
    Actions (so all writes funnel through one place); `runner` is the SDK
    AgentRunner; `gateway`/`store`/`worktrees` are the adapters. `run` is
    `runner.run` (async). All collaborators are injected → fully fakeable."""

    def __init__(
        self,
        cfg: DispatchConfig,
        gateway: WorkflowGitHubReads,
        store: WorkflowState,
        runner: AgentRuntime,
        executor: ActionExecutor,
        journal: EventSink | None = None,
        dispatch_claim_target: Callable[[RunAgent], int | None] | None = None,
        review_authority: ReviewAuthority | None = None,
    ):
        self.cfg = cfg
        self.gw = gateway
        self.store = store
        self.runner = runner
        self.executor = executor
        self.journal = journal
        self.dispatch_claim_target = dispatch_claim_target
        # PR → issue answers this process has actually received, so a dying
        # gateway does not silently drop the per-issue spend scope.
        self._issue_memo: dict[int, int] = {}
        self.review_workflow = ReviewWorkflow(
            replace(cfg.review_workflow_config(), journal=journal),
            gateway, store, runner,
            self._run_logged,
            authority=review_authority,
        )

    async def _run_logged(self, label: str, issue: int, task):
        """Run one agent leg, bracketing it with start/done breadcrumbs so a long
        in-session run isn't a silent gap in the log."""
        return await run_leg(
            self.runner,
            self.cfg.log,
            Leg(
                label.upper().replace("-", "_").replace(":", "_"),
                LegTarget.PR,
                issue,
                True,
                ("global", "repository", "issue"),
            ),
            task,
        )

    # Kinds keyed by PR number rather than issue number. The per-issue spend cap
    # needs the ISSUE behind the work, so these are resolved through the gateway.
    _PR_KEYED = frozenset({AgentKind.REVIEW, AgentKind.RE_REVIEW})

    def _lookup_issue(self, pr: int) -> int | None:
        """The PR's issue from the gateway; on a failed lookup, the last answer
        this process got for the same PR rather than a silent issueless spend."""
        try:
            issue = self.gw.issue_for_pr(pr)
        except Exception as exc:  # noqa: BLE001 — a lookup failure ≠ a blocked run
            self.cfg.log(f"spend: could not resolve issue for PR #{pr}: {exc}")
            return self._issue_memo.get(pr)
        if issue is not None:
            self._issue_memo[pr] = issue
        return issue

    def _spend_target(self, action: RunAgent) -> tuple[int | None, int | None]:
        """Issue scope and the reserved claim target.

        ADR 0008 makes the decision-side reservation authoritative for a
        production dispatch. Re-reading linkage here can disagree with the
        observed world after its global claim was acquired. Hand-built
        dispatchers without reservations retain the gateway fallback.
        """
        if self.dispatch_claim_target is not None:
            target = self.dispatch_claim_target(action)
            if target is not None:
                return (target if target > 0 else None), target
        if action.kind not in self._PR_KEYED:
            return action.number, None
        return self._lookup_issue(action.number), None

    async def dispatch(self, action: RunAgent) -> None:
        handler = {
            AgentKind.REVIEW: self.review_workflow.review,
            AgentKind.RE_REVIEW: self.review_workflow.re_review,
        }.get(action.kind)
        if handler is None:
            return
        handler = partial(handler, binding=action.workflow)
        issue, claim_target = self._spend_target(action)
        if action.kind in {
            AgentKind.REVIEW,
            AgentKind.RE_REVIEW,
        } and claim_target is not None:
            handler = partial(handler, claim_target=claim_target)
        # Tag every leg this dispatch runs (a multi-agent review runs six) with the
        # same context, so the ledger attributes each one to the work that caused it.
        if isinstance(self.runner, ContextualRuntime):
            self.runner.set_context(
                self.cfg.role,
                action.kind.name,
                action.number,
                issue,
                claim_target=claim_target,
                workflow=action.workflow,
                head=action.head,
            )
        try:
            await handler(action.number)
        except WorkflowAuthorityError as exc:
            # Authority holds are not provider deaths; they are logged and the
            # next tick re-derives the work.
            self.cfg.log(f"{action.kind.name} #{action.number} held: {exc}")
            if self.journal is not None:
                self.journal.emit(TelemetryEvent(
                    TelemetryWarning("workflow_authority_held", str(exc)),
                    Correlation(issue=issue),
                ))
