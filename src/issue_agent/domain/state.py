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
               (Q8) newer than both the stop and the label's latest
               application -> Release; else Wait. Labelled without one (the comment
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
6a. Stewardship an issue with no agent-ended STEWARD leg (a legacy combined
               TRIAGE result counts as one) -> STEWARD (gated as in step 8).
               Stewarded: a laundered `duplicate_of` with no released
               duplicate stop -> NeedsHuman(DUPLICATE). Then a laundered
               `blocked_by` and no harness blocker comment yet -> LinkBlocker
               (free; the comment, written after the link or its refusal,
               ends it, so a link a human deletes stays deleted). Then no
               agent-ended TRIAGE leg -> TRIAGE (gated likewise); then no harness triage
               comment yet -> TriageNote (free; tier, rationale and criteria
               in the one comment); else step 7.
7. Item state  PR: head unknown -> Wait; conflicted -> REBASE; CI unread or
               for another head, or GitHub still shows the head the newest
               agent leg pushed away from (its end head) -> Wait; red ->
               FIX_CI with just the failures no repair on the head was handed
               (tauri #1078), unless every red check is red or rerunning on
               the base tip too -> Wait (#313); none new -> Wait while a
               failing run still runs, else Rerun (free) if no run on the head
               is past its first attempt, else NeedsHuman (ATTEMPTS);
               pending -> Wait; green and BEHIND -> Refresh; a human's standing CHANGES_REQUESTED on
               head -> ADDRESS_REVIEW; no bot verdict on head -> REVIEW; bot
               CHANGES_REQUESTED on head -> ADDRESS_REVIEW; bot APPROVED head ->
               Merge, unless a human's standing CHANGES_REQUESTED is on an
               older head (Wait) or GitHub does not report it mergeable and
               unblocked (Wait: branch protection waits on a human).
               Issue: `ai-ready`, open, no open or merged harness PR,
               triaged -> RESOLVE.
8. Dispatch gates, for a Dispatch from step 7 only (merge/refresh/open are
               free): an issue act (no PR yet) whose issue GitHub marks
               "blocked by" an open issue, in any repository -> Wait (BLOCKED;
               the observation that shows every blocker closed lets it start;
               an item with a PR is never gated); bot review rounds >= cap
               (ADDRESS_REVIEW on a bot verdict) -> NeedsHuman; attempts >= cap -> NeedsHuman; the
               issue's lifetime cap cannot afford a leg -> NeedsHuman; machine
               backoff -> Backoff; daily or global headroom short -> Wait
               (silent, Q7).

Routing: a routed act (RESOLVE, FIX_CI, ADDRESS_REVIEW, REBASE) runs at the
issue's tier from its latest agent-ended TRIAGE leg (DEFAULT_TIER without
one, e.g. a pre-cutover issue or a malformed result), escalated one rung per
earlier attempt in the act's attempt run (`routing.escalate`). REVIEW runs
at the tier of the fix leg that pushed its head (`_review_tier`), else the
issue's triaged tier, else None: the configured REVIEW_MODEL.

Attempts for act A (Q21) are the tail run of A's agent legs after the reset
floor: machine legs are skipped, any other act's agent leg ends the run, and
REVIEW counts only legs on the current head. Progress (an act changing, or a
review of a new head) therefore never trips the cap; repeating one act
without progress does. FIX_CI counts tries, not legs: a leg on a new head,
handed a failure from a CI run that began after the head's first leg began
and no earlier leg was handed (a push, re-run or dispatch it or anyone
caused), or with no readable keys, starts a try; a leg within the step's own try runs at its tier and routes to
the runtime holding the session.

Untrusted timestamps fail toward stopping: an unparseable one never resets a
count or releases a stop, and an unparseable bot review still counts as a round.
Likewise a push GitHub cannot attribute to a login (an unlinked commit email)
is never a reset: only a push by a known non-harness login is.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from itertools import takewhile
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType

from .actions import ISSUE_ACTS, ROUTED_ACTS, AgentKind, Evidence, Target, check_evidence, check_tier
from .backoff import LegEnd, machine_backoff_until
from .checks import Check, Observed
from .ci import BaseRed, Clear, Failed, Waiting, charge, is_pending, parse_timestamp, reduce_ci
from .evidence import ChangeRequest, CiFailures, began, new_failures
from .identity import login_eq
from .labels import Label
from .refs import harness_branch_issue
from .routing import DEFAULT_TIER, Tier, escalate
from .triage import Steward, Triage
from .world import REVIEW_APPROVED, REVIEW_CHANGES_REQUESTED, BlockerRef, Branch, Comment, PRView, WorldState

ATTEMPT_CAP = 3
REVIEW_ROUND_CAP = 3
DISPATCHABLE = frozenset({
    AgentKind.STEWARD, AgentKind.TRIAGE, AgentKind.RESOLVE, AgentKind.FIX_CI, AgentKind.REBASE, AgentKind.REVIEW,
    AgentKind.ADDRESS_REVIEW,
})
_MAX_MARKER = 200
_STANDING = frozenset({REVIEW_APPROVED, REVIEW_CHANGES_REQUESTED, "DISMISSED"})
_STOP_TAG_RE = re.compile(r"<!-- needs-human:([a-z-]+) -->")
# Carried by the harness's one triage comment per issue; derive reads it back.
TRIAGE_TAG = "<!-- issue-agent:triage -->"
# Carried by the harness's one comment about a steward's "blocked by" claim.
BLOCKER_TAG = "<!-- issue-agent:blocked-by -->"


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
    BASE_RED = "base-red"  # the PR's red checks are red on the base tip too (#313)
    MERGEABILITY_UNKNOWN = "mergeability-unknown"  # or reported BLOCKED
    HUMAN_CHANGES_REQUESTED = "human-changes-requested"
    HEADROOM = "headroom"
    BLOCKED = "blocked"  # GitHub's "blocked by" names an open issue


class StopReason(str, Enum):
    WORKFLOW_PATH = "workflow-path"
    LIFETIME_CAP = "lifetime-cap"
    ATTEMPTS = "attempts"
    REVIEW_ROUNDS = "review-rounds"
    QUESTION = "question"
    # Triage flagged a suspected duplicate: a judgment, so a human decides.
    DUPLICATE = "duplicate"


def stop_tag(reason: StopReason) -> str:
    """The hidden tag a needs-human comment carries; derive reads it back."""
    return f"<!-- needs-human:{reason.value} -->"


def _need_head(head: object) -> None:
    if not isinstance(head, str) or not head:
        raise ValueError("a PR-stage state requires the decided head")


@dataclass(frozen=True)
class Dispatch:
    act: AgentKind
    head: str = ""  # "" exactly for the issue acts (STEWARD, TRIAGE, RESOLVE): no PR yet
    # The leg's tier: see `actions.check_tier`.
    tier: Tier | None = None
    # What a repair is handed: see `actions.check_evidence`.
    evidence: Evidence | None = None

    def __post_init__(self) -> None:
        if self.act not in DISPATCHABLE:
            raise ValueError("not a dispatchable act")
        if self.act in ISSUE_ACTS:
            if self.head != "":
                raise ValueError("an issue act has no head")
        else:
            _need_head(self.head)
        object.__setattr__(self, "tier", check_tier(self.act, self.tier))
        check_evidence(self.act, self.evidence)


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
class Rerun:
    """Re-run the failed jobs of each (run id, attempt) on `head`, free: the
    harness, never the agent, re-runs a failure the leg left as a flake, once
    per head (owner, PR #403)."""

    head: str
    runs: tuple[tuple[int, int], ...]

    def __post_init__(self) -> None:
        _need_head(self.head)
        if not self.runs or any(type(r) is not int or type(a) is not int or r <= 0 or a <= 0 for r, a in self.runs):
            raise ValueError("a re-run names at least one run and its observed attempt")


@dataclass(frozen=True)
class OpenPR:
    """A pushed harness branch has no PR yet: open it (free, no agent)."""

    branch: str

    def __post_init__(self) -> None:
        if not isinstance(self.branch, str) or harness_branch_issue(self.branch) is None:
            raise ValueError("OpenPR requires a harness branch")


@dataclass(frozen=True)
class TriageNote:
    """A triaged issue has no harness triage comment yet: post one (free).
    `anchor` is the triage leg's end, keying the comment once per triage;
    `steward` carries the criteria (empty when STEWARD returned nothing)."""

    triage: Triage
    anchor: str
    steward: Steward = Steward()

    def __post_init__(self) -> None:
        if (not isinstance(self.triage, Triage) or not isinstance(self.steward, Steward)
                or not isinstance(self.anchor, str) or not self.anchor):
            raise ValueError("a triage note requires the triage, the steward result and an anchor")


@dataclass(frozen=True)
class LinkBlocker:
    """A steward named an open issue this one cannot be done before: link it
    on GitHub (free). `anchor` is the steward leg's end, keying the write."""

    blocker: int
    reason: str
    anchor: str

    def __post_init__(self) -> None:
        if (type(self.blocker) is not int or self.blocker <= 0 or not isinstance(self.reason, str) or not self.reason
                or not isinstance(self.anchor, str) or not self.anchor):
            raise ValueError("a blocker link requires the blocker, the reason and an anchor")


@dataclass(frozen=True)
class Release:
    """A qualifying reply released the stop: remove `needs-human`. The item
    proceeds next tick, with its counts reset past the released stop comment."""


@dataclass(frozen=True)
class Wait:
    """`blocked_by` names the open blockers, carried exactly by a BLOCKED wait."""

    reason: WaitReason
    blocked_by: tuple[BlockerRef, ...] = ()

    def __post_init__(self) -> None:
        if (not isinstance(self.blocked_by, tuple) or any(not isinstance(b, BlockerRef) for b in self.blocked_by)
                or bool(self.blocked_by) != (self.reason is WaitReason.BLOCKED)):
            raise ValueError("blockers are carried exactly by a blocked wait")


@dataclass(frozen=True)
class NeedsHuman:
    """A judgment stop. `anchor` (the item's last leg end, or "-") makes the
    comment's idempotency key unique per episode and stable within one.
    `question` is the agent's question, carried exactly by a QUESTION stop;
    `duplicate_of` (with triage's one-line `detail`) exactly by a DUPLICATE stop."""

    reason: StopReason
    anchor: str
    act: AgentKind | None = None
    question: str = ""
    duplicate_of: int = 0
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.reason, StopReason) or not isinstance(self.anchor, str) or not self.anchor:
            raise ValueError("needs-human requires a reason and an anchor")
        if (self.act is not None) != (self.reason is StopReason.ATTEMPTS) or (
            self.act is not None and self.act not in DISPATCHABLE
        ):
            raise ValueError("an act is named exactly for an attempts stop")
        if not isinstance(self.question, str) or bool(self.question) != (self.reason is StopReason.QUESTION):
            raise ValueError("a question is carried exactly by a question stop")
        if (type(self.duplicate_of) is not int or self.duplicate_of < 0
                or (self.duplicate_of > 0) != (self.reason is StopReason.DUPLICATE)
                or not isinstance(self.detail, str) or (self.detail and self.reason is not StopReason.DUPLICATE)):
            raise ValueError("a duplicate target is carried exactly by a duplicate stop")


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


DerivedState = (Dispatch | Merge | Refresh | Rerun | OpenPR | TriageNote | LinkBlocker | Release | Wait | NeedsHuman
                | Busy | Backoff)


@dataclass(frozen=True)
class _Item:
    key: ItemKey
    issue: int
    labels: frozenset[Label]
    comments: tuple[Comment, ...]
    pr: PRView | None = None
    branches: tuple[Branch, ...] = ()
    labeled_at: str = ""
    base: tuple[Check, ...] | None = None  # the PR base's tip reports; None = unread
    blocked_by: tuple[BlockerRef, ...] = ()


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
        _Item(issue_key(i.number), i.number, i.labels, i.comments, branches=tuple(pushed.get(i.number, ())),
              labeled_at=i.needs_human_labeled_at, blocked_by=i.blocked_by)
        for i in world.issues
        if i.state == "open" and i.has(Label.AI_READY) and i.number > 0 and i.number not in shadowed
    ] + [_Item(pr_key(p.number), n, p.labels, p.comments, p, labeled_at=p.needs_human_labeled_at,
               base=world.base.reports if world.base and world.base.ref == p.base_ref_name else None)
         for p, n in prs if p.number > 0]
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
    human = human_comments(item.comments, harness, legs)
    # Older stops were released; they still matter below (reset floor, workflow).
    last_stop = max((s for s in stops if last_end is None or s[0] > last_end), default=None)
    labelled = Label.NEEDS_HUMAN in item.labels
    if labelled and last_stop is not None:
        if item.issue in local.lifetime_capped and last_stop[1] == StopReason.LIFETIME_CAP.value:
            return Wait(WaitReason.NEEDS_HUMAN)
        # A reply releases only if it is newer than the label too: an operator
        # re-park after a reply must not be undone (5d-1 review).
        parked = parse_timestamp(item.labeled_at) if item.labeled_at else last_stop[0]
        if parked is None:
            return Wait(WaitReason.NEEDS_HUMAN)
        if any(at > max(last_stop[0], parked) for at, _ in human):
            return Release()
        return Wait(WaitReason.NEEDS_HUMAN)
    state = _proceed(item, local, harness, legs, human, stops, anchor)
    if labelled and not isinstance(state, NeedsHuman):
        return Wait(WaitReason.NEEDS_HUMAN)
    return state


def _proceed(
    item: _Item, local: Local, harness: Harness, legs: tuple[LegEnd, ...],
    human: tuple[tuple[datetime, Comment], ...], stops: list[tuple[datetime, str]], anchor: str,
) -> DerivedState:
    last_stop = max((at for at, _ in stops), default=None)
    # Newest agent end; a tie goes to the later-recorded leg, as in _attempts.
    last_agent = next((leg for leg in reversed(sorted(legs, key=lambda leg: leg.ended_at)) if not leg.machine), None)
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

    issue_legs = local.legs.get(issue_key(item.issue), ())
    triage = _latest_triage(issue_legs)
    floor = _reset_floor(item, [at for at, _ in human], last_stop, harness)
    if pr is None:
        # STEWARD first: a duplicate stops before any grading is bought.
        steward = latest_steward(issue_legs)
        if steward is None:
            step = Dispatch(AgentKind.STEWARD)
        elif (stop := _duplicate_stop(steward, stops, anchor)) is not None:
            return stop
        elif (link := _blocker_link(steward, item.issue, item.comments, harness)) is not None:
            return link
        elif triage is None:
            step = Dispatch(AgentKind.TRIAGE)
        else:
            criteria = steward.steward or Steward()
            # An unusable grade still posts the steward's criteria, at the tier it runs at.
            graded = triage.triage or (Triage(DEFAULT_TIER) if criteria.acceptance_criteria else None)
            if graded is not None and not any(harness.wrote(c) and TRIAGE_TAG in c.body for c in item.comments):
                return TriageNote(graded, triage.ended_at.isoformat(), criteria)
            step = Dispatch(AgentKind.RESOLVE)
    elif (last_agent is not None and last_agent.end_head not in ("", last_agent.head)
          and pr.head_ref_oid == last_agent.head and (floor is None or floor < last_agent.ended_at)):
        # Read-your-writes, not a timer (owner, PR #403): the newest leg pushed and
        # GitHub does not show it yet. A human reset since then ends the wait.
        return Wait(WaitReason.CI_UNKNOWN)
    else:
        step = _pr_step(pr, harness, item.base, _handed(legs, pr.head_ref_oid, floor), anchor)
    if not isinstance(step, Dispatch):
        return step
    # Before the gates: nothing is spent, and a stop would ask a human to act on an issue that is only waiting.
    if step.act in ISSUE_ACTS and item.blocked_by:
        return Wait(WaitReason.BLOCKED, item.blocked_by)
    if (step.act is AgentKind.ADDRESS_REVIEW and pr is not None
            and _bot_verdict_on_head(pr, harness) == REVIEW_CHANGES_REQUESTED
            and _rounds(pr, floor, harness) >= REVIEW_ROUND_CAP):
        return NeedsHuman(StopReason.REVIEW_ROUNDS, anchor)
    attempts = _attempts(legs, step, floor)
    if attempts >= ATTEMPT_CAP:
        return NeedsHuman(StopReason.ATTEMPTS, anchor, step.act)
    if item.issue in local.lifetime_capped:
        return NeedsHuman(StopReason.LIFETIME_CAP, anchor)
    until = machine_backoff_until(legs, step.act)
    if until is not None and until > local.now:
        return Backoff(step.act, until)
    if not local.headroom:
        return Wait(WaitReason.HEADROOM)
    triaged = triage.triage.tier if triage is not None and triage.triage is not None else None
    if step.act in ROUTED_ACTS:
        return Dispatch(step.act, step.head, escalate(triaged or DEFAULT_TIER, attempts), step.evidence)
    if step.act is AgentKind.REVIEW and pr is not None:
        return Dispatch(step.act, step.head, _review_tier(pr, harness, legs, local, item.issue) or triaged)
    return step


def _review_tier(pr: PRView, harness: Harness, legs: tuple[LegEnd, ...], local: Local, issue: int) -> Tier | None:
    """The tier of the fix leg that pushed the head under review (owner,
    2026-10-13: the reviewer runs at the fixer's tier, escalation included):
    the issue's newest agent-ended routed leg. None when that leg cannot be
    identified: a head a human pushed, or a newest leg that predates routing."""
    if any(p.actor.strip() and not harness.owns_login(p.actor) for p in pr.pushes):
        return None
    fixes = [leg for leg in (*legs, *local.legs.get(issue_key(issue), ()))
             if leg.kind in ROUTED_ACTS and not leg.machine]
    producer = max(fixes, key=lambda leg: leg.ended_at, default=None)
    return producer.tier if producer is not None else None


def _latest_triage(legs: tuple[LegEnd, ...]) -> LegEnd | None:
    """The issue's newest agent-ended TRIAGE leg. A machine end is no triage
    (it retries under the machine backoff); an agent end whose output did not
    parse still is one (triage ran; the issue proceeds at DEFAULT_TIER)."""
    return next((leg for leg in reversed(sorted(legs, key=lambda leg: leg.ended_at))
                 if leg.kind is AgentKind.TRIAGE and not leg.machine), None)


def latest_steward(legs: tuple[LegEnd, ...]) -> LegEnd | None:
    """The issue's newest agent-ended leg that did the steward's work: a
    STEWARD leg, or a legacy combined TRIAGE leg whose result carries the
    steward's fields. An agent end whose output did not parse still counts
    (it ran; the issue proceeds with no criteria and no duplicate stop)."""
    return next((leg for leg in reversed(sorted(legs, key=lambda leg: leg.ended_at))
                 if not leg.machine and (leg.kind is AgentKind.STEWARD or leg.steward is not None)), None)


def _duplicate_stop(leg: LegEnd, stops: list[tuple[datetime, str]], anchor: str) -> NeedsHuman | None:
    """The duplicate stop a steward result owes, until a human released one."""
    steward = leg.steward
    if steward is None or steward.duplicate_of is None or any(code == StopReason.DUPLICATE.value for _, code in stops):
        return None
    return NeedsHuman(StopReason.DUPLICATE, anchor, duplicate_of=steward.duplicate_of, detail=steward.duplicate_reason)


def _blocker_link(leg: LegEnd, issue: int, comments: tuple[Comment, ...], harness: Harness) -> LinkBlocker | None:
    """The link a steward's dependency claim owes, until the harness has said
    anything about one on this issue: at most one link per issue, ever. A
    claim on the issue itself is never one (the record read drops it too)."""
    steward = leg.steward
    if steward is None or steward.blocked_by in (None, issue) or any(
            harness.wrote(c) and BLOCKER_TAG in c.body for c in comments):
        return None
    return LinkBlocker(steward.blocked_by, steward.blocked_by_reason, leg.ended_at.isoformat())


def _handed(legs: tuple[LegEnd, ...], head: str, floor: datetime | None) -> frozenset[str]:
    """The failure keys FIX_CI agent ends on `head` were handed since the reset
    floor. A machine end handed nothing (it never ran, or died); a human reset
    (a comment, review or released stop) re-arms every failure."""
    return frozenset(key for leg in legs if leg.kind is AgentKind.FIX_CI and leg.head == head
                     and (floor is None or leg.ended_at > floor) for key in leg.evidence)


def _pr_step(
    pr: PRView, harness: Harness, base: tuple[Check, ...] | None, handed: frozenset[str], anchor: str,
) -> DerivedState:
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
        ci = charge(ci, base)
    if isinstance(ci, Failed):
        fresh = new_failures(pr.ci, handed)
        if fresh is not None:
            return Dispatch(AgentKind.FIX_CI, head, evidence=fresh)
        runs = {(c.run_id, c.run_attempt) for c in pr.ci.reports
                if c.bucket == "fail" and c.run_id is not None and c.run_attempt is not None}
        if any(is_pending(c) and c.run_id in {r for r, _ in runs} for c in pr.ci.reports):
            # A failing run may still hand the session a new failure, and GitHub
            # will not re-run a run in progress. Another run's check never holds this.
            return Wait(WaitReason.CI_PENDING)
        # A repair had every failure and pushed nothing: the same evidence cannot help. A
        # flake gets one harness re-run per head; a run past attempt 1 was re-run already.
        if runs and all((c.run_attempt or 1) == 1 for c in pr.ci.reports):
            return Rerun(head, tuple(sorted(runs)))
        return NeedsHuman(StopReason.ATTEMPTS, anchor, AgentKind.FIX_CI)
    # The base is the repo's to fix (Q13: flakes are the repo's). The PR
    # waits uncharged and resumes when the base tip stops failing.
    if isinstance(ci, BaseRed):
        return Wait(WaitReason.BASE_RED)
    if isinstance(ci, Waiting):
        return Wait(WaitReason.CI_PENDING)
    if not isinstance(ci, Clear):
        return Wait(WaitReason.CI_UNKNOWN)
    # Before review: updating moves the head, which would void a review on it.
    if pr.is_behind():
        return Refresh(head)
    standing = _human_change_requests(pr, harness)
    human_heads = frozenset(standing.values())
    if head in human_heads:
        return Dispatch(AgentKind.ADDRESS_REVIEW, head,
                        evidence=_change_request(pr, lambda login: standing.get(login.strip().casefold()) == head))
    verdict = _bot_verdict_on_head(pr, harness)
    if verdict is None:
        return Dispatch(AgentKind.REVIEW, head)
    if verdict == REVIEW_CHANGES_REQUESTED:
        return Dispatch(AgentKind.ADDRESS_REVIEW, head,
                        evidence=_change_request(pr, lambda login: login_eq(login, harness.review_bot)))
    if human_heads:
        return Wait(WaitReason.HUMAN_CHANGES_REQUESTED)
    # BLOCKED with green CI and the bot's approval: branch protection waits on
    # a human (approval, CODEOWNERS). GitHub clears it; nothing to write.
    if pr.mergeable.upper() != "MERGEABLE" or pr.merge_state.upper() == "BLOCKED":
        return Wait(WaitReason.MERGEABILITY_UNKNOWN)
    return Merge(head)


def _change_request(pr: PRView, by: Callable[[str], bool]) -> ChangeRequest:
    """The newest CHANGES_REQUESTED review on the head by a reviewer `by`
    accepts: both callers saw one (the bot's verdict, or a human's standing
    request, on this head)."""
    review = max((r for r in pr.reviews if r.commit_id == pr.head_ref_oid and r.author.strip() and by(r.author)
                  and r.state.upper() == REVIEW_CHANGES_REQUESTED),
                 key=lambda r: (parse_timestamp(r.submitted_at) is not None, r.submitted_at))
    return ChangeRequest(review.author, pr.head_ref_oid, review.id, review.body)


def _bot_verdict_on_head(pr: PRView, harness: Harness) -> str | None:
    verdicts = [
        (parse_timestamp(r.submitted_at) is not None, r.submitted_at, i, r.state.upper())
        for i, r in enumerate(pr.reviews)
        if login_eq(r.author, harness.review_bot) and r.commit_id == pr.head_ref_oid
        and r.state.upper() in (REVIEW_APPROVED, REVIEW_CHANGES_REQUESTED)
    ]
    return max(verdicts)[3] if verdicts else None


def _human_change_requests(pr: PRView, harness: Harness) -> Mapping[str, str]:
    """Each non-harness reviewer (casefolded login) with a standing
    CHANGES_REQUESTED, and the head it is on: that reviewer's latest
    APPROVED/CHANGES_REQUESTED/DISMISSED review is a change request. A later
    approval or dismissal by the same reviewer clears it."""
    latest: dict[str, tuple[bool, str, int, str, str]] = {}
    for i, r in enumerate(pr.reviews):
        state = r.state.upper()
        if harness.owns_login(r.author) or not r.author.strip() or state not in _STANDING:
            continue
        rank = (parse_timestamp(r.submitted_at) is not None, r.submitted_at, i, state, r.commit_id)
        who = r.author.strip().casefold()
        latest[who] = max(latest.get(who, rank), rank)
    return {who: commit for who, (_, _, _, state, commit) in latest.items() if state == REVIEW_CHANGES_REQUESTED}


def _attempts(legs: tuple[LegEnd, ...], step: Dispatch, floor: datetime | None) -> int:
    """The attempts in the act's current run of agent legs since the floor.
    FIX_CI counts tries, the step's own not counted (owner, PR #403): a
    failure of a CI run that existed before the head's first leg, or that an
    earlier leg was handed, resumes at its tier; ATTEMPT_CAP bounds new runs."""
    run: list[LegEnd] = []
    for leg in reversed(sorted(legs, key=lambda leg: leg.ended_at)):  # ties: later-recorded is newer
        if floor is not None and leg.ended_at <= floor:
            break
        if leg.machine:
            continue
        if leg.kind is not step.act or (step.act is AgentKind.REVIEW and leg.head != step.head):
            break
        run.append(leg)
    if step.act is not AgentKind.FIX_CI:
        return len(run)

    def new_try(head: str, keys: Iterable[str], earlier: list[LegEnd]) -> bool:
        # A run already handed (a key of the same start) or begun before the head's first leg resumes; an
        # unreadable time or a keyless (legacy, corrupt) leg fails toward counting.
        same = list(takewhile(lambda leg: leg.head == head, reversed(earlier)))
        if not same or not keys:
            return True
        since = same[-1].started_at or same[-1].ended_at
        handed = {began(key) for leg in same for key in leg.evidence}
        return any((at := began(key)) is None or (at > since and at not in handed) for key in keys)

    oldest = run[::-1]
    keys = step.evidence.keys if isinstance(step.evidence, CiFailures) else ()
    return sum(new_try(leg.head, leg.evidence, oldest[:i]) for i, leg in enumerate(oldest)) - (
        not new_try(step.head, keys, oldest))


def human_comments(
    comments: Iterable[Comment], harness: Harness, legs: Iterable[LegEnd],
) -> tuple[tuple[datetime, Comment], ...]:
    """The comments a human wrote, with their times, oldest first: the one
    definition release, the reset floor and the fixer's reply text share.

    Not human: the harness's own (marker and login), a bot's, one whose
    timestamp does not parse (it fails toward not a reply), and one inside one
    of the item's leg windows (start, end]. The agent inherits the
    ambient GitHub credentials and comments without the marker, under FIX_BOT
    or the operator's login, which can be the same login (Q1), so its run
    window is the only signal (#342). A leg without a start never launched
    (the legs table stamps `started_at` at launch), so it posted nothing."""
    windows = [(leg.started_at, leg.ended_at) for leg in legs if leg.started_at is not None]
    picked = [(at, c) for c in comments
              if (at := parse_timestamp(c.created_at)) is not None and not harness.wrote(c) and not c.is_bot
              and not any(start < at <= end for start, end in windows)]
    return tuple(sorted(picked, key=lambda p: p[0]))


def _reset_floor(
    item: _Item, human: list[datetime], last_stop: datetime | None, harness: Harness,
) -> datetime | None:
    """The latest reset event (Q3): a human comment, or a non-harness review
    or push. A stop comment on an unlabelled item was released (reply or label
    removal), so it is a reset too."""
    events = list(human)
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
    "WaitReason", "StopReason", "stop_tag", "Dispatch", "Merge", "Refresh", "Rerun", "OpenPR", "TriageNote", "TRIAGE_TAG",
    "BLOCKER_TAG", "LinkBlocker",
    "Release", "Wait", "NeedsHuman",
    "Busy", "Backoff", "DerivedState", "derive", "human_comments", "latest_steward", "touches_github",
]
