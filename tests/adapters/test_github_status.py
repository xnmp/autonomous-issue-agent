"""One HTTP classification for reads and writes (#462, ADR 0001)."""
import json
from types import SimpleNamespace

import pytest

from issue_agent.adapters.github import Accepted, Refused, GhUnavailable, GitHubGateway, _subprocess_runner


def http(status, body=None):
    return f"HTTP/2.0 {status} Answer\r\n\r\n" + json.dumps(body or {})


@pytest.mark.parametrize("status,message,kind", [
    (404, "gateway timeout", None), (422, "bad credentials", None),
    (401, "untranslated", "auth"), (429, "untranslated", "rate-limit"),
    (503, "untranslated", "server"), (599, "x" * 100000, "server"),
])
def test_read_failures_follow_the_http_status_even_when_message_is_misleading(monkeypatch, status, message, kind):
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: SimpleNamespace(
        returncode=1, stdout=json.dumps({"message": message}), stderr=f"gh: {message} (HTTP {status})"))
    if kind is None:
        assert _subprocess_runner(["gh", "api", "repos/o/r"], None) == ""
    else:
        with pytest.raises(GhUnavailable) as caught:
            _subprocess_runner(["gh", "api", "repos/o/r"], None)
        assert caught.value.kind == kind


@pytest.mark.parametrize("operation", ["close", "reopen", "label", "merge"])
@pytest.mark.parametrize("status", [200, 403, 422])
def test_state_writes_use_githubs_answer_without_reading_it_back(operation, status):
    calls=[]
    def runner(argv, token):
        calls.append(argv)
        if argv[1:3] == ["pr", "view"]:
            assert len(calls) == 1, "an answered merge must not be read back"
            return json.dumps({"autoMergeRequest": None, "headRefOid": "head"})
        assert argv[1:3] == ["api", "--include"]
        return http(status, {"message": "answer"})
    gateway=GitHubGateway("o/r", runner=runner)
    if operation == "merge":
        assert gateway.enable_auto_merge(5, "squash", "head") is (status == 200)
    else:
        answer = (gateway.close_issue(5, "completed") if operation == "close" else
                  gateway.reopen_issue(5) if operation == "reopen" else
                  gateway.relabel("issue", 5, None, "ready"))
        assert answer == (Accepted() if status == 200 else Refused(status, "answer"))
    assert len(calls) == (2 if operation == "merge" else 1)


@pytest.mark.parametrize("status", [200, 403])
def test_disabling_legacy_auto_merge_uses_its_answer_before_the_head_bound_merge(status):
    calls = []
    def runner(argv, token):
        calls.append(argv)
        if argv[1:3] == ["pr", "view"]:
            return json.dumps({"id": "PR_node", "autoMergeRequest": {}, "headRefOid": "head"})
        return http(status if "graphql" in argv else 200)
    assert GitHubGateway("o/r", runner=runner).enable_auto_merge(5, "squash", "head") is (status == 200)
    assert any("pr=PR_node" in a for a in calls)
    merges = [a for a in calls if "repos/o/r/pulls/5/merge" in a]
    assert bool(merges) is (status == 200)
    if merges:
        assert "sha=head" in merges[0]


def test_label_answer_is_success_even_while_the_read_lags_and_a_refusal_keeps_the_old_label():
    from issue_agent.adapters.act import relabel
    class Labels:
        answer = Accepted()
        writes = []
        def has_label(self, kind, number, label):
            return label == "old"
        def relabel(self, kind, number, remove, add):
            self.writes.append((remove, add))
            return self.answer
    g = Labels()
    assert relabel(g, "issue", 5, add=("new",), remove=("old",)) == (("new",), ("old",))
    g.writes = []
    g.answer = Refused(403, "forbidden")
    with pytest.raises(RuntimeError, match="refused"):
        relabel(g, "issue", 5, add=("new",), remove=("old",))
    assert g.writes == [(None, "new")]


@pytest.mark.parametrize("status,kind", [(403, "rate-limit"), (404, None)])
def test_a_read_preserves_retry_after_without_turning_other_refusals_into_rate_limits(monkeypatch, status, kind):
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: SimpleNamespace(
        returncode=1, stdout="", stderr=f"HTTP {status}: Forbidden; Retry-After: 60"))
    if kind:
        with pytest.raises(GhUnavailable) as caught:
            _subprocess_runner(["gh", "api", "repos/o/r"], None)
        assert (caught.value.kind, caught.value.retry_after_s) == (kind, 60.0)
    else:
        assert _subprocess_runner(["gh", "api", "repos/o/r"], None) == ""


@pytest.mark.parametrize("deletion", [204, 403, None])
def test_a_merged_branch_is_deleted_and_cleanup_failure_does_not_undo_the_merge(deletion):
    calls=[]
    def runner(argv, token):
        calls.append(argv)
        if argv[1:3] == ["pr", "view"]:
            return json.dumps({"autoMergeRequest": None, "headRefOid": "head", "headRefName": "fix/5", "isCrossRepository": False})
        if "DELETE" in argv:
            if deletion is None:
                raise GhUnavailable("network", "reply lost")
            return http(deletion)
        return http(200)
    assert GitHubGateway("o/r", runner=runner).enable_auto_merge(5, "squash", "head")
    assert any("repos/o/r/git/refs/heads/fix%2F5" in a and "DELETE" in a for a in calls)


@pytest.mark.parametrize("message", ["API rate-limit exceeded", "abuse detection"])
def test_status_bearing_rate_limit_messages_use_the_shared_classifier(monkeypatch, message):
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: SimpleNamespace(
        returncode=1, stdout="", stderr=f"HTTP 403: {message}"))
    with pytest.raises(GhUnavailable) as caught:
        _subprocess_runner(["gh", "api", "repos/o/r"], None)
    assert caught.value.kind == "rate-limit"


@pytest.mark.parametrize("fork", [True, None, "false"])
def test_merging_a_fork_never_deletes_a_same_named_branch_in_the_target_repo(fork):
    calls=[]
    def runner(argv, token):
        calls.append(argv)
        if argv[1:3] == ["pr", "view"]:
            return json.dumps({"headRefOid": "head", "headRefName": "fix/5", "isCrossRepository": fork})
        return http(200)
    assert GitHubGateway("o/r", runner=runner).enable_auto_merge(5, "squash", "head")
    assert not any("DELETE" in a for a in calls)
