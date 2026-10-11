"""Generation-fenced review workflow under ADR 0015.

Owns single, delegated, and multi-agent review dispatch outside the central
AgentDispatcher so review behavior can evolve behind a role-specific module.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Protocol

from ...domain import review as review_dom
from ...domain.events import (
    Correlation,
    TelemetryEvent,
    TelemetryWarning,
)
from ...domain.leg import Leg, LegTarget
from ...domain.trust import Untrusted, as_untrusted, untrusted_block
from ..planners import tasks as task_builders
from ..planners.tasks import ReviewTaskConfig
from ...adapters.github import format_pr_discussion
from ...adapters.runtime_plan import AgentRuntime, ContextualRuntime
from ...domain.actions import AgentKind
from ...domain.review_execution import NoReviewSession, ReusableReviewSession, ReviewSession, ReviewChildIdentity, ReviewChildKind, ReviewPinDeferred
from ...domain.review_rounds import ReviewPlanningConclusion, ReviewPlanningRefused, ReviewRoundPlan
from ...domain.agent_task import (
    RUNTIME_CAPABILITIES,
    RuntimeCapabilities,
    RuntimeName,
)
from ...domain.gate import modified_existing_tests_from_diff
from ...domain.fix import breaker_key
from ...domain.identity import login_eq
from ...domain.refs import issue_from_branch
from ...domain.outcome import AgentResult
from ...domain.ports import (
    EventSink,
    WorkflowGitHubReads,
    WorkflowState,
)
from ...domain.workflow_identity import PullRequestRef, WorkflowBinding, WorkflowFamily
from ...adapters.workflow_rows import WorkflowAuthorityError
from .base import run_leg

# A round that could not PIN its checkout never ran a paid leg — it must defer
# (retry next poll) without feeding the unproductive-round counter, or five
# free deferrals walk a healthy PR into needs-human-review (0d2e295 H3).
_PIN_DEFERRED_STATUS = "review_checkout_deferred"

@dataclass(frozen=True)
class _PatchShortcut:
    """Outcome of the mechanical-rebase shortcut."""

    settled: bool = False


def _pin_deferred(result) -> bool:
    return bool(result.is_error) and result.status == _PIN_DEFERRED_STATUS


@dataclass(frozen=True)
class BoundReviewRound:
    """One dispatch of an ACTIVE review family's owed review (ADR 0015).

    Minted by the review authority from a fresh harness read at execution:
    `binding` is the generation the paid legs and the verdict are fenced to;
    `baseline_head` is the same-generation reviewed head a mechanical rebase
    may copy from (None when a fresh review is required); `guidance` is the
    committed episode guidance, the only guidance a bound review reads.
    """

    binding: WorkflowBinding
    head_sha: str
    baseline_head: str | None
    guidance: str
    plan: ReviewRoundPlan
    claim_target: int
    kind: AgentKind
    session: ReviewSession = NoReviewSession()
    observation: object = field(compare=False, repr=False, default=None)


from ...domain.review_test_evidence import PostedReview


class ReviewAuthority(Protocol):
    """The review workflow authority a bound review consults and submits through."""

    def begin(self, binding: WorkflowBinding, head_sha: str, *, expected_claim_target: int) -> BoundReviewRound | None:
        """The round, None when nothing is owed; raises `WorkflowAuthorityError` when held."""
        ...

    def patch_covered(self, round: BoundReviewRound, patch_id: str) -> bool: ...

    def stage(self, round: BoundReviewRound, kind: ReviewChildKind, names: tuple[str, ...]) -> tuple[ReviewChildIdentity, ...]: ...

    def finish(self, round: BoundReviewRound) -> None: ...

    def stop(self, round: BoundReviewRound, conclusion: ReviewPlanningConclusion) -> None: ...

    def prior_rejections(self, round: BoundReviewRound, non_resetting: frozenset[str]) -> int: ...

    def evidence_due(self, binding: WorkflowBinding, head_sha: str) -> bool:
        """Whether every harness read a review of this head needs is off its backoff."""
        ...

    def test_due(self, round: BoundReviewRound, patch_id: str) -> bool: ...

    async def submit(
        self, round: BoundReviewRound, event: str, body: str, *, patch_id: str, escalate_cause: str | None,
        counted_past: frozenset[str] = frozenset(),
    ) -> PostedReview | None:
        """The posted outcome once the submission's receipt landed; its settlement decides any hold."""
        ...


@dataclass(frozen=True)
class ReviewWorkflowConfig:
    review: ReviewTaskConfig | None
    review_mode: review_dom.ReviewMode
    max_review_rejections: int
    review_bot: str
    review_trigger_label: str
    log: Callable[[str], None]
    reviewer_runtime: RuntimeName = RuntimeName.CLAUDE
    reviewer_capabilities: RuntimeCapabilities = RUNTIME_CAPABILITIES[
        RuntimeName.CLAUDE
    ]
    # Codex ignores RuntimePlan's SDK-only tool policy, so it must run only after
    # its read-only governor is installed in this explicit checkout.  An empty
    # cwd is a containment failure, never permission to fall back to os.getcwd().
    execution_cwd: str = ""
    prepare_cwd: Callable[[str], None] | None = None
    journal: EventSink | None = None
    # Pinned per-PR review checkout: (pr, head_sha) → abs path whose HEAD is
    # exactly head_sha, or raise. When set, review legs never execute in the
    # operator's live checkout — on-disk reads there see whatever branch and
    # dirty state the operator has (keifu #165, 2026-08-01: three phantom
    # REQUEST_CHANGES rounds quoting stale chong-dev code under a fresh SHA).
    # A failure to pin DEFERS the round; reviewing unpinned code is worse than
    # waiting a poll.
    review_checkout: Callable[[int, str], str] | None = None
    review_checkout_cleanup: Callable[[int], None] | None = None

    def __post_init__(self) -> None:
        runtime = (
            self.reviewer_runtime
            if isinstance(self.reviewer_runtime, RuntimeName)
            else RuntimeName(self.reviewer_runtime)
        )
        object.__setattr__(self, "reviewer_runtime", runtime)
        object.__setattr__(
            self, "reviewer_capabilities", RUNTIME_CAPABILITIES[runtime]
        )


@dataclass
class ReviewWorkflow:
    cfg: ReviewWorkflowConfig
    gateway: WorkflowGitHubReads
    store: WorkflowState
    runner: AgentRuntime
    log_run: Callable[[str, int, object], Awaitable[object]] | None = None
    # Missing composition remains representable so dispatch can report a
    # migration-required refusal before performing reads or paid work.
    authority: ReviewAuthority | None = None

    def _claim_matches(self, pr, claim_target: int | None) -> bool:
        """Enforce ADR 0008's observation-to-admission target fence."""
        if claim_target is None:
            return True
        fresh_target = breaker_key(pr)
        if fresh_target == claim_target:
            return True
        self.cfg.log(
            f"review #{pr.number}: deferred — observed target {claim_target} "
            f"changed to {fresh_target} before execution"
        )
        # Spend-safe but otherwise silent: a persistent linkage flap re-defers
        # every tick, so surface it in the dashboard's per-issue event view
        # (ADR 0008).
        if self.cfg.journal is not None:
            try:
                self.cfg.journal.emit(
                    TelemetryEvent(
                        TelemetryWarning(
                            "dispatch_deferred",
                            f"observed target {claim_target} changed to "
                            f"{fresh_target} before execution",
                            {
                                "observed_target": claim_target,
                                "fresh_target": fresh_target,
                            },
                        ),
                        Correlation(pr=pr.number),
                    )
                )
            except Exception as exc:  # noqa: BLE001 — telemetry must not block
                self.cfg.log(
                    f"review #{pr.number}: deferral not journalled ({exc})"
                )
        return False

    def _no_progress(self, pr, reason: str) -> None:
        """Finalization classifies the outer outcome from durable child evidence."""
        self.cfg.log(f"review #{pr.number}: round bought nothing ({reason})")

    def _pin_deferral(self, p, result) -> bool:
        """Journal the harness stop; Current SQL owns its bounded deferral streak."""
        if not _pin_deferred(result):
            return False
        if self.cfg.journal is not None:
            try:
                self.cfg.journal.emit(
                    TelemetryEvent(
                        TelemetryWarning(
                            "review_pin_deferred",
                            f"review #{p.number}: pinned checkout at "
                            f"{p.head_ref_oid[:12] or '?'} unavailable — "
                            "durable round deferred",
                            {"head": p.head_ref_oid},
                        ),
                        Correlation(pr=p.number),
                    )
                )
            except Exception as exc:  # noqa: BLE001 — telemetry must not block
                self.cfg.log(
                    f"review #{p.number}: pin deferral not journalled ({exc})"
                )
        return True

    async def _run_logged(self, label: str, pr: int, task, head_sha: str = "", *,
                          bound: BoundReviewRound, child: ReviewChildIdentity):
        task = self._prepare_task(task, pr, head_sha)
        if isinstance(task, AgentResult):
            assert self.authority is not None
            self.authority.stop(bound, ReviewPinDeferred("harness-pin:" + bound.plan.round.round_id))
            return task
        if task is None:
            assert self.authority is not None
            self.authority.stop(bound, ReviewPlanningRefused("harness-preparation:" + bound.plan.round.round_id))
            return AgentResult(
                is_error=True,
                status="containment_unavailable",
                summary="Codex review needs an explicit TARGET_DIR for its governor hook",
            )
        if isinstance(self.runner, ContextualRuntime):
            self.runner.set_context("review", bound.kind.name, pr,
                                    bound.claim_target if bound.claim_target > 0 else None,
                                    claim_target=bound.claim_target, workflow=bound.binding, review_child=child)
        if self.log_run is not None:
            return await self.log_run(label, pr, task)
        return await run_leg(
            self.runner,
            self.cfg.log,
            Leg(
                label.upper().replace("-", "_").replace(":", "_"),
                LegTarget.PR,
                pr,
                True,
                ("global", "repository", "issue"),
            ),
            task,
        )

    def _prepare_task(self, task, pr: int = 0, head_sha: str = ""):
        """Pin the leg's cwd to the reviewed head, then install Codex's review
        governor immediately before each agent leg.

        A builder-time write can mutate whichever checkout happens to run a
        test, including an in-flight fixer worktree — so the hook install
        happens here, into the same directory the leg will execute in. That
        directory is the per-PR pinned checkout when configured (the verdict
        binds `head_sha`; the code the reviewer reads must be that SHA, not
        the operator's live checkout), else the configured target checkout.
        """
        cwd = self.cfg.execution_cwd
        if self.cfg.review_checkout is not None and pr:
            if not head_sha:
                # Never silently fall back to the live checkout: an empty head
                # SHA means the observation was degenerate — defer, re-observe
                # (0d2e295 M2).
                self.cfg.log(
                    f"review #{pr}: no head SHA to pin the checkout to — "
                    "deferring this round"
                )
                return AgentResult(
                    is_error=True,
                    status=_PIN_DEFERRED_STATUS,
                    summary=f"review #{pr}: no head SHA to pin",
                )
            try:
                cwd = self.cfg.review_checkout(pr, head_sha)
            except Exception as exc:  # noqa: BLE001 — stale review is the worse outcome
                self.cfg.log(
                    f"review #{pr}: pinned checkout at {head_sha[:12]} "
                    f"unavailable ({exc}) — deferring this round"
                )
                return AgentResult(
                    is_error=True,
                    status=_PIN_DEFERRED_STATUS,
                    summary=f"review #{pr}: pinned checkout unavailable",
                )
        if not self.cfg.reviewer_capabilities.requires_external_governor:
            return replace(task, cwd=cwd) if cwd else task
        if not cwd or self.cfg.prepare_cwd is None:
            self.cfg.log(
                "review: Codex containment unavailable — set TARGET_DIR to the "
                "review checkout; refusing to run without a governor hook"
            )
            return None
        try:
            self.cfg.prepare_cwd(cwd)
        except OSError as exc:
            self.cfg.log(
                f"review: Codex containment setup failed in {cwd}: {exc}; "
                "refusing to run"
            )
            return None
        return replace(task, cwd=cwd)

    async def review(
        self, pr: int, *, claim_target: int | None = None,
        binding: WorkflowBinding | None = None,
    ) -> None:
        self._require_authority(binding, pr)
        p = self.gateway.get_pr(pr)
        if not self._claim_matches(p, claim_target):
            return
        assert binding is not None
        bound = self._begin_bound(p, binding)
        if bound is None:
            return
        assert self.authority is not None
        try:
            await self._review_selected(pr, p, bound, claim_target)
        finally:
            self.authority.finish(bound)

    async def _review_selected(self, pr: int, p, bound: BoundReviewRound, claim_target: int | None) -> None:
        if self.cfg.review_mode is review_dom.ReviewMode.MULTI:
            await self._review_multi(
                pr,
                round_number=bound.plan.displayed_round,
                p=p,
                claim_target=claim_target,
                bound=bound,
            )
            return
        # SINGLE and DELEGATED share the submit-before-persist contract; they differ
        # only in the task builder (delegated attaches a code-scout subagent).
        diff = self._revision_bound_diff(p)
        if diff is None:
            self._no_progress(p, "revision-bound diff unavailable")
            assert self.authority is not None
            self.authority.stop(bound, ReviewPlanningRefused("harness-diff:" + bound.plan.round.round_id))
            return
        signature = review_dom.patch_signature(diff)
        assert self.authority is not None
        if not self.authority.test_due(bound, signature):
            self.authority.stop(bound, ReviewPlanningRefused("ordinary-test-backoff:" + bound.plan.round.round_id))
            return
        guidance = bound.guidance
        discussion = self._pr_discussion(p)
        if self.cfg.review_mode is review_dom.ReviewMode.DELEGATED:
            task = task_builders.build_delegated_review_task(
                pr,
                as_untrusted(p.title),
                as_untrusted(p.body),
                as_untrusted(diff),
                self.cfg.review,
                operator_guidance=guidance,
                discussion=discussion,
            )
        else:
            task = task_builders.build_review_task(
                pr,
                as_untrusted(p.title),
                as_untrusted(p.body),
                as_untrusted(diff),
                self.cfg.review,
                operator_guidance=guidance,
                discussion=discussion,
            )
        task = self._with_test_changes(task, diff)
        assert self.authority is not None
        child = self.authority.stage(bound, ReviewChildKind.REVIEWER, ("review",))[0]
        result = await self._run_logged("review", pr, task, p.head_ref_oid, bound=bound, child=child)
        if self._pin_deferral(p, result):
            return
        if result.is_error:
            self.cfg.log(
                f"review #{pr}: agent errored (status={result.status or 'error'}, "
                f"cost=${result.cost_usd:.2f}) — no verdict, will retry. If cost is "
                f"near the budget cap, raise REVIEW_BUDGET_USD (delegated review + "
                f"scout subagents share one budget)."
            )
            self._no_progress(p, f"agent error: {result.status or 'error'}")
            return
        # Submit BEFORE persisting "reviewed" state. If the POST fails (e.g. a 422
        # self-approval from a misconfigured identity), we must NOT record a session/
        # sha — otherwise the PR looks reviewed-once and never gets retried, with no
        # review on it. State advances only on a confirmed submit.
        counted_past = frozenset()
        prior = self._prior_rejections(bound, counted_past)
        if not await self._submit_review(
            p, result, bound.plan.displayed_round, prior,
            patch_id=signature,
            bound=bound,
            counted_past=counted_past,
        ):
            self._no_progress(p, "review POST failed")
            return
        self._cleanup_checkout(p.number)

    async def re_review(
        self, pr: int, *, claim_target: int | None = None,
        binding: WorkflowBinding | None = None,
    ) -> None:
        # The diff comes first, in EVERY mode: a head advance that leaves the
        # patch unchanged (a mechanical rebase) must be settled by one `gh pr
        # diff` rather than by a paid review round — most of all under MULTI,
        # whose round is a gate + 5 dimensions + N validators (harness #2).
        self._require_authority(binding, pr)
        p = self.gateway.get_pr(pr)
        if not self._claim_matches(p, claim_target):
            return
        assert binding is not None
        bound = self._begin_bound(p, binding)
        if bound is None:
            return
        assert self.authority is not None
        try:
            await self._re_review_selected(pr, p, bound, claim_target)
        finally:
            self.authority.finish(bound)

    async def _re_review_selected(self, pr: int, p, bound: BoundReviewRound, claim_target: int | None) -> None:
        diff = self._revision_bound_diff(p)
        if diff is None:
            self._no_progress(p, "revision-bound diff unavailable")
            assert self.authority is not None
            self.authority.stop(bound, ReviewPlanningRefused("harness-diff:" + bound.plan.round.round_id))
            return
        signature = review_dom.patch_signature(diff)
        assert self.authority is not None
        if not self.authority.test_due(bound, signature):
            self.authority.stop(bound, ReviewPlanningRefused("ordinary-test-backoff:" + bound.plan.round.round_id))
            return
        patch_shortcut = await self._bound_patch_unchanged(p, diff, signature, bound, claim_target)
        if patch_shortcut.settled:
            return
        round_number = bound.plan.displayed_round
        if self.cfg.review_mode is review_dom.ReviewMode.MULTI:
            await self._review_multi(
                pr,
                round_number=round_number,
                p=p,
                diff=diff,
                claim_target=claim_target,
                bound=bound,
            )
            return
        # A fresh generation reviews the whole patch against the PR base.
        last_sha = bound.baseline_head or p.base_ref_oid or "the pull request base"
        guidance = bound.guidance
        discussion = self._pr_discussion(p)
        if self.cfg.review_mode is review_dom.ReviewMode.DELEGATED:
            # Fresh session (no resume) → feed the prior review body as reference
            # instead of replaying the transcript. Use the most recent review with a
            # body (the reviewer's own last verdict).
            prior_review = next(
                (r.body for r in reversed(p.reviews) if r.body.strip()), ""
            )
            task = task_builders.build_delegated_re_review_task(
                pr,
                round_number,
                last_sha,
                p.head_ref_oid,
                as_untrusted(diff),
                as_untrusted(prior_review),
                self.cfg.review,
                operator_guidance=guidance,
                discussion=discussion,
            )
        else:
            if not isinstance(bound.session, ReusableReviewSession):
                prior = review_dom.latest_reviewer_verdict(p, self.cfg.review_bot)
                task = task_builders.build_fresh_re_review_task(
                    pr,
                    round_number,
                    as_untrusted(p.title),
                    as_untrusted(p.body),
                    last_sha,
                    p.head_ref_oid,
                    as_untrusted(diff),
                    as_untrusted(prior.body if prior else ""),
                    self.cfg.review,
                    operator_guidance=guidance,
                    discussion=discussion,
                )
            else:
                task = task_builders.build_re_review_task(
                    pr,
                    round_number,
                    last_sha,
                    p.head_ref_oid,
                    as_untrusted(diff),
                    bound.session.handle, self.cfg.review,
                    operator_guidance=guidance,
                    discussion=discussion,
                )
        task = self._with_test_changes(task, diff)
        assert self.authority is not None
        child = self.authority.stage(bound, ReviewChildKind.REVIEWER, ("review",))[0]
        result = await self._run_logged("re-review", pr, task, p.head_ref_oid, bound=bound, child=child)
        if self._pin_deferral(p, result):
            return
        if result.is_error:
            # do NOT advance sha/round — retry same diff next poll. Log cost so a
            # budget-cap abort (delegated review + scout subagents share one budget)
            # is visible rather than a silent re-review loop.
            self.cfg.log(
                f"re-review #{pr} r{round_number}: agent errored "
                f"(status={result.status or 'error'}, cost=${result.cost_usd:.2f}) — "
                f"no verdict, will retry. Raise REVIEW_BUDGET_USD if cost is near cap."
            )
            self._no_progress(p, f"agent error: {result.status or 'error'}")
            return
        # Submit first; advance sha/round only on a confirmed submit (see _review).
        counted_past = frozenset()
        prior = self._prior_rejections(bound, counted_past)
        if not await self._submit_review(
            p, result, round_number, prior,
            patch_id=signature,
            bound=bound,
            counted_past=counted_past,
        ):
            self._no_progress(p, "review POST failed")
            return
        self._cleanup_checkout(p.number)

    def _require_authority(self, binding: WorkflowBinding | None, pr: int) -> None:
        if not isinstance(binding, WorkflowBinding) or self.authority is None:
            raise WorkflowAuthorityError("review_migration_required: review dispatch requires a workflow binding and authority")
        if (binding.authority.family is not WorkflowFamily.REVIEW_REJECTIONS
                or binding.fence.key.target != PullRequestRef(pr)):
            raise WorkflowAuthorityError("review dispatch binding belongs to another workflow")

    # ── bound (active review family) rounds, ADR 0015 ────────────────────────
    def _begin_bound(self, p, binding: WorkflowBinding) -> BoundReviewRound | None:
        """The owed round for this dispatch, or None when nothing may be bought now.

        The workflow authority re-decides from a fresh read: a held or stale
        binding raises `WorkflowAuthorityError` (a hold, not a death), a
        covered head returns None. An evidence read still backing
        off also returns None: its schedule, not this dispatch, decides when
        the next read happens.
        """
        if self.authority is None:
            raise WorkflowAuthorityError(
                "a bound review dispatch requires the review workflow authority"
            )
        # The schedule is consulted before the complete review read, so a
        # backing-off PR costs no read at all this tick.
        if not self.authority.evidence_due(binding, p.head_ref_oid):
            self.cfg.log(
                f"review #{p.number}: an evidence read is backing off — "
                "deferred to its scheduled read"
            )
            return None
        bound = self.authority.begin(binding, p.head_ref_oid, expected_claim_target=breaker_key(p))
        if bound is None:
            self.cfg.log(
                f"review #{p.number}: the review workflow owes nothing at "
                f"{p.head_ref_oid[:8] or '?'} — no review agent run"
            )
            return None
        return bound

    def _prior_rejections(
        self, bound: BoundReviewRound, non_resetting: frozenset[str]
    ) -> int:
        assert self.authority is not None
        # The complete harness read, never the world view's review list.
        return self.authority.prior_rejections(bound, non_resetting)

    async def _post(
        self, sub, bound: BoundReviewRound, patch_id: str,
        counted_past: frozenset[str] = frozenset(),
    ) -> PostedReview | None:
        assert self.authority is not None
        return await self.authority.submit(
            bound, sub.event, sub.body, patch_id=patch_id,
            escalate_cause=sub.escalate_cause if sub.escalate else None,
            counted_past=counted_past,
        )

    async def _bound_patch_unchanged(
        self, p, diff: str, signature: str, bound: BoundReviewRound, claim_target: int | None,
    ) -> _PatchShortcut:
        """Reuse a verdict only against the same-generation reviewed baseline.

        Patch reuse is legal only when the workflow baseline covers this
        patch in this generation; after a
        re-arm or release the continuation requires a fresh review and this
        returns unsettled. The copied verdict is itself a bound submission,
        so it becomes the new baseline.
        """
        if bound.baseline_head is None or bound.baseline_head == p.head_ref_oid:
            return _PatchShortcut()
        assert self.authority is not None
        if not self.authority.patch_covered(bound, signature):
            return _PatchShortcut()
        prior = review_dom.latest_reviewer_verdict(p, self.cfg.review_bot)
        if prior is None or prior.commit_id != bound.baseline_head:
            return _PatchShortcut()
        if review_dom.UNGRANTED_DOWNGRADE_MARKER in prior.body:
            return _PatchShortcut()
        event = {"APPROVED": "APPROVE", "CHANGES_REQUESTED": "REQUEST_CHANGES"}.get(prior.state)
        if event is None:
            return _PatchShortcut()
        body = prior.body.strip() or "Mechanical rebase: patch is unchanged from the reviewed revision."
        posted = await self.authority.submit(bound, event, body, patch_id=signature, escalate_cause=None)
        if posted is None:
            if not self.authority.test_due(bound, signature):
                self.cfg.log(f"re-review #{p.number}: copied verdict awaits independent ordinary-test evidence")
                return _PatchShortcut(settled=True)
            self.cfg.log(
                f"re-review #{p.number}: unchanged patch verdict could not be "
                "bound to the new head — reviewing normally"
            )
            return _PatchShortcut()
        self.cfg.log(
            f"re-review #{p.number}: head moved to {p.head_ref_oid[:8] or '?'} but the "
            "patch is unchanged against the reviewed baseline — copied the "
            f"verdict as {posted.event}, no review agent run"
        )
        self._cleanup_checkout(p.number)
        return _PatchShortcut(settled=True)

    @staticmethod
    def _with_test_changes(task, diff: str):
        paths = modified_existing_tests_from_diff(diff)
        if not paths:
            return task
        notice = (
            "\n\n## Existing tests modified or deleted\n"
            "Judge each change explicitly for skipped or weakened coverage.\n"
            + untrusted_block("existing_test_paths", as_untrusted("\n".join(f"- {path}" for path in paths)))
        )
        return replace(task, intent=replace(task.intent, prompt=task.intent.prompt + notice))

    def _pr_discussion(self, p) -> Untrusted | None:
        """The PR conversation as bounded UNTRUSTED data, or None when empty.

        Comments already ride on the PRView the review world fetched (`gh pr
        view --json comments`), so this costs no extra GitHub call. None — not
        an empty string — for an empty thread, so the builders render no
        section at all rather than an empty labelled one.

        Incident 2026-08-08 (tauri #613/#614): the fixer answered a demand for
        impossible evidence on the operator-block channel, which the reviewer
        cannot see, and the review rejected to its ceiling twice."""
        text = format_pr_discussion(p.comments)
        return as_untrusted(text) if text else None

    def _revision_bound_diff(self, p) -> str | None:
        if not p.base_ref_oid or not p.head_ref_oid:
            return None
        try:
            evidence = self.gateway.pr_diff(
                p.number, p.base_ref_oid, p.head_ref_oid
            )
        except Exception as exc:  # noqa: BLE001 — unreadable evidence cannot approve
            self.cfg.log(
                f"review #{p.number}: immutable diff unavailable ({exc})"
            )
            return None
        if (
            evidence.base_oid != p.base_ref_oid
            or evidence.head_oid != p.head_ref_oid
            or not evidence.text.strip()
        ):
            self.cfg.log(
                f"review #{p.number}: immutable diff revision mismatch"
            )
            return None
        return evidence.text

    async def _gather_legs(self, coros: list) -> list:
        """asyncio.gather that never lets one leg's exception abort the fan-out.
        `AgentRunner.run` RAISES on a pre-result transport death (it does not
        return is_error=True), so a bare gather would propagate that out of
        _review_multi and lose every sibling leg's result for this poll. Coerce a
        raised leg to an errored AgentResult so the caller's is_error/`complete`
        bookkeeping runs uniformly — a dead leg marks the review incomplete, it
        does not sink the whole round."""
        results = await asyncio.gather(*coros, return_exceptions=True)
        return [
            AgentResult(is_error=True, summary=f"leg raised: {type(r).__name__}")
            if isinstance(r, BaseException) else r
            for r in results
        ]

    async def _review_multi(
        self,
        pr: int,
        round_number: int,
        p=None,
        diff: str | None = None,
        claim_target: int | None = None,
        *, bound: BoundReviewRound,
    ) -> None:
        """The /code-review pattern as dispatcher orchestration: skip-gate →
        parallel dimension specialists → per-finding adversarial validation →
        pure synthesis. Fans the LLM legs out concurrently (each SDK query is its
        own subprocess); the verdict is `review.synthesize_review` (pure). Shares
        the single-pass path's submit-before-persist contract: state advances only
        on a confirmed POST, and the round ceiling still escalates a stuck PR.

        The multi-agent path is stateless per round (no session resume) — every
        round re-derives findings from the CURRENT diff, so a re-review naturally
        re-checks the whole PR rather than only the incremental change. SQLite owns the reviewed
        baseline and decides when another review is owed.

        `p`/`diff` are accepted pre-fetched so `re_review`'s rebase short-circuit
        can inspect the diff before delegating here without paying for a second
        `gh pr view`/`gh pr diff`; omitted (the initial-review path) they are
        fetched here as before."""
        p = self.gateway.get_pr(pr) if p is None else p
        if diff is None:
            diff = self._revision_bound_diff(p)
            if diff is None:
                self._no_progress(p, "revision-bound diff unavailable")
                assert self.authority is not None
                self.authority.stop(bound, ReviewPlanningRefused("harness-diff:" + bound.plan.round.round_id))
                return
        signature = review_dom.patch_signature(diff)
        assert self.authority is not None
        if not self.authority.test_due(bound, signature):
            self.authority.stop(bound, ReviewPlanningRefused("ordinary-test-backoff:" + bound.plan.round.round_id))
            return
        rcfg = self.cfg.review
        guidance = bound.guidance
        discussion = self._pr_discussion(p)
        # Escalation counts the reviewer's OWN prior rejections, not pushes (a
        # rebase that lands here without a prior REQUEST_CHANGES must not consume a
        # round). Derived from GitHub review state, robust across daemon restarts.
        counted_past = frozenset()
        prior = self._prior_rejections(bound, counted_past)

        # 1. skip-gate — a pure doc/format/config diff short-circuits to APPROVE.
        # No PR conversation here on purpose: its only failure direction ends
        # the review in an APPROVE, and a comment is the wrong evidence for
        # "is this diff behavioural?" (see build_review_gate_task).
        assert self.authority is not None
        gate_child = self.authority.stage(bound, ReviewChildKind.GATE, ("gate",))[0]
        gate = await self._run_logged("review-gate", pr, head_sha=p.head_ref_oid, bound=bound, child=gate_child, task=
            self._with_test_changes(task_builders.build_review_gate_task(
                pr,
                as_untrusted(p.title),
                as_untrusted(p.body),
                as_untrusted(diff),
                rcfg,
                operator_guidance=guidance,
            ), diff),
        )
        if self._pin_deferral(p, gate):
            return
        if gate.is_error:
            # transport failure — leave state untouched, retry next poll (but
            # COUNT the round: the retry is not free, and an is_error gate that
            # keeps repeating is the cheapest shape of the P0R-3 loop).
            self._no_progress(p, f"gate leg errored: {gate.summary}")
            return
        # Fail SAFE: a degenerate gate result (is_error=False but empty envelope)
        # must default to reviewable=True — the conservative choice is to RUN the
        # full review, never to silently APPROVE unreviewed code. Only an explicit
        # reviewable=false can skip only diffs without existing-test edits.
        if (
            not gate.field_bool("reviewable", default=True)
            and not modified_existing_tests_from_diff(diff)
        ):
            sub = review_dom.gate_submission(
                prior,
                self.cfg.max_review_rejections,
            )
            self.cfg.log(
                f"review #{pr}: gate says non-reviewable "
                f"({gate.field('reason') or 'no behavioural change'}) "
                f"→ {sub.event}"
            )
            if await self._post(sub, bound, signature, counted_past):
                self._cleanup_checkout(p.number)
            else:
                self._no_progress(p, f"gate {sub.event} POST failed")
            return

        # 2. dimension specialists — parallel, independent, each hunts one lens.
        # An errored leg makes the review INCOMPLETE (a defect-detecting lens may
        # not have run) — tracked so synthesis never emits a silent APPROVE on a
        # partial result; it retries next poll instead.
        dims = task_builders.REVIEW_DIMENSIONS
        dim_children = self.authority.stage(bound, ReviewChildKind.DIMENSION, tuple(key for key, _ in dims))
        dim_results = await self._gather_legs([
            self._run_logged(f"review:{key}", pr, head_sha=p.head_ref_oid, bound=bound, child=child, task=
                self._with_test_changes(task_builders.build_review_dimension_task(
                    pr,
                    key,
                    focus,
                    as_untrusted(p.title),
                    as_untrusted(p.body),
                    as_untrusted(diff),
                    rcfg,
                    operator_guidance=guidance,
                    discussion=discussion,
                ), diff)
            )
            for (key, focus), child in zip(dims, dim_children)
        ])
        lost = next((r for r in dim_results if _pin_deferred(r)), None)
        if lost is not None:
            # The pinned checkout vanished mid-round (gate ensured it, a
            # dimension lost it). Sibling spend is sunk either way; the streak
            # counter keeps this bounded and journalled.
            self.cfg.log(f"review #{pr}: pinned checkout lost mid-round — deferring")
            self._pin_deferral(p, lost)
            return
        complete = True
        candidates: list[review_dom.Finding] = []
        for (key, _focus), res in zip(dims, dim_results):
            if res.is_error:
                self.cfg.log(f"review #{pr}: dimension {key} errored — review incomplete")
                complete = False
                continue
            candidates.extend(review_dom.parse_findings(key, res.structured))
        self.cfg.log(f"review #{pr}: {len(candidates)} candidate finding(s) "
                     f"across {len(dims)} dimensions")

        # 3. adversarial validation — one refuter per candidate, in parallel.
        # Deliberately NOT given the operator's scope note: it adjudicates one
        # already-raised claim on its technical merits, and scope text there
        # only widens a refuter's licence to dismiss a real defect. A
        # scope-inappropriate finding is prevented at the specialist, not
        # argued away at the validator.
        #
        # A validator transport error is NOT a refutation: it also marks the review
        # incomplete (so a genuine finding whose validator died is not silently
        # dropped into an APPROVE), while confirmed stays False so it isn't reported
        # as a fact either — the PR is simply retried next poll.
        if candidates:
            validators = self.authority.stage(bound, ReviewChildKind.VALIDATOR,
                                              tuple(f"{finding.dimension}:{index}" for index, finding in enumerate(candidates)))
            verdicts = await self._gather_legs([
                self._run_logged(f"validate:{f.dimension}", pr, head_sha=p.head_ref_oid, bound=bound, child=child, task=
                    task_builders.build_review_validation_task(
                        pr,
                        f.dimension,
                        as_untrusted(f.location),
                        as_untrusted(f.claim),
                        as_untrusted(diff),
                        rcfg,
                    )
                )
                for f, child in zip(candidates, validators)
            ])
            lost = next((v for v in verdicts if _pin_deferred(v)), None)
            if lost is not None:
                self.cfg.log(
                    f"review #{pr}: pinned checkout lost mid-round — deferring"
                )
                self._pin_deferral(p, lost)
                return
            validated = []
            for f, v in zip(candidates, verdicts):
                if v.is_error:
                    complete = False
                validated.append(replace(
                    f, confirmed=(not v.is_error) and bool(v.field_bool("confirmed")),
                    verdict=v.field("verdict")))
        else:
            validated = []
        n_conf = sum(1 for f in validated if f.confirmed)
        self.cfg.log(f"review #{pr}: {n_conf}/{len(validated)} finding(s) confirmed"
                     f"{'' if complete else ' (INCOMPLETE — a leg errored)'}")

        # 4. pure synthesis → verdict (round ceiling applied inside). None when the
        # review was incomplete AND nothing blocking survived → submit nothing,
        # retry next poll (mirrors the gate.is_error path; never a silent APPROVE).
        sub = review_dom.synthesize_review(
            validated,
            prior,
            self.cfg.max_review_rejections,
            complete=complete,
        )
        if sub is None:
            self.cfg.log(f"review #{pr}: incomplete and no blocking finding — "
                         "submitting nothing, will retry next poll")
            # One finalization accounts for all actually launched siblings,
            # including returned failures when no verdict can be synthesized.
            self._no_progress(p, "incomplete fan-out, no verdict to submit")
            return
        if not await self._post(sub, bound, signature, counted_past):
            self._no_progress(p, "review POST failed")
            return  # failed POST → leave state untouched (retried next poll)
        self._cleanup_checkout(p.number)

    def _cleanup_checkout(self, pr: int) -> None:
        # A settled round no longer needs its pinned checkout; the next round
        # recreates one at the then-current head.
        if self.cfg.review_checkout_cleanup is not None:
            try:
                self.cfg.review_checkout_cleanup(pr)
            except Exception as exc:  # noqa: BLE001
                self.cfg.log(
                    f"review #{pr}: pinned checkout not removed ({exc})"
                )

    async def _submit_review(
        self,
        p,
        result,
        round_number: int,
        prior_rejections: int,
        patch_id: str,
        *, bound: BoundReviewRound,
        counted_past: frozenset[str] = frozenset(),
    ) -> bool:
        """Submit the verdict; return True iff the gh POST succeeded. On a failed
        submit, return False WITHOUT escalating/relabelling — the caller leaves state
        untouched so the PR is retried next poll rather than stranded as "reviewed".
        `round_number` is for the human-facing log line only; `prior_rejections`
        (the reviewer's own prior REQUEST_CHANGES count) drives the escalation
        ceiling."""
        pr = p.number
        sub = review_dom.review_submission(
            result.field("decision"), result.field("body") or "",
            prior_rejections, self.cfg.max_review_rejections,
        )
        posted = await self._post(sub, bound, patch_id, counted_past)
        if posted is None:
            return False
        # Log the OUTCOME of a successful submit — always on (not DEBUG-gated).
        escalated = " → escalated (needs-human-review)" if posted.escalated else ""
        # Show the rejection tally that actually drives escalation, not merely
        # the push-derived round number.
        tally = (
            f", rejection {prior_rejections + 1}/{self.cfg.max_review_rejections}"
            if posted.event != "APPROVE" else ""
        )
        self.cfg.log(
            f"review #{pr} r{round_number}{tally}: submitted {posted.event}{escalated}"
        )
        return True
