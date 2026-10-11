"""GitHubGateway — pure JSON→WorldState parsers + identity + write-call shapes.

The parsers are the real-vs-fake seam, so they are asserted directly against the
documented gh --json shapes. The gateway is driven with a fake runner that
records argv and returns canned JSON (no network, no real gh)."""

import errno
import json

from issue_agent.adapters.github import (
    _CLOSED_CLEANUP_LIMIT,
    _FAILED_LOG_BYTES,
    _FAILED_LOG_LINES,
    _INLINE_COMMENT_LIMIT,
    _LIST_LIMIT,
    _REVIEW_COMMENT_BODY_BYTES,
    _REVIEW_COMMENTS_TOTAL_BYTES,
    GitHubGateway,
    Identity,
    MissingListLimit,
    format_pr_discussion,
    format_review_comments,
    build_add_blocked_by_args,
    build_blocker_id_args,
    build_close_issue_args,
    build_comment_args,
    build_enable_auto_merge_args,
    build_relabel_args,
    build_submit_review_args,
    parse_closing_refs,
    parse_issue,
    parse_pr,
    parse_terminal_pr,
    parse_world,
)
from issue_agent.domain.actions import (
    ClearIssueState,
    CloseIssue,
    Relabel,
    RemoveWorktree,
    Target,
)
from issue_agent.domain.fix import FixConfig, FixState, decide_fix
from issue_agent.domain.labels import Label
from issue_agent.domain.world import CIState, Comment
from issue_agent.domain.ci import ci_state, parse_timestamp
from issue_agent.domain.checks import Unavailable, Observed
from tests.fakes.workflow_authority import resolve_snapshot


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


def test_parse_pr_maps_reviews_and_closing_ref():
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
        "closingIssuesReferences": [{"number": 5}],
    }
    pr = parse_pr(d)
    assert pr.number == 100
    assert pr.head_ref_name == "fix/5-null-token"
    assert pr.base_ref_oid == "base123"
    assert pr.head_ref_oid == "abc123"
    assert pr.closing_issue == 5
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


def test_parse_world():
    w = parse_world([{"number": 1, "state": "OPEN"}], [{"number": 2, "state": "MERGED"}])
    assert len(w.issues) == 1 and len(w.prs) == 1
    assert w.prs[0].state == "MERGED"


def test_parse_issue_extracts_declared_paths():
    d = {"number": 5, "state": "OPEN",
         "body": "Implement `backend/app/infra/s3_file_store.py` per ADR-0007."}
    iss = parse_issue(d)
    assert "backend/app/infra/s3_file_store.py" in iss.declared_paths


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


def test_parse_pr_reads_draft_and_author():
    d = {"number": 1, "state": "OPEN", "isDraft": True, "author": {"login": "alice"}}
    p = parse_pr(d)
    assert p.is_draft is True and p.author == "alice"
    # defaults when absent
    p2 = parse_pr({"number": 2, "state": "OPEN"})
    assert p2.is_draft is False and p2.author == ""


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


def test_gateway_reports_scheduler_progress_after_each_bounded_call():
    progress = []
    gateway = GitHubGateway(
        "o/r",
        runner=lambda _argv, _token: '[{"number":7}]',
        progress=lambda: progress.append("completed"),
    )

    assert gateway.list_issue_numbers("ai-ready") == [7]
    assert progress == ["completed"]


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
    assert build_blocker_id_args("o/r", 42) == ["api", "repos/o/r/issues/42", "--jq", ".id"]
    assert build_add_blocked_by_args("o/r", 7, 5551234) == [
        "api", "repos/o/r/issues/7/dependencies/blocked_by",
        "--method", "POST", "-F", "issue_id=5551234",
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


def test_issue_closure_reads_exact_state_and_reason():
    fr = FakeRunner(
        {
            "issue view 5": {
                "state": "CLOSED",
                "stateReason": "COMPLETED",
            }
        }
    )
    gw = GitHubGateway("o/r", runner=fr)

    assert gw.issue_closure(5) == (True, "completed")
    argv, _ = fr.calls[0]
    assert argv[-2:] == ["--json", "state,stateReason"]


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


def test_add_blocked_by_resolves_id_then_posts():
    # #7 blocked_by #42: first resolve #42's numeric id, then POST it.
    fr = FakeRunner({"api repos/o/r/issues/42": "5551234"})
    gw = GitHubGateway("o/r", runner=fr)
    gw.add_blocked_by(7, 42)
    assert len(fr.calls) == 2
    id_argv, _ = fr.calls[0]
    assert id_argv[1] == "api" and id_argv[2] == "repos/o/r/issues/42" and ".id" in id_argv
    post_argv, _ = fr.calls[1]
    assert post_argv[2] == "repos/o/r/issues/7/dependencies/blocked_by"
    assert "POST" in post_argv and "issue_id=5551234" in post_argv


def test_add_blocked_by_failsoft_when_blocker_missing():
    # blocker id lookup returns "" → no POST, no raise.
    fr = FakeRunner()  # every call → ""
    gw = GitHubGateway("o/r", runner=fr)
    gw.add_blocked_by(7, 42)
    assert len(fr.calls) == 1  # only the id lookup; POST skipped


def test_submit_review_returns_true_on_success():
    # gh `api ... POST` prints the created-review JSON on success → non-empty → True.
    fr = FakeRunner({"api repos/o/r/pulls/100/reviews": {"id": 1, "state": "APPROVED"}})
    gw = GitHubGateway("o/r", runner=fr)
    assert gw.submit_review(100, "APPROVE", "lgtm", "head") is True
    argv, _ = fr.calls[0]
    assert argv[1] == "api" and "--method" in argv and "POST" in argv


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


def test_pr_diff_empty_read_is_unavailable_not_approvable():
    gw = GitHubGateway("o/r", runner=FakeRunner())

    with pytest.raises(RuntimeError, match="diff is unavailable"):
        gw.pr_diff(100, "base-sha", "head-sha")


def test_has_review_flattens_all_paginated_api_pages():
    fr = FakeRunner(
        {
            "api repos/o/r/pulls/100/reviews": [
                [
                    {
                        "state": "APPROVED",
                        "body": "old",
                        "commit_id": "head",
                    }
                ],
                [
                    {
                        "state": "APPROVED",
                        "body": "lgtm",
                        "commit_id": "head",
                    }
                ],
            ]
        }
    )
    gw = GitHubGateway("o/r", runner=fr)

    assert gw.has_review(100, "APPROVE", "lgtm", "head")
    argv, _token = fr.calls[0]
    assert argv[-2:] == ["--paginate", "--slurp"]


def test_get_issue_parses_response():
    resp = {"number": 5, "title": "x", "labels": [{"name": "ai-ready"}],
            "state": "OPEN", "author": {"login": "a"}}
    fr = FakeRunner({"issue view 5": resp})
    gw = GitHubGateway("o/r", runner=fr)
    iss = gw.get_issue(5)
    assert iss.number == 5 and iss.has(Label.AI_READY)




def test_get_pr_checks_are_head_bound_and_keep_only_latest_attempt():
    calls = []

    def runner(argv, token):
        del token
        calls.append(argv)
        command = " ".join(argv)
        if "pr view 100" in command:
            return json.dumps(
                {
                    "number": 100,
                    "headRefOid": "h1",
                    "state": "OPEN",
                }
            )
        if "/commits/h1/check-runs" in command:
            return json.dumps(
                [
                    {
                        "check_runs": [
                            {
                                "id": 1,
                                "name": "unit",
                                "app": {"id": 7},
                                "status": "completed",
                                "conclusion": "failure",
                                "started_at": "2026-01-01T00:00:00Z",
                            },
                            {
                                "id": 2,
                                "name": "unit",
                                "app": {"id": 7},
                                "status": "completed",
                                "conclusion": "success",
                                "started_at": "2026-01-01T01:00:00Z",
                            },
                        ]
                    }
                ]
            )
        if "/commits/h1/statuses" in command:
            return json.dumps(
                [
                    [
                        {"context": "legacy", "state": "success"},
                        {"context": "legacy", "state": "failure"},
                    ]
                ]
            )
        if "/commits/h2/" in command:
            raise AssertionError("moving current head must not select CI")
        return ""

    checks = GitHubGateway("o/r", runner=runner)._checks_for_head("h1")
    assert [check["bucket"] for check in checks] == ["pass", "pass"]
    assert [check["name"] for check in checks] == ["unit", "legacy"]
    assert all(
        "/commits/h1/" in " ".join(call)
        for call in calls
        if "/commits/" in " ".join(call)
    )


# ── external-repo reviewer: build_review_world + ai-review parsing ────────────
def test_parse_pr_round_trips_ai_review_label():
    # the ai-review tag must survive parsing into PRView (it's an enum member now;
    # _labels silently drops names that aren't in Label, so this proves the wiring).
    pr = parse_pr({"number": 815, "labels": [{"name": "ai-review"}], "state": "OPEN"})
    assert pr.has(Label.AI_REVIEW)


def test_build_review_world_queries_trigger_label_and_no_issues():
    pr_resp = {"number": 815, "headRefName": "feature/x", "state": "OPEN",
               "labels": [{"name": "ai-review"}]}
    fr = FakeRunner({"pr list": [{"number": 815}], "pr view 815": pr_resp,
                     "pr checks 815": []})
    gw = GitHubGateway("o/ext", runner=fr, review_pr_label="ai-review")
    w = gw.build_review_world()
    # only PRs, no issue queries at all
    assert w.issues == ()
    assert [p.number for p in w.prs] == [815]
    assert not any(c[0][1] == "issue" for c in fr.calls)  # no `gh issue …`
    # the PR-list call carried our configured label
    list_calls = [c[0] for c in fr.calls if c[0][1:3] == ["pr", "list"]]
    assert list_calls and "ai-review" in list_calls[0]


def test_review_world_pr_view_omits_mergeability_fields():
    """The reviewer must never request mergeable/mergeStateStatus: GitHub computes
    them lazily and the call can hang forever on a just-merged PR, freezing the
    poll. Only the fixer (rebase logic) needs them. Regression for the review
    daemon wedging behind a hung `gh pr view` and never leaving a comment."""
    pr_resp = {"number": 815, "headRefName": "feature/x", "state": "OPEN",
               "labels": [{"name": "ai-review"}]}
    fr = FakeRunner({"pr list": [{"number": 815}], "pr view 815": pr_resp,
                     "pr checks 815": []})
    gw = GitHubGateway("o/ext", runner=fr, review_pr_label="ai-review")
    gw.build_review_world()
    view_calls = [c[0] for c in fr.calls if c[0][1:3] == ["pr", "view"]]
    assert view_calls, "expected a pr view call"
    fields = view_calls[0][view_calls[0].index("--json") + 1]
    assert "mergeable" not in fields and "mergeStateStatus" not in fields
    # the fixer path DOES ask for them (auto-rebase needs is_conflicting/needs_rebase)
    fr2 = FakeRunner({"pr view 815": pr_resp, "pr checks 815": []})
    GitHubGateway("o/r", runner=fr2).get_pr(815)
    v2 = [c[0] for c in fr2.calls if c[0][1:3] == ["pr", "view"]][0]
    assert "mergeable" in v2[v2.index("--json") + 1]








def test_review_world_survives_one_failing_pr_fetch():
    """One PR whose `gh pr view` returns empty (hung/killed → parse_pr(None) raises)
    must NOT abort the whole poll: the other PRs still populate the world, so a
    newer PR isn't stranded behind a broken older one. Regression for #382 going
    unreviewed while #374's view hung."""
    good = {"number": 900, "state": "OPEN", "labels": [{"name": "ai-review"}]}

    def runner(argv, token):
        key = " ".join(argv)
        if "pr list" in key:
            return json.dumps([{"number": 374}, {"number": 900}])
        if "pr view 374" in key:
            return ""  # the hung/killed PR → empty → parse would raise
        if "pr view 900" in key:
            return json.dumps(good)
        return ""

    gw = GitHubGateway("o/r", runner=runner, review_pr_label="ai-review")
    w = gw.build_review_world()
    assert [p.number for p in w.prs] == [900]  # 374 dropped, 900 survived


def test_build_steward_world_includes_open_and_closed_ai_done_and_open_prs():
    # open issue list returns #5; closed-ai-done list returns #9. The world must
    # union them so the final Done transition isn't missed on an already-closed issue.
    def runner(argv, token):
        key = " ".join(argv)
        if "issue list" in key and "--state open" in key:
            return json.dumps([{"number": 5}])
        if "issue list" in key and "--state closed" in key:
            assert "ai-done" in argv          # closed query is scoped to ai-done
            return json.dumps([{"number": 9}])
        if "issue view 5" in key:
            return json.dumps({"number": 5, "state": "OPEN", "labels": [{"name": "ai-ready"}]})
        if "issue view 9" in key:
            return json.dumps({"number": 9, "state": "CLOSED", "labels": [{"name": "ai-done"}]})
        if "pr list" in key:
            return json.dumps([{"number": 12}])
        if "pr view 12" in key:
            assert "pr checks" not in key
            return json.dumps(
                {
                    "number": 12,
                    "state": "OPEN",
                    "body": "change",
                    "files": [{"path": "src/auth/token.py"}],
                }
            )
        return ""

    gw = GitHubGateway("o/r", runner=runner)
    w = gw.build_steward_world()
    assert sorted(i.number for i in w.issues) == [5, 9]
    assert [pr.number for pr in w.prs] == [12]


# ── P0.7: loud warning when a world-building list hits the --limit cap ────────
def test_build_triage_world_warns_when_issue_list_hits_limit(capsys):
    # exactly _LIST_LIMIT items: gh gives no "there were more" signal short of
    # pagination, so a count that HITS the cap is the only observable proxy for
    # truncation — this must be LOUD (FAILURE-MODES #66), not silently dropped.
    def runner(argv, token):
        key = " ".join(argv)
        if "issue list" in key:
            return json.dumps([{"number": n} for n in range(1, _LIST_LIMIT + 1)])
        if "issue view" in key:
            return json.dumps({"number": int(argv[3]), "state": "OPEN"})
        return ""

    gw = GitHubGateway("o/r", runner=runner)
    w = gw.build_triage_world()
    assert len(w.issues) == _LIST_LIMIT
    err = capsys.readouterr().err
    assert "TRUNCATED" in err and str(_LIST_LIMIT) in err


def test_build_triage_world_no_warning_when_issue_list_below_limit(capsys):
    def runner(argv, token):
        key = " ".join(argv)
        if "issue list" in key:
            return json.dumps([{"number": 1}, {"number": 2}])
        if "issue view" in key:
            return json.dumps({"number": int(argv[3]), "state": "OPEN"})
        return ""

    gw = GitHubGateway("o/r", runner=runner)
    gw.build_triage_world()
    assert capsys.readouterr().err == ""


def test_list_all_open_pr_numbers_warns_when_pr_list_hits_limit(capsys):
    # same helper, `gh pr list` call site — proves the warning isn't wired to
    # only one of the two list kinds named in P0.7.
    prs = [{"number": n} for n in range(1, _LIST_LIMIT + 1)]
    fr = FakeRunner({"pr list": prs})
    gw = GitHubGateway("o/r", runner=fr)
    nums = gw.list_all_open_pr_numbers()
    assert len(nums) == _LIST_LIMIT
    err = capsys.readouterr().err
    assert "TRUNCATED" in err and str(_LIST_LIMIT) in err


def test_list_all_open_pr_numbers_no_warning_below_limit(capsys):
    fr = FakeRunner({"pr list": [{"number": 1}]})
    gw = GitHubGateway("o/r", runner=fr)
    gw.list_all_open_pr_numbers()
    assert capsys.readouterr().err == ""


# ── native "blocked by" dependency gate ───────────────────────────────────────
def test_has_open_blocker_false_when_summary_zero_no_extra_call():
    # summary.blocked_by == 0 → return False WITHOUT the second (list) API call.
    calls = []

    def runner(argv, token):
        calls.append(" ".join(argv))
        key = " ".join(argv)
        if key.endswith("issue_dependencies_summary.blocked_by // 0") or "issues/57" in key:
            return "0"
        return ""

    gw = GitHubGateway("o/r", runner=runner)
    assert gw.has_open_blocker(57) is False
    # cheapest-first: never hit the /dependencies/blocked_by endpoint
    assert not any("dependencies/blocked_by" in c for c in calls)


def test_has_open_blocker_true_when_a_blocker_is_open():
    def runner(argv, token):
        key = " ".join(argv)
        if "dependencies/blocked_by" in key:      # the list call
            return json.dumps([91])  # jq reduced to open issue numbers
        if "issues/57" in key:                     # the summary call
            return "2"
        return ""

    gw = GitHubGateway("o/r", runner=runner)
    assert gw.has_open_blocker(57) is True


def test_has_open_blocker_false_when_all_blockers_closed():
    # summary non-zero (counts a CLOSED blocker) but no blocker is open → False.
    def runner(argv, token):
        key = " ".join(argv)
        if "dependencies/blocked_by" in key:
            return json.dumps([])
        if "issues/57" in key:
            return "1"
        return ""

    gw = GitHubGateway("o/r", runner=runner)
    assert gw.has_open_blocker(57) is False


def test_has_open_blocker_fail_soft_on_error():
    # any API error → False (never strand a dispatchable issue on a transient read).
    def runner(argv, token):
        raise RuntimeError("gh boom")

    gw = GitHubGateway("o/r", runner=runner)
    assert gw.has_open_blocker(57) is False


def test_build_world_enriches_only_ready_issues_with_blocker_gate():
    # ai-ready #5 gets a blocked-by read; ai-working #6 does NOT (not gated).
    def runner(argv, token):
        key = " ".join(argv)
        if "issue list" in key and "ai-ready" in key:
            return json.dumps([{"number": 5}])
        if "issue list" in key and ("ai-working" in key or "ai-blocked" in key):
            return json.dumps([{"number": 6}]) if "ai-working" in key else "[]"
        if "pr list" in key:
            return "[]"
        if "issue view 5" in key:
            return json.dumps({"number": 5, "state": "OPEN", "labels": [{"name": "ai-ready"}]})
        if "issue view 6" in key:
            return json.dumps({"number": 6, "state": "OPEN", "labels": [{"name": "ai-working"}]})
        if "issues/5/dependencies/blocked_by" in key:
            return json.dumps([42])
        if "issues/5" in key:
            return "1"
        return ""

    gw = GitHubGateway("o/r", runner=runner)
    w = gw.build_world()
    by_num = {i.number: i for i in w.issues}
    assert by_num[5].blocked_by_open is True     # ready issue gated + blocked
    assert by_num[5].open_blocker_numbers == (42,)
    assert by_num[6].blocked_by_open is False    # working issue never gated


def test_build_world_gates_issues_linked_from_an_open_pr_on_their_blockers():
    # keifu #130/#157 (2026-08-03): the repair arm is gated on the same
    # blocked-by read as admission, so an issue that already has an OPEN PR
    # must pay it too — via the PR's closing reference (#130) or, absent one,
    # its numeric branch (#157). An ai-working issue with no PR still doesn't.
    calls = []

    def runner(argv, token):
        calls.append(" ".join(argv))
        key = " ".join(argv)
        if "issue list" in key and "ai-pr-open" in key:
            return json.dumps([{"number": 130}, {"number": 157}])
        if "issue list" in key and "ai-working" in key:
            return json.dumps([{"number": 6}])
        if "issue list" in key:
            return "[]"
        if "pr list" in key:
            return json.dumps([{"number": 100, "state": "OPEN"},
                               {"number": 101, "state": "OPEN"}])
        if "pr view 100" in key:
            return json.dumps({
                "number": 100, "state": "OPEN", "labels": [{"name": "ai-pr-open"}],
                "headRefName": "fix/999-unrelated",
                "closingIssuesReferences": [{"number": 130}],
            })
        if "pr view 101" in key:
            return json.dumps({
                "number": 101, "state": "OPEN", "labels": [{"name": "ai-pr-open"}],
                "headRefName": "fix/157-something", "closingIssuesReferences": [],
            })
        for n in (130, 157, 6):
            if f"issue view {n}" in key:
                return json.dumps({"number": n, "state": "OPEN",
                                   "labels": [{"name": "ai-pr-open"}]})
        if "issues/130/dependencies/blocked_by" in key:
            return json.dumps([185])
        if "issues/157/dependencies/blocked_by" in key:
            return json.dumps([185, 186])
        if "issues/130" in key or "issues/157" in key:
            return "1"
        return ""

    gw = GitHubGateway("o/r", runner=runner)
    by_num = {i.number: i for i in gw.build_world().issues}

    # linked by closing reference…
    assert by_num[130].blocked_by_open is True
    assert by_num[130].open_blocker_numbers == (185,)
    # …and linked only by a numeric branch name.
    assert by_num[157].blocked_by_open is True
    assert by_num[157].open_blocker_numbers == (185, 186)
    # The un-linked, non-ready issue never pays the read.
    assert by_num[6].blocked_by_open is False
    assert by_num[6].open_blocker_numbers == ()
    assert not any("issues/6" in c for c in calls)


def test_build_world_does_not_gate_an_issue_whose_only_pr_is_closed():
    # A non-OPEN PR gets no repair legs, so its issue buys nothing from the read.
    calls = []

    def runner(argv, token):
        calls.append(" ".join(argv))
        key = " ".join(argv)
        if "issue list" in key and "ai-pr-open" in key and "closed" not in key:
            return json.dumps([{"number": 130}])
        if "issue list" in key:
            return "[]"
        if "pr list" in key:
            return json.dumps([{"number": 100, "state": "MERGED"}])
        if "pr view 100" in key:
            return json.dumps({
                "number": 100, "state": "MERGED", "labels": [{"name": "ai-pr-open"}],
                "headRefName": "fix/130-x",
            })
        if "issue view 130" in key:
            return json.dumps({"number": 130, "state": "OPEN",
                               "labels": [{"name": "ai-pr-open"}]})
        return ""

    gw = GitHubGateway("o/r", runner=runner)
    assert gw.build_world().issues[0].blocked_by_open is False
    assert not any("dependencies/blocked_by" in c for c in calls)


def test_build_world_survives_an_open_pr_with_no_resolvable_issue():
    # A Jira-keyed branch and no closing reference: nothing to gate, and the
    # linkage scan must not raise on it.
    def runner(argv, token):
        key = " ".join(argv)
        if "issue list" in key:
            return "[]"
        if "pr list" in key:
            return json.dumps([{"number": 100, "state": "OPEN"}])
        if "pr view 100" in key:
            return json.dumps({
                "number": 100, "state": "OPEN", "labels": [{"name": "ai-pr-open"}],
                "headRefName": "feat/ACME-2468-thing",
            })
        return ""

    gw = GitHubGateway("o/r", runner=runner)
    w = gw.build_world()
    assert [p.number for p in w.prs] == [100] and w.issues == ()


def test_build_world_drops_an_open_pr_the_stale_search_still_labels():
    # keifu PR #179 (2026-08-03): an operator park removed ai-pr-open, but the
    # search-backed label list kept returning the PR for minutes, so every
    # path-overlapping ai-ready issue stayed held on a PR the harness no longer
    # owned. The per-PR fetch's labels are fresh — they are the authority.
    def runner(argv, token):
        key = " ".join(argv)
        if "issue list" in key:
            return "[]"
        if "pr list" in key:
            return json.dumps([{"number": 179, "state": "OPEN"}])
        if "pr view 179" in key:
            return json.dumps({
                "number": 179, "state": "OPEN",
                "labels": [{"name": "ai-parked"}],
                "files": [{"path": "src/app/mod.rs"}],
            })
        return ""

    gw = GitHubGateway("o/r", runner=runner)
    assert gw.build_world().prs == ()


def test_build_world_keeps_a_merged_pr_still_wearing_the_label():
    # The merge-cleanup arms exist to drain exactly this state; the freshness
    # filter must only govern the OPEN slice.
    def runner(argv, token):
        key = " ".join(argv)
        if "issue list" in key:
            return "[]"
        if "pr list" in key:
            return json.dumps([{"number": 60, "state": "MERGED"}])
        if "pr view 60" in key:
            return json.dumps({
                "number": 60, "state": "MERGED",
                "labels": [{"name": "ai-pr-open"}],
            })
        return ""

    gw = GitHubGateway("o/r", runner=runner)
    assert [p.number for p in gw.build_world().prs] == [60]


# ── closing-reference graph for the duplicate-PR guard (#39) ─────────────────
def test_parse_closing_refs_flattens_pr_to_issue_pairs():
    data = [
        {"number": 558, "author": {"login": "xnmp"}, "baseRefName": "main",
         "closingIssuesReferences": [{"number": 554}, {"number": 555}]},
        {"number": 559, "author": {"login": "fix-bot"}, "baseRefName": "dev",
         "closingIssuesReferences": [{"number": 7}]},
    ]
    refs = parse_closing_refs(data)
    assert [(r.pr, r.issue, r.author) for r in refs] == [
        (558, 554, "xnmp"), (558, 555, "xnmp"), (559, 7, "fix-bot"),
    ]
    assert refs[0].base_ref_name == "main"


def test_parse_closing_refs_tolerates_every_missing_field():
    # This feeds a guard that REFUSES work: a shape surprise must degrade to
    # "no evidence of a duplicate" (dispatch as before), never to an exception
    # that kills the whole world build.
    data = [
        {"number": 1},                                    # no refs key
        {"number": 2, "closingIssuesReferences": None},   # null refs
        {"number": 3, "author": None, "closingIssuesReferences": [{"number": 9}]},
        {"closingIssuesReferences": [{"number": 9}]},     # no PR number
        {"number": 5, "closingIssuesReferences": [{}, {"number": "x"}]},
        "not-a-dict",
    ]
    refs = parse_closing_refs(data)
    assert [(r.pr, r.issue, r.author) for r in refs] == [(3, 9, "")]
    assert parse_closing_refs(None) == () and parse_closing_refs({}) == ()


def test_open_closing_refs_asks_for_every_open_pr_in_one_call():
    fr = FakeRunner({"pr list": [
        {"number": 558, "author": {"login": "xnmp"},
         "closingIssuesReferences": [{"number": 554}]},
    ]})
    gw = GitHubGateway("o/r", runner=fr)
    refs = gw.open_closing_refs()
    assert [(r.pr, r.issue) for r in refs] == [(558, 554)]
    argv = fr.calls[0][0]
    assert argv[1:3] == ["pr", "list"]
    assert "--label" not in argv               # the guard's subject is UNLABELLED PRs
    assert "open" in argv and "closingIssuesReferences" in " ".join(argv)
    assert _limit_of(argv) is not None         # review C2: never an unbounded list
    assert len(fr.calls) == 1                  # one call, no per-PR fetch


def test_build_world_carries_closing_refs_into_the_world():
    def runner(argv, token):
        key = " ".join(argv)
        if "issue list" in key:
            return "[]"
        if "closingIssuesReferences" in key:
            return json.dumps([
                {"number": 558, "author": {"login": "xnmp"},
                 "closingIssuesReferences": [{"number": 554}]},
            ])
        return "[]"

    w = GitHubGateway("o/r", runner=runner).build_world()
    assert [(r.pr, r.issue, r.author) for r in w.closing_refs] == [(558, 554, "xnmp")]


def test_open_closing_refs_fails_soft_to_empty():
    # A guard that refuses work must never be the thing that crashes a poll —
    # and "" reads as the pre-#39 behaviour (dispatch).
    gw = GitHubGateway("o/r", runner=_raising_runner(RuntimeError("boom")))
    assert gw.open_closing_refs() == ()


# ── free CI re-run (#38) ─────────────────────────────────────────────────────
def test_rerun_failed_checks_targets_exact_revision_bound_runs():
    fr = FakeRunner({
        "api repos/o/r/actions/runs/4242": {
            "id": 4242,
            "head_sha": "head-1",
            "run_attempt": 3,
        },
        "api repos/o/r/actions/runs/4343": {
            "id": 4343,
            "head_sha": "head-1",
            "run_attempt": 1,
        },
    })
    gw = GitHubGateway("o/r", runner=fr)
    assert gw.rerun_failed_checks("head-1", (4242, 4343)) == (4242, 4343)
    reruns = [
        call[0][2]
        for call in fr.calls
        if call[0][1] == "api" and call[0][2].endswith("/rerun-failed-jobs")
    ]
    assert reruns == [
        "repos/o/r/actions/runs/4242/rerun-failed-jobs",
        "repos/o/r/actions/runs/4343/rerun-failed-jobs",
    ]


def test_rerun_failed_checks_rejects_a_run_from_another_head_before_writing():
    fr = FakeRunner({
        "api repos/o/r/actions/runs/4242": {
            "id": 4242,
            "head_sha": "newer-head",
            "run_attempt": 1,
        },
    })
    gw = GitHubGateway("o/r", runner=fr)
    assert gw.rerun_failed_checks("observed-head", (4242,)) is None
    assert not any(
        call[0][2].endswith("/rerun-failed-jobs") for call in fr.calls
    )


def test_rerun_failed_checks_preserves_transport_failure_for_durable_recovery():
    gw = GitHubGateway("o/r", runner=_raising_runner(GhUnavailable("server", "HTTP 503")))
    with pytest.raises(GhUnavailable):
        gw.rerun_failed_checks("head-1", (4242,))


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


def test_every_rerunnable_conclusion_is_found_by_run_id_at_the_observed_head():
    """#177: the free re-run's candidates are the failed runs of the CURRENT
    head. Each red conclusion must carry its workflow-run identity, and that id
    must resolve at the head the decision observed — an unidentified failure
    falls through to the paid leg instead of re-running a stranger's run."""
    for conclusion in ("failure", "timed_out", "startup_failure"):
        gw = _pr_with_red_run(conclusion)
        checks = gw._checks_for_head("head-581")
        assert checks[0]["bucket"] == "fail", conclusion
        assert "/actions/runs/4242" in checks[0]["link"], conclusion
        assert gw.workflow_run_attempts("head-581", (4242,)) == {4242: 1}
        assert gw.workflow_run_attempts("some-other-head", (4242,)) is None


def test_a_cancelled_run_reads_as_a_settled_failure_not_as_green():
    """The load-bearing half of the wedged-run remediation (keifu PR #177).

    The harness cancels a wedged run so the pre-existing free re-run can act on
    it, and that handoff exists ONLY because a cancelled run observes as a
    settled FAILURE. If it ever read as pass/none instead, the harness would
    have cancelled a PR's CI and then armed auto-merge on the silence.
    """

    def runner(argv, token):
        key = " ".join(argv)
        if "pr view 177" in key:
            return json.dumps({
                "number": 177, "state": "OPEN", "headRefOid": "head-177",
            })
        if "/commits/head-177/check-runs" in key:
            return json.dumps([{"check_runs": [{
                "name": "Test (windows-latest)",
                "status": "completed",
                "conclusion": "cancelled",
                "details_url": (
                    "https://github.com/o/r/actions/runs/4242/job/99"
                ),
            }]}])
        if "/commits/head-177/statuses" in key:
            return json.dumps([[]])
        if "actions/runs/4242" in key:
            return json.dumps({"conclusion": "cancelled"})
        return ""

    checks = GitHubGateway("o/r", runner=runner)._checks_for_head("head-177")
    assert checks[0]["bucket"] == "fail"
    assert "/actions/runs/4242" in checks[0]["link"]


# ── wedged-run cancel (keifu PR #177) ────────────────────────────────────────
def test_cancel_workflow_runs_cancels_the_runs_pinning_the_observed_head():
    fr = FakeRunner({
        "api repos/o/r/actions/runs/4242": {
            "id": 4242, "head_sha": "head-1", "status": "in_progress",
        },
        "api repos/o/r/actions/runs/4343": {
            "id": 4343, "head_sha": "head-1", "status": "queued",
        },
    })
    gw = GitHubGateway("o/r", runner=fr)

    assert gw.cancel_workflow_runs("head-1", (4242, 4343)) == (4242, 4343)
    assert [
        call[0][2] for call in fr.calls
        if call[0][1] == "api" and call[0][2].endswith("/cancel")
    ] == [
        "repos/o/r/actions/runs/4242/cancel",
        "repos/o/r/actions/runs/4343/cancel",
    ]


def test_cancel_workflow_runs_refuses_a_run_belonging_to_a_newer_push():
    # Cancelling is destructive: a run whose head_sha is not the commit the
    # decision looked at is someone else's CI, and a partial cancel would leave
    # the real pin in place while reading as handled.
    fr = FakeRunner({
        "api repos/o/r/actions/runs/4242": {
            "id": 4242, "head_sha": "head-1", "status": "in_progress",
        },
        "api repos/o/r/actions/runs/4343": {
            "id": 4343, "head_sha": "newer-head", "status": "in_progress",
        },
    })
    gw = GitHubGateway("o/r", runner=fr)

    assert gw.cancel_workflow_runs("head-1", (4242, 4343)) is None
    assert not any(call[0][2].endswith("/cancel") for call in fr.calls)


def test_cancel_workflow_runs_is_idempotent_over_an_already_completed_run():
    # Durable replay must not fail because GitHub already took the run down.
    fr = FakeRunner({
        "api repos/o/r/actions/runs/4242": {
            "id": 4242, "head_sha": "head-1", "status": "completed",
        },
    })
    gw = GitHubGateway("o/r", runner=fr)

    assert gw.cancel_workflow_runs("head-1", (4242,)) == ()
    assert not any(call[0][2].endswith("/cancel") for call in fr.calls)


def test_cancel_workflow_runs_rejects_unusable_run_identities():
    gw = GitHubGateway("o/r", runner=FakeRunner({}))
    assert gw.cancel_workflow_runs("", (4242,)) is None
    assert gw.cancel_workflow_runs("head-1", ()) is None
    assert gw.cancel_workflow_runs("head-1", (0,)) is None
    assert gw.cancel_workflow_runs("head-1", (-1,)) is None


def test_cancel_workflow_runs_rejects_a_malformed_or_statusless_answer():
    fr = FakeRunner({
        "api repos/o/r/actions/runs/4242": {"id": 4242, "head_sha": "head-1"},
    })
    assert GitHubGateway("o/r", runner=fr).cancel_workflow_runs(
        "head-1", (4242,)
    ) is None
    assert not any(call[0][2].endswith("/cancel") for call in fr.calls)


def test_cancel_workflow_runs_preserves_transport_failure_for_durable_recovery():
    gw = GitHubGateway(
        "o/r", runner=_raising_runner(GhUnavailable("server", "HTTP 503"))
    )
    with pytest.raises(GhUnavailable):
        gw.cancel_workflow_runs("head-1", (4242,))


def test_failed_log_uses_the_same_verified_attempt_as_the_rerun_path():
    fr = FakeRunner({
        "api repos/o/r/actions/runs/77": {"id": 77, "head_sha": "head", "run_attempt": 2},
        "run view 77": "boom",
    })
    gw = GitHubGateway("o/r", runner=fr)
    assert gw.workflow_run_attempts("head", (77,)) == {77: 2}
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


_ROLLUP_ERR = (
    "GraphQL: Resource not accessible by integration "
    "(node.statusCheckRollup.nodes.0.commit.statusCheckRollup)"
)






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
    the API, not outages. They must keep degrading to "" — `_safe_get_pr`,
    `build_review_world`'s missing-label tolerance and `submit_review`'s False
    all depend on it, so a blanket raise would wedge the daemon."""
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
    per-call case so `_safe_get_pr` can still isolate one wedged PR."""
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


def test_build_world_propagates_gh_unavailable():
    """A world built on a failed read is a lie. The builders must NOT degrade it
    to an empty WorldState — that is exactly how a throttled poll came to see
    zero issues and zero PRs."""
    gw = GitHubGateway("o/r", runner=_raising_runner(GhUnavailable("rate-limit", "exceeded")))
    with pytest.raises(GhUnavailable):
        gw.build_world()


@pytest.mark.parametrize(
    "build", ["build_review_world", "build_triage_world", "build_steward_world"]
)
def test_all_world_builders_propagate_gh_unavailable(build):
    gw = GitHubGateway("o/r", runner=_raising_runner(GhUnavailable("server", "HTTP 503")))
    with pytest.raises(GhUnavailable):
        getattr(gw, build)()


def test_review_world_isolates_a_timed_out_pr_but_not_a_rate_limit(capsys):
    """Two infra failures, two different blast radii. A single hung `gh pr view`
    recurs every poll while the PR exists, so isolating it keeps newer PRs
    reviewable (the #374/#382 incident). A rate limit fails EVERY fetch, so
    isolating it would rebuild the empty world V10 is about."""
    good = {"number": 900, "state": "OPEN", "labels": [{"name": "ai-review"}]}

    def runner(argv, token):
        key = " ".join(argv)
        if "pr list" in key:
            return json.dumps([{"number": 374}, {"number": 900}])
        if "pr view 374" in key:
            raise GhUnavailable("network", "gh timed out after 120s", timed_out=True)
        if "pr view 900" in key:
            return json.dumps(good)
        return ""

    gw = GitHubGateway("o/r", runner=runner, review_pr_label="ai-review")
    assert [p.number for p in gw.build_review_world().prs] == [900]
    assert "skip PR #374" in capsys.readouterr().err

    def throttled(argv, token):
        if "pr list" in " ".join(argv):
            return json.dumps([{"number": 374}, {"number": 900}])
        raise GhUnavailable("rate-limit", "API rate limit exceeded")

    gw2 = GitHubGateway("o/r", runner=throttled, review_pr_label="ai-review")
    with pytest.raises(GhUnavailable):
        gw2.build_review_world()


def test_has_open_blocker_failsoft_for_api_answers_but_not_for_infra():
    """"False" is not a neutral degradation here — it means "not blocked", which
    DISPATCHES the issue. Fine when the API genuinely answered (no dependencies
    endpoint); wrong when it answered nothing at all."""
    gw_ok = GitHubGateway("o/r", runner=_raising_runner(ValueError("odd shape")))
    assert gw_ok.has_open_blocker(5) is False
    gw_infra = GitHubGateway("o/r", runner=_raising_runner(GhUnavailable("rate-limit", "x")))
    with pytest.raises(GhUnavailable):
        gw_infra.has_open_blocker(5)


# ── review C2: every `gh … list` states its ceiling ───────────────────────────
def _limit_of(argv):
    return argv[argv.index("--limit") + 1] if "--limit" in argv else None


def test_list_issue_numbers_passes_limit_and_warns_on_truncation(capsys):
    """review C2. Without --limit gh answers with the 30 NEWEST issues and looks
    exactly like a complete answer, so past 30 ai-ready issues the OLDEST are
    never dispatched — every poll, no error. The cap must be stated, and hitting
    it must be loud."""
    fr = FakeRunner({"issue list": [{"number": n} for n in range(1, _LIST_LIMIT + 1)]})
    gw = GitHubGateway("o/r", runner=fr)
    nums = gw.list_issue_numbers("ai-ready")
    assert len(nums) == _LIST_LIMIT
    assert _limit_of(fr.calls[0][0]) == str(_LIST_LIMIT)
    err = capsys.readouterr().err
    assert "TRUNCATED" in err and "ai-ready" in err


def test_list_issue_numbers_no_warning_below_limit(capsys):
    fr = FakeRunner({"issue list": [{"number": 1}, {"number": 2}]})
    gw = GitHubGateway("o/r", runner=fr)
    assert gw.list_issue_numbers("ai-ready") == [1, 2]
    assert capsys.readouterr().err == ""


def test_list_pr_numbers_passes_limit_and_warns_on_truncation(capsys):
    """review C2, PR half: past 30 open ai-pr-open PRs the oldest vanish from the
    world, so their failing CI is never fixed and they never merge."""
    fr = FakeRunner({"pr list": [{"number": n} for n in range(1, _LIST_LIMIT + 1)]})
    gw = GitHubGateway("o/r", runner=fr)
    nums = gw.list_pr_numbers("ai-pr-open")
    assert len(nums) == _LIST_LIMIT
    assert _limit_of(fr.calls[0][0]) == str(_LIST_LIMIT)
    err = capsys.readouterr().err
    assert "TRUNCATED" in err and "ai-pr-open" in err


def test_list_pr_numbers_no_warning_below_limit(capsys):
    fr = FakeRunner({"pr list": [{"number": 1}]})
    gw = GitHubGateway("o/r", runner=fr)
    assert gw.list_pr_numbers("ai-pr-open") == [1]
    assert capsys.readouterr().err == ""


def test_pr_for_issue_passes_limit_and_warns_on_truncation(capsys):
    """review C2. pr_for_issue scans open PRs for a `fix/<issue>-…` head; capped
    at 30 it reports "no PR" for a PR that exists, and the fixer re-opens work
    that is already in flight."""
    prs = [{"number": n, "headRefName": f"feat/{n}-x"} for n in range(1, _LIST_LIMIT + 1)]
    fr = FakeRunner({"pr list": prs})
    gw = GitHubGateway("o/r", runner=fr)
    assert gw.pr_for_issue(_LIST_LIMIT) == _LIST_LIMIT   # the OLDEST-ranked entry still found
    assert _limit_of(fr.calls[0][0]) == str(_LIST_LIMIT)
    assert "TRUNCATED" in capsys.readouterr().err


def test_pr_for_issue_no_warning_below_limit(capsys):
    fr = FakeRunner({"pr list": [{"number": 100, "headRefName": "fix/5-null-token"}]})
    gw = GitHubGateway("o/r", runner=fr)
    assert gw.pr_for_issue(5) == 100
    assert gw.pr_for_issue(6) is None
    assert capsys.readouterr().err == ""


def test_ci_reset_linkage_selects_closing_refs_and_refuses_ambiguity():
    prs = [{"number": 100, "headRefName": "unrelated", "closingIssuesReferences": [{"number": 5}]}]
    gw = GitHubGateway("o/r", runner=FakeRunner({"pr list": prs}))
    assert gw.pr_for_issue(5, require_unique=True) == 100
    assert gw.pr_for_issue(5) is None
    prs.append({"number": 101, "headRefName": "fix/5-current", "closingIssuesReferences": []})
    gw = GitHubGateway("o/r", runner=FakeRunner({"pr list": prs}))
    with pytest.raises(ValueError, match="ambiguous"):
        gw.pr_for_issue(5, require_unique=True)
    assert gw.pr_for_issue(5) == 101


@pytest.mark.parametrize("prs", [
    None,
    [{"number": 100, "headRefName": "fix/5-work"}],
    [{"number": 100, "headRefName": "fix/5-work", "closingIssuesReferences": [None]}],
    *[[{"number": invalid, "headRefName": "other", "closingIssuesReferences": []}] for invalid in (True, 0, -1, 2**63)],
    *[[{"number": 100, "headRefName": "other", "closingIssuesReferences": [{"number": invalid}]}] for invalid in (True, -1, 2**63)],
    [{"number": n, "headRefName": f"fix/{n}-work", "closingIssuesReferences": []} for n in range(_LIST_LIMIT)],
])
def test_ci_reset_linkage_refuses_incomplete_observations(prs):
    gw = GitHubGateway("o/r", runner=FakeRunner({"pr list": prs}))
    with pytest.raises(ValueError, match="CI reset|linkage is unavailable"):
        gw.pr_for_issue(5, require_unique=True)


def test_pr_discovery_uses_the_shared_branch_issue_policy():
    from issue_agent.domain.refs import BRANCH_PREFIXES

    for prefix in BRANCH_PREFIXES:
        gw = GitHubGateway("o/r", runner=FakeRunner({"pr list": [
            {"number": 1, "headRefName": f"{prefix}/7030-other"},
            {"number": 2, "headRefName": f"{prefix}/703-work"},
        ]}))
        assert gw.pr_for_issue(703) == 2


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


@pytest.mark.parametrize(
    "build", ["build_world", "build_review_world", "build_triage_world", "build_steward_world"]
)
def test_every_world_builder_list_call_carries_a_limit(build):
    """review C2 sweep across all four builders — the observable contract, not the
    guard: no world is ever built from a silently-capped listing."""
    calls = []

    def runner(argv, token):
        calls.append(argv)
        return "[]"

    gw = GitHubGateway("o/r", runner=runner, review_adopt_unlabeled=True)
    getattr(gw, build)()
    lists = [a for a in calls if len(a) > 2 and a[2] == "list"]
    assert lists, f"{build} made no list call"
    for argv in lists:
        assert _limit_of(argv) is not None, f"{build}: {' '.join(argv)} has no --limit"


# ── review M19: one unreadable issue ≠ a dead poll ────────────────────────────
def _issue_world_runner(bad, *, label="ai-working", raises=None):
    """Two issues (#7, #8) carrying `label`; #7 is broken per `bad`/`raises`."""
    def runner(argv, token):
        key = " ".join(argv)
        if "issue list" in key:
            return json.dumps([{"number": 7}, {"number": 8}]) if label in key else "[]"
        if "pr list" in key:
            return "[]"
        if "issue view 7" in key:
            if raises is not None:
                raise raises
            return bad
        if "issue view 8" in key:
            return json.dumps({"number": 8, "state": "OPEN", "labels": [{"name": label}]})
        return ""
    return runner


@pytest.mark.parametrize("bad", ["", "{}"])
def test_build_world_survives_one_unreadable_issue(bad, capsys):
    """review M19: `gh issue view` on an issue that was transferred, converted to a
    discussion or locked answers empty/partial → parse_issue raises (AttributeError
    / KeyError) → the exception used to leave the world-build generator and kill the
    ENTIRE poll, every tick, permanently (the incident _safe_get_pr records, on the
    other item type). Only the bad issue may be dropped."""
    gw = GitHubGateway("o/r", runner=_issue_world_runner(bad))
    w = gw.build_world()
    assert [i.number for i in w.issues] == [8]
    assert "skip issue #7" in capsys.readouterr().err


def test_build_world_keeps_both_issues_when_both_are_readable(capsys):
    # anti-vacuity twin: the isolation above is not just dropping everything.
    gw = GitHubGateway(
        "o/r",
        runner=_issue_world_runner(json.dumps({"number": 7, "state": "OPEN", "labels": []})),
    )
    assert [i.number for i in gw.build_world().issues] == [7, 8]
    assert capsys.readouterr().err == ""


def test_build_world_isolates_a_timed_out_issue_but_not_a_rate_limit(capsys):
    """review M19, blast radius — the exact twin of the PR test above. A single
    wedged `gh issue view` recurs every poll while that issue exists, so isolating
    it keeps every other issue dispatchable. A rate limit fails EVERY fetch, so
    isolating it would rebuild the empty world V10 is about."""
    timed_out = GhUnavailable("network", "gh timed out after 120s", timed_out=True)
    gw = GitHubGateway("o/r", runner=_issue_world_runner(None, raises=timed_out))
    assert [i.number for i in gw.build_world().issues] == [8]
    assert "skip issue #7" in capsys.readouterr().err

    throttled = _issue_world_runner(
        None, raises=GhUnavailable("rate-limit", "API rate limit exceeded")
    )
    with pytest.raises(GhUnavailable):
        GitHubGateway("o/r", runner=throttled).build_world()


def test_build_world_isolates_a_wedged_blocker_read(capsys):
    """A wedged `gh api …/dependencies/blocked_by` neither kills the poll nor
    drops the issue: it marks the issue blocked with no witnesses (fail-closed
    for BOTH admission and the PR repair arm) until a clean tick re-derives
    the truth. Dropping used to be the safe direction when only
    pick_ready_issue consumed the flag; the repair arm fails OPEN for a
    missing issue, so a drop would have dispatched into the very wedge the
    blocker tracks (adversarial review F1, 2026-08-03)."""
    def runner(argv, token):
        key = " ".join(argv)
        if "issue list" in key:
            return json.dumps([{"number": 7}, {"number": 8}]) if "ai-ready" in key else "[]"
        if "pr list" in key:
            return "[]"
        if "issue view" in key:
            return json.dumps(
                {"number": int(argv[3]), "state": "OPEN", "labels": [{"name": "ai-ready"}]}
            )
        if "issues/7" in key:
            raise GhUnavailable("network", "gh timed out after 120s", timed_out=True)
        return "0"                      # #8: no declared blockers

    gw = GitHubGateway("o/r", runner=runner)
    world = gw.build_world()
    assert [i.number for i in world.issues] == [7, 8]
    seven = world.issues[0]
    assert (seven.blocked_by_open, seven.open_blocker_numbers) == (True, ())
    assert world.issues[1].blocked_by_open is False
    assert "skip issue-blockers #7" in capsys.readouterr().err


@pytest.mark.parametrize("build", ["build_triage_world", "build_steward_world"])
def test_steward_issue_worlds_survive_one_unreadable_issue(build, capsys):
    # review M19: the steward/triage polls read EVERY open issue, so they are the
    # most likely to meet the one bad one.
    def runner(argv, token):
        key = " ".join(argv)
        if "issue list" in key and "--state open" in key:
            return json.dumps([{"number": 7}, {"number": 8}])
        if "issue list" in key:
            return "[]"
        if "issue view 7" in key:
            return "{}"                                   # KeyError in parse_issue
        if "issue view 8" in key:
            return json.dumps({"number": 8, "state": "OPEN"})
        return ""

    gw = GitHubGateway("o/r", runner=runner)
    w = getattr(gw, build)()
    assert [i.number for i in w.issues] == [8]
    assert "skip issue #7" in capsys.readouterr().err


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


def test_change_signature_is_one_rest_point_and_fails_soft():
    """The probe read: a single REST issues?per_page=1 call (issues list includes
    PRs), returning number@updated_at — and "" (assume-changed) when gh answered
    nothing, because a broken probe must degrade to building, never to sleeping."""
    fr = FakeRunner({"api repos/o/r/issues?state=all&sort=updated": "531@2026-07-27T00:00:00Z\n"})
    gw = GitHubGateway("o/r", Identity(static_token="t"), runner=fr)
    assert gw.change_signature() == "531@2026-07-27T00:00:00Z"
    argv, token = fr.calls[0]
    assert argv[1] == "api" and "per_page=1" in argv[2] and "sort=updated" in argv[2]
    assert token == "t"

    assert GitHubGateway("o/r", Identity(), runner=FakeRunner()).change_signature() == ""




# ── recurrence context (harness #10) ─────────────────────────────────────────
def test_recently_merged_issues_reports_one_entry_per_closed_issue():
    """One MergedRef per REFERENCED ISSUE, carrying the issue's own number and
    title (a recurrence matches a new report against old REPORTS, not against the
    PR titles that resolved them)."""
    fr = FakeRunner({"pr list": [
        {"number": 300, "title": "fix: null token", "mergedAt": "2026-07-20T10:00:00Z",
         "closingIssuesReferences": [{"number": 42, "title": "Login crashes on null token"},
                                     {"number": 43, "title": "Session drops"}]},
        {"number": 301, "title": "chore: bump", "mergedAt": "2026-07-21T10:00:00Z",
         "closingIssuesReferences": []},
    ]})
    gw = GitHubGateway("o/r", Identity(static_token="t"), runner=fr)

    merged = gw.recently_merged_issues("fix-bot", limit=30)

    assert [(m.issue, m.pr, m.title) for m in merged] == [
        (42, 300, "Login crashes on null token"),
        (43, 300, "Session drops"),
    ]
    assert merged[0].merged_at == "2026-07-20T10:00:00Z"
    argv = [a for a, _ in fr.calls][0]
    assert "--author" in argv and "fix-bot" in argv
    assert argv[argv.index("--limit") + 1] == "30"
    assert "merged" in argv


def test_recently_merged_issues_fails_soft():
    """Advisory context may never kill the leg it enriches: an unreadable list,
    garbage JSON, or no author at all is simply "no reference set"."""
    def boom(argv, token):
        raise RuntimeError("gh boom")

    assert GitHubGateway("o/r", runner=boom).recently_merged_issues("fix-bot") == ()
    assert GitHubGateway(
        "o/r", runner=lambda a, t: "not json"
    ).recently_merged_issues("fix-bot") == ()
    # no author → no reference set, and NO gh call at all
    calls = []
    gw = GitHubGateway("o/r", runner=lambda a, t: calls.append(a) or "[]")
    assert gw.recently_merged_issues("") == ()
    assert calls == []


def test_issue_state_reason_normalises_the_graphql_enum():
    fr = FakeRunner({"issue view 522": {"stateReason": "REOPENED"}})
    gw = GitHubGateway("o/r", Identity(static_token="t"), runner=fr)
    assert gw.issue_state_reason(522) == "reopened"


def test_issue_state_reason_fails_soft_to_no_evidence():
    """"" means "no evidence of a recurrence" — the silent, correct default. A
    completed close must never read as a reopen."""
    fr = FakeRunner({"issue view 522": {"stateReason": "COMPLETED"}})
    assert GitHubGateway("o/r", runner=fr).issue_state_reason(522) == "completed"

    def boom(argv, token):
        raise RuntimeError("gh boom")

    assert GitHubGateway("o/r", runner=boom).issue_state_reason(522) == ""
    assert GitHubGateway("o/r", runner=lambda a, t: "").issue_state_reason(522) == ""
    assert GitHubGateway("o/r", runner=lambda a, t: "null").issue_state_reason(522) == ""


def test_merged_prs_in_the_world_get_the_cheap_fetch():
    """The --state all label list serves STALE search results (merged PRs
    unlabeled a day earlier still listed), and each stale entry was paying the
    hang-class mergeable fetch + a checks call EVERY tick. A non-OPEN PR must
    cost one plain view: no mergeable fields, no `gh pr checks`."""
    pr_json = {"number": 540, "title": "t", "body": "", "headRefName": "fix/9-x",
               "headRefOid": "", "baseRefName": "dev", "labels": [], "reviews": [],
               "state": "MERGED", "closingIssuesReferences": [], "files": [],
               "isDraft": False, "author": {"login": "b"}}
    open_json = {**pr_json, "number": 541, "state": "OPEN",
                 "labels": [{"name": "ai-pr-open"}]}
    fr = FakeRunner({
        "pr list --repo": [{"number": 540, "state": "MERGED"},
                           {"number": 541, "state": "OPEN"}],
        "pr view 540": pr_json,
        "pr view 541": open_json,
        "pr checks 541": [],
        "issue list --repo": [],
    })
    gw = GitHubGateway("o/r", Identity(static_token="t"), runner=fr)
    world = gw.build_world()
    assert {p.number for p in world.prs} == {540, 541}

    view_540 = [a for a, _ in fr.calls if a[2:4] == ["view", "540"]]
    view_541 = [a for a, _ in fr.calls if a[2:4] == ["view", "541"]]
    assert view_540 and "mergeable" not in view_540[0][-1]   # cheap fields only
    assert view_541 and "mergeable" in view_541[0][-1]       # open PR: full
    assert not any(
        "/commits/head-540/check-runs" in " ".join(a)
        for a, _ in fr.calls
    )
    assert any(
        "/branches/" in " ".join(a)
        for a, _ in fr.calls
    )


# ── check start times + world timestamp (#30) ────────────────────────────────
def test_build_world_stamps_the_poll_time():
    # The domain may not read a clock, so the world it decides on carries one.
    gw = GitHubGateway("o/r", runner=FakeRunner({"issue list": [], "pr list": []}))
    assert parse_timestamp(gw.build_world().now) is not None


def test_parse_world_is_unstamped_unless_told():
    assert parse_world([], []).now == ""


# ── merge cleanup end-to-end: merge → GitHub auto-close → next poll (#28) ─────
# These drive the REAL world build, because the defect lived in what the build
# FETCHES, not in what the domain does with it. Every previous cleanup test
# handed `cleanup_actions` an `issue_observed_pr_open=True` that the post-merge
# loop never supplies, so all of them passed while zero issues in five
# deployment days ever reached ai-done.

def _gh_repo(issues, prs):
    """A `gh` serving a whole described repo, list calls and view calls agreeing.

    `issues`: {number: {"state", "labels", "stateReason"}} — served from whichever
    `--label/--state` slice genuinely matches, so a test cannot smuggle an issue
    into a world the real builder would not have fetched.
    `prs`: {number: {"state", "labels", "issue", "listed"}} — `listed` is
    deliberately independent of `labels`, because that is the live GitHub
    behaviour this whole bug turns on: `gh pr list --label ai-pr-open --state all`
    keeps serving merged PRs for about a day AFTER their label was removed (stale
    search index), and only the per-PR view tells the truth."""
    def runner(argv, token):
        key = " ".join(argv)
        if "issue list" in key:
            label = argv[argv.index("--label") + 1]
            state = argv[argv.index("--state") + 1]
            return json.dumps([
                {"number": n} for n, i in sorted(issues.items())
                if label in i["labels"]
                and (state == "all" or state == i["state"].lower())
            ])
        if "pr list" in key:
            state = argv[argv.index("--state") + 1]
            return json.dumps([
                {"number": n, "state": p["state"]} for n, p in sorted(prs.items())
                if p.get("listed", True)
                and (state == "all" or state.upper() == p["state"])
            ])
        if "issue view" in key:
            n = int(argv[3])
            i = issues[n]
            return json.dumps({
                "number": n, "title": "t", "body": "", "state": i["state"],
                "stateReason": i.get("stateReason", ""),
                "labels": [{"name": x} for x in i["labels"]],
            })
        if "pr view" in key:
            n = int(argv[3])
            p = prs[n]
            return json.dumps({
                "number": n, "title": "t", "body": f"Fixes #{p['issue']}",
                "state": p["state"], "headRefName": f"fix/{p['issue']}-thing",
                "headRefOid": "abc", "baseRefName": "dev",
                "labels": [{"name": x} for x in p["labels"]],
                "reviews": [], "files": [], "isDraft": False,
                "author": {"login": "fix-bot"},
                "closingIssuesReferences": [{"number": p["issue"]}],
            })
        return ""

    return runner


_CFG = FixConfig(fix_bot="fix-bot")


def _decide(runner):
    observed = GitHubGateway("o/r", runner=runner).build_world()
    return decide_fix(
        observed,
        _CFG, FixState(resolve_workflows={i.number: resolve_snapshot(i.number) for i in observed.issues}),
        fixer_busy=False,
    )


def test_merge_cleanup_runs_on_the_poll_after_github_auto_closed_the_issue():
    """The invariant nothing covered: after a merge the issue ends up ai-done and
    its worktree is gone. `Fixes #n` is mandatory (check-pr.sh), so GitHub closes
    the issue at the instant of merge; the very next poll must still observe it."""
    runner = _gh_repo(
        issues={8: {"state": "CLOSED", "labels": ["ai-pr-open"]}},
        prs={50: {"state": "MERGED", "labels": ["ai-pr-open"], "issue": 8}},
    )
    world = GitHubGateway("o/r", runner=runner).build_world()
    assert 8 in {i.number for i in world.issues}, "closed ai-pr-open issue must be observable"

    actions = decide_fix(world, _CFG, FixState(resolve_workflows={8: resolve_snapshot(8)}), fixer_busy=False)
    assert Relabel(Target.ISSUE, 8, Label.AI_PR_OPEN, Label.AI_DONE, workflow=resolve_snapshot(8).binding) in actions
    assert RemoveWorktree(8) in actions
    assert ClearIssueState(pr=50, issue=8, merged=True) in actions
    # GitHub already closed it — re-closing would re-comment "Resolved by #50".
    assert not any(isinstance(a, CloseIssue) for a in actions)


def test_backlog_issue_whose_pr_lost_its_label_still_gets_cleaned_up():
    """The shape the deployed backlog is ACTUALLY in, verified live: the
    unconditional PR-side relabel already ran at merge+1, so the merged PR carries
    no labels and `needs_cleanup` is False for it forever. Only the issue-driven
    arm can reach these — without it the widened query would observe the same
    stuck issues every poll and never drain them."""
    actions = _decide(_gh_repo(
        issues={8: {"state": "CLOSED", "labels": ["ai-pr-open"]}},
        # still in the stale search index, but its real label set is empty
        prs={50: {"state": "MERGED", "labels": [], "issue": 8}},
    ))
    assert Relabel(Target.ISSUE, 8, Label.AI_PR_OPEN, Label.AI_DONE, workflow=resolve_snapshot(8).binding) in actions
    assert RemoveWorktree(8) in actions
    assert ClearIssueState(pr=None, issue=8, merged=True) in actions
    assert not any(isinstance(a, CloseIssue) for a in actions)


def test_backlog_issue_is_cleaned_up_even_when_the_pr_has_left_the_world():
    # …and once the stale index expires the PR is not in `world.prs` at all.
    actions = _decide(_gh_repo(
        issues={8: {"state": "CLOSED", "labels": ["ai-pr-open"]}}, prs={},
    ))
    assert Relabel(Target.ISSUE, 8, Label.AI_PR_OPEN, Label.AI_DONE, workflow=resolve_snapshot(8).binding) in actions
    assert RemoveWorktree(8) in actions


def test_stranded_issue_repairs_itself_on_the_next_tick():
    """Ordering/stranding: `cleanup_actions` emits the PR-side relabel
    UNCONDITIONALLY, and the executor applies each action in its own try/except,
    so a transient gh failure on the issue relabel at tick N leaves the issue
    ai-pr-open with its only handle — the PR label — already gone. Tick N+1 is
    exactly the backlog shape above, so the pipeline now has a retry loop it never
    had."""
    tick_n_plus_1 = _gh_repo(
        issues={8: {"state": "CLOSED", "labels": ["ai-pr-open"]}},
        prs={50: {"state": "MERGED", "labels": [], "issue": 8}},
    )
    actions = _decide(tick_n_plus_1)
    assert Relabel(Target.ISSUE, 8, Label.AI_PR_OPEN, Label.AI_DONE, workflow=resolve_snapshot(8).binding) in actions

    # …and it is ONE-SHOT: the repair's own relabel removes ai-pr-open, which
    # drops the issue out of both queries that feed the arm.
    after = _decide(_gh_repo(
        issues={8: {"state": "CLOSED", "labels": ["ai-done"]}},
        prs={50: {"state": "MERGED", "labels": [], "issue": 8}},
    ))
    assert not any(isinstance(a, RemoveWorktree) for a in after)
    assert not any(isinstance(a, ClearIssueState) for a in after)


def test_an_open_ai_pr_open_issue_that_was_never_closed_is_left_alone():
    """The one ambiguous shape, deliberately not acted on: indistinguishable from
    a healthy in-flight issue whose PR was dropped from this world by a transient
    read failure. Acting would delete a live session's worktree."""
    actions = _decide(_gh_repo(
        issues={8: {"state": "OPEN", "labels": ["ai-pr-open"]}}, prs={},
    ))
    assert actions == []


def test_a_human_reopen_is_relabelled_ai_done_and_never_re_closed():
    """#10's recurrence signal is `open + ai-done + stateReason reopened`. An
    operator reopening a stuck issue must survive the poll that repairs it —
    ai-done is what MAKES the reopen legible to the steward, and re-closing it
    within one poll would erase the truest guardrail the harness has."""
    reopened = {"state": "OPEN", "labels": ["ai-pr-open"], "stateReason": "REOPENED"}
    # (a) via the issue-driven arm — the PR is gone
    actions = _decide(_gh_repo(issues={8: reopened}, prs={}))
    assert Relabel(Target.ISSUE, 8, Label.AI_PR_OPEN, Label.AI_DONE, workflow=resolve_snapshot(8).binding) in actions
    assert not any(isinstance(a, CloseIssue) for a in actions)
    # (b) via the PR-driven arm — the merged PR is still labelled, so without the
    # stateReason guard `close_issue_on_merge` would close the reopen outright.
    actions = _decide(_gh_repo(
        issues={8: reopened},
        prs={50: {"state": "MERGED", "labels": ["ai-pr-open"], "issue": 8}},
    ))
    assert Relabel(Target.ISSUE, 8, Label.AI_PR_OPEN, Label.AI_DONE, workflow=resolve_snapshot(8).binding) in actions
    assert not any(isinstance(a, CloseIssue) for a in actions)


def test_an_in_flight_issue_is_never_touched_by_the_issue_driven_arm():
    # Anti-vacuity for `covered`: the ordinary mid-flight shape is an OPEN
    # ai-pr-open issue with an OPEN ai-pr-open PR. Nothing may fire on it.
    actions = _decide(_gh_repo(
        issues={8: {"state": "OPEN", "labels": ["ai-pr-open"]}},
        prs={50: {"state": "OPEN", "labels": ["ai-pr-open"], "issue": 8}},
    ))
    assert not any(isinstance(a, RemoveWorktree) for a in actions)
    assert not any(isinstance(a, ClearIssueState) for a in actions)
    assert not any(
        isinstance(a, Relabel) and a.target is Target.ISSUE for a in actions
    )


def test_widened_world_heals_the_backlog_without_stomping_a_fresh_dispatch():
    """P1R-15 in the world the widening actually produces. #8 was just dispatched
    (a dispatch MOVES the label ai-ready → ai-working, so it carries ai-working and
    NOT ai-pr-open) while its own stale MERGED PR is still labelled; #9 is a
    backlog issue being healed in the same tick. Both halves matter: drop the
    widening and #9's cleanup vanishes; drop the gate and #8's live worktree is
    deleted."""
    actions = _decide(_gh_repo(
        issues={
            8: {"state": "OPEN", "labels": ["ai-working"]},
            9: {"state": "CLOSED", "labels": ["ai-pr-open"]},
        },
        prs={50: {"state": "MERGED", "labels": ["ai-pr-open"], "issue": 8}},
    ))
    # the backlog issue is healed — this is what the widening buys
    assert Relabel(Target.ISSUE, 9, Label.AI_PR_OPEN, Label.AI_DONE, workflow=resolve_snapshot(9).binding) in actions
    assert RemoveWorktree(9) in actions
    # …and the freshly-dispatched issue is untouched
    assert RemoveWorktree(8) not in actions
    assert Relabel(Target.ISSUE, 8, Label.AI_PR_OPEN, Label.AI_DONE, workflow=resolve_snapshot(8).binding) not in actions
    assert not any(isinstance(a, ClearIssueState) and a.issue == 8 for a in actions)
    # …while the PR-side arm, which is never stale, still fires.
    assert Relabel(Target.PR, 50, Label.AI_PR_OPEN, None) in actions


def test_closed_ai_pr_open_slice_is_bounded_below_the_list_limit():
    """A repo with a backlog deeper than the cap must not turn one poll into
    hundreds of `gh issue view` calls; it is worked off newest-first instead."""
    fr = FakeRunner({"issue list": [], "pr list": []})
    GitHubGateway("o/r", runner=fr).build_world()
    closed = [
        a for a, _ in fr.calls
        if a[1:3] == ["issue", "list"] and "closed" in a and "ai-pr-open" in a
    ]
    assert len(closed) == 1
    assert closed[0][closed[0].index("--limit") + 1] == str(_CLOSED_CLEANUP_LIMIT)
    assert _CLOSED_CLEANUP_LIMIT < _LIST_LIMIT


def test_closed_slice_truncation_is_loud_at_its_own_cap(capsys):
    """The truncation guard must compare against the cap the CALL used, not
    _LIST_LIMIT — otherwise a fully-saturated backlog slice is silent."""
    def runner(argv, token):
        key = " ".join(argv)
        if "issue list" in key and "--state closed" in key:
            return json.dumps([{"number": n} for n in range(_CLOSED_CLEANUP_LIMIT)])
        if "issue list" in key or "pr list" in key:
            return "[]"
        return json.dumps({"number": 1, "state": "CLOSED", "labels": []})

    GitHubGateway("o/r", runner=runner).build_world()
    assert "TRUNCATED" in capsys.readouterr().err


# ── #48: a failed `gh pr checks` is UNKNOWN, not an empty world ──────────────






def test_get_pr_marks_checks_unknown_when_the_read_fails():
    """The hole (#48): a swallowed non-infra CI read failure used to
    rebuild the PR with checks=() — indistinguishable from a no-CI repo, which
    decide_fix treats as mergeable. The PR must instead say the checks were
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


def test_a_list_built_terminal_pr_decides_exactly_like_the_view_built_one():
    """The guardrail. The `gh pr view` payload is strictly RICHER than the list
    row (body/comments/reviews/files), so this compares the cheap view against a
    realistic full one and pins field-by-field which facts must be identical —
    every one a lifecycle decision reads off a non-OPEN PR — and which are
    honestly marked unread rather than defaulted to empty."""
    view_payload = {
        **_TERMINAL_ROW,
        "body": "Fixes #12",
        "comments": [{"author": {"login": "reviewer"}, "body": "lgtm"}],
        "reviews": [{"author": {"login": "reviewer"}, "state": "APPROVED"}],
        "files": [{"path": "src/thing.py"}],
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
    }
    cheap = parse_terminal_pr(_TERMINAL_ROW, label="ai-pr-open")
    full = parse_pr(view_payload)
    decision_fields = (
        "number", "state", "labels", "closing_issue", "head_ref_name",
        "head_ref_oid", "base_ref_name", "base_ref_oid", "title", "is_draft",
        "author",
    )
    assert [getattr(cheap, f) for f in decision_fields] == [
        getattr(full, f) for f in decision_fields
    ]
    assert (cheap.number, cheap.state, cheap.closing_issue) == (60, "MERGED", 12)
    assert cheap.labels == frozenset({Label.AI_DONE})
    # UNREAD, never read-as-empty (#48): both flags say so, and the values the
    # richer payload would have carried are absent rather than wrong.
    assert isinstance(cheap.ci, Unavailable) and not cheap.content_known
    assert (cheap.body, cheap.comments, cheap.reviews, cheap.changed_files) == (
        "", (), (), frozenset()
    )
    assert full.content_known is True


def test_a_terminal_pr_still_wearing_the_label_keeps_its_full_fetch():
    """A PR that can still DRIVE cleanup is decided on authoritative labels: the
    search index is stale in the "still listed" direction, so a row showing the
    label cannot distinguish fresh from stale."""
    row = {**_TERMINAL_ROW, "labels": [{"name": "ai-pr-open"}]}
    assert parse_terminal_pr(row, label="ai-pr-open") is None


def test_an_open_or_unknown_state_keeps_its_full_fetch():
    assert parse_terminal_pr({**_TERMINAL_ROW, "state": "OPEN"}, label="x") is None
    assert parse_terminal_pr({**_TERMINAL_ROW, "state": ""}, label="x") is None
    assert parse_terminal_pr({**_TERMINAL_ROW, "state": None}, label="x") is None


def test_a_malformed_or_partial_row_keeps_its_full_fetch():
    """Never launder partial data into a trusted view."""
    for key in _TERMINAL_ROW:
        partial = {k: v for k, v in _TERMINAL_ROW.items() if k != key}
        assert parse_terminal_pr(partial, label="ai-pr-open") is None, key
    assert parse_terminal_pr({**_TERMINAL_ROW, "labels": "ai-done"}, label="x") is None
    assert parse_terminal_pr(
        {**_TERMINAL_ROW, "closingIssuesReferences": {}}, label="x"
    ) is None
    assert parse_terminal_pr({**_TERMINAL_ROW, "number": "sixty"}, label="x") is None
    assert parse_terminal_pr(None, label="x") is None
    assert parse_terminal_pr([_TERMINAL_ROW], label="x") is None


def test_a_hostile_row_can_never_raise_out_of_a_world_build():
    """The real contract: a world build has no per-item isolation around this
    call, so an oddly-shaped row must degrade to "fetch it properly" — never to
    a dead poll (the V10 class)."""
    hostile = [
        {**_TERMINAL_ROW, "author": "fix-bot"},              # str, not {login}
        {**_TERMINAL_ROW, "author": None},
        {**_TERMINAL_ROW, "labels": [None, 7, {"nope": 1}]},
        {**_TERMINAL_ROW, "closingIssuesReferences": [{"number": "twelve"}]},
        {**_TERMINAL_ROW, "headRefName": None, "title": None},
        {**_TERMINAL_ROW, "isDraft": {"weird": True}},
        {**_TERMINAL_ROW, "number": None},
        {**_TERMINAL_ROW, "state": ["MERGED"]},
    ]
    for row in hostile:
        parse_terminal_pr(row, label="ai-pr-open")  # must not raise


def test_a_bare_string_label_list_still_blocks_the_cheap_path():
    """The gate and the view parser must never disagree about "carries the
    label": reading a bare-string list as "no labels" would take the cheap path
    for a PR whose PRView then DOES carry the trigger label."""
    row = {**_TERMINAL_ROW, "labels": ["ai-pr-open"]}
    assert parse_terminal_pr(row, label="ai-pr-open") is None
    # …and an operator-configured trigger label the Label enum never heard of.
    rogue = {**_TERMINAL_ROW, "labels": ["team-review"]}
    assert parse_terminal_pr(rogue, label="team-review") is None
    assert parse_terminal_pr(rogue, label="ai-pr-open") is not None


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


def test_build_world_spends_no_pr_view_on_the_stale_unlabelled_tail():
    calls = []
    gw = GitHubGateway("o/r", runner=_tail_runner([_TERMINAL_ROW], calls=calls))
    prs = gw.build_world().prs
    assert [(p.number, p.state) for p in prs] == [(60, "MERGED")]
    assert not [c for c in calls if "pr view" in c]


def test_build_review_world_spends_no_pr_view_on_the_stale_unlabelled_tail():
    calls = []
    gw = GitHubGateway(
        "o/r", runner=_tail_runner([_TERMINAL_ROW], calls=calls),
        review_pr_label="ai-review",
    )
    prs = gw.build_review_world().prs
    assert [(p.number, p.state) for p in prs] == [(60, "MERGED")]
    assert not [c for c in calls if "pr view" in c]


def test_the_stale_tail_still_leaves_the_orphaned_cleanup_arm_free_to_repair():
    """#28 class guardrail: the ISSUE-driven repair fires precisely because the
    unlabelled merged PR does not "cover" its issue. Building that PR from the
    list payload must not change which issues are covered."""
    from issue_agent.domain.fix import orphaned_cleanup_actions

    def runner(argv, token):
        key = " ".join(argv)
        if "issue list" in key:
            return json.dumps([{"number": 12}])
        if "pr list" in key:
            return json.dumps([_TERMINAL_ROW])
        if "issue view 12" in key:
            return json.dumps({
                "number": 12, "state": "CLOSED",
                "labels": [{"name": "ai-pr-open"}],
            })
        return ""

    world = GitHubGateway("o/r", runner=runner).build_world()
    assert [type(a).__name__ for a in orphaned_cleanup_actions(world)] == [
        "RemoveWorktree", "Relabel", "ClearIssueState",
    ]


def test_a_terminal_pr_still_wearing_the_label_is_fetched_and_drives_cleanup():
    calls = []
    row = {**_TERMINAL_ROW, "labels": [{"name": "ai-pr-open"}]}
    gw = GitHubGateway(
        "o/r",
        runner=_tail_runner(
            [row],
            views={60: {"number": 60, "state": "MERGED",
                        "labels": [{"name": "ai-pr-open"}]}},
            calls=calls,
        ),
    )
    prs = gw.build_world().prs
    assert [(p.number, p.state, Label.AI_PR_OPEN in p.labels) for p in prs] == [
        (60, "MERGED", True)
    ]
    assert [c for c in calls if "pr view 60" in c]


# ── #163: the fixer's blind-spot reading (CI + mergeability over REST) ────────
def _signal_runner(pull, checks=(), statuses=()):
    def runner(argv, token):
        key = " ".join(argv)
        if "/pulls/" in key:
            return pull
        if "/check-runs" in key:
            return json.dumps([{"check_runs": list(checks)}])
        if "/statuses" in key:
            return json.dumps([list(statuses)])
        return ""

    return runner


def _per_head_runner(per_head):
    """A runner that serves a DIFFERENT pull object and check set per PR number,
    so a reading is pinned to the head it was taken for."""
    last = {"number": None}

    def runner(argv, token):
        key = " ".join(argv)
        for number in per_head:
            if f"/pulls/{number}" in key:
                last["number"] = number
                return per_head[number]["pull"]
        if "/check-runs" in key:
            return json.dumps(
                [{"check_runs": per_head[last["number"]].get("checks", [])}]
            )
        if "/statuses" in key:
            return json.dumps([[]])
        return ""

    return runner


def test_head_signals_reads_ci_and_mergeability_for_each_head():
    from issue_agent.domain.world import HeadRef

    gw = GitHubGateway(
        "o/r",
        runner=_per_head_runner({
            7: {
                "pull": "OPEN\tfalse\ttrue\tclean\tsha7",
                "checks": [
                    {"name": "ci", "status": "completed", "conclusion": "success"}
                ],
            },
            9: {
                "pull": "OPEN\tfalse\tfalse\tdirty\tsha9",
                "checks": [{"name": "ci", "status": "queued"}],
            },
        }),
    )
    signals = gw.head_signals((HeadRef(7), HeadRef(9)))
    assert signals is not None
    assert [(s.number, s.head_ref_oid) for s in signals] == [(7, "sha7"), (9, "sha9")]
    assert signals[0].mergeable != signals[1].mergeable
    assert signals[0].ci != signals[1].ci
    assert [s.failing for s in signals] == [False, False]


def test_one_unreadable_head_makes_the_whole_reading_unread():
    """Partial coverage would let the predicate gate on a head it never saw."""
    from issue_agent.domain.world import HeadRef

    gw = GitHubGateway(
        "o/r",
        runner=_per_head_runner({
            7: {"pull": "OPEN\tfalse\ttrue\tclean\tsha7"},
            9: {"pull": "garbage"},
        }),
    )
    assert gw.head_signals((HeadRef(7), HeadRef(9))) is None


def test_a_rerun_that_lands_on_the_same_bucket_still_moves_the_reading():
    """`startedAt` is the only field that separates a re-run from the original
    when name/state/bucket are identical."""
    from issue_agent.domain.world import HeadRef

    def gw_for(started):
        return GitHubGateway(
            "o/r",
            runner=_signal_runner(
                "OPEN\tfalse\ttrue\tclean\tsha9",
                checks=[{
                    "name": "ci", "status": "completed", "conclusion": "success",
                    "started_at": started,
                }],
            ),
        )

    first = gw_for("2026-08-09T10:00:00Z").head_signals((HeadRef(7),))
    rerun = gw_for("2026-08-09T11:00:00Z").head_signals((HeadRef(7),))
    assert first != rerun


def test_a_timed_out_head_is_unread_and_does_not_kill_the_tick():
    """One wedged `gh api` must not skip EVERY fix tick for as long as it stays
    wedged — the read lives outside the build's per-PR isolation now."""
    from issue_agent.domain.world import HeadRef

    def wedged(argv, token):
        raise GhUnavailable("network", "timeout", timed_out=True)

    assert GitHubGateway("o/r", runner=wedged).head_signals((HeadRef(7),)) is None


def test_a_systemic_gh_failure_still_propagates_to_the_skip_tick_arm():
    """Anti-regression twin: a rate limit is not one head's problem."""
    from issue_agent.domain.world import HeadRef

    def throttled(argv, token):
        raise GhUnavailable("rate-limit", "secondary limit")

    with pytest.raises(GhUnavailable):
        GitHubGateway("o/r", runner=throttled).head_signals((HeadRef(7),))


def test_head_signals_flags_a_failing_check_so_the_tick_rebuilds():
    from issue_agent.domain.world import HeadRef

    gw = GitHubGateway(
        "o/r",
        runner=_signal_runner(
            "OPEN\tfalse\ttrue\tunstable\tsha9",
            checks=[{"name": "ci", "status": "completed", "conclusion": "failure"}],
        ),
    )
    assert gw.head_signals((HeadRef(7),))[0].failing is True


def test_head_signals_are_none_when_a_head_is_unreadable():
    from issue_agent.domain.world import HeadRef

    short = GitHubGateway("o/r", runner=_signal_runner("OPEN\ttrue"))
    assert short.head_signals((HeadRef(7),)) is None
    no_sha = GitHubGateway("o/r", runner=_signal_runner("OPEN\tfalse\ttrue\tclean\t"))
    assert no_sha.head_signals((HeadRef(7),)) is None

    def broken_checks(argv, token):
        if "/pulls/" in " ".join(argv):
            return "OPEN\tfalse\ttrue\tclean\tsha9"
        return "not json at all"

    assert GitHubGateway("o/r", runner=broken_checks).head_signals(
        (HeadRef(7),)
    ) is None


def test_head_signals_spend_nothing_without_heads():
    calls = []

    def runner(argv, token):
        calls.append(argv)
        return ""

    assert GitHubGateway("o/r", runner=runner).head_signals(()) == ()
    assert calls == []
