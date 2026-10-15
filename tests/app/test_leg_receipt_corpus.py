"""#441's money guardrail: every leg receipt in the live control store (scrubbed
into `tests/fixtures/leg_receipts_corpus.json`, with what the pre-#441 code
derived from it) reads exactly as it did. Attempt caps protect money, so a
receipt that changes class, cap, question or resume choice fails here."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.dashweb.readonly_store import ReadOnlyControlStore
from issue_agent.app.dashweb.service import leg_rows
from issue_agent.app.tick import leg_ends, run_item
from issue_agent.domain import dashboard as dash
from issue_agent.domain.attempt import AnswerResume, Continue, Fresh, latest_session, next_leg
from issue_agent.domain.backoff import LegEnd

CORPUS = json.loads((Path(__file__).parents[1] / "fixtures" / "leg_receipts_corpus.json").read_text())
ROWS, EXPECTED = CORPUS["rows"], CORPUS["expected"]


@pytest.fixture(scope="module")
def store(tmp_path_factory) -> tuple[str, SQLiteControlStore]:
    path = str(tmp_path_factory.mktemp("corpus") / "control.db")
    SQLiteControlStore(path).initialize()
    db = sqlite3.connect(path)
    for r in ROWS:
        settled = r["charged_micros"] is not None
        db.execute(
            "INSERT INTO legs(leg_id, repository, role, kind, issue, number, head, owner, runtime, state, reserved_at, "
            "started_at, ended_at, end_reason, currency, reserved_micros, charged_micros, basis, origin, "
            "provider_receipt_id, settled_at, leg_class, session_id, result_json) "
            "VALUES (?, 'o/r', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'USD', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (r["leg_id"], r["role"], r["kind"], r["issue"], r["number"], r["head"], f"o/r:{r['role']}", r["runtime"],
             r["state"], r["reserved_at"], r["started_at"], r["ended_at"], r["end_reason"], r["reserved_micros"],
             r["charged_micros"], r["basis"], r["origin"], f"p-{r['leg_id']}" if settled else None,
             r["ended_at"] if settled else None, r["leg_class"], r["session_id"], r["result_json"]))
    db.commit()
    db.close()
    return path, SQLiteControlStore(path)


def _ends(control: SQLiteControlStore):
    return leg_ends(control.leg_runs("o/r", sorted({r["issue"] for r in ROWS if r["issue"]})))


def _record(end: LegEnd) -> dict:
    return {"machine": end.machine, "capped": end.capped, "question": end.question, "evidence": sorted(end.evidence),
            "end_head": end.end_head, "tier": end.tier.value if end.tier else None, "end": end.end,
            "triage": end.triage.tier.value if end.triage else None,
            "steward": list(end.steward.acceptance_criteria) if end.steward else None,
            "ended_at": end.ended_at.isoformat()}


def _old_start(plan: dict):
    """The pre-#441 fix role's choice: continue a cut-off session, else resume the asked one, else fresh."""
    if plan["continues"]:
        return Continue(plan["continues"])
    return AnswerResume(plan["answer_session"]) if plan["answering"] and plan["answer_session"] else Fresh()


def test_the_corpus_spans_the_classes_the_rule_reads():
    receipts = [json.loads(r["result_json"]) if r["result_json"] else {} for r in ROWS]
    assert len(ROWS) >= 200
    assert {r["leg_class"] for r in ROWS} == {"agent", "asked", "machine", None}
    assert sum(1 for r in receipts if r.get("capped") is True) >= 2  # pre-#441 cap records
    assert sum(1 for p in EXPECTED["plans"].values() if p["continues"]) >= 3  # legs that were the free resume
    assert sum(1 for p in EXPECTED["plans"].values() if p["answering"]) >= 4  # legs that answered a question


def test_every_live_receipt_reads_as_the_pre_441_code_read_it(store):
    _, control = store
    got = {run.leg_id: _record(end) for run, end in _ends(control)}
    assert got == {leg: end for leg, end in EXPECTED["ends"].items() if end is not None}


def test_every_routed_leg_was_started_as_the_rule_starts_it(store):
    # The read-time rule sees the legs before each one in end order; the old fix
    # role saw the legs ended at its dispatch. One fix leg per issue at a time
    # makes those the same legs.
    _, control = store
    earlier: dict = {}
    for run, end in _ends(control):
        key = run_item(run)
        if run.leg_id in EXPECTED["plans"]:
            old = EXPECTED["plans"][run.leg_id]
            plan = next_leg(earlier.get(key, ()), end.runtime)
            assert (plan.start, plan.answering.leg_id if plan.answering else None) == (
                _old_start(old), old["answering"]), run.leg_id
        earlier.setdefault(key, []).append(end)


def test_each_item_next_leg_starts_as_before_on_either_runtime(store):
    _, control = store
    by_item: dict = {}
    for run, end in _ends(control):
        by_item.setdefault(run_item(run), []).append(end)
    checked = 0
    for key, ends in by_item.items():
        for runtime in ("claude", "codex"):
            old = EXPECTED["after"][f"{key.target.value}:{key.number}:{runtime}"]
            plan = next_leg(ends, runtime)
            assert (plan.start, plan.answering.leg_id if plan.answering else None) == (
                _old_start(old), old["answering"]), (key, runtime)
            if plan.answering is not None:
                assert latest_session((plan.answering,), runtime) == old["answer_session"]
            checked += 1
    assert checked == len(EXPECTED["after"])


def test_the_dashboard_reads_each_issue_triage_and_steward_as_before(store):
    path, _ = store
    with ReadOnlyControlStore(path) as ro:
        legs = leg_rows(ro, "o/r")
    for issue, old in EXPECTED["board"].items():
        own = dash.issue_legs(legs, int(issue))
        triage, steward = dash.issue_triage(own), dash.issue_steward(own)
        assert (triage.tier.value if triage else None) == old["triage"], issue
        assert (list(steward.acceptance_criteria) if steward else None) == old["steward"], issue


def test_the_dashboard_classes_every_leg_as_derive_does(store):
    path, control = store
    derived = {run.leg_id: ("machine" if end.machine else "asked" if end.question else "agent")
               for run, end in _ends(control)}
    with ReadOnlyControlStore(path) as ro:
        legs = leg_rows(ro, "o/r")
    # Every corpus leg ended (a refusal is released with an end), so derive classes each one.
    assert {leg.leg_id: leg.leg_class for leg in legs} == derived
