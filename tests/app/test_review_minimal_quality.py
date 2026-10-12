"""Review verdicts, required CI and retirement of local execution evidence."""

from dataclasses import replace
import sqlite3

import pytest

from issue_agent.adapters.workflow_rows import persist_workflow_snapshot
from issue_agent.domain.checks import Observed, RequiredCheck
from issue_agent.domain.fix import FixConfig, FixState, should_automerge
from issue_agent.domain.labels import Label
from issue_agent.domain.review import ReviewMode
from issue_agent.domain.world import Check
from issue_agent.domain.workflow import EvidenceUnavailable, NoAuthorizedRemediation, OpenGate
from tests.app.resolve_acceptance_harness import ScriptedProvider
from tests.app.review_acceptance_harness import (
    NHR, PR, compose_review, create_review_deployment, install_clock, isolate_authority, verdicts,
)
from tests.adapters.test_control_store import _historical_fixture


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    result = create_review_deployment(tmp_path)
    isolate_authority(result, monkeypatch)
    install_clock(result, monkeypatch)
    result.activate()
    compose_review(result).activate_execution(monkeypatch)
    return result


@pytest.mark.parametrize("bucket,mergeable", [("pending", False), ("fail", False), (None, False), ("pass", True)])
def test_model_approval_without_local_execution_still_requires_ci(deployment, tmp_path, monkeypatch, bucket, mergeable):
    marker = tmp_path / "target-tests-ran"
    monkeypatch.setenv("TEST_CMD", f"touch '{marker}'; exit 1")
    monkeypatch.setenv("ALLOW_UNTESTED", "invalid-retired-review-waiver")
    client = compose_review(deployment, provider=ScriptedProvider(verdicts("APPROVE")))
    client.boot()
    client.tick()
    assert client.bot_reviews()[-1].state == "APPROVED"
    assert not marker.exists()
    pr = client.remote.get_pr(PR)
    reports = () if bucket is None else (Check("required-unit", bucket=bucket, required=True),)
    pr = replace(pr, merge_state="CLEAN", ci=Observed(pr.head_ref_oid, pr.head_ref_oid,
                                frozenset({RequiredCheck("required-unit")}), reports))
    assert should_automerge(pr, FixState(), FixConfig(fix_bot="fix-bot")) is mergeable


@pytest.mark.parametrize("mode", [ReviewMode.SINGLE, ReviewMode.DELEGATED, ReviewMode.MULTI])
def test_workflow_changes_escalate_instead_of_model_approval(deployment, mode):
    provider = ScriptedProvider(verdicts("APPROVE"))
    if mode is ReviewMode.MULTI:
        from issue_agent.domain.outcome import AgentResult, ExitReason
        provider = ScriptedProvider([AgentResult(is_error=False, exit_reason=ExitReason.OK, cost_usd=0.1, structured={"reviewable": False, "reason": "configuration"})])
    client = compose_review(deployment, provider=provider, mode=mode)
    head = deployment.heads[0]
    client.remote.set_diff(head, "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n"
                                 "--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n"
                                 "@@ -1 +1 @@\n-old\n+new\n")
    client.boot()
    client.tick()
    review = client.bot_reviews()[-1]
    assert review.state == "COMMENTED" and ".github/workflows/" in review.body
    client.tick(2)
    assert Label(NHR) in client.remote.get_pr(PR).labels
    assert len(client.bot_reviews()) == 1


@pytest.mark.parametrize("rearming", [False, True])
def test_legacy_receipts_and_execution_hold_are_discarded_before_next_tick(deployment, rearming):
    client = compose_review(deployment, provider=ScriptedProvider(verdicts("APPROVE")))
    client.boot()
    state = client.state()
    gate = OpenGate("ordinary-test:legacy", state.revision.fence.generation,
                    EvidenceUnavailable("independent operator tests", deployment.clock.now(), 12, "unavailable"),
                    NoAuthorizedRemediation("retired execution renewal"))
    with client.review.control.transaction() as db:
        persist_workflow_snapshot(db, replace(state, gates=(gate,)))
    with sqlite3.connect(deployment.control_path) as db:
        _historical_fixture(db, 41)
        for suffix in ("intent", "invocation", "completion", "evidence"):
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?, 'o/r', 'legacy', ?, ?)",
                       (f"review-round-test-{suffix}", deployment.clock.now().isoformat(),
                        '{"type":"retired-execution-record","fields":{}}'))
        if rearming:
            db.execute("UPDATE workflow_gates SET state='rearming' WHERE gate_id=?", (gate.gate_id,))
            db.execute("INSERT INTO workflow_rearms VALUES (?, 'o/r', 'pr', ?, ?, ?, ?, 'pending', ?, ?, ?)",
                       ("legacy-renewal", PR, gate.gate_id, state.revision.fence.generation,
                        state.revision.fence.generation + 1, "legacy", "legacy", "legacy"))
    restarted = compose_review(deployment, provider=ScriptedProvider(verdicts("APPROVE")))
    restarted.boot()
    assert restarted.gates() == []
    restarted.tick()
    assert restarted.bot_reviews()[-1].state == "APPROVED"
    assert len(restarted.provider.tasks) == 1
