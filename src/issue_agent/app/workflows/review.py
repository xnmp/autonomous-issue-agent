"""Review workflow continuation under ADR 0009.

Owns single, delegated, and multi-agent review dispatch outside the central
AgentDispatcher so review behavior can evolve behind a role-specific module.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

from ...domain import review as review_dom
from ...domain.events import (
    Correlation,
    ReviewSkipped,
    TelemetryEvent,
    TelemetryWarning,
)
from ...domain.leg import Leg, LegTarget
from ...domain.trust import as_untrusted
from ..planners import tasks as task_builders
from ..planners.tasks import ReviewTaskConfig
from ...adapters.runtime_plan import AgentRuntime
from ...domain.agent_task import (
    RUNTIME_CAPABILITIES,
    RuntimeCapabilities,
    RuntimeName,
)
from ...domain.fix import breaker_key
from ...domain.labels import Label
from ...domain.lifecycle import pr_escalation
from ...domain.outcome import AgentResult
from ...domain.ports import (
    ActionExecutor,
    EventSink,
    WorkflowGitHubReads,
    WorkflowState,
)
from .base import run_leg

_EXTERNAL_REVIEW_SESSION = "github-review"
_FRESH_REVIEW_SESSION = "codex-review"


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

    def _escalate(self, pr: int, rejections: int) -> None:
        """The reviewer is out of rounds → hand the PR to a human, THROUGH the
        write funnel (review M10/D4).

        This used to be a bare `gateway.relabel(..., "needs-human-review")` with
        a raw string, so the one state change an operator most needs to hear
        about — the reviewer giving up on a PR — emitted no `escalation` journal
        event and fired no ALERT_CMD, while the spend-cap escalation on the very
        same daemon did. `Executor` detects the needs-human-review move itself,
        so routing the actions through it is what restores both."""
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
        # every tick, so surface it on the dashboard warning feed (ADR 0008).
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

    async def _run_logged(self, label: str, pr: int, task):
        task = self._prepare_task(task)
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

    def _prepare_task(self, task):
        """Install Codex's review governor immediately before each agent leg.

        A builder-time write can mutate whichever checkout happens to run a
        test, including an in-flight fixer worktree.  Review has no worktree of
        its own, so the configured target checkout is the sole permitted hook
        destination and is also made the task's execution cwd.
        """
        if not self.cfg.reviewer_capabilities.requires_external_governor:
            return task
        if not self.cfg.execution_cwd or self.cfg.prepare_cwd is None:
            self.cfg.log(
                "review: Codex containment unavailable — set TARGET_DIR to the "
                "review checkout; refusing to run without a governor hook"
            )
            return None
        try:
            self.cfg.prepare_cwd(self.cfg.execution_cwd)
        except OSError as exc:
            self.cfg.log(
                f"review: Codex containment setup failed in {self.cfg.execution_cwd}: {exc}; "
                "refusing to run"
            )
            return None
        return replace(task, cwd=self.cfg.execution_cwd)

    async def review(
        self, pr: int, *, claim_target: int | None = None
    ) -> None:
        p = self.gateway.get_pr(pr)
        if not self._claim_matches(p, claim_target):
            return
        if self._already_reviewed(p):
            return
        if self.cfg.review_mode is review_dom.ReviewMode.MULTI:
            await self._review_multi(
                pr,
                round_number=1,
                p=p,
                claim_target=claim_target,
            )
            return
        # SINGLE and DELEGATED share the submit-before-persist contract; they differ
        # only in the task builder (delegated attaches a code-scout subagent).
        diff = self._revision_bound_diff(p)
        if diff is None:
            self._no_progress(p, "revision-bound diff unavailable")
            return
        signature = review_dom.patch_signature(diff)
        if self.cfg.review_mode is review_dom.ReviewMode.DELEGATED:
            task = task_builders.build_delegated_review_task(
                pr,
                as_untrusted(p.title),
                as_untrusted(p.body),
                as_untrusted(diff),
                self.cfg.review,
            )
        else:
            task = task_builders.build_review_task(
                pr,
                as_untrusted(p.title),
                as_untrusted(p.body),
                as_untrusted(diff),
                self.cfg.review,
            )
        result = await self._run_logged("review", pr, task)
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
        prior = review_dom.count_prior_rejections(p.reviews, self.cfg.review_bot)
        if not self._submit_review(
            pr, p.head_ref_oid, result, 1, prior
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
        self.store.set_last_sha(pr, p.head_ref_oid)
        self._record_patch(pr, signature)
        self.store.set_round(pr, 1)
        self._progress(p, claim_target)

    async def re_review(
        self, pr: int, *, claim_target: int | None = None
    ) -> None:
        # The diff comes first, in EVERY mode: a head advance that leaves the
        # patch unchanged (a mechanical rebase) must be settled by one `gh pr
        # diff` rather than by a paid review round — most of all under MULTI,
        # whose round is a gate + 5 dimensions + N validators (harness #2).
        p = self.gateway.get_pr(pr)
        if not self._claim_matches(p, claim_target):
            return
        if self._already_reviewed(p):
            return
        diff = self._revision_bound_diff(p)
        if diff is None:
            self._no_progress(p, "revision-bound diff unavailable")
            return
        signature = review_dom.patch_signature(diff)
        if self._patch_unchanged(p, signature):
            return
        round_number = self.store.get_round(pr) + 1
        if self.cfg.review_mode is review_dom.ReviewMode.MULTI:
            await self._review_multi(
                pr,
                round_number=round_number,
                p=p,
                diff=diff,
                claim_target=claim_target,
            )
            return
        last_sha = self.store.get_last_sha(pr)
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
                )
            else:
                task = task_builders.build_re_review_task(
                    pr,
                    round_number,
                    last_sha,
                    p.head_ref_oid,
                    as_untrusted(diff),
                    session_id, self.cfg.review,
                )
        result = await self._run_logged("re-review", pr, task)
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
        prior = review_dom.count_prior_rejections(p.reviews, self.cfg.review_bot)
        if not self._submit_review(
            pr, p.head_ref_oid, result, round_number, prior
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
        self.store.set_last_sha(pr, p.head_ref_oid)
        self._record_patch(pr, signature)
        self.store.set_round(pr, round_number)
        self._progress(p, claim_target)

    def _already_reviewed(self, p) -> bool:
        """Settle a retried dispatch when GitHub already has our verdict on HEAD."""
        if not review_dom.reviewer_verdict_on_head(p, self.cfg.review_bot):
            return False
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
        return True

    # ── rebase short-circuit (harness #2) ────────────────────────────────────
    def _record_patch(self, pr: int, signature: str) -> None:
        """Remember WHAT the review just looked at, next to `last-sha`'s WHICH.
        Best-effort: a store that cannot record it only costs the next rebase a
        review round, whereas raising here would undo a submitted verdict."""
        try:
            self.store.set_last_patch_id(pr, signature)
        except Exception as exc:  # noqa: BLE001 — a cache write must never fail a review
            self.cfg.log(f"review #{pr}: reviewed-patch id not persisted ({exc})")

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

    def _patch_unchanged(self, p, signature: str) -> bool:
        """True — and the skip is already committed — when the head SHA advanced
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
            return False
        if not stored or stored != signature:
            return False
        try:
            prior_sha = self.store.get_last_sha(p.number)
        except Exception as exc:  # noqa: BLE001 — an unreadable cache means "review it"
            self.cfg.log(
                f"re-review #{p.number}: reviewed revision unreadable ({exc})"
            )
            return False
        prior = review_dom.latest_reviewer_verdict(
            p, self.cfg.review_bot
        )
        if (
            prior is None
            or not p.head_ref_oid
            or not prior_sha
            or prior.commit_id != prior_sha
        ):
            return False
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
            return False
        try:
            self.store.set_last_sha(p.number, p.head_ref_oid)
        except Exception as exc:  # noqa: BLE001 — without this write the skip would repeat
            self.cfg.log(
                f"re-review #{p.number}: unchanged-patch verdict landed on the "
                f"new head, but last-sha could not be advanced ({exc}) — "
                "GitHub reconciliation will settle it next poll"
            )
            return True
        self.cfg.log(
            f"re-review #{p.number}: head moved to {p.head_ref_oid[:8] or '?'} but the "
            "patch is unchanged (mechanical rebase) — copied the existing verdict "
            "to the new head, no review agent run"
        )
        return True

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
        # Escalation counts the reviewer's OWN prior rejections, not pushes (a
        # rebase that lands here without a prior REQUEST_CHANGES must not consume a
        # round). Derived from GitHub review state, robust across daemon restarts.
        prior = review_dom.count_prior_rejections(p.reviews, self.cfg.review_bot)

        # 1. skip-gate — a pure doc/format/config diff short-circuits to APPROVE.
        gate = await self._run_logged("review-gate", pr,
            task_builders.build_review_gate_task(
                pr,
                as_untrusted(p.title),
                as_untrusted(p.body),
                as_untrusted(diff),
                rcfg,
            ),
        )
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
            self.cfg.log(f"review #{pr}: gate says non-reviewable "
                         f"({gate.field('reason') or 'no behavioural change'}) → APPROVE")
            sub = review_dom.gate_submission(prior, self.cfg.max_review_rejections)
            if self.executor.submit_review(
                pr, sub.event, sub.body, p.head_ref_oid
            ):
                self._persist_review_state(pr, p.head_ref_oid, round_number, signature)
                self._progress(p, claim_target)
            else:
                self._no_progress(p, "gate APPROVE POST failed")
            return

        # 2. dimension specialists — parallel, independent, each hunts one lens.
        # An errored leg makes the review INCOMPLETE (a defect-detecting lens may
        # not have run) — tracked so synthesis never emits a silent APPROVE on a
        # partial result; it retries next poll instead.
        dims = task_builders.REVIEW_DIMENSIONS
        dim_results = await self._gather_legs([
            self._run_logged(f"review:{key}", pr,
                task_builders.build_review_dimension_task(
                    pr,
                    key,
                    focus,
                    as_untrusted(p.title),
                    as_untrusted(p.body),
                    as_untrusted(diff),
                    rcfg,
                )
            )
            for key, focus in dims
        ])
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

        # 3. adversarial validation — one refuter per candidate, in parallel. A
        # validator transport error is NOT a refutation: it also marks the review
        # incomplete (so a genuine finding whose validator died is not silently
        # dropped into an APPROVE), while confirmed stays False so it isn't reported
        # as a fact either — the PR is simply retried next poll.
        if candidates:
            verdicts = await self._gather_legs([
                self._run_logged(f"validate:{f.dimension}", pr,
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
            validated, prior, self.cfg.max_review_rejections, complete=complete)
        if sub is None:
            self.cfg.log(f"review #{pr}: incomplete and no blocking finding — "
                         "submitting nothing, will retry next poll")
            # The expensive shape: gate + every dimension + every validator were
            # PAID FOR and the whole fan-out re-runs next poll on a returned
            # result, never touching the dispatcher's raise-path counter.
            self._no_progress(p, "incomplete fan-out, no verdict to submit")
            return
        if not self.executor.submit_review(
            pr, sub.event, sub.body, p.head_ref_oid
        ):
            self._no_progress(p, "review POST failed")
            return  # failed POST → leave state untouched (retried next poll)
        if sub.escalate:
            self._escalate(pr, prior + 1)
        self._persist_review_state(pr, p.head_ref_oid, round_number, signature)
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

    def _persist_review_state(
        self, pr: int, head_oid: str, round_number: int, signature: str
    ) -> None:
        """Advance per-PR review state after a confirmed submit. The multi-agent
        path has no SDK session to record, so a sentinel marks the PR reviewed-once
        (so the watcher moves to re-review on the next SHA advance). `signature`
        records WHAT was reviewed, so the next head advance can be classified as a
        real revision or a mechanical rebase without paying for a round."""
        self.store.save_pr_session(pr, self.store.get_pr_session(pr) or "multi")
        self.store.set_last_sha(pr, head_oid)
        self._record_patch(pr, signature)
        self.store.set_round(pr, round_number)

    def _submit_review(
        self,
        pr: int,
        expected_head: str,
        result,
        round_number: int,
        prior_rejections: int,
    ) -> bool:
        """Submit the verdict; return True iff the gh POST succeeded. On a failed
        submit, return False WITHOUT escalating/relabelling — the caller leaves state
        untouched so the PR is retried next poll rather than stranded as "reviewed".
        `round_number` is for the human-facing log line only; `prior_rejections`
        (the reviewer's own prior REQUEST_CHANGES count) drives the escalation
        ceiling."""
        sub = review_dom.review_submission(
            result.field("decision"), result.field("body"),
            prior_rejections, self.cfg.max_review_rejections,
        )
        if not self.executor.submit_review(
            pr, sub.event, sub.body, expected_head
        ):
            return False
        # Log the OUTCOME of a successful submit — always on (not DEBUG-gated). Without
        # this, a clean review was invisible on stdout: `dispatch` then silence until
        # the next poll (the per-message sdk: trace is DEBUG-only, and only the ERROR
        # path logged). The verdict is the one line an operator needs to see land.
        escalated = " → escalated (needs-human-review)" if sub.escalate else ""
        # Show the rejection tally (what actually drives escalation) for a
        # REQUEST_CHANGES/escalate, not just the push-based round_number — the
        # latter is derived from stored state and reads wrong after a STATE_DIR
        # wipe, whereas prior_rejections is recomputed from GitHub every time.
        tally = (
            f", rejection {prior_rejections + 1}/{self.cfg.max_review_rejections}"
            if sub.event != "APPROVE" else ""
        )
        self.cfg.log(
            f"review #{pr} r{round_number}{tally}: submitted {sub.event}{escalated}"
        )
        if sub.escalate:
            self._escalate(pr, prior_rejections + 1)
        return True
