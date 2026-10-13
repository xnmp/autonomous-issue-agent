"""Endpoint shape + failure posture, over a stubbed backend.

The collectors are tested directly elsewhere; what these pin is the HTTP
contract: the payload keys a client indexes, the 400/404 boundaries, and the
rule that a collector blowing up degrades to a warning rather than a 500 — a
dashboard that fails during an incident is a dashboard that is not there
during incidents.
"""

import json

import pytest

pytest.importorskip("fastapi")

from issue_agent.adapters.dashweb import build as dashboard_build  # noqa: E402
from issue_agent.adapters.dashweb import bundle  # noqa: E402
from issue_agent.app.dashweb import server  # noqa: E402
from asgi_client import ASGITestClient as TestClient  # noqa: E402


class StubBackend:
    """Every method returns a canned payload; `explode` makes one raise."""

    harness_root = "/h"

    def __init__(self, explode=""):
        self.explode = explode
        self.calls = []

    def known(self, name):
        return name != "nosuch"

    def _maybe(self, name):
        self.calls.append(name)
        if self.explode == name:
            raise RuntimeError("collector blew up")

    def repos(self):
        self._maybe("repos")
        return {"repos": [{
            "name": "demo", "repo": "o/r", "target_dir": "/tmp/demo",
            "is_self": False, "active": True, "frecency": 1.0, "roles": [],
            "last_activity_ts": None, "in_flight": 1, "done": 0,
        }], "warnings": []}

    def usage(self):
        self._maybe("usage")
        return {
            "claude": {"five_hour_pct": 1.0, "five_hour_resets_at": None, "seven_day_pct": None,
                       "seven_day_resets_at": None, "source": "oauth", "error": None},
            "codex": {"weekly_pct": 2.0, "window_minutes": 10080, "resets_at": None, "plan": None,
                      "source": "rollout", "sampled_at": None, "error": None},
            "harness": {"global_daily_cap_usd": None, "today_all_repos_usd": 0.0, "per_repo": []},
            "warnings": [],
        }

    def verification(self):
        self._maybe("verification")
        return {"rows": [{
            "id": "#1", "change": "test", "shipped": None, "status": "pending",
            "evidence": None, "raw": "| #1 | test | pending |",
        }], "warnings": []}

    def overview(self, name, timezone_name=None):
        self._maybe("overview")
        self.overview_timezone = timezone_name
        return {
            "metrics": {
                "merged_prs": 1, "open_items": 1, "needs_human": 0, "total_cost_usd": 1.0,
                "cost_per_merged_pr_usd": 1.0, "by_basis": [{"basis": "provider_reported", "usd": 1.0}],
                "by_kind": [{"kind": "RESOLVE", "usd": 1.0, "runs": 1}], "first_pass_approvals": 1,
                "first_pass_changes": 0, "first_pass_rate": 1.0, "rounds_per_pr": 1.0,
                "legs": 1, "machine_legs": 0, "today_usd": 1.0,
            },
            "caps": {"issue_cap_usd": None, "daily_cap_usd": None, "global_daily_cap_usd": None, "today_usd": 1.0},
            "health": [], "needs_attention": [], "health_trend": [],
            "spend_per_hour": [
                {"hour_start_iso": "2026-07-29T11:00:00+00:00", "usd": 1.0},
                {"hour_start_iso": "2026-07-29T12:00:00+00:00", "usd": 0.0},
            ],
            "warnings": [],
        }

    def issues(self, name):
        self._maybe("issues")
        return {"in_flight": [{
                    "issue": 7, "title": None, "url": None, "pr": None, "pr_url": None,
                    "board": {"status": "working", "activity": "queued", "act": "RESOLVE",
                              "headline": "Resolve next", "reason": "r", "since_ts": None},
                    "rail": {"current": "resolve", "furthest": "resolve", "resolve_attempts": 0,
                             "review_rounds": 0, "ci_runs": 0, "steps": 0, "live": False},
                    "cost_usd": 0.0, "estimated": False, "legs": 0, "started_ts": None, "last_ts": None,
                    "leg_running": False,
                }], "done": [], "backlog": [], "gh_ok": True, "warnings": []}

    def issue(self, name, number):
        self._maybe("issue")
        return None if number == 404 else {
            "issue": number, "title": None, "url": None, "done": False, "pull_requests": [],
            "cost_usd": 0.0, "estimated": False, "steps": [], "warnings": [],
        }

    def promote(self, name, number):
        self._maybe("promote")
        return (True, "ok")

    def unblock(self, name, number):
        self._maybe("unblock")
        self.unblocked = number
        return (True, "ok")


WRITE_HEADERS = {"X-Issue-Agent-Dashboard": "1"}
# The guard only serves loopback, so the test client must look like a browser
# pointed at 127.0.0.1 — TestClient's default `testserver` host is exactly the
# rebound-hostname case the guard exists to reject.
BASE_URL = "http://127.0.0.1:8321"


def client(backend=None, root=None, headers=WRITE_HEADERS):
    app = server.create_app(backend=backend or StubBackend(), **({"harness_root": str(root)} if root else {}))
    return TestClient(app, headers=headers, base_url=BASE_URL)


def test_usage_returns_all_three_meters():
    body = client().get("/api/usage").json()
    assert set(body) >= {"claude", "codex", "harness", "warnings"}


def test_verification_returns_rows():
    assert client().get("/api/verification").json()["rows"][0]["status"] == "pending"


def test_overview_carries_metrics_caps_attention_and_health():
    body = client().get("/api/repos/demo/overview").json()
    assert set(body) >= {"metrics", "caps", "health", "needs_attention"}
    assert body["metrics"]["cost_per_merged_pr_usd"] == 1.0


def test_a_degraded_overview_still_answers_with_an_empty_spend_series():
    backend = StubBackend(explode="overview")
    body = client(backend).get("/api/repos/demo/overview").json()
    assert body["spend_per_hour"] == [] and body["warnings"]


def test_issues_returns_all_three_lanes_in_one_call():
    body = client().get("/api/repos/demo/issues").json()
    assert set(body) >= {"in_flight", "done", "backlog", "gh_ok"}
    assert body["in_flight"][0]["board"]["status"] == "working"


def test_issue_detail_is_404_for_an_unknown_issue():
    assert client().get("/api/repos/demo/issues/404").status_code == 404
    assert client().get("/api/repos/demo/issues/7").json()["issue"] == 7


# ── failure posture ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("collector,path", [
    ("repos", "/api/repos"), ("usage", "/api/usage"), ("verification", "/api/verification"),
    ("overview", "/api/repos/demo/overview"), ("issues", "/api/repos/demo/issues"),
    ("issue", "/api/repos/demo/issues/7"),
])
def test_a_collector_failure_degrades_to_a_warning_never_a_500(collector, path):
    r = client(StubBackend(explode=collector)).get(path)
    assert r.status_code == 200
    assert r.json()["warnings"]


# ── POST ──────────────────────────────────────────────────────────────────────
def test_promote_reports_success():
    assert client().post("/api/repos/demo/issues/9/promote").json() == {"ok": True, "detail": "ok"}


@pytest.mark.parametrize("verb", ["promote", "unblock"])
def test_a_write_whose_collectors_fail_is_refused_not_a_500(verb):
    """Both read before they write: a failure is a readable refusal."""
    response = client(StubBackend(explode=verb)).post(f"/api/repos/demo/issues/9/{verb}")
    assert response.status_code == 400
    assert response.json()["ok"] is False
    assert "collector blew up" in response.json()["detail"]


def test_unblock_rejects_a_non_positive_issue_number():
    assert client().post("/api/repos/demo/issues/0/unblock").status_code == 400


# ── static ────────────────────────────────────────────────────────────────────
_BUILD_INPUTS = '{"schema":1,"files":["build-inputs.json"],"directories":[]}'


def _manifest(tmp_path) -> str:
    return json.dumps({"schema": 1, "inputs": bundle.source_hashes(tmp_path),
                       "outputs": bundle.output_hashes(tmp_path)})


def _dist(tmp_path, index: str | None = "<html>app</html>"):
    dist = tmp_path / "dashboard" / "dist"
    dist.mkdir(parents=True)
    if index is not None:
        (dist / "index.html").write_text(index)
    return dist


def test_without_a_built_spa_the_root_explains_how_to_build_it(tmp_path):
    r = client(root=tmp_path, headers=None).get("/")
    assert r.status_code == 200 and "make dashboard-ui" in r.text


def test_a_built_spa_is_served_with_a_fallback_for_client_routes(tmp_path):
    dist = _dist(tmp_path)
    (tmp_path / "dashboard" / "build-inputs.json").write_text(_BUILD_INPUTS)
    (dist / "assets").mkdir()
    (dist / "assets" / "app.js").write_text("console.log(1)")
    (dist / bundle.MANIFEST_NAME).write_text(_manifest(tmp_path))
    c = client(root=tmp_path, headers=None)
    assert "app</html>" in c.get("/").text
    assert "console.log(1)" in c.get("/assets/app.js").text
    assert "app</html>" in c.get("/repo/demo/issue/7").text


def test_a_running_server_keeps_its_immutable_version_after_new_publication(tmp_path):
    dashboard = tmp_path / "dashboard"
    dist_root = dashboard / "dist"
    dashboard.mkdir()
    (dashboard / "build-inputs.json").write_text(_BUILD_INPUTS)

    def publish(build_id, content):
        version = dist_root / "builds" / build_id
        version.mkdir(parents=True)
        (version / "index.html").write_text(content)
        (dist_root / bundle.POINTER_NAME).write_text(json.dumps({"schema": 1, "build": f"builds/{build_id}"}))
        (version / bundle.MANIFEST_NAME).write_text(_manifest(tmp_path))

    publish("v1", "<html>version one</html>")
    first = client(root=tmp_path, headers=None)
    publish("v2", "<html>version two</html>")
    dashboard_build.garbage_collect_builds(dashboard)
    second = client(root=tmp_path, headers=None)

    assert "version one" in first.get("/").text
    assert "version two" in second.get("/").text


def test_an_invalid_version_pointer_degrades_to_build_instructions(tmp_path):
    (_dist(tmp_path, index=None) / bundle.POINTER_NAME).write_text("{")

    c = client(root=tmp_path, headers=None)

    assert "dashboard UI unavailable" in c.get("/").text
    assert "make dashboard-ui" in c.get("/").text


def test_an_unknown_api_path_is_a_404_not_the_spa(tmp_path):
    _dist(tmp_path)
    assert client(root=tmp_path, headers=None).get("/api/nope").status_code == 404


def test_a_traversal_path_cannot_escape_the_build_directory(tmp_path):
    dist = _dist(tmp_path)
    (tmp_path / "secret.txt").write_text("SECRET")
    assert "SECRET" not in client(root=tmp_path, headers=None).get("/../secret.txt").text
    assert server._safe_asset(str(dist), "../secret.txt") == ""


def test_a_symlinked_asset_cannot_escape_the_build_directory(tmp_path):
    dist = _dist(tmp_path, index=None)
    outside = tmp_path / "secret.txt"
    outside.write_text("SECRET")
    (dist / "leak.txt").symlink_to(outside)

    assert server._safe_asset(str(dist), "leak.txt") == ""


# ── request guard (the RCE chain this closes) ─────────────────────────────────
# A page the operator merely VISITS must not be able to reach a write endpoint.
# Before these guards existed, one cross-origin POST reached `make point-at`
# with an attacker-chosen `name`, which make expanded into a shell recipe;
# today the writes are label changes, which a visited page must not make either.
def test_a_write_without_the_dashboard_header_is_refused():
    backend = StubBackend()
    r = client(backend, headers=None).post("/api/repos/demo/issues/9/park")
    assert r.status_code == 403
    assert "park" not in backend.calls


def test_a_cross_origin_write_is_refused_even_with_the_header():
    backend = StubBackend()
    r = client(backend).post("/api/repos/demo/issues/9/park", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert "park" not in backend.calls


def test_a_rebound_hostname_is_refused_on_reads_too():
    backend = StubBackend()
    r = client(backend).get("/api/repos", headers={"Host": "attacker.example"})
    assert r.status_code == 421
    assert "repos" not in backend.calls


def test_same_origin_requests_pass_the_guard():
    assert client().get("/api/repos", headers={"Origin": BASE_URL}).status_code == 200


@pytest.mark.parametrize("path", ["/api/repos/nosuch/overview", "/api/repos/nosuch/issues",
                                  "/api/repos/nosuch/issues/7"])
def test_an_unconfigured_deployment_name_is_404_and_reaches_no_collector(path):
    backend = StubBackend()
    r = client(backend).get(path)
    assert r.status_code == 404
    assert backend.calls == []


@pytest.mark.parametrize("number", [0, -5])
def test_a_non_positive_issue_number_never_reaches_gh(number):
    backend = StubBackend()
    assert client(backend).post(f"/api/repos/demo/issues/{number}/promote").status_code == 400
    assert backend.calls == []


@pytest.mark.parametrize("host,ok", [
    ("127.0.0.1:8321", True), ("localhost:8321", True), ("127.0.0.1", True),
    ("evil.example", False), ("", False), ("127.0.0.1.evil.example:8321", False),
])
def test_loopback_host_accepts_only_this_machine(host, ok):
    assert server.loopback_host(host) is ok


@pytest.mark.parametrize("origin,host,ok", [
    ("", "127.0.0.1:8321", True),
    ("http://127.0.0.1:8321", "127.0.0.1:8321", True),
    ("http://evil.example", "127.0.0.1:8321", False),
])
def test_same_origin_compares_origin_against_the_served_host(origin, host, ok):
    assert server.same_origin(origin, host) is ok
