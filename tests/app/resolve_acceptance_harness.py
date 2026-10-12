"""Composed resolve re-arm acceptance harness (ADR 0015 fault matrix).

The fix daemon is ``roles.compose_fix_daemon`` -- the function behind
``build_fix_daemon`` -- so the command drain, boot/wake recovery (with the
outbox flush), the re-arm reconciliation binding, the daily spend gate,
dispatch claims, path reservations, ``WorkspaceAdmission`` and worktree
removal are production wiring, not a replica. Operator commands and the held
projection go through the dashboard ``Backend`` over the production
``open_resolve_operator``, which loads the deployment's launch snapshot
(``config/.active.demo.env``) and opens the canonical control authority under
an isolated, monkeypatched machine authority root.

Simulated: the paid provider, GitHub transport (``PersistentGitHub``, SQLite,
survives restarts), the Git remote URL mapping and the ``gh``/contract-capture
binaries. Unavoidable simplifications versus a live deployment:

- each harness tick joins the daemon's dispatch pool before returning, so a
  paid leg never overlaps the next tick;
- every ambient ``datetime.now()`` on the path follows one persisted clock
  file, so restarts can advance or skew time;
- deployment discovery (``Backend.names``) is pinned to the one deployment
  instead of listing the CLI's global ``config/repos``;
- the FastAPI route guard is not in the loop (``make test`` has no fastapi);
  test_resolve_operator_api covers it.

Every restart rebuilds every adapter over the same databases, Git checkout,
state directories and clock file. These are controlled replays, not live
deployment evidence.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from functools import partial
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess

from issue_agent.adapters import accounting_runtime
from issue_agent.adapters.control_path import canonical_control_store_path
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.store import Store
from issue_agent.adapters.workflow_control import SQLiteWorkflowControl
from issue_agent.adapters.worktree import Worktrees
from issue_agent.app import roles
from issue_agent.app.config import HarnessConfig, from_env, owner_identity_for_target
from issue_agent.app.loop import Daemon
from issue_agent.app.resolve_control import open_resolve_operator
from issue_agent.app.workflows.resolve_rearm import ResolveRearmWorkflow
from issue_agent.domain.labels import Label
from issue_agent.domain.outcome import AgentResult
from issue_agent.domain.recovery import RecoveryTrigger
from issue_agent.domain.workflow import ResolveEpisode, WorkflowSnapshot
from issue_agent.domain.workflow_policy import reconcile_resolve_ceiling
from issue_agent.domain.world import IssueView
from tests.adapters.test_episode_workspace import REMOTE_URL, MappedRemoteGit
from tests.fakes.persistent_github import PersistentGitHub
from tests.fakes.workflow_authority import install_workflow, resolve_snapshot, with_issue_recovery

ISSUE = 1
REPO = "o/r"
DEPLOYMENT = "demo"
TITLE = "Rearm safely"
BODY = "Original requirements"


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
    "issue_agent.adapters.event_log", "issue_agent.app.loop", "issue_agent.app.recovery",
    "issue_agent.app.roles", "issue_agent.app.workflows.fix", "issue_agent.app.resolve_control",
    "issue_agent.app.dashweb.service",
)


def install_clock(deployment: "Deployment", monkeypatch=None) -> None:
    """Route every ambient `datetime.now()` on the resolve path through one clock."""
    import importlib

    _CLOCK_FILE[:] = [deployment.clock.path]
    for name in _CLOCKED_MODULES:
        module = importlib.import_module(name)
        if monkeypatch is None:
            setattr(module, "datetime", _ShiftedDateTime)
        else:
            monkeypatch.setattr(module, "datetime", _ShiftedDateTime)


@dataclass(frozen=True)
class Deployment:
    """Everything persistent: databases, Git repositories, state dirs, clock."""

    root: Path
    repo_root: Path
    origin: Path
    upstream: Path

    @property
    def clock(self) -> Clock:
        return Clock(self.root / "clock-offset")

    @property
    def github_path(self) -> Path:
        return self.root / "github.sqlite"

    @property
    def authority_root(self) -> Path:
        return self.root / "authority"

    @property
    def control_path(self) -> str:
        return canonical_control_store_path(authority_root=str(self.authority_root))

    def env(self) -> dict[str, str]:
        return {
            "REPO": REPO,
            "FIX_BOT": "fix-bot",
            # A pinned model skips the paid difficulty router, so provider
            # task counts are resolve legs only.
            "FIX_MODEL": "test-model",
            "TARGET_DIR": str(self.repo_root),
            "STATE_DIR": str(self.root / "fix-state"),
            "REVIEW_STATE_DIR": str(self.root / "review-state"),
        }

    def config(self) -> HarnessConfig:
        return from_env(self.env())

    def owner(self) -> str:
        cfg = self.config()
        return owner_identity_for_target(cfg.repo, cfg.target_dir, "fix")

    def worktrees(self) -> Worktrees:
        return Worktrees(str(self.repo_root), run=MappedRemoteGit(self.origin))

    def github(self, fault: Callable[[str], None] = lambda _: None) -> PersistentGitHub:
        return PersistentGitHub(self.github_path, fault=fault)

    def write_snapshot(self) -> None:
        """The launch snapshot the operator transport loads (``issue-agent start``)."""
        config = self.root / "config"
        config.mkdir(exist_ok=True)
        lines = (f"{key}={shlex.quote(value)}" for key, value in self.env().items())
        (config / f".active.{DEPLOYMENT}.env").write_text("\n".join(lines) + "\n")

    def control(self) -> SQLiteControlStore:
        store = SQLiteControlStore(self.control_path, now=self.clock.now)
        store.initialize()
        store.bind_active_owner(self.owner())
        return store


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def create_deployment(root: Path) -> Deployment:
    """Isolated origin + checkout whose `origin` URL is the o/r GitHub URL."""
    origin = root / "origin.git"
    repo = root / "repo"
    upstream = root / "upstream"
    _git(root, "init", "--bare", str(origin))
    repo.mkdir()
    _git(repo, "init", "-b", "dev")
    _git(repo, "config", "user.email", "acceptance@example.invalid")
    _git(repo, "config", "user.name", "Acceptance")
    (repo / "baseline.txt").write_text("base one\n")
    (repo / ".gitignore").write_text(".claude/\n")
    _git(repo, "add", "baseline.txt", ".gitignore")
    _git(repo, "commit", "-m", "base one")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-u", "origin", "dev")
    _git(repo, "remote", "set-url", "origin", REMOTE_URL)
    _git(root, "clone", str(origin), str(upstream))
    _git(upstream, "checkout", "dev")
    _git(upstream, "config", "user.email", "upstream@example.invalid")
    _git(upstream, "config", "user.name", "Upstream")
    deployment = Deployment(root, repo, origin, upstream)
    deployment.write_snapshot()
    return deployment


def isolate_authority(deployment: Deployment, monkeypatch=None) -> None:
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


def exhausted_snapshot(ceiling: int, number: int = ISSUE) -> WorkflowSnapshot:
    state = replace(with_issue_recovery(resolve_snapshot(number)), episode=ResolveEpisode(f"exhausted-{number}", ceiling))
    return reconcile_resolve_ceiling(state, ceiling=ceiling, evidence_ids=(f"prior-attempts-{number}",)).snapshot


def seed_exhausted(deployment: Deployment, *labels: str, number: int = ISSUE,
                   extra_gates: tuple = ()) -> WorkflowSnapshot:
    """Remote issue plus an exhausted resolve episode held by its ceiling gate."""
    labels = labels or (Label.NEEDS_HUMAN_REVIEW.value,)
    deployment.github().seed(IssueView(
        number, title=TITLE, body=BODY, updated_at="2026-09-14T00:00:00Z",
        labels=frozenset(Label(label) for label in labels),
    ))
    state = exhausted_snapshot(deployment.config().max_resolve_retries, number)
    if extra_gates:
        state = replace(state, gates=state.gates + tuple(extra_gates))
    install_workflow(deployment.control(), state)
    return state


def seed_episode(deployment: Deployment, failures: int, *labels: str, number: int = ISSUE,
                 comments: tuple = ()) -> WorkflowSnapshot:
    """Remote issue plus a live (not yet exhausted) resolve episode."""
    deployment.github().seed(IssueView(
        number, title=TITLE, body=BODY, updated_at="2026-09-14T00:00:00Z",
        labels=frozenset(Label(label) for label in labels), comments=tuple(comments),
    ))
    state = replace(with_issue_recovery(resolve_snapshot(number)), episode=ResolveEpisode(f"episode-{number}", failures))
    install_workflow(deployment.control(), state)
    return state


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


def _publication_context(role: str, cwd: str | None):
    """Real branch discovery without requiring the `gh` binary on test hosts."""
    if role != "fix" or cwd is None:
        return None
    branch = subprocess.run(["git", "-C", cwd, "branch", "--show-current"],
                            capture_output=True, text=True, check=False).stdout.strip()
    if not branch:
        return None
    return accounting_runtime._FixPublicationContext(
        str(Path(cwd).resolve()), branch, "/usr/bin/git", "/usr/bin/gh", "/usr/bin:/bin",
    )


def patch_publication_environment(monkeypatch=None) -> None:
    """External binary capabilities only; authority stays real."""
    patches = ((accounting_runtime, "_fix_publication_context", _publication_context),)
    for module, name, value in patches:
        if monkeypatch is None:
            setattr(module, name, value)
        else:
            monkeypatch.setattr(module, name, value)


def child_environment(deployment: "Deployment") -> None:
    """Apply the harness's external-capability patches inside a spawned process."""
    isolate_authority(deployment)
    install_clock(deployment)
    patch_publication_environment()


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




@dataclass
class Composition:
    deployment: Deployment
    cfg: HarnessConfig
    owner: str
    fix: roles.FixDaemonComposition
    remote: PersistentGitHub
    provider: ScriptedProvider
    log: list[str]
    operator_fault: list[Callable[[str], None]] = field(default_factory=list)

    @property
    def daemon(self) -> Daemon:
        return self.fix.daemon

    @property
    def control(self) -> SQLiteControlStore:
        return self.fix.control

    @property
    def workflow(self) -> SQLiteWorkflowControl:
        return self.fix.workflow_control

    @property
    def rearm(self) -> ResolveRearmWorkflow:
        return self.fix.rearm

    @property
    def key(self):
        return resolve_snapshot(ISSUE).binding.fence.key

    def tick(self, count: int = 1) -> None:
        for _ in range(count):
            self.daemon.tick()
            # Production overlaps paid legs with later ticks; acceptance joins
            # each bounded set so one tick is one observable step.
            self.daemon.wait_for_dispatches()

    def boot(self) -> None:
        self.fix.recovery.reconcile(RecoveryTrigger.BOOT)

    def state(self, number: int = ISSUE) -> WorkflowSnapshot:
        current = self.workflow.load_workflow(resolve_snapshot(number).binding.fence.key)
        assert isinstance(current, WorkflowSnapshot), current
        return current

    def labels(self, number: int = ISSUE) -> set[str]:
        return {label.value for label in self.remote.get_issue(number).labels}

    # -- operator transport (the dashboard service layer) --------------------

    def backend(self):
        """The dashboard ``Backend`` over the production operator factory.

        ``make test`` runs without the optional fastapi extra, so acceptance
        drives the Backend methods the routes invoke; the HTTP guard itself is
        covered by test_resolve_operator_api. Every request opens a fresh
        ``open_resolve_operator`` from the deployment's launch snapshot; only
        the GitHub transport and the Git remote mapping are injected.
        """
        from issue_agent.app.dashweb.service import Backend

        deployment = self.deployment

        def transport(_cfg, _env):
            return deployment.github(self.operator_fault[0] if self.operator_fault else (lambda _: None))

        factory = partial(open_resolve_operator, gateway=transport,
                          worktrees=lambda _cfg: deployment.worktrees(), log=self.log.append)
        backend = Backend(harness_root=str(deployment.root), control_db_path=deployment.control_path,
                          resolve_factory=factory, now_fn=deployment.clock.now)
        # Deployment discovery lists config/repos under the CLI's global root.
        backend.names = lambda: (DEPLOYMENT,)
        return backend

    def read(self, request_id: str | None = None) -> dict:
        return self.backend().resolve_control(DEPLOYMENT, ISSUE, request_id).model_dump(mode="json")

    def request_body(self, request_id: str, *, guidance: dict | None = None, control: dict | None = None,
                     cause: str = "resolve_ceiling") -> dict:
        control = control or self.read()
        gate = next(g for g in control["gates"] if g["cause"]["kind"] == cause)
        return {"operation": "request_rearm", "expected": control["expected"], "request_id": request_id,
                "gate_id": gate["gate_id"], "guidance": guidance or {"kind": "keep"}}

    def post(self, body: dict) -> dict:
        from pydantic import TypeAdapter
        from issue_agent.app.dashweb.resolve_contracts import ResolveCommand

        command = TypeAdapter(ResolveCommand).validate_json(json.dumps(body))
        return self.backend().resolve_command(DEPLOYMENT, ISSUE, command).model_dump(mode="json")

    def request_rearm(self, request_id: str = "operator-request", **kwargs) -> dict:
        return self.post(self.request_body(request_id, **kwargs))

    def cancel(self, request_id: str, cancellation_id: str = "operator-cancel", reason: str = "pause") -> dict:
        control = self.read()
        return self.post({"operation": "cancel_rearm", "expected": control["expected"],
                          "cancellation_id": cancellation_id, "request_id": request_id, "reason": reason})

    def continue_rearm(self, stopped_request_id: str, request_id: str = "operator-continue") -> dict:
        control = self.read()
        return self.post({"operation": "continue_rearm", "expected": control["expected"],
                          "request_id": request_id, "stopped_request_id": stopped_request_id})

    def gate_causes(self) -> list[str]:
        return [type(getattr(item, "gate", item).cause).__name__ for item in self.state().gates]

    def charges(self) -> list[tuple]:
        with sqlite3.connect(self.control.path) as db:
            return db.execute("SELECT run_id, amount_micros FROM charges ORDER BY run_id").fetchall()


def compose(
    deployment: Deployment, *, fault: Callable[[str], None] = lambda _: None,
    provider: ScriptedProvider | None = None,
    operator_fault: Callable[[str], None] | None = None,
    retry_ceiling: int | None = None,
) -> Composition:
    """The production fix daemon (``roles.compose_fix_daemon``) over the deployment."""
    cfg = deployment.config()
    if retry_ceiling is not None:
        cfg = replace(cfg, effect_retry_ceiling=retry_ceiling)
    remote = deployment.github(fault)
    provider = provider or ScriptedProvider()
    log: list[str] = []
    fix = roles.compose_fix_daemon(
        cfg, remote, Store(cfg.state_dir, runtime=cfg.fixer_runtime.value), deployment.worktrees(),
        provider, log=log.append,
    )
    assert os.path.realpath(fix.control.path) == os.path.realpath(deployment.control_path)
    return Composition(deployment, cfg, fix.owner_id, fix, remote, provider, log,
                       [operator_fault] if operator_fault is not None else [])
