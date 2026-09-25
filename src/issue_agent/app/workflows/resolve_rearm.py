"""Compose durable resolve re-arm observations and workspace obligations (ADR 0015)."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
import sqlite3

from issue_agent.adapters.effects import command_for_action
from issue_agent.adapters.workflow_rows import WorkflowRevisionConflict
from issue_agent.domain.ports import FixWorktreePort, WorkflowGitHubReads
from issue_agent.domain.refs import slugify
from issue_agent.domain.resolve_rearm import UnavailableResolveRearm
from issue_agent.domain.resolve_workspace import EpisodeWorkspacePrepared
from issue_agent.domain.workflow import (
    AuthorizeAbsentRetry, CancelRearm, ContinueGrantedRearm, LinkedPrPresent, NoLinkedPr, ObservedIssue,
    OperatorActor, PendingRearm, RequestRearm, WorkflowCommitted, WorkflowConflict,
    WorkflowRejected, WorkflowRejection, WorkflowSnapshot, WorkflowUnchanged,
)
from issue_agent.domain.workflow_identity import WorkflowBinding, WorkflowKey, WorkflowRevision
from issue_agent.domain.workflow_policy import (
    continue_resolve_rearm, request_resolve_rearm, resolve_rearm_projection,
)
from issue_agent.domain.workflow_ports import WorkflowControl


def _no_external_holds(issue: ObservedIssue) -> tuple[str, ...]:
    return ()


@dataclass(frozen=True)
class ResolveRearmWorkflow:
    control: WorkflowControl
    gateway: WorkflowGitHubReads
    worktrees: FixWorktreePort
    now: Callable[[], datetime]
    log: Callable[[str], None]
    approved_host: str = "github.com"
    external_holds: Callable[[ObservedIssue], tuple[str, ...]] = _no_external_holds

    def _observe(self, key: WorkflowKey) -> tuple[ObservedIssue, NoLinkedPr | LinkedPrPresent, tuple[str, ...]]:
        issue = ObservedIssue(key, self.gateway.get_issue(key.target.number), self.now())
        pr = self.gateway.pr_for_issue(key.target.number)
        linkage = (
            NoLinkedPr(key, self.now()) if pr is None else
            LinkedPrPresent(key, pr, self.gateway.get_pr(pr).head_ref_oid, self.now())
        )
        return issue, linkage, self.external_holds(issue)

    def submit(
        self, command: RequestRearm | ContinueGrantedRearm | CancelRearm | AuthorizeAbsentRetry,
        actor: OperatorActor, *, binding: WorkflowBinding | None = None,
    ) -> WorkflowCommitted | WorkflowConflict | WorkflowRejected:
        if not isinstance(command, (RequestRearm, ContinueGrantedRearm, CancelRearm, AuthorizeAbsentRetry)) or not isinstance(actor, OperatorActor):
            return WorkflowRejected(WorkflowRejection.INVALID_SCOPE, "re-arm requires a typed command and authenticated operator")
        if binding is not None and (not isinstance(binding, WorkflowBinding) or binding.fence != command.expected.fence):
            return WorkflowRejected(WorkflowRejection.INVALID_SCOPE, "re-arm binding disagrees with its expected revision")
        key = command.expected.fence.key
        try:
            if isinstance(command, CancelRearm):
                # Cancellation owns its barrier so canonical replay precedes CAS.
                return self.control.cancel_rearm(command, actor, binding=binding)
            if isinstance(command, AuthorizeAbsentRetry):
                # Evidence is the harness's own durable read; no remote I/O
                # here, and the grant executes only through the fix drain.
                return self.control.authorize_retry(command, actor, binding=binding)
            accepted = self.control.accepted_rearm(key, command.request_id)
            if accepted is not None:
                current = self.control.load_workflow(key)
                if not isinstance(current, WorkflowSnapshot):
                    raise ValueError("accepted re-arm workflow is unavailable")
                if accepted.command != command or accepted.actor.subject != actor.subject:
                    return WorkflowConflict(current.revision, "request identity was reused with different content or operator")
                if binding is not None:
                    intent = self.control.lookup_rearm(key, command.request_id)
                    from issue_agent.domain.workflow import ResolveRearmAcceptance

                    accepted_binding = (accepted.preflight.workspace.source
                                        if isinstance(accepted.preflight, ResolveRearmAcceptance)
                                        else WorkflowBinding(command.expected.fence, intent.handoff.destination.authority, intent.handoff.destination.episode_id)
                                        if intent is not None else None)
                    if binding != accepted_binding:
                        return WorkflowConflict(current.revision, "request replay names another family epoch or episode")
                return WorkflowCommitted(current, idempotent=True)
            with self.control.rearm_authority(command.expected, binding=binding) as current:
                issue, linkage, holds = self._observe(key)
                if isinstance(command, RequestRearm):
                    fallback = f"fix/{key.target.number}-{slugify(issue.issue.title) or 'issue'}"
                    branch = self.worktrees.branch_for_issue(key.target.number, fallback)
                    if branch is None or branch == "":
                        branch = fallback
                    base = self.worktrees.observe_and_pin_episode_base(
                        key.repository, observed_at=self.now(), approved_host=self.approved_host,
                    )
                    workspace = self.control.observe_workspace(current.binding, base, branch)
                    proposal = request_resolve_rearm(
                        current, command, actor, issue, linkage, now=self.now(),
                        active_attempts=(), external_holds=holds, workspace=workspace,
                    )
                else:
                    proposal = continue_resolve_rearm(
                        current, command, actor, issue, linkage, now=self.now(),
                        active_attempts=(), external_holds=holds,
                    )
                if isinstance(proposal, WorkflowRejected):
                    return proposal
                if isinstance(proposal, WorkflowUnchanged):
                    return WorkflowCommitted(proposal.snapshot, idempotent=True)
                assert isinstance(proposal.snapshot.intent, PendingRearm)
                action = resolve_rearm_projection(proposal.snapshot, issue.issue, external_holds=holds)
                projection = command_for_action(
                    action, repository=key.repository, executor_identity=self.control.writer.owner_id,
                )
                assert projection is not None
                projection = replace(
                    projection, command_id=proposal.snapshot.intent.required_commands[0],
                    idempotency_key=f"workflow:{command.request_id}",
                )
                return self.control.commit_workflow(
                    command.expected, (current.authority,), replace(proposal, commands=(projection,)),
                )
        except WorkflowRevisionConflict as exc:
            return WorkflowConflict(exc.current, str(exc))
        except sqlite3.Error:
            raise
        except Exception as exc:
            self.log(f"resolve_rearm_submit_unavailable issue={key.target.number}: {exc}")
            return WorkflowRejected(WorkflowRejection.OBSERVATION_UNAVAILABLE, _error(exc))

    def reconcile(self) -> int:
        progressed = 0
        for candidate in self.control.pending_rearms(limit=32):
            if isinstance(candidate, UnavailableResolveRearm):
                self.log(f"resolve_rearm_unavailable request={candidate.request_id}: {candidate.reason}")
                continue
            key = candidate.snapshot.revision.fence.key
            observed_revision: WorkflowRevision | None = None
            try:
                with self.control.rearm_authority(candidate.snapshot.revision) as current:
                    observed_revision = current.revision
                    issue, linkage, holds = self._observe(key)
                    result = self.control.reconcile_rearm(
                        current.revision, issue, linkage, external_holds=holds,
                    )
                    if isinstance(result, WorkflowConflict):
                        raise ValueError(result.reason)
                    current = result.snapshot
                    observed_revision = current.revision
                    if not isinstance(current.intent, PendingRearm):
                        progressed += 1
                        continue
                    # The same exclusive authority covers both Git and its fresh
                    # postcondition; no SQL transaction or remote write spans it.
                    prepared = self.control.prepared_workspace(current.revision)
                    if prepared is None:
                        reset = self.worktrees.prepare_episode_workspace(
                            current.intent.handoff, approved_host=self.approved_host,
                        )
                        prepared = self.control.complete_workspace_handoff(EpisodeWorkspacePrepared(
                            current.intent.handoff, current.binding, reset, self.now(),
                        ))
                    else:
                        self.worktrees.verify_episode_workspace(prepared, approved_host=self.approved_host)
                    issue, linkage, holds = self._observe(key)
                    result = self.control.reconcile_rearm(
                        current.revision, issue, linkage, external_holds=holds,
                        workspace_verified=prepared,
                    )
                    if isinstance(result, WorkflowConflict):
                        raise ValueError(result.reason)
                    observed_revision = result.snapshot.revision
                    progressed += 1
            except sqlite3.Error:
                raise
            except Exception as exc:
                self.log(f"resolve_rearm_recovery_unavailable issue={key.target.number}: {exc}")
                if observed_revision is None:
                    continue
                try:
                    # A later owner's revision did not make this failed read.
                    # Deferral's CAS must reject concurrent authority changes.
                    self.control.defer_rearm(observed_revision, _error(exc))
                except sqlite3.Error:
                    raise
                except Exception as defer_error:
                    self.log(f"resolve_rearm_defer_unavailable issue={key.target.number}: {defer_error}")
        return progressed


def _error(exc: Exception) -> str:
    return (str(exc).replace("\x00", "") or type(exc).__name__)[:16384]
