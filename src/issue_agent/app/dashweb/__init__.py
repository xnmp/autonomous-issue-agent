"""Interactive web dashboard — FastAPI backend over the journal-only aggregation.

`app/dashboard.py` stays the single source of truth for costs, stages and legs;
this package adds the three things a text snapshot cannot have: live polling,
cached read-only `gh` context (titles, PR state, the backlog), and the three
operator actions that only a loopback server can offer.

fastapi/uvicorn live in the optional `dash` extra, so nothing here may be
imported by a daemon path. The submodules are exposed lazily for exactly that
reason: `import issue_agent.app.dashweb` must not pull fastapi into a process
that only wanted the collectors.
"""

from __future__ import annotations

__all__ = ["create_app", "serve"]


def __getattr__(name: str):
    if name in ("create_app", "serve"):
        from . import server

        return getattr(server, name)
    raise AttributeError(name)
