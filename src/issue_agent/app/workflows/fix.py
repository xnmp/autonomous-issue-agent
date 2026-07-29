"""Fix workflow dispatch.

This module owns the impure fix-side agent dispatch sequence: gather GitHub and
store state, build an agent task, run it, persist session/counter markers, then
apply the pure follow-up actions. It is extracted from AgentDispatcher so the
central dispatcher can become a thin role router.
"""

from __future__ import annotations

import contextlib
import dataclasses
from collections.abc import Callable
from dataclasses import dataclass

from ...domain import fix as fix_domain
from ...domain import outcome
from ..planners import tasks as task_builders
from ..planners.tasks import FixTaskConfig
from ...domain.actions import AgentKind, Relabel, Target
from ...domain.agent_task import AgentRuntime
from ...domain.ci import latest_other_review
from ...domain.effort import (
    DEFAULT_EFFORT,
    DEFAULT_TIER,
    EscalationSignals,
    Stance,
    escalate,
)
from ...domain.labels import Label
from ...domain.refs import slugify
from ...domain.review import count_outstanding_change_requests
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
    journal: object | None = None


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

    def _decay_infra_deaths(self, key: int, result, alt_key: int | None = None) -> None:
        """A leg that RETURNED a result clears `key`'s infra-death ceiling
        (review P1R-3, and the residual P0R-3 flagged).

        The counter bounds CONSECUTIVE deaths — "the session keeps dying" — but
        nothing ever reset it, so it was a lifetime tally: five unrelated deaths
        spread over a month of otherwise healthy work eventually walk a healthy
        issue to needs-human-review. The one cleaner that removed it,
        `cleanup_pr(merged=True)`, only runs on work that already SUCCEEDED —
        never on the issue that is still trying.

        Any returned result counts, including an errored one: the ceiling asks
        "did this session survive to deliver anything?", not "was the answer
        good". An honest failure still marches the ordinary retry breakers,
        which are what bound it. A key that never returns a result never reaches
        here, so the loop this ceiling was tuned for still escalates.

        Called instead of, never as well as, the bump sites below — a leg is
        either an infra death or it is not.

        `alt_key` exists because the READ side unions two key spaces (review
        P1R-1): the dispatcher records a raise-path death under `-pr` whenever
        its pre-handler `issue_for_pr` lookup failed, and `roles._fix_state`
        therefore sums `breaker_key(pr)` and `-pr` into the key the domain asks
        for. Clearing only the key THIS leg resolved to would leave a death
        stranded in the other half forever: the transient that fails the lookup
        is the same transient that kills the session, so the pair is not rare,
        and `cleanup_pr` only ever clears whichever key is current at cleanup
        time. The stale count then reads as a real death on every later poll and
        walks a healthy PR toward needs-human-review. Decay must clear exactly
        the set the read unions.
        """
        if outcome.infra_death(result.exit_reason):
            return
        self.store.clear_infra_deaths(key)
        if alt_key is not None and alt_key != key:
            self.store.clear_infra_deaths(alt_key)

    def _leg_kind(self, kind: str | None):
        """Journal the next run under its own `kind` (#40), when the wrapped
        runtime knows how. A bare/undecorated runner (an injected fake, the
        legacy adapters) has no `leg_kind` and simply keeps the dispatch's
        kind — a naming nicety must never be able to kill a leg."""
        override = getattr(self.runner, "leg_kind", None) if kind else None
        return override(kind) if override is not None else contextlib.nullcontext()

    async def _run_logged(self, label: str, issue: int, task, *, kind: str | None = None):
        self.cfg.log(f"agent #{issue}: {label} — running")
        with self._leg_kind(kind):
            result = await self.runner.run(task)
        self.cfg.log(
            f"agent #{issue}: {label} — done "
            f"(status={outcome.status_label(result)}"
            f"{f', ${result.cost_usd:.2f}' if result.cost_usd else ''})"
        )
        return result

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
        return EscalationSignals(
            recurrence=bool(
                self._try(lambda: iss is not None and iss.has(Label.RECURRENCE), False)
            ),
            resolve_retries=int(
                self._try(lambda: self.store.get_resolve_retries(issue), 0) or 0
            ),
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
                            tiered: bool) -> None:
        """Record the rung this leg climbed, and why. ONE event per escalated
        leg — `leg` distinguishes them, so "how many legs ran escalated" is a
        group-by, not an inference.

        The event may only report what actually reached the dispatch. A repair
        leg has NO tier (it keeps the resumed session's model), so `tiered` is
        False there and both tier fields are null rather than the DEFAULT_TIER
        placeholder `_escalated` needs internally to have something to climb
        from. Reporting that placeholder would attribute a sonnet repair leg to
        `complex` in exactly the `group_by(.to_tier)` that CLAUDE.md makes the
        load-bearing routing guardrail — the metric this event exists to keep
        honest. `model` is the model that ran — None when the leg named none and
        the runtime picked its own, matching how the tier fields encode "not
        applicable" — so the split is derivable from what happened rather than
        from a hypothetical stance.

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
                "route_escalation", issue=issue, leg=leg,
                notches=signals.notches(),
                from_tier=before.tier if tiered else None,
                to_tier=after.tier if tiered else None,
                from_effort=before.effort.value, to_effort=after.effort.value,
                # `None`, not "" — the same "not applicable" encoding the tier
                # fields use, so an unrouted leg (router died, no FIX_MODEL: the
                # runtime picks) does not open a "" bucket in `group_by(.model)`.
                model=model,
                recurrence=signals.recurrence,
                resolve_retries=signals.resolve_retries,
                review_rounds=signals.review_rounds,
            )
        except Exception as exc:  # noqa: BLE001 — telemetry never fails a dispatch
            self.cfg.log(f"agent #{issue}: escalation not journalled ({exc})")

    def _escalated(
        self, cfg: FixTaskConfig, issue: int, leg: str, *, tier: str | None = None,
        iss=None, review_rounds: int = 0,
    ) -> FixTaskConfig:
        """Apply the escalation ladder to `cfg` for one leg (#16).

        `tier` is the router's verdict when this leg chose one; None means the
        leg inherited a model (a pinned FIX_MODEL, or a resumed session's) and
        only the EFFORT half of the ladder applies — a repair leg resumes work
        that already has shape, and swapping the model under a live transcript
        buys a different reasoner an unfamiliar context. So a repair leg thinks
        harder; only a fresh resolve re-prices the model.

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
        stance = escalate(base, notches)
        # Tier is only re-priced when this leg picked one; effort always applies.
        model = (
            task_builders.route_model(stance.tier, self.cfg.fixer_runtime)
            if tier is not None else cfg.model
        )
        # Same rule as the journal: name the tier move only when this leg chose
        # a tier, so the log cannot claim a climb the dispatch did not make.
        move = (
            f"tier {base.tier}→{stance.tier}, " if tier is not None
            else f"model {model or '(runtime default)'} kept, "
        )
        self.cfg.log(
            f"agent #{issue}: {leg} — escalating {notches} notch(es) "
            f"(recurrence={signals.recurrence}, resolve_retries={signals.resolve_retries}, "
            f"review_rounds={signals.review_rounds}): "
            f"{move}effort {base.effort.value}→{stance.effort.value}"
        )
        self._journal_escalation(
            issue, leg, base, stance, signals, model, tiered=tier is not None
        )
        return dataclasses.replace(cfg, model=model, effort=stance.effort)

    def _repair_fix_cfg(
        self, issue: int, leg: str, *, iss=None, review_rounds: int = 0
    ) -> FixTaskConfig | None:
        """Effort-escalated config for a leg that resumes an existing session
        (address-review / fix-CI / rebase / resume). Model untouched — see
        `_escalated`. `iss` is fetched when the caller does not already hold it,
        so the recurrence signal is not resolve-only."""
        cfg = self.cfg.fix
        if cfg is None:
            return cfg
        # #21: resume the session with the model that BUILT it. A repair leg
        # used to dispatch with no model, and the runner's fallback silently
        # swapped the routed reasoner for the configured default under the
        # live transcript. A pinned FIX_MODEL still wins; no stored marker
        # (pre-#21 sessions, unrouted resolves) keeps today's fallback.
        if not cfg.model:
            inherited = self.store.get_model(issue)
            if inherited:
                cfg = dataclasses.replace(cfg, model=inherited)
        # Behind the kill switch: with escalation off nothing reads the labels,
        # so paying a `gh issue view` per repair leg would be pure waste.
        if iss is None and self.cfg.escalate_on_evidence:
            iss = self._issue_for_signals(issue)
        return self._escalated(cfg, issue, leg, iss=iss, review_rounds=review_rounds)

    async def _routed_fix_cfg(self, issue: int, iss) -> FixTaskConfig:
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
            return cfg
        if cfg.model:
            return self._escalated(cfg, issue, "resolve", iss=iss)
        try:
            result = await self._run_logged(
                "route", issue,
                task_builders.build_router_task(
                    iss.title, iss.body, runtime=self.cfg.fixer_runtime
                ),
                kind=AgentKind.ROUTE.name,
            )
            tier = result.field("tier")
        except Exception as exc:  # noqa: BLE001 — routing is advisory, never fatal
            self.cfg.log(f"agent #{issue}: route — failed ({exc}); dispatching unrouted")
            return self._escalated(cfg, issue, "resolve", iss=iss)
        routed = self._escalated(cfg, issue, "resolve", tier=tier or DEFAULT_TIER, iss=iss)
        self.cfg.log(
            f"agent #{issue}: route — tier={tier or 'unknown'} → model={routed.model}"
        )
        return routed

    async def resolve(self, issue: int) -> None:
        if self.cfg.author_tests_split:
            await self.resolve_split(issue)
            return
        iss = self.gateway.get_issue(issue)
        branch = f"fix/{issue}-{slugify(iss.title) or 'issue'}"
        cwd = self._ensure_cwd(issue, branch)
        fix_cfg = await self._routed_fix_cfg(issue, iss)
        self.store.save_model(issue, fix_cfg.model)
        task = task_builders.build_resolve_task(issue, iss.title, iss.body, cwd, fix_cfg)
        result = await self._run_logged("resolve", issue, task)
        self.store.save_session(issue, result.session_id)
        self._decay_infra_deaths(issue, result)
        pr = self.gateway.pr_for_issue(issue)
        if outcome.resolve_followup(result, pr is not None) is outcome.ResolveFollowup.RETRY:
            self._bump_resolve_retry(issue, result)
        self.executor.execute_all(self._resolve_actions(issue, result, pr))

    def _resolve_actions(self, issue: int, result, pr: int | None):
        """Retire an already-implemented issue closed without opening a PR."""
        closed_without_pr = False
        if pr is None and not result.is_error and result.status in ("pushed", "done"):
            refreshed = self._try(lambda: self.gateway.get_issue(issue), None)
            closed_without_pr = (
                refreshed is not None and refreshed.state != "open"
            )
        return outcome.resolve_actions(
            issue,
            result,
            pr,
            issue_closed_without_pr=closed_without_pr,
        )

    async def resolve_split(self, issue: int) -> None:
        iss = self.gateway.get_issue(issue)
        branch = f"fix/{issue}-{slugify(iss.title) or 'issue'}"
        cwd = self._ensure_cwd(issue, branch)

        fix_cfg = await self._routed_fix_cfg(issue, iss)
        self.store.save_model(issue, fix_cfg.model)
        if self.store.get_phase(issue) != "authored":
            author = await self.runner.run(
                task_builders.build_author_task(issue, iss.title, iss.body, cwd, fix_cfg)
            )
            self.store.save_session(issue, author.session_id)
            self._decay_infra_deaths(issue, author)
            fu_author = outcome.author_followup(author)
            if fu_author is not outcome.AuthorFollowup.AUTHORED:
                if fu_author is outcome.AuthorFollowup.RETRY:
                    self._bump_resolve_retry(issue, author)
                self.executor.execute_all(outcome.author_actions(issue, author))
                return
            self.store.set_phase(issue, "authored")

        await self._implement_loop(issue, iss, cwd, fix_cfg)

    async def _implement_loop(self, issue: int, iss, cwd: str, fix_cfg=None) -> None:
        fix_cfg = fix_cfg if fix_cfg is not None else self.cfg.fix
        uphold_note = ""
        trail: list[str] = []
        rounds = 0
        while True:
            impl = await self.runner.run(
                task_builders.build_implement_task(
                    issue, iss.title, iss.body, cwd, fix_cfg, uphold_note=uphold_note
                )
            )
            self.store.save_session(issue, impl.session_id)
            self._decay_infra_deaths(issue, impl)
            pr = self.gateway.pr_for_issue(issue)
            fu = outcome.implement_followup(impl, pr is not None)
            if fu is not outcome.ImplementFollowup.CONTRACT_CHANGE:
                if fu is outcome.ImplementFollowup.RETRY:
                    self._bump_resolve_retry(issue, impl)
                self.executor.execute_all(self._resolve_actions(issue, impl, pr))
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
                # The SAME config the implementer ran on (routed + escalated),
                # not the unrouted base: the adjudicator's job is to overrule
                # the implementer's objection, and an arbiter that reasons
                # weaker than the leg it is arbitrating decides the wrong way
                # exactly when the issue is hard enough to have escalated.
                task_builders.build_adjudicate_task(
                    issue, iss.title, iss.body, objection, cwd, fix_cfg
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
            reply, cwd, self.store.get_session(issue),
            self._repair_fix_cfg(issue, "resume", iss=iss), fork=fork
        )
        result = await self.runner.run(task)
        self.store.save_session(issue, result.session_id)
        pr = self.gateway.pr_for_issue(issue)
        if outcome.infra_death(result.exit_reason):
            # review C3/D2c, RETURNED-result half.
            #
            # R1-F10: this used to claim resume_actions "does NOTHING… so the
            # reply must not be consumed by a session that never ran… the
            # issue stays ai-working, the reaper requeues it, and the next
            # poll pays for the SAME resume again." Both halves were wrong.
            # The reply is ALREADY consumed regardless of this session's fate:
            # set_resumed_at (above) is written BEFORE the run, precisely so
            # the dedup mark holds even if the session that follows dies —
            # not only after a successful resume. And the orphaned ai-working
            # issue is not requeued back into "awaiting the same reply":
            # domain.fix.reap_actions repairs it to ai-pr-open when it already
            # has an open PR (the common case here — most resumes happen on an
            # issue that already opened one; as of HEAD f0b4040, review
            # P0R-5), or to ai-ready when it does not. Either way the NEXT
            # poll pays for a fresh RESOLVE or PR-leg dispatch, never "the
            # same resume again" — issues_needing_resume won't re-fire on a
            # reply whose timestamp already matches the (pre-run) resumed-at
            # mark. What this counter actually bounds is that repeat-dispatch
            # loop — the session dying before producing ANY result, over and
            # over, with no retry counter of its own to catch it. A leg either
            # raises (AgentDispatcher.dispatch bumps) or returns (here), never
            # both, so a death is counted exactly once.
            self.store.bump_infra_deaths(issue)
        else:
            # No alt_key: RESUME is issue-keyed at the dispatcher
            # (`_issue_number` returns action.number without a lookup), so its
            # count can never land in the `-pr` half — and `pr` here is an
            # Optional from `pr_for_issue`, which `-pr` would not survive.
            self._decay_infra_deaths(issue, result)
        self.executor.execute_all(outcome.resume_actions(issue, result, pr))

    async def resume_pr(self, pr: int) -> None:
        """Resume a PR-stage block using the linked issue's saved transcript."""
        issue = self.gateway.issue_for_pr(pr)
        if issue is None:
            return
        p = self.gateway.get_pr(pr)
        reply_comment = fix_domain.last_human_reply(p, self.cfg.fix_bot)
        if reply_comment is None or not reply_comment.body.strip():
            if reply_comment is not None:
                self.store.set_pr_resumed_at(pr, reply_comment.created_at)
            self.executor.execute_all([
                Relabel(Target.PR, pr, Label.AI_WORKING, Label.AI_BLOCKED)
            ])
            return
        # Persist before the paid run: an unchanged PR reply must not fire again
        # after a crash or another blocked result.
        self.store.set_pr_resumed_at(pr, reply_comment.created_at)
        iss = self.gateway.get_issue(issue)
        cwd = self._ensure_cwd(issue, p.head_ref_name)
        fork = self.store.bump_resumes(issue) >= self.cfg.fork_threshold
        task = task_builders.build_resume_task(
            reply_comment.body, cwd, self.store.get_session(issue),
            self._repair_fix_cfg(issue, "resume", iss=iss), fork=fork,
        )
        result = await self.runner.run(task)
        self.store.save_session(issue, result.session_id)
        if outcome.infra_death(result.exit_reason):
            self.store.bump_infra_deaths(issue)
        else:
            self._decay_infra_deaths(issue, result, alt_key=-pr)
        self.executor.execute_all(outcome.pr_resume_actions(pr, result))

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
            pr, review_body, inline, cwd, self.store.get_session(issue),
            self._repair_fix_cfg(
                issue, "address-review",
                review_rounds=count_outstanding_change_requests(p.reviews, self.cfg.fix_bot),
            ),
            fork=fork,
        )
        result = await self.runner.run(task)
        self.store.save_session(issue, result.session_id)
        out = outcome.address_review_actions(pr, result)
        self.executor.execute_all(out.actions)
        if outcome.infra_death(result.exit_reason):
            # review C3/D2c, RETURNED-result half. address_review_actions
            # exempts an external kill from the review-retry ceiling (no attempt
            # was made) and leaves the PR untouched, so the next poll sees the same
            # CHANGES_REQUESTED review and pays for the leg again — unbounded
            # until this counter bounds it. Same key the review-retry counter
            # uses, so the decide side reads one key per issue. A leg either
            # raises or returns, so a death is counted exactly once.
            self.store.bump_infra_deaths(issue)
        else:
            self._decay_infra_deaths(issue, result, alt_key=-pr)
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
            pr, failed, failed_log, cwd, self.store.get_session(issue),
            # No `review_rounds` here (nor on `rebase`), though `p.reviews` is in
            # hand: review dissatisfaction is evidence about the RESOLUTION, and
            # a red pipeline or a conflicted branch is a different failure mode.
            # Spending the ladder on it would charge the wrong leg for the
            # reviewer's objections. Recurrence and resolve retries still apply.
            self._repair_fix_cfg(issue, "fix-ci"),
            mcp_servers=mcp_servers,
        )
        result = await self.runner.run(task)
        self.store.save_session(issue, result.session_id)
        # An externally-killed session made no attempt, so it must not march the
        # CI breaker toward needs-human-review (FAILURE-MODES #58: six SIGKILLs on
        # one PR burned the whole budget on work that never ran).
        if not outcome.infra_death(result.exit_reason):
            self.store.set_ci_retries(issue, self.store.get_ci_retries(issue) + 1)
            self._decay_infra_deaths(issue, result, alt_key=-pr)
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
            pr, self.cfg.base_branch, cwd, self.store.get_session(issue),
            self._repair_fix_cfg(issue, "rebase")
        )
        result = await self.runner.run(task)
        self.store.save_session(issue, result.session_id)
        # Same rule as fix_ci: an infra kill is not a failed rebase attempt.
        if not outcome.infra_death(result.exit_reason):
            self.store.set_rebase_retries(issue, self.store.get_rebase_retries(issue) + 1)
            self._decay_infra_deaths(issue, result, alt_key=-pr)
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
