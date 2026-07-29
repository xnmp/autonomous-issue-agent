"""Journal collection and text/HTML rendering for the dashboard."""

from __future__ import annotations

import glob
import html as _html
import json
import os
from datetime import datetime, timezone

from ..adapters.journal import read_events
from ..domain.dashboard import *
from ..domain.dashboard import _utcnow

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
