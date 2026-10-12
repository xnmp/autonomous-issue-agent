"""Shared acceptance-harness support: process-loss faults, one persisted
clock for every rebuilt process, an isolated control authority, a scripted
paid provider, and real process death at a chosen boundary."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.domain.outcome import AgentResult


class Crash(BaseException):
    """Process loss inside one thread: bypasses application exception handlers."""


def fail_at(point: str, exc: BaseException | None = None, *, times: int = 1):
    """Raise at the named PersistentGitHub fault point ``times`` times."""
    remaining = [times]

    def fault(observed: str) -> None:
        if observed == point and remaining[0] > 0:
            remaining[0] -= 1
            raise (exc if exc is not None else Crash(point))
    return fault


class Clock:
    """Wall clock plus a persisted offset shared by every rebuilt process."""

    def __init__(self, path: Path):
        self.path = path

    def offset(self) -> timedelta:
        try:
            return timedelta(seconds=float(self.path.read_text()))
        except FileNotFoundError:
            return timedelta(0)

    def now(self) -> datetime:
        return datetime.now(timezone.utc) + self.offset()

    def advance(self, delta: timedelta) -> None:
        self.path.write_text(str((self.offset() + delta).total_seconds()))


_REAL_DATETIME = datetime
_CLOCK_FILE: list[Path] = []


class _ShiftedMeta(type):
    def __instancecheck__(cls, instance) -> bool:
        return isinstance(instance, _REAL_DATETIME)

    def __subclasscheck__(cls, subclass) -> bool:
        return issubclass(subclass, _REAL_DATETIME)


class _ShiftedDateTime(_REAL_DATETIME, metaclass=_ShiftedMeta):
    """`datetime` whose `now()` follows the deployment clock file.

    Production composes one wall clock; the harness must too, otherwise the
    accounting runtime, fix workflow and control store disagree about order.
    """

    @classmethod
    def now(cls, tz=None):
        offset = Clock(_CLOCK_FILE[0]).offset() if _CLOCK_FILE else timedelta(0)
        return _REAL_DATETIME.now(tz) + offset


_CLOCKED_MODULES = (
    "issue_agent.adapters.accounting_runtime", "issue_agent.adapters.control_store",
    "issue_agent.adapters.durable_effects", "issue_agent.adapters.journal",
    "issue_agent.app.loop", "issue_agent.app.recovery",
    "issue_agent.app.roles", "issue_agent.app.fix", "issue_agent.app.dashweb.service",
)


def install_clock(deployment, monkeypatch=None) -> None:
    """Route every ambient `datetime.now()` on the daemon path through one clock."""
    import importlib

    _CLOCK_FILE[:] = [deployment.clock.path]
    for name in _CLOCKED_MODULES:
        module = importlib.import_module(name)
        if monkeypatch is None:
            setattr(module, "datetime", _ShiftedDateTime)
        else:
            monkeypatch.setattr(module, "datetime", _ShiftedDateTime)


def isolate_authority(deployment, monkeypatch=None) -> None:
    """Point the machine control authority at the deployment; refuse any other store.

    ``from_env`` always derives the machine authority root, which is the real
    per-user control database outside tests.
    """
    from issue_agent.app import config as config_module

    root = str(deployment.authority_root)
    original = SQLiteControlStore.__init__

    def isolated(self, path, *args, **kwargs):
        if not Path(path).resolve().is_relative_to(deployment.root.resolve()):
            raise AssertionError(f"acceptance opened a non-isolated control store: {path}")
        original(self, path, *args, **kwargs)

    patches = ((config_module, "machine_control_authority_root", lambda: root),
               (SQLiteControlStore, "__init__", isolated))
    for owner, name, value in patches:
        if monkeypatch is None:
            setattr(owner, name, value)
        else:
            monkeypatch.setattr(owner, name, value)


class ScriptedProvider:
    """Paid provider double: records every launched task, then returns results."""

    def __init__(self, results=(), *, during: Callable[[object], None] = lambda _task: None):
        self.results = list(results)
        self.during = during
        self.launch: Callable[[object], None] | None = None
        self.tasks: list = []

    async def run(self, task):
        self.tasks.append(task)
        if self.launch is not None:
            self.launch(task)
        self.during(task)
        if self.results:
            return self.results.pop(0)
        return AgentResult(is_error=True, cost_usd=0.25, session_id=f"attempt-{len(self.tasks)}")


def run_until_exit(target, *args, code: int, timeout: float = 90) -> None:
    """Real process death at a chosen boundary (spawn: no inherited state)."""
    import multiprocessing

    process = multiprocessing.get_context("spawn").Process(target=target, args=args)
    process.start()
    try:
        process.join(timeout=timeout)
        assert process.exitcode == code, f"child exited {process.exitcode}, expected {code}"
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
