"""FastAPI app factory, static SPA mount, and the loopback runner.

The HTTP layer is deliberately thin: every route calls one collector and returns
what it produced. `Backend` is the composition point (journal snapshots + cached
gh + usage meters), and `create_app(backend=…)` takes any object with the same
methods, which is how the endpoint tests run without touching disk, `gh`, or the
network.

**Read-only failure posture.** No route raises for a collector failure. A
missing journal, a dead `gh`, an uninstalled codex — each yields empty data and
a `warnings` entry, because a dashboard that 500s during an incident is a
dashboard that is not there during incidents. The only non-200s are 404 (a
deployment or issue that does not exist, a chart that cannot be generated) and
400 (a malformed action body).
"""

from __future__ import annotations

import html
import mimetypes
import os
import sys
import threading
from collections.abc import Callable

from ...adapters.dashweb import actions, costchart
from ...adapters.dashweb.bundle import bundle_snapshot, published_dist
from ...adapters.control_path import canonical_control_store_path
from .. import cli
from . import contracts
from .ports import DashboardBackend
from .service import Backend

HOST = "127.0.0.1"
# A cross-site form POST cannot set a custom header, and a cross-site fetch that
# sets one is preflighted — with no CORS middleware the preflight fails. So
# requiring this header on every write is what stops a page the operator merely
# VISITS from launching daemons or relabelling issues.
CSRF_HEADER = "x-issue-agent-dashboard"
GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS = 30
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")


def loopback_host(host_header: str) -> bool:
    """True when a Host header names this machine. Rejecting anything else is
    the DNS-rebinding guard: loopback binding stops other hosts from connecting,
    but not the operator's own browser from being pointed here by a hostile
    name that resolves to 127.0.0.1."""
    if not host_header:
        return False
    name = host_header.rsplit(":", 1)[0] if not host_header.endswith("]") else host_header
    return name.strip().lower() in LOOPBACK_HOSTS


def same_origin(origin: str, host_header: str) -> bool:
    """An Origin, when the browser sends one, must be this server itself."""
    if not origin:
        return True
    prefix = origin.split("://", 1)[-1] if "://" in origin else origin
    return prefix.strip().lower() == host_header.strip().lower()


def dist_dir(harness_root: str | None = None) -> str:
    return str(published_dist(harness_root or cli.HARNESS_ROOT))


def dist_ready(harness_root: str | None = None) -> bool:
    return bundle_snapshot(harness_root or cli.HARNESS_ROOT).status.ready


# ── HTTP ──────────────────────────────────────────────────────────────────────
def create_app(
    backend: DashboardBackend | None = None,
    harness_root: str | None = None,
    offload_backend: bool | None = None,
):
    from fastapi import Body, FastAPI, Header, HTTPException, Query
    from fastapi.encoders import jsonable_encoder
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import HTMLResponse, JSONResponse, Response
    from starlette.concurrency import run_in_threadpool

    backend = backend or Backend(
        harness_root=harness_root,
        control_db_path=canonical_control_store_path(),
    )
    should_offload = isinstance(backend, Backend) if offload_backend is None else offload_backend
    root = harness_root or getattr(backend, "harness_root", cli.HARNESS_ROOT)
    assets = bundle_snapshot(root, pin=True)
    assets_ready = assets.status.ready
    dist = str(assets.dist) if assets_ready else os.path.join(root, "dashboard", "dist")
    app = FastAPI(title="issue-agent dashboard", docs_url=None, redoc_url=None)
    if assets_ready:
        app.state.dashboard_bundle_snapshot = assets
    elif assets.lease is not None:
        assets.lease.close()

    def degraded(kind: str, exc: Exception, **empty) -> dict:
        return {
            **empty,
            "warnings": [f"{kind} unavailable ({type(exc).__name__}: {exc})"],
        }

    @app.exception_handler(RequestValidationError)
    async def rejected_body(_request, exc: RequestValidationError):
        """A refused body must render as a 422, never as a 500.

        FastAPI's default handler echoes the offending `input` back, and a body
        can legally carry text that JSON accepts but UTF-8 cannot encode (a lone
        surrogate such as "\\ud800"): rendering it raises UnicodeEncodeError
        INSIDE the error response, and the operator gets a 500 for a validation
        failure. Sanitizing every string in the payload fixes the class rather
        than the one field that surfaced it.
        """
        return JSONResponse(
            {"detail": _encodable(jsonable_encoder(exc.errors()))},
            status_code=422,
        )

    @app.middleware("http")
    async def guard(request, call_next):
        host = request.headers.get("host", "")
        if not loopback_host(host):
            return JSONResponse({"detail": "host not allowed"}, status_code=421)
        if not same_origin(request.headers.get("origin", ""), host):
            return JSONResponse({"detail": "cross-origin request"}, status_code=403)
        if request.method not in ("GET", "HEAD") and CSRF_HEADER not in request.headers:
            return JSONResponse(
                {"detail": f"missing {CSRF_HEADER} header"}, status_code=403
            )
        return await call_next(request)

    async def invoke(call, *args):
        if should_offload:
            return await run_in_threadpool(call, *args)
        return call(*args)

    async def require_known(name: str) -> None:
        if not await invoke(backend.known, name):
            raise HTTPException(404, f"no such deployment: '{name}'")

    @app.get("/api/repos", response_model=contracts.ReposPayload)
    async def api_repos():
        try:
            return await invoke(backend.repos)
        except Exception as exc:  # noqa: BLE001
            return degraded("deployment scan", exc, repos=[])

    @app.get("/api/usage", response_model=contracts.UsagePayload)
    async def api_usage():
        try:
            return await invoke(backend.usage)
        except Exception as exc:  # noqa: BLE001
            return degraded("usage meters", exc, claude=None, codex=None, harness=None)

    @app.get("/api/verification", response_model=contracts.VerificationPayload)
    async def api_verification():
        try:
            return await invoke(backend.verification)
        except Exception as exc:  # noqa: BLE001
            return degraded("verification ledger", exc, rows=[])

    @app.get("/api/repos/{name}/overview", response_model=contracts.OverviewPayload)
    async def api_overview(
        name: str,
        timezone_name: str | None = Query(default=None, alias="timezone", max_length=100),
    ):
        await require_known(name)
        try:
            return await invoke(backend.overview, name, timezone_name)
        except Exception as exc:  # noqa: BLE001
            return degraded(
                "overview",
                exc,
                metrics=None,
                cost_mix=None,
                caps=None,
                health=[],
                needs_attention=[],
                escalation_reasons=[],
                interventions=[],
                unattributed_count=0,
                health_trend=[],
            )

    @app.get("/api/repos/{name}/issues", response_model=contracts.IssuesPayload)
    async def api_issues(name: str):
        await require_known(name)
        try:
            return await invoke(backend.issues, name)
        except Exception as exc:  # noqa: BLE001
            return degraded(
                "issues", exc, in_flight=[], pending=[], done=[], backlog=[], gh_ok=False
            )

    @app.get(
        "/api/repos/{name}/issues/{number}",
        response_model=contracts.IssueDetailPayload,
        response_model_exclude_unset=True,
    )
    async def api_issue(name: str, number: int):
        await require_known(name)
        try:
            detail = await invoke(backend.issue, name, number)
        except Exception as exc:  # noqa: BLE001
            return degraded(
                "issue detail",
                exc,
                issue=number,
                title=None,
                url=None,
                stage=None,
                done=False,
                prs=[],
                pull_requests=[],
                cost_usd=0.0,
                elapsed_s=None,
                working_s=None,
                idle_s=None,
                idle_active=False,
                steps=[],
                events=[],
            )
        if detail is None:
            raise HTTPException(404, f"issue #{number} has no journal history in '{name}'")
        return detail

    @app.get(
        "/api/repos/{name}/issues/{number}/steps/{idx}/events",
        response_model=contracts.StepEventsPayload,
    )
    async def api_step_events(name: str, number: int, idx: int):
        await require_known(name)
        try:
            payload = await invoke(backend.step_events, name, number, idx)
        except Exception as exc:  # noqa: BLE001
            return degraded("step events", exc, events=[])
        if payload is None:
            raise HTTPException(404, f"issue #{number} has no journal history in '{name}'")
        return payload

    @app.get("/api/repos/{name}/issues/{number}/cost-chart")
    async def api_cost_chart(name: str, number: int, session_id: str = ""):
        await require_known(name)
        if not costchart.valid_session_id(session_id):
            raise HTTPException(400, "session_id must match ^[A-Za-z0-9_-]+$")
        chart, error = await invoke(backend.cost_chart, session_id)
        if error:
            raise HTTPException(404, error)
        # The chart is generated from a transcript containing untrusted text
        # (issue bodies, PR comments, fetched pages). `sandbox` drops it into an
        # opaque origin, so script in it cannot reach the action endpoints.
        return HTMLResponse(
            chart, headers={"Content-Security-Policy": "sandbox; default-src 'none'"}
        )

    @app.post("/api/repos/{name}/start", response_model=contracts.ActionResult)
    async def api_start(name: str):
        ok, detail = await invoke(backend.start, name)
        return JSONResponse({"ok": ok, "detail": detail}, status_code=200 if ok else 400)

    @app.post(
        "/api/repos/{name}/issues/{number}/promote",
        response_model=contracts.ActionResult,
    )
    async def api_promote(name: str, number: int):
        if number < 1:
            raise HTTPException(400, "issue number must be positive")
        ok, detail = await invoke(backend.promote, name, number)
        return JSONResponse({"ok": ok, "detail": detail}, status_code=200 if ok else 400)

    @app.post(
        "/api/repos/{name}/issues/{number}/unblock",
        response_model=contracts.ActionResult,
    )
    async def api_unblock(
        name: str,
        number: int,
        body: contracts.UnblockRequest | None = None,
        user_agent: str = Header(default="", include_in_schema=False),
    ):
        if number < 1:
            raise HTTPException(400, "issue number must be positive")
        # Which hand pulled the lever: the browser UI or a bare API caller
        # (curl, an agent). A courtesy provenance stamp, not authentication —
        # the drill-down badges API-channel unblocks so operator history and
        # automation history stay tellable apart.
        channel = "ui" if user_agent.startswith("Mozilla") else "api"
        # Unlike the other actions this one reads (snapshot + GitHub) before it
        # writes, so it has collector failure modes: report them as a refused
        # action, never as a 500 the operator cannot read.
        try:
            ok, detail = await invoke(
                backend.unblock, name, number, channel
            )
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"unblock unavailable ({type(exc).__name__}: {exc})"
        return JSONResponse({"ok": ok, "detail": detail}, status_code=200 if ok else 400)

    @app.post(
        "/api/repos/{name}/issues/{number}/park",
        response_model=contracts.ActionResult,
    )
    async def api_park(
        name: str,
        number: int,
        user_agent: str = Header(default="", include_in_schema=False),
    ):
        if number < 1:
            raise HTTPException(400, "issue number must be positive")
        channel = "ui" if user_agent.startswith("Mozilla") else "api"
        # Same collector failure modes as unblock: read before write, so a
        # broken GitHub read is a refusal the operator can act on, not a 500.
        try:
            ok, detail = await invoke(backend.park, name, number, channel)
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"park unavailable ({type(exc).__name__}: {exc})"
        return JSONResponse({"ok": ok, "detail": detail}, status_code=200 if ok else 400)

    @app.post(
        "/api/repos/{name}/issues/{number}/priority",
        response_model=contracts.ActionResult,
    )
    async def api_priority(name: str, number: int, body: dict = Body(default={})):
        if number < 1:
            raise HTTPException(400, "issue number must be positive")
        priority = body.get("priority") if isinstance(body, dict) else None
        if priority is not None and priority not in actions.PRIORITY_LABELS:
            raise HTTPException(400, "priority must be P1, P2, P3 or null")
        ok, detail = await invoke(backend.priority, name, number, priority)
        return JSONResponse({"ok": ok, "detail": detail}, status_code=200 if ok else 400)

    @app.get("/{full_path:path}")
    async def spa(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(404, "no such endpoint")
        index = os.path.join(dist, "index.html")
        if not assets_ready:
            return HTMLResponse(instructions_page(root))
        asset = _safe_asset(dist, full_path)
        path = asset or index
        with open(path, "rb") as handle:
            content = handle.read()
        media_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        return Response(content, media_type=media_type)

    return app


def _encodable(value):
    """The same structure with every string forced to encodable UTF-8, so a
    JSONResponse over it cannot raise. Lone surrogates become U+FFFD."""
    if isinstance(value, str):
        return value.encode("utf-8", "replace").decode("utf-8")
    if isinstance(value, dict):
        return {_encodable(k): _encodable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encodable(v) for v in value]
    return value


def _safe_asset(dist: str, relative: str) -> str:
    """A file inside `dist` for this URL path, or "" — path traversal never
    escapes the build directory."""
    if not relative:
        return ""
    candidate = os.path.realpath(os.path.join(dist, relative))
    base = os.path.realpath(dist)
    if candidate != base and not candidate.startswith(base + os.sep):
        return ""
    return candidate if os.path.isfile(candidate) else ""


def instructions_page(harness_root: str | None = None) -> str:
    return (
        "<!doctype html><meta charset='utf-8'>"
        "<title>issue-agent dashboard</title>"
        "<style>body{font:14px/1.6 ui-monospace,monospace;margin:3rem auto;"
        "max-width:44rem;padding:0 1rem;background:#0e1330;color:#d8dce8}"
        "code{background:#141a3d;padding:.15rem .35rem;border-radius:3px}</style>"
        "<h1>dashboard UI unavailable</h1>"
        "<p>The API is live; the single-page app is missing, stale, or invalid:</p>"
        f"<pre><code>{html.escape(cli.dashboard_build_command(harness_root))}</code></pre>"
        "<p>Then reload this page. The JSON API is available under "
        "<code>/api/</code> in the meantime.</p>"
    )


def _run_server_until_reload(
    server,
    reload_check: Callable[[], bool] | None,
    *,
    poll_seconds: float = 1.0,
    log: Callable[[str], None] | None = None,
) -> int:
    """Run one uvicorn instance and request its graceful shutdown on reload.

    Setting ``should_exit`` makes uvicorn close its listeners and wait for
    active requests before ``run`` returns. Only then do we return exit 87 to
    the process supervisor, so no response is cut in half.
    """
    if reload_check is None:
        server.run()
        return 0

    stopped = threading.Event()
    reload_requested = threading.Event()

    def watch() -> None:
        while not stopped.wait(poll_seconds):
            if not reload_check():
                continue
            reload_requested.set()
            message = (
                "dashboard: source changed — exiting for supervisor restart; "
                "re-run `issue-agent dashboard` if unsupervised"
            )
            (log or (lambda value: print(value, file=sys.stderr)))(message)
            server.should_exit = True
            return

    watcher = threading.Thread(
        target=watch,
        name="issue-agent-dashboard-reload",
        daemon=True,
    )
    watcher.start()
    try:
        server.run()
    finally:
        stopped.set()
        watcher.join(timeout=max(1.0, poll_seconds * 2))
    if reload_requested.is_set():
        from ...adapters.reload import RELOAD_EXIT_CODE

        return RELOAD_EXIT_CODE
    return 0


def serve(
    port: int, host: str = HOST, backend: DashboardBackend | None = None
) -> int:
    """Run the dashboard on loopback. Loopback is not a default but a
    constraint: the POST actions launch daemons and relabel issues."""
    if host not in ("127.0.0.1", "::1"):
        raise ValueError("dashboard actions require a literal loopback listener")
    import uvicorn

    reload_check = None
    if os.environ.get("HOT_RELOAD", "1") != "0":
        from ...adapters.reload import ReloadWatch

        reload_check = ReloadWatch(cli.HARNESS_ROOT).changed
    config = uvicorn.Config(
        create_app(backend=backend),
        host=host,
        port=port,
        log_level="warning",
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS,
    )
    return _run_server_until_reload(uvicorn.Server(config), reload_check)
