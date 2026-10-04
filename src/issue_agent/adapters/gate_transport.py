"""Trusted shell gate composition over the existing Git facts and executor."""

import os
import time
from dataclasses import replace

from ..domain.gate import PushLookupPolicy
from .governor import GateContext, complete_gate_context
from .worktree import build_gate_context


def shell_gate_context(cwd: str, env: dict[str, str]) -> GateContext:
    def flag(name: str, default: str) -> bool:
        value = env.get(name, default)
        if value not in {"0", "1"}:
            raise ValueError(f"malformed {name}")
        return value == "1"

    base = env.get("BASE_BRANCH", "dev")
    if not base or len(base) > 256 or "\x00" in base:
        raise ValueError("malformed BASE_BRANCH")
    receipt = env.get("HARNESS_SCREENSHOTS_READ", "")
    if receipt and not os.path.isabs(receipt):
        receipt = os.path.abspath(os.path.join(cwd, receipt))
    retry_delay = float(env.get("GATE_GH_RETRY_SLEEP", "2"))
    if not 0 <= retry_delay <= 30:
        raise ValueError("malformed GATE_GH_RETRY_SLEEP")
    context = build_gate_context(cwd, base_branch=base,
        env_test_cmd=env.get("TEST_CMD"),
        red_must_fail=flag("RED_MUST_FAIL", "1"),
        verify_red_at_commit=flag("VERIFY_RED_AT_COMMIT", "1"),
        allow_untested=flag("ALLOW_UNTESTED", "0"),
        screenshots_read_path=receipt,
        retry_sleep=lambda: time.sleep(retry_delay),
        durable_publish=False)
    # The legacy receipt capability and stricter unknown-PR boundary belong to
    # this transport; no target argv/env switch can select another profile.
    return replace(complete_gate_context(context, cwd, env=env),
        push_lookup_policy=PushLookupPolicy.REQUIRE_VERIFIED)
