"""Durable ownership and teardown of child process groups."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from ..domain.contracts import RunId


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
        self.tree_directory = self.state_dir / "process-trees"
        self.intent_directory = self.state_dir / "launch-intents"
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
        self, run_id: RunId, executable: str
    ) -> tuple[str, dict[str, str]]:
        """Return an SDK-compatible CLI path that registers before exec."""

        token = self.prepare_launch(run_id)
        target = os.path.realpath(executable)
        if not os.path.isfile(target) or not os.access(target, os.X_OK):
            raise ValueError(f"supervised CLI is not executable: {target}")
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        wrapper = self.state_dir / "supervised-cli"
        content = (
            f"#!{sys.executable}\n"
            "from issue_agent.adapters.supervised_exec import supervised_cli_main\n"
            "supervised_cli_main()\n"
        )
        if not wrapper.exists() or wrapper.read_text(encoding="utf-8") != content:
            fd, temporary = tempfile.mkstemp(
                dir=self.state_dir, prefix=".supervised-cli.", text=True
            )
            try:
                os.fchmod(fd, 0o700)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, wrapper)
            except BaseException:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
                raise
        return str(wrapper), {
            "ISSUE_AGENT_SUPERVISOR_STATE_DIR": str(self.state_dir),
            "ISSUE_AGENT_SUPERVISOR_RUN_ID": str(run_id),
            "ISSUE_AGENT_SUPERVISOR_LAUNCH_TOKEN": token,
            "ISSUE_AGENT_SUPERVISED_TARGET": target,
            **self.launch_guard_env(),
        }

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
        target = self._path(run_id)
        fd, temporary = tempfile.mkstemp(
            dir=self.directory, prefix=f".{target.name}.", text=True
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(
                    {
                        "run_id": str(run_id),
                        "pgid": pgid,
                        "leader": {
                            "pid": leader.pid,
                            "start_ticks": leader.start_ticks,
                        },
                    },
                    stream,
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            return ProcessRegistration(run_id, pgid, leader)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def unregister(self, run_id: RunId) -> None:
        try:
            self._path(run_id).unlink()
        except FileNotFoundError:
            pass

    def kill_tree(self, run_id: RunId, grace_seconds: float | None = None) -> None:
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
            self._write_intent(intent)
            registration = self._read(self._path(run_id))
        if registration is None:
            return
        grace = self.grace_seconds if grace_seconds is None else grace_seconds
        self._terminate(registration, grace)
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

    def recover_orphans(self) -> tuple[RunId, ...]:
        self._discard_dead_launcher_intents()
        return self._reap_registered(self.grace_seconds)

    def recover_abandoned_orphans(self) -> tuple[RunId, ...]:
        """Safely drain a pre-deployment-scope registry during upgrade.

        A legacy state root can be shared by sibling checkouts. A live launch
        owner or live external parent therefore makes a receipt authoritative
        for that sibling, not an orphan this process may terminate.
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
            if (
                intent is not None
                and self.matches_identity(
                    intent.launcher.pid,
                    intent.launcher.start_ticks,
                )
            ):
                continue
            leader = table.get(registration.leader.pid)
            if (
                leader is not None
                and leader[0] == registration.leader
                and leader[1] > 1
                and leader[1] in table
            ):
                continue
            self.kill_tree(registration.run_id, self.grace_seconds)
            recovered.append(registration.run_id)

        for path in (
            sorted(self.tree_directory.glob("*.json"))
            if self.tree_directory.exists()
            else ()
        ):
            run_id, identities = self._read_tree(path)
            if run_id is None:
                path.rename(path.with_suffix(".invalid"))
                continue
            live_pids = {
                identity.pid
                for identity in identities
                if (
                    (entry := table.get(identity.pid)) is not None
                    and entry[0] == identity
                )
            }
            externally_owned = any(
                entry[1] > 1
                and entry[1] not in live_pids
                and entry[1] in table
                for identity in identities
                if (
                    (entry := table.get(identity.pid)) is not None
                    and entry[0] == identity
                )
            )
            if externally_owned:
                continue
            self._terminate_identities(identities, self.grace_seconds)
            recovered.append(run_id)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        self._discard_dead_launcher_intents()
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
            self.kill_tree(registration.run_id, grace_seconds)
            recovered.append(registration.run_id)
        for path in (
            sorted(self.tree_directory.glob("*.json"))
            if self.tree_directory.exists()
            else ()
        ):
            run_id, identities = self._read_tree(path)
            if run_id is None:
                path.rename(path.with_suffix(".invalid"))
                continue
            self._terminate_identities(identities, grace_seconds)
            recovered.append(run_id)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        return tuple(recovered)

    def direct_children(self, pid: int | None = None) -> tuple[ProcessIdentity, ...]:
        parent = os.getpid() if pid is None else pid
        table = self._process_table()
        return tuple(
            identity
            for identity, ppid, _pgid in table.values()
            if ppid == parent
        )

    def register_process_tree(
        self, run_id: RunId, roots: tuple[ProcessIdentity, ...]
    ) -> None:
        """Persist roots and all currently visible descendants atomically."""

        if not roots:
            return
        table = self._process_table()
        identities = {identity.pid: identity for identity in roots}
        frontier = set(identities)
        while frontier:
            parents = frontier
            frontier = {
                identity.pid
                for identity, ppid, _pgid in table.values()
                if ppid in parents and identity.pid not in identities
            }
            identities.update(
                {
                    pid: table[pid][0]
                    for pid in frontier
                    if pid in table
                }
            )
        path = self._tree_path(run_id)
        prior_run, prior = self._read_tree(path)
        if prior_run == run_id:
            identities.update({identity.pid: identity for identity in prior})
        self.tree_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            dir=self.tree_directory, prefix=f".{path.name}.", text=True
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(
                    {
                        "run_id": str(run_id),
                        "processes": [
                            {"pid": identity.pid, "start_ticks": identity.start_ticks}
                            for identity in sorted(
                                identities.values(), key=lambda item: item.pid
                            )
                        ],
                    },
                    stream,
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def kill_process_tree(
        self, run_id: RunId, grace_seconds: float | None = None
    ) -> None:
        path = self._tree_path(run_id)
        stored_run, identities = self._read_tree(path)
        if stored_run is None:
            return
        grace = self.grace_seconds if grace_seconds is None else grace_seconds
        self._terminate_identities(identities, grace)
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def registrations(self) -> tuple[ProcessRegistration, ...]:
        if not self.directory.exists():
            return ()
        return tuple(
            registration
            for path in sorted(self.directory.glob("*.json"))
            if (registration := self._read(path)) is not None
        )

    def _path(self, run_id: RunId) -> Path:
        safe = str(run_id)
        if not safe or safe in {".", ".."} or "/" in safe or "\0" in safe:
            raise ValueError("run id is not safe for a registry filename")
        return self.directory / f"{safe}.json"

    def _tree_path(self, run_id: RunId) -> Path:
        safe = str(run_id)
        if not safe or safe in {".", ".."} or "/" in safe or "\0" in safe:
            raise ValueError("run id is not safe for a registry filename")
        return self.tree_directory / f"{safe}.json"

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
        target = self._intent_path(intent.run_id)
        fd, temporary = tempfile.mkstemp(
            dir=self.intent_directory, prefix=f".{target.name}.", text=True
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(
                    {
                        "run_id": str(intent.run_id),
                        "token": intent.token,
                        "state": intent.state,
                        "launcher": {
                            "pid": intent.launcher.pid,
                            "start_ticks": intent.launcher.start_ticks,
                        },
                    },
                    stream,
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

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
    def _read_tree(
        path: Path,
    ) -> tuple[RunId | None, tuple[ProcessIdentity, ...]]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            run_id = RunId(str(raw["run_id"]))
            identities = tuple(
                ProcessIdentity(int(item["pid"]), int(item["start_ticks"]))
                for item in raw["processes"]
            )
            if not str(run_id) or any(
                item.pid <= 0 or item.start_ticks <= 0 for item in identities
            ):
                raise ValueError("invalid process identity")
            return run_id, identities
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None, ()

    @staticmethod
    def _process_table() -> dict[int, tuple[ProcessIdentity, int, int]]:
        """Return live process identity, parent pid, and process-group id."""

        table: dict[int, tuple[ProcessIdentity, int, int]] = {}
        proc = Path("/proc")
        if proc.is_dir():
            for path in proc.iterdir():
                if not path.name.isdigit():
                    continue
                try:
                    raw = (path / "stat").read_text(encoding="utf-8")
                    pid_text, separator, tail = raw.partition("(")
                    if not separator:
                        continue
                    _command, separator, fields_text = tail.rpartition(")")
                    if not separator:
                        continue
                    fields = fields_text.split()
                    if fields[0] in {"Z", "X"}:
                        continue
                    pid = int(pid_text)
                    ppid = int(fields[1])
                    pgid = int(fields[2])
                    start_ticks = int(fields[19])
                    table[pid] = (
                        ProcessIdentity(pid, start_ticks),
                        ppid,
                        pgid,
                    )
                except (OSError, ValueError, IndexError):
                    continue
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
        current = cls._process_table().get(pid)
        return current[0] if current is not None else None

    @classmethod
    def _same_process(cls, identity: ProcessIdentity) -> bool:
        current = cls._process_table().get(identity.pid)
        return current is not None and current[0].start_ticks == identity.start_ticks

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
        cls, identities: tuple[ProcessIdentity, ...], grace_seconds: float
    ) -> None:
        live = tuple(identity for identity in identities if cls._same_process(identity))
        for identity in reversed(live):
            try:
                os.kill(identity.pid, signal.SIGTERM)
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
                os.kill(identity.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    @staticmethod
    def _alive(pgid: int) -> bool:
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    @classmethod
    def _terminate(
        cls, registration: ProcessRegistration, grace_seconds: float
    ) -> None:
        current_leader = cls._identity_for_pid(registration.leader.pid)
        if current_leader is not None and current_leader != registration.leader:
            # The numeric pid was reused. The receipt no longer proves ownership
            # of any process group, so signalling would be unsafe.
            return
        if current_leader is None:
            members = cls._group_identities(registration.pgid)
            cls._terminate_identities(members, grace_seconds)
            return
        pgid = registration.pgid
        owned = {
            identity.pid: identity
            for identity in cls._group_identities(pgid)
        }
        try:
            os.killpg(pgid, signal.SIGTERM)
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
        cls._terminate_identities(tuple(owned.values()), 0.0)
