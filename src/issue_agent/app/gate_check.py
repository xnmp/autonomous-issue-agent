"""Absolute harness-owned entry for advisory shell gates."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# -I drops cwd and PYTHONPATH; only this entry's own harness package is added.
_HARNESS_SOURCE = Path(__file__).resolve().parents[2]
if not (_HARNESS_SOURCE / "issue_agent" / "__init__.py").is_file():
    raise SystemExit("Gate blocked: harness-owned package is unavailable")
sys.path.insert(0, str(_HARNESS_SOURCE))

from issue_agent.adapters import governor
from issue_agent.adapters.gate_transport import shell_gate_context
from issue_agent.adapters.worktree import _test_timeout_s
from issue_agent.domain import gate


_OPERATIONS = frozenset({"commit", "commit-order", "pr", "push", "run-target"})


def main(argv: list[str] | None = None) -> int:
    tokens = list(sys.argv[1:] if argv is None else argv)
    if not tokens or tokens[0] not in _OPERATIONS or len(tokens) > 4096 or any(len(token) > 2_000_000 or "\x00" in token for token in tokens):
        print("Gate blocked: malformed operation or arguments", file=sys.stderr)
        return 1
    operation, arguments = tokens[0], tokens[1:]
    try:
        cwd = os.getcwd()
        if operation == "run-target":
            if len(arguments) != 1 or not arguments[0]:
                raise ValueError("target command required")
            rc, _output = governor.run_target_command(arguments[0], cwd, _test_timeout_s(), capture=False)
            if rc is None:
                print("GATE RECURSION: target command re-entered the harness gate", file=sys.stderr)
                return 213
            return rc
        ctx = shell_gate_context(cwd, dict(os.environ))
        if ctx.policy_input_error is not None:
            raise ValueError(ctx.policy_input_error)
        if operation == "commit-order":
            if ctx.base_resolvable and not ctx.history_available:
                raise ValueError("required Git history facts are unavailable")
            result = gate.check_commit_order(governor._extract_after_flag(arguments, "-m", "--message"), ctx.branch_subjects, ctx.base_resolvable)
            reasons = [] if result.ok else [result.reason]
        elif operation == "commit":
            result = governor.commit_gate(arguments, ctx, include_order=False)
            reasons = [] if result.allowed else [result.reason]
        elif operation == "pr":
            reasons = governor._pr_gate(arguments, ctx) if ctx.base_resolvable else [f"cannot resolve base branch '{ctx.base_branch}' to compute the diff"]
        else:
            reasons = governor._push_gate(arguments, ctx)
        if not reasons and operation == "pr" and ctx.allow_untested and not gate.resolve_test_cmd(ctx.harness_test_cmd, ctx.env_test_cmd):
            print("GATE WARNING: no test command; operator ALLOW_UNTESTED opt-out")
        if reasons:
            print(f"{operation.capitalize()} blocked: {reasons[0]}")
            return 1
        return 0
    except Exception as exc:
        print(f"Gate blocked: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
