"""The act layer's keyed comment: claimed in SQLite before the write and
confirmed by a read-back after it, so a lost reply or a replay never posts
twice, and a stop comment never lands on an item that is not yet stopped."""

import pytest

from issue_agent.adapters.act import GitHubAct
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.domain.actions import PostComment, Target
from issue_agent.domain.labels import Label

STOP = PostComment(Target.ISSUE, 5, "stopped: attempts exhausted", key="needs-human:5:attempts")


class Gateway:
    def __init__(self, labels=(), lose_reply=False):
        self.labels, self.lose_reply = set(labels), lose_reply
        self.comments: list[str] = []
        self.posted: list[str] = []

    def has_label(self, kind, number, label):
        return label in self.labels

    def has_comment(self, kind, number, body):
        return body in self.comments

    def comment(self, kind, number, body):
        self.posted.append(body)
        self.comments.append(body)
        if self.lose_reply:
            self.lose_reply = False
            raise RuntimeError("the comment landed; its reply was lost")


def _act(tmp_path, gateway) -> GitHubAct:
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()
    return GitHubAct(
        gateway,
        store,
        repository="o/r",
        base_branch="dev",
        merge_method="squash",
        marker="m",
        dispatch=lambda _a: None,
        remove_worktree=lambda _i: None,
        log=lambda _m: None,
    )


def test_a_keyed_comment_lands_once_across_a_lost_reply_and_later_replays(tmp_path):
    gh = Gateway(labels={Label.NEEDS_HUMAN.value}, lose_reply=True)
    act = _act(tmp_path, gh)
    with pytest.raises(RuntimeError, match="reply was lost"):
        act.execute(STOP)
    act.execute(STOP)  # the claimed key reads the landed comment back
    gh.comments.clear()  # deleted on GitHub later: the confirmed key still holds
    act.execute(STOP)

    assert gh.posted == [STOP.body]


def test_a_stop_comment_is_withheld_until_its_needs_human_label_is_on_the_item(tmp_path):
    gh = Gateway()
    with pytest.raises(RuntimeError, match="withheld"):
        _act(tmp_path, gh).execute(STOP)
    assert gh.posted == []


def test_the_triage_comment_posts_once_however_often_derive_asks_for_it(tmp_path):
    from issue_agent.domain.policy import triage_comment
    from issue_agent.domain.routing import Tier
    from issue_agent.domain.state import Harness, TriageNote
    from issue_agent.domain.triage import Steward, Triage

    note = TriageNote(Triage(Tier.LOW), "2026-10-01T12:00:00+00:00", Steward(("Esc closes it",)))
    action = PostComment(Target.ISSUE, 5, triage_comment(note, Harness("fix-bot", "review-bot")),
                         key=f"triage:issue:5:{note.anchor}")
    gh = Gateway(lose_reply=True)
    act = _act(tmp_path, gh)
    with pytest.raises(RuntimeError, match="reply was lost"):
        act.execute(action)
    act.execute(action)
    act.execute(action)  # an observation that lags the post asks again
    assert gh.posted == [action.body]
