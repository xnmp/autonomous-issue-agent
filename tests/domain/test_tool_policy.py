"""The fixer's deny list keeps credential hygiene; network tools stay usable."""

from issue_agent.domain.tool_policy import FIXER_DENY

CREDENTIAL_RULES = frozenset({
    "Bash(gh auth:*)", "Bash(gh secret:*)",
    # secrets on disk, at the root and at any depth
    "Read(.env)", "Read(.env.*)", "Read(**/.env)", "Read(**/.env.*)", "Read(**/*.pem)", "Read(**/id_rsa*)",
})


def test_credential_rules_are_denied():
    assert CREDENTIAL_RULES <= set(FIXER_DENY)


def test_network_tools_are_not_denied():
    # tauri #1078: a denied `curl` localhost probe was read as "dev server denied".
    assert not {"WebFetch", "WebSearch"} & set(FIXER_DENY)
    assert not [r for r in FIXER_DENY if r.startswith(("Bash(curl", "Bash(wget"))]


def test_the_fixer_keeps_its_working_tools():
    # The pre-push review is a Task subagent; the TDD loop is git/test/gh Bash work.
    assert not {"Task", "Bash", "Read", "Edit", "Write"} & set(FIXER_DENY)
    assert not [r for r in FIXER_DENY if r.startswith(("Bash(git", "Bash(gh pr", "Bash(gh issue"))]


def test_ci_and_review_reads_are_not_denied():
    # PR #403: agents read job logs and inline review comments with `gh api`.
    # The `gh run watch` deny went because a repair is always handed its
    # evidence (tauri #1078); AGENT.md carries the no-wait rule.
    assert not [r for r in FIXER_DENY if r.startswith(("Bash(gh api", "Bash(gh run"))]
