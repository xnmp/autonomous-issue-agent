"""The dashboard over a fixture deployment: the board is what derive decides
for the observed world plus the store's legs and leases, and the only writes
are label changes."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from issue_agent.adapters.dashweb import ghdata
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

    def merged_prs(self, repo, limit=1000):
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
    body = (
        f'REPO="o/r"\nFIX_BOT="fixbot"\nREVIEW_BOT="reviewbot"\nSTATE_DIR="{state}/fix"\n'
        f'REVIEW_STATE_DIR="{state}/review"\nISSUE_SPEND_CAP_USD="25"\nDAILY_SPEND_CAP_USD="700"\n'
    )
    (config / "repos" / "demo.env").write_text(body)
    (config / ".active.demo.env").write_text(body)


def _store(path: Path) -> None:
    SQLiteControlStore(str(path)).initialize()
    db = sqlite3.connect(path)
    ended = (
        "INSERT INTO legs(leg_id, repository, role, kind, issue, number, head, owner, runtime, state, "
        "reserved_at, started_at, ended_at, currency, reserved_micros, charged_micros, basis, origin, "
        "provider_receipt_id, settled_at, leg_class, session_id, result_json) VALUES "
        "(?, 'o/r', 'fix', ?, ?, ?, '', 'o/r:fix', 'claude', 'ended', ?, ?, ?, 'USD', 10000000, ?, "
        "'provider_reported', 'normal_return', ?, ?, ?, 's', ?)"
    )
    db.execute(
        ended,
        (
            "L2",
            "RESOLVE",
            2,
            0,
            store_ts(-60),
            store_ts(-60),
            store_ts(-50),
            2_000_000,
            "r2",
            store_ts(-50),
            "asked",
            json.dumps({"question": "Which API?"}),
        ),
    )
    db.execute(
        ended,
        (
            "L8",
            "RESOLVE",
            8,
            0,
            store_ts(-300),
            store_ts(-300),
            store_ts(-290),
            3_000_000,
            "r8",
            store_ts(-290),
            "agent",
            "{}",
        ),
    )
    db.execute(
        "INSERT INTO legs(leg_id, repository, role, kind, issue, number, head, owner, runtime, state, "
        "reserved_at, started_at, currency, reserved_micros) VALUES "
        "('L4', 'o/r', 'fix', 'RESOLVE', 4, 0, '', 'o/r:fix', 'codex', 'running', ?, ?, 'USD', 10000000)",
        (store_ts(-5), store_ts(-5)),
    )
    db.execute(
        "INSERT INTO leases(repository, role, target, owner, leg_id, pid, pgid, start_ticks, acquired_at) "
        "VALUES ('o/r', 'fix', 4, 'o/r:fix', 'L4', 999999, 999999, 1, ?)",
        (store_ts(-5),),
    )
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
            PRView(
                30, head_ref_name="fix/3-pr", head_ref_oid="h3", ci=pending, mergeable="MERGEABLE", merge_state="CLEAN"
            ),
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
        gh=Gh(
            issues=[
                {"number": 6, "title": "someday", "state": "OPEN", "labels": [], "createdAt": ts(-1440)},
                {"number": 8, "title": "shipped", "state": "CLOSED", "labels": []},
            ],
            merged=[
                {"number": 80, "title": "fix 8", "url": "u80", "mergedAt": ts(-280), "headRefName": "fix/8-done"},
                {"number": 81, "title": "a human's", "headRefName": "feat/9-x", "mergedAt": ts(-10)},
            ],
        ),
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
    assert board[1]["board"]["status"] == "waiting" and board[1]["board"]["on"] == "slot"
    assert board[2]["board"] | {"detail": None} == {
        "status": "needs_you",
        "reason": "question",
        "headline": "Answer the agent's question",
        "detail": None,
        "target": "issue",
        "since_ts": ts(-40),
        "hint": "Reply on GitHub with the answer; the agent resumes with it.",
    }
    assert "Which API?" in board[2]["board"]["detail"] and board[2]["action"] == "open"
    assert (board[3]["board"]["activity"], board[3]["pr"], board[3]["pr_ci_state"]) == ("ci", 30, "pending")
    assert board[3]["rail"]["current"] == "ci"
    assert (board[4]["board"]["activity"], board[4]["board"]["act"], board[4]["leg_running"]) == (
        "agent",
        "RESOLVE",
        True,
    )
    assert board[5]["board"]["reason"] == "labelled" and board[5]["action"] == "unblock"
    assert board[2]["cost_usd"] == 2.0 and board[2]["rail"]["resolve_attempts"] == 1
    assert payload["observed_at"] == NOW.isoformat() and payload["gh_ok"] is True


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


def _every_read(backend) -> list:
    from issue_agent.app.dashboard import collect

    return [
        backend.issues("demo"),
        backend.overview("demo", "UTC"),
        backend.issue("demo", 2),
        backend.repos(),
        backend.issues("demo", wait=True),
        collect("demo", backend=backend, observe=True),
    ]


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
    return {
        "main": content(db),
        "wal": content(Path(f"{db}-wal")),
        "mode": mode,
        "others": sorted(p.name for p in db.parent.iterdir() if not p.name.startswith(db.name)),
    }


def test_no_read_path_writes_the_control_store(deployment):
    backend, _log, db, _gateway = deployment
    before = _fingerprint(db)
    assert before["mode"] == "wal"
    _every_read(backend)
    assert _fingerprint(db) == before


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
    client = TestClient(
        server.create_app(backend=backend, offload_backend=False),
        base_url="http://127.0.0.1:8765",
        headers={"X-Issue-Agent-Dashboard": "1"},
    )
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
    backend.gateway_factory = lambda repo: Gateway(
        WorldState(
            issues=(IssueView(1, "fix me", labels=frozenset({Label.AI_READY})),),
            prs=(
                PRView(
                    90,
                    head_ref_name="fix/9-pr",
                    head_ref_oid="h9",
                    ci=green,
                    mergeable="MERGEABLE",
                    merge_state="CLEAN",
                ),
            ),
            now=ts(0),
        )
    )
    backend._observations = type(backend._observations)(executor=InlineExecutor())  # type: ignore[arg-type]
    backend.issues("demo")

    board = {card["issue"]: card["board"] for card in backend.issues("demo")["in_flight"]}

    assert (board[1]["status"], board[1]["on"]) == ("waiting", "slot")
    assert (board[9]["status"], board[9]["on"]) == ("waiting", "headroom")


def test_a_truncated_merged_list_is_not_a_github_failure(deployment):
    backend, *_ = deployment
    backend.gh = Gh(
        merged=[
            {"number": 1000 + n, "title": "t", "headRefName": f"fix/{2000 + n}-x", "mergedAt": ts(-n)}
            for n in range(ghdata.MERGED_PR_LIMIT)
        ]
    )
    backend._observations.invalidate("demo")
    backend.issues("demo")

    payload = backend.issues("demo")
    metrics = backend.overview("demo", "UTC")["metrics"]

    assert payload["gh_ok"] is True and payload["warnings"] == [] and payload["merged_partial"] is True
    assert metrics["merged_prs"] == ghdata.MERGED_PR_LIMIT and metrics["merged_prs_partial"] is True


def test_the_overview_shows_the_caps_the_core_reads_and_each_roles_headroom(deployment):
    """Committed today is settled + reserved by `reserved_at` (the store's
    snapshot `tick.headroom` compares), so a running leg's reservation counts."""
    backend, *_ = deployment
    caps = backend.overview("demo", "UTC")["caps"]

    assert (caps["today_usd"], caps["global_today_usd"], caps["daily_cap_usd"]) == (15.0, 15.0, 700.0)
    assert [(row["role"], row["fits"]) for row in caps["headroom"]] == [("fix", True), ("review", True)]


def test_an_unreadable_store_has_no_caps_rather_than_zero(deployment, tmp_path):
    backend, *_ = deployment
    backend.control_path = lambda cfg: str(tmp_path / "absent.db")
    backend._configs = type(backend._configs)(ttl_s=0)

    caps = backend.overview("demo", "UTC")["caps"]

    assert caps["today_usd"] is None and caps["global_today_usd"] is None
    assert [row["fits"] for row in caps["headroom"]] == [None, None]


def test_the_overview_lists_the_repositorys_leases_with_their_legs(deployment):
    backend, _log, db, _gateway = deployment
    other = sqlite3.connect(db)
    other.execute(
        "INSERT INTO leases(repository, role, target, owner, leg_id, pid, pgid, start_ticks, acquired_at) "
        "VALUES ('x/other', 'fix', 4, 'x/other:fix', NULL, 7, 7, 1, ?)", (store_ts(-1),))
    other.commit()
    other.close()

    leases = backend.overview("demo", "UTC")["leases"]

    assert leases == [{
        "role": "fix", "issue": 4, "pr": None, "leg_id": "L4", "kind": "RESOLVE", "leg_state": "running",
        "pid": 999999, "acquired_ts": (NOW - timedelta(minutes=5)).isoformat(),
    }]


def test_role_health_reads_the_heartbeats_state_and_in_flight_dispatches(deployment, tmp_path):
    backend, *_ = deployment
    fix = tmp_path / "state" / "fix"
    fix.mkdir(parents=True)
    (fix / "heartbeat.json").write_text(json.dumps(
        {"in_flight": [{"kind": "REBASE", "number": 30}, {"kind": 1, "number": "x"}], "pid": 1, "state": "running",
         "updated_at": ts(-1)}))
    review = tmp_path / "state" / "review"
    review.mkdir(parents=True)
    (review / "heartbeat.json").write_text(json.dumps({"in_flight": [], "state": "stopped", "updated_at": ts(-1)}))

    health = {row["role"]: row for row in backend.overview("demo", "UTC")["health"]}

    assert (health["fix"]["state"], health["fix"]["stale"]) == ("running", False)
    assert health["fix"]["in_flight"] == [{"kind": "REBASE", "number": 30}]  # the malformed row is dropped
    assert (health["review"]["state"], health["review"]["stale"]) == ("stopped", True)


@pytest.mark.parametrize("body", ["", "{not json", '{"updated_at": "2026-10-13T12:00:00", "state": "running"}'])
def test_a_missing_malformed_or_naive_heartbeat_is_stale(deployment, tmp_path, body):
    backend, *_ = deployment
    fix = tmp_path / "state" / "fix"
    fix.mkdir(parents=True)
    if body:
        (fix / "heartbeat.json").write_text(body)

    health = {row["role"]: row for row in backend.overview("demo", "UTC")["health"]}

    assert health["fix"]["stale"] is True and health["fix"]["heartbeat_ts"] is None


def test_issue_legs_carry_their_model_from_the_role_journal(deployment, tmp_path):
    backend, *_ = deployment
    fix = tmp_path / "state" / "fix"
    fix.mkdir(parents=True)
    (fix / "journal.jsonl").write_text("\n".join([
        json.dumps({"event": "agent_run_start", "run_id": "L2", "model": ""}),
        "{torn",
        # The journal's own leg_id is not the table's: run_id is.
        json.dumps({"event": "agent_run", "run_id": "L2", "leg_id": "j-L8", "model": "gpt-6.1-sol"}),
        json.dumps({"event": "agent_run", "run_id": "", "leg_id": "L8", "model": "wrong"}),
    ]) + "\n")

    steps = backend.issue("demo", 2)["steps"]

    assert [(s["leg_id"], s["runtime"], s["model"], s["pricing_basis"]) for s in steps] == [
        ("L2", "claude", "gpt-6.1-sol", "provider_reported")]
    assert backend.issue("demo", 8)["steps"][0]["model"] is None  # no journal line: unknown, not guessed


def test_carried_legs_are_one_pre_cutover_total_not_legacy_kind_rows(deployment):
    backend, _log, db, _gateway = deployment
    carried = sqlite3.connect(db)
    carried.execute(
        "INSERT INTO legs(leg_id, repository, role, kind, issue, number, head, owner, runtime, state, reserved_at, "
        "ended_at, currency, reserved_micros, charged_micros, basis, origin, settled_at, provider_receipt_id) VALUES "
        "('C1', 'o/r', 'carried', 'ROUTE', 2, 0, '', 'legacy', 'codex', 'ended', ?, ?, 'USD', 0, 1500000, 'synthetic', "
        "'normal_return', ?, 'rc1')",
        (store_ts(-900), store_ts(-899), store_ts(-899)))
    carried.commit()
    carried.close()

    detail = backend.issue("demo", 2)
    kinds = {row["kind"] for row in backend.overview("demo", "UTC")["metrics"]["by_kind"]}

    assert [s["kind"] for s in detail["steps"]] == ["RESOLVE"]
    assert detail["carried"] == {"legs": 1, "cost_usd": 1.5} and detail["cost_usd"] == 3.5
    assert "ROUTE" not in kinds and "carried" in kinds


def test_a_malformed_lease_stamp_is_store_unavailable_not_a_server_error(deployment):
    backend, _log, db, _gateway = deployment
    bad = sqlite3.connect(db)
    bad.execute("UPDATE leases SET acquired_at='2026-10-13T11:55:00' WHERE leg_id='L4'")  # naive
    bad.commit()
    bad.close()

    payload = backend.repos()

    assert payload["repos"][0]["in_flight"] is None
    assert backend.overview("demo", "UTC")["store_error"].startswith("control store unavailable")


def test_a_journal_line_appended_after_a_read_is_seen_on_the_next(deployment, tmp_path):
    backend, *_ = deployment
    journal = tmp_path / "state" / "fix" / "journal.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text("")
    assert backend.issue("demo", 2)["steps"][0]["model"] is None

    with journal.open("a") as stream:
        stream.write(json.dumps({"event": "agent_run", "run_id": "L2", "model": "gpt-6.1-sol"}) + "\n")

    assert backend.issue("demo", 2)["steps"][0]["model"] == "gpt-6.1-sol"


def test_a_refresh_waits_for_the_slowest_github_read_not_the_sum(deployment):
    """Serial reads were ~24s per refresh (world 13.5 + issues 8 + merged 2.4)."""
    backend, *_ = deployment
    # Each read blocks until all three are in flight; serial reads would time out.
    barrier = threading.Barrier(3, timeout=5)

    class Overlapping:
        def build_world(self, _base):
            barrier.wait()
            return _world()

    class Gh:
        def issues(self, _repo):
            barrier.wait()
            return [], ""

        def merged_prs(self, _repo):
            barrier.wait()
            return [], ""

    backend.gateway_factory = lambda repo: Overlapping()
    backend.gh = Gh()
    backend._observations.invalidate("demo")

    payload = backend.issues("demo", wait=True)

    assert payload["gh_ok"] is True and payload["warnings"] == []



def test_triage_is_the_first_stage_and_the_issue_page_shows_its_tier_criteria_and_duplicate(deployment, tmp_path):
    backend, *_ = deployment
    triage = {"tier": "low", "acceptance_criteria": ["Esc closes it"], "duplicate_of": 2,
              "duplicate_reason": "Same dialog", "rationale": "One handler."}
    db = sqlite3.connect(tmp_path / "control.db")
    db.execute(
        "INSERT INTO legs(leg_id, repository, role, kind, issue, number, head, owner, runtime, state, reserved_at, "
        "started_at, ended_at, currency, reserved_micros, charged_micros, basis, origin, provider_receipt_id, "
        "settled_at, leg_class, session_id, result_json) VALUES ('T1', 'o/r', 'fix', 'TRIAGE', 1, 1, '', 'o/r:fix', "
        "'claude', 'ended', ?, ?, ?, 'USD', 2000000, 10000, 'provider_reported', 'normal_return', 'rt1', ?, 'agent', "
        "'t', ?)",
        (store_ts(-30), store_ts(-30), store_ts(-29), store_ts(-29), json.dumps({"triage": triage, "summary": ""})),
    )
    db.commit()
    db.close()

    card = next(c for c in backend.issues("demo")["in_flight"] if c["issue"] == 1)
    assert card["board"]["reason"] == "duplicate" and "#2" in card["board"]["detail"]
    assert card["rail"]["current"] == "triage"
    detail = backend.issue("demo", 1)
    assert detail["triage"] == {"tier": "low", "acceptance_criteria": ["Esc closes it"], "duplicate_of": 2,
                                "duplicate_reason": "Same dialog", "rationale": "One handler."}
    assert [(s["kind"], s["tier"]) for s in detail["steps"]] == [("TRIAGE", "low")]
    assert [visit["stage"] for visit in detail["history"]] == ["triage"]


def test_loading_the_first_observation_is_not_a_warning(deployment):
    """The dashboard says "Reading GitHub…" from a null `observed_at`; a warning
    chip for the same state double-reported loading as a fault."""
    backend, *_ = deployment

    class Pending:
        def submit(self, fn, *args):
            return Future()  # never resolves: the first observation has not landed

    backend._observations = type(backend._observations)(ttl_s=60, executor=Pending())

    pages = [backend.issues("demo"), backend.issue("demo", 5), backend.overview("demo")]

    assert [(p["observed_at"], p["warnings"]) for p in pages] == [(None, [])] * 3
