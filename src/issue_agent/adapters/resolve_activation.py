"""Offline resolve-family import and activation I/O; ADR 0015.

The only live reader of the retired `resolve-retries`/`last-error` files. It
never writes them. Activation opens existing authority without creating or
migrating it, holds the three exclusive deployment barriers, revalidates the
sources outside SQL, and commits epoch, imported workflows, ceiling gates and
import decisions in one short transaction.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import stat
from collections.abc import Callable
from contextlib import ExitStack, closing
from dataclasses import dataclass
from pathlib import Path

from ..domain.resolve_activation import (
    LEGACY_LAST_ERROR, LEGACY_RETRIES, LEGACY_SUFFIXES, MAX_LEGACY_FILE_BYTES,
    ActivationCommitted, ActivationDecision, AlreadyActive, DeploymentInventory,
    ExistingActivation, ImportedResolveTarget, LegacyAbsent, LegacyEntry, LegacyFile,
    LegacyPresent, LegacyResolveRecord, LegacyUnreadable, ResolveActivationPlan,
    UnmappedLegacyEntry, imported_snapshot, imported_workflow, imports_digest,
)
from ..domain.workflow_identity import IssueRef, WorkflowFamily, WorkflowKey, canonical_workflow_repository
from .control_store import SQLiteControlStore
from .deployment_authority import deployment_effect_barrier
from .workflow_codec import encode_workflow
from .workflow_rows import (
    WorkflowAuthorityError, family_run_scope_bounds, family_target_kind, persist_workflow_snapshot, workflow_scope,
    insert_workflow_component,
)

_LEGACY_NAME = re.compile(r"issue-(.*)\.(" + "|".join(re.escape(s) for s in LEGACY_SUFFIXES) + r")", re.DOTALL)
_ISSUE_NUMBER = re.compile(r"[1-9][0-9]{0,18}")
_ACTIVE_RUN_STATES = ("reserved", "starting", "running")
_TERMINAL_COMMAND_STATES = ("succeeded", "permanent_failure")
_ROLES = ("fix", "review", "steward")
_FAMILY = WorkflowFamily.RESOLVE.value


class ResolveActivationRefused(WorkflowAuthorityError):
    pass


class ActivationSourcesChanged(ResolveActivationRefused):
    """Revalidated sources or inventory no longer produce the reviewed plan."""


def _printable(name: str) -> str:
    return ascii(name)[1:-1][:256] or "?"


def _read_legacy_file(path: str) -> LegacyFile:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return LegacyAbsent()
    except OSError as exc:
        return LegacyUnreadable(f"cannot stat: {exc.strerror or exc}")
    if stat.S_ISLNK(metadata.st_mode):
        return LegacyUnreadable("symbolic link")
    if not stat.S_ISREG(metadata.st_mode):
        return LegacyUnreadable("not a regular file")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        return LegacyAbsent()
    except OSError as exc:
        return LegacyUnreadable(f"cannot open: {exc.strerror or exc}")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return LegacyUnreadable("not a regular file")
        chunks: list[bytes] = []
        remaining = MAX_LEGACY_FILE_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError as exc:
        return LegacyUnreadable(f"cannot read: {exc.strerror or exc}")
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    if len(raw) > MAX_LEGACY_FILE_BYTES:
        return LegacyUnreadable(f"exceeds {MAX_LEGACY_FILE_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return LegacyUnreadable("not valid UTF-8")
    return LegacyPresent(hashlib.sha256(raw).hexdigest(), text)


def read_legacy_resolve_state(state_dir: str, root: str = "fix") -> tuple[LegacyEntry, ...]:
    """Every legacy resolve key in one labelled state root; read-only."""
    try:
        names = sorted(entry.name for entry in os.scandir(state_dir))
    except FileNotFoundError:
        return ()
    except OSError as exc:
        return (UnmappedLegacyEntry("<state-dir>", _printable(f"state directory unreadable: {exc.strerror or exc}"), root),)
    files: dict[int, dict[str, LegacyFile]] = {}
    unmapped: list[UnmappedLegacyEntry] = []
    for name in names:
        match = _LEGACY_NAME.fullmatch(name)
        if match is None:
            continue
        number, suffix = match.groups()
        if _ISSUE_NUMBER.fullmatch(number) is None or int(number) > 2**63 - 1:
            unmapped.append(UnmappedLegacyEntry(_printable(name), "not a canonical positive issue number", root))
            continue
        files.setdefault(int(number), {})[suffix] = _read_legacy_file(os.path.join(state_dir, name))
    records: list[LegacyEntry] = [
        LegacyResolveRecord(issue, found.get(LEGACY_RETRIES, LegacyAbsent()), found.get(LEGACY_LAST_ERROR, LegacyAbsent()), root)
        for issue, found in sorted(files.items())
    ]
    return tuple(records + unmapped)


LegacyRoots = tuple[tuple[str, str], ...]


def distinct_roots(roots: LegacyRoots) -> LegacyRoots:
    """One label per physical directory; aliases would fake duplicates."""
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for label, path in roots:
        real = os.path.realpath(path)
        if real not in seen:
            seen.add(real)
            result.append((label, path))
    return tuple(result)


def read_legacy_resolve_roots(roots: LegacyRoots) -> tuple[LegacyEntry, ...]:
    """Every root that ever held resolve state; the same issue in two roots
    is ambiguous and blocks its import rather than choosing either."""
    return tuple(entry for label, path in distinct_roots(roots) for entry in read_legacy_resolve_state(path, label))


def _owner_repository(owner_id: str) -> str | None:
    try:
        return canonical_workflow_repository(owner_id.partition(":")[0])
    except ValueError:
        return None


def _attributed(rows, repository: str) -> tuple[str, ...]:
    # Work whose owner cannot be attributed to another repository is ours:
    # an unattributable run can never be silently mapped away.
    return tuple(sorted({
        str(row[0]) for row in rows if _owner_repository(str(row[1])) in (repository, None)
    }))


def _ours(value: object, repository: str) -> bool:
    """Legacy rows may hold case/whitespace variants or unparseable values;
    only a value that canonicalises to another repository is someone else's."""
    try:
        return canonical_workflow_repository(str(value)) == repository
    except ValueError:
        return True


def read_inventory(db: sqlite3.Connection, repository: str) -> DeploymentInventory:
    repository = canonical_workflow_repository(repository)
    return family_inventory(db, repository, WorkflowFamily.RESOLVE, _existing_activation(db, repository))


def family_inventory(
    db: sqlite3.Connection, repository: str, family: WorkflowFamily, activation: ExistingActivation | None,
) -> DeploymentInventory:
    """Non-quiescent work of any family, and `family`'s own retained authority.

    Runs, leases, claims, commands and reservations are the repository's
    whatever their family: an activation needs every writer stopped. Only
    `family`'s rows are its retained authority; another family's rows belong
    to that family's activation and never block this one.
    """
    repository = canonical_workflow_repository(repository)
    runs = _attributed(db.execute(
        f"SELECT run_id, owner_id FROM runs WHERE state IN ({','.join('?' * len(_ACTIVE_RUN_STATES))})",
        _ACTIVE_RUN_STATES,
    ), repository)
    leases = _attributed(db.execute("SELECT run_id, owner_id FROM leases"), repository)
    claims = tuple(sorted({
        f"{row['role']}:{row['target']}" for row in db.execute("SELECT repository, role, target FROM dispatch_claims")
        if _ours(row["repository"], repository)
    }))
    commands = tuple(sorted({
        str(row["command_id"]) for row in db.execute(
            "SELECT command_id, repository FROM lifecycle_commands WHERE state NOT IN (?, ?)",
            _TERMINAL_COMMAND_STATES,
        ) if _ours(row["repository"], repository)
    }))
    reservations = tuple(sorted({
        str(row["run_id"]) for row in db.execute(
            "SELECT run_id, repository, workload_repository FROM budget_reservations WHERE state='active'")
        if (scoped := [v for v in (row["repository"], row["workload_repository"]) if v != "*"]) == []
        or any(_ours(value, repository) for value in scoped)
    }))
    kind = family_target_kind(family)
    retained = {
        f"{row[0]}:{row[1]}" for table in ("workflows", "workflow_gates", "workflow_rearms")
        for row in db.execute(f"SELECT target_kind, target_number FROM {table} WHERE repository=? AND target_kind=?",
                              (repository, kind))
    }
    retained |= {
        f"run:{row[0]}" for row in db.execute(
            "SELECT run_id FROM runs WHERE workflow_episode_key>=? AND workflow_episode_key<?",
            family_run_scope_bounds(repository, family),
        )
    }
    return DeploymentInventory(
        repository, runs, leases, claims, commands, reservations, activation, tuple(sorted(retained)),
    )


def _existing_activation(db: sqlite3.Connection, repository: str) -> ExistingActivation | None:
    row = db.execute(
        "SELECT epoch, source_digest, inventory_digest FROM workflow_authority_epochs WHERE repository=? AND family=?",
        (repository, _FAMILY),
    ).fetchone()
    if row is None:
        return None
    decisions: list[ImportedResolveTarget] = []
    try:
        for item in db.execute(
            "SELECT * FROM workflow_activation_imports WHERE repository=? AND family=? ORDER BY target_number",
            (repository, _FAMILY),
        ):
            decision = ImportedResolveTarget.parse(item["decision_json"])
            if (item["target_kind"], item["target_number"], item["epoch"], item["source_digest"]) != (
                "issue", decision.issue, row["epoch"], row["source_digest"],
            ):
                raise ValueError("import decision index disagrees with its activation")
            decisions.append(decision)
        evidence = db.execute(
            "SELECT ceiling, plan_digest FROM workflow_activation_evidence WHERE repository=? AND family=? AND epoch=?",
            (repository, _FAMILY, row["epoch"]),
        ).fetchone()
        if evidence is None:
            raise ValueError("activation lacks its recorded ceiling and plan digest")
        if any(decision.ceiling != evidence["ceiling"] for decision in decisions):
            raise ValueError("import decisions disagree with the recorded ceiling")
        return ExistingActivation(
            row["epoch"], row["source_digest"], row["inventory_digest"], imports_digest(tuple(decisions)),
            evidence["ceiling"], evidence["plan_digest"],
        )
    except (ValueError, TypeError) as exc:
        raise WorkflowAuthorityError(f"invalid resolve activation evidence: {exc}") from exc


class SQLiteResolveActivation:
    """Operator-only activation; the daemons never construct this."""

    def __init__(self, control: SQLiteControlStore) -> None:
        if not isinstance(control, SQLiteControlStore):
            raise ValueError("activation requires the existing control store")
        self._control = control

    def inventory(self, repository: str) -> DeploymentInventory:
        self._control.require_initialized()
        with closing(self._control._connect()) as db:
            db.execute("BEGIN")
            try:
                return read_inventory(db, repository)
            finally:
                db.rollback()

    def activate(
        self, plan: ResolveActivationPlan, *, revalidate: Callable[[], ActivationDecision],
    ) -> ActivationCommitted:
        if not isinstance(plan, ResolveActivationPlan):
            raise ValueError("activation requires a validated plan")
        # Initialization has a different lock order; activation never creates,
        # adopts or migrates authority while holding these barriers.
        self._control.require_initialized()
        with ExitStack() as barriers:
            for role in _ROLES:
                barriers.enter_context(deployment_effect_barrier(self._control.path, plan.repository, role))
            fresh = revalidate()
            if isinstance(fresh, AlreadyActive) and fresh.plan == plan:
                replay = True
            elif isinstance(fresh, ResolveActivationPlan) and fresh == plan:
                replay = False
            else:
                raise ActivationSourcesChanged("legacy sources or deployment inventory changed since the plan was reviewed")
            with self._control.transaction() as db:
                current = read_inventory(db, plan.repository)
                if current.activation is not None:
                    if replay and plan.matches(current.activation):
                        return ActivationCommitted(plan, idempotent=True)
                    raise ResolveActivationRefused("resolve family is already active with a different plan")
                if replay:
                    raise ResolveActivationRefused("resolve activation disappeared while revalidating")
                if not current.quiescent or current.digest != plan.inventory_digest:
                    raise ActivationSourcesChanged("deployment inventory changed before commit: " + "; ".join(current.blockers()))
                if current.retained_targets:
                    raise ResolveActivationRefused("workflow authority is retained without an activation")
                self._commit(db, plan)
        return ActivationCommitted(plan)

    def _commit(self, db: sqlite3.Connection, plan: ResolveActivationPlan) -> None:
        db.execute(
            "INSERT INTO workflow_authority_epochs VALUES (?, ?, ?, ?, ?, ?)",
            (plan.repository, _FAMILY, plan.epoch, plan.source_digest, plan.inventory_digest,
             self._control._now().isoformat()),
        )
        db.execute(
            "INSERT INTO workflow_activation_evidence VALUES (?, ?, ?, ?, ?)",
            (plan.repository, _FAMILY, plan.epoch, plan.ceiling, plan.plan_digest),
        )
        held = 0
        for target in plan.imports:
            initial = imported_snapshot(plan, target)
            scope = workflow_scope(WorkflowKey(plan.repository, IssueRef(target.issue)))
            db.execute(
                "INSERT INTO workflows VALUES (?, ?, ?, ?, ?, ?)",
                (*scope, initial.revision.fence.generation, initial.revision.version,
                 encode_workflow((initial.authority, initial.episode))),
            )
            insert_workflow_component(db, initial.revision.fence.key, initial.authority, initial.episode.episode_id)
            after = imported_workflow(plan, target)
            if bool(after.gates) != target.held:
                raise ResolveActivationRefused(f"issue #{target.issue}: import decision disagrees with the ceiling policy")
            if after != initial:
                persist_workflow_snapshot(db, after)
                held += 1
            db.execute(
                "INSERT INTO workflow_activation_imports VALUES (?, ?, ?, ?, ?, ?, ?)",
                (plan.repository, _FAMILY, plan.epoch, *scope[1:], plan.source_digest, target.canonical()),
            )
        self._control._transition(db, "workflow_activation", f"{plan.repository}:{_FAMILY}", "activated", {
            "epoch": plan.epoch, "source_digest": plan.source_digest,
            "inventory_digest": plan.inventory_digest, "plan_digest": plan.plan_digest,
            "ceiling": plan.ceiling, "imported": len(plan.imports), "held": held,
        })


@dataclass(frozen=True)
class RehearsalCopy:
    control_path: str
    roots: LegacyRoots


def file_digests(paths: tuple[str, ...]) -> dict[str, str | None]:
    """Byte digests of sources a rehearsal promises never to write."""
    result: dict[str, str | None] = {}
    for path in paths:
        try:
            metadata = os.lstat(path)
            if stat.S_ISLNK(metadata.st_mode):
                result[path] = "link:" + os.readlink(path)
            elif stat.S_ISREG(metadata.st_mode):
                result[path] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            else:
                result[path] = f"mode:{stat.S_IFMT(metadata.st_mode)}"
        except FileNotFoundError:
            result[path] = None
    return result


def _overlaps(first: str, second: str) -> bool:
    return os.path.commonpath((first, second)) in (first, second)


def copy_for_rehearsal(
    control_path: str, roots: LegacyRoots, destination: str, *, protected: tuple[str, ...] = (),
    legacy_name: re.Pattern[str] = _LEGACY_NAME,
) -> RehearsalCopy:
    """Isolated copy of authority and legacy resolve sources; never overwrites.

    The original is never opened through the store (which would select WAL and
    could checkpoint into it). A read-only connection feeds the SQLite backup
    API, so committed WAL content is included; the copy is then checkpointed.
    Like any WAL reader it may leave SQLite's empty `-wal`/`-shm` sidecar; the
    database, witness and legacy files are never modified.
    """
    witness_path = f"{control_path}.authority"
    for path in (control_path, witness_path):
        metadata = os.lstat(path)
        if not stat.S_ISREG(metadata.st_mode):
            raise ResolveActivationRefused(f"rehearsal source is not a regular file: {path}")
    roots = distinct_roots(roots)
    # A destination inside an original would write into it (a directory named
    # like a legacy key permanently blocks real activation); one containing an
    # original could shadow it. Neither is isolated.
    originals = {os.path.realpath(p) for p in (
        control_path, os.path.dirname(control_path), witness_path, *(p for _, p in roots), *protected,
    )}
    target = os.path.realpath(destination)
    if any(_overlaps(target, original) for original in originals):
        raise ResolveActivationRefused("rehearsal destination must not be inside or contain an original path")
    root = Path(destination)
    if root.is_symlink() or (root.exists() and (not root.is_dir() or any(root.iterdir()))):
        raise ResolveActivationRefused("rehearsal destination must be a new or empty directory")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    (root / "control").mkdir(mode=0o700)
    (root / "state").mkdir(mode=0o700)
    copy_path = str(root / "control" / os.path.basename(control_path))
    with closing(sqlite3.connect(f"{Path(control_path).as_uri()}?mode=ro", uri=True)) as source, \
            closing(sqlite3.connect(copy_path)) as target:
        source.backup(target)
        target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    os.chmod(copy_path, stat.S_IRUSR | stat.S_IWUSR)
    fd = os.open(f"{copy_path}.authority", os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
    try:
        os.write(fd, Path(witness_path).read_bytes())
    finally:
        os.close(fd)
    copied: list[tuple[str, str]] = []
    for index, (label, state_dir) in enumerate(roots):
        target_root = root / "state" / str(index)
        target_root.mkdir(mode=0o700)
        _copy_legacy_root(state_dir, target_root, legacy_name)
        copied.append((label, str(target_root)))
    return RehearsalCopy(copy_path, tuple(copied))


def _copy_legacy_root(state_dir: str, copied_state: Path, legacy_name: re.Pattern[str]) -> None:
    try:
        names = sorted(entry.name for entry in os.scandir(state_dir) if legacy_name.fullmatch(entry.name))
    except FileNotFoundError:
        names = []
    for name in names:
        source_path = os.path.join(state_dir, name)
        target_path = copied_state / name
        metadata = os.lstat(source_path)
        if stat.S_ISLNK(metadata.st_mode):
            os.symlink(os.readlink(source_path), target_path)
        elif stat.S_ISDIR(metadata.st_mode):
            target_path.mkdir()
        elif stat.S_ISREG(metadata.st_mode):
            shutil.copyfile(source_path, target_path, follow_symlinks=False)
        else:
            raise ResolveActivationRefused(f"cannot reproduce special legacy entry {_printable(name)} in a rehearsal copy")
