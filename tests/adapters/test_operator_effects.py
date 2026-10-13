"""Operator writes: label moves through `act.relabel` (read-before and
read-back, no command queue) and the CLI's bootstrap steps."""

from __future__ import annotations

import json
from types import SimpleNamespace

from issue_agent.adapters.operator_effects import GitHubOperatorEffects


class Labels:
    """A GitHub label surface whose writes can be lost after they land."""

    def __init__(self, **labels: set[str]):
        self.labels = {("issue", 9): set(labels.get("issue", set())), ("pr", 12): set(labels.get("pr", set()))}
        self.writes: list[tuple[str, int, str | None, str | None]] = []
        self.lose_next_reply = False
        self.refuse_writes = False

    def has_label(self, kind, number, label):
        return label in self.labels.setdefault((kind, number), set())

    def relabel(self, kind, number, remove, add):
        if self.refuse_writes:
            raise OSError("no network")
        self.writes.append((kind, number, remove, add))
        current = self.labels.setdefault((kind, number), set())
        if remove is not None:
            current.discard(remove)
        if add is not None:
            current.add(add)
        if self.lose_next_reply:
            self.lose_next_reply = False
            raise OSError("reply lost after the write landed")


class Journal:
    def __init__(self):
        self.events = []

    def emit(self, event, fields):
        self.events.append((event, dict(fields)))


def _effects(gateway, *, run=None, journal=None):
    return GitHubOperatorEffects("o/r", gateway, identity="o/r:dashboard", run=run or _unexpected, journal=journal)


def _unexpected(argv, **_kwargs):
    raise AssertionError(f"unexpected command: {argv}")


def test_label_move_reports_exactly_what_changed_on_github():
    gateway = Labels(issue={"needs-human", "bug"})

    effect = _effects(gateway).set_labels("issue", 9, add=("ai-ready",), remove=("needs-human",))

    assert effect.applied and effect.added == ("ai-ready",) and effect.removed == ("needs-human",)
    assert gateway.labels[("issue", 9)] == {"ai-ready", "bug"}


def test_repeating_a_label_move_writes_nothing_the_second_time():
    gateway = Labels(issue={"needs-human"})
    effects = _effects(gateway)

    effects.set_labels("issue", 9, add=("ai-ready",), remove=("needs-human",))
    writes = len(gateway.writes)
    again = effects.set_labels("issue", 9, add=("ai-ready",), remove=("needs-human",))

    assert again.applied and again.added == () and again.removed == ()
    assert len(gateway.writes) == writes


def test_a_crash_after_the_write_landed_is_not_written_twice_on_retry():
    gateway = Labels(issue=set())
    gateway.lose_next_reply = True
    effects = _effects(gateway)

    first = effects.set_labels("issue", 9, add=("needs-human",))
    retry = effects.set_labels("issue", 9, add=("needs-human",))

    assert first.applied is False
    assert retry.applied and retry.added == ()
    assert gateway.writes == [("issue", 9, None, "needs-human")]
    assert gateway.labels[("issue", 9)] == {"needs-human"}


def test_absent_labels_are_never_removed_blindly():
    gateway = Labels(pr={"ai-pr-open"})

    effect = _effects(gateway).set_labels("pr", 12, remove=("needs-human",))

    assert effect.applied and effect.removed == ()
    assert gateway.writes == []


def test_malformed_label_requests_are_refused_without_a_write():
    gateway = Labels()
    effects = _effects(gateway)

    for kind, number, add, remove in (
        ("issue", 9, (), ()),
        ("discussion", 9, ("x",), ()),
        ("issue", 0, ("x",), ()),
        ("issue", -3, ("x",), ()),
        ("issue", True, ("x",), ()),
    ):
        assert effects.set_labels(kind, number, add=add, remove=remove).applied is False
    assert gateway.writes == []


def test_a_label_both_added_and_removed_is_only_added():
    gateway = Labels(issue=set())

    effect = _effects(gateway).set_labels("issue", 9, add=("ai-ready",), remove=("ai-ready",))

    assert effect.added == ("ai-ready",) and effect.removed == ()
    assert gateway.labels[("issue", 9)] == {"ai-ready"}


def test_an_applied_label_change_is_journalled_as_an_operator_action():
    gateway = Labels(issue={"needs-human"})
    journal = Journal()

    _effects(gateway, journal=journal).set_labels(
        "issue",
        9,
        add=("ai-ready",),
        remove=("needs-human",),
        action_kind="operator_unblock",
        action_attributes={"channel": "ui"},
    )

    ((name, fields),) = journal.events
    action = fields["action"]
    assert name == "action" and action["type"] == "operator_unblock"
    assert action["number"] == 9 and fields["issue"] == 9
    assert action["add"] == ["ai-ready"]
    assert action["remove"] == ["needs-human"]
    assert action["operator"] == "o/r:dashboard"
    assert action["channel"] == "ui"


def test_a_label_change_that_changed_nothing_is_not_journalled():
    journal = Journal()

    _effects(Labels(issue={"P1"}), journal=journal).set_issue_labels(9, add=("P1",))

    assert journal.events == []


def test_a_failed_label_change_is_not_journalled_as_applied():
    gateway = Labels(issue={"needs-human"})
    gateway.refuse_writes = True
    journal = Journal()

    applied, detail = _effects(gateway, journal=journal).set_issue_labels(9, remove=("needs-human",))

    assert applied is False and "no network" in detail
    assert journal.events == []


class Remote:
    """`gh`/`git` for the bootstrap steps."""

    def __init__(self):
        self.calls = []
        self.catalog: set[str] = set()
        self.remote_branches: dict[str, str] = {}
        self.local_head = "a" * 40

    def result(self, stdout="", returncode=0, stderr=""):
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)

    def run(self, argv, **_kwargs):
        self.calls.append(tuple(argv))
        if argv[:3] == ["gh", "label", "list"]:
            return self.result(json.dumps([{"name": label} for label in sorted(self.catalog)]))
        if argv[:3] == ["gh", "label", "create"]:
            self.catalog.add(argv[3])
            return self.result()
        if argv[:3] == ["git", "-C", "/repo"] and argv[3] == "rev-parse":
            return self.result(self.local_head + "\n")
        if argv[:3] == ["git", "-C", "/repo"] and argv[3] == "ls-remote":
            branch = argv[-1].rsplit("/", 1)[-1]
            head = self.remote_branches.get(branch, "")
            return self.result(f"{head}\trefs/heads/{branch}\n" if head else "")
        if argv[:3] == ["git", "-C", "/repo"] and argv[3] == "push":
            self.remote_branches[argv[-1]] = self.local_head
            return self.result()
        raise AssertionError(f"unexpected command: {argv}")


def test_bootstrap_steps_land_and_are_idempotent():
    remote = Remote()
    effects = _effects(Labels(), run=remote.run)

    for _ in range(2):
        assert effects.ensure_label("P1", color="b60205") == (True, "ok")
        assert effects.publish_base_branch("/repo", "dev") == (True, "ok")

    assert remote.catalog == {"P1"}
    assert remote.remote_branches == {"dev": remote.local_head}
    mutations = [c for c in remote.calls if c[:3] == ("gh", "label", "create") or "push" in c]
    assert len(mutations) == 2


def test_a_remote_base_branch_at_another_revision_is_never_overwritten():
    remote = Remote()
    remote.remote_branches["dev"] = "b" * 40

    applied, detail = _effects(Labels(), run=remote.run).publish_base_branch("/repo", "dev")

    assert applied is False and "unexpected revision" in detail
    assert not any("push" in call for call in remote.calls)
