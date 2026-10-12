"""Review verdicts, required CI and retirement of local execution evidence."""

from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from issue_agent.adapters.workflow_rows import persist_workflow_snapshot
from issue_agent.domain.checks import Observed, RequiredCheck
from issue_agent.domain.fix import FixConfig, FixState, should_automerge
from issue_agent.domain.labels import Label
from issue_agent.domain.review import ReviewMode
from issue_agent.domain.world import Check
from issue_agent.domain.workflow import EvidenceUnavailable, NoAuthorizedRemediation, OpenGate
from issue_agent.app.config import deployment_identity_for_target
from tests.app.resolve_acceptance_harness import ScriptedProvider
from tests.app.review_acceptance_harness import (
    NHR, PR, ReviewDeployment, compose_review, create_review_deployment, install_clock, isolate_authority, verdicts,
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


@pytest.fixture
def interrupted_schema41(tmp_path, monkeypatch):
    """A schema-41 deployment captured from origin/dev 8251a37.

    Its APPROVE command carries the retired `test_evidence_ref`; the reply was
    lost after GitHub published the review, leaving it `unknown_outcome`.
    """
    fixtures = Path(__file__).parents[1] / "fixtures" / "review_cutover"
    metadata = json.loads((fixtures / "interrupted-approval.json").read_text())
    target = tmp_path / "target"
    subprocess.run(["git", "clone", "--quiet", str(fixtures / "interrupted-approval.bundle"), str(target)], check=True)
    result = ReviewDeployment(tmp_path, target, metadata["base"], tuple(metadata["heads"]))
    isolate_authority(result, monkeypatch)
    install_clock(result, monkeypatch)
    from issue_agent.adapters.control_store import SQLiteControlStore
    SQLiteControlStore(result.control_path, now=result.clock.now).initialize()
    result.github()
    sql = (fixtures / "interrupted-approval.control.sql").read_text()
    # Deployment identities hash the checkout path, so the captured owner
    # stays in this deployment's lineage only under the same rewrite.
    captured, restored = Path(metadata["root"]) / "target", target
    for role in ("fix", "review", "steward"):
        sql = sql.replace(deployment_identity_for_target("o/r", str(captured), role),
                          deployment_identity_for_target("o/r", str(restored), role))
    with sqlite3.connect(result.control_path) as db:
        _historical_fixture(db, 41)
        db.executescript(sql.replace(metadata["root"], str(tmp_path)))
        assert db.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 41
    with sqlite3.connect(result.github_path) as db:
        db.execute("DELETE FROM clock")
        db.executescript((fixtures / "interrupted-approval.github.sql").read_text())
    (tmp_path / "clock-offset").write_text("60")
    return result


@pytest.mark.parametrize("posted", [False, True])
def test_interrupted_schema41_submission_is_retired_and_next_tick_reviews(interrupted_schema41, posted):
    """GitHub stays authority: the forgotten approval's head is reviewed afresh.

    A published approval is not reconciled through the retired payload, so it
    is posted again on the same head; an unpublished one is posted once.
    """
    deployment = interrupted_schema41
    with sqlite3.connect(deployment.control_path) as db:
        (payload,) = db.execute("SELECT payload_json FROM lifecycle_commands WHERE kind='submit_review'").fetchone()
        assert "test_evidence_ref" in json.loads(payload)
    if not posted:
        with sqlite3.connect(deployment.github_path) as db:
            db.execute("DELETE FROM reviews")
    restarted = compose_review(deployment, provider=ScriptedProvider(verdicts("APPROVE", "APPROVE")))
    restarted.boot()
    restarted.tick(3)
    assert len(restarted.provider.tasks) == 1
    head = restarted.remote.get_pr(PR).head_ref_oid
    assert [(r.state, r.commit_id) for r in restarted.bot_reviews()] == [("APPROVED", head)] * (2 if posted else 1)
    with sqlite3.connect(deployment.control_path) as db:
        assert db.execute("SELECT count(*) FROM lifecycle_commands WHERE kind='submit_review' "
                          "AND state<>'succeeded'").fetchone() == (0,)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert not any(marker in line for line in restarted.log for marker in (
        "SubmissionUnresolved", "invalid workflow authority", "could not be recovered"))
