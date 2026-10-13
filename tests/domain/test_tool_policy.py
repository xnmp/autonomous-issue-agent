import pytest

from issue_agent.domain.tool_policy import DENIED_TOOLS, FIXER_DENY, denial_reason


def bash(cmd):
    return denial_reason("Bash", {"command": cmd}, FIXER_DENY)


@pytest.mark.parametrize("cmd", [
    "curl http://x", "wget x", "a && curl x", "a; wget x", "a | curl x", "a || curl x",
    "FOO=1 curl x", "env curl x", "/usr/bin/curl x", 'bash -c "curl x"', "sh -c 'a && wget y'",
    "gh api repos/x", "gh auth token", "gh secret list", "echo hi\ncurl x",
])
def test_denied_bash_carries_reason(cmd):
    assert bash(cmd)


def test_curl_reason_points_to_alternative():
    assert "agent-browser" in bash("x && curl y")


@pytest.mark.parametrize("cmd", [
    "bun run dev", "agent-browser open http://localhost:1436", "gh pr create", "gh issue view 3",
    "echo curl", "grep curl README.md", "ls", "", "curlish x",
])
def test_allowed_bash_passes(cmd):
    assert bash(cmd) is None


@pytest.mark.parametrize("tool", ["WebFetch", "WebSearch"])
def test_web_tools_denied(tool):
    assert denial_reason(tool, {"url": "x"}, FIXER_DENY)


@pytest.mark.parametrize("path", [".env", ".env.local", "/a/b/.env", "/a/k.pem", "/h/.ssh/id_rsa", "/h/id_rsa.pub"])
def test_secret_reads_denied(path):
    assert denial_reason("Read", {"file_path": path}, FIXER_DENY)


def test_ordinary_read_passes():
    assert denial_reason("Read", {"file_path": "/a/src/env.py"}, FIXER_DENY) is None


def test_every_table_entry_has_reason_and_feeds_disallowed_tools():
    assert FIXER_DENY == tuple(d.rule for d in DENIED_TOOLS)
    assert all(d.reason.strip() for d in DENIED_TOOLS)


@pytest.mark.parametrize("name,inp", [
    (None, None), ("Bash", None), ("Bash", {"command": None}), ("Bash", {"command": 5}),
    ("Bash", {"command": "'unterminated curl"}), ("Bash", {"command": "a" * 5_000_000}),
    ("Read", {"file_path": ["x"]}), (7, {}), ("Bash", "curl x"),
])
def test_malformed_or_huge_input_never_raises(name, inp):
    denial_reason(name, inp, FIXER_DENY)


@pytest.mark.parametrize("cmd", [
    'git commit -m "docs: replace curl; wget usage"', 'echo "a | curl b"', "grep -E 'error|curl ' src",
    "git commit -F - <<'EOF'\nfix\ncurl is gone\nEOF", 'echo "x\ncurl y"', "echo hi # ; curl", "'unterminated curl",
])
def test_quoted_or_heredoc_text_is_not_a_command(cmd):
    assert bash(cmd) is None


def test_reasonless_rules_install_no_hook():
    from issue_agent.domain.tool_policy import has_reasons
    assert not has_reasons(("Edit", "Write")) and has_reasons(FIXER_DENY)
