"""Pure dashboard lifecycle, attribution, and metric model.

The model consumes already-parsed journal events. It knows nothing about files,
processes, HTTP, GitHub, or rendering.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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
class Leg:
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
    legs: tuple[Leg, ...]
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
    today_usd: float  # from the global ledger, scoped to this repo

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
    "TRIAGE": "triaging",
}


def stage_for(events, running: tuple[RunningLeg, ...] = ()) -> str:
    """Best-effort lifecycle stage for one issue.

    Rules, in priority order:

      1. a leg is running now → that leg's stage ("resolving", "reviewing",
         "fixing CI", …). What the harness is doing beats what it last did.
      2. otherwise the newest event that matches one of:
         `+ai-done` / a merged `clear state`   → "done"
         `escalation` / `+needs-human-review`  → "escalated"
         `recurrence`                          → "recurred"
         `spend_block`                         → "blocked (spend)"
         `hold`                                → "held"
         `+ai-blocked`                         → "awaiting human reply"
         review run APPROVE                    → "awaiting merge"
         review run REQUEST_CHANGES            → "changes requested"
         errored run                           → "run failed"
         resolve/repair run settled            → "awaiting review"
         `+ai-working` / `+ai-pr-open`         → "working" / "pr open"
         `+ai-ready`                           → "queued"
      3. nothing matched → "unknown".
    """
    if running:
        kind = running[-1].kind
        return _STAGE_RUNNING.get(kind, f"running {kind or 'agent'}".strip())
    for e in reversed(list(events)):
        stage = _stage_of_event(e)
        if stage:
            return stage
    return "unknown"


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


def _leg(e: dict) -> Leg:
    cost = _float(e.get("cost_usd"))
    estimated = _float(e.get("cost_estimated_usd"))
    return Leg(
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


def build_metrics(rows, unattributed, today_usd: float) -> Metrics:
    legs = [leg for row in rows for leg in row.legs]
    legs += [
        _leg(e) for e in unattributed if e.get("event") == "agent_run"
    ]
    by_kind: dict[str, tuple[float, int]] = {}
    for leg in legs:
        cost, n = by_kind.get(leg.kind, (0.0, 0))
        by_kind[leg.kind] = (cost + leg.cost_usd, n + 1)
    total = sum(leg.cost_usd for leg in legs)
    done = [row for row in rows if row.done]
    approvals, changes = first_pass_verdicts(rows)
    reviewed = [row for row in rows if row.rounds]
    return Metrics(
        done_issues=len(done),
        in_flight_issues=sum(1 for row in rows if not row.done),
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
        today_usd=round(today_usd, 4),
    )


def build_deployment(
    name: str,
    repo: str,
    feeds,
    ledger_events=(),
    now_fn=None,
    stale_after_s: float = STALE_AFTER_S,
) -> Deployment:
    """Assemble one deployment's report. Pure: `feeds` is
    `(role, journal_path, log_path, events)` tuples already read off disk.
    """
    now_fn = now_fn or _utcnow
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
    today_usd = sum(
        _float(e.get("cost_usd"))
        for e in ledger_events
        if e.get("event") == "spend"
        and str(e.get("ts", ""))[:10] == today
        and (not repo or e.get("repo") == repo)
    )
    return Deployment(
        name=name,
        repo=repo,
        health=health,
        issues=rows,
        metrics=build_metrics(rows, unattributed, today_usd),
        unattributed=unattributed,
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




def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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


def unsettled_start_events(events, window_s: float = 86400.0, now_fn=_utcnow) -> list[dict]:
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
