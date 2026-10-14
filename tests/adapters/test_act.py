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


# --- cancelling the workflow runs a merge left running ---------------------

import json  # noqa: E402

from issue_agent.adapters.github import GhUnavailable, GitHubGateway  # noqa: E402

CUTOFF = "2026-10-14T10:00:00Z"


class _Gh:
    """A fake `gh` at the runner seam, answering the way the real runner does:
    a refused cancel (403/409) is an EMPTY answer, an accepted one `{}`."""

    def __init__(self, runs, *, refused=()):
        self.runs, self.refused = runs, set(refused)
        self.cancelled: list[int] = []
        self.listed = ""

    def __call__(self, argv, token):
        if argv[1] == "api" and "/actions/runs?" in argv[2]:
            self.listed = argv[2]
            return self.runs if isinstance(self.runs, str) else json.dumps({"workflow_runs": self.runs})
        if argv[1:3] == ["api", "--method"] and argv[4].endswith("/cancel"):
            run_id = int(argv[4].split("/")[-2])
            if run_id in self.refused:
                return ""
            self.cancelled.append(run_id)
            return "{}"
        raise AssertionError(argv)


def _cancel(tmp_path, gh):
    lines: list[str] = []
    act = _act(tmp_path, GitHubGateway("o/r", runner=gh))
    act.log = lines.append
    act.cancel_runs("PR #7", "fix/3-slug", CUTOFF)
    return lines


def _run(i, status, created="2026-10-14T09:00:00Z", repo="o/r"):
    return {"id": i, "status": status, "created_at": created, "head_repository": {"full_name": repo}}


def test_unfinished_runs_are_cancelled_but_not_finished_ones_or_post_merge_workflows(tmp_path):
    gh = _Gh([_run(1, "in_progress"), _run(2, "queued"), _run(3, "completed"),
              _run(4, "queued", "2026-10-14T10:00:00Z"), _run(5, "in_progress", "2026-10-14T10:00:05Z")])
    _cancel(tmp_path, gh)
    assert gh.cancelled == [1, 2] and "branch=fix%2F3-slug" in gh.listed


def test_a_fork_run_on_a_same_named_branch_is_never_cancelled(tmp_path):
    odd = {"id": 5, "status": "queued", "created_at": "2026-10-14T09:00:00Z"}  # head repository absent
    bad = {**odd, "id": 6, "head_repository": {"full_name": 7}}  # malformed
    null = {**odd, "id": 7, "head_repository": None}  # deleted fork
    gh = _Gh([_run(1, "queued", repo="someone/r"), _run(2, "queued"), _run(3, "queued", repo="O/R"), odd, bad, null])
    _cancel(tmp_path, gh)
    assert gh.cancelled == [2, 3]


def test_a_braces_body_is_a_successful_cancel_and_an_empty_answer_a_refusal(tmp_path):
    assert _cancel(tmp_path, _Gh([_run(1, "queued")])) == ["PR #7: cancelled 1/1 workflow runs left on the merged branch"]
    assert "0/1" in _cancel(tmp_path, _Gh([_run(1, "queued")], refused={1}))[0]


def test_a_refused_cancel_is_logged_once_and_the_rest_still_cancel(tmp_path):
    gh = _Gh([_run(1, "queued"), _run(2, "queued"), _run(3, "queued")], refused={1, 2})
    lines = _cancel(tmp_path, gh)
    assert gh.cancelled == [3] and len(lines) == 1 and "1/3" in lines[0] and "refused" in lines[0]


def test_a_throttled_github_stops_the_cancel_loop(tmp_path):
    class Throttled(_Gh):
        def __call__(self, argv, token):
            if argv[1:3] == ["api", "--method"]:
                self.cancelled.append(-1)
                raise GhUnavailable("rate-limit", "slow down")
            return super().__call__(argv, token)

    gh = Throttled([_run(i, "queued") for i in range(1, 50)])
    lines = _cancel(tmp_path, gh)
    assert gh.cancelled == [-1] and len(lines) == 1 and "0/49" in lines[0]


@pytest.mark.parametrize("runs", ["not json{", "{}", '"text"', "null", "[1, null]", '{"workflow_runs": 3}',
                                  '{"workflow_runs": [1, null, {"id": "x"}]}'])
def test_a_malformed_run_list_is_survived_without_cancelling(tmp_path, runs):
    gh = _Gh(runs)
    assert gh.cancelled == [] and len(_cancel(tmp_path, gh)) <= 1 and gh.cancelled == []


def test_an_empty_run_list_is_silent(tmp_path):
    assert _cancel(tmp_path, _Gh([])) == []


def test_a_huge_run_list_is_bounded(tmp_path):
    gh = _Gh([_run(i, "queued") for i in range(1, 5001)])
    _cancel(tmp_path, gh)
    assert gh.cancelled == list(range(1, 101))


def test_an_unreachable_github_cancels_nothing_and_does_not_raise(tmp_path):
    def down(argv, token):
        raise RuntimeError("gh exploded")

    lines = _cancel(tmp_path, down)
    assert len(lines) == 1 and "unavailable" in lines[0]
