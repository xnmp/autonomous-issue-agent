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
from typing import assert_never

from ...domain import dashboard as dash
from ...domain.actions import AgentKind
from ...domain.identity import is_bot_login, login_eq
from ...domain.outcome import ExitReason
from ...domain.world import ReviewDecision

# Frecency windows: (seconds, weight). An event inside 24h counts in all three,
# which is the intended pile-up — a repo that moved this morning must outrank a
# repo that moved a fortnight ago even if the fortnight-old one has more events.
FRECENCY_WINDOWS = ((86400.0, 3.0), (7 * 86400.0, 1.0), (30 * 86400.0, 0.25))

# The lifecycle labels the harness owns. An open issue carrying none of them has
# never been offered to the harness — that is the backlog.
LIFECYCLE_LABELS = frozenset(
    {"ai-ready", "ai-working", "ai-done", "ai-blocked", "needs-human-review",
     "ai-parked"}
)
PRIORITY_LABELS = ("P1", "P2", "P3")
_AGENT_KINDS = frozenset(kind.name for kind in AgentKind)
_DISPATCH_AGENT_KINDS = _AGENT_KINDS - {"ROUTE"}
_PR_AGENT_KINDS = dash.REVIEW_KINDS | dash.REPAIR_KINDS | {"PR_RESUME"}
# Legs the steward runs before an issue is admitted to the fix pipeline. An
# issue can be triaged and then wait hours in the backlog for `ai-ready`, and
# that wait belongs to the operator's queue, not to the pipeline's clock.
_PRE_ADMISSION_KINDS = dash.STEWARD_KINDS | {"GROOM"}

# Compact-row text budget: one line, bounded, for prose the harness did not
# author (agent summaries, escalation comments, operator input).
_REASON_CHARS = 240
# An escalation and the comment that explains it are journalled by the same
# executor pass, so only a handful of events can separate them. A wider window
# would pair an escalation with an unrelated comment from a later leg.
_ESCALATION_COMMENT_WINDOW = 4
# Operator interventions the drill-down renders as steps.
OPERATOR_ACTION_STEPS = {
    "operator_unblock": "unblock",
    "operator_relabel": "relabel (operator)",
    # Dismissing the reviewer's standing change requests is the half of an
    # unblock that actually re-arms the rejection ceiling; when it FAILS the
    # unblock still reports ok, so the step is the only place an operator can
    # see that the PR is still wedged (keifu #173).
    "operator_dismiss_review": "review re-arm",
}


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
            alive = {h.role: None for h in health}
            active = None
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
        payload["liveness"] = "unknown"
        payload["warnings"].append(
            "process scan unavailable — daemon liveness is unknown"
        )
    return payload


# ── legs → steps ──────────────────────────────────────────────────────────────
def leg_state(leg: dash.RunSummary) -> str:
    """A settled leg is `failed` when the envelope reports an error or a
    terminal reason in the error taxonomy. `status` is the AGENT's claim;
    `is_error`/`exit_reason` are the envelope's, and only the envelope can see
    a run that died."""
    if (
        leg.kind == "ROUTE"
        and leg.exit_reason.strip().casefold() == "budget_exhausted"
    ):
        # Routing is advisory: its fallback still dispatches the fixer. Show
        # the overrun without presenting the issue as a failed execution.
        return "advisory"
    if dash.leg_failed(leg.is_error, leg.exit_reason):
        return "failed"
    return "blocked" if leg.status.lower() == "blocked" else "ok"


def collapse_steps(row: dash.IssueRow) -> list[dict]:
    """The card's compact story strip: the issue's history in timestamp order,
    with CONSECUTIVE runs of the same kind folded into one episode — attempts
    counted, costs SUMMED (three FIX_CI tries are one episode whose cost is
    three legs' money and whose state is how the last try ended). A kind that
    recurs after something else ran stays its own episode: "review, fix,
    review again" is the story the strip exists to tell, and the old
    one-entry-per-kind collapse erased it. Operator interventions interleave
    where they happened; running legs close the strip."""
    ordered = [
        {
            "kind": leg.kind,
            "state": leg_state(leg),
            "status": leg.status or None,
            "cost_usd": leg.cost_usd,
            "ts": leg.ts,
        }
        for leg in row.legs
    ]
    ordered.extend(
        {
            "kind": kind,
            "state": state,
            "status": None,
            "cost_usd": 0.0,
            "ts": dash.parse_ts(event.get("ts")),
        }
        for event, kind, _note, state in _operator_steps(row)
    )
    ordered.sort(
        key=lambda step: step["ts"] or datetime.max.replace(tzinfo=timezone.utc)
    )
    episodes: list[dict] = []
    for step in ordered:
        last = episodes[-1] if episodes else None
        if last is not None and last["kind"] == step["kind"]:
            last["count"] += 1
            last["cost_usd"] = round(last["cost_usd"] + step["cost_usd"], 4)
            last["state"] = step["state"]
            last["status"] = step["status"]
            continue
        episodes.append(
            {
                "kind": step["kind"],
                "state": step["state"],
                "status": step["status"],
                "cost_usd": round(step["cost_usd"], 4),
                "count": 1,
            }
        )
    episodes.extend(
        {"kind": r.kind, "state": "running", "status": None, "cost_usd": 0.0, "count": 1}
        for r in row.running
    )
    return episodes


def _label_list(value: object) -> tuple:
    return tuple(value) if isinstance(value, (list, tuple)) else ()


def _operator_note(action: dict) -> str | None:
    if action.get("labels_applied") is False:
        requested = [
            f"{verb} {', '.join(labels)}"
            for verb, key in (
                ("remove", "requested_remove"),
                ("add", "requested_add"),
            )
            if (
                labels := [
                    str(label)
                    for label in _label_list(action.get(key))
                    if str(label)
                ]
            )
        ]
        changes = [
            "label edit failed"
            + (f" (requested {'; '.join(requested)})" if requested else "")
        ]
    else:
        changes = [
            f"{verb} {', '.join(labels)}"
            for verb, key in (("removed", "remove"), ("added", "add"))
            if (
                labels := [
                    str(label)
                    for label in _label_list(action.get(key))
                    if str(label)
                ]
            )
        ]
    if action.get("retries_cleared"):
        changes.append("reset retry counters")
    # A re-arm that rescoped the review and one that merely repeated the last
    # one look identical in history otherwise — and "we unblocked it three
    # times" reads very differently once you can see the third carried a note.
    if action.get("guidance") == "set":
        changes.append("with scope note")
    elif action.get("guidance") == "cleared":
        changes.append("scope note cleared")
    if "dismissed" in action:
        review = str(action.get("review_id") or "")
        changes.append(
            f"dismissed review {review}".strip()
            if action.get("dismissed")
            else f"NOT dismissed: {action.get('detail') or 'unknown'}"
        )
    return _reason_line("; ".join(changes))


def _operator_steps(
    row: dash.IssueRow,
) -> list[tuple[dict, str, str | None, str]]:
    """Journalled operator interventions, as (event, kind, note, state).

    A manual unblock costs nothing but changes what the harness does next, so
    it belongs in the step list beside the paid legs rather than only in the
    raw event feed, where nobody reads it.
    """
    steps = []
    for event in row.events:
        action = event.get("action")
        if not isinstance(action, dict):
            continue
        kind = OPERATOR_ACTION_STEPS.get(str(action.get("type") or ""))
        if kind is not None:
            # Channel provenance: an unblock driven over the bare API (an
            # agent, curl) wears its channel so it is never mistaken for a
            # human's judgement call. Events older than the stamp have no
            # channel and stay unmarked.
            if action.get("channel") == "api":
                kind = f"{kind} (api)"
            state = "failed" if action.get("labels_applied") is False else "done"
            steps.append((event, kind, _operator_note(action), state))
    return steps


def _review_round(row: dash.IssueRow, leg: dash.RunSummary) -> int | None:
    """1-based verdict ordinal for this PR; failed attempts consume none."""
    ordinal = 0
    for other in row.legs:
        if dash.review_verdict(other) is not None and other.number == leg.number:
            ordinal += 1
            if other is leg:
                return ordinal
    return None


def _leg_note(row: dash.IssueRow, leg: dash.RunSummary) -> str | None:
    """One line of story per step: the verdict, the question, or the failure.
    A leg that did what it was asked adds nothing."""
    settled = _settled_event(row, leg)
    summary = _reason_line(settled.get("needs") or settled.get("summary"))
    if leg_state(leg) in {"failed", "advisory"}:
        reason = str(leg.exit_reason or "").strip() or "failed without a reason"
        return _reason_line(f"{reason} — {summary}" if summary else reason)
    if leg.status.lower() == "blocked":
        return summary
    if (verdict := dash.review_verdict(leg)) is not None:
        ordinal = _review_round(row, leg)
        decision = verdict.decision.value
        return decision if ordinal is None else f"{decision} round {ordinal}"
    return None


def detailed_steps(row: dash.IssueRow) -> list[dict]:
    """One row per leg plus operator interventions, in timestamp order.
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
            "effort": _effort_for_leg(row, leg),
            "session_id": leg.session_id or None,
            "turns": _turns(row, leg),
            "usage": _usage(row, leg),
            "has_transcript": bool(leg.session_id),
            "note": _leg_note(row, leg),
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
            "note": None,
        }
        for i, r in enumerate(row.running)
    )
    steps.extend(
        {
            "idx": len(steps) + i,
            "kind": kind,
            "role": event.get("role"),
            "ts": event.get("ts"),
            "state": state,
            "status": None,
            "exit_reason": None,
            "duration_s": None,
            "cost_usd": 0.0,
            "estimated": False,
            "model": None,
            "effort": None,
            "session_id": None,
            "turns": None,
            "usage": None,
            "has_transcript": False,
            "note": note,
        }
        for i, (event, kind, note, state) in enumerate(_operator_steps(row))
    )
    # The three sources above each arrive in their own order; without this
    # sort every operator intervention rendered after every leg, however early
    # it actually happened (keifu batch 3, 2026-08-02). Unparseable stamps
    # sort last, keeping their relative order.
    steps.sort(
        key=lambda step: dash.parse_ts(step.get("ts")) or datetime.max.replace(tzinfo=timezone.utc)
    )
    # `idx` is the drill-down's cursor: `step_window_events` resolves it by list
    # position, so it must be the position and not a running total taken while
    # the list is still growing.
    for position, step in enumerate(steps):
        step["idx"] = position
    return steps


def _settled_event(row: dash.IssueRow, leg: dash.RunSummary) -> dict:
    """The raw `agent_run` a RunSummary was built from — it keeps only what the
    aggregation needs, and the drill-down also wants turns/usage."""
    for e in row.events:
        if (
            e.get("event") == "agent_run"
                and dash.parse_ts(e.get("ts")) == leg.ts
            and str(e.get("kind") or "?") == leg.kind
        ):
            return e
    return {}


def _turns(row: dash.IssueRow, leg: dash.RunSummary):
    return _settled_event(row, leg).get("turns")


def _usage(row: dash.IssueRow, leg: dash.RunSummary):
    usage = _settled_event(row, leg).get("usage")
    return usage if isinstance(usage, dict) else None


def _model_for_leg(row: dash.IssueRow, leg: dash.RunSummary) -> str | None:
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


def _effort_for_leg(row: dash.IssueRow, leg: dash.RunSummary) -> str | None:
    settled = _settled_event(row, leg)
    if settled.get("effort"):
        return str(settled["effort"])
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
            )
        )
        and event.get("effort")
    ]
    return str(starts[-1]["effort"]) if starts else None


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
        # A GitHub dependency hold reads "blocked by", matching the language
        # the operator used when they parked the issue; the overlap hold keeps
        # "waiting on" because the blocker there is our own in-flight work.
        if event.get("reason") == "blocker":
            return f"blocked by #{event['blocker_issue']}"
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
    window: tuple[datetime, datetime] | None = None,
) -> tuple[float, float]:
    """Valid suspension corrections, counted once across role detectors.

    `window` bounds which part of a suspension can correct anything: only the
    portion inside the measured span shortened that span. A correction is
    validated against `max_seconds` as reported, before clipping, so a
    malformed duration cannot be rescued by falling partly inside the window.
    """
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
        if window is not None:
            began, finished = max(started_at, window[0]), min(ended_at, window[1])
            if began >= finished:
                continue
            started_at, ended_at = began, finished
            seconds = (finished - began).total_seconds()
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


def _union_intervals(
    intervals: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    merged: list[tuple[datetime, datetime]] = []
    for begin, finish in sorted(intervals):
        if merged and begin <= merged[-1][1]:
            last_begin, last_finish = merged[-1]
            merged[-1] = (last_begin, max(last_finish, finish))
        else:
            merged.append((begin, finish))
    return merged


def _clip_intervals(
    intervals: list[tuple[datetime, datetime]],
    begin: datetime,
    finish: datetime,
) -> list[tuple[datetime, datetime]]:
    clipped = [(max(b, begin), min(f, finish)) for b, f in intervals]
    return [iv for iv in clipped if iv[0] < iv[1]]


def _total_seconds(intervals: list[tuple[datetime, datetime]]) -> float:
    return sum(max(0.0, (f - b).total_seconds()) for b, f in intervals)


def _leg_spans(
    row: dash.IssueRow,
    start: datetime,
    end: datetime,
) -> list[tuple[str, datetime, datetime]]:
    """Every leg's occupied window, kind-tagged, clamped to [start, end].

    A settled leg's journal stamp is its settlement; its runtime is the
    reported duration walked back from there. A leg running right now extends
    to `end`. The clamp keeps a malformed duration from minting work time
    outside the issue's life.
    """
    spans: list[tuple[str, datetime, datetime]] = []
    for leg in row.legs:
        if leg.ts is None:
            continue
        seconds = leg.duration_s
        if not math.isfinite(seconds) or seconds <= 0:
            continue
        try:
            begin = leg.ts - timedelta(seconds=seconds)
        except OverflowError:
            begin = start
        spans.append((leg.kind.upper(), max(begin, start), min(leg.ts, end)))
    for leg in row.running:
        if leg.ts is None:
            continue
        spans.append((leg.kind.upper(), max(leg.ts, start), end))
    return [span for span in spans if span[1] < span[2]]


def _leg_intervals(
    row: dash.IssueRow,
    start: datetime,
    end: datetime,
) -> list[tuple[datetime, datetime]]:
    """When the harness was actually inside a task, as a disjoint union."""
    return _union_intervals([(b, f) for _, b, f in _leg_spans(row, start, end)])


def _pipeline_anchor(
    spans: list[tuple[str, datetime, datetime]],
) -> datetime | None:
    """Where the pipeline clock starts: the first non-steward leg."""
    return min(
        (b for kind, b, _ in spans if kind not in _PRE_ADMISSION_KINDS),
        default=None,
    )


def _duration_breakdown(
    row: dash.IssueRow,
    start: datetime | None,
    end: datetime,
) -> tuple[float | None, float | None, float | None, bool]:
    """Working time is time spent inside tasks — the same durations the
    drill-down lists, so the per-task times sum (modulo overlap and clamping)
    to the card's work clock. Everything else the pipeline lived through —
    waiting on CI, on a review slot, on a dependency, on a human — is idle.
    The work clock ticks only while a leg is actually running (#34 sat
    "awaiting merge" with a ticking work clock and 6h of invented work).

    The wall clock anchors at the first fix-pipeline leg, not the issue's first
    event. Pre-admission legs contribute their own runtime and nothing around
    it: keifu #185 (2026-08-03) was triaged at 00:24 and admitted at 01:25, and
    charged that backlog hour to idle the moment its resolve began. Queueing
    for admission is not pipeline idleness.
    """
    if start is None:
        return None, None, None, False
    spans = _leg_spans(row, start, end)
    work_intervals = _union_intervals([(b, f) for _, b, f in spans])
    raw_working = _total_seconds(work_intervals)
    anchor = _pipeline_anchor(spans)
    if anchor is None:
        if spans:
            # Triaged and sitting in the backlog: the pipeline clock has not
            # started, so there is no span for idleness to live in.
            return raw_working, raw_working, 0.0, False
        # No legs at all: nothing to anchor on, so the issue's own window is
        # the only measure available.
        anchor = start
    pre_seconds = _total_seconds(_clip_intervals(work_intervals, start, anchor))
    wall_seconds = max(0.0, (end - anchor).total_seconds())
    suspended, suspended_while_working = _suspension_partition(
        row,
        max(0.0, (end - start).total_seconds()),
        _clip_intervals(work_intervals, anchor, end),
        window=(anchor, end),
    )
    elapsed = pre_seconds + max(0.0, wall_seconds - suspended)
    working = min(elapsed, max(0.0, raw_working - suspended_while_working))
    idle = max(0.0, elapsed - working)
    idle_active = not row.done and not row.running
    return elapsed, working, idle, idle_active


def _flags(row: dash.IssueRow, human_action: str | None = None) -> list[str]:
    flags = []
    if row.escalations:
        flags.append("escalated")
    if row.recurrences:
        flags.append("recurred")
    if row.spend_blocks:
        flags.append("spend-blocked")
    newest_leg_blocked = (
        bool(row.legs)
        and row.legs[-1].status.lower() == "blocked"
        and row.stage in {"awaiting review", "reviewed", "awaiting human reply"}
    )
    if row.stage == "blocked" or newest_leg_blocked:
        flags.append("blocked")
    if _deferred_event(row):
        flags.append("waiting")
    if human_action:
        flags.append(human_action)
    return flags


def _reason_line(raw: object) -> str | None:
    """First line of prose the harness did not author, bounded for a row.

    A line that OPENS with a bold heading — every breaker comment does, and it
    would otherwise read as literal asterisks outside a Markdown pane — has all
    of its bold markers dropped; a line that merely contains one is left alone.
    """
    if not isinstance(raw, str) or not (text := raw.strip()):
        return None
    line = text.splitlines()[0].strip()
    if line.startswith("**"):
        line = line.replace("**", "").strip()
    return line[:_REASON_CHARS] or None


def _escalation_reason(row: dash.IssueRow) -> str | None:
    """Why the harness handed this issue to a human, while it still is.

    The `escalation` event carries no prose; the escalating pass states its
    reason in a comment posted in the same executor pass, so the newest
    escalation is paired with the nearest comment action on the same target.
    Ties break toward the later event — a breaker comments, then escalates.

    Retirement follows `_active_escalation_at`: a relabel that took
    needs-human-review off, or a finished issue, ends the reason with it. The
    clients gate their unblock affordances on this field, so a reason that
    outlived its escalation would offer to unblock nothing.
    """
    events = row.events
    if row.done or _active_escalation_at(row) is None:
        return None
    for index in range(len(events) - 1, -1, -1):
        if events[index].get("event") != "escalation":
            continue
        numbers = {
            number
            for number in (events[index].get("pr"), events[index].get("issue"))
            if isinstance(number, int)
        }
        candidates = [
            (abs(offset - index), -offset, reason)
            for offset in range(
                max(0, index - _ESCALATION_COMMENT_WINDOW),
                min(len(events), index + _ESCALATION_COMMENT_WINDOW + 1),
            )
            if isinstance(action := events[offset].get("action"), dict)
            # escalate_ci_pending carries its reason in its own body — the
            # pending-stall path posts no separate comment, so keying on
            # "comment" alone left those escalations reasonless and the UI
            # fell back to a generic line (keifu PRs #182/#183, 2026-08-03).
            and action.get("type") in ("comment", "escalate_ci_pending")
            and (
                not numbers
                or action.get("number") in numbers
                or action.get("pr") in numbers
            )
            # A body-less comment beside the escalation is not the reason, and
            # must not shadow the real one a little further out.
            and (reason := _reason_line(action.get("body"))) is not None
        ]
        return min(candidates)[2] if candidates else None
    return None


def _blocked_reason(row: dash.IssueRow) -> str | None:
    """First useful line from the newest blocked leg, bounded for compact rows."""
    for event in reversed(row.events):
        if event.get("event") != "agent_run" or event.get("status") != "blocked":
            continue
        raw = str(event.get("needs") or event.get("summary") or "").strip()
        return raw.splitlines()[0][:240] if raw else None
    return None


def _display_stage(row: dash.IssueRow) -> str:
    if row.stage == "blocked" and _blocked_reason(row):
        return "awaiting human reply"
    return row.stage
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


def _action_label_moves(event: dict) -> tuple[set[str], set[str]] | None:
    """Label adds/removes an action event performed, structured first.

    Operator park/unpark journal their real moves as `add`/`remove` attribute
    lists while their display line names only the park label ("operator unpark
    issue #130: -ai-parked" omitted the +ai-pr-open that should have retired
    the escalation — keifu #130, 2026-08-03, stayed in the blocked section
    with a stale CI-pending reason after CI went green). The parsed relabel
    desc remains the fallback for events older than the structured form.
    """
    action = event.get("action")
    if isinstance(action, dict):
        # Operator effects journal their attempted move even when GitHub
        # rejected it so the reset remains auditable.  A failed intent is not
        # an observed label transition and must never retire dashboard
        # attention while the live label view is unavailable.
        if action.get("labels_applied") is False:
            return set(), set()
        add, remove = action.get("add"), action.get("remove")
        if isinstance(add, list) or isinstance(remove, list):
            return (
                {item for item in (add or ()) if isinstance(item, str)},
                {item for item in (remove or ()) if isinstance(item, str)},
            )
    fact = dash.parse_action(str(event.get("desc") or ""))
    if fact is not None and fact.verb == "relabel":
        return set(fact.added), set(fact.removed)
    return None


def _journal_attention_at(
    row: dash.IssueRow,
) -> tuple[str | None, datetime | None]:
    """Human-attention stamp from the row's own journal, label views unseen.

    Escalations (event or +needs-human-review/+needs-budget-review relabel)
    and blocks (+ai-blocked relabel) are tracked separately: removing one
    label class must not clear the other's stamp. An active escalation wins
    over an active block — the escalation is the harder stop.
    """
    escalated_at: datetime | None = None
    blocked_at: datetime | None = None
    for event in row.events:
        name = event.get("event")
        if name == "escalation":
            escalated_at = dash.parse_ts(event.get("ts"))
            continue
        if name != "action":
            continue
        fact = dash.parse_action(str(event.get("desc") or ""))
        if fact is not None and fact.verb == "clear":
            # A closed/merged PR ends the attention hold the same way the
            # stage derivation treats it; the re-queue may be an unjournalled
            # GitHub write, so waiting for a label removal freezes forever.
            escalated_at = None
            blocked_at = None
            continue
        moves = _action_label_moves(event)
        if moves is None:
            continue
        added, removed = moves
        stamp = dash.parse_ts(event.get("ts"))
        # An add with an unparseable ts must not read as "no escalation".
        if added & {"needs-human-review", "needs-budget-review"} and stamp:
            escalated_at = stamp
        if "ai-blocked" in added and stamp:
            blocked_at = stamp
        if removed & {"needs-human-review", "needs-budget-review"}:
            escalated_at = None
        if "ai-blocked" in removed:
            blocked_at = None
        # A later lifecycle label supersedes attention wholesale: the daemon
        # re-derives queued/working/pr-open AFTER an operator clears the hold
        # (the clearing itself may be an unjournalled GitHub write), so the
        # add is the journal's proof the issue resumed.
        if added & {"ai-ready", "ai-working", "ai-pr-open", "ai-done"}:
            escalated_at = None
            blocked_at = None
    if escalated_at is not None:
        return "escalated", escalated_at
    if blocked_at is not None:
        return "awaiting-reply", blocked_at
    return None, None


def _active_escalation_at(row: dash.IssueRow) -> datetime | None:
    """Newest escalation not superseded by a recorded label transition.

    Retirement: taking needs-human-review away ends the escalation, and so
    does an explicit operator unpark — after re-arming, offering the stale
    escalation reason ("CI pending 60 minutes") misdescribes a row the
    pipeline is actively driving again (keifu #130, 2026-08-03). Deliberately
    NOT any lifecycle-label add: a daemon relabel (-ai-blocked +ai-working on
    an operator reply) can land while GitHub still carries needs-human-review
    on the PR, and stripping the reason then would take the unblock
    affordance off a live escalation (adversarial review F6)."""
    active_at: datetime | None = None
    for event in row.events:
        if event.get("event") == "escalation":
            active_at = dash.parse_ts(event.get("ts"))
        elif event.get("event") == "action":
            action = event.get("action")
            if (
                isinstance(action, dict)
                and action.get("type") == "operator_unpark"
            ):
                active_at = None
                continue
            moves = _action_label_moves(event)
            if moves is not None and "needs-human-review" in moves[1]:
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
    # needs-budget-review is an escalation too: only a cap decision resumes it,
    # and its own prompt comment may already have been answered, so the
    # awaiting-reply arm below cannot be relied on to surface it.
    attention = {"needs-human-review", "needs-budget-review"}
    if attention & (issue_labels | pr_labels):
        external_updates = (
            dash.parse_ts(item.get("updatedAt"))
            for item, labels in (
                (gh_issue, issue_labels),
                (gh_pr, pr_labels),
            )
            if item is not None and attention & labels
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
    # Journal-truth fallback for entities whose LIVE label view is absent:
    # before the PR enrichment carried labels, a PR-level escalation or block
    # was invisible to the arms above — the row read "escalated" (journal)
    # while the work clock ran to NOW (keifu PRs #173/#177, 2026-08-02). The
    # fallback must NOT outlive a live view that contradicts it: the journal
    # records only the harness's OWN writes, so an operator clearing
    # ai-blocked with a plain gh write left the fallback holding the row
    # "awaiting-reply" forever, freezing both clocks while paid legs ran
    # (keifu #142/#181, 2026-08-03). GitHub is authority for labels; the
    # journal is a witness for windows where GitHub wasn't fetched.
    journal_kind, journal_at = _journal_attention_at(row)
    if journal_kind is not None and journal_at is not None:
        views = [v for v in (gh_issue, gh_pr) if v is not None]
        live_labels_everywhere = bool(views) and all(
            "labels" in v for v in views
        )
        contradicted = live_labels_everywhere and not (
            (attention | {"ai-blocked"}) & (issue_labels | pr_labels)
        )
        if not contradicted:
            return journal_kind, journal_at
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
    fixer_matches = [
        event
        for event in matches
        if event.get("role") == "fix" and event.get("kind") != "ROUTE"
    ]
    selected = fixer_matches or matches
    return str(selected[-1]["effort"]) if selected else None


def model_badges(row: dash.IssueRow, models=None) -> list[dict]:
    return [
        {"model": model, "effort": _model_effort(row, model)}
        for model in (row.models if models is None else models)
    ]


def fixer_card_models(row: dash.IssueRow) -> list[str]:
    """Fixer models used by non-routing legs, in first-seen order."""
    settled = (
        _model_for_leg(row, leg)
        for leg in row.legs
        if leg.role == "fix" and leg.kind != "ROUTE"
    )
    running = (
        leg.model
        for leg in row.running
        if leg.role == "fix" and leg.kind != "ROUTE"
    )
    return list(dict.fromkeys(
        model for model in (*settled, *running) if model
    ))


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


def _clock_end(
    row: dash.IssueRow,
    started: datetime | None,
    now: datetime,
    human_wait_since: datetime | None,
) -> datetime:
    """Where the working/idle clocks stop.

    In flight the clock runs to NOW; done, to the last thing that happened. An
    issue parked on a human is neither: the harness stopped working at the
    escalation/block point, and letting elapsed keep climbing charges the
    harness for the operator's queue. `human_wait_s` still ticks live, so the
    wait itself stays visible. A wait stamp at or before the issue's first
    event cannot bound anything, so it is ignored.
    """
    if row.done and row.last_ts:
        return row.last_ts
    if (
        human_wait_since is not None
        and started is not None
        and started < human_wait_since < now
    ):
        return human_wait_since
    return now


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
    issue_verification_reason: str | None = None,
    pr_verification_reason: str | None = None,
) -> dict:
    started = _started(row)
    pr = row.prs[-1] if row.prs else None
    gh_pr = (gh_prs or {}).get(pr) if pr is not None else None
    human_action, human_wait_since = _human_action(row, gh_issue, gh_pr)
    elapsed, working, idle, idle_active = _duration_breakdown(
        row, started, _clock_end(row, started, now, human_wait_since)
    )
    displayed_models = fixer_card_models(row)
    human_wait_s = (
        max(0.0, (now - human_wait_since).total_seconds())
        if human_wait_since is not None
        else None
    )
    card = {
        "issue": row.issue,
        "title": (gh_issue or {}).get("title"),
        "url": (gh_issue or {}).get("url"),
        "labels": card_labels(row, gh_issue),
        "pr": pr,
        "pr_url": (gh_pr or {}).get("url"),
        "pr_state": _pr_state(gh_pr),
        "pr_review_state": _pr_review_state(gh_pr),
        "pr_ci_state": _pr_ci_state(gh_pr),
        "stage": _display_stage(row),
        "attention_reason": _blocked_reason(row),
        "escalation_reason": _escalation_reason(row),
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
        # A paid leg is executing RIGHT NOW. The client tick predicate needs
        # this directly: human_action alone must not dead-stop both clocks
        # while a leg is visibly spending (keifu #142/#181, 2026-08-03).
        "leg_running": bool(row.running) and not row.done,
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
        "issue_verification_reason": issue_verification_reason,
        "pr_verification_reason": pr_verification_reason,
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


_CI_FAILURE = {
    "FAILURE",
    "ERROR",
    "TIMED_OUT",
    "CANCELLED",
    "ACTION_REQUIRED",
    "STARTUP_FAILURE",
}
_CI_SUCCESS = {"SUCCESS", "NEUTRAL", "SKIPPED"}


def _pr_ci_state(gh_pr: dict | None) -> str | None:
    """CI's one-word answer for the PR chip: failing beats pending beats
    passing, and no checks (or an unreadable rollup) is None — an unknown must
    render as no signal, never as green."""
    checks = (gh_pr or {}).get("statusCheckRollup")
    if not isinstance(checks, list):
        return None
    verdicts = [
        str(check.get("conclusion") or check.get("state") or "").upper()
        for check in checks
        if isinstance(check, dict)
    ]
    if not verdicts:
        return None
    if any(verdict in _CI_FAILURE for verdict in verdicts):
        return "failing"
    if all(verdict in _CI_SUCCESS for verdict in verdicts):
        return "passing"
    return "pending"


def _pr_review_state(gh_pr: dict | None) -> str | None:
    """Whether review says this PR can merge, or asked for changes.

    Both inputs are optional at GitHub's end — a PAT without the scopes to read
    `reviewDecision`/`mergeable` returns nulls, and a merge queue can leave
    `mergeable` UNKNOWN mid-computation. An unknown answer must read as "no
    badge": claiming mergeable on missing data is the one wrong answer here.
    """
    if not gh_pr:
        return None
    decision = str(gh_pr.get("reviewDecision") or "").upper()
    if decision == "CHANGES_REQUESTED":
        return "changes_requested"
    mergeable = str(gh_pr.get("mergeable") or "").upper()
    if decision == "APPROVED" and mergeable == "MERGEABLE":
        return "mergeable"
    return None


def hold_state(
    row: dash.IssueRow | None,
    labels,
) -> tuple[bool, str | None]:
    """Whether this issue is waiting rather than working, and why.

    Parked outranks any journalled dependency hold: the operator's decision is
    the reason the issue is waiting, not the hold that preceded it. One
    definition, because a pending card that said "held" while the attention
    panel stayed silent would be two answers to the same question.
    """
    parked = "ai-parked" in set(labels)
    defer = {} if parked else (_deferred_event(row) if row else {})
    return (parked or bool(defer), "parked" if parked else waiting_label(defer))


def pending_card(issue: dict, row: dash.IssueRow | None) -> dict:
    labels = labels_of(issue)
    held, hold_reason = hold_state(row, labels)
    return {
        "issue": int(issue.get("number") or 0),
        "title": issue.get("title"),
        "url": issue.get("url"),
        "body": issue.get("body"),
        "priority": priority_of(labels),
        "labels": labels,
        "held": held,
        "hold_reason": hold_reason,
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
    failure = " ".join(
        str(event.get(field) or "").lower()
        for field in ("status", "envelope_subtype", "exit_reason")
    )
    outcome = dash.settled_run_from_event(event)
    if isinstance(outcome, dash.FailedRun):
        budget = (
            outcome.reason is ExitReason.BUDGET_EXHAUSTED
            or "error_max_budget_usd" in failure
        )
        timed_out = outcome.reason in {
            ExitReason.IDLE_TIMEOUT,
            ExitReason.WALL_TIMEOUT,
        } or "timeout" in failure
        return {
            "severity": "attention",
            "title": f"Issue #{issue} run stopped",
            "detail": (
                "Budget limit reached"
                if budget
                else "Run timed out" if timed_out else "Agent run failed"
            ),
            "issue": issue,
            "pr": number if kind in dash.REVIEW_KINDS | dash.REPAIR_KINDS else None,
        }
    if isinstance(outcome, dash.BlockedRun):
        return {
            "severity": "attention",
            "title": f"Issue #{issue} needs input",
            "detail": "Agent is blocked on a human reply",
            "issue": issue,
            "pr": None,
        }
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
    if isinstance(outcome, dash.ReviewVerdict):
        if outcome.decision is ReviewDecision.APPROVE:
            approved = True
        elif outcome.decision is ReviewDecision.REQUEST_CHANGES:
            approved = False
        elif outcome.decision is ReviewDecision.ESCALATE:
            return None
        else:
            assert_never(outcome.decision)
        if number is None:
            return None
        return {
            "severity": "info" if approved else "attention",
            "title": (
                f"PR #{number} approved"
                if approved
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


def card_labels(row: dash.IssueRow, gh_issue: dict | None) -> list[str]:
    """An issue card's labels, the v2 world projection first.

    A v2 deployment's GitHub enrichment deliberately carries no labels — the
    projection owns them — so reading only the enrichment left every v2 card
    unlabelled. A v1 row has no projected facts and only GitHub can answer.
    Purely presentational either way: nothing here moves a card between lanes.
    """
    projected = row.facts.get("labels")
    if isinstance(projected, (set, frozenset)):
        return sorted(str(label) for label in projected)
    if isinstance(projected, (tuple, list)):
        return [str(label) for label in projected]
    return labels_of(gh_issue or {})


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
# One screenful of history. The interventions-per-PR tile answers "how much
# hand-holding"; this list answers "what did the operator actually have to do",
# and past a screenful nobody reads it — the journal is the archive.
INTERVENTION_TIMELINE_LIMIT = 50


def intervention_timeline(
    metrics: dash.Metrics,
    limit: int = INTERVENTION_TIMELINE_LIMIT,
) -> list[dict]:
    """The fold's itemized operator actions, newest first and bounded."""
    items = metrics.interventions
    if limit >= 0:
        items = items[len(items) - limit:] if limit else ()
    rows = []
    for item in reversed(items):
        action = dict(item.action)
        kind = OPERATOR_ACTION_STEPS.get(item.kind, item.kind or "operator action")
        # Channel provenance, same rule as the per-issue step list: an action
        # driven over the bare API is never shown as a human's judgement call.
        if action.get("channel") == "api":
            kind = f"{kind} (api)"
        rows.append(
            {
                "ts": item.ts.isoformat() if item.ts is not None else None,
                "action_class": item.action_class,
                "kind": kind,
                "issue": item.issue,
                "pr": item.pr,
                "detail": _operator_note(action),
            }
        )
    return rows


# The attention classes, most urgent first: the harness has literally asked a
# question, then a human owns the outcome, then money, then the queue is merely
# waiting. Ordering is the panel's whole point — a flat list nobody can triage
# is the state the overview already had.
ATTENTION_ORDER = (
    "blocked",
    "escalated",
    "needs-budget-review",
    "spend-blocked",
    "held",
)


def _row_labels(row: dash.IssueRow) -> set[str]:
    labels = row.facts.get("labels", ())
    if isinstance(labels, (tuple, list, set, frozenset)):
        return {str(label) for label in labels}
    return set()


def _attention_reasons(row: dash.IssueRow) -> list[tuple[str, str | None]]:
    """Every reason this open issue is waiting on a person, richest detail
    first. An issue can be several at once (blocked AND out of budget), and
    collapsing that to one row would hide the half the operator must act on."""
    labels = _row_labels(row)
    reasons: list[tuple[str, str | None]] = []
    if "ai-blocked" in labels:
        reasons.append(("blocked", _blocked_reason(row)))
    if "needs-human-review" in labels:
        reasons.append(("escalated", _escalation_reason(row)))
    if "needs-budget-review" in labels:
        reasons.append(("needs-budget-review", None))
    if row.spend_blocks:
        reasons.append(("spend-blocked", f"{row.spend_blocks} spend block(s)"))
    held, hold_reason = hold_state(row, labels)
    if held:
        reasons.append(("held", hold_reason))
    return reasons


def needs_attention(deployment: dash.Deployment) -> list[dict]:
    """Open issues waiting on a person, with the reason each one is waiting."""
    items = []
    for row in deployment.issues:
        if row.done:
            continue
        for kind, detail in _attention_reasons(row):
            items.append(
                {
                    "issue": row.issue,
                    # The world projection carries the title but no URL, and
                    # the overview deliberately makes no GitHub call: an
                    # at-a-glance panel must not depend on a network read.
                    "title": row.facts.get("title"),
                    "kind": kind,
                    "detail": detail,
                }
            )
    return sorted(
        items,
        key=lambda item: (ATTENTION_ORDER.index(item["kind"]), item["issue"]),
    )


def escalation_reasons(deployment: dash.Deployment) -> list[dict]:
    """Why each currently-escalated issue was handed to a human.

    The same extraction the issue card and the drill-down use, so the tile's
    sub-lines and the row an operator opens next cannot state different
    reasons. An escalation whose reason the journal never recorded is still
    listed — "escalated, reason unrecorded" is itself worth seeing.
    """
    return [
        {"issue": row.issue, "reason": _escalation_reason(row)}
        for row in deployment.issues
        if not row.done and _active_escalation_at(row) is not None
    ]


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


SPEND_WINDOW_HOURS = 48


def _hour_floor(ts: datetime) -> datetime:
    return ts.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def spend_buckets(
    charges,
    now: datetime,
    *,
    hours: int = SPEND_WINDOW_HOURS,
) -> list[dict]:
    """Hourly USD totals over the trailing window, oldest bucket first.

    Empty hours are emitted as zero: a sparse series would let the client
    re-space points and read a quiet night as a busy one. A charge outside the
    window — including one stamped in the future by a skewed clock — is dropped
    rather than clamped into an hour it did not happen in.
    """
    if hours <= 0:
        return []
    end = _hour_floor(now)
    totals = {end - timedelta(hours=offset): 0.0 for offset in range(hours)}
    for ts, usd in charges:
        if ts is None:
            continue
        # Only a real number is money. A stringified or absent cost is an
        # unpriceable leg, and coercing one would invent spend rather than
        # report the gap.
        if isinstance(usd, bool) or not isinstance(usd, (int, float)):
            continue
        amount = float(usd)
        if not math.isfinite(amount):
            continue
        bucket = _hour_floor(ts)
        if bucket in totals:
            totals[bucket] += amount
    return [
        {"hour_start_iso": iso(hour), "usd": round(total, 6)}
        for hour, total in sorted(totals.items())
    ]


def spend_per_hour(
    deployment: dash.Deployment,
    now: datetime,
    *,
    hours: int = SPEND_WINDOW_HOURS,
) -> list[dict]:
    """Settled agent-run spend by hour, issue-attributed and deployment-level.

    Estimated (subscription) dollars are spend and are counted. The basis is the
    same legs, over the same rows, that feed `metrics.by_kind` — rows the
    present/done filter hides included, deployment-level runs billed the same
    way — so the series and the headline totals cannot disagree about what a
    leg cost.
    """
    charges = [
        (leg.ts, leg.cost_usd)
        for row in deployment.metrics_issues
        for leg in row.legs
    ]
    charges += [
        (leg.ts, leg.cost_usd)
        for leg in dash.unattributed_legs(deployment.unattributed)
    ]
    return spend_buckets(charges, now, hours=hours)


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
        "interventions_total": metrics.interventions_total,
        "interventions_per_merged_pr": metrics.interventions_per_merged_pr,
        "interventions_by_class": [
            {"name": name, "count": count}
            for name, count in metrics.interventions_by_class
        ],
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
        alive = {h.role: None for h in dep.health}
    return {
        "metrics": metrics_json(dep.metrics),
        "cost_mix": cost_mix(dep.metrics),
        "caps": {
            "issue_cap_usd": issue_cap_usd,
            "daily_cap_usd": daily_cap_usd,
            "today_usd": dep.metrics.today_usd,
        },
        "health": [health_json(h, alive[h.role]) for h in dep.health],
        "needs_attention": needs_attention(dep),
        "escalation_reasons": escalation_reasons(dep),
        "interventions": intervention_timeline(dep.metrics),
        # A count, not the rows: an event naming no issue is a correlation the
        # producer failed to stamp, so the number is a telemetry-bug signal and
        # the bodies belong in the journal, not on the operator's overview.
        "unattributed_count": len(dep.unattributed),
        "warnings": [
            *dep.warnings,
            *([] if scan_ok else ["daemon liveness is unknown"]),
        ],
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
    pr = row.prs[-1] if row.prs else None
    gh_pr = (gh_prs or {}).get(pr) if pr is not None else None
    _human_action_kind, human_wait_since = _human_action(row, gh_issue, gh_pr)
    elapsed, working, idle, idle_active = _duration_breakdown(
        row, started, _clock_end(row, started, now, human_wait_since)
    )
    return {
        "issue": row.issue,
        "title": (gh_issue or {}).get("title"),
        "url": (gh_issue or {}).get("url"),
        "stage": _display_stage(row),
        "attention_reason": _blocked_reason(row),
        "escalation_reason": _escalation_reason(row),
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
        # A paid leg is executing RIGHT NOW. The client tick predicate needs
        # this directly: human_action alone must not dead-stop both clocks
        # while a leg is visibly spending (keifu #142/#181, 2026-08-03).
        "leg_running": bool(row.running) and not row.done,
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
