"""A corrupt refusal-ledger row holds its issue and is cleared by the operator's re-arm (#262)."""

import pytest

from issue_agent.adapters.control_store import CorruptControlStore
from issue_agent.domain.workflow_identity import IssueRef, WorkflowKey
from tests.app.resolve_acceptance_harness import (
    ISSUE, compose, create_deployment, install_clock, isolate_authority,
    patch_publication_environment, seed_exhausted,
)


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    patch_publication_environment(monkeypatch)
    bare = create_deployment(tmp_path)
    isolate_authority(bare, monkeypatch)
    install_clock(bare, monkeypatch)
    seed_exhausted(bare)
    return bare


def _row_count(client) -> int:
    with client.control.transaction() as db:
        return db.execute(
            "SELECT count(*) FROM level_state WHERE namespace='fix-admission-refusals'"
        ).fetchone()[0]


def test_corrupt_row_holds_the_issue_then_rearm_clears_it_and_counting_restarts(deployment):
    client = compose(deployment)
    client.boot()
    key = WorkflowKey(client.key.repository, IssueRef(ISSUE))
    with client.control.transaction() as db:
        db.execute(
            "INSERT INTO level_state(namespace, scope, key, payload_json, updated_at) "
            "VALUES ('fix-admission-refusals', ?, ?, 'garbage', '2026-01-01T00:00:00+00:00')",
            (key.repository, str(ISSUE)),
        )
    selected = client.workflow.read_issue_recovery(key)

    with pytest.raises(CorruptControlStore):
        client.workflow.fix_admission_refusals(selected)

    assert client.request_rearm("operator-request")["outcome"] == "accepted"

    assert _row_count(client) == 0
    renewed = client.workflow.read_issue_recovery(key)
    assert client.workflow.fix_admission_refusals(renewed) == 0
    assert client.control.record_fix_admission_refusal(key.repository, ISSUE, renewed.episode_scope, "x") == 1
