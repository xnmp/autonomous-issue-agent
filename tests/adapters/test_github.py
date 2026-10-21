"""GitHubGateway — pure JSON→WorldState parsers + identity + write-call shapes.

The parsers are the real-vs-fake seam, so they are asserted directly against the
documented gh --json shapes. The gateway is driven with a fake runner that
records argv and returns canned JSON (no network, no real gh)."""

import errno
import json
import subprocess as _subprocess
from types import SimpleNamespace

import pytest

from issue_agent.adapters.github import (
    Accepted,
    GhUnavailable,
    FixObservationIncomplete,
    Refused,
    GitHubGateway,
    Identity,
    _subprocess_runner,
    build_relabel_args,
    build_submit_review_args,
    classify_gh_failure,
    format_pr_discussion,
    parse_pr,
    parse_write_answer,
)
from issue_agent.domain.checks import Unavailable
from issue_agent.domain.labels import Label
from issue_agent.domain.world import Comment
from tests.adapters.test_github_fix_observe import issue_node, pr_node, labels


@pytest.mark.parametrize("count", [0, 999, 1000, 10**30])
def test_open_issue_count_returns_the_exact_repository_count(count):
    def run(argv, token):
        return json.dumps({"data": {"repository": {"issues": {"totalCount": count}}}})

    assert GitHubGateway("owner/repo", runner=run).open_issue_count() == count


def test_the_issue_index_never_offers_an_issue_closed_as_a_duplicate():
    """tauri-explorer #1332 was named a duplicate of #1333, closed as a duplicate of #1332."""
    rows = [{"number": 1333, "state": "CLOSED", "stateReason": "DUPLICATE", "title": "HTML preview", "labels": []},
            {"number": 1325, "state": "CLOSED", "stateReason": "COMPLETED", "title": "peek carets", "labels": []},
            {"number": 1320, "state": "CLOSED", "stateReason": "NOT_PLANNED", "title": "wontfix", "labels": []},
            {"number": 1330, "state": "OPEN", "stateReason": None, "title": "lineage", "labels": []},
            {"number": 1310, "state": "OPEN", "title": "no reason read", "labels": []}]
    index = GitHubGateway("o/r", runner=lambda argv, token: json.dumps(rows)).issue_index()
    assert [row[:2] for row in index] == [(1325, "closed"), (1320, "closed"), (1330, "open"), (1310, "open")]


@pytest.mark.parametrize("payload", [None, {}, {"issues": None}, {"issues": {}},
    *({"issues": {"totalCount": value}} for value in (None, True, -1, "1", 1.5))])
def test_open_issue_count_refuses_an_unreadable_or_malformed_answer(payload):
    def run(argv, token):
        return json.dumps({"data": {"repository": payload}})

    with pytest.raises(FixObservationIncomplete):
        GitHubGateway("owner/repo", runner=run).open_issue_count()


def test_parse_pr_maps_the_graphql_shape():
    pr = parse_pr(
        {
            "number": 100,
            "headRefName": "fix/5-null-token",
            "headRefOid": "abc123",
            "baseRefName": "dev",
            "baseRefOid": "base123",
            "state": "OPEN",
            # _labels silently drops names that aren't in Label (retired ones).
            "labels": [{"name": "needs-human"}, {"name": "ai-review"}],
            "reviews": [
                {
                    "author": {"__typename": "User", "login": "review-bot"},
                    "state": "APPROVED",
                    "submittedAt": "t2",
                    "commit": {"oid": "sha-graphql"},
                }
            ],
            "comments": [
                {"author": {"__typename": "User", "login": "alice"}, "body": "Use A.", "createdAt": "t2",
                 "url": "https://github.com/o/r/pull/100#issuecomment-1"},
                {"author": {"__typename": "User", "login": "bob"}, "body": "B.", "createdAt": "t3", "url": 7},  # malformed: no link
            ],
            "files": [{"path": "backend/app/__init__.py"}, {"path": "backend/x.py"}],
        }
    )
    assert (pr.number, pr.head_ref_name, pr.head_ref_oid, pr.base_ref_oid) == (
        100,
        "fix/5-null-token",
        "abc123",
        "base123",
    )
    assert pr.labels == frozenset({Label.NEEDS_HUMAN})
    # GraphQL shape (`gh pr view --json reviews`) nests the commit under commit.oid.
    assert (pr.reviews[0].author, pr.reviews[0].state, pr.reviews[0].commit_id) == (
        "review-bot",
        "APPROVED",
        "sha-graphql",
    )
    assert pr.comments == (Comment("alice", "Use A.", "t2", "https://github.com/o/r/pull/100#issuecomment-1"),
                           Comment("bob", "B.", "t3"))
    assert pr.changed_files == frozenset({"backend/app/__init__.py", "backend/x.py"})
    assert parse_pr({"number": 100, "state": "OPEN"}).changed_files == frozenset()


def test_parse_pr_review_shapes_and_malformed_commits():
    # The reviews API uses .user.login + .submitted_at (snake) instead of author,
    # and carries the commit flat as commit_id (REST shape) instead of commit.oid.
    rest = parse_pr(
        {
            "number": 1,
            "reviews": [{"user": {"__typename": "User", "login": "x"}, "state": "APPROVED", "submitted_at": "t1", "commit_id": "sha-rest"}],
        }
    )
    assert (rest.reviews[0].author, rest.reviews[0].submitted_at, rest.reviews[0].commit_id) == ("x", "t1", "sha-rest")
    # A non-dict "commit" (or a commit dict missing "oid") must not crash the
    # parse — falls through to the commit_id fallback, then to "".
    malformed = parse_pr(
        {
            "number": 1,
            "reviews": [
                {"author": {"__typename": "User", "login": "x"}, "state": "APPROVED", "commit": "not-a-dict"},
                {"author": {"__typename": "User", "login": "y"}, "state": "APPROVED", "commit": {}},
            ],
        }
    )
    assert [r.commit_id for r in malformed.reviews] == ["", ""]


def test_parse_pr_reads_mergeable():
    assert parse_pr({"number": 1, "state": "OPEN", "mergeable": "CONFLICTING"}).is_conflicting()
    assert not parse_pr({"number": 1, "state": "OPEN", "mergeable": "MERGEABLE"}).is_conflicting()
    # missing/UNKNOWN → not conflicting (don't act on GitHub still computing it)
    assert not parse_pr({"number": 1, "state": "OPEN"}).is_conflicting()


# ── gateway with a fake runner ────────────────────────────────────────────────
class FakeRunner:
    """Records (argv, token); answers by the longest canned prefix of argv[1:4],
    and "" (gh's fail-soft answer) for everything else."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def __call__(self, argv, token):
        self.calls.append((argv, token))
        key = " ".join(argv[1:4])
        for k, v in self.responses.items():
            if key.startswith(k):
                if argv[1:3] == ["pr", "view"] and isinstance(v, dict) and not v.get("headRefOid"):
                    v = {**v, "headRefOid": f"head-{argv[3]}"}
                return v if isinstance(v, str) else json.dumps(v)
        return ""


def test_write_command_builders_are_pure_contracts():
    assert build_submit_review_args("o/r", 100, "APPROVE", "lgtm", "head") == [
        "repos/o/r/pulls/100/reviews",
        "--method",
        "POST",
        "-f",
        "event=APPROVE",
        "-f",
        "body=lgtm",
        "-f",
        "commit_id=head",
    ]
    # REST, not `gh <kind> edit`: GraphQL label edits need read:org, which the
    # supported repo-scoped classic bot PAT lacks (PR #171 wedge, 2026-08-02).
    # Add BEFORE remove: a permanently failing second call must strand "both
    # labels" (visible to the world build), never a label-less target.
    assert build_relabel_args("o/r", "issue", 5, "ai-ready", "ai-working") == [
        ["--method", "POST", "repos/o/r/issues/5/labels", "-f", "labels[]=ai-working"],
        ["--method", "DELETE", "repos/o/r/issues/5/labels/ai-ready"],
    ]
    assert build_relabel_args("o/r", "pr", 171, None, "ai-pr-open") == [
        ["--method", "POST", "repos/o/r/issues/171/labels", "-f", "labels[]=ai-pr-open"]
    ]
    assert build_relabel_args("o/r", "pr", 7, "needs human review?", None) == [
        ["--method", "DELETE", "repos/o/r/issues/7/labels/needs%20human%20review%3F"]
    ]
    assert build_relabel_args("o/r", "issue", 5, None, None) is None


def test_relabel_runs_with_the_identity_token_and_skips_a_noop():
    calls = []
    def runner(argv, token):
        calls.append((argv, token))
        return _http(200)
    gateway = GitHubGateway("o/r", Identity(static_token="t"), runner=runner)
    assert gateway.relabel("issue", 5, "ai-ready", "ai-working") == Accepted()
    assert calls == [
        (["gh", "api", "--include", "--method", "POST", "repos/o/r/issues/5/labels", "-f", "labels[]=ai-working"], "t"),
        (["gh", "api", "--include", "--method", "DELETE", "repos/o/r/issues/5/labels/ai-ready"], "t"),
    ]
    calls.clear()
    assert gateway.relabel("issue", 5, None, None) == Accepted()
    assert calls == []


def _http(status: int, body: object = None, **headers: str) -> str:
    """What `gh api --include` prints: status line, headers, blank line, body."""
    lines = [f"HTTP/2.0 {status} X", "Content-Type: application/json", *(f"{k}: {v}" for k, v in headers.items())]
    return "\r\n".join(lines) + "\r\n\r\n" + ("" if body is None else json.dumps(body))


@pytest.mark.parametrize("answer,merged", [(_http(202, {"message": "Updating pull request branch."}), True),
                                           (_http(422, {"message": "expected head sha didn't match"}), False)])
def test_update_branch_is_true_on_githubs_acceptance_without_reading_the_head_back(answer, merged):
    class UpdateRunner(FakeRunner):
        def __call__(self, argv, token):
            self.calls.append((argv, token))
            if argv[1:4] == ["pr", "view", "100"]:
                return json.dumps({"autoMergeRequest": None, "headRefOid": "head"})  # the merge has not run yet
            if argv[3:4] == ["repos/o/r/pulls/100/update-branch"]:
                return answer
            raise AssertionError(argv)

    fr = UpdateRunner()
    assert GitHubGateway("o/r", runner=fr).update_branch(100, "head") is merged
    (argv, _), = (c for c in fr.calls if c[0][1] == "api")
    assert argv[1:5] == ["api", "--include", "repos/o/r/pulls/100/update-branch", "--method"]
    assert "expected_head_sha=head" in argv


def test_update_branch_no_longer_swallows_github_not_answering():
    def runner(argv, token):
        if argv[1:3] == ["pr", "view"]:
            return json.dumps({"autoMergeRequest": None, "headRefOid": "head"})
        raise GhUnavailable("network", "gh timed out")

    with pytest.raises(GhUnavailable):
        GitHubGateway("o/r", runner=runner).update_branch(100, "head")


def test_update_branch_refuses_to_advance_when_legacy_arm_cannot_be_revoked():
    fr = FakeRunner({"pr view 100": {"id": "PR_node", "autoMergeRequest": {"enabledAt": "now"}, "headRefOid": "head"},
                     "api --include graphql": _http(200, {"errors": [{"type": "FORBIDDEN", "message": "refused"}]})})

    assert GitHubGateway("o/r", runner=fr).update_branch(100, "head") is False

    assert any("pr=PR_node" in argv for argv, _ in fr.calls)
    assert not any("repos/o/r/pulls/100/update-branch" in argv for argv, _ in fr.calls)


def test_viewer_login_names_the_identity_the_credential_acts_as():
    fr = FakeRunner({"api graphql": "review-app[bot]\n"})
    assert GitHubGateway("o/r", runner=fr).viewer_login() == "review-app[bot]"
    assert "query=query{viewer{login}}" in fr.calls[0][0]
    assert GitHubGateway("o/r", runner=FakeRunner()).viewer_login() == ""  # GitHub refused to answer


def test_a_refused_review_is_githubs_refusal_and_an_unanswered_one_raises():
    refused = FakeRunner({"api --include repos/o/r/pulls/100/reviews": _http(422, {"message": "Can not approve your own pull request"})})
    assert GitHubGateway("o/r", runner=refused).submit_review(100, "APPROVE", "lgtm", "head") == Refused(
        422, "Can not approve your own pull request")
    with pytest.raises(GhUnavailable):  # gh printed no HTTP answer
        GitHubGateway("o/r", runner=FakeRunner()).submit_review(100, "APPROVE", "lgtm", "head")


def test_get_issue_parses_response():
    resp = {"data": {"repository": {"issue": issue_node(title="x")}}}
    iss = GitHubGateway("o/r", runner=FakeRunner({"api graphql": resp})).get_issue(5)
    assert iss.number == 5 and iss.has(Label.AI_READY)


def test_the_issue_index_carries_only_harness_labels_and_tolerates_a_missing_list():
    rows = [{"number": 2, "state": "OPEN", "title": "Epic", "labels": [{"name": "epic"}, {"name": "ai-ready"}]},
            {"number": 3, "state": "CLOSED", "title": "Old"}, {"number": 4, "state": "OPEN", "labels": "junk"}]
    index = GitHubGateway("o/r", runner=FakeRunner({"issue list": rows})).issue_index()
    assert index == ((2, "open", "Epic", frozenset({Label.AI_READY})), (3, "closed", "Old", frozenset()),
                     (4, "open", "", frozenset()))


def test_the_issue_index_lists_one_authors_issues_when_asked():
    runner = FakeRunner({"issue list": []})
    gateway = GitHubGateway("o/r", runner=runner)
    gateway.issue_index()
    gateway.issue_index(1000, author="@me")
    assert "--author" not in runner.calls[0][0]
    argv = runner.calls[1][0]
    assert argv[argv.index("--author") + 1] == "@me" and argv[argv.index("--limit") + 1] == "1000"


# ── V10: infra failure ≠ "nothing to do" ──────────────────────────────────────
def _run_with(monkeypatch, stderr, rc=1):
    monkeypatch.setattr(
        _subprocess,
        "run",
        lambda argv, capture_output, text, env, timeout: SimpleNamespace(returncode=rc, stdout="", stderr=stderr),
    )
    return lambda argv=None: _subprocess_runner(argv or ["gh", "pr", "view", "1"], None)


@pytest.mark.parametrize(
    "stderr",
    [
        "GraphQL: Could not resolve to a PullRequest with the number of 999.",
        "HTTP 404: Not Found (https://api.github.com/repos/o/r/issues/999)",
        "HTTP 422: Can not approve your own pull request",
        "GraphQL: Resource not accessible by integration",  # bare 403, permanent
        "could not find any label named ai-review",
    ],
)
def test_runner_keeps_failsoft_for_ordinary_non_zero_exits(monkeypatch, stderr):
    """Regression guard for the fail-soft contract the whole gateway rests on: a
    deleted PR, a 404, a 422 self-approval and a missing label are ANSWERS from
    the API, not outages. They must keep degrading to "", so a blanket raise
    would wedge the daemon."""
    assert _run_with(monkeypatch, stderr)() == ""
    assert classify_gh_failure(stderr) is None


@pytest.mark.parametrize(
    "stderr,kind",
    [
        # V10: throttling used to return "" → the tick observed an empty world and
        # acted on it. It must be an exception carrying its kind.
        ("HTTP 403: API rate limit exceeded for user ID 583231. (https://api.github.com/…)", "rate-limit"),
        ("HTTP 429: Too Many Requests", "rate-limit"),
        ("You have exceeded a secondary rate limit. Please wait a few minutes.", "rate-limit"),
        ("HTTP 403: x-ratelimit-remaining: 0", "rate-limit"),
        ("error connecting to api.github.com", "network"),
        ("Could not resolve host: api.github.com", "network"),
        ("dial tcp: lookup api.github.com: no such host", "network"),
        ("Get https://api.github.com: net/http: TLS handshake timeout", "network"),
        ("connection refused", "network"),
        # A local process/thread failure returned no GitHub answer; treating it as an
        # empty response releases dependency gates under peak load.
        ("runtime/cgo: pthread_create failed: Resource temporarily unavailable", "local-resource"),
        ("runtime: failed to create new OS thread\nfatal error: newosproc", "local-resource"),
        ("fork/exec /usr/bin/gh: cannot allocate memory", "local-resource"),
    ],
)
def test_runner_raises_unavailability_with_its_kind(monkeypatch, stderr, kind):
    with pytest.raises(GhUnavailable) as ei:
        _run_with(monkeypatch, stderr)()
    assert ei.value.kind == kind


@pytest.mark.parametrize(
    "stderr,kind",
    [
        ("HTTP 502: Bad gateway", "server"),
        ("HTTP 503 (https://api.github.com/graphql)", "server"),
        # contains "timeout" — the 5xx test must win, or a GitHub outage reads as
        # a local network fault and the operator debugs the wrong box.
        ("HTTP 504: Gateway Timeout", "server"),
        # a bare 5xx-looking number is NOT an outage — it's an issue number.
        ("GraphQL: Could not resolve to an Issue with the number 502.", None),
        # Auth outages are transient unavailability.
        ("HTTP 401: Requires authentication (https://api.github.com/graphql)", "auth"),
        ("gh: Bad credentials (HTTP 401)", "auth"),
    ],
)
def test_server_and_auth_classification_requires_the_http_token(stderr, kind):
    assert classify_gh_failure(stderr) == kind


def test_runner_redacts_and_bounds_a_large_publication_body(monkeypatch, capsys) -> None:
    body = "untrusted body " * 10_000

    with pytest.raises(GhUnavailable):
        _run_with(monkeypatch, "runtime/cgo: pthread_create failed: Resource temporarily unavailable")(
            ["gh", "issue", "comment", "7", "--body", body]
        )

    logged = capsys.readouterr().err
    assert body[:100] not in logged
    assert "<body redacted:" in logged
    assert len(logged.encode()) < 9_000


def test_spawn_failure_redacts_a_large_graphql_body_field(monkeypatch, capsys) -> None:
    body = "untrusted review " * 10_000

    def fail_to_start(*_args, **_kwargs):
        raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")

    monkeypatch.setattr(_subprocess, "run", fail_to_start)

    with pytest.raises(GhUnavailable):
        _subprocess_runner(["gh", "api", "graphql", "-f", f"body={body}"], None)

    logged = capsys.readouterr().err
    assert body[:100] not in logged
    assert "body=<redacted:" in logged
    assert len(logged.encode()) < 3_000


def test_runner_raises_network_on_timeout(monkeypatch, capsys):
    """The bounded kill produced NO data; returning "" made a hung `gh pr view` look
    like a successful read of an empty world."""

    def _timeout(argv, capture_output, text, env, timeout):
        raise _subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    monkeypatch.setattr(_subprocess, "run", _timeout)
    with pytest.raises(GhUnavailable) as ei:
        _subprocess_runner(["gh", "pr", "view", "374", "--repo", "o/r"], None)
    assert ei.value.kind == "network"
    assert "timed out" in capsys.readouterr().err


def test_gh_unavailable_detail_is_one_bounded_line():
    exc = GhUnavailable("rate-limit", "API rate limit exceeded\n  for user ID 1\n" + "x" * 500)
    assert "\n" not in exc.detail and len(exc.detail) <= 200
    assert "rate-limited" in str(exc)


def _raising_runner(exc):
    def runner(argv, token):
        raise exc

    return runner


# ── PR conversation → review prompt (2026-08-08, tauri #613/#614) ─────────────
def test_pr_discussion_is_newest_first_and_keeps_bot_authors():
    # Newest first because the byte ceiling truncates the tail and the answer to
    # "was this demand already addressed?" is at the recent end. Bot comments
    # stay: the fixer's own blocked-reason comment is the one that matters here.
    text = format_pr_discussion(
        (
            Comment(author="author", body="opened"),
            Comment(author="harness-fixer[bot]", body="no display in this deployment"),
        )
    )
    assert text.index("no display in this deployment") < text.index("opened")
    assert "@harness-fixer[bot]" in text


def test_a_long_thread_is_truncated_with_an_explicit_marker():
    text = format_pr_discussion(tuple(Comment(author="rev", body=f"comment {n} " + "x" * 400) for n in range(200)))
    assert "[older comments truncated]" in text
    # The NEWEST survives, the oldest is what fell off.
    assert "comment 199" in text
    assert "comment 0 " not in text


def test_one_huge_comment_is_capped_and_says_so():
    text = format_pr_discussion((Comment(author="rev", body="old point"), Comment(author="spam", body="A" * 50_000)))
    # Marked, not silently amputated — an unmarked cut reads as a whole comment.
    assert "[…comment truncated]" in text
    assert len(text.encode()) < 2_500  # one capped comment + the short one
    assert "old point" in text  # the rest of the thread survives


# ── review ids: the agent reads a change request's inline comments by it ──
@pytest.mark.parametrize("raw,review_id", [
    ({"databaseId": 42, "id": "PRR_node", "state": "CHANGES_REQUESTED"}, 42),  # GraphQL: the node id is no review id
    ({"id": 42, "state": "CHANGES_REQUESTED"}, 42),  # REST
    ({"id": "PRR_node", "state": "CHANGES_REQUESTED"}, None),
    ({"databaseId": 0, "state": "CHANGES_REQUESTED"}, None),
    ({"databaseId": True, "state": "CHANGES_REQUESTED"}, None),
])
def test_a_review_carries_its_id_only_when_github_gave_a_real_one(raw, review_id):
    pr = parse_pr({"number": 1, "headRefName": "fix/1-x", "headRefOid": "h", "state": "OPEN", "reviews": [raw]})
    assert pr.reviews[0].id == review_id


# ── #48: a failed `gh pr checks` is UNKNOWN, not an empty world ──────────────
@pytest.mark.parametrize("checks", [True, False])
def test_unread_checks_are_unknown_not_an_empty_world(checks):
    """The hole (#48): a swallowed non-infra CI read failure used to rebuild the
    PR with checks=() — indistinguishable from a no-CI repo, which the fix
    policy treats as mergeable. get_pr(checks=False) never asked: a PR that
    flips OPEN between the list and the view must not arrive as honest-NONE
    mergeable either (review F3)."""
    node = pr_node(comments={"nodes": [], "totalCount": 0, "pageInfo": {"hasPreviousPage": False}},
                   reviews={"nodes": [], "totalCount": 0, "pageInfo": {"hasPreviousPage": False}},
                   files={"nodes": [], "totalCount": 0, "pageInfo": {"hasNextPage": False}})
    fr = FakeRunner({"api graphql": {"data": {"repository": {"pullRequest": node}}}})
    pr = GitHubGateway("o/r", runner=fr).get_pr(100, checks=checks)  # the policy read is unmatched → unavailable
    assert pr.checks == () and isinstance(pr.ci, Unavailable)


# ── the harness's one re-run of a head (owner, PR #403) ─────────────────────
def test_run_attempt_reads_the_runs_current_attempt():
    fr = FakeRunner({"api repos/o/r/actions/runs/900": {"id": 900, "run_attempt": 2, "status": "queued"}})
    assert GitHubGateway("o/r", runner=fr).run_attempt(900) == 2


@pytest.mark.parametrize("answer", [{}, {"run_attempt": 0}, {"run_attempt": "2"}, {"run_attempt": True}, [], ""])
def test_an_unreadable_run_raises(answer):
    with pytest.raises(ValueError):
        GitHubGateway("o/r", runner=FakeRunner({"api repos/o/r/actions/runs/900": answer})).run_attempt(900)


def test_a_rerun_posts_the_failed_jobs_rerun_and_returns_githubs_answer():
    fr = FakeRunner({"api --include --method": _http(201, {})})
    assert GitHubGateway("o/r", runner=fr).rerun_failed_checks(900) == Accepted()
    assert fr.calls[0][0][1:] == ["api", "--include", "--method", "POST", "repos/o/r/actions/runs/900/rerun-failed-jobs"]
    refused = FakeRunner({"api --include --method": _http(403, {"message": "Must have admin rights"})})
    assert GitHubGateway("o/r", runner=refused).rerun_failed_checks(900) == Refused(403, "Must have admin rights")


# ── #440: a write's answer, read from GitHub's status and headers ─────────────
@pytest.mark.parametrize("out,answer", [
    (_http(201, {"id": 77, "user": {"__typename": "User", "login": "fixer"}}), Accepted(77)),
    (_http(202, {}), Accepted()),  # a cancel: nothing created
    (_http(204), Accepted()),  # no body at all
    (_http(201, {"id": "77"}), Accepted()),  # a malformed id is no receipt id, still accepted
    (_http(201, {"id": True}), Accepted()),
    (_http(201, {"id": -3}), Accepted()),
    (_http(201, ["not", "an", "object"]), Accepted()),
    (_http(201, None) + "not json{", Accepted()),
    (_http(404, {"message": "Not Found"}), Refused(404, "Not Found")),
    (_http(410, {"message": "Issues are disabled for this repo"}), Refused(410, "Issues are disabled for this repo")),
    # GitHub lists Retry-After among the headers it exposes on every answer: not a rate limit.
    (_http(403, {"message": "Resource not accessible by integration"},
           **{"Access-Control-Expose-Headers": "ETag, Link, Location, Retry-After, X-GitHub-OTP"}),
     Refused(403, "Resource not accessible by integration")),
    (_http(403, {"message": "Unable to create comment because issue is locked."}),
     Refused(403, "Unable to create comment because issue is locked.")),
    (_http(422, {"message": "Validation Failed", "errors": [{"code": "custom"}]}), Refused(422, "Validation Failed")),
    (_http(422), Refused(422, "HTTP 422")),  # a refusal without a body
    # GraphQL answers its refusals with a 200 that carries errors.
    (_http(200, {"data": {"addBlockedBy": None}, "errors": [{"type": "NOT_FOUND", "message": "Could not resolve"}]}),
     Refused(200, "Could not resolve")),
    (_http(422, {"message": "x" * 100_000}), Refused(422, "x" * 299 + "…")),  # a huge reason is bounded
])
def test_a_write_answer_is_read_from_the_status_line_and_the_receipt_id(out, answer):
    assert parse_write_answer(out, "id") == answer


@pytest.mark.parametrize("out,kind,retry_after", [
    ("", "network", None),  # no answer at all: a transport failure
    ("gh: error connecting to api.github.com", "network", None),
    (_http(502, {"message": "Bad Gateway"}), "server", None),  # it may have landed
    (_http(500), "server", None),
    # An untyped GraphQL error is GitHub's own failure, after which the mutation may have committed.
    (_http(200, {"errors": [{"message": "Something went wrong while executing your query."}]}), "server", None),
    (_http(200, {"errors": ["a bare string error"]}), "server", None),
    (_http(401, {"message": "Bad credentials"}), "auth", None),
    (_http(429, {"message": "Too Many Requests"}, **{"Retry-After": "30"}), "rate-limit", 30.0),
    (_http(403, {"message": "You have exceeded a secondary rate limit."}), "rate-limit", None),
    (_http(403, {"message": "Forbidden"}, **{"Retry-After": "60"}), "rate-limit", 60.0),
    (_http(403, {"message": "API rate limit exceeded"}, **{"X-RateLimit-Remaining": "0"}), "rate-limit", None),
    (_http(200, {"errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded for user"}]}), "rate-limit",
     None),
])
def test_a_write_github_did_not_answer_raises_with_its_kind(out, kind, retry_after):
    with pytest.raises(GhUnavailable) as ei:
        parse_write_answer(out, "id")
    assert (ei.value.kind, ei.value.retry_after_s) == (kind, retry_after)


def test_the_runner_hands_back_an_answered_writes_output_whatever_the_exit(monkeypatch):
    refused = _http(422, {"message": "Validation Failed"})
    monkeypatch.setattr(_subprocess, "run", lambda argv, capture_output, text, env, timeout: SimpleNamespace(
        returncode=1, stdout=refused, stderr="gh: Validation Failed (HTTP 422)"))
    assert _subprocess_runner(["gh", "api", "--include", "repos/o/r/issues"], None) == refused
    assert _subprocess_runner(["gh", "api", "repos/o/r/issues"], None) == ""  # a read keeps its fail-soft ""


@pytest.mark.parametrize("call,argv_tail,receipt", [
    (lambda g: g.comment("pr", 5, "hello"), ["--method", "POST", "repos/o/r/issues/5/comments", "-f", "body=hello"], 9),
    (lambda g: g.create_issue("T", "B"), ["--method", "POST", "repos/o/r/issues", "-f", "title=T", "-f", "body=B"], 9),
    (lambda g: g.submit_review(5, "APPROVE", "ok", "h"),
     ["repos/o/r/pulls/5/reviews", "--method", "POST", "-f", "event=APPROVE", "-f", "body=ok", "-f", "commit_id=h"], 9),
])
def test_creating_writes_return_the_created_objects_id_from_the_answer(call, argv_tail, receipt):
    calls = []

    def runner(argv, token):
        calls.append(argv)
        return _http(201, {"id": 9, "number": 9})

    assert call(GitHubGateway("o/r", runner=runner)) == Accepted(receipt)
    assert calls == [["gh", "api", "--include", *argv_tail]]


@pytest.mark.parametrize("answer,expected", [
    ({"visibility": "public", "permissions": {"push": True}, "has_issues": True, "archived": False}, ("public", True)),
    ({"visibility": "private", "permissions": {"push": False}, "has_issues": True, "archived": False}, ("private", False)),
    ({"visibility": "public", "permissions": {"push": True}, "has_issues": False, "archived": False}, ("public", False)),
    ({"visibility": "public", "permissions": {"push": True}, "has_issues": True, "archived": True}, ("public", False)),
    ({"visibility": "internal"}, ("internal", False)),  # no permissions block: cannot file
])
def test_filing_facts_read_visibility_and_whether_this_credential_can_file_issues(answer, expected):
    import json as _json

    from issue_agent.adapters.github import GitHubGateway
    calls = []
    gw = GitHubGateway("o/r", runner=lambda argv, token: calls.append(argv) or _json.dumps(answer))
    assert gw.for_repo("x/h").filing_facts() == expected
    assert calls == [["gh", "api", "repos/x/h"]]


@pytest.mark.parametrize("answer", ["", "[]", '{"visibility": "secret"}', '{"name": "r"}'])
def test_unreadable_filing_facts_raise(answer):
    from issue_agent.adapters.github import FixObservationIncomplete, GitHubGateway
    with pytest.raises(FixObservationIncomplete):
        GitHubGateway("o/r", runner=lambda argv, token: answer).filing_facts()
