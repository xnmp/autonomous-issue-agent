"""The PR/commit/push gate — pure port of plugin/hooks/checks/*.sh + _pr_common.sh.

Every rule returns a `GateResult` (Allow | Deny(reason)). The rules that derive
truth from data — branch name, commit prefixes + TDD ordering, PR body, required
test in the diff, evidence, test-immutability, ADR references, the adversarial
verdict — are PURE functions of their inputs and fully unit-tested here.

The two rules that must touch the world — running the test command (§1) and the
red-fail revert simulation (§10) — are expressed as higher-order functions that
take an injected `run_tests`/`revert_and_run` callable, so the *decision logic*
(when to skip, fail-open, what counts as a failure) is tested purely with a
fake callable, and only the ~3-line real shell-out lives in the adapter.

File-name patterns + prefix sets are kept byte-for-byte consistent with the bash
(PR_TEST_FILE_RE, VALID_PREFIX) — those constants are the contract.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .refs import is_governed_branch


# ── result type ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class GateResult:
    ok: bool
    reason: str = ""


ALLOW = GateResult(True)


def deny(reason: str) -> GateResult:
    return GateResult(False, reason)


# ── shared constants (must match _pr_common.sh / check-commit*.sh) ────────────
# "this looks like a test file" (PR_TEST_FILE_RE in _pr_common.sh).
TEST_FILE_RE = re.compile(
    r"(e2e|integration|regression|test|spec).*\.(spec|test)\."
    r"|(^|/)test_.*\.py"
    r"|.*_test\.(py|go|ts|js)"
    r"|tests?/.*\.py"
)

# Stricter pattern for the feat/ branch "must add an e2e/integration test" rule
# (check-pr.sh §5 feat/* arm).
FEAT_TEST_RE = re.compile(
    r"(e2e|integration).*\.(spec|test)\."
    r"|(^|/)test_.*\.py"
    r"|.*_test\.(py|go|ts|js)"
    r"|tests?/.*\.(py|spec|test)"
)

# Two accepted forms for the harness's TDD/phase markers:
#   1. BARE:   `red: …`, `green: …`, `plan: …`, `evidence: …`   (original)
#   2. SCOPED: `<type>(red): …`, `<type>(green): …`, …          (commitlint-safe)
# The scoped form exists because a target repo with a commitlint `commit-msg` hook
# (@commitlint/config-conventional) REJECTS a bare `red:` — its type must be one of
# feat|fix|docs|chore|… . A conventional scope `(red)` on a legal type passes both
# commitlint AND our phase detection, so the harness works on commitlint repos
# without disabling their hook. See _phase() / is_red / is_green below.
_PHASES = ("plan", "red", "green", "evidence")
_CONVENTIONAL = ("build", "chore", "ci", "docs", "feat", "fix", "perf",
                 "refactor", "revert", "style", "test")
# A subject is valid if it is a bare harness prefix OR a conventional commit
# (optionally carrying a phase scope like `test(red):`). Trailing space required
# after the colon, matching the bash byte-for-byte.
VALID_COMMIT_PREFIX = re.compile(
    r"^(plan|red|green|evidence|docs|chore|refactor|test|fix|feat"
    r"|build|ci|perf|revert|style)(\([^)]*\))?: "
)

# Extract the TDD phase from either form. `red: x` → "red"; `test(red): x` → "red";
# `feat(green): x` → "green"; anything else → "".
_PHASE_RE = re.compile(
    r"^(?:(plan|red|green|evidence)|"
    r"(?:build|chore|ci|docs|feat|fix|perf|refactor|revert|style|test)"
    r"\((plan|red|green|evidence)\)): "
)


def commit_phase(msg: str) -> str:
    """The TDD/lifecycle phase of a commit subject (bare or conventional-scoped),
    or '' if none. Single source of truth for red/green/plan/evidence detection so
    the bare and commitlint-safe scoped forms are treated identically everywhere."""
    m = _PHASE_RE.match(msg or "")
    if not m:
        return ""
    return m.group(1) or m.group(2) or ""


def is_red(msg: str) -> bool:
    return commit_phase(msg) == "red"


def is_green(msg: str) -> bool:
    return commit_phase(msg) == "green"

CLOSING_KEYWORD_RE = re.compile(
    r"(fix(es|ed)?|close[sd]?|resolve[sd]?) +#[0-9]+", re.IGNORECASE
)

_BRANCH_NAME_RE = re.compile(r"^(feat|fix|chore|refactor)/[0-9]+-")


def looks_like_test(path: str) -> bool:
    return bool(TEST_FILE_RE.search(path))


# Image extensions allowed to be COMMITTED under evidence/ (visual proof renders in
# the reviewer's authenticated Files-changed tab). Everything else under evidence/
# (logs, dumps, binaries) stays gitignored + blocked from the diff.
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def is_evidence_image(path: str) -> bool:
    return path.startswith("evidence/") and path.lower().endswith(IMAGE_EXTS)


# ── branch name (check-branch-name.sh) ────────────────────────────────────────
def check_branch_name(name: str) -> GateResult:
    """Branch must be <type>/<issue>-<desc>. Empty name → allow (not a
    branch-creating form; the governor shouldn't route those)."""
    if not name:
        return ALLOW
    if not _BRANCH_NAME_RE.match(name):
        return deny(
            f"Branch name '{name}' is invalid. Use <type>/<issue>-<desc> where "
            "type is feat|fix|chore|refactor, e.g. fix/42-null-token."
        )
    return ALLOW


# ── commit message prefix (check-commit.sh) ───────────────────────────────────
def check_commit_message(msg: str) -> GateResult:
    """A non-empty message must carry an allowed prefix. Empty/file-based
    messages are allowed here (the PR gate re-derives truth from the log)."""
    if msg and not VALID_COMMIT_PREFIX.match(msg):
        return deny(
            "Commit message must start with an allowed prefix: plan: red: green: "
            "evidence: docs: chore: refactor: test: fix: feat: — or, for a repo whose "
            "commitlint forbids those, a conventional type carrying the phase as a "
            "scope, e.g. test(red): / feat(green):.  Got: " + msg
        )
    return ALLOW


# ── commit ordering (check-commit-order.sh) ───────────────────────────────────
def check_commit_order(
    incoming_msg: str, branch_subjects: Iterable[str], base_resolvable: bool = True
) -> GateResult:
    """A `green:` commit requires a prior `red:` already on the branch. Only
    green: is constrained; an unresolvable base fails OPEN (gate degraded)."""
    if not is_green(incoming_msg):
        return ALLOW
    if not base_resolvable:
        return ALLOW  # degraded: note-and-allow, like the bash
    if any(is_red(s) for s in branch_subjects):
        return ALLOW
    return deny(
        "TDD ordering violation: a 'green:' commit requires a prior 'red:' commit "
        "on this branch. Commit your failing tests first."
    )


def check_commit_log(subjects: Iterable[str]) -> GateResult:
    """Re-derived from the git LOG (closes the -F bypass): every subject has an
    allowed prefix AND no green: precedes the first red:. Port of
    _pr_common.sh:prc_check_commit_log."""
    seen_red = False
    for s in subjects:
        if not s:
            continue
        if not VALID_COMMIT_PREFIX.match(s):
            return deny(
                f"commit '{s[:60]}' lacks an allowed prefix "
                "(plan|red|green|evidence|docs|chore|refactor|test|fix|feat:)"
            )
        if is_red(s):
            seen_red = True
        elif is_green(s) and not seen_red:
            return deny(
                "a 'green:' commit appears before any 'red:' commit on the branch "
                "(TDD ordering)"
            )
    return ALLOW


# ── PR body (check-pr.sh §0b/2/3) ──────────────────────────────────────────────
def check_pr_base(pr_base: str, base_branch: str) -> GateResult:
    """PR must explicitly target BASE_BRANCH (no --base defaults to the repo's
    default branch, breaking CI routing + auto-merge). Port of §0b."""
    if not pr_base:
        return deny(
            f"PR has no --base; it must explicitly target '{base_branch}' "
            "(e.g. 'gh pr create --base "
            f"{base_branch} …'). Without --base, gh defaults to the repo's default branch."
        )
    if pr_base != base_branch:
        return deny(
            f"PR base is '{pr_base}' but must be '{base_branch}' (BASE_BRANCH). "
            f"Re-create with '--base {base_branch}'."
        )
    return ALLOW


def check_pr_body(body: str) -> GateResult:
    """AC section + a closing keyword linking the issue. Port of §2/§3."""
    if "## Acceptance Criteria" not in body:
        return deny("PR body is missing the '## Acceptance Criteria' section")
    if not CLOSING_KEYWORD_RE.search(body):
        return deny("PR body must link the issue, e.g. 'Fixes #<issue>'")
    return ALLOW


def check_no_web(argv: Iterable[str]) -> GateResult:
    """--web (browser PR creation) evades the body checks. Port of §0."""
    if "--web" in argv:
        return deny(
            "browser PR creation (--web) bypasses the gate; use a non-interactive "
            "'gh pr create'"
        )
    return ALLOW


# ── required test in the diff (check-pr.sh §5) ────────────────────────────────
def check_required_test(branch: str, changed: Iterable[str]) -> GateResult:
    """feat/ ⇒ an e2e/integration (or conventional) test; fix/ ⇒ a regression
    test. chore/refactor need none. Derived from the branch + diff, not prefixes."""
    files = list(changed)
    if branch.startswith("feat/"):
        if not any(FEAT_TEST_RE.search(f) for f in files):
            return deny(
                "feat/ branch must add an e2e/integration (or conventional) test "
                "(none found in the diff)"
            )
    elif branch.startswith("fix/"):
        if not any(looks_like_test(f) for f in files):
            return deny("fix/ branch must add a regression test (none found in the diff)")
    return ALLOW


# ── evidence + test-immutability (check-pr.sh §7/§7b) ─────────────────────────
def check_no_evidence(changed: Iterable[str]) -> GateResult:
    """evidence/ may contain ONLY committed screenshot images (visual proof that
    renders in the PR's Files-changed tab). Any non-image path under evidence/
    (logs, dumps, binaries) is rejected — those stay gitignored."""
    for f in changed:
        if f.startswith("evidence/") and not is_evidence_image(f):
            return deny(
                f"evidence/ may only contain screenshot images ({', '.join(IMAGE_EXTS)}); "
                f"'{f}' is not one — non-image evidence stays gitignored, not committed"
            )
    return ALLOW


def check_visual_evidence(
    visual_ac: bool, changed: Iterable[str], screenshots_read: Iterable[str]
) -> GateResult:
    """When the agent declared a visual acceptance criterion (.harness/visual-ac),
    the PR must commit at least one screenshot under evidence/ AND the agent must
    have actually READ each committed screenshot (loaded it into its vision context,
    recorded by the PostToolUse(Read) hook in .harness/screenshots-read). A captured-
    but-unread screenshot is unverified proof → blocked. Non-visual issues (the
    common case) → ALLOW."""
    if not visual_ac:
        return ALLOW
    images = [f for f in changed if is_evidence_image(f)]
    if not images:
        return deny(
            "a visual acceptance criterion was declared (.harness/visual-ac) but no "
            "screenshot is committed under evidence/ — capture one with agent-browser"
        )
    read = set(screenshots_read)
    unread = [os.path.basename(f) for f in images if os.path.basename(f) not in read]
    if unread:
        return deny(
            "committed screenshot(s) were never read/verified by the agent: "
            f"{', '.join(unread)} — open each with the Read tool and confirm it shows "
            "the criterion met before opening the PR"
        )
    return ALLOW


def check_tests_immutable(
    changed: Iterable[str], exists_at_base: Callable[[str], bool]
) -> GateResult:
    """Existing test files must not be modified/deleted; only NEW tests added.
    `exists_at_base(path)` reports whether the file existed at the base ref."""
    for f in changed:
        if looks_like_test(f) and exists_at_base(f):
            return deny(
                f"existing test file '{f}' was modified/deleted — add NEW tests "
                "instead of changing existing ones"
            )
    return ALLOW


# ── markers + adversarial verdict (check-pr.sh §4/§8) ─────────────────────────
def check_markers(ac_posted: bool, test_plan_posted: bool) -> GateResult:
    if not ac_posted:
        return deny(
            "no Acceptance Criteria posted on the issue yet (.harness/ac-posted "
            "missing — post AC as an issue comment FIRST)"
        )
    if not test_plan_posted:
        return deny("no Test Plan posted on the issue yet (.harness/test-plan-posted missing)")
    return ALLOW


def check_adversarial_verdict(verdict: str) -> GateResult:
    """.harness/adversarial-verdict must start with PASS. Port of §8."""
    if not verdict.startswith("PASS"):
        return deny(
            "adversarial pre-PR review did not return PASS (.harness/adversarial-verdict)"
        )
    return ALLOW


# ── test command resolution (_pr_common.sh:prc_resolve_test_cmd) ──────────────
def resolve_test_cmd(harness_test_cmd: str | None, env_test_cmd: str | None) -> str:
    """Prefer the agent-declared per-PR command (.harness/test-cmd), else
    $TEST_CMD. Strips blanks; returns '' when neither is set."""
    for candidate in (harness_test_cmd, env_test_cmd):
        if candidate:
            first = next(
                (ln for ln in candidate.replace("\r", "").splitlines() if ln.strip()),
                "",
            )
            if first:
                return first
    return ""


# ── §1 tests + §10 red-fail (impure rules expressed over injected callables) ──
def check_tests(test_cmd: str, run_tests: Callable[[str], bool]) -> GateResult:
    """§1: the test suite must pass. Empty command → ALLOW but the caller should
    surface the toothless-gate warning (no command means §10 is also disabled).
    `run_tests(cmd) -> passed?`."""
    if not test_cmd:
        return ALLOW  # caller warns; genuinely optional for some repos
    return ALLOW if run_tests(test_cmd) else deny(f"local test suite failed: {test_cmd}")


def gate_is_toothless(test_cmd: str) -> bool:
    """True when there is no test command — §1 and §10 both silently no-op. The
    caller emits the GATE WARNING. (Pure helper so the warning is testable.)"""
    return not test_cmd


def check_red_is_red(
    commit_msg: str, test_cmd: str, run_tests: Callable[[str], bool], enabled: bool = True
) -> GateResult:
    """Verify a `red:` commit is ACTUALLY red — run the test command on the working
    tree at commit time and require it to FAIL. The commit-order gate only checks the
    `red:`/`green:` LABEL ordering; nothing else executes the suite while the impl is
    absent until §10 at PR time. This is the earlier, in-session fail-fast: the agent
    (or the author leg) learns immediately that its 'failing' test actually passes.

    Only fires for a `red:`-prefixed commit. No test command → ALLOW (toothless, like
    §1). `enabled=False` disables it (heavy suites that can't run in a commit hook).
    A PASSING suite at a red commit → DENY (the test is not red).
    Note: this tests the WORKING TREE; in disciplined test-first order the impl isn't
    written yet, so working tree == 'test without impl'. §10 remains the backstop that
    checks the COMMITTED state with impl reverted (and catches post-red test weakening)."""
    if not enabled or not is_red(commit_msg):
        return ALLOW
    if not test_cmd:
        return ALLOW
    if not run_tests(test_cmd):
        return ALLOW  # tests fail → genuinely red → good
    return deny(
        f"'red:' commit but the test suite PASSES ({test_cmd}) — the new test is not "
        "actually failing. A red commit must contain a test that FAILS without the "
        "implementation (write the failing test first, before any implementation). If "
        "it passes now, it is tautological or the implementation already exists."
    )


@dataclass(frozen=True)
class RedFailInputs:
    test_cmd: str
    new_test_files: tuple[str, ...]   # new test files (kept at HEAD)
    impl_files: tuple[str, ...]       # implementation/non-test changes (reverted)
    red_must_fail: bool = True
    base_resolvable: bool = True


def partition_changed(
    changed: Iterable[str], exists_at_base: Callable[[str], bool]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split a changed-file list into (new_test_files, impl_files), the partition
    §10 needs: a NEW test file is one that looks like a test AND did not exist at
    base; everything else is implementation (or a non-test change)."""
    new_tests: list[str] = []
    impl: list[str] = []
    for f in changed:
        if not f:
            continue
        if looks_like_test(f) and not exists_at_base(f):
            new_tests.append(f)
        else:
            impl.append(f)
    return tuple(new_tests), tuple(impl)


def check_red_fails(
    inputs: RedFailInputs,
    revert_and_run: Callable[[tuple[str, ...]], bool],
) -> GateResult:
    """§10: with the implementation reverted, the NEW tests MUST fail (real red).
    Catches the tautological / wrong-layer test (#39 class).

    Skips (ALLOW) when: RED_MUST_FAIL=0, no test command, base unresolvable, no
    new tests (§5 owns "must add a test"), or a pure-test diff (nothing to
    revert). `revert_and_run(impl_files) -> passed?` performs the transient
    revert + test run in the worktree and is responsible for restoring HEAD; it
    fails OPEN (returns False ⇒ red ⇒ ALLOW only via this function's logic) — see
    note below. A passing suite with the impl gone ⇒ the tests don't exercise the
    change ⇒ DENY.
    """
    if not inputs.red_must_fail:
        return ALLOW
    if not inputs.test_cmd:
        return ALLOW
    if not inputs.base_resolvable:
        return ALLOW  # fail-open: degraded simulation must never wedge a real PR
    if not inputs.new_test_files:
        return ALLOW  # §5 owns the "must add a test" rule
    if not inputs.impl_files:
        return ALLOW  # pure-test diff: nothing to revert

    passed_with_impl_reverted = revert_and_run(inputs.impl_files)
    if passed_with_impl_reverted:
        return deny(
            "the new test(s) still PASS with the implementation reverted — they do "
            "not exercise the change (tautological or wrong-layer test). Assert on "
            "the observable behaviour the change produces, not on a constant that is "
            f"always true. New test file(s): {' '.join(inputs.new_test_files)}"
        )
    return ALLOW
