"""The review role: observe -> derive -> policy -> act, REVIEW legs only (slice 5c).

The role ticks exactly like the fix role (`tick.py`) and keeps only
`RunAgent(REVIEW)`: derive owes a review when CI is green on a harness PR's
head and the review bot has no verdict on that head. Every other Action
(labels, stop comments, merges, the other legs) is the fix role's, so
`needs-human` keeps one writer and a REVIEW attempt cap is stopped by fix.

A REVIEW leg reads the exact head (a detached checkout pinned to it and the
base...head diff), runs the configured review model once, and the harness
submits the parsed verdict as one GitHub review on that commit under the
review bot's identity. The verdict is the agent's claim: anything but an
exact APPROVE or REQUEST_CHANGES with a body is no verdict, and the leg
counts as an attempt (derive caps REVIEW attempts per head). So does an end
retrying cannot change: GitHub refusing the submission (a 4xx) or a head with
nothing to review. A transport failure is a machine end and backs off.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace

from ..adapters.act import submit_review
from ..adapters.control_store import LegRun
from ..domain.actions import Action, AgentKind, RunAgent
from ..domain.agent_task import RuntimeName
from ..domain.identity import login_eq
from ..domain.leg import Leg, LegTarget
from ..domain.outcome import LegClass
from ..domain.policy import actions as policy_actions
from ..domain.refs import harness_branch_issue
from ..domain.review import Verdict, parse_verdict
from ..domain.routing import Tier
from ..domain.state import DerivedState, Harness, ItemKey, Local, pr_key
from ..domain.trust import as_untrusted
from ..domain.world import REVIEW_APPROVED, REVIEW_CHANGES_REQUESTED, Comment, PRView, WorldState
from ..adapters.github import format_pr_discussion
from ..adapters.worktree import ReviewCheckouts
from .config import HarnessConfig
from .planners import tasks
from .tick import Choose, Slots, Role, compose_role, leg_class, run_item, run_leg, stderr_log


def review_actions(states: Mapping[ItemKey, DerivedState], harness: Harness, slots: int) -> tuple[Action, ...]:
    """The tick's REVIEW dispatches, at most `slots` (PRs by number)."""
    out: list[Action] = []
    for action in policy_actions(states, harness):
        if isinstance(action, RunAgent) and action.kind is AgentKind.REVIEW and slots > 0:
            slots -= 1
            out.append(action)
    return tuple(out)


def review_choose(harness: Harness) -> Choose:
    def choose(_world: WorldState, states: Mapping[ItemKey, DerivedState], slots: Slots,
               _local: Local) -> tuple[Action, ...]:
        return review_actions(states, harness, slots.heavy)
    return choose


def stored_verdict(runs: Iterable[LegRun], pr: int, head: str) -> Verdict | None:
    """The verdict of this head's newest agent REVIEW leg, if it parsed one.
    The receipt holds what `parse_verdict` accepted; it is parsed again here,
    since a receipt is only as trusted as the agent output it came from."""
    mine = sorted((r for r in runs if r.kind == AgentKind.REVIEW.name and run_item(r) == pr_key(pr)
                   and r.head == head and leg_class(r) is not LegClass.MACHINE), key=lambda r: r.completed_at)
    if not mine or mine[-1].receipt is None:
        return None
    raw = mine[-1].receipt.get("verdict")
    if not isinstance(raw, dict):
        return None
    return parse_verdict({"decision": raw.get("event"), "body": raw.get("body")})


@dataclass(frozen=True)
class ReviewAct:
    """The review role's act step: it only dispatches REVIEW legs, which
    submit their own verdicts."""

    dispatch: Callable[[RunAgent], None]

    def execute(self, action: Action) -> None:
        if not isinstance(action, RunAgent):
            raise TypeError(f"the review role does not execute {action!r}")
        self.dispatch(action)


class _NothingToReview(Exception):
    """The head's base...head diff is empty: an end retrying cannot change."""


# The prior review rides along whole up to this size; it is the bot's own
# body, already bounded by REVIEW_BODY_LIMIT, so this only trims the extreme.
_PRIOR_REVIEW_BYTES = 20_000


@dataclass(frozen=True)
class ReviewSettings:
    repository: str
    owner_id: str
    harness: Harness


class ReviewLegs:
    """Runs one REVIEW leg and submits its verdict. A preparation or transport
    failure is recorded as a refused (machine) leg, so the PR backs off instead
    of retrying hot. GitHub refusing the submission, or an empty diff, is
    recorded as an attempt, so the REVIEW attempt cap stops the PR at
    `needs-human` (written by the fix role)."""

    def __init__(
        self,
        gateway,
        control,
        settings: ReviewSettings,
        *,
        runtime,
        accounting,
        checkouts,
        prepare_cwd: Callable[[str, RuntimeName | None], None] | None,
        task_config: tasks.ReviewTaskConfig,
        route: Callable[[Tier | None], tuple[RuntimeName | None, str | None]] | None = None,
        log: Callable[[str], None],
    ) -> None:
        self.gateway = gateway
        self.control = control
        self.settings = settings
        self.runtime = runtime
        self.accounting = accounting
        self.checkouts = checkouts
        self.prepare_cwd = prepare_cwd
        self.task_config = task_config
        # None: the role's default runtime with the configured REVIEW_MODEL.
        self.route = route or (lambda _tier: (None, task_config.model))
        self.log = log

    async def run(self, action: RunAgent) -> None:
        if action.kind is not AgentKind.REVIEW:
            raise ValueError(f"the review role does not run {action.kind.name}")
        pr = self.gateway.get_pr(action.number, mergeability=False, checks=False)
        issue = harness_branch_issue(pr.head_ref_name)
        if pr.state != "OPEN" or issue is None or pr.head_ref_oid != action.head:
            self.log(f"REVIEW PR #{action.number}: skipped — PR closed or head moved")
            return
        repo, owner = self.settings.repository, self.settings.owner_id
        if not self.control.lease(repo, "review", issue, owner):
            self.log(f"REVIEW PR #{action.number}: skipped — issue #{issue} already has a review leg in flight")
            return
        try:
            await self._run_claimed(action, issue, pr)
        finally:
            self.control.unlease(repo, "review", issue, owner)
            try:
                self.checkouts.remove(action.number)
            except Exception as exc:  # noqa: BLE001 - a stale checkout is disk, not state
                self.log(f"REVIEW PR #{action.number}: pinned checkout not removed: {exc}")

    async def _run_claimed(self, action: RunAgent, issue: int, pr: PRView) -> None:
        bot = self.settings.harness.review_bot
        on_head = [r for r in pr.reviews if login_eq(r.author, bot) and r.commit_id == action.head]
        if any(r.state.upper() in (REVIEW_APPROVED, REVIEW_CHANGES_REQUESTED) for r in on_head):
            self.log(f"REVIEW PR #{action.number}: skipped — already reviewed at {action.head[:12]}")
            return
        runs = self.control.leg_runs(self.settings.repository, (issue,))
        # A REVIEW leg never resumes a session, so a wall cap is an attempt.
        self.runtime.set_context("review", action.kind.name, action.number, issue, claim_target=issue,
                                 head=action.head, resumed=True,
                                 tier=action.tier.value if action.tier is not None else "")
        verdict = stored_verdict(runs, action.number, action.head)
        if verdict is not None and not on_head:
            # A paid verdict whose submission never landed (a crash, or GitHub
            # refused it): submit it, free. A dismissed verdict is not resubmitted.
            self._submit(action, verdict)
            return
        try:
            task = self._task(action, pr)
        except _NothingToReview:
            self.log(f"REVIEW PR #{action.number}: base...head is empty at {action.head[:12]}; "
                     "nothing to review (counted as an attempt)")
            self.accounting.record_refused_leg(attempt={"summary": "nothing to review"})
            return
        except Exception as exc:  # noqa: BLE001 - recorded as a machine end below
            self.log(f"REVIEW PR #{action.number}: preparation failed: {exc}")
            self.accounting.record_refused_leg()
            return
        result = await run_leg(
            self.runtime, self.log, Leg(action.kind.name, LegTarget.PR, action.number, True,
                                        ("global", "repository", "issue")), task,
        )
        verdict = parse_verdict(result.structured) if not result.is_error else None
        if verdict is None:
            self.log(f"REVIEW PR #{action.number}: no usable verdict "
                     f"(status={result.status or ('error' if result.is_error else 'ok')}); nothing submitted")
            return
        self._submit(action, verdict)

    def _submit(self, action: RunAgent, verdict: Verdict) -> None:
        pr, head = action.number, action.head
        try:
            landed = submit_review(self.gateway, pr, head, verdict, self.settings.harness.review_bot)
        except Exception as exc:  # noqa: BLE001 - GitHub did not answer or confirm: a machine end
            self.log(f"REVIEW PR #{pr}: submission failed, retried after backoff: {exc}")
            self.accounting.record_refused_leg()
            return
        if landed:
            self.log(f"PR #{pr}: {verdict.event} stands on {head[:12]}")
            return
        self.log(f"REVIEW PR #{pr}: GitHub refused the {verdict.event} on {head[:12]} (counted as an attempt)")
        self.accounting.record_refused_leg(attempt={"verdict": {"event": verdict.event, "body": verdict.body}})

    def _task(self, action: RunAgent, pr: PRView):
        runtime, model = self.route(action.tier)
        self.log(f"REVIEW PR #{action.number}: tier {getattr(action.tier, 'value', '(none)')} -> "
                 f"{runtime.value if runtime else '(default runtime)'}:{model or '(default model)'}")
        cwd = self.checkouts.ensure(action.number, action.head)
        if self.prepare_cwd is not None:
            self.prepare_cwd(cwd, runtime)
        diff = self.gateway.pr_diff(action.number, pr.base_ref_oid, action.head)
        if diff.head_oid != action.head or diff.base_oid != pr.base_ref_oid:
            raise RuntimeError("the revision-bound diff does not match the reviewed head")
        if not diff.text.strip():
            raise _NothingToReview
        discussion = format_pr_discussion(pr.comments)
        prior = _prior_review(pr, self.settings.harness.review_bot)
        task = tasks.build_review_task(
            action.number, as_untrusted(pr.title), as_untrusted(pr.body), as_untrusted(diff.text),
            self.task_config, discussion=as_untrusted(discussion) if discussion else None,
            prior_review=as_untrusted(prior) if prior else None,
        )
        return replace(task, cwd=cwd, runtime=runtime, model=model)


def _prior_review(pr: PRView, bot: str) -> str:
    """The review bot's latest review body on this PR, bounded and marked like
    the discussion: what the reviewer asked for last round, so it can judge
    whether the new commits answer it rather than re-judge from scratch."""
    mine = [r for r in pr.reviews if login_eq(r.author, bot) and r.body.strip()]
    latest = max(mine, key=lambda r: r.submitted_at, default=None)
    if latest is None:
        return ""
    return format_pr_discussion((Comment(latest.author, latest.body),),
                                body_bytes=_PRIOR_REVIEW_BYTES, total_bytes=_PRIOR_REVIEW_BYTES)


def compose_review_daemon(
    cfg: HarnessConfig, gateway, runner, *, checkouts=None, log: Callable[[str], None] | None = None,
) -> Role:
    """The review role: REVIEW dispatches only, one at a time.

    `checkouts` pins each leg to its head; by default a detached per-PR
    worktree of TARGET_DIR (keifu #165: reviews of the operator's live
    checkout produced phantom change requests under fresh SHAs). Two
    misconfigurations no retry can fix refuse to start (ValueError, so `main`
    exits INVALID_CONFIG): no TARGET_DIR, and a review credential that is not
    REVIEW_BOT (it posted a duplicate review on every retry, since the
    read-back looked for the wrong login)."""
    log = log or stderr_log("review")
    if checkouts is None:
        if not cfg.target_dir:
            raise ValueError("the review role needs TARGET_DIR: each review reads a checkout pinned to its head")
        checkouts = ReviewCheckouts(cfg.target_dir, worktree_root=cfg.worktree_root)
    acting_as = gateway.viewer_login()
    if not login_eq(acting_as, cfg.review_bot):
        raise ValueError(
            f"REVIEW_BOT is {cfg.review_bot!r} but the review credential acts as "
            f"{acting_as or 'nobody GitHub would name'!r}: set REVIEW_BOT to the "
            "credential's login, or give the review role REVIEW_BOT's credential")
    harness = Harness(fix_bot=cfg.fix_bot, review_bot=cfg.review_bot)
    task_config = tasks.ReviewTaskConfig(cfg.repo, cfg.base_branch, cfg.review_prompt, cfg.review_budget_usd,
                                         cfg.review_model)

    def make_legs(*, runtime, accounting, control, owner_id, prepare_cwd) -> ReviewLegs:
        return ReviewLegs(gateway, control, ReviewSettings(cfg.repo, owner_id, harness), runtime=runtime,
                          accounting=accounting, checkouts=checkouts, prepare_cwd=prepare_cwd,
                          task_config=task_config, route=cfg.review_route, log=log)

    role = compose_role(cfg, "review", gateway, runner, make_legs=make_legs,
                        make_act=lambda dispatch, _control: ReviewAct(dispatch),
                        choose=review_choose(harness), log=log)
    log(f"review: capabilities: review=on runtime={cfg.reviewer_runtime.value}")
    return role


__all__ = ["ReviewAct", "ReviewLegs", "ReviewSettings", "compose_review_daemon", "review_actions",
           "review_choose", "stored_verdict"]
