"""Interactive web dashboard: a FastAPI backend over the derived state.

The board is `domain.state.derive` over the roles' observation and the
control store (read-only); metrics come from the `legs` table plus GitHub.
The only writes are two label changes (promote, unblock).

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
