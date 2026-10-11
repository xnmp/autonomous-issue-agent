"""Hexagonal ports owned by the core.

Failure contracts are explicit:

* read ports raise their typed unavailable error; absence is a normal value;
* write ports execute only durable, idempotency-keyed commands;
* spend and control-store ports return typed dispositions and fail closed;
* telemetry sinks may drop events but are never queried for authority.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from typing import Protocol, TypeVar, runtime_checkable

from .actions import Action
from .ci_history import CiAttempt
from .events import Correlation, EventEnvelope, JsonValue, TelemetryEvent
from .contracts import (
    BudgetScope,
    LifecycleCommand,
    Money,
    PriceQuote,
    ReservationId,
    RunId,
)
from .world import IssueView, PRView, PullRequestDiff, WorldState
from .resolve_split import ContractRevision, ContractResetResult
from .resolve_workspace import ObservedResolveBase, EpisodeWorkspaceHandoff, EpisodeWorkspacePrepared

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


class RecentlyMergedRef(Protocol):
    @property
    def issue(self) -> int: ...
    @property
    def title(self) -> str: ...
    @property
    def pr(self) -> int: ...
    @property
    def merged_at(self) -> str: ...


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
    def pr_commit_messages(self, pr: int) -> tuple[str, ...]: ...
    def build_world(self) -> WorldState: ...
    def review_comments(self, pr: int) -> str: ...
    def failed_run_log(self, head: str, run_ids: tuple[int, ...]) -> str: ...
    def recently_merged_issues(
        self, author: str, limit: int = 30
    ) -> tuple[RecentlyMergedRef, ...]: ...


class WorkflowState(Protocol):
    """Advisory repair context and the retained Jira workflow path.

    Selected lifecycle continuity uses typed SQL workflow control. Jira alone
    retains its existing infrastructure counter; malformed counter reads fail
    that path. CI history, predictions and Jira mirrors may return empty context.
    """

    def save_predicted_paths(
        self, issue: int, paths: frozenset[str]
    ) -> None: ...
    # Missing bounded context does not change admission authority.
    def get_ci_history(self, issue: int) -> tuple[CiAttempt, ...]: ...
    def append_ci_attempt(
        self, issue: int, attempt: CiAttempt
    ) -> tuple[CiAttempt, ...]: ...
    def get_infra_deaths(self, key: int) -> int: ...
    def bump_infra_deaths(self, key: int) -> int: ...
    def set_infra_deaths(self, key: int, count: int) -> None: ...
    def get_jira_key(self, issue: int) -> str: ...
    def set_jira_key(self, issue: int, key: str) -> None: ...
    def get_jira_phase(self, issue: int) -> str: ...
    def set_jira_phase(self, issue: int, phase: str) -> None: ...


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


class LegacyJournal(Protocol):
    """Read-only locator retained solely for historical JSONL conversion."""

    @property
    def path(self) -> str: ...
    @property
    def canonical(self) -> bool: ...


@runtime_checkable
class WorktreePort(Protocol):
    """Local checkout ownership.

    ``branch_for_issue`` returns ``None`` only when no checkout exists. Invalid
    or unsafe paths and command failures raise; callers must not infer absence.
    """

    def ensure(self, issue: int, branch: str) -> str: ...
    def remove(self, issue: int) -> None: ...
    def branch_for_issue(self, issue: int, fallback: str, *, preferred: str = "") -> str | None: ...


class FixWorktreePort(WorktreePort, Protocol):
    def reset_contract(self, issue: int, revision: "ContractRevision", intent_id: str) -> "ContractResetResult": ...

    def observe_and_pin_episode_base(self, repository: str, *, observed_at: datetime, approved_host: str = "github.com") -> ObservedResolveBase: ...

    def prepare_episode_workspace(self, handoff: EpisodeWorkspaceHandoff, *, approved_host: str = "github.com") -> ContractResetResult: ...

    def verify_episode_workspace(self, completed: EpisodeWorkspacePrepared, *, approved_host: str = "github.com") -> None: ...

    def episode_workspace_path(self, completed: EpisodeWorkspacePrepared) -> str: ...


class EventSink(Protocol):
    """Best-effort typed telemetry only; never a control-state reader.

    Implementations contain write failures and may drop an event. The method
    has no success result because telemetry cannot authorize later behavior.
    """

    def emit(self, event: TelemetryEvent) -> None: ...


class InfraBreakerPort(Protocol):
    """Deployment-wide infra circuit breaker, as a workflow sees it.

    Write-only from the workflow's side: a leg reports what happened to it and
    never asks whether admission is open. The admission question is the tick
    gate's, so a leg already in flight when the breaker trips still finishes
    and settles its spend (`app.infra_breaker.InfraBreaker`).
    """

    def record_death(self, issue: int, leg: str = "", reason: str = "") -> None: ...
    def record_success(self) -> None: ...


class Clock(Protocol):
    """Injected wall/monotonic time; invalid sleeps raise ``ValueError``."""

    def now(self) -> datetime: ...
    def monotonic_ns(self) -> int: ...
    def sleep(self, seconds: float) -> None: ...


class AccountingStatus(str):
    AVAILABLE = "available"
    EXHAUSTED = "exhausted"
    UNAVAILABLE = "unavailable"
    CORRUPT = "corrupt"
    CONFLICT = "conflict"


class SpendAccount(Protocol):
    """Transactional paid-admission authority.

    Every mutation is idempotent. Expected exhaustion/unavailability/conflict
    is returned as a typed disposition; malformed requests raise before I/O.
    """

    def snapshot(self, scope: BudgetScope) -> object: ...
    def reserve(
        self,
        *,
        run_id: RunId,
        scope: BudgetScope,
        quote: PriceQuote,
        cap: Money,
    ) -> object: ...
    def settle(self, *, run_id: RunId, amount: Money, receipt_id: str) -> object: ...
    def release(self, *, run_id: RunId, reason: str) -> object: ...
    def reconcile(self, owner: str) -> object: ...


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


class RunAbandonmentSink(Protocol):
    """Emit a correction for a durable run proven dead by recovery.

    ``False`` means the referenced start was unavailable; telemetry failure
    never changes operational recovery state.
    """

    def abandon_run(self, run_id: RunId, reason: str) -> bool: ...


class WorktreeRecovery(Protocol):
    """Idempotent local checkout recovery; any validation failure is fatal."""

    def prune_validate(self) -> int: ...


class CommandRecovery(Protocol):
    """Reconcile-first remote command recovery; ambiguity stays pending."""

    def drain_pending_commands(self) -> int: ...


class ControlStore(Protocol):
    """Transactional control authority.

    Claims/receipts return ``False`` for an idempotent/conflicting repeat.
    Corruption, schema, and storage failures raise and fail closed.
    """

    def transaction(self) -> AbstractContextManager[object]: ...
    def claim(self, namespace: str, scope: str, key: str) -> bool: ...
    def receipt(
        self, namespace: str, scope: str, key: str, payload: Mapping[str, object]
    ) -> bool: ...
    def flush_outbox(
        self,
        emit: Callable[[EventEnvelope], None],
        limit: int = 100,
        *,
        destination: str | None = None,
    ) -> int: ...


@runtime_checkable
class PendingEffect(Protocol):
    """One durable lifecycle command that is still owed to GitHub."""

    @property
    def command(self) -> LifecycleCommand: ...
    @property
    def state(self) -> str: ...


@runtime_checkable
class PendingEffectReads(Protocol):
    """Read side of the durable lifecycle-command queue (ADR 0012).

    Answers exactly one question — "is this remote write still owed?" — so a
    caller can tell a write that was DEFERRED to the durable outbox from one
    that was never committed at all. It never authorizes an effect and never
    substitutes for the executor's own result. Optional: a composition without
    it must treat an unconfirmed write as lost, which is the conservative
    reading.
    """

    def pending_commands(
        self,
        *,
        repository: str | None = None,
        executor_identity: str | None = None,
    ) -> tuple[PendingEffect, ...]: ...


@runtime_checkable
class LevelState(Protocol):
    """Restart-safe level memory; corrupt/unavailable reads fail closed."""

    def load_level_state(
        self, namespace: str, scope: str
    ) -> Mapping[str, Mapping[str, JsonValue]]: ...

    def replace_level_state(
        self,
        namespace: str,
        scope: str,
        values: Mapping[str, Mapping[str, JsonValue]],
    ) -> None: ...


class WorldObservationStore(Protocol):
    """Persist committed observation hashes.

    ``False`` is an idempotent unchanged observation. Storage failures raise;
    they are not interpreted as a changed or empty world.
    """

    def record_world_observation(
        self,
        *,
        deployment: str,
        subject_kind: str,
        subject_number: int,
        content_hash: str,
        facts: Mapping[str, JsonValue],
        correlation: Correlation,
    ) -> bool: ...

    def record_world_snapshot(
        self,
        *,
        deployment: str,
        subject_kind: str,
        observations: Mapping[
            int,
            tuple[str, Mapping[str, JsonValue], Correlation],
        ],
    ) -> int: ...

    def retire_projection_feed(
        self,
        *,
        deployment: str,
        repository: str,
        role: str,
    ) -> int: ...


@runtime_checkable
class PredictionReader(Protocol):
    """Read path prediction; empty means absent, storage failures raise."""
    def get_predicted_paths(self, issue: int) -> frozenset[str]: ...


@runtime_checkable
class IssueStateReasonReads(Protocol):
    """Read GitHub state reason; transport failures raise."""
    def issue_state_reason(self, issue: int) -> str: ...


@runtime_checkable
class RecentMergesReads(Protocol):
    """Read recent merges; transport failure raises, no matches is empty."""
    def recently_merged_issues(self, author: str) -> tuple[object, ...]: ...


@runtime_checkable
class ReviewPromptReads(Protocol):
    """Read review context; transport failure raises."""
    def review_comments(self, pr: int) -> str: ...


@runtime_checkable
class CiPromptReads(Protocol):
    """Read CI failure context; transport failure raises."""
    def failed_run_log(self, head: str, run_ids: tuple[int, ...]) -> str: ...
