"""The fixer's deny list is credential hygiene; network tools stay usable."""

import pytest

from issue_agent.domain.tool_policy import FIXER_DENY

CREDENTIAL_RULES = frozenset({
    "Bash(gh auth:*)", "Bash(gh secret:*)",
    # secrets on disk, at the root and at any depth
    "Read(.env)", "Read(.env.*)", "Read(**/.env)", "Read(**/.env.*)", "Read(**/*.pem)", "Read(**/id_rsa*)",
})
# PR #403 deletes these two rows (the agent reads its own CI logs; #1128 is a prompt rule).
CI_RULES = frozenset({"Bash(gh api:*)", "Bash(gh run watch:*)"})


def _denies(tool: str, command: str = "") -> bool:
    """Claude permission-rule matching for the two forms the list uses:
    a bare tool name, or `Bash(<prefix>:*)`."""
    for rule in FIXER_DENY:
        if rule == tool:
            return True
        if tool == "Bash" and rule.startswith("Bash(") and rule.endswith(":*)"):
            prefix = rule[len("Bash("):-len(":*)")]
            if command == prefix or command.startswith(prefix + " "):
                return True
    return False


def test_deny_list_holds_exactly_the_credential_rules():
    assert set(FIXER_DENY) == CREDENTIAL_RULES | CI_RULES
    assert len(FIXER_DENY) == len(set(FIXER_DENY))


@pytest.mark.parametrize("tool,command", [
    ("WebFetch", ""), ("WebSearch", ""),
    ("Bash", "curl -sf http://localhost:1420"), ("Bash", "wget https://docs.rs/x"),
    ("Bash", "gh pr view 12"), ("Bash", "gh run view 1 --log-failed"),
])
def test_network_and_workflow_tools_are_allowed(tool, command):
    assert not _denies(tool, command)


@pytest.mark.parametrize("command", ["gh auth token", "gh secret list"])
def test_credential_commands_are_denied(command):
    assert _denies("Bash", command)
