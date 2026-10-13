import asyncio

from issue_agent.adapters.agent_runner import deny_reason_hook
from issue_agent.domain.tool_policy import FIXER_DENY

def test_hook_emits_deny_decision_and_tolerates_garbage():
    hook = deny_reason_hook(FIXER_DENY)
    out = asyncio.run(hook({"tool_name": "Bash", "tool_input": {"command": "x && curl y"}}, None, None))
    spec = out["hookSpecificOutput"]
    assert spec["permissionDecision"] == "deny" and "agent-browser" in spec["permissionDecisionReason"]
    assert asyncio.run(hook({"tool_name": "Bash", "tool_input": {"command": "ls"}}, None, None)) == {}
    assert asyncio.run(hook("garbage", None, None)) == {}
