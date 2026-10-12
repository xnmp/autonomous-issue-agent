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

from ..domain.model_routing import ModelRoute

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
from ..domain.ports import (
    ActionExecutor,
    FixWorktreePort,
    EventSink,
    WorkflowGitHubReads,
    WorkflowState,
)
from ..domain.workflow_ports import WorkflowControl
from .planners.tasks import FixTaskConfig, ReviewTaskConfig
from .workflows.fix import FixWorkflow, FixWorkflowConfig
from .workflows.review import ReviewAuthority, ReviewWorkflow, ReviewWorkflowConfig
from .workflows.base import run_leg


def _ignore_log(_message: str) -> None:
    pass


@dataclass
class DispatchConfig:
    base_branch: str = "dev"
    fix: FixTaskConfig | None = None
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
    fix_mcp_config: str = ""
    harness_root: str = ""
    # PR label removed when a review escalates to needs-human-review. Matches the
    # reviewer's trigger label (default ai-pr-open; ai-review for an external repo).
    review_trigger_label: str = "ai-pr-open"
    # Diagnostic logger (stderr in production).
    log: Callable[[str], None] = _ignore_log
    # Fixer login, threaded to FixWorkflow so its resume path filters comments
    # with the same rule the decide side uses (see FixWorkflowConfig.fix_bot).
    fix_bot: str = ""
    # Fixer runtime ("claude" | "codex"), threaded to FixWorkflow so difficulty
    # routing picks from the runtime's own model family (see MODEL_TIERS).
    fixer_runtime: RuntimeName = RuntimeName.CLAUDE

    # Kill switch for the evidence-driven tier/effort ladder (#16). See
    # FixWorkflowConfig.escalate_on_evidence.
    fix_model_routes: tuple[ModelRoute, ...] = ()
    available_fix_runtimes: frozenset[RuntimeName] = frozenset()
    escalate_on_evidence: bool = True
    # Paid accounting is enforced by the runtime wrapper over SQLite. These two
    # fields only provide durable run attribution.
    role: str = ""
    repo: str = ""
    command_identity: str = ""
    deployment_owner_id: str = ""
    config_version: str = ""
    max_ci_retries: int = 3
    max_review_retries: int = 3
    max_rebase_retries: int = 3
    operator_guidance: Callable[[int], str] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.reviewer_runtime, RuntimeName):
            self.reviewer_runtime = RuntimeName(self.reviewer_runtime)
        if not isinstance(self.fixer_runtime, RuntimeName):
            self.fixer_runtime = RuntimeName(self.fixer_runtime)

    def fix_workflow_config(self) -> FixWorkflowConfig:
        return FixWorkflowConfig(
            base_branch=self.base_branch,
            fix=self.fix,
            fork_threshold=self.fork_threshold,
            fix_mcp_config=self.fix_mcp_config,
            harness_root=self.harness_root,
            log=self.log,
            fix_bot=self.fix_bot,
            fixer_runtime=self.fixer_runtime,
            fix_model_routes=self.fix_model_routes,
            available_fix_runtimes=self.available_fix_runtimes,
            escalate_on_evidence=self.escalate_on_evidence,
            operator_guidance=self.operator_guidance,
            max_ci_retries=self.max_ci_retries,
            max_review_retries=self.max_review_retries,
            max_rebase_retries=self.max_rebase_retries,
        )

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


class _NoFixWorktrees:
    """Typed null object for roles that cannot dispatch fix work."""

    @staticmethod
    def _unavailable() -> RuntimeError:
        return RuntimeError("fix worktrees are unavailable for this role")

    def ensure(self, issue: int, branch: str) -> str:
        del issue, branch
        raise self._unavailable()

    def remove(self, issue: int) -> None:
        del issue
        raise self._unavailable()

    def branch_for_issue(self, issue: int, fallback: str, *, preferred: str = "") -> str | None:
        del issue, fallback
        raise self._unavailable()


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
        worktrees: FixWorktreePort | None,
        runner: AgentRuntime,
        executor: ActionExecutor,
        prepare_cwd: Callable[[str], None] | None = None,
        journal: EventSink | None = None,
        reserve_fix_paths: Callable[[int, frozenset[str]], bool] | None = None,
        dispatch_claim_target: Callable[[RunAgent], int | None] | None = None,
        workflow_control: WorkflowControl | None = None,
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
        fix_worktrees = worktrees if worktrees is not None else _NoFixWorktrees()
        self.fix_workflow = FixWorkflow(
            # The journal arrives as a constructor argument, not through
            # DispatchConfig, so it is folded in here.
            replace(cfg.fix_workflow_config(), journal=journal),
            gateway,
            store,
            fix_worktrees,
            runner,
            executor,
            prepare_cwd,
            reserve_fix_paths,
            workflow_control,
        )
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
    _PR_KEYED = frozenset({
        AgentKind.PR_RESUME, AgentKind.ADDRESS_REVIEW, AgentKind.FIX_CI, AgentKind.REBASE,
        AgentKind.REVIEW, AgentKind.RE_REVIEW,
    })

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
            AgentKind.RESOLVE: self.fix_workflow.resolve,
            AgentKind.RESUME: self.fix_workflow.resume,
            AgentKind.PR_RESUME: self.fix_workflow.resume_pr,
            AgentKind.ADDRESS_REVIEW: self.fix_workflow.address_review,
            AgentKind.FIX_CI: self.fix_workflow.fix_ci,
            AgentKind.REBASE: self.fix_workflow.rebase,
            AgentKind.REVIEW: self.review_workflow.review,
            AgentKind.RE_REVIEW: self.review_workflow.re_review,
        }.get(action.kind)
        if handler is None:
            return
        if action.kind in {AgentKind.RESOLVE, AgentKind.RESUME, AgentKind.REVIEW, AgentKind.RE_REVIEW}:
            handler = partial(handler, binding=action.workflow)
        issue, claim_target = self._spend_target(action)
        if action.kind in {
            AgentKind.PR_RESUME,
            AgentKind.ADDRESS_REVIEW,
            AgentKind.FIX_CI,
            AgentKind.REBASE,
        } and claim_target is not None:
            # ADR 0008: a fix PR dispatch whose reserved claim carries no issue
            # must not fall through to a live linkage lookup — that would run
            # under an identity that was never claimed. decide_fix currently
            # never dispatches issueless PR work, so this is a fence, not a path.
            if issue is None:
                self.cfg.log(
                    f"dispatch {action.kind.name} PR #{action.number}: deferred "
                    f"— claim target {claim_target} carries no issue; refusing "
                    "live-lookup identity (ADR 0008)"
                )
                if self.journal is not None:
                    try:
                        self.journal.emit(
                            TelemetryEvent(
                                TelemetryWarning(
                                    "dispatch_deferred",
                                    f"{action.kind.name} claim target "
                                    f"{claim_target} carries no issue",
                                    {"observed_target": claim_target},
                                ),
                                Correlation(pr=action.number),
                            )
                        )
                    except Exception as exc:  # noqa: BLE001 — telemetry must not block
                        self.cfg.log(
                            f"dispatch PR #{action.number}: deferral not "
                            f"journalled ({exc})"
                        )
                return
            handler = partial(handler, issue_hint=issue)
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
