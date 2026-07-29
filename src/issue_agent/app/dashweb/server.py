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
import os
from datetime import datetime, timezone

from .. import cli
from .. import dashboard as dash
from . import actions, costchart, ghdata, snapshots, usage, verification

HOST = "127.0.0.1"
# A cross-site form POST cannot set a custom header, and a cross-site fetch that
# sets one is preflighted — with no CORS middleware the preflight fails. So
# requiring this header on every write is what stops a page the operator merely
# VISITS from launching daemons or relabelling issues.
CSRF_HEADER = "x-issue-agent-dashboard"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
    return os.path.join(harness_root or cli.HARNESS_ROOT, "dashboard", "dist")


def dist_ready(harness_root: str | None = None) -> bool:
    return os.path.isfile(os.path.join(dist_dir(harness_root), "index.html"))


class Backend:
    """Every collector the API exposes, over one cached `gh` reader."""

    def __init__(
        self,
        harness_root: str | None = None,
        gh: ghdata.GhReader | None = None,
        now_fn=None,
        stale_after_s: float = dash.STALE_AFTER_S,
        snapshot_ttl_s: float = 3.0,
    ):
        self.harness_root = harness_root or cli.HARNESS_ROOT
        self.gh = gh or ghdata.GhReader()
        self.now_fn = now_fn or _utcnow
        self.stale_after_s = stale_after_s
        # Building one snapshot re-reads three journals AND sources the overlay
        # through bash (the CLI's shell-semantics read). At the UI's 5s poll,
        # across three deployments and two views, that is a lot of forks for
        # data that cannot have changed. A few seconds of staleness is free —
        # the daemons themselves tick every 20s.
        self._snapshots = ghdata.TtlCache(ttl_s=snapshot_ttl_s)

    # ── journal side ──
    def names(self) -> tuple[str, ...]:
        return snapshots.deployment_names()

    def known(self, name: str) -> bool:
        """A deployment the operator actually configured.

        Every route gates on this. `name` reaches a subprocess argv and several
        filesystem paths, so an unvalidated one is both an injection surface and
        an unbounded-work surface (each miss forks bash to source an overlay
        that does not exist, and pins a cache entry)."""
        return name in self.names()

    def snapshot(self, name: str) -> snapshots.Snapshot:
        cached = self._snapshots.get(name)
        if cached is not None:
            return cached
        return self._snapshots.put(
            name,
            snapshots.collect_deployment(
                name, now_fn=self.now_fn, stale_after_s=self.stale_after_s
            ),
        )

    def repos(self) -> dict:
        live, scan_ok = snapshots.scan_proc_daemons()
        snaps = [self.snapshot(name) for name in self.names()]
        return snapshots.repos_payload(snaps, live, scan_ok, self.now_fn())

    def usage(self) -> dict:
        return usage.usage_payload(self.names())

    def verification(self) -> dict:
        return verification.verification_payload(
            verification.default_path(self.harness_root)
        )

    def overview(self, name: str) -> dict:
        live, scan_ok = snapshots.scan_proc_daemons()
        snapshot = self.snapshot(name)
        all_snapshots = [self.snapshot(candidate) for candidate in self.names()]
        issues, issue_error = self.gh.issues(snapshot.repo)
        recurrence_events = [
            event
            for row in snapshot.deployment.issues
            for event in row.events
            if event.get("event") == "recurrence"
        ]
        payload = snapshots.overview_payload(snapshot, live, scan_ok)
        payload["health_trend"] = snapshots.health_trend(
            issues,
            recurrence_events,
            first_class_day=snapshots.first_observed_class_day(snapshot, all_snapshots),
        )
        if issue_error:
            payload["warnings"].append(f"GitHub issue history unavailable: {issue_error}")
        elif isinstance(issues, list) and len(issues) >= ghdata.ISSUE_HISTORY_LIMIT:
            payload["warnings"].append(
                "GitHub issue history may be incomplete: reached the 1,000-issue fetch limit"
            )
        return payload

    # ── gh-augmented ──
    def _gh_context(self, repo: str) -> tuple[dict, dict, list[str]]:
        issues_payload, issues_error = self.gh.issues(repo)
        prs_payload, prs_error = self.gh.prs(repo)
        warnings = [m for m in (issues_error, prs_error) if m]
        return (
            ghdata.index_by_number(issues_payload),
            ghdata.index_by_number(prs_payload),
            warnings,
        )

    def issues(self, name: str) -> dict:
        snap = self.snapshot(name)
        now = self.now_fn()
        gh_issues, gh_prs, warnings = self._gh_context(snap.repo)
        rows = snap.deployment.issues
        metrics = snap.deployment.metrics
        projected_cost = metrics.cost_per_done_usd if metrics.done_issues else None
        projected_eta = snapshots.mean_wall_clock_s(rows)

        def card(row):
            return snapshots.issue_card(
                row,
                now,
                gh_issue=gh_issues.get(row.issue),
                gh_prs=gh_prs,
                projected_cost_usd=projected_cost,
                projected_eta_s=projected_eta,
            )

        row_by_issue = {row.issue: row for row in rows}
        in_flight_numbers = {row.issue for row in rows if not row.done}
        pending, backlog = [], []
        for number, issue in sorted(gh_issues.items()):
            if not ghdata.is_open(issue):
                continue
            labels = snapshots.labels_of(issue)
            if "ai-ready" in labels and number not in in_flight_numbers:
                pending.append(snapshots.pending_card(issue, row_by_issue.get(number)))
            elif snapshots.is_backlog(issue):
                backlog.append(snapshots.backlog_card(issue, now))
        return {
            "in_flight": [card(row) for row in rows if not row.done],
            "pending": pending,
            "done": [card(row) for row in rows if row.done],
            "backlog": backlog,
            "gh_ok": not warnings,
            "warnings": warnings,
        }

    def issue(self, name: str, number: int) -> dict | None:
        snap = self.snapshot(name)
        row = snapshots.find_row(snap, number)
        if row is None:
            return None
        gh_issues, _prs, warnings = self._gh_context(snap.repo)
        detail = snapshots.issue_detail(row, self.now_fn(), gh_issues.get(number))
        detail["warnings"] = warnings
        return detail

    def step_events(self, name: str, number: int, idx: int) -> dict | None:
        row = snapshots.find_row(self.snapshot(name), number)
        if row is None:
            return None
        return {"events": snapshots.step_window_events(row, idx), "warnings": []}

    def cost_chart(self, session_id: str) -> tuple[str, str]:
        return costchart.render_cost_chart(session_id)

    # ── actions ──
    def start(self, name: str) -> tuple[bool, str]:
        if not self.known(name):
            return False, f"unknown deployment '{name}'"
        live, scan_ok = snapshots.scan_proc_daemons()
        if scan_ok and name in live:
            return False, f"deployment '{name}' already has live daemons"
        log_path = actions.start_log_path(cli.log_dir(), name)
        return actions.launch_detached(
            actions.start_command(self.harness_root, name), log_path
        )

    def promote(self, name: str, number: int) -> tuple[bool, str]:
        if not self.known(name):
            return False, f"unknown deployment '{name}'"
        repo = snapshots.overlay_field(name, "REPO")
        if not repo:
            return False, f"no REPO configured for deployment '{name}'"
        return actions.run_label_commands([actions.promote_command(repo, number)])

    def priority(self, name: str, number: int, priority: str | None) -> tuple[bool, str]:
        if not self.known(name):
            return False, f"unknown deployment '{name}'"
        repo = snapshots.overlay_field(name, "REPO")
        if not repo:
            return False, f"no REPO configured for deployment '{name}'"
        return actions.run_label_commands(
            actions.priority_commands(repo, number, priority)
        )


# ── HTTP ──────────────────────────────────────────────────────────────────────
def create_app(backend: Backend | None = None, harness_root: str | None = None):
    from fastapi import Body, FastAPI, HTTPException
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

    backend = backend or Backend(harness_root=harness_root)
    root = harness_root or getattr(backend, "harness_root", cli.HARNESS_ROOT)
    dist = dist_dir(root)
    app = FastAPI(title="issue-agent dashboard", docs_url=None, redoc_url=None)

    def degraded(kind: str, exc: Exception, **empty) -> JSONResponse:
        return JSONResponse(
            {**empty, "warnings": [f"{kind} unavailable ({type(exc).__name__}: {exc})"]}
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

    def require_known(name: str) -> None:
        if not backend.known(name):
            raise HTTPException(404, f"no such deployment: '{name}'")

    @app.get("/api/repos")
    def api_repos():
        try:
            return backend.repos()
        except Exception as exc:  # noqa: BLE001
            return degraded("deployment scan", exc, repos=[])

    @app.get("/api/usage")
    def api_usage():
        try:
            return backend.usage()
        except Exception as exc:  # noqa: BLE001
            return degraded("usage meters", exc, claude=None, codex=None, harness=None)

    @app.get("/api/verification")
    def api_verification():
        try:
            return backend.verification()
        except Exception as exc:  # noqa: BLE001
            return degraded("verification ledger", exc, rows=[])

    @app.get("/api/repos/{name}/overview")
    def api_overview(name: str):
        require_known(name)
        try:
            return backend.overview(name)
        except Exception as exc:  # noqa: BLE001
            return degraded("overview", exc, metrics=None, cost_mix=None, health=[], health_trend=[])

    @app.get("/api/repos/{name}/issues")
    def api_issues(name: str):
        require_known(name)
        try:
            return backend.issues(name)
        except Exception as exc:  # noqa: BLE001
            return degraded(
                "issues", exc, in_flight=[], pending=[], done=[], backlog=[], gh_ok=False
            )

    @app.get("/api/repos/{name}/issues/{number}")
    def api_issue(name: str, number: int):
        require_known(name)
        try:
            detail = backend.issue(name, number)
        except Exception as exc:  # noqa: BLE001
            return degraded("issue detail", exc, issue=number, steps=[], events=[])
        if detail is None:
            raise HTTPException(404, f"issue #{number} has no journal history in '{name}'")
        return detail

    @app.get("/api/repos/{name}/issues/{number}/steps/{idx}/events")
    def api_step_events(name: str, number: int, idx: int):
        require_known(name)
        try:
            payload = backend.step_events(name, number, idx)
        except Exception as exc:  # noqa: BLE001
            return degraded("step events", exc, events=[])
        if payload is None:
            raise HTTPException(404, f"issue #{number} has no journal history in '{name}'")
        return payload

    @app.get("/api/repos/{name}/issues/{number}/cost-chart")
    def api_cost_chart(name: str, number: int, session_id: str = ""):
        require_known(name)
        if not costchart.valid_session_id(session_id):
            raise HTTPException(400, "session_id must match ^[A-Za-z0-9_-]+$")
        chart, error = backend.cost_chart(session_id)
        if error:
            raise HTTPException(404, error)
        # The chart is generated from a transcript containing untrusted text
        # (issue bodies, PR comments, fetched pages). `sandbox` drops it into an
        # opaque origin, so script in it cannot reach the action endpoints.
        return HTMLResponse(
            chart, headers={"Content-Security-Policy": "sandbox; default-src 'none'"}
        )

    @app.post("/api/repos/{name}/start")
    def api_start(name: str):
        ok, detail = backend.start(name)
        return JSONResponse({"ok": ok, "detail": detail}, status_code=200 if ok else 400)

    @app.post("/api/repos/{name}/issues/{number}/promote")
    def api_promote(name: str, number: int):
        if number < 1:
            raise HTTPException(400, "issue number must be positive")
        ok, detail = backend.promote(name, number)
        return JSONResponse({"ok": ok, "detail": detail}, status_code=200 if ok else 400)

    @app.post("/api/repos/{name}/issues/{number}/priority")
    def api_priority(name: str, number: int, body: dict = Body(default={})):
        if number < 1:
            raise HTTPException(400, "issue number must be positive")
        priority = body.get("priority") if isinstance(body, dict) else None
        if priority is not None and priority not in actions.PRIORITY_LABELS:
            raise HTTPException(400, "priority must be P1, P2, P3 or null")
        ok, detail = backend.priority(name, number, priority)
        return JSONResponse({"ok": ok, "detail": detail}, status_code=200 if ok else 400)

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(404, "no such endpoint")
        index = os.path.join(dist, "index.html")
        if not os.path.isfile(index):
            return HTMLResponse(instructions_page())
        asset = _safe_asset(dist, full_path)
        return FileResponse(asset or index)

    return app


def _safe_asset(dist: str, relative: str) -> str:
    """A file inside `dist` for this URL path, or "" — path traversal never
    escapes the build directory."""
    if not relative:
        return ""
    candidate = os.path.normpath(os.path.join(dist, relative))
    base = os.path.normpath(dist)
    if candidate != base and not candidate.startswith(base + os.sep):
        return ""
    return candidate if os.path.isfile(candidate) else ""


def instructions_page() -> str:
    return (
        "<!doctype html><meta charset='utf-8'>"
        "<title>issue-agent dashboard</title>"
        "<style>body{font:14px/1.6 ui-monospace,monospace;margin:3rem auto;"
        "max-width:44rem;padding:0 1rem;background:#0e1330;color:#d8dce8}"
        "code{background:#141a3d;padding:.15rem .35rem;border-radius:3px}</style>"
        "<h1>dashboard UI not built</h1>"
        "<p>The API is live; the single-page app has not been built yet:</p>"
        f"<pre><code>{html.escape('uv sync --extra dash && make dashboard-ui')}</code></pre>"
        "<p>Then reload this page. The JSON API is available under "
        "<code>/api/</code> in the meantime.</p>"
    )


def serve(port: int, host: str = HOST, backend: Backend | None = None) -> None:
    """Run the dashboard on loopback. Loopback is not a default but a
    constraint: the POST actions launch daemons and relabel issues."""
    import uvicorn

    uvicorn.run(create_app(backend=backend), host=host, port=port, log_level="warning")
