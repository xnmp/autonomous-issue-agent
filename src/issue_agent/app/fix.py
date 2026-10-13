"""The fix role: observe -> derive -> policy -> act (refactor slice 5b).

One tick reads GitHub (`GitHubGateway.build_world`) and the local facts
GitHub cannot hold (`tick.Facts`), derives every item's state, maps it to
Actions, and executes them (`adapters.act.GitHubAct`). Nothing about an item's lifecycle is
stored here: the run table records ended legs, the dedupe table records which
idempotent writes landed, and everything else is re-derived next tick.

The fix role executes every Action except `RunAgent(REVIEW)`, which belongs to
the review role.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from types import MappingProxyType

from ..adapters.control_store import LegRun
from ..domain.actions import ISSUE_ACTS, Action, AgentKind, MergePullRequest, RunAgent, Target
from ..domain.agent_task import RuntimeName
from ..domain.backoff import LegEnd, dispatched_kind
from ..domain.ci import failed_checks
from ..domain.labels import Label
from ..domain.leg import Leg, LegTarget
from ..domain.outcome import LegClass
from ..domain.policy import actions as policy_actions
from ..domain.pr_body import has_acceptance_criteria
from ..domain.refs import harness_branch_issue, slugify
from ..domain.routing import DEFAULT_TIER, Tier
from ..domain.triage import Triage, triage_from_record
from ..domain.state import DISPATCHABLE, DerivedState, Harness, ItemKey, Local, Wait, WaitReason, human_comments
from ..domain.trust import as_untrusted
from ..domain.world import REVIEW_CHANGES_REQUESTED, Comment, WorldState
from ..adapters.act import GitHubAct
from .config import HarnessConfig
from .planners import tasks
from .tick import Choose, Role, Slots, compose_role, dispatch_item, leg_class, local_legs, run_item, run_leg, stderr_log

_FIX_ACTS = DISPATCHABLE - {AgentKind.REVIEW}


def dispatch_order(keys: Iterable[ItemKey], waiting_since: Mapping[ItemKey, datetime]) -> list[ItemKey]:
    """Who gets a scarce slot first: PR-stage items before issue-stage (finish
    work in progress before starting new), then the item that has waited longest
    (its newest leg end; none = never served = oldest), then by number so the
    order is total. #1125 starved behind #1039/#1117/#1122 under issues-first,
    by-number order."""
    never = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(keys, key=lambda k: (k.target is not Target.PR, waiting_since.get(k, never), k.number))


def fix_actions(
    states: Mapping[ItemKey, DerivedState], harness: Harness, slots: int, *, merge: bool = True,
    light: int | None = None, waiting_since: Mapping[ItemKey, datetime] = MappingProxyType({}),
) -> tuple[Action, ...]:
    """The tick's Actions for the fix role: all but REVIEW dispatches. At most
    `slots` slot-holding dispatches, granted in `dispatch_order`; TRIAGE holds
    no slot and is bounded by `light` (None: unbounded), by number. With
    `merge` off (AUTO_MERGE=false) an approved green PR waits for a human."""
    per_item = {key: policy_actions({key: state}, harness) for key, state in states.items()}
    granted: set[RunAgent] = set()
    for key in dispatch_order(per_item, waiting_since):
        for action in per_item[key]:
            if not isinstance(action, RunAgent) or action.kind not in _FIX_ACTS:
                continue
            if action.kind.holds_dispatch_slot:
                if slots > 0:
                    slots -= 1
                    granted.add(action)
            elif light is None or light > 0:
                light = None if light is None else light - 1
                granted.add(action)
    out: list[Action] = []
    for key in sorted(per_item, key=lambda k: k.sort_key):
        for action in per_item[key]:
            if isinstance(action, MergePullRequest) and not merge:
                continue
            if not isinstance(action, RunAgent) or action in granted:
                out.append(action)
    return tuple(out)


def human_reply(comments: Iterable[Comment], after: datetime, harness: Harness, legs: Iterable[LegEnd]) -> str:
    """The human comments (derive's definition, so none inside one of the
    item's leg windows) posted after `after`, oldest first."""
    return "\n\n".join(f"@{c.author}: {c.body}" for at, c in human_comments(comments, harness, legs) if at > after)


Route = tuple[RuntimeName, "str | None"]


@dataclass(frozen=True)
class FixSettings:
    repository: str
    auto_merge: bool
    base_branch: str
    runtime: str
    owner_id: str
    harness: Harness
    # (runtime, model) per tier (FIX_MODEL_<TIER>) and for TRIAGE (TRIAGE_MODEL).
    routes: Mapping[Tier, Route] = field(default_factory=dict)
    triage_route: Route | None = None
    triage_budget_usd: float = 2.0
    # An empty scratch directory: TRIAGE reads only its prompt.
    triage_cwd: str = ""

    def route(self, tier: Tier | None) -> Route:
        return self.routes.get(tier or DEFAULT_TIER) or (RuntimeName(self.runtime), None)


class FixLegs:
    """Runs one fix leg: re-checks the item against GitHub, prepares the
    worktree, picks the session to resume, builds the task, and runs it
    through the accounting runtime. A preparation failure is recorded as a
    refused (machine) leg so the item backs off instead of retrying hot. A
    question the leg asks is recorded in its receipt; derive's QUESTION stop
    comment carries it (one writer, one comment)."""

    def __init__(
        self,
        gateway,
        control,
        settings: FixSettings,
        *,
        runtime,
        accounting,
        worktrees,
        task_config: tasks.FixTaskConfig,
        prepare_cwd: Callable[[str, RuntimeName | None], None] | None,
        log: Callable[[str], None],
    ) -> None:
        self.gateway = gateway
        self.control = control
        self.settings = settings
        self.runtime = runtime
        self.accounting = accounting
        self.worktrees = worktrees
        self.task_config = task_config
        self.prepare_cwd = prepare_cwd
        self.log = log

    async def run(self, action: RunAgent) -> None:
        if action.kind not in _FIX_ACTS:
            raise ValueError(f"the fix role does not run {action.kind.name}")
        item = dispatch_item(action)
        if action.kind in ISSUE_ACTS:
            subject = self.gateway.get_issue(action.number)
            if subject.state != "open" or Label.AI_READY not in subject.labels or Label.NEEDS_HUMAN in subject.labels:
                self.log(f"{action.kind.name} #{action.number}: skipped — issue no longer ready")
                return
            issue = action.number
        else:
            subject = self.gateway.get_pr(action.number, mergeability=False, checks=action.kind is AgentKind.FIX_CI)
            issue = harness_branch_issue(subject.head_ref_name)
            if subject.state != "OPEN" or issue is None or subject.head_ref_oid != action.head:
                self.log(f"{action.kind.name} PR #{action.number}: skipped — PR closed or head moved")
                return
        owner = self.settings.owner_id
        repo = self.settings.repository
        if not self.control.lease(repo, "fix", issue, owner):
            self.log(f"{action.kind.name} #{action.number}: skipped — issue #{issue} already has a fix leg in flight")
            return
        try:
            await self._run_claimed(action, item, issue, subject)
        finally:
            self.control.unlease(repo, "fix", issue, owner)

    async def _run_claimed(self, action: RunAgent, item: ItemKey, issue: int, subject) -> None:
        if action.kind is AgentKind.TRIAGE:
            await self._triage(action, subject)
            return
        recorded = self.control.leg_runs(self.settings.repository, (issue,))
        runtime, model = self.settings.route(action.tier)
        # The review role's legs and TRIAGE's are not this role's sessions to continue.
        runs = sorted((r for r in recorded if dispatched_kind(r.kind) in _FIX_ACTS - {AgentKind.TRIAGE}),
                      key=lambda r: r.completed_at)
        mine = [r for r in runs if run_item(r) == item]
        # The newest agent leg, the rule derive uses: machine and transport
        # deaths after it neither drop its question nor reset resume-once.
        last = next((i for i in range(len(mine) - 1, -1, -1) if leg_class(mine[i]) is not LegClass.MACHINE), -1)
        asked = mine[last] if last >= 0 and leg_class(mine[last]) is LegClass.ASKED else None
        # Resume once (classify_leg): continue the newest machine end since that
        # leg that left a session; a wall cap on the continuation is an attempt.
        cut_off = next((r for r in reversed(mine[last + 1:]) if self._session([r], runtime)), None)
        self.log(f"{action.kind.name} #{action.number}: tier {getattr(action.tier, 'value', '?')} -> "
                 f"{runtime.value}:{model or '(default model)'}")
        self.runtime.set_context("fix", action.kind.name, action.number, issue, claim_target=issue,
                                 head=action.head, resumed=cut_off is not None,
                                 tier=action.tier.value if action.tier is not None else "")
        try:
            if action.kind is AgentKind.RESOLVE:
                branch = self._resolve_branch(issue, subject.title)
                cwd = self.worktrees.ensure(issue, branch)
            else:
                cwd = self.worktrees.sync(issue, subject.head_ref_name, action.head)
            if self.prepare_cwd is not None:
                self.prepare_cwd(cwd, runtime)
            # Every leg of the item, either role's, as derive sees them (#342).
            reply = human_reply(subject.comments, asked.completed_at, self.settings.harness,
                                local_legs(recorded).get(item, ())) if asked else ""
            cfg = replace(self.task_config, runtime=runtime, model=model)
            criteria = _criteria(recorded) if action.kind is AgentKind.RESOLVE else ()
            task = self._task(action, subject, cwd, runs, cut_off, asked, reply, cfg, runtime, criteria)
            task = replace(task, env={**task.env, **self._git_identity()})
        except Exception as exc:  # noqa: BLE001 - recorded as a machine end below
            self.log(f"{action.kind.name} #{action.number}: preparation failed: {exc}")
            self.accounting.record_refused_leg()
            return
        target = LegTarget.ISSUE if action.kind is AgentKind.RESOLVE else LegTarget.PR
        await run_leg(
            self.runtime, self.log, Leg(action.kind.name, target, action.number, True, ("global", "repository", "issue")),
            task,
        )

    def _git_identity(self) -> dict[str, str]:
        """Commits attribute to the fix bot (or to no login at all), never to
        the operator: derive reads a push by a non-harness login as a human
        reset, which would keep the attempt cap from ever tripping."""
        login = self.settings.harness.fix_bot.strip()
        email = f"{login}@users.noreply.github.com"
        return {"GIT_AUTHOR_NAME": login, "GIT_AUTHOR_EMAIL": email,
                "GIT_COMMITTER_NAME": login, "GIT_COMMITTER_EMAIL": email}

    def _resolve_branch(self, issue: int, title: str) -> str:
        fallback = f"fix/{issue}-{slugify(title) or 'issue'}"
        recovered = self.worktrees.branch_for_issue(issue, fallback)
        return recovered if recovered and harness_branch_issue(recovered) == issue else fallback

    async def _triage(self, action: RunAgent, subject) -> None:
        """One TRIAGE leg. The issues it may name as a duplicate are read from
        GitHub here and enumerated in its schema, which is what the accounting
        runtime launders its `duplicate_of` against."""
        runtime, model = self.settings.triage_route or self.settings.route(Tier.LOW)
        # resumed: triage never resumes, so a wall timeout is its one attempt
        # (the issue runs at DEFAULT_TIER), not a machine end retried forever.
        self.runtime.set_context("fix", action.kind.name, action.number, action.number, claim_target=action.number,
                                 resumed=True)
        try:
            # Per issue: concurrent triage legs never share a cwd (or a CODEX_HOME).
            cwd = os.path.join(self.settings.triage_cwd, str(action.number))
            # No prepare_cwd: that writes the fix contract into Codex's home; a
            # read-only leg's home is written without it at launch.
            os.makedirs(cwd, mode=0o700, exist_ok=True)
        except Exception as exc:  # noqa: BLE001 - recorded as a machine end below
            self.log(f"TRIAGE #{action.number}: preparation failed: {exc}")
            self.accounting.record_refused_leg()
            return
        index = [row for row in _soft_rows(self.gateway.issue_index, self.log) if row[0] != action.number]
        listing = "\n".join(f"#{n} ({state}) {title[:200]}" for n, state, title in index) or "(none)"
        task = tasks.build_triage_task(
            action.number, as_untrusted(subject.title), as_untrusted(subject.body), as_untrusted(listing),
            tuple(n for n, _, _ in index), cwd, model=model, runtime=runtime,
            max_budget_usd=self.settings.triage_budget_usd,
            criteria_wanted=not has_acceptance_criteria(subject.body or ""),
        )
        self.log(f"TRIAGE #{action.number}: {runtime.value}:{model or '(default model)'}")
        await run_leg(self.runtime, self.log, Leg("TRIAGE", LegTarget.ISSUE, action.number, True,
                                                  ("global", "repository", "issue")), task)

    def _session(self, runs: list[LegRun], runtime: RuntimeName) -> str:
        """The newest resumable session of `runtime`: a leg routed to another
        runtime starts fresh rather than resume a transcript it cannot read."""
        for run in reversed(runs):
            receipt = run.receipt or {}
            session = receipt.get("session")
            if receipt.get("runtime") == runtime.value and isinstance(session, str) and session:
                return session
        return ""

    def _task(
        self, action: RunAgent, subject, cwd: str, runs: list[LegRun], cut_off: LegRun | None, asked: LegRun | None,
        reply: str, cfg: tasks.FixTaskConfig, runtime: RuntimeName, criteria: tuple[str, ...] = (),
    ):
        """`cut_off` is the machine end this leg continues; `asked` the agent
        leg whose question a human has since answered with `reply`."""
        if action.kind is AgentKind.RESOLVE:
            if cut_off is not None:
                return tasks.build_continue_task(cwd, self._session([cut_off], runtime), cfg)
            if asked is not None:
                if session := self._session([asked], runtime):
                    return tasks.build_resume_task(as_untrusted(reply), cwd, session, cfg)
                return tasks.build_fresh_reply_task(
                    action.number, as_untrusted(subject.title), as_untrusted(subject.body), as_untrusted(reply), cwd, cfg,
                )
            return tasks.build_resolve_task(
                action.number, as_untrusted(subject.title), as_untrusted(subject.body), cwd, cfg,
                self._resolve_branch(action.number, subject.title),
                as_untrusted("\n".join(f"- {c}" for c in criteria)) if criteria else None,
            )
        # PR-stage legs continue the issue's latest session on their runtime: it wrote the code.
        session = self._session(runs, runtime)
        if action.kind is AgentKind.FIX_CI:
            checks = failed_checks(subject.ci)
            run_ids = tuple(dict.fromkeys(c.run_id for c in checks if c.run_id is not None))
            task = tasks.build_fix_ci_task(
                action.number, as_untrusted(", ".join(c.name for c in checks)),
                as_untrusted(_soft(lambda: self.gateway.failed_run_log(action.head, run_ids), self.log)),
                cwd, session, cfg,
            )
        elif action.kind is AgentKind.REBASE:
            task = tasks.build_rebase_task(action.number, self.settings.base_branch, cwd, session, cfg)
        else:
            review = max(
                (r for r in subject.reviews
                 if r.commit_id == action.head and r.state.upper() == REVIEW_CHANGES_REQUESTED),
                key=lambda r: r.submitted_at, default=None,
            )
            task = tasks.build_address_review_task(
                action.number, as_untrusted(review.body if review is not None else ""),
                as_untrusted(_soft(lambda: self.gateway.review_comments(action.number), self.log)),
                cwd, session, cfg,
            )
        return tasks.with_human_reply(task, as_untrusted(reply)) if reply else task


def _criteria(runs: Iterable[LegRun]) -> tuple[str, ...]:
    """The acceptance criteria of the issue's latest agent-ended TRIAGE leg."""
    triaged = [r for r in runs if r.kind == AgentKind.TRIAGE.name and leg_class(r) is not LegClass.MACHINE]
    latest = max(triaged, key=lambda r: r.completed_at, default=None)
    found: Triage | None = triage_from_record((latest.receipt or {}).get("triage")) if latest is not None else None
    return found.acceptance_criteria if found is not None else ()


def _soft_rows(read: Callable[[], tuple], log: Callable[[str], None]) -> tuple:
    """A listing as prompt input: a failed read offers nothing."""
    try:
        return tuple(read())
    except Exception as exc:  # noqa: BLE001 - context, never control flow
        log(f"prompt input unavailable: {exc}")
        return ()


def _soft(read: Callable[[], str], log: Callable[[str], None]) -> str:
    """A prompt input: context, never control flow. A failed read leaves the
    leg with a weaker prompt instead of killing it."""
    try:
        return read() or ""
    except Exception as exc:  # noqa: BLE001 - see docstring
        log(f"prompt input unavailable: {exc}")
        return ""


def fix_choose(settings: FixSettings, log: Callable[[str], None]) -> Choose:
    def choose(world: WorldState, states: Mapping[ItemKey, DerivedState], slots: Slots,
               local: Local) -> tuple[Action, ...]:
        prs = {p.number: p for p in world.prs}
        for key, state in sorted(states.items(), key=lambda kv: kv[0].sort_key):
            if state == Wait(WaitReason.MERGEABILITY_UNKNOWN) and (pr := prs.get(key.number)) is not None:
                # Approved and green but not mergeable now: BLOCKED waits on a human (branch protection).
                log(f"fix: PR #{key.number} waits to merge: mergeable={pr.mergeable or '?'} "
                    f"state={pr.merge_state or '?'}")
        waiting = {k: ends[-1].ended_at for k, ends in local.legs.items() if ends}
        return fix_actions(states, settings.harness, slots.heavy, merge=settings.auto_merge,
                           light=slots.light, waiting_since=waiting)
    return choose


def compose_fix_daemon(
    cfg: HarnessConfig, gateway, worktrees, runner, *, log: Callable[[str], None] | None = None,
) -> Role:
    """The fix role: every Action but REVIEW, paid legs up to
    FIX_DISPATCH_CONCURRENCY."""
    log = log or stderr_log("fix")
    settings = FixSettings(cfg.repo, cfg.auto_merge, cfg.base_branch, cfg.fixer_runtime.value,
                           "", Harness(fix_bot=cfg.fix_bot, review_bot=cfg.review_bot),
                           routes={tier: cfg.fix_route(tier) for tier in Tier}, triage_route=cfg.triage_route(),
                           triage_budget_usd=cfg.triage_budget_usd,
                           triage_cwd=os.path.join(cfg.state_dir, "triage"))
    task_config = tasks.FixTaskConfig(cfg.repo, cfg.base_branch, cfg.agent_md, cfg.fix_budget_usd,
                                      cfg.fix_model, cfg.fix_effort, runtime=cfg.fixer_runtime)

    def make_legs(*, runtime, accounting, control, owner_id, prepare_cwd) -> FixLegs:
        return FixLegs(gateway, control, replace(settings, owner_id=owner_id), runtime=runtime,
                       accounting=accounting, worktrees=worktrees, task_config=task_config,
                       prepare_cwd=prepare_cwd, log=log)

    def make_act(dispatch, control) -> GitHubAct:
        return GitHubAct(gateway, control, repository=cfg.repo, base_branch=cfg.base_branch,
                         merge_method=cfg.merge_method, marker=settings.harness.marker, dispatch=dispatch,
                         remove_worktree=worktrees.remove, log=log)

    role = compose_role(cfg, "fix", gateway, runner, make_legs=make_legs, make_act=make_act,
                        choose=fix_choose(settings, log), worktrees=worktrees,
                        capacity=cfg.fix_dispatch_concurrency, log=log)
    log(f"fix: capabilities: triage=on resolve=on merge={'on' if cfg.auto_merge else 'off'}")
    return role


__all__ = ["FixLegs", "FixSettings", "compose_fix_daemon", "fix_actions", "fix_choose", "human_reply"]
