"""Hexagonal ports owned by the core.

Failure contracts are explicit:

* read ports raise their typed unavailable error; absence is a normal value;
* write ports execute only durable, idempotency-keyed commands;
* spend and control-store ports return typed dispositions and fail closed;
* telemetry sinks may drop events but are never queried for authority.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Protocol, TypeVar, runtime_checkable

from .actions import Action
from .events import TelemetryEvent
from .contracts import RunId
from .world import IssueView, PRView, PullRequestDiff

T = TypeVar("T")


class WorkflowControlError(ValueError):
    """A present control record is unreadable or invalid, not an unused allowance."""

    def __init__(self, path: str, reason: str):
        self.path = path
        self.reason = reason
        super().__init__(f"Workflow control unavailable at {path}: {reason}")


@runtime_checkable
class GitHubReads(Protocol):
    """Authoritative GitHub observations; transport failure raises."""

    def issue_for_pr(self, pr: int) -> int | None: ...
    def has_label(self, kind: str, number: int, label: str) -> bool: ...
    def has_comment(self, kind: str, number: int, body: str) -> bool: ...
    def is_issue_closed(self, issue: int) -> bool: ...


@runtime_checkable
class WorkflowGitHubReads(GitHubReads, Protocol):
    """Read surface required by paid workflows.

    Missing optional enrichment is represented by an empty value. A transport
    failure raises the adapter's typed unavailable exception and aborts the
    observation; it is never converted to an empty world.
    """

    def get_issue(self, number: int) -> IssueView: ...
    def get_pr(
        self, number: int, *, mergeability: bool = True, checks: bool = True
    ) -> PRView: ...
    def pr_for_issue(self, issue: int, *, require_unique: bool = False) -> int | None: ...
    def pr_diff(
        self, pr: int, base_oid: str, head_oid: str
    ) -> PullRequestDiff: ...
    def review_comments(self, pr: int) -> str: ...
    def failed_run_log(self, head: str, run_ids: tuple[int, ...]) -> str: ...

class WorkflowState(Protocol):
    """Advisory repair context and the dispatcher infrastructure counter.

    Selected lifecycle continuity uses typed SQL workflow control. CI history
    and predictions may return empty context.
    """

    def save_predicted_paths(
        self, issue: int, paths: frozenset[str]
    ) -> None: ...


class ActionExecutor(Protocol):
    """Durable privileged effects.

    ``False`` means at least one effect remains non-terminal. Transport and
    ambiguous outcomes are persisted/reconciled by the implementation rather
    than exposed as a successful return.
    """

    def execute_all(self, actions: Iterable[Action]) -> bool: ...
    def replay_pending(self) -> bool: ...
    def submit_review(
        self, pr: int, event: str, body: str, expected_head: str
    ) -> bool: ...



class EventSink(Protocol):
    """Best-effort typed telemetry only; never a control-state reader.

    Implementations contain write failures and may drop an event. The method
    has no success result because telemetry cannot authorize later behavior.
    """

    def emit(self, event: TelemetryEvent) -> None: ...


class Clock(Protocol):
    """Injected wall/monotonic time; invalid sleeps raise ``ValueError``."""

    def now(self) -> datetime: ...
    def monotonic_ns(self) -> int: ...
    def sleep(self, seconds: float) -> None: ...


class ProcessSupervisor(Protocol):
    """Durable process-group ownership.

    ``supervised_command`` registers the group in the child before paid work is
    executed. ``kill_tree`` is idempotent; an unknown/already-dead group is
    success.
    """

    def supervised_command(
        self, run_id: RunId, argv: tuple[str, ...] | list[str]
    ) -> tuple[str, ...]: ...
    def supervised_cli(
        self, run_id: RunId, executable: str
    ) -> tuple[str, dict[str, str]]: ...
    def unregister(self, run_id: RunId) -> None: ...
    def kill_tree(self, run_id: RunId, grace_seconds: float = 10.0) -> None: ...
    def recover_orphans(self) -> tuple[RunId, ...]: ...
    def recover_abandoned_orphans(self) -> tuple[RunId, ...]: ...


class RecoveryLease(Protocol):
    @property
    def run_id(self) -> RunId: ...


class RecoveryDisposition(Protocol):
    @property
    def value(self) -> str: ...


class RecoveryRelease(Protocol):
    @property
    def disposition(self) -> RecoveryDisposition: ...


class RecoveryControl(Protocol):
    """Operational subset used by boot/suspend recovery.

    Store corruption/unavailability raises and blocks admission/startup.
    Missing leases/reservations are normal empty results.
    """

    def settle_terminal_estimate(self, run_id: RunId, reason: str) -> bool: ...

    def reclaim_dead_leases(
        self, owner_id: str, is_alive: Callable[[int, int], bool]
    ) -> tuple[RunId, ...]: ...
    def active_reservation_runs(self, owner_id: str) -> tuple[RunId, ...]: ...
    def stale_active_reservation_runs(
        self, owner_id: str, created_before: datetime
    ) -> tuple[RunId, ...]: ...
    def leases(self, owner_id: str) -> tuple[RecoveryLease, ...]: ...
    def release_unlaunched(
        self, run_id: RunId, reason: str
    ) -> RecoveryRelease: ...
    def release_retained(
        self,
        run_id: RunId,
        reason: str,
        retained_before: datetime,
    ) -> RecoveryRelease: ...
    def pending_commands(self) -> tuple[object, ...]: ...


class SpendReconciler(Protocol):
    """Settle exact surviving provider evidence; ``False`` means indeterminate.

    Provider or store failures propagate so recovery cannot release capacity
    after an unverified attempt.
    """

    def reconcile(self, run_id: RunId) -> bool: ...


class StaleReservationReconciler(Protocol):
    """Run the standing reservation recovery sweep on demand.

    Admission uses this to reclaim headroom held by provably dead runs before
    escalating a budget trip to a human. It grants no new release authority:
    implementations must delegate to the same recovery pass boot, wake, and
    periodic reconciliation already run, under identical eligibility rules.

    Returns the number of runs the sweep left without an active reservation.
    """

    def reconcile_stale_reservations(self) -> int: ...


class WorktreeRecovery(Protocol):
    """Idempotent local checkout recovery; any validation failure is fatal."""

    def prune_validate(self) -> int: ...


class CommandRecovery(Protocol):
    """Reconcile-first remote command recovery; ambiguity stays pending."""

    def drain_pending_commands(self) -> int: ...


