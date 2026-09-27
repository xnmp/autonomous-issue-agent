"""The review daemon's runtime for an ACTIVE review-rejection family (ADR 0015).

A repository whose review family is not activated never reaches this module's
decisions: `ActiveReviewRuntime.active()` reads the family row every tick, so
activation is runtime capability detection rather than launch-time config,
and the legacy review path stays byte-for-byte the inactive behaviour.

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

from ..adapters.control_store import ControlStoreError, SQLiteControlStore
from ..adapters.effects import GitHubEffectExecutor, UnknownEffectOutcome, bound_review_command_id
from ..adapters.review_effects import (
    BoundReviewSubmissionOp, observe_pull_request, pull_request_observation, read_pull_request,
)
from ..adapters.review_workflow_control import SQLiteReviewWorkflowControl, read_review_guidance
from ..adapters.workflow_rows import WorkflowAuthorityError
from ..domain.actions import AgentKind
from ..domain.labels import Label
from ..domain.lifecycle import retry_breaker
from ..domain.fix import breaker_key
from ..domain.review import (
    UNPRODUCTIVE_ROUND_HOLD, ReviewConfig, ReviewExcluded, review_candidacy, review_eligibility,
)
from ..domain.review_dispatch_record import (
    MAX_DEPARTED_READS, AdmissionOutcome, DecisionBasis, DecisionEntry, DecisionRead, DecisionUpdate,
    DepartedReadFailed, RecordedDispatch, ReviewDeferred, ReviewNotAdmitted, ReviewTerminal, admitted_decision,
)
from ..domain.review_evidence import ReviewEvidenceKind
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


def unproductive_round_keys(pr) -> frozenset[int]:
    """The counter keys one PR's unproductive review rounds are recorded under.

    New failures and recovered deaths use the stable `-PR` key; the linked
    issue key (`breaker_key`) remains only for counters written before that
    policy. Both the dispatch decision and the re-arm hold source sum exactly
    this set, so they can never disagree about a tripped breaker.
    """
    return frozenset({breaker_key(pr), -pr.number})


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
) -> Callable[[ObservedPullRequest, object], tuple[str, ...]]:
    """Trusted holds a review re-arm must respect beside its own ceiling gate.

    Sources: the harness-owned hold labels on the observed PR and the
    unmigrated unproductive-round breaker (the review role's own counter),
    read for the pull request view of the same harness read. A counter read
    failure, or a read with no view, raises, which the control records as an
    unavailable observation: never permission to clear.
    """
    def holds(observed: ObservedPullRequest, view: object) -> tuple[str, ...]:
        if getattr(view, "number", None) != observed.key.target.number:
            raise ValueError("the unproductive-round counter needs this read's pull request view")
        return trusted_external_holds(observed.labels, unproductive_rounds(view), max_unproductive_rounds)

    return holds


def compose_review_control(
    control: SQLiteControlStore, gateway: object, *, repository: str, owner_id: str, ceiling: int,
    trigger_label: Label, review_bot: str,
    external_holds: Callable[[ObservedPullRequest, object], tuple[str, ...]],
    trailers: Callable[[int], object] | None,
) -> SQLiteReviewWorkflowControl:
    return SQLiteReviewWorkflowControl(
        control, WorkflowWriter(repository, "review", owner_id), gateway=gateway, ceiling=ceiling,
        trigger_label=trigger_label, rule=CountingRule(review_bot), external_holds=external_holds,
        trailers=trailers,
    )


def configured_review_control(
    cfg: HarnessConfig, control: SQLiteControlStore, gateway: object, store, owner_id: str,
) -> SQLiteReviewWorkflowControl:
    """The review owner's workflow authority as configured for this deployment.

    The review daemon and the local operator transport both compose it here,
    so an operator re-arm is accepted under exactly the ceiling, trigger,
    counting rule and trusted hold sources the daemon enforces.
    """
    review = cfg.review_runtime
    trailers = getattr(gateway, "pr_commit_messages", None)
    return compose_review_control(
        control, gateway, repository=cfg.repo, owner_id=owner_id, ceiling=review.max_rejections,
        trigger_label=Label(review.trigger_label), review_bot=review.bot,
        external_holds=review_external_holds(
            unproductive_rounds_reader(store, lambda: control.abandoned_dispatch_counts(owner_id)),
            review.max_unproductive_rounds,
        ),
        trailers=trailers if callable(trailers) else None,
    )


def review_guidance_reader(
    control: SQLiteControlStore, repository: str, legacy: Callable[[int], str],
) -> Callable[[int], str]:
    """The fix role's read-only guidance source for PR repairs.

    Active family: the committed review episode's guidance; an unreadable
    authority raises `WorkflowAuthorityError`, which defers the repair.
    Inactive: the legacy per-PR record, unchanged.
    """
    def read(pr: int) -> str:
        guidance = read_review_guidance(control, repository, pr)
        return legacy(pr) if guidance is None else guidance

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
        review_bot: str, log: Callable[[str], None],
    ) -> None:
        self.control = control
        self._effects = effects
        self._gateway = gateway
        self._review_bot = review_bot
        self._log = log
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
            ("submissions", self.control.reconcile_submissions),
            ("re-arms", self.control.reconcile_pending_rearms),
            ("holds", self.control.reconcile_holds),
        ):
            try:
                progressed += step()
            except (WorkflowAuthorityError, ControlStoreError, ValueError) as exc:
                self._log(f"review workflow {name} reconciliation deferred: {exc}")
        return progressed

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
        now = self._now()
        for pr in world.prs:
            key = self._key(pr.number)
            observed = pull_request_observation(key, pr, now, PartialReviews("world observation"))
            basis = self._basis(observed, rounds.get(pr.number, 0), cfg)
            recorded: RecordedDispatch
            try:
                recorded = self._plan_one(pr, key, observed, cfg, rounds.get(pr.number, 0), owed, errors, held)
            except (WorkflowAuthorityError, ControlStoreError, ValueError) as exc:
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
            self.control.journal_decision_record_failure(detail[:512], suppressed)
        except Exception as exc:  # noqa: BLE001 - the report is best effort; the log line above already ran
            self._log(f"review dispatch decision failure not journalled: {type(exc).__name__}: {exc}"[:600])

    def _plan_one(
        self, pr, key: WorkflowKey, observed: ObservedPullRequest | PullRequestUnavailable, cfg: ReviewConfig,
        unproductive_rounds: int, owed: dict[int, tuple[AgentKind, WorkflowBinding]], errors: dict[int, str],
        held: dict[int, str],
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
        decision = self.control.dispatch_decision(observed, patch_id=None)
        if isinstance(decision, (ReviewHeld, SubmissionUnresolved, ReleaseProjectionPending)):
            if excluded is None:
                held[pr.number] = type(decision).__name__
            return decision
        if excluded is not None:
            return ReviewNotAdmitted(excluded)
        if isinstance(decision, ReviewOwed):
            if not self.evidence_due(state.binding, observed.head_sha):
                # A backing-off read is decided by its schedule, before any read or paid leg.
                held[pr.number] = "evidence read backing off"
                return ReviewDeferred(held[pr.number])
            kind = AgentKind.REVIEW if decision.kind is ReviewKind.INITIAL else AgentKind.RE_REVIEW
            owed[pr.number] = (kind, state.binding)
            admission = review_eligibility(pr, cfg, unproductive_rounds)
            return ReviewNotAdmitted(admission.holds) if isinstance(admission, ReviewExcluded) else decision
        return decision

    def _basis(
        self, observed: ObservedPullRequest | PullRequestUnavailable, unproductive_rounds: int, cfg: ReviewConfig,
    ) -> DecisionBasis | None:
        if not isinstance(observed, ObservedPullRequest):
            return None
        try:
            return DecisionBasis(observed.head_sha, observed.state, observed.labels,
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
        These reads feed only the operator view, so no failure here leaves
        this method: it becomes that pull request's failed read.
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
            except Exception as exc:  # noqa: BLE001 - see docstring
                entries.append(DepartedReadFailed(key, f"its facts are unavailable: {type(exc).__name__}: {exc}"[:512]))
        return tuple(entries)

    def _departed_entry(self, key: WorkflowKey, cfg: ReviewConfig) -> DecisionEntry:
        observed, view = read_pull_request(self._gateway, key, self._now(), reviews=False)
        if not isinstance(observed, ObservedPullRequest):
            return DepartedReadFailed(key, (
                f"missing from the review daemon's pull request read, and its own read failed: {observed.reason}"
            )[:512])
        holds = self.control.external_holds(observed, view)
        basis = DecisionBasis(observed.head_sha, observed.state, observed.labels, holds)
        return DecisionUpdate(key, self._departed_decision(key, observed, view, cfg), basis, DecisionRead.DEPARTED_READ)

    def _departed_decision(
        self, key: WorkflowKey, observed: ObservedPullRequest, view, cfg: ReviewConfig,
    ) -> RecordedDispatch:
        """As `_plan_one` decides, for a PR the review read no longer lists: workflow holds before candidacy."""
        if observed.state is not PullRequestState.OPEN:
            return ReviewTerminal(observed.state)
        decision = self.control.dispatch_decision(observed, patch_id=None)
        if isinstance(decision, (ReviewHeld, SubmissionUnresolved, ReleaseProjectionPending)):
            return decision
        candidacy = review_candidacy(view, cfg)
        if isinstance(candidacy, ReviewExcluded):
            return ReviewNotAdmitted(candidacy.holds)
        return ReviewDeferred("open, but missing from the review daemon's pull request read; it is decided when "
                              "that read returns it")

    # -- ReviewAuthority (the dispatching review's side) -------------------------------------

    def begin(self, binding: WorkflowBinding, head_sha: str) -> BoundReviewRound | None:
        key = binding.fence.key
        if key.repository != self.repository or not isinstance(key.target, PullRequestRef):
            raise WorkflowAuthorityError("the review dispatch binding belongs to another repository")
        observed = observe_pull_request(self._gateway, key, self._now(), reviews=True)
        if not isinstance(observed, ObservedPullRequest):
            self.control.record_evidence(key, head_sha, ReviewEvidenceKind.REVIEW_HISTORY,
                                         error=observed.reason or "pull request read unavailable")
            raise WorkflowAuthorityError(f"pull request read unavailable: {observed.reason}")
        if observed.head_sha != head_sha:
            raise WorkflowAuthorityError("the pull request head moved during dispatch")
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
        decision = self.control.dispatch_decision(observed, patch_id=None)
        if isinstance(decision, ReviewNotOwed):
            return None
        if not isinstance(decision, ReviewOwed):
            raise WorkflowAuthorityError(f"review dispatch held: {type(decision).__name__}")
        continuation = state.episode.continuation
        baseline = (continuation.head_sha if isinstance(continuation, ReviewedBaseline)
                    and continuation.reviewed_generation == binding.fence.generation else None)
        return BoundReviewRound(binding, observed.head_sha, baseline, state.episode.guidance, observed)

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
        return all(self.control.evidence_due(key, head_sha, kind) for kind in ReviewEvidenceKind)

    def record_evidence(self, bound: BoundReviewRound, error: str | None) -> None:
        self.control.record_evidence(bound.binding.fence.key, bound.head_sha, ReviewEvidenceKind.EXEMPTION_TRAILERS,
                                     error=error)

    def submit(
        self, bound: BoundReviewRound, event: str, body: str, *, patch_id: str, escalate_cause: str | None,
        counted_past: frozenset[str] = frozenset(),
    ) -> bool:
        """Submit a bound verdict or escalation; its settlement alone decides what it holds.

        `counted_past` is the set of current-generation approvals the verdict
        counted prior rejections past; a ceiling escalation records it so its
        gate is judged under the same rule.
        """
        pr = bound.binding.fence.key.target.number
        counting = tuple(sorted(counted_past)) if escalate_cause == "ceiling" else ()
        command = bound_review_command_id(BoundReviewSubmissionOp(
            pr, event, body, bound.head_sha, self._review_bot, patch_id, escalate_cause, counting), bound.binding)
        try:
            landed = self._effects.submit_bound_review(
                pr, event, body, bound.head_sha, workflow=bound.binding, reviewer=self._review_bot,
                patch_id=patch_id, escalate_cause=escalate_cause, non_resetting_commits=counting,
            )
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
            return False
        try:
            self.control.settle_submission(command)
        except (WorkflowAuthorityError, ControlStoreError) as exc:
            # The receipt is durable; the reconciliation pass applies it.
            self._log(f"review #{pr}: submission landed; settlement deferred ({exc})")
        return True


def unproductive_rounds_reader(store, abandoned: Callable[[], Mapping[int, int]]) -> Callable[[object], int]:
    """The review role's unproductive-round counter for one PR view, over `unproductive_round_keys`."""
    def read(pr: object) -> int:
        deaths = abandoned()
        return sum(store.get_infra_deaths(key) + deaths.get(key, 0) for key in unproductive_round_keys(pr))

    return read

