"""Pure dashboard lifecycle, attribution, and metric model.

The model consumes already-parsed journal events. It knows nothing about files,
processes, HTTP, GitHub, or rendering.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping

from .events import (
    ActionApplied,
    AgentRunCompleted,
    AgentRunStallCorrected,
    AgentRunStarted,
    DaemonStateChanged,
    EscalationRaised,
    EventEnvelope,
    LevelChanged,
    RecurrenceDetected,
    SpendBlocked,
    SpendRecorded,
    WorldObserved,
    JsonValue,
    encode_event,
)
from .labels import LIFECYCLE_LABELS
from .projection import ReadModel

# The three role daemons, in the order `journal_paths` returns their journals.
ROLE_NAMES = ("fix", "review", "steward")

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
STEWARD_KINDS = frozenset({"TRIAGE", "STEWARD_DRIVE", "JIRA_SYNC"})
# Kinds whose `number` is an ISSUE; everything else numbers a PR.
ISSUE_NUMBERED = RESOLVE_KINDS | STEWARD_KINDS


# ── action-desc parsing ───────────────────────────────────────────────────────
# `Executor` journals applied actions as a human one-liner (`{"event":"action",
# "desc":"relabel issue #7: -ai-ready +ai-working"}`) — there are no structured
# fields to read. Parsing the sentence back is the only way to attribute those
# events to an issue, so the grammar lives here, in one place, table-tested.
_RE_RELABEL = re.compile(r"^relabel (issue|pr) #(\d+): (.*)$")
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
    m = _RE_RELABEL.match(desc)
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

    @property
    def first_pass_rate(self) -> float | None:
        total = self.first_pass_approvals + self.first_pass_changes
        return (self.first_pass_approvals / total) if total else None


@dataclass(frozen=True)
class Deployment:
    name: str
    repo: str
    health: tuple[DaemonHealth, ...]
    issues: tuple[IssueRow, ...]  # newest activity first
    metrics: Metrics
    unattributed: tuple[dict, ...]  # events naming no issue (daily caps, ticks)
    schema_version: int = 1
    warnings: tuple[str, ...] = ()


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


def build_deployment_v2(
    name: str,
    repo: str,
    feeds: Iterable[
        tuple[str, str, str, tuple[EventEnvelope, ...]]
    ],
    model: ReadModel,
    now_fn,
    stale_after_s: float = STALE_AFTER_S,
    heartbeat_times: Mapping[str, datetime] | None = None,
    warnings: tuple[str, ...] = (),
    settled_today_usd: float | None = None,
    settled_today_unavailable: bool = False,
) -> Deployment:
    """Build the dashboard exclusively from the canonical schema-v2 fold.

    GitHub observations enter through ``WorldObserved``. No current GitHub read,
    prose parsing, journal-tail inference, or legacy event name participates in
    lifecycle state.
    """

    now = now_fn()
    feed_rows = tuple(feeds)
    heartbeats = heartbeat_times or {}
    health = tuple(
        _v2_health(
            role,
            path,
            log,
            events,
            now,
            stale_after_s,
            heartbeats.get(role),
        )
        for role, path, log, events in feed_rows
    )
    pr_issue = _v2_pr_issue(model)
    grouped: dict[int, list[EventEnvelope]] = {}
    unattributed_events: list[EventEnvelope] = []
    for event in model.events:
        issue = _v2_event_issue(event, pr_issue)
        if issue is None:
            unattributed_events.append(event)
        else:
            grouped.setdefault(issue, []).append(event)

    historical_rows = tuple(
        sorted(
            (
                _v2_issue_row(issue, events, model, pr_issue)
                for issue, events in grouped.items()
            ),
            key=_row_sort_key,
            reverse=True,
        )
    )
    rows = tuple(
        row
        for row in historical_rows
        if row.facts.get("present") is True or row.done
    )
    today = now.astimezone(timezone.utc).date()
    today_usd = (
        None
        if settled_today_unavailable
        else (
            settled_today_usd
            if settled_today_usd is not None
            else sum(
                payload.amount_micros / 1_000_000
                for event in model.events
                if event.occurred_at.astimezone(timezone.utc).date() == today
                and isinstance((payload := event.payload), SpendRecorded)
            )
        )
    )
    unattributed = tuple(
        _dashboard_event_view(event, model)
        for event in unattributed_events
    )
    return Deployment(
        name=name,
        repo=repo,
        health=health,
        issues=rows,
        metrics=build_metrics(historical_rows, unattributed, today_usd),
        unattributed=unattributed,
        schema_version=2,
        warnings=warnings,
    )


def _v2_health(
    role: str,
    path: str,
    log_path: str,
    events: tuple[EventEnvelope, ...],
    now: datetime,
    stale_after_s: float,
    heartbeat_at: datetime | None = None,
) -> DaemonHealth:
    ordered = sorted(events, key=lambda event: event.order_key)
    last_event_at = ordered[-1].occurred_at if ordered else None
    candidates = tuple(
        stamp for stamp in (last_event_at, heartbeat_at) if stamp is not None
    )
    last = max(candidates) if candidates else None
    daemon_states = tuple(
        event.payload
        for event in ordered
        if isinstance(event.payload, DaemonStateChanged)
    )
    explicitly_stopped = (
        bool(daemon_states) and daemon_states[-1].state == "stopped"
    )
    return DaemonHealth(
        role=role,
        journal_path=path,
        log_path=log_path,
        events=len(events),
        last_ts=last,
        last_event=ordered[-1].type if ordered else "",
        stale=(
            explicitly_stopped
            or last is None
            or (now - last).total_seconds() > stale_after_s
        ),
    )


def _v2_pr_issue(model: ReadModel) -> dict[int, int]:
    result: dict[int, int] = {}
    for event in model.events:
        correlation = event.correlation
        if correlation.pr is not None and correlation.issue is not None:
            result[correlation.pr] = correlation.issue
    for (kind, number), facts in model.world_facts.items():
        if kind != "pr" or facts.get("present") is False:
            continue
        closing = facts.get("closing_issue")
        if isinstance(closing, int) and closing > 0:
            result[number] = closing
    return result


def _v2_event_issue(
    event: EventEnvelope, pr_issue: dict[int, int]
) -> int | None:
    correlation = event.correlation
    if correlation.issue is not None:
        return correlation.issue
    if correlation.pr is not None:
        return pr_issue.get(correlation.pr)
    payload = event.payload
    if isinstance(payload, WorldObserved):
        if payload.subject_kind == "issue":
            return payload.subject_number
        if payload.subject_kind == "pr":
            return pr_issue.get(payload.subject_number)
    if isinstance(payload, EscalationRaised) and payload.target == "issue":
        return payload.number
    return None


def _v2_issue_row(
    issue: int,
    events: list[EventEnvelope],
    model: ReadModel,
    pr_issue: dict[int, int],
) -> IssueRow:
    ordered = sorted(events, key=lambda event: event.order_key)
    run_ids = {
        event.correlation.run_id
        for event in ordered
        if event.correlation.run_id
    }
    legs: list[RunSummary] = []
    running: list[RunningLeg] = []
    for run_id in run_ids:
        run = model.runs.get(run_id)
        if run is None:
            continue
        start_payload = (
            run.start.payload if run.start is not None else None
        )
        completion_payload = (
            run.completion.payload if run.completion is not None else None
        )
        if isinstance(completion_payload, AgentRunCompleted):
            correlation = run.completion.correlation
            legs.append(
                RunSummary(
                    ts=run.completion.occurred_at,
                    role=run.completion.producer.role,
                    kind=completion_payload.kind,
                    number=correlation.pr or correlation.issue,
                    issue=issue,
                    model=completion_payload.model,
                    status=completion_payload.status,
                    exit_reason=completion_payload.exit_reason or "",
                    duration_s=completion_payload.duration_ms / 1000,
                    cost_usd=(completion_payload.cost_micros or 0) / 1_000_000,
                    estimated=completion_payload.pricing_basis
                    == "learned_estimate",
                    session_id=correlation.session_id or "",
                )
            )
        elif run.abandoned is None and isinstance(
            start_payload, AgentRunStarted
        ):
            assert run.start is not None
            correlation = run.start.correlation
            running.append(
                RunningLeg(
                    ts=run.start.occurred_at,
                    role=run.start.producer.role,
                    kind=start_payload.kind,
                    number=correlation.pr or correlation.issue,
                    issue=issue,
                    model=start_payload.model,
                )
            )

    legs.sort(key=lambda leg: leg.ts or datetime.min.replace(tzinfo=timezone.utc))
    running.sort(
        key=lambda leg: leg.ts or datetime.min.replace(tzinfo=timezone.utc)
    )
    facts = _v2_issue_facts(issue, ordered, model)
    labels_raw = facts.get("labels", ())
    labels = (
        {str(label) for label in labels_raw}
        if isinstance(labels_raw, (tuple, list, set, frozenset))
        else set()
    )
    done = str(facts.get("state") or "").casefold() == "closed" or "ai-done" in labels
    stage = _v2_stage(ordered, tuple(running), tuple(legs), labels, done)
    lane = _v2_lane(facts, labels, done)
    prs = tuple(
        sorted(
            {
                pr
                for pr, linked_issue in pr_issue.items()
                if linked_issue == issue
            }
            | {
                event.correlation.pr
                for event in ordered
                if event.correlation.pr is not None
            }
        )
    )
    review_legs = [leg for leg in legs if leg.kind in REVIEW_KINDS]
    spend_events = [
        event.payload
        for event in ordered
        if isinstance(event.payload, SpendRecorded)
    ]
    cost_usd = (
        sum(payload.amount_micros for payload in spend_events) / 1_000_000
        if spend_events
        else sum(leg.cost_usd for leg in legs)
    )
    return IssueRow(
        issue=issue,
        stage=stage,
        cost_usd=round(cost_usd, 4),
        legs=tuple(legs),
        running=() if done else tuple(running),
        rounds=len(review_legs),
        approvals=sum(leg.status == "APPROVE" for leg in review_legs),
        changes_requested=sum(
            leg.status == "REQUEST_CHANGES" for leg in review_legs
        ),
        escalations=sum(
            isinstance(event.payload, EscalationRaised) for event in ordered
        ),
        recurrences=sum(
            isinstance(event.payload, RecurrenceDetected) for event in ordered
        ),
        spend_blocks=sum(
            isinstance(event.payload, SpendBlocked) for event in ordered
        ),
        prs=prs,
        models=tuple(
            dict.fromkeys(
                leg.model
                for leg in (*legs, *running)
                if leg.model
            )
        ),
        last_ts=ordered[-1].occurred_at if ordered else None,
        done=done,
        events=tuple(
            _dashboard_event_view(event, model) for event in ordered
        ),
        facts=facts,
        lane=lane,
    )


def _v2_issue_facts(
    issue: int,
    ordered: list[EventEnvelope],
    model: ReadModel,
) -> dict[str, JsonValue]:
    """Keep terminal facts for history after the live world tombstones a row."""

    current = dict(model.world_facts.get(("issue", issue), {}))
    if current.get("present") is True:
        return current
    historical: dict[str, JsonValue] = {}
    for event in ordered:
        payload = event.payload
        if (
            isinstance(payload, WorldObserved)
            and payload.subject_kind == "issue"
            and payload.subject_number == issue
            and payload.facts.get("present") is not False
        ):
            historical.update(payload.facts)
    historical.update(current)
    historical["present"] = False
    return historical


def _dashboard_event_view(
    event: EventEnvelope,
    model: ReadModel,
) -> dict[str, JsonValue]:
    """Typed event plus presentation fields; never a lifecycle/state fold."""

    encoded = encode_event(event)
    payload = event.payload
    correlation = event.correlation
    view: dict[str, JsonValue] = {
        **encoded,
        "ts": encoded["occurred_at"],
        "role": event.producer.role,
        **{
            key: value
            for key, value in {
                "issue": correlation.issue,
                "pr": correlation.pr,
                "dispatch": correlation.dispatch_id,
                "run_id": correlation.run_id,
                "leg_id": correlation.leg_id,
                "session_id": correlation.session_id,
            }.items()
            if value is not None
        },
    }
    data = dict(encoded["data"])
    view.update(data)
    if isinstance(payload, AgentRunStarted):
        view.update(dict(payload.attributes or {}))
        view["event"] = "agent_run_start"
        view["number"] = correlation.pr or correlation.issue
    elif isinstance(payload, AgentRunCompleted):
        view.update(dict(payload.attributes or {}))
        view["event"] = "agent_run"
        view["number"] = correlation.pr or correlation.issue
        view["duration_s"] = payload.duration_ms / 1000
        view["cost_usd"] = (
            payload.cost_micros / 1_000_000
            if payload.cost_micros is not None
            else None
        )
        view["usage"] = (
            dict(payload.usage) if payload.usage is not None else None
        )
    elif isinstance(payload, ActionApplied):
        structured = dict(payload.attributes or {})
        structured.setdefault("type", payload.kind)
        structured.setdefault("target", payload.target.upper())
        if payload.number is not None:
            structured.setdefault("number", payload.number)
        view.update(
            {
                "event": "action",
                "desc": payload.display,
                "action": structured,
            }
        )
    elif isinstance(payload, EscalationRaised):
        view["event"] = "escalation"
    elif isinstance(payload, SpendBlocked):
        view["event"] = "spend_block"
    elif isinstance(payload, SpendRecorded):
        view["event"] = "spend"
        view["cost_usd"] = payload.amount_micros / 1_000_000
    elif isinstance(payload, LevelChanged):
        attributes = dict(payload.attributes or {})
        view.update(attributes)
        view["event"] = "deferred"
        if (
            "blocker_pr" not in attributes
            and "blocker_issue" not in attributes
        ):
            view["blocker"] = (
                payload.state_key.rsplit(":", 1)[-1] or None
            )
    elif isinstance(payload, AgentRunStallCorrected):
        corrected = next(
            (
                candidate
                for candidate in model.events
                if candidate.event_id == payload.corrects
            ),
            None,
        )
        view.update(
            {
                "event": "agent_run_stall",
                "stall_started_at": (
                    corrected.occurred_at.isoformat()
                    if corrected is not None
                    else event.occurred_at.isoformat()
                ),
                "suspended_seconds": payload.suspended_ms / 1000,
            }
        )
    else:
        view["event"] = event.type
    return view


def _v2_lane(
    facts: Mapping[str, JsonValue],
    labels: set[str],
    done: bool,
) -> str:
    if done:
        return "done"
    if facts.get("present") is False:
        return "historical"
    if (
        "ai-ready" in labels
        and labels.isdisjoint({"ai-blocked", "needs-human-review"})
    ):
        return "pending"
    lifecycle = {label.value for label in LIFECYCLE_LABELS}
    if (
        str(facts.get("state") or "").casefold() == "open"
        and labels.isdisjoint(lifecycle)
    ):
        return "backlog"
    return "in_flight"


def _v2_stage(
    events: list[EventEnvelope],
    running: tuple[RunningLeg, ...],
    legs: tuple[RunSummary, ...],
    labels: set[str],
    done: bool,
) -> str:
    if done:
        return "done"
    if "needs-human-review" in labels:
        return "escalated"
    if "ai-blocked" in labels:
        return "blocked"
    if running:
        return _STAGE_RUNNING.get(
            running[-1].kind,
            f"running {running[-1].kind or 'agent'}",
        )
    if any(isinstance(event.payload, RecurrenceDetected) for event in events):
        return "recurred"
    if any(isinstance(event.payload, SpendBlocked) for event in events):
        return "blocked (spend)"
    if legs:
        leg = legs[-1]
        if leg.exit_reason:
            return "run failed"
        if leg.status == "blocked":
            return "awaiting human reply"
        if leg.kind in REVIEW_KINDS:
            if leg.status == "APPROVE":
                return "awaiting merge"
            if leg.status == "REQUEST_CHANGES":
                return "changes requested"
            return "reviewed"
        if leg.kind in RESOLVE_KINDS | REPAIR_KINDS:
            return "awaiting review"
    for label, stage in (
        ("ai-pr-open", "pr open"),
        ("ai-working", "working"),
        ("ai-ready", "queued"),
    ):
        if label in labels:
            return stage
    return "unknown"


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
    "TRIAGE": "triaging",
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
        kind = str(e.get("kind") or "")
        status = str(e.get("status") or "")
        if e.get("is_error"):
            return "run failed"
        if status == "blocked":
            return "awaiting human reply"
        if kind in REVIEW_KINDS:
            if status == "APPROVE":
                return "awaiting merge"
            if status == "REQUEST_CHANGES":
                return "changes requested"
            return "reviewed"
        if kind in RESOLVE_KINDS or kind in REPAIR_KINDS:
            return "awaiting review"
        return ""
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
    reviews = [leg for leg in legs if leg.kind in REVIEW_KINDS]
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
        approvals=sum(1 for leg in reviews if leg.status == "APPROVE"),
        changes_requested=sum(1 for leg in reviews if leg.status == "REQUEST_CHANGES"),
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
        duration_s=_float(e.get("duration_s")),
        # The BILLED figure, exactly as `JournalingRuntime` computes it for the
        # ledger: the true cost when there is one, else the token estimate. A
        # dashboard that showed $0.00 for every aborted run would understate the
        # one leg family most worth watching.
        cost_usd=cost if cost > 0 else estimated,
        estimated=cost <= 0 and estimated > 0,
        session_id=str(e.get("session_id") or ""),
    )


def _running_leg(e: dict) -> RunningLeg:
    return RunningLeg(
        ts=parse_ts(e.get("ts")),
        role=str(e.get("role") or ""),
        kind=str(e.get("kind") or "?"),
        number=_int(e.get("number")),
        issue=_int(e.get("issue")),
        model=str(e.get("model") or ""),
    )


def first_pass_verdicts(rows) -> tuple[int, int]:
    """(approvals, changes-requested) over the FIRST review verdict per PR.

    Per PR, not per review: the named guardrail is "did the first review of this
    PR approve it", so later rounds — which are the *consequence* of a first-pass
    rejection — must not be counted again or a bad PR reviewed four times reads
    as three-quarters healthy.
    """
    first: dict[tuple[int, int | None], str] = {}
    for row in rows:
        for leg in row.legs:
            if leg.kind not in REVIEW_KINDS or leg.status not in (
                "APPROVE", "REQUEST_CHANGES"
            ):
                continue
            first.setdefault((row.issue, leg.number), leg.status)
    verdicts = list(first.values())
    return (
        verdicts.count("APPROVE"),
        verdicts.count("REQUEST_CHANGES"),
    )


def build_metrics(
    rows,
    unattributed,
    today_usd: float | None,
) -> Metrics:
    legs = [leg for row in rows for leg in row.legs]
    legs += [
        _leg(e) for e in unattributed if e.get("event") == "agent_run"
    ]
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
    today = _stamp_date(now)
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
                and str(e.get("ts", ""))[:10] == today
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




def _stamp_date(now: datetime) -> str:
    return now.astimezone(timezone.utc).date().isoformat()


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
