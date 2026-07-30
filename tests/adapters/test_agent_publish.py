from pathlib import Path

import pytest

from issue_agent.adapters.agent_publish import (
    AgentPublicationBroker,
    CommandResult,
)
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.event_log import EventLog
from issue_agent.domain.contracts import CommandKind
from issue_agent.domain.events import (
    ControlTransitionCommitted,
    Producer,
    decode_event_line,
)


class Remote:
    def __init__(self):
        self.head = "abc123"
        self.branch = "issue-42"
        self.remote = ""
        self.pr = ""
        self.effects = []
        self.crash_after_push = False

    def run(self, argv, _cwd):
        if argv == ("git", "rev-parse", "HEAD"):
            return CommandResult(0, self.head + "\n")
        if argv == ("git", "branch", "--show-current"):
            return CommandResult(0, self.branch + "\n")
        if argv[:3] == ("git", "ls-remote", "--heads"):
            output = (
                f"{self.remote}\trefs/heads/{self.branch}\n"
                if self.remote
                else ""
            )
            return CommandResult(0, output)
        if argv[:2] == ("git", "push"):
            self.effects.append(argv)
            self.remote = self.head
            if self.crash_after_push:
                raise KeyboardInterrupt
            return CommandResult(0, "pushed\n")
        if argv[:3] == ("gh", "pr", "list"):
            output = (
                f'[{{"number":42,"headRefOid":"{self.head}",'
                f'"url":"{self.pr}"}}]'
                if self.pr
                else "[]"
            )
            return CommandResult(0, output)
        if argv[:3] == ("gh", "pr", "create"):
            self.effects.append(argv)
            self.pr = "https://example.test/pull/42"
            return CommandResult(0, self.pr + "\n")
        raise AssertionError(argv)


def store(tmp_path: Path) -> SQLiteControlStore:
    value = SQLiteControlStore(str(tmp_path / "control.db"))
    value.initialize()
    return value


def broker(tmp_path: Path, remote: Remote) -> AgentPublicationBroker:
    return AgentPublicationBroker(
        store(tmp_path),
        repository="o/r",
        owner="o/r:fix",
        run=remote.run,
    )


def test_push_is_committed_then_receipted(tmp_path):
    remote = Remote()
    control = store(tmp_path)
    events = EventLog(
        str(tmp_path / "events-v2.jsonl"),
        Producer("deployment", "o/r", "fix", 123, "boot"),
    )
    control.bind_event_outbox(
        events.prepare,
        events.append_committed,
        destination=events.producer.feed_identity,
    )
    publisher = AgentPublicationBroker(
        control,
        repository="o/r",
        owner="o/r:fix",
        run=remote.run,
    )

    result = publisher.publish(
        ("git", "push", "--force-with-lease", "origin", "HEAD:issue-42"),
        str(tmp_path),
    )

    assert result.code == 0
    assert len(remote.effects) == 1
    assert control.pending_commands() == ()
    with control._connect() as db:
        kind, state, attempts = db.execute(
            "SELECT kind, state, attempts FROM lifecycle_commands"
        ).fetchone()
        receipt = db.execute(
            "SELECT status, remote_revision FROM effect_receipts"
        ).fetchone()
    assert (kind, state, attempts) == (
        CommandKind.PUBLISH_BRANCH.value,
        "succeeded",
        1,
    )
    assert tuple(receipt) == ("succeeded", remote.head)
    emitted = tuple(
        decode_event_line(line)
        for line in Path(events.path).read_text().splitlines()
    )
    assert [
        event.payload.state
        for event in emitted
        if isinstance(event.payload, ControlTransitionCommitted)
        and event.payload.entity_kind == "lifecycle_command"
    ] == ["pending", "executing", "succeeded"]


def test_crash_after_push_is_reconciled_without_duplicate_effect(tmp_path):
    remote = Remote()
    remote.crash_after_push = True
    control = store(tmp_path)
    publisher = AgentPublicationBroker(
        control,
        repository="o/r",
        owner="o/r:fix",
        run=remote.run,
    )

    with pytest.raises(KeyboardInterrupt):
        publisher.publish(
            ("git", "push", "origin", "HEAD:issue-42"), str(tmp_path)
        )
    assert len(remote.effects) == 1
    assert control.pending_commands()[0].state == "executing"

    remote.crash_after_push = False
    assert publisher.drain_pending_commands() == 1
    assert len(remote.effects) == 1
    assert control.pending_commands() == ()


def test_pending_push_refuses_an_intervening_remote_revision(tmp_path):
    remote = Remote()
    remote.remote = "prior"
    control = store(tmp_path)
    publisher = AgentPublicationBroker(
        control,
        repository="o/r",
        owner="o/r:fix",
        run=remote.run,
    )
    command = publisher._command(
        ("git", "push", "--force", "origin", "HEAD:issue-42"),
        str(tmp_path),
    )
    pending = control.enqueue_command(command)
    remote.remote = "intervening"

    result = publisher._run_pending(pending)

    assert result.code == 75
    assert remote.effects == []
    assert control.pending_commands()[0].state == "pending"


def test_pr_body_file_is_snapshotted_before_command_commit(tmp_path):
    remote = Remote()
    body = tmp_path / "body.md"
    body.write_text("Fixes #42\n")
    publisher = broker(tmp_path, remote)

    result = publisher.publish(
        (
            "gh",
            "pr",
            "create",
            "--base",
            "dev",
            "--title",
            "Fix",
            "--body-file",
            "body.md",
        ),
        str(tmp_path),
    )

    assert result.code == 0
    effect = remote.effects[0]
    assert "--body-file" not in effect
    assert effect[effect.index("--body") + 1] == "Fixes #42\n"


@pytest.mark.parametrize(
    "args",
    (
        ("git", "push", "--dry-run", "origin", "HEAD:issue-42"),
        ("git", "push", "--force", "origin", "HEAD:main"),
        ("git", "push", "--tags", "origin"),
    ),
)
def test_broker_rejects_non_effects_and_broad_or_protected_pushes(
    tmp_path, args
):
    with pytest.raises(ValueError):
        broker(tmp_path, Remote()).publish(args, str(tmp_path))
