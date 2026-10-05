import json
from datetime import datetime, timezone

import pytest

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.app import cli

NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)


def test_cli_records_external_action_and_exports_same_observation_fold(tmp_path, capsys):
    database = tmp_path / "control.db"
    SQLiteControlStore(str(database)).initialize()
    observation = tmp_path / "observation.json"
    observation.write_text(json.dumps(dict(kind="intervention", record_id="service-restart", repository="o/r", actor="operator", provenance="local service logs", occurred_at=NOW.isoformat(), category="diagnosis", source="service", description="diagnosed service failure", duration_seconds=120, interruptions=1)))
    with pytest.raises(SystemExit) as first:
        cli.main(["measurement", "record", "--database", str(database), "--deployment", "test", "--input", str(observation)])
    assert first.value.code == 0
    assert json.loads(capsys.readouterr().out)["recorded"] is True
    specification = tmp_path / "window.json"
    specification.write_text(json.dumps(dict(started_at="2026-10-04T00:00:00+00:00", ended_at="2026-10-05T00:00:00+00:00", observation_cutoff="2026-10-05T00:00:00+00:00", supported_profile=dict(deployment="test", runtime="codex", model="gpt", posture="trusted inbound", config_version="v1"))))
    with pytest.raises(SystemExit) as exported:
        cli.main(["measurement", "export", "--database", str(database), "--repo", "o/r", "--specification", str(specification)])
    assert exported.value.code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["observations"]["interventions_observed"] == 1
    assert result["observations"]["measured_duration_seconds"] == 120
    assert result["observations"]["measured_interruptions"] == 1
    assert result["comparability"] == "indeterminate"
    assert result["primary_admitted_bindings"] == 0


def test_cli_invalid_input_returns_failure_without_partial_record(tmp_path, capsys):
    database = tmp_path / "control.db"
    SQLiteControlStore(str(database)).initialize()
    observation = tmp_path / "bad.json"
    observation.write_text('{"kind":"intervention", "duration_seconds":true}')
    with pytest.raises(SystemExit) as result:
        cli.main(["measurement", "record", "--database", str(database), "--deployment", "test", "--input", str(observation)])
    assert result.value.code == 1
    assert "measurement:" in capsys.readouterr().err


def test_dashboard_measurement_access_returns_the_same_local_export(tmp_path):
    pytest.importorskip("fastapi")
    from asgi_client import ASGITestClient
    from issue_agent.app.dashweb.server import create_app
    from issue_agent.app.measurement import local_export
    database = tmp_path / "control.db"
    SQLiteControlStore(str(database)).initialize()
    specification = dict(started_at="2026-10-04T00:00:00Z", ended_at="2026-10-05T00:00:00Z", observation_cutoff="2026-10-05T00:00:00Z", supported_profile=dict(deployment="test", runtime="codex", model="gpt", posture="trusted inbound", config_version="v1"))
    class LocalBackend:
        def known(self, name):
            return name == "test"
        def measurement(self, name, raw):
            return local_export(str(database), "o/r", raw)
    client = ASGITestClient(create_app(backend=LocalBackend()), base_url="http://127.0.0.1")
    result = client.get("/api/repos/test/measurement", params={"specification": json.dumps(specification)})
    assert result.status_code == 200
    assert result.json() == local_export(str(database), "o/r", specification)
    malformed = client.get("/api/repos/test/measurement", params={"specification": "[]"})
    assert malformed.status_code == 400
