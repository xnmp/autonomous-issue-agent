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
  * Fail-CLOSED when parsing cannot resolve a possibly governed command.
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
    from issue_agent.domain.governor_policy import (
        Governed,
        arg_value,
        bypass_reason,
        classify,
        extract_commands,
        extract_commands_with_env,
        find_violations,
        resolve,
        Unresolvable,
    )
except ModuleNotFoundError:
    _ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.path.insert(0, os.path.join(_ROOT, "src"))
    from issue_agent.domain.governor_policy import (  # type: ignore[no-redef]
        Governed,
        arg_value,
        bypass_reason,
        classify,
        extract_commands,
        extract_commands_with_env,
        find_violations,
        resolve,
        Unresolvable,
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


# Read-only Bash surfaces, per LEG ROLE.
#
# Each profile is an allowlist of exact verbs. Adding a verb here widens what a
# leg reading attacker-controlled issue/PR text can do with the daemon's ambient
# credentials, so nothing mutating belongs in any of them.
_READ_ONLY_PROFILES: dict[str, tuple[frozenset, frozenset, dict, str]] = {
    "review": (
        frozenset({"cat", "ls"}),                     # bare commands
        frozenset({"diff"}),                          # git subcommands
        {"pr": frozenset({"diff", "view"})},          # gh <noun> <verb>
        "Codex review is read-only; only git diff, gh pr diff/view, cat, and ls are allowed",
    ),
}


def read_only_violation(command: str, role: str = "") -> str | None:
    """Reject every Bash command outside `role`'s read-only surface.

    Codex does not consume AgentTask.allowed_tools/disallowed_tools. The
    read-only sandbox protects the checkout, but it does not make a `gh` write
    command harmless, so this hook also prevents an untrusted issue/PR prompt
    from using the daemon's ambient credentials as an escape hatch.

    An UNRECOGNISED role gets the reviewer's narrowest profile, not a wider one:
    the role arrives from `ISSUE_AGENT_LEG_ROLE`, which only the dispatching
    runner writes, so an unknown value means the env did not propagate — and a
    propagation failure must never widen a security boundary.
    """
    profile = _READ_ONLY_PROFILES.get(role)
    if profile is None:
        # Name the observed role: the fallback is deliberate (a propagation
        # failure must never widen a security boundary), but the reviewer's
        # message on another role's leg sends the operator hunting in the wrong
        # role's config.
        commands, git_verbs, gh_nouns, message = _READ_ONLY_PROFILES["review"]
        if role:
            message = (
                f"{message} (leg role {role!r} has no read-only profile; "
                "the reviewer's applied)"
            )
    else:
        commands, git_verbs, gh_nouns, message = profile
    for words, _env in extract_commands_with_env(command):
        if not words:
            continue
        if words[0] in commands:
            continue
        if words[0] == "git" and words[1:2]:
            # `git --no-pager log` is a form agents write constantly; the
            # pager-suppression flags are inert, so unwrap ONLY those before
            # the verb match. `-c`/other globals stay blocked (config
            # injection reaches pagers and hooks).
            verb_at = 1
            while verb_at < len(words) and words[verb_at] in ("--no-pager", "-P"):
                verb_at += 1
            if verb_at < len(words) and words[verb_at] in git_verbs:
                continue
        if (
            len(words) >= 3
            and words[0] == "gh"
            and words[2] in gh_nouns.get(words[1], frozenset())
        ):
            continue
        return message
    return None


def review_only_violation(command: str) -> str | None:
    """Back-compatible alias for the reviewer's profile."""
    return read_only_violation(command, "review")


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        sys.exit(0)  # not our concern; let other hooks/permissions handle it
    command = (payload.get("tool_input") or {}).get("command", "")
    if not command:
        sys.exit(0)

    # Read-only mode is declared by the REVIEWER's generated hook script, which
    # `export`s the flag into this process. That declaration is ambient
    # filesystem state living at the reviewer's cwd, and a FIXER worktree nested
    # under that cwd resolves to the same hook (#113: the first codex review of a
    # deployment installed a read-only hook at TARGET_DIR and every subsequent
    # fixer leg — whose worktrees live at TARGET_DIR/.claude/worktrees/issue-N —
    # was denied git/tests/commit/push and blocked asking a human why).
    #
    # The leg's ROLE is the authority, and only the runner that dispatches the
    # leg can state it. Unsetting the flag in the fixer's process env cannot
    # work: the hook re-exports it downstream of that env. So precedence, not
    # absence, is what settles it.
    #
    # Fail direction is deliberate and asymmetric. An ABSENT role reads as "not
    # a fixer", so read-only still applies — a reviewer whose env failed to
    # propagate keeps its sandbox (fail CLOSED on the security boundary). A
    # fixer whose env failed to propagate is merely blocked, which is loud,
    # costs no correctness, and is the state #113 already describes.
    leg_role = os.environ.get("ISSUE_AGENT_LEG_ROLE", "")
    read_only = (
        os.environ.get("ISSUE_AGENT_REVIEW_READ_ONLY") == "1"
        and leg_role != "fix"
    )
    if read_only:
        # The role selects WHICH read-only surface applies.
        violation = read_only_violation(command, leg_role)
        if violation:
            _block(f"Blocked: {violation}")

    resolution = resolve(command, repository=os.environ.get("REPO", ""))
    if isinstance(resolution, Unresolvable):
        _block(
            "Blocked: command cannot be governed statically — "
            f"{resolution.reason}. Use a literal executable and argv."
        )

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
        reason = bypass_reason(
            g.full_words,
            g.kind,
            env,
            g.extra_env,
            g.durable_wrapper,
        )
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
        if (
            os.environ.get("ISSUE_AGENT_DURABLE_PUBLISH") == "1"
            and g.kind in {"git-push", "gh-pr-create"}
            and not g.durable_wrapper
        ):
            command = (
                "issue-agent-publish git push …"
                if g.kind == "git-push"
                else "issue-agent-publish gh pr create …"
            )
            _block(
                f"Blocked: remote publication must use `{command}` so the "
                "effect has a durable command and receipt"
            )
        if g.kind == "git-commit":
            rc2, out2 = _run_checker(ORDER_CHECKER, g.checker_args)
            if rc2 != 0:
                _block(out2)

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as exc:
        sys.stderr.write(
            "Blocked: command governor failed safely — "
            f"{type(exc).__name__}\n"
        )
        sys.exit(2)
