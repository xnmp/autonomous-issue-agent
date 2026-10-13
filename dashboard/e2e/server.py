"""Full-stack server for Playwright: the real dashboard app and the real
`Backend` (derive, the read-only control store, the role journals and
heartbeats) over one fixture deployment. Only GitHub and the provider usage
meters are fixtures, so the board text and the legs rows the specs assert are
what the production read path produces."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import uvicorn

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.app.dashweb.server import create_app
from issue_agent.app.dashweb.service import Backend
from issue_agent.domain.checks import Check, Observed
from issue_agent.domain.labels import Label
from issue_agent.domain.state import Harness, StopReason, stop_tag
from issue_agent.domain.world import BlockerRef, Comment, IssueView, PRView, WorldState

# Noon of the current UTC day: every fixture leg falls inside the day the caps
# sum, whenever the suite runs.
NOW = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
REPO = "o/demo"
HARNESS = Harness("fixbot", "reviewbot")


def at(minutes: float) -> str:
    return (NOW + timedelta(minutes=minutes)).isoformat()


def stamp(minutes: float) -> str:
    return (NOW + timedelta(minutes=minutes)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def world() -> WorldState:
    ready = frozenset({Label.AI_READY})
    question = Comment("fixbot", f"{HARNESS.marker}\n{stop_tag(StopReason.QUESTION)}\nWhich API should it call?", at(-40))
    pending = Observed("h3", "h3", frozenset(), (Check("test", state="IN_PROGRESS", bucket="pending"),))
    return WorldState(
        issues=(
            IssueView(1, "Queued work", labels=ready),
            IssueView(2, "Asked a question", labels=ready | {Label.NEEDS_HUMAN}, comments=(question,)),
            IssueView(3, "Has a PR", labels=ready),
            IssueView(4, "Running now", labels=ready),
            IssueView(5, "Waits on a blocker", labels=ready, blocked_by=(BlockerRef(1),)),
        ),
        prs=(PRView(30, title="fix: has a PR", head_ref_name="fix/3-pr", head_ref_oid="h3", ci=pending,
                    mergeable="MERGEABLE", merge_state="CLEAN"),),
        now=at(0),
    )


def deployment(root: Path) -> None:
    state = root / "state"
    (root / "config" / "repos").mkdir(parents=True)
    body = (f'REPO="{REPO}"\nFIX_BOT="fixbot"\nREVIEW_BOT="reviewbot"\nSTATE_DIR="{state}/fix"\n'
            f'REVIEW_STATE_DIR="{state}/review"\nISSUE_SPEND_CAP_USD="25"\nDAILY_SPEND_CAP_USD="700"\n')
    (root / "config" / "repos" / "demo.env").write_text(body)
    (root / "config" / ".active.demo.env").write_text(body)
    for role in ("fix", "review"):
        (state / role).mkdir(parents=True)
        (state / role / "heartbeat.json").write_text(json.dumps({
            "in_flight": [{"kind": "RESOLVE", "number": 4}] if role == "fix" else [],
            "pid": 1, "state": "running", "updated_at": at(0)}))
    (state / "fix" / "journal.jsonl").write_text(
        json.dumps({"event": "agent_run", "run_id": "L3", "model": "gpt-6.1-sol"}) + "\n")

    path = root / "control.db"
    SQLiteControlStore(str(path)).initialize()
    db = sqlite3.connect(path)
    settled = ("INSERT INTO legs(leg_id, repository, role, kind, issue, number, head, owner, runtime, state, "
               "reserved_at, started_at, ended_at, currency, reserved_micros, charged_micros, basis, origin, "
               "provider_receipt_id, settled_at, leg_class, session_id, result_json) VALUES "
               "(?, ?, ?, ?, ?, ?, '', ?, 'codex', 'ended', ?, ?, ?, 'USD', 10000000, ?, ?, 'normal_return', ?, ?, ?, "
               "'s', ?)")
    db.execute(settled, ("L2", REPO, "fix", "RESOLVE", 2, 0, f"{REPO}:fix", stamp(-60), stamp(-60), stamp(-50),
                         2_000_000, "provider_reported", "r2", stamp(-50), "asked",
                         json.dumps({"question": "Which API should it call?"})))
    db.execute(settled, ("L3", REPO, "fix", "RESOLVE", 3, 0, f"{REPO}:fix", stamp(-30), stamp(-30), stamp(-20),
                         1_250_000, "configured_rate_card", "r3", stamp(-20), "agent",
                         json.dumps({"summary": "Opened the fix branch."})))
    db.execute(settled, ("C3", REPO, "carried", "ROUTE", 3, 0, "legacy", stamp(-90), stamp(-90), stamp(-89),
                         400_000, "synthetic", "c3", stamp(-89), None, "{}"))
    db.execute("INSERT INTO legs(leg_id, repository, role, kind, issue, number, head, owner, runtime, state, "
               "reserved_at, started_at, currency, reserved_micros) VALUES "
               "('L4', ?, 'fix', 'RESOLVE', 4, 0, '', ?, 'codex', 'running', ?, ?, 'USD', 10000000)",
               (REPO, f"{REPO}:fix", stamp(-5), stamp(-5)))
    db.execute("INSERT INTO leases(repository, role, target, owner, leg_id, pid, pgid, start_ticks, acquired_at) "
               "VALUES (?, 'fix', 4, ?, 'L4', 999999, 999999, 1, ?)", (REPO, f"{REPO}:fix", stamp(-5)))
    db.commit()
    db.close()


class Gateway:
    def build_world(self, _base):
        return world()


class Gh:
    def issues(self, _repo, limit=1000):
        return [{"number": 6, "title": "Someday", "state": "OPEN", "labels": [], "createdAt": at(-1440)}], ""

    def merged_prs(self, _repo, limit=200):
        return [], ""


class Inline:
    """Observations complete inside the poll that schedules them."""

    def submit(self, fn, *args):
        from concurrent.futures import Future

        future: Future = Future()
        future.set_result(fn(*args))
        return future


class FixtureBackend(Backend):
    def usage(self) -> dict:
        return {"claude": None, "codex": None, "harness": None, "warnings": []}

    def verification(self) -> dict:
        return {"rows": [], "warnings": []}


def fixture_app():
    root = Path(tempfile.mkdtemp(prefix="dashboard-e2e-"))
    deployment(root)
    backend = FixtureBackend(
        str(root), gh=Gh(), gateway_factory=lambda _repo: Gateway(), now_fn=lambda: NOW,
        environ={"PATH": "/usr/bin:/bin", "HOME": str(root)}, observe_executor=Inline(),  # type: ignore[arg-type]
        proc_scan=lambda: ({"demo": {"fix", "review"}}, True), control_path=lambda _cfg: str(root / "control.db"),
    )
    backend.issues("demo")  # the first poll schedules the observation; the page then reads it
    # The SPA bundle is this checkout's; the deployment is the fixture's.
    return create_app(backend, harness_root=str(Path(__file__).resolve().parents[2]))


if __name__ == "__main__":
    uvicorn.run(fixture_app(), host="127.0.0.1", port=4174, log_level="warning")
