"""Derive each harness item's state from one observation. Pure.

The contract is the tick table of docs/open/2026-10-12-minimal-harness-design.md
read with docs/open/2026-10-12-refactor-decisions.md. Nothing here is stored:
every tick re-derives from GitHub (the `WorldState`) plus the few local facts
GitHub cannot hold (`Local`: ended legs, leases, money headroom, `now`).

Precedence, the single place it is defined (first match wins, per item):

1. Busy        a lease is held on the item or its issue, or one of its legs
               ended after the observation was taken (the world may predate
               that leg's push). A running leg's outcome is unknown, so
               nothing else is decided about the item.
2. Unread      the PR's content could not be read -> Wait (no stop or label
               logic runs on a placeholder).
3. needs-human labelled with this episode's stop comment (one newer than the
               item's last leg end; a stop is always posted after the legs it
               judges): a lifetime stop on a still-capped issue -> Wait (money
               is a grant, so a reply cannot release it); a qualifying reply
               (Q8) -> Release; else Wait. Labelled without one (the comment
               failed, or a human labelled it): the steps below run, a
               NeedsHuman is emitted (the label add is idempotent, the comment
               keyed) and anything else waits.
4. Question    the item's latest agent leg asked a human for judgment and no
               stop comment follows it -> NeedsHuman, whose comment carries
               the question (the stop is the single needs-human writer).
5. Workflow    the PR touches `.github/` and no released workflow stop exists
               on it -> NeedsHuman.
6. Publication an issue with a pushed `fix/<issue>-` branch ahead of base and
               owed a PR -> OpenPR (free; the agent pushes, the harness
               opens, Q11).
7. Item state  PR: head unknown -> Wait; conflicted -> REBASE; CI unread or
               for another head -> Wait; red -> FIX_CI; pending -> Wait; green
               and BEHIND -> Refresh; a human's standing CHANGES_REQUESTED on
               head -> ADDRESS_REVIEW; no bot verdict on head -> REVIEW; bot
               CHANGES_REQUESTED on head -> ADDRESS_REVIEW; bot APPROVED head ->
               Merge, unless a human's standing CHANGES_REQUESTED is on an
               older head (Wait) or GitHub does not report it mergeable and
               unblocked (Wait: branch protection waits on a human).
               Issue: `ai-ready`, open, no open or merged harness PR ->
               RESOLVE.
8. Dispatch gates, for a Dispatch from step 7 only (merge/refresh/open are
               free): bot review rounds >= cap (ADDRESS_REVIEW on a bot
               verdict) -> NeedsHuman; attempts >= cap -> NeedsHuman; the
               issue's lifetime cap cannot afford a leg -> NeedsHuman; machine
               backoff -> Backoff; daily or global headroom short -> Wait
               (silent, Q7).

Attempts for act A (Q21) are the tail run of A's agent legs after the reset
floor: machine legs are skipped, any other act's agent leg ends the run, and
REVIEW counts only legs on the current head. Progress (an act changing, or a
review of a new head) therefore never trips the cap; repeating one act
without progress does.

Untrusted timestamps fail toward stopping: an unparseable one never resets a
count or releases a stop, and an unparseable bot review still counts as a round.
Likewise a push GitHub cannot attribute to a login (an unlinked commit email)
is never a reset: only a push by a known non-harness login is.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType

from .actions import AgentKind, Target
from .backoff import LegEnd, machine_backoff_until
from .checks import Observed
from .ci import Clear, Failed, Waiting, parse_timestamp, reduce_ci
from .identity import login_eq
from .labels import Label
from .refs import harness_branch_issue
from .world import REVIEW_APPROVED, REVIEW_CHANGES_REQUESTED, Branch, Comment, PRView, WorldState

ATTEMPT_CAP = 3
REVIEW_ROUND_CAP = 3
DISPATCHABLE = frozenset({
    AgentKind.RESOLVE, AgentKind.FIX_CI, AgentKind.REBASE, AgentKind.REVIEW, AgentKind.ADDRESS_REVIEW,
})
_MAX_MARKER = 200
_STANDING = frozenset({REVIEW_APPROVED, REVIEW_CHANGES_REQUESTED, "DISMISSED"})
_STOP_TAG_RE = re.compile(r"<!-- needs-human:([a-z-]+) -->")


@dataclass(frozen=True)
class ItemKey:
    """An issue (issue stage) or a harness PR (PR stage)."""

    target: Target
    number: int

    def __post_init__(self) -> None:
        if not isinstance(self.target, Target) or type(self.number) is not int or self.number <= 0:
            raise ValueError("item key requires a target and a positive number")

    @property
    def sort_key(self) -> tuple[int, int]:
        return (0 if self.target is Target.ISSUE else 1, self.number)


def issue_key(number: int) -> ItemKey:
    return ItemKey(Target.ISSUE, number)


def pr_key(number: int) -> ItemKey:
    return ItemKey(Target.PR, number)


@dataclass(frozen=True)
class Harness:
    """Who the harness is on GitHub. A harness comment carries the hidden
    marker AND a harness login: the marker alone is forgeable by any commenter,
    and the login alone is not enough because FIX_BOT can be the operator's own
    login (Q1). Reviews and pushes are attributed by login."""

    fix_bot: str
    review_bot: str
    marker: str = "<!-- issue-agent -->"

    def __post_init__(self) -> None:
        if any(not isinstance(v, str) or not v.strip() for v in (self.fix_bot, self.review_bot)):
            raise ValueError("harness requires both bot logins")
        if (not isinstance(self.marker, str) or len(self.marker) > _MAX_MARKER
                or not self.marker.startswith("<!--") or not self.marker.endswith("-->")
                or len(self.marker) <= len("<!---->")):
            raise ValueError("harness marker must be a bounded hidden HTML comment")

    def owns_login(self, login: str) -> bool:
        return login_eq(login, self.fix_bot) or login_eq(login, self.review_bot)

    def wrote(self, comment: Comment) -> bool:
        return self.marker in comment.body and self.owns_login(comment.author)


@dataclass(frozen=True)
class Local:
    """The local facts derive needs, as plain values. `legs` are the ended legs
    per item; `lifetime_capped` holds issue numbers over their lifetime cap;
    `headroom` is False when the daily or global cap leaves no room for a leg."""

    now: datetime
    legs: Mapping[ItemKey, tuple[LegEnd, ...]] = field(default_factory=dict)
    leases: frozenset[ItemKey] = frozenset()
    headroom: bool = True
    lifetime_capped: frozenset[int] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.now, datetime) or self.now.utcoffset() is None:
            raise ValueError("local facts require an aware now")
        if not isinstance(self.legs, Mapping) or any(
            not isinstance(k, ItemKey) or not isinstance(v, tuple) or any(not isinstance(e, LegEnd) for e in v)
            for k, v in self.legs.items()
        ):
            raise ValueError("legs must map item keys to tuples of leg ends")
        object.__setattr__(self, "legs", MappingProxyType(dict(self.legs)))
        if not isinstance(self.leases, frozenset) or any(not isinstance(k, ItemKey) for k in self.leases):
            raise ValueError("leases must be a frozenset of item keys")
        if type(self.headroom) is not bool:
            raise ValueError("headroom must be an exact bool")
        if not isinstance(self.lifetime_capped, frozenset) or any(
            type(n) is not int or n <= 0 for n in self.lifetime_capped
        ):
            raise ValueError("lifetime_capped must be a frozenset of issue numbers")


class WaitReason(str, Enum):
    NEEDS_HUMAN = "needs-human"
    HEAD_UNKNOWN = "head-unknown"
    CI_UNKNOWN = "ci-unknown"
    CI_PENDING = "ci-pending"
    MERGEABILITY_UNKNOWN = "mergeability-unknown"  # or reported BLOCKED
    HUMAN_CHANGES_REQUESTED = "human-changes-requested"
    HEADROOM = "headroom"


class StopReason(str, Enum):
    WORKFLOW_PATH = "workflow-path"
    LIFETIME_CAP = "lifetime-cap"
    ATTEMPTS = "attempts"
    REVIEW_ROUNDS = "review-rounds"
    QUESTION = "question"


def stop_tag(reason: StopReason) -> str:
    """The hidden tag a needs-human comment carries; derive reads it back."""
    return f"<!-- needs-human:{reason.value} -->"


def _need_head(head: object) -> None:
    if not isinstance(head, str) or not head:
        raise ValueError("a PR-stage state requires the decided head")


@dataclass(frozen=True)
class Dispatch:
    act: AgentKind
    head: str = ""  # "" exactly for RESOLVE, which has no PR yet

    def __post_init__(self) -> None:
        if self.act not in DISPATCHABLE:
            raise ValueError("not a dispatchable act")
        if self.act is AgentKind.RESOLVE:
            if self.head != "":
                raise ValueError("RESOLVE has no head")
        else:
            _need_head(self.head)


@dataclass(frozen=True)
class Merge:
    head: str

    def __post_init__(self) -> None:
        _need_head(self.head)


@dataclass(frozen=True)
class Refresh:
    """BEHIND but clean: GitHub's update-branch, free, no agent (Q10)."""

    head: str

    def __post_init__(self) -> None:
        _need_head(self.head)


@dataclass(frozen=True)
class OpenPR:
    """A pushed harness branch has no PR yet: open it (free, no agent)."""

    branch: str

    def __post_init__(self) -> None:
        if not isinstance(self.branch, str) or harness_branch_issue(self.branch) is None:
            raise ValueError("OpenPR requires a harness branch")


@dataclass(frozen=True)
class Release:
    """A qualifying reply released the stop: remove `needs-human`. The item
    proceeds next tick, with its counts reset past the released stop comment."""


@dataclass(frozen=True)
class Wait:
    reason: WaitReason


@dataclass(frozen=True)
class NeedsHuman:
    """A judgment stop. `anchor` (the item's last leg end, or "-") makes the
    comment's idempotency key unique per episode and stable within one.
    `question` is the agent's question, carried exactly by a QUESTION stop."""

    reason: StopReason
    anchor: str
    act: AgentKind | None = None
    question: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.reason, StopReason) or not isinstance(self.anchor, str) or not self.anchor:
            raise ValueError("needs-human requires a reason and an anchor")
        if (self.act is not None) != (self.reason is StopReason.ATTEMPTS) or (
            self.act is not None and self.act not in DISPATCHABLE
        ):
            raise ValueError("an act is named exactly for an attempts stop")
        if not isinstance(self.question, str) or bool(self.question) != (self.reason is StopReason.QUESTION):
            raise ValueError("a question is carried exactly by a question stop")


@dataclass(frozen=True)
class Busy:
    pass


@dataclass(frozen=True)
class Backoff:
    act: AgentKind
    until: datetime

    def __post_init__(self) -> None:
        if self.act not in DISPATCHABLE or not isinstance(self.until, datetime) or self.until.utcoffset() is None:
            raise ValueError("backoff requires a dispatchable act and an aware time")


DerivedState = Dispatch | Merge | Refresh | OpenPR | Release | Wait | NeedsHuman | Busy | Backoff


@dataclass(frozen=True)
class _Item:
    key: ItemKey
    issue: int
    labels: frozenset[Label]
    comments: tuple[Comment, ...]
    pr: PRView | None = None
    branches: tuple[Branch, ...] = ()


def derive(world: WorldState, local: Local, harness: Harness) -> Mapping[ItemKey, DerivedState]:
    """Every harness item in `world` mapped to its derived state.

    Items: each open harness PR (head branch `fix/<issue>-`, Q1), and each open
    `ai-ready` issue with no open or merged harness PR. Anything else is not
    the harness's.
    """
    prs = [(p, n) for p in world.prs if p.state == "OPEN" and (n := harness_branch_issue(p.head_ref_name))]
    # An open PR shadows its issue; a merged one means the issue is done, even
    # while it stays open on a non-default base.
    shadowed = {n for _, n in prs} | {
        n for p in world.prs if p.state == "MERGED" and (n := harness_branch_issue(p.head_ref_name))
    }
    pushed: dict[int, list[Branch]] = {}
    for b in world.branches:
        if (n := harness_branch_issue(b.name)) is not None:
            pushed.setdefault(n, []).append(b)
    items = [
        _Item(issue_key(i.number), i.number, i.labels, i.comments, branches=tuple(pushed.get(i.number, ())))
        for i in world.issues
        if i.state == "open" and i.has(Label.AI_READY) and i.number > 0 and i.number not in shadowed
    ] + [_Item(pr_key(p.number), n, p.labels, p.comments, p) for p, n in prs if p.number > 0]
    observed = parse_timestamp(world.now)
    return {item.key: _decide(item, local, harness, observed) for item in items}


def touches_github(paths: Iterable[str]) -> bool:
    """Any changed path under `.github/` (the existing review rule's scope)."""
    return any(isinstance(p, str) and p.removeprefix("./").startswith(".github/") for p in paths)


def _decide(item: _Item, local: Local, harness: Harness, observed: datetime | None) -> DerivedState:
    if item.key in local.leases or issue_key(item.issue) in local.leases:
        return Busy()
    legs = local.legs.get(item.key, ())
    last_end = max((leg.ended_at for leg in legs), default=None)
    # The world is read before the local facts: a leg that ended after the read
    # began may have pushed what this world does not show yet.
    if observed is not None and last_end is not None and last_end > observed:
        return Busy()
    if item.pr is not None and not item.pr.content_known:
        return Wait(WaitReason.HEAD_UNKNOWN)
    anchor = last_end.isoformat() if last_end else "-"
    comments = [(at, c) for c in item.comments if (at := parse_timestamp(c.created_at))]
    stops = [(at, m.group(1)) for at, c in comments if harness.wrote(c) and (m := _STOP_TAG_RE.search(c.body))]
    # Older stops were released; they still matter below (reset floor, workflow).
    last_stop = max((s for s in stops if last_end is None or s[0] > last_end), default=None)
    labelled = Label.NEEDS_HUMAN in item.labels
    if labelled and last_stop is not None:
        if item.issue in local.lifetime_capped and last_stop[1] == StopReason.LIFETIME_CAP.value:
            return Wait(WaitReason.NEEDS_HUMAN)
        if any(at > last_stop[0] and _human_comment(c, harness) for at, c in comments):
            return Release()
        return Wait(WaitReason.NEEDS_HUMAN)
    state = _proceed(item, local, harness, legs, comments, stops, anchor)
    if labelled and not isinstance(state, NeedsHuman):
        return Wait(WaitReason.NEEDS_HUMAN)
    return state


def _proceed(
    item: _Item, local: Local, harness: Harness, legs: tuple[LegEnd, ...],
    comments: list[tuple[datetime, Comment]], stops: list[tuple[datetime, str]], anchor: str,
) -> DerivedState:
    last_stop = max((at for at, _ in stops), default=None)
    agent_legs = [leg for leg in legs if not leg.machine]
    last_agent = max(agent_legs, key=lambda leg: leg.ended_at, default=None)
    if (last_agent is not None and last_agent.question is not None
            and not any(at > last_agent.ended_at for at, _ in stops)):
        return NeedsHuman(StopReason.QUESTION, anchor, question=last_agent.question)

    pr = item.pr
    released_workflow = any(code == StopReason.WORKFLOW_PATH.value for _, code in stops)
    if pr is not None and touches_github(pr.changed_files) and not released_workflow:
        return NeedsHuman(StopReason.WORKFLOW_PATH, anchor)
    if pr is None and item.branches:
        return OpenPR(max(item.branches, key=lambda b: (parse_timestamp(b.committed_at) is not None,
                                                        b.committed_at, b.name)).name)

    step = _pr_step(pr, harness) if pr is not None else Dispatch(AgentKind.RESOLVE)
    if not isinstance(step, Dispatch):
        return step
    floor = _reset_floor(item, comments, last_stop, harness, legs)
    if (step.act is AgentKind.ADDRESS_REVIEW and pr is not None
            and _bot_verdict_on_head(pr, harness) == REVIEW_CHANGES_REQUESTED
            and _rounds(pr, floor, harness) >= REVIEW_ROUND_CAP):
        return NeedsHuman(StopReason.REVIEW_ROUNDS, anchor)
    if _attempts(legs, step, floor) >= ATTEMPT_CAP:
        return NeedsHuman(StopReason.ATTEMPTS, anchor, step.act)
    if item.issue in local.lifetime_capped:
        return NeedsHuman(StopReason.LIFETIME_CAP, anchor)
    until = machine_backoff_until(legs, step.act)
    if until is not None and until > local.now:
        return Backoff(step.act, until)
    if not local.headroom:
        return Wait(WaitReason.HEADROOM)
    return step


def _pr_step(pr: PRView, harness: Harness) -> DerivedState:
    head = pr.head_ref_oid
    if not head:
        return Wait(WaitReason.HEAD_UNKNOWN)
    # Before CI: GitHub runs no pull_request checks on a conflicted PR.
    if pr.is_conflicting():
        return Dispatch(AgentKind.REBASE, head)
    if not isinstance(pr.ci, Observed) or pr.ci.head != head:
        return Wait(WaitReason.CI_UNKNOWN)
    ci = reduce_ci(pr.ci)
    if isinstance(ci, Failed):
        return Dispatch(AgentKind.FIX_CI, head)
    if isinstance(ci, Waiting):
        return Wait(WaitReason.CI_PENDING)
    if not isinstance(ci, Clear):
        return Wait(WaitReason.CI_UNKNOWN)
    # Before review: updating moves the head, which would void a review on it.
    if pr.is_behind():
        return Refresh(head)
    human_heads = _human_change_requests(pr, harness)
    if head in human_heads:
        return Dispatch(AgentKind.ADDRESS_REVIEW, head)
    verdict = _bot_verdict_on_head(pr, harness)
    if verdict is None:
        return Dispatch(AgentKind.REVIEW, head)
    if verdict == REVIEW_CHANGES_REQUESTED:
        return Dispatch(AgentKind.ADDRESS_REVIEW, head)
    if human_heads:
        return Wait(WaitReason.HUMAN_CHANGES_REQUESTED)
    # BLOCKED with green CI and the bot's approval: branch protection waits on
    # a human (approval, CODEOWNERS). GitHub clears it; nothing to write.
    if pr.mergeable.upper() != "MERGEABLE" or pr.merge_state.upper() == "BLOCKED":
        return Wait(WaitReason.MERGEABILITY_UNKNOWN)
    return Merge(head)


def _bot_verdict_on_head(pr: PRView, harness: Harness) -> str | None:
    verdicts = [
        (parse_timestamp(r.submitted_at) is not None, r.submitted_at, i, r.state.upper())
        for i, r in enumerate(pr.reviews)
        if login_eq(r.author, harness.review_bot) and r.commit_id == pr.head_ref_oid
        and r.state.upper() in (REVIEW_APPROVED, REVIEW_CHANGES_REQUESTED)
    ]
    return max(verdicts)[3] if verdicts else None


def _human_change_requests(pr: PRView, harness: Harness) -> frozenset[str]:
    """Heads carrying a non-harness reviewer's standing CHANGES_REQUESTED: that
    reviewer's latest APPROVED/CHANGES_REQUESTED/DISMISSED review is a change
    request. A later approval or dismissal by the same reviewer clears it."""
    latest: dict[str, tuple[bool, str, int, str, str]] = {}
    for i, r in enumerate(pr.reviews):
        state = r.state.upper()
        if harness.owns_login(r.author) or not r.author.strip() or state not in _STANDING:
            continue
        rank = (parse_timestamp(r.submitted_at) is not None, r.submitted_at, i, state, r.commit_id)
        who = r.author.strip().casefold()
        latest[who] = max(latest.get(who, rank), rank)
    return frozenset(commit for _, _, _, state, commit in latest.values() if state == REVIEW_CHANGES_REQUESTED)


def _attempts(legs: tuple[LegEnd, ...], step: Dispatch, floor: datetime | None) -> int:
    run = 0
    for leg in sorted(legs, key=lambda leg: leg.ended_at, reverse=True):
        if floor is not None and leg.ended_at <= floor:
            break
        if leg.machine:
            continue
        if leg.kind is not step.act or (step.act is AgentKind.REVIEW and leg.head != step.head):
            break
        run += 1
    return run


def _human_comment(comment: Comment, harness: Harness) -> bool:
    return not harness.wrote(comment) and not comment.is_bot


def _reset_floor(
    item: _Item, comments: list[tuple[datetime, Comment]], last_stop: datetime | None, harness: Harness,
    legs: tuple[LegEnd, ...],
) -> datetime | None:
    """The latest reset event (Q3): a non-harness comment, review or push. A
    stop comment on an unlabelled item was released (reply or label removal),
    so it is a reset too. A comment inside one of the item's leg windows is the
    agent's own (it comments under the bot's login without the marker)."""
    windows = [(leg.started_at, leg.ended_at) for leg in legs if leg.started_at is not None]
    events = [at for at, c in comments if _human_comment(c, harness)
              and not any(start < at <= end for start, end in windows)]
    if last_stop is not None:
        events.append(last_stop)
    if item.pr is not None:
        events += [at for r in item.pr.reviews
                   if not harness.owns_login(r.author) and r.state.upper() not in ("", "PENDING")
                   and (at := parse_timestamp(r.submitted_at))]
        events += [at for p in item.pr.pushes
                   if p.actor.strip() and not harness.owns_login(p.actor) and (at := parse_timestamp(p.pushed_at))]
    return max(events, default=None)


def _rounds(pr: PRView, floor: datetime | None, harness: Harness) -> int:
    def counts(raw: str) -> bool:
        at = parse_timestamp(raw)
        return floor is None or at is None or at > floor
    return sum(1 for r in pr.reviews if login_eq(r.author, harness.review_bot)
               and r.state.upper() == REVIEW_CHANGES_REQUESTED and counts(r.submitted_at))


__all__ = [
    "ATTEMPT_CAP", "REVIEW_ROUND_CAP", "DISPATCHABLE", "ItemKey", "issue_key", "pr_key", "Harness", "Local",
    "WaitReason", "StopReason", "stop_tag", "Dispatch", "Merge", "Refresh", "OpenPR", "Release", "Wait", "NeedsHuman",
    "Busy", "Backoff", "DerivedState", "derive", "touches_github",
]
