"""The dashboard over a fixture deployment: the board is what derive decides
for the observed world plus the store's legs and leases, and the only writes
are label changes."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.app.dashweb.service import Backend
from issue_agent.domain.checks import Check, Observed
from issue_agent.domain.labels import Label
from issue_agent.domain.state import Harness, StopReason, stop_tag
from issue_agent.domain.world import Comment, IssueView, PRView, WorldState

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
MARKER = Harness("fixbot", "reviewbot").marker


def ts(minutes: float) -> str:
    return (NOW + timedelta(minutes=minutes)).isoformat()


def store_ts(minutes: float) -> str:
    return (NOW + timedelta(minutes=minutes)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class InlineExecutor:
    def submit(self, fn, *args):
        future: Future = Future()
        try:
            future.set_result(fn(*args))
        except Exception as exc:  # noqa: BLE001
            future.set_exception(exc)
        return future


class Gateway:
    def __init__(self, world):
        self.world = world
        self.reads = 0

    def build_world(self, base):
        self.reads += 1
        return self.world


class Gh:
    def __init__(self, issues=(), merged=()):
        self._issues, self._merged = list(issues), list(merged)

    def issues(self, repo, limit=1000):
        return self._issues, ""

    def merged_prs(self, repo, limit=200):
        return self._merged, ""


class Effects:
    def __init__(self, log):
        self.log = log

    def move_hold(self, issue, *, park, channel):
        self.log.append(("move_hold", issue, park))
        return True, "ok"

    def set_issue_labels(self, issue, *, add=(), remove=()):
        self.log.append(("labels", issue, tuple(add), tuple(remove)))
        return True, "ok"


def _write_env(root: Path, state: Path) -> None:
    config = root / "config"
    (config / "repos").mkdir(parents=True)
    body = (f'REPO="o/r"\nFIX_BOT="fixbot"\nREVIEW_BOT="reviewbot"\nSTATE_DIR="{state}/fix"\n'
            f'REVIEW_STATE_DIR="{state}/review"\nISSUE_SPEND_CAP_USD="25"\nDAILY_SPEND_CAP_USD="700"\n')
    (config / "repos" / "demo.env").write_text(body)
    (config / ".active.demo.env").write_text(body)


def _store(path: Path) -> None:
    SQLiteControlStore(str(path)).initialize()
    db = sqlite3.connect(path)
    ended = ("INSERT INTO legs(leg_id, repository, role, kind, issue, number, head, owner, runtime, state, "
             "reserved_at, started_at, ended_at, currency, reserved_micros, charged_micros, basis, origin, "
             "provider_receipt_id, settled_at, leg_class, session_id, result_json) VALUES "
             "(?, 'o/r', 'fix', ?, ?, ?, '', 'o/r:fix', 'claude', 'ended', ?, ?, ?, 'USD', 10000000, ?, "
             "'provider_reported', 'normal_return', ?, ?, ?, 's', ?)")
    db.execute(ended, ("L2", "RESOLVE", 2, 0, store_ts(-60), store_ts(-60), store_ts(-50), 2_000_000, "r2",
                       store_ts(-50), "asked", json.dumps({"question": "Which API?"})))
    db.execute(ended, ("L8", "RESOLVE", 8, 0, store_ts(-300), store_ts(-300), store_ts(-290), 3_000_000, "r8",
                       store_ts(-290), "agent", "{}"))
    db.execute("INSERT INTO legs(leg_id, repository, role, kind, issue, number, head, owner, runtime, state, "
               "reserved_at, started_at, currency, reserved_micros) VALUES "
               "('L4', 'o/r', 'fix', 'RESOLVE', 4, 0, '', 'o/r:fix', 'codex', 'running', ?, ?, 'USD', 10000000)",
               (store_ts(-5), store_ts(-5)))
    db.execute("INSERT INTO leases(repository, role, target, owner, leg_id, pid, pgid, start_ticks, acquired_at) "
               "VALUES ('o/r', 'fix', 4, 'o/r:fix', 'L4', 999999, 999999, 1, ?)", (store_ts(-5),))
    db.commit()
    db.close()


def _world() -> WorldState:
    ready = frozenset({Label.AI_READY})
    question = Comment("fixbot", f"{MARKER}\n{stop_tag(StopReason.QUESTION)}\n**needs-human**: Which API?", ts(-40))
    pending = Observed("h3", "h3", frozenset(), (Check("test", state="IN_PROGRESS", bucket="pending"),))
    return WorldState(
        issues=(
            IssueView(1, "queued work", labels=ready),
            IssueView(2, "asked a question", labels=ready | {Label.NEEDS_HUMAN}, comments=(question,)),
            IssueView(3, "has a PR", labels=ready),
            IssueView(4, "running", labels=ready),
            IssueView(5, "parked by hand", labels=frozenset({Label.NEEDS_HUMAN})),
        ),
        prs=(
            PRView(30, head_ref_name="fix/3-pr", head_ref_oid="h3", ci=pending, mergeable="MERGEABLE",
                   merge_state="CLEAN"),
            PRView(50, head_ref_name="fix/5-pr", head_ref_oid="h5", labels=frozenset({Label.NEEDS_HUMAN})),
        ),
        now=ts(0),
    )


@pytest.fixture
def deployment(tmp_path):
    _write_env(tmp_path / "root", tmp_path / "state")
    _store(tmp_path / "control.db")
    log: list = []
    gateway = Gateway(_world())
    backend = Backend(
        str(tmp_path / "root"),
        gh=Gh(issues=[{"number": 6, "title": "someday", "state": "OPEN", "labels": [], "createdAt": ts(-1440)},
                      {"number": 8, "title": "shipped", "state": "CLOSED", "labels": []}],
              merged=[{"number": 80, "title": "fix 8", "url": "u80", "mergedAt": ts(-280),
                       "headRefName": "fix/8-done"},
                      {"number": 81, "title": "a human's", "headRefName": "feat/9-x", "mergedAt": ts(-10)}]),
        gateway_factory=lambda repo: gateway,
        now_fn=lambda: NOW,
        effects_factory=lambda repo, identity: Effects(log),
        environ={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)},
        observe_executor=InlineExecutor(),
        proc_scan=lambda: ({}, True),
        control_path=lambda cfg: str(tmp_path / "control.db"),
    )
    backend.issues("demo")  # the first poll schedules the observation
    return backend, log, tmp_path / "control.db", gateway


def test_the_board_shows_each_items_derived_state(deployment):
    backend, _log, _db, _gateway = deployment
    payload = backend.issues("demo")
    board = {card["issue"]: card for card in payload["in_flight"]}

    assert sorted(board) == [1, 2, 3, 4, 5]
    assert board[1]["board"]["status"] == "working" and board[1]["board"]["act"] == "RESOLVE"
    assert board[2]["board"] | {"detail": None} == {
        "status": "needs_you", "reason": "question", "headline": "Answer the agent's question", "detail": None,
        "target": "issue", "since_ts": ts(-40),
        "hint": "Reply on GitHub with the answer; the agent resumes with it.",
    }
    assert "Which API?" in board[2]["board"]["detail"] and board[2]["action"] == "open"
    assert (board[3]["board"]["activity"], board[3]["pr"], board[3]["pr_ci_state"]) == ("ci", 30, "pending")
    assert board[3]["rail"]["current"] == "ci"
    assert (board[4]["board"]["activity"], board[4]["board"]["act"], board[4]["leg_running"]) == (
        "agent", "RESOLVE", True)
    assert board[5]["board"]["reason"] == "labelled" and board[5]["action"] == "unblock"
    assert board[2]["cost_usd"] == 2.0 and board[2]["rail"]["resolve_attempts"] == 1
    assert payload["observed_at"] == NOW.isoformat() and payload["gh_ok"] is True


def test_done_backlog_and_notifications_come_from_github_lists_and_legs(deployment):
    backend, *_ = deployment
    payload = backend.issues("demo")

    assert [(d["issue"], d["pr"], d["cost_usd"]) for d in payload["done"]] == [(8, 80, 3.0)]
    assert [b["issue"] for b in payload["backlog"]] == [6]
    assert payload["backlog"][0]["age_days"] == 1
    ids = {n["id"] for n in payload["notifications"]}
    assert {"leg:L4:start", "leg:L2:end", f"needs:2:question:{ts(-40)}"} <= ids


def test_the_issue_page_lists_legs_and_the_unblock_control(deployment):
    backend, *_ = deployment
    detail = backend.issue("demo", 5)
    assert detail["held_for_human"] is True and detail["board"]["status"] == "needs_you"
    assert [p["number"] for p in detail["pull_requests"]] == [50]

    asked = backend.issue("demo", 2)
    assert [(s["kind"], s["outcome"], s["question"]) for s in asked["steps"]] == [("RESOLVE", "asked", "Which API?")]

    done = backend.issue("demo", 8)
    assert done["done"] is True and done["rail"]["current"] == "merge"
    assert backend.issue("demo", 999) is None


def test_overview_metrics_come_from_legs_and_merges(deployment):
    backend, *_ = deployment
    overview = backend.overview("demo", "UTC")
    m = overview["metrics"]
    assert (m["merged_prs"], m["open_items"], m["needs_human"]) == (1, 5, 2)
    assert m["total_cost_usd"] == 15.0  # 2 + 3 settled, 10 reserved by the running leg
    assert m["cost_per_merged_pr_usd"] == 15.0
    assert [a["issue"] for a in overview["needs_attention"]] == [2, 5]
    assert overview["caps"]["issue_cap_usd"] == 25.0


def _every_read(backend) -> list:
    from issue_agent.app.dashboard import collect

    return [backend.issues("demo"), backend.overview("demo", "UTC"), backend.issue("demo", 2), backend.repos(),
            backend.issues("demo", wait=True), collect("demo", backend=backend, observe=True)]


def _fingerprint(db: Path) -> dict:
    """The store's bytes and journal mode. The main file and the WAL's frames
    must not change. A WAL reader (any, read-only included) creates an empty
    `-wal`/`-shm` pair when none exists and records its read mark in `-shm`,
    SQLite's shared-memory index, not durable state: absent `-wal` reads as
    empty and `-shm` is compared only by its existence."""
    def content(path: Path) -> str:
        return hashlib.sha256(path.read_bytes() if path.exists() else b"").hexdigest()

    probe = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    try:
        mode = probe.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        probe.close()
    return {"main": content(db), "wal": content(Path(f"{db}-wal")), "mode": mode,
            "others": sorted(p.name for p in db.parent.iterdir() if not p.name.startswith(db.name))}


def test_no_read_path_writes_the_control_store(deployment):
    backend, _log, db, _gateway = deployment
    before = _fingerprint(db)
    assert before["mode"] == "wal"
    _every_read(backend)
    assert _fingerprint(db) == before


def test_a_legacy_store_in_delete_mode_is_unavailable_and_untouched(deployment, tmp_path):
    """The cutover leaves a refused legacy store in DELETE mode; a read-write
    open would flip it to WAL and race the daemon's retry."""
    backend, *_ = deployment
    legacy = tmp_path / "legacy" / "control.db"
    legacy.parent.mkdir()
    db = sqlite3.connect(legacy)
    db.execute("PRAGMA journal_mode=DELETE")
    db.execute("CREATE TABLE schema_migrations(version INTEGER)")
    db.commit()
    db.close()
    backend.control_path = lambda cfg: str(legacy)
    backend._configs = type(backend._configs)(ttl_s=0)
    before = _fingerprint(legacy)
    listing = sorted(p.name for p in legacy.parent.iterdir())

    payload, overview, *_ = _every_read(backend)

    assert _fingerprint(legacy) == before and before["mode"] == "delete"
    assert sorted(p.name for p in legacy.parent.iterdir()) == listing
    assert payload["in_flight"] == [] and "predates the 3-table cutover" in payload["store_error"]
    assert overview["store_error"] == payload["store_error"]


@pytest.mark.parametrize("make", ["missing", "garbage"])
def test_an_unreadable_store_derives_no_board_and_says_so(deployment, tmp_path, make):
    backend, *_ = deployment
    path = tmp_path / "bad.db"
    if make == "garbage":
        path.write_bytes(b"not a database" * 100)
    backend.control_path = lambda cfg: str(path)
    backend._configs = type(backend._configs)(ttl_s=0)

    payload = backend.issues("demo")

    assert payload["in_flight"] == [] and payload["store_error"].startswith("control store unavailable")
    assert payload["gh_ok"] is True  # GitHub was read; the store was not
    assert backend.repos()["repos"][0]["in_flight"] is None
    assert make == "garbage" or not path.exists()


def test_an_unresolvable_store_path_still_allows_reads_and_writes(deployment):
    backend, log, *_ = deployment

    def conflict(cfg):
        raise RuntimeError("two authorities")

    backend.control_path = conflict
    backend._configs = type(backend._configs)(ttl_s=0)
    assert "two authorities" in backend.issues("demo")["store_error"]
    assert backend.promote("demo", 6) == (True, "ok") and log[-1][0] == "labels"


def test_the_only_writes_are_label_changes(deployment):
    pytest.importorskip("fastapi")
    from issue_agent.app.dashweb import server

    backend, log, *_ = deployment
    app = server.create_app(backend=backend, offload_backend=False)
    posts = sorted(route.path for route in app.routes if "POST" in getattr(route, "methods", set()))
    assert posts == [f"/api/repos/{{name}}/issues/{{number}}/{verb}" for verb in ("promote", "unblock")]

    assert backend.unblock("demo", 7) == (True, "ok")
    assert backend.promote("demo", 6) == (True, "ok")
    assert backend.unblock("nosuch", 7)[0] is False
    assert log == [("move_hold", 7, False), ("labels", 6, ("ai-ready",), ())]


@pytest.mark.parametrize("number", [8, 30, 404])  # closed, a PR, unknown
def test_promote_refuses_anything_but_an_open_issue(deployment, number):
    backend, log, *_ = deployment
    ok, detail = backend.promote("demo", number)
    assert not ok and "not an open issue" in detail and log == []


def test_a_failing_write_is_a_refusal_not_a_server_error(deployment):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from issue_agent.app.dashweb import server

    backend, *_ = deployment

    def explode(cfg):
        raise RuntimeError("config gone")

    backend.deployment = explode
    client = TestClient(server.create_app(backend=backend, offload_backend=False), base_url="http://127.0.0.1:8765",
                        headers={"X-Issue-Agent-Dashboard": "1"})
    for verb in ("promote", "unblock"):
        response = client.post(f"/api/repos/demo/issues/6/{verb}")
        assert response.status_code == 400 and "config gone" in response.json()["detail"]


def test_a_write_refreshes_the_observation(deployment):
    backend, _log, _db, gateway = deployment
    backend.issues("demo")
    reads = gateway.reads
    backend.unblock("demo", 7)
    backend.issues("demo")
    assert gateway.reads == reads + 1


def test_a_review_waits_on_the_review_legs_price_while_a_fix_leg_still_fits(deployment, tmp_path):
    """Daily cap $12, nothing spent: a $10 fix leg fits, a $15 review leg
    does not, so the review role waits and the board says so."""
    backend, *_ = deployment
    env = tmp_path / "root" / "config" / ".active.demo.env"
    env.write_text(env.read_text().replace('DAILY_SPEND_CAP_USD="700"', 'DAILY_SPEND_CAP_USD="12"'))
    empty = tmp_path / "empty" / "control.db"
    SQLiteControlStore(str(empty)).initialize()
    backend.control_path = lambda cfg: str(empty)
    backend._configs = type(backend._configs)(ttl_s=0)
    green = Observed("h9", "h9", frozenset(), (Check("test", state="SUCCESS", bucket="pass"),))
    backend.gateway_factory = lambda repo: Gateway(WorldState(
        issues=(IssueView(1, "fix me", labels=frozenset({Label.AI_READY})),),
        prs=(PRView(90, head_ref_name="fix/9-pr", head_ref_oid="h9", ci=green, mergeable="MERGEABLE",
                    merge_state="CLEAN"),),
        now=ts(0)))
    backend._observations = type(backend._observations)(executor=InlineExecutor())  # type: ignore[arg-type]
    backend.issues("demo")

    board = {card["issue"]: card["board"] for card in backend.issues("demo")["in_flight"]}

    assert (board[1]["status"], board[1]["act"]) == ("working", "RESOLVE")
    assert (board[9]["status"], board[9]["on"]) == ("waiting", "headroom")


def test_a_truncated_merged_list_is_not_a_github_failure(deployment):
    backend, *_ = deployment
    backend.gh = Gh(merged=[{"number": 1000 + n, "title": "t", "headRefName": f"fix/{2000 + n}-x",
                             "mergedAt": ts(-n)} for n in range(200)])
    backend._observations.invalidate("demo")
    backend.issues("demo")

    payload = backend.issues("demo")
    metrics = backend.overview("demo", "UTC")["metrics"]

    assert payload["gh_ok"] is True and any("limited to the newest 200" in w for w in payload["warnings"])
    assert metrics["merged_prs"] == 200 and metrics["merged_prs_partial"] is True


def test_a_failed_github_read_is_gh_not_ok(deployment):
    backend, *_ = deployment

    class Down(Gh):
        def issues(self, repo, limit=1000):
            return None, "gh: HTTP 502"

    backend.gh = Down()
    backend._observations.invalidate("demo")
    backend.issues("demo")
    assert backend.issues("demo")["gh_ok"] is False


def test_carried_legs_show_as_history_not_failures(deployment):
    backend, _log, db, _gateway = deployment
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO legs(leg_id, repository, role, kind, issue, number, head, owner, runtime, state, "
                 "reserved_at, started_at, ended_at, currency, reserved_micros, charged_micros, basis, origin, "
                 "provider_receipt_id, settled_at) VALUES ('C1', 'o/r', 'carried', 'RESOLVE', 1, 0, '', 'carried', "
                 "'claude', 'ended', ?, ?, ?, 'USD', 4000000, 4000000, 'provider_reported', 'normal_return', 'rc', ?)",
                 (store_ts(-900), store_ts(-900), store_ts(-890), store_ts(-890)))
    conn.commit()
    conn.close()

    card = next(c for c in backend.issues("demo")["in_flight"] if c["issue"] == 1)
    detail = backend.issue("demo", 1)
    overview = backend.overview("demo", "UTC")["metrics"]

    assert card["cost_usd"] == 4.0 and card["rail"]["resolve_attempts"] == 0 and card["history"] == []
    assert [s["outcome"] for s in detail["steps"]] == ["carried"]
    assert overview["machine_legs"] == 0
