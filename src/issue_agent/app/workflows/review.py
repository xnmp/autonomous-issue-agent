"""Review workflow continuation under ADR 0009.

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
    ReviewSkipped,
    TelemetryEvent,
    TelemetryWarning,
)
from ...domain.leg import Leg, LegTarget
from ...domain.trust import Untrusted, as_untrusted
from ..planners import tasks as task_builders
from ..planners.schemas import review_schema_with_exemptions
from ..planners.tasks import ReviewTaskConfig
from ...adapters.github import format_pr_discussion
from ...adapters.runtime_plan import AgentRuntime
from ...domain.agent_task import (
    RUNTIME_CAPABILITIES,
    RuntimeCapabilities,
    RuntimeName,
)
from ...domain.exemptions import (
    ExemptionDeclaration,
    MAX_DECLARATIONS_PER_CHANGE,
    RULE_REQUIRED_TEST,
    RULE_TESTS_IMMUTABLE,
    authenticated_grant_pairs,
    open_fence_marker,
    parse_exemption_declarations,
    parse_exemption_grants,
    render_grant_line,
    valid_subject,
)
from ...domain.gate import modified_existing_tests_from_diff
from ...domain.fix import breaker_key
from ...domain.identity import login_eq
from ...domain.refs import issue_from_branch
from ...domain.labels import Label
from ...domain.lifecycle import pr_escalation
from ...domain.outcome import AgentResult
from ...domain.ports import (
    ActionExecutor,
    EventSink,
    WorkflowGitHubReads,
    WorkflowState,
)
from ...domain.workflow_identity import WorkflowBinding
from ...adapters.workflow_rows import WorkflowAuthorityError
from .base import run_leg

_EXTERNAL_REVIEW_SESSION = "github-review"
_FRESH_REVIEW_SESSION = "codex-review"

# A round that could not PIN its checkout never ran a paid leg — it must defer
# (retry next poll) without feeding the unproductive-round counter, or five
# free deferrals walk a healthy PR into needs-human-review (0d2e295 H3).
_PIN_DEFERRED_STATUS = "review_checkout_deferred"

# …but "free" cannot mean "unbounded and invisible" (adversarial F1): a
# checkout that can NEVER be pinned (TARGET_DIR not a repo, dead origin) would
# otherwise stall the PR forever with no escalation and no dashboard signal.
# Past this consecutive-deferral streak, further deferrals count as
# unproductive rounds again, restoring the escalation bound.
_MAX_FREE_PIN_DEFERRALS = 5

# Commit-message evidence is needed to discover `required-test` declarations.
# A transient read failure must not be interpreted as "none declared", but an
# outage also cannot create a silent forever-loop. As with checkout pinning,
# the first few free deferrals retry without consuming a paid-round breaker;
# a persistent failure eventually enters the existing bounded escalation path.
_MAX_FREE_REVIEW_EVIDENCE_DEFERRALS = 5


@dataclass(frozen=True)
class _PatchShortcut:
    """Outcome of the mechanical-rebase shortcut.

    An incomplete prior approval is not settled, but its old revision must
    remain visible to rejection accounting when the normal review runs.
    """

    settled: bool = False
    non_resetting_approval_commits: frozenset[str] = frozenset()


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
    observation: object = field(compare=False, repr=False, default=None)


class ReviewAuthority(Protocol):
    """The review workflow authority a bound review consults and submits through."""

    def begin(self, binding: WorkflowBinding, head_sha: str) -> BoundReviewRound | None:
        """The round, None when nothing is owed; raises `WorkflowAuthorityError` when held."""
        ...

    def patch_covered(self, round: BoundReviewRound, patch_id: str) -> bool: ...

    def prior_rejections(self, round: BoundReviewRound, non_resetting: frozenset[str]) -> int: ...

    def evidence_due(self, binding: WorkflowBinding, head_sha: str) -> bool:
        """Whether every harness read a review of this head needs is off its backoff."""
        ...

    def record_evidence(self, round: BoundReviewRound, error: str | None) -> None: ...

    def submit(
        self, round: BoundReviewRound, event: str, body: str, *, patch_id: str, escalate_cause: str | None,
        counted_past: frozenset[str] = frozenset(),
    ) -> bool:
        """True once the submission's receipt landed; its settlement decides any hold."""
        ...


@dataclass(frozen=True)
class ReviewWorkflowConfig:
    review: ReviewTaskConfig | None
    review_mode: review_dom.ReviewMode
    max_review_rejections: int
    review_bot: str
    review_trigger_label: str
    log: Callable[[str], None]
    # ADR 0014 grant-authentication key. Consumed ONLY by `_grant_suffix`,
    # harness-side; never enters an agent env or prompt. "" → no grant can
    # be minted (declared PRs then escalate via the withheld arm).
    exemption_mac_key: str = ""
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
    # Required, not optional: escalation writes go through the funnel or not at
    # all (review M10). A default would let a caller silently reintroduce the
    # direct-gateway bypass this field exists to remove.
    executor: ActionExecutor
    log_run: Callable[[str, int, object], Awaitable[object]] | None = None
    # Present only when the review family may be active: a bound dispatch
    # (RunAgent.workflow) is refused without it, never run unbound.
    authority: ReviewAuthority | None = None
    # Consecutive pin-deferral streak per PR (daemon lifetime; a restart
    # resets it, and a permanently broken config re-earns it within minutes).
    _pin_deferrals: dict[int, int] = field(default_factory=dict)
    _evidence_deferrals: dict[int, int] = field(default_factory=dict)
    _evidence_errors: dict[int, str] = field(default_factory=dict)

    def _trigger_label(self) -> Label:
        """The reviewer's trigger label as a domain Label. Both the default
        (ai-pr-open) and the external-repo value (ai-review) are real Labels; an
        unknown string is a misconfiguration, and crashing here — after a verdict
        has already been POSTed — would strand the PR mid-escalation, so fall
        back loudly to ai-pr-open."""
        try:
            return Label(self.cfg.review_trigger_label)
        except ValueError:
            self.cfg.log(
                f"review: REVIEW_TRIGGER_LABEL '{self.cfg.review_trigger_label}' is not a "
                f"known lifecycle label — escalating from '{Label.AI_PR_OPEN.value}' instead"
            )
            return Label.AI_PR_OPEN

    def _escalate(self, pr: int, rejections: int, cause: str = "ceiling") -> None:
        """The reviewer is out of rounds → hand the PR to a human, THROUGH the
        write funnel (review M10/D4).

        This used to be a bare `gateway.relabel(..., "needs-human-review")` with
        a raw string, so the one state change an operator most needs to hear
        about — the reviewer giving up on a PR — emitted no `escalation` journal
        event and fired no ALERT_CMD, while the spend-cap escalation on the very
        same daemon did. `Executor` detects the needs-human-review move itself,
        so routing the actions through it is what restores both."""
        # The reason must name the REAL cause: a verdict-driven escalation
        # phrased as a ceiling trip reads like a fencepost bug (keifu PR #177,
        # 2026-08-02: round-3 ESCALATE verdict, comment said "3 time(s) ...
        # ceiling 4") and feeds the dashboard's escalation_reason the wrong WHY.
        if cause == "verdict":
            reason = (
                f"the reviewer returned ESCALATE on rejection {rejections} of "
                f"{self.cfg.max_review_rejections}: this PR needs a human "
                "decision the review loop cannot make."
            )
        else:
            reason = (
                f"the reviewer requested changes {rejections} time(s) without the PR "
                f"converging (ceiling {self.cfg.max_review_rejections})."
            )
        self.executor.execute_all(
            pr_escalation(pr, reason, from_label=self._trigger_label()).actions()
        )

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

    def _no_progress(
        self,
        pr,
        reason: str,
    ) -> None:
        """Record that a PAID review round advanced nothing (review P0R-3).

        Every `return` in this class that skips `save_pr_session`/`set_last_sha`
        leaves `needs_initial_review`/`needs_re_review` True, so the identical
        dispatch is bought again on the next poll — under REVIEW_MODE=multi that
        is a gate + 5 dimensions + N validators, re-run every poll on a RESULT
        the workflow chose not to use. Nothing counted those rounds: the
        dispatcher's `except BaseException` bump only fires when the leg RAISED
        out of the workflow, and these paths all return normally.

        Review convergence is PR-keyed, independently of issue-keyed claim and
        spend attribution. A later closing-link change must not erase a paid
        failed round from the reviewer's ceiling.
        """
        key = -pr.number
        try:
            n = self.store.bump_infra_deaths(key)
        except Exception as exc:  # noqa: BLE001 — a counter must not mask the failure
            self.cfg.log(f"review #{pr.number}: no-progress counter not persisted ({exc})")
            return
        self.cfg.log(
            f"review #{pr.number}: round bought nothing ({reason}) — "
            f"unproductive round {n}"
        )

    def _pin_deferral(self, p, result) -> bool:
        """True when `result` is a pin deferral — the caller must return
        without submitting anything. Every deferral is journalled on the ADR
        0008 pattern (same shape as `_claim_matches`' dispatch_deferred: a
        persistent flap must be visible in the dashboard's per-issue event
        view, not only the log), and only a bounded streak is exempt from the
        unproductive-round counter — past `_MAX_FREE_PIN_DEFERRALS` the
        escalation bound is restored so an unpinnable PR still reaches a
        human."""
        if not _pin_deferred(result):
            return False
        n = self._pin_deferrals.get(p.number, 0) + 1
        self._pin_deferrals[p.number] = n
        if self.cfg.journal is not None:
            try:
                self.cfg.journal.emit(
                    TelemetryEvent(
                        TelemetryWarning(
                            "review_pin_deferred",
                            f"review #{p.number}: pinned checkout at "
                            f"{p.head_ref_oid[:12] or '?'} unavailable — "
                            f"deferral {n} in a row",
                            {"streak": n, "head": p.head_ref_oid},
                        ),
                        Correlation(pr=p.number),
                    )
                )
            except Exception as exc:  # noqa: BLE001 — telemetry must not block
                self.cfg.log(
                    f"review #{p.number}: pin deferral not journalled ({exc})"
                )
        if n > _MAX_FREE_PIN_DEFERRALS:
            self._no_progress(p, f"pin deferral streak {n}")
        return True

    def _evidence_deferral(self, p, reason: str) -> None:
        """Defer a review whose approval evidence could not be read.

        This happens before a paid leg, so a short GitHub outage gets a small
        free retry window. The streak is journalled and bounded: persistent
        unreadability feeds the existing unproductive-round breaker instead
        of leaving the PR invisible forever.
        """
        n = self._evidence_deferrals.get(p.number, 0) + 1
        self._evidence_deferrals[p.number] = n
        self.cfg.log(
            f"review #{p.number}: approval evidence unavailable ({reason}) — "
            f"deferral {n} in a row"
        )
        if self.cfg.journal is not None:
            try:
                self.cfg.journal.emit(
                    TelemetryEvent(
                        TelemetryWarning(
                            "review_exemption_evidence_deferred",
                            f"review #{p.number}: {reason} — deferral {n} in a row",
                            {
                                "streak": n,
                                "head": p.head_ref_oid,
                                "reason": reason,
                            },
                        ),
                        Correlation(pr=p.number),
                    )
                )
            except Exception as exc:  # noqa: BLE001 — telemetry must not block
                self.cfg.log(
                    f"review #{p.number}: evidence deferral not journalled "
                    f"({exc})"
                )
        if n > _MAX_FREE_REVIEW_EVIDENCE_DEFERRALS:
            self._no_progress(p, f"approval evidence deferral streak {n}")

    async def _run_logged(self, label: str, pr: int, task, head_sha: str = ""):
        task = self._prepare_task(task, pr, head_sha)
        if isinstance(task, AgentResult):
            return task
        if task is None:
            return AgentResult(
                is_error=True,
                status="containment_unavailable",
                summary="Codex review needs an explicit TARGET_DIR for its governor hook",
            )
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
        p = self.gateway.get_pr(pr)
        if not self._claim_matches(p, claim_target):
            return
        bound = None
        if binding is not None:
            bound = self._begin_bound(p, binding)
            if bound is None:
                return
        elif self._already_reviewed(p, claim_target) is not False:
            return
        non_resetting_approvals = (
            self._non_resetting_approval_commits(p) if bound is None else frozenset()
        )
        if self.cfg.review_mode is review_dom.ReviewMode.MULTI:
            await self._review_multi(
                pr,
                round_number=1,
                p=p,
                claim_target=claim_target,
                non_resetting_approval_commits=non_resetting_approvals,
                bound=bound,
            )
            return
        # SINGLE and DELEGATED share the submit-before-persist contract; they differ
        # only in the task builder (delegated attaches a code-scout subagent).
        diff = self._revision_bound_diff(p)
        if diff is None:
            self._no_progress(p, "revision-bound diff unavailable")
            return
        signature = review_dom.patch_signature(diff)
        guidance = self._guidance(pr, bound)
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
        exemption_request = self._exemption_grant_notice(p, diff)
        if exemption_request is None:
            self._evidence_unavailable(p, bound, "exemption declarations unreadable")
            return
        self._evidence_available(bound)
        notice, declared_exemptions = exemption_request
        if notice:
            task = self._with_exemption_notice(task, notice, declared_exemptions)
        result = await self._run_logged("review", pr, task, p.head_ref_oid)
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
        counted_past = self._counted_past(
            p, bound, non_resetting_approvals, signature, declared_exemptions
        )
        prior = self._prior_rejections(p, bound, counted_past)
        grant_suffix = self._grant_suffix(
            p, signature, result, declared_exemptions
        )
        if not self._submit_review(
            p, p.head_ref_oid, result, 1, prior,
            patch_id=signature,
            body_suffix=grant_suffix,
            declared_exemptions=declared_exemptions,
            bound=bound,
            counted_past=counted_past,
        ):
            self._no_progress(p, "review POST failed")
            return
        self.store.save_pr_session(
            pr,
            (
                _FRESH_REVIEW_SESSION
                if not self.cfg.reviewer_capabilities.resume_preserves_sandbox
                else result.session_id
            ),
        )
        if bound is None:
            # A bound verdict's receipt is the reviewed baseline; the legacy
            # head/patch cache would be a second, divergent authority.
            self.store.set_last_sha(pr, p.head_ref_oid)
            self._record_patch(pr, signature)
        self.store.set_round(pr, 1)
        self._progress(p, claim_target)

    async def re_review(
        self, pr: int, *, claim_target: int | None = None,
        binding: WorkflowBinding | None = None,
    ) -> None:
        # The diff comes first, in EVERY mode: a head advance that leaves the
        # patch unchanged (a mechanical rebase) must be settled by one `gh pr
        # diff` rather than by a paid review round — most of all under MULTI,
        # whose round is a gate + 5 dimensions + N validators (harness #2).
        p = self.gateway.get_pr(pr)
        if not self._claim_matches(p, claim_target):
            return
        bound = None
        if binding is not None:
            bound = self._begin_bound(p, binding)
            if bound is None:
                return
        elif self._already_reviewed(p, claim_target) is not False:
            return
        non_resetting_approvals = (
            self._non_resetting_approval_commits(p) if bound is None else frozenset()
        )
        diff = self._revision_bound_diff(p)
        if diff is None:
            self._no_progress(p, "revision-bound diff unavailable")
            return
        signature = review_dom.patch_signature(diff)
        patch_shortcut = (
            self._patch_unchanged(p, diff, signature, claim_target)
            if bound is None
            else self._bound_patch_unchanged(p, diff, signature, bound, claim_target)
        )
        if patch_shortcut.settled:
            return
        non_resetting_approvals |= (
            patch_shortcut.non_resetting_approval_commits
        )
        round_number = self.store.get_round(pr) + 1
        if self.cfg.review_mode is review_dom.ReviewMode.MULTI:
            await self._review_multi(
                pr,
                round_number=round_number,
                p=p,
                diff=diff,
                claim_target=claim_target,
                non_resetting_approval_commits=non_resetting_approvals,
                bound=bound,
            )
            return
        # A bound round's only reviewed head is its same-generation baseline;
        # without one the whole patch against its base is under review, and
        # the legacy head cache is never consulted once the family is active.
        last_sha = (
            self.store.get_last_sha(pr) if bound is None
            else bound.baseline_head or p.base_ref_oid or "the pull request base"
        )
        guidance = self._guidance(pr, bound)
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
            session_id = self.store.get_pr_session(pr)
            if session_id in (_EXTERNAL_REVIEW_SESSION, _FRESH_REVIEW_SESSION):
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
                    session_id, self.cfg.review,
                    operator_guidance=guidance,
                    discussion=discussion,
                )
        exemption_request = self._exemption_grant_notice(p, diff)
        if exemption_request is None:
            self._evidence_unavailable(p, bound, "exemption declarations unreadable")
            return
        self._evidence_available(bound)
        notice, declared_exemptions = exemption_request
        if notice:
            task = self._with_exemption_notice(task, notice, declared_exemptions)
        result = await self._run_logged("re-review", pr, task, p.head_ref_oid)
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
        counted_past = self._counted_past(
            p, bound, non_resetting_approvals, signature, declared_exemptions
        )
        prior = self._prior_rejections(p, bound, counted_past)
        grant_suffix = self._grant_suffix(
            p, signature, result, declared_exemptions
        )
        if not self._submit_review(
            p, p.head_ref_oid, result, round_number, prior,
            patch_id=signature,
            body_suffix=grant_suffix,
            declared_exemptions=declared_exemptions,
            bound=bound,
            counted_past=counted_past,
        ):
            self._no_progress(p, "review POST failed")
            return
        if result.session_id:
            self.store.save_pr_session(
                pr,
                (
                    _FRESH_REVIEW_SESSION
                    if not self.cfg.reviewer_capabilities.resume_preserves_sandbox
                    else result.session_id
                ),
            )
        if bound is None:
            self.store.set_last_sha(pr, p.head_ref_oid)
            self._record_patch(pr, signature)
        self.store.set_round(pr, round_number)
        self._progress(p, claim_target)

    # ── bound (active review family) rounds, ADR 0015 ────────────────────────
    def _begin_bound(self, p, binding: WorkflowBinding) -> BoundReviewRound | None:
        """The owed round for this dispatch, or None when nothing may be bought now.

        The workflow authority re-decides from a fresh read: a held or stale
        binding raises `WorkflowAuthorityError` (a hold, not a death), a
        covered head returns None. An exemption-evidence read still backing
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
        bound = self.authority.begin(binding, p.head_ref_oid)
        if bound is None:
            self.cfg.log(
                f"review #{p.number}: the review workflow owes nothing at "
                f"{p.head_ref_oid[:8] or '?'} — no review agent run"
            )
            return None
        return bound

    def _guidance(self, pr: int, bound: BoundReviewRound | None) -> str:
        # A bound review reads only the committed episode guidance: the
        # legacy record is not its authority once the family is active.
        return bound.guidance if bound is not None else self.store.get_operator_guidance(pr)

    def _evidence_unavailable(self, p, bound: BoundReviewRound | None, reason: str) -> None:
        error = self._evidence_errors.pop(p.number, "")
        if bound is None or self.authority is None:
            self._evidence_deferral(p, reason)
            return
        # Bounded and visible: the authority backs the read off and, at its
        # ceiling, opens an EvidenceUnavailable hold (never an approval).
        self.authority.record_evidence(bound, f"{reason}: {error}" if error else reason)
        self.cfg.log(f"review #{p.number}: {reason} — scheduled evidence read recorded")

    def _evidence_available(self, bound: BoundReviewRound | None) -> None:
        if bound is not None and self.authority is not None:
            self.authority.record_evidence(bound, None)

    def _counted_past(
        self, p, bound: BoundReviewRound | None, non_resetting: frozenset[str],
        patch_id: str, declared: tuple[ExemptionDeclaration, ...],
    ) -> frozenset[str]:
        """Approvals the rejection count runs past without a reset.

        Legacy rounds reach here only after `_already_reviewed` proved a
        current-head approval incomplete. Bound rounds skip that settlement,
        so they decide the current-head approval on its own terms: it is
        non-resetting only when its authenticated grants do not cover this
        revision's declarations (ADR 0014), never merely for being there.
        """
        if bound is None:
            return non_resetting
        latest = review_dom.latest_reviewer_verdict(p, self.cfg.review_bot)
        if (latest is not None and latest.state == "APPROVED" and latest.commit_id == p.head_ref_oid
                and not self._grants_cover(p, patch_id, declared)):
            return non_resetting | {p.head_ref_oid}
        return non_resetting

    def _prior_rejections(
        self, p, bound: BoundReviewRound | None, non_resetting: frozenset[str]
    ) -> int:
        if bound is None or self.authority is None:
            return review_dom.count_prior_rejections(
                p.reviews,
                self.cfg.review_bot,
                non_resetting_approval_commits=non_resetting,
            )
        # The complete harness read, never the world view's review list.
        return self.authority.prior_rejections(bound, non_resetting)

    def _post(
        self, p, sub, bound: BoundReviewRound | None, patch_id: str,
        counted_past: frozenset[str] = frozenset(),
    ) -> bool:
        if bound is None or self.authority is None:
            return self.executor.submit_review(p.number, sub.event, sub.body, p.head_ref_oid)
        return self.authority.submit(
            bound, sub.event, sub.body, patch_id=patch_id,
            escalate_cause=sub.escalate_cause if sub.escalate else None,
            counted_past=counted_past,
        )

    def _escalate_unbound(
        self, bound: BoundReviewRound | None, pr: int, rejections: int, cause: str,
    ) -> None:
        # A bound escalation's hold is decided by settling its receipt (the
        # workflow projects needs-human-review in the same commit as the
        # baseline or the ceiling gate); relabelling here would bypass that.
        if bound is None:
            self._escalate(pr, rejections, cause=cause)

    def _bound_patch_unchanged(
        self, p, diff: str, signature: str, bound: BoundReviewRound, claim_target: int | None,
    ) -> _PatchShortcut:
        """`_patch_unchanged` against the same-generation reviewed baseline only.

        Patch reuse is legal only when the workflow's own baseline (never the
        legacy head/patch cache) covers this patch in this generation; after a
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
        if prior.state == "CHANGES_REQUESTED" and review_dom.UNGRANTED_DOWNGRADE_MARKER in prior.body:
            return _PatchShortcut()
        if prior.state == "APPROVED":
            complete = self._approval_grants_complete(p, diff)
            if complete is None:
                self._evidence_unavailable(
                    p, bound, "exemption declarations unreadable for unchanged patch"
                )
                return _PatchShortcut(settled=True)
            self._evidence_available(bound)
            if not complete:
                return _PatchShortcut(
                    non_resetting_approval_commits=frozenset({bound.baseline_head})
                )
        event = {"APPROVED": "APPROVE", "CHANGES_REQUESTED": "REQUEST_CHANGES"}.get(prior.state)
        if event is None:
            return _PatchShortcut()
        body = prior.body.strip() or "Mechanical rebase: patch is unchanged from the reviewed revision."
        if not self.authority.submit(bound, event, body, patch_id=signature, escalate_cause=None):
            self.cfg.log(
                f"re-review #{p.number}: unchanged patch verdict could not be "
                "bound to the new head — reviewing normally"
            )
            return _PatchShortcut()
        self.cfg.log(
            f"re-review #{p.number}: head moved to {p.head_ref_oid[:8] or '?'} but the "
            "patch is unchanged against the reviewed baseline — copied the "
            "verdict as a bound submission, no review agent run"
        )
        self._progress(p, claim_target)
        return _PatchShortcut(settled=True)

    def _already_reviewed(
        self, p, claim_target: int | None = None
    ) -> bool | None:
        """Settle a retried dispatch when GitHub has a usable verdict on HEAD.

        True means settled, False means no usable verdict and the ordinary
        review should run, and None means current evidence was unreadable so
        this tick was explicitly deferred. An APPROVED review is usable only
        when its authenticated grants cover every declaration derived from the
        current revision; REQUEST_CHANGES already fails closed and needs no
        grant validation.
        """
        latest = review_dom.latest_reviewer_verdict(p, self.cfg.review_bot)
        if not latest or latest.commit_id != p.head_ref_oid:
            return False
        if latest.state == "APPROVED":
            diff = self._revision_bound_diff(p)
            if diff is None:
                self._evidence_deferral(
                    p, "revision-bound diff unavailable for existing approval"
                )
                return None
            complete = self._approval_grants_complete(p, diff)
            if complete is None:
                self._evidence_deferral(
                    p, "exemption declarations unreadable for existing approval"
                )
                return None
            if not complete:
                self.cfg.log(
                    f"review #{p.number}: existing approval on head "
                    f"{p.head_ref_oid[:8]} lacks required authenticated grants "
                    "— reviewing normally"
                )
                return False
            self._record_patch(p.number, review_dom.patch_signature(diff))
        self.store.save_pr_session(
            p.number, self.store.get_pr_session(p.number) or _EXTERNAL_REVIEW_SESSION
        )
        self.store.set_last_sha(p.number, p.head_ref_oid)
        self.cfg.log(
            f"review #{p.number}: {self.cfg.review_bot} already submitted a verdict "
            f"for head {p.head_ref_oid[:8]} — no review agent run"
        )
        if self.cfg.journal is not None:
            self.cfg.journal.emit(
                TelemetryEvent(
                    ReviewSkipped(
                        self.cfg.review_bot,
                        p.head_ref_oid,
                        "reviewer-verdict-on-head",
                    ),
                    Correlation(pr=p.number),
                )
            )
        self._progress(p, claim_target)
        return True

    def _approval_grants_complete(self, p, diff: str) -> bool | None:
        """Whether existing approvals authorize every current declaration.

        None is transient evidence failure, not an empty declaration set.
        Grant syntax is aggregated across every reviewer-bot approval and then
        MAC-, issue-, and patch-checked exactly as at merge. A later refusal or
        omission does not revoke authority already granted for identical patch
        content (ADR 0014).
        """
        request = self._exemption_grant_notice(p, diff)
        if request is None:
            return None
        _notice, declarations = request
        return self._grants_cover(p, review_dom.patch_signature(diff), declarations)

    def _grants_cover(self, p, patch_id: str, declarations: tuple[ExemptionDeclaration, ...]) -> bool:
        """Whether the reviewer's approvals authenticate every declaration for this patch (no reads)."""
        if not declarations:
            return True
        grants = tuple(
            grant
            for review in p.reviews
            if review.state == "APPROVED"
            and login_eq(review.author, self.cfg.review_bot)
            for grant in parse_exemption_grants(review.body)
        )
        granted = authenticated_grant_pairs(
            grants,
            issue_number=issue_from_branch(p.head_ref_name),
            patch_id=patch_id,
            mac_key=self.cfg.exemption_mac_key,
        )
        return all(
            (declaration.rule, declaration.subject) in granted
            for declaration in declarations
        )

    def _non_resetting_approval_commits(self, p) -> frozenset[str]:
        """Current approval commits known incomplete by `_already_reviewed`.

        Call only after reconciliation returned False. A current-head approval
        on that path was proven to lack aggregate authenticated grants; raw
        GitHub APPROVED state must not erase unresolved prior rejections.
        """
        latest = review_dom.latest_reviewer_verdict(p, self.cfg.review_bot)
        if (
            latest is not None
            and latest.state == "APPROVED"
            and latest.commit_id == p.head_ref_oid
        ):
            return frozenset({p.head_ref_oid})
        return frozenset()

    # ── rebase short-circuit (harness #2) ────────────────────────────────────
    def _record_patch(self, pr: int, signature: str) -> None:
        """Remember WHAT the review just looked at, next to `last-sha`'s WHICH.
        Best-effort: a store that cannot record it only costs the next rebase a
        review round, whereas raising here would undo a submitted verdict."""
        try:
            self.store.set_last_patch_id(pr, signature)
        except Exception as exc:  # noqa: BLE001 — a cache write must never fail a review
            self.cfg.log(f"review #{pr}: reviewed-patch id not persisted ({exc})")

    _EXEMPTION_JUDGMENT = {
        "tests-immutable": (
            "grant ONLY if the existing test encodes a wrong expectation "
            "(the test itself is the bug) and this diff corrects it without "
            "weakening what the suite verifies; additive coverage belongs "
            "in a new test file and does not justify modifying an existing "
            "test file; wholesale rewriting existing tests is refused"
        ),
        "required-test": (
            "grant ONLY if this change GENUINELY cannot be covered by a "
            "meaningful automated test (pure config, comment-only, tooling "
            "glue); 'a test would be tedious' is a refusal"
        ),
    }

    def _exemption_grant_notice(
        self, p, diff: str
    ) -> tuple[str, tuple[ExemptionDeclaration, ...]] | None:
        """ADR 0014: when the PR's commits declare gate exemptions, tell the
        reviewer to grant via the STRUCTURED `exemption_grants` field — never
        via body text. The harness renders the authenticated grant lines
        itself (`_grant_suffix`), binding the patch id the model never sees;
        prose can therefore quote or discuss anything without minting
        authority (round-2 adversarial review: string-matching grants out of
        prose survived fence-quoting tricks).

        Returns (notice, declarations); ("", ()) when there are none; None
        when commit-message evidence is unreadable. That last state is not
        declaration absence: a hidden `required-test` trailer would otherwise
        produce an approved-but-unmergeable review. A branch with no issue
        binding still returns its declarations: no grant can be minted for it,
        so an approval must become REQUEST_CHANGES rather than repeat that
        same dead end.

        tests-immutable subjects are AUTO-DECLARED from the same
        revision-checked diff being reviewed — the merge-side collector
        derives its subjects from the identical text, so the reviewer is
        always asked about exactly the set the merge rule will require.
        required-test stays trailer-declared (stated intent the diff cannot
        carry); a trailer read failure only costs that rule's notice.

        MULTI-mode reviews do not carry the notice yet — their verdict body
        is synthesized, not authored in one leg; a declared PR under MULTI
        escalates via the withheld-exemption arm in decide_fix."""
        declared: dict[ExemptionDeclaration, None] = {}
        auto_subjects, _undecidable = modified_existing_tests_from_diff(diff)
        for path in auto_subjects:
            # Auto-declared subjects are diff-derived, so unlike trailer
            # subjects they never passed the grammar; one that cannot be
            # minted must not be offered for granting (the gate denies these
            # pre-publish, so this is belt-and-braces for pre-existing PRs).
            if valid_subject(path):
                declared.setdefault(
                    ExemptionDeclaration(RULE_TESTS_IMMUTABLE, path)
                )
        try:
            messages = self.gateway.pr_commit_messages(p.number)
        except Exception as exc:  # noqa: BLE001 — notice is an arm, not a gate
            self.cfg.log(
                f"review #{p.number}: exemption trailers unreadable ({exc}) — "
                "deferring without a verdict"
            )
            self._evidence_errors[p.number] = f"{type(exc).__name__}: {exc}"[:512]
            return None
        for message in messages:
            for declaration in parse_exemption_declarations(message):
                if declaration.rule == RULE_REQUIRED_TEST:
                    declared.setdefault(declaration)
        if not declared:
            return "", ()
        # The auto-declared set is not bounded by the trailer parser's cap,
        # and every subject lands in the reviewer's TRUSTED prompt tail plus
        # the schema enum. Bound it here too (round-4 MINOR 7). This slice
        # must stay UNREACHABLE for a change within every rule's scope bound:
        # the per-rule caps sum to at most MAX_DECLARATIONS_PER_CHANGE (the
        # exemptions.py invariant), because a declaration truncated here is
        # ungrantable — it vanishes from the schema, the reviewer cannot
        # grant it, and the merge withholds durably for this head. Over-bound
        # changes are denied by the gate before reaching review.
        declared = dict(
            list(declared.items())[:MAX_DECLARATIONS_PER_CHANGE]
        )
        judgments = "\n".join(
            f"- `{rule}`: {self._EXEMPTION_JUDGMENT.get(rule, 'no guidance — refuse')}"
            for rule in {d.rule for d in declared}
        )
        declared_list = "\n".join(
            f'- rule="{d.rule}" subject="{d.subject}"' for d in declared
        )
        notice = (
            "\n\n## Gate exemption REQUESTED\n"
            "This change requests gate exemptions (modified existing test "
            "files are automatic requests; `required-test` is declared by "
            "trailer):\n"
            f"{declared_list}\n"
            "The gate admitted the change so you can judge it; the PR "
            "cannot auto-merge until you explicitly grant "
            "each request. DEFAULT IS REFUSE. Judgment per rule:\n"
            f"{judgments}\n"
            "To grant a declaration, your verdict must be APPROVE and your "
            "structured output must list that exact {rule, subject} pair in "
            "the `exemption_grants` field. Body text NEVER grants — do not "
            "write grant syntax in prose. To refuse, omit the pair (or the "
            "whole field) and explain the refusal in your body. Grants bind "
            "to the current diff; any new push voids them and a fresh "
            "review must re-grant."
        )
        return notice, tuple(declared)

    @staticmethod
    def _with_exemption_notice(
        task, notice: str, declarations: tuple[ExemptionDeclaration, ...]
    ):
        """Attach the exemption notice AND swap in the enum-constrained
        review schema: `exemption_grants` exists only on declared PRs and
        each entry is an object-level enum over exactly the declared
        {rule, subject} pairs, so an undeclared or cross-matched grant is
        unrepresentable at generation time — not merely dropped later.
        A task with a non-review schema (no `decision`) keeps its schema."""
        intent = task.intent
        schema = intent.schema
        if not schema or "decision" not in schema.get("properties", {}):
            # No structured verdict channel → the notice would instruct a
            # field the model cannot emit (round-3 MINOR 9). Skip both; the
            # declared PR then escalates via the withheld arm, fail closed.
            return task
        schema = review_schema_with_exemptions(
            [(d.rule, d.subject) for d in declarations]
        )
        return replace(
            task,
            intent=replace(
                intent, prompt=intent.prompt + notice, schema=schema
            ),
        )

    def _grant_suffix(
        self,
        p,
        signature: str,
        result,
        declarations: tuple[ExemptionDeclaration, ...],
    ) -> str:
        """Render the authenticated grant lines for the reviewer's structured
        `exemption_grants` — the ONLY path that mints grant syntax (ADR
        0014). Validation is allowlist-shaped: decision must be APPROVE, and
        each {rule, subject} entry must match a declaration verbatim;
        anything else is dropped and logged, never guessed at. The patch id
        comes from the harness's own revision-checked diff — the model never
        sees it, so a hallucinated or prompt-injected grant cannot bind."""
        raw = result.field("exemption_grants")
        if not raw or not isinstance(raw, list):
            return ""
        if result.field("decision") != "APPROVE":
            self.cfg.log(
                f"review #{p.number}: exemption_grants on a non-APPROVE "
                "verdict — ignored (only an approval grants)"
            )
            return ""
        if not self.cfg.exemption_mac_key:
            self.cfg.log(
                f"review #{p.number}: reviewer granted exemptions but no "
                "EXEMPTION_MAC_KEY/REVIEW_BOT_TOKEN key material is "
                "configured — grants cannot be authenticated, none minted"
            )
            return ""
        issue = issue_from_branch(p.head_ref_name)
        if issue is None or not signature:
            return ""
        declared = set(declarations)
        lines: dict[str, None] = {}
        dropped = 0
        for entry in raw[:32]:  # a loose runtime can't spam unbounded logs
            if not isinstance(entry, dict):
                continue
            candidate = ExemptionDeclaration(
                str(entry.get("rule", "")), str(entry.get("subject", ""))
            )
            if candidate not in declared:
                dropped += 1
                if dropped <= 5:
                    self.cfg.log(
                        f"review #{p.number}: grant for undeclared "
                        f"exemption rule={candidate.rule!r} "
                        f"subject={candidate.subject!r} — dropped"
                    )
                continue
            if not valid_subject(candidate.subject):
                # An auto-declared diff path outside the grant grammar's
                # charset cannot be rendered; report the real cause instead
                # of silently losing the grant (the PR then escalates with
                # this subject named as ungranted).
                self.cfg.log(
                    f"review #{p.number}: granted subject "
                    f"{candidate.subject!r} is outside the grant grammar "
                    "charset — cannot mint; the merge side will withhold"
                )
                continue
            lines.setdefault(
                render_grant_line(
                    self.cfg.exemption_mac_key,
                    candidate.rule,
                    issue,
                    candidate.subject,
                    signature,
                )
            )
        if dropped > 5:
            self.cfg.log(
                f"review #{p.number}: …and {dropped - 5} more undeclared "
                "grant entries dropped"
            )
        if not lines:
            return ""
        self.cfg.log(
            f"review #{p.number}: reviewer granted {len(lines)} gate "
            "exemption(s) via structured output"
        )
        return "\n\n" + "\n".join(lines)

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

    def _patch_unchanged(
        self,
        p,
        diff: str,
        signature: str,
        claim_target: int | None = None,
    ) -> _PatchShortcut:
        """A settled shortcut when the head advanced but the patch did not.

        `non_resetting_approval_commits` carries an incomplete approval on the
        prior head into the normal review path. Without that evidence a
        mechanical rebase would make the raw APPROVED verdict reset the live
        rejection ceiling even though it never made the PR mergeable.

        The skip is already committed when `settled` is true: the head SHA advanced
        but the PATCH did not: a rebase/amend whose diff against the base is
        identical modulo blob hashes and hunk offsets. The re-review could only
        reach the verdict already sitting on the PR, so the approval is carried
        forward (`require_last_push_approval=false` keeps GitHub's required
        review satisfied across the force-push) and no review agent runs.

        Advancing `last-sha` is what makes this a ONE-TIME skip rather than a new
        per-poll loop: `decide_review` re-emits RE_REVIEW for as long as
        `needs_re_review` is true, and that predicate reads `ReviewState.last_sha`,
        which `app.roles._review_state` loads from exactly this `get_last_sha`
        key. So the write below is the same state the decide loop reads, and the
        next poll sees head == last_sha and dispatches nothing.

        Fail-safe in both directions: no stored signature (a PR reviewed before
        this existed) or an unreadable/unwritable store → return False and review
        normally. An unreviewed revision must never be skipped; a redundant
        review only costs money.
        """
        try:
            stored = self.store.get_last_patch_id(p.number)
        except Exception as exc:  # noqa: BLE001 — an unreadable cache means "review it"
            self.cfg.log(f"re-review #{p.number}: reviewed-patch id unreadable ({exc})")
            return _PatchShortcut()
        if not stored or stored != signature:
            return _PatchShortcut()
        try:
            prior_sha = self.store.get_last_sha(p.number)
        except Exception as exc:  # noqa: BLE001 — an unreadable cache means "review it"
            self.cfg.log(
                f"re-review #{p.number}: reviewed revision unreadable ({exc})"
            )
            return _PatchShortcut()
        prior = review_dom.latest_reviewer_verdict(
            p, self.cfg.review_bot
        )
        if (
            prior is None
            or not p.head_ref_oid
            or not prior_sha
            or prior.commit_id != prior_sha
        ):
            return _PatchShortcut()
        if (
            prior.state == "CHANGES_REQUESTED"
            and review_dom.UNGRANTED_DOWNGRADE_MARKER in prior.body
        ):
            # A downgrade-for-ungranted-exemptions is reviewer-remediable
            # only: the fixer has nothing to change, so copying it onto every
            # mechanical rebase walks an approved-on-the-merits patch straight
            # to the rejection ceiling (tauri #634). A fresh reviewer run is
            # the one act that can grant — take the normal path.
            self.cfg.log(
                f"re-review #{p.number}: unchanged patch, but the standing "
                "verdict is an ungranted-exemption downgrade — reviewing "
                "normally so the reviewer can grant, not copying it"
            )
            return _PatchShortcut()
        if prior.state == "APPROVED":
            complete = self._approval_grants_complete(p, diff)
            if complete is None:
                self._evidence_deferral(
                    p, "exemption declarations unreadable for unchanged patch"
                )
                return _PatchShortcut(settled=True)
            if not complete:
                self.cfg.log(
                    f"re-review #{p.number}: unchanged prior approval lacks "
                    "required authenticated grants — reviewing normally"
                )
                return _PatchShortcut(
                    non_resetting_approval_commits=frozenset({prior_sha})
                )
        event = {
            "APPROVED": "APPROVE",
            "CHANGES_REQUESTED": "REQUEST_CHANGES",
        }.get(prior.state, "COMMENT")
        body = (
            prior.body.strip()
            or "Mechanical rebase: patch is unchanged from the reviewed revision."
        )
        if not self.executor.submit_review(
            p.number, event, body, p.head_ref_oid
        ):
            self.cfg.log(
                f"re-review #{p.number}: unchanged patch verdict could not be "
                "bound to the new head — reviewing normally"
            )
            return _PatchShortcut()
        try:
            self.store.set_last_sha(p.number, p.head_ref_oid)
        except Exception as exc:  # noqa: BLE001 — without this write the skip would repeat
            self.cfg.log(
                f"re-review #{p.number}: unchanged-patch verdict landed on the "
                f"new head, but last-sha could not be advanced ({exc}) — "
                "GitHub reconciliation will settle it next poll"
            )
            return _PatchShortcut(settled=True)
        self.cfg.log(
            f"re-review #{p.number}: head moved to {p.head_ref_oid[:8] or '?'} but the "
            "patch is unchanged (mechanical rebase) — copied the existing verdict "
            "to the new head, no review agent run"
        )
        self._progress(p, claim_target)
        return _PatchShortcut(settled=True)

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
        non_resetting_approval_commits: frozenset[str] = frozenset(),
        bound: BoundReviewRound | None = None,
    ) -> None:
        """The /code-review pattern as dispatcher orchestration: skip-gate →
        parallel dimension specialists → per-finding adversarial validation →
        pure synthesis. Fans the LLM legs out concurrently (each SDK query is its
        own subprocess); the verdict is `review.synthesize_review` (pure). Shares
        the single-pass path's submit-before-persist contract: state advances only
        on a confirmed POST, and the round ceiling still escalates a stuck PR.

        The multi-agent path is stateless per round (no session resume) — every
        round re-derives findings from the CURRENT diff, so a re-review naturally
        re-checks the whole PR rather than only the incremental change. It stores
        a sentinel session id so the watcher's `needs_initial_review` (keyed on a
        non-empty pr-session) flips to re-review on the next SHA advance.

        `p`/`diff` are accepted pre-fetched so `re_review`'s rebase short-circuit
        can inspect the diff before delegating here without paying for a second
        `gh pr view`/`gh pr diff`; omitted (the initial-review path) they are
        fetched here as before."""
        p = self.gateway.get_pr(pr) if p is None else p
        if diff is None:
            diff = self._revision_bound_diff(p)
            if diff is None:
                self._no_progress(p, "revision-bound diff unavailable")
                return
        signature = review_dom.patch_signature(diff)
        rcfg = self.cfg.review
        guidance = self._guidance(pr, bound)
        discussion = self._pr_discussion(p)
        exemption_request = self._exemption_grant_notice(p, diff)
        if exemption_request is None:
            self._evidence_unavailable(p, bound, "exemption declarations unreadable")
            return
        self._evidence_available(bound)
        _notice, declared_exemptions = exemption_request
        grant_resolution = self._grant_resolution(
            p,
            signature,
            body_suffix="",
            declared_exemptions=declared_exemptions,
        )
        # Escalation counts the reviewer's OWN prior rejections, not pushes (a
        # rebase that lands here without a prior REQUEST_CHANGES must not consume a
        # round). Derived from GitHub review state, robust across daemon restarts.
        counted_past = self._counted_past(
            p, bound, non_resetting_approval_commits, signature, declared_exemptions
        )
        prior = self._prior_rejections(p, bound, counted_past)

        # 1. skip-gate — a pure doc/format/config diff short-circuits to APPROVE.
        # No PR conversation here on purpose: its only failure direction ends
        # the review in an APPROVE, and a comment is the wrong evidence for
        # "is this diff behavioural?" (see build_review_gate_task).
        gate = await self._run_logged("review-gate", pr, head_sha=p.head_ref_oid, task=
            task_builders.build_review_gate_task(
                pr,
                as_untrusted(p.title),
                as_untrusted(p.body),
                as_untrusted(diff),
                rcfg,
                operator_guidance=guidance,
            ),
        )
        if self._pin_deferral(p, gate):
            return
        if gate.is_error:
            # transport failure — leave state untouched, retry next poll (but
            # COUNT the round: the retry is not free, and an is_error gate that
            # keeps repeating is the cheapest shape of the P0R-3 loop).
            self._no_progress(p, "gate leg errored")
            return
        # Fail SAFE: a degenerate gate result (is_error=False but empty envelope)
        # must default to reviewable=True — the conservative choice is to RUN the
        # full review, never to silently APPROVE unreviewed code. Only an explicit
        # reviewable=false short-circuits.
        if not gate.field_bool("reviewable", default=True):
            target = review_dom.ReviewTarget(
                issue_from_branch(p.head_ref_name), signature
            )
            sub = review_dom.gate_submission(
                prior,
                self.cfg.max_review_rejections,
                grant_resolution=grant_resolution,
                target=target,
            )
            self.cfg.log(
                f"review #{pr}: gate says non-reviewable "
                f"({gate.field('reason') or 'no behavioural change'}) "
                f"→ {sub.event}"
            )
            if self._post(p, sub, bound, signature, counted_past):
                if sub.escalate:
                    self._escalate_unbound(bound, pr, prior + 1, sub.escalate_cause)
                self._persist_review_state(pr, p.head_ref_oid, round_number, signature, bound=bound)
                self._progress(p, claim_target)
            else:
                self._no_progress(p, f"gate {sub.event} POST failed")
            return

        # 2. dimension specialists — parallel, independent, each hunts one lens.
        # An errored leg makes the review INCOMPLETE (a defect-detecting lens may
        # not have run) — tracked so synthesis never emits a silent APPROVE on a
        # partial result; it retries next poll instead.
        dims = task_builders.REVIEW_DIMENSIONS
        dim_results = await self._gather_legs([
            self._run_logged(f"review:{key}", pr, head_sha=p.head_ref_oid, task=
                task_builders.build_review_dimension_task(
                    pr,
                    key,
                    focus,
                    as_untrusted(p.title),
                    as_untrusted(p.body),
                    as_untrusted(diff),
                    rcfg,
                    operator_guidance=guidance,
                    discussion=discussion,
                )
            )
            for key, focus in dims
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
            verdicts = await self._gather_legs([
                self._run_logged(f"validate:{f.dimension}", pr, head_sha=p.head_ref_oid, task=
                    task_builders.build_review_validation_task(
                        pr,
                        f.dimension,
                        as_untrusted(f.location),
                        as_untrusted(f.claim),
                        as_untrusted(diff),
                        rcfg,
                    )
                )
                for f in candidates
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
            grant_resolution=grant_resolution,
            target=review_dom.ReviewTarget(
                issue_from_branch(p.head_ref_name), signature
            ),
        )
        if sub is None:
            self.cfg.log(f"review #{pr}: incomplete and no blocking finding — "
                         "submitting nothing, will retry next poll")
            # The expensive shape: gate + every dimension + every validator were
            # PAID FOR and the whole fan-out re-runs next poll on a returned
            # result, never touching the dispatcher's raise-path counter.
            self._no_progress(p, "incomplete fan-out, no verdict to submit")
            return
        if not self._post(p, sub, bound, signature, counted_past):
            self._no_progress(p, "review POST failed")
            return  # failed POST → leave state untouched (retried next poll)
        if sub.escalate:
            self._escalate_unbound(bound, pr, prior + 1, sub.escalate_cause)
        self._persist_review_state(pr, p.head_ref_oid, round_number, signature, bound=bound)
        self._progress(p, claim_target)

    def _progress(self, p, claim_target: int | None = None) -> None:
        """A round DID advance: clear the no-progress tally (the reviewer's twin
        of review P1R-3's decay).

        Without this the ceiling is a LIFETIME tally: a long-lived PR that
        accumulates five flaky rounds over weeks of otherwise healthy reviewing
        escalates on its next needed review, having never actually looped. The
        counter's question is "is this PR stuck in a non-converging round?", and
        a confirmed submit answers no — so success resets it, exactly as the fix
        role's legs do. Keyed off the PRView so it matches `_no_progress`'s
        `breaker_key(pr)` write with no extra gh call.

        BOTH halves of the key space are cleared, because `roles._review_state`
        READS their union (review P1R-1): the dispatcher records a raise-path
        death under `-pr` whenever its `issue_for_pr` lookup failed, and the
        transient that fails that lookup is the same transient that kills the
        session. Clearing only the resolved key would strand that death forever
        — it reads as real on every later poll and walks a healthy PR toward
        needs-human-review. Decay must clear exactly the set the read unions."""
        # Clear the stable key plus the current issue key used by deployments
        # predating the PR-keyed policy. New failures are written only to -PR.
        legacy = (
            claim_target
            if claim_target is not None
            else breaker_key(p)
        )
        for key in {-p.number, legacy}:
            try:
                self.store.clear_infra_deaths(key)
            except Exception as exc:  # noqa: BLE001 — decay is best-effort, never fatal
                self.cfg.log(f"review #{p.number}: no-progress counter not cleared ({exc})")
        # The pin-deferral streak clears on confirmed PROGRESS, not on a
        # successful pin: multi-mode's gate pins successfully every round even
        # when a later dimension defers the round, and a pin-time reset would
        # let that loop evade the deferral bound forever.
        self._pin_deferrals.pop(p.number, None)
        self._evidence_deferrals.pop(p.number, None)
        # A settled round no longer needs its pinned checkout; the next round
        # recreates one at the then-current head. Best-effort — a leftover
        # worktree is a directory, not a correctness hazard.
        if self.cfg.review_checkout_cleanup is not None:
            try:
                self.cfg.review_checkout_cleanup(p.number)
            except Exception as exc:  # noqa: BLE001
                self.cfg.log(
                    f"review #{p.number}: pinned checkout not removed ({exc})"
                )

    def _persist_review_state(
        self, pr: int, head_oid: str, round_number: int, signature: str,
        *, bound: BoundReviewRound | None = None,
    ) -> None:
        """Advance per-PR review state after a confirmed submit. The multi-agent
        path has no SDK session to record, so a sentinel marks the PR reviewed-once
        (so the watcher moves to re-review on the next SHA advance). `signature`
        records WHAT was reviewed, so the next head advance can be classified as a
        real revision or a mechanical rebase without paying for a round."""
        self.store.save_pr_session(pr, self.store.get_pr_session(pr) or "multi")
        if bound is None:
            self.store.set_last_sha(pr, head_oid)
            self._record_patch(pr, signature)
        self.store.set_round(pr, round_number)

    def _submit_review(
        self,
        p,
        expected_head: str,
        result,
        round_number: int,
        prior_rejections: int,
        patch_id: str,
        body_suffix: str = "",
        declared_exemptions: tuple[ExemptionDeclaration, ...] = (),
        bound: BoundReviewRound | None = None,
        counted_past: frozenset[str] = frozenset(),
    ) -> bool:
        """Submit the verdict; return True iff the gh POST succeeded. On a failed
        submit, return False WITHOUT escalating/relabelling — the caller leaves state
        untouched so the PR is retried next poll rather than stranded as "reviewed".
        `round_number` is for the human-facing log line only; `prior_rejections`
        (the reviewer's own prior REQUEST_CHANGES count) drives the escalation
        ceiling. `body_suffix` is HARNESS-authored trusted text appended after
        the model's body — today the ADR 0014 grant lines from `_grant_suffix`,
        the only place grant syntax is minted. `declared_exemptions` is the
        revision-derived set the same review was asked to grant. Completeness
        aggregates authenticated grants from every existing reviewer approval
        plus the current suffix: ADR 0014 binds authority to patch content, so
        a later omission cannot revoke an earlier valid grant for this patch."""
        pr = p.number
        grant_resolution = self._grant_resolution(
            p,
            patch_id,
            body_suffix=body_suffix,
            declared_exemptions=declared_exemptions,
        )
        rejected_exemptions = (
            grant_resolution.requests
            if isinstance(grant_resolution, review_dom.RejectedExemptions)
            else ()
        )
        # A partial grant cannot survive a REQUEST_CHANGES review: the merge
        # collector intentionally trusts grant lines only in APPROVED reviews.
        # Drop all newly minted lines on this round and require a complete set
        # across existing approvals plus the next approval, rather than publishing
        # authority-shaped text in a review whose actual event denies the patch.
        effective_suffix = "" if rejected_exemptions else body_suffix
        body = result.field("body") or ""
        if effective_suffix:
            # A model body ending inside an unclosed fence would swallow the
            # harness's grant lines and later escalate with a false "the
            # reviewer did not grant" (round-3 MINOR 7). Close it first.
            dangling = open_fence_marker(body)
            if dangling:
                body += f"\n{dangling}"
        sub = review_dom.review_submission(
            result.field("decision"), body + effective_suffix,
            prior_rejections, self.cfg.max_review_rejections,
            grant_resolution=grant_resolution,
            target=review_dom.ReviewTarget(
                issue_from_branch(p.head_ref_name), patch_id
            ),
        )
        if bound is None:
            if not self.executor.submit_review(
                pr, sub.event, sub.body, expected_head
            ):
                return False
        elif not self._post(p, sub, bound, patch_id, counted_past):
            return False
        # Log the OUTCOME of a successful submit — always on (not DEBUG-gated).
        escalated = " → escalated (needs-human-review)" if sub.escalate else ""
        # Show the rejection tally that actually drives escalation, not merely
        # the push-derived round number.
        tally = (
            f", rejection {prior_rejections + 1}/{self.cfg.max_review_rejections}"
            if sub.event != "APPROVE" else ""
        )
        self.cfg.log(
            f"review #{pr} r{round_number}{tally}: submitted {sub.event}{escalated}"
        )
        if sub.escalate:
            self._escalate_unbound(bound, pr, prior_rejections + 1, sub.escalate_cause)
        return True

    def _grant_resolution(
        self,
        p,
        patch_id: str,
        *,
        body_suffix: str,
        declared_exemptions: tuple[ExemptionDeclaration, ...],
    ) -> review_dom.GrantResolution:
        """Prove complete grants or return the exact requests that forbid APPROVE."""
        grants = [
            grant
            for review in p.reviews
            if review.state == "APPROVED"
            and login_eq(review.author, self.cfg.review_bot)
            for grant in parse_exemption_grants(review.body)
        ]
        grants.extend(parse_exemption_grants(body_suffix))
        granted = authenticated_grant_pairs(
            grants,
            issue_number=issue_from_branch(p.head_ref_name),
            patch_id=patch_id,
            mac_key=self.cfg.exemption_mac_key,
        )
        required = tuple(
            (declaration.rule, declaration.subject)
            for declaration in declared_exemptions
        )
        return review_dom.resolve_authenticated_grants(
            required,
            granted,
            target=review_dom.ReviewTarget(
                issue_from_branch(p.head_ref_name), patch_id
            ),
        )
