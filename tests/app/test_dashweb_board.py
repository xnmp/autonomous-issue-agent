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


def test_reading_never_writes_the_control_store(deployment):
    backend, _log, db, _gateway = deployment
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    backend.issues("demo"), backend.overview("demo"), backend.issue("demo", 2), backend.repos()
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_an_unreadable_store_still_derives_the_board_and_says_so(deployment, tmp_path):
    backend, *_ = deployment
    backend.control_path = lambda cfg: str(tmp_path / "missing.db")
    backend._configs = type(backend._configs)(ttl_s=0)
    payload = backend.issues("demo")
    assert payload["in_flight"] and any("control store" in w for w in payload["warnings"])


def test_the_only_writes_are_label_changes(deployment):
    pytest.importorskip("fastapi")
    from issue_agent.app.dashweb import server

    backend, log, *_ = deployment
    app = server.create_app(backend=backend, offload_backend=False)
    posts = sorted(route.path for route in app.routes if "POST" in getattr(route, "methods", set()))
    assert posts == [f"/api/repos/{{name}}/issues/{{number}}/{verb}" for verb in ("park", "promote", "unblock")]

    assert backend.park("demo", 7) == (True, "ok")
    assert backend.unblock("demo", 7) == (True, "ok")
    assert backend.promote("demo", 6) == (True, "ok")
    assert backend.park("nosuch", 7)[0] is False
    assert log == [("move_hold", 7, True), ("move_hold", 7, False), ("labels", 6, ("ai-ready",), ())]


def test_a_write_refreshes_the_observation(deployment):
    backend, _log, _db, gateway = deployment
    backend.issues("demo")
    reads = gateway.reads
    backend.park("demo", 7)
    backend.issues("demo")
    assert gateway.reads == reads + 1
