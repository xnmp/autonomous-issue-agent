"""The act layer's keyed comment: claimed in SQLite before the write and
confirmed by a read-back after it, so a lost reply or a replay never posts
twice, and a stop comment never lands on an item that is not yet stopped."""

import uuid
from types import SimpleNamespace

import pytest

from issue_agent.adapters.act import GitHubAct
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.github import BlockerLinkFacts, FixObservationIncomplete, GhUnavailable
from issue_agent.domain.actions import LinkBlockedBy, OpenPullRequest, PostComment, Target
from issue_agent.domain.contracts import RunId
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

        def has_comment(self, kind, number, body):
            if self.reply == "readable":
                return super().has_comment(kind, number, body)
            return GitHubGateway.has_comment(SimpleNamespace(_view=lambda *a: self.reply), kind, number, body)

    gh = Unreadable(labels={Label.NEEDS_HUMAN.value})
    act = _act(tmp_path, gh)
    act.execute(STOP)
    for reply in (None, [], {}, {"comments": None}, {"comments": "x"}):
        gh.reply = reply
        with pytest.raises(FixObservationIncomplete):
            act.execute(STOP)
    gh.reply = {"comments": [None, 7, {"body": STOP.body}]}  # junk entries beside the real one
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
    assert gh.posted == [action.body, action.body] and gh.comments == [action.body]


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
        return number

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
        assert [m for m in logs if "filing again" in m] == [
            f"issue for {FILE.title!r} was deleted or retitled; filing again"]


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
