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
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType

from ..adapters.control_store import LegRun
from ..domain.actions import ISSUE_ACTS, ROUTED_ACTS, Action, AgentKind, MergePullRequest, RunAgent, Target
from ..domain.agent_task import RuntimeName
from ..domain.attempt import AnswerResume, Continue, NextLeg, latest_session, next_leg
from ..domain.backoff import LegEnd
from ..domain.ci import base_fix
from ..domain.evidence import ChangeRequest, CiFailures
from ..domain.labels import Label
from ..domain.leg import Leg, LegTarget
from ..domain.gaps import GapTarget
from ..domain.policy import actions as policy_actions, base_ci_actions, dispatch_order, gap_actions
from ..domain.refs import harness_branch_issue, slugify
from ..domain.routing import DEFAULT_TIER, ModelRoute, Tier
from ..domain.state import (DISPATCHABLE, DerivedState, Harness, ItemKey, Local, Wait, WaitReason, human_comments, issue_key,
                            picked_up, pr_key)
from ..domain.triage import states_criteria
from ..domain.trust import as_untrusted
from ..domain.world import Comment, Pickup, WorldState
from ..adapters.act import GitHubAct
from ..adapters.reload import remote_repo
from .config import HarnessConfig
from .live_config import LiveConfig
from .planners import tasks
from .tick import Choose, Role, Slots, compose_role, dispatch_item, local_legs, run_leg, steward_criteria, stderr_log

_FIX_ACTS = DISPATCHABLE - {AgentKind.REVIEW}


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
    owner_id: str
    harness: Harness
    # The route of a leg of this kind at this tier (FIX_MODEL_<TIER>,
    # TRIAGE_MODEL, STEWARD_MODEL), read as the leg starts: routes are live.
    route: Callable[[AgentKind, Tier | None], ModelRoute]
    # The issue pickup now in force: the pre-launch re-check applies derive's rule.
    pickup: Callable[[], Pickup] = lambda: Pickup.READY
    # The reservation of each light leg, STEWARD and TRIAGE alike.
    triage_budget_usd: float = 2.0
    # An empty scratch directory: the light acts read only their prompt.
    triage_cwd: str = ""
    # Where harness gaps are filed: the harness checkout's GitHub origin, or ""
    # (off: `harness_gap_repo`); and whether a report may name and link the target.
    harness_repo: str = ""
    target_public: bool = False


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
            if not picked_up(subject, self.settings.harness, self.settings.pickup()) or Label.NEEDS_HUMAN in subject.labels:
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
        route = self.settings.route(action.kind, action.tier)
        runtime, model, effort = route.runtime, route.model, route.effort
        # Every leg of the item, either role's, as derive sees them (#342).
        legs = local_legs(recorded)
        mine = legs.get(item, ())
        plan = next_leg(mine, runtime.value)
        self.log(f"{action.kind.name} #{action.number}: tier {getattr(action.tier, 'value', '?')} -> "
                 f"{runtime.value}:{model or '(default model)'}{':' + effort.value if effort else ''}")
        self.runtime.set_context("fix", action.kind.name, action.number, issue, claim_target=issue,
                                 head=action.head, tier=action.tier.value if action.tier is not None else "")
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
            asked = plan.answering
            reply = human_reply(subject.comments, asked.ended_at, self.settings.harness, mine) if asked else None
            cfg = replace(self.task_config, runtime=runtime, model=model, effort=effort)
            task = self._task(action, subject, cwd, legs, plan, cfg, runtime, recorded)
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
        route = self.settings.route(action.kind, None)
        runtime, model, effort = route.runtime, route.model, route.effort
        self.runtime.set_context("fix", name, action.number, action.number, claim_target=action.number)
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
                # Open issues only. A report on a closed issue is a regression,
                # a change to what it delivered (#1328 reversed #1325, which a
                # title alone cannot show) or already fixed, which RESOLVE proves.
                # A closed one is never the original to close this against.
                index = [row for row in self.gateway.issue_index()
                         if row[0] != action.number and row[1] == "open"]
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
                blockable=tuple(n for n, _, _, labels in index
                                if Label.AI_READY in labels and Label.NEEDS_HUMAN not in labels),
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

    def _task(
        self, action: RunAgent, subject, cwd: str, legs: Mapping[ItemKey, tuple[LegEnd, ...]], plan: NextLeg,
        cfg: tasks.FixTaskConfig, runtime: RuntimeName, recorded: Iterable[LegRun],
    ):
        """A RESOLVE starts as `plan` says; a fresh session is briefed like a first RESOLVE."""
        if action.kind is AgentKind.RESOLVE:
            match plan.start:
                case Continue(session):
                    return tasks.build_continue_task(cwd, session, cfg)
                case AnswerResume(session):
                    return tasks.build_resume_task(cwd, session, cfg)
            return tasks.build_resolve_task(
                action.number, cwd, cfg, self._resolve_branch(action.number, subject.title),
                steward_criteria(recorded),
            )
        # PR-stage legs continue the issue's latest session on their runtime: it wrote the code.
        session = latest_session(sorted((end for ends in legs.values() for end in ends if end.kind in ROUTED_ACTS),
                                        key=lambda end: end.ended_at), runtime.value)
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
        merged = {n: p.merged_at for p in world.prs if p.state == "MERGED" and (n := harness_branch_issue(p.head_ref_name))}
        base = base_ci_actions(world.base, settings.harness.marker, {i.number: i.labels for i in world.issues}, merged)
        fixing = base_fix(world.base)
        first = frozenset({issue_key(n) for n in fixing} | {
            pr_key(p.number) for p in world.prs if p.state == "OPEN" and harness_branch_issue(p.head_ref_name) in fixing})
        gaps = gap_actions(local.legs, states, GapTarget(settings.repository, settings.target_public),
                           settings.harness_repo, settings.harness.marker, local.reported)
        return fix_actions(states, settings.harness, slots.heavy, merge=settings.auto_merge,
                           light=slots.light, waiting_since=waiting, first=first) + base + gaps
        # Architecture passes (ADR 0017) are off at the operator's request
        # (2026-10-21, tauri-explorer release). Re-enable: append
        # `architecture_actions(world, settings.harness)` above.
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
    live: LiveConfig | None = None,
) -> Role:
    """The fix role: every Action but REVIEW, paid legs up to
    FIX_DISPATCH_CONCURRENCY. Routes, concurrency, caps and the issue pickup
    come from `live`'s current snapshot (ADR 0007 amendment)."""
    log = log or stderr_log("fix")
    harness_repo, target_public = harness_gap_repo(cfg, gateway, log)
    live = live or LiveConfig(cfg)

    def route(kind: AgentKind, tier: Tier | None) -> ModelRoute:
        current = live.current
        if kind is AgentKind.STEWARD:
            return current.steward_route()
        if kind is AgentKind.TRIAGE:
            return current.triage_route()
        return current.fix_route(tier or DEFAULT_TIER)

    settings = FixSettings(cfg.repo, cfg.auto_merge, cfg.base_branch, "",
                           Harness(fix_bot=cfg.fix_bot, review_bot=cfg.review_bot), route,
                           pickup=lambda: live.current.pickup,
                           triage_budget_usd=cfg.triage_budget_usd,
                           triage_cwd=os.path.join(cfg.state_dir, "triage"),
                           harness_repo=harness_repo, target_public=target_public)
    task_config = tasks.FixTaskConfig(cfg.repo, cfg.base_branch, cfg.agent_md, cfg.fix_budget_usd,
                                      runtime=cfg.fixer_runtime)

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
                        capacity=lambda current: current.fix_dispatch_concurrency, live=live, log=log)
    log(f"fix: capabilities: triage=on resolve=on merge={'on' if cfg.auto_merge else 'off'}")
    return role


__all__ = ["FixLegs", "FixSettings", "compose_fix_daemon", "fix_actions", "fix_choose", "harness_gap_repo",
           "human_reply"]
