"""Fix workflow continuation under ADR 0009 and ADR 0015.

This module owns the impure fix-side agent dispatch sequence: gather GitHub and
store state, build an agent task, run it, commit the captured resolve outcome,
then apply pure follow-up actions. It is extracted from AgentDispatcher so the
central dispatcher can become a thin role router.
"""

from __future__ import annotations

import contextlib
import dataclasses
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from ...domain import fix as fix_domain
from ...domain.events import (
    BudgetPathEntered,
    Correlation,
    InfraPathEntered,
    RoutingEscalated,
    TelemetryEvent,
    TelemetryWarning,
)
from ...domain.leg import Leg, LegTarget
from ...domain import outcome
from ..planners import tasks as task_builders
from ..planners.tasks import FixTaskConfig
from ...domain.actions import AgentKind, ClearIssueState, CloseIssue, PostComment, Relabel, RemoveWorktree, Target
from ...domain.agent_task import RuntimeName, run_input_fingerprint
from ...domain.workflow import AuthoredResolve, HeldProjection, ResolveResultKind, ResolveSession, ResolveStage, WorkflowConflict, WorkflowSnapshot
from ...domain.workflow import ObservedIssue, NoLinkedPr, LinkedPrPresent
from ...domain.resolve_return import ResolveCleanupCompleted, ResolveInputs, ReturnedResolve, UnavailableResolveReturn
from ...domain.resolve_split import (
    AdjudicationInput,
    ContractResetCompleted,
    ContractResetIntent,
    NeedsAdjudication,
    ReauthorInput,
    ResetIntent,
    SplitContext,
    SplitImplement,
    SplitReply,
    SplitSelection,
    UnavailableContractRevision,
    split_context,
)
from ...domain.workflow_identity import IssueRef, PullRequestRef, WorkflowBinding, WorkflowKey
from ...domain.workflow_policy import classify_resolve_attempt, decide_return
from ...adapters.workflow_rows import WorkflowAuthorityError
from ...adapters.runtime_plan import AgentRuntime, ContextualRuntime, RunTelemetryRuntime
from ...adapters.github import format_pr_discussion
from ...adapters.worktree import (
    read_captured_contract_revision,
    verify_contract_revision,
)
from ...domain.ports import (
    ActionExecutor,
    CiPromptReads,
    FixWorktreePort,
    EventSink,
    InfraBreakerPort,
    ReviewPromptReads,
    WorkflowGitHubReads,
    WorkflowState,
)
from ...domain.workflow_ports import WorkflowControl
from ...domain.ci import latest_other_review
from ...domain.ci_history import make_attempt
from ...domain.effort import (
    DEFAULT_EFFORT,
    DEFAULT_TIER,
    Effort,
    EscalationSignals,
    Stance,
    TIER_LADDER,
    escalate,
)
from ...domain.labels import Label
from ...domain import continuation_components as cc
from ...domain.lifecycle import AllowedEligibility, LifecycleOperation, issue_eligibility
from ...domain.refs import slugify
from ...domain.review import count_outstanding_change_requests
from ...domain.trust import as_untrusted, untrusted_block
from ...domain.world import (
    REVIEW_CHANGES_REQUESTED,
    PRView,
    normalize_predicted_paths,
    paths_overlap_open_prs,
)
from ..mcp import mcp_servers_for_checks
from .base import run_leg
from .completion import execute_issue_completion


def _discard_route_decision(
    _model: str | None, _effort: object | None
) -> None:
    return None


@dataclass(frozen=True)
class FixWorkflowConfig:
    base_branch: str
    fix: FixTaskConfig | None
    fork_threshold: int
    author_tests_split: bool
    max_contract_rounds: int
    fix_mcp_config: str
    harness_root: str
    log: Callable[[str], None]
    max_ci_retries: int = 3
    max_review_retries: int = 3
    max_rebase_retries: int = 3
    # The fixer's own login. The resume path must exclude the fixer's comments
    # with the SAME filter the decide side uses (domain.fix.last_human_reply),
    # or the resumed-at dedup mark records a different comment than the decide
    # loop compares against (V1/#13). Default "" keeps existing tests working.
    fix_bot: str = ""
    # Which fixer runtime the routed model must belong to ("claude" | "codex") —
    # a codex fixer routed to a Claude alias cannot resolve it (see MODEL_TIERS).
    fixer_runtime: str = "claude"
    # Escalate tier/effort when the harness has evidence the last attempt
    # under-delivered (#16). Off restores the pre-#16 behaviour exactly: the
    # router's verdict and the configured effort, whatever has happened before.
    escalate_on_evidence: bool = True
    # Where `route_escalation` events land (adapters.journal.Journal, or None in
    # tests / when the daemon runs without one). Escalations must be queryable:
    # the routing guardrail is "split the metrics by tier", which is unanswerable
    # if the tier a leg actually ran at was never recorded.
    journal: EventSink | None = None
    # Deployment-wide infra breaker (app.infra_breaker.InfraBreaker). Every
    # infra-class death is reported to it beside the per-issue ceiling charge,
    # and every returned result clears it. None = per-issue ceilings only,
    # which is the pre-breaker behaviour every hand-built config keeps.
    infra_breaker: InfraBreakerPort | None = None
    operator_guidance: Callable[[int], str] | None = None


@dataclass
class FixWorkflow:
    cfg: FixWorkflowConfig
    gateway: WorkflowGitHubReads
    store: WorkflowState
    worktrees: FixWorktreePort
    runner: AgentRuntime
    executor: ActionExecutor
    prepare_cwd: Callable[[str], None] | None = None
    # Atomic application-level reservation update for router-expanded paths.
    # None keeps standalone workflow tests and serial callers unchanged.
    reserve_paths: Callable[[int, frozenset[str]], bool] | None = None
    workflow_control: WorkflowControl | None = None

    def _resolve_state(self, issue: int, binding: WorkflowBinding | None, *, allow_held: bool = False) -> WorkflowSnapshot:
        control = self.workflow_control
        if control is None or not isinstance(binding, WorkflowBinding):
            raise WorkflowAuthorityError("resolve requires captured durable workflow authority")
        if binding.fence.key != WorkflowKey(control.writer.repository, IssueRef(issue)):
            raise WorkflowAuthorityError("resolve binding belongs to another target")
        state = control.load_workflow(binding.fence.key)
        if not isinstance(state, WorkflowSnapshot) or state.binding != binding:
            raise WorkflowAuthorityError("resolve authority is unavailable or stale")
        if not allow_held and HeldProjection(state).held:
            raise WorkflowAuthorityError("resolve workflow is held")
        return state

    @staticmethod
    def _guided(task, state: WorkflowSnapshot):
        if not state.episode.guidance:
            return task
        return dataclasses.replace(task, intent=dataclasses.replace(
            task.intent, prompt=task.intent.prompt + "\n\n" + untrusted_block("operator_guidance", as_untrusted(state.episode.guidance)),
        ))

    def _record_resolve_result(self, issue: int, result, stage: ResolveStage, task, binding: WorkflowBinding,
                               followup: outcome.ResolveFollowup, *, progressed: bool = False) -> bool:
        control = self.workflow_control
        assert control is not None
        classified = classify_resolve_attempt(result, followup)
        if classified is None:
            if outcome.infra_class_failure(result) or outcome.budget_exhausted(result.exit_reason):
                self._resolve_state(issue, binding, allow_held=True)
                return True
            raise WorkflowAuthorityError("resolve result lacks its accounting-owned attempt")
        if classified.attempt.binding != binding:
            raise WorkflowAuthorityError("resolve result disagrees with its captured dispatch binding")
        kind = classified.kind
        if outcome.budget_exhausted(result.exit_reason):
            kind = ResolveResultKind.BUDGET_REFUSAL
        elif outcome.infra_class_failure(result):
            kind = ResolveResultKind.INFRA_FAILURE
        elif progressed:
            kind = ResolveResultKind.STAGE_PROGRESSED
        session = None
        if result.session_id and stage is not ResolveStage.ROUTE and kind not in (ResolveResultKind.INFRA_FAILURE, ResolveResultKind.BUDGET_REFUSAL):
            runtime = task.runtime.value if task.runtime is not None else self._resolve_runtime_name()
            session = ResolveSession(classified.attempt, result.session_id, runtime, task.model or "<runtime-default>")
        recorded = control.record_attempt(dataclasses.replace(classified, kind=kind, stage=stage, session=session))
        if isinstance(recorded, WorkflowConflict):
            raise WorkflowAuthorityError(recorded.reason)
        return recorded.snapshot.binding == binding

    def _execute_resolve_actions(self, issue: int, actions, binding: WorkflowBinding, *, current=None) -> bool:
        state = self._resolve_state(issue, binding, allow_held=True)
        if HeldProjection(state).held:
            self.cfg.log(f"resolve #{issue}: completion deferred; durable workflow is held")
            return False
        bound = [dataclasses.replace(action, workflow=binding) if isinstance(action, (Relabel, PostComment, CloseIssue)) else action for action in actions]
        if current is None or current.state != "open":
            return self.executor.execute_all(bound)
        return execute_issue_completion(self.gateway, self.executor, current, bound, LifecycleOperation.COMPLETE_RESOLVE, self.cfg.log)

    def _returned_result(self, result, binding: WorkflowBinding) -> ReturnedResolve | None:
        if result.workflow_attempt is None:
            if outcome.infra_class_failure(result) or outcome.budget_exhausted(result.exit_reason):
                return None
            raise WorkflowAuthorityError("paid resolve return lacks accounting provenance")
        if result.workflow_attempt.binding != binding:
            raise WorkflowAuthorityError("paid return disagrees with dispatch binding")
        control = self.workflow_control
        assert control is not None
        returned = control.returned_result(result.workflow_attempt)
        if returned is None:
            raise WorkflowAuthorityError("paid return lacks atomic settlement evidence; recovery is required")
        return returned

    def _observe_return(self, returned: ReturnedResolve):
        key = returned.attempt.binding.fence.key
        assert isinstance(key.target, IssueRef)
        current = self.gateway.get_issue(key.target.number)
        observed = ObservedIssue(key, current, datetime.now(timezone.utc))
        pr = self.gateway.pr_for_issue(key.target.number)
        linkage = (
            NoLinkedPr(key, datetime.now(timezone.utc)) if pr is None else
            LinkedPrPresent(key, pr, self.gateway.get_pr(pr).head_ref_oid, datetime.now(timezone.utc))
        )
        return observed, linkage

    def _finish_return(self, returned: ReturnedResolve) -> bool:
        control = self.workflow_control
        assert control is not None
        key = returned.attempt.binding.fence.key
        assert isinstance(key.target, IssueRef)
        try:
            observed, linkage = self._observe_return(returned)
            classified, actions = decide_return(
                returned, observed, linkage, now=datetime.now(timezone.utc)
            )
            recover_contract = isinstance(
                returned.contract_revision, UnavailableContractRevision
            ) and (
                classified.kind is ResolveResultKind.STAGE_PROGRESSED
                and observed.issue.state == "open"
                and isinstance(linkage, NoLinkedPr)
            ) and (
                (
                    returned.stage is ResolveStage.AUTHOR
                    and outcome.author_followup(returned.result())
                    is outcome.AuthorFollowup.AUTHORED
                )
                or (
                    returned.stage is ResolveStage.ADJUDICATE
                    and not returned.is_error
                    and not outcome.infra_class_failure(returned.result())
                    and not outcome.budget_exhausted(returned.exit_reason)
                )
            )
            if recover_contract:
                assert returned.split_input is not None
                context = split_context(returned.split_input)
                with control.return_effect_authority(returned):
                    observed, linkage = self._observe_return(returned)
                    refreshed, refreshed_actions = decide_return(
                        returned,
                        observed,
                        linkage,
                        now=datetime.now(timezone.utc),
                    )
                    if any(
                        isinstance(action, (RemoveWorktree, ClearIssueState))
                        for action in refreshed_actions
                    ):
                        if not refreshed_actions or not all(
                            isinstance(action, (RemoveWorktree, ClearIssueState))
                            for action in refreshed_actions
                        ):
                            raise WorkflowAuthorityError(
                                "returned cleanup contains unsupported effects"
                            )
                        if not self.executor.execute_all(refreshed_actions):
                            raise WorkflowAuthorityError(
                                "returned issue cleanup remains incomplete"
                            )
                        assert isinstance(linkage, NoLinkedPr)
                        cleanup = ResolveCleanupCompleted(
                            returned.attempt, observed, linkage
                        )
                        committed = control.finalize_return(
                            returned,
                            observed,
                            linkage,
                            cleanup=cleanup,
                        )
                    elif (
                        refreshed.kind is ResolveResultKind.STAGE_PROGRESSED
                        and observed.issue.state == "open"
                        and isinstance(linkage, NoLinkedPr)
                    ):
                        cwd = self.worktrees.ensure(
                            key.target.number, context.branch
                        )
                        revision = read_captured_contract_revision(
                            cwd,
                            key.target.number,
                            str(returned.attempt.run_id),
                            context.branch,
                        )
                        verify_contract_revision(
                            cwd, key.target.number, revision
                        )
                        committed = control.finalize_return(
                            returned,
                            observed,
                            linkage,
                            contract_revision=revision,
                        )
                    else:
                        committed = control.finalize_return(
                            returned, observed, linkage
                        )
            elif any(isinstance(action, (RemoveWorktree, ClearIssueState)) for action in actions):
                # Re-arm/owner retirement cannot race deletion of this episode's
                # worktree. A crash leaves the receipt held for idempotent replay.
                with control.return_effect_authority(returned):
                    observed, linkage = self._observe_return(returned)
                    _, actions = decide_return(returned, observed, linkage, now=datetime.now(timezone.utc))
                    cleanup = None
                    if actions and all(isinstance(action, (RemoveWorktree, ClearIssueState)) for action in actions):
                        if not self.executor.execute_all(actions):
                            raise WorkflowAuthorityError("returned issue cleanup remains incomplete")
                        assert isinstance(linkage, NoLinkedPr)
                        cleanup = ResolveCleanupCompleted(returned.attempt, observed, linkage)
                    committed = control.finalize_return(returned, observed, linkage, cleanup=cleanup)
            else:
                committed = control.finalize_return(returned, observed, linkage)
            if isinstance(committed, WorkflowConflict):
                raise WorkflowAuthorityError(committed.reason)
            if not committed.idempotent and committed.snapshot.binding == returned.attempt.binding:
                pr = linkage.pr if isinstance(linkage, LinkedPrPresent) else None
                self._journal_budget_path(key.target.number, returned.stage.value.casefold(), returned.exit_reason, pr)
            return True
        except Exception as exc:
            # The paid run did return. Observation uncertainty is a workflow
            # hold, not a provider death or permission to buy a replacement.
            control.defer_return(returned, str(exc)[:16384] or type(exc).__name__)
            self.cfg.log(f"resolve #{key.target.number}: returned result held for reconciliation: {exc}")
            return False

    def reconcile_returns(self) -> int:
        control = self.workflow_control
        if control is None:
            return 0
        completed = control.reconcile_pr_repair_returns()
        for pending in control.pending_returns(limit=32):
            if isinstance(pending, UnavailableResolveReturn):
                self.cfg.log(f"resolve result {pending.record_key}: authority unavailable; next audit {pending.next_read_at.isoformat()}: {pending.reason}")
                continue
            try:
                completed += int(self._finish_return(pending.returned))
            except Exception as exc:
                # Corrupt/unwritable authority must not prevent a healthy peer
                # from recovering; its existing gate continues to bar admission.
                self.cfg.log(f"resolve result {pending.returned.attempt.run_id}: recovery unavailable: {exc}")
        return completed

    def _resolve_resume_cfg(self, issue: int, state: WorkflowSnapshot, iss):
        cfg = self.cfg.fix
        if cfg is None:
            raise WorkflowAuthorityError("resolve configuration is unavailable")
        session = state.episode.continuation.session
        if session is not None:
            if session.runtime != self._resolve_runtime_name():
                raise WorkflowAuthorityError("resolve session belongs to a different runtime")
            cfg = dataclasses.replace(cfg, model=None if session.model == "<runtime-default>" else session.model)
        return self._escalated(cfg, issue, "resume", iss=iss)

    def _resolve_runtime_name(self) -> str:
        runtime = self.cfg.fixer_runtime
        return runtime.value if isinstance(runtime, RuntimeName) else runtime


    def _ensure_cwd(self, issue: int, branch: str, state: WorkflowSnapshot | None = None) -> str:
        from ...domain.workflow import CompletedRearm

        if state is not None and isinstance(state.intent, CompletedRearm):
            if branch != state.intent.handoff.branch:
                raise WorkflowAuthorityError("selected branch differs from the prepared workspace handoff")
            # Admission verifies this exact checkout under the deployment fence;
            # constructing a task must never recreate a missing prepared tree.
            cwd = self.worktrees.episode_workspace_path(state.intent.workspace)
        else:
            cwd = self.worktrees.ensure(issue, branch)
        if self.prepare_cwd is not None:
            self.prepare_cwd(cwd)
        self.cfg.log(f"worktree #{issue}: ready on '{branch}' → {cwd}")
        return cwd

    def _resume_branch(self, issue: int, title: str, state: WorkflowSnapshot) -> str:
        fallback = f"fix/{issue}-{slugify(title) or 'issue'}"
        remembered = state.issue_recovery.state.remembered_branch if isinstance(state.issue_recovery, cc.ActiveIssueRecovery) else ""
        return self.worktrees.branch_for_issue(
            issue, fallback, preferred=remembered
        ) or remembered or fallback

    def _review_comments(self, pr: int) -> str:
        """Best-effort fetch of a prompt INPUT (review M13/D6).

        The two repair legs feed the agent a failed-CI log and the reviewer's
        inline comments; both are context, not control flow. `GitHubGateway`
        already fails soft to "", but the workflow must survive a gateway that
        lacks the method entirely (an injected fake, a legacy adapter) or that
        raises unexpectedly — M13's contract is that a missing log may never
        kill the dispatch: the leg still runs, with the weaker prompt the code
        shipped before the methods existed, and the agent rediscovers the
        failure itself. The alternative (an AttributeError out of fix_ci) turns
        a cosmetic gap into a dead repair leg."""
        if not isinstance(self.gateway, ReviewPromptReads):
            return ""
        try:
            return self.gateway.review_comments(pr) or ""
        except Exception as exc:  # noqa: BLE001 — see docstring
            self.cfg.log(f"prompt input 'review_comments' unavailable: {exc}")
            return ""

    def _failed_run_log(self, pr: PRView) -> str:
        if not isinstance(self.gateway, CiPromptReads):
            return ""
        try:
            run_ids = tuple(dict.fromkeys(
                c.run_id for c in pr.checks
                if c.bucket == "fail" and not c.advisory and c.run_id is not None
            ))
            return self.gateway.failed_run_log(pr.head_ref_oid, run_ids) or ""
        except Exception as exc:  # noqa: BLE001 — advisory prompt enrichment
            self.cfg.log(f"prompt input 'failed_run_log' unavailable: {exc}")
            return ""

    def _report_return_health(self, result) -> None:
        # Issue episode changes require the exact paid run receipt captured by
        # accounting; this retains the independent deployment probe behavior.
        if not outcome.infra_class_failure(result):
            self._report_healthy_leg()

    def _leg_kind(self, kind: str | None):
        """Journal the next run under its own `kind` (#40), when the wrapped
        runtime knows how. A bare/undecorated runner (an injected fake, the
        legacy adapters) has no `leg_kind` and simply keeps the dispatch's
        kind — a naming nicety must never be able to kill a leg."""
        if kind and isinstance(self.runner, RunTelemetryRuntime):
            return self.runner.leg_kind(kind)
        return contextlib.nullcontext()

    def _route_receipt(self):
        """Expose a ROUTE decision to a journalling runner when it supports it.

        The router result is only a tier. This workflow is the layer that turns
        it into the selected model and effort, so it is the sole honest source
        for the ROUTE event's decision fields. Bare runners used by callers and
        tests retain their existing one-method runtime contract.
        """
        if isinstance(self.runner, RunTelemetryRuntime):
            return self.runner.route_receipt()
        return contextlib.nullcontext(_discard_route_decision)

    async def _run_logged(self, label: str, issue: int, task, *, kind: str | None = None):
        if task.issue_reply is None and task.split_input is not None:
            context = split_context(task.split_input)
            if context.reply is not None:
                assert self.workflow_control is not None
                state = self._resolve_state(issue, self.runner.workflow_binding)
                selected = cc.select_issue_recovery(state)
                if not isinstance(selected, cc.SelectedIssueRecovery):
                    raise WorkflowAuthorityError("split reply continuation requires migration")
                if cc.reply_advances(context.reply.created_at, selected.component.state.consumed_reply_at):
                    task = dataclasses.replace(task, issue_reply=cc.IssueReplyInput(selected, context.reply.created_at, context.reply.body, context.branch))
        return await run_leg(
            self.runner,
            self.cfg.log,
            Leg(
                kind or label.upper().replace("-", "_"),
                LegTarget.ISSUE,
                issue,
                True,
                ("global", "repository", "issue"),
            ),
            task,
        )

    # ── escalation on evidence (#16) ────────────────────────────────────────
    @staticmethod
    def _try(fn, default):
        try:
            return fn()
        except Exception:  # noqa: BLE001 — see `_signals`
            return default

    def _signals(self, issue: int, iss=None, review_rounds: int = 0) -> EscalationSignals:
        """Evidence that the last attempt on this issue under-delivered, read
        from state the harness already keeps — no new sensors (#16).

        Every read is best-effort. These counters are an OPTIMISATION of which
        configuration to dispatch, never a precondition for dispatching: a
        store that cannot answer (missing file, injected fake without the
        method) must degrade to "no evidence" and let the leg run, exactly as
        it did before this existed. Failing a resolve because a retry counter
        was unreadable would trade a cheap mis-tiering for a dead pipeline.

        `review_rounds` is passed IN rather than read here because the only
        honest source is the PR's own review list, which a resolve dispatch
        does not have. The store's `review-retries` counter is deliberately NOT
        used: it counts attempts at the CURRENT review and re-keys to 0 on every
        new one (`address_review`'s `base = n if at == review_at else 0`), so
        "rounds on this PR are climbing" is exactly the question it cannot
        answer — it reads 1 forever no matter how many times the reviewer comes
        back.
        """
        resolve_retries = 0
        if self.workflow_control is not None:
            try:
                state = self.workflow_control.load_workflow(WorkflowKey(self.workflow_control.writer.repository, IssueRef(issue)))
                if isinstance(state, WorkflowSnapshot):
                    resolve_retries = state.episode.failures
                else:
                    self.cfg.log(f"issue #{issue}: no available resolve escalation evidence")
            except Exception as exc:  # noqa: BLE001 — advisory only; selected admission validates authority separately
                self.cfg.log(f"issue #{issue}: resolve escalation evidence unavailable ({exc})")
        ci_retries = 0
        if self.workflow_control is not None:
            try:
                recovery = self.workflow_control.read_issue_recovery(WorkflowKey(self.workflow_control.writer.repository, IssueRef(issue)))
                if isinstance(recovery, cc.SelectedIssueRecovery):
                    ci_retries = recovery.component.state.ci_failures
            except (ValueError, sqlite3.Error) as exc:
                self.cfg.log(f"issue #{issue}: CI escalation evidence unavailable ({exc})")
        return EscalationSignals(
            recurrence=bool(
                self._try(lambda: iss is not None and iss.has(Label.RECURRENCE), False)
            ),
            resolve_retries=resolve_retries,
            ci_retries=ci_retries,
            review_rounds=max(0, int(review_rounds or 0)),
        )

    def _issue_for_signals(self, issue: int):
        """The IssueView a repair leg needs to see a `recurrence` label, or None.

        A repair leg holds a PR, not an issue, so without this the recurrence
        signal would be visible to the resolve leg and invisible to every leg
        that repairs its output — the asymmetry being worst precisely on an
        issue the harness already failed once. One gateway read against a paid
        session is a rounding error; a read that fails costs the signal, not the
        dispatch — logged, so "why didn't it escalate" stays answerable."""
        try:
            return self.gateway.get_issue(issue)
        except Exception as exc:  # noqa: BLE001 — see docstring
            self.cfg.log(
                f"agent #{issue}: recurrence signal unavailable ({exc}); "
                "escalating on the remaining evidence only"
            )
            return None

    def _journal_escalation(self, issue: int, leg: str, before: Stance, after: Stance,
                            signals: EscalationSignals, model: str | None,
                            tiered: bool, ceiling: bool) -> None:
        """Record the rung this leg climbed, and why. ONE event per escalated
        leg — `leg` distinguishes them, so "how many legs ran escalated" is a
        group-by, not an inference.

        The event may only report what actually reached the dispatch. A repair
        leg has NO tier: it keeps the resumed session's model, even when its
        evidence would qualify a fresh resolve for the capability ceiling. Its
        tier fields are therefore null rather than the DEFAULT_TIER placeholder
        `_escalated` needs internally. `ceiling` is true only when a fresh
        resolve actually selected the capability-ceiling model, separating
        Fable/Astra from normal `complex` routing in the load-bearing tier split.
        `model` is the model that ran — None when the leg named none and the
        runtime picked its own — so the split is derived from dispatch facts.

        Named `route_escalation`, NOT `escalation`: that event name already means
        "handed to a human" (`app.execute` emits it on a needs-human-review move,
        and `cli.py`'s escalation-rate metric counts it). Reusing it would fold a
        cost decision into the guardrail that is supposed to detect cheap models
        failing — the exact signal #16 must leave measurable.
        """
        journal = self.cfg.journal
        if journal is None:
            return
        try:
            journal.emit(
                TelemetryEvent(
                    RoutingEscalated(
                        leg,
                        before.tier if tiered else None,
                        after.tier if tiered else None,
                        before.effort.value,
                        after.effort.value,
                        {
                            "notches": signals.notches(),
                            "recurrence": signals.recurrence,
                            "resolve_retries": signals.resolve_retries,
                            "ci_retries": signals.ci_retries,
                            "review_rounds": signals.review_rounds,
                        },
                        model,
                        ceiling,
                    ),
                    Correlation(issue=issue),
                )
            )
        except Exception as exc:  # noqa: BLE001 — telemetry never fails a dispatch
            self.cfg.log(f"agent #{issue}: escalation not journalled ({exc})")

    def _escalated(
        self, cfg: FixTaskConfig, issue: int, leg: str, *, tier: str | None = None,
        iss=None, review_rounds: int = 0,
    ) -> FixTaskConfig:
        """Apply the escalation ladder to `cfg` for one leg (#16).

        `tier` is the router's verdict when this leg chose one. None means the
        leg inherited a model (a pinned FIX_MODEL or resumed session), so only
        the effort half of the ladder applies. The capability ceiling is a
        fresh-resolve routing decision: repair legs keep the model that built
        their transcript, including a Codex Terra session after Astra becomes the
        current ceiling, while still receiving HIGH effort for hard evidence.

        Returns `cfg` untouched when there is no evidence, so the common path
        emits no effort at all and each runtime keeps its own default.
        """
        signals = self._signals(issue, iss, review_rounds)
        notches = signals.notches() if self.cfg.escalate_on_evidence else 0
        # `cfg.effort or DEFAULT_EFFORT`: an unset FIX_EFFORT means "leave the
        # runtime's own default alone", and the ladder has to start SOMEWHERE.
        # It assumes that default is medium — true for codex (pinned by c995e55)
        # and for the Claude SDK today. If a runtime's default were lower, the
        # first notch would buy two rungs rather than one; it can never buy less,
        # so the assumption can only over-deliver, never silently under-think.
        base = Stance(
            tier=tier if tier is not None else DEFAULT_TIER,
            effort=cfg.effort or DEFAULT_EFFORT,
        )
        if notches <= 0:
            return cfg if tier is None else dataclasses.replace(
                cfg, model=task_builders.route_model(tier, self.cfg.fixer_runtime)
            )
        operator_pinned = bool(self.cfg.fix and self.cfg.fix.model)
        ceiling = leg == "resolve" and signals.requires_ceiling() and not operator_pinned
        stance = (
            Stance(TIER_LADDER[-1], Effort.HIGH)
            if signals.requires_ceiling() else escalate(base, notches)
        )
        # A configured model is an explicit operator choice. Only a fresh
        # resolve has a router tier and may replace its normal route at the
        # hard-evidence ceiling; resumed repair legs preserve their transcript.
        tiered = tier is not None
        model = (
            (
                task_builders.route_ceiling_model(self.cfg.fixer_runtime)
                if ceiling else task_builders.route_model(stance.tier, self.cfg.fixer_runtime)
            )
            if tiered or ceiling else cfg.model
        )
        # Same rule as the journal: name the tier move only when this leg chose
        # a tier, so the log cannot claim a climb the dispatch did not make.
        move = (
            f"tier {base.tier}→{stance.tier}, " if tier is not None
            else (
                f"model {cfg.model or '(runtime default)'}→{model}, "
                if ceiling else f"model {cfg.model or '(runtime default)'} kept, "
            )
        )
        self.cfg.log(
            f"agent #{issue}: {leg} — escalating {notches} notch(es) "
            f"(recurrence={signals.recurrence}, resolve_retries={signals.resolve_retries}, "
            f"ci_retries={signals.ci_retries}, "
            f"review_rounds={signals.review_rounds}): "
            f"{move}effort {base.effort.value}→{stance.effort.value}"
        )
        self._journal_escalation(
            issue, leg, base, stance, signals, model, tiered=tiered, ceiling=ceiling
        )
        notice = (
            "This is a difficult problem that has failed in the past. "
            "Take extra care: validate the root cause and the observable result "
            "before declaring it complete."
            if signals.failure_count() >= 2 else cfg.difficulty_notice
        )
        return dataclasses.replace(
            cfg, model=model, effort=stance.effort, difficulty_notice=notice
        )

    def _repair_fix_cfg(
        self, issue: int, leg: str, *, iss=None, review_rounds: int = 0, inherited_model: str = ""
    ) -> FixTaskConfig | None:
        """Effort-escalated config for a leg that resumes an existing session
        (address-review / fix-CI / rebase / resume). Its model stays attached to
        the resumed transcript; only fresh resolve routing can select the
        capability ceiling — see `_escalated`. `iss` is fetched when the caller
        does not already hold it, so the recurrence signal is not resolve-only."""
        cfg = self.cfg.fix
        if cfg is None:
            return cfg
        # #21: resume the session with the model that BUILT it. A repair leg
        # used to dispatch with no model, and the runner's fallback silently
        # swapped the routed reasoner for the configured default under the
        # live transcript. A pinned FIX_MODEL still wins; no stored marker
        # (pre-#21 sessions, unrouted resolves) keeps today's fallback.
        if not cfg.model:
            if inherited_model:
                cfg = dataclasses.replace(cfg, model=None if inherited_model == "<runtime-default>" else inherited_model)
        # Behind the kill switch: with escalation off nothing reads the labels,
        # so paying a `gh issue view` per repair leg would be pure waste.
        if iss is None and self.cfg.escalate_on_evidence:
            iss = self._issue_for_signals(issue)
        return self._escalated(cfg, issue, leg, iss=iss, review_rounds=review_rounds)

    def _prediction_overlaps_open_pr(self, issue: int, paths: frozenset[str]) -> bool:
        """Whether the router's new hint conflicts with an already-open PR.

        The decision tick selected this issue before the router ran. Re-reading
        the existing world here is therefore the one chance to turn that
        already-paid classification into a defer rather than starting a resolve
        that races a known conflict. A failed read preserves the former
        fail-open behavior.
        """
        if not paths:
            return False
        try:
            world = self.gateway.build_world()
        except Exception as exc:  # noqa: BLE001 — prediction is advisory
            self.cfg.log(
                f"agent #{issue}: route — overlap check unavailable ({exc}); "
                "dispatching normally"
            )
            return False
        return paths_overlap_open_prs(paths, world.prs)

    async def _routed_fix_cfg(
        self, issue: int, iss, cwd: str, binding: WorkflowBinding,
    ) -> tuple[FixTaskConfig, bool]:
        """Difficulty-routed, evidence-escalated FixTaskConfig for a RESOLVE
        dispatch (P4.1/#8, ladder #16).

        Runs the cheap classifier ONLY when no FIX_MODEL is pinned — an operator
        override wins, and a pinned model must not pay the router run. Failure
        must never cost the dispatch: a router run that errors or returns
        garbage degrades via route_model's totality (unknown tier → the
        runtime's `standard`), and a run that RAISES degrades all the way to
        the unrouted default — resolving on the wrong tier beats not resolving.
        A pinned model still gets the EFFORT half of the ladder: the operator
        chose the model, not how hard it thinks.

        Only the resolve family routes. The follow-up legs (resume/CI/review/
        rebase) resume an existing session where the work already has shape;
        re-classifying there would pay router runs to answer a question the
        resolve leg already answered.
        """
        cfg = self.cfg.fix
        if cfg is None:
            return cfg, False
        if cfg.model:
            return self._escalated(cfg, issue, "resolve", iss=iss), False
        state = self._resolve_state(issue, binding)
        task = self._guided(task_builders.build_router_task(
            as_untrusted(iss.title), as_untrusted(iss.body), runtime=self.cfg.fixer_runtime,
            max_budget_usd=cfg.router_budget_usd, cwd=cwd,
        ), state)
        with self._route_receipt() as record_decision:
            try:
                result = await self._run_logged(
                    "route", issue, task,
                    kind=AgentKind.ROUTE.name,
                )
            except Exception as exc:  # noqa: BLE001 — routing is advisory, never fatal
                self.cfg.log(f"agent #{issue}: route — failed ({exc}); dispatching unrouted")
                return self._escalated(cfg, issue, "resolve", iss=iss), False
            if not self._record_resolve_result(issue, result, ResolveStage.ROUTE, task, binding, outcome.ResolveFollowup.BLOCKED):
                raise WorkflowAuthorityError("workflow changed while routing")
            tier = result.field("tier")
            predicted_paths = normalize_predicted_paths(result.field("predicted_paths"))
            self.store.save_predicted_paths(issue, predicted_paths)
            routed = self._escalated(
                cfg, issue, "resolve", tier=tier or DEFAULT_TIER, iss=iss
            )
            record_decision(routed.model, routed.effort)
            self.cfg.log(
                f"agent #{issue}: route — tier={tier or 'unknown'} → model={routed.model}"
            )
            reservation_conflict = (
                self.reserve_paths is not None
                and not self.reserve_paths(issue, predicted_paths)
            )
            return routed, (
                reservation_conflict
                or self._prediction_overlaps_open_pr(issue, predicted_paths)
            )

    async def resolve(self, issue: int, *, binding: WorkflowBinding | None = None) -> None:
        state = self._resolve_state(issue, binding)
        assert binding is not None
        if self.cfg.author_tests_split:
            await self.resolve_split(issue, binding=binding)
            return
        iss = self.gateway.get_issue(issue)
        branch = self._resume_branch(issue, iss.title, state)
        cwd = self._ensure_cwd(issue, branch, state)
        fix_cfg, deferred_for_overlap = await self._routed_fix_cfg(issue, iss, cwd, binding)
        if deferred_for_overlap:
            self._execute_resolve_actions(issue, [
                Relabel(Target.ISSUE, issue, Label.AI_WORKING, Label.AI_READY)
            ], binding)
            return
        self._resolve_state(issue, binding)
        task = self._guided(task_builders.build_resolve_task(
            issue,
            as_untrusted(iss.title),
            as_untrusted(iss.body),
            cwd,
            fix_cfg,
        ), state)
        task = dataclasses.replace(task, resolve_inputs=ResolveInputs(iss.title, iss.body, iss.author))
        result = await self._run_logged("resolve", issue, task)
        returned = self._returned_result(result, binding)
        self._report_return_health(result)
        if returned is not None:
            if outcome.infra_class_failure(result):
                self._charge_infra_death(issue, "resolve", result)
            if self._finish_return(returned):
                self.executor.replay_pending()
            return
        pr = self.gateway.pr_for_issue(issue)
        self._journal_budget_path(issue, "resolve", result.exit_reason, pr)
        self._complete_resolve(issue, result, pr, observed=iss, task=task, stage=ResolveStage.RESOLVE, binding=binding)

    def _complete_resolve(self, issue: int, result, pr: int | None, *, observed=None, task, stage: ResolveStage, binding: WorkflowBinding) -> bool:
        current = self.gateway.get_issue(issue)
        changed = observed is not None and pr is None and result.status == "blocked" and not (
            outcome.budget_exhausted(result.exit_reason) or outcome.infra_class_failure(result)
        ) and (
            current.title, current.body, current.author
        ) != (observed.title, observed.body, observed.author)
        followup = outcome.ResolveFollowup.RETRY if changed else outcome.resolve_followup(result, pr is not None)
        if not self._record_resolve_result(issue, result, stage, task, binding, followup):
            return False
        if outcome.infra_class_failure(result):
            self._charge_infra_death(issue, stage.value.casefold(), result)
        actions = self._resolve_actions(issue, result, pr)
        if changed:
            self.cfg.log(f"resolve #{issue}: requirements changed during work; requeueing for the new facts")
            actions = [Relabel(Target.ISSUE, issue, Label.AI_WORKING, Label.AI_READY)]
        return self._execute_resolve_actions(issue, actions, binding, current=current)

    def _resolve_actions(self, issue: int, result, pr: int | None):
        """Retire an already-implemented issue closed without opening a PR."""
        closed_without_pr = False
        if pr is None and not result.is_error and result.status in ("pushed", "done"):
            refreshed = self._try(lambda: self.gateway.get_issue(issue), None)
            closed_without_pr = (
                refreshed is not None and refreshed.state != "open"
            )
        if not closed_without_pr and not isinstance(
            issue_eligibility(self.gateway.get_issue(issue), LifecycleOperation.COMPLETE_RESOLVE),
            AllowedEligibility,
        ):
            return []
        return outcome.resolve_actions(
            issue,
            result,
            pr,
            issue_closed_without_pr=closed_without_pr,
        )

    def _author_task(self, issue: int, iss, cwd: str, fix_cfg, human_reply: str, state: WorkflowSnapshot):
        return self._guided(task_builders.build_author_task(
            issue, as_untrusted(iss.title), as_untrusted(iss.body), cwd, fix_cfg,
            human_reply=as_untrusted(human_reply),
        ), state)

    @staticmethod
    def _split_issue_available(iss, *, reply_resume: bool = False) -> bool:
        if isinstance(
            issue_eligibility(iss, LifecycleOperation.COMPLETE_RESOLVE),
            AllowedEligibility,
        ):
            return True
        return reply_resume and isinstance(
            issue_eligibility(iss, LifecycleOperation.RESUME),
            AllowedEligibility,
        )

    def _split_cfg(self, context: SplitContext) -> FixTaskConfig:
        cfg = self.cfg.fix
        if cfg is None:
            raise WorkflowAuthorityError("resolve configuration is unavailable")
        if context.selection.runtime != self._resolve_runtime_name():
            raise WorkflowAuthorityError("split context belongs to a different runtime")
        model = (
            None
            if context.selection.model == "<runtime-default>"
            else context.selection.model
        )
        return dataclasses.replace(
            cfg,
            model=model,
            effort=context.selection.effort,
            difficulty_notice=context.selection.difficulty_notice,
        )

    def _split_reply(
        self,
        iss,
        requested: str,
        prior: SplitReply | None,
    ) -> SplitReply | None:
        latest = fix_domain.last_human_reply(iss, self.cfg.fix_bot)
        if requested:
            if (
                latest is None
                or latest.body != requested
                or not latest.author
                or not latest.created_at
            ):
                raise WorkflowAuthorityError(
                    "split reply identity is unavailable or changed"
                )
            return SplitReply(latest.author, latest.created_at, latest.body)
        if prior is None or latest is None:
            return prior
        identity = (prior.author, prior.created_at, prior.body)
        observed = [(item.author, item.created_at, item.body) for item in iss.comments]
        latest_identity = (latest.author, latest.created_at, latest.body)
        if (
            identity not in observed
            or latest_identity == identity
            or latest.body == prior.body
        ):
            return prior
        if not latest.author or not latest.created_at:
            raise WorkflowAuthorityError("new split reply lacks a stable identity")
        return SplitReply(latest.author, latest.created_at, latest.body)

    def _authored_inputs_current(
        self,
        checkpoint: object,
        task,
        iss,
    ) -> bool:
        if (
            not isinstance(checkpoint, AuthoredResolve)
            or checkpoint.author_admission.input_fingerprint
            != run_input_fingerprint(task.intent)
        ):
            return False
        control = self.workflow_control
        assert control is not None
        authored = control.returned_result(checkpoint.author_attempt)
        return (
            authored is not None
            and authored.inputs == ResolveInputs(iss.title, iss.body, iss.author)
        )

    def _complete_split_reset(
        self,
        issue: int,
        intent: ResetIntent,
    ) -> ContractResetCompleted:
        control = self.workflow_control
        assert control is not None
        with control.contract_reset_authority(intent) as existing_ack:
            if existing_ack is not None:
                return existing_ack
            reset = self.worktrees.reset_contract(
                issue, intent.baseline.revision, intent.intent_id
            )
            return control.complete_contract_reset(
                ContractResetCompleted(
                    intent, reset, datetime.now(timezone.utc)
                )
            )

    def _fresh_reauthor_task(
        self,
        issue: int,
        cwd: str,
        context: SplitContext,
        binding: WorkflowBinding,
        expected_inputs: ResolveInputs,
        input_fingerprint: str,
    ):
        """Rebuild replacement AUTHOR inputs around each destructive reset."""

        latest = self.gateway.get_issue(issue)
        latest_state = self._resolve_state(issue, binding)
        if (
            not self._split_issue_available(
                latest, reply_resume=context.reply is not None
            )
            or self.gateway.pr_for_issue(issue) is not None
        ):
            return None
        latest_reply = self._split_reply(latest, "", context.reply)
        if latest_reply != context.reply:
            return None
        latest_task = self._author_task(
            issue,
            latest,
            cwd,
            self._split_cfg(context),
            context.human_reply,
            latest_state,
        )
        latest_inputs = ResolveInputs(
            latest.title, latest.body, latest.author
        )
        latest_task = dataclasses.replace(
            latest_task,
            resolve_inputs=latest_inputs,
            split_input=context,
        )
        if (
            latest_inputs != expected_inputs
            or run_input_fingerprint(latest_task.intent) != input_fingerprint
        ):
            return None
        return latest_task

    async def _run_split_author(
        self,
        issue: int,
        iss,
        task,
        context: SplitContext,
        binding: WorkflowBinding,
    ) -> bool:
        task = dataclasses.replace(
            task,
            resolve_inputs=ResolveInputs(iss.title, iss.body, iss.author),
            split_input=context,
        )
        control = self.workflow_control
        assert control is not None
        expected_inputs = task.resolve_inputs
        assert expected_inputs is not None
        input_fingerprint = run_input_fingerprint(task.intent)
        intent = control.reauthor_reset_intent(binding, input_fingerprint)
        if intent is not None:
            fresh_task = self._fresh_reauthor_task(
                issue,
                task.cwd,
                context,
                binding,
                expected_inputs,
                input_fingerprint,
            )
            if fresh_task is None:
                self.cfg.log(
                    f"resolve #{issue}: replacement author reset deferred for fresh inputs"
                )
                return False
            acknowledgement = self._complete_split_reset(issue, intent)
            # GitHub is not covered by the local deployment barrier. Refuse a
            # paid replacement if issue, lifecycle, reply, or guidance changed
            # while the displaced implementation was being archived.
            task = self._fresh_reauthor_task(
                issue,
                task.cwd,
                context,
                binding,
                expected_inputs,
                input_fingerprint,
            )
            if task is None:
                self.cfg.log(
                    f"resolve #{issue}: replacement author inputs changed during reset"
                )
                return False
            task = dataclasses.replace(
                task,
                split_input=ReauthorInput(acknowledgement, context),
            )
        author = await self._run_logged("author", issue, task, kind="AUTHOR")
        returned = self._returned_result(author, binding)
        if outcome.infra_class_failure(author):
            self._charge_infra_death(issue, "author", author)
        else:
            self._report_return_health(author)
        if returned is not None:
            completed = self._finish_return(returned)
            if completed:
                self.executor.replay_pending()
            return completed
        return False

    async def resolve_split(self, issue: int, human_reply: str = "", *, binding: WorkflowBinding | None = None) -> None:
        state = self._resolve_state(issue, binding)
        assert binding is not None
        iss = self.gateway.get_issue(issue)
        if (
            not self._split_issue_available(
                iss, reply_resume=bool(human_reply)
            )
            or self.gateway.pr_for_issue(issue) is not None
        ):
            return
        checkpoint = state.episode.continuation
        retained = (
            checkpoint.split.context
            if isinstance(checkpoint, AuthoredResolve)
            and checkpoint.split is not None
            else None
        )
        branch = retained.branch if retained is not None else self._resume_branch(issue, iss.title, state)
        cwd = self._ensure_cwd(issue, branch, state)
        if retained is not None:
            fix_cfg = self._split_cfg(retained)
            deferred_for_overlap = False
        elif human_reply:
            fix_cfg = self._resolve_resume_cfg(issue, state, iss)
            deferred_for_overlap = False
        else:
            fix_cfg, deferred_for_overlap = await self._routed_fix_cfg(issue, iss, cwd, binding)
        if deferred_for_overlap:
            self._execute_resolve_actions(issue, [Relabel(Target.ISSUE, issue, Label.AI_WORKING, Label.AI_READY)], binding)
            return

        selection = (
            retained.selection
            if retained is not None
            else SplitSelection(
                self._resolve_runtime_name(),
                fix_cfg.model or "<runtime-default>",
                fix_cfg.effort,
                fix_cfg.difficulty_notice,
            )
        )
        state = self._resolve_state(issue, binding)
        iss = self.gateway.get_issue(issue)
        if (
            not self._split_issue_available(
                iss,
                reply_resume=(
                    bool(human_reply)
                    or (
                        retained is not None
                        and retained.reply is not None
                    )
                ),
            )
            or self.gateway.pr_for_issue(issue) is not None
        ):
            return
        reply = self._split_reply(
            iss,
            human_reply,
            retained.reply if retained is not None else None,
        )
        context = (
            dataclasses.replace(retained, reply=reply)
            if retained is not None
            else SplitContext(branch, selection, reply)
        )
        author_task = self._author_task(
            issue, iss, cwd, fix_cfg, context.human_reply, state
        )
        checkpoint = state.episode.continuation
        if not self._authored_inputs_current(checkpoint, author_task, iss):
            if not await self._run_split_author(
                issue,
                iss,
                author_task,
                dataclasses.replace(context, trail=()),
                binding,
            ):
                return
            state = self._resolve_state(issue, binding, allow_held=True)
            checkpoint = state.episode.continuation
            if not isinstance(checkpoint, AuthoredResolve) or not isinstance(
                checkpoint.split, SplitImplement
            ):
                return
        await self._implement_loop(issue, cwd, binding=binding, selected_reply=reply)

    async def _implement_loop(
        self, issue: int, cwd: str, *, binding: WorkflowBinding, selected_reply: SplitReply | None = None,
    ) -> None:
        while True:
            state = self._resolve_state(issue, binding)
            fresh = self.gateway.get_issue(issue)
            checkpoint = state.episode.continuation
            if not isinstance(checkpoint, AuthoredResolve) or checkpoint.split is None:
                return
            cursor = checkpoint.split
            context = cursor.context
            fix_cfg = self._split_cfg(context)
            reply = self._split_reply(fresh, "", context.reply)
            current_context = dataclasses.replace(context, reply=reply)
            author_task = self._author_task(
                issue, fresh, cwd, fix_cfg, current_context.human_reply, state
            )
            if (
                not self._split_issue_available(
                    fresh, reply_resume=context.reply is not None
                )
                or self.gateway.pr_for_issue(issue) is not None
            ):
                return
            if not self._authored_inputs_current(checkpoint, author_task, fresh):
                self.cfg.log(
                    f"resolve #{issue}: author inputs changed; re-authoring the contract"
                )
                if not await self._run_split_author(
                    issue,
                    fresh,
                    author_task,
                    dataclasses.replace(current_context, trail=()),
                    binding,
                ):
                    return
                continue
            if isinstance(cursor, NeedsAdjudication):
                control = self.workflow_control
                assert control is not None
                intent = ContractResetIntent(binding, cursor)
                latest = self.gateway.get_issue(issue)
                latest_state = self._resolve_state(issue, binding)
                latest_reply = self._split_reply(
                    latest, "", context.reply
                )
                latest_task = self._author_task(
                    issue,
                    latest,
                    cwd,
                    fix_cfg,
                    latest_reply.body if latest_reply is not None else "",
                    latest_state,
                )
                if (
                    not self._split_issue_available(
                        latest, reply_resume=context.reply is not None
                    )
                    or self.gateway.pr_for_issue(issue) is not None
                    or latest_reply != context.reply
                    or not self._authored_inputs_current(
                        latest_state.episode.continuation,
                        latest_task,
                        latest,
                    )
                ):
                    self.cfg.log(
                        f"resolve #{issue}: contract reset deferred for fresh inputs"
                    )
                    return
                acknowledgement = self._complete_split_reset(issue, intent)
                split_input = AdjudicationInput(acknowledgement)
                task = self._guided(
                    task_builders.build_adjudicate_task(
                        issue,
                        as_untrusted(fresh.title),
                        as_untrusted(fresh.body),
                        as_untrusted(cursor.objection),
                        cwd,
                        fix_cfg,
                        human_reply=as_untrusted(context.human_reply),
                    ),
                    state,
                )
                leg = "adjudicate"
                kind = "ADJUDICATE"
            else:
                split_input = cursor
                first_implementation = (
                    cursor.source == cursor.baseline.source
                    or (
                        bool(context.trail)
                        and context.trail[-1].attempt == cursor.source
                    )
                )
                if first_implementation:
                    verify_contract_revision(
                        cwd, issue, cursor.baseline.revision
                    )
                task = self._guided(
                    task_builders.build_implement_task(
                        issue,
                        as_untrusted(fresh.title),
                        as_untrusted(fresh.body),
                        cwd,
                        fix_cfg,
                        uphold_note=as_untrusted(context.uphold_note),
                        human_reply=as_untrusted(context.human_reply),
                    ),
                    state,
                )
                leg = "implement"
                kind = "IMPLEMENT"
            task = dataclasses.replace(
                task,
                resolve_inputs=ResolveInputs(
                    fresh.title, fresh.body, fresh.author
                ),
                split_input=split_input,
            )
            if selected_reply is not None:
                selected = cc.select_issue_recovery(state)
                if not isinstance(selected, cc.SelectedIssueRecovery):
                    raise WorkflowAuthorityError("selected split reply continuation requires migration")
                if cc.reply_advances(selected_reply.created_at, selected.component.state.consumed_reply_at):
                    task = dataclasses.replace(task, issue_reply=cc.IssueReplyInput(selected, selected_reply.created_at, selected_reply.body, context.branch))
            result = await self._run_logged(leg, issue, task, kind=kind)
            returned = self._returned_result(result, binding)
            if outcome.infra_class_failure(result):
                self._charge_infra_death(issue, leg, result)
            else:
                self._report_return_health(result)
            if returned is None:
                # Only legacy non-accounting infra/budget fakes can arrive
                # without the captured return this split plan requested.
                return
            if not self._finish_return(returned):
                return
            self.executor.replay_pending()
            if outcome.infra_class_failure(result) or outcome.budget_exhausted(
                result.exit_reason
            ):
                return
            if (
                leg == "implement"
                and outcome.implement_followup(result, False)
                is not outcome.ImplementFollowup.CONTRACT_CHANGE
            ):
                return

    def _charge_infra_death(self, issue: int, leg: str, result) -> None:
        """Report the deployment fault and the already captured SQL episode.

        Reporting must survive unavailable authority without charging the run
        a second time through the dispatcher exception path.
        """
        self._report_infra_death(issue, leg, outcome.infra_class_reason(result))
        try:
            selected = self.workflow_control.read_issue_recovery(WorkflowKey(self.workflow_control.writer.repository, IssueRef(issue))) if self.workflow_control is not None else None
            if not isinstance(selected, cc.SelectedIssueRecovery):
                raise WorkflowAuthorityError("fix infrastructure episode is unavailable")
            deaths = selected.component.state.infra_failures
        except Exception as exc:  # noqa: BLE001 — reporting preserves the original fault
            self.cfg.log(f"agent #{issue}: infra episode unavailable ({exc})")
            return
        journal = self.cfg.journal
        if journal is None:
            return
        try:
            journal.emit(
                TelemetryEvent(
                    InfraPathEntered(
                        leg, outcome.infra_class_reason(result), deaths
                    ),
                    Correlation(issue=issue),
                )
            )
        except Exception as exc:  # noqa: BLE001 — telemetry never fails a leg
            self.cfg.log(f"agent #{issue}: infra path not journalled ({exc})")

    def _report_infra_death(self, issue: int, leg: str, reason: str) -> None:
        """Feed the deployment-wide breaker. Never fails the leg: the breaker
        is an efficiency control, and a broken one must not become a second
        failure on top of the death it is being told about."""
        breaker = self.cfg.infra_breaker
        if breaker is None:
            return
        try:
            breaker.record_death(issue, leg, reason)
        except Exception as exc:  # noqa: BLE001 — see docstring
            self.cfg.log(f"agent #{issue}: infra breaker not updated ({exc})")

    def _report_healthy_leg(self) -> None:
        breaker = self.cfg.infra_breaker
        if breaker is None:
            return
        try:
            breaker.record_success()
        except Exception as exc:  # noqa: BLE001 — see `_report_infra_death`
            self.cfg.log(f"infra breaker not cleared ({exc})")

    def _journal_budget_path(self, issue: int, leg: str, reason, pr: int | None) -> None:
        """Record the operator-cap path separately from capability escalation."""
        if pr is not None or not outcome.budget_exhausted(reason):
            return
        journal = self.cfg.journal
        if journal is None:
            return
        try:
            journal.emit(
                TelemetryEvent(
                    BudgetPathEntered(leg, reason.value),
                    Correlation(issue=issue),
                )
            )
        except Exception as exc:  # noqa: BLE001 — telemetry never fails a dispatch
            self.cfg.log(f"agent #{issue}: budget path not journalled ({exc})")

    async def resume(self, issue: int, *, binding: WorkflowBinding | None = None) -> None:
        iss = self.gateway.get_issue(issue)
        pr_existing = self.gateway.pr_for_issue(issue)
        state = None
        reply_state = None
        effect_binding = binding
        if pr_existing is None:
            state = self._resolve_state(issue, binding)
            reply_state = state
            assert binding is not None
        else:
            if binding is not None:
                reply_state = self._resolve_state(issue, binding, allow_held=True)
            elif self.workflow_control is not None:
                captured = self.workflow_control.load_workflow(WorkflowKey(self.workflow_control.writer.repository, IssueRef(issue)))
                if not isinstance(captured, WorkflowSnapshot):
                    raise WorkflowAuthorityError("PR reply requires available linked-issue command authority")
                effect_binding = captured.binding
                reply_state = captured
            if isinstance(self.runner, ContextualRuntime):
                self.runner.set_context("fix", "PR_RESUME", pr_existing, issue, workflow=None)
        reply_comment = fix_domain.last_human_reply(iss, self.cfg.fix_bot)
        selected = cc.select_issue_recovery(reply_state) if reply_state is not None else None
        if not isinstance(selected, cc.SelectedIssueRecovery):
            raise WorkflowAuthorityError("issue reply continuation is inactive or migration blocked")
        if isinstance(selected, cc.SelectedIssueRecovery) and reply_comment is not None and reply_comment.body.strip() and not cc.reply_advances(reply_comment.created_at, selected.component.state.consumed_reply_at):
            return
        if reply_comment is None or not reply_comment.body.strip():
            if reply_comment is not None:
                assert self.workflow_control is not None
                assert reply_state is not None
                if self.workflow_control.consume_blank_issue_reply(reply_state.binding, reply_comment.created_at, reply_comment.body):
                    self.executor.replay_pending()
                return
            actions = [Relabel(Target.ISSUE, issue, Label.AI_WORKING, Label.AI_BLOCKED, workflow=effect_binding)]
            if state is not None:
                self._execute_resolve_actions(issue, actions, state.binding)
            else:
                self.executor.execute_all(actions)
            return
        reply = reply_comment.body
        if self.cfg.author_tests_split and state is not None:
            await self.resolve_split(issue, human_reply=reply, binding=binding)
            return
        repair = None
        if pr_existing is not None:
            current_pr = self.gateway.get_pr(pr_existing)
            if current_pr.state != "OPEN" or fix_domain.breaker_key(current_pr) != issue:
                return
            control = self.workflow_control
            assert control is not None
            repair_selected = control.read_pr_repair(WorkflowKey(control.writer.repository, PullRequestRef(pr_existing)))
            if not isinstance(repair_selected, cc.SelectedPRRepair):
                self.cfg.log(f"PR #{pr_existing}: linked reply deferred; repair continuity is inactive or blocked")
                return
            control.reconcile_pr_repair_returns()
            repair_selected = control.read_pr_repair(repair_selected.binding.fence.key)
            if not isinstance(repair_selected, cc.SelectedPRRepair):
                return
            branch = current_pr.head_ref_name
            runtime = RuntimeName(self._resolve_runtime_name())
            handoff = (control.pr_repair_handoff(issue, PullRequestRef(pr_existing), branch, runtime)
                       if repair_selected.component.state.actual_model is None and repair_selected.component.state.session is None else None)
            repair = cc.PRRepairInput(repair_selected, branch, runtime, handoff, head=current_pr.head_ref_oid)
            fork = max(repair_selected.component.state.resumes, handoff.resumes if handoff is not None else 0) + 1 >= self.cfg.fork_threshold
        else:
            branch = self._resume_branch(issue, iss.title, state)
            fork = selected.component.state.resumes + 1 >= self.cfg.fork_threshold
        cwd = self._ensure_cwd(issue, branch, state)
        if state is not None:
            session = state.episode.continuation.session
            session_id = session.session_id if session is not None else ""
            fix_cfg = self._resolve_resume_cfg(issue, state, iss)
        else:
            assert repair is not None
            session_id = repair.session.handle if repair.session is not None else ""
            fix_cfg = self._repair_fix_cfg(issue, "resume", iss=iss, inherited_model=repair.model)
        task = (
            task_builders.build_resume_task(as_untrusted(reply), cwd, session_id, fix_cfg, fork=fork)
            if session_id else task_builders.build_fresh_reply_task(
                issue, as_untrusted(iss.title), as_untrusted(iss.body), as_untrusted(reply), cwd, fix_cfg,
            )
        )
        if state is not None:
            self._resolve_state(issue, binding)
            task = self._guided(task, state)
            task = dataclasses.replace(task, resolve_inputs=ResolveInputs(iss.title, iss.body, iss.author))
            assert isinstance(selected, cc.SelectedIssueRecovery)
            task = dataclasses.replace(task, issue_reply=cc.IssueReplyInput(selected, reply_comment.created_at, reply, branch))
        else:
            assert pr_existing is not None
            assert repair is not None
            task = dataclasses.replace(task, issue_reply=cc.LinkedPRIssueReplyInput(selected, reply_comment.created_at, reply, PullRequestRef(pr_existing)),
                                       pr_repair_input=repair)
            self.runner.set_context("fix", "PR_RESUME", pr_existing, issue, workflow=repair.selection.binding)
        result = await self._run_logged("resume", issue, task, kind="RESUME" if state is not None else "PR_RESUME")
        if state is not None:
            assert binding is not None
            returned = self._returned_result(result, binding)
            if returned is not None:
                if outcome.infra_class_failure(result):
                    self._charge_infra_death(issue, "resume", result)
                else:
                    self._report_return_health(result)
                if self._finish_return(returned):
                    self.executor.replay_pending()
                return
        pr = self.gateway.pr_for_issue(issue)
        if state is not None:
            assert binding is not None
            # A reply-driven failure remains blocked; it does not buy a resolve
            # strike unless fresh requirements explicitly require a requeue.
            current = self.gateway.get_issue(issue)
            changed = result.status == "blocked" and not (
                outcome.infra_class_failure(result) or outcome.budget_exhausted(result.exit_reason)
            ) and (current.title, current.body, current.author) != (iss.title, iss.body, iss.author)
            followup = outcome.ResolveFollowup.PR_OPENED if pr is not None else (
                outcome.ResolveFollowup.RETRY if changed else outcome.ResolveFollowup.BLOCKED
            )
            if not self._record_resolve_result(
                issue, result, ResolveStage.RESUME, task, binding, followup,
                progressed=pr is None and not result.is_error and result.status != "blocked",
            ):
                return
        else:
            changed = False
            current = None
        if outcome.infra_class_failure(result):
            self._charge_infra_death(issue, "resume", result)
        else:
            self._report_return_health(result)
        actions = ([Relabel(Target.ISSUE, issue, Label.AI_WORKING, Label.AI_READY)] if changed
                   else outcome.resume_actions(issue, result, pr))
        if state is not None:
            self._execute_resolve_actions(issue, actions, state.binding, current=current)
        else:
            # PR work owns no resolve allowance, but its issue effects retain
            # the issue generation captured before the paid repair began.
            self.executor.execute_all([
                dataclasses.replace(action, workflow=effect_binding)
                if isinstance(action, (Relabel, PostComment)) and action.target is Target.ISSUE
                else action for action in actions
            ])

    def _dispatch_pr(self, pr: int, issue_hint: int | None):
        """Bind fresh PR state to the issue captured by its claim (ADR 0008)."""
        if issue_hint is None:
            issue = self.gateway.issue_for_pr(pr)
            if issue is None:
                return None
            return self.gateway.get_pr(pr), issue
        current = self.gateway.get_pr(pr)
        fresh_target = fix_domain.breaker_key(current)
        if fresh_target != issue_hint:
            self.cfg.log(
                f"dispatch for PR #{pr}: deferred — observed target "
                f"{issue_hint} changed to {fresh_target} before execution"
            )
            self._journal_deferral(pr, issue_hint, fresh_target)
            return None
        return current, issue_hint if issue_hint > 0 else None

    def _journal_deferral(
        self, pr: int, observed: int, fresh: int
    ) -> None:
        """A deferral is spend-safe but silent: a persistent linkage flap would
        re-defer the same dispatch every tick with only a log line. The event
        makes a livelock visible in the dashboard's per-issue event view
        (ADR 0008)."""
        journal = self.cfg.journal
        if journal is None:
            return
        try:
            journal.emit(
                TelemetryEvent(
                    TelemetryWarning(
                        "dispatch_deferred",
                        f"observed target {observed} changed to {fresh} "
                        "before execution",
                        {"observed_target": observed, "fresh_target": fresh},
                    ),
                    Correlation(pr=pr),
                )
            )
        except Exception as exc:  # noqa: BLE001 — telemetry must not block dispatch
            self.cfg.log(f"dispatch for PR #{pr}: deferral not journalled ({exc})")

    async def resume_pr(
        self, pr: int, *, issue_hint: int | None = None
    ) -> None:
        """Resume the selected PR reply with its compatible SQL transcript."""
        dispatch = self._dispatch_pr(pr, issue_hint)
        if dispatch is None:
            return
        p, issue = dispatch
        if issue is None or p.state != "OPEN":
            return
        # Read before any repair side effect: an unavailable active-review read
        # raises a workflow hold and defers the whole repair (ADR 0015).
        guidance = self._pr_guidance(p.number)
        control = self.workflow_control
        selected = control.read_pr_repair(WorkflowKey(control.writer.repository, PullRequestRef(pr))) if control is not None else None
        if not isinstance(selected, cc.SelectedPRRepair):
            self.cfg.log(f"PR #{pr}: reply deferred; PR repair continuity is inactive or blocked")
            return
        control.reconcile_pr_repair_returns()
        selected = control.read_pr_repair(selected.binding.fence.key)
        if not isinstance(selected, cc.SelectedPRRepair):
            return
        reply_comment = fix_domain.last_human_reply(p, self.cfg.fix_bot)
        if reply_comment is None or not reply_comment.body.strip():
            if reply_comment is not None:
                control.consume_blank_pr_reply(selected.binding, reply_comment.created_at, reply_comment.body)
            return
        if not cc.reply_advances(reply_comment.created_at, selected.component.state.consumed_reply_at):
            return
        runtime = RuntimeName(self._resolve_runtime_name())
        handoff = (control.pr_repair_handoff(issue, PullRequestRef(pr), p.head_ref_name, runtime)
                   if selected.component.state.actual_model is None and selected.component.state.session is None else None)
        repair = cc.PRRepairInput(selected, p.head_ref_name, runtime, handoff,
                                  reply=cc.PRReplyInput(reply_comment.created_at, reply_comment.body), head=p.head_ref_oid)
        if repair.session is None:
            self.cfg.log(f"PR #{pr}: reply deferred; no compatible session is available")
            return
        iss = self.gateway.get_issue(issue)
        session_id = repair.session.handle
        leg = "fix-ci" if any(check.bucket == "fail" and not check.advisory for check in p.checks) else "resume"
        fix_cfg = self._repair_fix_cfg(issue, leg, iss=iss, inherited_model=repair.model)
        cwd = self._ensure_cwd(issue, p.head_ref_name)
        fork = max(selected.component.state.resumes, handoff.resumes if handoff is not None else 0) + 1 >= self.cfg.fork_threshold
        if any(check.bucket == "fail" and not check.advisory for check in p.checks):
            task = self._ci_task(p, issue, cwd, fork=fork, session_id=session_id, fix_cfg=fix_cfg)
        else:
            task = task_builders.build_resume_task(
                as_untrusted(reply_comment.body),
                cwd,
                session_id,
                fix_cfg, fork=fork,
            )
        task = dataclasses.replace(task, pr_repair_input=repair)
        self.runner.set_context("fix", "PR_RESUME", pr, issue, workflow=selected.binding)
        result = await self._run_pr_logged(
            "pr-resume", issue, task, p, kind="PR_RESUME", reply=reply_comment.body,
            guidance=guidance,
        )
        if outcome.infra_class_failure(result):
            self._charge_infra_death(issue, "pr-resume", result)
        else:
            self._report_return_health(result)
        self.executor.execute_all(outcome.pr_resume_actions(pr, result))

    def _resolve_handoff_session(self, issue: int) -> ResolveSession | None:
        """Consume a prior resolve transcript without reviving its file writer."""
        if self.workflow_control is None:
            return None
        try:
            state = self.workflow_control.load_workflow(WorkflowKey(self.workflow_control.writer.repository, IssueRef(issue)))
        except Exception as exc:  # noqa: BLE001 — unavailable handoff cannot authorize paid resume
            self.cfg.log(f"issue #{issue}: resolve session handoff unavailable ({exc})")
            return None
        if isinstance(state, WorkflowSnapshot):
            session = state.episode.continuation.session
            if session is not None and session.runtime == self._resolve_runtime_name():
                return session
        return None

    async def address_review(
        self, pr: int, *, issue_hint: int | None = None
    ) -> None:
        dispatch = self._dispatch_pr(pr, issue_hint)
        if dispatch is None:
            return
        p, issue = dispatch
        if issue is None:
            return
        control = self.workflow_control
        # Guidance is independently required before repair admission. Its
        # unreadable source retains the existing visible guidance hold.
        guidance = self._pr_guidance(p.number)
        selected = control.read_pr_repair(WorkflowKey(control.writer.repository, PullRequestRef(pr))) if control is not None else None
        if not isinstance(selected, cc.SelectedPRRepair):
            self.cfg.log(f"address-review #{pr}: PR repair continuity is inactive or blocked")
            return
        control.reconcile_pr_repair_returns()
        selected = control.read_pr_repair(selected.binding.fence.key)
        if not isinstance(selected, cc.SelectedPRRepair):
            return
        issue_selected = control.read_issue_recovery(WorkflowKey(control.writer.repository, IssueRef(issue)))
        if not isinstance(issue_selected, cc.SelectedIssueRecovery):
            self.cfg.log(f"address-review #{pr}: issue recovery allowance is inactive or blocked")
            return
        review_at = self._current_review_at(p)
        if not review_at:
            return
        if p.state != "OPEN" or self.gateway.pr_for_issue(issue, require_unique=True) != pr:
            self.cfg.log(f"address-review #{pr}: current issue-to-PR linkage is ambiguous or changed")
            return
        runtime = RuntimeName(self._resolve_runtime_name())
        handoff = (control.pr_repair_handoff(issue, PullRequestRef(pr), p.head_ref_name, runtime)
                   if selected.component.state.actual_model is None and selected.component.state.session is None else None)
        repair = cc.PRRepairInput(selected, p.head_ref_name, runtime, handoff, review=cc.ReviewRepairInput(
            issue_selected, PullRequestRef(pr), p.head_ref_oid, review_at,
            self.cfg.max_review_retries if self.cfg.max_review_retries > 0 else 2**63-1), head=p.head_ref_oid)
        cwd = self._ensure_cwd(issue, p.head_ref_name)
        review_body = self.latest_review_body(p)
        # The file:line asks reviewers actually make live in the INLINE comments,
        # not the review body (review M13): dispatching without them rendered an
        # empty "Inline comments:" header and cost rounds off the 4-rejection
        # ceiling while the fixer guessed at what was being asked.
        inline = self._review_comments(pr)
        fork = max(selected.component.state.resumes, handoff.resumes if handoff is not None else 0) + 1 >= self.cfg.fork_threshold
        task = task_builders.build_address_review_task(
            pr,
            as_untrusted(review_body),
            as_untrusted(inline),
            cwd,
            repair.session.handle if repair.session is not None else "",
            self._repair_fix_cfg(
                issue, "address-review",
                review_rounds=count_outstanding_change_requests(p.reviews, self.cfg.fix_bot),
                inherited_model=repair.model,
            ),
            fork=fork,
        )
        task = dataclasses.replace(task, pr_repair_input=repair)
        self.runner.set_context("fix", "ADDRESS_REVIEW", pr, issue, workflow=selected.binding)
        result = await self._run_pr_logged(
            "address-review", issue, task, p, kind="ADDRESS_REVIEW", guidance=guidance,
        )
        out = outcome.address_review_actions(pr, result)
        self.executor.execute_all(out.actions)
        if outcome.infra_class_failure(result):
            # review C3/D2c, RETURNED-result half. address_review_actions
            # exempts an external kill from the review-retry ceiling (no attempt
            # was made) and leaves the PR untouched, so the next poll sees the same
            # CHANGES_REQUESTED review and pays for the leg again — unbounded
            # until this counter bounds it. Same key the review-retry counter
            # uses, so the decide side reads one key per issue. A leg either
            # raises or returns, so a death is counted exactly once.
            self._charge_infra_death(issue, "address-review", result)
        else:
            self._report_return_health(result)

    def _pr_guidance(self, pr: int) -> str:
        """The operator's scope note for a PR repair, from its one owner.

        The review role's committed episode owns it. Missing or unreadable
        authority defers the repair until migration or recovery.
        """
        if self.cfg.operator_guidance is None:
            raise WorkflowAuthorityError("review guidance authority is unavailable")
        return self.cfg.operator_guidance(pr)

    async def _run_pr_logged(self, label, issue, task, p: PRView, *, kind, guidance: str, reply=None):
        if self.workflow_control is None:
            raise WorkflowAuthorityError("CI remediation repair context authority is unavailable")
        selected = self.workflow_control.read_pr_repair(WorkflowKey(self.workflow_control.writer.repository, PullRequestRef(p.number)))
        if not isinstance(selected, cc.SelectedPRRepair):
            raise WorkflowAuthorityError("CI remediation repair context authority is inactive")
        remedy = selected.component.state.ci_rerun
        rerun_head = (remedy.capture.head if isinstance(remedy, cc.OwedCIRemediation)
                      else remedy.owed.capture.head if isinstance(remedy, cc.SettledCIRemediation)
                      else remedy.head if isinstance(remedy, cc.ImportedCIRemediation) else "")
        current = task_builders.with_pr_repair_context(
            task, p,
            rerun_head=rerun_head,
            operator_guidance=guidance,
            discussion=as_untrusted(format_pr_discussion(p.comments)),
            human_reply=as_untrusted(reply) if reply is not None else None,
        )
        return await self._run_logged(label, issue, current, kind=kind)

    def _ci_task(self, p: PRView, issue: int, cwd: str, *, fork: bool = False,
                 session_id: str = "", fix_cfg: FixTaskConfig | None = None):
        pr = p.number
        failed = "\n".join(c.name for c in p.checks if c.bucket == "fail")
        failed_log = self._failed_run_log(p)
        mcp_servers = mcp_servers_for_checks(
            self.cfg.fix_mcp_config, [c.name for c in p.checks], self.cfg.harness_root
        )
        # PRIOR attempts only — the current one is the prompt's own subject, and
        # a history that always contained it could never be empty. `_try`-wrapped
        # because history is context, not authority: an unreadable store must
        # cost the leg its extra section, never its dispatch (`_signals`' rule).
        prior_attempts = self._try(lambda: self.store.get_ci_history(issue), ()) or ()
        return task_builders.build_fix_ci_task(
            pr,
            as_untrusted(failed),
            as_untrusted(failed_log),
            cwd,
            session_id,
            # No `review_rounds` here (nor on `rebase`), though `p.reviews` is in
            # hand: review dissatisfaction is evidence about the RESOLUTION, and
            # a red pipeline or a conflicted branch is a different failure mode.
            # Spending the ladder on it would charge the wrong leg for the
            # reviewer's objections. Recurrence and resolve retries still apply.
            self._repair_fix_cfg(issue, "fix-ci") if fix_cfg is None else fix_cfg,
            mcp_servers=mcp_servers,
            ci_history=prior_attempts,
            fork=fork,
        )

    async def fix_ci(
        self, pr: int, *, issue_hint: int | None = None
    ) -> None:
        dispatch = self._dispatch_pr(pr, issue_hint)
        if dispatch is None:
            return
        p, issue = dispatch
        if issue is None:
            return
        control = self.workflow_control
        selected = control.read_pr_repair(WorkflowKey(control.writer.repository, PullRequestRef(pr))) if control is not None else None
        if not isinstance(selected, cc.SelectedPRRepair):
            self.cfg.log(f"fix-ci #{pr}: PR repair continuity is inactive or blocked")
            return
        # Read before any repair side effect: an unavailable active-review read
        # raises a workflow hold and defers the whole repair (ADR 0015).
        control.reconcile_pr_repair_returns()
        selected = control.read_pr_repair(selected.binding.fence.key)
        if not isinstance(selected, cc.SelectedPRRepair):
            return
        issue_selected = control.read_issue_recovery(WorkflowKey(control.writer.repository, IssueRef(issue)))
        if not isinstance(issue_selected, cc.SelectedIssueRecovery):
            self.cfg.log(f"fix-ci #{pr}: issue recovery allowance is inactive or blocked")
            return
        runtime = RuntimeName(self._resolve_runtime_name())
        handoff = (control.pr_repair_handoff(issue, PullRequestRef(pr), p.head_ref_name, runtime)
                   if selected.component.state.actual_model is None and selected.component.state.session is None else None)
        repair = cc.PRRepairInput(selected, p.head_ref_name, runtime, handoff,
            cc.CIRepairInput(issue_selected, PullRequestRef(pr), p.head_ref_oid, self.cfg.max_ci_retries), head=p.head_ref_oid)
        guidance = self._pr_guidance(p.number)
        cwd = self._ensure_cwd(issue, p.head_ref_name)
        attempt = make_attempt(p.head_ref_oid, [c.name for c in p.checks if c.bucket == "fail"])
        task = dataclasses.replace(self._ci_task(p, issue, cwd,
            session_id=repair.session.handle if repair.session is not None else "",
            fix_cfg=self._repair_fix_cfg(issue, "fix-ci", inherited_model=repair.model)), pr_repair_input=repair)
        self.runner.set_context("fix", "FIX_CI", pr, issue, workflow=selected.binding)
        result = await self._run_pr_logged("fix-ci", issue, task, p, kind="FIX_CI", guidance=guidance)
        # An externally-killed session made no attempt, so it must not march the
        # CI breaker toward needs-human-review (FAILURE-MODES #58: six SIGKILLs on
        # one PR burned the whole budget on work that never ran). Neither did a
        # budget-capped one (#155): an admission rejection is deterministic, so
        # counting it walks a healthy PR to needs-human-review at $0 per strike
        # (keifu PR #174, 2026-08-02) — fix_ci_actions routes it to the budget
        # surface instead.
        # The attempt history obeys the SAME rule as the counter above, for the
        # same reason: a budget-capped tick is a deterministic $0 non-attempt
        # (#155), and it does not advance the breaker — so `decide_fix`
        # re-dispatches it every poll. Recording those would let five capped
        # ticks evict the whole real record and leave five identical
        # zero-information rows, i.e. the blindness this exists to remove,
        # disguised as a populated history. An infra death and an agent error
        # DO get recorded: those legs ran, and their failure is the evidence.
        capped = outcome.budget_exhausted(result.exit_reason)
        if not capped:
            self._try(lambda: self.store.append_ci_attempt(issue, attempt), None)
        if capped:
            pass
        elif not outcome.infra_class_failure(result):
            self._report_return_health(result)
        else:
            # Separate, higher ceiling for the deaths themselves (review C3/D2c)
            # — same key the CI counter uses, so the decide side reads one key.
            # `ci_retries` is ALSO router difficulty evidence
            # (EscalationSignals), so a harness fault charged here would inflate
            # effort and model tier as well as the breaker (keifu #130).
            self._charge_infra_death(issue, "fix-ci", result)
        self.executor.execute_all(outcome.fix_ci_actions(pr, result))

    async def rebase(
        self, pr: int, *, issue_hint: int | None = None
    ) -> None:
        dispatch = self._dispatch_pr(pr, issue_hint)
        if dispatch is None:
            return
        p, issue = dispatch
        if issue is None:
            return
        guidance = self._pr_guidance(p.number)
        control = self.workflow_control
        selected = control.read_pr_repair(WorkflowKey(control.writer.repository, PullRequestRef(pr))) if control is not None else None
        if not isinstance(selected, cc.SelectedPRRepair):
            self.cfg.log(f"rebase #{pr}: PR repair continuity is inactive or blocked")
            return
        control.reconcile_pr_repair_returns()
        selected = control.read_pr_repair(selected.binding.fence.key)
        if not isinstance(selected, cc.SelectedPRRepair):
            return
        issue_selected = control.read_issue_recovery(WorkflowKey(control.writer.repository, IssueRef(issue)))
        if not isinstance(issue_selected, cc.SelectedIssueRecovery):
            self.cfg.log(f"rebase #{pr}: issue recovery allowance is inactive or blocked")
            return
        if p.state != "OPEN" or self.gateway.pr_for_issue(issue, require_unique=True) != pr:
            self.cfg.log(f"rebase #{pr}: current issue-to-PR linkage is ambiguous or changed")
            return
        runtime = RuntimeName(self._resolve_runtime_name())
        handoff = (control.pr_repair_handoff(issue, PullRequestRef(pr), p.head_ref_name, runtime)
                   if selected.component.state.actual_model is None and selected.component.state.session is None else None)
        repair = cc.PRRepairInput(selected, p.head_ref_name, runtime, handoff, rebase=cc.RebaseRepairInput(
            issue_selected, PullRequestRef(pr), p.head_ref_oid, p.head_ref_name,
            self.cfg.max_rebase_retries if self.cfg.max_rebase_retries > 0 else 2**63-1), head=p.head_ref_oid)
        cwd = self._ensure_cwd(issue, p.head_ref_name)
        task = task_builders.build_rebase_task(
            pr, self.cfg.base_branch, cwd, repair.session.handle if repair.session is not None else "",
            self._repair_fix_cfg(issue, "rebase", inherited_model=repair.model)
        )
        task = dataclasses.replace(task, pr_repair_input=repair)
        self.runner.set_context("fix", "REBASE", pr, issue, workflow=selected.binding)
        result = await self._run_pr_logged("rebase", issue, task, p, kind="REBASE", guidance=guidance)
        # Same rule as fix_ci: neither an infra kill nor a budget-capped
        # admission/leg is a failed rebase attempt (#155).
        if outcome.budget_exhausted(result.exit_reason):
            pass
        elif not outcome.infra_class_failure(result):
            self._report_return_health(result)
        else:  # review C3/D2c, as in fix_ci
            self._charge_infra_death(issue, "rebase", result)
        self.executor.execute_all(outcome.fix_ci_actions(pr, result))

    @staticmethod
    def latest_review_body(pr) -> str:
        for review in reversed(pr.reviews):
            if review.body.strip():
                return review.body
        return ""

    def _current_review_at(self, pr) -> str:
        """Timestamp key of the review being addressed: the latest review by a
        DISTINCT actor iff it requests changes — the SAME selector the decide
        side gates dispatch on (latest_other_review). The previous scan (last
        CHANGES_REQUESTED in tuple order, author-agnostic) could key the
        counter to a DIFFERENT review than the one dispatched on; a mis-keyed
        counter reads 0 forever and the breaker never trips (the V2 shape)."""
        review = latest_other_review(pr.reviews, self.cfg.fix_bot)
        if review is not None and review.state == REVIEW_CHANGES_REQUESTED:
            return review.submitted_at
        return ""
