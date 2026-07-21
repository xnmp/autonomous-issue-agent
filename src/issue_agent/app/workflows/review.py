"""Review workflow dispatch.

Owns single, delegated, and multi-agent review dispatch outside the central
AgentDispatcher so review behavior can evolve behind a role-specific module.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace

from ...domain import review as review_dom
from ..planners import tasks as task_builders
from ..planners.tasks import ReviewTaskConfig
from ...domain.agent_task import AgentRuntime
from ...domain.outcome import AgentResult


@dataclass(frozen=True)
class ReviewWorkflowConfig:
    review: ReviewTaskConfig | None
    review_mode: review_dom.ReviewMode
    max_review_rounds: int
    review_trigger_label: str
    log: Callable[[str], None]


@dataclass
class ReviewWorkflow:
    cfg: ReviewWorkflowConfig
    gateway: object
    store: object
    runner: AgentRuntime
    log_run: Callable[[str, int, object], object] | None = None

    async def _run_logged(self, label: str, pr: int, task):
        if self.log_run is not None:
            return await self.log_run(label, pr, task)
        self.cfg.log(f"agent #{pr}: {label} — running")
        result = await self.runner.run(task)
        self.cfg.log(
            f"agent #{pr}: {label} — done "
            f"(status={result.status or ('error' if result.is_error else 'ok')}"
            f"{f', ${result.cost_usd:.2f}' if result.cost_usd else ''})"
        )
        return result

    async def review(self, pr: int) -> None:
        if self.cfg.review_mode is review_dom.ReviewMode.MULTI:
            await self._review_multi(pr, round_number=1)
            return
        # SINGLE and DELEGATED share the submit-before-persist contract; they differ
        # only in the task builder (delegated attaches a code-scout subagent).
        p = self.gateway.get_pr(pr)
        diff = self.gateway.pr_diff(pr)
        if self.cfg.review_mode is review_dom.ReviewMode.DELEGATED:
            task = task_builders.build_delegated_review_task(
                pr, p.title, p.body, diff, self.cfg.review
            )
        else:
            task = task_builders.build_review_task(
                pr, p.title, p.body, diff, self.cfg.review
            )
        result = await self.runner.run(task)
        if result.is_error:
            self.cfg.log(
                f"review #{pr}: agent errored (status={result.status or 'error'}, "
                f"cost=${result.cost_usd:.2f}) — no verdict, will retry. If cost is "
                f"near the budget cap, raise REVIEW_BUDGET_USD (delegated review + "
                f"scout subagents share one budget)."
            )
            return
        # Submit BEFORE persisting "reviewed" state. If the POST fails (e.g. a 422
        # self-approval from a misconfigured identity), we must NOT record a session/
        # sha — otherwise the PR looks reviewed-once and never gets retried, with no
        # review on it. State advances only on a confirmed submit.
        if not self._submit_review(pr, result, 1):
            return
        self.store.save_pr_session(pr, result.session_id)
        self.store.set_last_sha(pr, p.head_ref_oid)
        self.store.set_round(pr, 1)

    async def re_review(self, pr: int) -> None:
        if self.cfg.review_mode is review_dom.ReviewMode.MULTI:
            await self._review_multi(pr, round_number=self.store.get_round(pr) + 1)
            return
        p = self.gateway.get_pr(pr)
        round_number = self.store.get_round(pr) + 1
        last_sha = self.store.get_last_sha(pr)
        diff = self.gateway.pr_diff(pr)
        if self.cfg.review_mode is review_dom.ReviewMode.DELEGATED:
            # Fresh session (no resume) → feed the prior review body as reference
            # instead of replaying the transcript. Use the most recent review with a
            # body (the reviewer's own last verdict).
            prior_review = next(
                (r.body for r in reversed(p.reviews) if r.body.strip()), ""
            )
            task = task_builders.build_delegated_re_review_task(
                pr, round_number, last_sha, p.head_ref_oid, diff,
                prior_review, self.cfg.review,
            )
        else:
            task = task_builders.build_re_review_task(
                pr, round_number, last_sha, p.head_ref_oid, diff,
                self.store.get_pr_session(pr), self.cfg.review,
            )
        result = await self.runner.run(task)
        if result.is_error:
            # do NOT advance sha/round — retry same diff next poll. Log cost so a
            # budget-cap abort (delegated review + scout subagents share one budget)
            # is visible rather than a silent re-review loop.
            self.cfg.log(
                f"re-review #{pr} r{round_number}: agent errored "
                f"(status={result.status or 'error'}, cost=${result.cost_usd:.2f}) — "
                f"no verdict, will retry. Raise REVIEW_BUDGET_USD if cost is near cap."
            )
            return
        # Submit first; advance sha/round only on a confirmed submit (see _review).
        if not self._submit_review(pr, result, round_number):
            return
        self.store.set_last_sha(pr, p.head_ref_oid)
        self.store.set_round(pr, round_number)

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

    async def _review_multi(self, pr: int, round_number: int) -> None:
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
        non-empty pr-session) flips to re-review on the next SHA advance."""
        p = self.gateway.get_pr(pr)
        diff = self.gateway.pr_diff(pr)
        rcfg = self.cfg.review

        # 1. skip-gate — a pure doc/format/config diff short-circuits to APPROVE.
        gate = await self._run_logged("review-gate", pr,
            task_builders.build_review_gate_task(pr, p.title, p.body, diff, rcfg))
        if gate.is_error:
            return  # transport failure — leave state untouched, retry next poll
        # Fail SAFE: a degenerate gate result (is_error=False but empty envelope)
        # must default to reviewable=True — the conservative choice is to RUN the
        # full review, never to silently APPROVE unreviewed code. Only an explicit
        # reviewable=false short-circuits.
        if not gate.field_bool("reviewable", default=True):
            self.cfg.log(f"review #{pr}: gate says non-reviewable "
                         f"({gate.field('reason') or 'no behavioural change'}) → APPROVE")
            sub = review_dom.gate_submission(round_number, self.cfg.max_review_rounds)
            if self.gateway.submit_review(pr, sub.event, sub.body):
                self._persist_review_state(pr, p.head_ref_oid, round_number)
            return

        # 2. dimension specialists — parallel, independent, each hunts one lens.
        # An errored leg makes the review INCOMPLETE (a defect-detecting lens may
        # not have run) — tracked so synthesis never emits a silent APPROVE on a
        # partial result; it retries next poll instead.
        dims = task_builders.REVIEW_DIMENSIONS
        dim_results = await self._gather_legs([
            self._run_logged(f"review:{key}", pr,
                task_builders.build_review_dimension_task(
                    pr, key, focus, p.title, p.body, diff, rcfg))
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
                        pr, f.dimension, f.location, f.claim, diff, rcfg))
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
            validated, round_number, self.cfg.max_review_rounds, complete=complete)
        if sub is None:
            self.cfg.log(f"review #{pr}: incomplete and no blocking finding — "
                         "submitting nothing, will retry next poll")
            return
        if not self.gateway.submit_review(pr, sub.event, sub.body):
            return  # failed POST → leave state untouched (retried next poll)
        if sub.escalate:
            self.gateway.relabel("pr", pr, self.cfg.review_trigger_label, "needs-human-review")
        self._persist_review_state(pr, p.head_ref_oid, round_number)

    def _persist_review_state(self, pr: int, head_oid: str, round_number: int) -> None:
        """Advance per-PR review state after a confirmed submit. The multi-agent
        path has no SDK session to record, so a sentinel marks the PR reviewed-once
        (so the watcher moves to re-review on the next SHA advance)."""
        self.store.save_pr_session(pr, self.store.get_pr_session(pr) or "multi")
        self.store.set_last_sha(pr, head_oid)
        self.store.set_round(pr, round_number)

    def _submit_review(self, pr: int, result, round_number: int) -> bool:
        """Submit the verdict; return True iff the gh POST succeeded. On a failed
        submit, return False WITHOUT escalating/relabelling — the caller leaves state
        untouched so the PR is retried next poll rather than stranded as "reviewed"."""
        sub = review_dom.review_submission(
            result.field("decision"), result.field("body"),
            round_number, self.cfg.max_review_rounds,
        )
        if not self.gateway.submit_review(pr, sub.event, sub.body):
            return False
        if sub.escalate:
            self.gateway.relabel("pr", pr, self.cfg.review_trigger_label, "needs-human-review")
        return True
