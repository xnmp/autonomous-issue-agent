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
import hashlib
import json
import math

from ...domain import dashboard as dash
from ...domain.actions import AgentKind
from ...domain.identity import is_bot_login, login_eq

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
_AGENT_KINDS = frozenset(kind.name for kind in AgentKind)
_DISPATCH_AGENT_KINDS = _AGENT_KINDS - {"ROUTE"}
_PR_AGENT_KINDS = dash.REVIEW_KINDS | dash.REPAIR_KINDS | {"PR_RESUME"}


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
            "effort": _settled_event(row, leg).get("effort"),
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
            "effort": next(
                (
                    event.get("effort")
                    for event in reversed(row.events)
                    if event.get("event") == "agent_run_start"
                    and event.get("kind") == r.kind
                    and event.get("number") == r.number
                ),
                None,
            ),
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


def _dispatch_supersedes_legacy_hold(event: dict) -> bool:
    if event.get("event") == "agent_run_start":
        return str(event.get("kind") or "").upper() in {"ROUTE", "RESOLVE", "RESUME"}
    return (
        event.get("event") == "action"
        and str(event.get("desc") or "").lower().startswith("dispatch ")
    )


def _defer_key(event: dict) -> tuple:
    return (
        event.get("issue"),
        event.get("reason"),
        event.get("blocker_pr"),
        event.get("blocker_issue"),
    )


def _deferred_event(row: dash.IssueRow) -> dict:
    """The newest active structured defer, with a historical-journal fallback."""
    active: dict[tuple, dict] = {}
    legacy: dict = {}
    for event in row.events:
        if event.get("event") == "deferred":
            # Transition-aware state supersedes any level-only record emitted
            # by an older daemon, including after the matching clear.
            legacy = {}
            key = _defer_key(event)
            if event.get("state") == "cleared":
                active.pop(key, None)
            elif event.get("state") == "waiting":
                active[key] = event
        elif event.get("event") == "hold":
            legacy = event
        elif legacy and _dispatch_supersedes_legacy_hold(event):
            legacy = {}
    return list(active.values())[-1] if active else legacy


def waiting_label(event: dict) -> str | None:
    if not event:
        return None
    if event.get("blocker_pr") is not None:
        return f"waiting on PR #{event['blocker_pr']}"
    if event.get("blocker_issue") is not None:
        return f"waiting on #{event['blocker_issue']}"
    if event.get("event") == "hold":
        blocker = str(event.get("blocker") or "")
        if blocker.lower().startswith("pr#"):
            return f"waiting on PR #{blocker[3:]}"
    return "waiting on dependency"


def _overlap_seconds(
    start: datetime,
    end: datetime,
    intervals: list[tuple[datetime, datetime]],
) -> float:
    return sum(
        max(0.0, (min(end, idle_end) - max(start, idle_start)).total_seconds())
        for idle_start, idle_end in intervals
    )


def _legacy_suspension_partition(
    intervals: list[tuple[datetime, datetime, float]],
    idle_intervals: list[tuple[datetime, datetime]],
) -> tuple[float, float]:
    """Deduplicate corrections written before stable stall identities existed.

    Compatibility for journal rows written before #69. Remove after retained
    operator journals all postdate the deployment containing #69.
    """
    if not intervals:
        return 0.0, 0.0
    ordered = sorted(intervals)
    current_start, cluster_end, current_seconds = ordered[0]
    current_idle = _overlap_seconds(
        current_start,
        cluster_end,
        idle_intervals,
    )
    total = 0.0
    idle_total = 0.0
    for started_at, ended_at, seconds in ordered[1:]:
        overlap = _overlap_seconds(started_at, ended_at, idle_intervals)
        if started_at <= cluster_end:
            cluster_end = max(cluster_end, ended_at)
            if seconds > current_seconds or (
                seconds == current_seconds and overlap > current_idle
            ):
                current_seconds = seconds
                current_idle = overlap
            continue
        total += current_seconds
        idle_total += current_idle
        cluster_end = ended_at
        current_seconds = seconds
        current_idle = overlap
    return total + current_seconds, idle_total + current_idle


def _identified_suspension_partition(
    corrections: list[tuple[float, float, datetime, datetime]],
    idle_intervals: list[tuple[datetime, datetime]],
) -> tuple[float, float]:
    """Take one maximum correction per physical clock-offset cluster."""
    if not corrections:
        return 0.0, 0.0
    ordered = sorted(corrections)
    previous_offset, current_seconds, start, end = ordered[0]
    current_idle = min(
        current_seconds,
        _overlap_seconds(start, end, idle_intervals),
    )
    total = 0.0
    idle_total = 0.0
    for offset, seconds, start, end in ordered[1:]:
        overlap = min(seconds, _overlap_seconds(start, end, idle_intervals))
        # Journal wall stamps are integral seconds while monotonic is sampled
        # just after them. Separate daemons can therefore report the same boot
        # offset about one second apart.
        if offset - previous_offset <= 2.0:
            if seconds > current_seconds or (
                seconds == current_seconds and overlap > current_idle
            ):
                current_seconds = seconds
                current_idle = overlap
        else:
            total += current_seconds
            idle_total += current_idle
            current_seconds = seconds
            current_idle = overlap
        previous_offset = offset
    return total + current_seconds, idle_total + current_idle


def _suspension_partition(
    row: dash.IssueRow,
    max_seconds: float,
    idle_intervals: list[tuple[datetime, datetime]] | None = None,
) -> tuple[float, float]:
    """Valid suspension corrections, counted once across role detectors."""
    intervals: list[tuple[datetime, datetime, float]] = []
    identified: list[tuple[float, float, datetime, datetime]] = []
    idle = idle_intervals or []
    for event in row.events:
        if event.get("event") != "agent_run_stall":
            continue
        started_at = dash.parse_ts(event.get("stall_started_at"))
        if started_at is None:
            continue
        try:
            seconds = float(event.get("suspended_seconds") or 0)
        except (OverflowError, TypeError, ValueError):
            continue
        if (
            not math.isfinite(seconds)
            or seconds <= 0
            or seconds > max(0.0, max_seconds)
        ):
            continue
        try:
            clock_offset = float(event.get("stall_clock_offset_seconds"))
        except (OverflowError, TypeError, ValueError):
            clock_offset = math.nan
        try:
            ended_at = started_at + timedelta(seconds=seconds)
        except OverflowError:
            continue
        if math.isfinite(clock_offset):
            identified.append((clock_offset, seconds, started_at, ended_at))
        else:
            intervals.append((started_at, ended_at, seconds))
    identified_total, identified_idle = _identified_suspension_partition(
        identified,
        idle,
    )
    legacy_total, legacy_idle = _legacy_suspension_partition(intervals, idle)
    return (
        identified_total + legacy_total,
        identified_idle + legacy_idle,
    )


def _suspended_seconds(row: dash.IssueRow, max_seconds: float) -> float:
    return _suspension_partition(row, max_seconds)[0]


def _idle_intervals(
    row: dash.IssueRow,
    end: datetime,
) -> tuple[list[tuple[datetime, datetime]], bool]:
    """Union dependency-wait intervals so overlapping blockers count once."""
    active: set[tuple] = set()
    legacy = False
    idle_started: datetime | None = None
    intervals: list[tuple[datetime, datetime]] = []
    timed_events = sorted(
        (
            (stamp, index, event)
            for index, event in enumerate(row.events)
            if (stamp := dash.parse_ts(event.get("ts"))) is not None
            and stamp <= end
        ),
        key=lambda item: (item[0], item[1]),
    )
    for stamp, _index, event in timed_events:
        was_idle = bool(active) or legacy
        if event.get("event") == "deferred":
            legacy = False
            key = _defer_key(event)
            if event.get("state") == "cleared":
                active.discard(key)
            elif event.get("state") == "waiting":
                active.add(key)
        elif event.get("event") == "hold":
            legacy = True
        elif legacy and _dispatch_supersedes_legacy_hold(event):
            legacy = False
        is_idle = bool(active) or legacy
        if is_idle and not was_idle:
            idle_started = stamp
        elif was_idle and not is_idle and idle_started is not None:
            intervals.append((idle_started, stamp))
            idle_started = None
    if idle_started is not None:
        intervals.append((idle_started, end))
    return intervals, bool(active) or legacy


def _duration_breakdown(
    row: dash.IssueRow,
    start: datetime | None,
    end: datetime,
) -> tuple[float | None, float | None, float | None, bool]:
    if start is None:
        return None, None, None, False
    wall_seconds = max(0.0, (end - start).total_seconds())
    idle_intervals, idle_active = _idle_intervals(row, end)
    suspended, suspended_while_idle = _suspension_partition(
        row,
        wall_seconds,
        idle_intervals,
    )
    elapsed = max(0.0, wall_seconds - suspended)
    raw_idle = sum(
        max(0.0, (idle_end - idle_start).total_seconds())
        for idle_start, idle_end in idle_intervals
    )
    idle = min(elapsed, max(0.0, raw_idle - suspended_while_idle))
    return elapsed, elapsed - idle, idle, idle_active and not row.done


def _flags(row: dash.IssueRow, human_action: str | None = None) -> list[str]:
    flags = []
    if row.escalations:
        flags.append("escalated")
    if row.recurrences:
        flags.append("recurred")
    if row.spend_blocks:
        flags.append("spend-blocked")
    if _deferred_event(row):
        flags.append("waiting")
    if human_action:
        flags.append(human_action)
    return flags


def _labels_set(issue: dict | None) -> set[str]:
    return set(labels_of(issue or {}))


def _unanswered_prompt_at(issue: dict | None) -> datetime | None:
    comments = sorted(
        (
            (
                stamp,
                str(comment.get("body") or ""),
                str(
                    (
                        comment.get("author", {}).get("login")
                        if isinstance(comment.get("author"), dict)
                        else ""
                    )
                    or ""
                ),
            )
            for comment in (issue or {}).get("comments") or []
            if isinstance(comment, dict)
            and (stamp := dash.parse_ts(comment.get("createdAt"))) is not None
        ),
        key=lambda item: item[0],
    )
    prompt_at: datetime | None = None
    prompt_author = ""
    for stamp, body, author in comments:
        if (
            any(
                body.startswith(marker)
                for marker in (
                    "**Agent needs input:**",
                    "**Agent still needs input:**",
                    "**Agent needs clarification on review:**",
                    "**Author needs input:**",
                )
            )
            and "_Reply to unblock" in body
        ):
            prompt_at = stamp
            prompt_author = author
        elif (
            prompt_at is not None
            and body.strip()
            and (
                login_eq(author, prompt_author)
                or not is_bot_login(author)
                or body.lstrip().startswith("/unblock")
            )
        ):
            prompt_at = None
            prompt_author = ""
    return prompt_at


def _checks_green(checks) -> bool:
    if not isinstance(checks, list):
        return False
    success = {"SUCCESS", "NEUTRAL", "SKIPPED"}
    return all(
        isinstance(check, dict)
        and str(check.get("conclusion") or check.get("state") or "").upper() in success
        for check in checks
    )


def _merge_ready_since(pr: dict) -> datetime | None:
    stamps = [
        stamp
        for review in pr.get("latestReviews") or []
        if isinstance(review, dict)
        and str(review.get("state") or "").upper() == "APPROVED"
        and (stamp := dash.parse_ts(review.get("submittedAt"))) is not None
    ]
    stamps.extend(
        stamp
        for check in pr.get("statusCheckRollup") or []
        if isinstance(check, dict)
        and (stamp := dash.parse_ts(check.get("completedAt"))) is not None
    )
    return max(stamps, default=dash.parse_ts(pr.get("updatedAt")))


def _active_escalation_at(row: dash.IssueRow) -> datetime | None:
    """Newest escalation not superseded by a recorded label transition."""
    active_at: datetime | None = None
    for event in row.events:
        if event.get("event") == "escalation":
            active_at = dash.parse_ts(event.get("ts"))
        elif event.get("event") == "action":
            fact = dash.parse_action(str(event.get("desc") or ""))
            if (
                fact is not None
                and fact.verb == "relabel"
                and "needs-human-review" in fact.removed
            ):
                active_at = None
    return active_at


def _human_action(
    row: dash.IssueRow,
    gh_issue: dict | None,
    gh_pr: dict | None,
) -> tuple[str | None, datetime | None]:
    if row.done:
        return None, None
    issue_labels = _labels_set(gh_issue)
    pr_labels = _labels_set(gh_pr)
    if "needs-human-review" in issue_labels | pr_labels:
        external_updates = (
            dash.parse_ts(item.get("updatedAt"))
            for item, labels in (
                (gh_issue, issue_labels),
                (gh_pr, pr_labels),
            )
            if item is not None and "needs-human-review" in labels
        )
        return "escalated", _active_escalation_at(row) or max(
            (stamp for stamp in external_updates if stamp is not None),
            default=None,
        )
    prompt_times = [
        prompt_at
        for item, labels in (
            (gh_issue, issue_labels),
            (gh_pr, pr_labels),
        )
        if "ai-blocked" in labels
        and (prompt_at := _unanswered_prompt_at(item)) is not None
    ]
    if prompt_times:
        return "awaiting-reply", max(prompt_times)
    if (
        gh_pr
        and str(gh_pr.get("state") or "").upper() == "OPEN"
        and not gh_pr.get("mergedAt")
        and not gh_pr.get("isDraft")
        and str(gh_pr.get("mergeable") or "").upper() == "MERGEABLE"
        and str(gh_pr.get("mergeStateStatus") or "").upper() == "CLEAN"
        and str(gh_pr.get("reviewDecision") or "").upper() == "APPROVED"
        and _checks_green(gh_pr.get("statusCheckRollup"))
        and not gh_pr.get("autoMergeRequest")
    ):
        return "merge-ready", _merge_ready_since(gh_pr)
    return None, None


def _model_effort(row: dash.IssueRow, model: str) -> str | None:
    matches = [
        event
        for event in row.events
        if event.get("model") == model and event.get("effort")
    ]
    return str(matches[-1]["effort"]) if matches else None


def model_badges(row: dash.IssueRow, models=None) -> list[dict]:
    return [
        {"model": model, "effort": _model_effort(row, model)}
        for model in (row.models if models is None else models)
    ]


def active_card_models(row: dash.IssueRow) -> list[str]:
    """Models doing issue work, excluding models used only for review."""
    review_models = {
        str(event.get("model"))
        for event in row.events
        if event.get("kind") in dash.REVIEW_KINDS and event.get("model")
    }
    issue_work_models = {
        str(event.get("model"))
        for event in row.events
        if event.get("kind") not in dash.REVIEW_KINDS
        and event.get("kind") != "ROUTE"
        and event.get("model")
    }
    return [
        model
        for model in row.models
        if model not in review_models or model in issue_work_models
    ]


def projection_model(row: dash.IssueRow) -> str | None:
    """The resolve model whose historical peers best predict total issue cost."""
    resolve = [
        _model_for_leg(row, leg)
        for leg in row.legs
        if leg.kind in {"RESOLVE", "RESUME"}
    ]
    running = [
        leg.model for leg in row.running
        if leg.kind in {"RESOLVE", "RESUME"} and leg.model
    ]
    candidates = [model for model in (*resolve, *running) if model]
    return candidates[-1] if candidates else (row.models[-1] if row.models else None)


def projected_cost_by_model(rows) -> dict[str, float]:
    """Mean completed issue cost grouped by the model that resolved it."""
    costs: dict[str, list[float]] = {}
    for row in rows:
        if not row.done or not (model := projection_model(row)):
            continue
        costs.setdefault(model, []).append(row.cost_usd)
    return {
        model: round(sum(values) / len(values), 4)
        for model, values in costs.items()
    }


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
    projected_model: str | None = None,
    issue_verification: str | None = None,
    pr_verification: str | None = None,
) -> dict:
    started = _started(row)
    pr = row.prs[-1] if row.prs else None
    gh_pr = (gh_prs or {}).get(pr) if pr is not None else None
    # In flight: the clock is still running, so elapsed is measured to NOW.
    # Done: to the last thing that happened, which is where the clock stopped.
    end = row.last_ts if (row.done and row.last_ts) else now
    elapsed, working, idle, idle_active = _duration_breakdown(row, started, end)
    displayed_models = list(row.models) if row.done else active_card_models(row)
    human_action, human_wait_since = _human_action(row, gh_issue, gh_pr)
    human_wait_s = (
        max(0.0, (now - human_wait_since).total_seconds())
        if human_wait_since is not None
        else None
    )
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
        "projected_model": projected_model,
        "started_ts": iso(started),
        "last_ts": iso(row.last_ts),
        "elapsed_s": round(elapsed) if elapsed is not None else None,
        "working_s": round(working) if working is not None else None,
        "idle_s": round(idle) if idle is not None else None,
        "idle_active": idle_active,
        "human_action": human_action,
        "human_wait_since_ts": iso(human_wait_since),
        "human_wait_s": round(human_wait_s) if human_wait_s is not None else None,
        "projected_eta_s": projected_eta_s,
        "models": displayed_models,
        "model_badges": model_badges(row, displayed_models),
        "rounds": row.rounds,
        "flags": _flags(row, human_action),
        "issue_verification": issue_verification,
        "pr_verification": pr_verification,
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
    defer = _deferred_event(row) if row else {}
    return {
        "issue": int(issue.get("number") or 0),
        "title": issue.get("title"),
        "url": issue.get("url"),
        "priority": priority_of(labels),
        "labels": labels,
        "held": bool(defer),
        "hold_reason": waiting_label(defer),
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
        "age_s": age_seconds(issue.get("createdAt"), now),
        "comments": _comment_count(issue.get("comments")),
        "issue_type": _label_value(labels, ("type:",), exact=("bug", "feature")),
        "difficulty": _label_value(labels, ("difficulty:", "effort:")),
        "impact": _label_value(labels, ("impact:",)),
    }


def _label_value(
    labels: list[str],
    prefixes: tuple[str, ...],
    *,
    exact: tuple[str, ...] = (),
) -> str | None:
    """Extract one compact planning value without teaching the SPA label grammar."""
    lowered = [(label, label.lower()) for label in labels]
    for expected in exact:
        if any(value == expected for _label, value in lowered):
            return expected
    for label, value in lowered:
        for prefix in prefixes:
            if value.startswith(prefix) and len(label) > len(prefix):
                return label[len(prefix):]
    return None


def journal_notifications(rows, limit: int = 100) -> list[dict]:
    """Actionable journal transitions, newest window only.

    The browser establishes its high-water mark from this stream on first load,
    so returning history is safe. Unknown and malformed events deliberately
    disappear rather than weakening the whole dashboard response.
    """
    candidates: list[tuple[datetime, int, int, dict]] = []
    ordinal = 0
    for row in rows:
        for event in row.events:
            stamp = dash.parse_ts(event.get("ts"))
            if stamp is not None:
                candidates.append((stamp, ordinal, row.issue, event))
            ordinal += 1
    candidates.sort(key=lambda item: (item[0], item[1]))
    notifications = []
    notification_ids = set()
    for stamp, _ordinal, issue, event in candidates:
        projected = _journal_notification(event, issue)
        if projected is None:
            continue
        identity_source = (
            {"issue": issue, "dispatch": event["dispatch"]}
            if event.get("event") == "agent_run_start"
            else {"issue": issue, "event": event}
        )
        identity = hashlib.sha256(
            json.dumps(
                identity_source,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        if identity in notification_ids:
            continue
        notification_ids.add(identity)
        notifications.append({
            "id": identity,
            "ts": iso(stamp),
            **projected,
        })
    return notifications[-max(0, limit):] if limit > 0 else []


def _journal_notification(event: dict, issue: int) -> dict | None:
    name = event.get("event")
    if name == "action":
        fact = dash.parse_action(str(event.get("desc") or ""))
        if fact and (
            (fact.verb == "clear" and fact.merged)
            or (fact.verb == "relabel" and "ai-done" in fact.added)
        ):
            pr = fact.number if fact.target == "pr" else None
            return {
                "severity": "info",
                "title": f"Issue #{issue} completed",
                "detail": f"PR #{pr} merged" if pr is not None else None,
                "issue": issue,
                "pr": pr,
            }
        return None
    if name == "escalation":
        number = _event_number(event.get("number"))
        return {
            "severity": "attention",
            "title": f"Issue #{issue} escalated",
            "detail": "Human review is required",
            "issue": issue,
            "pr": number if event.get("target") == "pr" else None,
        }
    if name == "agent_run_start":
        kind = str(event.get("kind") or "")
        role = event.get("role")
        dispatch = event.get("dispatch")
        number = _event_number(event.get("number"))
        if (
            kind not in _DISPATCH_AGENT_KINDS
            or role not in dash.ROLE_NAMES
            or number is None
            or not isinstance(dispatch, str)
            or not dispatch.strip()
        ):
            return None
        pr = number if kind in _PR_AGENT_KINDS else None
        target = f" for PR #{pr}" if pr is not None else ""
        return {
            "severity": "info",
            "title": f"Issue #{issue} dispatched",
            "detail": f"{kind or 'Agent run'}{target} started",
            "issue": issue,
            "pr": pr,
        }
    if name != "agent_run":
        return None
    status = str(event.get("status") or "")
    kind = str(event.get("kind") or "")
    number = _event_number(event.get("number"))
    if status == "blocked":
        return {
            "severity": "attention",
            "title": f"Issue #{issue} needs input",
            "detail": "Agent is blocked on a human reply",
            "issue": issue,
            "pr": None,
        }
    failure = " ".join(
        str(event.get(field) or "").lower()
        for field in ("status", "envelope_subtype", "exit_reason")
    )
    if "error_max_budget_usd" in failure or "timeout" in failure:
        budget = "error_max_budget_usd" in failure
        return {
            "severity": "attention",
            "title": f"Issue #{issue} run stopped",
            "detail": (
                "Budget limit reached"
                if budget
                else "Run timed out"
            ),
            "issue": issue,
            "pr": number if kind in dash.REVIEW_KINDS | dash.REPAIR_KINDS else None,
        }
    if kind in dash.REVIEW_KINDS and status in {"APPROVE", "REQUEST_CHANGES"}:
        if number is None:
            return None
        return {
            "severity": "info" if status == "APPROVE" else "attention",
            "title": (
                f"PR #{number} approved"
                if status == "APPROVE"
                else f"PR #{number} needs changes"
            ),
            "detail": "Review verdict submitted",
            "issue": issue,
            "pr": number,
        }
    return None


def _event_number(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


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


def age_seconds(created_at, now: datetime) -> int | None:
    ts = dash.parse_ts(created_at)
    return max(0, int((now - ts).total_seconds())) if ts else None


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
    elapsed, working, idle, idle_active = _duration_breakdown(row, started, end)
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
        "elapsed_s": round(elapsed) if elapsed is not None else None,
        "working_s": round(working) if working is not None else None,
        "idle_s": round(idle) if idle is not None else None,
        "idle_active": idle_active,
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
