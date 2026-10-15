"""Durable ownership and teardown of child process groups."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from .atomic_file import atomic_write
from .leg_sandbox import SandboxRefused
from ..domain.contracts import RunId
from .signal_log import send_signal


@dataclass(frozen=True)
class ProcessRegistration:
    run_id: RunId
    pgid: int
    leader: ProcessIdentity


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_ticks: int


@dataclass(frozen=True)
class LaunchIntent:
    run_id: RunId
    token: str
    state: str
    launcher: ProcessIdentity


class ProcessSupervisor:
    """Persist process groups before work proceeds and reap them idempotently.

    Registry files are intentionally one-per-run.  Atomic replace means a crash
    cannot leave a partially-written aggregate registry that hides every other
    live child.
    """

    def __init__(self, state_dir: str, *, grace_seconds: float = 10.0):
        if not state_dir:
            raise ValueError("process supervisor state directory is required")
        if grace_seconds < 0:
            raise ValueError("process grace period cannot be negative")
        self.state_dir = Path(state_dir).resolve()
        self.directory = self.state_dir / "process-groups"
        self.intent_directory = self.state_dir / "launch-intents"
        self.tmp_directory = self.state_dir / "leg-tmp"
        self.grace_seconds = grace_seconds

    def supervised_command(
        self, run_id: RunId, argv: tuple[str, ...] | list[str]
    ) -> tuple[str, ...]:
        """Wrap a command so its new-session leader registers before exec."""

        if not argv:
            raise ValueError("supervised command cannot be empty")
        token = self.prepare_launch(run_id)
        return (
            sys.executable,
            "-m",
            "issue_agent.adapters.supervised_exec",
            str(self.state_dir),
            str(run_id),
            token,
            *argv,
        )

    def launch_guard_env(self) -> dict[str, str]:
        """Identify the daemon that must remain alive through child registration."""

        identity = self.process_identity(os.getpid())
        if identity is None:
            raise ProcessLookupError("launcher process identity is unavailable")
        return {
            "ISSUE_AGENT_SUPERVISOR_PARENT_PID": str(identity.pid),
            "ISSUE_AGENT_SUPERVISOR_PARENT_START_TICKS": str(
                identity.start_ticks
            ),
        }

    def supervised_cli(
        self, run_id: RunId, command: Sequence[str]
    ) -> tuple[str, dict[str, str]]:
        """Return an SDK-compatible CLI path that registers, then execs
        `command` (the CLI, or the sandbox wrapping it) with the SDK's
        arguments appended."""

        if not command:
            raise ValueError("supervised CLI command cannot be empty")
        if not os.path.isfile(command[0]) or not os.access(command[0], os.X_OK):
            raise ValueError(f"supervised CLI is not executable: {command[0]}")
        token = self.prepare_launch(run_id)
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        wrapper = self.state_dir / "supervised-cli"
        content = (
            f"#!{sys.executable}\n"
            "from issue_agent.adapters.supervised_exec import supervised_cli_main\n"
            "supervised_cli_main()\n"
        )
        if not wrapper.exists() or wrapper.read_text(encoding="utf-8") != content:
            atomic_write(wrapper, content, mode=0o700)
        return str(wrapper), {
            "ISSUE_AGENT_SUPERVISOR_STATE_DIR": str(self.state_dir),
            "ISSUE_AGENT_SUPERVISOR_RUN_ID": str(run_id),
            "ISSUE_AGENT_SUPERVISOR_LAUNCH_TOKEN": token,
            "ISSUE_AGENT_SUPERVISED_TARGET": json.dumps(list(command)),
            **self.launch_guard_env(),
        }

    def leg_tmp(self, run_id: RunId) -> str:
        """Where the run's sandbox puts the leg's disk-backed /tmp (#470). It
        goes with the run's processes (`kill_tree`), or with recovery after a
        crash. It is filed under this daemon's identity, because the sandbox
        makes it before any launch intent exists: a recovery in another daemon
        tells a crash's leftover from a live launch by that identity alone,
        so a leg whose launcher has none is refused (a host without /proc)."""
        me = self.process_identity(os.getpid())
        if me is None:
            raise SandboxRefused("the daemon's process identity is unreadable (no /proc), so a leg's temp "
                                 "directory could not be told from a crash's leftover")
        return str(self.tmp_directory / f"{me.pid}-{me.start_ticks}" / self._safe_name(run_id))

    def prepare_launch(self, run_id: RunId) -> str:
        """Persist a generation-token intent before a provider can be spawned."""

        identity = self.process_identity(os.getpid())
        if identity is None:
            raise ProcessLookupError("launcher process identity is unavailable")
        token = uuid.uuid4().hex
        with self._intent_lock():
            registration_path = self._path(run_id)
            if registration_path.exists():
                raise RuntimeError(
                    f"run {run_id} still has a process registration"
                )
            self._write_intent(
                LaunchIntent(run_id, token, "pending", identity)
            )
        return token

    def register_prepared_launch(
        self,
        run_id: RunId,
        token: str,
        pgid: int,
        *,
        leader_pid: int | None = None,
    ) -> ProcessRegistration:
        """Register only if cancellation has not tombstoned this generation."""

        if not token:
            raise ValueError("supervised launch token is required")
        with self._intent_lock():
            intent = self._read_intent(self._intent_path(run_id))
            if (
                intent is None
                or intent.token != token
                or intent.state != "pending"
            ):
                raise RuntimeError(
                    "supervised launch was cancelled or superseded"
                )
            registration = self.register(
                run_id, pgid, leader_pid=leader_pid
            )
            self._write_intent(
                LaunchIntent(run_id, token, "registered", intent.launcher)
            )
            return registration

    def require_registered_launch(self, run_id: RunId, token: str) -> None:
        """Final pre-exec check; later cancellation sees the registration."""

        with self._intent_lock():
            intent = self._read_intent(self._intent_path(run_id))
            if (
                intent is None
                or intent.token != token
                or intent.state != "registered"
                or self._read(self._path(run_id)) is None
            ):
                raise RuntimeError(
                    "supervised launch is no longer authorized"
                )

    def register(
        self, run_id: RunId, pgid: int, *, leader_pid: int | None = None
    ) -> ProcessRegistration:
        if not str(run_id) or pgid <= 0:
            raise ValueError("run id and positive pgid are required")
        leader = self._identity_for_pid(pgid if leader_pid is None else leader_pid)
        if leader is None:
            raise ProcessLookupError("process-group leader identity is unavailable")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_write(self._path(run_id), json.dumps({
            "run_id": str(run_id), "pgid": pgid,
            "leader": {"pid": leader.pid, "start_ticks": leader.start_ticks},
        }))
        return ProcessRegistration(run_id, pgid, leader)

    def unregister(self, run_id: RunId) -> None:
        try:
            self._path(run_id).unlink()
        except FileNotFoundError:
            pass

    def kill_tree(
        self,
        run_id: RunId,
        grace_seconds: float | None = None,
        *,
        reason: str = "leg_end_cleanup",
    ) -> None:
        with self._intent_lock():
            intent_path = self._intent_path(run_id)
            intent = self._read_intent(intent_path)
            if intent is None:
                identity = self.process_identity(os.getpid())
                if identity is None:
                    raise ProcessLookupError(
                        "cancelling process identity is unavailable"
                    )
                intent = LaunchIntent(
                    run_id,
                    f"cancelled-{uuid.uuid4().hex}",
                    "cancelled",
                    identity,
                )
            else:
                intent = LaunchIntent(
                    run_id,
                    intent.token,
                    "cancelled",
                    intent.launcher,
                )
            registration = self._read(self._path(run_id))
            unwritten: OSError | None = None
            try:
                self._write_intent(intent)
            except OSError as exc:
                # Without the tombstone a pending launch could still register,
                # so nothing is torn down. A registered group dies anyway, with
                # the wrapper that would check it: a full disk must not keep the
                # group alive and its temp directory filling that disk.
                if registration is None:
                    raise
                unwritten = exc
        if registration is None:
            # The cancelled intent stops any registration, so nothing runs.
            self._remove_tmp(run_id)
            return
        grace = self.grace_seconds if grace_seconds is None else grace_seconds
        self._terminate(registration, grace, reason, run_id)
        with self._intent_lock():
            current = self._read(self._path(run_id))
            if current == registration:
                self.unregister(run_id)
            current_intent = self._read_intent(self._intent_path(run_id))
            if (
                current_intent is not None
                and current_intent.token == intent.token
                and current_intent.state == "cancelled"
            ):
                self._remove_intent(run_id)
        self._remove_tmp(run_id)
        if unwritten is not None:
            raise unwritten

    def recover_orphans(self) -> tuple[RunId, ...]:
        # Leftovers go first: they may be what filled the disk the reaping writes to.
        self._remove_leftover_tmp()
        self._discard_dead_launcher_intents()
        recovered = self._reap_registered(self.grace_seconds)
        self._remove_leftover_tmp()
        return recovered

    def recover_abandoned_orphans(self) -> tuple[RunId, ...]:
        """Reap a registry another daemon may own: only groups whose launcher
        is dead.

        A receipt with a launch intent is decided by its launcher's identity
        alone: once that daemon is dead its group is an orphan, whatever
        subreaper adopted it. A legacy receipt without one falls back to its
        leader's parent: a live external parent keeps it.
        """

        table = self._process_table()
        recovered: list[RunId] = []
        for path in (
            sorted(self.directory.glob("*.json"))
            if self.directory.exists()
            else ()
        ):
            registration = self._read(path)
            if registration is None:
                path.rename(path.with_suffix(".invalid"))
                continue
            intent = self._read_intent(
                self._intent_path(registration.run_id)
            )
            if intent is not None:
                if self.matches_identity(intent.launcher.pid, intent.launcher.start_ticks):
                    continue
            elif (
                (leader := table.get(registration.leader.pid)) is not None
                and leader[0] == registration.leader
                and leader[1] > 1
                and leader[1] in table
            ):
                continue
            self.kill_tree(registration.run_id, self.grace_seconds, reason="orphan_reclaim")
            recovered.append(registration.run_id)

        self._discard_dead_launcher_intents()
        self._remove_leftover_tmp()
        return tuple(recovered)

    def force_kill_all(self) -> tuple[RunId, ...]:
        """Reap every owned process immediately at the application deadline."""

        self._cancel_all_intents()
        return self._reap_registered(0.0)

    def _reap_registered(self, grace_seconds: float) -> tuple[RunId, ...]:
        recovered: list[RunId] = []
        for path in (
            sorted(self.directory.glob("*.json"))
            if self.directory.exists()
            else ()
        ):
            registration = self._read(path)
            if registration is None:
                # A malformed ownership receipt cannot safely identify a target.
                # Quarantine it instead of guessing a process id.
                path.rename(path.with_suffix(".invalid"))
                continue
            self.kill_tree(
                registration.run_id,
                grace_seconds,
                reason="drain_deadline" if grace_seconds == 0.0 else "orphan_reclaim",
            )
            recovered.append(registration.run_id)
        return tuple(recovered)

    @staticmethod
    def _safe_name(run_id: RunId) -> str:
        safe = str(run_id)
        if not safe or safe in {".", ".."} or "/" in safe or "\0" in safe:
            raise ValueError("run id is not safe for a registry filename")
        return safe

    def _path(self, run_id: RunId) -> Path:
        return self.directory / f"{self._safe_name(run_id)}.json"

    def _launcher_directories(self) -> list[Path]:
        if not self.tmp_directory.is_dir():
            return []
        return [p for p in self.tmp_directory.iterdir() if p.is_dir() and not p.is_symlink()]

    def _remove_tmp(self, run_id: RunId) -> None:
        # A file a dying sandbox process still writes may survive until a recovery's sweep.
        name = self._safe_name(run_id)
        for launcher in self._launcher_directories():
            _remove_tree(launcher / name)

    def _remove_leftover_tmp(self) -> None:
        """Remove temp directories no process can use: their launching daemon
        is dead and their run holds no registration (a registered group is
        reaped by `kill_tree`, which removes its directory)."""
        for launcher in self._launcher_directories():
            pid, _, ticks = launcher.name.partition("-")
            if pid.isdigit() and ticks.isdigit() and self.matches_identity(int(pid), int(ticks)):
                continue
            try:
                runs = list(launcher.iterdir())
            except OSError:  # a sibling's recovery swept it first
                continue
            for run in runs:
                if not (self.directory / f"{run.name}.json").exists():
                    _remove_tree(run)
            with contextlib.suppress(OSError):
                launcher.rmdir()  # once empty

    def _intent_path(self, run_id: RunId) -> Path:
        safe = str(run_id)
        if not safe or safe in {".", ".."} or "/" in safe or "\0" in safe:
            raise ValueError("run id is not safe for an intent filename")
        return self.intent_directory / f"{safe}.json"

    @contextmanager
    def _intent_lock(self) -> Iterator[None]:
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.state_dir / ".launch-intents.lock"
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _write_intent(self, intent: LaunchIntent) -> None:
        self.intent_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_write(self._intent_path(intent.run_id), json.dumps({
            "run_id": str(intent.run_id), "token": intent.token, "state": intent.state,
            "launcher": {"pid": intent.launcher.pid, "start_ticks": intent.launcher.start_ticks},
        }))

    @staticmethod
    def _read_intent(path: Path) -> LaunchIntent | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            intent = LaunchIntent(
                RunId(str(raw["run_id"])),
                str(raw["token"]),
                str(raw["state"]),
                ProcessIdentity(
                    int(raw["launcher"]["pid"]),
                    int(raw["launcher"]["start_ticks"]),
                ),
            )
            if (
                not str(intent.run_id)
                or not intent.token
                or intent.state not in {"pending", "registered", "cancelled"}
                or intent.launcher.pid <= 0
                or intent.launcher.start_ticks <= 0
            ):
                return None
            return intent
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None

    def _remove_intent(self, run_id: RunId) -> None:
        try:
            self._intent_path(run_id).unlink()
        except FileNotFoundError:
            pass

    def _cancel_all_intents(self) -> None:
        with self._intent_lock():
            paths = (
                sorted(self.intent_directory.glob("*.json"))
                if self.intent_directory.exists()
                else ()
            )
            for path in paths:
                intent = self._read_intent(path)
                if intent is None:
                    path.rename(path.with_suffix(".invalid"))
                    continue
                self._write_intent(
                    LaunchIntent(
                        intent.run_id,
                        intent.token,
                        "cancelled",
                        intent.launcher,
                    )
                )

    def _discard_dead_launcher_intents(self) -> None:
        with self._intent_lock():
            paths = (
                sorted(self.intent_directory.glob("*.json"))
                if self.intent_directory.exists()
                else ()
            )
            for path in paths:
                intent = self._read_intent(path)
                if intent is None:
                    path.rename(path.with_suffix(".invalid"))
                    continue
                if not self.matches_identity(
                    intent.launcher.pid, intent.launcher.start_ticks
                ):
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass

    @staticmethod
    def _read(path: Path) -> ProcessRegistration | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            run_id = RunId(str(raw["run_id"]))
            pgid = int(raw["pgid"])
            leader_raw = raw["leader"]
            leader = ProcessIdentity(
                int(leader_raw["pid"]), int(leader_raw["start_ticks"])
            )
            if (
                not str(run_id)
                or pgid <= 0
                or leader.pid <= 0
                or leader.start_ticks <= 0
            ):
                return None
            return ProcessRegistration(run_id, pgid, leader)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None

    @staticmethod
    def _proc_entry(path: Path) -> tuple[ProcessIdentity, int, int] | None:
        try:
            raw = path.read_text(encoding="utf-8")
            pid_text, separator, tail = raw.partition("(")
            if not separator:
                return None
            _command, separator, fields_text = tail.rpartition(")")
            if not separator:
                return None
            fields = fields_text.split()
            if fields[0] in {"Z", "X"}:
                return None
            return ProcessIdentity(int(pid_text), int(fields[19])), int(fields[1]), int(fields[2])
        except (OSError, ValueError, IndexError, UnicodeError):
            return None

    @classmethod
    def _process_table(cls) -> dict[int, tuple[ProcessIdentity, int, int]]:
        """Return live process identity, parent pid, and process-group id."""

        table: dict[int, tuple[ProcessIdentity, int, int]] = {}
        proc = Path("/proc")
        if proc.is_dir():
            for path in proc.iterdir():
                if not path.name.isdigit():
                    continue
                entry = cls._proc_entry(path / "stat")
                if entry is not None:
                    table[entry[0].pid] = entry
            return table
        try:
            completed = subprocess.run(
                ["ps", "-axo", "pid=,ppid=,pgid=,state=,lstart="],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.SubprocessError):
            return table
        for line in completed.stdout.splitlines():
            try:
                pid_text, ppid_text, pgid_text, state, started = line.split(
                    maxsplit=4
                )
                if state.startswith(("Z", "X")):
                    continue
                pid = int(pid_text)
                ppid = int(ppid_text)
                pgid = int(pgid_text)
                start_ticks = int.from_bytes(
                    sha256(started.encode()).digest()[:8], "big"
                )
                table[pid] = (ProcessIdentity(pid, start_ticks), ppid, pgid)
            except ValueError:
                continue
        return table

    @classmethod
    def _identity_for_pid(cls, pid: int) -> ProcessIdentity | None:
        if type(pid) is not int or pid <= 0:
            return None
        if Path("/proc").is_dir():
            current = cls._proc_entry(Path(f"/proc/{pid}/stat"))
            return current[0] if current is not None and current[0].pid == pid else None
        current = cls._process_table().get(pid)
        return current[0] if current is not None else None

    @classmethod
    def _same_process(cls, identity: ProcessIdentity) -> bool:
        return cls._identity_for_pid(identity.pid) == identity

    @classmethod
    def _group_identities(cls, pgid: int) -> tuple[ProcessIdentity, ...]:
        return tuple(
            identity
            for identity, _ppid, member_pgid in cls._process_table().values()
            if member_pgid == pgid
        )

    @classmethod
    def live_group_identities(cls, pgid: int) -> tuple[ProcessIdentity, ...]:
        """Return reuse-safe identities for every live member of a group."""

        return cls._group_identities(pgid)

    @classmethod
    def process_identity(cls, pid: int) -> ProcessIdentity | None:
        """Return the reuse-safe identity for a currently live process."""

        return cls._identity_for_pid(pid)

    @classmethod
    def matches_identity(cls, pid: int, start_ticks: int) -> bool:
        if pid <= 0 or start_ticks <= 0:
            return False
        return cls._same_process(ProcessIdentity(pid, start_ticks))

    @classmethod
    def _terminate_identities(
        cls,
        identities: tuple[ProcessIdentity, ...],
        grace_seconds: float,
        reason: str,
        run_id: RunId | None,
    ) -> None:
        live = tuple(identity for identity in identities if cls._same_process(identity))
        for identity in reversed(live):
            try:
                send_signal(signal.SIGTERM, pid=identity.pid, reason=reason, run_id=run_id)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + max(0.0, grace_seconds)
        while (
            any(cls._same_process(identity) for identity in live)
            and time.monotonic() < deadline
        ):
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        for identity in reversed(live):
            if not cls._same_process(identity):
                continue
            try:
                send_signal(signal.SIGKILL, pid=identity.pid, reason=f"{reason}:grace_expired", run_id=run_id)
            except ProcessLookupError:
                pass

    @classmethod
    def _terminate(
        cls,
        registration: ProcessRegistration,
        grace_seconds: float,
        reason: str,
        run_id: RunId | None,
    ) -> None:
        current_leader = cls._identity_for_pid(registration.leader.pid)
        if current_leader is not None and current_leader != registration.leader:
            # The numeric pid was reused. The receipt no longer proves ownership
            # of any process group, so signalling would be unsafe.
            return
        if current_leader is None:
            members = cls._group_identities(registration.pgid)
            cls._terminate_identities(members, grace_seconds, reason, run_id)
            return
        pgid = registration.pgid
        owned = {
            identity.pid: identity
            for identity in cls._group_identities(pgid)
        }
        try:
            send_signal(signal.SIGTERM, pgid=pgid, reason=reason, run_id=run_id)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + max(0.0, grace_seconds)
        while time.monotonic() < deadline:
            live_owned = tuple(
                identity
                for identity in owned.values()
                if cls._same_process(identity)
            )
            if not live_owned:
                return
            owned.update(
                (identity.pid, identity)
                for identity in cls._group_identities(pgid)
            )
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        cls._terminate_identities(tuple(owned.values()), 0.0, reason, run_id)


def _remove_tree(path: Path) -> None:
    """Remove a leg's temp directory, never following a link the leg planted.
    A leg can leave a directory it made read-only (a permission test, an
    extracted archive) that rmtree alone cannot empty, so every directory is
    made the owner's first. Called once the run's processes are gone: the
    check before each chmod is not atomic against a live writer."""
    if not path.is_dir() or path.is_symlink():
        return
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)
        for _root, dirs, _files, fd in os.fwalk(path):
            for name in dirs:
                with contextlib.suppress(OSError):
                    if stat.S_ISDIR(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                        os.chmod(name, 0o700, dir_fd=fd)
    shutil.rmtree(path, ignore_errors=True)
    if os.path.lexists(path):
        # Both walks hold a descriptor per level: a tree deeper than the open
        # file limit (raised to the hard limit at daemon start) stays.
        sys.stderr.write(f"[process-supervisor] could not remove leg temp directory {path}\n")
