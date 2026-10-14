"""A role heartbeat as the dashboard reads it: a hot-reload drain is reported
only by a well-formed `reload_pending: true`."""

from __future__ import annotations

import json

import pytest

from issue_agent.adapters.dashweb.system import read_beat

_BASE = {"state": "running", "updated_at": "2026-10-14T10:00:00+00:00",
         "in_flight": [{"kind": "FIX_CI", "number": 1078}]}


def test_a_drain_reported_by_the_daemon_is_read(tmp_path):
    path = tmp_path / "heartbeat.json"
    path.write_text(json.dumps(_BASE | {"reload_pending": True}))
    beat = read_beat(str(path))
    assert beat is not None and beat.reload_pending is True and beat.in_flight == (("FIX_CI", 1078),)


@pytest.mark.parametrize("value", [False, "true", 1, None, [True], {"x": 1}])
def test_an_absent_or_malformed_drain_flag_reads_as_no_drain(tmp_path, value):
    path = tmp_path / "heartbeat.json"
    path.write_text(json.dumps(_BASE | ({} if value is None else {"reload_pending": value})))
    beat = read_beat(str(path))
    assert beat is not None and beat.reload_pending is False
