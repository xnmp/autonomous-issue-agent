"""Isolated accounting-backed resolve workflow fixtures, never live authority."""

import contextlib
import dataclasses
import hashlib
import tempfile
from pathlib import Path
from unittest.mock import patch

from issue_agent.adapters import accounting_runtime
from issue_agent.adapters.accounting_runtime import AccountingRuntime
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.journal import JournalingRuntime
from issue_agent.adapters.workflow_control import SQLiteWorkflowControl
from issue_agent.adapters.effects import GitHubEffectExecutor
from issue_agent.adapters.worktree import Worktrees
from issue_agent.app.workflows.fix import FixWorkflow
from issue_agent.app.workflows import fix as fix_workflow_module
from issue_agent.domain.contracts import Money, RunId, WorkflowAttemptIdentity
from issue_agent.domain.pricing import BudgetPolicy, ModelRates, RateTable, UnknownModelPolicy
from issue_agent.domain.workflow import FreshResolve, ResolveSession
from issue_agent.domain.resolve_split import AdjudicationInput, ContractRevision
from issue_agent.domain.workflow_identity import IssueRef, WorkflowKey, WorkflowWriter
from issue_agent.domain.world import PRView
from tests.fakes.workflow_authority import install_workflow, resolve_snapshot
from tests.fakes.legacy_journal import Journal


def seed_resolve(store, issue, *, failures=0, session_id=None, runtime="claude", model="m", guidance=""):
    state = resolve_snapshot(issue)
    if session_id:
        source = WorkflowAttemptIdentity(state.binding, RunId("imported-session"), "imported-leg")
        state = dataclasses.replace(state, revision=dataclasses.replace(state.revision, version=1), episode=dataclasses.replace(
            state.episode, continuation=FreshResolve(ResolveSession(source, session_id, runtime, model), 1),
        ))
    object.__setattr__(store, "_workflow_seed", dataclasses.replace(state, episode=dataclasses.replace(state.episode, failures=failures, guidance=guidance)))


def resolve_state(store, issue=5):
    return store._workflow_authority.load_workflow(WorkflowKey("o/r", IssueRef(issue)))


class AccountedFixtureWorkflow(FixWorkflow):
    """Direct workflow calls receive the same captured context as dispatch."""

    @contextlib.contextmanager
    def _accounted(self, issue, kind, binding):
        original = self.runner
        original_effects = self.executor._effect_executor
        self.executor._effect_executor = GitHubEffectExecutor(
            self.gateway, self.store._workflow_db, repository="o/r",
            identity="o/r:fix:fixture", log=self.cfg.log,
        )
        inner = original.inner if isinstance(original, JournalingRuntime) else original
        maximum = self.cfg.fix.max_budget_usd
        limits = {("fix", stage): Money.from_major("USD", maximum) for stage in (
            "RESOLVE", "RESUME", "PR_RESUME", "AUTHOR", "IMPLEMENT", "ADJUDICATE",
        )}
        limits[("fix", "ROUTE")] = Money.from_major("USD", self.cfg.fix.router_budget_usd)
        accounted = AccountingRuntime(
            inner, self.store._workflow_db, "o/r", "fix", None, None, None, "fixture", self._resolve_runtime_name(),
            BudgetPolicy(limits), RateTable("fixture", "1", "USD", {}, UnknownModelPolicy.USE_FALLBACK,
                                            ModelRates(1, 1, 1, 1)),
            owner_id="o/r:fix:fixture", max_contract_rounds=self.cfg.max_contract_rounds,
        )
        journalled = original if isinstance(original, JournalingRuntime) else JournalingRuntime(
            accounted, Journal(str(Path(self.store._workflow_dir.name) / "events.jsonl")), repo="o/r", role="fix",
        )
        if isinstance(original, JournalingRuntime):
            original.inner = accounted
        self.runner = journalled
        journalled.set_context("fix", kind, issue, issue, workflow=binding)
        publication = accounting_runtime._FixPublicationContext(
            f"/wt/issue-{issue}", f"fix/{issue}-fixture", "/usr/bin/git", "/usr/bin/gh", "/usr/bin:/bin",
        )
        contract_head = ""

        def captured_revision(_cwd, _issue, capture, branch):
            nonlocal contract_head
            latest_task = inner.tasks[-1] if getattr(inner, "tasks", None) else None
            latest_result = None
            if getattr(inner, "results", None):
                index = min(len(inner.tasks) - 1, len(inner.results) - 1)
                latest_result = inner.results[index]
            elif hasattr(inner, "result"):
                latest_result = inner.result
            revised = (
                latest_task is not None
                and isinstance(latest_task.split_input, AdjudicationInput)
                and latest_result is not None
                and latest_result.field("verdict") == "revised"
            )
            if not contract_head or revised:
                contract_head = hashlib.sha256(capture.encode()).hexdigest()
            return ContractRevision(branch, contract_head)

        try:
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(
                    accounting_runtime,
                    "_fix_publication_context",
                    return_value=publication,
                ))
                if not isinstance(self.worktrees, Worktrees):
                    stack.enter_context(patch.object(
                        accounting_runtime,
                        "capture_contract_revision",
                        side_effect=captured_revision,
                    ))
                    stack.enter_context(patch.object(
                        fix_workflow_module,
                        "verify_contract_revision",
                        side_effect=lambda _cwd, _issue, revision: revision,
                    ))
                yield
        finally:
            self.runner = original
            self.executor._effect_executor = original_effects
            if isinstance(original, JournalingRuntime):
                original.inner = inner

    async def resolve(self, issue, *, binding=None):
        binding = binding or resolve_state(self.store, issue).binding
        with self._accounted(issue, "RESOLVE", binding):
            await super().resolve(issue, binding=binding)

    async def resume(self, issue, *, binding=None):
        binding = binding or resolve_state(self.store, issue).binding
        with self._accounted(issue, "RESUME", binding):
            await super().resume(issue, binding=binding)



def attach_workflow_authority(
    gw, store, *, resolve_ceiling=20, max_contract_rounds=2
):
    # Every gateway instance models a fresh authoritative observation. Some
    # tests deliberately reuse one control store across several instances, so
    # normalization cannot be coupled to first-time database initialization.
    for number, issue in tuple(gw.issues.items()):
        if not issue.updated_at:
            gw.issues[number] = dataclasses.replace(
                issue, updated_at=f"fixture-revision-{number}"
            )
    if not hasattr(store, "_workflow_db"):
        for number in getattr(gw, "_pr_for_issue", {}).values():
            gw.prs.setdefault(number, PRView(number, head_ref_oid="head-sha"))
        object.__setattr__(store, "_workflow_dir", tempfile.TemporaryDirectory(prefix="workflow-test-"))
        object.__setattr__(store, "_workflow_db", SQLiteControlStore(str(Path(store._workflow_dir.name) / "control.sqlite")))
        store._workflow_db.initialize()
        seeded = getattr(store, "_workflow_seed", None)
        numbers = set(gw.issues) | {p.closing_issue for p in gw.prs.values() if p.closing_issue}
        numbers.add(5)
        for number in numbers:
            install_workflow(store._workflow_db, seeded if seeded is not None and seeded.revision.fence.key.target.number == number else resolve_snapshot(number))
        object.__setattr__(store, "_workflow_authority", SQLiteWorkflowControl(
            store._workflow_db,
            WorkflowWriter("o/r", "fix", "o/r:fix:fixture"),
            resolve_ceiling=resolve_ceiling,
            max_contract_rounds=max_contract_rounds,
        ))
    return store._workflow_authority
