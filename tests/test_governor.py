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

from issue_agent.adapters import governor_policy as gov  # noqa: E402
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


# ── KNOWN OPEN HOLE: indirect-execution bypass (M9 / R3-F8) ───────────────────
# `classify()` only recognises a governed command when the FIRST word of a
# `command` AST node (after `env`/passthrough-wrapper unwrapping, see
# `_strip_env_wrapper`) is `git` or `gh`. `sh -c '<payload>'`, `bash -c
# '<payload>'`, `python -c '<payload>'`, `eval '<payload>'`, and `xargs <cmd>
# <args>` all put the real command's name in a position `classify` never
# looks: as the second word of the outer node (`sh`/`bash`/`eval`/`xargs`), or
# buried inside an opaque interpreter-language string that tree-sitter-bash
# parses as a single string/raw_string leaf, never as a nested `command` node
# with its own child commands. `extract_commands_with_env` therefore reports
# `kinds=[]` for the whole line and `find_violations` returns `[]` — the gate
# is not merely weak against these, it is never consulted at all.
#
# This is NOT a parser bug: `governor_policy.py` is the exact same module the
# shell hook (`bash_governor.py`, see `test_shell_hook_reuses_shared_policy_
# functions` above) and the in-process SDK/Codex adapter both import — the
# AST parsing and bypass-string detection that this file spends 300+ lines
# testing are sound. The hole is structural: an indirection layer (a shell
# invoked with `-c`, an `eval`, a language interpreter, an argument-forwarder
# like `xargs`) that classify()'s single-first-word heuristic was never
# designed to see through. Closing it (backlog P3.2) requires either
# recursively re-parsing `-c`/`eval` string payloads as bash and unwrapping
# `xargs`'s leading non-flag argument as the real command name, or an
# allow-list stance (govern-by-default, not bypass-by-default).
#
# Blast radius: the fixer runs the SDK agent with `permission_mode=
# bypassPermissions` (see adapters/governor.py + app/main.py), where this
# governor hook plus `disallowed_tools` are the ONLY two policy layers left —
# there is no allow-list to fall back on. Any of these five vectors lets a
# fixer session commit with `--no-verify`, push straight past the push gate,
# or write `core.hooksPath`, with the governor never even entering the
# decision (not "misclassifies" — never called).
#
# Each bypass below is paired with a PASSING test proving the governor DOES
# block the exact same payload in its direct form — the anti-vacuity check
# that rules out an xfail merely encoding a typo'd payload nothing would ever
# catch. `strict=True` means the day someone closes this hole, the xfail
# flips to a hard failure here, forcing this record to be updated rather than
# silently going stale.
def test_direct_no_verify_is_blocked_anti_vacuity_baseline():
    # Baseline for every xfail below: the DIRECT form of the exact payload
    # each bypass smuggles through IS blocked today.
    assert gov.find_violations("git commit --no-verify -m x")


@pytest.mark.xfail(
    strict=True,
    reason="M9/R3-F8: `sh -c '<payload>'` puts the payload in an opaque "
    "string leaf; classify() never sees a nested `git` command, so "
    "`sh -c 'git commit --no-verify -m x'` skips the gate entirely.",
)
def test_sh_c_bypasses_the_no_verify_block():
    assert gov.find_violations("sh -c 'git commit --no-verify -m x'")


@pytest.mark.xfail(
    strict=True,
    reason="M9/R3-F8: same as sh -c — `bash -c '<payload>'` is an opaque "
    "string leaf to the parser, so `bash -c 'git commit --no-verify -m x'` "
    "skips the gate entirely.",
)
def test_bash_c_bypasses_the_no_verify_block():
    assert gov.find_violations("bash -c 'git commit --no-verify -m x'")


@pytest.mark.xfail(
    strict=True,
    reason="M9/R3-F8: `python -c '<payload>'` hides the git invocation "
    "inside a language-interpreter string classify() never inspects, so "
    "`python -c \"...os.system('git commit --no-verify -m x')\"` skips the "
    "gate entirely.",
)
def test_python_c_bypasses_the_no_verify_block():
    assert gov.find_violations(
        "python -c \"import os; os.system('git commit --no-verify -m x')\""
    )


@pytest.mark.xfail(
    strict=True,
    reason="M9/R3-F8: `eval '<payload>'` re-executes its string argument as "
    "shell at runtime, but statically it is one opaque string leaf, so "
    "`eval 'git commit --no-verify -m x'` skips the gate entirely.",
)
def test_eval_bypasses_the_no_verify_block():
    assert gov.find_violations("eval 'git commit --no-verify -m x'")


@pytest.mark.xfail(
    strict=True,
    reason="M9/R3-F8: `xargs <cmd> <args>` puts `git` as the SECOND word of "
    "the command node (first is `xargs`), which classify()'s "
    "first-word-only check never looks past, so `xargs git commit "
    "--no-verify -m x` skips the gate entirely.",
)
def test_xargs_bypasses_the_no_verify_block():
    assert gov.find_violations("xargs git commit --no-verify -m x")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
