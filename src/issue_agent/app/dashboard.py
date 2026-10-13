"""One-shot text and HTML renders of the dashboard, and the plain HTML server.

They read the same model as the web dashboard (`app/dashweb/service.Backend`):
the board is each item's derived state, the metrics come from `legs`.
`python -m issue_agent.app.dashweb.main --text/--html` uses them; so does `app/cli.py` until it
drops its dashboard imports (the core must not import the dashboard).
"""

from __future__ import annotations

import html as _html
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

from ..adapters.dashweb.system import deployment_names
from ..domain import dashboard as dash
from ..domain.dashboard import STALE_AFTER_S

__all__ = ["DeploymentView", "collect", "deployment_names", "render_html", "render_text", "serve"]

_GROUPS = (("needs_you", "Needs you"), ("working", "Working"), ("waiting", "Waiting"))


@dataclass(frozen=True)
class DeploymentView:
    name: str
    repo: str
    metrics: dash.Metrics
    board: tuple[dict, ...] | None
    warnings: tuple[str, ...]


def collect(
    name: str, repo: str = "", *_legacy: object, stale_after_s: float = STALE_AFTER_S, control_db_path: str = "",
    backend=None, observe: bool = False,
) -> DeploymentView:
    """One deployment's metrics from `legs`, and with `observe` its board
    (a blocking GitHub read). The positional and keyword arguments after
    `repo` are the CLI's old journal-fold parameters and are ignored."""
    del _legacy, stale_after_s, control_db_path
    from .dashweb.service import Backend

    backend = backend or Backend()
    dep = backend.deployment(name)
    legs, warnings = backend.legs(dep)
    board: tuple[dict, ...] | None = None
    merged = None
    if observe:
        payload = backend.issues(name, wait=True)
        board = tuple(payload["in_flight"])
        merged = len(payload["done"])
        warnings = [*warnings, *payload["warnings"]]
    now = backend.now_fn()
    metrics = dash.metrics(
        legs, merged_prs=merged, open_items=len(board) if board is not None else None,
        needs_human=sum(1 for card in board or () if card["board"]["status"] == "needs_you") if board else None,
        today_start=now.replace(hour=0, minute=0, second=0, microsecond=0),
    )
    return DeploymentView(name, dep.cfg.repo or repo, metrics, board, tuple(warnings))


def _rows(view: DeploymentView) -> Iterable[tuple[str, str, list[dict]]]:
    for group, title in _GROUPS:
        yield group, title, [card for card in view.board or () if card["board"]["status"] == group]


def render_text(deployments: Iterable[DeploymentView], now_fn=None, details: bool = False) -> str:
    del now_fn
    lines: list[str] = []
    for view in deployments:
        lines.append(f"== {view.name} ({view.repo or 'no REPO'}) ==")
        lines.append(dash.metrics_line(view.metrics))
        lines += [f"! {warning}" for warning in view.warnings]
        if view.board is None:
            lines.append("(board not observed)")
        for _group, title, cards in _rows(view):
            if not cards:
                continue
            lines.append(f"{title}:")
            for card in cards:
                status = card["board"]
                pr = f"  PR #{card['pr']}" if card.get("pr") else ""
                lines.append(f"  #{card['issue']:<6} {status['headline']}{pr}  ${card['cost_usd']:.2f}")
                if details:
                    lines.append(f"           {status.get('reason') or status.get('detail') or ''}".rstrip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_html(deployments: Iterable[DeploymentView], now_fn=None) -> str:
    now = (now_fn or (lambda: datetime.now(timezone.utc)))()
    esc = _html.escape
    parts = ["<!doctype html><meta charset='utf-8'><title>issue-agent dashboard</title>",
             "<style>body{font:14px/1.5 ui-monospace,monospace;margin:2rem;background:#0e1330;color:#d8dce8}"
             "td,th{padding:.2rem .6rem;text-align:left}h2{margin-top:2rem}</style>",
             f"<p>generated {esc(now.isoformat())}</p>"]
    for view in deployments:
        parts.append(f"<h2>{esc(view.name)} <small>{esc(view.repo)}</small></h2>")
        parts.append(f"<p>{esc(dash.metrics_line(view.metrics))}</p>")
        parts += [f"<p>! {esc(warning)}</p>" for warning in view.warnings]
        for _group, title, cards in _rows(view):
            if not cards:
                continue
            parts.append(f"<h3>{esc(title)}</h3><table><tr><th>issue</th><th>status</th><th>PR</th><th>cost</th></tr>")
            for card in cards:
                parts.append(
                    f"<tr><td>#{card['issue']} {esc(card.get('title') or '')}</td>"
                    f"<td>{esc(card['board']['headline'])}</td><td>{card.get('pr') or ''}</td>"
                    f"<td>${card['cost_usd']:.2f}</td></tr>"
                )
            parts.append("</table>")
    return "".join(parts)


# ── serving ───────────────────────────────────────────────────────────────────
def _serve_until_reload(server, reload_check, poll_seconds: float) -> int:
    import threading

    if reload_check is None:
        server.serve_forever()
        return 0

    stopped = threading.Event()
    reload_requested = threading.Event()

    def watch() -> None:
        while not stopped.wait(poll_seconds):
            if not reload_check():
                continue
            reload_requested.set()
            server.shutdown()
            return

    watcher = threading.Thread(target=watch, name="issue-agent-dashboard-fallback-reload", daemon=True)
    watcher.start()
    try:
        server.serve_forever()
    finally:
        stopped.set()
        server.server_close()
        watcher.join(timeout=max(1.0, poll_seconds * 2))
    if reload_requested.is_set():
        from ..adapters.reload import RELOAD_EXIT_CODE

        return RELOAD_EXIT_CODE
    return 0


def serve(
    port: int,
    build_html: Callable[[], str],
    host: str = "127.0.0.1",
    *,
    reload_check=None,
    poll_seconds: float = 1.0,
) -> int:
    """Serve a freshly rendered page on every GET, on loopback, until Ctrl-C."""
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

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    return _serve_until_reload(HTTPServer((host, port), Handler), reload_check, poll_seconds)
