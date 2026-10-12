"""Shared lifecycle coordinator for durable privileged effects."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Generic, Protocol, TypeVar

from ..domain.contracts import (
    CommandId,
    EffectReceipt,
    EffectStatus,
    LifecycleCommand,
)
from .control_store import PendingCommand
from .deployment_authority import EffectAuthority

T = TypeVar("T")


class EffectControl(Protocol):
    def enqueue_command(self, command: LifecycleCommand) -> PendingCommand: ...

    def enqueue_command_occurrence(
        self, command: LifecycleCommand
    ) -> PendingCommand: ...

    def begin_command(self, command_id: CommandId) -> None: ...

    def retry_reconciled_command(self, command_id: CommandId) -> None: ...

    def resolve_unknown_effect(self, receipt: EffectReceipt) -> bool: ...

    def record_effect(self, receipt: EffectReceipt) -> bool: ...

    def exhaust_command(self, receipt: EffectReceipt) -> bool: ...


@dataclass(frozen=True)
class EffectOutcome(Generic[T]):
    value: T
    status: EffectStatus
    remote_object_id: str | None = None
    remote_revision: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, EffectStatus):
            raise TypeError("effect outcome status must use EffectStatus")
        if self.status is EffectStatus.SUCCEEDED and self.error is not None:
            raise ValueError("a successful effect outcome cannot carry an error")
        if self.status is not EffectStatus.SUCCEEDED and not self.error:
            raise ValueError("an unsuccessful effect outcome requires an error")


class DurableEffectRunner:
    """Own command transitions, receipts, and deployment authority ordering."""

    def __init__(
        self,
        control: EffectControl,
        authority: EffectAuthority,
        *,
        executor_identity: str,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not executor_identity:
            raise ValueError("durable effect executor identity is required")
        self.control = control
        self.authority = authority
        self.executor_identity = executor_identity
        self.now = now

    def enqueue(self, command: LifecycleCommand) -> PendingCommand:
        return self.control.enqueue_command(command)

    def enqueue_occurrence(
        self, command: LifecycleCommand
    ) -> PendingCommand:
        return self.control.enqueue_command_occurrence(command)

    def authorized(self, operation: Callable[[], T]) -> T:
        """Run a remote mutation that belongs to an existing command."""

        with self.authority.effect():
            return operation()

    def execute(
        self,
        command: LifecycleCommand,
        operation: Callable[[], EffectOutcome[T]],
    ) -> T:
        """Begin, execute, verify, and receipt one effect under authority."""

        with self.authority.effect():
            self.control.begin_command(command.command_id)
            return self._perform(command, operation)

    def execute_reconciled(
        self,
        command: LifecycleCommand,
        begin: Callable[[], bool],
        operation: Callable[[], EffectOutcome[T]],
    ) -> T | None:
        """One exclusive span for an uncertain command's reads, spend and write.

        `begin` reads and records (or commits a grant's consumption); False
        means no write is owed. While a live attempt holds the command's span
        another drain neither reads nor spends it, and gets None.
        """

        with self.authority.effect(), self.authority.command(str(command.command_id)) as held:
            if not held or not begin():
                return None
            return self._perform(command, operation)

    def _perform(
        self,
        command: LifecycleCommand,
        operation: Callable[[], EffectOutcome[T]],
    ) -> T:
        started = self.now()
        try:
            outcome = operation()
            receipt = self._outcome_receipt(command, started, outcome)
        except BaseException as exc:
            self._commit_receipt(
                self._receipt(
                    command,
                    started,
                    EffectStatus.UNKNOWN_OUTCOME,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            raise
        self._commit_receipt(receipt)
        return outcome.value

    def record_existing(
        self,
        command: LifecycleCommand,
        outcome: EffectOutcome[T],
        *,
        started: datetime | None = None,
    ) -> T:
        """Receipt an already-executing command without beginning it again."""

        with self.authority.effect():
            receipt = self._outcome_receipt(
                command,
                started or self.now(),
                outcome,
            )
            self._commit_receipt(receipt)
        return outcome.value

    def resolve_unknown(
        self,
        command: LifecycleCommand,
        outcome: EffectOutcome[T],
    ) -> T:
        with self.authority.effect():
            now = self.now()
            receipt = self._outcome_receipt(command, now, outcome)
            if receipt.status is not EffectStatus.SUCCEEDED:
                raise ValueError("resolved unknown effects must be successful")
            # False = another actor settled the same success first. The caller's
            # answer is unchanged either way: the remote effect is applied, and
            # nothing further is owed. Only a state that cannot hold a verified
            # success raises out of the store.
            self.control.resolve_unknown_effect(receipt)
        return outcome.value

    def exhaust(self, command: LifecycleCommand, error: str) -> bool:
        """Retire a command whose retry ceiling is spent. True iff this call
        performed the transition (so the caller escalates exactly once)."""

        if not error:
            raise ValueError("an exhausted command requires an error")
        with self.authority.effect():
            now = self.now()
            return self.control.exhaust_command(
                self._receipt(
                    command,
                    now,
                    EffectStatus.PERMANENT_FAILURE,
                    error=error,
                )
            )

    def retry_reconciled(self, command: LifecycleCommand) -> None:
        with self.authority.effect():
            self.control.retry_reconciled_command(command.command_id)

    def _commit_receipt(self, receipt: EffectReceipt) -> None:
        if not self.control.record_effect(receipt):
            raise RuntimeError(
                f"conflicting durable effect receipt: {receipt.command_id}"
            )

    def _outcome_receipt(
        self,
        command: LifecycleCommand,
        started: datetime,
        outcome: EffectOutcome[T],
    ) -> EffectReceipt:
        if not isinstance(outcome, EffectOutcome):
            raise TypeError("durable effect operation returned no outcome")
        return self._receipt(
            command,
            started,
            outcome.status,
            remote_object_id=outcome.remote_object_id,
            remote_revision=outcome.remote_revision,
            error=outcome.error,
        )

    def _receipt(
        self,
        command: LifecycleCommand,
        started: datetime,
        status: EffectStatus,
        *,
        remote_object_id: str | None = None,
        remote_revision: str | None = None,
        error: str | None = None,
    ) -> EffectReceipt:
        return EffectReceipt(
            command.command_id,
            self.executor_identity,
            started,
            self.now(),
            status,
            remote_object_id=remote_object_id,
            remote_revision=remote_revision,
            error=error,
        )
