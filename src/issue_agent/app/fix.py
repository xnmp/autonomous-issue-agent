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
from datetime import datetime
from types import MappingProxyType

from ..adapters.control_store import LegRun
from ..domain.actions import ISSUE_ACTS, Action, AgentKind, MergePullRequest, RunAgent, Target
from ..domain.agent_task import RuntimeName
from ..domain.backoff import LegEnd, dispatched_kind
from ..domain.ci import base_fix
from ..domain.evidence import ChangeRequest, CiFailures
from ..domain.labels import Label
from ..domain.leg import Leg, LegTarget
from ..domain.outcome import LegClass
from ..domain.gaps import GapTarget
from ..domain.policy import actions as policy_actions, base_ci_actions, dispatch_order, gap_actions
from ..domain.refs import harness_branch_issue, slugify
from ..domain.routing import DEFAULT_TIER, ModelRoute, Tier
from ..domain.state import DISPATCHABLE, DerivedState, Harness, ItemKey, Local, Wait, WaitReason, human_comments, issue_key, pr_key
from ..domain.triage import states_criteria
from ..domain.trust import as_untrusted
from ..domain.world import Comment, WorldState
from ..adapters.act import GitHubAct
from ..adapters.reload import remote_repo
from .config import HarnessConfig
from .planners import tasks
from .tick import Choose, Role, Slots, compose_role, dispatch_item, leg_class, local_legs, run_item, run_leg, steward_criteria, stderr_log

_FIX_ACTS = DISPATCHABLE - {AgentKind.REVIEW}
_LIGHT_ACTS = frozenset(kind for kind in AgentKind if not kind.holds_dispatch_slot)


def fix_actions(
    states: Mapping[ItemKey, DerivedState], harness: Harness, slots: int, *, merge: bool = True,
    light: int | None = None, waiting_since: Mapping[ItemKey, datetime] = MappingProxyType({}),
    first: frozenset[ItemKey] = frozenset(),
) -> tuple[Action, ...]:
    """The tick's Actions for the fix role: all but REVIEW dispatches. At most
    `slots` slot-holding dispatches, granted in `dispatch_order` (base-fix issues `first`); STEWARD and TRIAGE hold
    no slot and is bounded by `light` (None: unbounded), in the same order. With
    `merge` off (AUTO_MERGE=false) an approved green PR waits for a human."""
    per_item = {key: policy_actions({key: state}, harness) for key, state in states.items()}
    granted: set[RunAgent] = set()
    for key in dispatch_order(per_item, waiting_since, first):
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


def human_reply(
    comments: Iterable[Comment], after: datetime, harness: Harness, legs: Iterable[LegEnd],
) -> Comment | None:
    """The first human comment (derive's definition, so none inside one of
    the item's leg windows) posted after `after`: the answer the fixer's
    prompt points at; it reads that comment and any later ones itself."""
    return next((c for at, c in human_comments(comments, harness, legs) if at > after), None)


@dataclass(frozen=True)
class FixSettings:
    repository: str
    auto_merge: bool
    base_branch: str
    runtime: str
    owner_id: str
    harness: Harness
    # The route per tier (FIX_MODEL_<TIER>), for TRIAGE (TRIAGE_MODEL) and for
    # STEWARD (STEWARD_MODEL).
    routes: Mapping[Tier, ModelRoute] = field(default_factory=dict)
    triage_route: ModelRoute | None = None
    steward_route: ModelRoute | None = None
    # The reservation of each light leg, STEWARD and TRIAGE alike.
    triage_budget_usd: float = 2.0
    # An empty scratch directory: the light acts read only their prompt.
    triage_cwd: str = ""
    # Where harness gaps are filed: the harness checkout's GitHub origin, or ""
    # (off: `harness_gap_repo`); and whether a report may name and link the target.
    harness_repo: str = ""
    target_public: bool = False

    def route(self, tier: Tier | None) -> ModelRoute:
        return self.routes.get(tier or DEFAULT_TIER) or ModelRoute(RuntimeName(self.runtime), None)


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
            subject = self.gateway.get_pr(action.number, mergeability=False, checks=False)
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
        if not action.kind.holds_dispatch_slot:
            await self._light(action, subject)
            return
        recorded = self.control.leg_runs(self.settings.repository, (issue,))
        route = self.settings.route(action.tier)
        runtime, model, effort = route.runtime, route.model, route.effort
        # The review role's legs and the light acts' are not this role's sessions to continue.
        runs = sorted((r for r in recorded if dispatched_kind(r.kind) in _FIX_ACTS - _LIGHT_ACTS),
                      key=lambda r: r.completed_at)
        mine = [r for r in runs if run_item(r) == item]
        # The newest agent leg, the rule derive uses: machine and transport
        # deaths after it neither drop its question nor reset resume-once.
        last = next((i for i in range(len(mine) - 1, -1, -1) if leg_class(mine[i]) is not LegClass.MACHINE), -1)
        asked = mine[last] if last >= 0 and leg_class(mine[last]) is LegClass.ASKED else None
        # Resume once (classify_leg): continue the newest machine end since that
        # leg that left a session; a cap hit on the continuation is an attempt.
        cut_off = next((r for r in reversed(mine[last + 1:]) if self._session([r], runtime)), None)
        self.log(f"{action.kind.name} #{action.number}: tier {getattr(action.tier, 'value', '?')} -> "
                 f"{runtime.value}:{model or '(default model)'}{':' + effort.value if effort else ''}")
        self.runtime.set_context("fix", action.kind.name, action.number, issue, claim_target=issue,
                                 head=action.head, resumed=cut_off is not None,
                                 tier=action.tier.value if action.tier is not None else "")
        if isinstance(action.evidence, CiFailures):
            self.log(f"FIX_CI #{action.number}: handed on {action.head[:12]}: "
                     f"{', '.join(f.name for f in action.evidence.failures)[:300]}")
        try:
            if action.kind is AgentKind.RESOLVE:
                branch = self._resolve_branch(issue, subject.title)
                cwd = self.worktrees.ensure(issue, branch)
            else:
                branch = subject.head_ref_name
                cwd = self.worktrees.sync(issue, branch, action.head)
            if self.prepare_cwd is not None:
                self.prepare_cwd(cwd, runtime)
            # Every leg of the item, either role's, as derive sees them (#342).
            reply = human_reply(subject.comments, asked.completed_at, self.settings.harness,
                                local_legs(recorded).get(item, ())) if asked else None
            cfg = replace(self.task_config, runtime=runtime, model=model, effort=effort)
            task = self._task(action, subject, cwd, runs, cut_off, asked, cfg, runtime, recorded)
            if asked is not None:
                target = "issue" if action.kind is AgentKind.RESOLVE else "pr"
                task = tasks.with_human_reply(task, self.settings.repository, target, action.number, reply)
            task = replace(task, env={**task.env, **self._git_identity()}, branch=branch)
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
        recovered = self.worktrees.branch_for_issue(issue)
        return recovered if recovered and harness_branch_issue(recovered) == issue else fallback

    async def _light(self, action: RunAgent, subject) -> None:
        """One STEWARD or TRIAGE leg. STEWARD's duplicate candidates are read
        from GitHub here and enumerated in its schema, which is what the
        accounting runtime launders its `duplicate_of` against; TRIAGE reads
        the steward's criteria from the recorded legs."""
        steward = action.kind is AgentKind.STEWARD
        name = action.kind.name
        route = (self.settings.steward_route or self.settings.triage_route) if steward else self.settings.triage_route
        route = route or self.settings.route(Tier.LOW)
        runtime, model, effort = route.runtime, route.model, route.effort
        # resumed: the light acts never resume, so a leg cap is their one
        # attempt (STEWARD: no criteria; TRIAGE: DEFAULT_TIER), not a machine
        # end retried forever.
        self.runtime.set_context("fix", name, action.number, action.number, claim_target=action.number,
                                 resumed=True)
        try:
            # Per issue: concurrent light legs never share a cwd (or a CODEX_HOME).
            cwd = os.path.join(self.settings.triage_cwd, str(action.number))
            # No prepare_cwd: that writes the fix contract into Codex's home; a
            # read-only leg's home is written without it at launch.
            os.makedirs(cwd, mode=0o700, exist_ok=True)
        except Exception as exc:  # noqa: BLE001 - recorded as a machine end below
            self.log(f"{name} #{action.number}: preparation failed: {exc}")
            self.accounting.record_refused_leg()
            return
        if steward:
            try:
                # The steward judges duplicates and blockers against this index:
                # a failed read is a machine end, never a blind one-shot steward.
                index = [row for row in self.gateway.issue_index() if row[0] != action.number]
            except Exception as exc:  # noqa: BLE001 - recorded as a machine end below
                self.log(f"{name} #{action.number}: not dispatched — issue index unavailable: {exc}")
                self.accounting.record_refused_leg()
                return
            listing = "\n".join(f"#{n} ({state}) {title[:200]}" for n, state, title, _ in index) or "(none)"
            task = tasks.build_steward_task(
                action.number, as_untrusted(subject.title), as_untrusted(subject.body), as_untrusted(listing),
                tuple(n for n, _, _, _ in index), cwd, model=model, runtime=runtime, effort=effort,
                max_budget_usd=self.settings.triage_budget_usd,
                criteria_wanted=not states_criteria(subject.body or ""),
                # Only issues the harness itself will work on and close: model
                # output must not hold a ready issue behind an epic, an
                # unadmitted issue or one parked for a human.
                blockable=tuple(n for n, state, _, labels in index if state == "open"
                                and Label.AI_READY in labels and Label.NEEDS_HUMAN not in labels),
            )
        else:
            task = tasks.build_triage_task(
                action.number, as_untrusted(subject.title), as_untrusted(subject.body), cwd,
                model=model, runtime=runtime, effort=effort, max_budget_usd=self.settings.triage_budget_usd,
                criteria=steward_criteria(self.control.leg_runs(self.settings.repository, (action.number,))),
            )
        self.log(f"{name} #{action.number}: {runtime.value}:{model or '(default model)'}"
                 f"{':' + effort.value if effort else ''}")
        await run_leg(self.runtime, self.log, Leg(name, LegTarget.ISSUE, action.number, True,
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
        cfg: tasks.FixTaskConfig, runtime: RuntimeName, recorded: Iterable[LegRun],
    ):
        """`cut_off` is the machine end this leg continues; `asked` the agent
        leg whose question a human has since answered (the caller points at
        the answer). A fresh session is briefed like a first RESOLVE."""
        if action.kind is AgentKind.RESOLVE:
            if cut_off is not None:
                return tasks.build_continue_task(cwd, self._session([cut_off], runtime), cfg)
            if asked is not None and (session := self._session([asked], runtime)):
                return tasks.build_resume_task(cwd, session, cfg)
            return tasks.build_resolve_task(
                action.number, cwd, cfg, self._resolve_branch(action.number, subject.title),
                steward_criteria(recorded),
            )
        # PR-stage legs continue the issue's latest session on their runtime: it wrote the code.
        session = self._session(runs, runtime)
        if isinstance(action.evidence, CiFailures):
            task = tasks.build_fix_ci_task(action.number, action.evidence, cwd, session, cfg)
        elif isinstance(action.evidence, ChangeRequest):
            task = tasks.build_address_review_task(action.number, action.evidence, cwd, session, cfg)
        elif action.kind is AgentKind.REBASE:
            task = tasks.build_rebase_task(action.number, self.settings.base_branch, cwd, session, cfg)
        else:
            raise ValueError(f"{action.kind.name} has no evidence to repair")
        return task


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
        for pr in world.prs:  # a fresh PR waits from the leg that opened it, which is keyed by its issue
            issue = harness_branch_issue(pr.head_ref_name)
            if pr_key(pr.number) not in waiting and issue is not None and issue_key(issue) in waiting:
                waiting[pr_key(pr.number)] = waiting[issue_key(issue)]
        # The base branch's own CI is not an item: one issue per failing workflow per red streak (#402).
        admitted = (i.number for i in world.issues if i.has(Label.AI_READY) or i.has(Label.NEEDS_HUMAN))
        merged = (n for p in world.prs if p.state == "MERGED" and (n := harness_branch_issue(p.head_ref_name)))
        base = base_ci_actions(world.base_ci, settings.base_branch, settings.harness.marker, admitted, merged)
        fixing = base_fix(world.base_ci, settings.base_branch)
        first = frozenset({issue_key(n) for n in fixing} | {
            pr_key(p.number) for p in world.prs if p.state == "OPEN" and harness_branch_issue(p.head_ref_name) in fixing})
        gaps = gap_actions(local.legs, states, GapTarget(settings.repository, settings.target_public),
                           settings.harness_repo, settings.harness.marker, local.reported)
        return fix_actions(states, settings.harness, slots.heavy, merge=settings.auto_merge,
                           light=slots.light, waiting_since=waiting, first=first) + base + gaps
    return choose


def harness_gap_repo(cfg: HarnessConfig, gateway, log: Callable[[str], None]) -> tuple[str, bool]:
    """Where the fix role files harness gaps, and whether the target is public
    (a report then names and links it; else it is only "a private target"). The
    harness checkout's GitHub origin, written with FIX_BOT_TOKEN (ADR 0001);
    "" (off, logged) when the checkout has no GitHub origin, the credential
    cannot file issues there (no push, issues off, archived: a clone of
    someone else's harness files nothing upstream), or either repository's
    facts are unreadable. Read once per daemon start."""
    harness = remote_repo(cfg.harness_root) if cfg.harness_root else ""
    if not harness:
        log("fix: harness gaps are not filed: the harness checkout has no GitHub origin")
        return "", False
    try:
        (_, writable), (visibility, _) = gateway.for_repo(harness).filing_facts(), gateway.filing_facts()
    except Exception as exc:  # noqa: BLE001 - unknown facts fail toward not filing
        log(f"fix: harness gaps are not filed: repository facts unreadable: {str(exc)[:200]}")
        return "", False
    if not writable:
        log(f"fix: harness gaps are not filed: the fix credential cannot file issues in {harness}")
        return "", False
    log(f"fix: harness gaps are filed in {harness}")
    return harness, visibility == "public"


def compose_fix_daemon(
    cfg: HarnessConfig, gateway, worktrees, runner, *, log: Callable[[str], None] | None = None,
) -> Role:
    """The fix role: every Action but REVIEW, paid legs up to
    FIX_DISPATCH_CONCURRENCY."""
    log = log or stderr_log("fix")
    harness_repo, target_public = harness_gap_repo(cfg, gateway, log)
    settings = FixSettings(cfg.repo, cfg.auto_merge, cfg.base_branch, cfg.fixer_runtime.value,
                           "", Harness(fix_bot=cfg.fix_bot, review_bot=cfg.review_bot),
                           routes={tier: cfg.fix_route(tier) for tier in Tier}, triage_route=cfg.triage_route(),
                           steward_route=cfg.steward_route(),
                           triage_budget_usd=cfg.triage_budget_usd,
                           triage_cwd=os.path.join(cfg.state_dir, "triage"),
                           harness_repo=harness_repo, target_public=target_public)
    task_config = tasks.FixTaskConfig(cfg.repo, cfg.base_branch, cfg.agent_md, cfg.fix_budget_usd,
                                      cfg.fix_model, runtime=cfg.fixer_runtime)

    def make_legs(*, runtime, accounting, control, owner_id, prepare_cwd) -> FixLegs:
        return FixLegs(gateway, control, replace(settings, owner_id=owner_id), runtime=runtime,
                       accounting=accounting, worktrees=worktrees, task_config=task_config,
                       prepare_cwd=prepare_cwd, log=log)

    def make_act(dispatch, control) -> GitHubAct:
        return GitHubAct(gateway, control, repository=cfg.repo, base_branch=cfg.base_branch,
                         merge_method=cfg.merge_method, marker=settings.harness.marker, bot=cfg.fix_bot, dispatch=dispatch,
                         remove_worktree=worktrees.remove, log=log)

    role = compose_role(cfg, "fix", gateway, runner, make_legs=make_legs, make_act=make_act,
                        choose=fix_choose(settings, log), worktrees=worktrees,
                        capacity=cfg.fix_dispatch_concurrency, log=log)
    log(f"fix: capabilities: triage=on resolve=on merge={'on' if cfg.auto_merge else 'off'}")
    return role


__all__ = ["FixLegs", "FixSettings", "compose_fix_daemon", "fix_actions", "fix_choose", "harness_gap_repo",
           "human_reply"]
