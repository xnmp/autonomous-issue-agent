"""GitHubGateway — pure JSON→WorldState parsers + identity + write-call shapes.

The parsers are the real-vs-fake seam, so they are asserted directly against the
documented gh --json shapes. The gateway is driven with a fake runner that
records argv and returns canned JSON (no network, no real gh)."""

import json

from issue_agent.adapters.github import (
    _LIST_LIMIT,
    GitHubGateway,
    Identity,
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
