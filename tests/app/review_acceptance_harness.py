"""Composed review-rejection acceptance harness (ADR 0015 review slice, increment 6).

The review daemon is ``roles.compose_review_daemon`` -- the function behind
``build_review_daemon`` -- so the observe/decide loop, the active-family
dispatch plan, the dispatcher and ``ReviewWorkflow``, paid admission through
the accounting runtime, the durable effect executor and its command drain,
boot recovery, and the review re-arm/ceiling reconciliation bound into both
the executor drain and ``EffectRecovery`` are production wiring. The control
store is real SQLite under an isolated authority root; the review checkout is
a real Git repository whose commits are the PR heads.

Operator commands go straight to ``SQLiteReviewWorkflowControl`` (the
composition's own instance, or a fresh one in a restarted process): the HTTP
operator transport for review rejections is increment 7.

Simulated: the paid reviewer (``ScriptedProvider``), GitHub
(``PersistentGitHub``: SQLite, survives restarts, paginated reviews with
fault points) and family activation, which is a direct authority-epoch row
because the review activation importer is increment 8. As in the resolve
harness each tick joins its dispatches, and every ambient ``datetime.now()``
follows one persisted clock file so restarts can advance time. These are
controlled replays, not live deployment evidence.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
import shlex
import sqlite3
import subprocess

from issue_agent.adapters.control_path import canonical_control_store_path
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.review_workflow_control import SQLiteReviewWorkflowControl
from issue_agent.adapters.store import Store
from issue_agent.app import roles
from issue_agent.app.config import HarnessConfig, from_env
from issue_agent.domain.labels import Label
from issue_agent.domain.outcome import AgentResult
from issue_agent.domain.recovery import RecoveryTrigger
from issue_agent.domain.review_workflow import ReviewWorkflowSnapshot
from issue_agent.domain.workflow import KeepGuidance, OpenGate, OperatorActor, RearmingGate, RequestRearm
from issue_agent.domain.workflow_identity import PullRequestRef, WorkflowFamily, WorkflowKey
from issue_agent.domain.world import IssueView, PRView
from tests.app.resolve_acceptance_harness import Clock, Crash, ScriptedProvider, fail_at, install_clock, isolate_authority
from tests.fakes.persistent_github import PersistentGitHub

REPO = "o/r"
PR = 7
BOT = "review-bot[bot]"
FIX_BOT = "fix-bot"
ACTOR = OperatorActor("operator:alice", "dashboard")
KEY = WorkflowKey(REPO, PullRequestRef(PR))
TRIGGER = Label.AI_PR_OPEN.value
NHR = Label.NEEDS_HUMAN_REVIEW.value
CEILING = 2

__all__ = ["ACTOR", "BOT", "CEILING", "Clock", "Crash", "KEY", "NHR", "PR", "TRIGGER", "fail_at"]


def diff_text(name: str, *, offset: int = 1) -> str:
    """A compare diff; `offset` moves hunks without changing the patch identity."""
    return (f"diff --git a/{name}.py b/{name}.py\n--- a/{name}.py\n+++ b/{name}.py\n"
            f"@@ -{offset},1 +{offset},2 @@\n context\n+{name} = True\n")


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


@dataclass(frozen=True)
class ReviewDeployment:
    """Everything persistent: control authority, GitHub double, review checkout, clock."""

    root: Path
    target: Path
    base: str
    heads: tuple[str, ...]
    ceiling: int = CEILING

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
            "REPO": REPO, "FIX_BOT": FIX_BOT, "REVIEW_BOT": BOT, "REVIEWER_RUNTIME": "claude",
            "REVIEW_MAX_REJECTIONS": str(self.ceiling), "TARGET_DIR": str(self.target),
            "STATE_DIR": str(self.root / "fix-state"), "REVIEW_STATE_DIR": str(self.root / "review-state"),
            "STEWARD_STATE_DIR": str(self.root / "steward-state"),
        }

    def config(self) -> HarnessConfig:
        return from_env(self.env())

    def github(self, fault: Callable[[str], None] = lambda _: None) -> PersistentGitHub:
        return PersistentGitHub(self.github_path, fault=fault, clock=self.clock.now)

    def activate(self) -> None:
        """Activate the review family (the importer is increment 8)."""
        store = SQLiteControlStore(self.control_path, now=self.clock.now)
        store.initialize()
        with store.transaction() as db:
            db.execute("INSERT INTO workflow_authority_epochs VALUES (?, ?, 1, 'acceptance', 'acceptance', ?)",
                       (REPO, WorkflowFamily.REVIEW_REJECTIONS.value, self.clock.now().isoformat()))

    def advance(self, minutes: float = 1) -> None:
        self.clock.advance(timedelta(minutes=minutes))


def create_review_deployment(root: Path, *, heads: int = 4, ceiling: int = CEILING) -> ReviewDeployment:
    """A real checkout whose commits are the PR heads, plus one open reviewed PR."""
    target = root / "target"
    target.mkdir()
    _git(target, "init", "-q", "-b", "dev")
    _git(target, "config", "user.email", "acceptance@example.invalid")
    _git(target, "config", "user.name", "Acceptance")
    (target / ".gitignore").write_text(".claude/\n")
    _git(target, "add", ".gitignore")
    _git(target, "commit", "-q", "-m", "base")
    base = _git(target, "rev-parse", "HEAD")
    shas = []
    for index in range(heads):
        (target / f"change{index}.py").write_text(f"change = {index}\n")
        _git(target, "add", f"change{index}.py")
        _git(target, "commit", "-q", "-m", f"head {index}")
        shas.append(_git(target, "rev-parse", "HEAD"))
    deployment = ReviewDeployment(root, target, base, tuple(shas), ceiling)
    remote = deployment.github()
    remote.seed(IssueView(1, title="Add thing", state="open"))
    remote.seed_pr(PRView(
        PR, head_ref_name="fix/1-thing", head_ref_oid=shas[0], base_ref_oid=base, title="Add thing",
        body="Adds the thing.", labels=frozenset({Label.AI_PR_OPEN}), state="OPEN", closing_issue=1,
        author=FIX_BOT, base_ref_name="dev",
    ))
    for index, sha in enumerate(shas):
        remote.set_diff(sha, diff_text(f"change{index}"))
    return deployment


def verdicts(*decisions: str) -> list[AgentResult]:
    return [AgentResult(is_error=False, cost_usd=0.1, session_id=f"review-{index}",
                        structured={"decision": decision, "body": f"verdict {index}: {decision.lower()}"})
            for index, decision in enumerate(decisions)]


@dataclass
class ReviewComposition:
    deployment: ReviewDeployment
    cfg: HarnessConfig
    review: roles.ReviewDaemonComposition
    remote: PersistentGitHub
    provider: ScriptedProvider
    log: list[str] = field(default_factory=list)

    @property
    def workflow(self) -> SQLiteReviewWorkflowControl:
        runtime = self.review.runtime
        assert runtime is not None
        return runtime.control

    def tick(self, count: int = 1, *, minutes: float = 1) -> None:
        for _ in range(count):
            self.deployment.advance(minutes)
            self.review.daemon.tick()
            self.review.daemon.wait_for_dispatches()

    def boot(self) -> None:
        self.review.recovery.reconcile(RecoveryTrigger.BOOT)

    def state(self) -> ReviewWorkflowSnapshot:
        current = self.workflow.load_workflow(KEY)
        assert isinstance(current, ReviewWorkflowSnapshot), current
        return current

    def gates(self) -> list:
        return [gate.gate if isinstance(gate, RearmingGate) else gate for gate in self.state().gates]

    def gate_causes(self) -> list[str]:
        return [type(gate.cause).__name__ for gate in self.gates()]

    def labels(self) -> set[str]:
        return {label.value for label in self.remote.get_pr(PR).labels}

    def submissions(self) -> list[list]:
        return [call for call in self.remote.calls if call[0] == "submit_review"]

    def bot_reviews(self) -> list:
        return [review for review in self.remote.get_pr(PR).reviews if review.author == BOT]

    def request(self, request_id: str = "request:1", *, guidance=None, gate_id: str | None = None):
        state = self.state()
        gate_id = gate_id or next(gate.gate_id for gate in self.gates() if isinstance(gate, OpenGate)
                                  and type(gate.cause).__name__ == "ReviewRejectionCeiling")
        command = RequestRearm(request_id, state.revision, gate_id, guidance or KeepGuidance(),
                               expected_binding=state.binding)
        return self.workflow.request_rearm(command, ACTOR)

    def charges(self) -> list[tuple]:
        with sqlite3.connect(self.review.control.path) as db:
            return db.execute("SELECT run_id, amount_micros FROM charges ORDER BY run_id").fetchall()


def compose_review(
    deployment: ReviewDeployment, *, fault: Callable[[str], None] = lambda _: None,
    provider: ScriptedProvider | None = None, alerts: Path | None = None,
) -> ReviewComposition:
    """The production review daemon (``roles.compose_review_daemon``) over the deployment.

    `alerts` makes ALERT_CMD append each operator alert line to that file.
    """
    cfg = deployment.config()
    if alerts is not None:
        cfg = replace(cfg, alert_cmd=f"cat >> {shlex.quote(str(alerts))}")
    remote = deployment.github(fault)
    provider = provider or ScriptedProvider()
    log: list[str] = []
    review = roles.compose_review_daemon(cfg, remote, Store(cfg.review_state_dir), provider, log=log.append)
    return ReviewComposition(deployment, cfg, review, remote, provider, log)


def child_environment(deployment: ReviewDeployment) -> None:
    isolate_authority(deployment)  # type: ignore[arg-type]  # duck-typed: root, authority_root
    install_clock(deployment)  # type: ignore[arg-type]
