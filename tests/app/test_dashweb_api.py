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
            "claude": {
                "five_hour_pct": 1.0, "five_hour_resets_at": None,
                "seven_day_pct": None, "seven_day_resets_at": None,
                "source": "oauth", "error": None,
            },
            "codex": {
                "weekly_pct": 2.0, "window_minutes": 10080, "resets_at": None,
                "plan": None, "source": "rollout", "sampled_at": None,
                "error": None,
            },
            "harness": {
                "global_daily_cap_usd": None, "today_all_repos_usd": 0.0,
                "per_repo": [],
            },
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
                "done_issues": 1, "in_flight_issues": 0, "total_cost_usd": 1.0,
                "cost_per_done_usd": 1.0, "first_pass_approvals": 1,
                "first_pass_changes": 0, "first_pass_rate": 1.0,
                "rounds_per_pr": 1.0, "escalations": 0, "recurrences": 0,
                "spend_blocks": 0, "today_usd": 1.0, "by_kind": [],
            },
            "cost_mix": {
                "resolve_usd": 1.0, "review_usd": 0.0, "repair_usd": 0.0,
                "steward_usd": 0.0,
            },
            "caps": {
                "issue_cap_usd": None, "daily_cap_usd": None, "today_usd": 1.0,
            },
            "health": [], "unattributed": [], "health_trend": [],
            "spend_per_hour": [
                {"hour_start_iso": "2026-07-29T11:00:00+00:00", "usd": 1.0},
                {"hour_start_iso": "2026-07-29T12:00:00+00:00", "usd": 0.0},
            ],
            "warnings": [],
        }

    def issues(self, name):
        self._maybe("issues")
        return {"in_flight": [{
                    "issue": 7, "title": None, "url": None, "pr": None,
                    "pr_url": None, "pr_state": None, "stage": "queued",
                    "done": False, "steps": [], "cost_usd": 0.0,
                    "estimated": False, "projected_cost_usd": None,
                    "started_ts": None, "last_ts": None, "elapsed_s": None,
                    "working_s": None, "idle_s": None, "idle_active": False,
                    "projected_eta_s": None, "models": [], "rounds": 0,
                    "flags": [],
                }], "pending": [], "done": [],
                "backlog": [], "gh_ok": True, "warnings": []}

    def issue(self, name, number):
        self._maybe("issue")
        return None if number == 404 else {
            "issue": number, "title": None, "url": None, "stage": "queued",
            "done": False, "prs": [], "pull_requests": [], "cost_usd": 0.0,
            "elapsed_s": None,
            "working_s": None,
            "idle_s": None,
            "idle_active": False,
            "steps": [], "events": [], "warnings": [],
        }

    def step_events(self, name, number, idx):
        self._maybe("step_events")
        return None if number == 404 else {"events": [{"event": "action"}], "warnings": []}

    def cost_chart(self, session_id):
        self._maybe("cost_chart")
        return ("<html>chart</html>", "") if session_id == "good" else ("", "no transcript")

    def start(self, name):
        self._maybe("start")
        return (False, "already active") if name == "busy" else (True, "/logs/x.log")

    def promote(self, name, number):
        self._maybe("promote")
        return (True, "ok")

    def unblock(self, name, number, channel="api"):
        self._maybe("unblock")
        self.unblock_channel = channel
        return (True, "ok")

    def priority(self, name, number, priority):
        self._maybe("priority")
        self.priority_seen = priority
        return (True, "ok")


WRITE_HEADERS = {"X-Issue-Agent-Dashboard": "1"}
# The guard only serves loopback, so the test client must look like a browser
# pointed at 127.0.0.1 — TestClient's default `testserver` host is exactly the
# rebound-hostname case the guard exists to reject.
BASE_URL = "http://127.0.0.1:8321"


def client(backend=None):
    return TestClient(
        server.create_app(backend=backend or StubBackend()),
        headers=WRITE_HEADERS,
        base_url=BASE_URL,
    )


# ── GET ───────────────────────────────────────────────────────────────────────
def test_repos_returns_the_pre_sorted_deployment_list():
    body = client().get("/api/repos").json()
    assert body["repos"][0]["name"] == "demo" and body["warnings"] == []


def test_usage_returns_all_three_meters():
    body = client().get("/api/usage").json()
    assert set(body) >= {"claude", "codex", "harness", "warnings"}


def test_verification_returns_rows():
    assert client().get("/api/verification").json()["rows"][0]["status"] == "pending"


def test_overview_carries_metrics_cost_mix_caps_and_health():
    body = client().get("/api/repos/demo/overview").json()
    assert set(body) >= {"metrics", "cost_mix", "caps", "health", "unattributed"}


def test_overview_carries_the_hourly_spend_series():
    series = client().get("/api/repos/demo/overview").json()["spend_per_hour"]
    assert [bucket["usd"] for bucket in series] == [1.0, 0.0]
    assert series[0]["hour_start_iso"] == "2026-07-29T11:00:00+00:00"


def test_a_degraded_overview_still_answers_with_an_empty_spend_series():
    backend = StubBackend(explode="overview")
    body = client(backend).get("/api/repos/demo/overview").json()
    assert body["spend_per_hour"] == [] and body["warnings"]


def test_overview_forwards_the_browser_timezone():
    backend = StubBackend()
    response = client(backend).get(
        "/api/repos/demo/overview?timezone=Australia%2FSydney"
    )
    assert response.status_code == 200
    assert backend.overview_timezone == "Australia/Sydney"


def test_issues_returns_all_four_buckets_in_one_call():
    body = client().get("/api/repos/demo/issues").json()
    assert set(body) >= {"in_flight", "pending", "done", "backlog", "gh_ok"}


def test_issue_detail_is_404_when_the_journal_has_no_such_issue():
    assert client().get("/api/repos/demo/issues/404").status_code == 404
    assert client().get("/api/repos/demo/issues/7").json()["issue"] == 7


def test_step_events_are_scoped_to_the_step_index():
    body = client().get("/api/repos/demo/issues/7/steps/0/events").json()
    assert body["events"] == [{"event": "action"}]
    assert client().get("/api/repos/demo/issues/404/steps/0/events").status_code == 404


def test_the_cost_chart_is_served_as_html():
    r = client().get("/api/repos/demo/issues/7/cost-chart?session_id=good")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "chart" in r.text


def test_a_chart_that_cannot_be_generated_is_a_404_with_a_readable_detail():
    r = client().get("/api/repos/demo/issues/7/cost-chart?session_id=absent")
    assert r.status_code == 404 and "transcript" in r.json()["detail"]


@pytest.mark.parametrize("session_id", ["", "../../etc/passwd", "a b"])
def test_a_path_unsafe_session_id_is_rejected_before_any_work(session_id):
    backend = StubBackend()
    r = TestClient(server.create_app(backend=backend), headers=WRITE_HEADERS, base_url=BASE_URL).get(
        "/api/repos/demo/issues/7/cost-chart", params={"session_id": session_id}
    )
    assert r.status_code == 400
    assert "cost_chart" not in backend.calls


# ── failure posture ───────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "collector,path",
    [
        ("repos", "/api/repos"),
        ("usage", "/api/usage"),
        ("verification", "/api/verification"),
        ("overview", "/api/repos/demo/overview"),
        ("issues", "/api/repos/demo/issues"),
        ("issue", "/api/repos/demo/issues/7"),
        ("step_events", "/api/repos/demo/issues/7/steps/0/events"),
    ],
)
def test_a_collector_failure_degrades_to_a_warning_never_a_500(collector, path):
    r = TestClient(server.create_app(backend=StubBackend(explode=collector)),
                    headers=WRITE_HEADERS, base_url=BASE_URL).get(path)
    assert r.status_code == 200
    assert r.json()["warnings"]


def test_issue_failure_payload_still_satisfies_the_rendering_contract():
    body = client(StubBackend(explode="issue")).get("/api/repos/demo/issues/7").json()

    assert body == {
        "issue": 7,
        "title": None,
        "url": None,
        "stage": None,
        "done": False,
        "prs": [],
        "pull_requests": [],
        "cost_usd": 0.0,
        "elapsed_s": None,
        "working_s": None,
        "idle_s": None,
        "idle_active": False,
        "steps": [],
        "events": [],
        "warnings": ["issue detail unavailable (RuntimeError: collector blew up)"],
    }


# ── POST ──────────────────────────────────────────────────────────────────────
def test_start_returns_the_log_path_it_launched_into():
    body = client().post("/api/repos/demo/start").json()
    assert body["ok"] is True and body["detail"].endswith(".log")


def test_start_on_a_live_deployment_is_refused():
    r = client().post("/api/repos/busy/start")
    assert r.status_code == 400 and r.json()["ok"] is False


def test_promote_reports_success():
    assert client().post("/api/repos/demo/issues/9/promote").json() == {
        "ok": True, "detail": "ok"
    }


def test_unblock_reports_success():
    assert client().post("/api/repos/demo/issues/9/unblock").json() == {
        "ok": True, "detail": "ok"
    }


def test_unblock_stamps_the_channel_from_the_user_agent():
    # A browser (the dashboard UI) journals channel=ui; anything else — curl,
    # an agent — journals channel=api and wears the louder badge downstream.
    backend = StubBackend()
    c = TestClient(server.create_app(backend=backend), headers=WRITE_HEADERS, base_url=BASE_URL)
    c.post("/api/repos/demo/issues/9/unblock",
           headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"})
    assert backend.unblock_channel == "ui"
    c.post("/api/repos/demo/issues/9/unblock", headers={"User-Agent": "curl/8.5"})
    assert backend.unblock_channel == "api"
    c.post("/api/repos/demo/issues/9/unblock", headers={"User-Agent": ""})
    assert backend.unblock_channel == "api"


def test_an_unblock_whose_collectors_fail_is_refused_not_a_500():
    """It reads (snapshot + GitHub) before it writes, so it has failure modes
    the other actions do not — the operator must get a readable refusal."""
    backend = StubBackend(explode="unblock")
    c = TestClient(server.create_app(backend=backend), headers=WRITE_HEADERS, base_url=BASE_URL)
    response = c.post("/api/repos/demo/issues/9/unblock")
    assert response.status_code == 400
    assert response.json()["ok"] is False
    assert "collector blew up" in response.json()["detail"]


def test_unblock_rejects_a_non_positive_issue_number():
    assert client().post("/api/repos/demo/issues/0/unblock").status_code == 400


def test_priority_accepts_the_three_labels_and_null():
    backend = StubBackend()
    c = TestClient(server.create_app(backend=backend), headers=WRITE_HEADERS, base_url=BASE_URL)
    for value in ("P1", "P2", "P3", None):
        assert c.post("/api/repos/demo/issues/9/priority",
                      json={"priority": value}).json()["ok"] is True
        assert backend.priority_seen == value


def test_an_unknown_priority_is_a_400_and_never_reaches_the_backend():
    backend = StubBackend()
    r = TestClient(server.create_app(backend=backend), headers=WRITE_HEADERS, base_url=BASE_URL).post(
        "/api/repos/demo/issues/9/priority", json={"priority": "P9"}
    )
    assert r.status_code == 400 and "priority" not in backend.calls


# ── static ────────────────────────────────────────────────────────────────────
def _write_fresh_spa(tmp_path):
    dashboard = tmp_path / "dashboard"
    dist = dashboard / "dist"
    dist.mkdir(parents=True)
    (dashboard / "build-inputs.json").write_text(
        '{"schema":1,"files":["build-inputs.json"],"directories":[]}'
    )
    (dist / "index.html").write_text("<html>app</html>")
    (dist / "assets").mkdir()
    (dist / "assets" / "app.js").write_text("console.log(1)")
    (dist / bundle.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": 1,
                "inputs": bundle.source_hashes(tmp_path),
                "outputs": bundle.output_hashes(tmp_path),
            }
        )
    )
    return dist


def test_without_a_built_spa_the_root_explains_how_to_build_it(tmp_path):
    c = TestClient(server.create_app(backend=StubBackend(), harness_root=str(tmp_path)),
                   base_url=BASE_URL)
    r = c.get("/")
    assert r.status_code == 200 and "make dashboard-ui" in r.text


def test_a_built_spa_is_served_with_a_fallback_for_client_routes(tmp_path):
    dist = _write_fresh_spa(tmp_path)
    c = TestClient(server.create_app(backend=StubBackend(), harness_root=str(tmp_path)),
                   base_url=BASE_URL)
    assert "app</html>" in c.get("/").text
    assert "console.log(1)" in c.get("/assets/app.js").text
    assert "app</html>" in c.get("/repo/demo/issue/7").text


def test_a_stale_spa_is_not_served_even_when_index_exists(tmp_path):
    _write_fresh_spa(tmp_path)
    (tmp_path / "dashboard" / "build-inputs.json").write_text(
        '{"schema":1,"files":["build-inputs.json"],"directories":["src"]}'
    )
    c = TestClient(
        server.create_app(backend=StubBackend(), harness_root=str(tmp_path)),
        base_url=BASE_URL,
    )

    assert "make dashboard-ui" in c.get("/").text
    assert "app</html>" not in c.get("/").text


def test_a_running_server_keeps_its_immutable_version_after_new_publication(tmp_path):
    dashboard = tmp_path / "dashboard"
    dist_root = dashboard / "dist"
    (dashboard / "build-inputs.json").parent.mkdir(parents=True)
    (dashboard / "build-inputs.json").write_text(
        '{"schema":1,"files":["build-inputs.json"],"directories":[]}'
    )

    def publish(build_id, content):
        version = dist_root / "builds" / build_id
        version.mkdir(parents=True)
        (version / "index.html").write_text(content)
        (dist_root / bundle.POINTER_NAME).write_text(
            json.dumps({"schema": 1, "build": f"builds/{build_id}"})
        )
        (version / bundle.MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "schema": 1,
                    "inputs": bundle.source_hashes(tmp_path),
                    "outputs": bundle.output_hashes(tmp_path),
                }
            )
        )

    publish("v1", "<html>version one</html>")
    first = TestClient(
        server.create_app(backend=StubBackend(), harness_root=str(tmp_path)),
        base_url=BASE_URL,
    )
    publish("v2", "<html>version two</html>")
    dashboard_build.garbage_collect_builds(dashboard)
    second = TestClient(
        server.create_app(backend=StubBackend(), harness_root=str(tmp_path)),
        base_url=BASE_URL,
    )

    assert "version one" in first.get("/").text
    assert "version two" in second.get("/").text


def test_an_invalid_version_pointer_degrades_to_build_instructions(tmp_path):
    dist = tmp_path / "dashboard" / "dist"
    dist.mkdir(parents=True)
    (dist / bundle.POINTER_NAME).write_text("{")

    client = TestClient(
        server.create_app(backend=StubBackend(), harness_root=str(tmp_path)),
        base_url=BASE_URL,
    )

    assert "dashboard UI unavailable" in client.get("/").text
    assert "make dashboard-ui" in client.get("/").text


def test_an_unknown_api_path_is_a_404_not_the_spa(tmp_path):
    dist = tmp_path / "dashboard" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html>app</html>")
    c = TestClient(server.create_app(backend=StubBackend(), harness_root=str(tmp_path)),
                   base_url=BASE_URL)
    assert c.get("/api/nope").status_code == 404


def test_a_traversal_path_cannot_escape_the_build_directory(tmp_path):
    dist = tmp_path / "dashboard" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html>app</html>")
    (tmp_path / "secret.txt").write_text("SECRET")
    c = TestClient(server.create_app(backend=StubBackend(), harness_root=str(tmp_path)),
                   base_url=BASE_URL)
    assert "SECRET" not in c.get("/../secret.txt").text
    assert server._safe_asset(str(dist), "../secret.txt") == ""


def test_a_symlinked_asset_cannot_escape_the_build_directory(tmp_path):
    dist = tmp_path / "dashboard" / "dist"
    dist.mkdir(parents=True)
    outside = tmp_path / "secret.txt"
    outside.write_text("SECRET")
    (dist / "leak.txt").symlink_to(outside)

    assert server._safe_asset(str(dist), "leak.txt") == ""


# ── request guard (the RCE chain this closes) ─────────────────────────────────
# A page the operator merely VISITS must not be able to reach a write endpoint.
# Before these guards existed, one cross-origin POST reached `make point-at`
# with an attacker-chosen `name`, which make expanded into a shell recipe.
def test_a_write_without_the_dashboard_header_is_refused():
    backend = StubBackend()
    c = TestClient(server.create_app(backend=backend), base_url=BASE_URL)  # no header
    r = c.post("/api/repos/demo/start")
    assert r.status_code == 403
    assert "start" not in backend.calls


def test_a_cross_origin_write_is_refused_even_with_the_header():
    backend = StubBackend()
    c = TestClient(server.create_app(backend=backend), headers=WRITE_HEADERS, base_url=BASE_URL)
    r = c.post("/api/repos/demo/start", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert "start" not in backend.calls


def test_a_rebound_hostname_is_refused_on_reads_too():
    backend = StubBackend()
    c = TestClient(server.create_app(backend=backend), headers=WRITE_HEADERS, base_url=BASE_URL)
    r = c.get("/api/repos", headers={"Host": "attacker.example"})
    assert r.status_code == 421
    assert "repos" not in backend.calls


def test_same_origin_requests_pass_the_guard():
    c = client()
    assert c.get("/api/repos", headers={"Origin": BASE_URL}).status_code == 200


@pytest.mark.parametrize("path", [
    "/api/repos/nosuch/overview",
    "/api/repos/nosuch/issues",
    "/api/repos/nosuch/issues/7",
    "/api/repos/nosuch/issues/7/steps/0/events",
    "/api/repos/nosuch/issues/7/cost-chart?session_id=good",
])
def test_an_unconfigured_deployment_name_is_404_and_reaches_no_collector(path):
    backend = StubBackend()
    r = TestClient(server.create_app(backend=backend), headers=WRITE_HEADERS, base_url=BASE_URL).get(path)
    assert r.status_code == 404
    assert backend.calls == []


@pytest.mark.parametrize("number", [0, -5])
def test_a_non_positive_issue_number_never_reaches_gh(number):
    backend = StubBackend()
    c = TestClient(server.create_app(backend=backend), headers=WRITE_HEADERS, base_url=BASE_URL)
    assert c.post(f"/api/repos/demo/issues/{number}/promote").status_code == 400
    assert c.post(f"/api/repos/demo/issues/{number}/priority",
                  json={"priority": "P1"}).status_code == 400
    assert backend.calls == []


def test_the_cost_chart_is_served_sandboxed():
    r = client().get("/api/repos/demo/issues/7/cost-chart?session_id=good")
    assert "sandbox" in r.headers["content-security-policy"]


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
