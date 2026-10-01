"""Direct triage fixtures use the same SQL admission and effects as dispatch."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from issue_agent.adapters.accounting_runtime import AccountingRuntime
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.effects import GitHubEffectExecutor
from issue_agent.adapters.workflow_codec import encode_workflow
from issue_agent.adapters.workflow_rows import insert_workflow_component, load_issue_aggregate, persist_workflow_snapshot, workflow_scope
from issue_agent.app.execute import Executor
from issue_agent.app.workflows.steward import StewardWorkflow
from issue_agent.domain.contracts import Money
from issue_agent.domain.continuation_components import InactiveSteward, StewardLane
from issue_agent.domain.pricing import BudgetPolicy, ModelRates, RateTable, UnknownModelPolicy
from issue_agent.domain.workflow import IssueWorkflowShell
from issue_agent.domain.workflow_identity import IssueRef, WorkflowFamily, WorkflowKey
from tests.adapters.test_continuation_admission import active
from tests.adapters.test_steward_delivery import bind_events
from tests.adapters.test_workflow_admission import NOW
from tests.fakes.workflow_authority import resolve_snapshot


OWNER = "o/r:steward:fixture"


def install_steward(control, issues):
    with control.transaction() as db:
        db.execute("INSERT OR IGNORE INTO workflow_authority_epochs VALUES (?,?,?,?,?,?)",
                   ("o/r", WorkflowFamily.STEWARD.value, 1, "fixture", "fixture", NOW.isoformat()))
        for issue in issues:
            key = WorkflowKey("o/r", IssueRef(issue))
            existing = load_issue_aggregate(db, key)
            if existing is not None and not isinstance(existing.steward, InactiveSteward):
                continue
            revision = existing.revision if existing is not None else resolve_snapshot(issue).revision
            component = active(key, WorkflowFamily.STEWARD)
            if existing is None:
                shell = IssueWorkflowShell(revision, steward=component)
                db.execute("INSERT INTO workflows VALUES (?,?,?,?,?,?)",
                       (*workflow_scope(key), revision.fence.generation, revision.version, encode_workflow(shell)))
                insert_workflow_component(db, key, component.identity.authority, component.identity.episode_id)
            else:
                insert_workflow_component(db, key, component.identity.authority, component.identity.episode_id)
                persist_workflow_snapshot(db, replace(existing, steward=component))


def triage_lane(store, issue):
    return store._steward_control.read_steward(WorkflowKey("o/r", IssueRef(issue))).component.state.triage


def groom_lane(store, issue):
    return store._steward_control.read_steward(WorkflowKey("o/r", IssueRef(issue))).component.state.groom


class AccountedStewardWorkflow(StewardWorkflow):
    def _prepare(self, lane):
        if not hasattr(self.store, "_steward_control"):
            directory = TemporaryDirectory(prefix="steward-fixture-")
            self.store._steward_directory = directory
            control = SQLiteControlStore(str(Path(directory.name) / "control.db"), now=lambda: NOW)
            control.initialize()
            bind_events(control)
            self.store._steward_control = control
        control = self.store._steward_control
        install_steward(control, self.gateway.issues)
        self.gateway.comment_author = self.cfg.groom.steward_bot if lane is StewardLane.GROOM else self.cfg.triage_cfg.steward_bot
        if not isinstance(self.runner, AccountingRuntime):
            self.runner = AccountingRuntime(self.runner, control, "o/r", "steward", None, None, None, "fixture", "claude",
                BudgetPolicy({("steward", "TRIAGE"): Money.from_major("USD", self.cfg.triage.max_budget_usd),
                              ("steward", "GROOM"): Money.from_major("USD", self.cfg.groom.max_budget_usd if self.cfg.groom else 0.75)}),
                RateTable("fixture", "1", "USD", {}, UnknownModelPolicy.USE_FALLBACK, ModelRates(1, 1, 1, 1)), owner_id=OWNER)
            self.cfg = replace(self.cfg, dedupe=control, repo="o/r", owner_id=OWNER)
            self.executor = Executor(self.gateway, remove_worktree=lambda n: None,
                effect_executor=GitHubEffectExecutor(self.gateway, control, repository="o/r", identity=OWNER,
                    retry_ceiling=getattr(self, "_fixture_effect_ceiling", 10)), retry_delays=())
    async def triage(self, issue):
        self._prepare(StewardLane.TRIAGE)
        await super().triage(issue)

    async def groom(self, issue):
        if self.cfg.groom is None:
            return
        self._prepare(StewardLane.GROOM)
        await super().groom(issue)
