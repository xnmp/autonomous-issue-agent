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
from dataclasses import dataclass
from datetime import datetime
import sqlite3

from ..adapters.control_store import ControlStoreError, SQLiteControlStore
from ..adapters.effects import GitHubEffectExecutor, UnknownEffectOutcome, bound_review_command_id
from ..adapters.review_effects import BoundReviewSubmissionOp, observe_pull_request, pull_request_observation
from ..adapters.review_workflow_control import SQLiteReviewWorkflowControl, read_review_guidance
from ..adapters.workflow_rows import WorkflowAuthorityError
from ..domain.actions import AgentKind
from ..domain.labels import Label
from ..domain.lifecycle import retry_breaker
from ..domain.fix import breaker_key
from ..domain.review import ReviewConfig, should_adopt
from ..domain.review_evidence import ReviewEvidenceKind
from ..domain.review_rejection import CountingRule
from ..domain.review_workflow import (
    ObservedPullRequest, PartialReviews, PullRequestUnavailable, ReviewedBaseline, ReviewWorkflowSnapshot,
    decisive_reviews,
)
from ..domain.review_workflow_policy import (
    ReviewKind, ReviewNotOwed, ReviewOwed, counted_rejections,
)
from ..domain.workflow import WorkflowAbsent, WorkflowUnavailable
from ..domain.workflow_identity import PullRequestRef, WorkflowBinding, WorkflowKey, WorkflowWriter
from .workflows.review import BoundReviewRound

# Labels only the harness (or a human acting on the lifecycle) applies. They
# are never derived from agent output or the PR body, so they are a trusted
# hold source for re-arm acceptance and completion (ADR 0012 provenance).
_TRUSTED_HOLD_LABELS = (Label.AI_BLOCKED, Label.AI_PARKED, Label.NEEDS_BUDGET_REVIEW)
_UNRESOLVED_STATES = frozenset({"executing", "unknown_outcome", "retryable_failure"})


def unproductive_round_keys(pr) -> frozenset[int]:
    """The counter keys one PR's unproductive review rounds are recorded under.

    New failures and recovered deaths use the stable `-PR` key; the linked
    issue key (`breaker_key`) remains only for counters written before that
    policy. Both the dispatch decision and the re-arm hold source sum exactly
    this set, so they can never disagree about a tripped breaker.
    """
    return frozenset({breaker_key(pr), -pr.number})


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
        found = tuple(label.value for label in _TRUSTED_HOLD_LABELS if label in observed.labels)
        if retry_breaker(unproductive_rounds(view), max_unproductive_rounds).tripped:
            found += ("unproductive review round ceiling",)
        return found

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
class ReviewDispatchPlan:
    """What the active workflow owes this tick, per PR, and what it could not read."""

    owed: Mapping[int, tuple[AgentKind, WorkflowBinding]]
    errors: Mapping[int, str]
    held: Mapping[int, str]


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

    def plan(self, world, cfg: ReviewConfig) -> ReviewDispatchPlan:
        """Per candidate PR: owed (with its binding), held, or unreadable.

        A PR the workflow has never seen is observed once (the initial
        workflow row). The decision uses the world's head, state and labels;
        its review list is not complete, so it is never used to count or
        dismiss; the dispatch re-decides from a complete read before paying.
        """
        owed: dict[int, tuple[AgentKind, WorkflowBinding]] = {}
        errors: dict[int, str] = {}
        held: dict[int, str] = {}
        now = self._now()
        for pr in world.prs:
            # Terminal PRs ride in the review world only for session cleanup.
            if pr.state.upper() != "OPEN" or not (pr.has(cfg.trigger_label) or should_adopt(pr, cfg)):
                continue
            key = self._key(pr.number)
            try:
                observed = pull_request_observation(key, pr, now, PartialReviews("world observation"))
                if isinstance(observed, PullRequestUnavailable):
                    errors[pr.number] = observed.reason
                    continue
                state = self.control.load_workflow(key)
                if isinstance(state, WorkflowAbsent):
                    if not self.control.first_observation_due(key):
                        # Its schedule, not every tick, decides the next first read.
                        held[pr.number] = "first observation backing off"
                        continue
                    created = self.control.ensure_observed_workflow(key)
                    if isinstance(created, PullRequestUnavailable):
                        errors[pr.number] = created.reason or "pull request read unavailable"
                        continue
                    state = self.control.load_workflow(key)
                if isinstance(state, WorkflowUnavailable):
                    errors[pr.number] = state.reason
                    continue
                if not isinstance(state, ReviewWorkflowSnapshot):
                    errors[pr.number] = "review workflow is not readable"
                    continue
                decision = self.control.dispatch_decision(observed, patch_id=None)
            except (WorkflowAuthorityError, ControlStoreError, ValueError) as exc:
                errors[pr.number] = str(exc)[:512] or type(exc).__name__
                continue
            if isinstance(decision, ReviewOwed):
                try:
                    due = self.evidence_due(state.binding, observed.head_sha)
                except (WorkflowAuthorityError, ControlStoreError, ValueError) as exc:
                    errors[pr.number] = str(exc)[:512] or type(exc).__name__
                    continue
                if not due:
                    # A backing-off read is decided by its schedule, before any read or paid leg.
                    held[pr.number] = "evidence read backing off"
                    continue
                kind = AgentKind.REVIEW if decision.kind is ReviewKind.INITIAL else AgentKind.RE_REVIEW
                owed[pr.number] = (kind, state.binding)
            elif not isinstance(decision, ReviewNotOwed):
                held[pr.number] = type(decision).__name__
        return ReviewDispatchPlan(owed, errors, held)

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

