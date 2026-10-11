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
    GateContext from the hook's cwd using the git-backed builder and
    returns the SDK deny shape on the first violation. Fail-OPEN on its own
    errors (the gate is a fast nudge; CI/review are the real boundary), but
    fail-CLOSED on a recognised bypass — same policy as the bash.

The bypass core is REUSED, never re-transcribed, so the (subtle, well-tested)
--no-verify / HUSKY=0 / core.hooksPath detection stays single-sourced.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from ..domain import gate
from ..domain.contracts import TestExecution, TestExecutionStatus
from ..domain.exemptions import (
    ExemptionDeclaration,
    RULE_REQUIRED_TEST,
    declared_subjects,
)
from ..domain.refs import is_governed_branch
from ..domain import governor_policy


def _no_adrs() -> tuple[gate.Adr, ...]:
    """Default `GateContext.load_adrs`: no ADRs ⇒ the §9 citation rule is inert.

    A NAMED function, not a lambda, so `complete_gate_context` can distinguish
    "the context builder never wired this" (fill it in from the worktree) from
    "a caller deliberately injected a loader that finds nothing" (leave it
    alone). An identity check against a lambda default would work too, but only
    by accident of where the lambda was written."""
    return ()


def _no_check(_cmd: str) -> bool:
    """Default `GateContext.run_check`: nothing runs, nothing fails. Inert, and
    only ever consulted when `lint_cmd`/`typecheck_cmd` are non-empty."""
    return True


def _unbound_test_execution(_cmd: str) -> TestExecution:
    """Fail closed when a context omits the trusted red-test executor."""
    return TestExecution(
        TestExecutionStatus.INFRA_ERROR,
        ("unbound",),
        127,
        0,
        "",
        "",
        "unbound",
        "unknown",
    )


# ── worktree facts the gate rules need (injected; built from git) ─────────────
@dataclass(frozen=True)
class GateContext:
    push_lookup_policy: gate.PushLookupPolicy = gate.PushLookupPolicy.CHECK_AVAILABLE_FACTS
    instruction_blobs: Callable[[bool], tuple[tuple[str, str], ...] | None] = lambda staged: ()
    commit_image_sizes: Callable[[], tuple[tuple[str, int], ...] | None] = lambda: ()
    image_limit_bytes: int = 700 * 1024
    policy_input_error: str | None = None
    base_branch: str = "dev"
    repository: str = ""                     # deployment repo (owner/name)
    branch: str = ""                         # current HEAD branch
    history_available: bool = True
    branch_diff_available: bool = True
    branch_subjects: tuple[str, ...] = ()    # commit subjects on branch since base
    # Subjects already on the observed remote PR branch (base..origin/<branch>);
    # published history the commit-log gate must not re-police (keifu #182).
    published_subjects: tuple[str, ...] = ()
    # #124 exemption declarations from Gate-Exemption commit trailers on the
    # branch. A declaration only makes the violation visible (the merge rule
    # withholds until REVIEW_BOT grants); () = none declared.
    declared_exemptions: tuple[ExemptionDeclaration, ...] = ()
    # Lines that LOOK like Gate-Exemption trailers but failed to parse
    # (typo'd rule, out-of-charset subject like `@scope/pkg`). They are
    # rightly inert, but silently so — the denial hint below keeps a typo
    # from recreating the #126 comply-how loop.
    malformed_exemption_lines: int = 0
    # OLD-side paths of modified/deleted/renamed files. Kept apart from
    # `changed_files` (new-side): a deleted test path there would read as an
    # ADDED test and satisfy check_required_test. Only the tests-immutable
    # check consults it, so a rename out of the test tree is still an
    # exemption request (round-4 adversarial review + parity check).
    touched_at_base: tuple[str, ...] = ()
    # Modified files whose branch diff adds lines only (numstat deletions 0).
    # Only check_tests_immutable consults it: a purely additive edit to an
    # existing test is refused at commit time (the reviewer refuses those
    # grants on policy — 2026-08-09 tauri batch, 4 paid rounds). () = the
    # builder could not tell → pre-existing reviewer-caught behavior.
    additive_only: tuple[str, ...] = ()
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
    harness_test_cmd: str | None = None
    env_test_cmd: str | None = None
    # Operator opt-out for a genuinely test-less repo (ALLOW_UNTESTED=1 in the
    # repo overlay). Read from the DAEMON's env, never the agent's command line —
    # the agent must not be able to grant itself the exemption.
    allow_untested: bool = False
    exists_at_base: Callable[[str], bool] = lambda f: False
    run_tests: Callable[[str], bool | None] = lambda c: True
    run_red_tests: Callable[[str], TestExecution] = _unbound_test_execution
    revert_and_run: Callable[[tuple[str, ...]], TestExecution] = lambda impl: TestExecution(
        TestExecutionStatus.INFRA_ERROR,
        ("unbound",),
        127,
        0,
        "",
        "",
        "unbound",
        "unknown",
    )
    red_must_fail: bool = True
    verify_red_at_commit: bool = True   # run the suite on a `red:` commit, require failure
    durable_publish: bool = False
    # ── the two rules ported from bash in wave P1-2 (review M6) ───────────────
    # The worktree the gate is judging. Filled by `complete_gate_context` from
    # the hook's cwd; "" means "nobody said", which makes the worktree-derived
    # facts below inert rather than wrong.
    worktree_dir: str = ""
    # Commit-time fast gates (check-commit.sh:54-59). OPERATOR facts read from
    # the daemon's environment — exactly the environment the bash checker
    # inherits — never from the agent's command line.
    lint_cmd: str = ""
    typecheck_cmd: str = ""
    # Runs a fast project command (lint/typecheck) in the worktree → passed?
    # Separate from `run_tests` because these are commit-hook-fast by contract
    # and must not inherit the test suite's timeout budget. Default is inert.
    run_check: Callable[[str], bool] = _no_check
    # ADR facts, as a LAZY loader for the same reason `open_pr` is one: this
    # context is rebuilt for EVERY Bash call, and reading docs/adr/ is a
    # directory walk plus N file reads. Only `_pr_gate` ever calls it.
    load_adrs: Callable[[], tuple[gate.Adr, ...]] = _no_adrs


# ── worktree/env facts for the two rules ported from bash (review M6) ─────────
# `docs/adr/[0-9]*.md`, the same glob check-pr.sh §9 uses.
_ADR_GLOB = "[0-9]*.md"
# A single ADR is a short markdown file; this bounds a pathological one (and a
# non-ADR file that happens to match the glob) without truncating any real one.
_ADR_MAX_BYTES = 64 * 1024


def read_adrs(adr_dir: str) -> tuple[gate.Adr, ...]:
    """Load `docs/adr/[0-9]*.md` into the domain's `Adr` values, sorted by
    filename (the bash glob's order, so both stacks report the SAME first
    offending ADR when a diff violates several).

    Never raises: a missing directory, an unreadable file, or a permission error
    yields fewer ADRs, never an exception — this runs inside the PreToolUse hook,
    whose only other option is to fail open on the whole gate."""
    import glob

    adrs: list[gate.Adr] = []
    try:
        # `glob.escape` on the DIRECTORY only: a worktree path containing `[` or
        # `?` would otherwise be read as part of the pattern and silently match
        # nothing — i.e. the rule would go quiet instead of failing.
        paths = sorted(glob.glob(os.path.join(glob.escape(adr_dir), _ADR_GLOB)))
    except OSError:
        return ()
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read(_ADR_MAX_BYTES)
        except OSError:
            continue
        adrs.append(gate.parse_adr(os.path.basename(path), text))
    return tuple(adrs)


# Commit-time lint/typecheck must be commit-hook-fast by contract (check-commit.sh
# runs them synchronously inside a PreToolUse hook). This is a hard ceiling, not a
# budget: a lint that hangs would otherwise hold the agent's whole turn open.
_CHECK_TIMEOUT_S = 120.0


# ── Guarded execution of TARGET-OWNED commands (TEST_CMD / LINT_CMD /
# TYPECHECK_CMD / EDIT_CHECK_CMD) ──────────────────────────────────────────────
#
# Every place the harness executes a command it got from configuration or from
# an agent-written marker MUST go through `run_target_command` (python) or its
# shell launcher (`run_target_test_cmd` in bin/point-at.sh). The advisory shell
# gates and their compatibility wrapper invoke this Python executor directly. The 2026-07-29 self-hosting fork
# bomb (#63) is why: such a command can — via env inheritance or a
# `.harness/test-cmd` marker — re-enter these very gates and re-run itself
# without bound. Three defenses, all of which live HERE so no exec site can
# forget one:
#
#   1. env scrub: the child env drops every deployment knob (the target owns
#      its own configuration);
#   2. re-entrancy bound: the child env is stamped with HARNESS_GATE_DEPTH and
#      executing at depth ≥ 2 is refused (None). The counter lives in the env
#      by design — simple and observable; a command that wraps itself in
#      `env -i` erases it, which doctor warns about (documented edge, not
#      hidden machinery);
#   3. containment: the child runs in its own session and a timeout kills the
#      whole process group, not just the direct shell.

# Static floor. Single python source of truth (worktree.py and
# tests/conftest.py import it); the two bash twins keep hand copies and
# tests/test_deployment_env_sanitization.sh asserts the three lists are
# identical so they cannot drift silently. The deployment's FULL knob set
# additionally arrives at runtime via HARNESS_DEPLOY_KNOBS (a name manifest
# compose-env.sh appends to the composed env), so a newly added knob is
# scrubbed without anyone editing these lists.
TARGET_CMD_SCRUB_KNOBS = (
    "TEST_CMD", "LINT_CMD", "TYPECHECK_CMD", "EDIT_CHECK_CMD", "ALLOW_UNTESTED",
    "RED_MUST_FAIL", "VERIFY_RED_AT_COMMIT", "BASE_BRANCH",
    "GATE_TEST_TIMEOUT_S", "GATE_TEST_FAILURE_EXIT_CODES",
    "GATE_GH_RETRY_SLEEP", "HARNESS_SCREENSHOTS_READ",
    "GH_APP_ID",
    "GH_APP_KEY_FILE", "GH_APP_INSTALL_ID", "TARGET_DIR", "AGENT_ENV",
    "ISSUE_AGENT_DEPLOYMENT_ID",
)


def target_cmd_scrub_set(environment: dict[str, str] | None = None) -> set[str]:
    """Every env name to drop from a target command's child env: the static
    floor plus the deployment manifest (HARNESS_DEPLOY_KNOBS) and the manifest
    var itself."""
    values = os.environ if environment is None else environment
    manifest = values.get("HARNESS_DEPLOY_KNOBS", "").split()
    return set(TARGET_CMD_SCRUB_KNOBS) | set(manifest) | {"HARNESS_DEPLOY_KNOBS"}


def gate_depth() -> int:
    """Current re-entrancy depth from the HARNESS_GATE_DEPTH env counter.

    Garbled values read as 0 — fail toward running once, never toward
    disabling the gate."""
    raw = os.environ.get("HARNESS_GATE_DEPTH", "0")
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def target_command_inputs(cmd: str, environment: dict[str, str], depth: int) -> tuple[tuple[str, ...], dict[str, str]]:
    if depth >= 2:
        raise ValueError("target command recursion limit")
    env = {key: value for key, value in environment.items() if key not in target_cmd_scrub_set(environment)}
    env["HARNESS_GATE_DEPTH"] = str(depth + 1)
    return ("bash", "-c", cmd), env


def run_target_command(
    cmd: str, cwd: str, timeout: float, capture: bool = False
) -> tuple[int | None, str]:
    """Execute a target-owned command under the three #63 defenses.

    Returns (exit_code, output). exit_code is None ⇔ the run was REFUSED
    because it would re-enter the gate — callers must fail CLOSED on None and
    say why (a refusal is not a red suite; conflating them sends the agent
    chasing phantom test failures). A timeout kills the process group and
    reports 124. `output` is "" unless capture=True. Never raises."""
    depth = gate_depth()
    if depth >= 2:
        print(
            f"[gate] GATE RECURSION: refusing to run a target command at gate "
            f"depth {depth} — it re-entered the harness gate (self-referential "
            f"TEST_CMD/.harness/test-cmd, see #63): {cmd}",
            file=sys.stderr,
        )
        return None, ""
    argv, env = target_command_inputs(cmd, dict(os.environ), depth)
    pipe = subprocess.PIPE if capture else None
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd or None, env=env, start_new_session=True,
            stdout=pipe, stderr=pipe, text=capture or None,
        )
    except OSError as e:
        return 127, str(e) if capture else ""
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, ((out or "") + (err or "")) if capture else ""
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.communicate(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass
        print(
            f"[gate] target command timed out after {timeout:g}s; its process "
            f"group was killed (reported as FAILING, not a pass): {cmd}",
            file=sys.stderr,
        )
        return 124, ""


def _run_project_check(cmd: str, cwd: str) -> bool:
    """Run a project lint/typecheck command in `cwd` → did it pass?

    A timeout, spawn failure, or gate-recursion refusal reports FALSE (did not
    pass), matching the bash's `eval "$LINT_CMD" || fail`: a check that cannot
    complete has not passed. Never raises — `evaluate_command` runs under a
    broad fail-OPEN except, so an escaping exception would silently ALLOW the
    commit."""
    rc, _ = run_target_command(cmd, cwd, _CHECK_TIMEOUT_S)
    return rc == 0


def complete_gate_context(
    ctx: GateContext,
    cwd: str,
    env: dict[str, str] | None = None,
    read_adrs_fn: Callable[[str], tuple[gate.Adr, ...]] = read_adrs,
    run_check_fn: Callable[[str, str], bool] = _run_project_check,
) -> GateContext:
    """Fill in the facts the two ported rules (review M6) need, from the hook's
    cwd and the daemon's environment.

    Why here and not in `build_gate_context`: those are worktree/env facts the
    git-backed builder does not currently gather, and the SDK hook is the one
    place that provably knows the worktree (it DENIES when `cwd` is missing —
    P1R-17). Every field is filled only when the builder left it at its inert
    default, so a caller that wires them explicitly (tests, a future
    `build_gate_context` that takes them as parameters) is never overridden. When
    `build_gate_context` grows these parameters this function collapses to
    nothing and can be deleted — the sentinel checks make that a no-op change.

    `LINT_CMD`/`TYPECHECK_CMD` come from the process environment because that is
    literally the same source the bash checker reads: check-commit.sh is spawned
    by the daemon and inherits its env, and on the SDK path the governor IS the
    daemon process. They are operator knobs (`config/agent.env.example`), never
    agent-settable — the agent's own `KEY=val cmd` prefixes are recovered by
    `governor_policy` for bypass detection and never reach here."""
    values = os.environ if env is None else env
    fields: dict[str, Any] = {}
    if not ctx.worktree_dir:
        fields["worktree_dir"] = cwd
    if ctx.load_adrs is _no_adrs:
        adr_dir = os.path.join(cwd, "docs", "adr")
        fields["load_adrs"] = lambda: read_adrs_fn(adr_dir)
    if not ctx.lint_cmd:
        fields["lint_cmd"] = values.get("LINT_CMD", "") or ""
    if not ctx.typecheck_cmd:
        fields["typecheck_cmd"] = values.get("TYPECHECK_CMD", "") or ""
    image_limit = values.get("EVIDENCE_MAX_IMAGE_KB", "700")
    if len(image_limit) > 7 or not image_limit.isascii() or not image_limit.isdigit() or int(image_limit) > 1024 * 1024:
        fields["policy_input_error"] = "malformed EVIDENCE_MAX_IMAGE_KB"
    else:
        fields["image_limit_bytes"] = int(image_limit) * 1024
    if ctx.run_check is _no_check:
        fields["run_check"] = lambda cmd: run_check_fn(cmd, cwd)
    return replace(ctx, **fields) if fields else ctx


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


def _pr_body(argv: list[str], ctx: GateContext) -> str:
    """The PR body `gh pr create` will post, from either spelling.

    ADR-reference checks need the body even when passed as a file. Resolve the
    path against the worktree; an unreadable file supplies no reference evidence.
    """
    inline = _extract_after_flag(argv, "--body", "-b")
    if inline:
        return inline
    path = _extract_after_flag(argv, "--body-file", "-F")
    if not path:
        return ""
    try:
        with open(os.path.join(ctx.worktree_dir, path), encoding="utf-8",
                  errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


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
    body = _pr_body(argv, ctx)
    pr_base = _extract_after_flag(argv, "--base", "-B")

    # The tuple order IS the contract check-pr.sh mirrors, section for section
    # (§0, §0b, §5, §6, §7, §7b, §9, then §1 and §7c).
    # tests/test_gate_parity.py runs golden cases through both and compares the
    # first denial, so a reorder on one side without the other fails loudly.
    cheap_checks: tuple[Callable[[], gate.GateResult], ...] = (
        lambda: gate.check_no_web(argv),
        lambda: gate.check_pr_base(pr_base, ctx.base_branch),
        lambda: gate.ALLOW if ctx.history_available and ctx.branch_diff_available else gate.deny("required Git history/diff facts are unavailable"),
        lambda: _hint_malformed_declarations(
            gate.check_required_test(
                ctx.branch,
                ctx.changed_files,
                declared_subjects(ctx.declared_exemptions, RULE_REQUIRED_TEST),
            ),
            ctx,
        ),
        lambda: gate.check_commit_log(
            ctx.branch_subjects, ctx.published_subjects
        ),
        lambda: gate.check_no_evidence(ctx.changed_files),
        lambda: gate.check_no_harness_state(ctx.changed_files),
        lambda: gate.check_generated_instructions(ctx.instruction_blobs(False)),
        # tests-immutable subjects are AUTO-DECLARED from the diff — no
        # trailer, so no malformed-trailer hint applies here.
        lambda: gate.check_tests_immutable(
            tuple(dict.fromkeys(ctx.changed_files + ctx.touched_at_base)),
            ctx.exists_at_base,
            ctx.additive_only,
        ),
        # §9 — ported from bash in wave P1-2 (review M6). Cheap only in the sense
        # that matters here (no test run), but it does touch the filesystem, so
        # it sits LAST among the cheap checks: a repo with no docs/adr/ pays one
        # failed glob, and only once every earlier rule has passed.
        lambda: gate.check_adr_references(ctx.load_adrs, ctx.changed_files, body),
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
        branch, lookup, checks_runnable=ctx.base_resolvable, policy=ctx.push_lookup_policy
    )
    if not precondition.ok:
        return [precondition.reason]
    if not ctx.base_resolvable:
        return []

    # Cheap structural checks first, the suite-running one LAST, short-circuiting
    # on the first denial (review M21 — `_pr_gate` was fixed in wave P1-1, this
    # gate was not). It used to run `check_tests` FIRST and then evaluate every
    # remaining rule regardless, so a push with a bad commit prefix still paid a
    # full GATE_TEST_TIMEOUT_S test run before being told about the prefix. The
    # caller only ever reads `reasons[0]`, so returning one element changes
    # nothing observable. check-push.sh runs the same four in the same order.
    cheap_checks: tuple[Callable[[], gate.GateResult], ...] = (
        lambda: gate.ALLOW if ctx.history_available and ctx.branch_diff_available else gate.deny("required Git history/diff facts are unavailable"),
        lambda: gate.check_commit_log(
            ctx.branch_subjects, ctx.published_subjects
        ),
        lambda: gate.check_no_evidence(ctx.changed_files),
        lambda: gate.check_no_harness_state(ctx.changed_files),
        lambda: gate.check_generated_instructions(ctx.instruction_blobs(False)),
        lambda: gate.check_tests_immutable(
            tuple(dict.fromkeys(ctx.changed_files + ctx.touched_at_base)),
            ctx.exists_at_base,
            ctx.additive_only,
        ),
    )
    for check in cheap_checks:
        result = check()
        if not result.ok:
            return [_annotate(result.reason, branch, mode)]

    test_cmd = gate.resolve_test_cmd(ctx.harness_test_cmd, ctx.env_test_cmd)
    tests = gate.check_tests(test_cmd, ctx.run_tests, allow_untested=ctx.allow_untested)
    if not tests.ok:
        return [_annotate(tests.reason, branch, mode)]
    return []


def _hint_malformed_declarations(
    result: gate.GateResult, ctx: GateContext
) -> gate.GateResult:
    """Append a hint to an exemptable rule's DENIAL when the branch carries
    Gate-Exemption-looking lines that failed to parse. The parser's silence
    is correct (malformed ⇒ not a declaration, fail closed) but unteachable:
    without this, an agent that typo'd the rule name or used an
    out-of-charset subject sees the same denial as one that never declared,
    and loops on 'how do I comply' (#126 class)."""
    if result.ok or not ctx.malformed_exemption_lines:
        return result
    n = ctx.malformed_exemption_lines
    return gate.deny(
        result.reason
        + f" NOTE: {n} Gate-Exemption line(s) in your commit messages did "
        "not parse and were ignored — the format is exactly "
        "'Gate-Exemption: <rule> <subject>' where <rule> is an exemptable "
        "rule (tests-immutable, required-test) and <subject> is a plain "
        "path of [A-Za-z0-9._/+-] (or * for required-test)."
    )


def _annotate(reason: str, branch: str, mode: gate.PushGateMode) -> str:
    """A denial reached with the PR state UNKNOWN gets the 'the lookup failed,
    the rule is still real' note; an ENFORCE denial is returned verbatim."""
    if mode is gate.PushGateMode.UNVERIFIED:
        return gate.unverified_push_denial(reason, branch)
    return reason


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


def _allow_git_config(
    _command: governor_policy.Governed, _ctx: GateContext
) -> Verdict:
    """`git config` reaches routing only for bypass checks handled earlier."""

    return Verdict(True)


def _gate_branch(
    command: governor_policy.Governed, _ctx: GateContext
) -> Verdict:
    name = _extract_after_flag(list(command.checker_args), "-b", "-c")
    result = gate.check_branch_name(name)
    return Verdict(result.ok, result.reason)


def commit_gate(argv: list[str], ctx: GateContext, *, include_order: bool = True) -> Verdict:
    message = _extract_after_flag(argv, "-m", "--message")
    for check in (
        lambda: gate.check_commit_message(message),
        lambda: gate.deny("required Git history facts are unavailable") if include_order and ctx.base_resolvable and not ctx.history_available else gate.ALLOW,
        lambda: gate.check_commit_order(message, ctx.branch_subjects, ctx.base_resolvable) if include_order else gate.ALLOW,
        lambda: gate.check_generated_instructions(ctx.instruction_blobs(True)),
        lambda: gate.check_image_sizes(ctx.commit_image_sizes(), ctx.image_limit_bytes),
        lambda: gate.check_project_commands(ctx.lint_cmd, ctx.typecheck_cmd, ctx.run_check),
    ):
        result = check()
        if not result.ok:
            return Verdict(False, result.reason)
    test_cmd = gate.resolve_test_cmd(ctx.harness_test_cmd, ctx.env_test_cmd)
    red = gate.check_red_is_red(
        message, test_cmd, ctx.run_red_tests, ctx.verify_red_at_commit
    )
    return Verdict(red.ok, red.reason)


def _gate_commit(command: governor_policy.Governed, ctx: GateContext) -> Verdict:
    return commit_gate(list(command.checker_args), ctx)


def _gate_pr_create(
    command: governor_policy.Governed, ctx: GateContext
) -> Verdict:
    reasons = _pr_gate(list(command.checker_args), ctx)
    if reasons:
        return Verdict(False, f"PR blocked: {reasons[0]}")
    if ctx.durable_publish and not command.durable_wrapper:
        return Verdict(
            False,
            "PR publication must use `issue-agent-publish gh pr create "
            "…` so the remote effect has a durable command and receipt",
        )
    return Verdict(True)


def _gate_push(
    command: governor_policy.Governed, ctx: GateContext
) -> Verdict:
    reasons = _push_gate(list(command.checker_args), ctx)
    if reasons:
        return Verdict(False, f"Push blocked: {reasons[0]}")
    if ctx.durable_publish and not command.durable_wrapper:
        return Verdict(
            False,
            "Branch publication must use `issue-agent-publish git push "
            "…` so the remote effect has a durable command and receipt",
        )
    return Verdict(True)


_GATE_HANDLERS: dict[
    str, Callable[[governor_policy.Governed, GateContext], Verdict]
] = {
    "git-branch": _gate_branch,
    "git-commit": _gate_commit,
    "git-config": _allow_git_config,
    "git-push": _gate_push,
    "gh-pr-create": _gate_pr_create,
}

if frozenset(_GATE_HANDLERS) != governor_policy.GOVERNED_KINDS:
    raise RuntimeError("governor gate registry is not exhaustive")


def evaluate_command(command: str, ctx: GateContext) -> Verdict:
    """Decide whether a Bash command is allowed. Bypass detection first (fail
    CLOSED), then route each governed command to its gate. Returns the FIRST
    violation's reason (the hook can only deny once)."""
    try:
        resolution = governor_policy.resolve(
            command, repository=ctx.repository
        )
        if isinstance(resolution, governor_policy.Unresolvable):
            return Verdict(
                False,
                "Blocked: command cannot be governed statically — "
                f"{resolution.reason}. Use a literal executable and argv.",
            )

        # 1) Bypass detection across every command node (reuse the bash core).
        bypass = governor_policy.find_violations(command)
        if bypass:
            return Verdict(False, f"Blocked: {bypass[0]}")

        # 2) Route governed commands to gate checks.
        for words, _env in governor_policy.extract_commands_with_env(command):
            governed = governor_policy.classify(words)
            if governed is None:
                continue
            if ctx.policy_input_error is not None:
                return Verdict(False, ctx.policy_input_error)
            handler = _GATE_HANDLERS.get(governed.kind)
            if handler is None:
                return Verdict(
                    False,
                    f"Blocked: no gate registered for {governed.kind!r}",
                )
            result = handler(governed, ctx)
            if not result.allowed:
                return result
        return Verdict(True)
    except Exception as exc:
        return Verdict(
            False,
            "Blocked: command governor failed safely — "
            f"{type(exc).__name__}. Retry with a simpler literal command.",
        )


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
    Default runner; injectable in tests. Goes through `run_target_command` so
    EDIT_CHECK_CMD gets the same #63 defenses as TEST_CMD (it is just as
    target-owned and just as able to re-enter the gate). A recursion refusal
    reports ok=False with the reason in the output."""
    rc, out = run_target_command(command, cwd, 120, capture=True)
    if rc is None:
        return False, "GATE RECURSION: EDIT_CHECK_CMD re-entered the harness gate (see #63); refused."
    return rc == 0, out


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
    is the git/fs-backed fact gatherer; injected so this stays pure and
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
                    "the run_in_background TOOL OPTION is disabled for this agent "
                    "(it ends the turn without a result and loses the run). This "
                    "is not a ban on long-running processes: to start a server, "
                    "detach it in the SHELL instead — "
                    "`nohup <cmd> > /tmp/<name>.log 2>&1 &` with run_in_background "
                    "omitted. That returns immediately, keeps running across later "
                    "tool calls, and is allowed. Poll its log or curl the port to "
                    "wait for readiness, and kill it when you are done."
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
            # `build_ctx` gathers the git-derived facts; `complete_gate_context`
            # adds the worktree/env facts the two rules ported from bash need
            # (ADRs, LINT_CMD/TYPECHECK_CMD — review M6). Without this line both
            # rules go inert on the SDK path, which is the exact state M6 found;
            # tests/test_gate_parity.py's absence tests remove it and assert the
            # parity suite fails.
            ctx = complete_gate_context(build_ctx(cwd), cwd)
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
    PreToolUse(Bash) governor;
    additionally registers a PostToolUse(Edit|Write|…) typecheck hook when
    `edit_check_cmd` is set (deterministic edit-time gate). `log` receives a live
    breadcrumb per allowed commit (red:/green:/docs:); `debug` additionally traces
    every command + verdict. Imports the SDK lazily."""
    from claude_agent_sdk import HookMatcher

    post: list = []
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
