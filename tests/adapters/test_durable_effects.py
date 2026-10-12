from datetime import datetime, timezone
from typing import Any, cast

import pytest

from issue_agent.adapters.control_store import (
    SQLiteControlStore,
)
from issue_agent.adapters.deployment_authority import (
    UnfencedEffectAuthority,
)
from issue_agent.adapters.durable_effects import (
    DurableEffectRunner,
    EffectOutcome,
)
from issue_agent.domain.contracts import (
    CommandId,
    CommandKind,
    EffectStatus,
    LifecycleCommand,
)


NOW = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)


def _command(command_id: str = "effect") -> LifecycleCommand:
    return LifecycleCommand(
        CommandId(command_id),
        f"effect:{command_id}",
        CommandKind.POST_COMMENT,
        "issue:1",
        "revision-1",
        {"body": "done"},
        "cfg",
        "o/r",
        "o/r:fix",
    )


def _runner(tmp_path) -> tuple[DurableEffectRunner, SQLiteControlStore]:
    store = SQLiteControlStore(str(tmp_path / "control.db"), now=lambda: NOW)
    store.initialize()
    return (
        DurableEffectRunner(
            store,
            UnfencedEffectAuthority(),
            executor_identity="o/r:fix",
            now=lambda: NOW,
        ),
        store,
    )


def _reply_lost() -> EffectOutcome[None]:
    raise RuntimeError("reply lost")


def test_runner_commits_executing_before_effect_and_terminal_receipt_after(
    tmp_path,
) -> None:
    runner, store = _runner(tmp_path)
    command = _command()
    runner.enqueue(command)
    observed = []

    def effect() -> EffectOutcome[str]:
        observed.append(store.pending_commands()[0].state)
        return EffectOutcome(
            "remote-value",
            EffectStatus.SUCCEEDED,
            remote_object_id="comment-1",
            remote_revision="revision-2",
        )

    assert runner.execute(command, effect) == "remote-value"

    receipt = store.effect_receipt(command.command_id)
    assert observed == ["executing"]
    assert receipt is not None
    assert receipt.status is EffectStatus.SUCCEEDED
    assert receipt.remote_object_id == "comment-1"
    assert receipt.remote_revision == "revision-2"
    assert store.pending_commands() == ()


def test_interrupted_effect_is_durably_unknown_and_recoverable(tmp_path) -> None:
    runner, store = _runner(tmp_path)
    command = _command()
    runner.enqueue(command)

    def interrupted() -> EffectOutcome[None]:
        raise KeyboardInterrupt("lost reply")

    with pytest.raises(KeyboardInterrupt, match="lost reply"):
        runner.execute(command, interrupted)

    pending = store.pending_commands()[0]
    receipt = store.effect_receipt(command.command_id)
    assert pending.state == EffectStatus.UNKNOWN_OUTCOME.value
    assert receipt is not None
    assert receipt.status is EffectStatus.UNKNOWN_OUTCOME
    assert receipt.error == "KeyboardInterrupt: lost reply"


def test_malformed_effect_outcome_becomes_unknown_instead_of_staying_executing(
    tmp_path,
) -> None:
    runner, store = _runner(tmp_path)
    command = _command()
    runner.enqueue(command)

    with pytest.raises(ValueError, match="requires an error"):
        runner.execute(
            command,
            lambda: EffectOutcome(None, EffectStatus.RETRYABLE_FAILURE),
        )

    assert store.pending_commands()[0].state == (
        EffectStatus.UNKNOWN_OUTCOME.value
    )


def test_null_effect_outcome_becomes_unknown_instead_of_staying_executing(
    tmp_path,
) -> None:
    runner, store = _runner(tmp_path)
    command = _command()
    runner.enqueue(command)

    with pytest.raises(TypeError, match="returned no outcome"):
        runner.execute(command, lambda: cast(Any, None))

    assert store.pending_commands()[0].state == (
        EffectStatus.UNKNOWN_OUTCOME.value
    )
