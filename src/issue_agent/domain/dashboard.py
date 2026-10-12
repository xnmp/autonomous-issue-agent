"""Pure dashboard lifecycle, attribution, and metric model.

The model consumes already-parsed journal events. It knows nothing about files,
processes, HTTP, GitHub, or rendering.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import Enum
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Literal, Mapping

from .actions import AgentKind
from .events import JsonValue
from .labels import Label
from .outcome import ExitReason
from .world import ReviewDecision


def _utc_date(timestamp: float) -> date:
    return datetime.fromtimestamp(timestamp, timezone.utc).date()


# The three role daemons, in the order `journal_paths` returns their journals.
ROLE_NAMES = ("fix", "review")

# A daemon that has not journalled anything in this long is probably not
# ticking. Generous relative to POLL_SECONDS=20 because a long resolve run emits
# nothing between its start and settled lines, and a 15-minute session must not
# read as a dead daemon.
STALE_AFTER_S = 1800.0

# Leg families, for the cost mix and the stage heuristic. Kinds outside these
# sets still cost money and are still reported — they just fall into "other".
RESOLVE_KINDS = frozenset({"RESOLVE", "RESUME", "ROUTE"})
REVIEW_KINDS = frozenset({"REVIEW", "RE_REVIEW"})
REPAIR_KINDS = frozenset({"FIX_CI", "ADDRESS_REVIEW", "REBASE"})
# Kinds whose `number` is an ISSUE; everything else numbers a PR.
ISSUE_NUMBERED = RESOLVE_KINDS


# ── action-desc parsing ───────────────────────────────────────────────────────
# `Executor` journals applied actions as a human one-liner (`{"event":"action",
# "desc":"relabel issue #7: -ai-ready +ai-working"}`) — there are no structured
# fields to read. Parsing the sentence back is the only way to attribute those
# events to an issue, so the grammar lives here, in one place, table-tested.
_RE_RELABEL = re.compile(r"^relabel (issue|pr) #(\d+): (.*)$")
# The dashboard's journaled unblock is semantically a relabel (removal-only);
# parsing it as one lets attention-stage/clock derivations see the clear.
_RE_OPERATOR_UNBLOCK = re.compile(r"^operator unblock (issue|pr) #(\d+): (.*)$")
_RE_DISPATCH = re.compile(r"^dispatch ([A-Z_]+) #(\d+)$")
_RE_CLEAR = re.compile(
    r"^clear state for pr #(\d+)(?:, issue #(\d+))? \((merged|closed-unmerged)\)$"
)
_RE_PR_VERB = re.compile(
    r"^(enable auto-merge|update branch|re-run failed checks|comment pr) "
    r"(?:PR )?#(\d+)"
)
_RE_ISSUE_VERB = re.compile(r"^(close issue|comment issue|remove worktree for) #?(\d+)")
_RE_WORKTREE = re.compile(r"^remove worktree for #(\d+)$")


@dataclass(frozen=True)
class ActionFact:
    """What one journalled `action` line was about."""

    verb: str  # relabel | dispatch | clear | automerge | close | comment | worktree
    target: str  # "issue" or "pr" — which number `number` is
    number: int
    kind: str = ""  # dispatch: the AgentKind; clear: merged/closed-unmerged
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    issue: int | None = None  # named explicitly by the desc, when it is
    merged: bool = False


def parse_action(desc: str) -> ActionFact | None:
    """One `action` desc → the fact it records, or None if unrecognised.

    Unrecognised is a normal outcome, not an error: a new action type adds a new
    sentence and this returns None until the grammar learns it, which costs the
    dashboard one unattributed event rather than a crash.
    """
    desc = (desc or "").strip()
    m = _RE_RELABEL.match(desc) or _RE_OPERATOR_UNBLOCK.match(desc)
    if m:
        target, number, moves = m.group(1), int(m.group(2)), m.group(3)
        added = tuple(t[1:] for t in moves.split() if t.startswith("+"))
        removed = tuple(t[1:] for t in moves.split() if t.startswith("-"))
        return ActionFact("relabel", target, number, added=added, removed=removed)
    m = _RE_DISPATCH.match(desc)
    if m:
        kind = m.group(1)
        target = "issue" if kind in ISSUE_NUMBERED else "pr"
        return ActionFact("dispatch", target, int(m.group(2)), kind=kind)
    m = _RE_CLEAR.match(desc)
    if m:
        issue = int(m.group(2)) if m.group(2) else None
        return ActionFact(
            "clear", "pr", int(m.group(1)), kind=m.group(3), issue=issue,
            merged=(m.group(3) == "merged"),
        )
    m = _RE_WORKTREE.match(desc)
    if m:
        return ActionFact("worktree", "issue", int(m.group(1)))
    m = _RE_PR_VERB.match(desc)
    if m:
        return ActionFact(m.group(1).split()[0], "pr", int(m.group(2)))
    m = _RE_ISSUE_VERB.match(desc)
    if m:
        return ActionFact(m.group(1).split()[0], "issue", int(m.group(2)))
    return None


# ── issue attribution ─────────────────────────────────────────────────────────
def pr_issue_map(events) -> dict[int, int]:
    """PR number → the issue it closes, learned from the agent runs themselves.

    A review leg journals `number` = the PR and `issue` = the issue it closes,
    so the harness's own receipts carry the mapping; nothing has to be asked of
    GitHub. Later events win, since a PR's closing-ref can be added after the
    first run against it.
    """
    out: dict[int, int] = {}
    for e in events:
        if e.get("event") not in ("agent_run", "agent_run_start"):
            continue
        issue, number = _int(e.get("issue")), _int(e.get("number"))
        if issue is not None and number is not None:
            out[number] = issue
    return out


def event_issue(event: dict, pr_issue: dict[int, int]) -> int | None:
    """The issue an event belongs to, or None when it names none (a `daily`
    spend block, a `tick_gated`) — those are deployment-level facts."""
    kind = str(event.get("kind") or "")
    name = event.get("event")
    if name in ("agent_run", "agent_run_start"):
        issue = _int(event.get("issue"))
        if issue is not None:
            return issue
        number = _int(event.get("number"))
        if number is None:
            return None
        return number if kind in ISSUE_NUMBERED else pr_issue.get(number)
    if name == "action":
        fact = parse_action(str(event.get("desc") or ""))
        if fact is None:
            return None
        if fact.issue is not None:
            return fact.issue
        return fact.number if fact.target == "issue" else pr_issue.get(fact.number)
    if name == "escalation":
        number = _int(event.get("number"))
        if number is None:
            return None
        return number if event.get("target") == "issue" else pr_issue.get(number)
    issue = _int(event.get("issue"))
    if issue is not None:
        return issue
    pr = _int(event.get("pr"))
    return pr_issue.get(pr) if pr is not None else None


# ── rows ──────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class RunSummary:
    """One settled agent run — the unit both the cost mix and the rounds count
    are computed over."""

    ts: datetime | None
    role: str
    kind: str
    number: int | None
    issue: int | None
    model: str
    status: str
    exit_reason: str
    duration_s: float
    cost_usd: float
    estimated: bool
    session_id: str
    # The envelope's semantic error signal. Defaulted so pre-existing positional
    # constructions keep working; every producer in this module sets it.
    is_error: bool = False


@dataclass(frozen=True)
class OtherAgentKind:
    """A legitimate extension/legacy kind outside the dispatch enum."""

    value: str


AgentRunKind = AgentKind | OtherAgentKind


@dataclass(frozen=True)
class FailedRun:
    """An agent run whose envelope did not settle cleanly."""

    kind: AgentRunKind
    reason: ExitReason


@dataclass(frozen=True)
class BlockedRun:
    """A clean agent response that explicitly needs human input."""

    kind: AgentRunKind


@dataclass(frozen=True)
class ReviewVerdict:
    """A clean reviewer decision. Failed runs cannot inhabit this variant."""

    kind: Literal[AgentKind.REVIEW, AgentKind.RE_REVIEW]
    decision: ReviewDecision


@dataclass(frozen=True)
class CompletedRun:
    """A clean run that is not a structured review verdict."""

    kind: AgentKind


@dataclass(frozen=True)
class UnrecognizedRun:
    """A legacy/corrupt kind retained for display but denied typed meaning."""

    kind: str


SettledRun = (
    FailedRun | BlockedRun | ReviewVerdict | CompletedRun | UnrecognizedRun
)


# The two ways a leg can say "this run ended cleanly": claude settles the happy
# path with no exit reason at all, codex settles it as `ok`. Keying failure on
# "the field is non-empty" therefore rendered every healthy codex leg as "run
# failed" (#158) — a claude-shaped default, not a real signal.
_CLEAN_EXIT_REASONS = frozenset({"", ExitReason.OK.value})


def leg_failed(is_error: bool, exit_reason: str) -> bool:
    """Whether a settled leg should render as failed.

    Keys on the semantic error signal: the envelope's `is_error`, or an exit
    reason that is not one of the clean sentinels. An `is_error` leg with no
    exit reason still fails; `ok` with `is_error=False` renders whatever
    lifecycle state the leg would have had without the field.

    An unrecognised reason (a legacy journal, a runtime the taxonomy has not
    caught up with) counts as failed. Only a clean sentinel is evidence of a
    clean finish, and a silently-swallowed death is the worse failure mode for
    a dashboard whose job is showing which legs died.
    """
    if is_error:
        return True
    return exit_reason.strip().casefold() not in _CLEAN_EXIT_REASONS


def _agent_kind(raw: str) -> AgentKind | None:
    try:
        return AgentKind[raw]
    except KeyError:
        return None


def _failure_reason(leg: RunSummary) -> ExitReason:
    raw = leg.exit_reason.strip().casefold()
    if raw in _CLEAN_EXIT_REASONS:
        return ExitReason.AGENT_ERROR
    try:
        return ExitReason(raw)
    except ValueError:
        return ExitReason.AGENT_ERROR


def settled_run(leg: RunSummary) -> SettledRun:
    """Parse the permissive journal projection into a closed outcome union.

    Failure is classified first. A runtime may preserve parsed structured
    output beside ``is_error=True``; that raw contradiction becomes only a
    ``FailedRun`` here, so no downstream typed value can expose its APPROVE as
    a verdict.
    """
    if leg_failed(leg.is_error, leg.exit_reason):
        kind = _agent_kind(leg.kind) or OtherAgentKind(leg.kind)
        return FailedRun(kind, _failure_reason(leg))
    kind = _agent_kind(leg.kind)
    if leg.status.casefold() == "blocked":
        return BlockedRun(kind or OtherAgentKind(leg.kind))
    if kind is None:
        return UnrecognizedRun(leg.kind)
    if kind in {AgentKind.REVIEW, AgentKind.RE_REVIEW}:
        try:
            decision = ReviewDecision(leg.status)
        except ValueError:
            return CompletedRun(kind)
        if kind is AgentKind.REVIEW:
            return ReviewVerdict(AgentKind.REVIEW, decision)
        return ReviewVerdict(AgentKind.RE_REVIEW, decision)
    return CompletedRun(kind)


def review_verdict(leg: RunSummary) -> ReviewVerdict | None:
    if leg.kind not in REVIEW_KINDS:
        return None
    outcome = settled_run(leg)
    return outcome if isinstance(outcome, ReviewVerdict) else None


def _settled_leg_stage(leg: RunSummary) -> str:
    outcome = settled_run(leg)
    if isinstance(outcome, FailedRun):
        return "run failed"
    if isinstance(outcome, BlockedRun):
        return "awaiting human reply"
    if isinstance(outcome, ReviewVerdict):
        if outcome.decision is ReviewDecision.APPROVE:
            return "awaiting merge"
        if outcome.decision is ReviewDecision.REQUEST_CHANGES:
            return "changes requested"
        return "reviewed"
    if (
        isinstance(outcome, CompletedRun)
        and outcome.kind in {AgentKind.REVIEW, AgentKind.RE_REVIEW}
    ):
        return "reviewed"
    if leg.kind in RESOLVE_KINDS | REPAIR_KINDS:
        return "awaiting review"
    return ""


def _review_verdict_legs(
    legs: Iterable[RunSummary],
) -> list[tuple[RunSummary, ReviewVerdict]]:
    result = []
    for leg in legs:
        if (verdict := review_verdict(leg)) is not None:
            result.append((leg, verdict))
    return result


@dataclass(frozen=True)
class RunningLeg:
    """An `agent_run_start` with no settlement: either a leg running right now
    or a leg whose process died (the journal cannot tell them apart — a stale
    `since` is the tell)."""

    ts: datetime | None
    role: str
    kind: str
    number: int | None
    issue: int | None
    model: str


@dataclass(frozen=True)
class IssueRow:
    issue: int
    stage: str
    cost_usd: float
    legs: tuple[RunSummary, ...]
    running: tuple[RunningLeg, ...]
    rounds: int  # review legs against this issue's PRs
    approvals: int
    changes_requested: int
    escalations: int
    recurrences: int
    spend_blocks: int
    prs: tuple[int, ...]
    models: tuple[str, ...]
    last_ts: datetime | None
    done: bool
    events: tuple[dict, ...]  # the drill-down, in global ts order
    facts: Mapping[str, JsonValue] = field(default_factory=dict)
    lane: str = "in_flight"

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(leg.kind for leg in self.legs))


@dataclass(frozen=True)
class DaemonHealth:
    role: str
    journal_path: str
    log_path: str
    events: int
    last_ts: datetime | None
    last_event: str
    stale: bool  # no events for STALE_AFTER_S — or never any at all


@dataclass(frozen=True)
class Metrics:
    done_issues: int
    in_flight_issues: int
    total_cost_usd: float
    cost_per_done_usd: float
    by_kind: tuple[tuple[str, float, int], ...]  # (kind, $, runs), dearest first
    first_pass_approvals: int
    first_pass_changes: int
    rounds_per_pr: float
    escalations: int
    recurrences: int
    spend_blocks: int
    today_usd: float | None  # settled SQLite spend, or unknown
    # (class, count) for every class in INTERVENTION_CLASSES, always all three.
    interventions_by_class: tuple[tuple[str, int], ...] = ()
    # The same interventions, itemized and oldest first. `interventions_by_class`
    # is a fold of this, so a tile that disagreed with the timeline would be a
    # bug in one reading rather than two independent classifications.
    interventions: tuple["InterventionItem", ...] = ()

    @property
    def first_pass_rate(self) -> float | None:
        total = self.first_pass_approvals + self.first_pass_changes
        return (self.first_pass_approvals / total) if total else None

    @property
    def interventions_total(self) -> int:
        return sum(count for _name, count in self.interventions_by_class)

    @property
    def interventions_per_merged_pr(self) -> float | None:
        """Rearm+other interventions per merged PR; None when nothing merged.

        `policy` is excluded from the headline ratio on purpose: a hand promote
        is a chosen gate, so charging it here would make a deliberate
        configuration read as harness unreliability. It stays in
        `interventions_by_class` so the choice remains visible.

        None rather than 0.0 when `done_issues` is 0: "nobody intervened" and
        "nothing merged to divide by" are opposite readings of the same day.
        """
        if not self.done_issues:
            return None
        charged = sum(
            count
            for name, count in self.interventions_by_class
            if name != INTERVENTION_POLICY
        )
        return round(charged / self.done_issues, 2)


@dataclass(frozen=True)
class Deployment:
    name: str
    repo: str
    health: tuple[DaemonHealth, ...]
    issues: tuple[IssueRow, ...]  # newest activity first
    metrics: Metrics
    unattributed: tuple[dict, ...]  # events naming no issue (daily caps, ticks)
    warnings: tuple[str, ...] = ()
    # Every row the fold produced, including the ones the present/done filter
    # keeps out of `issues` — a retired or no longer observable issue whose legs
    # were still paid for. Metrics charge this set, so any second view of the
    # same money (the hourly spend series above all) must read it too, or the
    # chart and the headline total disagree about what the deployment spent.
    historical_issues: tuple[IssueRow, ...] = ()

    @property
    def metrics_issues(self) -> tuple[IssueRow, ...]:
        """The rows `build_metrics` charged. Falls back to the display rows for
        a Deployment assembled without a separate historical set."""
        return self.historical_issues or self.issues


@dataclass(frozen=True)
class Snapshot:
    """A deployment aggregate with configuration context needed by views."""

    name: str
    repo: str
    target_dir: str
    deployment: Deployment
    timestamps: tuple[datetime | None, ...]

    @property
    def last_activity(self) -> datetime | None:
        stamps = [ts for ts in self.timestamps if ts]
        return max(stamps) if stamps else None



# ── stage inference (HEURISTIC — see the table in the docstring) ──────────────
# Ordered rules, applied to the issue's events NEWEST FIRST; the first match
# wins. This is a best-effort read of "where is this issue", not a state
# machine: the harness has no single place that writes a stage, so the stage is
# reconstructed from the traces it does leave. Every rule is one journal fact.
_STAGE_RUNNING = {
    "RESOLVE": "resolving",
    "RESUME": "resolving",
    "ROUTE": "routing",
    "REVIEW": "reviewing",
    "RE_REVIEW": "reviewing",
    "FIX_CI": "fixing CI",
    "ADDRESS_REVIEW": "addressing review",
    "REBASE": "rebasing",
}


def stage_for(events, running: tuple[RunningLeg, ...] = ()) -> str:
    """Best-effort lifecycle stage for one issue.

    Rules, in priority order:

      1. an active human-attention lifecycle state → "blocked" / "escalated".
         Human attention outranks process activity, which remains in drill-down.
      2. a leg is running now → that leg's stage ("resolving", "reviewing",
         "fixing CI", …).
      3. otherwise the newest event that matches one of:
         `+ai-done` / a merged `clear state`   → "done"
         `escalation` / `+needs-human-review`  → "escalated"
         `recurrence`                          → "recurred"
         `spend_block`                         → "blocked (spend)"
         `hold`                                → "held"
         `+ai-blocked`                         → "blocked" (the web projection
                                                  adds the recorded reason)
         review run APPROVE                    → "awaiting merge"
         review run REQUEST_CHANGES            → "changes requested"
         errored run                           → "run failed"
         resolve/repair run settled            → "awaiting review"
         `+ai-working` / `+ai-pr-open`         → "working" / "pr open"
         `+ai-ready`                           → "queued"
      4. nothing matched → "unknown".
    """
    attention = _human_attention_stage(events)
    if attention:
        return attention
    if running:
        kind = running[-1].kind
        return _STAGE_RUNNING.get(kind, f"running {kind or 'agent'}".strip())
    for e in reversed(list(events)):
        stage = _stage_of_event(e)
        if stage:
            return stage
    return "unknown"


def _human_attention_stage(events) -> str:
    """Current attention state from its newest explicit transition."""
    for event in reversed(list(events)):
        if event.get("event") == "action":
            fact = parse_action(str(event.get("desc") or ""))
            if fact is None:
                continue
            if fact.verb == "clear":
                return ""
            if fact.verb != "relabel":
                continue
            if "needs-human-review" in fact.added:
                return "escalated"
            if "ai-blocked" in fact.added:
                return "blocked"
            if (
                "needs-human-review" in fact.removed
                or "ai-blocked" in fact.removed
            ):
                return ""
        elif event.get("event") == "escalation":
            return "escalated"
    return ""


def _stage_of_event(e: dict) -> str:
    name = e.get("event")
    if name == "action":
        fact = parse_action(str(e.get("desc") or ""))
        if fact is None:
            return ""
        if fact.verb == "clear":
            return "done" if fact.merged else "closed unmerged"
        if fact.verb == "relabel":
            for label, stage in (
                ("ai-done", "done"),
                ("needs-human-review", "escalated"),
                ("ai-blocked", "blocked"),
                ("ai-pr-open", "pr open"),
                ("ai-working", "working"),
                ("ai-ready", "queued"),
            ):
                if label in fact.added:
                    return stage
        return ""
    if name == "escalation":
        return "escalated"
    if name == "recurrence":
        return "recurred"
    if name == "spend_block":
        return "blocked (spend)"
    if name == "hold":
        return "held"
    if name == "agent_run":
        return _settled_leg_stage(_leg(e))
    return ""


# ── aggregation ───────────────────────────────────────────────────────────────
def build_issue_rows(events, running_starts) -> tuple[IssueRow, ...]:
    """Group a deployment's merged event stream into one row per issue.

    `events` must already be in global `ts` order across the three role journals
    — every "latest event" read below depends on it.
    """
    pr_issue = pr_issue_map(events)
    grouped: dict[int, list[dict]] = {}
    for e in events:
        issue = event_issue(e, pr_issue)
        if issue is not None:
            grouped.setdefault(issue, []).append(e)
    running_by_issue: dict[int, list[RunningLeg]] = {}
    for e in running_starts:
        issue = event_issue(e, pr_issue)
        if issue is not None:
            running_by_issue.setdefault(issue, []).append(_running_leg(e))

    rows = [
        _issue_row(issue, evs, tuple(running_by_issue.get(issue, ())))
        for issue, evs in grouped.items()
    ]
    return tuple(sorted(rows, key=_row_sort_key, reverse=True))


def _row_sort_key(row: IssueRow):
    return (row.last_ts or datetime.min.replace(tzinfo=timezone.utc), row.issue)


def _done_state(events: list[dict]) -> bool:
    """Fold lifecycle evidence so a reopened issue does not stay historically done."""
    done = False
    active_labels = {"ai-ready", "ai-working", "ai-blocked", "needs-human-review"}
    for event in events:
        event_name = event.get("event")
        if event_name == "recurrence":
            done = False
        if event_name != "action":
            continue
        fact = parse_action(str(event.get("desc") or ""))
        if fact is None:
            continue
        if fact.verb == "clear":
            done = fact.merged
        elif fact.verb == "relabel":
            if "ai-done" in fact.added:
                done = True
            elif "ai-done" in fact.removed or active_labels.intersection(fact.added):
                done = False
    return done


def _issue_row(issue: int, events: list[dict], running: tuple[RunningLeg, ...]) -> IssueRow:
    legs = tuple(_leg(e) for e in events if e.get("event") == "agent_run")
    reviews = _review_verdict_legs(legs)
    facts = [
        parse_action(str(e.get("desc") or ""))
        for e in events
        if e.get("event") == "action"
    ]
    prs = tuple(
        dict.fromkeys(
            [leg.number for leg in legs if leg.number and leg.kind not in ISSUE_NUMBERED]
            + [f.number for f in facts if f is not None and f.target == "pr"]
        )
    )
    done = _done_state(events)
    timestamps = [ts for ts in (parse_ts(e.get("ts")) for e in events) if ts]
    effective_running = () if done else running
    return IssueRow(
        issue=issue,
        # Terminal lifecycle evidence is authoritative. A torn/stale start can
        # remain unmatched in the journal after merge, but it must not make a
        # completed issue appear to be running again.
        stage="done" if done else stage_for(events, effective_running),
        cost_usd=round(sum(leg.cost_usd for leg in legs), 4),
        legs=legs,
        running=effective_running,
        rounds=len(reviews),
        approvals=sum(
            verdict.decision is ReviewDecision.APPROVE
            for _leg, verdict in reviews
        ),
        changes_requested=sum(
            verdict.decision is ReviewDecision.REQUEST_CHANGES
            for _leg, verdict in reviews
        ),
        escalations=sum(1 for e in events if e.get("event") == "escalation"),
        recurrences=sum(1 for e in events if e.get("event") == "recurrence"),
        spend_blocks=sum(1 for e in events if e.get("event") == "spend_block"),
        prs=prs,
        # Starts carry the configured model before a task settles. Reading both
        # event shapes keeps live tasks visible and recovers reviewer models
        # from older settlements that omitted the model field.
        models=tuple(
            dict.fromkeys(
                str(event.get("model"))
                for event in events
                if event.get("event") in ("agent_run_start", "agent_run")
                and event.get("kind") != "ROUTE"
                and event.get("model")
            )
        ),
        last_ts=max(timestamps) if timestamps else None,
        done=done,
        events=tuple(events),
    )


def _leg(e: dict) -> RunSummary:
    cost = _float(e.get("cost_usd"))
    estimated = _float(e.get("cost_estimated_usd"))
    return RunSummary(
        ts=parse_ts(e.get("ts")),
        role=str(e.get("role") or ""),
        kind=str(e.get("kind") or "?"),
        number=_int(e.get("number")),
        issue=_int(e.get("issue")),
        model=str(e.get("model") or ""),
        status=str(e.get("status") or ""),
        exit_reason=str(e.get("exit_reason") or ""),
        is_error=bool(e.get("is_error")),
        duration_s=_float(e.get("duration_s")),
        # The BILLED figure, exactly as `JournalingRuntime` computes it for the
        # ledger: the true cost when there is one, else the token estimate. A
        # dashboard that showed $0.00 for every aborted run would understate the
        # one leg family most worth watching.
        cost_usd=cost if cost > 0 else estimated,
        estimated=cost <= 0 and estimated > 0,
        session_id=str(e.get("session_id") or ""),
    )


def settled_run_from_event(event: dict) -> SettledRun:
    """Typed outcome at the legacy journal-dict boundary."""
    return settled_run(_leg(event))


def _running_leg(e: dict) -> RunningLeg:
    return RunningLeg(
        ts=parse_ts(e.get("ts")),
        role=str(e.get("role") or ""),
        kind=str(e.get("kind") or "?"),
        number=_int(e.get("number")),
        issue=_int(e.get("issue")),
        model=str(e.get("model") or ""),
    )


def unattributed_legs(unattributed) -> list[RunSummary]:
    """Deployment-level paid legs — the runs naming no issue.

    Shared so that every view of this money reads the same billed figure:
    `_leg` prefers the true cost and falls back to the token estimate, and a
    caller that read `cost_usd` off the raw event would silently drop every
    subscription-priced leg.
    """
    return [
        _leg(e)
        for e in unattributed
        if isinstance(e, dict) and e.get("event") == "agent_run"
    ]


def first_pass_verdicts(rows) -> tuple[int, int]:
    """(approvals, changes-requested) over the FIRST review verdict per PR.

    Per PR, not per review: the named guardrail is "did the first review of this
    PR approve it", so later rounds — which are the *consequence* of a first-pass
    rejection — must not be counted again or a bad PR reviewed four times reads
    as three-quarters healthy.
    """
    first: dict[tuple[int, int | None], ReviewDecision] = {}
    for row in rows:
        for leg in row.legs:
            verdict = review_verdict(leg)
            if verdict is None or verdict.decision is ReviewDecision.ESCALATE:
                continue
            first.setdefault((row.issue, leg.number), verdict.decision)
    verdicts = list(first.values())
    return (
        verdicts.count(ReviewDecision.APPROVE),
        verdicts.count(ReviewDecision.REQUEST_CHANGES),
    )


# ── operator interventions ────────────────────────────────────────────────────
# "Interventions per merged PR" is the adjudicating metric for running the
# harness without a manager, so it has to be a number rather than a feeling.
#
# The automated split is a PROXY and is named as one. The gap / config /
# judgment causes an operator assigns in a session review need a human to say
# WHY the harness stopped; ADR 0013's ledger is where those land. Nothing here
# invents them. What the journal can answer on its own is what the operator had
# to DO:
#
#   rearm  — the harness wedged and a human re-armed it (unblock, review
#            dismissal, priority nudge). Closest available stand-in for the
#            gap class until ADR 0013 lands causes.
#   policy — a chosen gate rather than a defect: promoting an issue to ai-ready
#            by hand is the operator's admission gate.
#   other  — an operator event this grammar does not recognise. An unclassified
#            intervention is still an intervention, so it is counted, never
#            dropped; a growing `other` means the grammar owes a class.
#
# Manual gh-side work (closing an issue by hand)
# reaches the harness as an observed world fact, not as an operator event, and
# is therefore NOT in this count. The number is a floor on human involvement.
INTERVENTION_REARM = "rearm"
INTERVENTION_POLICY = "policy"
INTERVENTION_OTHER = "other"
INTERVENTION_CLASSES = (
    INTERVENTION_REARM,
    INTERVENTION_POLICY,
    INTERVENTION_OTHER,
)

# Two further DISPLAY classes, for the itemized timeline only. Each refines one
# metric class rather than competing with it (see `timeline_class`), so the
# guardrail series keeps its three-class history while the timeline can say
# which kind of re-arm or which kind of unrecognised action this row was.
INTERVENTION_GUIDANCE = "guidance"   # a re-arm that also moved the scope note
INTERVENTION_RELABEL = "relabel"     # an operator label move outside the grammar
INTERVENTION_TIMELINE_CLASSES = (
    INTERVENTION_REARM,
    INTERVENTION_GUIDANCE,
    INTERVENTION_RELABEL,
    INTERVENTION_POLICY,
    INTERVENTION_OTHER,
)

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class InterventionItem:
    """One journalled operator action, retained rather than counted away.

    `action` is the raw projected payload: the fold has no vocabulary for
    operator prose, and the view tier already owns the one-line rendering of a
    label move, a scope note, or a failed dismissal.
    """

    identity: str
    ts: datetime | None
    metric_class: str    # one of INTERVENTION_CLASSES — what the tile counts
    action_class: str    # one of INTERVENTION_TIMELINE_CLASSES — what the row says
    kind: str            # the endpoint's `ActionApplied.kind`, verbatim
    issue: int | None
    pr: int | None
    action: Mapping[str, JsonValue]

# `ActionApplied.kind` values and `command_id` prefixes minted by the
# dashboard's operator endpoints (app/dashweb/service.py). The relabel path
# routes through a durable command whose id is a content digest, so its class
# is read off the labels it moved instead.
_REARM_ACTION_KINDS = frozenset({"operator_unblock", "operator_dismiss_review"})
_REARM_COMMAND_PREFIXES = ("operator-unblock:", "operator-dismiss-review:")
_RELABEL_ACTION_KIND = "operator_relabel"
# Hand-adding the promotion label is the operator's admission gate.
# Deliberately narrow: some
# other lifecycle label arriving from an operator endpoint is a new behaviour
# nobody has classified, and `other` is where it should surface.
_POLICY_ADD_LABELS = frozenset({Label.AI_READY.value})
_PRIORITY_LABELS = frozenset({Label.P1.value, Label.P2.value, Label.P3.value})


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _label_set(value) -> frozenset[str]:
    """The labels in an action attribute, tolerating anything at all.

    Operator payloads are JSON off disk: a torn writer, a hand-edited journal,
    or a future field shape must cost this fold a mis-classification at worst,
    never a traceback.
    """
    if isinstance(value, (list, tuple)):
        return frozenset(_text(item) for item in value if _text(item))
    return frozenset()


def operator_action(event) -> Mapping | None:
    """The operator-authored `action` payload of one projected event, or None.

    Provenance: an intervention is an action the DASHBOARD's operator endpoints
    journalled — they stamp `attributes.operator` with `<repo>:dashboard` and
    mint `operator_*` kinds. A daemon's own relabel carries neither and is not
    an intervention, however similar the sentence reads.
    """
    if not isinstance(event, Mapping) or event.get("event") != "action":
        return None
    action = event.get("action")
    if not isinstance(action, Mapping):
        return None
    if (
        _text(action.get("operator"))
        or _text(action.get("type")).startswith("operator")
        or _text(event.get("command_id")).startswith("operator-")
    ):
        return action
    return None


def classify_intervention(action, command_id="") -> str:
    """One operator action → `rearm` | `policy` | `other`. Total, never raises."""
    if not isinstance(action, Mapping):
        return INTERVENTION_OTHER
    kind = _text(action.get("type"))
    if kind in _REARM_ACTION_KINDS or _text(command_id).startswith(
        _REARM_COMMAND_PREFIXES
    ):
        return INTERVENTION_REARM
    if kind == _RELABEL_ACTION_KIND:
        added = _label_set(action.get("add"))
        removed = _label_set(action.get("remove"))
        if added & _POLICY_ADD_LABELS:
            return INTERVENTION_POLICY
        moved = added | removed
        # A priority set/clear also removes the other two P labels, so the whole
        # move stays inside the priority vocabulary.
        if moved and moved <= _PRIORITY_LABELS:
            return INTERVENTION_REARM
    return INTERVENTION_OTHER


def timeline_class(action, metric_class: str) -> str:
    """The finer DISPLAY class for one operator action.

    Strict refinement of `classify_intervention`, never a contradiction of it:
    `guidance` is a `rearm` that also moved the stored scope note, `relabel` is
    an `other` that came from the relabel endpoint. A timeline row and the
    intervention tile therefore never disagree about what happened — the tile
    just says less. The metric keeps the coarse three so the guardrail series
    stays comparable across deployments that predate this view.
    """
    if not isinstance(action, Mapping):
        return INTERVENTION_OTHER
    if metric_class == INTERVENTION_REARM:
        if _text(action.get("guidance")):
            return INTERVENTION_GUIDANCE
        return INTERVENTION_REARM
    if metric_class == INTERVENTION_OTHER:
        if _text(action.get("type")) == _RELABEL_ACTION_KIND:
            return INTERVENTION_RELABEL
        return INTERVENTION_OTHER
    return metric_class


def intervention_items(rows, unattributed=()) -> tuple[InterventionItem, ...]:
    """Every journalled operator intervention, oldest first, deduplicated.

    The counts below are a fold of this list, so the tile and the timeline
    cannot drift: one classification pass, two readings of it.
    """
    events = [event for row in rows for event in row.events]
    events.extend(unattributed)
    items: list[InterventionItem] = []
    seen: set[str] = set()
    for index, event in enumerate(events):
        action = operator_action(event)
        if action is None:
            continue
        # An action correlated to both an issue and a PR can be projected onto
        # more than one row; the event id is what makes it one intervention.
        identity = _text(event.get("event_id")) or f"position:{index}"
        if identity in seen:
            continue
        seen.add(identity)
        metric_class = classify_intervention(action, event.get("command_id"))
        items.append(
            InterventionItem(
                identity=identity,
                ts=parse_ts(event.get("ts")),
                metric_class=metric_class,
                action_class=timeline_class(action, metric_class),
                kind=_text(action.get("type")),
                issue=_int(event.get("issue")),
                pr=_int(event.get("pr")),
                action=action,
            )
        )
    # Chronological, with a stable tie-break for the several targets one
    # endpoint journals in the same pass (an issue and each of its PRs).
    items.sort(key=lambda item: (item.ts is not None, item.ts or _EPOCH))
    return tuple(items)


def intervention_counts(rows, unattributed=()) -> tuple[tuple[str, int], ...]:
    """Operator interventions per class, in `INTERVENTION_CLASSES` order.

    Every class is present with an explicit zero: a missing key and a real zero
    read identically in a dashboard tile, and the difference matters when the
    question is "did anyone touch this deployment".
    """
    counts = dict.fromkeys(INTERVENTION_CLASSES, 0)
    for item in intervention_items(rows, unattributed):
        counts[item.metric_class] += 1
    return tuple((name, counts[name]) for name in INTERVENTION_CLASSES)


def interventions_line(metrics: Metrics) -> str:
    """The one-line CLI readout. Pure so `issue-agent status` and the dashboard
    cannot drift on how the same numbers are worded."""
    by_class = dict(metrics.interventions_by_class)
    ratio = metrics.interventions_per_merged_pr
    return (
        f"interventions: {metrics.interventions_total} ("
        + ", ".join(f"{name} {by_class.get(name, 0)}" for name in INTERVENTION_CLASSES)
        + ") — "
        + (
            f"{ratio:.1f} per merged PR"
            if ratio is not None
            else "no merged PRs yet"
        )
    )


def build_metrics(
    rows,
    unattributed,
    today_usd: float | None,
) -> Metrics:
    legs = [leg for row in rows for leg in row.legs]
    legs += unattributed_legs(unattributed)
    by_kind: dict[str, tuple[float, int]] = {}
    for leg in legs:
        cost, n = by_kind.get(leg.kind, (0.0, 0))
        by_kind[leg.kind] = (cost + leg.cost_usd, n + 1)
    unattributed_cost: dict[str, float] = {}
    for index, event in enumerate(unattributed):
        event_name = event.get("event")
        if event_name not in {"agent_run", "spend"}:
            continue
        identity = str(
            event.get("run_id")
            or event.get("event_id")
            or f"unattributed:{index}"
        )
        if event_name == "spend" or identity not in unattributed_cost:
            unattributed_cost[identity] = _float(event.get("cost_usd"))
    total = sum(row.cost_usd for row in rows) + sum(
        unattributed_cost.values()
    )
    done = [row for row in rows if row.done]
    approvals, changes = first_pass_verdicts(rows)
    reviewed = [row for row in rows if row.rounds]
    interventions = intervention_items(rows, unattributed)
    counts = dict.fromkeys(INTERVENTION_CLASSES, 0)
    for item in interventions:
        counts[item.metric_class] += 1
    return Metrics(
        done_issues=len(done),
        in_flight_issues=sum(
            1
            for row in rows
            if not row.done and row.lane == "in_flight"
        ),
        total_cost_usd=round(total, 4),
        cost_per_done_usd=round(total / len(done), 4) if done else 0.0,
        by_kind=tuple(
            (kind, round(cost, 4), n)
            for kind, (cost, n) in sorted(
                by_kind.items(), key=lambda kv: (-kv[1][0], kv[0])
            )
        ),
        first_pass_approvals=approvals,
        first_pass_changes=changes,
        rounds_per_pr=(
            round(sum(r.rounds for r in reviewed) / len(reviewed), 2) if reviewed else 0.0
        ),
        escalations=sum(row.escalations for row in rows)
        + sum(1 for e in unattributed if e.get("event") == "escalation"),
        recurrences=sum(row.recurrences for row in rows),
        spend_blocks=sum(row.spend_blocks for row in rows)
        + sum(1 for e in unattributed if e.get("event") == "spend_block"),
        today_usd=round(today_usd, 4) if today_usd is not None else None,
        interventions_by_class=tuple(
            (name, counts[name]) for name in INTERVENTION_CLASSES
        ),
        interventions=interventions,
    )


def build_deployment(
    name: str,
    repo: str,
    feeds,
    ledger_events=(),
    now_fn=None,
    stale_after_s: float = STALE_AFTER_S,
    warnings: tuple[str, ...] = (),
    settled_today_usd: float | None = None,
    settled_today_unavailable: bool = False,
    to_local_date: Callable[[float], date] = _utc_date,
) -> Deployment:
    """Assemble one deployment's report. Pure: `feeds` is
    `(role, journal_path, log_path, events)` tuples already read off disk.
    """
    if now_fn is None:
        raise ValueError("build_deployment requires an injected clock")
    now = now_fn()
    health = tuple(
        _health(role, journal, log, events, now, stale_after_s)
        for role, journal, log, events in feeds
    )
    merged: list[dict] = []
    for role, _journal, _log, events in feeds:
        # The file itself is the role boundary. Persist that provenance into the
        # read model so generic events such as "action: comment issue" answer
        # who performed them.
        merged.extend({**event, "role": event.get("role") or role} for event in events)
    merged.sort(key=lambda e: parse_ts(e.get("ts")) or datetime.min.replace(tzinfo=timezone.utc))
    running = unsettled_start_events(merged, 86400.0, now_fn)
    rows = build_issue_rows(merged, running)
    pr_issue = pr_issue_map(merged)
    unattributed = tuple(e for e in merged if event_issue(e, pr_issue) is None)
    today = to_local_date(now.timestamp()).isoformat()
    today_usd = (
        None
        if settled_today_unavailable
        else (
            settled_today_usd
            if settled_today_usd is not None
            else sum(
                _float(e.get("cost_usd"))
                for e in ledger_events
                if e.get("event") == "spend"
                and (
                    (stamp := parse_ts(e.get("ts"))) is not None
                    and to_local_date(stamp.timestamp()).isoformat() == today
                )
                and (not repo or e.get("repo") == repo)
            )
        )
    )
    return Deployment(
        name=name,
        repo=repo,
        health=health,
        issues=rows,
        metrics=build_metrics(rows, unattributed, today_usd),
        unattributed=unattributed,
        warnings=warnings,
    )


def _health(role, journal_path, log_path, events, now, stale_after_s) -> DaemonHealth:
    timestamps = [ts for ts in (parse_ts(e.get("ts")) for e in events) if ts]
    last = max(timestamps) if timestamps else None
    last_event = ""
    if events:
        last_event = str(events[-1].get("event") or "")
    return DaemonHealth(
        role=role,
        journal_path=journal_path,
        log_path=log_path,
        events=len(events),
        last_ts=last,
        last_event=last_event,
        # Never having run is stale too: "this daemon has produced nothing" is
        # exactly the condition an operator is scanning the column for.
        stale=(last is None or (now - last).total_seconds() > stale_after_s),
    )


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def parse_ts(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _events_within(events, seconds: float, now_fn) -> list[dict]:
    cutoff = now_fn() - timedelta(seconds=seconds)
    return [
        event for event in events
        if (stamp := parse_ts(event.get("ts"))) is not None and stamp >= cutoff
    ]


def unsettled_start_events(events, window_s: float, now_fn) -> list[dict]:
    """Return agent starts still owed a settlement, grouped per dispatch."""
    scoped = _events_within(events, window_s, now_fn) if window_s > 0 else list(events)
    starts: dict[tuple, list[dict]] = {}
    settled: dict[tuple, int] = {}
    for event in scoped:
        event_name = event.get("event")
        if event_name not in ("agent_run_start", "agent_run"):
            continue
        dispatch = str(event.get("dispatch") or "")
        key = (
            ("dispatch", dispatch)
            if dispatch
            else ("context", str(event.get("kind", "")), _int(event.get("number")))
        )
        if event_name == "agent_run_start":
            starts.setdefault(key, []).append(event)
        else:
            settled[key] = settled.get(key, 0) + 1
    outstanding: list[dict] = []
    for key, start_events in starts.items():
        missing = (
            1
            if key[0] == "dispatch" and len(start_events) > settled.get(key, 0)
            else len(start_events) - settled.get(key, 0)
        )
        if missing > 0:
            outstanding.extend(start_events[-missing:])
    return outstanding


# ── issues board: stage track ─────────────────────────────────────────────────
# The board shows where an issue is on a fixed five-stage track and how much it
# churned there. One fold produces the ordered stage visits; the rail, its
# counts and the drawer history all read that one sequence, so they can never
# disagree about the same issue.
class Stage(str, Enum):
    TRIAGE = "triage"
    RESOLVE = "resolve"
    REVIEW = "review"
    CI = "ci"
    MERGE = "merge"


STAGE_ORDER: tuple[Stage, ...] = tuple(Stage)

# Synthetic step kinds the projection adds for observations that are not legs:
# one CI run per distinct PR head with observed checks, and the merge itself.
CI_RUN_KIND = "CI"
MERGE_KIND = "MERGE"

_STAGE_OF_KIND: Mapping[str, Stage] = {
    **{kind: Stage.RESOLVE for kind in ("RESOLVE", "RESUME", "ROUTE")},
    **{kind: Stage.REVIEW for kind in ("REVIEW", "RE_REVIEW", "ADDRESS_REVIEW")},
    MERGE_KIND: Stage.MERGE,
}
_RESOLVE_ATTEMPT_KINDS = frozenset({"RESOLVE", "RESUME"})


def stage_of_kind(kind: str) -> Stage | None:
    """The stage a step kind belongs to, or None when it inherits one.

    REBASE and PR_RESUME are repairs inside whatever stage is in progress, as
    are operator interventions and any kind this build does not know. A rebase
    is never a stage of its own: it adds a CI run (a new head), and it returns
    the issue to Review only when a reviewer runs again on the changed patch.

    CI runs and FIX_CI legs also inherit: CI fails and is fixed on every head,
    approved or not, and only an approval enters the CI stage (`stage_track`).
    """
    return _STAGE_OF_KIND.get(kind.strip().upper())


class StepOutcome(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    CHANGES_REQUESTED = "changes_requested"
    DONE = "done"
    RUNNING = "running"


@dataclass(frozen=True)
class TrackStep:
    """One thing that happened to an issue: a leg, an operator step, or an
    observation. `verdict` marks a review decision, which is what one review
    round means (a failed review run decides nothing)."""

    kind: str
    outcome: StepOutcome
    ts: datetime | None = None
    verdict: bool = False


@dataclass(frozen=True)
class TrackChip:
    """Consecutive identical steps (same kind, same result) folded into one."""

    kind: str
    outcome: StepOutcome
    count: int


@dataclass(frozen=True)
class StageVisit:
    stage: Stage
    chips: tuple[TrackChip, ...]

    @property
    def steps(self) -> int:
        return sum(chip.count for chip in self.chips)


@dataclass(frozen=True)
class StageTrack:
    visits: tuple[StageVisit, ...]
    current: Stage
    furthest: Stage
    resolve_attempts: int
    review_rounds: int
    ci_runs: int

    @property
    def steps(self) -> int:
        return sum(visit.steps for visit in self.visits)


_NEVER = datetime.max.replace(tzinfo=timezone.utc)


def stage_track(steps: Iterable[TrackStep]) -> StageTrack:
    """Fold an issue's steps into its ordered stage visits.

    Steps are ordered by timestamp; a step with no usable timestamp sorts
    after every stamped one and keeps its relative order. A step whose kind
    names no stage inherits the stage in progress (Triage before anything
    happened). A visit ends when the stage changes, so going back to an
    earlier stage, e.g. a CI fix whose new patch is reviewed again, starts a
    new visit rather than extending the old one.

    CI runs alongside review on every head, but the CI stage is what follows
    an approval: an approving review decision moves the stage in progress to
    CI, and a CI run or CI fix before the patch is approved stays in the stage
    in progress. Otherwise every PR whose checks fail before its first review
    would read as having been sent back from CI.

    Counts:
      resolve attempts — RESOLVE and RESUME runs, whatever their outcome;
      review rounds    — review decisions (`verdict` steps);
      CI runs          — one per PR head on which CI was observed (the
                         projection's CI steps). A journal with no head
                         observations still has its FIX_CI legs: each answered
                         a failed run, so they are counted instead.
    """
    ordered = sorted(
        steps,
        key=lambda step: _aware(step.ts) if isinstance(step.ts, datetime) else _NEVER,
    )
    visits: list[tuple[Stage, list[TrackChip]]] = []
    in_progress = Stage.TRIAGE
    reached = in_progress
    for step in ordered:
        kind = step.kind.strip().upper()
        in_progress = stage_of_kind(kind) or in_progress
        if not visits or visits[-1][0] is not in_progress:
            visits.append((in_progress, []))
        chips = visits[-1][1]
        if chips and (chips[-1].kind, chips[-1].outcome) == (step.kind, step.outcome):
            chips[-1] = TrackChip(step.kind, step.outcome, chips[-1].count + 1)
        else:
            chips.append(TrackChip(step.kind, step.outcome, 1))
        if step.verdict and step.outcome is StepOutcome.PASSED and kind in REVIEW_KINDS:
            in_progress = Stage.CI
        # An approval reaches CI even when a later re-review returns to
        # Review before any CI step lands there.
        reached = max(reached, in_progress, key=STAGE_ORDER.index)
    kinds = [step.kind.strip().upper() for step in ordered]
    folded = tuple(StageVisit(stage, tuple(chips)) for stage, chips in visits)
    return StageTrack(
        visits=folded,
        current=in_progress,
        furthest=max(
            (reached, *(visit.stage for visit in folded)),
            key=STAGE_ORDER.index,
        ),
        resolve_attempts=sum(kind in _RESOLVE_ATTEMPT_KINDS for kind in kinds),
        review_rounds=sum(step.verdict for step in ordered),
        ci_runs=kinds.count(CI_RUN_KIND) or kinds.count("FIX_CI"),
    )


def _aware(stamp: datetime) -> datetime:
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


# ── issues board: status ──────────────────────────────────────────────────────
# Every in-flight or queued issue is in exactly one of three groups. The
# variants below are the only shapes a row can take, so "escalated and
# working" or "blocked on nothing" cannot be expressed.
class Ask(str, Enum):
    REARM = "rearm"
    ESCALATED = "escalated"
    BUDGET = "budget"
    AWAITING_REPLY = "awaiting_reply"
    MERGE = "merge"
    STALLED = "stalled"


class Activity(str, Enum):
    AGENT = "agent"
    CI = "ci"
    MERGE = "merge"
    HANDOFF = "handoff"


@dataclass(frozen=True)
class OnIssue:
    # None when GitHub reports an open dependency without naming it.
    issue: int | None


@dataclass(frozen=True)
class OnPr:
    pr: int


@dataclass(frozen=True)
class OnOverlap:
    issue: int | None = None
    pr: int | None = None


TriageWhy = Literal["parked"]


@dataclass(frozen=True)
class OnTriage:
    why: TriageWhy


@dataclass(frozen=True)
class OnQueue:
    priority: str | None = None


BlockedOn = OnIssue | OnPr | OnOverlap | OnTriage | OnQueue


@dataclass(frozen=True)
class NeedsYou:
    ask: Ask
    headline: str
    reason: str
    since: datetime | None


@dataclass(frozen=True)
class Working:
    activity: Activity
    headline: str
    reason: str
    since: datetime | None


@dataclass(frozen=True)
class Blocked:
    on: BlockedOn
    headline: str
    reason: str
    since: datetime | None


BoardStatus = NeedsYou | Working | Blocked


# Facts the projection has already parsed and attributed. Each is a variant,
# not a flag: the decision below matches on shape.
@dataclass(frozen=True)
class Escalated:
    since: datetime | None
    # The harness's own escalation comment (first line), when one was paired.
    reason: str | None = None
    # A circuit breaker tripped: only an explicit re-arm grants another try.
    breaker: bool = False
    # The cap decision (`needs-budget-review`) is what resumes it.
    budget: bool = False


@dataclass(frozen=True)
class AwaitingReply:
    since: datetime | None
    question: str | None = None


Attention = Escalated | AwaitingReply


@dataclass(frozen=True)
class MergeReady:
    """GitHub would merge the open PR now: open, not a draft, mergeable,
    clean, approved, CI green or absent, no auto-merge armed. The same rule
    starts the human-wait clock, so the board and the clock agree."""

    since: datetime | None


# Who merges an approved, green PR: the fixer (`fix.harness_merges_branch`),
# GitHub's own auto-merge armed by someone else, or the operator.
MergeOwner = Literal["harness", "github", "operator"]


@dataclass(frozen=True)
class Hold:
    on: OnIssue | OnPr | OnOverlap
    since: datetime | None


@dataclass(frozen=True)
class TriageHold:
    why: TriageWhy
    since: datetime | None


@dataclass(frozen=True)
class StallObserved:
    since: datetime | None
    # The current deferral reason (#296) when a journalled defer gives one.
    deferral: str | None = None


@dataclass(frozen=True)
class BoardFacts:
    track: StageTrack
    attention: Attention | None = None
    triage: TriageHold | None = None
    running_kind: str | None = None
    hold: Hold | None = None
    # Current-head CI rollup of the open PR, None when unknown or no PR.
    ci: Literal["pending", "passing", "failing"] | None = None
    stall: StallObserved | None = None
    # An ai-ready issue with nothing in progress: it waits for the fixer.
    queued: bool = False
    priority: str | None = None
    merge_ready: MergeReady | None = None
    merge_owner: MergeOwner = "operator"
    # GitHub's review decision on the current head: False when it requires a
    # review or asks for changes, None when unread.
    github_approved: bool | None = None
    # An approving review is bound to the current head commit. A step that
    # can push (a rebase) does not always change the patch: the reviewer
    # carries its approval onto an unchanged patch without a paid leg.
    head_approved: bool = False
    # GitHub refuses the merge right now: draft, conflicting, behind or
    # blocked by a branch rule.
    merge_refused: bool = False
    last_activity: datetime | None = None


_RUNNING_HEADLINE: Mapping[str, str] = {
    "RESOLVE": "Resolving",
    "RESUME": "Resolving",
    "ROUTE": "Routing",
    "REVIEW": "Reviewing",
    "RE_REVIEW": "Reviewing",
    "ADDRESS_REVIEW": "Addressing review",
    "FIX_CI": "Fixing CI",
    "REBASE": "Rebasing",
    "PR_RESUME": "Resuming",
}

# Framing the harness puts around its own escalation comments
# (domain/lifecycle.py PrEscalation/IssueEscalation, domain/outcome.py budget
# review). The headline already says it, so the row's reason drops it.
_BREAKER_PREFIX = "Circuit breaker:"
_BREAKER_SUFFIX = "Escalating to a human."
_BUDGET_PREFIX = "Budget review needed:"


def _sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return text
    text = text[0].upper() + text[1:]
    return text if text.endswith((".", "?", "!")) else f"{text}."


def _short_reason(reason: str | None, *prefixes: str) -> str | None:
    """The harness message without the framing the headline already says."""
    if not reason or not reason.strip():
        return None
    text = reason.strip()
    for prefix in prefixes:
        if text.casefold().startswith(prefix.casefold()):
            text = text[len(prefix):].strip()
    if text.endswith(_BREAKER_SUFFIX):
        text = text[: -len(_BREAKER_SUFFIX)].strip()
    return _sentence(text) or None


# Steps that can push a new head: an approval before one of them was given to
# a different patch.
_PATCH_KINDS = RESOLVE_KINDS | REPAIR_KINDS | {"PR_RESUME"}


ReviewState = Literal["approved", "changes", "patched", "none"]


def _review_state(track: StageTrack) -> ReviewState:
    """The harness's newest review decision, or "patched" when a step that
    can push a new head ran after it: that decision was about a different
    patch."""
    patched = False
    for visit in reversed(track.visits):
        for chip in reversed(visit.chips):
            kind = chip.kind.strip().upper()
            if kind in _PATCH_KINDS:
                patched = True
            elif kind in REVIEW_KINDS and chip.outcome in {
                StepOutcome.PASSED,
                StepOutcome.CHANGES_REQUESTED,
            }:
                if patched:
                    return "patched"
                return "approved" if chip.outcome is StepOutcome.PASSED else "changes"
    return "none"


def _current_approval(facts: BoardFacts) -> bool:
    """No step since the newest approval may have changed the patch, or an
    approval is bound to the current head itself. The step kinds only say a
    head may have moved; head-bound evidence says what it was approved on."""
    state = _review_state(facts.track)
    return state != "patched" or facts.head_approved


def _approved(facts: BoardFacts) -> bool:
    """Whether the current patch is approved.

    A lifetime approval survives neither a CI fix's new head nor a rebase
    unless an approval is bound to the new head, and GitHub saying the
    current head still needs a review overrides it.
    """
    state = _review_state(facts.track)
    return facts.github_approved is not False and (
        state == "approved" or (state == "patched" and facts.head_approved)
    )


def _approved_line(track: StageTrack) -> str:
    rounds = track.review_rounds
    return f"Approved on review round {rounds}" if rounds > 0 else "Approved"


# Who merges, (when GitHub would merge now, when it has not confirmed yet).
_MERGER: Mapping[str, tuple[str, str]] = {
    "harness": ("The harness merges it next.", "The harness merges it once GitHub allows."),
    "github": ("GitHub merges it automatically.", "GitHub merges it automatically once its rules pass."),
}


def _merge_ready(facts: BoardFacts, ready: MergeReady) -> BoardStatus:
    """GitHub would merge it now: whoever owns the merge acts."""
    merger = _MERGER.get(facts.merge_owner)
    if merger is None:
        return NeedsYou(
            Ask.MERGE,
            "Merge the PR",
            "Approved and green. The harness does not merge this PR.",
            ready.since,
        )
    return Working(
        Activity.MERGE,
        "Ready to merge",
        f"{_approved_line(facts.track)} and green. {merger[0]}",
        ready.since,
    )


def _approved_waiting(facts: BoardFacts) -> Working:
    """Approved, but GitHub has not confirmed it would merge. Unread GitHub
    state never claims the operator's attention."""
    approved, since = _approved_line(facts.track), facts.last_activity
    if facts.merge_refused:
        return Working(Activity.HANDOFF, "Approved", f"{approved}. GitHub does not allow the merge yet.", since)
    if facts.ci == "failing":
        return Working(Activity.HANDOFF, "Approved", f"{approved}. CI failed on the current head.", since)
    merger = _MERGER.get(facts.merge_owner)
    if facts.ci == "passing" and merger is not None:
        return Working(Activity.MERGE, "Ready to merge", f"{approved} and green. {merger[1]}", since)
    return Working(Activity.HANDOFF, "Approved", f"{approved}. GitHub has not confirmed it can merge.", since)


def _needs_you(attention: Attention) -> NeedsYou:
    if isinstance(attention, AwaitingReply):
        return NeedsYou(
            Ask.AWAITING_REPLY,
            "Reply to the agent",
            _short_reason(attention.question)
            or "The agent asked a question. Reply on GitHub to unblock it.",
            attention.since,
        )
    if attention.budget:
        return NeedsYou(
            Ask.BUDGET,
            "Approve spend or close",
            _short_reason(attention.reason, _BUDGET_PREFIX)
            or "Spend reached its cap. Only a cap decision resumes it.",
            attention.since,
        )
    if attention.breaker:
        return NeedsYou(
            Ask.REARM,
            "Re-arm or close",
            _short_reason(attention.reason, _BREAKER_PREFIX)
            or "A circuit breaker holds it until you re-arm.",
            attention.since,
        )
    return NeedsYou(
        Ask.ESCALATED,
        "Answer the agent",
        _short_reason(attention.reason) or "Escalated. See the issue comments.",
        attention.since,
    )


def _working_reason(track: StageTrack, kind: str) -> str:
    if kind in REVIEW_KINDS:
        return f"Review round {track.review_rounds + 1}."
    if kind == "ADDRESS_REVIEW":
        return f"Review round {max(track.review_rounds, 1)} asked for changes."
    if kind == "FIX_CI":
        return f"CI run {max(track.ci_runs, 1)} failed."
    if kind in _RESOLVE_ATTEMPT_KINDS:
        return f"Attempt {max(track.resolve_attempts, 1)}."
    if kind == "REBASE":
        return "Bringing the branch up to date with its base."
    return "An agent is working on it."


def _handoff(facts: BoardFacts) -> Working:
    """Nothing is running, but the next move is the harness's."""
    track, since = facts.track, facts.last_activity
    last = track.visits[-1].chips[-1] if track.visits else None
    kind = last.kind.strip().upper() if last is not None else ""
    if last is not None and last.outcome is StepOutcome.FAILED:
        name = kind.replace("_", " ").lower()
        return Working(Activity.HANDOFF, "Retrying", f"The last step ({name}) failed; the harness tries again.", since)
    if last is not None and last.outcome is StepOutcome.CHANGES_REQUESTED:
        return Working(Activity.HANDOFF, "Changes requested", f"Review round {track.review_rounds} asked for changes.", since)
    if facts.github_approved is False:
        return Working(Activity.HANDOFF, "Awaiting review", "GitHub requires a review on the current head.", since)
    if last is not None and _approved(facts):
        return _approved_waiting(facts)
    if last is not None and (kind in RESOLVE_KINDS | REPAIR_KINDS or kind == CI_RUN_KIND):
        return Working(Activity.HANDOFF, "Awaiting review", "The reviewer picks it up next.", since)
    return Working(Activity.HANDOFF, "Up next", "The harness picks it up on its next tick.", since)


def _blocked_on(hold: Hold) -> Blocked:
    on = hold.on
    if isinstance(on, OnIssue) and on.issue is None:
        return Blocked(on, "Waiting on a dependency", "Starts when its open dependency closes.", hold.since)
    if isinstance(on, OnIssue):
        return Blocked(on, f"Waiting for #{on.issue}", f"Starts when #{on.issue} merges.", hold.since)
    if isinstance(on, OnPr):
        return Blocked(on, f"Waiting for PR #{on.pr}", f"Starts when PR #{on.pr} merges.", hold.since)
    target = (
        f"#{on.issue}" if on.issue is not None
        else f"PR #{on.pr}" if on.pr is not None
        else None
    )
    if target is None:
        return Blocked(on, "Waiting on an overlap", "Its files overlap work in flight. Starts when that merges.", hold.since)
    return Blocked(on, f"Waiting for {target}", f"Its files overlap {target}. Starts when that merges.", hold.since)


def board_status(facts: BoardFacts) -> BoardStatus:
    """Which group a row belongs to, and the one line that says why.

    Precedence, first match wins:
      1. parked: the operator moved it out of the pipeline on purpose;
      2. escalated or awaiting a reply: nothing moves until a person acts,
         even while a leg is still running (that leg cannot clear the hold);
      3. a leg is running: a reviewer re-reading a new head outranks the
         approval it may withdraw;
      4. GitHub would merge it now (`MergeReady`) and no patch step ran
         since the harness's newest review: Needs you when the operator
         merges, Working when the harness or GitHub's auto-merge does;
      5. a hold on another issue or PR, or a file overlap;
      6. CI is running on the current head;
      7. stalled: no leg, hold or escalation, yet nothing changed for a whole
         stall threshold, so the harness stopped without escalating;
      8. queued for the fixer;
      9. otherwise the harness owns the next move.
    """
    triage = facts.triage
    if triage is not None:
        return Blocked(
            OnTriage("parked"),
            "Waiting on triage",
            "Parked for human review. Unpark it to hand it back to the harness.",
            triage.since,
        )
    if facts.attention is not None:
        return _needs_you(facts.attention)
    if facts.running_kind is not None:
        kind = facts.running_kind.strip().upper()
        return Working(
            Activity.AGENT,
            _RUNNING_HEADLINE.get(kind, "Agent running"),
            _working_reason(facts.track, kind),
            facts.last_activity,
        )
    if facts.merge_ready is not None and _current_approval(facts):
        return _merge_ready(facts, facts.merge_ready)
    if facts.hold is not None:
        return _blocked_on(facts.hold)
    if facts.ci == "pending":
        reason = (
            f"{_approved_line(facts.track)}."
            if _approved(facts)
            else f"Run {max(facts.track.ci_runs, 1)} on the current head."
        )
        return Working(Activity.CI, "CI running", reason, facts.last_activity)
    if facts.stall is not None:
        deferral = (facts.stall.deferral or "").strip()
        lead = f"Deferred: {deferral}. " if deferral else "Nothing is moving it. "
        return NeedsYou(
            Ask.STALLED,
            "Investigate the stall",
            lead + "The harness stopped without escalating.",
            facts.stall.since,
        )
    if facts.queued:
        priority = f" ({facts.priority})" if facts.priority else ""
        return Blocked(
            OnQueue(facts.priority),
            "Queued",
            f"Starts when the fixer picks it up{priority}.",
            None,
        )
    return _handoff(facts)


OVERSPEND_ALARM_RATIO = 2.0


def overspend_ratio(cost_usd: object, budget_usd: object) -> float | None:
    """Spend over the projected cost, only when it is a row-level alarm.

    The threshold is deliberately 2x: at 1.5x it flagged most of a live board,
    which is noise rather than a signal. Unknown, non-positive or non-finite
    figures have no ratio.
    """
    cost, budget = _finite(cost_usd), _finite(budget_usd)
    if cost is None or budget is None or budget <= 0:
        return None
    ratio = cost / budget
    # An overflowing quotient is not a figure anyone can act on, and JSON
    # cannot carry it.
    if not math.isfinite(ratio) or ratio < OVERSPEND_ALARM_RATIO:
        return None
    return round(ratio, 2)


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:  # an int beyond float range is no usable figure
        return None
    return number if math.isfinite(number) else None
