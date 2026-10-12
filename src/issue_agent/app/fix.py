"""The fix role: observe -> derive -> policy -> act (refactor slice 5b).

One tick reads GitHub (`GitHubGateway.build_fix_world`) and the local facts
GitHub cannot hold (`FixFacts`), derives every item's state, maps it to
Actions, and executes them (`adapters.act.GitHubAct`). Nothing about an item's lifecycle is
stored here: the run table records ended legs, the dedupe table records which
idempotent writes landed, and everything else is re-derived next tick.

The fix role executes every Action except `RunAgent(REVIEW)`, which belongs to
the review role.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from ..adapters.control_store import FixRun
from ..domain.actions import Action, AgentKind, MergePullRequest, PostComment, RunAgent, Target
from ..domain.backoff import LegEnd, dispatched_kind
from ..domain.ci import failed_checks, parse_timestamp
from ..domain.labels import Label
from ..domain.leg import Leg, LegTarget
from ..domain.outcome import LegClass, classify_leg
from ..domain.policy import actions as policy_actions
from ..domain.refs import harness_branch_issue, slugify
from ..domain.state import Harness, ItemKey, Local, derive, issue_key, pr_key
from ..domain.trust import as_untrusted
from ..domain.world import REVIEW_CHANGES_REQUESTED, Comment, WorldState
from .planners import tasks
from .workflows.base import run_leg

# Run kinds written before slice 5b map onto the acts they continued; ROUTE
# was an advisory classifier leg, never a dispatch.
_LEGACY_KIND = {"RESUME": AgentKind.RESOLVE}
_ISSUE_RUN_KINDS = frozenset({"RESOLVE", "RESUME"})
_PR_RUN_KINDS = frozenset({"FIX_CI", "REBASE", "ADDRESS_REVIEW", "PR_RESUME"})
_FIX_ACTS = frozenset({AgentKind.RESOLVE, AgentKind.FIX_CI, AgentKind.REBASE, AgentKind.ADDRESS_REVIEW})
_QUESTION_LIMIT = 4000


def run_item(run: FixRun) -> ItemKey | None:
    """The item a recorded fix run belongs to: its issue for issue-stage legs,
    its PR for PR-stage legs."""
    if run.kind in _ISSUE_RUN_KINDS and run.issue > 0:
        return issue_key(run.issue)
    if run.kind in _PR_RUN_KINDS and run.number > 0:
        return pr_key(run.number)
    return None


def leg_end(run: FixRun) -> LegEnd | None:
    """A recorded run as derive's `LegEnd`. A run without a `fix-leg` receipt
    never returned (died, reclaimed, refused before launch): a machine end."""
    kind = _LEGACY_KIND.get(run.kind) or dispatched_kind(run.kind)
    if kind is None or kind is AgentKind.ROUTE or run.completed_at.utcoffset() is None:
        return None
    leg = run.receipt.get("leg") if run.receipt is not None else None
    started = run.started_at
    if started is not None and (started.utcoffset() is None or started > run.completed_at):
        started = None
    return LegEnd(
        kind, run.completed_at, leg not in (LegClass.AGENT.value, LegClass.ASKED.value), run.head,
        asked=leg == LegClass.ASKED.value, started_at=started,
    )


def local_legs(runs: Iterable[FixRun]) -> dict[ItemKey, tuple[LegEnd, ...]]:
    out: dict[ItemKey, list[LegEnd]] = {}
    for run in runs:
        key, end = run_item(run), leg_end(run)
        if key is not None and end is not None:
            out.setdefault(key, []).append(end)
    return {key: tuple(sorted(ends, key=lambda e: e.ended_at)) for key, ends in out.items()}


def dispatch_item(action: RunAgent) -> ItemKey:
    return issue_key(action.number) if action.kind is AgentKind.RESOLVE else pr_key(action.number)


def leased_items(active: Iterable[RunAgent], held_issues: Iterable[int]) -> frozenset[ItemKey]:
    """Items with a leg in flight: this process's dispatches, plus every issue
    another process (or an orphaned child) holds a live fix claim or lease on."""
    return frozenset(dispatch_item(a) for a in active) | frozenset(
        issue_key(n) for n in held_issues if type(n) is int and n > 0
    )


def item_issues(world: WorldState) -> frozenset[int]:
    return frozenset(i.number for i in world.issues if i.number > 0) | frozenset(
        n for p in world.prs if (n := harness_branch_issue(p.head_ref_name)) is not None
    )


def fix_actions(
    world: WorldState, local: Local, harness: Harness, slots: int, *, merge: bool = True,
) -> tuple[Action, ...]:
    """The tick's Actions for the fix role: all but REVIEW dispatches, with at
    most `slots` paid dispatches (issues first, then PRs, by number). With
    `merge` off (AUTO_MERGE=false) an approved green PR waits for a human."""
    out: list[Action] = []
    for action in policy_actions(derive(world, local, harness), harness):
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


def question_body(harness: Harness, needs: str) -> str:
    """The agent's question as a marked harness comment. The text is the
    agent's: hidden-comment openers are neutralised so it can never carry a
    stop tag or the harness marker."""
    text = (needs or "").replace("<!--", "<​!--").strip()[:_QUESTION_LIMIT] or "(no question text)"
    return f"{harness.marker}\n**The agent needs a human's judgment:**\n\n{text}"


@dataclass(frozen=True)
class FixSettings:
    repository: str
    auto_merge: bool
    base_branch: str
    runtime: str
    owner_id: str
    harness: Harness


class FixFacts:
    """The local facts derive needs, read from SQLite each tick. Any read
    failure raises: the daemon skips the tick rather than deciding blind."""

    def __init__(
        self,
        control,
        settings: FixSettings,
        *,
        active: Callable[[], Iterable[RunAgent]],
        headroom: Callable[[], bool],
        lifetime_capped: Callable[[int], bool],
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.control = control
        self.settings = settings
        self.active = active
        self.headroom = headroom
        self.lifetime_capped = lifetime_capped
        self.now = now

    def held_issues(self) -> set[int]:
        repo = self.settings.repository
        held = {c.target_key for c in self.control.active_dispatch_claims(repo, "fix")}
        for lease in self.control.leases():
            try:
                dispatch = json.loads(lease.dispatch_id)
            except (TypeError, ValueError):
                continue
            if isinstance(dispatch, dict) and dispatch.get("role") == "fix" and dispatch.get("repository") == repo:
                held.add(dispatch.get("issue"))
        return held

    def __call__(self, world: WorldState) -> Local:
        # Sampled before the SQLite reads: a leg ending mid-read then reads
        # busy for one more tick instead of free with its end not yet seen.
        active = tuple(self.active())
        issues = item_issues(world)
        runs = self.control.fix_runs(self.settings.repository, issues)
        return Local(
            now=self.now(),
            legs=local_legs(runs),
            leases=leased_items(active, self.held_issues()),
            headroom=self.headroom(),
            lifetime_capped=frozenset(n for n in issues if self.lifetime_capped(n)),
        )


class FixLegs:
    """Runs one fix leg: re-checks the item against GitHub, prepares the
    worktree, picks the session to resume, builds the task, and runs it
    through the accounting runtime. A preparation failure is recorded as a
    refused (machine) leg so the item backs off instead of retrying hot."""

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
        post: Callable[[PostComment], None],
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
        self.post = post
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
        self.runtime.set_context("fix", action.kind.name, action.number, issue, claim_target=issue, head=action.head)
        if not self.control.claim_dispatch(repo, "fix", issue, action.kind.name, owner, frozenset()):
            self.log(f"{action.kind.name} #{action.number}: skipped — issue #{issue} is held by another leg")
            return
        try:
            await self._run_claimed(action, item, issue, subject)
        finally:
            self.control.release_dispatch_claim(repo, "fix", issue, owner)

    async def _run_claimed(self, action: RunAgent, item: ItemKey, issue: int, subject) -> None:
        runs = sorted(self.control.fix_runs(self.settings.repository, (issue,)), key=lambda r: r.completed_at)
        latest = next((r for r in reversed(runs) if run_item(r) == item), None)
        try:
            if action.kind is AgentKind.RESOLVE:
                branch = self._resolve_branch(issue, subject.title)
                cwd = self.worktrees.ensure(issue, branch)
            else:
                cwd = self.worktrees.sync(issue, subject.head_ref_name, action.head)
            if self.prepare_cwd is not None:
                self.prepare_cwd(cwd)
            task = self._task(action, subject, cwd, runs, latest)
            task = replace(task, env={**task.env, **self._git_identity()})
        except Exception as exc:  # noqa: BLE001 - recorded as a machine end below
            self.log(f"{action.kind.name} #{action.number}: preparation failed: {exc}")
            self.accounting.record_refused_leg()
            return
        target = LegTarget.ISSUE if action.kind is AgentKind.RESOLVE else LegTarget.PR
        result = await run_leg(
            self.runtime, self.log, Leg(action.kind.name, target, action.number, True, ("global", "repository", "issue")),
            task,
        )
        if classify_leg(result, resumed=bool(task.resume)) is LegClass.ASKED:
            self.post(PostComment(
                Target.ISSUE if action.kind is AgentKind.RESOLVE else Target.PR, action.number,
                question_body(self.settings.harness, result.needs),
                key=f"question:{item.target.name.lower()}:{item.number}:{uuid.uuid4().hex}",
            ))

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

    def _session(self, runs: list[FixRun]) -> str:
        for run in reversed(runs):
            receipt = run.receipt or {}
            session = receipt.get("session")
            if receipt.get("runtime") == self.settings.runtime and isinstance(session, str) and session:
                return session
        return ""

    def _task(self, action: RunAgent, subject, cwd: str, runs: list[FixRun], latest: FixRun | None):
        cfg = self.task_config
        receipt = (latest.receipt or {}) if latest is not None else {}
        asked = receipt.get("leg") == LegClass.ASKED.value
        latest_session = self._session([latest]) if latest is not None else ""
        reply = human_reply(subject.comments, latest.completed_at, self.settings.harness) if asked and latest else ""
        if action.kind is AgentKind.RESOLVE:
            if asked:
                if latest_session:
                    return tasks.build_resume_task(as_untrusted(reply), cwd, latest_session, cfg)
                return tasks.build_fresh_reply_task(
                    action.number, as_untrusted(subject.title), as_untrusted(subject.body), as_untrusted(reply), cwd, cfg,
                )
            if latest is not None and receipt.get("leg") == LegClass.MACHINE.value and latest_session:
                # A wall-cap machine end resumes its session once (classify_leg).
                return tasks.build_continue_task(cwd, latest_session, cfg)
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


def decide_with(
    facts: Callable[[WorldState], Local],
    settings: FixSettings,
    slots: Callable[[], int],
    log: Callable[[str], None],
) -> Callable[[WorldState], list[Action]]:
    def decide(world: WorldState) -> list[Action]:
        chosen = list(fix_actions(world, facts(world), settings.harness, slots(), merge=settings.auto_merge))
        for action in chosen:
            log(f"fix: {action!r}")
        return chosen
    return decide


def lease_slots(daemon_ref: Mapping[str, object]) -> Callable[[], int]:
    def slots() -> int:
        daemon = daemon_ref.get("daemon")
        return int(getattr(daemon, "available_dispatch_slots", 0)) if daemon is not None else 0
    return slots


__all__ = [
    "FixFacts", "FixLegs", "FixSettings", "decide_with", "dispatch_item", "fix_actions", "human_reply",
    "item_issues", "leased_items", "leg_end", "local_legs", "question_body", "run_item",
]
