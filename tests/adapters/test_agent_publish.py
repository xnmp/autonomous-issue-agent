import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from issue_agent.adapters import agent_publish
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
        self.pr_state = ""
        self.pr_head = ""
        self.effects = []
        self.crash_after_push = False
        self.intervene_before_push = ""
        self.create_pr_head = ""

    def run(self, argv, _cwd):
        if argv == ("git", "rev-parse", "HEAD"):
            return CommandResult(0, self.head + "\n")
        if argv == ("git", "branch", "--show-current"):
            return CommandResult(0, self.branch + "\n")
        if argv == ("git", "remote", "get-url", "--push", "origin"):
            return CommandResult(0, "git@github.com:o/r.git\n")
        if argv[:3] == ("git", "ls-remote", "--heads"):
            output = (
                f"{self.remote}\trefs/heads/{self.branch}\n"
                if self.remote
                else ""
            )
            return CommandResult(0, output)
        if argv[:2] == ("git", "push"):
            self.effects.append(argv)
            if self.intervene_before_push:
                self.remote = self.intervene_before_push
            lease = next(
                arg for arg in argv if arg.startswith("--force-with-lease=")
            )
            expected = lease.split(":", 1)[1]
            if self.remote != expected:
                return CommandResult(1, "", "stale remote lease")
            self.remote = argv[-1].split(":", 1)[0]
            if self.crash_after_push:
                raise KeyboardInterrupt
            return CommandResult(0, "pushed\n")
        if argv[:3] == ("gh", "pr", "list"):
            output = (
                f'[{{"number":42,"headRefOid":"{self.pr_head or self.head}",'
                f'"state":"{self.pr_state}","url":"{self.pr}"}}]'
                if self.pr and self.pr_state == "OPEN"
                else "[]"
            )
            return CommandResult(0, output)
        if argv[:3] == ("gh", "pr", "create"):
            self.effects.append(argv)
            self.pr = "https://example.test/pull/42"
            self.pr_state = "OPEN"
            self.pr_head = self.create_pr_head or self.head
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
        ("git", "push", "origin", "HEAD:issue-42"),
        str(tmp_path),
    )

    assert result.code == 0
    assert len(remote.effects) == 1
    assert remote.effects[0] == (
        "git",
        "push",
        "--force-with-lease=refs/heads/issue-42:",
        "origin",
        "abc123:refs/heads/issue-42",
    )
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
        ("git", "push", "origin", "HEAD:issue-42"),
        str(tmp_path),
    )
    pending = control.enqueue_command(command)
    remote.remote = "intervening"

    result = publisher._run_pending(pending)

    assert result.code == 75
    assert remote.effects == []
    assert control.pending_commands()[0].state == "pending"


def test_replayed_push_uses_captured_sha_after_local_head_moves(tmp_path):
    remote = Remote()
    control = store(tmp_path)
    publisher = AgentPublicationBroker(
        control,
        repository="o/r",
        owner="o/r:fix",
        run=remote.run,
    )
    command = publisher._command(
        ("git", "push", "origin", "HEAD:issue-42"),
        str(tmp_path),
    )
    pending = control.enqueue_command(command)
    remote.head = "moved-local-head"

    result = publisher._run_pending(pending)

    assert result.code == 0
    assert remote.remote == "abc123"
    assert remote.effects[0][-1] == "abc123:refs/heads/issue-42"


def test_push_exact_lease_preserves_concurrent_remote_update(tmp_path):
    remote = Remote()
    remote.remote = "prior"
    remote.intervene_before_push = "intervening"
    publisher = broker(tmp_path, remote)

    result = publisher.publish(
        ("git", "push", "origin", "HEAD:issue-42"),
        str(tmp_path),
    )

    assert result.code != 0
    assert remote.remote == "intervening"
    with publisher.store._connect() as db:
        assert db.execute(
            "SELECT state FROM lifecycle_commands"
        ).fetchone()[0] == "unknown_outcome"


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
    assert effect[effect.index("--repo") + 1] == "github.com/o/r"
    assert effect[effect.index("--head") + 1] == "issue-42"


def test_closed_pr_with_same_head_does_not_satisfy_open_pr_publication(
    tmp_path,
) -> None:
    remote = Remote()
    remote.pr = "https://example.test/pull/41"
    remote.pr_state = "CLOSED"
    publisher = broker(tmp_path, remote)

    result = publisher.publish(
        (
            "gh",
            "pr",
            "create",
            "--base",
            "dev",
            "--title",
            "Replacement",
            "--body",
            "Fixes #42",
        ),
        str(tmp_path),
    )

    assert result.code == 0
    assert len(remote.effects) == 1
    assert remote.effects[0][:3] == ("gh", "pr", "create")
    assert remote.pr_state == "OPEN"


def test_pr_create_exit_zero_requires_matching_remote_head(tmp_path):
    remote = Remote()
    remote.create_pr_head = "different-head"
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
            "--body",
            "Fixes #42",
        ),
        str(tmp_path),
    )

    assert result.code != 0
    assert remote.pr_head == "different-head"
    with publisher.store._connect() as db:
        command_state = db.execute(
            "SELECT state FROM lifecycle_commands"
        ).fetchone()[0]
        receipt_state = db.execute(
            "SELECT status FROM effect_receipts"
        ).fetchone()[0]
    assert (command_state, receipt_state) == (
        "retryable_failure",
        "retryable_failure",
    )


@pytest.mark.parametrize(
    "args",
    (
        ("git", "push", "--dry-run", "origin", "HEAD:issue-42"),
        ("git", "push", "--force", "origin", "HEAD:main"),
        ("git", "push", "--force-with-lease", "origin", "HEAD:issue-42"),
        ("git", "push", "--tags", "origin"),
        ("git", "push", "upstream", "HEAD:issue-42"),
        ("git", "push", "origin", "other:issue-42"),
    ),
)
def test_broker_rejects_non_effects_and_broad_or_protected_pushes(
    tmp_path, args
):
    with pytest.raises(ValueError):
        broker(tmp_path, Remote()).publish(args, str(tmp_path))


@pytest.mark.parametrize(
    "args",
    (
        (
            "gh",
            "pr",
            "create",
            "--repo",
            "attacker/other",
            "--title",
            "Fix",
        ),
        (
            "gh",
            "pr",
            "create",
            "--head=attacker:branch",
            "--title",
            "Fix",
        ),
        (
            "gh",
            "pr",
            "create",
            "-Rattacker/other",
            "--title",
            "Fix",
        ),
    ),
)
def test_broker_rejects_cross_repository_pull_request_scope(tmp_path, args):
    with pytest.raises(ValueError, match="fixed by the durable command"):
        broker(tmp_path, Remote()).publish(args, str(tmp_path))


def test_broker_rejects_origin_for_a_different_repository(tmp_path):
    remote = Remote()

    def run(argv, cwd):
        if argv == ("git", "remote", "get-url", "--push", "origin"):
            return CommandResult(0, "git@github.com:attacker/other.git\n")
        return remote.run(argv, cwd)

    publisher = AgentPublicationBroker(
        store(tmp_path),
        repository="o/r",
        owner="o/r:fix",
        run=run,
    )

    with pytest.raises(ValueError, match="configured repository"):
        publisher.publish(
            ("git", "push", "origin", "HEAD:issue-42"),
            str(tmp_path),
        )


def test_broker_uses_configured_host_not_child_gh_host(
    tmp_path,
    monkeypatch,
) -> None:
    remote = Remote()
    monkeypatch.setenv("GH_HOST", "attacker.example")
    publisher = AgentPublicationBroker(
        store(tmp_path),
        repository="o/r",
        host="github.com",
        owner="o/r:fix",
        run=remote.run,
    )

    assert publisher.publish(
        ("git", "push", "origin", "HEAD:issue-42"),
        str(tmp_path),
    ).code == 0


def test_broker_rejects_child_selected_remote_host(
    tmp_path,
    monkeypatch,
) -> None:
    remote = Remote()
    monkeypatch.setenv("GH_HOST", "attacker.example")

    def run(argv, cwd):
        if argv == ("git", "remote", "get-url", "--push", "origin"):
            return CommandResult(0, "git@attacker.example:o/r.git\n")
        return remote.run(argv, cwd)

    publisher = AgentPublicationBroker(
        store(tmp_path),
        repository="o/r",
        host="github.com",
        owner="o/r:fix",
        run=run,
    )

    with pytest.raises(ValueError, match="configured repository"):
        publisher.publish(
            ("git", "push", "origin", "HEAD:issue-42"),
            str(tmp_path),
        )


def test_macos_process_environment_reads_compact_authority_bundle(
    monkeypatch,
) -> None:
    authority = {
        "ISSUE_AGENT_CONTROL_DB": "/state/control.db",
        "ISSUE_AGENT_REPOSITORY": "o/r",
        "ISSUE_AGENT_PUBLICATION_OWNER": "owner",
        "ISSUE_AGENT_RUN_ID": "run",
        "ISSUE_AGENT_CONFIG_VERSION": "cfg",
        "ISSUE_AGENT_GITHUB_HOST": "github.example",
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(authority, separators=(",", ":")).encode()
    ).decode()

    def no_proc(_self):
        raise FileNotFoundError

    monkeypatch.setattr(agent_publish.sys, "platform", "darwin")
    monkeypatch.setattr(agent_publish.Path, "read_bytes", no_proc)
    monkeypatch.setattr(
        agent_publish.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=f"agent --flag ISSUE_AGENT_AUTHORITY_BUNDLE={encoded}\n",
        ),
    )

    assert agent_publish._process_environment(123) == authority


def test_macos_process_parent_uses_ps_without_proc(monkeypatch) -> None:
    def no_proc(_self, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(agent_publish.sys, "platform", "darwin")
    monkeypatch.setattr(agent_publish.Path, "read_text", no_proc)
    monkeypatch.setattr(
        agent_publish.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="42\n",
        ),
    )

    assert agent_publish._process_parent(123) == 42


def test_cli_uses_supervised_ancestor_authority_not_child_environment(
    monkeypatch,
    tmp_path,
) -> None:
    captured = {}
    trusted = {
        "ISSUE_AGENT_CONTROL_DB": str(tmp_path / "trusted.db"),
        "ISSUE_AGENT_REPOSITORY": "o/r",
        "ISSUE_AGENT_PUBLICATION_OWNER": "o/r:fix:deployment",
        "ISSUE_AGENT_RUN_ID": "run-1",
        "ISSUE_AGENT_CONFIG_VERSION": "cfg-1",
        "ISSUE_AGENT_GITHUB_HOST": "github.com",
    }

    class TrustedStore:
        def __init__(self, path):
            captured["path"] = path

        def integrity_check(self):
            return None

    class Publisher:
        def __init__(self, _store, **kwargs):
            captured.update(kwargs)

        def publish(self, argv, cwd):
            captured["argv"] = argv
            captured["cwd"] = cwd
            return CommandResult(0, "ok\n")

    monkeypatch.setenv("ISSUE_AGENT_CONTROL_DB", "/tmp/attacker.db")
    monkeypatch.setenv("ISSUE_AGENT_REPOSITORY", "attacker/other")
    monkeypatch.setattr(
        agent_publish,
        "_trusted_publication_environment",
        lambda: trusted,
    )
    monkeypatch.setattr(agent_publish, "SQLiteControlStore", TrustedStore)
    monkeypatch.setattr(agent_publish, "AgentPublicationBroker", Publisher)

    with pytest.raises(SystemExit) as stopped:
        agent_publish.main(["git", "push", "origin", "HEAD:issue-42"])

    assert stopped.value.code == 0
    assert captured["path"] == trusted["ISSUE_AGENT_CONTROL_DB"]
    assert captured["repository"] == "o/r"
    assert captured["host"] == "github.com"
    assert captured["owner"] == "o/r:fix:deployment"
    assert captured["config_version"] == "cfg-1"
