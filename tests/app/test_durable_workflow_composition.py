"""Bounded crash/interleaving cases through real daemon and durable authority.

Only provider/worktree/GitHub transport is simulated. Restart recreates the
daemon, executors, Store and SQLite clients over the same two independent
databases. These are controlled restart replays, not live deployment evidence.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import multiprocessing
import os
from pathlib import Path
import sqlite3
from threading import Barrier
from zoneinfo import ZoneInfo
from unittest.mock import patch

import pytest

from issue_agent.adapters.control_store import SCHEMA_VERSION, SQLiteControlStore, RetiredOwnerError
from issue_agent.adapters import accounting_runtime
from issue_agent.adapters.accounting_runtime import AccountingRuntime
from issue_agent.adapters.effects import GitHubEffectExecutor
from issue_agent.adapters.event_log import EventLog, read_event_log
from issue_agent.adapters.github import GhUnavailable
from issue_agent.adapters.journal import JournalingRuntime
from issue_agent.adapters.typed_journal import TypedJournal
from issue_agent.adapters.store import Store
from issue_agent.adapters.workflow_codec import decode_workflow
from issue_agent.adapters.workflow_control import SQLiteWorkflowControl
from issue_agent.app import roles
from issue_agent.app.dispatch import AgentDispatcher, DispatchConfig
from issue_agent.app.execute import Executor
from issue_agent.app.loop import Daemon
from issue_agent.app.planners.tasks import FixTaskConfig, TriageTaskConfig
from issue_agent.app.workflows.steward import StewardWorkflow, StewardWorkflowConfig
from issue_agent.app.workflows import fix as fix_workflow_module
from issue_agent.domain.actions import AgentKind, PostComment, Relabel, RunAgent, Target
from issue_agent.domain.contracts import EffectStatus, Money, daily_budget_scope
from issue_agent.domain.fix import FixConfig, decide_fix
from issue_agent.domain.events import ControlTransitionCommitted, Producer, SpendRecorded
from issue_agent.domain.labels import Label
from issue_agent.domain.lifecycle import LifecycleOperation, completion_guard
from issue_agent.domain.outcome import AgentResult
from issue_agent.domain.pricing import BudgetPolicy
from issue_agent.domain.triage import TriageConfig
from issue_agent.domain.workflow import ResolveAdmission
from issue_agent.domain.resolve_split import ContractRevision, ContractResetResult
from issue_agent.domain.workflow_identity import WorkflowBinding, WorkflowWriter
from tests.domain.builders import issue, pr
from tests.fakes.persistent_github import PersistentGitHub
from tests.fakes.workflow_authority import install_workflow, resolve_snapshot, with_issue_recovery
from tests.app.test_continuation_activation_cli import command as continuation_command, stopped
from tests.app.test_resolve_activation_cli import deployment, dump, run as resolve_command


class Crash(BaseException):
    """Process loss bypasses application exception/retry handlers."""


class Worktrees:
    def __init__(self, root: Path):
        self.root = root
        self.resets = []

    def ensure(self, number: int, branch: str) -> str:
        return str(self.root)

    def branch_for_issue(self, number: int, fallback: str, *, preferred: str = "") -> str:
        return preferred or fallback

    def reset(self, number: int) -> None:
        self.resets.append(number)

    def reset_contract(self, issue: int, revision: ContractRevision, intent_id: str) -> ContractResetResult:
        import hashlib
        self.resets.append(issue)
        return ContractResetResult(revision, "refs/issue-agent/contract-reset/" + hashlib.sha256(intent_id.encode()).hexdigest())


class Provider:
    def __init__(self, after_run=lambda: None, result=None):
        self.after_run = after_run
        self.result = result or AgentResult(is_error=True, cost_usd=0.25, session_id="provider-receipt")
        self.tasks = []

    async def run(self, task):
        self.tasks.append(task)
        self.after_run()
        return self.result


@dataclass
class Composition:
    daemon: Daemon
    remote: PersistentGitHub
    control: SQLiteControlStore
    workflow: SQLiteWorkflowControl
    effects: GitHubEffectExecutor
    provider: Provider
    dispatched: list[RunAgent]
    log: list[str]


@pytest.fixture(autouse=True)
def controlled_publication_context(monkeypatch):
    monkeypatch.setattr(fix_workflow_module, "verify_contract_revision", lambda *args: None)
    monkeypatch.setattr(accounting_runtime, "capture_contract_revision",
                        lambda cwd, issue, capture_id, branch: ContractRevision(branch, "a" * 40))
    monkeypatch.setattr(
        accounting_runtime, "_fix_publication_context",
        lambda _role, cwd: accounting_runtime._FixPublicationContext(
            str(Path(cwd).resolve()), "fix/7-test", "/usr/bin/git", "/usr/bin/gh", "/usr/bin:/bin",
        ) if _role == "fix" else None,
    )


class SplitProvider(Provider):
    async def run(self, task):
        self.tasks.append(task)
        if "TEST/INTERFACE AUTHOR" in task.prompt:
            return AgentResult(False, status="tests_authored", session_id="author", cost_usd=.25)
        if "independent AUTHOR adjudicating" in task.prompt:
            return AgentResult(False, status="done", session_id="adjudicator", cost_usd=.25,
                               structured={"verdict": "upheld", "rationale": "The public API must remain stable."})
        return AgentResult(False, status="contract_change", needs="The contract requires the wrong API.", session_id="implementer", cost_usd=.25)


# ADR 0015 initial reconciliation delay: an unknown outcome's first read is due then.
UNKNOWN_READ_DELAY = timedelta(seconds=31)


def compose(root: Path, *, fault=lambda _: None, provider=None, decide=None, role="fix", split=False,
            later: timedelta = timedelta(0), control_path: Path | None = None,
            state_dir: Path | None = None) -> Composition:
    """``later`` restarts the process that much after the prior one (store clock)."""
    remote = PersistentGitHub(root / "github.db", fault=fault)
    control = SQLiteControlStore(str(control_path or root / "control.db"), now=lambda: datetime.now(timezone.utc) + later)
    control.initialize()
    workflow = SQLiteWorkflowControl(
        control, WorkflowWriter("o/r", "fix", "o/r:fix"),
        resolve_ceiling=FixConfig(fix_bot="fix-bot").max_resolve_retries,
    )
    events = EventLog(str(root / "events.jsonl"), Producer("test", "o/r", role, os.getpid(), "test-boot"))
    control.bind_event_outbox(events.prepare, events.append_committed)
    state = Store(state_dir=str(state_dir or root / role))
    messages = []
    journal = TypedJournal(str(root / "legacy-events.jsonl"), events)
    effects = GitHubEffectExecutor(remote, control, repository="o/r", identity=f"o/r:{role}", log=messages.append,
        fix_config=FixConfig(fix_bot="fix-bot"))
    provider = provider or Provider()
    runtime = AccountingRuntime(
        provider, control, "o/r", "fix", Money("USD", 5_000_000), None, None,
        "test", "claude", BudgetPolicy({("fix", kind): Money("USD", 3_000_000)
                                         for kind in ("RESOLVE", "RESUME", "FIX_CI", "AUTHOR", "IMPLEMENT", "ADJUDICATE")}),
        max_contract_rounds=2,
    )
    executor = Executor(remote, effect_executor=effects, retry_delays=(), log=messages.append, remove_worktree=lambda _: None, clear_issue_state=lambda action: state.cleanup_pr(action.pr, action.issue, action.merged))
    dispatcher = AgentDispatcher(
        DispatchConfig(fix=FixTaskConfig("o/r", agent_md="test", model="test-model"), fix_bot="fix-bot", log=messages.append,
                       author_tests_split=split, operator_guidance=roles.review_guidance_reader(control, "o/r")),
        remote, state, Worktrees(root), JournalingRuntime(runtime, journal, repo="o/r", role="fix", runtime="claude"), executor, workflow_control=workflow,
    )
    executor.bind_workflow_reconciliation(dispatcher.fix_workflow.reconcile_returns)
    dispatched = []

    def dispatch(action):
        dispatched.append(action)
        return asyncio.run(dispatcher.dispatch(action))

    executor._run_agent = dispatch
    policy = decide or (lambda world: decide_fix(
        world, FixConfig(fix_bot="fix-bot"),
        roles._fix_state(state, world, workflow_control=workflow), fixer_busy=False,
    ))
    return Composition(Daemon("fix", remote.build_world, policy, executor, log=messages.append, journal=journal), remote, control, workflow, effects, provider, dispatched, messages)


def seed(root: Path, *labels: str) -> None:
    PersistentGitHub(root / "github.db").seed(issue(7, *labels, updated_at="2026-09-14T00:00:00Z"))
    control = SQLiteControlStore(str(root / "control.db"))
    control.initialize()
    from tests.fakes.workflow_authority import with_issue_recovery
    install_workflow(control, with_issue_recovery(resolve_snapshot(7)))


def _compatible_sql_build_checkout(root: Path) -> Path:
    import io
    import subprocess
    import tarfile
    checkout = root / "compatible-sql-build"
    checkout.mkdir()
    archive = subprocess.run(["git", "archive", "f3bf33b1182dccabe9bf4b077d74f0d4af53492a", "src/issue_agent"],
                             cwd=Path(__file__).resolve().parents[2], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as files:
        files.extractall(checkout, filter="data")
    return checkout


def _copied_authoritative_rows(path: Path) -> dict[str, tuple[str, ...]]:
    with sqlite3.connect(path) as db:
        tables = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "AND name<>'schema_migrations'")]
        return {name: tuple(sorted(repr(row) for row in db.execute(f'SELECT * FROM "{name}"')))
                for name in tables}


def _stopped_copy_index_rollback(path: Path) -> None:
    """Explicit fixture-only schema43→38 rollback; every authority row survives."""
    from issue_agent.adapters.control_store import _SCHEMA_MIGRATIONS, _sql_statements
    index_migrations = {
        40: ("event_outbox_pending", "event_outbox_pending_destination", "lifecycle_review_generation_candidates"),
        39: ("workflow_repair_owed", "continuation_pr_admission_identity", "reservations_active_workload"),
    }
    charge_origin = next(item for item in _SCHEMA_MIGRATIONS if item.version == 41)
    with sqlite3.connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        assert db.execute("DELETE FROM schema_migrations WHERE version=43").rowcount == 1
        self_clear_index = next(item for item in _SCHEMA_MIGRATIONS if item.version == 38)
        db.execute(self_clear_index.source)
        assert db.execute("DELETE FROM schema_migrations WHERE version=42").rowcount == 1
        assert db.execute("SELECT version,source_digest FROM schema_migrations ORDER BY version DESC LIMIT 1").fetchone() == (41, charge_origin.digest)
        # The schema-38 build has no charge origin; every copied charge carries the default, so nothing is lost.
        assert db.execute("SELECT count(*) FROM charges WHERE origin<>'normal_return'").fetchone()[0] == 0
        db.execute("ALTER TABLE charges DROP COLUMN origin")
        assert db.execute("DELETE FROM schema_migrations WHERE version=41").rowcount == 1
    before = _copied_authoritative_rows(path)
    with sqlite3.connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        definitions = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type='index'"))
        for version, names in index_migrations.items():
            migration = next(item for item in _SCHEMA_MIGRATIONS if item.version == version)
            assert db.execute("SELECT version,source_digest FROM schema_migrations ORDER BY version DESC LIMIT 1").fetchone() == (version, migration.digest)
            statements = tuple(_sql_statements(migration.source))
            assert len(statements) == len(names)
            for name, statement in zip(names, statements, strict=True):
                assert definitions[name] == statement.strip().removesuffix(";")
            for name in names:
                db.execute(f'DROP INDEX "{name}"')
            assert db.execute("DELETE FROM schema_migrations WHERE version=? AND source_digest=?", (version, migration.digest)).rowcount == 1
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 38
    assert _copied_authoritative_rows(path) == before


def _forward_copy_indexes(path: Path, expected_rows: dict[str, tuple[str, ...]]) -> None:
    SQLiteControlStore(str(path)).initialize()
    assert _copied_authoritative_rows(path) == expected_rows
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == SCHEMA_VERSION
        assert {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE name IN "
            "('workflow_repair_owed','continuation_pr_admission_identity','reservations_active_workload')")} == {
                "workflow_repair_owed", "continuation_pr_admission_identity", "reservations_active_workload"}


def test_configured_copied_cutover_dispatch_and_restart_keep_sql_authority(stopped, capsys, tmp_path, monkeypatch):
    from issue_agent.domain.world import Check, Comment
    from dataclasses import replace
    from issue_agent.domain.workflow_identity import IssueRef, PullRequestRef, WorkflowKey
    from tests.fakes.review_workflow import held
    state, original, _ = stopped
    for number in (7, 8, 9):
        (state / f"issue-{number}.resolve-retries").write_text("0")
    (state / "issue-7.resumes").write_text("1")
    (state / "issue-7.resumed-at").write_text("2026-10-01T00:00:00Z")
    (state / "issue-7.ci-retries").write_text("2")
    (state / "issue-7.stall-sig").write_text('{"sig":"old-observation","since":"2026-10-01T00:00:00Z"}')
    (state / "issue-8.resumes").write_text("unknown")
    install_workflow(original, replace(held(key=WorkflowKey("o/r", PullRequestRef(90))), gates=()))
    (state / "pr-90.ci-rerun-head").write_text("copy-head")
    code, primary, error = resolve_command(capsys, "shadow")
    assert code == 0, error
    code, _, error = resolve_command(capsys, "activate", "--confirm-plan-digest", primary["plan"]["plan_digest"])
    assert code == 0, error
    (state / "issue-9.resolve-retries").unlink()
    before = dump(original)
    files = {path.name: path.read_bytes() for path in state.iterdir()}
    code, shadow, error = continuation_command(capsys, "fix", "shadow")
    assert code == 0, error
    assert {(target["family"], target["number"], target["eligible"]) for target in shadow["targets"]} == {
        ("issue_recovery", 7, True), ("issue_recovery", 8, False), ("issue_recovery", 9, True), ("pr_repair", 90, True)}
    copy = tmp_path / "configured-cutover"
    code, rehearsal, error = continuation_command(capsys, "fix", "rehearse", "--copy-to", str(copy))
    assert code == 0 and all(rehearsal["checks"].values()), error
    copied_control = copy / "issue-agent-control" / Path(original.path).name
    copied_state = next(path.parent for path in (copy / "state").glob("*/issue-7.resumes"))
    remote = PersistentGitHub(copy / "github.db")
    reply = "2026-10-02T00:00:00Z"
    remote.seed(issue(7, "ai-blocked", updated_at=reply,
                      comments=(Comment("human", "Use the local destination.", reply),)))
    remote.seed(issue(8, "ai-ready"))
    remote.seed(issue(10, "ai-ready"))
    returned = False
    def provider_returned():
        nonlocal returned
        returned = True
    def crash_after_capture(point):
        if returned and point == "read":
            raise Crash("copied paid return before remote followup")
    first = compose(copy, control_path=copied_control, state_dir=copied_state,
                    fault=crash_after_capture, provider=Provider(provider_returned,
                    AgentResult(False, status="blocked", needs="Which local directory?", session_id="copied-session", cost_usd=.25)))
    try:
        first.daemon.tick()
    except Crash:
        pass
    else:
        pytest.fail(repr((first.dispatched, first.log)))
    assert len(first.provider.tasks) == 1
    selected = first.control.read_issue_recovery(WorkflowKey("o/r", IssueRef(7)))
    assert (selected.component.state.consumed_reply_at, selected.component.state.resumes,
            selected.component.state.ci_failures) == (reply, 2, 2)
    restarted = compose(copy, control_path=copied_control, state_dir=copied_state)
    assert len(restarted.workflow.pending_returns()) == 1
    restarted.daemon.tick()
    assert restarted.workflow.pending_returns() == ()
    assert restarted.remote.get_issue(7).has(Label.AI_BLOCKED)
    assert any("Which local directory?" in comment.body for comment in restarted.remote.get_issue(7).comments)
    for number in (7, 8, 10):
        (copied_state / f"issue-{number}.resumes").write_text("0")
        (copied_state / f"issue-{number}.resumed-at").write_text("")
    restarted.daemon.tick()
    assert restarted.provider.tasks == []
    assert restarted.remote.get_issue(8).has(Label.AI_READY)
    assert restarted.remote.get_issue(10).has(Label.AI_READY)
    remote.seed(issue(9, "ai-pr-open", updated_at=reply))
    remote.seed_pr(pr(90, "ai-pr-open", head_ref_oid="copy-head", closing_issue=9,
                      checks=(Check("tests", bucket="fail"),)))
    monkeypatch.setattr(accounting_runtime, "_fix_publication_context", lambda _role, cwd:
        accounting_runtime._FixPublicationContext(str(Path(cwd).resolve()), "fix/90-x", "/usr/bin/git", "/usr/bin/gh", "/usr/bin:/bin"))
    blocked = compose(copy, control_path=copied_control, state_dir=copied_state,
                      provider=Provider(result=AgentResult(False, status="blocked", needs="Publication denied",
                      structured={"blocker": "publication"}, session_id="copied-ci-session", cost_usd=.25)))
    blocked.daemon.tick()
    assert len(blocked.provider.tasks) == 1
    assert blocked.remote.get_pr(90).has(Label.AI_BLOCKED)
    with sqlite3.connect(copied_control) as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 2
        assert db.execute("SELECT amount_micros FROM charges").fetchall() == [(250_000,), (250_000,)]
    import subprocess
    checkout = _compatible_sql_build_checkout(tmp_path)
    repository = Path(__file__).resolve().parents[2]
    script = '''
import json, pathlib, sys
import issue_agent
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.store import Store
from issue_agent.adapters.workflow_control import SQLiteWorkflowControl
from issue_agent.adapters.workflow_codec import encode_workflow
from issue_agent.app import roles
from issue_agent.domain.actions import RunAgent
from issue_agent.domain.fix import FixConfig, decide_fix
from issue_agent.domain.labels import Label
from issue_agent.domain.world import Comment, IssueView, WorldState
from issue_agent.domain.workflow_identity import IssueRef, WorkflowKey, WorkflowWriter
assert pathlib.Path(issue_agent.__file__).is_relative_to(sys.argv[3])
control = SQLiteControlStore(sys.argv[1])
control.initialize()
workflow = SQLiteWorkflowControl(control, WorkflowWriter("o/r", "fix", "o/r:fix"), resolve_ceiling=3)
world = WorldState(issues=(IssueView(7, labels=frozenset({Label.AI_BLOCKED}),
    comments=(Comment("human", "Use the local destination.", "2026-10-02T00:00:00Z"),)),
    IssueView(8, labels=frozenset({Label.AI_READY})), IssueView(10, labels=frozenset({Label.AI_READY}))))
actions = decide_fix(world, FixConfig(fix_bot="fix-bot"), roles._fix_state(Store(sys.argv[2]), world, workflow_control=workflow), fixer_busy=False)
assert not any(isinstance(action, RunAgent) for action in actions)
values = {number: control.read_issue_recovery(WorkflowKey("o/r", IssueRef(number))) for number in (7, 9)}
print(json.dumps({str(number): encode_workflow((value.binding, value.revision, value.component)) for number, value in values.items()}))
'''
    def read_old_build():
        return subprocess.run(["uv", "run", "--offline", "--no-sync", "--project", str(repository), "python", "-c", script,
                               str(copied_control), str(copied_state), str(checkout)], cwd=checkout,
                              env={**os.environ, "PYTHONPATH": str(checkout / "src")},
                              capture_output=True, text=True)
    newer_sql = dump(blocked.control)
    refused = read_old_build()
    assert refused.returncode != 0 and str(SCHEMA_VERSION) in refused.stderr and "38" in refused.stderr
    assert dump(blocked.control) == newer_sql
    # No index-rollback read here: fix rows written after the self-clear fields
    # were deleted are not readable by the schema-38 build (forward-only).
    assert dump(original) == before
    assert {path.name: path.read_bytes() for path in state.iterdir()} == files


@pytest.mark.parametrize("lane", ["TRIAGE", "GROOM"])
def test_configured_copied_steward_dispatch_recovers_owed_verdict_without_repurchase(stopped, capsys, tmp_path, lane):
    from issue_agent.app.planners.tasks import GroomTaskConfig
    from issue_agent.domain import continuation_components as cc
    from issue_agent.domain.workflow_identity import IssueRef, WorkflowKey
    _, original, env = stopped
    state = Path(env["STEWARD_STATE_DIR"])
    state.mkdir(parents=True)
    for name in ("triage-7.at", "groom-7.at"):
        (state / name).write_text("2026-09-13T00:00:00Z")
    (state / "groom-7.content").write_text("a" * 64)
    (state / "issue-7.steward-stalls-triage").write_text("1")
    (state / "issue-7.steward-stalls-groom").write_text("1")
    (state / "issue-8.steward-stalls-groom").write_text("unknown")
    before = dump(original)
    files = {path.name: path.read_bytes() for path in state.iterdir()}
    code, shadow, error = continuation_command(capsys, "steward", "shadow")
    assert code == 0, error
    assert [(target["number"], target["eligible"]) for target in shadow["targets"]] == [(7, True), (8, False)]
    copy = tmp_path / "configured-steward-cutover"
    code, rehearsal, error = continuation_command(capsys, "steward", "rehearse", "--copy-to", str(copy))
    assert code == 0 and all(rehearsal["checks"].values()), error
    copied_control = copy / "issue-agent-control" / Path(original.path).name
    copied_state = next(path.parent for path in (copy / "state").glob("*/triage-7.at"))
    remote = PersistentGitHub(copy / "github.db")
    remote.seed(issue(7, "ai-triage", updated_at="2026-09-14T00:00:00Z"))
    remote.seed(issue(8, "ai-triage", updated_at="2026-09-14T00:00:00Z"))
    remote.seed(issue(10, "ai-triage", updated_at="2026-09-14T00:00:00Z"))
    remote.seed_pr(pr(300, state="MERGED", closing_issue=42, title="Original defect"))
    verdict = {"verdict": "ready", "plain_summary": "clear", "acceptance_criteria": ["works"],
               "creator_questions": [], "owner_questions": [], "blocked_by_issues": []}
    provider = Provider(result=AgentResult(False, cost_usd=.01, structured=verdict if lane == "TRIAGE" else {"resolved_by": 42}))
    kind = AgentKind.TRIAGE if lane == "TRIAGE" else AgentKind.GROOM
    logs = []
    def configured(provider, fault=lambda _: None, later=timedelta(0)):
        control = SQLiteControlStore(str(copied_control), now=lambda: datetime.now(timezone.utc) + later)
        control.initialize()
        github = PersistentGitHub(copy / "github.db", fault=fault)
        github.comment_author = "steward-bot"
        events = EventLog(str(copy / "events.jsonl"), Producer("test", "o/r", "steward", os.getpid(), "copied-steward"))
        control.bind_event_outbox(events.prepare, events.append_committed)
        effects = GitHubEffectExecutor(github, control, repository="o/r", identity="o/r:steward", log=logs.append)
        executor = Executor(github, effect_executor=effects, retry_delays=(), log=logs.append)
        runtime = AccountingRuntime(provider, control, "o/r", "steward", None, None, None, "test", "claude",
            BudgetPolicy({("steward", name): Money("USD", 750_000) for name in ("TRIAGE", "GROOM")}), owner_id="o/r:steward")
        dispatcher = AgentDispatcher(DispatchConfig(repo="o/r", role="steward", deployment_owner_id="o/r:steward",
            triage=TriageTaskConfig("steward-bot", triage_prompt="test"),
            triage_cfg=TriageConfig("steward-bot", enabled=True, promote=True),
            groom=GroomTaskConfig("steward-bot"), fix_bot="fix-bot", log=logs.append),
            github, Store(str(copied_state)), Worktrees(copy), runtime, executor, dedupe=control)
        executor._run_agent = lambda action: asyncio.run(dispatcher.dispatch(action))
        daemon = Daemon("steward", github.build_world, lambda _: [RunAgent(kind, number) for number in (7, 8, 10)], executor, log=logs.append)
        return daemon, control, github
    first, control, _ = configured(provider, fail_at("comment_applied"))
    with pytest.raises(Crash):
        first.tick()
    assert len(provider.tasks) == 1
    key = WorkflowKey("o/r", IssueRef(7))
    owed = control.read_steward(key).component.state
    selected_lane = owed.triage if lane == "TRIAGE" else owed.groom
    sibling = owed.groom if lane == "TRIAGE" else owed.triage
    assert selected_lane.pending_delivery and selected_lane.baseline_at == "2026-09-13T00:00:00Z"
    restarted_provider = Provider()
    restarted, current, github = configured(restarted_provider, later=UNKNOWN_READ_DELAY)
    restarted.tick()
    restarted.tick()
    delivered = current.read_steward(key).component.state
    completed_lane = delivered.triage if lane == "TRIAGE" else delivered.groom
    assert not completed_lane.pending_delivery and completed_lane.baseline_at == github.get_issue(7).updated_at, "\n".join(logs)
    assert (delivered.groom if lane == "TRIAGE" else delivered.triage) == sibling
    assert restarted_provider.tasks == []
    assert github.get_issue(7).has(Label.AI_READY if lane == "TRIAGE" else Label.MAYBE_RESOLVED)
    assert len(github.get_issue(7).comments) == 1
    assert isinstance(current.read_steward(WorkflowKey("o/r", IssueRef(8))), cc.BlockedSteward)
    with sqlite3.connect(copied_control) as db:
        assert db.execute("SELECT kind FROM runs").fetchall() == [(lane,)]
        assert db.execute("SELECT amount_micros FROM charges").fetchall() == [(10_000,)]
    import json
    import subprocess
    from issue_agent.adapters.workflow_codec import encode_workflow
    checkout = _compatible_sql_build_checkout(tmp_path)
    script = '''
import json, pathlib, sys
import issue_agent
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.workflow_codec import encode_workflow
from issue_agent.domain.workflow_identity import IssueRef, WorkflowKey
assert pathlib.Path(issue_agent.__file__).is_relative_to(sys.argv[2])
control = SQLiteControlStore(sys.argv[1])
control.initialize()
selected = control.read_steward(WorkflowKey("o/r", IssueRef(7)))
print(json.dumps(encode_workflow((selected.binding, selected.revision, selected.component))))
'''
    def read_old_build():
        return subprocess.run(["uv", "run", "--offline", "--no-sync", "--project", str(Path(__file__).resolve().parents[2]),
                               "python", "-c", script, str(copied_control), str(checkout)], cwd=checkout,
                              env={**os.environ, "PYTHONPATH": str(checkout / "src")}, capture_output=True, text=True)
    later_rows = _copied_authoritative_rows(copied_control)
    newer_sql = dump(current)
    refused = read_old_build()
    assert refused.returncode != 0 and str(SCHEMA_VERSION) in refused.stderr and "38" in refused.stderr
    assert dump(current) == newer_sql
    _stopped_copy_index_rollback(copied_control)
    later_sql = dump(current)
    rollback = read_old_build()
    assert rollback.returncode == 0, rollback.stderr
    selected = current.read_steward(key)
    assert json.loads(rollback.stdout) == encode_workflow((selected.binding, selected.revision, selected.component))
    assert dump(current) == later_sql
    _forward_copy_indexes(copied_control, later_rows)
    assert dump(original) == before
    assert {path.name: path.read_bytes() for path in state.iterdir()} == files


def fail_at(point: str):
    def fault(observed: str):
        if observed == point:
            raise Crash(point)
    return fault


@pytest.mark.parametrize("failed_read", ["get_issue", "pr_for_issue"])
def test_paid_return_recovers_each_failed_observation_without_rebuying(tmp_path, monkeypatch, failed_read):
    seed(tmp_path, "ai-ready")
    paid = False

    def returned():
        nonlocal paid
        paid = True

    provider = Provider(returned, AgentResult(False, status="blocked", needs="Which destination?", session_id="paid-session", cost_usd=.25))
    first = compose(tmp_path, provider=provider)
    original = getattr(first.remote, failed_read)

    def unavailable(number):
        if paid:
            raise GhUnavailable("network", "post-run observation unavailable")
        return original(number)

    monkeypatch.setattr(first.remote, failed_read, unavailable)
    first.daemon.tick()
    assert len(provider.tasks) == 1
    key = resolve_snapshot(7).binding.fence.key
    held = first.workflow.load_workflow(key)
    assert held.gates
    assert held.episode.failures == 0
    assert held.episode.continuation.session is None
    assert not any(call[0] == "comment" for call in first.remote.calls)

    restarted = compose(tmp_path)
    monkeypatch.setattr(restarted.control, "_now", lambda: datetime.now(timezone.utc) + timedelta(minutes=2))
    for _ in range(3):
        restarted.daemon.tick()
    assert restarted.provider.tasks == []
    assert restarted.remote.get_issue(7).has(Label.AI_BLOCKED)
    assert not restarted.remote.get_issue(7).has(Label.AI_WORKING)
    assert len([call for call in restarted.remote.calls if call[0] == "comment"]) == 1
    recovered = restarted.workflow.load_workflow(key)
    assert recovered.episode.continuation.session.session_id == "paid-session"
    assert recovered.episode.failures == 0
    assert recovered.gates == ()
    assert restarted.workflow.pending_returns() == ()
    with sqlite3.connect(tmp_path / "control.db") as db:
        assert db.execute("SELECT COUNT(*) FROM charges").fetchone()[0] == 1


def test_corrupt_paid_return_holds_only_its_issue_while_restart_advances_a_healthy_peer(tmp_path, monkeypatch):
    seed(tmp_path, "ai-ready")
    paid = False

    def returned():
        nonlocal paid
        paid = True

    first = compose(tmp_path, provider=Provider(returned))
    original = first.remote.get_issue

    def unavailable(number):
        if paid:
            raise GhUnavailable("network", "post-run observation unavailable")
        return original(number)

    monkeypatch.setattr(first.remote, "get_issue", unavailable)
    first.daemon.tick()
    with first.control.transaction() as db:
        db.execute("UPDATE dedupe_keys SET payload_json='{}' WHERE namespace='workflow-return'")
    first.remote.seed(issue(8, "ai-ready", updated_at="2026-09-14T00:00:00Z"))
    install_workflow(first.control, with_issue_recovery(resolve_snapshot(8)))

    restarted = compose(tmp_path)
    monkeypatch.setattr(restarted.control, "_now", lambda: datetime.now(timezone.utc) + timedelta(minutes=2))
    restarted.daemon.tick()

    assert len(restarted.provider.tasks) == 1
    assert [action.number for action in restarted.dispatched] == [8]
    assert restarted.workflow.load_workflow(resolve_snapshot(7).binding.fence.key).gates
    assert restarted.workflow.load_workflow(resolve_snapshot(8).binding.fence.key).episode.failures == 1
    assert any("authority unavailable" in line for line in restarted.log)
    with restarted.control._connect() as db:
        assert db.execute("SELECT count(*) FROM charges").fetchone()[0] == 2


@pytest.mark.parametrize("stage", ["AUTHOR", "IMPLEMENT", "ADJUDICATE"])
def test_split_settlement_captures_the_return_before_application_receives_it(tmp_path, monkeypatch, stage):
    seed(tmp_path, "ai-ready")

    first = compose(tmp_path, provider=SplitProvider(), split=True)
    settle = AccountingRuntime._settle_result

    def crash_after_settlement(runtime, run_id, model, result, **kwargs):
        settled = settle(runtime, run_id, model, result, **kwargs)
        with runtime.store._connect() as db:
            kind = db.execute("SELECT kind FROM runs WHERE run_id=?", (str(run_id),)).fetchone()[0]
        if kind == stage:
            raise Crash(f"after {stage} settlement")
        return settled

    monkeypatch.setattr(AccountingRuntime, "_settle_result", crash_after_settlement)
    with pytest.raises(Crash, match=stage):
        first.daemon.tick()
    pending = first.workflow.pending_returns()
    assert len(pending) == 1
    assert pending[0].returned.stage.value == stage
    assert first.workflow.load_workflow(resolve_snapshot(7).binding.fence.key).gates
    with first.control._connect() as db:
        assert db.execute("SELECT count(*) FROM charges").fetchone()[0] == ("AUTHOR", "IMPLEMENT", "ADJUDICATE").index(stage) + 1


def test_process_loss_after_capture_retains_session_and_durable_followup(tmp_path):
    seed(tmp_path, "ai-ready")
    paid = False

    def returned():
        nonlocal paid
        paid = True

    def crash_after_return(point):
        if paid and point == "read":
            raise Crash("after capture, before observation")

    first = compose(tmp_path, fault=crash_after_return, provider=Provider(
        returned, AgentResult(False, status="blocked", needs="Which destination?", session_id="paid-session", cost_usd=.25),
    ))
    with pytest.raises(Crash):
        first.daemon.tick()
    restarted = compose(tmp_path)
    assert restarted.workflow.pending_returns()[0].returned.session.session_id == "paid-session"
    restarted.daemon.tick()
    assert restarted.provider.tasks == []
    assert restarted.remote.get_issue(7).has(Label.AI_BLOCKED)
    assert restarted.workflow.pending_returns() == ()


def _terminated_daemon(root: Path, boundary: str) -> None:
    action = PostComment(Target.ISSUE, 7, "Survives process loss.", workflow=resolve_snapshot(7).binding)
    client = compose(root, decide=lambda _: [action])
    if boundary == "after_begin":
        begin = client.control.begin_command

        def interrupted_begin(command_id):
            begin(command_id)
            os._exit(71)

        client.control.begin_command = interrupted_begin
    else:
        client.control.record_effect = lambda _: os._exit(71)
    client.daemon.tick()


def _terminated_after_paid_settlement(root: Path) -> None:
    client = compose(root, provider=Provider(result=AgentResult(
        False, status="blocked", needs="Which destination?", session_id="paid-session", cost_usd=.25,
    )))
    settle = AccountingRuntime._settle_result

    def after_settlement(self, *args, **kwargs):
        settle(self, *args, **kwargs)
        os._exit(72)

    context = accounting_runtime._FixPublicationContext(
        str(root.resolve()), "fix/7-test", "/usr/bin/git", "/usr/bin/gh", "/usr/bin:/bin",
    )
    with patch.object(AccountingRuntime, "_settle_result", after_settlement), patch.object(
        accounting_runtime, "_fix_publication_context", return_value=context,
    ):
        client.daemon.tick()


def _terminated_after_split_settlement(root: Path, stage: str) -> None:
    client = compose(root, provider=SplitProvider(), split=True)
    settle = AccountingRuntime._settle_result

    def after_settlement(runtime, run_id, model, result, **kwargs):
        settled = settle(runtime, run_id, model, result, **kwargs)
        with runtime.store._connect() as db:
            kind = db.execute("SELECT kind FROM runs WHERE run_id=?", (str(run_id),)).fetchone()[0]
        if kind == stage:
            os._exit(73)
        return settled

    context = accounting_runtime._FixPublicationContext(
        str(root.resolve()), "fix/7-test", "/usr/bin/git", "/usr/bin/gh", "/usr/bin:/bin",
    )
    with patch.object(AccountingRuntime, "_settle_result", after_settlement), patch.object(
        accounting_runtime, "_fix_publication_context", return_value=context,
    ), patch.object(accounting_runtime, "capture_contract_revision", side_effect=lambda cwd, number, capture_id, branch: ContractRevision(branch, "a" * 40)), patch.object(
        fix_workflow_module, "verify_contract_revision", return_value=None,
    ):
        client.daemon.tick()


@pytest.mark.parametrize("stage,next_stage", [("AUTHOR", "IMPLEMENT"), ("IMPLEMENT", "ADJUDICATE"), ("ADJUDICATE", "IMPLEMENT")])
def test_process_exit_preserves_split_stage_without_repeating_completed_paid_work(tmp_path, stage, next_stage):
    seed(tmp_path, "ai-ready")
    process = multiprocessing.get_context("spawn").Process(target=_terminated_after_split_settlement, args=(tmp_path, stage))
    process.start()
    try:
        process.join(timeout=10)
        assert process.exitcode == 73
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
    restarted = compose(tmp_path, split=True)
    receipt = restarted.workflow.pending_returns()[0].returned
    assert receipt.stage.value == stage
    restarted.daemon.tick()
    assert restarted.workflow.pending_returns() == ()
    assert restarted.provider.tasks == []
    state = restarted.workflow.load_workflow(resolve_snapshot(7).binding.fence.key)
    from issue_agent.domain.resolve_split import NeedsAdjudication
    cursor = state.episode.continuation.split
    assert ("ADJUDICATE" if isinstance(cursor, NeedsAdjudication) else "IMPLEMENT") == next_stage
    if stage == "ADJUDICATE":
        assert cursor.context.uphold_note == "The public API must remain stable."
    with restarted.control._connect() as db:
        assert db.execute("SELECT count(*) FROM charges").fetchone()[0] == ("AUTHOR", "IMPLEMENT", "ADJUDICATE").index(stage) + 1


def test_process_exit_after_settlement_cannot_leave_a_charge_without_return_evidence(tmp_path):
    seed(tmp_path, "ai-ready")
    process = multiprocessing.get_context("spawn").Process(target=_terminated_after_paid_settlement, args=(tmp_path,))
    process.start()
    try:
        process.join(timeout=10)
        assert process.exitcode == 72
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
    restarted = compose(tmp_path)
    receipt = restarted.workflow.pending_returns()[0].returned
    assert receipt.session.session_id == "paid-session"
    with sqlite3.connect(tmp_path / "control.db") as db:
        assert db.execute("SELECT count(*) FROM charges").fetchone()[0] == 1
    restarted.daemon.tick()
    assert restarted.provider.tasks == []
    assert restarted.remote.get_issue(7).has(Label.AI_BLOCKED)
    assert restarted.workflow.pending_returns() == ()


@pytest.mark.parametrize("value", ["😀" * 22000, None, {"unexpected": "object"}, "contains\x00nul"], ids=["large-unicode", "null", "object", "nul"])
def test_paid_result_prose_is_bounded_before_atomic_receipt(tmp_path, value):
    seed(tmp_path, "ai-ready")
    client = compose(tmp_path, provider=Provider(result=AgentResult(
        False, status="blocked", needs=value, summary=value, session_id="paid-session", cost_usd=.25,
    )))
    client.daemon.tick()
    with sqlite3.connect(tmp_path / "control.db") as db:
        assert db.execute("SELECT count(*) FROM charges").fetchone()[0] == 1
        payload = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-return'").fetchone()[0]
    from issue_agent.domain.resolve_return import ReturnedResolve
    receipt = decode_workflow(payload, ReturnedResolve)
    assert receipt.session.session_id == "paid-session"
    assert len(receipt.needs) <= 4000
    assert "\x00" not in receipt.needs
    if isinstance(value, dict):
        assert receipt.is_error
    elif isinstance(value, str) and len(value) > 4000:
        assert "[truncated]" in receipt.needs


@pytest.mark.parametrize("boundary", ["after_begin", "before_receipt"])
def test_process_death_restarts_from_committed_command_without_duplicate(tmp_path, boundary):
    seed(tmp_path, "needs-human-review")
    process = multiprocessing.get_context("spawn").Process(target=_terminated_daemon, args=(tmp_path, boundary))
    process.start()
    try:
        process.join(timeout=10)
        assert process.exitcode == 71
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
    restarted = compose(tmp_path)
    pending = restarted.control.pending_commands()
    assert len(pending) == 1
    command_id = pending[0].command.command_id
    assert pending[0].state == "executing"
    for _ in range(3):
        restarted.daemon.tick()
    assert restarted.control.pending_commands() == ()
    assert restarted.control.effect_receipt(command_id).status is EffectStatus.SUCCEEDED
    assert restarted.remote.calls == [["comment", 7, "Survives process loss."]]
    assert restarted.provider.tasks == []


@pytest.mark.parametrize("point", ["comment_applied", "receipt_commit"])
def test_remote_success_before_receipt_reconciles_once_after_restart(tmp_path, monkeypatch, point):
    seed(tmp_path, "needs-human-review")
    action = PostComment(Target.ISSUE, 7, "Waiting for operator approval.", workflow=resolve_snapshot(7).binding)
    first = compose(tmp_path, fault=fail_at(point), decide=lambda _: [action])
    if point == "receipt_commit":
        monkeypatch.setattr(first.control, "record_effect", lambda _: (_ for _ in ()).throw(Crash(point)))
    with pytest.raises(Crash):
        first.daemon.tick()
    pending = first.control.pending_commands()
    assert len(pending) == 1
    command = pending[0].command
    assert pending[0].state in {"executing", "unknown_outcome"}
    # An unknown outcome is re-read on its persisted schedule, not at once.
    restarted = compose(tmp_path, decide=lambda _: [action], later=UNKNOWN_READ_DELAY)
    for _ in range(3):
        restarted.daemon.tick()
    assert restarted.control.pending_commands() == ()
    assert restarted.control.effect_receipt(command.command_id).status is EffectStatus.SUCCEEDED
    assert restarted.remote.calls == [["comment", 7, action.body]]
    assert restarted.remote.get_issue(7).has(Label.NEEDS_HUMAN_REVIEW)
    assert restarted.provider.tasks == []


@pytest.mark.parametrize("boundary", ["INSERT", "UPDATE"])
def test_local_commit_failure_cannot_apply_remote_mutation(tmp_path, boundary):
    seed(tmp_path, "needs-human-review")
    action = PostComment(Target.ISSUE, 7, "Durable intent first.", workflow=resolve_snapshot(7).binding)
    first = compose(tmp_path, decide=lambda _: [action])
    with sqlite3.connect(first.control.path) as db:
        db.execute(f"CREATE TRIGGER injected_failure AFTER {boundary} ON lifecycle_commands BEGIN SELECT RAISE(ABORT, 'injected transaction failure'); END")
    first.daemon.tick()
    assert first.remote.calls == []
    assert any("injected transaction failure" in message for message in first.log)
    if boundary == "INSERT":
        assert first.control.pending_commands() == ()
        assert read_event_log(str(tmp_path / "events.jsonl")).events == ()
    else:
        assert first.control.pending_commands()[0].state == "pending"
    with sqlite3.connect(first.control.path) as db:
        db.execute("DROP TRIGGER injected_failure")
    restarted = compose(tmp_path, decide=lambda _: [action])
    for _ in range(3):
        restarted.daemon.tick()
    assert restarted.remote.calls == [["comment", 7, action.body]]
    assert restarted.control.pending_commands() == ()


def test_unknown_remote_result_remains_visible_and_does_not_repeat_write(tmp_path):
    seed(tmp_path, "needs-human-review")
    action = PostComment(Target.ISSUE, 7, "Operator evidence.", workflow=resolve_snapshot(7).binding)
    first = compose(tmp_path, fault=fail_at("comment_applied"), decide=lambda _: [action])
    with pytest.raises(Crash):
        first.daemon.tick()
    command = first.control.pending_commands()[0].command

    def unavailable(point):
        if point == "read":
            raise GhUnavailable("network", "readback unavailable")

    restarted = compose(tmp_path, fault=unavailable)
    for _ in range(3):
        restarted.daemon.tick()
    assert restarted.remote.calls == [["comment", 7, action.body]]
    assert restarted.control.pending_commands()[0].command.command_id == command.command_id
    assert restarted.control.effect_receipt(command.command_id).status is EffectStatus.UNKNOWN_OUTCOME
    assert any(
        isinstance(event.payload, ControlTransitionCommitted)
        and event.payload.entity_id == command.command_id
        and event.payload.state == "unknown_outcome"
        for event in read_event_log(str(tmp_path / "events.jsonl")).events
    )
    assert any("readback unavailable" in message for message in restarted.log)
    assert restarted.provider.tasks == []


def test_partial_relabel_preserves_hold_and_never_pays_after_restart(tmp_path):
    seed(tmp_path, "ai-ready")
    hold = Relabel(Target.ISSUE, 7, Label.AI_READY, Label.NEEDS_HUMAN_REVIEW, workflow=resolve_snapshot(7).binding)
    first = compose(tmp_path, fault=fail_at("label_added"), decide=lambda _: [hold])
    with pytest.raises(Crash):
        first.daemon.tick()
    assert first.remote.get_issue(7).labels == frozenset({Label.AI_READY, Label.NEEDS_HUMAN_REVIEW})
    restarted = compose(tmp_path)
    for _ in range(4):
        restarted.daemon.tick()
    assert restarted.remote.get_issue(7).has(Label.NEEDS_HUMAN_REVIEW)
    assert restarted.provider.tasks == []


def test_hold_added_during_resolve_completion_survives_restart(tmp_path):
    seed(tmp_path, "ai-ready")
    human = PersistentGitHub(tmp_path / "github.db")
    provider = Provider(lambda: human.relabel("issue", 7, None, "needs-human-review"))
    first = compose(tmp_path, provider=provider)
    first.daemon.tick()
    assert len(provider.tasks) == 1
    restarted = compose(tmp_path)
    for _ in range(4):
        restarted.daemon.tick()
    held = restarted.remote.get_issue(7)
    assert held.has(Label.NEEDS_HUMAN_REVIEW)
    assert not held.has(Label.AI_READY)
    assert restarted.provider.tasks == []


def test_concurrent_rearm_deliveries_do_not_erase_a_newer_hold(tmp_path):
    seed(tmp_path, "needs-human-review")
    revision = PersistentGitHub(tmp_path / "github.db").get_issue(7).updated_at
    rearm = Relabel(Target.ISSUE, 7, Label.NEEDS_HUMAN_REVIEW, Label.AI_READY, expected_revision=revision, workflow=resolve_snapshot(7).binding)
    barrier = Barrier(2)

    def deliver():
        client = compose(tmp_path, decide=lambda _: [rearm])
        barrier.wait(timeout=5)
        client.daemon.tick()

    with ThreadPoolExecutor(max_workers=2) as pool:
        deliveries = [pool.submit(deliver) for _ in range(2)]
        for delivery in deliveries:
            delivery.result(timeout=10)
    human = PersistentGitHub(tmp_path / "github.db")
    assert human.get_issue(7).has(Label.AI_READY)
    human.relabel("issue", 7, "ai-ready", "needs-human-review")
    delayed = compose(tmp_path, decide=lambda _: [rearm])
    delayed.daemon.tick()
    restarted = compose(tmp_path)
    for _ in range(3):
        restarted.daemon.tick()
    assert restarted.remote.get_issue(7).labels == frozenset({Label.NEEDS_HUMAN_REVIEW})
    assert restarted.provider.tasks == []


def test_paid_attempt_settlement_is_conserved_when_completion_receipt_is_lost(tmp_path):
    seed(tmp_path, "ai-ready")
    # The external worktree capability is controlled; the accounting wrapper,
    # reserve/settle transaction and workflow are the production composition.
    provider = Provider(result=AgentResult(is_error=False, status="blocked", needs="Which API version?", cost_usd=0.25, session_id="provider-receipt"))
    completed_labels = 0

    def crash_after_completion(point):
        nonlocal completed_labels
        if point == "relabel_applied":
            completed_labels += 1
            if completed_labels == 2:
                raise Crash("completion applied before receipt")

    first = compose(tmp_path, provider=provider, fault=crash_after_completion)
    with pytest.raises(Crash):
        first.daemon.tick()
    assert len(provider.tasks) == 1
    run_id = provider.tasks[0].env["ISSUE_AGENT_RUN_ID"]
    assert run_id
    with first.control._connect() as db:
        admission = decode_workflow(db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-admission' "
            "AND scope='o/r' AND key=?", (run_id,),
        ).fetchone()[0], ResolveAdmission)
        run_binding = decode_workflow(db.execute(
            "SELECT workflow_binding_json FROM runs WHERE run_id=?", (run_id,),
        ).fetchone()[0], WorkflowBinding)
    assert len(first.dispatched) == 1
    assert first.dispatched[0].kind is AgentKind.RESOLVE
    assert admission.attempt.binding == run_binding == first.dispatched[0].workflow
    assert run_binding == resolve_snapshot(7).binding
    scope = daily_budget_scope("o/r", "repository", "USD", datetime.now(timezone.utc), ZoneInfo("UTC"))
    before = first.control.snapshot(scope)
    assert before.settled == Money("USD", 250_000)
    assert before.reserved == Money.zero("USD")
    assert first.control.active_reservation_runs() == ()

    pending = first.control.pending_commands()[0].command
    assert pending.workflow == run_binding
    command_id = pending.command_id
    restarted = compose(tmp_path, later=UNKNOWN_READ_DELAY)
    for _ in range(4):
        restarted.daemon.tick()
    assert restarted.provider.tasks == []
    assert restarted.control.snapshot(scope) == before
    assert restarted.control.active_reservation_runs() == ()
    assert restarted.control.effect_receipt(command_id).status is EffectStatus.SUCCEEDED
    assert restarted.remote.get_issue(7).labels == frozenset({Label.AI_BLOCKED})
    assert len([call for call in restarted.remote.calls if call[0] == "comment"]) == 1
    transitions = [call for call in restarted.remote.calls if call[0] == "relabel"]
    assert len([call for call in transitions if call[3] == "ai-blocked"]) == 1
    assert len([call for call in transitions if call[2] == "ai-working"]) == 1
    with sqlite3.connect(restarted.control.path) as db:
        assert db.execute(
            "SELECT runs.run_id, runs.state, charges.run_id, "
            "charges.provider_receipt_id, charges.amount_micros "
            "FROM runs JOIN charges ON charges.run_id = runs.run_id"
        ).fetchall() == [(
            run_id, "completed", run_id,
            f"claude:provider-receipt:{run_id}", 250_000,
        )]
    settlements = [
        event for event in read_event_log(str(tmp_path / "events.jsonl")).events
        if isinstance(event.payload, SpendRecorded)
    ]
    assert len(settlements) == 1
    assert settlements[0].payload.run_id == run_id
    assert settlements[0].correlation.run_id == run_id
    assert settlements[0].correlation.issue == 7
    assert settlements[0].payload.amount_micros == 250_000


@pytest.mark.parametrize("human_hold", [False, True], ids=["repair-own-add", "preserve-new-hold"])
def test_guarded_partial_relabel_recovery_preserves_completion_authority(tmp_path, human_hold):
    seed(tmp_path, "ai-working")
    observed = PersistentGitHub(tmp_path / "github.db").get_issue(7)
    action = Relabel(
        Target.ISSUE, 7, Label.AI_WORKING, Label.AI_READY,
        expected_revision=observed.updated_at,
        completion_guard=completion_guard(observed, LifecycleOperation.COMPLETE_RESOLVE),
        workflow=resolve_snapshot(7).binding,
    )
    first = compose(tmp_path, fault=fail_at("label_added"), decide=lambda _: [action])
    with pytest.raises(Crash):
        first.daemon.tick()
    command_id = first.control.pending_commands()[0].command.command_id
    human = PersistentGitHub(tmp_path / "github.db")
    assert human.get_issue(7).labels == frozenset({Label.AI_WORKING, Label.AI_READY})
    if human_hold:
        human.relabel("issue", 7, None, "needs-human-review")
    # Exercise replay before new admission; the next fresh attempt is a separate
    # scheduler decision, not a recovery of the interrupted remote mutation.
    restarted = compose(tmp_path, decide=lambda _: [], later=UNKNOWN_READ_DELAY)
    for _ in range(3):
        restarted.daemon.tick()
    current = restarted.remote.get_issue(7)
    receipt = restarted.control.effect_receipt(command_id)
    if human_hold:
        assert current.has(Label.NEEDS_HUMAN_REVIEW)
        assert receipt.status is not EffectStatus.SUCCEEDED
    else:
        assert current.labels == frozenset({Label.AI_READY})
        assert receipt.status is EffectStatus.SUCCEEDED
    assert restarted.provider.tasks == []


@pytest.mark.parametrize("intervention", ["none", "hold", "reopen"], ids=["ordinary-merge", "hold-between-effects", "reopen-between-effects"])
def test_observed_merged_pr_cleanup_finishes_close_unless_human_intervenes(tmp_path, intervention):
    seed(tmp_path, "ai-pr-open")
    human = PersistentGitHub(tmp_path / "github.db")
    human.seed_pr(pr(70, "ai-pr-open", state="MERGED", closing_issue=7, head_ref_oid="merged-head"))
    interposed = False

    def hold_after_done(point):
        nonlocal interposed
        if intervention != "none" and point == "relabel_applied" and not interposed:
            interposed = True
            if intervention == "hold":
                human.relabel("issue", 7, None, "needs-human-review")
            else:
                human.reopen_issue(7)

    client = compose(tmp_path, fault=hold_after_done)
    client.daemon.tick()
    restarted = compose(tmp_path)
    for _ in range(3):
        restarted.daemon.tick()
    final = restarted.remote.get_issue(7)
    assert final.has(Label.AI_DONE)
    assert not final.has(Label.AI_PR_OPEN)
    assert not restarted.remote.get_pr(70).has(Label.AI_PR_OPEN)
    if intervention != "none":
        assert interposed
        assert final.state == "open"
        if intervention == "hold":
            assert final.has(Label.NEEDS_HUMAN_REVIEW)
        else:
            assert final.state_reason == "reopened"
        assert not any(call[0] == "close" for call in human.calls)
    else:
        assert final.state == "closed"
        assert final.state_reason == "completed"
        assert len([call for call in human.calls if call[0] == "close"]) == 1
    assert client.provider.tasks == restarted.provider.tasks == []


def test_triage_promotion_finishes_advisory_recurrence_after_own_transition(tmp_path):
    seed(tmp_path)
    remote = PersistentGitHub(tmp_path / "github.db")
    remote.seed_pr(pr(300, state="MERGED", closing_issue=42, title="Original defect"))
    provider = Provider(result=AgentResult(is_error=False, cost_usd=0.01, structured={
        "verdict": "ready", "plain_summary": "clear", "acceptance_criteria": ["works"],
        "creator_questions": [], "owner_questions": [], "blocked_by_issues": [],
        "recurrence_of": 42,
    }))
    client = compose(tmp_path, role="steward", provider=provider)
    state = Store(state_dir=str(tmp_path / "steward"))
    from tests.fakes.accounted_steward import install_steward
    install_steward(client.control, (7,))
    client.remote.comment_author = "steward-bot"
    runtime = AccountingRuntime(provider, client.control, "o/r", "steward", None, None, None, "test", "claude",
        BudgetPolicy({("steward", "TRIAGE"): Money("USD", 750_000)}), owner_id="o/r:steward")
    workflow = StewardWorkflow(
        StewardWorkflowConfig(
            triage=TriageTaskConfig("steward-bot", triage_prompt="test"),
            triage_cfg=TriageConfig("steward-bot", enabled=True, promote=True),
            log=client.log.append, fix_bot="fix-bot", repo="o/r", dedupe=client.control, owner_id="o/r:steward",
        ),
        client.remote, state, runtime, client.daemon.executor,
    )
    client.daemon.executor._run_agent = lambda action: asyncio.run(workflow.triage(action.number))
    client.daemon.decide = lambda _: [RunAgent(AgentKind.TRIAGE, 7)]
    client.daemon.tick()
    after = client.remote.get_issue(7)
    assert after.has(Label.AI_READY), client.log
    assert after.has(Label.RECURRENCE)
    assert any("#42" in comment.body and "PR #300" in comment.body for comment in after.comments)
    assert client.control.read_steward(resolve_snapshot(7).revision.fence.key).component.state.triage.baseline_at == after.updated_at
    assert not Path(state.state_dir, "triage-7.at").exists()
    restarted = compose(tmp_path, role="steward", decide=lambda _: [])
    restarted.daemon.tick()
    assert restarted.control.pending_commands() == ()
    assert len(provider.tasks) == 1
    assert restarted.remote.get_issue(7) == after


def test_changed_issue_body_cannot_apply_obsolete_blocked_answer_or_reset_allowance(tmp_path):
    seed(tmp_path, "ai-ready")
    human = PersistentGitHub(tmp_path / "github.db")
    human.edit_issue_body(7, "Which API version should be used?")
    changes = 0

    def answer_during_run():
        nonlocal changes
        changes += 1
        human.edit_issue_body(7, f"Use API version 2; ambiguity resolved. Clarification {changes}.")

    provider = Provider(
        answer_during_run,
        AgentResult(is_error=False, status="blocked", needs="Which API version?", cost_usd=0.25, session_id="obsolete-verdict"),
    )
    client = compose(tmp_path, provider=provider)
    client.daemon.tick()
    current = human.get_issue(7)
    assert len(provider.tasks) == 1
    assert not current.has(Label.AI_BLOCKED)
    assert not any("Which API version?" in comment.body for comment in current.comments)
    assert current.has(Label.AI_READY) or current.has(Label.AI_WORKING)
    key = resolve_snapshot(7).revision.fence.key
    allowance_used = client.workflow.load_workflow(key).episode.failures
    assert allowance_used == 1
    restarted = compose(tmp_path, decide=lambda _: [])
    restarted.daemon.tick()
    assert restarted.workflow.load_workflow(key).episode.failures == allowance_used
    scope = daily_budget_scope("o/r", "repository", "USD", datetime.now(timezone.utc), ZoneInfo("UTC"))
    assert restarted.control.snapshot(scope).settled == Money("USD", 250_000)
    assert restarted.control.snapshot(scope).reserved == Money.zero("USD")
    resumed = compose(tmp_path, provider=provider)
    for _ in range(6):
        resumed.daemon.tick()
    assert len(provider.tasks) == FixConfig(fix_bot="fix-bot").max_resolve_retries
    assert resumed.workflow.load_workflow(key).episode.failures == len(provider.tasks)
    assert resumed.workflow.held_projection(key).held
    assert human.get_issue(7).has(Label.NEEDS_HUMAN_REVIEW)
    assert not human.get_issue(7).has(Label.AI_BLOCKED)
    assert resumed.control.snapshot(scope).settled == Money("USD", 250_000 * len(provider.tasks))
    assert resumed.control.snapshot(scope).reserved == Money.zero("USD")


def seed_repair_replay(root: Path, *, branch="fix/7-test", head="head-b", run_id=None) -> None:
    from dataclasses import replace
    from issue_agent.domain import continuation_components as cc
    from issue_agent.domain.world import Check
    from issue_agent.domain.workflow_identity import WorkflowFamily, WorkflowKey, PullRequestRef
    from tests.fakes.review_workflow import held
    from tests.adapters.test_continuation_admission import active, install_secondary
    seed(root, "ai-pr-open")
    remote = PersistentGitHub(root / "github.db")
    remote.seed_pr(pr(70, "ai-pr-open", closing_issue=7, head_ref_name=branch, head_ref_oid=head,
                      checks=(Check("current-unit", bucket="fail", run_id=run_id),)))
    control = SQLiteControlStore(str(root / "control.db"))
    base = replace(held(key=WorkflowKey("o/r", PullRequestRef(70))), gates=())
    install_workflow(control, base)
    remote.link_pr(7, 70)
    component = active(base.revision.fence.key, WorkflowFamily.PR_REPAIR)
    install_secondary(control, replace(base, pr_repair=component), component)


@pytest.mark.parametrize("matching", [False, True], ids=["mismatched-publication-identity", "matched-publication-identity"])
def test_recorded_publication_identity_replay_is_bounded_without_operator(tmp_path, matching):
    # Baseline escaped family branch-publication-identity; transport replay,
    # not a claim to reproduce the original target repository verbatim.
    from dataclasses import asdict
    from issue_agent.domain.world import Check
    seed_repair_replay(tmp_path, branch="fix/7-test" if matching else "test/7-diagnostics")
    remote = PersistentGitHub(tmp_path / "github.db")
    def published():
        remote.external_pr_change(70, checks=[asdict(Check("current-unit", bucket="pass", run_id=702))])
    provider = Provider(published, AgentResult(False, status="pushed", cost_usd=.25, session_id="publication-replay"))
    first = compose(tmp_path, provider=provider)
    first.daemon.tick()
    restarted = compose(tmp_path, provider=provider)
    for _ in range(8):
        restarted.daemon.tick()
    if matching:
        assert len(provider.tasks) == 1
        assert restarted.remote.get_pr(70).checks[0].bucket == "pass"
        assert not restarted.remote.get_pr(70).has(Label.NEEDS_HUMAN_REVIEW)
    else:
        assert provider.tasks == []
        assert restarted.remote.get_pr(70).has(Label.NEEDS_HUMAN_REVIEW)
        assert any("publication" in message for message in first.log + restarted.log)
    with sqlite3.connect(restarted.control.path) as db:
        assert db.execute("SELECT COUNT(*),COALESCE(SUM(amount_micros),0) FROM charges").fetchone() == ((1, 250_000) if matching else (0, 0))
    assert restarted.control.active_reservation_runs() == ()


def test_recorded_repair_context_replay_uses_current_head_after_restart(tmp_path):
    # Baseline escaped repair-context-revision: stale historical context must
    # not request another operator rerun of already consumed current evidence.
    from dataclasses import asdict
    from issue_agent.domain.world import Check
    from issue_agent.domain.ci_history import CiAttempt
    seed_repair_replay(tmp_path, head="head-a", run_id=701)
    first = compose(tmp_path)
    first.daemon.tick()
    first.remote.advance_run_attempts("head-a", (701,), 2)
    settled = compose(tmp_path, later=UNKNOWN_READ_DELAY)
    settled.effects.drain_pending_commands()
    Store(str(tmp_path / "fix")).append_ci_attempt(7, CiAttempt("head-a", ("obsolete-unit",)))
    remote = PersistentGitHub(tmp_path / "github.db")
    remote.external_pr_change(70, head_ref_oid="head-b", checks=[asdict(Check("current-unit", bucket="fail", run_id=702))])
    remote.set_failed_run_log("head-b", (702,), "PINNED_HEAD_B_LOG: current failing assertion")
    fresh = compose(tmp_path, later=UNKNOWN_READ_DELAY)
    fresh.daemon.tick()
    remote.advance_run_attempts("head-b", (702,), 2)
    def repaired():
        remote.external_pr_change(70, checks=[asdict(Check("current-unit", bucket="pass", run_id=702))])
    provider = Provider(repaired, AgentResult(False, status="pushed", cost_usd=.25, session_id="repair-context-replay"))
    restarted = compose(tmp_path, provider=provider, later=UNKNOWN_READ_DELAY * 2)
    for _ in range(5):
        restarted.daemon.tick()
    assert len(provider.tasks) == 1
    prompt = provider.tasks[0].prompt
    assert "head-b" in prompt and "PINNED_HEAD_B_LOG" in prompt
    assert "already consumed for this head" in prompt
    assert "head-a" in prompt and "obsolete-unit" in prompt
    assert restarted.remote.get_pr(70).checks[0].bucket == "pass"
    assert [call for call in remote.calls if call[0] == "rerun"] == [["rerun", "head-a", [701]], ["rerun", "head-b", [702]]]
    with sqlite3.connect(restarted.control.path) as db:
        assert db.execute("SELECT COUNT(*),SUM(amount_micros) FROM charges").fetchone() == (1, 250_000)
    assert restarted.control.active_reservation_runs() == ()


def publication_recovery_action(composition):
    from issue_agent.domain import continuation_components as cc
    from issue_agent.domain.actions import EscalatePRRecovery
    from issue_agent.domain.workflow_identity import WorkflowKey, PullRequestRef
    selected = composition.control.read_pr_repair(WorkflowKey("o/r", PullRequestRef(70)))
    capture = cc.PublicationIdentityEscalationCapture(selected.binding, "head-b", "test/7-diagnostics", 7, "fix/7-test", "Harness publication identity mismatch; repair checkout before resuming.", "ai-pr-open")
    return EscalatePRRecovery(70, capture.head, capture.body, "publication_identity", capture)


def enqueue_publication_recovery(composition, action):
    from issue_agent.adapters.effects import command_for_action
    from issue_agent.adapters.workflow_codec import encode_workflow
    return composition.control.enqueue_command(command_for_action(action, repository="o/r", executor_identity="o/r:fix", payload_extra={"pr_recovery": encode_workflow(action.capture)}))


def test_publication_refusal_unsent_command_drains_after_restart(tmp_path):
    seed_repair_replay(tmp_path, branch="test/7-diagnostics")
    original = compose(tmp_path)
    action = publication_recovery_action(original)
    enqueue_publication_recovery(original, action)
    restarted = compose(tmp_path)
    assert restarted.effects.drain_pending_commands() == 1
    assert restarted.remote.get_pr(70).has(Label.NEEDS_HUMAN_REVIEW)
    assert len([call for call in restarted.remote.calls if call[0] == "comment_pr"]) == 1
    assert restarted.provider.tasks == []


@pytest.mark.parametrize("change", ["head", "branch", "generation", "linkage"])
def test_publication_refusal_changed_subject_retires_without_remote_hold(tmp_path, change):
    from dataclasses import replace
    from issue_agent.domain.workflow_identity import WorkflowKey, PullRequestRef
    seed_repair_replay(tmp_path, branch="test/7-diagnostics")
    original = compose(tmp_path)
    action = publication_recovery_action(original)
    enqueue_publication_recovery(original, action)
    if change == "head":
        original.remote.external_pr_change(70, head_ref_oid="head-c")
    elif change == "branch":
        original.remote.external_pr_change(70, head_ref_name="other/7")
    elif change == "linkage":
        original.remote.link_pr(7, 71)
    else:
        from issue_agent.adapters.workflow_rows import load_review_workflow_row, persist_workflow_snapshot
        with original.control.transaction() as db:
            current = load_review_workflow_row(db, WorkflowKey("o/r", PullRequestRef(70)))
            persist_workflow_snapshot(db, replace(current, revision=replace(current.revision, fence=replace(current.revision.fence, generation=current.revision.fence.generation + 1))))
    restarted = compose(tmp_path)
    restarted.effects.drain_pending_commands()
    assert not restarted.remote.get_pr(70).has(Label.NEEDS_HUMAN_REVIEW)
    assert not any(call[0] in {"comment_pr", "relabel_pr"} for call in restarted.remote.calls)
    assert restarted.provider.tasks == []


def test_publication_refusal_unavailable_read_defers_without_write(tmp_path):
    seed_repair_replay(tmp_path, branch="test/7-diagnostics")
    original = compose(tmp_path)
    enqueue_publication_recovery(original, publication_recovery_action(original))
    def unavailable(point):
        if point == "observation_read":
            raise GhUnavailable("transport", "publication observation unavailable")
    restarted = compose(tmp_path, fault=unavailable)
    restarted.effects.drain_pending_commands()
    assert restarted.remote.calls == []
    assert len(restarted.control.pending_commands()) == 1
    available = compose(tmp_path, later=UNKNOWN_READ_DELAY)
    available.effects.drain_pending_commands()
    assert available.remote.get_pr(70).has(Label.NEEDS_HUMAN_REVIEW)


def test_publication_refusal_comment_does_not_authorize_stale_label(tmp_path):
    seed_repair_replay(tmp_path, branch="test/7-diagnostics")
    composed = compose(tmp_path)
    action = publication_recovery_action(composed)
    original_comment = composed.remote.comment
    def changed_after_comment(kind, number, body):
        original_comment(kind, number, body)
        composed.remote.external_pr_change(70, head_ref_oid="head-c")
    composed.remote.comment = changed_after_comment
    from issue_agent.adapters.effects import StaleExpectedRevision
    with pytest.raises(StaleExpectedRevision):
        composed.effects.execute(action)
    restarted = compose(tmp_path, later=UNKNOWN_READ_DELAY)
    restarted.effects.drain_pending_commands()
    assert not restarted.remote.get_pr(70).has(Label.NEEDS_HUMAN_REVIEW)
    assert len([call for call in restarted.remote.calls if call[0] == "comment_pr"]) == 1
    assert not any(call[0] == "relabel_pr" for call in restarted.remote.calls)


def test_publication_escalation_rejects_legacy_pending_ci_cause(tmp_path):
    from issue_agent.domain.actions import EscalatePRRecovery
    from issue_agent.domain import continuation_components as cc
    seed_repair_replay(tmp_path, branch="test/7-diagnostics")
    composed = compose(tmp_path)
    publication = publication_recovery_action(composed)
    pending = cc.PendingCIEscalationCapture(publication.capture.binding, "head-b", "head-b", "pending CI")
    with pytest.raises(ValueError, match="typed cause"):
        EscalatePRRecovery(70, "head-b", "pending CI", "pending_ci", pending)


@pytest.mark.parametrize("error", [sqlite3.DatabaseError("authority storage failed"), RuntimeError("placeholder")])
def test_publication_prewrite_storage_failure_remains_local(tmp_path, error):
    from issue_agent.adapters.control_store import ControlStoreError
    seed_repair_replay(tmp_path, branch="test/7-diagnostics")
    composed = compose(tmp_path)
    action = publication_recovery_action(composed)
    enqueue_publication_recovery(composed, action)
    fault = error if isinstance(error, sqlite3.Error) else ControlStoreError("authority storage failed")
    original = composed.control.validate_pr_recovery
    def broken(command):
        raise fault
    composed.control.validate_pr_recovery = broken
    with pytest.raises(type(fault), match="authority storage failed"):
        composed.effects.execute(action)
    composed.control.validate_pr_recovery = original
    assert composed.remote.calls == []
    assert composed.effects.drain_pending_commands() == 1
    assert composed.remote.get_pr(70).has(Label.NEEDS_HUMAN_REVIEW)


def test_publication_prewrite_schedule_survives_restart_gates_and_recovers(tmp_path):
    from dataclasses import replace
    from issue_agent.domain.workflow import EvidenceUnavailable, OpenGate, NoAuthorizedRemediation
    from issue_agent.adapters.workflow_rows import load_review_workflow_row, persist_workflow_snapshot
    from issue_agent.domain.workflow_identity import WorkflowKey, PullRequestRef
    seed_repair_replay(tmp_path, branch="test/7-diagnostics")
    composed = compose(tmp_path)
    key = WorkflowKey("o/r", PullRequestRef(70))
    with composed.control.transaction() as db:
        current = load_review_workflow_row(db, key)
        unrelated = OpenGate("independent", 0, EvidenceUnavailable("other measurement", composed.control.now(), 1, "separate evidence"), NoAuthorizedRemediation("observe"))
        persist_workflow_snapshot(db, replace(current, gates=(unrelated,)))
    pending = enqueue_publication_recovery(composed, publication_recovery_action(composed))
    reads = []
    def unavailable(point):
        if point == "observation_read":
            reads.append(point)
            raise GhUnavailable("transport", "publication read unavailable")
    clock = composed.control.now()
    restarted = compose(tmp_path, fault=unavailable)
    restarted.control._now = lambda: clock
    restarted.effects.drain_pending_commands()
    schedule = restarted.control.prewrite_read(pending.command.command_id)
    assert schedule.failed_reads == 1
    assert schedule.next_read_at > clock
    before = len(reads)
    again = compose(tmp_path, fault=unavailable)
    again.control._now = lambda: clock
    again.effects.drain_pending_commands()
    assert len(reads) == before
    for _ in range(8):
        clock = schedule.next_read_at + timedelta(seconds=1)
        again.effects.drain_pending_commands()
        schedule = again.control.prewrite_read(pending.command.command_id)
    with again.control.transaction() as db:
        gates = load_review_workflow_row(db, key).gates
    assert any(isinstance(gate.cause, EvidenceUnavailable) and gate.gate_id != "independent" for gate in gates)
    assert again.remote.calls == []
    clock = schedule.next_read_at + timedelta(seconds=1)
    recovered = compose(tmp_path)
    recovered.control._now = lambda: clock
    assert recovered.effects.drain_pending_commands() == 1
    assert recovered.control.prewrite_read(pending.command.command_id) is None
    assert recovered.remote.get_pr(70).has(Label.NEEDS_HUMAN_REVIEW)
    with recovered.control.transaction() as db:
        assert [gate.gate_id for gate in load_review_workflow_row(db, key).gates] == ["independent"]


def test_publication_unknown_partial_comment_reconciles_after_restart(tmp_path):
    seed_repair_replay(tmp_path, branch="test/7-diagnostics")
    composed = compose(tmp_path)
    action = publication_recovery_action(composed)
    original = composed.remote.comment
    def lost(kind, number, body):
        original(kind, number, body)
        raise RuntimeError("comment reply lost")
    composed.remote.comment = lost
    with pytest.raises(RuntimeError, match="reply lost"):
        composed.effects.execute(action)
    restarted = compose(tmp_path, later=UNKNOWN_READ_DELAY)
    assert restarted.effects.drain_pending_commands() == 1
    assert restarted.remote.get_pr(70).has(Label.NEEDS_HUMAN_REVIEW)
    assert len([call for call in restarted.remote.calls if call[0] == "comment_pr"]) == 1


def test_same_head_ci_success_cannot_suppress_publication_cause(tmp_path):
    from dataclasses import asdict
    from issue_agent.domain.actions import EscalateCiPending
    from issue_agent.domain.checks import Check
    seed_repair_replay(tmp_path, branch="test/7-diagnostics")
    composed = compose(tmp_path)
    composed.remote.external_pr_change(70, checks=[asdict(
        Check("external", "PENDING", "pending", "2000-01-01T00:00:00Z", kind="status"))])
    assert composed.effects.execute(EscalateCiPending(70, "head-b", "CI needs operator attention"))
    assert composed.effects.execute(publication_recovery_action(composed))
    assert len([call for call in composed.remote.calls if call[0] == "comment_pr"]) == 2
    assert composed.provider.tasks == []


def test_provider_status_cannot_mint_publication_identity_escalation(tmp_path):
    seed_repair_replay(tmp_path)
    provider = Provider(result=AgentResult(True, status="publication_identity_refused", summary="provider claims harness refusal", cost_usd=.25))
    composed = compose(tmp_path, provider=provider)
    composed.daemon.tick()
    assert len(provider.tasks) == 1
    assert not composed.remote.get_pr(70).has(Label.NEEDS_HUMAN_REVIEW)
    assert not any(call[0] == "comment_pr" for call in composed.remote.calls)
    with sqlite3.connect(composed.control.path) as db:
        assert db.execute("SELECT COUNT(*) FROM charges").fetchone()[0] == 1


def test_publication_uncertain_ceiling_recovers_read_only_without_grant(tmp_path):
    from issue_agent.domain.workflow import ReconciliationOnly, RemoteOutcomeUncertain
    from issue_agent.adapters.workflow_rows import load_review_workflow_row
    from issue_agent.domain.workflow_identity import WorkflowKey, PullRequestRef
    seed_repair_replay(tmp_path, branch="test/7-diagnostics")
    composed = compose(tmp_path)
    action = publication_recovery_action(composed)
    original = composed.remote.comment
    def lost(kind, number, body):
        original(kind, number, body)
        raise RuntimeError("publication comment reply lost")
    composed.remote.comment = lost
    with pytest.raises(RuntimeError, match="reply lost"):
        composed.effects.execute(action)
    pending = composed.control.pending_commands()[0]
    def unavailable(point):
        if point == "read":
            raise GhUnavailable("transport", "unknown publication outcome")
    clock = composed.control.now() + UNKNOWN_READ_DELAY
    restarted = compose(tmp_path, fault=unavailable)
    restarted.control._now = lambda: clock
    for _ in range(9):
        restarted.effects.drain_pending_commands()
        state = restarted.control.command_reconciliation(pending.command.command_id)
        clock = (state.history if isinstance(state, ReconciliationOnly) else state).next_read_at + timedelta(seconds=1)
    assert isinstance(state, ReconciliationOnly)
    key = WorkflowKey("o/r", PullRequestRef(70))
    with restarted.control.transaction() as db:
        assert any(isinstance(gate.cause, RemoteOutcomeUncertain) for gate in load_review_workflow_row(db, key).gates)
    remote = PersistentGitHub(tmp_path / "github.db")
    remote.external_pr_change(70, labels=["needs-human-review"])
    before = list(remote.calls)
    recovered = compose(tmp_path)
    recovered.control._now = lambda: clock
    assert recovered.effects.drain_pending_commands() == 1
    assert recovered.remote.calls == before
    assert recovered.control.command_reconciliation(pending.command.command_id) is None
    with recovered.control.transaction() as db:
        assert load_review_workflow_row(db, key).gates == ()


def test_publication_absent_partial_effect_unavailable_guard_uses_unresolved_schedule(tmp_path):
    from issue_agent.domain.workflow import ReconciliationOnly, RemoteOutcomeUncertain
    from issue_agent.adapters.workflow_rows import load_review_workflow_row
    from issue_agent.domain.workflow_identity import WorkflowKey, PullRequestRef
    seed_repair_replay(tmp_path, branch="test/7-diagnostics")
    composed = compose(tmp_path)
    action = publication_recovery_action(composed)
    original = composed.remote.comment
    def lost(kind, number, body):
        original(kind, number, body)
        raise RuntimeError("comment reply lost")
    composed.remote.comment = lost
    with pytest.raises(RuntimeError, match="reply lost"):
        composed.effects.execute(action)
    pending = composed.control.pending_commands()[0]
    clock = composed.control.now() + UNKNOWN_READ_DELAY
    reads = []
    def unavailable(*args, **kwargs):
        reads.append(clock)
        raise GhUnavailable("transport", "fresh subject unavailable")
    restarted = compose(tmp_path)
    restarted.control._now = lambda: clock
    restarted.remote.has_comment = lambda *_: True
    restarted.remote.has_label = lambda *_: False
    restarted.remote.get_pr = unavailable
    restarted.effects.drain_pending_commands()
    state = restarted.control.command_reconciliation(pending.command.command_id)
    assert state.failed_reads == 1
    assert state.next_read_at > clock
    assert restarted.control.prewrite_read(pending.command.command_id) is None
    again = compose(tmp_path)
    again.control._now = lambda: clock
    again.remote.has_comment = lambda *_: True
    again.remote.has_label = lambda *_: False
    again.remote.get_pr = unavailable
    before = len(reads)
    again.effects.drain_pending_commands()
    assert len(reads) == before
    for _ in range(8):
        clock = state.next_read_at + timedelta(seconds=1)
        again.effects.drain_pending_commands()
        state = again.control.command_reconciliation(pending.command.command_id)
        if isinstance(state, ReconciliationOnly):
            break
    assert isinstance(state, ReconciliationOnly)
    with again.control.transaction() as db:
        assert any(isinstance(gate.cause, RemoteOutcomeUncertain) for gate in load_review_workflow_row(db, WorkflowKey("o/r", PullRequestRef(70))).gates)
    assert not any(call[0] == "relabel_pr" for call in again.remote.calls)
