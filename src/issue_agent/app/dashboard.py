"""One-shot text and HTML renders of the dashboard.

They read the same model as the web dashboard (`app/dashweb/service.Backend`):
the board is each item's derived state, the metrics come from `legs`.
`python -m issue_agent.app.dashweb.main --text/--html` uses them.
"""

from __future__ import annotations

import html as _html
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

from ..domain import dashboard as dash

__all__ = ["DeploymentView", "collect", "render_html", "render_text"]

_GROUPS = (("needs_you", "Needs you"), ("agent", "Agent running"), ("ci", "CI & GitHub"),
           ("slot", "Waiting for a slot"), ("waiting", "Waiting"))


@dataclass(frozen=True)
class DeploymentView:
    name: str
    repo: str
    metrics: dash.Metrics
    board: tuple[dict, ...] | None
    warnings: tuple[str, ...]


def collect(name: str, repo: str = "", *, backend=None, observe: bool = False) -> DeploymentView:
    """One deployment's metrics from `legs`, and with `observe` its board
    (a blocking GitHub read)."""
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
    metrics = dash.metrics(
        legs, merged_prs=merged, open_items=len(board) if board is not None else None,
        needs_human=sum(1 for card in board or () if card["board"]["status"] == "needs_you") if board else None,
    )
    return DeploymentView(name, dep.cfg.repo or repo, metrics, board, tuple(warnings))


def _rows(view: DeploymentView) -> Iterable[tuple[str, str, list[dict]]]:
    for group, title in _GROUPS:
        yield group, title, [card for card in view.board or () if card["board"]["group"] == group]


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
