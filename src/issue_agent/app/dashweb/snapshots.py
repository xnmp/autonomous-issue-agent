"""Journal-side collectors: deployments, liveness, frecency, issue cards.

Every number here comes out of `app/dashboard.py` — `collect` reads the files,
`build_deployment` derives the rows, and this module only reshapes them into the
JSON the contract names. Nothing is re-derived: a stage, a cost or a leg that
disagreed with `issue-agent dashboard` would be a second truth.

The pure half (frecency, card assembly, step collapsing) takes parsed rows; the
impure half (`collect_deployment`, `scan_proc_daemons`) takes paths.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

from ...adapters.journal import default_ledger_path, parse_ts
from .. import cli
from .. import dashboard as dash

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


# ── liveness ──────────────────────────────────────────────────────────────────
def _environ_agent_env(blob: bytes) -> str:
    for item in blob.split(b"\0"):
        if item.startswith(b"AGENT_ENV="):
            return item[len("AGENT_ENV="):].decode(errors="replace")
    return ""


def deployment_of_env(agent_env: str) -> str:
    """`…/config/.active.<name>.env` → `<name>`; "" for anything else."""
    base = os.path.basename(agent_env or "")
    if base.startswith(".active.") and base.endswith(".env"):
        return base[len(".active."):-len(".env")]
    return ""


def scan_proc_daemons(proc_root: str = "/proc") -> tuple[dict[str, set[str]], bool]:
    """(deployment name → the roles with a live process, scan succeeded).

    Linux-only by construction: a daemon's argv (`… issue_agent.app.main fix`)
    carries no deployment marker, and its composed `AGENT_ENV` carries no role,
    so only the pair identifies "the fix daemon of deployment X". A name maps to
    an empty role set when only its launcher (`point-at.sh`) is up — still
    active, no role attributable yet.

    `False` for the second element means the scan itself did not work (no
    /proc, or nothing readable): callers must then fall back to the journal
    staleness heuristic rather than report every daemon dead.
    """
    found: dict[str, set[str]] = {}
    read_any = False
    try:
        pids = [p for p in os.listdir(proc_root) if p.isdigit()]
    except OSError:
        return {}, False
    for pid in pids:
        try:
            with open(os.path.join(proc_root, pid, "environ"), "rb") as fh:
                blob = fh.read()
        except OSError:
            continue  # another user's process, or it exited mid-scan
        read_any = True
        name = deployment_of_env(_environ_agent_env(blob))
        if not name:
            continue
        roles = found.setdefault(name, set())
        try:
            with open(os.path.join(proc_root, pid, "cmdline"), "rb") as fh:
                argv = fh.read().decode(errors="replace").split("\0")
        except OSError:
            continue
        roles.update(a for a in argv if a in dash.ROLE_NAMES)
    return found, read_any


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


# ── deployment collection ─────────────────────────────────────────────────────
@dataclass(frozen=True)
class Snapshot:
    """One deployment's journal truth plus the config context the API needs."""

    name: str
    repo: str
    target_dir: str
    deployment: dash.Deployment
    timestamps: tuple[datetime | None, ...]

    @property
    def last_activity(self) -> datetime | None:
        stamps = [ts for ts in self.timestamps if ts]
        return max(stamps) if stamps else None


def overlay_field(name: str, field: str) -> str:
    return cli.read_repo_env_field(os.path.join(cli.REPOS_DIR, f"{name}.env"), field)


def deployment_names(repos_dir: str | None = None) -> tuple[str, ...]:
    return dash.deployment_names(repos_dir if repos_dir is not None else cli.REPOS_DIR)


def collect_deployment(
    name: str, now_fn=None, stale_after_s: float = dash.STALE_AFTER_S
) -> Snapshot:
    """Read one deployment's journals + the global ledger (the only I/O here)."""
    repo = overlay_field(name, "REPO")
    deployment = dash.collect(
        name,
        repo,
        cli.journal_paths(name),
        cli.log_dir(),
        default_ledger_path(),
        now_fn=now_fn,
        stale_after_s=stale_after_s,
    )
    stamps = tuple(
        parse_ts(e.get("ts"))
        for row in deployment.issues
        for e in row.events
    ) + tuple(parse_ts(e.get("ts")) for e in deployment.unattributed)
    return Snapshot(
        name=name,
        repo=repo,
        target_dir=overlay_field(name, "TARGET_DIR"),
        deployment=deployment,
        timestamps=stamps,
    )


def harness_repo(harness_root: str | None = None) -> str:
    """The harness's own `owner/name`, for the `is_self` sidebar marker. "" when
    it cannot be read — a missing self-marker is cosmetic."""
    root = harness_root or cli.HARNESS_ROOT
    return cli.parse_remote_url(
        cli._run(["git", "remote", "get-url", "origin"], cwd=root)
    )


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


def repos_payload(snapshots, live: dict[str, set[str]], scan_ok: bool, now: datetime) -> dict:
    """`GET /api/repos`, pre-sorted by frecency then last activity."""
    self_repo = harness_repo()
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
            "model": leg.model or None,
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
            and parse_ts(e.get("ts")) == leg.ts
            and str(e.get("kind") or "?") == leg.kind
        ):
            return e
    return {}


def _turns(row: dash.IssueRow, leg: dash.Leg):
    return _settled_event(row, leg).get("turns")


def _usage(row: dash.IssueRow, leg: dash.Leg):
    usage = _settled_event(row, leg).get("usage")
    return usage if isinstance(usage, dict) else None


# ── issue cards ───────────────────────────────────────────────────────────────
def _started(row: dash.IssueRow) -> datetime | None:
    stamps = [ts for ts in (parse_ts(e.get("ts")) for e in row.events) if ts]
    return min(stamps) if stamps else None


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
            spans.append((last - start).total_seconds())
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
        "elapsed_s": round((end - started).total_seconds()) if started else None,
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


def backlog_card(issue: dict, now: datetime) -> dict:
    labels = labels_of(issue)
    return {
        "issue": int(issue.get("number") or 0),
        "title": issue.get("title"),
        "url": issue.get("url"),
        "labels": labels,
        "recurrence": "recurrence" in labels,
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
    ts = parse_ts(created_at)
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
    snapshot: Snapshot, live: dict[str, set[str]], scan_ok: bool
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
            "issue_cap_usd": cli._env_cap(snapshot.name, "ISSUE_SPEND_CAP_USD", 0.0) or None,
            "daily_cap_usd": cli.daily_cap(snapshot.name) or None,
            "today_usd": dep.metrics.today_usd,
        },
        "health": [health_json(h, alive[h.role]) for h in dep.health],
        "unattributed": list(dep.unattributed[-20:]),
        "warnings": [] if scan_ok else ["daemon liveness inferred from journal staleness"],
    }


# ── issue lookup ──────────────────────────────────────────────────────────────
def find_row(snapshot: Snapshot, issue: int) -> dash.IssueRow | None:
    for row in snapshot.deployment.issues:
        if row.issue == issue:
            return row
    return None


def issue_detail(row: dash.IssueRow, now: datetime, gh_issue: dict | None = None) -> dict:
    started = _started(row)
    end = row.last_ts if (row.done and row.last_ts) else now
    return {
        "issue": row.issue,
        "title": (gh_issue or {}).get("title"),
        "url": (gh_issue or {}).get("url"),
        "stage": row.stage,
        "done": row.done,
        "prs": list(row.prs),
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
    settled = parse_ts(steps[idx]["ts"])
    if settled is None:
        return []
    running = steps[idx]["state"] == "running"
    dispatches = [
        ts
        for ts in (
            parse_ts(e.get("ts"))
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
        if (ts := parse_ts(e.get("ts"))) is not None
        and opened <= ts
        and (running or ts <= settled)
    ]
