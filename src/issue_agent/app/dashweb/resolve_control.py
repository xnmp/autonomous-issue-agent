"""Shared operator-control wire helpers (the resolve projection itself was
deleted with the fix workflow in refactor slice 5b)."""


from ...domain import continuation_components as cc
from . import resolve_contracts as wire


def control_error(error: BaseException) -> str:
    return str(error).replace("\x00", "").encode("utf-8", errors="replace").decode("utf-8")[:16384] or type(error).__name__


def unavailable_control(error: BaseException) -> wire.UnavailableControl:
    """A failed authority read is unavailable, never an empty or absent gate set."""
    return wire.UnavailableControl(status="unavailable", reason=control_error(error))


def unavailable_command(error: BaseException) -> wire.CommandResult:
    """The stable request identity makes an uncertain response safe to replay."""
    return wire.CommandResult(outcome="unavailable", reason=control_error(error),
                              control=wire.UnavailableControl(status="unavailable", reason="control read unavailable"))


def recovery_control(selection: cc.IssueRecoverySelection | cc.PRRepairSelection) -> wire.RecoveryControl:
    if isinstance(selection, (cc.InactiveIssueRecovery, cc.InactivePRRepair)):
        return wire.RecoveryUnavailable(status="inactive", reason="Fix recovery authority is inactive.")
    component = selection.component
    state = component.state
    if isinstance(state, cc.IssueRecoveryState):
        return wire.IssueRecoveryAvailable(
            status="available", family="issue_recovery", generation=selection.revision.fence.generation,
            version=selection.revision.version, resumes=state.resumes, ci_failures=state.ci_failures,
            review_failures=state.review_failures.count, rebase_failures=state.rebase_failures,
            holds=["workflow"] if component.gates else [])
    return wire.PRRepairAvailable(
        status="available", family="pr_repair", generation=selection.revision.fence.generation,
        version=selection.revision.version, resumes=state.resumes, workflow_held=bool(component.gates),
        pending_effects=[name for name in ("ci_rerun", "ci_cancel", "branch_refresh", "pending_ci_escalation")
                        if isinstance(getattr(state, name), (cc.OwedCIRemediation, cc.OwedPRRecoveryEffect))])
