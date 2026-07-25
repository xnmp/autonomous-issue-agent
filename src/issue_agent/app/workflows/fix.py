"""Fix workflow dispatch.

This module owns the impure fix-side agent dispatch sequence: gather GitHub and
store state, build an agent task, run it, persist session/counter markers, then
apply the pure follow-up actions. It is extracted from AgentDispatcher so the
central dispatcher can become a thin role router.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ...domain import fix as fix_domain
from ...domain import outcome
from ..planners import tasks as task_builders
from ..planners.tasks import FixTaskConfig
from ...domain.actions import Relabel, Target
from ...domain.agent_task import AgentRuntime
from ...domain.ci import latest_other_review
from ...domain.labels import Label
from ...domain.refs import slugify
from ...domain.world import REVIEW_CHANGES_REQUESTED
from ..mcp import mcp_servers_for_checks


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
    # The fixer's own login. The resume path must exclude the fixer's comments
    # with the SAME filter the decide side uses (domain.fix.last_human_reply),
    # or the resumed-at dedup mark records a different comment than the decide
    # loop compares against (V1/#13). Default "" keeps existing tests working.
    fix_bot: str = ""


@dataclass
class FixWorkflow:
    cfg: FixWorkflowConfig
    gateway: object
    store: object
    worktrees: object
    runner: AgentRuntime
    executor: object
    prepare_cwd: Callable[[str], None] | None = None

    def _ensure_cwd(self, issue: int, branch: str) -> str:
        cwd = self.worktrees.ensure(issue, branch)
        if self.prepare_cwd is not None:
            self.prepare_cwd(cwd)
        self.cfg.log(f"worktree #{issue}: ready on '{branch}' → {cwd}")
        return cwd

    def _prompt_input(self, method: str, *args, **kwargs) -> str:
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
        fn = getattr(self.gateway, method, None)
        if fn is None:
            return ""
        try:
            return fn(*args, **kwargs) or ""
        except Exception as exc:  # noqa: BLE001 — see docstring
            self.cfg.log(f"prompt input '{method}' unavailable: {exc}")
            return ""

    async def _run_logged(self, label: str, issue: int, task):
        self.cfg.log(f"agent #{issue}: {label} — running")
        result = await self.runner.run(task)
        self.cfg.log(
            f"agent #{issue}: {label} — done "
            f"(status={result.status or ('error' if result.is_error else 'ok')}"
            f"{f', ${result.cost_usd:.2f}' if result.cost_usd else ''})"
        )
        return result

    async def resolve(self, issue: int) -> None:
        if self.cfg.author_tests_split:
            await self.resolve_split(issue)
            return
        iss = self.gateway.get_issue(issue)
        branch = f"fix/{issue}-{slugify(iss.title) or 'issue'}"
        cwd = self._ensure_cwd(issue, branch)
        task = task_builders.build_resolve_task(issue, iss.title, iss.body, cwd, self.cfg.fix)
        result = await self._run_logged("resolve", issue, task)
        self.store.save_session(issue, result.session_id)
        pr = self.gateway.pr_for_issue(issue)
        if outcome.resolve_followup(result, pr is not None) is outcome.ResolveFollowup.RETRY:
            self._bump_resolve_retry(issue, result)
        self.executor.execute_all(outcome.resolve_actions(issue, result, pr))

    async def resolve_split(self, issue: int) -> None:
        iss = self.gateway.get_issue(issue)
        branch = f"fix/{issue}-{slugify(iss.title) or 'issue'}"
        cwd = self._ensure_cwd(issue, branch)

        if self.store.get_phase(issue) != "authored":
            author = await self.runner.run(
                task_builders.build_author_task(issue, iss.title, iss.body, cwd, self.cfg.fix)
            )
            self.store.save_session(issue, author.session_id)
            fu_author = outcome.author_followup(author)
            if fu_author is not outcome.AuthorFollowup.AUTHORED:
                if fu_author is outcome.AuthorFollowup.RETRY:
                    self._bump_resolve_retry(issue, author)
                self.executor.execute_all(outcome.author_actions(issue, author))
                return
            self.store.set_phase(issue, "authored")

        await self._implement_loop(issue, iss, cwd)

    async def _implement_loop(self, issue: int, iss, cwd: str) -> None:
        uphold_note = ""
        trail: list[str] = []
        rounds = 0
        while True:
            impl = await self.runner.run(
                task_builders.build_implement_task(
                    issue, iss.title, iss.body, cwd, self.cfg.fix, uphold_note=uphold_note
                )
            )
            self.store.save_session(issue, impl.session_id)
            pr = self.gateway.pr_for_issue(issue)
            fu = outcome.implement_followup(impl, pr is not None)
            if fu is not outcome.ImplementFollowup.CONTRACT_CHANGE:
                if fu is outcome.ImplementFollowup.RETRY:
                    self._bump_resolve_retry(issue, impl)
                self.executor.execute_all(outcome.resolve_actions(issue, impl, pr))
                return

            objection = impl.needs or impl.summary or "see logs"
            if rounds >= self.cfg.max_contract_rounds:
                self.executor.execute_all(
                    outcome.contract_escalation_actions(issue, objection, "\n".join(trail))
                )
                return
            rounds += 1
            self.worktrees.reset(issue)
            adj = await self.runner.run(
                task_builders.build_adjudicate_task(
                    issue, iss.title, iss.body, objection, cwd, self.cfg.fix
                )
            )
            self.store.save_session(issue, adj.session_id)
            verdict = outcome.adjudication(adj)
            trail.append(
                f"round {rounds}: {adj.field('verdict') or 'upheld'} — "
                f"{adj.field('rationale') or adj.summary}"
            )
            uphold_note = (
                "" if verdict is outcome.Adjudication.REVISED
                else (adj.field("rationale") or adj.summary or "the contract is correct")
            )

    def _bump_resolve_retry(self, issue: int, result) -> None:
        """A resolve-family leg that ends in a REQUEUE (error, or a claimed
        'done' with no PR on GitHub) counts toward the resolve breaker. The T3
        convergence probe proved this leg loops UNBOUNDED without one — a full
        paid session per poll on the most expensive leg (#39's shape). Infra
        deaths don't count (no attempt was made); blocked / PR-opened outcomes
        don't requeue, so they never reach here."""
        if outcome.infra_death(result.exit_reason):
            # …but it IS counted against the separate, higher infra ceiling
            # (review C3/D2c): dying before a result is its own paid loop, and
            # nothing else bounds it. This is the RETURNED-result twin of
            # AgentDispatcher.dispatch's bump-on-raise; a leg either raises or
            # returns, so a single death is counted exactly once.
            self.store.bump_infra_deaths(issue)
            return
        self.store.set_resolve_retries(issue, self.store.get_resolve_retries(issue) + 1)

    async def resume(self, issue: int) -> None:
        iss = self.gateway.get_issue(issue)
        reply_comment = fix_domain.last_human_reply(iss, self.cfg.fix_bot)
        if reply_comment is None or not reply_comment.body.strip():
            # Dispatched on a reply that vanished on re-fetch or has an empty
            # body: nothing to resume on. The decide side already flipped
            # ai-blocked → ai-working; leaving that stands strands the issue
            # into the reaper (→ ai-ready → a full RESOLVE loop — T3 probe
            # finding). Consume the reply (if any) and put the issue back to
            # awaiting-human.
            if reply_comment is not None:
                self.store.set_resumed_at(issue, reply_comment.created_at)
            self.executor.execute_all([
                Relabel(Target.ISSUE, issue, Label.AI_WORKING, Label.AI_BLOCKED)
            ])
            return
        reply = reply_comment.body
        # V1/#13 write side: record WHICH reply this dispatch consumes, BEFORE
        # running — the decide loop's dedup (issues_needing_resume) compares
        # this mark against the thread, and without it one unanswered reply
        # re-fired a paid RESUME session every poll tick. Written pre-run so
        # even a crashed/blocked leg cannot loop on the same reply; a crashed
        # leg is requeued by the reaper (ai-working → ai-ready), not by
        # re-resuming.
        self.store.set_resumed_at(issue, reply_comment.created_at)
        pr_existing = self.gateway.pr_for_issue(issue)
        if self.cfg.author_tests_split and pr_existing is None:
            await self.resolve_split(issue)
            return
        branch = self.gateway.get_pr(pr_existing).head_ref_name if pr_existing else f"fix/{issue}-issue"
        cwd = self._ensure_cwd(issue, branch)
        fork = self.store.bump_resumes(issue) >= self.cfg.fork_threshold
        task = task_builders.build_resume_task(
            reply, cwd, self.store.get_session(issue), self.cfg.fix, fork=fork
        )
        result = await self.runner.run(task)
        self.store.save_session(issue, result.session_id)
        pr = self.gateway.pr_for_issue(issue)
        if outcome.infra_death(result.exit_reason):
            # review C3/D2c, RETURNED-result half. resume_actions deliberately
            # does NOTHING on an infra death (the reply must not be consumed by
            # a session that never ran) — so the issue stays ai-working, the
            # reaper requeues it, and the next poll pays for the same resume
            # again with no counter moving anywhere. This is the only bound on
            # that loop. A leg either raises (AgentDispatcher.dispatch bumps) or
            # returns (here), never both, so a death is counted exactly once.
            self.store.bump_infra_deaths(issue)
        self.executor.execute_all(outcome.resume_actions(issue, result, pr))

    async def address_review(self, pr: int) -> None:
        issue = self.gateway.issue_for_pr(pr)
        if issue is None:
            return
        p = self.gateway.get_pr(pr)
        cwd = self._ensure_cwd(issue, p.head_ref_name)
        review_body = self.latest_review_body(p)
        # The file:line asks reviewers actually make live in the INLINE comments,
        # not the review body (review M13): dispatching without them rendered an
        # empty "Inline comments:" header and cost rounds off the 4-rejection
        # ceiling while the fixer guessed at what was being asked.
        inline = self._prompt_input("review_comments", pr)
        fork = self.store.bump_resumes(issue) >= self.cfg.fork_threshold
        task = task_builders.build_address_review_task(
            pr, review_body, inline, cwd, self.store.get_session(issue), self.cfg.fix,
            fork=fork,
        )
        result = await self.runner.run(task)
        self.store.save_session(issue, result.session_id)
        out = outcome.address_review_actions(pr, result)
        self.executor.execute_all(out.actions)
        if outcome.infra_death(result.exit_reason):
            # review C3/D2c, RETURNED-result half. address_review_actions
            # exempts an external kill from the review-retry ceiling (no attempt
            # was made) and clears ai-working, so the next poll sees the same
            # CHANGES_REQUESTED review and pays for the leg again — unbounded
            # until this counter bounds it. Same key the review-retry counter
            # uses, so the decide side reads one key per issue. A leg either
            # raises or returns, so a death is counted exactly once.
            self.store.bump_infra_deaths(issue)
        if out.bump_retry:
            # Attempt counter keyed to the review being addressed: a count
            # stored under an OLDER review's timestamp restarts at 0 (that
            # review was superseded). There is deliberately no clear-on-success
            # — only a NEW review re-keys the counter (V2/#64; see
            # domain.outcome.AddressReviewOutcome).
            review_at = self._current_review_at(p)
            at, n = self.store.get_review_retry(issue)
            base = n if at == review_at else 0
            self.store.set_review_retry(issue, review_at, base + 1)

    async def fix_ci(self, pr: int) -> None:
        issue = self.gateway.issue_for_pr(pr)
        if issue is None:
            return
        p = self.gateway.get_pr(pr)
        cwd = self._ensure_cwd(issue, p.head_ref_name)
        failed = "\n".join(c.name for c in p.checks if c.bucket == "fail")
        # Check NAMES alone were all the prompt carried (review M13) — the agent
        # had to rediscover every failure with `gh api` denied by the governor.
        # `branch` is passed because the PRView is already in hand: it saves the
        # gateway a `gh pr view` round-trip it would otherwise make to find the
        # head branch `gh run list` needs.
        failed_log = self._prompt_input("failed_run_log", pr, branch=p.head_ref_name)
        mcp_servers = mcp_servers_for_checks(
            self.cfg.fix_mcp_config, [c.name for c in p.checks], self.cfg.harness_root
        )
        task = task_builders.build_fix_ci_task(
            pr, failed, failed_log, cwd, self.store.get_session(issue), self.cfg.fix,
            mcp_servers=mcp_servers,
        )
        result = await self.runner.run(task)
        self.store.save_session(issue, result.session_id)
        # An externally-killed session made no attempt, so it must not march the
        # CI breaker toward needs-human-review (FAILURE-MODES #58: six SIGKILLs on
        # one PR burned the whole budget on work that never ran).
        if not outcome.infra_death(result.exit_reason):
            self.store.set_ci_retries(issue, self.store.get_ci_retries(issue) + 1)
        else:
            # Separate, higher ceiling for the deaths themselves (review C3/D2c)
            # — same key the CI counter uses, so the decide side reads one key.
            self.store.bump_infra_deaths(issue)
        self.executor.execute_all(outcome.fix_ci_actions(pr, result))

    async def rebase(self, pr: int) -> None:
        issue = self.gateway.issue_for_pr(pr)
        if issue is None:
            return
        p = self.gateway.get_pr(pr)
        cwd = self._ensure_cwd(issue, p.head_ref_name)
        task = task_builders.build_rebase_task(
            pr, self.cfg.base_branch, cwd, self.store.get_session(issue), self.cfg.fix
        )
        result = await self.runner.run(task)
        self.store.save_session(issue, result.session_id)
        # Same rule as fix_ci: an infra kill is not a failed rebase attempt.
        if not outcome.infra_death(result.exit_reason):
            self.store.set_rebase_retries(issue, self.store.get_rebase_retries(issue) + 1)
        else:
            self.store.bump_infra_deaths(issue)  # review C3/D2c, as in fix_ci
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
