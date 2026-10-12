"""The review daemon's runtime for an ACTIVE review-rejection family (ADR 0015).

A repository whose review family is not activated never reaches this module's
decisions: `ActiveReviewRuntime.active()` reads the family row every tick, so
activation is runtime capability detection rather than launch-time config.
Inactive repositories require explicit operator migration before dispatch.

When active, the workflow authority is the only source of three things the
legacy path kept in per-PR files: whether a review is owed (sessions/last_sha
/patch-id are no longer consulted), the operator guidance paid legs read, and
the reviewed baseline a verdict establishes (a bound submission's receipt).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import sqlite3

from ..adapters.control_store import ControlStoreError, SQLiteControlStore, _dispatch_claim_target
from ..adapters.effects import GitHubEffectExecutor, UnknownEffectOutcome, bound_review_command_id
from ..adapters.review_effects import (
    BoundReviewSubmissionOp, pull_request_observation, read_pull_request,
)
from ..adapters.review_linkage import ReviewLinkedIssueUnavailable, review_linked_issue_holds
from ..adapters.review_workflow_control import SQLiteReviewWorkflowControl, read_review_guidance
from ..domain.review import PostedReview, posted_review
from ..adapters.review_execution_control import SQLiteReviewExecutionControl
from ..adapters.workflow_rows import WorkflowAuthorityError, load_review_workflow_row
from ..domain.actions import AgentKind
from ..domain.contracts import CommandId, CommandKind, LifecycleCommand
from ..domain.labels import Label
from ..domain.lifecycle import retry_breaker
from ..domain.fix import breaker_key
from ..domain.review import (
    UNPRODUCTIVE_ROUND_HOLD, ReviewConfig, ReviewExcluded, ReviewMode, review_candidacy, review_eligibility,
)
from ..domain.review_dispatch_record import (
    MAX_DEPARTED_READS, AdmissionOutcome, DecisionBasis, DecisionEntry, DecisionRead, DecisionUpdate,
    DepartedReadFailed, RecordedDispatch, ReviewDeferred, ReviewNotAdmitted, ReviewTerminal, admitted_decision,
)
from ..domain.review_evidence import ReviewEvidenceKind
from ..domain.agent_task import RUNTIME_CAPABILITIES, RuntimeName
from ..domain.review_execution import (
    CurrentReviewExecution, ReviewChildIdentity, ReviewChildKind,
    compatible_review_session,
)
from ..domain.review_preflight import ReviewPreflightSelection
from ..domain.review_rounds import ReviewStagePlan, ReviewSubmissionKind
from ..domain.review_rounds import ReviewPlanningConclusion, ReviewPlanningOpen, ReviewPlanningSealed
from ..adapters.workflow_codec import decode_workflow
from ..domain.review_rejection import CountingRule
from ..domain.review_workflow import (
    ObservedPullRequest, PartialReviews, PullRequestState, PullRequestUnavailable, ReviewedBaseline,
    ReviewWorkflowSnapshot, decisive_reviews,
)
from ..domain.review_workflow_policy import (
    ReleaseProjectionPending, ReviewHeld, ReviewKind, ReviewNotOwed, ReviewOwed, SubmissionUnresolved,
    counted_rejections,
)
from ..domain.workflow import WorkflowAbsent, WorkflowUnavailable
from ..domain.workflow_identity import PullRequestRef, WorkflowBinding, WorkflowKey, WorkflowWriter
from .config import HarnessConfig
from .workflows.review import BoundReviewRound

# Labels only the harness (or a human acting on the lifecycle) applies. They
# are never derived from agent output or the PR body, so they are a trusted
# hold source for re-arm acceptance and completion (ADR 0012 provenance).
_TRUSTED_HOLD_LABELS = (Label.AI_BLOCKED, Label.AI_PARKED, Label.NEEDS_BUDGET_REVIEW)
_UNRESOLVED_STATES = frozenset({"executing", "unknown_outcome", "retryable_failure"})
# A failure to record the advisory dispatch decisions is reported at most this
# often (ADR 0012): the next report carries how many were suppressed.
RECORD_FAILURE_REPORT_SECONDS = 900


class _LinkedIssueDeferred(ValueError):
    """Linked-issue evidence is unavailable or waiting for its scheduled read."""


def trusted_external_holds(
    labels: frozenset[Label], unproductive_rounds: int, max_unproductive_rounds: int,
) -> tuple[str, ...]:
    """The harness-owned hold labels present, then the unproductive-round breaker if it tripped."""
    found = tuple(label.value for label in _TRUSTED_HOLD_LABELS if label in labels)
    if retry_breaker(unproductive_rounds, max_unproductive_rounds).tripped:
        found += (UNPRODUCTIVE_ROUND_HOLD,)
    return found


def review_external_holds(
    unproductive_rounds: Callable[[object], int], max_unproductive_rounds: int,
    *, linked_holds: Callable[[ObservedPullRequest, object], tuple[str, ...]] | None = None,
) -> Callable[[ObservedPullRequest, object], tuple[str, ...]]:
    """Trusted holds a review re-arm must respect beside its own ceiling gate.

    Sources: harness-owned PR labels, the review role's unproductive-round
    counter, and fresh linked-issue lifecycle and workflow holds. The issue
    join uses the same PR view as the observation. An unreadable source
    raises; unavailable evidence never authorizes a release.
    """
    def holds(observed: ObservedPullRequest, view: object) -> tuple[str, ...]:
        if getattr(view, "number", None) != observed.key.target.number:
            raise ValueError("the unproductive-round counter needs this read's pull request view")
        found = trusted_external_holds(observed.labels, unproductive_rounds(view), max_unproductive_rounds)
        return found + (linked_holds(observed, view) if linked_holds is not None else ())

    return holds


def compose_review_control(
    control: SQLiteControlStore, gateway: object, *, repository: str, owner_id: str, ceiling: int,
    trigger_label: Label, review_bot: str,
    external_holds: Callable[[ObservedPullRequest, object], tuple[str, ...]],
    runtime_preflight: Callable[[], str | None] | None = None,
    execution_ceiling: int = 0,
) -> SQLiteReviewWorkflowControl:
    return SQLiteReviewWorkflowControl(
        control, WorkflowWriter(repository, "review", owner_id), gateway=gateway, ceiling=ceiling,
        trigger_label=trigger_label, rule=CountingRule(review_bot), external_holds=external_holds,
        runtime_preflight=runtime_preflight, execution_ceiling=execution_ceiling,
    )


def configured_review_control(
    cfg: HarnessConfig, control: SQLiteControlStore, gateway: object, store, owner_id: str,
    *, runtime_preflight: Callable[[], str | None] | None = None,
) -> SQLiteReviewWorkflowControl:
    """The review owner's workflow authority as configured for this deployment.

    The review daemon and the local operator transport both compose it here,
    so an operator re-arm is accepted under exactly the ceiling, trigger,
    counting rule and trusted hold sources the daemon enforces.
    """
    review = cfg.review_runtime
    return compose_review_control(
        control, gateway, repository=cfg.repo, owner_id=owner_id, ceiling=review.max_rejections,
        execution_ceiling=review.max_unproductive_rounds,
        trigger_label=Label(review.trigger_label), review_bot=review.bot,
        external_holds=review_external_holds(
            lambda view: current_review_failures(control, cfg.repo, view.number),
            review.max_unproductive_rounds,
            linked_holds=lambda observed, view: review_linked_issue_holds(control, gateway, observed.key, view),
        ),
        runtime_preflight=runtime_preflight,
    )


def current_review_failures(control: SQLiteControlStore, repository: str, pr: int) -> int:
    """The selected Current execution component owns the review failure allowance."""
    with control.transaction() as db:
        snapshot = load_review_workflow_row(db, WorkflowKey(repository, PullRequestRef(pr)))
    if snapshot is None:
        return 0
    if not isinstance(snapshot.execution, CurrentReviewExecution):
        raise WorkflowAuthorityError("review execution is held")
    return snapshot.execution.state.failures


def review_guidance_reader(
    control: SQLiteControlStore, repository: str,
) -> Callable[[int], str]:
    """The fix role's read-only guidance source for PR repairs.

    Active family: the committed review episode's guidance; an unreadable
    authority raises `WorkflowAuthorityError`, which defers the repair.
    Inactive authority defers repairs until explicit migration.
    """
    def read(pr: int) -> str:
        guidance = read_review_guidance(control, repository, pr)
        if guidance is None:
            raise WorkflowAuthorityError("review_migration_required")
        return guidance

    return read


@dataclass(frozen=True)
class PlannedDecision:
    """One PR's plan decision, recorded once this tick's dispatch admission is known."""

    key: WorkflowKey
    decision: RecordedDispatch
    basis: DecisionBasis | None


@dataclass(frozen=True)
class ReviewDispatchPlan:
    """What the active workflow owes this tick, per PR, what it could not read, and what it decided.

    `departed` is what the departed pull requests' own reads decided, read
    before dispatch admission because nothing about them depends on it.
    """

    owed: Mapping[int, tuple[AgentKind, WorkflowBinding]]
    errors: Mapping[int, str]
    held: Mapping[int, str]
    decisions: Mapping[int, PlannedDecision] = field(default_factory=dict)
    departed: tuple[DecisionEntry, ...] = ()


class ActiveReviewRuntime:
    """The review workflow authority as seen by the review daemon and its dispatches."""

    def __init__(
        self, control: SQLiteReviewWorkflowControl, effects: GitHubEffectExecutor, gateway: object, *,
        review_bot: str, log: Callable[[str], None], mode: ReviewMode = ReviewMode.SINGLE,
        runtime: RuntimeName = RuntimeName.CLAUDE, failure_ceiling: int = 5,
    ) -> None:
        self.control = control
        self._effects = effects
        self._gateway = gateway
        self._review_bot = review_bot
        self._log = log
        self._mode, self._runtime, self._failure_ceiling = mode, runtime, failure_ceiling
        self.execution = SQLiteReviewExecutionControl(
            control._control, control.writer, gateway,
            preparation_control=control if control._runtime_preflight is not None else None,
        )
        self._record_failures = 0
        self._record_failure_reported: datetime | None = None

    @property
    def repository(self) -> str:
        return self.control.writer.repository

    def _key(self, pr: int) -> WorkflowKey:
        return WorkflowKey(self.repository, PullRequestRef(pr))

    def _now(self) -> datetime:
        return self.control._now()

    def active(self) -> bool:
        return self.control.family_active()

    # -- reconciliation (bound into the executor drain and boot recovery) -------------------

    def reconcile(self) -> int:
        """Settle landed submissions, drive pending re-arms, then due holds; bounded per pass.

        Runs before the command drain and outside every effect runner's shared
        authority (ceiling and re-arm reconciliation take the exclusive
        deployment barriers). One failing pass never stops the others; an
        inactive family does nothing, so inactive repositories are unchanged.
        """
        try:
            if not self.active():
                return 0
        except (WorkflowAuthorityError, ControlStoreError) as exc:
            self._log(f"review workflow reconciliation held: {exc}")
            return 0
        progressed = 0
        for name, step in (
            ("rounds", self._reconcile_rounds),
            ("submissions", self.control.reconcile_submissions),
            ("re-arms", self.control.reconcile_pending_rearms),
            ("holds", self.control.reconcile_holds),
        ):
            try:
                progressed += step()
            except (WorkflowAuthorityError, ControlStoreError, ValueError) as exc:
                self._log(f"review workflow {name} reconciliation deferred: {exc}")
        return progressed

    def _reconcile_rounds(self) -> int:
        sweep = self.execution.reconcile_review_rounds()
        return len(sweep.sealed) + len(sweep.finalized)

    # -- dispatch plan ---------------------------------------------------------------------

    def plan(
        self, world, cfg: ReviewConfig, unproductive_rounds: Mapping[int, int] | None = None,
        hold_errors: Mapping[int, str] | None = None,
    ) -> ReviewDispatchPlan:
        """Per candidate PR: owed (with its binding), held, or unreadable; and each PR's decision to record.

        A PR the workflow has never seen is observed once (the initial
        workflow row). The decision uses the world's head, state and labels;
        its review list is not complete, so it is never used to count or
        dismiss; the dispatch re-decides from a complete read before paying.

        `unproductive_rounds` and `hold_errors` are the review role's breaker
        counters for this world (`decide_bound_review` skips a PR whose counter
        is unreadable). Nothing is recorded here: `record_plan` records the
        decisions once the role's dispatch admission is known. The departed
        pull requests' own reads run here, before admission.
        """
        rounds = unproductive_rounds or {}
        unreadable = hold_errors or {}
        owed: dict[int, tuple[AgentKind, WorkflowBinding]] = {}
        errors: dict[int, str] = {}
        held: dict[int, str] = {}
        decisions: dict[int, PlannedDecision] = {}
        external: dict[int, tuple[str, ...]] = {}
        now = self._now()
        for pr in world.prs:
            key = self._key(pr.number)
            observed = pull_request_observation(key, pr, now, PartialReviews("world observation"))
            basis = None
            recorded: RecordedDispatch
            try:
                recorded = self._plan_one(pr, key, observed, cfg, rounds.get(pr.number, 0), owed, errors, held,
                                          external)
                if pr.number in external or not isinstance(recorded, ReviewDeferred):
                    basis = self._basis(observed, rounds.get(pr.number, 0), cfg, external.get(pr.number))
            except (sqlite3.Error, OSError, WorkflowAuthorityError, ControlStoreError, ValueError) as exc:
                errors[pr.number] = str(exc)[:512] or type(exc).__name__
                recorded = ReviewDeferred(errors[pr.number])
            if pr.number in unreadable and not isinstance(recorded, ReviewTerminal):
                # `decide_bound_review` skips this PR; its breaker state is unknown.
                recorded, basis = ReviewDeferred(
                    f"unproductive-round counter unreadable: {unreadable[pr.number]}"[:512]), None
            decisions[pr.number] = PlannedDecision(key, recorded, basis)
        return ReviewDispatchPlan(owed, errors, held, decisions, self._plan_departed(world, cfg))

    def record_plan(self, plan: ReviewDispatchPlan, admissions: Mapping[int, AdmissionOutcome] | None = None) -> None:
        """Record this tick's decisions after dispatch admission, in one write; never raises.

        `admissions` is what the role's admission (the fix claim, reservations)
        did with an owed review it did not start this tick; the owed decision
        is recorded as withheld or running instead, so the view never says a
        review is being bought that admission refused. Paid admission (the
        spend cap) and the dispatch's own fresh read still decide after this.

        The record is the operator view's projection, never dispatch
        authority: after admission, any failure here is reported (rate
        bounded) and can never change or drop the admitted actions.
        """
        try:
            outcomes = admissions or {}
            entries = tuple(
                DecisionUpdate(planned.key, admitted_decision(planned.decision, outcomes.get(number)), planned.basis,
                               DecisionRead.REVIEW_READ)
                for number, planned in plan.decisions.items()
            ) + plan.departed
            recorded = self.control.record_dispatch_decisions(entries)
        except Exception as exc:  # noqa: BLE001 - see docstring: a projection never drops dispatch
            self._record_failed(f"{type(exc).__name__}: {exc}")
            return
        if recorded.skipped:
            self._record_failed("; ".join(recorded.skipped))

    def _record_failed(self, detail: str) -> None:
        """Report a failed decision record at most every `RECORD_FAILURE_REPORT_SECONDS` (ADR 0012)."""
        try:
            now = self._now()
            self._record_failures += 1
            reported = self._record_failure_reported
            if reported is not None and timedelta(0) <= now - reported < timedelta(seconds=RECORD_FAILURE_REPORT_SECONDS):
                return
            suppressed, self._record_failures, self._record_failure_reported = self._record_failures - 1, 0, now
            self._log(f"review dispatch decisions not recorded ({suppressed} earlier failures unreported): "
                      f"{detail[:512]}")
        except Exception as exc:  # noqa: BLE001 - the report is best effort
            self._log(f"review dispatch decision failure not reported: {type(exc).__name__}: {exc}"[:600])

    def _plan_one(
        self, pr, key: WorkflowKey, observed: ObservedPullRequest | PullRequestUnavailable, cfg: ReviewConfig,
        unproductive_rounds: int, owed: dict[int, tuple[AgentKind, WorkflowBinding]], errors: dict[int, str],
        held: dict[int, str], external: dict[int, tuple[str, ...]],
    ) -> RecordedDispatch:
        """One PR's plan entry, in the order the daemon acts on it, as the decision to record.

        The workflow's own holds (gates, an unresolved submission, a pending
        release relabel) come before label candidacy: the needs-human-review a
        ceiling projects is that gate's hold, not one outside the workflow. A
        PR outside review candidacy never gets a workflow created, and its
        unreadable facts are not dispatch errors (nothing would be dispatched).
        """
        if pr.state.upper() != "OPEN":
            # Terminal PRs ride in the review world only for session cleanup.
            if isinstance(observed, ObservedPullRequest) and observed.state is not PullRequestState.OPEN:
                return ReviewTerminal(observed.state)
            return ReviewDeferred("the pull request state is unavailable")
        candidacy = review_candidacy(pr, cfg)
        excluded = candidacy.holds if isinstance(candidacy, ReviewExcluded) else None
        if isinstance(observed, PullRequestUnavailable):
            reason = observed.reason or "pull request read unavailable"
            if excluded is None:
                errors[pr.number] = reason
            return ReviewDeferred(reason)
        state = self.control.load_workflow(key)
        if isinstance(state, WorkflowAbsent):
            if excluded is not None:
                return ReviewNotAdmitted(excluded)
            if not self.control.first_observation_due(key):
                # Its schedule, not every tick, decides the next first read.
                held[pr.number] = "first observation backing off"
                return ReviewDeferred(held[pr.number])
            created = self.control.ensure_observed_workflow(key)
            if isinstance(created, PullRequestUnavailable):
                errors[pr.number] = created.reason or "pull request read unavailable"
                return ReviewDeferred(errors[pr.number])
            state = self.control.load_workflow(key)
        if not isinstance(state, ReviewWorkflowSnapshot):
            reason = (state.reason if isinstance(state, WorkflowUnavailable) else "") or "review workflow is not readable"
            if excluded is None:
                errors[pr.number] = reason
            return ReviewDeferred(reason)
        if not isinstance(state.execution, CurrentReviewExecution):
            reason = "review_execution_migration_required"
            held[pr.number] = reason
            return ReviewDeferred(reason)
        recovery_hold = self.execution.pending_recovery_hold(key)
        if recovery_hold is not None:
            held[pr.number] = recovery_hold
            return ReviewDeferred(recovery_hold)
        if not self.evidence_due(state.binding, observed.head_sha):
            held[pr.number] = "evidence read backing off"
            return ReviewDeferred(held[pr.number])
        external[pr.number] = self._scheduled_external_holds(observed, pr)
        decision = self.control.dispatch_decision(observed, patch_id=None)
        if isinstance(decision, (ReviewHeld, SubmissionUnresolved, ReleaseProjectionPending)):
            if excluded is None:
                held[pr.number] = type(decision).__name__
            return decision
        if excluded is not None:
            return ReviewNotAdmitted(excluded)
        if external[pr.number]:
            held[pr.number] = "; ".join(external[pr.number])
            return ReviewNotAdmitted(external[pr.number])
        if isinstance(decision, ReviewOwed):
            kind = AgentKind.REVIEW if decision.kind is ReviewKind.INITIAL else AgentKind.RE_REVIEW
            owed[pr.number] = (kind, state.binding)
            admission = review_eligibility(pr, cfg, unproductive_rounds)
            return ReviewNotAdmitted(admission.holds) if isinstance(admission, ReviewExcluded) else decision
        return decision

    def _basis(
        self, observed: ObservedPullRequest | PullRequestUnavailable, unproductive_rounds: int, cfg: ReviewConfig,
        external_holds: tuple[str, ...] | None = None,
    ) -> DecisionBasis | None:
        if not isinstance(observed, ObservedPullRequest):
            return None
        try:
            return DecisionBasis(observed.head_sha, observed.state, observed.labels,
                                 external_holds if external_holds is not None else
                                 trusted_external_holds(observed.labels, unproductive_rounds,
                                                        cfg.max_unproductive_rounds))
        except ValueError:
            return None

    def _plan_departed(self, world, cfg: ReviewConfig) -> tuple[DecisionEntry, ...]:
        """Read pull requests the review read no longer returns, on their bounded schedule, and decide where they went.

        The review read lists trigger-labelled (and, when adopting, all open)
        pull requests, so a held pull request whose trigger was swapped for a
        hold, or one closed since, leaves it. Only a read decides it is closed.
        A failed read backs off exponentially (`record_departed_read_failure`).
        Remote failures back off per target. Local authority failures are
        reported separately and cannot advance the remote failure schedule.
        """
        try:
            numbers = self.control.departed_pull_requests(
                frozenset(pr.number for pr in world.prs), limit=MAX_DEPARTED_READS)
        except Exception as exc:  # noqa: BLE001 - see docstring
            self._record_failed(f"departed pull request reads deferred: {type(exc).__name__}: {exc}")
            return ()
        entries: list[DecisionEntry] = []
        for number in numbers:
            key = self._key(number)
            try:
                entries.append(self._departed_entry(key, cfg))
            except (sqlite3.Error, ControlStoreError, WorkflowAuthorityError) as exc:
                self._record_failed(f"departed pull request control unavailable: {type(exc).__name__}: {exc}")
            except Exception as exc:  # noqa: BLE001 - see docstring
                entries.append(DepartedReadFailed(key, f"its facts are unavailable: {type(exc).__name__}: {exc}"[:512]))
        return tuple(entries)

    def _departed_entry(self, key: WorkflowKey, cfg: ReviewConfig) -> DecisionEntry:
        observed, view = read_pull_request(self._gateway, key, self._now(), reviews=False)
        if not isinstance(observed, ObservedPullRequest):
            return DepartedReadFailed(key, (
                f"missing from the review daemon's pull request read, and its own read failed: {observed.reason}"
            )[:512])
        if observed.state is not PullRequestState.OPEN:
            return DecisionUpdate(key, ReviewTerminal(observed.state),
                                  DecisionBasis(observed.head_sha, observed.state, observed.labels, ()),
                                  DecisionRead.DEPARTED_READ)
        try:
            holds = self._scheduled_external_holds(observed, view)
        except _LinkedIssueDeferred as exc:
            return DecisionUpdate(key, ReviewDeferred(str(exc)), None, DecisionRead.DEPARTED_READ)
        basis = DecisionBasis(observed.head_sha, observed.state, observed.labels, holds)
        return DecisionUpdate(key, self._departed_decision(key, observed, view, cfg, holds), basis,
                              DecisionRead.DEPARTED_READ)

    def _departed_decision(
        self, key: WorkflowKey, observed: ObservedPullRequest, view, cfg: ReviewConfig, holds: tuple[str, ...],
    ) -> RecordedDispatch:
        """As `_plan_one` decides, for a PR the review read no longer lists: workflow holds before candidacy."""
        if observed.state is not PullRequestState.OPEN:
            return ReviewTerminal(observed.state)
        decision = self.control.dispatch_decision(observed, patch_id=None)
        if isinstance(decision, (ReviewHeld, SubmissionUnresolved, ReleaseProjectionPending)):
            return decision
        if holds:
            return ReviewNotAdmitted(holds)
        candidacy = review_candidacy(view, cfg)
        if isinstance(candidacy, ReviewExcluded):
            return ReviewNotAdmitted(candidacy.holds)
        return ReviewDeferred("open, but missing from the review daemon's pull request read; it is decided when "
                              "that read returns it")

    # -- ReviewAuthority (the dispatching review's side) -------------------------------------

    def _scheduled_external_holds(self, observed: ObservedPullRequest, view: object) -> tuple[str, ...]:
        key, head = observed.key, observed.head_sha
        kind = ReviewEvidenceKind.LINKED_ISSUE
        if not self.control.evidence_due(key, head, kind):
            raise _LinkedIssueDeferred("linked issue evidence read backing off")
        try:
            holds = self.control.external_holds(observed, view)
        except ReviewLinkedIssueUnavailable as exc:
            self.control.record_evidence(key, head, kind, error=str(exc)[:512] or "linked issue unavailable")
            raise _LinkedIssueDeferred(f"linked issue unavailable: {exc}"[:512]) from exc
        self.control.record_evidence(key, head, kind, error=None)
        return holds

    def begin(self, binding: WorkflowBinding, head_sha: str, *, expected_claim_target: int) -> BoundReviewRound | None:
        key = binding.fence.key
        if key.repository != self.repository or not isinstance(key.target, PullRequestRef):
            raise WorkflowAuthorityError("the review dispatch binding belongs to another repository")
        observed, view = read_pull_request(self._gateway, key, self._now(), reviews=True)
        if not isinstance(observed, ObservedPullRequest):
            self.control.record_evidence(key, head_sha, ReviewEvidenceKind.REVIEW_HISTORY,
                                         error=observed.reason or "pull request read unavailable")
            raise WorkflowAuthorityError(f"pull request read unavailable: {observed.reason}")
        if observed.head_sha != head_sha:
            raise WorkflowAuthorityError("the pull request head moved during dispatch")
        if breaker_key(view) != expected_claim_target:
            raise WorkflowAuthorityError("the pull request issue linkage changed during dispatch")
        if isinstance(observed.reviews, PartialReviews):
            # Checked before any paid leg: a partial read can neither count the
            # ceiling for the verdict nor prove the head is unreviewed. The
            # next attempt waits for the bounded schedule, which gates visibly.
            self.control.record_evidence(key, head_sha, ReviewEvidenceKind.REVIEW_HISTORY,
                                         error=observed.reviews.reason or "review history incomplete")
            raise WorkflowAuthorityError(f"pull request reviews unavailable: {observed.reviews.reason}")
        self.control.record_evidence(key, head_sha, ReviewEvidenceKind.REVIEW_HISTORY, error=None)
        state = self.control.load_workflow(key)
        if not isinstance(state, ReviewWorkflowSnapshot) or state.binding != binding:
            raise WorkflowAuthorityError("the review dispatch binding is no longer current")
        if not isinstance(state.execution, CurrentReviewExecution):
            raise WorkflowAuthorityError("review execution is held")
        try:
            holds = self._scheduled_external_holds(observed, view)
        except _LinkedIssueDeferred as exc:
            raise WorkflowAuthorityError(str(exc)) from exc
        if holds:
            raise WorkflowAuthorityError("review dispatch held: " + "; ".join(holds))
        decision = self.control.dispatch_decision(observed, patch_id=None)
        if isinstance(decision, ReviewNotOwed):
            return None
        if not isinstance(decision, ReviewOwed):
            raise WorkflowAuthorityError(f"review dispatch held: {type(decision).__name__}")
        continuation = state.episode.continuation
        baseline = (continuation.head_sha if isinstance(continuation, ReviewedBaseline)
                    and continuation.reviewed_generation == binding.fence.generation else None)
        state = self.control.load_workflow(key)
        if not isinstance(state, ReviewWorkflowSnapshot) or not isinstance(state.execution, CurrentReviewExecution):
            raise WorkflowAuthorityError("review execution is no longer current")
        with self.control._control.transaction() as db:
            claim = db.execute("SELECT occurrence_id,leg FROM dispatch_claims "
                               "WHERE repository=? AND role='review' AND target=? AND owner_id=?",
                (self.repository, _dispatch_claim_target(expected_claim_target), self.control.writer.owner_id)).fetchone()
        if claim is None:
            raise WorkflowAuthorityError("review execution requires its exact parent claim")
        selection = ReviewPreflightSelection(
            f"review-preflight:{claim['occurrence_id']}", state.revision, state.execution.binding,
            claim['occurrence_id'], expected_claim_target, observed, self.control._trigger,
            self._mode, self._runtime, self._failure_ceiling,
        )
        plan = self.execution.preflight(selection)
        session = compatible_review_session(state.execution.state.session, self._runtime, RUNTIME_CAPABILITIES[self._runtime])
        return BoundReviewRound(binding, observed.head_sha, baseline, state.episode.guidance,
                                plan, expected_claim_target, AgentKind[claim['leg']], session, observed)

    def stage(self, bound: BoundReviewRound, kind: ReviewChildKind, names: tuple[str, ...]) -> tuple[ReviewChildIdentity, ...]:
        children = tuple(ReviewChildIdentity(bound.plan.round, kind.value, name, kind) for name in names)
        state = self.control.load_workflow(bound.binding.fence.key)
        if not isinstance(state, ReviewWorkflowSnapshot):
            raise WorkflowAuthorityError("review stage lacks its current aggregate")
        self.execution.register_stage(bound.plan.round.round_id, state.revision, ReviewStagePlan(kind.value, kind, children))
        return children

    def finish(self, bound: BoundReviewRound) -> None:
        state = self.control.load_workflow(bound.binding.fence.key)
        if not isinstance(state, ReviewWorkflowSnapshot):
            raise WorkflowAuthorityError("review completion lacks its current aggregate")
        with self.control._control.transaction() as db:
            stored = self.execution._stored_round(db, bound.plan.round.round_id)
        if stored is None:
            raise WorkflowAuthorityError("review completion lost its durable round")
        planning = decode_workflow(stored[3]["planning_json"], ReviewPlanningOpen | ReviewPlanningSealed)
        if isinstance(planning, ReviewPlanningOpen):
            self.execution.seal_completed_planning(bound.plan.round.round_id, state.revision)
        for submission in stored[2].submissions:
            self.control.settle_submission(CommandId(submission.command_id))
        self.execution.finalize_completed_round(bound.plan.round.round_id)

    def stop(self, bound: BoundReviewRound, conclusion: ReviewPlanningConclusion) -> None:
        with self.control._control.transaction() as db:
            stored = self.execution._stored_round(db, bound.plan.round.round_id)
        if stored is None:
            raise WorkflowAuthorityError("review stop lost its durable round")
        planning = decode_workflow(stored[3]["planning_json"], ReviewPlanningOpen | ReviewPlanningSealed)
        if isinstance(planning, ReviewPlanningSealed):
            return
        state = self.control.load_workflow(bound.binding.fence.key)
        if not isinstance(state, ReviewWorkflowSnapshot):
            raise WorkflowAuthorityError("review stop lacks its current aggregate")
        self.execution.seal_completed_planning(bound.plan.round.round_id, state.revision, conclusion)

    def _observed(self, bound: BoundReviewRound) -> ObservedPullRequest:
        observed = bound.observation
        if not isinstance(observed, ObservedPullRequest) or observed.key != bound.binding.fence.key:
            raise WorkflowAuthorityError("the review round carries no harness observation")
        return observed

    def patch_covered(self, bound: BoundReviewRound, patch_id: str) -> bool:
        decision = self.control.dispatch_decision(self._observed(bound), patch_id=patch_id)
        return isinstance(decision, ReviewNotOwed)

    def prior_rejections(self, bound: BoundReviewRound, non_resetting: frozenset[str]) -> int:
        try:
            reviews = decisive_reviews(self._observed(bound))
        except ValueError as exc:
            # A partial read can neither establish nor disprove the ceiling.
            raise WorkflowAuthorityError(f"review history incomplete: {exc}") from exc
        return len(counted_rejections(reviews, CountingRule.of(self._review_bot, frozenset(non_resetting))))

    def evidence_due(self, binding: WorkflowBinding, head_sha: str) -> bool:
        """Whether every read a review of this head depends on may be attempted now."""
        key = binding.fence.key
        return all(self.control.evidence_due(key, head_sha, kind) for kind in ReviewEvidenceKind if kind is not ReviewEvidenceKind.EXEMPTION_TRAILERS)

    async def submit(
        self, bound: BoundReviewRound, event: str, body: str, *, patch_id: str, escalate_cause: str | None,
        counted_past: frozenset[str] = frozenset(),
    ) -> PostedReview | None:
        """Submit a bound verdict or escalation; its settlement alone decides what it holds.

        `counted_past` is the set of current-generation approvals the verdict
        counted prior rejections past; a ceiling escalation records it so its
        gate is judged under the same rule.
        """
        pr = bound.binding.fence.key.target.number
        counting = tuple(sorted(counted_past)) if escalate_cause == "ceiling" else ()
        command = bound_review_command_id(BoundReviewSubmissionOp(
            pr, event, body, bound.head_sha, self._review_bot, patch_id, escalate_cause, counting), bound.binding)
        op = BoundReviewSubmissionOp(pr, event, body, bound.head_sha, self._review_bot, patch_id, escalate_cause, counting)
        state = self.control.load_workflow(bound.binding.fence.key)
        if not isinstance(state, ReviewWorkflowSnapshot):
            raise WorkflowAuthorityError("review submission lacks its current aggregate")
        with self.control._control.transaction() as db:
            stored = self.execution._stored_round(db, bound.plan.round.round_id)
        if stored is None:
            raise WorkflowAuthorityError("review submission lacks its durable round")
        pending = self.execution.register_submission(
            bound.plan.round.round_id, state.revision,
            LifecycleCommand(command, f"github:{command}", CommandKind.SUBMIT_REVIEW, f"pr:{pr}",
                             bound.head_sha, op.payload(), self._effects.config_version,
                             self.repository, self.control.writer.owner_id, workflow=bound.binding),
            ReviewSubmissionKind.PAID if stored[2].stages else ReviewSubmissionKind.COPIED,
        )
        try:
            landed = self._effects.execute_registered_review(pending)
        except UnknownEffectOutcome as exc:
            # The drain settles it; dispatch holds on the unresolved submission.
            raise WorkflowAuthorityError(f"review submission outcome unresolved: {exc}") from exc
        except sqlite3.Error:
            raise
        except Exception as exc:
            # A transport failure after the command was begun is an unresolved
            # durable submission (a hold the drain reconciles), not a death of
            # the paid round that produced the verdict.
            pending = self._effects.store.latest_command(f"github:{command}")
            if pending is not None and pending.state in _UNRESOLVED_STATES:
                raise WorkflowAuthorityError(f"review submission outcome unresolved: {exc}") from exc
            raise
        if not landed:
            return None
        # The outer driver closes planning before joint settlement. A crash
        # leaves the same registered command for existing recovery to settle.
        return posted_review(event, escalate_cause)
