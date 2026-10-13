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

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from ..adapters.control_store import LegRun
from ..domain.actions import Action, AgentKind, MergePullRequest, RunAgent
from ..domain.backoff import dispatched_kind
from ..domain.ci import failed_checks, parse_timestamp
from ..domain.labels import Label
from ..domain.leg import Leg, LegTarget
from ..domain.outcome import LegClass
from ..domain.policy import actions as policy_actions
from ..domain.refs import harness_branch_issue, slugify
from ..domain.state import DISPATCHABLE, DerivedState, Harness, ItemKey, Wait, WaitReason
from ..domain.trust import as_untrusted
from ..domain.world import REVIEW_CHANGES_REQUESTED, Comment, WorldState
from .planners import tasks
from .tick import Choose, dispatch_item, leg_class, run_item
from .tick import run_leg

_FIX_ACTS = DISPATCHABLE - {AgentKind.REVIEW}


def fix_actions(
    states: Mapping[ItemKey, DerivedState], harness: Harness, slots: int, *, merge: bool = True,
) -> tuple[Action, ...]:
    """The tick's Actions for the fix role: all but REVIEW dispatches, with at
    most `slots` paid dispatches (issues first, then PRs, by number). With
    `merge` off (AUTO_MERGE=false) an approved green PR waits for a human."""
    out: list[Action] = []
    for action in policy_actions(states, harness):
        if isinstance(action, MergePullRequest) and not merge:
            continue
        if isinstance(action, RunAgent):
            if action.kind not in _FIX_ACTS or slots <= 0:
                continue
            slots -= 1
        out.append(action)
    return tuple(out)


def human_reply(comments: Iterable[Comment], after: datetime, harness: Harness) -> str:
    """Non-harness, non-bot comments posted after `after`, oldest first."""
    picked = [
        (at, c) for c in comments
        if (at := parse_timestamp(c.created_at)) is not None and at > after
        and not harness.wrote(c) and not c.is_bot
    ]
    return "\n\n".join(f"@{c.author}: {c.body}" for _, c in sorted(picked, key=lambda p: p[0]))


@dataclass(frozen=True)
class FixSettings:
    repository: str
    auto_merge: bool
    base_branch: str
    runtime: str
    owner_id: str
    harness: Harness


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
        prepare_cwd: Callable[[str], None] | None,
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
        if action.kind is AgentKind.RESOLVE:
            subject = self.gateway.get_issue(action.number)
            if subject.state != "open" or Label.AI_READY not in subject.labels or Label.NEEDS_HUMAN in subject.labels:
                self.log(f"RESOLVE #{action.number}: skipped — issue no longer ready")
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
        if not self.control.claim_dispatch(repo, "fix", issue, action.kind.name, owner):
            self.log(f"{action.kind.name} #{action.number}: skipped — issue #{issue} already has a fix leg in flight")
            return
        try:
            await self._run_claimed(action, item, issue, subject)
        finally:
            self.control.release_dispatch_claim(repo, "fix", issue, owner)

    async def _run_claimed(self, action: RunAgent, item: ItemKey, issue: int, subject) -> None:
        # The review role's legs are not this role's sessions to continue.
        runs = sorted((r for r in self.control.leg_runs(self.settings.repository, (issue,))
                       if dispatched_kind(r.kind) in _FIX_ACTS), key=lambda r: r.completed_at)
        mine = [r for r in runs if run_item(r) == item]
        # The newest agent leg, the rule derive uses: machine and transport
        # deaths after it neither drop its question nor reset resume-once.
        last = next((i for i in range(len(mine) - 1, -1, -1) if leg_class(mine[i]) is not LegClass.MACHINE), -1)
        asked = mine[last] if last >= 0 and leg_class(mine[last]) is LegClass.ASKED else None
        # Resume once (classify_leg): continue the newest machine end since that
        # leg that left a session; a wall cap on the continuation is an attempt.
        cut_off = next((r for r in reversed(mine[last + 1:]) if self._session([r])), None)
        self.runtime.set_context("fix", action.kind.name, action.number, issue, claim_target=issue,
                                 head=action.head, resumed=cut_off is not None)
        try:
            if action.kind is AgentKind.RESOLVE:
                branch = self._resolve_branch(issue, subject.title)
                cwd = self.worktrees.ensure(issue, branch)
            else:
                cwd = self.worktrees.sync(issue, subject.head_ref_name, action.head)
            if self.prepare_cwd is not None:
                self.prepare_cwd(cwd)
            task = self._task(action, subject, cwd, runs, cut_off, asked)
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

    def _session(self, runs: list[LegRun]) -> str:
        for run in reversed(runs):
            receipt = run.receipt or {}
            session = receipt.get("session")
            if receipt.get("runtime") == self.settings.runtime and isinstance(session, str) and session:
                return session
        return ""

    def _task(
        self, action: RunAgent, subject, cwd: str, runs: list[LegRun], cut_off: LegRun | None, asked: LegRun | None,
    ):
        """`cut_off` is the machine end this leg continues; `asked` the agent
        leg whose question a human has since answered."""
        cfg = self.task_config
        reply = human_reply(subject.comments, asked.completed_at, self.settings.harness) if asked else ""
        if action.kind is AgentKind.RESOLVE:
            if cut_off is not None:
                return tasks.build_continue_task(cwd, self._session([cut_off]), cfg)
            if asked is not None:
                if session := self._session([asked]):
                    return tasks.build_resume_task(as_untrusted(reply), cwd, session, cfg)
                return tasks.build_fresh_reply_task(
                    action.number, as_untrusted(subject.title), as_untrusted(subject.body), as_untrusted(reply), cwd, cfg,
                )
            return tasks.build_resolve_task(
                action.number, as_untrusted(subject.title), as_untrusted(subject.body), cwd, cfg,
                self._resolve_branch(action.number, subject.title),
            )
        # PR-stage legs continue the issue's latest session: it wrote the code.
        session = self._session(runs)
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


def _soft(read: Callable[[], str], log: Callable[[str], None]) -> str:
    """A prompt input: context, never control flow. A failed read leaves the
    leg with a weaker prompt instead of killing it."""
    try:
        return read() or ""
    except Exception as exc:  # noqa: BLE001 - see docstring
        log(f"prompt input unavailable: {exc}")
        return ""


def fix_choose(settings: FixSettings, log: Callable[[str], None]) -> Choose:
    def choose(world: WorldState, states: Mapping[ItemKey, DerivedState], slots: int) -> tuple[Action, ...]:
        prs = {p.number: p for p in world.prs}
        for key, state in sorted(states.items(), key=lambda kv: kv[0].sort_key):
            if state == Wait(WaitReason.MERGEABILITY_UNKNOWN) and (pr := prs.get(key.number)) is not None:
                # Approved and green but not mergeable now: BLOCKED waits on a human (branch protection).
                log(f"fix: PR #{key.number} waits to merge: mergeable={pr.mergeable or '?'} "
                    f"state={pr.merge_state or '?'}")
        return fix_actions(states, settings.harness, slots, merge=settings.auto_merge)
    return choose


__all__ = ["FixLegs", "FixSettings", "fix_actions", "fix_choose", "human_reply"]
