"""Legacy resolve mutations stop at the durable family-activation fence."""

from contextlib import ExitStack, closing
import multiprocessing
from pathlib import Path

import pytest

from issue_agent.adapters import control_store as control_store_module
from issue_agent.adapters import operator_effects as operator_effects_module
from issue_agent.adapters.control_store import (
    CorruptControlStore,
    IncompatibleSchema,
    SQLiteControlStore,
)
from issue_agent.adapters.deployment_authority import deployment_effect_barrier
from issue_agent.adapters.operator_effects import build_operator_effects
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from tests.adapters.test_workflow_control import control, resolve_snapshot
from tests.fakes.workflow_authority import install_workflow


REPOSITORY = "o/r"
ROLES = ("fix", "review", "steward")


def _activate(
    path: str,
    started,
    acquired,
    release,
    outcomes,
) -> None:
    store = SQLiteControlStore(path)
    started.set()
    try:
        with ExitStack() as barriers:
            for role in ROLES:
                barriers.enter_context(
                    deployment_effect_barrier(path, REPOSITORY, role)
                )
            with store.transaction() as db:
                db.execute(
                    "INSERT INTO workflow_authority_epochs VALUES "
                    "(?, 'resolve', 1, 'source', 'inventory', "
                    "'2026-09-23T00:00:00+00:00')",
                    (REPOSITORY,),
                )
            acquired.set()
            if not release.wait(10):
                raise TimeoutError("activation release was not signalled")
    except BaseException as exc:
        outcomes.put(("error", type(exc).__name__, str(exc)))
    else:
        outcomes.put(("activated", "", ""))


def _hold_legacy(
    path: str,
    started,
    acquired,
    release,
    outcomes,
) -> None:
    store = SQLiteControlStore(path)
    started.set()
    try:
        with store.legacy_resolve_authority(REPOSITORY):
            acquired.set()
            if not release.wait(10):
                raise TimeoutError("legacy release was not signalled")
    except BaseException as exc:
        outcomes.put(("error", type(exc).__name__, str(exc)))
    else:
        outcomes.put(("legacy", "", ""))


def _join(process: multiprocessing.Process) -> None:
    process.join(timeout=10)
    if process.is_alive():
        process.kill()
        process.join(timeout=5)
        raise AssertionError("authority worker did not terminate")
    assert process.exitcode == 0


def _activation_exists(path: str) -> bool:
    store = SQLiteControlStore(path)
    with closing(store._connect()) as db:
        return db.execute(
            "SELECT 1 FROM workflow_authority_epochs "
            "WHERE repository=? AND family='resolve'",
            (REPOSITORY,),
        ).fetchone() is not None


def _v18_store(path: Path) -> SQLiteControlStore:
    path.parent.mkdir(parents=True)
    store = SQLiteControlStore(str(path))
    with store._connect(create=True):
        pass
    for migration in control_store_module._SCHEMA_MIGRATIONS:
        if migration.version > 18:
            break
        store._apply_schema_migration(migration)
    store._write_authority_witness()
    return store


def test_verified_current_inactive_authority_yields_without_writes(control) -> None:
    with closing(control._connect()) as db:
        before = tuple(db.iterdump())

    with control.legacy_resolve_authority(REPOSITORY):
        with closing(control._connect()) as db:
            during = tuple(db.iterdump())

    with closing(control._connect()) as db:
        after = tuple(db.iterdump())
    assert during == before == after


def test_active_family_refuses_even_when_the_target_workflow_is_absent(control) -> None:
    with control.transaction() as db:
        db.execute(
            "INSERT INTO workflow_authority_epochs VALUES "
            "(?, 'resolve', 1, 'source', 'inventory', "
            "'2026-09-23T00:00:00+00:00')",
            (REPOSITORY,),
        )

    with pytest.raises(WorkflowAuthorityError, match="legacy unblock is retired"):
        with control.legacy_resolve_authority(REPOSITORY):
            pytest.fail("active family admitted a legacy mutation")


def test_active_family_with_a_target_workflow_refuses_legacy_authority(control) -> None:
    install_workflow(control, resolve_snapshot())

    with pytest.raises(WorkflowAuthorityError, match="legacy unblock is retired"):
        with control.legacy_resolve_authority(REPOSITORY):
            pytest.fail("active workflow admitted a legacy mutation")


def test_corrupt_or_missing_activation_cannot_look_inactive(control) -> None:
    state = resolve_snapshot()
    install_workflow(control, state)
    with control.transaction() as db:
        db.execute(
            "UPDATE workflow_authority_epochs SET epoch='corrupt' "
            "WHERE repository=? AND family='resolve'",
            (REPOSITORY,),
        )
    with pytest.raises(ValueError, match="epoch"):
        with control.legacy_resolve_authority(REPOSITORY):
            pytest.fail("corrupt activation admitted a legacy mutation")

    with control.transaction() as db:
        db.execute(
            "DELETE FROM workflow_authority_epochs WHERE repository=?",
            (REPOSITORY,),
        )
    with pytest.raises(WorkflowAuthorityError, match="missing despite retained"):
        with control.legacy_resolve_authority(REPOSITORY):
            pytest.fail("orphaned workflow authority admitted a legacy mutation")


def test_absent_or_unwitnessed_control_is_not_created_or_adopted(tmp_path: Path) -> None:
    absent_path = tmp_path / "absent" / "control.db"
    absent = SQLiteControlStore(str(absent_path))
    with pytest.raises(CorruptControlStore, match="initialized authority"):
        with absent.legacy_resolve_authority(REPOSITORY):
            pytest.fail("missing authority admitted a legacy mutation")
    assert not absent_path.exists()
    assert not Path(absent.authority_witness_path).exists()

    unwitnessed_path = tmp_path / "unwitnessed" / "control.db"
    unwitnessed = SQLiteControlStore(str(unwitnessed_path))
    unwitnessed.initialize()
    Path(unwitnessed.authority_witness_path).unlink()
    with pytest.raises(CorruptControlStore, match="initialized authority"):
        with unwitnessed.legacy_resolve_authority(REPOSITORY):
            pytest.fail("unwitnessed authority was adopted by a legacy write")
    assert unwitnessed_path.exists()
    assert not Path(unwitnessed.authority_witness_path).exists()


def test_v18_authority_requires_explicit_upgrade_without_migration(tmp_path: Path) -> None:
    store = _v18_store(tmp_path / "v18" / "control.db")

    with pytest.raises(IncompatibleSchema, match="stopped at schema 18"):
        with store.legacy_resolve_authority(REPOSITORY):
            pytest.fail("old schema admitted a legacy mutation")

    with closing(store._connect()) as db:
        versions = tuple(
            row[0] for row in db.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        )
        activation_table = db.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='workflow_authority_epochs'"
        ).fetchone()
    assert versions[-1] == 18
    assert activation_table is None


def test_supplied_operator_control_is_reused_without_initialization(
    control, monkeypatch,
) -> None:
    monkeypatch.setattr(
        operator_effects_module,
        "control_store_path",
        lambda _state_dirs, temporary_on_missing=False: control.path,
    )
    monkeypatch.setattr(
        control,
        "initialize",
        lambda: pytest.fail("supplied authority was initialized"),
    )

    effects = build_operator_effects(
        REPOSITORY,
        ("/state/fix", "/state/review", "/state/steward"),
        identity="o/r:dashboard",
        control=control,
    )

    assert effects.store is control


def test_supplied_operator_control_must_match_the_canonical_path(
    control, monkeypatch,
) -> None:
    monkeypatch.setattr(
        operator_effects_module,
        "control_store_path",
        lambda _state_dirs, temporary_on_missing=False: control.path + ".other",
    )
    with pytest.raises(ValueError, match="established control authority"):
        build_operator_effects(
            REPOSITORY,
            ("/state/fix", "/state/review", "/state/steward"),
            identity="o/r:dashboard",
            control=control,
        )


def test_legacy_first_blocks_activation_until_legacy_authority_releases(control) -> None:
    context = multiprocessing.get_context("spawn")
    legacy_started, legacy_acquired, release_legacy = (
        context.Event(), context.Event(), context.Event()
    )
    activation_started, activation_acquired, release_activation = (
        context.Event(), context.Event(), context.Event()
    )
    outcomes = context.Queue()
    legacy = context.Process(
        target=_hold_legacy,
        args=(control.path, legacy_started, legacy_acquired, release_legacy, outcomes),
    )
    activation = context.Process(
        target=_activate,
        args=(
            control.path,
            activation_started,
            activation_acquired,
            release_activation,
            outcomes,
        ),
    )
    legacy.start()
    assert legacy_started.wait(5) and legacy_acquired.wait(5)
    activation.start()
    assert activation_started.wait(5)
    assert not activation_acquired.wait(0.2)
    assert not _activation_exists(control.path)

    release_legacy.set()
    assert activation_acquired.wait(5)
    assert _activation_exists(control.path)
    release_activation.set()
    _join(legacy)
    _join(activation)
    assert sorted(outcomes.get(timeout=2)[0] for _ in range(2)) == [
        "activated", "legacy",
    ]


def test_activation_first_makes_waiting_legacy_authority_refuse(control) -> None:
    context = multiprocessing.get_context("spawn")
    activation_started, activation_acquired, release_activation = (
        context.Event(), context.Event(), context.Event()
    )
    legacy_started, legacy_acquired, release_legacy = (
        context.Event(), context.Event(), context.Event()
    )
    outcomes = context.Queue()
    activation = context.Process(
        target=_activate,
        args=(
            control.path,
            activation_started,
            activation_acquired,
            release_activation,
            outcomes,
        ),
    )
    legacy = context.Process(
        target=_hold_legacy,
        args=(control.path, legacy_started, legacy_acquired, release_legacy, outcomes),
    )
    activation.start()
    assert activation_started.wait(5) and activation_acquired.wait(5)
    legacy.start()
    assert legacy_started.wait(5)
    assert not legacy_acquired.wait(0.2)

    release_activation.set()
    _join(activation)
    _join(legacy)
    first, second = (outcomes.get(timeout=2), outcomes.get(timeout=2))
    assert {first[0], second[0]} == {"activated", "error"}
    error = first if first[0] == "error" else second
    assert error[1] == "WorkflowAuthorityError"
    assert "legacy unblock is retired" in error[2]
