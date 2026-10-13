"""The dashboard's read-only `legs` reader."""

from __future__ import annotations

import json
import sqlite3

import pytest

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.dashweb.legs import LegsUnavailable, read_legs

_INSERT = ("INSERT INTO legs(leg_id, repository, role, kind, issue, number, head, owner, runtime, state, "
           "reserved_at, started_at, ended_at, currency, reserved_micros, charged_micros, basis, origin, "
           "provider_receipt_id, settled_at, leg_class, session_id, usage_json, result_json) VALUES "
           "(?, ?, 'review', 'REVIEW', 7, 70, 'h', 'o/r:review', 'codex', 'ended', ?, ?, ?, 'USD', 5000000, ?, "
           "'estimate', 'normal_return', ?, ?, 'agent', 'sess', ?, ?)")


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "control.db"
    SQLiteControlStore(str(path)).initialize()
    return path


def _add(path, leg_id, repository="o/r", reserved="2026-10-13T08:00:00.000Z", usage=None, result=None):
    db = sqlite3.connect(path)
    db.execute(_INSERT, (leg_id, repository, reserved, reserved, "2026-10-13T08:05:00.000Z", 1_250_000,
                         f"r-{leg_id}", "2026-10-13T08:05:00.000Z", usage, result))
    db.commit()
    db.close()


def test_a_settled_review_leg_reads_with_its_money_tokens_and_verdict(store):
    usage = {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 2, "cache_write_tokens": 1,
             "source": "provider"}
    result = {"summary": "looks fine", "verdict": {"event": "request_changes", "body": "fix the test"}}
    _add(store, "a", usage=json.dumps(usage), result=json.dumps(result))

    (leg,) = read_legs(str(store), "O/R")
    assert (leg.kind, leg.issue, leg.number, leg.runtime) == ("REVIEW", 7, 70, "codex")
    assert (leg.charged_usd, leg.reserved_usd, leg.estimated) == (1.25, 5.0, True)
    assert leg.tokens is not None and leg.tokens.input_tokens == 10 and leg.tokens.cache_write_tokens == 1
    assert (leg.verdict, leg.verdict_body, leg.summary) == ("REQUEST_CHANGES", "fix the test", "looks fine")
    assert leg.outcome == "changes_requested" and leg.ended_at is not None and leg.ended_at.utcoffset() is not None


def test_other_repositories_are_filtered_and_order_is_by_reservation(store):
    _add(store, "late", reserved="2026-10-13T09:00:00.000Z")
    _add(store, "early", reserved="2026-10-13T07:00:00.000Z")
    _add(store, "elsewhere", repository="x/y")
    assert [leg.leg_id for leg in read_legs(str(store), "o/r")] == ["early", "late"]
    assert len(read_legs(str(store))) == 3


@pytest.mark.parametrize("usage,result", [
    ("not json", "not json"), (json.dumps([1, 2]), json.dumps("text")), (json.dumps({"input_tokens": "9"}), None),
])
def test_malformed_receipts_degrade_to_absent_fields(store, usage, result):
    _add(store, "a", usage=usage, result=result)
    (leg,) = read_legs(str(store), "o/r")
    assert leg.tokens is None and leg.verdict == "" and leg.summary == ""


def test_an_unparseable_reservation_stamp_skips_only_that_row(store):
    _add(store, "good")
    _add(store, "bad", reserved="yesterday")
    assert [leg.leg_id for leg in read_legs(str(store), "o/r")] == ["good"]


def test_a_missing_or_foreign_store_is_unavailable_not_created(tmp_path):
    missing = tmp_path / "absent.db"
    with pytest.raises(LegsUnavailable):
        read_legs(str(missing), "o/r")
    assert not missing.exists()
    foreign = tmp_path / "foreign.db"
    sqlite3.connect(foreign).close()
    with pytest.raises(LegsUnavailable):
        read_legs(str(foreign), "o/r")
    with pytest.raises(LegsUnavailable):
        read_legs("", "o/r")
