"""Operator dashboard — every deployment, every issue, one read-only view.

`issue-agent status` answers "is this ONE deployment alive, and what did it
spend today?". The question an operator actually asks in the morning is wider:
across every repo the harness is pointed at, what merged, what is still moving,
what is stuck, and where did the money go. This module is that aggregation.

**Journal-only truth (v1 limitation, deliberate).** Every number here is derived
from files the harness itself wrote — the per-role journals
(`<STATE_DIR>/journal.jsonl`) and the global spend ledger. Nothing calls `gh`.
So the dashboard cannot see a label a human moved by hand, a PR someone merged
in the UI, or an issue closed outside the harness: it reports what the harness
DID, not what GitHub currently says. Two consequences worth stating out loud:

  * "done" means the harness applied `+ai-done` or cleared state for a merged
    PR. A PR merged by a human is invisible here until some role journals about
    it.
  * a stage is a *heuristic* read of the latest events (see `stage_for`), not a
    state machine. It is a debugging aid, not an authority.

Adding `gh` would make the view authoritative and also make it slow, rate
limited, and capable of failing — a diagnostic that fails when things are going
wrong is worse than one that is honest about its scope. That trade is revisited
when a real operator question needs live PR state, not before.

Everything below is pure over the parsed events except `collect` (which reads
the files) and `serve` (which binds a socket). The same aggregation renders as
text and as a self-contained HTML snapshot, so the two can never disagree.
"""

from __future__ import annotations

import glob
import html as _html
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from ..adapters.journal import (
    parse_ts,
    read_events,
    unsettled_start_events,
)

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
         `+ai-blocked`                         → "blocked"
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
        if kind in REVIEW_KINDS:
            if status == "APPROVE":
                return "awaiting merge"
            if status == "REQUEST_CHANGES":
                return "changes requested"
            return "reviewed"
        if e.get("is_error"):
            return "run failed"
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
    done = any(
        f is not None
        and ((f.verb == "clear" and f.merged) or "ai-done" in f.added)
        for f in facts
    )
    timestamps = [ts for ts in (parse_ts(e.get("ts")) for e in events) if ts]
    return IssueRow(
        issue=issue,
        stage=stage_for(events, running),
        cost_usd=round(sum(leg.cost_usd for leg in legs), 4),
        legs=legs,
        running=running,
        rounds=len(reviews),
        approvals=sum(1 for leg in reviews if leg.status == "APPROVE"),
        changes_requested=sum(1 for leg in reviews if leg.status == "REQUEST_CHANGES"),
        escalations=sum(1 for e in events if e.get("event") == "escalation"),
        recurrences=sum(1 for e in events if e.get("event") == "recurrence"),
        spend_blocks=sum(1 for e in events if e.get("event") == "spend_block"),
        prs=prs,
        models=tuple(dict.fromkeys(leg.model for leg in legs if leg.model)),
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
    for _role, _journal, _log, events in feeds:
        merged.extend(events)
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


# ── collection (the only I/O) ─────────────────────────────────────────────────
def deployment_names(repos_dir: str) -> tuple[str, ...]:
    """Every overlay in `config/repos/` — one deployment each, sorted. A missing
    directory is zero deployments, not an error (a fresh install has none)."""
    return tuple(
        sorted(
            os.path.splitext(os.path.basename(p))[0]
            for p in glob.glob(os.path.join(repos_dir, "*.env"))
        )
    )


def collect(
    name: str,
    repo: str,
    journal_paths,
    log_dir: str,
    ledger_path: str,
    now_fn=None,
    stale_after_s: float = STALE_AFTER_S,
) -> Deployment:
    """Read one deployment's journals + the global ledger and report on them.
    Read-only; every missing file is an empty history (`read_events`'s rule)."""
    feeds = tuple(
        (role, path, os.path.join(log_dir, f"{role}-{name}.log"), read_events(path))
        for role, path in zip(ROLE_NAMES, list(journal_paths))
    )
    return build_deployment(
        name, repo, feeds, read_events(ledger_path), now_fn, stale_after_s
    )


# ── text render ───────────────────────────────────────────────────────────────
def render_text(deployments, now_fn=None, details: bool = False) -> str:
    now = (now_fn or _utcnow)()
    out: list[str] = [
        f"▸ issue-agent dashboard — {len(list(deployments))} deployment(s) "
        f"@ {now.replace(microsecond=0).isoformat()}"
    ]
    for dep in deployments:
        out.append("")
        out.append(f"═══ {dep.repo or '(repo unknown)'}   [{dep.name}]")
        out.append("  daemons:")
        for h in dep.health:
            mark = "STALE" if h.stale else "ok   "
            out.append(
                f"    {h.role:<8} {mark}  last {_ago(h.last_ts, now)}"
                f"  {h.last_event or '(no events)':<16} {h.events:>5} events"
            )
            out.append(f"             journal {h.journal_path}")
            out.append(f"             log     {h.log_path}")
        out.extend(_metrics_lines(dep.metrics))
        in_flight = [r for r in dep.issues if not r.done]
        done = [r for r in dep.issues if r.done]
        out.extend(_rows_lines("in flight", in_flight, now))
        out.extend(_rows_lines("done", done, now))
        if details:
            for row in dep.issues:
                out.append(f"  ── #{row.issue} events ──")
                for e in row.events:
                    out.append(f"    {json.dumps(e, sort_keys=True)}")
    return "\n".join(out)


def _metrics_lines(m: Metrics) -> list[str]:
    rate = m.first_pass_rate
    lines = [
        "  metrics:",
        f"    done {m.done_issues} · in flight {m.in_flight_issues} · "
        f"spend ${m.total_cost_usd:.2f} · "
        + (
            f"${m.cost_per_done_usd:.2f} per done issue"
            if m.done_issues
            else "no done issue yet"
        ),
        f"    today (ledger, this repo): ${m.today_usd:.2f}",
        "    cost mix: "
        + (
            " · ".join(
                f"{kind} ${cost:.2f}"
                f" ({(cost / m.total_cost_usd * 100 if m.total_cost_usd else 0):.0f}%, {n})"
                for kind, cost, n in m.by_kind
            )
            or "(no runs)"
        ),
        "    first-pass approval: "
        + (
            f"{m.first_pass_approvals}/{m.first_pass_approvals + m.first_pass_changes}"
            f" ({rate * 100:.0f}%)"
            if rate is not None
            else "(no review verdicts)"
        )
        + f" · rounds/PR {m.rounds_per_pr}",
        f"    escalations {m.escalations} · recurrences {m.recurrences} · "
        f"spend blocks {m.spend_blocks}",
    ]
    return lines


def _rows_lines(title: str, rows, now) -> list[str]:
    if not rows:
        return []
    out = [f"  {title} ({len(rows)}):"]
    for row in rows:
        running = (
            "  ← " + ", ".join(f"{r.kind} since {_ago(r.ts, now)}" for r in row.running)
            if row.running
            else ""
        )
        prs = ("PR " + ",".join(f"#{p}" for p in row.prs)) if row.prs else "no PR"
        out.append(
            f"    #{row.issue:<6} {row.stage:<18} ${row.cost_usd:>8.2f}  "
            f"{len(row.legs):>2} legs  {row.rounds} review(s)  {prs:<16} "
            f"{_ago(row.last_ts, now)}{running}"
        )
        flags = _flags(row)
        if flags:
            out.append(f"           {flags}")
    return out


def _flags(row: IssueRow) -> str:
    parts = []
    if row.escalations:
        parts.append(f"{row.escalations} escalation(s)")
    if row.spend_blocks:
        parts.append(f"{row.spend_blocks} spend block(s)")
    if row.recurrences:
        parts.append(f"{row.recurrences} recurrence(s)")
    errored = [leg for leg in row.legs if leg.exit_reason not in ("", "ok")]
    if errored:
        parts.append("exits: " + ",".join(sorted({leg.exit_reason for leg in errored})))
    if row.models:
        parts.append("models: " + ",".join(row.models))
    return " · ".join(parts)


# ── HTML render ───────────────────────────────────────────────────────────────
_CSS = """
:root { color-scheme: light dark; }
body { font: 14px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace;
       margin: 2rem auto; max-width: 74rem; padding: 0 1rem; }
h1 { font-size: 1.3rem; } h2 { font-size: 1.1rem; margin-top: 2.2rem; }
h3 { font-size: .95rem; margin: 1.4rem 0 .4rem; text-transform: uppercase;
     letter-spacing: .06em; opacity: .7; }
table { border-collapse: collapse; width: 100%; margin: .3rem 0 1rem; }
th, td { text-align: left; padding: .28rem .5rem; border-bottom: 1px solid #8883;
         vertical-align: top; }
th { font-weight: 600; opacity: .75; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.stale { color: #b00; font-weight: 600; }
.ok { opacity: .6; }
.flag { color: #a60; }
details { margin: .2rem 0 .6rem; }
pre { overflow-x: auto; background: #8881; padding: .5rem; font-size: 12px; }
.meta { opacity: .65; font-size: 12px; }
"""


def render_html(deployments, now_fn=None) -> str:
    now = (now_fn or _utcnow)()
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>issue-agent dashboard</title>",
        f"<style>{_CSS}</style></head><body>",
        "<h1>issue-agent dashboard</h1>",
        f"<p class='meta'>generated {_esc(now.replace(microsecond=0).isoformat())}"
        " · journal-only truth (no GitHub calls) · stages are heuristic</p>",
    ]
    for dep in deployments:
        parts.append(
            f"<h2>{_esc(dep.repo or '(repo unknown)')} "
            f"<span class='meta'>[{_esc(dep.name)}]</span></h2>"
        )
        parts.append(_html_metrics(dep.metrics))
        parts.append("<h3>daemons</h3>")
        parts.append(
            _table(
                ("role", "state", "last event", "when", "events", "journal", "log"),
                [
                    (
                        h.role,
                        "<span class='stale'>STALE</span>" if h.stale
                        else "<span class='ok'>ok</span>",
                        _esc(h.last_event or "—"),
                        _esc(_ago(h.last_ts, now)),
                        str(h.events),
                        f"<span class='meta'>{_esc(h.journal_path)}</span>",
                        f"<span class='meta'>{_esc(h.log_path)}</span>",
                    )
                    for h in dep.health
                ],
            )
        )
        in_flight = [r for r in dep.issues if not r.done]
        done = [r for r in dep.issues if r.done]
        parts.append(f"<h3>in flight ({len(in_flight)})</h3>")
        parts.append(_html_rows(in_flight, now))
        parts.append(f"<h3>done ({len(done)})</h3>")
        parts.append(_html_rows(done, now))
    parts.append("</body></html>")
    return "\n".join(parts)


def _html_metrics(m: Metrics) -> str:
    rate = m.first_pass_rate
    mix = " · ".join(f"{kind} ${cost:.2f} ({n})" for kind, cost, n in m.by_kind)
    rows = [
        ("done / in flight", f"{m.done_issues} / {m.in_flight_issues}"),
        ("spend (journal, all time)", f"${m.total_cost_usd:.2f}"),
        (
            "$ per done issue",
            f"${m.cost_per_done_usd:.2f}" if m.done_issues else "—",
        ),
        ("today (ledger, this repo)", f"${m.today_usd:.2f}"),
        ("cost mix by leg", _esc(mix) or "—"),
        (
            "first-pass approval",
            f"{m.first_pass_approvals}/{m.first_pass_approvals + m.first_pass_changes}"
            f" ({rate * 100:.0f}%)" if rate is not None else "—",
        ),
        ("rounds per reviewed PR", str(m.rounds_per_pr)),
        (
            "escalations / recurrences / spend blocks",
            f"{m.escalations} / {m.recurrences} / {m.spend_blocks}",
        ),
    ]
    body = "".join(f"<tr><th>{_esc(k)}</th><td>{v}</td></tr>" for k, v in rows)
    return f"<table>{body}</table>"


def _html_rows(rows, now) -> str:
    if not rows:
        return "<p class='meta'>(none)</p>"
    out = [
        "<table><tr><th>issue</th><th>stage</th><th>cost</th><th>legs</th>"
        "<th>reviews</th><th>PRs</th><th>last</th><th>notes</th></tr>"
    ]
    for row in rows:
        running = ", ".join(
            f"{r.kind} since {_ago(r.ts, now)}" for r in row.running
        )
        note = _flags(row)
        if running:
            note = f"running: {running}" + (f" · {note}" if note else "")
        out.append(
            "<tr>"
            f"<td>#{row.issue}</td>"
            f"<td>{_esc(row.stage)}</td>"
            f"<td class='num'>${row.cost_usd:.2f}</td>"
            f"<td class='num'>{len(row.legs)}</td>"
            f"<td class='num'>{row.rounds}</td>"
            f"<td>{_esc(', '.join('#' + str(p) for p in row.prs)) or '—'}</td>"
            f"<td>{_esc(_ago(row.last_ts, now))}</td>"
            f"<td class='flag'>{_esc(note)}</td>"
            "</tr>"
        )
        out.append(
            f"<tr><td colspan='8'>{_details(row)}</td></tr>"
        )
    out.append("</table>")
    return "".join(out)


def _details(row: IssueRow) -> str:
    legs = "".join(
        f"<tr><td>{_esc(_iso(leg.ts))}</td><td>{_esc(leg.role)}</td>"
        f"<td>{_esc(leg.kind)}</td><td>{_esc(leg.model or '—')}</td>"
        f"<td>{_esc(leg.status or '—')}</td><td>{_esc(leg.exit_reason or '—')}</td>"
        f"<td class='num'>{leg.duration_s:.0f}s</td>"
        f"<td class='num'>${leg.cost_usd:.2f}{'*' if leg.estimated else ''}</td>"
        f"<td class='meta'>{_esc(leg.session_id or '—')}</td></tr>"
        for leg in row.legs
    )
    raw = _esc("\n".join(json.dumps(e, sort_keys=True) for e in row.events))
    return (
        f"<details><summary>#{row.issue} drill-down "
        f"({len(row.legs)} legs, {len(row.events)} events)</summary>"
        "<table><tr><th>ts</th><th>role</th><th>leg</th><th>model</th>"
        "<th>status</th><th>exit</th><th>dur</th><th>cost</th>"
        "<th>session</th></tr>"
        f"{legs}</table>"
        f"<pre>{raw}</pre></details>"
    )


# ── serving ───────────────────────────────────────────────────────────────────
def serve(port: int, build_html, host: str = "127.0.0.1") -> None:
    """Serve a freshly regenerated snapshot on every GET, forever (Ctrl-C stops).

    `build_html` is called per request rather than once at startup so a browser
    refresh shows current state; the harness's own poll loop is the update
    frequency. Bound to loopback by default — this view names issue titles, log
    paths and session ids, none of which belong on a LAN by accident.
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — stdlib's name
            try:
                body = build_html().encode("utf-8")
            except Exception as exc:  # noqa: BLE001 — a broken read is a 500, not a crash
                body = f"dashboard error: {_html.escape(str(exc))}".encode("utf-8")
                self.send_response(500)
            else:
                self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):  # keep the terminal for the dashboard
            pass

    HTTPServer((host, port), Handler).serve_forever()


# ── small helpers ─────────────────────────────────────────────────────────────
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _stamp_date(now: datetime) -> str:
    return now.replace(microsecond=0).isoformat()[:10]


def _iso(ts: datetime | None) -> str:
    return ts.replace(microsecond=0).isoformat() if ts else "—"


def _ago(ts: datetime | None, now: datetime) -> str:
    if ts is None:
        return "never"
    seconds = (now - ts).total_seconds()
    if seconds < 0:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return f"{int(seconds)}s ago"


def _esc(value) -> str:
    return _html.escape(str(value))


def _table(headers, rows) -> str:
    head = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows
    )
    return f"<table><tr>{head}</tr>{body}</table>"


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


__all__ = [
    "ActionFact",
    "DaemonHealth",
    "Deployment",
    "IssueRow",
    "Leg",
    "Metrics",
    "RunningLeg",
    "STALE_AFTER_S",
    "build_deployment",
    "build_issue_rows",
    "build_metrics",
    "collect",
    "deployment_names",
    "event_issue",
    "first_pass_verdicts",
    "parse_action",
    "pr_issue_map",
    "render_html",
    "render_text",
    "serve",
    "stage_for",
]
