"""The dashboard's read model, as pure functions. No I/O, no clock.

The dashboard is a separate, read-only consumer (docs/open/2026-10-12-minimal-
harness-design.md): it shows what the core derives and what the `legs` table
recorded, and keeps no state or lifecycle rules of its own.

- **Board.** Each row is one harness item, mapped from the state
  `domain.state.derive` gives it over the same observation the roles read.
  `needs-human` is "Needs you", worded by its stop reason; everything else is
  either working (an agent, CI, or a free action is moving it) or a wait on
  something outside the harness (branch protection, a human reviewer, money
  headroom, a machine backoff, an unreadable GitHub field).
- **Legs.** The issue page, the rail, the history and every metric read the
  `legs` rows (refactor decision Q15: metrics from legs plus GitHub).

Inputs arrive as plain values: the adapter parses SQLite rows into `LegRow`,
GitHub into `domain.world` views.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from enum import Enum

from .actions import AgentKind
from .checks import Observed
from .ci import Clear, Failed, Waiting as CiWaiting, parse_timestamp, reduce_ci
from .state import (
    Backoff, Busy, DerivedState, Dispatch, Harness, ItemKey, Merge, NeedsHuman, OpenPR, Refresh, Release, StopReason,
    Wait, WaitReason, stop_tag,
)
from .world import Comment, PRView

ROLE_NAMES = ("fix", "review")
# A role heartbeat older than this reads as stale (the CLI's status uses it too).
STALE_AFTER_S = 1800.0

MESSAGE_CHARS = 4000
EXCERPT_CHARS = 500
SPEND_WINDOW_HOURS = 48
NOTIFICATION_LIMIT = 100
_ESTIMATED_BASES = frozenset({"estimate", "learned_estimate", "synthetic"})


def bounded(text: object, limit: int) -> str:
    value = text.strip() if isinstance(text, str) else ""
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


# ── legs ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Tokens:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class LegRow:
    """One `legs` row as the dashboard reads it. Money is in USD; an absent
    charge means the leg has not settled (it is reserved, running or was
    released without spending)."""

    leg_id: str
    role: str
    kind: str
    issue: int | None
    number: int
    head: str
    runtime: str
    state: str  # reserved | running | ended | released
    reserved_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    end_reason: str = ""
    reserved_usd: float = 0.0
    charged_usd: float | None = None
    basis: str = ""
    origin: str = ""
    leg_class: str = ""  # agent | asked | machine | "" (never returned)
    session_id: str = ""
    tokens: Tokens | None = None
    summary: str = ""
    question: str = ""
    verdict: str = ""  # the review verdict event (APPROVE / REQUEST_CHANGES), if any
    verdict_body: str = ""

    @property
    def running(self) -> bool:
        return self.state in ("reserved", "running")

    @property
    def counted(self) -> bool:
        """A leg that ran or may still run. A released leg never launched
        (an unlaunched reservation, or one refused before admission)."""
        return self.state != "released"

    @property
    def carried(self) -> bool:
        """Copied from the legacy store at the cutover: money history only.
        Derive never counts it (the store's `leg_runs` reads only fix and
        review legs; attempts restart at the cutover), so neither do the
        rail, the history chips, the failure counts or the toasts."""
        return self.role not in ROLE_NAMES

    @property
    def derived(self) -> bool:
        """A leg derive counts: it ran or may still run, and is not carried."""
        return self.counted and not self.carried

    @property
    def cost_usd(self) -> float:
        """What the leg costs now: its charge once settled, its reserved
        maximum while it may still spend, nothing once released."""
        if self.charged_usd is not None:
            return self.charged_usd
        return self.reserved_usd if self.running else 0.0

    @property
    def estimated(self) -> bool:
        return self.running or self.basis in _ESTIMATED_BASES or self.origin == "recovery_estimate"

    @property
    def outcome(self) -> str:
        """running | passed | changes_requested | asked | failed | done."""
        if self.running:
            return "running"
        if self.verdict == "APPROVE":
            return "passed"
        if self.verdict == "REQUEST_CHANGES":
            return "changes_requested"
        if self.leg_class == "asked":
            return "asked"
        if self.leg_class == "agent":
            return "done"
        return "failed"

    @property
    def at(self) -> datetime:
        return self.ended_at or self.started_at or self.reserved_at


def issue_legs(legs: Iterable[LegRow], issue: int) -> tuple[LegRow, ...]:
    return tuple(sorted((leg for leg in legs if leg.issue == issue), key=lambda leg: (leg.reserved_at, leg.leg_id)))


# ── board ────────────────────────────────────────────────────────────────────


class Stage(str, Enum):
    RESOLVE = "resolve"
    CI = "ci"
    REVIEW = "review"
    MERGE = "merge"


STAGE_ORDER: tuple[Stage, ...] = tuple(Stage)
_STAGE_OF_KIND: Mapping[str, Stage] = {
    AgentKind.RESOLVE.name: Stage.RESOLVE,
    AgentKind.REBASE.name: Stage.CI,
    AgentKind.FIX_CI.name: Stage.CI,
    AgentKind.REVIEW.name: Stage.REVIEW,
    AgentKind.ADDRESS_REVIEW.name: Stage.REVIEW,
}
_ACT_WORDS: Mapping[str, str] = {
    "RESOLVE": "Resolve", "REBASE": "Rebase", "FIX_CI": "Fix CI", "REVIEW": "Review",
    "ADDRESS_REVIEW": "Address review",
}


def stage_of_kind(kind: str) -> Stage | None:
    return _STAGE_OF_KIND.get(kind)


@dataclass(frozen=True)
class NeedsYou:
    """`needs-human`: nothing moves until a human acts. `reason` is the stop's
    reason, or "labelled" when the label carries no stop comment of this
    episode (an operator park, or a human who added it). `target` is where a
    reply releases it."""

    reason: str
    headline: str
    detail: str | None
    target: str  # issue | pr
    since_ts: str | None
    hint: str = ""  # what releases it, in one line
    status: str = "needs_you"


@dataclass(frozen=True)
class Working:
    """Moving without a human: an agent leg is running or next, CI is
    running, or a free action (merge, branch update, PR open, release) runs
    next tick."""

    activity: str  # agent | queued | ci | merge | update | open_pr | release
    act: str | None
    headline: str
    reason: str
    since_ts: str | None
    status: str = "working"


@dataclass(frozen=True)
class Waiting:
    """A wait on something outside the harness."""

    on: str  # branch-protection | human-review | headroom | backoff | github
    headline: str
    reason: str
    since_ts: str | None
    until_ts: str | None = None
    status: str = "waiting"


BoardStatus = NeedsYou | Working | Waiting

_STOP_HEADLINE: Mapping[str, str] = {
    StopReason.QUESTION.value: "Answer the agent's question",
    StopReason.ATTEMPTS.value: "Attempts exhausted",
    StopReason.REVIEW_ROUNDS.value: "Review dispute",
    StopReason.LIFETIME_CAP.value: "Lifetime budget spent",
    StopReason.WORKFLOW_PATH.value: "Workflow change needs a human",
    "labelled": "Parked",
}
_STOP_REASON: Mapping[str, str] = {
    StopReason.QUESTION.value: "Reply on GitHub with the answer; the agent resumes with it.",
    StopReason.ATTEMPTS.value: "The same act ran without progress; reply or unblock to let it try again.",
    StopReason.REVIEW_ROUNDS.value: "The reviewer kept requesting changes; reply or unblock to continue.",
    StopReason.LIFETIME_CAP.value: "Raise the per-issue budget, then reply or unblock.",
    StopReason.WORKFLOW_PATH.value: "The PR changes files under .github/; reply or unblock once judged.",
    "labelled": "Labelled needs-human with no stop comment; unblock to hand it back.",
}


@dataclass(frozen=True)
class Stop:
    """The newest harness stop comment on an item."""

    reason: str
    at: datetime
    message: str


def latest_stop(comments: Iterable[Comment], harness: Harness) -> Stop | None:
    """The newest stop comment the harness wrote (marker and login, as derive
    reads it), with its tags stripped for display."""
    tags = {stop_tag(reason): reason.value for reason in StopReason}
    best: Stop | None = None
    for comment in comments:
        at = parse_timestamp(comment.created_at)
        if at is None or not harness.wrote(comment):
            continue
        reason = next((code for tag, code in tags.items() if tag in comment.body), None)
        if reason is None or (best is not None and at <= best.at):
            continue
        text = comment.body.replace(harness.marker, "")
        for tag in tags:
            text = text.replace(tag, "")
        best = Stop(reason, at, bounded(text, MESSAGE_CHARS))
    return best


def _iso(stamp: datetime | None) -> str | None:
    return stamp.astimezone(timezone.utc).isoformat() if stamp is not None else None


def _needs_you(reason: str, target: str, since: datetime | None, detail: str | None) -> NeedsYou:
    return NeedsYou(reason, _STOP_HEADLINE.get(reason, "Needs a human"), detail or None, target, _iso(since),
                    _STOP_REASON.get(reason, "Reply on GitHub or unblock to hand it back."))


def board_status(
    state: DerivedState,
    *,
    target: str,
    comments: Iterable[Comment],
    pr: PRView | None,
    legs: tuple[LegRow, ...],
    last_end: datetime | None,
    harness: Harness,
) -> BoardStatus:
    """One item's board status from its derived state. `legs` are the item's
    issue's legs, oldest first; `comments` are the item's own (where its stop
    comment is posted); `last_end` is the newest end among the legs derive
    keyed to this item (its episode boundary, `state._decide`)."""
    running = next((leg for leg in reversed(legs) if leg.running), None)
    if isinstance(state, NeedsHuman):
        # About to be stopped this tick: policy adds the label and the comment.
        detail = state.question if state.reason is StopReason.QUESTION else None
        return _needs_you(state.reason.value, target, last_end, detail)
    if isinstance(state, Wait) and state.reason is WaitReason.NEEDS_HUMAN:
        stop = latest_stop(comments, harness)
        if stop is not None and (last_end is None or stop.at > last_end):
            return _needs_you(stop.reason, target, stop.at, stop.message)
        return _needs_you("labelled", target, None, None)
    if isinstance(state, Busy):
        act = running.kind if running is not None else None
        since = (running.started_at or running.reserved_at) if running is not None else None
        return Working("agent", act, f"{_ACT_WORDS.get(act or '', 'Agent')} running",
                       "An agent leg holds this item.", _iso(since))
    if isinstance(state, Dispatch):
        act = state.act.name
        return Working("queued", act, f"{_ACT_WORDS[act]} next", "Dispatches when a slot is free.", _iso(last_end))
    if isinstance(state, Merge):
        return Working("merge", None, "Merging", "Approved on its head with green CI.", _iso(last_end))
    if isinstance(state, Refresh):
        return Working("update", None, "Updating branch", "Behind its base; GitHub updates it next tick.",
                       _iso(last_end))
    if isinstance(state, OpenPR):
        return Working("open_pr", None, "Opening PR", f"Branch {state.branch} is pushed without a PR.",
                       _iso(last_end))
    if isinstance(state, Release):
        return Working("release", None, "Releasing", "A human replied; needs-human comes off next tick.",
                       _iso(last_end))
    if isinstance(state, Backoff):
        act = state.act.name
        return Waiting("backoff", f"{_ACT_WORDS[act]} backing off",
                       "The last leg ended on a machine failure; it retries after the backoff.",
                       _iso(last_end), _iso(state.until))
    if isinstance(state, Wait):
        return _wait_status(state.reason, pr, last_end)
    raise TypeError(f"unknown derived state {state!r}")


def _wait_status(reason: WaitReason, pr: PRView | None, last_end: datetime | None) -> BoardStatus:
    since = _iso(last_end)
    if reason is WaitReason.CI_PENDING:
        return Working("ci", None, "CI running", "Required checks are still running on the head.", since)
    if reason is WaitReason.HEADROOM:
        return Waiting("headroom", "Daily cap reached", "The daily or global spend cap leaves no room for a leg.",
                       since)
    if reason is WaitReason.HUMAN_CHANGES_REQUESTED:
        return Waiting("human-review", "Human review standing",
                       "A human's change request stands on an older head; they re-review.", since)
    if reason is WaitReason.MERGEABILITY_UNKNOWN and pr is not None and pr.merge_state.upper() == "BLOCKED":
        return Waiting("branch-protection", "Branch protection",
                       "Approved and green; GitHub waits on a required approval or check.", since)
    detail = {
        WaitReason.HEAD_UNKNOWN: "The PR could not be read in full this poll.",
        WaitReason.CI_UNKNOWN: "Required checks could not be read for the current head.",
        WaitReason.MERGEABILITY_UNKNOWN: "GitHub is still computing mergeability.",
    }.get(reason, "Waiting on GitHub.")
    return Waiting("github", "Reading GitHub", detail, since)


def primary_action(status: BoardStatus, *, held: bool) -> str | None:
    """The one action a row offers: unblock a `needs-human` label whose
    removal releases the item; a question or a spent lifetime budget is
    answered elsewhere. A stop derive decided but policy has not written yet
    carries no label to remove, so it offers to open the item instead."""
    if not isinstance(status, NeedsYou):
        return None
    if status.reason in (StopReason.QUESTION.value, StopReason.LIFETIME_CAP.value) or not held:
        return "open"
    return "unblock"


def role_states(
    fix: Mapping[ItemKey, DerivedState], review: Mapping[ItemKey, DerivedState],
) -> dict[ItemKey, DerivedState]:
    """One map from the two roles' derives (`tick.decide_with`), which differ
    only in each role's leg-price headroom: a REVIEW dispatch is the review
    role's to make or to wait on, every other act the fix role's."""
    def reviews(state: DerivedState | None) -> bool:
        return isinstance(state, Dispatch) and state.act is AgentKind.REVIEW

    return {key: review[key] if reviews(state) or reviews(review.get(key)) else state for key, state in fix.items()}


def ci_display(pr: PRView | None) -> str | None:
    """passing | failing | pending for the PR's current head, or None when
    unread or for another head (no guessed green)."""
    if pr is None or not isinstance(pr.ci, Observed) or pr.ci.head != pr.head_ref_oid:
        return None
    ci = reduce_ci(pr.ci)
    if isinstance(ci, Failed):
        return "failing"
    if isinstance(ci, CiWaiting):
        return "pending"
    return "passing" if isinstance(ci, Clear) else None


# ── rail and history ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Rail:
    current: str
    furthest: str
    resolve_attempts: int
    review_rounds: int
    ci_runs: int
    steps: int
    live: bool


@dataclass(frozen=True)
class TrackChip:
    kind: str
    outcome: str
    count: int


@dataclass(frozen=True)
class StageVisit:
    stage: str
    steps: tuple[TrackChip, ...]


def current_stage(state: DerivedState | None, *, has_pr: bool, legs: tuple[LegRow, ...]) -> Stage:
    running = next((leg for leg in reversed(legs) if leg.running), None)
    last = next((stage for leg in reversed(legs) if leg.derived and (stage := stage_of_kind(leg.kind))), None)
    fallback = last or (Stage.CI if has_pr else Stage.RESOLVE)
    if isinstance(state, (Dispatch, Backoff)):
        return _STAGE_OF_KIND[state.act.name]
    if isinstance(state, NeedsHuman) and state.act is not None:
        return _STAGE_OF_KIND[state.act.name]
    if isinstance(state, NeedsHuman) and state.reason is StopReason.REVIEW_ROUNDS:
        return Stage.REVIEW
    if isinstance(state, Merge):
        return Stage.MERGE
    if isinstance(state, Refresh):
        return Stage.CI
    if isinstance(state, OpenPR):
        return Stage.RESOLVE
    if isinstance(state, Busy) and running is not None:
        return stage_of_kind(running.kind) or fallback
    if isinstance(state, Wait):
        if state.reason is WaitReason.MERGEABILITY_UNKNOWN:
            return Stage.MERGE
        if state.reason is WaitReason.HUMAN_CHANGES_REQUESTED:
            return Stage.REVIEW
        if state.reason in (WaitReason.CI_PENDING, WaitReason.CI_UNKNOWN, WaitReason.HEAD_UNKNOWN):
            return Stage.CI
    return fallback


def rail(state: DerivedState | None, *, has_pr: bool, legs: tuple[LegRow, ...], done: bool = False) -> Rail:
    counted = [leg for leg in legs if leg.derived]
    current = Stage.MERGE if done else current_stage(state, has_pr=has_pr, legs=legs)
    reached = [stage for leg in counted if (stage := stage_of_kind(leg.kind))] + [current]
    furthest = max(reached, key=STAGE_ORDER.index)
    return Rail(
        current=current.value,
        furthest=furthest.value,
        resolve_attempts=sum(1 for leg in counted if leg.kind == AgentKind.RESOLVE.name),
        review_rounds=sum(1 for leg in counted if leg.kind == AgentKind.REVIEW.name),
        ci_runs=sum(1 for leg in counted if leg.kind in (AgentKind.FIX_CI.name, AgentKind.REBASE.name)),
        steps=len(counted),
        live=any(leg.running for leg in legs),
    )


def history(legs: tuple[LegRow, ...]) -> tuple[StageVisit, ...]:
    """One visit per run of consecutive legs in the same stage, oldest first;
    identical consecutive legs fold into one chip."""
    visits: list[tuple[Stage, list[TrackChip]]] = []
    for leg in legs:
        stage = stage_of_kind(leg.kind)
        if stage is None or not leg.derived:
            continue
        if not visits or visits[-1][0] is not stage:
            visits.append((stage, []))
        chips = visits[-1][1]
        if chips and chips[-1].kind == leg.kind and chips[-1].outcome == leg.outcome:
            chips[-1] = TrackChip(leg.kind, leg.outcome, chips[-1].count + 1)
        else:
            chips.append(TrackChip(leg.kind, leg.outcome, 1))
    return tuple(StageVisit(stage.value, tuple(chips)) for stage, chips in visits)


# ── metrics ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class KindCost:
    kind: str
    usd: float
    runs: int


@dataclass(frozen=True)
class BasisCost:
    basis: str
    usd: float


@dataclass(frozen=True)
class Metrics:
    """The CLAUDE.md primary metrics and guardrails that legs plus GitHub
    can answer. None means unknown (GitHub unread), never zero."""

    merged_prs: int | None
    merged_prs_partial: bool  # GitHub listed only the newest merged PRs: the count and per-PR cost are bounds
    open_items: int | None
    needs_human: int | None
    total_cost_usd: float
    cost_per_merged_pr_usd: float | None
    by_basis: tuple[BasisCost, ...]
    by_kind: tuple[KindCost, ...]
    first_pass_approvals: int
    first_pass_changes: int
    first_pass_rate: float | None
    rounds_per_pr: float | None
    legs: int
    machine_legs: int


CARRIED_KIND = "carried"


def _usd(value: float) -> float:
    return round(value, 6) if math.isfinite(value) else 0.0


def metrics(
    legs: Iterable[LegRow],
    *,
    merged_prs: int | None,
    open_items: int | None,
    needs_human: int | None,
    merged_prs_partial: bool = False,
) -> Metrics:
    """`by_kind` keys a carried leg as `carried`: its legacy kind (ROUTE,
    TRIAGE, RE_REVIEW, …) names no act of the design, only money history."""
    rows = [leg for leg in legs if leg.counted]
    total = sum(leg.cost_usd for leg in rows)
    by_kind: dict[str, list[float]] = {}
    by_basis: dict[str, float] = {}
    for leg in rows:
        by_kind.setdefault(CARRIED_KIND if leg.carried else leg.kind, []).append(leg.cost_usd)
        basis = leg.basis or ("unsettled" if leg.running else "unpriced")
        by_basis[basis] = by_basis.get(basis, 0.0) + leg.cost_usd
    verdicts: dict[int, list[str]] = {}
    for leg in sorted(rows, key=lambda leg: leg.at):
        if leg.kind == AgentKind.REVIEW.name and leg.verdict in ("APPROVE", "REQUEST_CHANGES") and leg.number > 0:
            verdicts.setdefault(leg.number, []).append(leg.verdict)
    approvals = sum(1 for seen in verdicts.values() if seen[0] == "APPROVE")
    changes = len(verdicts) - approvals
    return Metrics(
        merged_prs=merged_prs,
        merged_prs_partial=merged_prs_partial,
        open_items=open_items,
        needs_human=needs_human,
        total_cost_usd=_usd(total),
        cost_per_merged_pr_usd=_usd(total / merged_prs) if merged_prs else None,
        by_basis=tuple(BasisCost(basis, _usd(usd)) for basis, usd in sorted(by_basis.items())),
        by_kind=tuple(KindCost(kind, _usd(sum(costs)), len(costs)) for kind, costs in sorted(by_kind.items())),
        first_pass_approvals=approvals,
        first_pass_changes=changes,
        first_pass_rate=approvals / len(verdicts) if verdicts else None,
        rounds_per_pr=sum(len(seen) for seen in verdicts.values()) / len(verdicts) if verdicts else None,
        legs=len(rows),
        machine_legs=sum(1 for leg in rows if leg.derived and leg.outcome == "failed"),
    )


def metrics_line(m: Metrics) -> str:
    """One status line of the primary metrics."""
    per_pr = f"${m.cost_per_merged_pr_usd:.2f}/merged PR" if m.cost_per_merged_pr_usd is not None else "no merged PRs"
    merged = "?" if m.merged_prs is None else f"{'≥' if m.merged_prs_partial else ''}{m.merged_prs}"
    return f"spend ${m.total_cost_usd:.2f} over {m.legs} legs ({m.machine_legs} machine) · merged {merged} · {per_pr}"


@dataclass(frozen=True)
class SpendBucket:
    hour_start_iso: str
    usd: float


def spend_per_hour(legs: Iterable[LegRow], now: datetime, hours: int = SPEND_WINDOW_HOURS) -> tuple[SpendBucket, ...]:
    """Hourly USD over the trailing window, oldest first, empty hours as zero.
    A leg outside the window (a skewed future stamp included) is dropped."""
    if hours <= 0:
        return ()
    end = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    totals = {end - timedelta(hours=offset): 0.0 for offset in range(hours)}
    for leg in legs:
        if not leg.counted:
            continue
        bucket = leg.at.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        if bucket in totals:
            totals[bucket] += leg.cost_usd
    return tuple(SpendBucket(hour.isoformat(), _usd(usd)) for hour, usd in sorted(totals.items()))


@dataclass(frozen=True)
class HealthDay:
    day: str
    opened: int
    closed: int
    net_open: int


def health_trend(issues: Iterable[tuple[str, str]], local_tz: tzinfo) -> tuple[HealthDay, ...]:
    """Issues opened and closed per local day from GitHub (created, closed) stamps."""
    days: dict[str, list[int]] = {}
    for created, closed in issues:
        for index, raw in ((0, created), (1, closed)):
            stamp = parse_timestamp(raw)
            if stamp is not None:
                days.setdefault(stamp.astimezone(local_tz).date().isoformat(), [0, 0])[index] += 1
    return tuple(HealthDay(day, o, c, o - c) for day, (o, c) in sorted(days.items()))


# ── notifications ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Notification:
    id: str
    ts: str
    severity: str  # info | attention
    title: str
    detail: str | None = None
    issue: int | None = None
    pr: int | None = None


def notifications(
    legs: Iterable[LegRow], needs: Iterable[tuple[int, NeedsYou]], limit: int = NOTIFICATION_LIMIT,
) -> tuple[Notification, ...]:
    """Leg starts and ends, and items that need a human. Ids are stable, so a
    client toasts each once."""
    out: list[Notification] = []
    for leg in legs:
        if not leg.derived or leg.issue is None:
            continue
        pr = leg.number if leg.kind != AgentKind.RESOLVE.name and leg.number > 0 else None
        word = _ACT_WORDS.get(leg.kind, leg.kind)
        if leg.started_at is not None:
            out.append(Notification(f"leg:{leg.leg_id}:start", leg.started_at.isoformat(), "info",
                                    f"#{leg.issue} {word} started", None, leg.issue, pr))
        if leg.ended_at is not None and not leg.running:
            failed = leg.outcome == "failed"
            out.append(Notification(
                f"leg:{leg.leg_id}:end", leg.ended_at.isoformat(), "attention" if failed else "info",
                f"#{leg.issue} {word} {leg.outcome.replace('_', ' ')}",
                f"${leg.cost_usd:.2f}" + (" (estimated)" if leg.estimated else ""), leg.issue, pr,
            ))
    for issue, status in needs:
        out.append(Notification(f"needs:{issue}:{status.reason}:{status.since_ts or '-'}", status.since_ts or "",
                                "attention", f"#{issue} needs you: {status.headline}", status.reason, issue))
    out.sort(key=lambda n: (parse_timestamp(n.ts) or datetime.min.replace(tzinfo=timezone.utc), n.id))
    return tuple(out[-limit:]) if limit > 0 else ()


# ── labels ───────────────────────────────────────────────────────────────────


def label_value(labels: Iterable[str], prefixes: tuple[str, ...], exact: tuple[str, ...] = ()) -> str | None:
    """One compact planning value from labels like `type:bug` or `impact:high`."""
    lowered = [(label, label.lower()) for label in labels]
    for expected in exact:
        if any(value == expected for _, value in lowered):
            return expected
    for label, value in lowered:
        for prefix in prefixes:
            if value.startswith(prefix) and len(label) > len(prefix):
                return label[len(prefix):]
    return None


def frecency(stamps: Iterable[datetime], now: datetime) -> float:
    """Recent activity weighted by recency: a day counts 3, a week 1, a month 0.25."""
    score = 0.0
    for stamp in stamps:
        age = (now - stamp).total_seconds()
        if age < 0:
            continue
        score += 3.0 if age <= 86400 else 1.0 if age <= 7 * 86400 else 0.25 if age <= 30 * 86400 else 0.0
    return score
