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
from ...domain.fix import breaker_key
from ...domain.labels import Label
from ...domain.lifecycle import pr_escalation
from ...domain.outcome import AgentResult


@dataclass(frozen=True)
class ReviewWorkflowConfig:
    review: ReviewTaskConfig | None
    review_mode: review_dom.ReviewMode
    max_review_rejections: int
    review_bot: str
    review_trigger_label: str
    log: Callable[[str], None]


@dataclass
class ReviewWorkflow:
    cfg: ReviewWorkflowConfig
    gateway: object
    store: object
    runner: AgentRuntime
    # Required, not optional: escalation writes go through the funnel or not at
    # all (review M10). A default would let a caller silently reintroduce the
    # direct-gateway bypass this field exists to remove.
    executor: object
    log_run: Callable[[str, int, object], object] | None = None

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

    def _no_progress(self, pr, reason: str) -> None:
        """Record that a PAID review round advanced nothing (review P0R-3).

        Every `return` in this class that skips `save_pr_session`/`set_last_sha`
        leaves `needs_initial_review`/`needs_re_review` True, so the identical
        dispatch is bought again on the next poll — under REVIEW_MODE=multi that
        is a gate + 5 dimensions + N validators, re-run every poll on a RESULT
        the workflow chose not to use. Nothing counted those rounds: the
        dispatcher's `except BaseException` bump only fires when the leg RAISED
        out of the workflow, and these paths all return normally.

        Written under `breaker_key(pr)` — the same counter the dispatcher's
        death bump uses, and `app.roles._review_state` reads the union of that
        key and `-pr.number`, so writer and reader agree whichever branch each
        took. `pr` is the PRView (not the number) so the key is derived purely,
        with no extra gh call on a path that is already a failure.
        """
        key = breaker_key(pr)
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
            self._no_progress(p, f"agent error: {result.status or 'error'}")
            return
        # Submit BEFORE persisting "reviewed" state. If the POST fails (e.g. a 422
        # self-approval from a misconfigured identity), we must NOT record a session/
        # sha — otherwise the PR looks reviewed-once and never gets retried, with no
        # review on it. State advances only on a confirmed submit.
        prior = review_dom.count_prior_rejections(p.reviews, self.cfg.review_bot)
        if not self._submit_review(pr, result, 1, prior):
            self._no_progress(p, "review POST failed")
            return
        self.store.save_pr_session(pr, result.session_id)
        self.store.set_last_sha(pr, p.head_ref_oid)
        self.store.set_round(pr, 1)
        self._progress(p)

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
            self._no_progress(p, f"agent error: {result.status or 'error'}")
            return
        # Submit first; advance sha/round only on a confirmed submit (see _review).
        prior = review_dom.count_prior_rejections(p.reviews, self.cfg.review_bot)
        if not self._submit_review(pr, result, round_number, prior):
            self._no_progress(p, "review POST failed")
            return
        self.store.set_last_sha(pr, p.head_ref_oid)
        self.store.set_round(pr, round_number)
        self._progress(p)

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
        # Escalation counts the reviewer's OWN prior rejections, not pushes (a
        # rebase that lands here without a prior REQUEST_CHANGES must not consume a
        # round). Derived from GitHub review state, robust across daemon restarts.
        prior = review_dom.count_prior_rejections(p.reviews, self.cfg.review_bot)

        # 1. skip-gate — a pure doc/format/config diff short-circuits to APPROVE.
        gate = await self._run_logged("review-gate", pr,
            task_builders.build_review_gate_task(pr, p.title, p.body, diff, rcfg))
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
            if self.gateway.submit_review(pr, sub.event, sub.body):
                self._persist_review_state(pr, p.head_ref_oid, round_number)
                self._progress(p)
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
            validated, prior, self.cfg.max_review_rejections, complete=complete)
        if sub is None:
            self.cfg.log(f"review #{pr}: incomplete and no blocking finding — "
                         "submitting nothing, will retry next poll")
            # The expensive shape: gate + every dimension + every validator were
            # PAID FOR and the whole fan-out re-runs next poll on a returned
            # result, never touching the dispatcher's raise-path counter.
            self._no_progress(p, "incomplete fan-out, no verdict to submit")
            return
        if not self.gateway.submit_review(pr, sub.event, sub.body):
            self._no_progress(p, "review POST failed")
            return  # failed POST → leave state untouched (retried next poll)
        if sub.escalate:
            self._escalate(pr, prior + 1)
        self._persist_review_state(pr, p.head_ref_oid, round_number)
        self._progress(p)

    def _progress(self, p) -> None:
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
        for key in {breaker_key(p), -p.number}:
            try:
                self.store.clear_infra_deaths(key)
            except Exception as exc:  # noqa: BLE001 — decay is best-effort, never fatal
                self.cfg.log(f"review #{p.number}: no-progress counter not cleared ({exc})")

    def _persist_review_state(self, pr: int, head_oid: str, round_number: int) -> None:
        """Advance per-PR review state after a confirmed submit. The multi-agent
        path has no SDK session to record, so a sentinel marks the PR reviewed-once
        (so the watcher moves to re-review on the next SHA advance)."""
        self.store.save_pr_session(pr, self.store.get_pr_session(pr) or "multi")
        self.store.set_last_sha(pr, head_oid)
        self.store.set_round(pr, round_number)

    def _submit_review(
        self, pr: int, result, round_number: int, prior_rejections: int
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
        if not self.gateway.submit_review(pr, sub.event, sub.body):
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
