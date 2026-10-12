"""The GitHub drain must skip lifecycle kinds it does not own (#156).

The control store is SHARED: `AgentPublicationBroker` commits publish_branch /
open_pull_request commands under the same repository and owner identity that
`GitHubEffectExecutor.drain_pending_commands` scans. The drain used to hand
every pending command to `action_from_command`, which raises for any kind
outside the Action vocabulary — so while a publication was pending, keifu's
daemons logged

    lifecycle command … (publish_branch on branch:…) could not be drained:
    ValueError: command kind cannot be reconstructed as Action:
    CommandKind.PUBLISH_BRANCH

on EVERY tick (invisible before e47a3fe's logging, noise after it).

The behaviour pinned here: a command whose kind belongs to another executor is
left QUEUED untouched (its owner still drains it — it is never receipted or
dropped), is reported at most once per process rather than per tick, and does
not stop owned commands in the same queue from draining. e47a3fe's
"could not be drained" line is reserved for OWNED kinds that genuinely fail.
"""

from __future__ import annotations

import pytest

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.effects import GitHubEffectExecutor
from issue_agent.domain.actions import Relabel, Target
from issue_agent.domain.contracts import CommandId, CommandKind, LifecycleCommand
from issue_agent.domain.labels import Label

REPOSITORY = "o/r"
IDENTITY = "o/r:fix"


class EventuallyWritableGateway:
    """Refuses `refusals` writes (mutation returns, readback shows nothing),
    then applies them — the shape a transient GitHub write has."""

    def __init__(self, *, refusals: int = 0):
        self.refusals = refusals
        self.labels: set[tuple] = set()
        self.calls: list[tuple] = []
        self.issue_revision = "issue-rev-1"

    def relabel(self, kind, number, remove, add):
        self.calls.append(("relabel", kind, number, remove, add))
        if self.refusals > 0:
            self.refusals -= 1
            return
        if add:
            self.labels.add((kind, number, add))

    def has_label(self, kind, number, label):
        return (kind, number, label) in self.labels

    def get_issue(self, number):
        return _Issue(self.issue_revision)


class _Issue:
    def __init__(self, updated_at: str):
        self.updated_at = updated_at


def _executor(tmp_path, gateway, log):
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()
    return (
        GitHubEffectExecutor(
            gateway,
            store,
            repository=REPOSITORY,
            identity=IDENTITY,
            log=log,
        ),
        store,
    )


def _foreign_command(kind: CommandKind, target: str) -> LifecycleCommand:
    digest = f"{kind.value}-{target}"
    return LifecycleCommand(
        CommandId(digest),
        f"foreign:{digest}",
        kind,
        target,
        expected_revision=None,
        payload={"argv": ["git", "push", "origin", "fix/18-widget"]},
        repository=REPOSITORY,
        executor_identity=IDENTITY,
    )


def _pending_state(store, kind: CommandKind):
    return [
        (p.command.command_id, p.state, p.attempts)
        for p in store.pending_commands(
            repository=REPOSITORY, executor_identity=IDENTITY
        )
        if p.command.kind is kind
    ]


@pytest.mark.parametrize(
    "kind,target",
    [
        (CommandKind.PUBLISH_BRANCH, "branch:o/r:fix/18-widget"),
        (CommandKind.OPEN_PULL_REQUEST, "pr:o/r:fix/18-widget"),
    ],
)
def test_unowned_kind_stays_queued_and_is_reported_once(tmp_path, kind, target):
    lines: list[str] = []
    executor, store = _executor(tmp_path, EventuallyWritableGateway(), lines.append)
    store.enqueue_command(_foreign_command(kind, target))
    before = _pending_state(store, kind)

    for _ in range(5):
        assert executor.drain_pending_commands() == 0

    # Untouched: still queued in the same state for its real owner to drain.
    assert _pending_state(store, kind) == before
    assert len(before) == 1
    # Reported, but once per process — not once per tick.
    mentions = [line for line in lines if kind.value in line]
    assert len(mentions) == 1, lines
    assert "not owned" in mentions[0]
    # And NOT as a drain failure: nothing was attempted.
    assert not any("could not be drained" in line for line in lines)


def test_owned_command_drains_while_a_publication_command_is_queued(tmp_path):
    """The skip must not become a stop: the publication command sits ahead of
    an owned relabel in the same queue."""
    gateway = EventuallyWritableGateway(refusals=1)
    lines: list[str] = []
    executor, store = _executor(tmp_path, gateway, lines.append)
    store.enqueue_command(
        _foreign_command(CommandKind.PUBLISH_BRANCH, "branch:o/r:fix/18-widget")
    )

    executor.execute(Relabel(Target.ISSUE, 7, None, Label.AI_TRIAGE))  # refused
    assert executor.drain_pending_commands() == 1

    assert ("issue", 7, "ai-triage") in gateway.labels
    assert _pending_state(store, CommandKind.RELABEL) == []
    assert len(_pending_state(store, CommandKind.PUBLISH_BRANCH)) == 1
