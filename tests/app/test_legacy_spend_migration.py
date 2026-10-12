import json
from datetime import datetime, timezone

import pytest

from issue_agent.adapters.control_store import LegacySpendMigrationError
from issue_agent.app import roles
from issue_agent.app.config import HarnessConfig


def config(tmp_path):
    state = str(tmp_path / "state")
    return HarnessConfig(
        repo="o/r",
        state_dir=state,
        review_state_dir=str(tmp_path / "review"),
    )


def test_production_migration_imports_rotated_ledgers_once(tmp_path, monkeypatch):
    ledger = tmp_path / "spend.jsonl"
    archived = tmp_path / "spend.2026-06.jsonl"
    row = {
        "event": "spend",
        "ts": datetime(2026, 6, 30, tzinfo=timezone.utc).isoformat(),
        "repo": "o/r",
        "issue": 42,
        "cost_usd": 1,
    }
    archived.write_text(json.dumps(row) + "\n")
    ledger.write_text(
        json.dumps(
            {
                **row,
                "ts": datetime(2026, 7, 30, tzinfo=timezone.utc).isoformat(),
                "cost_usd": 2,
            }
        )
        + "\n"
    )
    monkeypatch.setattr(roles, "default_ledger_path", lambda: str(ledger))
    cfg = config(tmp_path)

    assert roles.migrate_legacy_spend(cfg, cfg.state_dir) == 2
    assert roles.migrate_legacy_spend(cfg, cfg.state_dir) == 0


def test_production_migration_fails_closed_on_unattributed_spend(
    tmp_path, monkeypatch
):
    ledger = tmp_path / "spend.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "event": "spend",
                "ts": datetime.now(timezone.utc).isoformat(),
                "repo": "o/r",
                "issue": None,
                "cost_usd": 1,
            }
        )
        + "\n"
    )
    monkeypatch.setattr(roles, "default_ledger_path", lambda: str(ledger))
    cfg = config(tmp_path)

    with pytest.raises(LegacySpendMigrationError):
        roles.migrate_legacy_spend(cfg, cfg.state_dir)
