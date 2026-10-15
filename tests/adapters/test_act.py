"""The act layer's keyed writes: claimed in SQLite before the write and
settled by GitHub's answer (#440), read back only when the answer was lost
or a settled write was deleted, so a lost reply or a replay never posts
twice, and a stop comment never lands on an item that is not yet stopped."""

import uuid
from types import SimpleNamespace

import pytest

from issue_agent.adapters.act import GitHubAct
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.github import Accepted, BlockerLinkFacts, FixObservationIncomplete, GhUnavailable, Refused
from issue_agent.domain.actions import AgentKind, LinkBlockedBy, OpenPullRequest, PostComment, Target
from issue_agent.domain.contracts import RunId
from issue_agent.domain.labels import Label
from issue_agent.domain.policy import blocker_comment
from issue_agent.domain.state import BLOCKER_TAG

STOP = PostComment(Target.ISSUE, 5, "stopped: attempts exhausted", key="needs-human:5:attempts")


class Gateway:
    """GitHub's comments. `lose_reply`: the next comment lands but its answer
    is lost (NoAnswer); `refuse`: GitHub answers every comment with it and
    writes nothing; `lag`: reads do not show the writes yet."""

    def __init__(self, labels=(), lose_reply=False):
        self.labels, self.lose_reply = set(labels), lose_reply
        self.comments: list[tuple[str, str]] = []
        self.posted: list[str] = []
        self.refuse: Refused | None = None
        self.lag = False

    def has_label(self, kind, number, label):
        return label in self.labels

    def has_comment(self, kind, number, body, author):
        return not self.lag and (author, body) in self.comments

    def comment(self, kind, number, body):
        self.posted.append(body)
        if self.refuse is not None:
            return self.refuse
        self.comments.append(("fixer", body))
        if self.lose_reply:
            self.lose_reply = False
            raise GhUnavailable("network", "the comment landed; its reply was lost")
        return Accepted(len(self.comments))


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
        bot="fixer",
        dispatch=lambda _a: None,
        remove_worktree=lambda _i: None,
        log=lambda _m: None,
    )


def test_a_keyed_comment_lands_once_across_a_lost_reply_and_later_replays(tmp_path):
    gh = Gateway(labels={Label.NEEDS_HUMAN.value}, lose_reply=True)
    act = _act(tmp_path, gh)
    with pytest.raises(GhUnavailable, match="reply was lost"):
        act.execute(STOP)
    act.execute(STOP)  # the claimed key reads the landed comment back
    act.execute(STOP)  # derive asks again on a stale observation: the comment is there

    assert gh.posted == [STOP.body]


def test_a_settled_comment_a_human_deleted_is_posted_again_once_and_logged(tmp_path):
    gh, logs = Gateway(labels={Label.NEEDS_HUMAN.value}), []
    act = _act(tmp_path, gh)
    act.log = logs.append
    act.execute(STOP)
    gh.comments.clear()  # deleted on GitHub: GitHub, not the settled key, says it is gone
    act.execute(STOP)
    act.execute(STOP)  # the repost is there: nothing more

    assert gh.posted == [STOP.body, STOP.body]
    assert len(logs) == 1 and "deleted" in logs[0] and STOP.key in logs[0]


def test_a_comment_whose_presence_cannot_be_read_is_not_posted_again(tmp_path):
    from issue_agent.adapters.github import GitHubGateway

    class Unreadable(Gateway):
        reply: object = "readable"

        def has_comment(self, kind, number, body, author):
            if self.reply == "readable":
                return super().has_comment(kind, number, body, author)
            return GitHubGateway.has_comment(SimpleNamespace(_view=lambda *a: self.reply), kind, number, body, author)

    gh = Unreadable(labels={Label.NEEDS_HUMAN.value})
    act = _act(tmp_path, gh)
    act.execute(STOP)
    for reply in (None, [], {}, {"comments": None}, {"comments": "x"}):
        gh.reply = reply
        with pytest.raises(FixObservationIncomplete):
            act.execute(STOP)
    gh.reply = {"comments": [None, 7, {"body": STOP.body, "author": {"login": "fixer"}}]}  # junk entries beside the real one
    act.execute(STOP)
    assert gh.posted == [STOP.body]


def test_a_stop_comment_is_withheld_until_its_needs_human_label_is_on_the_item(tmp_path):
    gh = Gateway()
    with pytest.raises(RuntimeError, match="withheld"):
        _act(tmp_path, gh).execute(STOP)
    assert gh.posted == []


def test_an_accepted_comment_is_settled_by_its_answer_however_far_the_read_lags(tmp_path):
    gh, logs = Gateway(labels={Label.NEEDS_HUMAN.value}), []
    act = _act(tmp_path, gh)
    act.log = logs.append
    gh.lag = True  # #433's class: the comment read lags the POST
    act.execute(STOP)  # no raise: the 201 is the receipt
    gh.lag = False
    act.execute(STOP)  # derive asks again on a stale observation: it is there
    assert gh.posted == [STOP.body] and logs == []


def test_a_refused_comment_is_settled_once_with_one_line_and_not_retried_by_act(tmp_path):
    gh, logs = Gateway(labels={Label.NEEDS_HUMAN.value}), []
    gh.refuse = Refused(403, "Unable to create comment because issue is locked.")
    act = _act(tmp_path, gh)
    act.log = logs.append
    act.execute(STOP)  # no raise: GitHub answered, and repeating it changes nothing
    assert gh.posted == [STOP.body]
    assert logs == [f"issue #5: comment {STOP.key}: GitHub refused it (HTTP 403), not retried: "
                    "Unable to create comment because issue is locked."]


def test_an_unanswered_comment_that_never_landed_is_posted_on_the_next_tick(tmp_path):
    class Lost(Gateway):
        def comment(self, kind, number, body):
            self.posted.append(body)
            if len(self.posted) == 1:
                raise GhUnavailable("network", "gh timed out")  # before GitHub saw it
            return super().comment(kind, number, body)

    gh = Lost(labels={Label.NEEDS_HUMAN.value})
    act = _act(tmp_path, gh)
    with pytest.raises(GhUnavailable):
        act.execute(STOP)
    act.execute(STOP)
    act.execute(STOP)
    assert gh.comments == [("fixer", STOP.body)]


def test_a_crash_before_the_post_leaves_a_claim_that_the_next_tick_writes_once(tmp_path):
    class Crashing(Gateway):
        crash = True

        def comment(self, kind, number, body):
            if self.crash:
                self.crash = False
                raise KeyboardInterrupt  # the process dies after the claim, before the POST
            return super().comment(kind, number, body)

    gh = Crashing(labels={Label.NEEDS_HUMAN.value})
    act = _act(tmp_path, gh)
    with pytest.raises(KeyboardInterrupt):
        act.execute(STOP)
    act.execute(STOP)
    act.execute(STOP)
    assert gh.posted == [STOP.body]


def test_a_crash_between_the_answer_and_the_confirm_is_read_back_not_posted_twice(tmp_path):
    gh = Gateway(labels={Label.NEEDS_HUMAN.value})
    act = _act(tmp_path, gh)
    confirm = act.control.confirm_key

    def crash(key):
        act.control.confirm_key = confirm
        raise KeyboardInterrupt  # the POST was answered; the process dies before the confirm

    act.control.confirm_key = crash
    with pytest.raises(KeyboardInterrupt):
        act.execute(STOP)
    act.execute(STOP)
    act.execute(STOP)
    assert gh.posted == [STOP.body]


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
    with pytest.raises(GhUnavailable, match="reply was lost"):
        act.execute(action)
    act.execute(action)
    act.execute(action)  # an observation that lags the post asks again
    assert gh.posted == [action.body]


def test_a_deleted_triage_comment_is_posted_again_so_the_issue_reaches_resolve(tmp_path):
    from issue_agent.domain.policy import triage_comment
    from issue_agent.domain.routing import Tier
    from issue_agent.domain.state import Harness, TriageNote
    from issue_agent.domain.triage import Steward, Triage

    harness = Harness("fix-bot", "review-bot")
    note = TriageNote(Triage(Tier.LOW), "2026-10-01T12:00:00+00:00", Steward(("Esc closes it",)))
    action = PostComment(Target.ISSUE, 5, triage_comment(note, harness), key=f"triage:issue:5:{note.anchor}")
    gh = Gateway()
    act = _act(tmp_path, gh)
    act.execute(action)
    gh.comments.clear()  # a human deletes it; derive keeps returning TriageNote
    act.execute(action)
    act.execute(action)  # present again: nothing more
    assert gh.posted == [action.body, action.body] and gh.comments == [("fixer", action.body)]


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
        return Accepted()


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


def test_an_accepted_link_is_explained_as_linked_though_the_link_read_lags(tmp_path):
    gh = LinkGateway(drop_link=True)  # the mutation's answer is accepted; a read would not show it yet
    _act(tmp_path, gh).execute(LINK)
    assert gh.mutations == [("I5", "I4")] and gh.posted == [blocker_comment("m", 4, LINK.reason)]


def test_a_lost_comment_reply_is_read_back_on_the_retry_and_the_link_not_rewritten(tmp_path):
    gh = LinkGateway(lose_reply=True)
    act = _act(tmp_path, gh)
    with pytest.raises(GhUnavailable, match="reply was lost"):
        act.execute(LINK)
    act.execute(LINK)
    assert gh.mutations == [("I5", "I4")] and gh.posted == [blocker_comment("m", 4, LINK.reason)]


def test_a_link_github_refuses_to_read_or_write_is_explained_once_not_retried_every_tick(tmp_path):
    class Refusing(LinkGateway):
        def add_blocked_by(self, issue_id, blocker_id):
            self.mutations.append((issue_id, blocker_id))
            return Refused(200, "Could not resolve to a node")

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


def test_an_unanswered_link_that_landed_is_read_back_on_the_next_tick_not_written_again(tmp_path):
    class LandsUnanswered(LinkGateway):
        def add_blocked_by(self, issue_id, blocker_id):
            super().add_blocked_by(issue_id, blocker_id)
            raise GhUnavailable("network", "gh timed out")

    gh = LandsUnanswered()
    act = _act(tmp_path, gh)
    with pytest.raises(GhUnavailable):
        act.execute(LINK)
    act.execute(LINK)
    assert gh.mutations == [("I5", "I4")] and gh.posted == [blocker_comment("m", 4, LINK.reason)]


# --- cancelling the workflow runs a merge left running ---------------------

import json  # noqa: E402

from issue_agent.adapters.github import GhUnavailable, GitHubGateway  # noqa: E402

CUTOFF = "2026-10-14T10:00:00Z"


def _http(status: int, body: str = "{}") -> str:
    return f"HTTP/2.0 {status} X\r\nContent-Type: application/json\r\n\r\n{body}"


class _Gh:
    """A fake `gh` at the runner seam, answering `gh api --include` the way
    GitHub does: a refused cancel is a 409, an accepted one a 202."""

    def __init__(self, runs, *, refused=()):
        self.runs, self.refused = runs, set(refused)
        self.cancelled: list[int] = []
        self.listed = ""

    def __call__(self, argv, token):
        if argv[1] == "api" and "/actions/runs?" in argv[2]:
            self.listed = argv[2]
            return self.runs if isinstance(self.runs, str) else json.dumps({"workflow_runs": self.runs})
        if argv[1:4] == ["api", "--include", "--method"] and argv[5].endswith("/cancel"):
            run_id = int(argv[5].split("/")[-2])
            if run_id in self.refused:
                return _http(409, '{"message": "Cannot cancel a workflow run that is completed."}')
            self.cancelled.append(run_id)
            return _http(202)
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


def test_a_202_is_a_successful_cancel_and_a_409_a_refusal(tmp_path):
    assert _cancel(tmp_path, _Gh([_run(1, "queued")])) == ["PR #7: cancelled 1/1 workflow runs left on the merged branch"]
    assert "0/1" in _cancel(tmp_path, _Gh([_run(1, "queued")], refused={1}))[0]


def test_a_refused_cancel_is_logged_once_and_the_rest_still_cancel(tmp_path):
    gh = _Gh([_run(1, "queued"), _run(2, "queued"), _run(3, "queued")], refused={1, 2})
    lines = _cancel(tmp_path, gh)
    assert gh.cancelled == [3] and len(lines) == 1 and "1/3" in lines[0] and "refused" in lines[0]


def test_a_throttled_github_stops_the_cancel_loop(tmp_path):
    class Throttled(_Gh):
        def __call__(self, argv, token):
            if argv[1:4] == ["api", "--include", "--method"]:
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


# ── gap 22: base-CI issues ───────────────────────────────────────────────────

from issue_agent.domain.actions import FileIssue, SetIssueState  # noqa: E402
from issue_agent.domain.world import IssueView  # noqa: E402

FILE = FileIssue("CI: macOS Smoke failing on dev", "m\nbody", key="base-ci:dev:CI: macOS Smoke failing on dev")


class IssueGateway(Gateway):
    """GitHub's issues as act writes them: created open and unlabelled, reopened and closed on request."""

    def __init__(self, *, lose_reply=False, ignore=False, **kw):
        super().__init__(**kw)
        self.issues: dict[int, IssueView] = {}
        self.lose_reply, self.ignore = lose_reply, ignore
        self.writes: list[tuple[str, int, str]] = []

    def create_issue(self, title, body):
        number = 100 + len(self.issues)
        self.issues[number] = IssueView(number, title, body, author="fix-bot")
        if self.lose_reply:
            raise GhUnavailable("timeout", "gh timed out", argv=["gh", "issue", "create"])
        return Accepted(number)

    def issue_index(self, limit=300, author=""):
        assert author == "@me"  # only the token's own issues can be the harness's
        return tuple((n, i.state, i.title, i.labels) for n, i in self.issues.items() if i.author == "fix-bot")

    def get_issue(self, number):
        return self.issues[number]

    def is_issue_closed(self, number):
        return self.issues[number].state == "closed"

    def _set(self, verb, number, comment, state):
        self.writes.append((verb, number, comment))
        if not self.ignore:
            self.issues[number] = IssueView(number, self.issues[number].title, state=state)

    def close_issue(self, number, reason, comment):
        self._set("close", number, comment, "closed")

    def reopen_issue(self, number, comment):
        self._set("reopen", number, comment, "open")


def test_a_filed_issue_is_confirmed_once_and_a_lagging_search_never_files_it_again(tmp_path):
    gh = IssueGateway()
    act = _act(tmp_path, gh)
    act.execute(FILE)
    act.execute(FILE)  # an issue search that lags the filing asks again
    assert [(i.title, i.body, i.labels) for i in gh.issues.values()] == [(FILE.title, FILE.body, frozenset())]


def test_a_filing_is_settled_by_its_answer_though_the_listing_lags_and_logged_with_its_number(tmp_path):
    class Lagging(IssueGateway):
        lag = True

        def issue_index(self, limit=300, author=""):
            return () if self.lag else super().issue_index(limit, author)

    gh, logs = Lagging(), []
    act = _act(tmp_path, gh)
    act.log = logs.append
    act.execute(FILE)  # nothing read after the create: the 201 carries #100
    gh.lag = False
    act.execute(FILE)
    assert list(gh.issues) == [100] and logs == [f"issue #100: filed {FILE.title!r}"]


def test_a_filing_github_refuses_is_settled_with_one_line(tmp_path):
    class Refusing(IssueGateway):
        def create_issue(self, title, body):
            self.writes.append(("create", 0, title))
            return Refused(410, "Issues are disabled for this repo")

    gh, logs = Refusing(), []
    act = _act(tmp_path, gh)
    act.log = logs.append
    act.execute(FILE)  # no raise
    assert gh.writes == [("create", 0, FILE.title)] and gh.issues == {}
    assert logs == [f"issue {FILE.title!r}: GitHub refused it (HTTP 410), not retried: Issues are disabled for this repo"]


def test_a_filed_issue_a_human_deleted_or_retitled_is_filed_again_once_and_logged(tmp_path):
    for gone in ("deleted", "retitled"):
        gh, logs = IssueGateway(), []
        act = _act(tmp_path / gone, gh)
        act.log = logs.append
        act.execute(FILE)
        (number,) = gh.issues
        if gone == "deleted":
            del gh.issues[number]
        else:
            gh.issues[number] = IssueView(number, "flaky macOS smoke, ignore", author="fix-bot")
        gh.issues[7] = IssueView(7, FILE.title, author="alice")  # a human's issue of that title is not the harness's
        act.execute(FILE)  # derive no longer finds the issue under its title and asks again
        act.execute(FILE)  # its search lags the new filing
        assert [i.author for i in gh.issues.values() if i.title == FILE.title] == ["alice", "fix-bot"]
        assert [m for m in logs if "deleted" in m] == [f"issue {FILE.title!r} was deleted; written again"]


def test_a_filing_whose_reply_was_lost_is_found_in_the_issue_listing_not_filed_again(tmp_path):
    gh = IssueGateway(lose_reply=True)
    act = _act(tmp_path, gh)
    with pytest.raises(GhUnavailable):
        act.execute(FILE)
    gh.lose_reply = False
    act.execute(FILE)  # the issue search has not indexed it yet, so derive asked again
    act.execute(FILE)
    assert [i.title for i in gh.issues.values()] == [FILE.title]


def test_a_claimed_filing_that_never_landed_is_filed(tmp_path):
    gh = IssueGateway(lose_reply=True)
    act = _act(tmp_path, gh)
    with pytest.raises(GhUnavailable):
        act.execute(FILE)
    gh.issues.clear()  # the create never reached GitHub
    gh.lose_reply = False
    act.execute(FILE)
    assert [i.title for i in gh.issues.values()] == [FILE.title]


def test_close_then_reopen_each_write_once_and_a_replay_in_the_same_state_writes_nothing(tmp_path):
    gh = IssueGateway()
    gh.issues[40] = IssueView(40, "CI: x failing on dev")
    act = _act(tmp_path, gh)
    for action in (SetIssueState(40, False, "m\ngreen"), SetIssueState(40, False, "m\ngreen"),
                   SetIssueState(40, True, "m\nred again"), SetIssueState(40, True, "m\nred again")):
        act.execute(action)
    assert gh.writes == [("close", 40, "m\ngreen"), ("reopen", 40, "m\nred again")]


@pytest.mark.parametrize("action", [SetIssueState(40, False, "m\ngreen"), SetIssueState(41, True, "m\nred")])
def test_a_state_change_that_does_not_read_back_raises(tmp_path, action):
    gh = IssueGateway(ignore=True)
    gh.issues[40] = IssueView(40, "CI: x failing on dev")
    gh.issues[41] = IssueView(41, "CI: y failing on dev", state="closed")
    with pytest.raises(RuntimeError, match="not confirmed"):
        _act(tmp_path, gh).execute(action)


# ── opening the PR (Q11): the agent's description, unchanged ─────────────────


class PrGateway:
    def __init__(self):
        self.opened: list[tuple[str, str, str, str]] = []

    def pull_request_for_branch(self, branch):
        return 100 if self.opened else None

    def get_issue(self, number):
        return SimpleNamespace(title="Null token crashes", body="## Acceptance criteria\n- [ ] 401", comments=())

    def open_pull_request(self, branch, base, title, body):
        self.opened.append((branch, base, title, body))
        return Accepted(100)


def _opened_body(tmp_path, *results) -> str:
    gh = PrGateway()
    act = _act(tmp_path, gh)
    for result in results:
        act.control.record_unpaid_leg(
            RunId(uuid.uuid4().hex), repository="o/r", role="fix", kind="RESOLVE", owner_id="o",
            issue=5, number=5, head="", runtime="claude", leg_class="agent", result=result)
    act.execute(OpenPullRequest(5, "fix/5-null-token"))
    ((branch, base, title, body),) = gh.opened
    assert (branch, base, title) == ("fix/5-null-token", "dev", "Null token crashes")
    return body


@pytest.mark.parametrize(
    "written",
    [
        "Fixed it.\n\n## Acceptance Criteria\n- [x] 401\n\nCloses #5",
        "No criteria and no closing line",  # the harness adds neither
        "Unclosed fence:\n```python\nprint(1)",  # malformed Markdown is the author's
        "x" * 20_000,  # the receipt's ceiling
    ],
    ids=["complete", "bare", "unclosed-fence", "huge"],
)
def test_the_pr_opens_with_the_agents_description_unchanged(tmp_path, written):
    assert _opened_body(tmp_path, {"summary": written}) == written


@pytest.mark.parametrize(
    "results",
    [(), ({"summary": ""},), ({"summary": "  \n"},), ({"summary": 7},), ({},)],
    ids=["no-leg", "empty", "blank", "not-text", "no-summary"],
)
def test_without_a_written_description_the_pr_still_names_its_issue(tmp_path, results):
    assert _opened_body(tmp_path, *results) == "Closes #5"


def test_a_refused_pr_open_raises_with_githubs_reason_and_opens_nothing(tmp_path):
    class Refusing(PrGateway):
        def open_pull_request(self, branch, base, title, body):
            return Refused(422, "No commits between dev and fix/5-null-token")

    with pytest.raises(RuntimeError, match="refused \\(HTTP 422\\) No commits between"):
        _act(tmp_path, Refusing()).execute(OpenPullRequest(5, "fix/5-null-token"))


# ── the harness's one re-run of a head (owner, PR #403) ─────────────────────
class RunGateway(Gateway):
    """Workflow runs by id -> current attempt; a POST re-run bumps it, but the
    run read lags it as GitHub's does (#433): `lagging` runs still read the old attempt."""

    def __init__(self, attempts):
        super().__init__()
        self.attempts = dict(attempts)
        self.inert_runs: set[int] = set()  # GitHub refuses these (403, too old): the attempt never moves
        self.lagging: set[int] = set()
        self.reruns: list[int] = []

    def run_attempt(self, run_id):
        return self.attempts[run_id]

    def rerun_failed_checks(self, run_id):
        self.reruns.append(run_id)
        if run_id in self.inert_runs:
            return Refused(403, "Must have admin rights to Repository.")
        if run_id not in self.lagging:
            self.attempts[run_id] += 1
        return Accepted()


def _rerun_act(tmp_path, gh, lines):
    act = _act(tmp_path, gh)
    act.log = lines.append
    return act


def test_a_rerun_lands_once_with_one_journal_line_and_a_replay_posts_nothing(tmp_path):
    from issue_agent.domain.actions import RerunFailedJobs

    gh, lines = RunGateway({900: 1, 901: 1}), []
    act = _rerun_act(tmp_path, gh, lines)
    action = RerunFailedJobs(7, "h1" * 10, ((900, 1), (901, 1)))
    act.execute(action)
    act.execute(action)  # derive asked again before the observation showed the new attempt
    assert gh.reruns == [900, 901]
    assert [x for x in lines if "re-ran" in x] == [
        f"PR #7: re-ran the failed jobs of run {r} on h1h1h1h1h1h1 (the harness's one re-run of this head)"
        for r in (900, 901)]


def test_a_rerun_whose_run_read_still_shows_the_old_attempt_counts_as_landed(tmp_path):
    from issue_agent.domain.actions import RerunFailedJobs

    gh, lines = RunGateway({900: 1}), []
    gh.lagging = {900}  # 201 on the POST, old attempt on the immediate GET
    _rerun_act(tmp_path, gh, lines).execute(RerunFailedJobs(7, "h1", ((900, 1),)))
    assert gh.reruns == [900]
    assert [x for x in lines if "re-ran" in x] == [
        "PR #7: re-ran the failed jobs of run 900 on h1 (the harness's one re-run of this head)"]


def test_a_refused_rerun_raises_after_journaling_the_runs_that_landed(tmp_path):
    from issue_agent.domain.actions import RerunFailedJobs

    gh, lines = RunGateway({900: 1, 901: 1}), []
    gh.inert_runs = {901}
    with pytest.raises(RuntimeError, match="run 901: re-run refused \\(HTTP 403\\)"):
        _rerun_act(tmp_path, gh, lines).execute(RerunFailedJobs(7, "h1", ((900, 1), (901, 1))))
    assert [x for x in lines if "re-ran" in x] == [
        "PR #7: re-ran the failed jobs of run 900 on h1 (the harness's one re-run of this head)"]


def test_an_unanswered_rerun_raises_and_the_next_tick_rereads_the_attempt_before_posting(tmp_path):
    from issue_agent.domain.actions import RerunFailedJobs

    class Unanswered(RunGateway):
        def rerun_failed_checks(self, run_id):
            super().rerun_failed_checks(run_id)  # GitHub took it
            raise GhUnavailable("network", "gh timed out")

    gh = Unanswered({900: 1})
    action = RerunFailedJobs(7, "h1", ((900, 1),))
    with pytest.raises(GhUnavailable):
        _rerun_act(tmp_path, gh, []).execute(action)
    _rerun_act(tmp_path, gh, []).execute(action)  # the new attempt reads back: not posted again
    assert gh.reruns == [900]


def test_a_stop_comment_left_by_a_previous_bot_login_is_posted_again_under_the_current_one(tmp_path):
    """#424: FIX_BOT changed after the stop comment landed. The old copy reads
    as human, so the item would never release; it is posted afresh."""
    gh = Gateway(labels={Label.NEEDS_HUMAN.value})
    act = _act(tmp_path, gh)
    act.execute(STOP)
    gh.comments = [("old-fixer", STOP.body)]  # the login changed between ticks
    act.execute(STOP)
    act.execute(STOP)

    assert gh.posted == [STOP.body, STOP.body]
    assert ("fixer", STOP.body) in gh.comments


# ── harness gaps, reported into the harness's own repository ────────────────

from datetime import datetime, timedelta, timezone  # noqa: E402

from issue_agent.domain.actions import ReportGap  # noqa: E402
from issue_agent.domain.gaps import GAP_REPORTS_PER_DAY  # noqa: E402

HARNESS = "xnmp/autonomous-issue-agent"


def _gap(item: int, title: str = "Harness gap: agent-reported harness defect in o/r") -> ReportGap:
    return ReportGap(HARNESS, title, f"gap:{HARNESS}:o/r:issue:{item}:{title}", f"m\nfiled from o/r#{item}",
                     f"m\nseen again on o/r#{item}")


class HarnessRepo(IssueGateway):
    """The harness repository, as the fix credential sees it; counts its reads."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.reads, self.hidden = 0, set()

    def issue_index(self, limit=300, author=""):
        self.reads += 1
        return tuple(row for row in super().issue_index(limit, author) if row[0] not in self.hidden)

    def comment(self, kind, number, body):
        self.posted.append(body)
        self.comments.append(("fixer", body))
        if self.lose_reply:
            self.lose_reply = False
            raise GhUnavailable("timeout", "gh timed out", argv=["gh", "issue", "comment"])
        return Accepted(len(self.comments))


class TargetRepo(Gateway):
    def __init__(self, harness: HarnessRepo):
        super().__init__()
        self.harness, self.repos = harness, []

    def for_repo(self, repo):
        self.repos.append(repo)
        return self.harness


def _gap_act(tmp_path, harness: HarnessRepo, logs: list[str] | None = None, now=None):
    store = SQLiteControlStore(str(tmp_path / "control.db"), now=now)
    store.initialize()
    return GitHubAct(TargetRepo(harness), store, repository="o/r", base_branch="dev", merge_method="squash",
                     marker="m", bot="fixer", dispatch=lambda _a: None, remove_worktree=lambda _i: None,
                     log=(logs if logs is not None else []).append)


def test_a_gap_is_filed_once_unlabelled_in_the_harness_repo_and_a_replay_reads_nothing(tmp_path):
    harness = HarnessRepo()
    act = _gap_act(tmp_path, harness)
    for _ in range(3):  # derive would ask on every tick
        act.execute(_gap(5))
    assert [(i.title, i.body, i.labels) for i in harness.issues.values()] == [
        (_gap(5).title, _gap(5).body, frozenset())]
    assert act.gateway.repos == [HARNESS] and harness.reads == 1 and harness.posted == []


def test_a_recurrence_on_another_item_comments_on_the_open_issue_once_rather_than_filing_again(tmp_path):
    harness = HarnessRepo()
    act = _gap_act(tmp_path, harness)
    act.execute(_gap(5))
    for _ in range(2):
        act.execute(_gap(6))
    assert len(harness.issues) == 1 and harness.posted == ["m\nseen again on o/r#6"]


def test_a_gap_whose_issue_a_human_closed_files_a_fresh_one(tmp_path):
    harness = HarnessRepo()
    act = _gap_act(tmp_path, harness)
    act.execute(_gap(5))
    (number,) = harness.issues
    harness.issues[number] = IssueView(number, _gap(5).title, state="closed", author="fix-bot")
    harness.issues[7] = IssueView(7, _gap(5).title, author="alice")  # a human's open issue is not the harness's
    act.execute(_gap(6))
    assert [(i.author, i.state) for i in harness.issues.values()] == [
        ("fix-bot", "closed"), ("alice", "open"), ("fix-bot", "open")]
    assert harness.posted == [] and harness.writes == []  # nothing reopened or commented


def test_a_gap_filing_whose_reply_was_lost_is_recognised_by_its_body_not_filed_or_commented_again(tmp_path):
    harness = HarnessRepo(lose_reply=True)
    act = _gap_act(tmp_path, harness)
    with pytest.raises(GhUnavailable):
        act.execute(_gap(5))
    harness.lose_reply = False
    act.execute(_gap(5))
    act.execute(_gap(5))
    assert len(harness.issues) == 1 and harness.posted == [] and harness.reads == 2


def test_a_lost_filing_beside_another_items_open_issue_is_recognised_not_commented(tmp_path):
    """Two items filed the same gap at once (the listing lagged the first),
    and the second's reply was lost. Its retry finds its own issue by body."""
    harness = HarnessRepo()
    act = _gap_act(tmp_path, harness)
    act.execute(_gap(5))
    harness.hidden, harness.lose_reply = set(harness.issues), True
    with pytest.raises(GhUnavailable):
        act.execute(_gap(6))
    harness.hidden, harness.lose_reply = set(), False
    act.execute(_gap(6))
    act.execute(_gap(6))
    assert len(harness.issues) == 2 and harness.posted == []


def test_a_recurrence_whose_comment_reply_was_lost_is_read_back_and_not_posted_twice(tmp_path):
    harness = HarnessRepo()
    act = _gap_act(tmp_path, harness)
    act.execute(_gap(5))
    harness.lose_reply = True
    with pytest.raises(GhUnavailable):
        act.execute(_gap(6))
    act.execute(_gap(6))
    assert harness.posted == ["m\nseen again on o/r#6"]


def test_a_gap_claim_that_never_landed_is_filed(tmp_path):
    harness = HarnessRepo(lose_reply=True)
    act = _gap_act(tmp_path, harness)
    with pytest.raises(GhUnavailable):
        act.execute(_gap(5))
    harness.issues.clear()  # the create never reached GitHub
    harness.lose_reply = False
    act.execute(_gap(5))
    assert [i.title for i in harness.issues.values()] == [_gap(5).title]


def test_a_report_github_refuses_is_settled_with_one_line_and_never_retried(tmp_path):
    """No Issues write, an archived repository: GitHub answers without the
    write, no retry changes it, so it costs one attempt, not one per poll."""
    class Refusing(HarnessRepo):
        attempts = 0

        def create_issue(self, title, body):
            self.attempts += 1
            return Refused(410, "Issues are disabled for this repo")

    harness, logs = Refusing(), []
    act = _gap_act(tmp_path, harness, logs)
    for _ in range(3):
        act.execute(_gap(5))
    assert harness.attempts == 1 and harness.issues == {}
    assert logs == [f"harness gap {_gap(5).title!r} in {HARNESS}: GitHub refused it (HTTP 410), not retried: "
                    "Issues are disabled for this repo"]


def test_a_recurrence_comment_github_refuses_is_settled_with_one_line_and_never_retried(tmp_path):
    """A locked issue: GitHub answers 403 and writes nothing. Retrying would
    list the harness's issues on every tick forever."""
    class Locked(HarnessRepo):
        def comment(self, kind, number, body):
            self.posted.append(body)  # attempted, never written
            return Refused(403, "Unable to create comment because issue is locked.")

    harness, logs = Locked(), []
    act = _gap_act(tmp_path, harness, logs)
    act.execute(_gap(5))
    reads = harness.reads
    for _ in range(3):
        act.execute(_gap(6))
    assert harness.posted == ["m\nseen again on o/r#6"] and harness.reads == reads + 1
    assert len([line for line in logs if "not retried" in line and "locked" in line]) == 1


def test_a_report_github_does_not_answer_is_retried_next_tick(tmp_path):
    """A secondary rate limit or an outage is transient: the key stays open."""
    class Throttled(HarnessRepo):
        throttled = True

        def create_issue(self, title, body):
            if self.throttled:
                raise GhUnavailable("rate-limit", "secondary rate limit", argv=["gh", "issue", "create"])
            return super().create_issue(title, body)

    harness = Throttled()
    act = _gap_act(tmp_path, harness)
    with pytest.raises(GhUnavailable):
        act.execute(_gap(5))
    harness.throttled = False
    act.execute(_gap(5))
    assert [i.title for i in harness.issues.values()] == [_gap(5).title]


def test_the_daily_ceiling_bounds_gap_reports_and_resets_the_next_utc_day(tmp_path):
    clock = [datetime(2026, 10, 15, 23, 0, tzinfo=timezone.utc)]
    harness = HarnessRepo()
    act = _gap_act(tmp_path, harness, now=lambda: clock[0])
    flood = [_gap(n, f"Harness gap: RESOLVE: 7 consecutive machine ends in o/r{n}") for n in range(GAP_REPORTS_PER_DAY + 2)]
    refused = []
    for action in flood + flood:
        try:
            act.execute(action)
        except RuntimeError as exc:  # refused, so not taken: the daemon logs it and does not re-tick
            refused.append(str(exc))
    assert len(harness.issues) == GAP_REPORTS_PER_DAY
    assert len(refused) == 4 and all("daily ceiling" in r for r in refused)
    clock[0] += timedelta(hours=2)
    for action in flood:
        act.execute(action)
    assert len(harness.issues) == GAP_REPORTS_PER_DAY + 2


def test_a_confirmed_gap_report_is_never_derived_again(tmp_path):
    """Policy reads act's confirmed keys, so a done report is not re-asked
    every tick (each asked action would cost a re-tick and a log line)."""
    from issue_agent.adapters.act import reported_gaps
    from issue_agent.domain.backoff import LegEnd
    from issue_agent.domain.gaps import GapTarget
    from issue_agent.domain.policy import gap_actions
    from issue_agent.domain.state import issue_key

    legs = {issue_key(5): (LegEnd(AgentKind.RESOLVE, datetime(2026, 10, 15, tzinfo=timezone.utc), False,
                                  harness_gap=True, leg_id="leg-1"),)}
    harness = HarnessRepo()
    act = _gap_act(tmp_path, harness)
    target = GapTarget("o/r", True)
    (asked,) = gap_actions(legs, (), target, HARNESS, "m")
    assert gap_actions(legs, (), target, HARNESS, "m", reported_gaps(act.control)) == (asked,)
    act.execute(asked)
    assert gap_actions(legs, (), target, HARNESS, "m", reported_gaps(act.control)) == ()
    assert len(harness.issues) == 1
