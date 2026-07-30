# /// script
# requires-python = ">=3.10"
# dependencies = ["tree-sitter>=0.22", "tree-sitter-bash>=0.23", "pytest>=8"]
# ///
"""Unit tests for the tree-sitter bash governor.

Run: uv run --with pytest pytest tests/test_governor.py -q
or:  uv run tests/test_governor.py   (pytest is in the inline deps)
"""

import os
import sys
import importlib.util

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from issue_agent.domain import governor_policy as gov  # noqa: E402
HOOK = os.path.join(ROOT, "plugin", "hooks", "bash_governor.py")
spec = importlib.util.spec_from_file_location("bash_governor", HOOK)
hook = importlib.util.module_from_spec(spec)
sys.modules["bash_governor"] = hook
spec.loader.exec_module(hook)


def test_shell_hook_reuses_shared_policy_functions():
    assert hook.extract_commands is gov.extract_commands
    assert hook.extract_commands_with_env is gov.extract_commands_with_env
    assert hook.classify is gov.classify
    assert hook.find_violations is gov.find_violations


def test_codex_review_hook_allows_only_the_review_bash_surface():
    assert hook.review_only_violation("git diff --stat && gh pr view 97") is None
    assert hook.review_only_violation("gh pr review 97 --approve") is not None
    assert hook.review_only_violation("curl https://example.test") is not None


# ── extract_commands: finds command nodes anywhere in the tree ────────────────
def test_extract_simple():
    assert gov.extract_commands("git commit -m x") == [["git", "commit", "-m", "x"]]


def test_extract_compound_and():
    cmds = gov.extract_commands('git add -A && git commit -m "x"')
    assert ["git", "add", "-A"] in cmds
    assert ["git", "commit", "-m", "x"] in cmds


def test_extract_command_substitution():
    cmds = gov.extract_commands('foo=$(git commit -m "sneaky")')
    assert ["git", "commit", "-m", "sneaky"] in cmds


def test_extract_subshell():
    cmds = gov.extract_commands("(cd sub && git commit -m x)")
    assert ["git", "commit", "-m", "x"] in cmds


def test_extract_pipeline():
    cmds = gov.extract_commands("echo hi | git commit -F -")
    assert ["git", "commit", "-F", "-"] in cmds


# ── classify: routes only governed commands ──────────────────────────────────
def test_classify_commit():
    g = gov.classify(["git", "commit", "-m", "green: x"])
    assert g.kind == "git-commit"
    assert g.checker_args == ["commit", "-m", "green: x"]


def test_classify_branch_checkout():
    g = gov.classify(["git", "checkout", "-b", "fix/12-bug"])
    assert g.kind == "git-branch"


def test_classify_branch_switch():
    g = gov.classify(["git", "switch", "-c", "feat/3-x"])
    assert g.kind == "git-branch"


def test_classify_switch_without_c_is_not_branch_creation():
    assert gov.classify(["git", "switch", "main"]) is None


def test_classify_pr_create():
    g = gov.classify(["gh", "pr", "create", "--fill"])
    assert g.kind == "gh-pr-create"


def test_classify_skips_global_flags():
    # `git -c k=v commit` must still be recognised as a commit.
    g = gov.classify(["git", "-c", "user.name=x", "commit", "-m", "y"])
    assert g is not None and g.kind == "git-commit"


def test_classify_ignores_unrelated():
    assert gov.classify(["echo", "git", "commit"]) is None
    assert gov.classify(["git", "status"]) is None
    assert gov.classify(["gh", "pr", "view", "1"]) is None


def test_false_positive_echo_not_governed():
    # The classic substring-matcher false positive: an echo mentioning git commit.
    assert gov.find_violations('echo "remember to git commit later"') == []
    # And it is not classified as governed at all.
    cmds = gov.extract_commands('echo "git commit later"')
    assert all(gov.classify(c) is None for c in cmds)


# ── bypass detection: fail closed, however nested ─────────────────────────────
def test_bypass_no_verify():
    assert gov.find_violations('git commit --no-verify -m "x"')


def test_bypass_commit_n_shorthand():
    assert gov.find_violations("git commit -n -m x")


def test_bypass_hookspath_inline_c():
    assert gov.find_violations("git -c core.hooksPath=/dev/null commit -m x")


def test_bypass_hookspath_nested_in_subshell():
    assert gov.find_violations(
        "(cd s && git -c core.hooksPath=/dev/null commit -n -m x)"
    )


def test_bypass_husky_env():
    # HUSKY=0 appears as part of the command line.
    assert gov.find_violations("HUSKY=0 git commit -m x")


def test_no_bypass_on_clean_commit():
    assert gov.find_violations('git commit -m "green: implement x"') == []


def test_pr_create_not_flagged_for_commit_n():
    # `-n` on gh pr create is unrelated to commit bypass; must not be blocked.
    assert gov.find_violations("gh pr create -n") == []


# ── quoting cannot hide a bypass ──────────────────────────────────────────────
def test_quoted_no_verify_still_detected():
    # Even if someone quotes the flag, arg_value unwraps it.
    assert gov.find_violations('git commit "--no-verify" -m x')


# ── adversarial-review regressions: governor bypasses that were found+fixed ───
def test_bypass_ansi_c_quoting():
    # $'--no-verify' parses as ansi_c_string — must be unwrapped, not dropped.
    assert gov.find_violations("git commit $'--no-verify' -m 'red: test'")
    assert gov.find_violations("git commit $'-n' -m test")


def test_bypass_clustered_short_flags():
    # -nm / -an / -nv each contain `n` == --no-verify.
    assert gov.find_violations("git commit -nm 'green: test'")
    assert gov.find_violations("git commit -an -m 'green: test'")
    assert gov.find_violations("git commit -nv -m 'green: test'")


def test_clustered_flag_without_n_is_fine():
    # -am (add+message) has no `n`; must NOT be flagged.
    assert gov.find_violations("git commit -am 'green: test'") == []


def test_bypass_backslash_escaped_flag():
    assert gov.find_violations("git commit --no\\-verify -m 'red: test'")
    assert gov.find_violations("git commit -\\n -m test")


def test_bypass_git_config_hookspath():
    assert gov.find_violations("git config core.hooksPath /dev/null")
    assert gov.find_violations("git config --global core.hooksPath /dev/null")


def test_git_config_unrelated_is_fine():
    assert gov.find_violations("git config user.name alice") == []


def test_bypass_absolute_path_git():
    assert gov.find_violations("/usr/bin/git commit --no-verify -m x")


def test_bypass_env_wrapped_husky():
    assert gov.find_violations("env HUSKY=0 git commit -m 'red: test'")


def test_bypass_env_wrapped_no_verify():
    assert gov.find_violations("env FOO=bar git commit --no-verify -m x")


def test_bypass_git_push_no_verify():
    assert gov.find_violations("git push --no-verify origin main")


def test_classify_push_routes_to_checker():
    # git push is now a checked kind (check-push.sh), not bypass-only.
    g = gov.classify(["git", "push", "origin", "feat/3-x"])
    assert g is not None and g.kind == "git-push"
    assert hook.CHECKERS.get("git-push") == "checks/check-push.sh"


def test_bypass_git_config_env_redirect():
    assert gov.find_violations("GIT_CONFIG_GLOBAL=/dev/null git commit -m x")
    assert gov.find_violations("GIT_CONFIG_NOSYSTEM=1 git commit -m x")


def test_bypass_home_override():
    assert gov.find_violations("HOME=/tmp git commit -m x")


def test_bypass_force_create_branch_classified():
    # -B / -C force-create must still route to the branch checker (classify only).
    assert gov.classify(["git", "checkout", "-B", "bad"]) is not None
    assert gov.classify(["git", "switch", "-C", "bad"]) is not None


# ── force-push / delete of a protected branch (AST-derived) ───────────────────
def test_force_push_protected_long_flag_blocked():
    assert gov.find_violations("git push --force origin main")
    assert gov.find_violations("git push --force origin dev")
    assert gov.find_violations("git push --force-with-lease origin master")


def test_force_push_protected_short_flag_blocked():
    assert gov.find_violations("git push -f origin main")


def test_force_push_protected_clustered_short_flag_blocked():
    # -fu (force + set-upstream short cluster) targeting a protected branch.
    assert gov.find_violations("git push -fu origin main")


def test_force_push_protected_plus_refspec_blocked():
    # The leading-'+' refspec is force syntax with no flag.
    assert gov.find_violations("git push origin +main")
    assert gov.find_violations("git push origin +dev")


def test_force_push_protected_colon_refspec_blocked():
    # HEAD:main destination is protected.
    assert gov.find_violations("git push --force origin HEAD:main")
    assert gov.find_violations("git push origin +HEAD:dev")


def test_delete_protected_branch_blocked():
    assert gov.find_violations("git push origin --delete main")
    assert gov.find_violations("git push --delete origin dev")
    assert gov.find_violations("git push origin -d master")


def test_force_push_own_feature_branch_allowed():
    # The legitimate address-review rebase-and-repush of the fixer's OWN branch.
    assert not gov.find_violations("git push --force origin fix/12-bug")
    assert not gov.find_violations("git push -f origin feat/3-x")
    assert not gov.find_violations("git push origin +fix/12-bug")
    assert not gov.find_violations("git push --force-with-lease origin chore/9-y")


def test_plain_push_to_protected_not_force_allowed():
    # A NON-force push to main is not a history-overwrite; the governor's
    # force-push guard must not fire (other gates/branch-protection handle policy).
    assert not gov.find_violations("git push origin main")
    assert not gov.find_violations("git push origin HEAD:dev")


# ── push destination refs (P1R-10) — the push gate's half of the same parse ───
# The stateful push gate used to key on the checked-out branch for EVERY push,
# so `git push origin HEAD:other` was checked against the wrong branch and a
# detached (mid-rebase) HEAD skipped the gate entirely. It now derives the
# destination from the argv through this shared parser, so the destructive-push
# guard and the gate can never disagree about what counts as a refspec.
def test_push_destination_refs_for_the_real_fixer_argvs():
    ref = gov.push_destination_refs
    # no refspec at all → the caller must fall back to the checked-out branch
    assert ref(["push"]) == ()
    assert ref(["push", "origin"]) == ()
    assert ref(["push", "-u", "origin"]) == ()
    # explicit destinations, in every shape the fixer actually emits
    assert ref(["push", "origin", "HEAD:refs/heads/fix/42-x"]) == ("fix/42-x",)
    assert ref(["push", "--force-with-lease", "origin", "fix/42-x"]) == ("fix/42-x",)
    assert ref(["push", "-u", "origin", "fix/42-x"]) == ("fix/42-x",)
    assert ref(["push", "origin", "+fix/42-x"]) == ("fix/42-x",)
    assert ref(["push", "origin", "--delete", "fix/42-x"]) == ("fix/42-x",)


def test_push_destination_refs_keeps_the_whole_branch_name():
    # `_basename` would truncate `fix/42-x` to `42-x`, which is_governed_branch
    # would then read as UNGOVERNED — the exact silent skip being closed.
    assert gov.push_destination_refs(
        ["push", "origin", "refs/heads/feat/7-thing"]
    ) == ("feat/7-thing",)


def test_push_destination_refs_returns_head_alias_verbatim():
    # Resolving `HEAD` against the checked-out branch is POLICY (domain/gate);
    # the parser reports what the argv said.
    assert gov.push_destination_refs(["push", "origin", "HEAD"]) == ("HEAD",)


def test_push_destination_refs_ignores_the_remote_slot_and_dedupes():
    assert gov.push_destination_refs(["push", "fix/42-x"]) == ()   # that's a remote
    assert gov.push_destination_refs(
        ["push", "origin", "fix/42-x", "fix/42-x", "feat/1-y"]
    ) == ("fix/42-x", "feat/1-y")


def test_force_push_no_refspec_deferred_to_check_push():
    # No explicit refspec → current branch is runtime state the governor cannot
    # see; defer to the stateful check-push.sh. The bypass guard must NOT fire.
    assert not gov.find_violations("git push --force")
    assert not gov.find_violations("git push -f")


def test_force_push_env_wrapped_protected_blocked():
    # AST unwrap of `env … git push --force origin main` still reaches the push.
    assert gov.find_violations("env GIT_X=1 git push --force origin main")


def test_bypass_husky_false_variants():
    assert gov.find_violations("HUSKY=false git commit -m x")
    assert gov.find_violations("OVERCOMMIT_DISABLE=1 git commit -m x")


def test_clean_commands_still_pass_after_hardening():
    # Regression guard: legitimate commands must NOT be flagged.
    assert gov.find_violations("git commit -m 'green: implement x'") == []
    assert gov.find_violations("git push origin feature-branch") == []
    assert gov.find_violations("git checkout -b fix/12-bug") == []
    assert gov.find_violations("env NODE_ENV=test pytest") == []


# ── verification-pass regressions (second adversarial review) ─────────────────
def test_bypass_double_env_wrapper():
    # `env env HUSKY=0 git commit` must be unwrapped to a fixed point.
    assert gov.find_violations("env env HUSKY=0 git commit -m x")
    assert gov.find_violations("env /usr/bin/env HUSKY=0 git commit -m x")


def test_bypass_passthrough_wrappers():
    # exec/command/builtin/nohup prefix a command transparently.
    assert gov.find_violations("exec git commit --no-verify -m x")
    assert gov.find_violations("command git commit --no-verify -m x")
    assert gov.find_violations("builtin git commit --no-verify -m x")
    assert gov.find_violations("nohup git commit --no-verify -m x")


def test_passthrough_then_env_then_git():
    assert gov.find_violations("exec env HUSKY=0 git commit -m x")


def test_git_push_dry_run_n_not_flagged():
    # `git push -n` is --dry-run, NOT --no-verify — must not be a false positive.
    assert gov.find_violations("git push -n origin main") == []
    # but the long form on push IS still blocked
    assert gov.find_violations("git push --no-verify origin main")


def test_passthrough_does_not_overmatch():
    # A passthrough wrapper in front of a NON-git command is harmless.
    assert gov.find_violations("exec pytest -n auto") == []
    assert gov.find_violations("time git status") == []


# ── indirect-execution regressions (M9 / R3-F8) ───────────────────────────────
# Runtime indirection is unresolvable by the static governor and must deny.
# Direct forms remain as anti-vacuity baselines for the smuggled payloads.
def test_direct_no_verify_is_blocked_anti_vacuity_baseline():
    # Baseline for every xfail below: the DIRECT form of the exact payload
    # each bypass smuggles through IS blocked today.
    assert gov.find_violations("git commit --no-verify -m x")


def test_sh_c_is_unresolvable_and_blocked():
    assert gov.find_violations("sh -c 'git commit --no-verify -m x'")


def test_bash_c_is_unresolvable_and_blocked():
    assert gov.find_violations("bash -c 'git commit --no-verify -m x'")


@pytest.mark.parametrize(
    "command",
    (
        "env bash -c 'git commit --no-verify -m x'",
        "timeout 5 bash -c 'git commit --no-verify -m x'",
        "sudo sh -c 'git commit --no-verify -m x'",
        "sudo -u root sh -c 'git commit --no-verify -m x'",
        "nice -n 5 bash -c 'git commit --no-verify -m x'",
        "stdbuf -o L git push --force origin main",
        "sudo --user root git push --force origin main",
    ),
)
def test_wrapped_dynamic_interpreters_are_unresolvable(command):
    assert gov.find_violations(command)


@pytest.mark.parametrize(
    "assignment",
    (
        "GH_HOST=attacker.example",
        "ISSUE_AGENT_GITHUB_HOST=attacker.example",
        "ISSUE_AGENT_AUTHORITY_BUNDLE=forged",
    ),
)
def test_durable_publication_host_authority_cannot_be_overridden(assignment):
    command = (
        f"env {assignment} issue-agent-publish "
        "gh pr create --title x --body y"
    )
    assert gov.find_violations(command)


def test_python_c_is_unresolvable_and_blocked():
    assert gov.find_violations(
        "python -c \"import os; os.system('git commit --no-verify -m x')\""
    )


def test_eval_is_unresolvable_and_blocked():
    assert gov.find_violations("eval 'git commit --no-verify -m x'")


def test_xargs_before_git_is_unresolvable_and_blocked():
    assert gov.find_violations("xargs git commit --no-verify -m x")


@pytest.mark.parametrize(
    "command",
    (
        'g() { git "$@"; }; g push origin main',
        'function g { command git "$@"; }; g commit --no-verify -m x',
        "alias g=git; g push origin main",
        "source ./helpers.sh; g push origin main",
        ". ./helpers.sh; g push origin main",
    ),
)
def test_runtime_command_indirection_is_unresolvable_and_blocked(command):
    resolution = gov.resolve(command)
    assert isinstance(resolution, gov.Unresolvable)
    assert gov.find_violations(command)


def test_even_benign_function_definition_is_not_misclassified_as_static():
    assert isinstance(
        gov.resolve("f() { echo ok; }; f"),
        gov.Unresolvable,
    )


# ── #113: read-only mode belongs to the LEG, not to the filesystem ───────────
# The reviewer's generated hook `export`s ISSUE_AGENT_REVIEW_READ_ONLY=1 at its
# own cwd. Fixer worktrees are nested under that cwd (TARGET_DIR/.claude/
# worktrees/issue-N), so a fixer leg resolves to the same hook and inherits the
# reviewer's sandbox: on 2026-07-29 the first codex review of the deployment
# blocked the next three fixer legs (#89/#92/#104) and PR #111 on git, tests,
# commit and push. The dispatching runner states the role; the role decides.
def _drive_main(monkeypatch, command: str, env: dict) -> int:
    """Run the hook's main() over one Bash payload; return its exit status."""
    import io
    import json as _json

    for key in ("ISSUE_AGENT_REVIEW_READ_ONLY", "ISSUE_AGENT_LEG_ROLE"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    payload = _json.dumps({"tool_input": {"command": command}})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    with pytest.raises(SystemExit) as exc:
        hook.main()
    return exc.value.code


# `gh pr review --approve` is denied by the review-only surface and is NOT
# otherwise governed, so it isolates the read-only decision by itself.
_REVIEW_ONLY_DENIED = "gh pr review 97 --approve"


def test_review_leg_keeps_its_read_only_sandbox(monkeypatch):
    assert _drive_main(
        monkeypatch, _REVIEW_ONLY_DENIED,
        {"ISSUE_AGENT_REVIEW_READ_ONLY": "1", "ISSUE_AGENT_LEG_ROLE": "review"},
    ) == 2


def test_read_only_without_a_declared_role_still_blocks(monkeypatch):
    """Fail CLOSED on the security boundary: a reviewer whose role env did not
    propagate keeps its sandbox rather than silently gaining a shell."""
    assert _drive_main(
        monkeypatch, _REVIEW_ONLY_DENIED, {"ISSUE_AGENT_REVIEW_READ_ONLY": "1"},
    ) == 2


def test_fix_leg_still_obeys_the_runtime_portable_gh_allowlist(monkeypatch):
    """The #113 regression: the flag is present (a nested fixer worktree ran the
    reviewer's hook), but this leg is a FIX, so the reviewer's surface must not
    apply to it."""
    assert _drive_main(
        monkeypatch, _REVIEW_ONLY_DENIED,
        {"ISSUE_AGENT_REVIEW_READ_ONLY": "1", "ISSUE_AGENT_LEG_ROLE": "fix"},
    ) == 2


def test_fix_role_does_not_disarm_the_ordinary_gates(monkeypatch):
    """Role only settles review-only mode. Everything the governor blocks for a
    fixer stays blocked — otherwise this fix would be a bypass switch."""
    assert _drive_main(
        monkeypatch, "git commit --no-verify -m x",
        {"ISSUE_AGENT_REVIEW_READ_ONLY": "1", "ISSUE_AGENT_LEG_ROLE": "fix"},
    ) == 2


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
