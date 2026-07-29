"""Journal-side collectors: deployments, liveness, frecency, issue cards.

Every number here comes out of `app/dashboard.py` — `collect` reads the files,
`build_deployment` derives the rows, and this module only reshapes them into the
JSON the contract names. Nothing is re-derived: a stage, a cost or a leg that
disagreed with `issue-agent dashboard` would be a second truth.

This module is pure: adapters collect snapshots and the application service
passes them here for projection into API payloads.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
import math

from ...domain import dashboard as dash

# Frecency windows: (seconds, weight). An event inside 24h counts in all three,
# which is the intended pile-up — a repo that moved this morning must outrank a
# repo that moved a fortnight ago even if the fortnight-old one has more events.
FRECENCY_WINDOWS = ((86400.0, 3.0), (7 * 86400.0, 1.0), (30 * 86400.0, 0.25))

# The lifecycle labels the harness owns. An open issue carrying none of them has
# never been offered to the harness — that is the backlog.
LIFECYCLE_LABELS = frozenset(
    {"ai-ready", "ai-working", "ai-done", "ai-blocked", "needs-human-review"}
)
PRIORITY_LABELS = ("P1", "P2", "P3")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(ts: datetime | None) -> str | None:
    return ts.replace(microsecond=0).isoformat() if ts else None


# ── frecency ──────────────────────────────────────────────────────────────────
def frecency_score(timestamps, now: datetime) -> float:
    """Recency-weighted event count. Pure over already-parsed timestamps."""
    stamps = [ts for ts in timestamps if ts is not None]
    return sum(
        weight * sum(1 for ts in stamps if 0 <= (now - ts).total_seconds() <= span)
        for span, weight in FRECENCY_WINDOWS
    )


def normalise_frecency(scores: dict[str, float]) -> dict[str, float]:
    """Scale to the busiest deployment. All-zero stays all-zero rather than
    dividing by zero and calling an idle fleet fully active."""
    top = max(scores.values(), default=0.0)
    if top <= 0:
        return {name: 0.0 for name in scores}
    return {name: round(score / top, 4) for name, score in scores.items()}


def health_json(h: dash.DaemonHealth, alive: bool | None) -> dict:
    return {
        "role": h.role,
        "alive": alive,
        "stale": h.stale,
        "last_event_ts": iso(h.last_ts),
        "last_event": h.last_event,
        "events": h.events,
        "journal_path": h.journal_path,
        "log_path": h.log_path,
    }


def repos_payload(
    snapshots,
    live: dict[str, set[str]],
    scan_ok: bool,
    now: datetime,
    self_repo: str = "",
) -> dict:
    """`GET /api/repos`, pre-sorted by frecency then last activity."""
    raw = {s.name: frecency_score(s.timestamps, now) for s in snapshots}
    scores = normalise_frecency(raw)
    rows = []
    for s in snapshots:
        health = s.deployment.health
        if scan_ok:
            roles_up = live.get(s.name)
            alive = {h.role: (roles_up is not None and h.role in roles_up) for h in health}
            active = s.name in live
        else:
            # No /proc: a daemon that journalled recently is the best available
            # evidence it is ticking. Announced in the payload, never silent.
            alive = {h.role: (not h.stale) for h in health}
            active = any(alive.values())
        rows.append(
            {
                "name": s.name,
                "repo": s.repo,
                "target_dir": s.target_dir,
                "is_self": bool(s.repo) and s.repo == self_repo,
                "active": active,
                "roles": [health_json(h, alive[h.role]) for h in health],
                "frecency": scores.get(s.name, 0.0),
                "last_activity_ts": iso(s.last_activity),
                "in_flight": s.deployment.metrics.in_flight_issues,
                "done": s.deployment.metrics.done_issues,
            }
        )
    rows.sort(key=lambda r: (r["frecency"], r["last_activity_ts"] or ""), reverse=True)
    payload = {"repos": rows, "warnings": []}
    if not scan_ok:
        payload["liveness"] = "journal-heuristic"
        payload["warnings"].append(
            "process scan unavailable — daemon liveness inferred from journal staleness"
        )
    return payload


# ── legs → steps ──────────────────────────────────────────────────────────────
def leg_state(leg: dash.Leg) -> str:
    """A settled leg is `failed` when it exited for any reason other than a
    clean finish. `status` is the AGENT's claim; `exit_reason` is the
    envelope's, and only the envelope can see a run that died."""
    return "failed" if leg.exit_reason not in ("", "ok") else "ok"


def collapse_steps(row: dash.IssueRow) -> list[dict]:
    """The card's compact step strip: one entry per kind, newest state winning,
    with the kind's costs SUMMED (three FIX_CI attempts cost three legs' money,
    and a strip that showed only the last one would understate repair spend).
    Running legs are appended last, in dispatch order."""
    collapsed: dict[str, dict] = {}
    for leg in row.legs:
        entry = collapsed.get(leg.kind)
        cost = round((entry["cost_usd"] if entry else 0.0) + leg.cost_usd, 4)
        collapsed[leg.kind] = {
            "kind": leg.kind,
            "state": leg_state(leg),
            "status": leg.status or None,
            "cost_usd": cost,
        }
    steps = list(collapsed.values())
    steps.extend(
        {"kind": r.kind, "state": "running", "status": None, "cost_usd": 0.0}
        for r in row.running
    )
    return steps


def detailed_steps(row: dash.IssueRow) -> list[dict]:
    """One row per leg, ts order, settled first then anything still running.
    Not collapsed: the drill-down's whole job is showing the retries."""
    steps = [
        {
            "idx": idx,
            "kind": leg.kind,
            "role": leg.role,
            "ts": iso(leg.ts),
            "state": leg_state(leg),
            "status": leg.status or None,
            "exit_reason": leg.exit_reason or None,
            "duration_s": leg.duration_s,
            "cost_usd": leg.cost_usd,
            "estimated": leg.estimated,
            "model": _model_for_leg(row, leg),
            "session_id": leg.session_id or None,
            "turns": _turns(row, leg),
            "usage": _usage(row, leg),
            "has_transcript": bool(leg.session_id),
        }
        for idx, leg in enumerate(row.legs)
    ]
    steps.extend(
        {
            "idx": len(steps) + i,
            "kind": r.kind,
            "role": r.role,
            "ts": iso(r.ts),
            "state": "running",
            "status": None,
            "exit_reason": None,
            "duration_s": None,
            "cost_usd": 0.0,
            "estimated": False,
            "model": r.model or None,
            "session_id": None,
            "turns": None,
            "usage": None,
            "has_transcript": False,
        }
        for i, r in enumerate(row.running)
    )
    return steps


def _settled_event(row: dash.IssueRow, leg: dash.Leg) -> dict:
    """The raw `agent_run` a Leg was built from — `Leg` keeps only what the
    aggregation needs, and the drill-down also wants turns/usage."""
    for e in row.events:
        if (
            e.get("event") == "agent_run"
                and dash.parse_ts(e.get("ts")) == leg.ts
            and str(e.get("kind") or "?") == leg.kind
        ):
            return e
    return {}


def _turns(row: dash.IssueRow, leg: dash.Leg):
    return _settled_event(row, leg).get("turns")


def _usage(row: dash.IssueRow, leg: dash.Leg):
    usage = _settled_event(row, leg).get("usage")
    return usage if isinstance(usage, dict) else None


def _model_for_leg(row: dash.IssueRow, leg: dash.Leg) -> str | None:
    """Configured model for a settled task, including old journal shapes.

    Some runtimes recorded the model on ``agent_run_start`` but not on the
    matching settlement. Dispatch identity is authoritative; the kind/number
    fallback covers pre-dispatch journals without borrowing a later task's
    model.
    """
    settled = _settled_event(row, leg)
    if settled.get("model"):
        return str(settled["model"])
    dispatch = str(settled.get("dispatch") or "")
    starts = [
        event
        for event in row.events
        if event.get("event") == "agent_run_start"
        and (
            (dispatch and str(event.get("dispatch") or "") == dispatch)
            or (
                not dispatch
                and event.get("kind") == leg.kind
                and event.get("number") == leg.number
                and (
                    leg.ts is None
                    or (stamp := dash.parse_ts(event.get("ts"))) is None
                    or stamp <= leg.ts
                )
            )
        )
        and event.get("model")
    ]
    return str(starts[-1]["model"]) if starts else None


# ── issue cards ───────────────────────────────────────────────────────────────
def _started(row: dash.IssueRow) -> datetime | None:
    stamps = [ts for ts in (dash.parse_ts(e.get("ts")) for e in row.events) if ts]
    return min(stamps) if stamps else None


def _legacy_suspended_seconds(
    intervals: list[tuple[datetime, datetime, float]],
) -> float:
    """Deduplicate corrections written before stable stall identities existed."""
    if not intervals:
        return 0.0
    ordered = sorted(intervals)
    _, current_end, current_seconds = ordered[0]
    total = 0.0
    for started_at, ended_at, seconds in ordered[1:]:
        if started_at <= current_end:
            current_end = max(current_end, ended_at)
            current_seconds = max(current_seconds, seconds)
            continue
        total += current_seconds
        current_end, current_seconds = ended_at, seconds
    return total + current_seconds


def _identified_suspended_seconds(corrections: list[tuple[float, float]]) -> float:
    """Take one maximum correction per physical clock-offset cluster."""
    if not corrections:
        return 0.0
    ordered = sorted(corrections)
    previous_offset, current_seconds = ordered[0]
    total = 0.0
    for offset, seconds in ordered[1:]:
        # Journal wall stamps are integral seconds while monotonic is sampled
        # just after them. Separate daemons can therefore report the same boot
        # offset about one second apart.
        if offset - previous_offset <= 2.0:
            current_seconds = max(current_seconds, seconds)
        else:
            total += current_seconds
            current_seconds = seconds
        previous_offset = offset
    return total + current_seconds


def _suspended_seconds(row: dash.IssueRow, max_seconds: float) -> float:
    """Valid suspension corrections, counted once across role detectors."""
    intervals: list[tuple[datetime, datetime, float]] = []
    identified: list[tuple[float, float]] = []
    for event in row.events:
        if event.get("event") != "agent_run_stall":
            continue
        started_at = dash.parse_ts(event.get("stall_started_at"))
        if started_at is None:
            continue
        try:
            seconds = float(event.get("suspended_seconds") or 0)
        except (TypeError, ValueError):
            continue
        if (
            not math.isfinite(seconds)
            or seconds <= 0
            or seconds > max(0.0, max_seconds)
        ):
            continue
        try:
            clock_offset = float(event.get("stall_clock_offset_seconds"))
        except (TypeError, ValueError):
            clock_offset = math.nan
        if math.isfinite(clock_offset):
            identified.append((clock_offset, seconds))
            continue
        try:
            ended_at = started_at + timedelta(seconds=seconds)
        except OverflowError:
            continue
        intervals.append((started_at, ended_at, seconds))
    return (
        _identified_suspended_seconds(identified)
        + _legacy_suspended_seconds(intervals)
    )


def _hold_event(row: dash.IssueRow) -> dict:
    holds = [e for e in row.events if e.get("event") == "hold"]
    return holds[-1] if holds else {}


def _flags(row: dash.IssueRow) -> list[str]:
    flags = []
    if row.escalations:
        flags.append("escalated")
    if row.recurrences:
        flags.append("recurred")
    if row.spend_blocks:
        flags.append("spend-blocked")
    if _hold_event(row):
        flags.append("held")
    return flags


def mean_wall_clock_s(rows) -> float | None:
    """Mean first-event→last-event seconds over DONE issues; None with none.
    The ETA a card projects is only ever this repo's own history."""
    spans = []
    for row in rows:
        if not row.done:
            continue
        start, last = _started(row), row.last_ts
        if start and last:
            wall_seconds = (last - start).total_seconds()
            spans.append(
                max(0.0, wall_seconds - _suspended_seconds(row, wall_seconds))
            )
    return round(sum(spans) / len(spans), 1) if spans else None


def issue_card(
    row: dash.IssueRow,
    now: datetime,
    *,
    gh_issue: dict | None = None,
    gh_prs: dict | None = None,
    projected_cost_usd: float | None = None,
    projected_eta_s: float | None = None,
) -> dict:
    started = _started(row)
    pr = row.prs[-1] if row.prs else None
    gh_pr = (gh_prs or {}).get(pr) if pr is not None else None
    # In flight: the clock is still running, so elapsed is measured to NOW.
    # Done: to the last thing that happened, which is where the clock stopped.
    end = row.last_ts if (row.done and row.last_ts) else now
    wall_seconds = (end - started).total_seconds() if started else None
    card = {
        "issue": row.issue,
        "title": (gh_issue or {}).get("title"),
        "url": (gh_issue or {}).get("url"),
        "pr": pr,
        "pr_url": (gh_pr or {}).get("url"),
        "pr_state": _pr_state(gh_pr),
        "stage": row.stage,
        "done": row.done,
        "steps": collapse_steps(row),
        "cost_usd": row.cost_usd,
        "estimated": any(leg.estimated for leg in row.legs),
        "projected_cost_usd": projected_cost_usd,
        "started_ts": iso(started),
        "last_ts": iso(row.last_ts),
        "elapsed_s": (
            round(
                max(
                    0.0,
                    wall_seconds - _suspended_seconds(row, wall_seconds),
                )
            )
            if wall_seconds is not None
            else None
        ),
        "projected_eta_s": projected_eta_s,
        "models": list(row.models),
        "rounds": row.rounds,
        "flags": _flags(row),
    }
    if row.done and gh_pr and gh_pr.get("mergedAt"):
        card["merged_ts"] = gh_pr["mergedAt"]
    return card


def _pr_state(gh_pr: dict | None) -> str | None:
    if not gh_pr:
        return None
    if gh_pr.get("mergedAt"):
        return "merged"
    state = str(gh_pr.get("state") or "").lower()
    return state or None


def pending_card(issue: dict, row: dash.IssueRow | None) -> dict:
    labels = labels_of(issue)
    hold = _hold_event(row) if row else {}
    return {
        "issue": int(issue.get("number") or 0),
        "title": issue.get("title"),
        "url": issue.get("url"),
        "priority": priority_of(labels),
        "labels": labels,
        "held": bool(hold),
        "hold_reason": (hold.get("reason") or hold.get("desc")) if hold else None,
        "overlap": None,
    }


def backlog_card(
    issue: dict,
    now: datetime,
    row: dash.IssueRow | None = None,
) -> dict:
    labels = labels_of(issue)
    recurrence_count = max(row.recurrences if row else 0, int("recurrence" in labels))
    return {
        "issue": int(issue.get("number") or 0),
        "title": issue.get("title"),
        "url": issue.get("url"),
        "body": issue.get("body"),
        "labels": labels,
        "recurrence": recurrence_count > 0,
        "recurrence_count": recurrence_count,
        "age_days": age_days(issue.get("createdAt"), now),
        "comments": _comment_count(issue.get("comments")),
    }


def _comment_count(value) -> int:
    if isinstance(value, list):
        return len(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def labels_of(issue: dict) -> list[str]:
    """gh renders labels as objects; a hand-built fixture may use strings."""
    out = []
    for label in issue.get("labels") or []:
        if isinstance(label, dict):
            name = label.get("name")
        else:
            name = label
        if name:
            out.append(str(name))
    return out


def priority_of(labels) -> str | None:
    for p in PRIORITY_LABELS:
        if p in labels:
            return p
    return None


def is_backlog(issue: dict) -> bool:
    return not (LIFECYCLE_LABELS & set(labels_of(issue)))


def age_days(created_at, now: datetime) -> int | None:
    ts = dash.parse_ts(created_at)
    return max(0, int((now - ts).total_seconds() // 86400)) if ts else None


# ── overview ──────────────────────────────────────────────────────────────────
def cost_mix(metrics: dash.Metrics) -> dict:
    """Spend split into the four families the Direction section tracks. Kinds in
    none of them land nowhere by design — `by_kind` already reports them
    verbatim, and inventing a fifth bucket would hide a new leg family."""
    families = {
        "resolve_usd": dash.RESOLVE_KINDS,
        "review_usd": dash.REVIEW_KINDS,
        "repair_usd": dash.REPAIR_KINDS,
        "steward_usd": dash.STEWARD_KINDS,
    }
    return {
        field: round(
            sum(cost for kind, cost, _n in metrics.by_kind if kind in kinds), 4
        )
        for field, kinds in families.items()
    }


def health_trend(
    issues,
    recurrence_events,
    *,
    first_observed_day: str | None,
    local_tz: tzinfo | None = None,
) -> list[dict]:
    """Daily target-repository issue health, using GitHub dates and journal recurrences.

    Creation and confirmed closure timestamps are GitHub facts; recurrences are
    journal facts. Keeping those sources explicit lets a failed GitHub read
    degrade to the recurrence signal instead of manufacturing lifecycle data.
    """
    days: dict[str, dict] = {}

    def row_for(value) -> dict | None:
        stamp = dash.parse_ts(value)
        if stamp is None:
            return None
        day = stamp.astimezone(local_tz or timezone.utc).date().isoformat()
        return days.setdefault(
            day,
            {"day": day, "opened": 0, "closed": 0, "net_open": 0,
             "recurrences": 0, "first_observation": False},
        )

    for issue in issues if isinstance(issues, list) else ():
        if not isinstance(issue, dict):
            continue
        if row := row_for(issue.get("createdAt")):
            row["opened"] += 1
        if row := row_for(issue.get("closedAt")):
            row["closed"] += 1
    for event in recurrence_events:
        if isinstance(event, dict) and event.get("event") == "recurrence":
            if row := row_for(event.get("ts")):
                row["recurrences"] += 1
    for row in days.values():
        row["net_open"] = row["opened"] - row["closed"]
        row["first_observation"] = row["day"] == first_observed_day
    return [days[day] for day in sorted(days)]


def first_observed_day(
    snapshot: dash.Snapshot,
    local_tz: tzinfo | None = None,
) -> str | None:
    """Native local day this deployment first journalled.

    Runtime and review-mode history is not journalled, so classifying historical
    timestamps with today's configuration would manufacture provenance.
    """
    timestamps = [stamp for stamp in snapshot.timestamps if stamp is not None]
    if not timestamps:
        return None
    return min(timestamps).astimezone(local_tz or timezone.utc).date().isoformat()


def metrics_json(metrics: dash.Metrics) -> dict:
    return {
        "done_issues": metrics.done_issues,
        "in_flight_issues": metrics.in_flight_issues,
        "total_cost_usd": metrics.total_cost_usd,
        "cost_per_done_usd": metrics.cost_per_done_usd,
        "first_pass_approvals": metrics.first_pass_approvals,
        "first_pass_changes": metrics.first_pass_changes,
        "first_pass_rate": metrics.first_pass_rate,
        "rounds_per_pr": metrics.rounds_per_pr,
        "escalations": metrics.escalations,
        "recurrences": metrics.recurrences,
        "spend_blocks": metrics.spend_blocks,
        "today_usd": metrics.today_usd,
        "by_kind": [
            {"kind": kind, "usd": cost, "runs": runs}
            for kind, cost, runs in metrics.by_kind
        ],
    }


def overview_payload(
    snapshot: dash.Snapshot,
    live: dict[str, set[str]],
    scan_ok: bool,
    issue_cap_usd: float | None = None,
    daily_cap_usd: float | None = None,
) -> dict:
    dep = snapshot.deployment
    if scan_ok:
        roles_up = live.get(snapshot.name)
        alive = {h.role: (roles_up is not None and h.role in roles_up) for h in dep.health}
    else:
        alive = {h.role: (not h.stale) for h in dep.health}
    return {
        "metrics": metrics_json(dep.metrics),
        "cost_mix": cost_mix(dep.metrics),
        "caps": {
            "issue_cap_usd": issue_cap_usd,
            "daily_cap_usd": daily_cap_usd,
            "today_usd": dep.metrics.today_usd,
        },
        "health": [health_json(h, alive[h.role]) for h in dep.health],
        "unattributed": list(dep.unattributed[-20:]),
        "warnings": [] if scan_ok else ["daemon liveness inferred from journal staleness"],
    }


# ── issue lookup ──────────────────────────────────────────────────────────────
def find_row(snapshot: dash.Snapshot, issue: int) -> dash.IssueRow | None:
    for row in snapshot.deployment.issues:
        if row.issue == issue:
            return row
    return None


def issue_detail(
    row: dash.IssueRow,
    now: datetime,
    gh_issue: dict | None = None,
    gh_prs: dict[int, dict] | None = None,
) -> dict:
    started = _started(row)
    end = row.last_ts if (row.done and row.last_ts) else now
    return {
        "issue": row.issue,
        "title": (gh_issue or {}).get("title"),
        "url": (gh_issue or {}).get("url"),
        "stage": row.stage,
        "done": row.done,
        "prs": list(row.prs),
        "pull_requests": [
            {
                "number": number,
                "url": (gh_prs or {}).get(number, {}).get("url"),
                "state": _pr_state((gh_prs or {}).get(number)),
            }
            for number in row.prs
        ],
        "cost_usd": row.cost_usd,
        "elapsed_s": round((end - started).total_seconds()) if started else None,
        "steps": detailed_steps(row),
        "events": list(row.events),
        "warnings": [],
    }


def step_window_events(row: dash.IssueRow, idx: int) -> list[dict]:
    """Every event of this issue between the step's dispatch and its settlement
    (or now, for a running leg), inclusive.

    The journal has no per-leg correlation for non-`agent_run` events, so the
    window is the honest approximation: what happened on this issue while that
    leg was the live one.
    """
    steps = detailed_steps(row)
    if idx < 0 or idx >= len(steps):
        return []
    settled = dash.parse_ts(steps[idx]["ts"])
    if settled is None:
        return []
    running = steps[idx]["state"] == "running"
    dispatches = [
        ts
        for ts in (
            dash.parse_ts(e.get("ts"))
            for e in row.events
            if e.get("event") == "agent_run_start"
        )
        if ts is not None
    ]
    # A running step's `ts` IS its dispatch; a settled one's dispatch is the
    # newest start at or before it.
    opened = settled if running else max(
        (ts for ts in dispatches if ts <= settled), default=settled
    )
    return [
        e
        for e in row.events
        if (ts := dash.parse_ts(e.get("ts"))) is not None
        and opened <= ts
        and (running or ts <= settled)
    ]
