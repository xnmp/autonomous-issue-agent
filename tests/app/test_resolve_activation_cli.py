"""Operator activation CLI over an isolated launch snapshot; ADR 0015."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import gc
import json
from pathlib import Path
import shlex
import sqlite3

import pytest

from issue_agent.adapters.control_path import canonical_control_store_path
from issue_agent.adapters.control_store import SCHEMA_VERSION, SQLiteControlStore
from issue_agent.adapters.resolve_activation import file_digests
from issue_agent.adapters.workflow_control import SQLiteWorkflowControl
from issue_agent.app import cli
from issue_agent.app.config import owner_identity_for_target
from issue_agent.domain.contracts import RunId
from issue_agent.domain.workflow import WorkflowMigrationRequired, WorkflowSnapshot
from issue_agent.domain.workflow_identity import IssueRef, WorkflowKey, WorkflowWriter


def write_snapshot(root, env):
    (root / "config" / ".active.target.env").write_text(
        "".join(f"{key}={shlex.quote(value)}\n" for key, value in env.items()))


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    env = {
        "REPO": "o/r", "TARGET_DIR": str(tmp_path / "checkout"),
        "STATE_DIR": str(tmp_path / "state" / "fix"),
        "REVIEW_STATE_DIR": str(tmp_path / "state" / "review"),
        "STEWARD_STATE_DIR": str(tmp_path / "state" / "steward"),
        "CONTROL_AUTHORITY_ROOT": str(tmp_path / "authority"),
        "ROLES": "fix review steward", "GH_HOST": "github.com", "MAX_RESOLVE_RETRIES": "3",
        "FIX_BOT": "fix-bot",
    }
    for key in (*env, "AGENT_ENV", "REPOINTED_FROM_DEPLOYMENTS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("issue_agent.app.config.machine_control_authority_root", lambda: env["CONTROL_AUTHORITY_ROOT"])
    original_constructor = SQLiteControlStore.__init__

    def isolated_constructor(self, path, *args, **kwargs):
        assert Path(path).is_relative_to(tmp_path), "activation opened non-isolated control authority"
        original_constructor(self, path, *args, **kwargs)

    monkeypatch.setattr(SQLiteControlStore, "__init__", isolated_constructor)
    Path(env["TARGET_DIR"]).mkdir()
    Path(env["STATE_DIR"]).mkdir(parents=True)
    (tmp_path / "config").mkdir()
    write_snapshot(tmp_path, env)
    store = SQLiteControlStore(canonical_control_store_path(authority_root=env["CONTROL_AUTHORITY_ROOT"]))
    store.initialize()
    monkeypatch.setattr(cli, "HARNESS_ROOT", str(tmp_path))
    monkeypatch.setattr(cli, "_launcher_pids", lambda name: [])
    monkeypatch.setattr(cli, "_role_pids", lambda name: {})
    monkeypatch.setattr(cli, "REPOS_DIR", str(tmp_path / "config" / "repos"))
    return Path(env["STATE_DIR"]), store, env


def run(capsys, *argv):
    args = cli.build_parser().parse_args(["resolve-activation", *argv, "--name", "target"])
    code = args.func(args)
    out = capsys.readouterr()
    return code, (json.loads(out.out) if out.out.strip() else None), out.err


def dump(store):
    with closing(sqlite3.connect(store.path)) as db:
        return tuple(db.iterdump())


def workflow(store, env, number):
    owner = owner_identity_for_target("o/r", env["TARGET_DIR"], "fix")
    control = SQLiteWorkflowControl(store, WorkflowWriter("o/r", "fix", owner), resolve_ceiling=3)
    return control.load_workflow(WorkflowKey("o/r", IssueRef(number)))


def seed(state):
    (state / "issue-7.resolve-retries").write_text("3")
    (state / "issue-7.last-error").write_text("tests failed")
    (state / "issue-8.resolve-retries").write_text("1")


def test_inventory_and_shadow_are_read_only_reports(deployment, capsys):
    state, store, _ = deployment
    seed(state)
    before = (dump(store), file_digests(tuple(str(p) for p in state.iterdir())))

    code, inventory, _ = run(capsys, "inventory")
    assert code == 0, _
    assert inventory["status"] == "ready" and "shadow" not in inventory
    assert inventory["inventory"]["quiescent"] and inventory["inventory"]["activation"] is None
    assert [r["issue"] for r in inventory["records"]] == [7, 8]
    code, shadow, _ = run(capsys, "shadow")
    assert code == 0
    rows = {row["issue"]: row for row in shadow["shadow"]}
    assert rows[7]["agrees"] and rows[7]["legacy_exhausted"] and rows[7]["new_held"]
    assert rows[8]["agrees"] and rows[8]["new_eligible"] and rows[8]["new_remaining"] == 2
    assert shadow["plan"]["source_digest"] == inventory["plan"]["source_digest"]
    assert (dump(store), file_digests(tuple(str(p) for p in state.iterdir()))) == before


def test_activation_requires_the_reviewed_digest_and_stopped_watchers(deployment, capsys, monkeypatch):
    state, store, env = deployment
    seed(state)
    _, reviewed, _ = run(capsys, "shadow")
    digest = reviewed["plan"]["plan_digest"]
    before = dump(store)

    assert run(capsys, "activate")[0] == 2
    code, _, err = run(capsys, "activate", "--confirm-plan-digest", "0" * 64)
    assert code == 2 and "differs" in err
    monkeypatch.setattr(cli, "_role_pids", lambda name: {"fix": [4242]})
    code, _, err = run(capsys, "activate", "--confirm-plan-digest", digest)
    assert code == 2 and "4242" in err
    assert dump(store) == before
    assert workflow(store, env, 7) == WorkflowMigrationRequired(WorkflowKey("o/r", IssueRef(7)))

    monkeypatch.setattr(cli, "_role_pids", lambda name: {})
    code, result, _ = run(capsys, "activate", "--confirm-plan-digest", digest)
    assert code == 0 and result["status"] == "activated"
    held = workflow(store, env, 7)
    assert isinstance(held, WorkflowSnapshot) and held.gates and held.episode.last_failure == "tests failed"
    code, again, _ = run(capsys, "activate", "--confirm-plan-digest", digest)
    assert code == 0 and again["status"] == "already_active_with_this_plan"
    assert run(capsys, "inventory")[1]["status"] == "already_active"


def test_blocked_sources_refuse_activation_without_writing(deployment, capsys):
    state, store, _ = deployment
    (state / "issue-7.resolve-retries").write_text("oops")
    before = dump(store)
    code, report, _ = run(capsys, "shadow")
    assert code == 0 and report["status"] == "blocked" and report["plan"] is None
    assert any("issue #7" in reason for reason in report["blockers"])
    code, result, _ = run(capsys, "activate", "--confirm-plan-digest", "0" * 64)
    assert code == 1 and result["status"] == "blocked"
    assert dump(store) == before


def _downgrade_to_v21(store):
    # Isolated fixture of a deployment still at the previous schema.
    with store.transaction() as db:
        for statement in (
            "DROP INDEX effect_receipts_remote_object",
            "ALTER TABLE lifecycle_commands DROP COLUMN prewrite_read_json",
            "DROP TRIGGER workflow_retry_grant_insert", "DROP TRIGGER workflow_retry_grant_update",
            "DROP INDEX workflow_retry_open_grant",
            "ALTER TABLE workflow_retry_authorizations DROP COLUMN disposition",
            "ALTER TABLE workflow_retry_authorizations DROP COLUMN disposed_at",
            "ALTER TABLE workflow_retry_authorizations DROP COLUMN disposition_reason",
            "ALTER TABLE workflow_retry_authorizations DROP COLUMN accepted_at",
            "ALTER TABLE workflow_retry_authorizations DROP COLUMN first_visit_at",
            "ALTER TABLE workflow_retry_authorizations DROP COLUMN unavailable_reads",
        ):
            db.execute(statement)
        db.execute("DROP TABLE workflow_activation_evidence")
        db.execute("DROP TABLE workflow_activation_imports")
        db.execute("DELETE FROM schema_migrations WHERE version>=22")
    gc.collect()
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def test_rehearsal_migrates_and_activates_only_an_isolated_copy(deployment, capsys, tmp_path):
    state, store, _ = deployment
    seed(state)
    _downgrade_to_v21(store)
    originals = (store.path, store.authority_witness_path, *(str(p) for p in state.iterdir()))
    before = file_digests(originals)

    code, result, _ = run(capsys, "rehearse", "--copy-to", str(tmp_path / "rehearsal"))

    assert code == 0 and result["status"] == "rehearsed", result
    assert result["checks"] == {
        "copied_sources_match": True, "activated": True, "imports_read_back": True,
        "reactivation_idempotent": True, "different_plan_refused": True, "originals_unchanged": True,
    }
    assert file_digests(originals) == before
    with closing(sqlite3.connect(f"{Path(store.path).as_uri()}?mode=ro", uri=True)) as db:
        assert db.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 21
        assert db.execute("SELECT count(*) FROM workflow_authority_epochs").fetchone()[0] == 0
    with closing(sqlite3.connect(result["copy"]["control_path"])) as db:
        assert db.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == SCHEMA_VERSION
        assert db.execute("SELECT count(*) FROM workflow_activation_imports").fetchone()[0] == 2


def test_rehearsal_requires_a_destination(deployment, capsys):
    assert run(capsys, "rehearse")[0] == 2


def test_confirmed_plan_digest_binds_the_reviewed_ceiling(deployment, capsys, tmp_path):
    state, store, env = deployment
    (state / "issue-7.resolve-retries").write_text("3")
    _, reviewed, _ = run(capsys, "shadow")
    assert reviewed["plan"]["imports"][0]["held"] is True
    write_snapshot(tmp_path, {**env, "MAX_RESOLVE_RETRIES": "0"})
    before = dump(store)
    code, _, err = run(capsys, "activate", "--confirm-plan-digest", reviewed["plan"]["plan_digest"])
    assert code == 2 and "plan digest differs" in err
    assert dump(store) == before


def heartbeat(directory, *, state="running", age_s=0.0, raw=None):
    directory.mkdir(parents=True, exist_ok=True)
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat()
    (directory / "heartbeat.json").write_text(
        raw if raw is not None else json.dumps({"pid": 1, "state": state, "updated_at": stamp}))


@pytest.mark.parametrize("beat", [
    {}, {"state": "draining"}, {"raw": "{not json"}, {"raw": '{"state": "running"}'},
    {"raw": '{"state": "running", "updated_at": "2026-01-01T00:00:00"}'}, {"age_s": -3600},
])
def test_live_or_unreadable_heartbeat_refuses_activation(deployment, capsys, beat):
    state, store, env = deployment
    seed(state)
    _, reviewed, _ = run(capsys, "shadow")
    heartbeat(Path(env["REVIEW_STATE_DIR"]), **beat)
    before = dump(store)
    code, _, err = run(capsys, "activate", "--confirm-plan-digest", reviewed["plan"]["plan_digest"])
    assert code == 2 and "heartbeat" in err
    assert dump(store) == before


@pytest.mark.parametrize("beat", [{"state": "stopped"}, {"age_s": 7200}])
def test_stopped_or_stale_heartbeat_does_not_block(deployment, capsys, beat):
    state, store, env = deployment
    seed(state)
    _, reviewed, _ = run(capsys, "shadow")
    heartbeat(Path(env["REVIEW_STATE_DIR"]), **beat)
    assert run(capsys, "activate", "--confirm-plan-digest", reviewed["plan"]["plan_digest"])[0] == 0


def test_same_repository_deployment_heartbeat_refuses_activation(deployment, capsys, tmp_path):
    state, store, _ = deployment
    seed(state)
    _, reviewed, _ = run(capsys, "shadow")
    other = tmp_path / "other-deployment" / "fix"
    (tmp_path / "config" / "repos").mkdir()
    (tmp_path / "config" / "repos" / "other.env").write_text(f"REPO='O/R'\nSTATE_DIR='{other}'\n")
    heartbeat(other)
    code, _, err = run(capsys, "activate", "--confirm-plan-digest", reviewed["plan"]["plan_digest"])
    assert code == 2 and "other:" in err
    (tmp_path / "config" / "repos" / "other.env").write_text(f"REPO='someone/else'\nSTATE_DIR='{other}'\n")
    assert run(capsys, "activate", "--confirm-plan-digest", reviewed["plan"]["plan_digest"])[0] == 0


def test_rowless_activation_keeps_its_recorded_ceiling_and_digest(deployment, capsys, tmp_path):
    _, store, env = deployment
    _, reviewed, _ = run(capsys, "shadow")
    digest = reviewed["plan"]["plan_digest"]
    assert reviewed["plan"]["imports"] == []
    assert run(capsys, "activate", "--confirm-plan-digest", digest)[0] == 0
    write_snapshot(tmp_path, {**env, "MAX_RESOLVE_RETRIES": "0"})
    code, report, _ = run(capsys, "inventory")
    assert code == 0 and report["status"] == "active_with_differences"
    assert report["plan"]["ceiling"] == 3 and report["plan"]["plan_digest"] == digest
    assert report["inventory"]["activation"]["ceiling"] == 3
    assert "configured resolve ceiling 0 differs from the ceiling 3" in report["differences"][0]
    before = dump(store)
    code, replay, _ = run(capsys, "activate", "--confirm-plan-digest", digest)
    assert code == 0 and replay["status"] == "already_active_with_this_plan"
    code, other, _ = run(capsys, "activate", "--confirm-plan-digest", "0" * 64)
    assert code == 1 and other["status"] == "active_with_differences"
    assert dump(store) == before


def test_activation_records_its_event_through_the_committed_outbox(deployment, capsys):
    state, store, _ = deployment
    seed(state)
    _, reviewed, _ = run(capsys, "shadow")
    assert run(capsys, "activate", "--confirm-plan-digest", reviewed["plan"]["plan_digest"])[0] == 0
    with closing(sqlite3.connect(store.path)) as db:
        rows = [(json.loads(raw), emitted) for raw, emitted in db.execute(
            "SELECT event_json, emitted_at FROM event_outbox")]
    activations = [(event, emitted) for event, emitted in rows if "workflow_activation" in json.dumps(event)]
    assert len(activations) == 1 and activations[0][1] is not None
    feed = (state / "events-v2.jsonl").read_text()
    assert "workflow_activation" in feed and reviewed["plan"]["source_digest"] in feed


def test_every_role_root_and_named_older_root_is_imported(deployment, capsys, tmp_path):
    state, store, env = deployment
    review = Path(env["REVIEW_STATE_DIR"])
    review.mkdir(parents=True)
    older = tmp_path / "older-state"
    older.mkdir()
    (review / "issue-5.resolve-retries").write_text("3")
    (older / "issue-6.resolve-retries").write_text("1")
    _, report, _ = run(capsys, "shadow", "--legacy-state-dir", str(older))
    assert report["status"] == "ready"
    assert [(i["issue"], i["failures"]) for i in report["plan"]["imports"]] == [(5, 3), (6, 1)]
    (state / "issue-5.last-error").write_text("same issue in the fix root")
    _, blocked, _ = run(capsys, "shadow")
    assert blocked["status"] == "blocked"
    assert any("issue #5: ambiguous duplicate" in reason for reason in blocked["blockers"])
    assert run(capsys, "shadow", "--legacy-state-dir", str(tmp_path / "missing"))[0] == 2


def test_after_activation_live_work_still_reports_already_active(deployment, capsys, tmp_path):
    state, store, env = deployment
    seed(state)
    _, reviewed, _ = run(capsys, "shadow")
    digest = reviewed["plan"]["plan_digest"]
    assert run(capsys, "activate", "--confirm-plan-digest", digest)[0] == 0
    store.start_run(RunId("live"), "d", "RESOLVE", owner_identity_for_target("o/r", env["TARGET_DIR"], "fix"),
                    "cfg", state="running")
    code, report, _ = run(capsys, "inventory")
    assert code == 0 and report["status"] == "already_active" and report["plan"]["plan_digest"] == digest
    code, again, _ = run(capsys, "activate", "--confirm-plan-digest", digest)
    assert code == 0 and again["status"] == "already_active_with_this_plan"
    write_snapshot(tmp_path, {**env, "MAX_RESOLVE_RETRIES": "5"})
    code, differs, _ = run(capsys, "shadow")
    assert code == 0 and differs["status"] == "active_with_differences"
    assert "configured resolve ceiling 5" in differs["differences"][0]
    before = dump(store)
    code, replay, _ = run(capsys, "activate", "--confirm-plan-digest", digest)
    assert code == 0 and replay["status"] == "already_active_with_this_plan" and replay["differences"]
    code, refused, _ = run(capsys, "activate", "--confirm-plan-digest", "1" * 64)
    assert code == 1 and refused["status"] == "active_with_differences"
    assert dump(store) == before


def test_rehearsal_copies_every_scanned_root(deployment, capsys, tmp_path):
    state, store, env = deployment
    review = Path(env["REVIEW_STATE_DIR"])
    review.mkdir(parents=True)
    (review / "issue-5.resolve-retries").write_text("3")
    seed(state)
    code, result, _ = run(capsys, "rehearse", "--copy-to", str(tmp_path / "rehearsal"))
    assert code == 0 and result["status"] == "rehearsed", result
    assert [i["issue"] for i in result["plan"]["imports"]] == [5, 7, 8]
    assert [r["label"] for r in result["copy"]["legacy_roots"]] == ["fix", "review", "steward"]
    assert result["checks"]["different_plan_refused"] is True


def test_rehearsal_refuses_destinations_overlapping_any_original(deployment, capsys, tmp_path):
    state, store, env = deployment
    seed(state)
    before = sorted(p.name for p in state.iterdir())
    for destination in (state / "issue-5.resolve-retries", state / "nested" / "copy",
                        Path(store.path).parent / "copy", tmp_path / "state"):
        code, _, err = run(capsys, "rehearse", "--copy-to", str(destination))
        assert code == 2 and "inside or contain" in err, destination
    assert sorted(p.name for p in state.iterdir()) == before
    assert not (Path(store.path).parent / "copy").exists()
