"""The act layer's keyed comment: claimed in SQLite before the write and
confirmed by a read-back after it, so a lost reply or a replay never posts
twice, and a stop comment never lands on an item that is not yet stopped."""

import pytest

from issue_agent.adapters.act import GitHubAct
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.github import BlockerLinkFacts, FixObservationIncomplete, GhUnavailable
from issue_agent.domain.actions import LinkBlockedBy, PostComment, Target
from issue_agent.domain.labels import Label
from issue_agent.domain.policy import blocker_comment
from issue_agent.domain.state import BLOCKER_TAG

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


# ── a steward's "blocked by" claim ───────────────────────────────────────────

LINK = LinkBlockedBy(5, 4, "Needs the export API #4 adds", key="blocked-by:issue:5:4:2026-10-01T12:00:00+00:00")


class LinkGateway(Gateway):
    """GitHub's two issues of a link: whether #4 is open, what blocks it, and the link itself."""

    def __init__(self, *, blocker_open=True, blocker_blocked_by=(), linked=False, drop_link=False, **kw):
        super().__init__(**kw)
        self.blocker_open, self.blocker_blocked_by, self.linked = blocker_open, tuple(blocker_blocked_by), linked
        self.drop_link = drop_link  # the mutation answers but the link never shows
        self.mutations: list[tuple[str, str]] = []

    def blocker_link(self, issue, blocker):
        return BlockerLinkFacts(f"I{issue}", f"I{blocker}", self.blocker_open, self.linked, self.blocker_blocked_by)

    def add_blocked_by(self, issue_id, blocker_id):
        self.mutations.append((issue_id, blocker_id))
        self.linked = not self.drop_link


def test_a_valid_claim_is_linked_read_back_and_explained_once(tmp_path):
    gh = LinkGateway()
    act = _act(tmp_path, gh)
    act.execute(LINK)
    assert gh.mutations == [("I5", "I4")] and gh.posted == [blocker_comment("m", 4, LINK.reason)]


def test_after_a_human_deletes_the_link_and_the_comment_the_link_stays_gone_and_the_matter_closes(tmp_path):
    gh = LinkGateway()
    act = _act(tmp_path, gh)
    act.execute(LINK)
    gh.linked = False
    gh.comments.clear()  # derive, missing its comment, asks again
    act.execute(LINK)
    act.execute(LINK)  # the closing comment is there: nothing more

    assert gh.mutations == [("I5", "I4")]
    closing = gh.posted[1:]
    assert len(closing) == 1 and "will not write that link again" in closing[0] and BLOCKER_TAG in closing[0]


def test_an_existing_link_is_not_written_again_but_still_explained(tmp_path):
    gh = LinkGateway(linked=True)
    _act(tmp_path, gh).execute(LINK)
    assert gh.mutations == [] and gh.posted == [blocker_comment("m", 4, LINK.reason)]


@pytest.mark.parametrize(
    ("facts", "refusal"),
    [({"blocker_open": False}, "#4 is closed"),
     ({"blocker_blocked_by": (9, 5)}, "#4 is itself blocked by this issue, so linking would make a cycle")],
)
def test_a_claim_fresh_reads_refuse_is_never_linked_and_says_why(tmp_path, facts, refusal):
    gh = LinkGateway(**facts)
    _act(tmp_path, gh).execute(LINK)
    assert gh.mutations == [] and gh.posted == [blocker_comment("m", 4, LINK.reason, refusal)]
    assert "did not link" in gh.posted[0]


def test_a_link_that_does_not_read_back_raises_and_posts_nothing(tmp_path):
    gh = LinkGateway(drop_link=True)
    with pytest.raises(RuntimeError, match="not confirmed"):
        _act(tmp_path, gh).execute(LINK)
    assert gh.posted == []


def test_a_lost_comment_reply_is_read_back_on_the_retry_and_the_link_not_rewritten(tmp_path):
    gh = LinkGateway(lose_reply=True)
    act = _act(tmp_path, gh)
    with pytest.raises(RuntimeError, match="reply was lost"):
        act.execute(LINK)
    act.execute(LINK)
    assert gh.mutations == [("I5", "I4")] and gh.posted == [blocker_comment("m", 4, LINK.reason)]


def test_a_link_github_refuses_to_read_or_write_is_explained_once_not_retried_every_tick(tmp_path):
    class Refusing(LinkGateway):
        def add_blocked_by(self, issue_id, blocker_id):
            self.mutations.append((issue_id, blocker_id))
            raise FixObservationIncomplete("GraphQL answered with errors or no data")

    gh = Refusing()
    act = _act(tmp_path, gh)
    act.execute(LINK)  # no raise: the keyed write is settled, so the tick does not retry it
    act.execute(LINK)
    assert gh.mutations == [("I5", "I4")] and "did not link it: GitHub would not" in gh.posted[0]


def test_no_answer_from_github_is_retried_next_tick_not_a_refusal(tmp_path):
    class Down(LinkGateway):
        def blocker_link(self, issue, blocker):
            raise GhUnavailable("network", "timed out")

    gh = Down()
    with pytest.raises(GhUnavailable):
        _act(tmp_path, gh).execute(LINK)
    assert gh.posted == []


def test_an_existing_link_is_reported_as_linked_even_if_the_blocker_closed_since(tmp_path):
    gh = LinkGateway(linked=True, blocker_open=False)
    _act(tmp_path, gh).execute(LINK)
    assert gh.mutations == [] and gh.posted == [blocker_comment("m", 4, LINK.reason)]


def test_an_unreadable_link_read_is_a_refusal_not_a_retry_every_tick(tmp_path):
    class Unreadable(LinkGateway):
        def blocker_link(self, issue, blocker):
            raise FixObservationIncomplete("#4's own blockers are unreadable: 1 blocker(s) unseen")

    gh = Unreadable()
    _act(tmp_path, gh).execute(LINK)  # no raise: settled as a refusal
    assert gh.mutations == [] and len(gh.posted) == 1 and "GitHub would not read or write" in gh.posted[0]


def test_a_link_that_lands_before_github_answers_with_an_error_is_reported_as_linked(tmp_path):
    class LandsThenErrors(LinkGateway):
        def add_blocked_by(self, issue_id, blocker_id):
            super().add_blocked_by(issue_id, blocker_id)
            raise FixObservationIncomplete("GraphQL answered with errors or no data")

    gh = LandsThenErrors()
    _act(tmp_path, gh).execute(LINK)
    assert gh.linked and gh.posted == [blocker_comment("m", 4, LINK.reason)]
