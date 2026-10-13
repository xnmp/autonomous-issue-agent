"""The dashboard's own entry point: `python -m issue_agent.app.dashweb.main`.

The dashboard is a separate component: the core CLI must not import it, so it
launches from here. It is a module entry rather than a console script because
the core's entry-point drift check heals every declared script. Serving is the default; `--text` and `--html` render once.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

DEFAULT_PORT = 8321


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="issue-agent-dashboard", description=__doc__)
    parser.add_argument("names", nargs="*", help="deployments to render (default: all)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="loopback port to serve on")
    parser.add_argument("--text", action="store_true", help="print the board once and exit")
    parser.add_argument("--html", metavar="PATH", help="write the board as one HTML page and exit")
    return parser


def _ready_bundle(root: str) -> str:
    """Empty when the SPA bundle matches this checkout (rebuilding it once
    if stale), else why it cannot be served."""
    from ...adapters.dashweb.build import build_dashboard
    from ...adapters.dashweb.bundle import bundle_status

    if bundle_status(root).ready:
        return ""
    code = build_dashboard(Path(root))
    after = bundle_status(root)
    return "" if code == 0 and after.ready else f"dashboard frontend rebuild failed (exit {code}; {after.detail})"


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    from .service import HARNESS_ROOT, Backend

    if args.text or args.html:
        from .. import dashboard as render

        backend = Backend()
        names = args.names or list(backend.names())
        views = [render.collect(name, backend=backend, observe=True) for name in names]
        if args.text:
            sys.stdout.write(render.render_text(views, details=True))
            return 0
        path = os.path.abspath(os.path.expanduser(args.html))
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(render.render_html(views))
        return 0
    try:
        problem = _ready_bundle(HARNESS_ROOT)
    except Exception as exc:  # noqa: BLE001 - startup failures are reported, not raised
        problem = f"{type(exc).__name__}: {exc}"
    if problem:
        from .server import build_command

        sys.stderr.write(f"dashboard startup failed: {problem}\n  rebuild: {build_command()}\n")
        return 2
    from .server import serve

    sys.stderr.write(f"serving the dashboard at http://127.0.0.1:{args.port}/ (Ctrl-C to stop)\n")
    try:
        return serve(args.port)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
