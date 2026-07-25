"""GitHubGateway — pure JSON→WorldState parsers + identity + write-call shapes.

The parsers are the real-vs-fake seam, so they are asserted directly against the
documented gh --json shapes. The gateway is driven with a fake runner that
records argv and returns canned JSON (no network, no real gh)."""

import json

from issue_agent.adapters.github import (
    _FAILED_LOG_BYTES,
    _FAILED_LOG_LINES,
    _INLINE_COMMENT_LIMIT,
    _LIST_LIMIT,
    _REVIEW_COMMENT_BODY_BYTES,
    _REVIEW_COMMENTS_TOTAL_BYTES,
    GitHubGateway,
    Identity,
    MissingListLimit,
    format_review_comments,
    build_add_blocked_by_args,
    build_blocker_id_args,
    build_close_issue_args,
    build_comment_args,
    build_enable_auto_merge_args,
    build_relabel_args,
    build_submit_review_args,
    parse_issue,
    parse_pr,
    parse_world,
)
from issue_agent.domain.labels import Label
from issue_agent.domain.world import CIState
from issue_agent.domain.ci import ci_state


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
    assert pr.head_ref_oid == "abc123"
    assert pr.closing_issue == 5
    assert pr.reviews[0].author == "review-bot"
    assert pr.reviews[0].state == "APPROVED"
    # GraphQL shape (`gh pr view --json reviews`) nests the commit under commit.oid.
    assert pr.reviews[0].commit_id == "sha-graphql"


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
    # parsed checks compose with the domain ci_state (pending beats failure).
    assert ci_state(pr.checks) is CIState.PENDING


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


# ── gateway with a fake runner ────────────────────────────────────────────────
class FakeRunner:
    def __init__(self, responses=None):
        self.calls = []          # list of (argv, token)
        self.responses = responses or {}

    def __call__(self, argv, token):
        self.calls.append((argv, token))
        # key responses by the subcommand pair (argv[1], argv[2])
        key = " ".join(argv[1:4])
        for k, v in self.responses.items():
            if key.startswith(k):
                return v if isinstance(v, str) else json.dumps(v)
        return ""


def test_write_command_builders_are_pure_contracts():
    assert build_submit_review_args("o/r", 100, "APPROVE", "lgtm") == [
        "api", "repos/o/r/pulls/100/reviews", "--method", "POST",
        "-f", "event=APPROVE", "-f", "body=lgtm",
    ]
    assert build_relabel_args("o/r", "issue", 5, "ai-ready", "ai-working") == [
        "issue", "edit", "5", "--repo", "o/r",
        "--remove-label", "ai-ready", "--add-label", "ai-working",
    ]
    assert build_relabel_args("o/r", "issue", 5, None, None) is None
    assert build_comment_args("o/r", "pr", 100, "hi") == [
        "pr", "comment", "100", "--repo", "o/r", "--body", "hi",
    ]
    assert build_close_issue_args("o/r", 5, "completed", "done") == [
        "issue", "close", "5", "--repo", "o/r", "--reason", "completed", "--comment", "done",
    ]
    assert build_close_issue_args("o/r", 5, "not_planned", "") == [
        "issue", "close", "5", "--repo", "o/r", "--reason", "not_planned",
    ]
    assert build_enable_auto_merge_args("o/r", 100, "squash") == [
        "pr", "merge", "100", "--repo", "o/r", "--auto", "--squash", "--delete-branch",
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
    argv, token = fr.calls[0]
    assert argv[:5] == ["gh", "issue", "edit", "5", "--repo"]
    assert "--remove-label" in argv and "ai-ready" in argv
    assert "--add-label" in argv and "ai-working" in argv
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
    fr = FakeRunner()
    gw = GitHubGateway("o/r", runner=fr)
    gw.enable_auto_merge(100, "squash")
    argv, _ = fr.calls[0]
    assert argv[1:4] == ["pr", "merge", "100"]
    assert "--auto" in argv and "--squash" in argv and "--delete-branch" in argv


def test_update_branch_argv():
    fr = FakeRunner()
    gw = GitHubGateway("o/r", runner=fr)
    gw.update_branch(100)
    argv, _ = fr.calls[0]
    assert argv[1:4] == ["pr", "update-branch", "100"]
    assert argv[4:6] == ["--repo", "o/r"]


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
    assert gw.submit_review(100, "APPROVE", "lgtm") is True
    argv, _ = fr.calls[0]
    assert argv[1] == "api" and "--method" in argv and "POST" in argv


def test_submit_review_returns_false_on_failure():
    # a failed POST (422/403) → runner returns "" (and logs stderr) → False.
    fr = FakeRunner()  # no canned response → "" for every call
    gw = GitHubGateway("o/r", runner=fr)
    assert gw.submit_review(100, "APPROVE", "lgtm") is False


def test_get_issue_parses_response():
    resp = {"number": 5, "title": "x", "labels": [{"name": "ai-ready"}],
            "state": "OPEN", "author": {"login": "a"}}
    fr = FakeRunner({"issue view 5": resp})
    gw = GitHubGateway("o/r", runner=fr)
    iss = gw.get_issue(5)
    assert iss.number == 5 and iss.has(Label.AI_READY)


def test_get_pr_merges_checks_endpoint():
    pr_resp = {"number": 100, "headRefName": "fix/5-x", "state": "OPEN", "labels": []}
    checks = [{"name": "unit", "bucket": "pass"}]
    fr = FakeRunner({"pr view 100": pr_resp, "pr checks 100": checks})
    gw = GitHubGateway("o/r", runner=fr)
    pr = gw.get_pr(100)
    assert pr.number == 100
    assert pr.checks and pr.checks[0].name == "unit"


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


def test_build_steward_world_includes_open_and_closed_ai_done():
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
        return ""

    gw = GitHubGateway("o/r", runner=runner)
    w = gw.build_steward_world()
    assert sorted(i.number for i in w.issues) == [5, 9]


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
            return json.dumps(["closed", "open"])  # jq already reduced to states
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
            return json.dumps(["closed"])
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
            return json.dumps(["open"])
        if "issues/5" in key:
            return "1"
        return ""

    gw = GitHubGateway("o/r", runner=runner)
    w = gw.build_world()
    by_num = {i.number: i for i in w.issues}
    assert by_num[5].blocked_by_open is True     # ready issue gated + blocked
    assert by_num[6].blocked_by_open is False    # working issue never gated


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


def test_benign_statuscheckrollup_failure_is_silent_without_debug(monkeypatch, capsys):
    """The `gh pr checks` statusCheckRollup failure (App token lacks checks read)
    is known-benign noise — one line per PR per poll. With debug OFF it must NOT
    hit stderr."""
    monkeypatch.delenv("ISSUE_AGENT_DEBUG", raising=False)
    monkeypatch.setattr(_subprocess, "run", _fake_run_factory(1, _ROLLUP_ERR))
    out = _subprocess_runner(["gh", "pr", "checks", "112", "--repo", "o/r"], None)
    assert out == ""                       # still fail-soft (empty result)
    assert capsys.readouterr().err == ""   # but silent


def test_benign_statuscheckrollup_failure_shown_with_debug(monkeypatch, capsys):
    """Debug ON → the benign noise IS logged (operator asked to see everything)."""
    monkeypatch.setenv("ISSUE_AGENT_DEBUG", "1")
    monkeypatch.setattr(_subprocess, "run", _fake_run_factory(1, _ROLLUP_ERR))
    _subprocess_runner(["gh", "pr", "checks", "112", "--repo", "o/r"], None)
    assert "gh failed" in capsys.readouterr().err


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


def test_runner_raises_network_on_timeout(monkeypatch, capsys):
    """The 120s kill produced NO data; returning "" made a hung `gh pr view` look
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
    """The ai-ready dependency read shares the issue's isolation boundary: a
    `gh api …/dependencies/blocked_by` that wedges on ONE issue drops that issue
    for a tick (safer than dispatching it ungated) instead of killing the poll."""
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
    assert [i.number for i in gw.build_world().issues] == [8]
    assert "skip issue #7" in capsys.readouterr().err


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


def test_failed_run_log_tails_the_newest_failed_run_on_the_head_branch():
    """review M13 / port of agent/fix-ci.sh:39-42. Keyed on the REAL head branch —
    `gh run list` has no PR filter, and the bash's first version guessed the branch
    from the issue number and read the wrong repo's runs."""
    log = "\n".join(f"line {i}" for i in range(1, 301))
    fr = FakeRunner({"run list": [{"databaseId": 4242}], "run view 4242": log})
    gw = GitHubGateway("o/r", runner=fr)
    out = gw.failed_run_log(100, branch="fix/5-null-token")
    assert out.splitlines() == [f"line {i}" for i in range(301 - _FAILED_LOG_LINES, 301)]
    list_argv = fr.calls[0][0]
    assert list_argv[1:3] == ["run", "list"]
    assert "fix/5-null-token" in list_argv
    assert "--status" in list_argv and "failure" in list_argv
    assert _limit_of(list_argv) is not None          # review C2 applies here too
    view_argv = fr.calls[1][0]
    assert view_argv[1:4] == ["run", "view", "4242"] and "--log-failed" in view_argv


def test_failed_run_log_resolves_the_head_branch_when_not_supplied():
    fr = FakeRunner({
        "pr view 100": {"number": 100, "headRefName": "fix/5-x", "state": "OPEN"},
        "pr checks 100": [], "run list": [{"databaseId": 1}], "run view 1": "boom",
    })
    gw = GitHubGateway("o/r", runner=fr)
    assert gw.failed_run_log(100) == "boom"
    run_list = [c[0] for c in fr.calls if c[0][1:3] == ["run", "list"]][0]
    assert "fix/5-x" in run_list


def test_failed_run_log_empty_when_no_run_failed():
    fr = FakeRunner({"run list": []})
    gw = GitHubGateway("o/r", runner=fr)
    assert gw.failed_run_log(100, branch="b") == ""
    assert not any(c[0][1:3] == ["run", "view"] for c in fr.calls)   # no pointless fetch


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
    assert GitHubGateway("o/r", runner=runner).failed_run_log(100, branch="b") == ""


def test_failed_run_log_does_not_swallow_a_programmer_error():
    """The fail-soft catch must not hide review C2's guard: a list argv built
    without --limit is a bug in this module, not a transient gh failure."""
    gw = GitHubGateway("o/r", runner=_raising_runner(MissingListLimit("no --limit")))
    with pytest.raises(MissingListLimit):
        gw.failed_run_log(100, branch="b")


# ── P1R-13: `tail -150` bounds LINE count, not bytes ──────────────────────────
def test_failed_run_log_bounds_by_bytes_not_just_lines():
    """Repro: a log whose lines are individually enormous (a minified bundle,
    a base64 blob, a per-line JSON dump) sails straight through the 150-LINE
    cap — measured, a 200MB CI log's last 150 lines was still a
    150,001,711-char prompt. ONE 5MB line (well within the 150-line cap) is
    enough to demonstrate the byte ceiling holds independently."""
    huge_line = "x" * 5_000_000
    fr = FakeRunner({"run list": [{"databaseId": 1}], "run view 1": huge_line})
    gw = GitHubGateway("o/r", runner=fr)
    out = gw.failed_run_log(100, branch="b")
    assert len(out.encode("utf-8")) <= _FAILED_LOG_BYTES
    assert out  # anti-vacuity: real (truncated) content survives, not wiped to ""


def test_failed_run_log_under_the_byte_cap_is_untouched():
    # anti-vacuity twin: a normal, small log is NOT truncated by the new cap —
    # the byte ceiling only bites the pathological case.
    log = "\n".join(f"line {i}" for i in range(1, 301))
    fr = FakeRunner({"run list": [{"databaseId": 1}], "run view 1": log})
    gw = GitHubGateway("o/r", runner=fr)
    out = gw.failed_run_log(100, branch="b")
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
