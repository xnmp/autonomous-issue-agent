"""Operator composition opens existing authority without migration or adoption."""

from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import shlex
import sqlite3

import pytest

from issue_agent.adapters.control_path import canonical_control_store_path
from issue_agent.adapters.control_store import ControlStoreError, CorruptControlStore, RetiredOwnerError, SQLiteControlStore
from issue_agent.adapters.github import GitHubGateway
from issue_agent.adapters.store import Store
from issue_agent.adapters.worktree import Worktrees
from issue_agent.app import resolve_control
from issue_agent.app.config import from_env, owner_identity_for_target
from issue_agent.domain.actions import Target
from issue_agent.domain.contracts import RunId
from issue_agent.domain.labels import Label
from issue_agent.domain.workflow import ObservedIssue
from issue_agent.domain.world import IssueView
from tests.adapters.test_workflow_control import NOW
from tests.fakes.in_memory_github import InMemoryGitHub
from tests.fakes.workflow_authority import resolve_snapshot


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    env = {
        "REPO": "o/r", "TARGET_DIR": str(tmp_path / "checkout"),
        "STATE_DIR": str(tmp_path / "state" / "fix"),
        "REVIEW_STATE_DIR": str(tmp_path / "state" / "review"),
        "CONTROL_AUTHORITY_ROOT": str(tmp_path / "authority"),
        "ROLES": "fix review", "BASE_BRANCH": "release",
        "FIX_BOT_TOKEN": "isolated-fix-token", "FIX_BOT": "fix-bot", "GH_HOST": "git.example.test",
        "FIXER_RUNTIME": "codex", "MAX_INFRA_DEATHS": "3",
    }
    for key in (*env, "AGENT_ENV", "REPOINTED_FROM_DEPLOYMENTS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GH_HOST", env["GH_HOST"])
    monkeypatch.setattr("issue_agent.app.config.machine_control_authority_root", lambda: env["CONTROL_AUTHORITY_ROOT"])
    original_constructor = SQLiteControlStore.__init__
    def isolated_constructor(self, path, *args, **kwargs):
        assert Path(path).is_relative_to(tmp_path), "factory attempted to open non-isolated control authority"
        original_constructor(self, path, *args, **kwargs)
    monkeypatch.setattr(SQLiteControlStore, "__init__", isolated_constructor)
    Path(env["TARGET_DIR"]).mkdir()
    config = tmp_path / "config"
    config.mkdir()
    snapshot = config / ".active.target.env"

    def write(values):
        snapshot.write_text("".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items()))

    write(env)
    path = canonical_control_store_path(authority_root=env["CONTROL_AUTHORITY_ROOT"])
    store = SQLiteControlStore(path, now=lambda: NOW)
    store.initialize()

    def forbidden(*args, **kwargs):
        pytest.fail("operator factory attempted initialization, adoption, or a remote call")

    monkeypatch.setattr(SQLiteControlStore, "initialize", forbidden)
    monkeypatch.setattr(SQLiteControlStore, "adopt_legacy_owner", forbidden)
    monkeypatch.setattr("issue_agent.adapters.github._subprocess_runner", forbidden)
    return tmp_path, env, store, write


def _open(deployment, **kwargs):
    root, env, store, write = deployment
    return resolve_control.open_resolve_operator("target", harness_root=str(root), now=lambda: NOW, **kwargs)


def _dump(store):
    with closing(sqlite3.connect(store.path)) as db:
        return tuple(db.iterdump())


def test_factory_preserves_configured_fix_owner_credentials_base_host_and_authority(deployment):
    root, env, store, write = deployment
    before = _dump(store)
    workflow = _open(deployment, control_db_path=store.path)
    assert workflow.control.writer.repository == "o/r"
    assert workflow.control.writer.owner_id == owner_identity_for_target("o/r", env["TARGET_DIR"], "fix")
    assert isinstance(workflow.gateway, GitHubGateway)
    assert isinstance(workflow.worktrees, Worktrees)
    assert workflow.gateway.repo == "o/r"
    assert workflow.gateway.identity.env_token() == "isolated-fix-token"
    assert workflow.worktrees.repo_dir == env["TARGET_DIR"]
    assert workflow.worktrees.base_branch == "release"
    assert workflow.approved_host == "git.example.test"
    assert _dump(store) == before


@pytest.mark.parametrize("damage", ["missing-database", "missing-witness", "invalid-witness", "stale-schema", "wrong-schema", "empty-database"])
def test_operator_refuses_uninitialized_or_invalid_authority_without_repair(deployment, damage):
    root, env, store, write = deployment
    # Damage the durable database, not a main file recoverable from a live WAL.
    with closing(store._connect()) as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    database = Path(store.path)
    witness = Path(store.authority_witness_path)
    if damage == "missing-database":
        database.unlink()
    elif damage == "missing-witness":
        witness.unlink()
    elif damage == "invalid-witness":
        witness.write_text("not-control-authority\n")
    elif damage == "empty-database":
        database.write_bytes(b"")
    else:
        with store.transaction() as db:
            if damage == "stale-schema":
                db.execute("DELETE FROM schema_migrations WHERE version=(SELECT max(version) FROM schema_migrations)")
            else:
                db.execute("UPDATE schema_migrations SET source_digest='foreign-schema' WHERE version=(SELECT max(version) FROM schema_migrations)")
    before = database.read_bytes() if database.exists() else None
    old_witness = witness.read_bytes() if witness.exists() else None
    with pytest.raises((ControlStoreError, sqlite3.Error)):
        _open(deployment)
    assert (database.read_bytes() if database.exists() else None) == before
    assert (witness.read_bytes() if witness.exists() else None) == old_witness


def test_require_initialized_missing_parent_has_no_creation_side_effect(tmp_path):
    path = tmp_path / "never-created" / "control.db"
    with pytest.raises(CorruptControlStore):
        SQLiteControlStore(str(path)).require_initialized()
    assert not path.parent.exists()


def test_wrong_control_path_does_not_open_or_create_it(deployment):
    root, env, store, write = deployment
    wrong = root / "wrong" / "authority.db"
    before = _dump(store)
    with pytest.raises(ValueError, match="disagree"):
        _open(deployment, control_db_path=str(wrong))
    assert not wrong.parent.exists()
    assert _dump(store) == before


def test_retired_fix_owner_cannot_be_reactivated_or_adopted(deployment):
    root, env, store, write = deployment
    owner = owner_identity_for_target("o/r", env["TARGET_DIR"], "fix")
    with store.transaction() as db:
        db.execute("INSERT INTO retired_owners VALUES (?,?,?,?,?)", (owner, "o/r:fix:successor", "o/r", "fix", NOW.isoformat()))
    before = _dump(store)
    with pytest.raises(RetiredOwnerError):
        _open(deployment)
    assert _dump(store) == before


@pytest.mark.parametrize("key,value", [
    ("REPO", ""), ("REPO", "not/a/repository"),
    ("TARGET_DIR", ""), ("TARGET_DIR", "relative-checkout"),
    ("ROLES", "review"), ("ROLES", ""),
])
def test_invalid_active_scope_is_refused_before_authority_mutation(deployment, key, value):
    root, env, store, write = deployment
    write({**env, key: value})
    before = _dump(store)
    with pytest.raises(ValueError):
        _open(deployment)
    assert _dump(store) == before


def test_missing_active_snapshot_cannot_open_ambient_deployment(deployment):
    root, env, store, write = deployment
    (root / "config" / ".active.target.env").unlink()
    before = _dump(store)
    with pytest.raises(ValueError, match="environment file"):
        _open(deployment)
    assert _dump(store) == before


@pytest.mark.parametrize("name", ["", ".", "..", "../target", "TARGET", " target ", None, 1])
def test_noncanonical_name_cannot_select_another_snapshot(deployment, name):
    root, env, store, write = deployment
    before = _dump(store)
    with pytest.raises(ValueError, match="canonical"):
        resolve_control.open_resolve_operator(name, harness_root=str(root))
    assert _dump(store) == before


@pytest.mark.parametrize("missing", ["REPO", "TARGET_DIR"])
def test_incomplete_snapshot_does_not_inherit_ambient_target_authority(deployment, monkeypatch, missing):
    root, env, store, write = deployment
    monkeypatch.setenv(missing, env[missing])
    write({key: value for key, value in env.items() if key != missing})
    before = _dump(store)
    with pytest.raises(ValueError, match="explicit repository and checkout"):
        _open(deployment)
    assert _dump(store) == before


def test_host_mismatch_refuses_instead_of_querying_process_host(deployment, monkeypatch):
    root, env, store, write = deployment
    monkeypatch.setenv("GH_HOST", "other.example.test")
    before = _dump(store)
    with pytest.raises(ValueError, match="host differs"):
        _open(deployment)
    assert _dump(store) == before


def test_ambient_token_role_base_and_agent_env_cannot_replace_snapshot(deployment, monkeypatch):
    root, env, store, write = deployment
    monkeypatch.setenv("FIX_BOT_TOKEN", "other-token")
    monkeypatch.setenv("ROLES", "review")
    monkeypatch.setenv("BASE_BRANCH", "other-base")
    monkeypatch.setenv("AGENT_ENV", str(root / "must-not-read.env"))
    workflow = _open(deployment)
    assert isinstance(workflow.gateway, GitHubGateway)
    assert workflow.gateway.identity.env_token() == env["FIX_BOT_TOKEN"]
    assert workflow.worktrees.base_branch == env["BASE_BRANCH"]


def _compose(deployment):
    root, env, control, write = deployment
    cfg = from_env(env)
    store = Store(cfg.state_dir, runtime="codex")
    workflow = resolve_control.compose_resolve_rearm(
        cfg, control, InMemoryGitHub(), Worktrees(cfg.target_dir), store,
        now=lambda: NOW, log=lambda _: None,
    )
    from tests.fakes.workflow_authority import install_workflow, with_issue_recovery
    install_workflow(control, with_issue_recovery(resolve_snapshot()))
    install_workflow(control, with_issue_recovery(resolve_snapshot(2)))
    issue = ObservedIssue(resolve_snapshot().binding.fence.key, IssueView(1, updated_at="revision"), NOW)
    return workflow, cfg, store, issue


def test_composed_holds_are_labels_and_machine_deaths_never_hold(deployment):
    workflow, cfg, store, issue = _compose(deployment)
    labels = frozenset({Label.AI_BLOCKED, Label.AI_PARKED, Label.NEEDS_BUDGET_REVIEW})
    observed = replace(issue, issue=replace(issue.issue, labels=labels))
    assert set(workflow.external_holds(observed)) == {label.value for label in labels}
    from tests.fakes.accounted_workflow import seed_infra_outcome
    from issue_agent.domain import continuation_components as cc
    control = deployment[2]
    for _ in range(10):
        seed_infra_outcome(control, 1, owner_id=workflow.control.writer.owner_id)
    seed_infra_outcome(control, 1, owner_id=workflow.control.writer.owner_id, reclaim=True)
    assert workflow.external_holds(issue) == ()
    assert set(workflow.external_holds(observed)) == {label.value for label in labels}


@pytest.mark.parametrize("reader", ["read_issue_recovery", "fix_leg_ends"])
def test_unavailable_hold_reader_is_not_treated_as_clear(deployment, monkeypatch, reader):
    """The re-arm refuses on the same unreadable authority the fixer holds on."""
    workflow, cfg, store, issue = _compose(deployment)
    def unavailable(*args, **kwargs):
        raise ValueError("hold authority unavailable")
    monkeypatch.setattr(SQLiteControlStore, reader, unavailable)
    with pytest.raises(ValueError, match="hold authority unavailable"):
        workflow.external_holds(issue)
