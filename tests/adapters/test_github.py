"""GitHubGateway — pure JSON→WorldState parsers + identity + write-call shapes.

The parsers are the real-vs-fake seam, so they are asserted directly against the
documented gh --json shapes. The gateway is driven with a fake runner that
records argv and returns canned JSON (no network, no real gh)."""

import errno
import json

from issue_agent.adapters.github import _FAILED_LOG_BYTES, _FAILED_LOG_LINES, _INLINE_COMMENT_LIMIT, _REVIEW_COMMENT_BODY_BYTES, _REVIEW_COMMENTS_TOTAL_BYTES, GitHubGateway, Identity, MissingListLimit, format_pr_discussion, format_review_comments, build_close_issue_args, build_comment_args, build_enable_auto_merge_args, build_relabel_args, build_submit_review_args, parse_issue, parse_pr
from issue_agent.domain.labels import Label
from issue_agent.domain.world import Comment
from issue_agent.domain.checks import Unavailable


# ── parsers ────────────────────────────────────────────────────────────────
def test_parse_issue_maps_fields():
    d = {
        "number": 5,
        "title": "Null token",
        "body": "crashes",
        "labels": [{"name": "ai-ready"}, {"name": "P1"}, {"name": "weird-unknown"}],
        "comments": [{"author": {"login": "alice"}, "body": "hi", "createdAt": "t1"}],
        "updatedAt": "t9",
        "author": {"login": "bob"},
        "state": "OPEN",
    }
    iss = parse_issue(d)
    assert iss.number == 5
    assert iss.has(Label.AI_READY) and iss.has(Label.P1)
    assert iss.author == "bob"
    assert iss.state == "open"            # normalised lower-case
    assert iss.comments[0].author == "alice"
    assert iss.updated_at == "t9"
    # unknown labels are dropped, not crash
    assert all(isinstance(x, Label) for x in iss.labels)


def test_parse_pr_maps_reviews():
    d = {
        "number": 100,
        "headRefName": "fix/5-null-token",
        "headRefOid": "abc123",
        "baseRefName": "dev",
        "baseRefOid": "base123",
        "labels": [{"name": "ai-pr-open"}],
        "reviews": [{
            "author": {"login": "review-bot"}, "state": "APPROVED", "submittedAt": "t2",
            "commit": {"oid": "sha-graphql"},
        }],
        "state": "OPEN",
    }
    pr = parse_pr(d)
    assert pr.number == 100
    assert pr.head_ref_name == "fix/5-null-token"
    assert pr.base_ref_oid == "base123"
    assert pr.head_ref_oid == "abc123"
    assert pr.reviews[0].author == "review-bot"
    assert pr.reviews[0].state == "APPROVED"
    # GraphQL shape (`gh pr view --json reviews`) nests the commit under commit.oid.
    assert pr.reviews[0].commit_id == "sha-graphql"


def test_parse_pr_maps_comments_for_pr_stage_unblock():
    pr = parse_pr({
        "number": 100,
        "comments": [{"author": {"login": "alice"}, "body": "Use A.", "createdAt": "t2"}],
    })
    assert pr.comments == (Comment("alice", "Use A.", "t2"),)


def test_parse_pr_review_api_user_shape():
    # the reviews API uses .user.login + .submitted_at (snake) instead of author,
    # and carries the commit flat as commit_id (REST shape) instead of commit.oid.
    d = {"number": 1, "reviews": [{
        "user": {"login": "x"}, "state": "APPROVED", "submitted_at": "t1",
        "commit_id": "sha-rest",
    }]}
    pr = parse_pr(d)
    assert pr.reviews[0].author == "x" and pr.reviews[0].submitted_at == "t1"
    assert pr.reviews[0].commit_id == "sha-rest"


def test_parse_pr_review_commit_id_defaults_empty_when_absent():
    # neither commit.oid nor commit_id present → "" (unknown), not a crash. The
    # ADDRESS_REVIEW supersession gate treats "" as "stay open" (see world.py).
    d = {"number": 1, "reviews": [{"author": {"login": "x"}, "state": "APPROVED"}]}
    pr = parse_pr(d)
    assert pr.reviews[0].commit_id == ""


def test_parse_pr_review_commit_id_tolerates_malformed_commit_field():
    # a non-dict "commit" (or a commit dict missing "oid") must not crash the
    # parse — falls through to the commit_id fallback, then to "".
    d = {"number": 1, "reviews": [
        {"author": {"login": "x"}, "state": "APPROVED", "commit": "not-a-dict"},
        {"author": {"login": "y"}, "state": "APPROVED", "commit": {}},
    ]}
    pr = parse_pr(d)
    assert pr.reviews[0].commit_id == ""
    assert pr.reviews[1].commit_id == ""


def test_parse_pr_checks_feed_ci_state():
    d = {"number": 1, "statusCheckRollup": [
        {"name": "unit", "bucket": "fail"}, {"name": "e2e", "state": "QUEUED"}]}
    pr = parse_pr(d)
    # The PR payload cannot prove a complete repository policy or report read.
    assert isinstance(pr.ci, Unavailable)


def test_parse_pr_extracts_changed_files():
    d = {"number": 100, "state": "OPEN",
         "files": [{"path": "backend/app/__init__.py"}, {"path": "backend/x.py"}]}
    pr = parse_pr(d)
    assert pr.changed_files == frozenset({"backend/app/__init__.py", "backend/x.py"})


def test_parse_pr_tolerates_missing_files():
    assert parse_pr({"number": 100, "state": "OPEN"}).changed_files == frozenset()


def test_parse_pr_reads_mergeable():
    assert parse_pr({"number": 1, "state": "OPEN", "mergeable": "CONFLICTING"}).is_conflicting()
    assert not parse_pr({"number": 1, "state": "OPEN", "mergeable": "MERGEABLE"}).is_conflicting()
    # missing/UNKNOWN → not conflicting (don't act on GitHub still computing it)
    assert not parse_pr({"number": 1, "state": "OPEN"}).is_conflicting()


def test_parse_pr_reads_author():
    p = parse_pr({"number": 1, "state": "OPEN", "author": {"login": "alice"}})
    assert p.author == "alice"
    # defaults when absent
    assert parse_pr({"number": 2, "state": "OPEN"}).author == ""


# ── identity ────────────────────────────────────────────────────────────────
def test_identity_static_token():
    assert Identity(static_token="tok").env_token() == "tok"


def test_identity_provider_overrides():
    calls = []
    ident = Identity(token_provider=lambda: calls.append(1) or "minted")
    assert ident.env_token() == "minted"
    assert calls == [1]


def test_identity_ambient_is_none():
    assert Identity().env_token() is None


# ── gateway with a fake runner ────────────────────────────────────────────────
class FakeRunner:
    def __init__(self, responses=None):
        self.calls = []          # list of (argv, token)
        self.responses = responses or {}
        self.last_pr_number = ""

    def __call__(self, argv, token):
        self.calls.append((argv, token))
        if argv[1:3] == ["pr", "view"]:
            self.last_pr_number = str(argv[3])
        # key responses by the subcommand pair (argv[1], argv[2])
        key = " ".join(argv[1:4])
        for k, v in self.responses.items():
            if key.startswith(k):
                if (
                    argv[1:3] == ["pr", "view"]
                    and isinstance(v, dict)
                    and not v.get("headRefOid")
                ):
                    v = {**v, "headRefOid": f"head-{argv[3]}"}
                return v if isinstance(v, str) else json.dumps(v)
        legacy = self.responses.get(f"pr checks {self.last_pr_number}")
        if "/check-runs?filter=latest&per_page=100" in " ".join(argv):
            if legacy is None:
                return ""
            runs = [
                {
                    "name": item.get("name", ""),
                    "status": (
                        "queued"
                        if item.get("bucket") == "pending"
                        or item.get("state") in {"QUEUED", "IN_PROGRESS"}
                        else "completed"
                    ),
                    "conclusion": (
                        "failure"
                        if item.get("bucket") == "fail"
                        else "success"
                    ),
                    "started_at": item.get("startedAt"),
                    "details_url": item.get("link"),
                }
                for item in legacy
            ]
            return json.dumps([{"check_runs": runs}])
        if "statuses?per_page=100" in " ".join(argv):
            return json.dumps([[]]) if legacy is not None else ""
        return ""


def test_write_command_builders_are_pure_contracts():
    assert build_submit_review_args(
        "o/r", 100, "APPROVE", "lgtm", "head"
    ) == [
        "api", "repos/o/r/pulls/100/reviews", "--method", "POST",
        "-f", "event=APPROVE", "-f", "body=lgtm",
        "-f", "commit_id=head",
    ]
    # REST, not `gh <kind> edit`: GraphQL label edits need read:org, which the
    # supported repo-scoped classic bot PAT lacks (PR #171 wedge, 2026-08-02).
    # Add BEFORE remove: a permanently failing second call must strand "both
    # labels" (visible to the world build), never a label-less target.
    assert build_relabel_args("o/r", "issue", 5, "ai-ready", "ai-working") == [
        ["api", "--method", "POST", "repos/o/r/issues/5/labels",
         "-f", "labels[]=ai-working"],
        ["api", "--method", "DELETE", "repos/o/r/issues/5/labels/ai-ready"],
    ]
    assert build_relabel_args("o/r", "pr", 171, None, "ai-pr-open") == [
        ["api", "--method", "POST", "repos/o/r/issues/171/labels",
         "-f", "labels[]=ai-pr-open"],
    ]
    assert build_relabel_args("o/r", "pr", 7, "needs human review?", None) == [
        ["api", "--method", "DELETE",
         "repos/o/r/issues/7/labels/needs%20human%20review%3F"],
    ]
    assert build_relabel_args("o/r", "issue", 5, None, None) is None
    assert build_comment_args("o/r", "pr", 100, "hi") == [
        "pr", "comment", "100", "--repo", "o/r", "--body", "hi",
    ]
    assert build_close_issue_args("o/r", 5, "completed", "done") == [
        "issue", "close", "5", "--repo", "o/r", "--reason", "completed", "--comment", "done",
    ]
    # gh's CLI vocabulary, not the GraphQL enum: "not_planned" exits 1 (keifu #193).
    assert build_close_issue_args("o/r", 5, "not_planned", "") == [
        "issue", "close", "5", "--repo", "o/r", "--reason", "not planned",
    ]
    assert build_enable_auto_merge_args(
        "o/r", 100, "squash", "head"
    ) == [
        "pr", "merge", "100", "--repo", "o/r", "--squash", "--delete-branch",
        "--match-head-commit", "head",
    ]


def test_relabel_builds_correct_argv():
    fr = FakeRunner()
    gw = GitHubGateway("o/r", Identity(static_token="t"), runner=fr)
    gw.relabel("issue", 5, "ai-ready", "ai-working")
    assert len(fr.calls) == 2
    add_argv, token = fr.calls[0]
    assert add_argv == [
        "gh", "api", "--method", "POST", "repos/o/r/issues/5/labels",
        "-f", "labels[]=ai-working",
    ]
    remove_argv, _ = fr.calls[1]
    assert remove_argv == [
        "gh", "api", "--method", "DELETE", "repos/o/r/issues/5/labels/ai-ready",
    ]
    assert token == "t"


def test_relabel_noop_when_nothing_to_change():
    fr = FakeRunner()
    gw = GitHubGateway("o/r", runner=fr)
    gw.relabel("issue", 5, None, None)
    assert fr.calls == []  # no gh call when there's nothing to add/remove


def test_close_issue_argv():
    fr = FakeRunner()
    gw = GitHubGateway("o/r", runner=fr)
    gw.close_issue(5, "completed", "Resolved by #100")
    argv, _ = fr.calls[0]
    assert argv[1:4] == ["issue", "close", "5"]
    assert "--reason" in argv and "completed" in argv
    assert "--comment" in argv


def test_enable_auto_merge_argv():
    fr = FakeRunner({"pr view 100": {
        "autoMergeRequest": None,
        "state": "MERGED",
        "headRefOid": "head",
    }})
    gw = GitHubGateway("o/r", runner=fr)
    gw.enable_auto_merge(100, "squash", "head")
    argv, _ = next(
        call for call in fr.calls
        if call[0][1:4] == ["pr", "merge", "100"]
    )
    assert argv[1:4] == ["pr", "merge", "100"]
    assert "--auto" not in argv
    assert "--squash" in argv and "--delete-branch" in argv
    assert argv[argv.index("--match-head-commit") + 1] == "head"


def test_enable_auto_merge_verifies_exact_head_was_merged():
    landed = FakeRunner({"pr view 100": {
        "state": "MERGED",
        "headRefOid": "head",
    }})
    assert GitHubGateway("o/r", runner=landed).enable_auto_merge(
        100, "squash", "head"
    ) is True

    refused = FakeRunner({"pr view 100": {
        "state": "OPEN",
        "headRefOid": "head",
    }})
    assert GitHubGateway("o/r", runner=refused).enable_auto_merge(
        100, "squash", "head"
    ) is False

    unreadable = FakeRunner()  # probe returns "" → not armed, never a crash
    assert GitHubGateway("o/r", runner=unreadable).enable_auto_merge(
        100, "squash", "head"
    ) is False

    stale = FakeRunner({"pr view 100": {
        "state": "MERGED",
        "headRefOid": "different-head",
    }})
    assert GitHubGateway("o/r", runner=stale).enable_auto_merge(
        100, "squash", "head"
    ) is False


def test_update_branch_argv():
    class UpdateRunner(FakeRunner):
        def __init__(self):
            super().__init__()
            self.head = "head"

        def __call__(self, argv, token):
            self.calls.append((argv, token))
            if argv[1:4] == ["pr", "view", "100"]:
                return json.dumps(
                    {
                        "autoMergeRequest": None,
                        "headRefOid": self.head,
                        "mergeStateStatus": "CLEAN",
                    }
                )
            if argv[1:3] == [
                "api",
                "repos/o/r/pulls/100/update-branch",
            ]:
                self.head = "updated-head"
                return "ok"
            return ""

    fr = UpdateRunner()
    gw = GitHubGateway("o/r", runner=fr)
    assert gw.update_branch(100, "head") is True
    argv, _ = fr.calls[1]
    assert argv[1:3] == ["api", "repos/o/r/pulls/100/update-branch"]
    assert "expected_head_sha=head" in argv


def test_update_branch_refuses_to_advance_when_legacy_arm_cannot_be_revoked():
    fr = FakeRunner({"pr view 100": {
        "autoMergeRequest": {"enabledAt": "now"},
        "headRefOid": "head",
    }})
    gw = GitHubGateway("o/r", runner=fr)

    assert gw.update_branch(100, "head") is False

    assert any("--disable-auto" in argv for argv, _ in fr.calls)
    assert not any(argv[1] == "api" for argv, _ in fr.calls)


def test_submit_review_returns_true_on_success():
    # gh `api ... POST` prints the created-review JSON on success → non-empty → True.
    fr = FakeRunner({"api repos/o/r/pulls/100/reviews": {"id": 1, "state": "APPROVED"}})
    gw = GitHubGateway("o/r", runner=fr)
    assert gw.submit_review(100, "APPROVE", "lgtm", "head") is True
    argv, _ = fr.calls[0]
    assert argv[1] == "api" and "--method" in argv and "POST" in argv


def test_viewer_login_names_the_identity_the_credential_acts_as():
    fr = FakeRunner({"api graphql": "review-app[bot]\n"})
    gw = GitHubGateway("o/r", runner=fr)

    assert gw.viewer_login() == "review-app[bot]"
    argv, _ = fr.calls[0]
    assert "query=query{viewer{login}}" in argv


def test_viewer_login_is_empty_when_github_refuses_to_answer():
    assert GitHubGateway("o/r", runner=FakeRunner()).viewer_login() == ""


def test_submit_review_returns_false_on_failure():
    # a failed POST (422/403) → runner returns "" (and logs stderr) → False.
    fr = FakeRunner()  # no canned response → "" for every call
    gw = GitHubGateway("o/r", runner=fr)
    assert gw.submit_review(100, "APPROVE", "lgtm", "head") is False


def test_pr_diff_is_fetched_for_exact_observed_revisions():
    fr = FakeRunner(
        {"api -H Accept: application/vnd.github.v3.diff": "diff text"}
    )
    gw = GitHubGateway("o/r", runner=fr)

    evidence = gw.pr_diff(100, "base-sha", "head-sha")

    assert evidence.base_oid == "base-sha"
    assert evidence.head_oid == "head-sha"
    argv, _ = fr.calls[0]
    assert "repos/o/r/compare/base-sha...head-sha" in argv


def test_pr_diff_empty_answer_is_returned_empty_not_invented():
    gw = GitHubGateway("o/r", runner=FakeRunner())

    assert gw.pr_diff(100, "base-sha", "head-sha").text == ""


def test_get_issue_parses_response():
    resp = {"number": 5, "title": "x", "labels": [{"name": "ai-ready"}],
            "state": "OPEN", "author": {"login": "a"}}
    fr = FakeRunner({"issue view 5": resp})
    gw = GitHubGateway("o/r", runner=fr)
    iss = gw.get_issue(5)
    assert iss.number == 5 and iss.has(Label.AI_READY)


# ── external-repo reviewer: build_review_world + ai-review parsing ────────────
def test_parse_pr_round_trips_ai_review_label():
    # the ai-review tag must survive parsing into PRView (it's an enum member now;
    # _labels silently drops names that aren't in Label, so this proves the wiring).
    pr = parse_pr({"number": 815, "labels": [{"name": "ai-review"}], "state": "OPEN"})
    assert pr.has(Label.AI_REVIEW)


# ── P0.7: loud warning when a world-building list hits the --limit cap ────────


# ── native "blocked by" dependency gate ───────────────────────────────────────
# ── closing-reference graph for the duplicate-PR guard (#39) ─────────────────
# ── free CI re-run (#38) ─────────────────────────────────────────────────────
def _pr_with_red_run(conclusion: str):
    def runner(argv, token):
        key = " ".join(argv)
        if "pr view 581" in key:
            return json.dumps({
                "number": 581, "state": "OPEN", "headRefOid": "head-581",
            })
        if "/commits/head-581/check-runs" in key:
            return json.dumps([{"check_runs": [{
                "name": "invocation",
                "status": "completed",
                "conclusion": conclusion,
                "details_url": (
                    "https://github.com/o/r/actions/runs/4242/job/99"
                ),
            }]}])
        if "/commits/head-581/statuses" in key:
            return json.dumps([[]])
        if "actions/runs/4242" in key:
            return json.dumps({
                "id": 4242,
                "head_sha": "head-581",
                "run_attempt": 1,
                "conclusion": conclusion,
            })
        return ""

    return GitHubGateway("o/r", runner=runner)


# ── wedged-run cancel (keifu PR #177) ────────────────────────────────────────
def test_failed_log_uses_the_same_verified_attempt_as_the_rerun_path():
    fr = FakeRunner({
        "api repos/o/r/actions/runs/77": {"id": 77, "head_sha": "head", "run_attempt": 2},
        "run view 77": "boom",
    })
    gw = GitHubGateway("o/r", runner=fr)
    assert gw.failed_run_log("head", (77,)) == "boom"
    assert "--attempt" in fr.calls[-1][0]
    assert fr.calls[-1][0][fr.calls[-1][0].index("--attempt") + 1] == "2"


# ── _subprocess_runner: benign statusCheckRollup noise gated behind debug ──────
import subprocess as _subprocess  # noqa: E402
from dataclasses import dataclass as _dc  # noqa: E402
from issue_agent.adapters.github import _subprocess_runner  # noqa: E402


@_dc
class _FakeCompleted:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _fake_run_factory(rc, stderr):
    def _run(argv, capture_output, text, env, timeout):  # match subprocess.run kw
        return _FakeCompleted(returncode=rc, stdout="", stderr=stderr)
    return _run


def test_non_benign_gh_failure_stays_loud_without_debug(monkeypatch, capsys):
    """A REAL failure (e.g. a 422 self-approval on submit) must never be silenced —
    the whole reason the stderr write exists. Not a pr-checks/rollup error → loud."""
    monkeypatch.delenv("ISSUE_AGENT_DEBUG", raising=False)
    monkeypatch.setattr(
        _subprocess, "run",
        _fake_run_factory(1, "HTTP 422: Can not approve your own pull request"),
    )
    _subprocess_runner(
        ["gh", "api", "repos/o/r/pulls/1/reviews", "--method", "POST"], None
    )
    err = capsys.readouterr().err
    assert "gh failed" in err and "422" in err


# ── V10: infra failure ≠ "nothing to do" ──────────────────────────────────────
import pytest  # noqa: E402
from issue_agent.adapters.github import (  # noqa: E402
    GhUnavailable,
    classify_gh_failure,
)


def _run_with(monkeypatch, stderr, rc=1):
    monkeypatch.setattr(_subprocess, "run", _fake_run_factory(rc, stderr))
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
    "stderr",
    [
        "HTTP 403: API rate limit exceeded for user ID 583231. (https://api.github.com/…)",
        "HTTP 429: Too Many Requests",
        "You have exceeded a secondary rate limit. Please wait a few minutes.",
        "HTTP 403: x-ratelimit-remaining: 0",
    ],
)
def test_runner_raises_rate_limit(monkeypatch, stderr):
    """The V10 case: throttling used to return "" → the tick observed an empty
    world and acted on it. It must now be an exception carrying its kind."""
    with pytest.raises(GhUnavailable) as ei:
        _run_with(monkeypatch, stderr)()
    assert ei.value.kind == "rate-limit"
    assert ei.value.timed_out is False


@pytest.mark.parametrize(
    "stderr",
    [
        "error connecting to api.github.com",
        "Could not resolve host: api.github.com",
        "dial tcp: lookup api.github.com: no such host",
        "Get https://api.github.com: net/http: TLS handshake timeout",
        "connection refused",
    ],
)
def test_runner_raises_network(monkeypatch, stderr):
    with pytest.raises(GhUnavailable) as ei:
        _run_with(monkeypatch, stderr)()
    assert ei.value.kind == "network"


@pytest.mark.parametrize(
    "stderr",
    [
        "runtime/cgo: pthread_create failed: Resource temporarily unavailable",
        "runtime: failed to create new OS thread\nfatal error: newosproc",
        "fork/exec /usr/bin/gh: cannot allocate memory",
    ],
)
def test_runner_raises_on_local_resource_exhaustion(monkeypatch, stderr):
    """A local process/thread failure returned no GitHub answer. Treating it as
    an ordinary empty response releases dependency gates under peak load."""

    with pytest.raises(GhUnavailable) as error:
        _run_with(monkeypatch, stderr)()
    assert error.value.kind == "local-resource"


def test_runner_bounds_a_local_crash_dump_in_the_operator_log(
    monkeypatch,
    capsys,
) -> None:
    stderr = (
        "runtime/cgo: pthread_create failed: Resource temporarily unavailable\n"
        + "register dump line\n" * 10_000
    )

    with pytest.raises(GhUnavailable):
        _run_with(monkeypatch, stderr)()

    logged = capsys.readouterr().err
    assert "pthread_create failed" in logged
    assert "[gh stderr truncated]" in logged
    assert len(logged.encode()) < 9_000


def test_runner_redacts_and_bounds_a_large_publication_body(
    monkeypatch,
    capsys,
) -> None:
    body = "untrusted body " * 10_000

    with pytest.raises(GhUnavailable):
        _run_with(
            monkeypatch,
            "runtime/cgo: pthread_create failed: Resource temporarily unavailable",
        )(["gh", "issue", "comment", "7", "--body", body])

    logged = capsys.readouterr().err
    assert body[:100] not in logged
    assert "<body redacted:" in logged
    assert len(logged.encode()) < 9_000


@pytest.mark.parametrize(
    "error",
    [
        BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable"),
        OSError(errno.ENOMEM, "Cannot allocate memory"),
    ],
)
def test_runner_raises_when_local_exhaustion_prevents_gh_from_starting(
    monkeypatch,
    capsys,
    error,
) -> None:
    def fail_to_start(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(_subprocess, "run", fail_to_start)

    with pytest.raises(GhUnavailable) as unavailable:
        _subprocess_runner(["gh", "issue", "list"], None)

    assert unavailable.value.kind == "local-resource"
    assert unavailable.value.argv == ("gh", "issue", "list")
    assert "gh failed to start" in capsys.readouterr().err


def test_spawn_failure_redacts_a_large_graphql_body_field(
    monkeypatch,
    capsys,
) -> None:
    body = "untrusted review " * 10_000

    def fail_to_start(*_args, **_kwargs):
        raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")

    monkeypatch.setattr(_subprocess, "run", fail_to_start)

    with pytest.raises(GhUnavailable):
        _subprocess_runner(
            ["gh", "api", "graphql", "-f", f"body={body}"],
            None,
        )

    logged = capsys.readouterr().err
    assert body[:100] not in logged
    assert "body=<redacted:" in logged
    assert len(logged.encode()) < 3_000


@pytest.mark.parametrize(
    "stderr, kind",
    [
        ("HTTP 502: Bad gateway", "server"),
        ("HTTP 503 (https://api.github.com/graphql)", "server"),
        # contains "timeout" — the 5xx test must win, or a GitHub outage reads as
        # a local network fault and the operator debugs the wrong box.
        ("HTTP 504: Gateway Timeout", "server"),
        # a bare 5xx-looking number is NOT an outage — it's an issue number.
        ("GraphQL: Could not resolve to an Issue with the number 502.", None),
    ],
)
def test_server_5xx_classification_requires_the_http_token(stderr, kind):
    assert classify_gh_failure(stderr) == kind


@pytest.mark.parametrize(
    "stderr",
    [
        "HTTP 401: Requires authentication (https://api.github.com/graphql)",
        "gh: Bad credentials (HTTP 401)",
    ],
)
def test_auth_outages_are_transient_unavailability(stderr):
    assert classify_gh_failure(stderr) == "auth"


def test_runner_raises_network_on_timeout(monkeypatch, capsys):
    """The bounded kill produced NO data; returning "" made a hung `gh pr view` look
    like a successful read of an empty world. `timed_out` marks it as the
    per-call case."""
    def _timeout(argv, capture_output, text, env, timeout):
        raise _subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    monkeypatch.setattr(_subprocess, "run", _timeout)
    with pytest.raises(GhUnavailable) as ei:
        _subprocess_runner(["gh", "pr", "view", "374", "--repo", "o/r"], None)
    assert ei.value.kind == "network" and ei.value.timed_out is True
    assert "timed out" in capsys.readouterr().err


def test_gh_unavailable_detail_is_one_bounded_line():
    exc = GhUnavailable("rate-limit", "API rate limit exceeded\n  for user ID 1\n" + "x" * 500)
    assert "\n" not in exc.detail and len(exc.detail) <= 200
    assert "rate-limited" in str(exc)


def _raising_runner(exc):
    def runner(argv, token):
        raise exc
    return runner


# ── review C2: every `gh … list` states its ceiling ───────────────────────────
def _limit_of(argv):
    return argv[argv.index("--limit") + 1] if "--limit" in argv else None


def test_gh_rejects_a_list_subcommand_without_limit():
    """review C2, made structural: a `gh … list` argv with no --limit is a BUG in
    this module (silent 30-item truncation), so it raises before gh ever runs
    rather than returning a plausible-looking short answer."""
    fr = FakeRunner()
    gw = GitHubGateway("o/r", runner=fr)
    with pytest.raises(ValueError):
        gw._gh_json(["issue", "list", "--repo", "o/r", "--json", "number"])
    with pytest.raises(MissingListLimit):
        gw._gh(["pr", "list", "--repo", "o/r"])          # raw-text list calls too
    assert fr.calls == []                                 # guard fires BEFORE gh runs
    # anti-vacuity: the same calls WITH --limit go through, and non-list
    # subcommands are untouched.
    gw._gh_json(["issue", "list", "--repo", "o/r", "--json", "number", "--limit", "500"])
    gw._gh(["pr", "view", "1", "--repo", "o/r"])
    gw._gh(["api", "repos/o/r/issues/5/dependencies/blocked_by"])
    assert len(fr.calls) == 3


# ── PR conversation → review prompt (2026-08-08, tauri #613/#614) ─────────────
def test_pr_discussion_is_newest_first_and_keeps_bot_authors():
    # Newest first because the byte ceiling truncates the tail and the answer to
    # "was this demand already addressed?" is at the recent end. Bot comments
    # stay: the fixer's own blocked-reason comment is the one that matters here.
    text = format_pr_discussion((
        Comment(author="author", body="opened"),
        Comment(author="harness-fixer[bot]", body="no display in this deployment"),
    ))
    assert text.index("no display in this deployment") < text.index("opened")
    assert "@harness-fixer[bot]" in text


def test_an_empty_thread_renders_nothing():
    assert format_pr_discussion(()) == ""
    assert format_pr_discussion((Comment(author="a", body="   "),)) == ""


def test_a_long_thread_is_truncated_with_an_explicit_marker():
    comments = tuple(
        Comment(author="rev", body=f"comment {n} " + "x" * 400) for n in range(200)
    )
    text = format_pr_discussion(comments)
    assert "[older comments truncated]" in text
    # The NEWEST survives, the oldest is what fell off.
    assert "comment 199" in text
    assert "comment 0 " not in text


def test_one_huge_comment_is_capped_and_says_so():
    text = format_pr_discussion((
        Comment(author="rev", body="old point"),
        Comment(author="spam", body="A" * 50_000),
    ))
    # Marked, not silently amputated — an unmarked cut reads as a whole comment.
    assert "[…comment truncated]" in text
    assert len(text.encode()) < 2_500          # one capped comment + the short one
    assert "old point" in text                 # the rest of the thread survives


def test_a_comment_under_the_ceiling_is_never_marked():
    text = format_pr_discussion((Comment(author="rev", body="short and whole"),))
    assert text == "@rev: short and whole"


# ── review M13 / D6: the two repair legs' prompt inputs ───────────────────────
def test_format_review_comments_renders_path_line_author_body():
    """review M13: the fixer never saw the file:line asks reviewers actually make.
    Formatting lives in Python (not in a jq filter inside an argv) so this text —
    the whole point of the feature — is assertable."""
    raw = [
        {"path": "a.py", "line": 12, "user": {"login": "rev"}, "body": "rename this",
         "created_at": "t1", "in_reply_to_id": None},
        # line null (outdated hunk) → original_line, as the bash's `.line // 0` did
        {"path": "b.py", "line": None, "original_line": 3, "user": {"login": "rev"},
         "body": "  drop the flag  ", "created_at": "t2"},
        # a REPLY is the fixer's own thread, not a new ask → excluded
        {"path": "a.py", "line": 12, "user": {"login": "fixer"}, "body": "done",
         "created_at": "t3", "in_reply_to_id": 99},
    ]
    assert format_review_comments(raw).splitlines() == [
        "a.py:12 (rev): rename this",
        "b.py:3 (rev): drop the flag",
    ]


def test_format_review_comments_keeps_the_newest_and_bounds_the_paste():
    # oldest→newest, last _INLINE_COMMENT_LIMIT — an unbounded paste is how a long
    # review thread blows the context window on the leg that must not fail.
    raw = [
        {"path": "f.py", "line": n, "user": {"login": "rev"}, "body": f"c{n}",
         "created_at": f"t{n:02d}"}
        for n in range(1, 21)
    ]
    lines = format_review_comments(list(reversed(raw))).splitlines()
    assert len(lines) == _INLINE_COMMENT_LIMIT
    assert lines[0].endswith("c13") and lines[-1].endswith("c20")


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        {"message": "Not Found"},                       # an API error object
        [{"body": "no path, no line, no user"}],        # every field missing
        ["not-a-dict"],
    ],
)
def test_format_review_comments_tolerates_every_malformed_shape(raw):
    out = format_review_comments(raw)
    assert isinstance(out, str)                         # never raises


def test_review_comments_fetches_top_level_comments_and_formats_them():
    raw = [{"path": "a.py", "line": 4, "user": {"login": "rev"}, "body": "fix",
            "created_at": "t1"}]
    fr = FakeRunner({"api repos/o/r/pulls/100/comments": raw})
    gw = GitHubGateway("o/r", runner=fr)
    assert gw.review_comments(100) == "a.py:4 (rev): fix"
    argv = fr.calls[0][0]
    assert argv[1] == "api" and argv[2].startswith("repos/o/r/pulls/100/comments")
    # per_page: the endpoint's default page is 30 and we keep the NEWEST few —
    # reading page 1 only would hand the fixer the OLDEST asks (review C2's shape).
    assert "per_page=100" in argv[2]


@pytest.mark.parametrize(
    "runner",
    [
        _raising_runner(GhUnavailable("rate-limit", "API rate limit exceeded")),
        _raising_runner(RuntimeError("gh boom")),
        FakeRunner(),                                   # gh answered ""
    ],
)
def test_review_comments_fails_soft_to_empty_string(runner):
    """D6: a comment fetch may never kill a dispatch — address-review must still
    run with the review BODY alone rather than not run at all."""
    assert GitHubGateway("o/r", runner=runner).review_comments(100) == ""


def test_failed_run_log_tails_only_the_observed_revision_bound_run():
    log = "\n".join(f"line {i}" for i in range(1, 301))
    fr = FakeRunner({
        "api repos/o/r/actions/runs/4242": {"id": 4242, "head_sha": "head", "run_attempt": 1},
        "run view 4242": log,
    })
    gw = GitHubGateway("o/r", runner=fr)
    out = gw.failed_run_log("head", (4242,))
    assert out.splitlines() == [f"line {i}" for i in range(301 - _FAILED_LOG_LINES, 301)]
    view_argv = fr.calls[1][0]
    assert view_argv[1:4] == ["run", "view", "4242"] and "--log-failed" in view_argv


@pytest.mark.parametrize("evidence", [
    {"id": 1, "head_sha": "old-head", "run_attempt": 1},
    {"id": 2, "head_sha": "head", "run_attempt": 1},
    {"id": 1, "head_sha": "head", "run_attempt": True},
    {"id": "bad", "head_sha": "head", "run_attempt": 1},
    {}, None,
])
def test_failed_run_log_never_borrows_a_stale_or_unverified_run(evidence):
    fr = FakeRunner({
        "api repos/o/r/actions/runs/1": evidence,
        "run list": [{"databaseId": 2}], "run view 1": "wrong log",
    })
    gw = GitHubGateway("o/r", runner=fr)
    assert gw.failed_run_log("head", (1,)) == ""
    assert not any(c[0][1] == "run" for c in fr.calls)


@pytest.mark.parametrize("head,run_ids", [("", (1,)), ("head", ()), ("head", (True,)), ("head", (-1,))])
def test_failed_run_log_empty_when_identity_is_unknown(head, run_ids):
    fr = FakeRunner({"run list": []})
    gw = GitHubGateway("o/r", runner=fr)
    assert gw.failed_run_log(head, run_ids) == ""
    assert fr.calls == []


@pytest.mark.parametrize(
    "runner",
    [
        _raising_runner(GhUnavailable("rate-limit", "API rate limit exceeded")),
        _raising_runner(RuntimeError("gh boom")),
        FakeRunner(),                                   # gh answered ""
    ],
)
def test_failed_run_log_fails_soft_to_empty_string(runner):
    """D6: a log fetch may never kill a dispatch — the fixer can rediscover the
    failure itself, but only if it is actually dispatched."""
    assert GitHubGateway("o/r", runner=runner).failed_run_log("head", (1,)) == ""


def test_failed_run_log_does_not_swallow_a_programmer_error():
    """The fail-soft catch must not hide review C2's guard: a list argv built
    without --limit is a bug in this module, not a transient gh failure."""
    gw = GitHubGateway("o/r", runner=_raising_runner(MissingListLimit("no --limit")))
    with pytest.raises(MissingListLimit):
        gw.failed_run_log("head", (1,))


# ── P1R-13: `tail -150` bounds LINE count, not bytes ──────────────────────────
def test_failed_run_log_bounds_by_bytes_not_just_lines():
    """Repro: a log whose lines are individually enormous (a minified bundle,
    a base64 blob, a per-line JSON dump) sails straight through the 150-LINE
    cap — measured, a 200MB CI log's last 150 lines was still a
    150,001,711-char prompt. ONE 5MB line (well within the 150-line cap) is
    enough to demonstrate the byte ceiling holds independently."""
    huge_line = "x" * 5_000_000
    fr = FakeRunner({
        "api repos/o/r/actions/runs/1": {"id": 1, "head_sha": "head", "run_attempt": 1},
        "run view 1": huge_line,
    })
    gw = GitHubGateway("o/r", runner=fr)
    out = gw.failed_run_log("head", (1,))
    assert len(out.encode("utf-8")) <= _FAILED_LOG_BYTES
    assert out  # anti-vacuity: real (truncated) content survives, not wiped to ""


def test_failed_run_log_under_the_byte_cap_is_untouched():
    # anti-vacuity twin: a normal, small log is NOT truncated by the new cap —
    # the byte ceiling only bites the pathological case.
    log = "\n".join(f"line {i}" for i in range(1, 301))
    fr = FakeRunner({
        "api repos/o/r/actions/runs/1": {"id": 1, "head_sha": "head", "run_attempt": 1},
        "run view 1": log,
    })
    gw = GitHubGateway("o/r", runner=fr)
    out = gw.failed_run_log("head", (1,))
    assert out.splitlines() == [f"line {i}" for i in range(301 - _FAILED_LOG_LINES, 301)]
    assert len(out.encode("utf-8")) < _FAILED_LOG_BYTES


def test_format_review_comments_bounds_pathological_comment_bytes():
    """P1R-13 repro: an inline review comment is writable by ANY user with
    read access to the repo (not just the PR author) — 10 x 500KB comments is
    otherwise a ~4MB prompt slipping through the OLD item-count-only cap."""
    huge = "A" * 500_000
    raw = [
        {"path": f"f{n}.py", "line": n, "user": {"login": "rev"}, "body": huge,
         "created_at": f"t{n:02d}"}
        for n in range(1, 11)
    ]
    out = format_review_comments(raw)
    assert len(out.encode("utf-8")) <= _REVIEW_COMMENTS_TOTAL_BYTES
    assert out  # anti-vacuity: not wiped to "", just bounded


def test_format_review_comments_per_comment_cap_applies_even_under_the_item_limit():
    # anti-vacuity twin: a SINGLE oversized comment (well under the 8-item
    # count limit) must still be capped by the per-comment byte ceiling, not
    # just the overall-block ceiling kicking in once several are combined.
    huge = "B" * 100_000
    raw = [{"path": "a.py", "line": 1, "user": {"login": "rev"}, "body": huge,
            "created_at": "t1"}]
    out = format_review_comments(raw)
    assert len(out.encode("utf-8")) <= _REVIEW_COMMENT_BODY_BYTES + 20  # + "path:line (author): " prefix
    assert out.startswith("a.py:1 (rev): B")


def test_format_review_comments_small_bodies_are_unaffected_by_the_cap():
    # anti-vacuity twin for the ordinary case (already covered above, restated
    # explicitly against the new byte constants so a regression that shrinks
    # them below a real comment's size is caught here too).
    raw = [{"path": "a.py", "line": 12, "user": {"login": "rev"}, "body": "rename this",
            "created_at": "t1", "in_reply_to_id": None}]
    assert format_review_comments(raw) == "a.py:12 (rev): rename this"


# ── check start times + world timestamp (#30) ────────────────────────────────


# ── merge cleanup end-to-end: merge → GitHub auto-close → next poll (#28) ─────
# These drive the REAL world build, because the defect lived in what the build
# FETCHES, not in what the domain does with it. Every previous cleanup test
# handed `cleanup_actions` an `issue_observed_pr_open=True` that the post-merge
# loop never supplies, so all of them passed while zero issues in five
# deployment days ever reached ai-done.

# ── #48: a failed `gh pr checks` is UNKNOWN, not an empty world ──────────────


def test_get_pr_marks_checks_unknown_when_the_read_fails():
    """The hole (#48): a swallowed non-infra CI read failure used to
    rebuild the PR with checks=() — indistinguishable from a no-CI repo, which
    the fix policy treats as mergeable. The PR must instead say the checks were
    never read this tick."""
    pr_resp = {"number": 100, "headRefName": "fix/5-x", "state": "OPEN", "labels": []}
    fr = FakeRunner({"pr view 100": pr_resp})  # policy read unmatched → unavailable
    pr = GitHubGateway("o/r", runner=fr).get_pr(100)
    assert pr.checks == () and isinstance(pr.ci, Unavailable)


def test_a_skipped_checks_call_is_unread_not_empty():
    """get_pr(checks=False) never asked — a PR that flips OPEN between the
    list and the view must not arrive as honest-NONE mergeable (review F3)."""
    pr_resp = {"number": 100, "headRefName": "fix/5-x", "state": "OPEN", "labels": []}
    fr = FakeRunner({"pr view 100": pr_resp})
    pr = GitHubGateway("o/r", runner=fr).get_pr(100, checks=False)
    assert isinstance(pr.ci, Unavailable)


# ── #163: the stale merged tail is built from the list payload, not `pr view` ─
# GitHub's search index keeps serving merged PRs for about a day after their
# label is removed. Nineteen of them cost a `gh pr view` EACH, in the fix build
# AND the review build — ~38 of ~71 GraphQL points per tick round. The labelled
# list costs the same two requests whatever fields it selects, so the same facts
# come back for free.
_TERMINAL_ROW = {
    "number": 60,
    "state": "MERGED",
    "labels": [{"name": "ai-done"}],
    "headRefName": "fix/12-thing",
    "headRefOid": "abc123",
    "baseRefName": "dev",
    "baseRefOid": "def456",
    "title": "fix: the thing",
    "isDraft": False,
    "author": {"login": "fix-bot"},
    "closingIssuesReferences": [{"number": 12}],
}


def _tail_runner(rows, views=None, calls=None):
    def runner(argv, token):
        key = " ".join(argv)
        if calls is not None:
            calls.append(key)
        if "issue list" in key:
            return "[]"
        if "pr list" in key:
            return json.dumps(rows)
        for number, payload in (views or {}).items():
            if f"pr view {number}" in key:
                return json.dumps(payload)
        return ""

    return runner


