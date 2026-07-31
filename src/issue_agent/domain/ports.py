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
from .events import Correlation, EventEnvelope, JsonValue, TelemetryEvent
from .contracts import (
    BudgetScope,
    CommandId,
    EffectReceipt,
    LifecycleCommand,
    Money,
    PriceQuote,
    ReservationId,
    RunId,
)
from .world import IssueView, PRView, PullRequestDiff, WorldState

T = TypeVar("T")


@runtime_checkable
class GitHubReads(Protocol):
    """Authoritative GitHub observations; transport failure raises."""

    def issue_for_pr(self, pr: int) -> int | None: ...
    def has_label(self, kind: str, number: int, label: str) -> bool: ...
    def has_comment(self, kind: str, number: int, body: str) -> bool: ...
    def is_issue_closed(self, issue: int) -> bool: ...


class RecentlyMergedRef(Protocol):
    issue: int
    title: str
    pr: int
    merged_at: str


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
    def pr_for_issue(self, issue: int) -> int | None: ...
    def pr_diff(
        self, pr: int, base_oid: str, head_oid: str
    ) -> PullRequestDiff: ...
    def build_world(self) -> WorldState: ...
    def review_comments(self, pr: int) -> str: ...
    def failed_run_log(self, pr: int, *, branch: str) -> str: ...
    def recently_merged_issues(
        self, author: str, limit: int = 30
    ) -> tuple[RecentlyMergedRef, ...]: ...


class GitHubEffects(Protocol):
    """Narrow privileged effect port.

    Commands are idempotent within ``idempotency_key`` scope.  An ambiguous
    remote response returns an UNKNOWN_OUTCOME receipt and is reconciled before
    retry.
    """

    def execute_command(self, command: LifecycleCommand) -> EffectReceipt: ...
    def reconcile_command(self, command_id: CommandId) -> EffectReceipt | None: ...


class WorkflowState(Protocol):
    """Durable workflow state.

    Missing scalar values use their documented empty/zero value. Read,
    corruption, and write failures propagate and fail the current workflow;
    callers never treat them as an absent marker.
    """

    def get_branch(self, issue: int) -> str: ...
    def save_branch(self, issue: int, branch: str) -> None: ...
    def get_session(self, issue: int) -> str: ...
    def save_session(self, issue: int, session_id: str) -> None: ...
    def get_model(self, issue: int) -> str: ...
    def save_model(self, issue: int, model: str | None) -> None: ...
    def save_predicted_paths(
        self, issue: int, paths: frozenset[str]
    ) -> None: ...
    def bump_resumes(self, issue: int) -> int: ...
    def set_resumed_at(self, issue: int, at: str) -> None: ...
    def set_pr_resumed_at(self, pr: int, at: str) -> None: ...
    def get_ci_retries(self, issue: int) -> int: ...
    def set_ci_retries(self, issue: int, retries: int) -> None: ...
    def get_resolve_retries(self, issue: int) -> int: ...
    def set_resolve_retries(self, issue: int, retries: int) -> None: ...
    def get_rebase_retries(self, issue: int) -> int: ...
    def set_rebase_retries(self, issue: int, retries: int) -> None: ...
    def get_review_retry(self, issue: int) -> tuple[str, int]: ...
    def set_review_retry(self, issue: int, at: str, retries: int) -> None: ...
    def get_phase(self, issue: int) -> str: ...
    def set_phase(self, issue: int, phase: str) -> None: ...
    def get_pr_session(self, pr: int) -> str: ...
    def save_pr_session(self, pr: int, session_id: str) -> None: ...
    def get_last_sha(self, pr: int) -> str: ...
    def set_last_sha(self, pr: int, sha: str) -> None: ...
    def get_last_patch_id(self, pr: int) -> str: ...
    def set_last_patch_id(self, pr: int, patch_id: str) -> None: ...
    def get_round(self, pr: int) -> int: ...
    def set_round(self, pr: int, round_number: int) -> None: ...
    def get_infra_deaths(self, key: int) -> int: ...
    def bump_infra_deaths(self, key: int) -> int: ...
    def set_infra_deaths(self, key: int, count: int) -> None: ...
    def clear_infra_deaths(self, key: int) -> None: ...
    def set_triage_at(self, issue: int, at: str) -> None: ...
    def set_groom_at(self, issue: int, at: str) -> None: ...
    def set_groom_content(self, issue: int, content: str) -> None: ...
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
    def submit_review(
        self, pr: int, event: str, body: str, expected_head: str
    ) -> bool: ...


class LegacyJournal(Protocol):
    """Read-only locator retained solely for historical JSONL conversion."""

    path: str
    canonical: bool


@runtime_checkable
class WorktreePort(Protocol):
    """Local checkout ownership.

    ``branch_for_issue`` returns ``None`` only when no checkout exists. Invalid
    or unsafe paths and command failures raise; callers must not infer absence.
    """

    def ensure(self, issue: int, branch: str) -> str: ...
    def remove(self, issue: int) -> None: ...
    def branch_for_issue(self, issue: int, fallback: str) -> str | None: ...


class FixWorktreePort(WorktreePort, Protocol):
    def reset(self, issue: int) -> None: ...


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
    run_id: RunId


class RecoveryDisposition(Protocol):
    value: str


class RecoveryRelease(Protocol):
    disposition: RecoveryDisposition


class RecoveryControl(Protocol):
    """Operational subset used by boot/suspend recovery.

    Store corruption/unavailability raises and blocks admission/startup.
    Missing leases/reservations are normal empty results.
    """

    def reclaim_dead_leases(
        self, owner_id: str, is_alive: Callable[[int, int], bool]
    ) -> tuple[RunId, ...]: ...
    def active_reservation_runs(self, owner_id: str) -> tuple[RunId, ...]: ...
    def stale_active_reservation_runs(
        self, owner_id: str, created_before: datetime
    ) -> tuple[RunId, ...]: ...
    def leases(self, owner_id: str) -> tuple[RecoveryLease, ...]: ...
    def release(self, run_id: RunId, reason: str) -> RecoveryRelease: ...
    def pending_commands(self) -> tuple[object, ...]: ...


class SpendReconciler(Protocol):
    """Settle exact surviving provider evidence; ``False`` means indeterminate.

    Provider or store failures propagate so recovery cannot release capacity
    after an unverified attempt.
    """

    def reconcile(self, run_id: RunId) -> bool: ...


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
class CiPendingState(Protocol):
    """Read the escalated head; empty means absent, storage failures raise."""
    def get_ci_pending_escalated_head(self, pr: int) -> str: ...


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
    def failed_run_log(self, pr: int, *, branch: str) -> str: ...
