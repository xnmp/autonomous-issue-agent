import base64
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from issue_agent.adapters import agent_publish
from issue_agent.adapters.agent_publish import (
    AgentPublicationBroker,
    CommandResult,
)
from issue_agent.adapters.control_store import (
    RetiredOwnerError,
    SQLiteControlStore,
)
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
        self.branch = "fix/42-issue"
        self.remote = ""
        self.pr = ""
        self.pr_state = ""
        self.pr_head = ""
        self.pr_base = ""
        self.pr_auto_merge = False
        self.pr_body = "## Acceptance Criteria\n- [ ] a\n\nFixes #42"
        self.body_view_fails = False
        self.effects = []
        self.crash_after_push = False
        self.intervene_before_push = ""
        self.create_pr_head = ""
        self.arm_after_push = False
        self.origin_url = "git@github.com:o/r.git"
        # Discard-check knobs (#154): by default the local head descends from
        # the remote tip (fast-forward), so the check is inert for every
        # pre-existing test. non_ff=True simulates a rewritten history; the
        # foreign/own adjudication itself runs against the REAL control
        # store's publish receipts.
        self.non_ff = False
        self.prior_object_missing = False
        self.fetch_fails = False

    def run(self, argv, _cwd):
        if argv[:2] == ("git", "cat-file"):
            return CommandResult(1 if self.prior_object_missing else 0, "")
        if argv[:2] == ("git", "fetch"):
            if self.fetch_fails:
                return CommandResult(1, "", "network down")
            self.prior_object_missing = False
            return CommandResult(0, "")
        if argv[:3] == ("git", "merge-base", "--is-ancestor"):
            return CommandResult(1 if self.non_ff else 0, "")
        if argv == ("git", "rev-parse", "HEAD"):
            return CommandResult(0, self.head + "\n")
        if argv == ("git", "branch", "--show-current"):
            return CommandResult(0, self.branch + "\n")
        if argv == ("git", "remote", "get-url", "--push", "origin"):
            return CommandResult(0, self.origin_url + "\n")
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
            if self.arm_after_push:
                self.pr_auto_merge = True
            if self.crash_after_push:
                raise KeyboardInterrupt
            return CommandResult(0, "pushed\n")
        if argv[:3] == ("gh", "pr", "list"):
            output = (
                f'[{{"number":42,"headRefOid":"{self.pr_head or self.head}",'
                f'"baseRefName":"{self.pr_base or "dev"}",'
                f'"state":"{self.pr_state}","url":"{self.pr}",'
                f'"autoMergeRequest":'
                f'{"{}" if self.pr_auto_merge else "null"}}}]'
                if self.pr and self.pr_state == "OPEN"
                else "[]"
            )
            return CommandResult(0, output)
        if argv[:3] == ("gh", "pr", "view") and "--json" in argv:
            if self.body_view_fails:
                return CommandResult(1, "", "not found")
            return CommandResult(0, json.dumps({"body": self.pr_body}))
        if argv[:3] == ("gh", "pr", "edit"):
            self.effects.append(argv)
            # GitHub stores CRLF for content submitted with LF.
            self.pr_body = argv[argv.index("--body") + 1].replace("\n", "\r\n")
            return CommandResult(0, "https://example.test/pull/42\n")
        if argv[:3] == ("gh", "pr", "create"):
            self.effects.append(argv)
            self.pr = "https://example.test/pull/42"
            self.pr_state = "OPEN"
            self.pr_head = self.create_pr_head or self.head
            self.pr_base = argv[argv.index("--base") + 1]
            return CommandResult(0, self.pr + "\n")
        if argv[:3] == ("gh", "pr", "close"):
            self.effects.append(argv)
            self.pr_state = "CLOSED"
            return CommandResult(0, "closed\n")
        if argv[:3] == ("gh", "pr", "merge") and "--disable-auto" in argv:
            self.effects.append(argv)
            self.pr_auto_merge = False
            return CommandResult(0, "disabled\n")
        raise AssertionError(argv)


def store(tmp_path: Path) -> SQLiteControlStore:
    value = SQLiteControlStore(str(tmp_path / "control.db"))
    value.initialize()
    return value


def broker(tmp_path: Path, remote: Remote) -> AgentPublicationBroker:
    return AgentPublicationBroker(
        store(tmp_path),
        repository="o/r",
        base_branch="dev",
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
        base_branch="dev",
        owner="o/r:fix",
        run=remote.run,
    )

    result = publisher.publish(
        ("git", "push", "origin", "HEAD:fix/42-issue"),
        str(tmp_path),
    )

    assert result.code == 0
    assert len(remote.effects) == 1
    assert remote.effects[0] == (
        "git",
        "push",
        "--force-with-lease=refs/heads/fix/42-issue:",
        "git@github.com:o/r.git",
        "abc123:refs/heads/fix/42-issue",
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


def test_owner_retirement_after_enqueue_fences_publication(tmp_path):
    remote = Remote()
    control = store(tmp_path)
    successor = SQLiteControlStore(control.path)
    successor.initialize()
    old_owner = "o/r:fix:deployment-1"
    new_owner = "o/r:fix:deployment-2"
    origin_reads = 0

    def retire_after_enqueue(argv, cwd):
        nonlocal origin_reads
        if argv == ("git", "remote", "get-url", "--push", "origin"):
            origin_reads += 1
            if origin_reads == 2:
                successor.adopt_legacy_owner(
                    "o/r",
                    "fix",
                    new_owner,
                    prior_owner_ids=(old_owner,),
                )
        return remote.run(argv, cwd)

    publisher = AgentPublicationBroker(
        control,
        repository="o/r",
        base_branch="dev",
        owner=old_owner,
        run=retire_after_enqueue,
    )

    with pytest.raises(RetiredOwnerError, match="deployment-2"):
        publisher.publish(
            ("git", "push", "origin", "HEAD:fix/42-issue"),
            str(tmp_path),
        )

    assert remote.effects == []
    assert successor.pending_commands(
        repository="o/r",
        executor_identity=new_owner,
    )


def test_owner_retirement_waits_for_inflight_remote_mutation(tmp_path):
    remote = Remote()
    control = store(tmp_path)
    successor = SQLiteControlStore(control.path)
    successor.initialize()
    old_owner = "o/r:fix:deployment-1"
    new_owner = "o/r:fix:deployment-2"
    mutation_started = Event()
    finish_mutation = Event()
    retirement_started = Event()
    retirement_completed = Event()

    def block_during_push(argv, cwd):
        if argv[:2] == ("git", "push"):
            mutation_started.set()
            assert finish_mutation.wait(timeout=1)
        return remote.run(argv, cwd)

    def retire_owner() -> tuple[int, int]:
        retirement_started.set()
        result = successor.adopt_legacy_owner(
            "o/r",
            "fix",
            new_owner,
            prior_owner_ids=(old_owner,),
        )
        retirement_completed.set()
        return result

    publisher = AgentPublicationBroker(
        control,
        repository="o/r",
        base_branch="dev",
        owner=old_owner,
        run=block_during_push,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        publication = pool.submit(
            publisher.publish,
            ("git", "push", "origin", "HEAD:fix/42-issue"),
            str(tmp_path),
        )
        assert mutation_started.wait(timeout=1)
        retirement = pool.submit(retire_owner)
        assert retirement_started.wait(timeout=1)
        assert not retirement_completed.wait(timeout=0.1)
        finish_mutation.set()

        assert publication.result(timeout=1).code == 0
        assert retirement.result(timeout=1) == (0, 0)

    assert retirement_completed.is_set()
    assert len(remote.effects) == 1
    with pytest.raises(RetiredOwnerError, match="deployment-2"):
        publisher.publish(
            ("git", "push", "origin", "HEAD:fix/42-issue"),
            str(tmp_path),
        )


def test_push_refuses_a_foreign_remote_tip(tmp_path):
    """#154 (observed live, keifu #169): the broker's leased force publish is
    race-safe but was not AUTHORSHIP-safe — an agent rebase erased an operator
    commit the reviewer then re-demanded. A non-fast-forward push over a tip
    this deployment never published must refuse with an actionable message,
    and nothing may reach the remote."""
    remote = Remote()
    remote.remote = "operator99"
    remote.non_ff = True
    publisher = broker(tmp_path, remote)

    with pytest.raises(ValueError, match="not what this harness last published"):
        publisher.publish(
            ("git", "push", "origin", "HEAD:fix/42-issue"),
            str(tmp_path),
        )
    assert remote.effects == []


def test_rewriting_our_own_published_history_still_publishes(tmp_path):
    """The ordinary conflicting-rebase flow: the remote tip is exactly what
    this broker last pushed (receipt-backed), so the agent may rewrite it —
    resolved-conflict commits change patch-ids, which is why provenance, not
    patch-equivalence, is the discriminator (adversarial P0-1)."""
    remote = Remote()
    publisher = broker(tmp_path, remote)
    first = publisher.publish(
        ("git", "push", "origin", "HEAD:fix/42-issue"),
        str(tmp_path),
    )
    assert first.code == 0 and remote.remote == "abc123"

    remote.head = "rebased99"  # rebased local history, non-FF vs abc123
    remote.non_ff = True
    result = publisher.publish(
        ("git", "push", "origin", "HEAD:fix/42-issue"),
        str(tmp_path),
    )

    assert result.code == 0
    assert remote.remote == "rebased99"


def test_unverifiable_prior_refuses_rather_than_publishing_blind(tmp_path):
    """'I cannot see what I would destroy' must never publish: the prior
    remote object absent locally AND unfetchable refuses the capture."""
    remote = Remote()
    remote.remote = "operator99"
    remote.non_ff = True
    remote.prior_object_missing = True
    remote.fetch_fails = True
    publisher = broker(tmp_path, remote)

    with pytest.raises(ValueError, match="cannot verify"):
        publisher.publish(
            ("git", "push", "origin", "HEAD:fix/42-issue"),
            str(tmp_path),
        )
    assert remote.effects == []


def test_missing_prior_object_is_fetched_then_checked(tmp_path):
    """A worktree that never fetched can still verify: one fetch, then the
    equivalence check adjudicates."""
    remote = Remote()
    remote.remote = "operator99"
    remote.non_ff = True
    remote.prior_object_missing = True
    publisher = broker(tmp_path, remote)

    with pytest.raises(ValueError, match="not what this harness last published"):
        publisher.publish(
            ("git", "push", "origin", "HEAD:fix/42-issue"),
            str(tmp_path),
        )
    assert remote.effects == []


def test_push_disables_and_verifies_existing_auto_merge_first(tmp_path):
    remote = Remote()
    remote.remote = "prior"
    remote.pr = "https://example.test/pull/42"
    remote.pr_state = "OPEN"
    remote.pr_head = "prior"
    remote.pr_auto_merge = True
    publisher = broker(tmp_path, remote)

    result = publisher.publish(
        ("git", "push", "origin", "HEAD:fix/42-issue"),
        str(tmp_path),
    )

    assert result.code == 0
    assert remote.pr_auto_merge is False
    assert remote.effects[0][:3] == ("gh", "pr", "merge")
    assert "--disable-auto" in remote.effects[0]
    assert remote.effects[1][:2] == ("git", "push")


def test_push_postcondition_revokes_arm_that_appears_during_update(tmp_path):
    remote = Remote()
    remote.remote = "prior"
    remote.pr = "https://example.test/pull/42"
    remote.pr_state = "OPEN"
    remote.pr_head = "prior"
    remote.arm_after_push = True
    publisher = broker(tmp_path, remote)

    result = publisher.publish(
        ("git", "push", "origin", "HEAD:fix/42-issue"),
        str(tmp_path),
    )

    assert result.code == 0
    assert remote.pr_auto_merge is False
    assert any("--disable-auto" in effect for effect in remote.effects)


def test_crash_recovery_revokes_arm_before_receipting_push(tmp_path):
    remote = Remote()
    remote.remote = "prior"
    remote.pr = "https://example.test/pull/42"
    remote.pr_state = "OPEN"
    remote.pr_head = "prior"
    remote.arm_after_push = True
    remote.crash_after_push = True
    publisher = broker(tmp_path, remote)

    with pytest.raises(KeyboardInterrupt):
        publisher.publish(
            ("git", "push", "origin", "HEAD:fix/42-issue"),
            str(tmp_path),
        )

    remote.crash_after_push = False
    remote.arm_after_push = False
    assert publisher.drain_pending_commands() == 1
    assert remote.pr_auto_merge is False
    assert any("--disable-auto" in effect for effect in remote.effects)


def test_crash_after_push_is_reconciled_without_duplicate_effect(tmp_path):
    remote = Remote()
    remote.crash_after_push = True
    control = store(tmp_path)
    publisher = AgentPublicationBroker(
        control,
        repository="o/r",
        base_branch="dev",
        owner="o/r:fix",
        run=remote.run,
    )

    with pytest.raises(KeyboardInterrupt):
        publisher.publish(
            ("git", "push", "origin", "HEAD:fix/42-issue"), str(tmp_path)
        )
    assert len(remote.effects) == 1
    assert control.pending_commands()[0].state == "unknown_outcome"

    remote.crash_after_push = False
    assert publisher.drain_pending_commands() == 1
    assert len(remote.effects) == 1
    assert control.pending_commands() == ()


def test_replay_refuses_switched_origin_until_authority_is_restored(tmp_path):
    remote = Remote()
    remote.crash_after_push = True
    control = store(tmp_path)
    publisher = AgentPublicationBroker(
        control,
        repository="o/r",
        base_branch="dev",
        owner="o/r:fix",
        run=remote.run,
    )

    with pytest.raises(KeyboardInterrupt):
        publisher.publish(
            ("git", "push", "origin", "HEAD:fix/42-issue"),
            str(tmp_path),
        )
    remote.crash_after_push = False
    remote.origin_url = "git@github.com:attacker/other.git"

    assert publisher.drain_pending_commands() == 0
    assert len(remote.effects) == 1
    assert control.pending_commands()[0].state == "unknown_outcome"

    remote.origin_url = "git@github.com:o/r.git"
    assert publisher.drain_pending_commands() == 1
    assert len(remote.effects) == 1
    assert control.pending_commands() == ()


def test_pending_publication_never_uses_a_switched_symbolic_origin(tmp_path):
    remote = Remote()
    control = store(tmp_path)
    publisher = AgentPublicationBroker(
        control,
        repository="o/r",
        base_branch="dev",
        owner="o/r:fix",
        run=remote.run,
    )
    command = publisher._command(
        ("git", "push", "origin", "HEAD:fix/42-issue"),
        str(tmp_path),
    )
    pending = control.enqueue_command(command)
    remote.origin_url = "git@github.com:attacker/other.git"

    result = publisher._run_pending(pending)

    assert result.code == 75
    assert "configured repository" in result.stderr
    assert remote.effects == []
    assert control.pending_commands()[0].state == "pending"


def test_pending_push_refuses_an_intervening_remote_revision(tmp_path):
    remote = Remote()
    remote.remote = "prior"
    control = store(tmp_path)
    publisher = AgentPublicationBroker(
        control,
        repository="o/r",
        base_branch="dev",
        owner="o/r:fix",
        run=remote.run,
    )
    command = publisher._command(
        ("git", "push", "origin", "HEAD:fix/42-issue"),
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
        base_branch="dev",
        owner="o/r:fix",
        run=remote.run,
    )
    command = publisher._command(
        ("git", "push", "origin", "HEAD:fix/42-issue"),
        str(tmp_path),
    )
    pending = control.enqueue_command(command)
    remote.head = "moved-local-head"

    result = publisher._run_pending(pending)

    assert result.code == 0
    assert remote.remote == "abc123"
    assert remote.effects[0][-1] == "abc123:refs/heads/fix/42-issue"


def test_push_exact_lease_preserves_concurrent_remote_update(tmp_path):
    remote = Remote()
    remote.remote = "prior"
    remote.intervene_before_push = "intervening"
    publisher = broker(tmp_path, remote)

    result = publisher.publish(
        ("git", "push", "origin", "HEAD:fix/42-issue"),
        str(tmp_path),
    )

    assert result.code != 0
    assert remote.remote == "intervening"
    with publisher.store._connect() as db:
        assert db.execute(
            "SELECT state FROM lifecycle_commands"
        ).fetchone()[0] == "unknown_outcome"


def test_pr_literal_body_is_snapshotted_before_command_commit(tmp_path):
    remote = Remote()
    remote.remote = remote.head
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
            "Fixes #42\n",
        ),
        str(tmp_path),
    )

    assert result.code == 0
    effect = remote.effects[0]
    assert effect[effect.index("--body") + 1] == "Fixes #42\n"
    assert effect[effect.index("--repo") + 1] == "github.com/o/r"
    assert effect[effect.index("--head") + 1] == "fix/42-issue"
    assert effect[effect.index("--base") + 1] == "dev"


def test_pr_publication_records_local_revision_drift_without_stranding(
    tmp_path,
):
    remote = Remote()
    remote.remote = remote.head
    control = store(tmp_path)
    publisher = AgentPublicationBroker(
        control,
        repository="o/r",
        base_branch="dev",
        owner="o/r:fix",
        run=remote.run,
    )
    command = publisher._command(
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
    pending = control.enqueue_command(command)
    remote.head = "new-local-head"

    result = publisher._run_pending(pending)

    assert result.code == 75
    assert remote.effects == []
    with control._connect() as db:
        state, attempts = db.execute(
            "SELECT state, attempts FROM lifecycle_commands"
        ).fetchone()
        statuses = tuple(
            row[0]
            for row in db.execute(
                "SELECT status FROM effect_receipts"
            ).fetchall()
        )
    assert (state, attempts) == ("permanent_failure", 1)
    assert statuses == ("permanent_failure",)
    assert publisher.drain_pending_commands() == 0


def test_pr_publication_records_remote_revision_drift_without_stranding(
    tmp_path,
):
    remote = Remote()
    remote.remote = remote.head
    control = store(tmp_path)
    publisher = AgentPublicationBroker(
        control,
        repository="o/r",
        base_branch="dev",
        owner="o/r:fix",
        run=remote.run,
    )
    command = publisher._command(
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
    pending = control.enqueue_command(command)
    remote.remote = "new-remote-head"

    result = publisher._run_pending(pending)

    assert result.code == 75
    assert remote.effects == []
    with control._connect() as db:
        state, attempts = db.execute(
            "SELECT state, attempts FROM lifecycle_commands"
        ).fetchone()
        statuses = tuple(
            row[0]
            for row in db.execute(
                "SELECT status FROM effect_receipts"
            ).fetchall()
        )
    assert (state, attempts) == ("permanent_failure", 1)
    assert statuses == ("permanent_failure",)
    assert publisher.drain_pending_commands() == 0


@pytest.mark.parametrize(
    "file_args",
    (
        ("--body-file", "/proc/1/environ"),
        ("--body-file=../secret",),
        ("--template", "/tmp/secret.md"),
        ("--template=../secret.md",),
        ("-T/tmp/secret.md",),
        ("-F", "/proc/1/environ"),
        ("-F../secret",),
        ("--editor",),
        ("--web",),
    ),
)
def test_pr_create_rejects_all_file_reading_options(
    tmp_path, file_args
) -> None:
    remote = Remote()
    remote.remote = remote.head
    publisher = broker(tmp_path, remote)

    with pytest.raises(ValueError, match="file-reading options"):
        publisher.publish(
            (
                "gh",
                "pr",
                "create",
                "--title",
                "Fix",
                *file_args,
            ),
            str(tmp_path),
        )

    assert remote.effects == []


def test_pr_create_replaces_child_selected_base_with_approved_base(
    tmp_path,
) -> None:
    remote = Remote()
    remote.remote = remote.head
    publisher = broker(tmp_path, remote)

    result = publisher.publish(
        (
            "gh",
            "pr",
            "create",
            "--base",
            "main",
            "--title",
            "Fix",
            "--body",
            "Fixes #42",
        ),
        str(tmp_path),
    )

    assert result.code == 0
    effect = remote.effects[0]
    assert effect.count("--base") == 1
    assert effect[effect.index("--base") + 1] == "dev"
    assert "main" not in effect


def test_closed_pr_with_same_head_does_not_satisfy_open_pr_publication(
    tmp_path,
) -> None:
    remote = Remote()
    remote.remote = remote.head
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
    remote.remote = remote.head
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
    assert remote.pr_state == "CLOSED"
    assert any(effect[:3] == ("gh", "pr", "close") for effect in remote.effects)
    with publisher.store._connect() as db:
        command_state = db.execute(
            "SELECT state FROM lifecycle_commands"
        ).fetchone()[0]
        receipt_state = db.execute(
            "SELECT status FROM effect_receipts"
        ).fetchone()[0]
    assert (command_state, receipt_state) == (
        "permanent_failure",
        "permanent_failure",
    )


def test_existing_same_head_pr_on_wrong_base_is_not_adopted(tmp_path):
    remote = Remote()
    remote.remote = remote.head
    remote.pr = "https://example.test/pull/42"
    remote.pr_state = "OPEN"
    remote.pr_head = remote.head
    remote.pr_base = "main"
    publisher = broker(tmp_path, remote)

    with pytest.raises(ValueError, match="different revision or base"):
        publisher.publish(
            (
                "gh",
                "pr",
                "create",
                "--title",
                "Fix",
                "--body",
                "Fixes #42",
            ),
            str(tmp_path),
        )

    assert remote.effects == []


@pytest.mark.parametrize(
    "args",
    (
        ("git", "push", "--dry-run", "origin", "HEAD:fix/42-issue"),
        ("git", "push", "--force", "origin", "HEAD:main"),
        ("git", "push", "--force-with-lease", "origin", "HEAD:fix/42-issue"),
        ("git", "push", "--tags", "origin"),
        ("git", "push", "upstream", "HEAD:fix/42-issue"),
        ("git", "push", "origin", "other:fix/42-issue"),
        ("git", "push", "origin", "HEAD:fix/99-other"),
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
                "--body",
                "Fixes #42",
            ),
        (
            "gh",
            "pr",
            "create",
                "--head=attacker:branch",
                "--title",
                "Fix",
                "--body",
                "Fixes #42",
            ),
        (
            "gh",
            "pr",
            "create",
                "-Rattacker/other",
                "--title",
                "Fix",
                "--body",
                "Fixes #42",
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
        base_branch="dev",
        owner="o/r:fix",
        run=run,
    )

    with pytest.raises(ValueError, match="configured repository"):
        publisher.publish(
            ("git", "push", "origin", "HEAD:fix/42-issue"),
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
        base_branch="dev",
        host="github.com",
        owner="o/r:fix",
        run=remote.run,
    )

    assert publisher.publish(
        ("git", "push", "origin", "HEAD:fix/42-issue"),
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
        base_branch="dev",
        host="github.com",
        owner="o/r:fix",
        run=run,
    )

    with pytest.raises(ValueError, match="configured repository"):
        publisher.publish(
            ("git", "push", "origin", "HEAD:fix/42-issue"),
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
        "ISSUE_AGENT_BASE_BRANCH": "dev",
        "ISSUE_AGENT_PUBLICATION_CWD": "/work/issue-42",
        "ISSUE_AGENT_PUBLICATION_BRANCH": "fix/42-issue",
        "ISSUE_AGENT_PUBLICATION_ISSUE": "42",
        "ISSUE_AGENT_PUBLICATION_KIND": "RESOLVE",
        "ISSUE_AGENT_GIT_BIN": "/usr/bin/git",
        "ISSUE_AGENT_GH_BIN": "/usr/bin/gh",
        "ISSUE_AGENT_EXEC_PATH": "/usr/bin:/bin",
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
        "ISSUE_AGENT_BASE_BRANCH": "dev",
        "ISSUE_AGENT_PUBLICATION_CWD": str(tmp_path),
        "ISSUE_AGENT_PUBLICATION_BRANCH": "fix/42-issue",
        "ISSUE_AGENT_PUBLICATION_ISSUE": "42",
        "ISSUE_AGENT_PUBLICATION_KIND": "RESOLVE",
        "ISSUE_AGENT_GIT_BIN": "/usr/bin/git",
        "ISSUE_AGENT_GH_BIN": "/usr/bin/gh",
        "ISSUE_AGENT_EXEC_PATH": "/usr/bin:/bin",
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
        agent_publish.main(["git", "push", "origin", "HEAD:fix/42-issue"])

    assert stopped.value.code == 0
    assert captured["path"] == trusted["ISSUE_AGENT_CONTROL_DB"]
    assert captured["repository"] == "o/r"
    assert captured["host"] == "github.com"
    assert captured["base_branch"] == "dev"
    assert captured["owner"] == "o/r:fix:deployment"
    assert captured["config_version"] == "cfg-1"
    assert captured["authorized_cwd"] == str(tmp_path)
    assert captured["authorized_branch"] == "fix/42-issue"
    assert captured["expected_issue"] == 42
    assert captured["git_binary"] == "/usr/bin/git"


def test_publication_environment_authenticates_via_trusted_gh(monkeypatch):
    """Nulling the global gitconfig removed the operator's credential.helper,
    so headless https pushes died on a username prompt (keifu #39,
    2026-08-01). The environment must route credentials through the trusted
    absolute gh binary and forbid terminal prompts."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/attacker/gitconfig")
    monkeypatch.setenv("GH_CONFIG_DIR", "/attacker/gh")
    env = agent_publish._publication_environment(
        "/usr/bin", host="github.com", gh_binary="/opt/gh/bin/gh"
    )
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_KEY_0"] == "credential.https://github.com.helper"
    assert env["GIT_CONFIG_VALUE_0"] == ""
    assert env["GIT_CONFIG_KEY_1"] == "credential.https://github.com.helper"
    assert env["GIT_CONFIG_VALUE_1"] == "!/opt/gh/bin/gh auth git-credential"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    # The scrubs that motivated the regression stay intact.
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert "GH_CONFIG_DIR" not in env


def test_publication_environment_without_gh_still_forbids_prompts(monkeypatch):
    env = agent_publish._publication_environment("/usr/bin", host="github.com")
    assert "GIT_CONFIG_COUNT" not in env
    assert env["GIT_TERMINAL_PROMPT"] == "0"



# ── PR body updates (keifu #177 / issue #162, 2026-08-02) ────────────────────
# An address-review leg was required by its reviewer to map an evidence
# screenshot to an acceptance criterion in the PR BODY, had no brokered
# operation for it, and escalated to a human who ran the PATCH by hand.

# Every replacement body must still satisfy the create-time PR body gate, so
# the fixtures below are gate-valid unless a test is about that rule.
GOOD_BODY = "## Acceptance Criteria\n- [x] a ![ac](evidence/a.png)\n\nFixes #42"


def _open_pr(remote: Remote) -> Remote:
    remote.pr = "https://example.test/pull/42"
    remote.pr_state = "OPEN"
    return remote


def test_body_update_on_own_pr_is_committed_then_receipted(tmp_path):
    remote = _open_pr(Remote())
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
        base_branch="dev",
        owner="o/r:fix",
        run=remote.run,
    )

    result = publisher.publish(
        ("gh", "pr", "edit", "42", "--body", GOOD_BODY), str(tmp_path)
    )

    assert result.code == 0
    assert remote.pr_body == GOOD_BODY.replace("\n", "\r\n")
    edit = next(e for e in remote.effects if e[:3] == ("gh", "pr", "edit"))
    # The durable command pins the repository regardless of what the child said.
    assert edit[3] == "42"
    assert edit[edit.index("--repo") + 1] == "github.com/o/r"

    with control._connect() as db:
        kind, state, target = db.execute(
            "SELECT kind, state, target FROM lifecycle_commands"
        ).fetchone()
        status, remote_object = db.execute(
            "SELECT status, remote_object_id FROM effect_receipts"
        ).fetchone()
    assert kind == CommandKind.UPDATE_PULL_REQUEST_BODY.value
    assert state == "succeeded"
    assert target == "pull-request-body:o/r:42"
    assert (status, remote_object) == ("succeeded", "42")

    emitted = tuple(
        decode_event_line(line)
        for line in Path(events.path).read_text().splitlines()
    )
    transitions = [
        event
        for event in emitted
        if isinstance(event.payload, ControlTransitionCommitted)
        and event.payload.entity_kind == "lifecycle_command"
    ]
    assert [event.payload.state for event in transitions] == [
        "pending",
        "executing",
        "succeeded",
    ]
    # Journalled with BOTH correlation identifiers, on the fold the dashboard
    # actually reads — not merely inside the command payload. The `executing`
    # transition is exempt: `begin_command_execution` is keyed by command id
    # alone and emits an empty correlation for EVERY kind, which is a
    # pre-existing gap this change does not widen.
    correlations = {
        (event.correlation.issue, event.correlation.pr)
        for event in transitions
    }
    assert (42, 42) in correlations
    assert correlations <= {(42, 42), (None, None)}


def test_body_update_correlates_the_issue_its_branch_names(tmp_path):
    """Issue and PR correlation are distinct fields, so a fixture where both
    happen to be 42 cannot show the issue is really derived from the branch."""
    remote = _open_pr(Remote())
    remote.branch = "fix/7-issue"
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
        base_branch="dev",
        owner="o/r:fix",
        run=remote.run,
    )

    assert publisher.publish(
        ("gh", "pr", "edit", "42", "--body", GOOD_BODY), str(tmp_path)
    ).code == 0

    correlations = {
        (event.correlation.issue, event.correlation.pr)
        for event in (
            decode_event_line(line)
            for line in Path(events.path).read_text().splitlines()
        )
        if isinstance(event.payload, ControlTransitionCommitted)
        and event.payload.entity_kind == "lifecycle_command"
    }
    assert (7, 42) in correlations          # branch names the issue, not the PR
    assert correlations <= {(7, 42), (None, None)}


@pytest.mark.parametrize("number", ("41", "43", "1", "999999"))
def test_body_update_refuses_a_pull_request_this_run_does_not_own(
    tmp_path, number
):
    remote = _open_pr(Remote())
    original = remote.pr_body

    with pytest.raises(ValueError, match="own PR"):
        broker(tmp_path, remote).publish(
            ("gh", "pr", "edit", number, "--body", GOOD_BODY), str(tmp_path)
        )

    assert remote.pr_body == original
    assert not any(e[:3] == ("gh", "pr", "edit") for e in remote.effects)


def test_body_update_refuses_an_own_branch_pr_onto_another_base(tmp_path):
    """`--head <branch>` alone can match a second open PR aimed at another
    base; only the PR onto the approved base belongs to this run."""
    remote = _open_pr(Remote())
    remote.pr_base = "main"
    original = remote.pr_body

    with pytest.raises(ValueError, match="own PR"):
        broker(tmp_path, remote).publish(
            ("gh", "pr", "edit", "42", "--body", GOOD_BODY), str(tmp_path)
        )

    assert remote.pr_body == original


def test_body_update_refuses_a_cross_repository_selector(tmp_path):
    remote = _open_pr(Remote())
    original = remote.pr_body

    with pytest.raises(ValueError):
        broker(tmp_path, remote).publish(
            (
                "gh",
                "pr",
                "edit",
                "https://github.com/attacker/other/pull/42",
                "--body",
                GOOD_BODY,
            ),
            str(tmp_path),
        )
    assert remote.pr_body == original


def test_body_update_pins_repository_even_when_the_child_names_another(
    tmp_path,
):
    remote = _open_pr(Remote())

    result = broker(tmp_path, remote).publish(
        ("gh", "pr", "edit", "42", "-Rattacker/other", "--body", GOOD_BODY),
        str(tmp_path),
    )

    assert result.code == 0
    edit = next(e for e in remote.effects if e[:3] == ("gh", "pr", "edit"))
    assert "attacker/other" not in edit
    assert edit[edit.index("--repo") + 1] == "github.com/o/r"


# The create-time body gate must survive the body becoming mutable: an edit
# that strips `Fixes #N` would leave the merged PR's issue open for the steward
# to re-triage into fresh paid legs.
@pytest.mark.parametrize(
    "body",
    (
        "",
        "x",
        "just some prose with no sections at all",
        "## Acceptance Criteria\n- [x] a",          # no closing keyword
        "Fixes #42",                                 # no criteria section
        "## Acceptance criteria\n- [x] a\n\nFixes #42",  # wrong case
    ),
)
def test_body_update_still_enforces_the_pull_request_body_gate(tmp_path, body):
    remote = _open_pr(Remote())
    original = remote.pr_body

    with pytest.raises(ValueError, match="body update rejected"):
        broker(tmp_path, remote).publish(
            ("gh", "pr", "edit", "42", "--body", body), str(tmp_path)
        )

    assert remote.pr_body == original
    assert not any(e[:3] == ("gh", "pr", "edit") for e in remote.effects)


def test_body_update_bounds_an_oversize_body(tmp_path):
    remote = _open_pr(Remote())
    original = remote.pr_body
    oversize = GOOD_BODY + "x" * agent_publish.MAX_PR_BODY_BYTES

    with pytest.raises(ValueError, match="exceeds the durable limit"):
        broker(tmp_path, remote).publish(
            ("gh", "pr", "edit", "42", "--body", oversize), str(tmp_path)
        )
    assert remote.pr_body == original


def test_body_update_measures_the_bound_in_bytes_not_characters(tmp_path):
    remaining = agent_publish.MAX_PR_BODY_BYTES - len(GOOD_BODY.encode())
    # "é" is two bytes, so an odd remainder needs one ASCII filler to land
    # exactly on the boundary.
    at_limit = GOOD_BODY + "x" * (remaining % 2) + "é" * (remaining // 2)
    assert len(at_limit.encode()) == agent_publish.MAX_PR_BODY_BYTES

    assert broker(tmp_path, _open_pr(Remote())).publish(
        ("gh", "pr", "edit", "42", "--body", at_limit), str(tmp_path)
    ).code == 0

    # One more CHARACTER is two more bytes — over the byte bound.
    with pytest.raises(ValueError, match="exceeds the durable limit"):
        broker(tmp_path, _open_pr(Remote())).publish(
            ("gh", "pr", "edit", "42", "--body", at_limit + "é"),
            str(tmp_path),
        )


@pytest.mark.parametrize(
    "args",
    (
        ("gh", "pr", "edit", "42"),                       # no body
        ("gh", "pr", "edit", "--body", GOOD_BODY),        # no selector
        ("gh", "pr", "edit", "42", "--body"),             # dangling value
        ("gh", "pr", "edit", "42", "--body-file", "/etc/passwd"),
        ("gh", "pr", "edit", "42", "-F", "/etc/passwd"),
        ("gh", "pr", "edit", "42", "--body", GOOD_BODY, "--title", "t"),
        ("gh", "pr", "edit", "42", "--body", GOOD_BODY, "--add-label", "ok"),
        ("gh", "pr", "edit", "42", "--body", GOOD_BODY, "--add-reviewer", "x"),
        ("gh", "pr", "edit", "42", "--body", GOOD_BODY, "--base", "main"),
        ("gh", "pr", "edit", "42", "43", "--body", GOOD_BODY),  # two selectors
        ("gh", "pr", "edit", "42", "--body", GOOD_BODY, "--body", GOOD_BODY),
        ("gh", "pr", "edit", "fix/42-issue", "--body", GOOD_BODY),
        ("gh", "pr", "edit", "0", "--body", GOOD_BODY),
        ("gh", "pr", "edit", "-1", "--body", GOOD_BODY),
        ("gh", "pr", "edit", "4٢", "--body", GOOD_BODY),   # non-ASCII digits
        ("gh", "pr", "edit", "42.0", "--body", GOOD_BODY),
        # `--` disagrees with gh about which token is the selector.
        ("gh", "pr", "edit", "--", "42", "--body", GOOD_BODY),
        ("gh", "pr", "edit", ""),
        ("gh", "pr", "merge", "42"),
        ("gh", "issue", "edit", "42", "--body", GOOD_BODY),
    ),
)
def test_body_update_refuses_malformed_or_widened_invocations(tmp_path, args):
    remote = _open_pr(Remote())
    original = remote.pr_body

    with pytest.raises((ValueError, RuntimeError)):
        broker(tmp_path, remote).publish(args, str(tmp_path))

    assert remote.pr_body == original
    assert not any(e[:3] == ("gh", "pr", "edit") for e in remote.effects)


def test_attached_shorthand_body_keeps_its_whole_value(tmp_path):
    """pflag reads `-bVALUE` as the entire VALUE; splitting it on `=` silently
    truncated a body whose first line carried one."""
    body = "## Acceptance Criteria\n- [x] a=b holds\n\nFixes #42"
    remote = _open_pr(Remote())

    assert broker(tmp_path, remote).publish(
        ("gh", "pr", "edit", "42", "-b" + body), str(tmp_path)
    ).code == 0
    assert remote.pr_body == body.replace("\n", "\r\n")


def test_body_update_refuses_when_someone_else_rewrote_the_body(tmp_path):
    remote = _open_pr(Remote())
    publisher = broker(tmp_path, remote)
    original_run = remote.run
    intervened = {"done": False}

    def intervene(argv, cwd):
        result = original_run(argv, cwd)
        if argv[:3] == ("gh", "pr", "view") and not intervened["done"]:
            intervened["done"] = True
            remote.pr_body = "an operator rewrote this"
        return result

    publisher.run = intervene

    result = publisher.publish(
        ("gh", "pr", "edit", "42", "--body", GOOD_BODY), str(tmp_path)
    )

    assert result.code == 75
    assert remote.pr_body == "an operator rewrote this"
    assert not any(e[:3] == ("gh", "pr", "edit") for e in remote.effects)


def test_body_update_refuses_a_rewrite_landing_after_the_preflight(tmp_path):
    """The pre-flight reconcile runs before authority is held; an operator edit
    inside that window must not be silently overwritten."""
    remote = _open_pr(Remote())
    publisher = broker(tmp_path, remote)
    original_run = remote.run
    views = {"count": 0}

    def intervene(argv, cwd):
        result = original_run(argv, cwd)
        if argv[:3] == ("gh", "pr", "view"):
            views["count"] += 1
            if views["count"] == 2:  # after capture AND after the pre-flight
                remote.pr_body = "an operator rewrote this"
        return result

    publisher.run = intervene

    result = publisher.publish(
        ("gh", "pr", "edit", "42", "--body", GOOD_BODY), str(tmp_path)
    )

    assert result.code != 0
    assert remote.pr_body == "an operator rewrote this"
    assert not any(e[:3] == ("gh", "pr", "edit") for e in remote.effects)


def test_body_update_already_applied_is_not_written_twice(tmp_path):
    remote = _open_pr(Remote())
    # Stored the way GitHub stores it — CRLF, no trailing newline — so this
    # asserts the round-trip normalization, not string identity.
    remote.pr_body = GOOD_BODY.replace("\n", "\r\n") + "\r\n"

    result = broker(tmp_path, remote).publish(
        ("gh", "pr", "edit", "42", "--body", GOOD_BODY), str(tmp_path)
    )

    assert result.code == 0
    assert not any(e[:3] == ("gh", "pr", "edit") for e in remote.effects)


def test_body_update_refuses_when_the_current_body_is_unreadable(tmp_path):
    remote = _open_pr(Remote())
    remote.body_view_fails = True

    with pytest.raises(RuntimeError, match="current pull-request body"):
        broker(tmp_path, remote).publish(
            ("gh", "pr", "edit", "42", "--body", GOOD_BODY), str(tmp_path)
        )


def test_body_update_refuses_when_the_branch_has_no_open_pull_request(tmp_path):
    remote = Remote()  # pr_state is unset — gh pr list returns []

    with pytest.raises(ValueError, match="own PR"):
        broker(tmp_path, remote).publish(
            ("gh", "pr", "edit", "42", "--body", GOOD_BODY), str(tmp_path)
        )


def test_pending_body_update_is_drained_by_the_broker(tmp_path):
    """The kind must be in the broker's drain set, or a body update interrupted
    mid-flight is stranded pending forever."""
    remote = _open_pr(Remote())
    control = store(tmp_path)
    publisher = AgentPublicationBroker(
        control,
        repository="o/r",
        base_branch="dev",
        owner="o/r:fix",
        run=remote.run,
    )
    crashed = {"done": False}

    def crash_once(argv, cwd):
        if argv[:3] == ("gh", "pr", "edit") and not crashed["done"]:
            crashed["done"] = True
            raise KeyboardInterrupt
        return remote.run(argv, cwd)

    publisher.run = crash_once
    with pytest.raises(KeyboardInterrupt):
        publisher.publish(
            ("gh", "pr", "edit", "42", "--body", GOOD_BODY), str(tmp_path)
        )
    assert control.pending_commands() != ()

    recovered = AgentPublicationBroker(
        control,
        repository="o/r",
        base_branch="dev",
        owner="o/r:fix",
        run=remote.run,
    )

    assert recovered.drain_pending_commands() == 1
    assert remote.pr_body == GOOD_BODY.replace("\n", "\r\n")
    assert control.pending_commands() == ()


def test_body_update_refuses_after_origin_is_switched(tmp_path):
    remote = _open_pr(Remote())
    control = store(tmp_path)
    publisher = AgentPublicationBroker(
        control,
        repository="o/r",
        base_branch="dev",
        owner="o/r:fix",
        run=remote.run,
    )
    crashed = {"done": False}

    def crash_once(argv, cwd):
        if argv[:3] == ("gh", "pr", "edit") and not crashed["done"]:
            crashed["done"] = True
            raise KeyboardInterrupt
        return remote.run(argv, cwd)

    publisher.run = crash_once
    with pytest.raises(KeyboardInterrupt):
        publisher.publish(
            ("gh", "pr", "edit", "42", "--body", GOOD_BODY), str(tmp_path)
        )

    remote.origin_url = "git@github.com:attacker/other.git"
    recovered = AgentPublicationBroker(
        control,
        repository="o/r",
        base_branch="dev",
        owner="o/r:fix",
        run=remote.run,
    )

    assert recovered.drain_pending_commands() == 0
    assert remote.pr_body != GOOD_BODY.replace("\n", "\r\n")
