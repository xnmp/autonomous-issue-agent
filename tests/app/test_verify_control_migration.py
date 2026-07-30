import json
from datetime import datetime, timedelta, timezone

import pytest

from scripts.verify_control_migration import summarize_legacy, verify


def _ledger(path, *, hours=48, malformed=False):
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    rows = [
        {
            "event": "spend",
            "ts": start.isoformat(),
            "repo": "o/r",
            "issue": 1,
            "cost_usd": 1.25,
        },
        {
            "event": "spend",
            "ts": (start + timedelta(hours=hours)).isoformat(),
            "repo": "o/r",
            "issue": 2,
            "billed_cost_usd": 2,
            "cost_usd": 9,
        },
    ]
    text = "\n".join(json.dumps(row) for row in rows) + "\n"
    if malformed:
        text += "{broken\n"
    path.write_text(text)


def test_shadow_replay_and_primary_canary_are_independently_verified(tmp_path):
    ledger = tmp_path / "spend.jsonl"
    _ledger(ledger)

    report = verify(
        ledger,
        tmp_path / "control.db",
        canary_repository="o/r",
        minimum_hours=24,
    )

    assert report["status"] == "passed"
    assert report["legacy"]["amount_micros"] == 3_250_000
    assert report["shadow"] == {
        "rows_match": True,
        "totals_match": True,
        "idempotent_replay": True,
    }
    assert report["primary_canary"]["legacy_export_unchanged"] is True


def test_verifier_rejects_short_or_malformed_windows(tmp_path):
    short = tmp_path / "short.jsonl"
    _ledger(short, hours=1)
    with pytest.raises(RuntimeError, match="require 24h"):
        verify(
            short,
            tmp_path / "short.db",
            canary_repository="o/r",
            minimum_hours=24,
        )

    malformed = tmp_path / "malformed.jsonl"
    _ledger(malformed, malformed=True)
    assert summarize_legacy(malformed).rejected == 1
    with pytest.raises(RuntimeError, match="rejected 1"):
        verify(
            malformed,
            tmp_path / "malformed.db",
            canary_repository="o/r",
            minimum_hours=24,
        )
