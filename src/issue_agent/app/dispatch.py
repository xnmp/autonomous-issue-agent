"""AgentDispatcher — turns a RunAgent(kind, n) into: gather inputs → build the
RuntimePlan → run it → apply pure follow-up Actions + persist session/counters.

This is the impure glue the executor's `run_agent` calls. It owns the GitHub
reads the follow-up needs (notably the ground-truth "did a PR actually open?"
check) and the worktree/session plumbing, but every DECISION is delegated to a
pure domain function (resolve_actions / resume_actions / address_review_actions /
fix_ci_actions / review_submission / triage.actions_for_verdict). So the
dispatcher stays thin and the logic stays tested in tests/domain.

Async because AgentRunner.run is async (the SDK query is a coroutine).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from collections.abc import Callable
from functools import partial

from ..domain import review as review_dom, triage as triage_dom
from ..domain.actions import AgentKind, RunAgent
from ..adapters.runtime_plan import (
    AgentRuntime,
    ContextualRuntime,
)
from ..domain.agent_task import (
    RuntimeName,
)
from ..domain.outcome import deterministic_death
from ..domain.leg import Leg, LegTarget
from ..domain.ports import (
    ActionExecutor,
    ControlStore,
    FixWorktreePort,
    EventSink,
    WorkflowGitHubReads,
    WorkflowState,
)
from .planners.tasks import FixTaskConfig, GroomTaskConfig, JiraTaskConfig, ReviewTaskConfig, TriageTaskConfig
from .workflows.fix import FixWorkflow, FixWorkflowConfig
from .workflows.jira import JiraSyncWorkflow, JiraSyncWorkflowConfig
from .workflows.review import ReviewWorkflow, ReviewWorkflowConfig
from .workflows.steward import StewardWorkflow, StewardWorkflowConfig
from .workflows.base import run_leg


def _ignore_log(_message: str) -> None:
    pass


@dataclass
class DispatchConfig:
    base_branch: str = "dev"
    fix: FixTaskConfig | None = None
    review: ReviewTaskConfig | None = None
    triage: TriageTaskConfig | None = None
    groom: GroomTaskConfig | None = None
    triage_cfg: triage_dom.TriageConfig | None = None
    jira: JiraTaskConfig | None = None
    fork_threshold: int = 5
    max_review_rejections: int = 4
    author_tests_split: bool = False
    max_contract_rounds: int = 2
    # REVIEW_MODE: which reviewer strategy to run —
    #   SINGLE    — one session-resumed agent reads diff + files itself (default).
    #   MULTI     — the /code-review fan-out (skip-gate → dimension specialists →
    #               per-finding adversarial validation → pure synthesis).
    #   DELEGATED — SINGLE + a cheap code-scout subagent the reviewer delegates
    #               codebase search to (expensive model reasons over summaries).
    review_mode: review_dom.ReviewMode = review_dom.ReviewMode.SINGLE
    # Reviewer bot login — used to count the reviewer's OWN prior REQUEST_CHANGES
    # verdicts on a PR (the rejection ceiling), distinguishing them from any human
    # or other-bot reviews.
    review_bot: str = ""
    reviewer_runtime: RuntimeName = RuntimeName.CLAUDE
    review_execution_cwd: str = ""
    review_prepare_cwd: Callable[[str], None] | None = None
    fix_mcp_config: str = ""
    harness_root: str = ""
    # PR label removed when a review escalates to needs-human-review. Matches the
    # reviewer's trigger label (default ai-pr-open; ai-review for an external repo).
    review_trigger_label: str = "ai-pr-open"
    # Diagnostic logger (stderr in production; the steward passes its tagged logger).
    log: Callable[[str], None] = _ignore_log
    # Fixer login, threaded to FixWorkflow so its resume path filters comments
    # with the same rule the decide side uses (see FixWorkflowConfig.fix_bot).
    fix_bot: str = ""
    # Fixer runtime ("claude" | "codex"), threaded to FixWorkflow so difficulty
    # routing picks from the runtime's own model family (see MODEL_TIERS).
    fixer_runtime: RuntimeName = RuntimeName.CLAUDE

    # Kill switch for the evidence-driven tier/effort ladder (#16). See
    # FixWorkflowConfig.escalate_on_evidence.
    escalate_on_evidence: bool = True
    # Paid accounting is enforced by the runtime wrapper over SQLite. These two
    # fields only provide durable run attribution.
    role: str = ""
    repo: str = ""
    command_identity: str = ""
    deployment_owner_id: str = ""
    config_version: str = ""
    # The ceiling this role's decide side reads off the infra-death counter
    # (fix: max_infra_deaths; review: max_unproductive_rounds). Used ONLY to
    # saturate the counter on a deterministic death (#13) so the role's existing
    # escalation fires on the next poll instead of after N identical retries.
    # 0 = unknown/disabled → the ordinary +1 bump, i.e. exactly today's behavior
    # for every hand-built DispatchConfig.
    max_infra_deaths: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.reviewer_runtime, RuntimeName):
            self.reviewer_runtime = RuntimeName(self.reviewer_runtime)
        if not isinstance(self.fixer_runtime, RuntimeName):
            self.fixer_runtime = RuntimeName(self.fixer_runtime)

    def fix_workflow_config(self) -> FixWorkflowConfig:
        return FixWorkflowConfig(
            base_branch=self.base_branch,
            fix=self.fix,
            fork_threshold=self.fork_threshold,
            author_tests_split=self.author_tests_split,
            max_contract_rounds=self.max_contract_rounds,
            fix_mcp_config=self.fix_mcp_config,
            harness_root=self.harness_root,
            log=self.log,
            fix_bot=self.fix_bot,
            fixer_runtime=self.fixer_runtime,
            escalate_on_evidence=self.escalate_on_evidence,
        )

    def review_workflow_config(self) -> ReviewWorkflowConfig:
        return ReviewWorkflowConfig(
            review=self.review,
            review_mode=self.review_mode,
            max_review_rejections=self.max_review_rejections,
            review_bot=self.review_bot,
            reviewer_runtime=self.reviewer_runtime,
            execution_cwd=self.review_execution_cwd,
            prepare_cwd=self.review_prepare_cwd,
            review_trigger_label=self.review_trigger_label,
            log=self.log,
        )

    def steward_workflow_config(self) -> StewardWorkflowConfig:
        return StewardWorkflowConfig(
            triage=self.triage,
            triage_cfg=self.triage_cfg,
            log=self.log,
            groom=self.groom,
            # The steward reads the FIXER's merged PRs (harness #10): "already
            # shipped by this harness" means shipped by the fix bot, not by the
            # steward's own identity.
            fix_bot=self.fix_bot,
            repo=self.repo,
        )

    def jira_workflow_config(self) -> JiraSyncWorkflowConfig:
        return JiraSyncWorkflowConfig(
            jira=self.jira,
            log=self.log,
            repository=self.repo,
            command_identity=self.command_identity,
            deployment_owner_id=self.deployment_owner_id,
            config_version=self.config_version,
        )


class _NoFixWorktrees:
    """Typed null object for roles that cannot dispatch fix work."""

    @staticmethod
    def _unavailable() -> RuntimeError:
        return RuntimeError("fix worktrees are unavailable for this role")

    def ensure(self, issue: int, branch: str) -> str:
        del issue, branch
        raise self._unavailable()

    def remove(self, issue: int) -> None:
        del issue
        raise self._unavailable()

    def branch_for_issue(self, issue: int, fallback: str) -> str | None:
        del issue, fallback
        raise self._unavailable()

    def reset(self, issue: int) -> None:
        del issue
        raise self._unavailable()


class AgentDispatcher:
    """Runs one RunAgent action end-to-end. `executor` applies the follow-up
    Actions (so all writes funnel through one place); `runner` is the SDK
    AgentRunner; `gateway`/`store`/`worktrees` are the adapters. `run` is
    `runner.run` (async). All collaborators are injected → fully fakeable."""

    def __init__(
        self,
        cfg: DispatchConfig,
        gateway: WorkflowGitHubReads,
        store: WorkflowState,
        worktrees: FixWorktreePort | None,
        runner: AgentRuntime,
        executor: ActionExecutor,
        prepare_cwd: Callable[[str], None] | None = None,
        journal: EventSink | None = None,
        reserve_fix_paths: Callable[[int, frozenset[str]], bool] | None = None,
        dedupe: ControlStore | None = None,
        dispatch_claim_target: Callable[[RunAgent], int | None] | None = None,
    ):
        self.cfg = cfg
        self.gw = gateway
        self.store = store
        self.runner = runner
        self.executor = executor
        self.journal = journal
        self.dispatch_claim_target = dispatch_claim_target
        # PR → issue answers this process has actually received. A dying
        # gateway must not silently move a counter into a key space nobody
        # reads (review P1R-1); see `_spend_target`.
        self._issue_memo: dict[int, int] = {}
        fix_worktrees = worktrees if worktrees is not None else _NoFixWorktrees()
        self.fix_workflow = FixWorkflow(
            # Journal folded in here for the same reason as the steward's below:
            # it arrives as a constructor argument, not through DispatchConfig.
            replace(cfg.fix_workflow_config(), journal=journal),
            gateway,
            store,
            fix_worktrees,
            runner,
            executor,
            prepare_cwd,
            reserve_fix_paths,
        )
        self.review_workflow = ReviewWorkflow(
            replace(cfg.review_workflow_config(), journal=journal),
            gateway, store, runner, executor,
            self._run_logged,
        )
        self.steward_workflow = StewardWorkflow(
            # The journal reaches the dispatcher as a constructor argument, not
            # through DispatchConfig, so it is folded in here rather than being
            # duplicated as a second config field that could disagree with it.
            replace(
                cfg.steward_workflow_config(),
                journal=journal,
                dedupe=dedupe,
            ),
            gateway, store, runner, executor,
        )
        self.jira_workflow = JiraSyncWorkflow(
            cfg.jira_workflow_config(),
            gateway,
            store,
            runner,
            dedupe if hasattr(dedupe, "enqueue_command") else None,
        )

    async def _run_logged(self, label: str, issue: int, task):
        """Run one agent leg, bracketing it with start/done breadcrumbs so a long
        in-session TDD run isn't a silent gap in the log. The per-commit milestones
        (red:/green:/docs:) are logged live by the governor hook as they happen."""
        return await run_leg(
            self.runner,
            self.cfg.log,
            Leg(
                label.upper().replace("-", "_").replace(":", "_"),
                LegTarget.PR,
                issue,
                True,
                ("global", "repository", "issue"),
            ),
            task,
        )

    # Kinds keyed by PR number rather than issue number. The per-issue spend cap
    # needs the ISSUE behind the work, so these are resolved through the gateway.
    _PR_KEYED = frozenset({
        AgentKind.PR_RESUME, AgentKind.ADDRESS_REVIEW, AgentKind.FIX_CI, AgentKind.REBASE,
        AgentKind.REVIEW, AgentKind.RE_REVIEW,
    })

    def _lookup_issue(self, pr: int) -> tuple[int | None, bool]:
        """`(issue, resolved)` from the gateway. `resolved` is False iff the
        lookup RAISED — which is a different fact from a clean `None`.

        Review P1R-1: those two used to collapse into `None`. A clean None means
        "this PR genuinely has no issue", whose breaker key IS `-pr`
        (`domain.fix.breaker_key`). A raised lookup means "unknown", and
        answering it with `-pr` moves the infra-death counter into a key space
        the fix role's decide side never reads. The coupling is the trap: the
        flakiness that kills sessions is the flakiness that fails this lookup,
        so the ceiling would fail exactly when it is needed."""
        try:
            issue = self.gw.issue_for_pr(pr)
        except Exception as exc:  # noqa: BLE001 — a lookup failure ≠ a blocked run
            self.cfg.log(f"spend: could not resolve issue for PR #{pr}: {exc}")
            return None, False
        if issue is not None:
            self._issue_memo[pr] = issue
        return issue, True

    def _spend_target(
        self, action: RunAgent
    ) -> tuple[int | None, bool, int | None]:
        """Issue scope, resolution status, and the reserved claim target.

        ADR 0008 makes the decision-side reservation authoritative for a
        production dispatch. Re-reading linkage here can disagree with the
        observed world after its global claim was acquired. Hand-built
        dispatchers without reservations retain the gateway fallback.
        """
        if self.dispatch_claim_target is not None:
            target = self.dispatch_claim_target(action)
            if target is not None:
                return (target if target > 0 else None), True, target
        if action.kind not in self._PR_KEYED:
            return action.number, True, None
        issue, resolved = self._lookup_issue(action.number)
        if resolved:
            return issue, True, None
        # The lookup failed. The last answer this process got for the SAME PR is
        # a far better key than a silent switch to the negated-PR space.
        return self._issue_memo.get(action.number), False, None

    def _issue_number(self, action: RunAgent) -> int | None:
        """The issue this dispatch spends against (see `_spend_target`)."""
        return self._spend_target(action)[0]

    def _death_key(self, action: RunAgent, issue: int | None, resolved: bool) -> int:
        """The `infra-deaths` key this dead dispatch is charged to.

        It MUST be a key the decide side reads: the issue number for issue legs
        and for any PR whose issue is resolvable, and `-pr` ONLY for a PR that
        genuinely has none (`domain.fix.breaker_key`'s convention). When the
        pre-handler lookup raised, `-pr` is a guess and not a fact, so:

          1. retry the lookup once — the transient that killed the session is
             frequently over by the time the exception unwinds, and one extra gh
             call on a path that just burned a whole session is free;
          2. fall back to whatever this process last resolved for that PR;
          3. only then use `-pr`, and say so loudly, because at that point the
             counter genuinely may not be read by the fix role's `_fix_state`
             (the review role already reads the union — see `roles._review_state`).
        """
        # Review retry policy is PR-keyed even when claim/spend attribution is
        # issue-keyed. A later relink must not erase a paid failed round.
        if action.kind in {AgentKind.REVIEW, AgentKind.RE_REVIEW}:
            return -action.number
        if issue is not None:
            return issue
        if action.kind not in self._PR_KEYED:
            return action.number
        if not resolved:
            retried, ok = self._lookup_issue(action.number)
            if retried is not None:
                return retried
            memo = self._issue_memo.get(action.number)
            if memo is not None:
                return memo
            if not ok:
                self.cfg.log(
                    f"infra-death for PR #{action.number} charged to the negated "
                    f"key -{action.number}: its issue could not be resolved on "
                    "either attempt, so the per-issue ceiling may not see it"
                )
        return -action.number

    async def dispatch(self, action: RunAgent) -> None:
        handler = {
            AgentKind.RESOLVE: self.fix_workflow.resolve,
            AgentKind.RESUME: self.fix_workflow.resume,
            AgentKind.PR_RESUME: self.fix_workflow.resume_pr,
            AgentKind.ADDRESS_REVIEW: self.fix_workflow.address_review,
            AgentKind.FIX_CI: self.fix_workflow.fix_ci,
            AgentKind.REBASE: self.fix_workflow.rebase,
            AgentKind.REVIEW: self.review_workflow.review,
            AgentKind.RE_REVIEW: self.review_workflow.re_review,
            AgentKind.TRIAGE: self.steward_workflow.triage,
            AgentKind.GROOM: self.steward_workflow.groom,
            AgentKind.JIRA_SYNC: self.jira_workflow.sync,
        }.get(action.kind)
        if handler is None:
            return
        issue, resolved, claim_target = self._spend_target(action)
        if action.kind in {
            AgentKind.PR_RESUME,
            AgentKind.ADDRESS_REVIEW,
            AgentKind.FIX_CI,
            AgentKind.REBASE,
        } and claim_target is not None:
            # ADR 0008: a fix PR dispatch whose reserved claim carries no issue
            # must not fall through to a live linkage lookup — that would run
            # under an identity that was never claimed. decide_fix currently
            # never dispatches issueless PR work, so this is a fence, not a path.
            if issue is None:
                self.cfg.log(
                    f"dispatch {action.kind.name} PR #{action.number}: deferred "
                    f"— claim target {claim_target} carries no issue; refusing "
                    "live-lookup identity (ADR 0008)"
                )
                return
            handler = partial(handler, issue_hint=issue)
        if action.kind in {
            AgentKind.REVIEW,
            AgentKind.RE_REVIEW,
        } and claim_target is not None:
            handler = partial(handler, claim_target=claim_target)
        # Tag every leg this dispatch runs (a multi-agent review runs six) with the
        # same context, so the ledger attributes each one to the work that caused it.
        if isinstance(self.runner, ContextualRuntime):
            self.runner.set_context(
                self.cfg.role,
                action.kind.name,
                action.number,
                issue,
                claim_target=claim_target,
            )
        try:
            await handler(action.number)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            # Review P1R-3. These three are the OPERATOR stopping the daemon, not
            # infrastructure failing: `issue-agent start` runs in the foreground
            # and Ctrl-C is the documented way to stop it. Counting them meant
            # five ordinary Ctrl-Cs escalated a healthy issue to
            # needs-human-review, while a SIGTERM (systemd's stop signal, which
            # raises no Python exception at all) counted nothing — the asymmetry
            # was exactly backwards.
            #
            # CancelledError is excluded WITH them, deliberately: under
            # `asyncio.run` — how `roles.py` drives every dispatch — a Ctrl-C
            # delivered while the loop is awaiting arrives here as a
            # CancelledError, because `asyncio.run` cancels the task in its
            # cleanup and only then re-raises KeyboardInterrupt to the caller.
            # Excluding only KeyboardInterrupt would leave the repro open on its
            # MORE likely path. Nothing inside the harness cancels a dispatch:
            # both runners convert their own wall/idle timeouts into returned
            # results, and the review fan-out gathers with
            # `return_exceptions=True`, so a CancelledError here means shutdown.
            # Re-raised untouched — a swallowed KeyboardInterrupt is a daemon
            # that ignores Ctrl-C, and a swallowed CancelledError is a task that
            # refuses to die.
            raise
        except BaseException as exc:
            # Review C3. A session that dies BEFORE `runner.run` returns RAISES
            # (agent_runner.py, deliberately), so the whole workflow tail —
            # save_session, the retry-counter bump, the follow-up actions — is
            # skipped. The death was therefore unbounded, unbilled and invisible:
            # `Daemon.tick` swallowed, the reaper put the issue back to ai-ready,
            # and the next poll paid for a full session again, forever. Wave A
            # made it VISIBLE (agent_run_start + a settled transport_death
            # journal line); this counter BOUNDS it (decide_fix escalates at
            # cfg.max_infra_deaths). Bump then re-raise UNCHANGED — the crash
            # isolation the raise buys is the contract, not a bug to fix here.
            #
            # `_death_key` (not a bare `-action.number` fallback) picks the key:
            # a failed pre-handler lookup must not silently relocate the counter
            # into a key space the decide side does not read — review P1R-1.
            key = self._death_key(action, issue, resolved)
            # Deterministic deaths (#13) are charged the WHOLE budget at once.
            # The breakers assume a retry might survive; when the cause is the
            # input the retry reproduces it exactly, so a +1 bump buys the same
            # unwinnable dispatch cap-1 more times at full session cost. Charging
            # the cap routes the PR/issue to whatever terminal path the role
            # already has (review: unproductive ceiling → pr_escalation →
            # needs-human-review) on the very next poll — no new label, no new
            # branch on the decide side.
            det = deterministic_death(
                f"{type(exc).__name__}: {exc}",
                str(getattr(exc, "harness_last_trace", "") or ""),
            )
            try:
                if det is not None and self.cfg.max_infra_deaths > 0:
                    charged = max(
                        self.store.get_infra_deaths(key), self.cfg.max_infra_deaths
                    )
                    self.store.set_infra_deaths(key, charged)
                    self.cfg.log(
                        f"#{key}: deterministic session death "
                        f"({det.signature}"
                        + (f" on {det.path}" if det.path else "")
                        + ") — retrying reproduces it, so the retry budget is "
                        f"spent now ({charged}/{self.cfg.max_infra_deaths}); "
                        "escalating to a human on the next poll"
                    )
                else:
                    self.store.bump_infra_deaths(key)
            except Exception as err:  # noqa: BLE001
                # A counter we could not persist must not replace the death we
                # are trying to report (the traceback is the diagnosis).
                self.cfg.log(f"infra-death counter not persisted for #{key}: {err}")
            raise
