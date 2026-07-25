"""In-process PreToolUse(Bash) governor — replaces dispatch.sh + the checker
subprocess fan-out, kept as a Python `HookMatcher` callable for the Agent SDK.

Two layers, mirroring the gate's pure/impure split:

  * `evaluate_command(command, ctx)` — PURE. Reuses the security-critical shared
    governor policy (extract_commands_with_env / classify / bypass_reason — fail CLOSED)
    and routes each governed command to
    the matching pure rule in `domain/gate.py`, reading worktree facts from an
    injected `GateContext`. Fully unit-tested with a constructed GateContext, no
    git/fs.

  * `make_pretooluse_hook(build_ctx)` — the SDK callback. Builds a real
    GateContext from the hook's cwd (the git-backed builder is Phase 4) and
    returns the SDK deny shape on the first violation. Fail-OPEN on its own
    errors (the gate is a fast nudge; CI/review are the real boundary), but
    fail-CLOSED on a recognised bypass — same policy as the bash.

The bypass core is REUSED, never re-transcribed, so the (subtle, well-tested)
--no-verify / HUSKY=0 / core.hooksPath detection stays single-sourced.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..domain import gate
from ..domain.refs import is_governed_branch
from . import governor_policy


# ── worktree facts the gate rules need (injected; built from git in Phase 4) ──
@dataclass(frozen=True)
class GateContext:
    base_branch: str = "dev"
    branch: str = ""                         # current HEAD branch
    branch_subjects: tuple[str, ...] = ()    # commit subjects on branch since base
    base_resolvable: bool = True
    changed_files: tuple[str, ...] = ()      # diff vs base
    # Push-gate precondition, as a LAZY probe rather than a value (review
    # known-gap-2 / M7). Two properties this shape buys:
    #   * cost — this context is rebuilt for EVERY Bash call the agent makes and
    #     the answer costs a `gh` round-trip, so it is resolved only when a push
    #     is actually being gated (see `_push_gate`), never behind an `ls`.
    #   * honesty — the answer is the domain's tri-state, so "I could not look"
    #     stays distinguishable from "there is no PR". The old
    #     `has_open_pr: bool = False` collapsed both into the permissive value
    #     AND was never populated by `build_gate_context`, so every push to an
    #     open PR (every ADDRESS_REVIEW / FIX_CI leg) ran with NO gate at all on
    #     the SDK path. The default is therefore ERROR — "nobody told me" is an
    #     unknown, and an unknown fails closed; a builder that forgets to wire
    #     the probe now denies loudly instead of disabling the gate silently.
    #
    # It takes the DESTINATION BRANCH as its argument, not zero args: the branch
    # a push writes is derived from the push argv (P1R-10), so the branch the
    # question is about is only known at gate time, never at context-build time.
    open_pr: Callable[[str], gate.OpenPrLookup] = lambda _branch: gate.OpenPrLookup.ERROR
    # Delay between the probe's two attempts (`resolve_open_pr`). The retry
    # POLICY is pure/domain; the actual sleeping is the adapter's business, so it
    # is injected and defaults to a no-op (tests never wait).
    open_pr_retry_sleep: Callable[[], None] = lambda: None
    ac_posted: bool = False
    test_plan_posted: bool = False
    adversarial_verdict: str = ""
    visual_ac: bool = False                  # agent declared a visual criterion
    screenshots_read: tuple[str, ...] = ()   # evidence images the agent actually read
    harness_test_cmd: str | None = None
    env_test_cmd: str | None = None
    # Operator opt-out for a genuinely test-less repo (ALLOW_UNTESTED=1 in the
    # repo overlay). Read from the DAEMON's env, never the agent's command line —
    # the agent must not be able to grant itself the exemption.
    allow_untested: bool = False
    exists_at_base: Callable[[str], bool] = lambda f: False
    run_tests: Callable[[str], bool] = lambda c: True
    revert_and_run: Callable[[tuple[str, ...]], bool] = lambda impl: False
    red_must_fail: bool = True
    verify_red_at_commit: bool = True   # run the suite on a `red:` commit, require failure


def _extract_after_flag(argv: list[str], *flags: str) -> str:
    """Value following the first of `flags` in argv (e.g. -b/-c for branch,
    -m/--message for commit message). '' if absent."""
    for i, a in enumerate(argv):
        if a in flags and i + 1 < len(argv):
            return argv[i + 1]
        for f in flags:
            if a.startswith(f + "="):
                return a.split("=", 1)[1]
            if f.startswith("-") and not f.startswith("--") and a.startswith(f) and a != f:
                return a[len(f):]  # -mMessage
    return ""


def _argv_has(argv: list[str], flag: str) -> bool:
    return flag in argv


def _pr_gate(argv: list[str], ctx: GateContext) -> list[str]:
    """Full PR-creation gate (check-pr.sh) as a list of deny reasons (review M21).

    Ordered CHEAP structural checks (pure string/tuple logic, no subprocess)
    BEFORE the two EXPENSIVE checks that run the full TEST_CMD —
    `check_red_fails` also mutates the worktree via `revert_and_run` (a
    checkout-revert-run-restore cycle) — and short-circuits on the FIRST
    denial. The caller (`evaluate_command`) only ever reads `reasons[0]`, so
    returning a single-element list on first hit changes nothing it
    observes. Before this, a trivial denial (e.g. wrong --base) still paid up
    to 2×GATE_TEST_TIMEOUT_S (default 600s each) before the agent saw it,
    because every rule ran regardless of whether an earlier one had already
    failed. The cheap checks are still evaluated lazily (one at a time) so a
    later cheap check's own cost — negligible here, but the property is the
    point — never runs past an earlier denial either.
    """
    body = _extract_after_flag(argv, "--body", "-b")
    pr_base = _extract_after_flag(argv, "--base", "-B")

    cheap_checks: tuple[Callable[[], gate.GateResult], ...] = (
        lambda: gate.check_no_web(argv),
        lambda: gate.check_pr_base(pr_base, ctx.base_branch),
        lambda: gate.check_pr_body(body),
        lambda: gate.check_markers(ctx.ac_posted, ctx.test_plan_posted),
        # pr_body arms the per-AC-item screenshot rule (§7d); inline --body/-b
        # only — --body-file is handled by the bash §7d, a pre-existing
        # extractor limitation.
        lambda: gate.check_visual_evidence(
            ctx.visual_ac, ctx.changed_files, ctx.screenshots_read, body
        ),
        lambda: gate.check_required_test(ctx.branch, ctx.changed_files),
        lambda: gate.check_commit_log(ctx.branch_subjects),
        lambda: gate.check_no_evidence(ctx.changed_files),
        lambda: gate.check_tests_immutable(ctx.changed_files, ctx.exists_at_base),
        lambda: gate.check_adversarial_verdict(ctx.adversarial_verdict),
    )
    for check in cheap_checks:
        result = check()
        if not result.ok:
            return [result.reason]

    # ── expensive: both run the full TEST_CMD; never reached above a cheap denial ──
    test_cmd = gate.resolve_test_cmd(ctx.harness_test_cmd, ctx.env_test_cmd)
    tests_result = gate.check_tests(test_cmd, ctx.run_tests, allow_untested=ctx.allow_untested)
    if not tests_result.ok:
        return [tests_result.reason]

    new_tests, impl = gate.partition_changed(ctx.changed_files, ctx.exists_at_base)
    red_fail_result = gate.check_red_fails(
        gate.RedFailInputs(
            test_cmd=test_cmd,
            new_test_files=new_tests,
            impl_files=impl,
            red_must_fail=ctx.red_must_fail,
            base_resolvable=ctx.base_resolvable,
            allow_untested=ctx.allow_untested,
        ),
        ctx.revert_and_run,
    )
    if not red_fail_result.ok:
        return [red_fail_result.reason]
    return []


def _push_gate_for_branch(branch: str, ctx: GateContext) -> list[str]:
    """The push gate (check-push.sh) for ONE destination branch.

    The open-PR lookup is resolved HERE — this is the only place that pays for
    the `gh` round-trip, because it is the only place whose decision depends on
    it (review known-gap-2: the context is rebuilt per Bash call, so an eager
    probe would put a GitHub call behind every command the agent runs). It is
    asked about `branch` — the ref being WRITTEN — because that is the ref a PR
    is attached to.

    Order:
      1. ungoverned destination → not our business, and never call gh;
      2. resolve the lookup (retry once — a transient blip is the common case);
      3. mode: ENFORCE (open PR) / UNVERIFIED (lookup failed) both run the
         state-derived checks; SKIP (no PR) leaves the pre-PR TDD phase free;
      4. UNVERIFIED with nothing checkable at all (unresolvable base) fails
         CLOSED via `check_push_precondition`; a UNVERIFIED denial is annotated
         so the agent knows the rule it failed is real.
    """
    if not is_governed_branch(branch):
        return []
    lookup = gate.resolve_open_pr(
        lambda: ctx.open_pr(branch), sleep=ctx.open_pr_retry_sleep
    )
    mode = gate.push_gate_mode(branch, lookup)
    if mode is gate.PushGateMode.SKIP:
        return []
    precondition = gate.check_push_precondition(
        branch, lookup, checks_runnable=ctx.base_resolvable
    )
    if not precondition.ok:
        return [precondition.reason]
    if not ctx.base_resolvable:
        return []
    reasons: list[str] = []

    def chk(result: gate.GateResult) -> None:
        if not result.ok:
            reasons.append(result.reason)

    test_cmd = gate.resolve_test_cmd(ctx.harness_test_cmd, ctx.env_test_cmd)
    chk(gate.check_tests(test_cmd, ctx.run_tests, allow_untested=ctx.allow_untested))
    chk(gate.check_commit_log(ctx.branch_subjects))
    chk(gate.check_no_evidence(ctx.changed_files))
    chk(gate.check_tests_immutable(ctx.changed_files, ctx.exists_at_base))
    if reasons and mode is gate.PushGateMode.UNVERIFIED:
        return [gate.unverified_push_denial(reasons[0], branch)]
    return reasons


def _push_gate(push_argv: list[str], ctx: GateContext) -> list[str]:
    """Push gate, keyed on the branch(es) the push actually WRITES.

    It used to key on `ctx.branch` (the worktree's checked-out branch) for every
    push, which is not the ref being written (P1R-10). The destinations come from
    the same AST parse the protected-branch guard uses
    (`governor_policy.push_destination_refs`); resolving them against the
    checked-out branch — including the "detached HEAD, no refspec ⇒ unknown"
    verdict — is pure policy and lives in the domain.

    KNOWN BOUND (unchanged here): `git -C /other/worktree push` is still checked
    against THIS worktree's facts (subjects/diff/markers), because the context is
    built once from the hook's cwd. The destination is now right; the state it is
    checked against can still be the wrong worktree's. Likewise `sh -c 'git push
    …'` classifies as nothing and never reaches this gate at all (M9/R3-F8)."""
    targets = gate.resolve_push_targets(
        governor_policy.push_destination_refs(push_argv), ctx.branch
    )
    known = gate.check_push_target(targets)
    if not known.ok:
        return [known.reason]
    assert targets is not None  # check_push_target denies exactly the None case
    for branch in targets:
        reasons = _push_gate_for_branch(branch, ctx)
        if reasons:
            return reasons
    return []


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str = ""


def evaluate_command(command: str, ctx: GateContext) -> Verdict:
    """Decide whether a Bash command is allowed. Bypass detection first (fail
    CLOSED), then route each governed command to its gate. Returns the FIRST
    violation's reason (the hook can only deny once)."""
    # 1) Bypass detection across every command node (reuse the bash core).
    bypass = governor_policy.find_violations(command)
    if bypass:
        return Verdict(False, f"Blocked: {bypass[0]}")

    # 2) Route governed commands to gate checks.
    for words, _env in governor_policy.extract_commands_with_env(command):
        g = governor_policy.classify(words)
        if g is None:
            continue
        if g.kind == "git-branch":
            name = _extract_after_flag(list(g.checker_args), "-b", "-c")
            res = gate.check_branch_name(name)
            if not res.ok:
                return Verdict(False, res.reason)
        elif g.kind == "git-commit":
            msg = _extract_after_flag(list(g.checker_args), "-m", "--message")
            res = gate.check_commit_message(msg)
            if not res.ok:
                return Verdict(False, res.reason)
            order = gate.check_commit_order(msg, ctx.branch_subjects, ctx.base_resolvable)
            if not order.ok:
                return Verdict(False, order.reason)
            # Fail-fast: a `red:` commit's tests must actually fail (run on the
            # working tree now). §10 at PR time remains the committed-state backstop.
            test_cmd = gate.resolve_test_cmd(ctx.harness_test_cmd, ctx.env_test_cmd)
            red = gate.check_red_is_red(
                msg, test_cmd, ctx.run_tests, ctx.verify_red_at_commit
            )
            if not red.ok:
                return Verdict(False, red.reason)
        elif g.kind == "gh-pr-create":
            reasons = _pr_gate(list(g.checker_args), ctx)
            if reasons:
                return Verdict(False, f"PR blocked: {reasons[0]}")
        elif g.kind == "git-push":
            reasons = _push_gate(list(g.checker_args), ctx)
            if reasons:
                return Verdict(False, f"Push blocked: {reasons[0]}")
        # git-config: bypass-only (handled above).
    return Verdict(True)


# ── SDK hook wiring ───────────────────────────────────────────────────────────
def _deny(reason: str) -> dict:
    """The SDK PreToolUse deny shape; reason is shown to the model as the tool
    result so it understands why and can fix in-session."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _run_edit_check(command: str, cwd: str) -> tuple[bool, str]:
    """Run the edit-time check command in `cwd`. Returns (ok, combined_output).
    Default runner (subprocess); injectable in tests. Never raises — a spawn
    failure is reported as ok=True (fail-open) by the caller."""
    import subprocess

    proc = subprocess.run(
        command, shell=True, cwd=cwd or None,
        capture_output=True, text=True, timeout=120,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, out


def make_posttooluse_hook(
    edit_check_cmd: str,
    run_check: Callable[[str, str], tuple[bool, str]] = _run_edit_check,
):
    """Build the PostToolUse(Edit|Write|…) HookCallback. After an edit-class tool
    call, run `edit_check_cmd` (a fast typecheck/lint) in the edit's cwd; on
    failure, BLOCK with the command output so the agent sees the error and fixes
    in-session — the deterministic-first gate that catches type errors before
    commit/CI. Fail-OPEN on any hook error (a broken check must never wedge the
    agent; commit/CI/review remain the real boundary). Empty command → no-op."""

    async def hook(input_data: Any, tool_use_id: Any, context: Any) -> dict:
        if not edit_check_cmd:
            return {}
        try:
            cwd = input_data.get("cwd", "") or os.getcwd()
            ok, output = run_check(edit_check_cmd, cwd)
        except Exception:
            return {}  # fail OPEN — never wedge the agent over a check spawn failure
        if ok:
            return {}
        tail = output.strip()[-3000:]  # keep the feedback bounded
        return {
            "decision": "block",
            "reason": (
                f"Edit check failed (`{edit_check_cmd}`). Fix these before continuing:\n\n"
                f"{tail}"
            ),
        }

    return hook


_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def _record_screenshot_read(cwd: str, name: str) -> None:
    """Append a read screenshot's basename to <cwd>/.harness/screenshots-read."""
    hdir = os.path.join(cwd, ".harness")
    os.makedirs(hdir, exist_ok=True)
    with open(os.path.join(hdir, "screenshots-read"), "a") as f:
        f.write(name + "\n")


def make_read_record_hook(
    record: Callable[[str, str], None] = _record_screenshot_read,
):
    """Build the PostToolUse(Read) HookCallback. When the agent Reads an evidence
    screenshot (an image under `evidence/`), it has loaded the pixels into its own
    vision context — record that so the PR gate can require captured screenshots were
    actually LOOKED AT (check_visual_evidence). Observe-only: never blocks, fail-open.
    The record side-effect is injected so it's faked in tests."""

    async def hook(input_data: Any, tool_use_id: Any, context: Any) -> dict:
        try:
            tool_input = input_data.get("tool_input") or {}
            path = tool_input.get("file_path", "") or ""
            low = path.lower()
            # Only evidence images count as screenshot-verification.
            if "evidence/" in low.replace(os.sep, "/") and low.endswith(_IMAGE_EXTS):
                cwd = input_data.get("cwd", "") or os.getcwd()
                record(cwd, os.path.basename(path))
        except Exception:
            return {}  # observe-only, never wedge the agent
        return {}

    return hook


_ISSUE_IN_CWD = re.compile(r"issue-(\d+)")


def _observe_allowed_commit(command: str, cwd: str, log: Callable[[str], None]) -> None:
    """When a `git commit` is about to run (and passed the gate), emit a live
    breadcrumb of its TDD milestone (`red:`/`green:`/`docs:`/…). This is the only
    seam that sees per-commit progress — the whole resolve is one in-session run,
    so without this the fixer log is silent between dispatch and PR. Observe-only;
    never raises, never affects the verdict."""
    try:
        for words, _env in governor_policy.extract_commands_with_env(command):
            g = governor_policy.classify(words)
            if g is not None and g.kind == "git-commit":
                msg = _extract_after_flag(list(g.checker_args), "-m", "--message")
                m = _ISSUE_IN_CWD.search(cwd)
                who = f"#{m.group(1)}" if m else "?"
                log(f"commit {who}: {msg.strip()[:80]}")
    except Exception:
        pass


def _cmd_head(command: str, n: int = 120) -> str:
    """First line of a command, truncated — for a one-line diagnostic that never
    dumps a multi-KB heredoc into the log."""
    head = command.strip().splitlines()[0] if command.strip() else ""
    return head[:n] + ("…" if len(head) > n else "")


def make_pretooluse_hook(
    build_ctx: Callable[[str], GateContext],
    log: Callable[[str], None] = lambda _m: None,
    debug: bool = False,
):
    """Build the PreToolUse(Bash) HookCallback. `build_ctx(cwd) -> GateContext`
    is the git/fs-backed fact gatherer (Phase 4); injected so this stays pure and
    testable. `log` receives a live breadcrumb for each allowed `git commit`
    (red:/green:/docs: milestones) so a long in-session run isn't a silent gap.
    When `debug`, every Bash command the agent attempts is logged with its
    verdict, and a swallowed gate exception is logged with its type — turning the
    fail-open path from a blind spot into a trace (ISSUE_AGENT_DEBUG=1).
    Returns an async callable matching the SDK HookCallback signature."""

    async def hook(input_data: Any, tool_use_id: Any, context: Any) -> dict:
        command = ""
        try:
            tool_input = input_data.get("tool_input") or {}
            # Reject backgrounded Bash. `run_in_background=true` spawns an SDK
            # background TASK; in SDK 0.2.110 the main turn then ends with
            # terminal_reason="background_requested" and NO terminal ResultMessage,
            # so our single-shot runner sees "no ResultMessage" ($0.00) and loses the
            # whole run (observed persistently on the reviewer — see
            # docs/issues/review-no-resultmessage-background-tasks.md). These agents
            # are single-shot and never need async background work, so deny it
            # unconditionally — before (and independent of) the command-content gate.
            if tool_input.get("run_in_background"):
                if debug:
                    log("gate: DENY background Bash (run_in_background=true)")
                return _deny(
                    "background execution is disabled for this agent — run the "
                    "command in the foreground (omit run_in_background) so the "
                    "session returns a result."
                )
            command = tool_input.get("command", "")
            if not command:
                return {}
            # A MISSING cwd must DENY, never fall back to os.getcwd() (P1R-17).
            # The daemon's own cwd is TARGET_DIR, checked out on dev/main — an
            # ungoverned branch — so building the context from it silently
            # disables the push gate (and every other worktree-derived rule)
            # instead of failing. `cwd` is a required field of the SDK's
            # PreToolUse hook input, so this arm is unreachable in a healthy
            # session; if it ever fires, the gate has lost its subject and the
            # only honest verdict is "I cannot check this".
            cwd = input_data.get("cwd", "") or ""
            if not cwd:
                if debug:
                    log(f"gate: DENY (no cwd in hook input) {_cmd_head(command)}")
                return _deny(
                    "the gate could not determine this command's working directory "
                    "(no `cwd` in the hook input), so it cannot read the worktree it "
                    "is supposed to check. Refusing rather than checking the wrong "
                    "directory — retry the command; if it persists, this is a harness "
                    "bug to report, not something to work around."
                )
            ctx = build_ctx(cwd)
            verdict = evaluate_command(command, ctx)
        except Exception as exc:
            # Fail OPEN on our own errors — never wedge the agent over a bug in
            # the gate. (Bypass detection inside evaluate_command fails closed.)
            # But SURFACE it under debug: a silently-swallowed gate crash was a
            # blind spot when diagnosing wedges.
            if debug:
                log(f"gate: FAIL-OPEN on {_cmd_head(command)!r} — "
                    f"{type(exc).__name__}: {exc}")
            return {}
        if debug:
            log(f"gate: {'ALLOW' if verdict.allowed else 'DENY'} {_cmd_head(command)}"
                + (f" — {verdict.reason}" if not verdict.allowed else ""))
        if not verdict.allowed:
            return _deny(verdict.reason)
        _observe_allowed_commit(command, cwd, log)  # observe-only, post-gate
        return {}

    return hook


def build_hooks(
    build_ctx: Callable[[str], GateContext],
    edit_check_cmd: str | None = None,
    log: Callable[[str], None] = lambda _m: None,
    debug: bool = False,
) -> dict:
    """Convenience: the hooks dict AgentRunner wants. Always registers the
    PreToolUse(Bash) governor and a PostToolUse(Read) screenshot-read recorder;
    additionally registers a PostToolUse(Edit|Write|…) typecheck hook when
    `edit_check_cmd` is set (deterministic edit-time gate). `log` receives a live
    breadcrumb per allowed commit (red:/green:/docs:); `debug` additionally traces
    every command + verdict. Imports the SDK lazily."""
    from claude_agent_sdk import HookMatcher

    post: list = [HookMatcher(matcher="Read", hooks=[make_read_record_hook()])]
    if edit_check_cmd:
        post.append(
            HookMatcher(
                matcher="Edit|Write|MultiEdit|NotebookEdit",
                hooks=[make_posttooluse_hook(edit_check_cmd)],
            )
        )
    return {
        "PreToolUse": [
            HookMatcher(matcher="Bash", hooks=[make_pretooluse_hook(build_ctx, log, debug)])
        ],
        "PostToolUse": post,
    }
