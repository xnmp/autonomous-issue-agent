# /// script
# requires-python = ">=3.10"
# dependencies = ["tree-sitter>=0.22", "tree-sitter-bash>=0.23"]
# ///
"""PreToolUse(Bash) governor for the autonomous issue agent.

Reads the hook JSON on stdin, parses ``.tool_input.command`` into an AST, finds
every *command* node (including those nested inside ``&&``/``||``/``;``,
pipelines, subshells, command substitution, and heredoc-redirected commands),
and routes governed commands to the appropriate checker.

Contract (Claude Code PreToolUse hook):
  * stdin  : JSON with ``.tool_input.command``
  * exit 0 : allow the Bash call
  * exit 2 : block the call; stderr is shown to the agent

Design rules:
  * Never executes the command — only inspects structure.
  * Fail-OPEN on parse errors / unknown input (other layers handle those).
  * Fail-CLOSED on a *recognised* bypass attempt (--no-verify, core.hooksPath,
    HUSKY=0, etc.), no matter how deeply nested.

The module is import-safe: ``main()`` only runs under ``__main__`` so the test
suite can call ``extract_commands`` / ``classify`` / ``find_violations``
directly without touching stdin or the filesystem.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
HOOK_DIR = os.path.dirname(os.path.abspath(__file__))

CHECKERS = {
    "git-branch": "checks/check-branch-name.sh",
    "git-commit": "checks/check-commit.sh",
    "gh-pr-create": "checks/check-pr.sh",
    # git-push runs check-push.sh, which enforces the state-derived gate ONLY when
    # the branch already has an open PR (re-verifies address-review pushes); it is
    # a no-op for ordinary pre-PR pushes. Bypass detection still runs first.
    "git-push": "checks/check-push.sh",
}

# A git-commit additionally triggers the ordering/linkage checker. It is keyed
# separately so a single `git commit` runs BOTH check-commit.sh (prefix/lint)
# and check-commit-order.sh (red-before-green, Fixes #n on the PR-less branch).
ORDER_CHECKER = "checks/check-commit-order.sh"

# git subcommands that, on their own, can DISABLE the gate or escape branch-name
# governance. These have no checker script — they are pure bypass guards
# (git-config can redirect core.hooksPath; git-push can run --no-verify).
try:
    from issue_agent.adapters.governor_policy import (
        Governed,
        arg_value,
        bypass_reason,
        classify,
        extract_commands,
        extract_commands_with_env,
        find_violations,
    )
except ModuleNotFoundError:
    _ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.path.insert(0, os.path.join(_ROOT, "src"))
    from issue_agent.adapters.governor_policy import (  # type: ignore[no-redef]
        Governed,
        arg_value,
        bypass_reason,
        classify,
        extract_commands,
        extract_commands_with_env,
        find_violations,
    )


# ── hook entry point ─────────────────────────────────────────────────────────
def _block(msg: str) -> None:
    sys.stderr.write(msg.rstrip() + "\n")
    sys.exit(2)


def _run_checker(script_rel: str, args: list[str]) -> tuple[int, str]:
    script = os.path.join(HOOK_DIR, script_rel)
    if not os.path.exists(script):
        # FAIL-CLOSED: a missing checker means the gate was tampered with or
        # mis-installed. Block rather than silently allow a governed command.
        return 1, (
            f"gate checker missing: {script_rel}. The enforcement plugin looks "
            f"incomplete — refusing the command. Reinstall .claude/hooks/."
        )
    res = subprocess.run(
        ["bash", script, *args], capture_output=True, text=True
    )
    return res.returncode, (res.stdout + res.stderr)


_REVIEW_READ_ONLY_COMMANDS = frozenset({"cat", "ls"})
_REVIEW_READ_ONLY_GIT = frozenset({"diff"})
_REVIEW_READ_ONLY_GH_PR = frozenset({"diff", "view"})


def review_only_violation(command: str) -> str | None:
    """Reject every reviewer Bash command outside the task's read-only surface.

    Codex does not consume AgentTask.allowed_tools/disallowed_tools. The
    read-only sandbox protects the checkout, but it does not make a `gh` write
    command harmless, so this hook also prevents an untrusted PR prompt from
    using the daemon's ambient credentials as an escape hatch.
    """
    for words, _env in extract_commands_with_env(command):
        if not words:
            continue
        if words[0] in _REVIEW_READ_ONLY_COMMANDS:
            continue
        if len(words) >= 2 and words[0] == "git" and words[1] in _REVIEW_READ_ONLY_GIT:
            continue
        if (
            len(words) >= 3
            and words[0] == "gh"
            and words[1] == "pr"
            and words[2] in _REVIEW_READ_ONLY_GH_PR
        ):
            continue
        return "Codex review is read-only; only git diff, gh pr diff/view, cat, and ls are allowed"
    return None


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        sys.exit(0)  # not our concern; let other hooks/permissions handle it
    command = (payload.get("tool_input") or {}).get("command", "")
    if not command:
        sys.exit(0)

    if os.environ.get("ISSUE_AGENT_REVIEW_READ_ONLY") == "1":
        violation = review_only_violation(command)
        if violation:
            _block(f"Blocked: {violation}")

    governed: list[tuple[Governed, list[str]]] = []
    for words, env in extract_commands_with_env(command):
        g = classify(words)
        if g is not None:
            governed.append((g, env))
    if not governed:
        sys.exit(0)

    # 1) Bypass detection first — fail closed, no checker can override it. Both
    #    the variable_assignment env prefixes (HUSKY=0 git …) and any env vars
    #    recovered from an `env KEY=VAL git …` wrapper (g.extra_env) are checked.
    for g, env in governed:
        reason = bypass_reason(g.full_words, g.kind, env, g.extra_env)
        if reason:
            _block(f"Blocked: {reason}")

    # 2) Route to checkers. git-config is bypass-only (no checker). git-push runs
    #    check-push.sh (open-PR-only re-verify). git-commit runs BOTH the
    #    prefix/lint checker and the ordering/linkage checker.
    for g, _env in governed:
        if g.kind not in CHECKERS:
            continue  # git-config: bypass guard only
        rc, out = _run_checker(CHECKERS[g.kind], g.checker_args)
        if rc != 0:
            _block(out)
        if g.kind == "git-commit":
            rc2, out2 = _run_checker(ORDER_CHECKER, g.checker_args)
            if rc2 != 0:
                _block(out2)

    sys.exit(0)


if __name__ == "__main__":
    main()
