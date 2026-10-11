"""ADR 0011 PR/commit/push gate policy.

Every rule returns a `GateResult` (Allow | Deny(reason)). The rules that derive
truth from data — branch name, commit prefixes + TDD ordering, required
test in the diff, evidence, test-immutability, ADR references, the adversarial
verdict — are PURE functions of their inputs and fully unit-tested here.

The two rules that must touch the world — running the test command (§1) and the
red-fail revert simulation (§10) — are expressed as higher-order functions that
take an injected `run_tests`/`revert_and_run` callable, so the *decision logic*
(when to skip, fail-open, what counts as a failure) is tested purely with a
fake callable, and only the ~3-line real shell-out lives in the adapter.

Both advisory transports compose these decisions over harness-collected facts;
there is no shell policy copy.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from enum import Enum

from .contracts import TestExecution, TestExecutionStatus
from .exemptions import (
    MAX_TEST_IMMUTABILITY_EXCEPTIONS,
    RULE_REQUIRED_TEST,
    RULE_TESTS_IMMUTABLE,
    SUBJECT_WHOLE_CHANGE,
    check_declaration_scope,
    valid_subject,
)
from .refs import BRANCH_PREFIXES, ISSUE_BRANCH_PATTERN, is_governed_branch


# ── result type ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class GateResult:
    ok: bool
    reason: str = ""


ALLOW = GateResult(True)


def deny(reason: str) -> GateResult:
    return GateResult(False, reason)


# ── policy constants ───────────────────────────────────────────────────────
# "this looks like a test file" (PR_TEST_FILE_RE in _pr_common.sh).
TEST_FILE_RE = re.compile(
    r"(e2e|integration|regression|test|spec).*\.(spec|test)\."
    r"|(^|/)test_.*\.py"
    r"|.*_test\.(py|go|ts|js|rs)"
    # Rust integration tests are BY CONVENTION bare .rs files under tests/
    # (cargo compiles each as its own test crate) — the detector's .py-only
    # tests/ arm rejected `tests/help_popup_scroll_test.rs` on keifu #155
    # (2026-08-01) even though `cargo test` ran it.
    r"|tests?/.*\.(py|rs)"
)

# Stricter pattern for the feat/ branch "must add an e2e/integration test" rule
# (check-pr.sh §5 feat/* arm).
FEAT_TEST_RE = re.compile(
    r"(e2e|integration).*\.(spec|test)\."
    r"|(^|/)test_.*\.py"
    r"|.*_test\.(py|go|ts|js|rs)"
    r"|tests?/.*\.(py|rs|spec|test)"
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

_BRANCH_NAME_RE = re.compile(ISSUE_BRANCH_PATTERN)


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
            f"type is {'|'.join(BRANCH_PREFIXES)}, e.g. fix/42-null-token."
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


def check_commit_log(
    subjects: Iterable[str], published: Iterable[str] = ()
) -> GateResult:
    """Re-derived from the git LOG (closes the -F bypass): every subject has an
    allowed prefix AND no green: precedes the first red:. Port of
    _pr_common.sh:prc_check_commit_log.

    `published` is the subjects already on the OBSERVED remote PR branch
    (base..origin/<branch>). Those commits are outside the agent's unilateral
    control — the publish broker or a human put them there — so the prefix rule
    does not re-police them: GitHub's own "Update branch" merge commit
    ("Merge branch 'chong-dev' into fix/130-…") deadlocked keifu PR #182
    (2026-08-03) between the broker, which required it as an ancestor, and this
    gate, which forbade publishing it. A red: among the published commits still
    counts for TDD ordering (a continuation branch's greens stay legal), and
    each published subject exempts only ONE occurrence — an agent re-using a
    published subject verbatim can smuggle at most an unprefixed no-op subject,
    never skip the ordering rule."""
    remaining: dict[str, int] = {}
    seen_red = False
    for s in published:
        if not s:
            continue
        remaining[s] = remaining.get(s, 0) + 1
        if is_red(s):
            seen_red = True
    for s in subjects:
        if not s:
            continue
        if remaining.get(s, 0) > 0:
            remaining[s] -= 1
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


def check_no_web(argv: Iterable[str]) -> GateResult:
    """--web (browser PR creation) evades the body checks. Port of §0."""
    if "--web" in argv:
        return deny(
            "browser PR creation (--web) bypasses the gate; use a non-interactive "
            "'gh pr create'"
        )
    return ALLOW


# ── required test in the diff (check-pr.sh §5) ────────────────────────────────
def check_required_test(
    branch: str, changed: Iterable[str], declared: Collection[str] = ()
) -> GateResult:
    """feat/ ⇒ an e2e/integration (or conventional) test; fix/ ⇒ a regression
    test. chore/refactor need none. Derived from the branch + diff, not prefixes.

    `declared` are the subjects a `Gate-Exemption: required-test *` trailer
    names (only the whole-change subject is meaningful). A declared exemption
    passes THIS check — the reviewer must be able to see and judge the
    test-less change — but authorizes nothing: auto-merge is withheld until
    REVIEW_BOT grants it against the current patch id (exemptions.py). The
    scope bound applies here exactly as in `check_tests_immutable` (round-3
    MAJOR 3: without it, `*` plus junk subjects passed the gate and then
    wedged at merge)."""
    if SUBJECT_WHOLE_CHANGE in declared:
        if check_declaration_scope(RULE_REQUIRED_TEST, declared):
            return ALLOW
        # A declared-but-VOID exemption must say WHY it is void, or the agent
        # re-reads "add the trailer" it already added and files a harness bug
        # (tauri #457, 2026-08-09: `*` plus a second subject in another commit
        # voided the exemption; the generic denial sent the fixer in circles).
        subjects = ", ".join(sorted(declared))
        return deny(
            "required-test exemption declared but VOID: this branch declares "
            f"{len(set(declared))} subjects for required-test ({subjects}) and "
            "the rule accepts exactly one, the whole-change '*'. Trailers "
            "accumulate across ALL commits on the branch — reword or drop the "
            "commit message(s) declaring the extra subject(s) so exactly one "
            "'Gate-Exemption: required-test *' remains"
        )
    files = list(changed)
    if branch.startswith("feat/"):
        if not any(FEAT_TEST_RE.search(f) for f in files):
            return deny(
                "feat/ branch must add an e2e/integration (or conventional) test "
                "(none found in the diff). If this change GENUINELY needs no "
                "test, request a reviewer exemption: add a commit with the "
                "trailer line 'Gate-Exemption: required-test *' — the reviewer "
                "must then explicitly grant it before the PR can merge"
            )
    elif branch.startswith("fix/"):
        if not any(looks_like_test(f) for f in files):
            return deny(
                "fix/ branch must add a regression test (none found in the "
                "diff). If this change GENUINELY needs no test, request a "
                "reviewer exemption: add a commit with the trailer line "
                "'Gate-Exemption: required-test *' — the reviewer must then "
                "explicitly grant it before the PR can merge"
            )
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


def check_no_harness_state(changed: Iterable[str]) -> GateResult:
    """.harness/ is harness-local control state and must NEVER become repo
    content. Keifu #156 (2026-08-01): PR #167 committed its issue-scoped
    `.harness/test-cmd`; once merged, every downstream worktree inherited a
    trusted-gate test command pinned to ANOTHER issue's single test, making
    the red-fail revert check vacuous repo-wide (a later PR was rejected as
    tautological because the gate ran the wrong issue's test). A committed
    marker is also an agent-writable input to the gate's own teeth — the
    exact channel P1R-18 closes for the env override."""
    for f in changed:
        if f == ".harness" or f.startswith(".harness/"):
            return deny(
                f"'{f}' is harness-local control state — .harness/ files are "
                "never committed; remove it from the diff (git rm --cached) "
                "and keep it gitignored"
            )
    return ALLOW



def check_generated_instructions(blobs: tuple[tuple[str, str], ...] | None) -> GateResult:
    if blobs is None:
        return deny("generated instruction evidence is unavailable")
    if not isinstance(blobs, tuple) or len(blobs) > 2:
        return deny("malformed generated instruction evidence")
    for blob in blobs:
        if not isinstance(blob, tuple) or len(blob) != 2:
            return deny("malformed generated instruction evidence")
        path, text = blob
        if not isinstance(path, str) or path not in {"AGENTS.md", "AGENTS.override.md"} or not isinstance(text, str) or len(text) > 2_000_000:
            return deny("malformed generated instruction evidence")
        if "<!-- issue-agent generated instructions: start -->" in text:
            return deny(f"{path} contains issue-agent generated instructions — restore the target-owned file")
    return ALLOW


def check_image_sizes(images: tuple[tuple[str, int], ...] | None, limit_bytes: int) -> GateResult:
    if type(limit_bytes) is not int or not 0 <= limit_bytes <= 1024 * 1024 * 1024:
        return deny("malformed evidence image size limit")
    if limit_bytes == 0:
        return ALLOW
    if images is None:
        return deny("evidence image sizes are unavailable")
    if not isinstance(images, tuple) or len(images) > 8192:
        return deny("malformed evidence image size fact")
    for image in images:
        if not isinstance(image, tuple) or len(image) != 2:
            return deny("malformed evidence image size fact")
        path, size = image
        if not isinstance(path, str) or not path or len(path) > 4096 or type(size) is not int or not 0 <= size <= 2**63 - 1:
            return deny("malformed evidence image size fact")
        if size > limit_bytes:
            return deny(f"image {path} exceeds the {limit_bytes // 1024}KB evidence size limit — downscale before committing")
    return ALLOW


class PushLookupPolicy(Enum):
    REQUIRE_VERIFIED = "require_verified"
    CHECK_AVAILABLE_FACTS = "check_available_facts"


def modified_existing_tests(
    changed: Iterable[str],
    exists_at_base: Callable[[str], bool],
) -> tuple[str, ...]:
    """The existing test files this change modifies or deletes — the
    AUTO-DECLARED tests-immutable exemption subjects (ADR 0014, revised
    2026-08-03). The diff itself is the declaration: no trailer ceremony,
    nothing for the fixer to get syntactically wrong, no comply-how loop.
    Shared by the gate (scope bound), the merge-side facts collector, and
    the reviewer notice, so all three always name the same subjects."""
    return tuple(
        f for f in changed if looks_like_test(f) and exists_at_base(f)
    )


# Per-file records. `diff --git` is emitted UNCONDITIONALLY for every changed
# path — including binary files, pure renames, mode-only changes, and paths
# suppressed by `.gitattributes -diff`, all of which emit NO `--- a/` line.
# Deriving from `--- a/` instead made absence-of-header mean "no exemption
# needed", so one `tests/** -diff` attribute disabled the whole regime
# (round-4 adversarial review, BLOCKER 1). Anchored at column 0 with the
# `diff --git ` prefix, which diff CONTENT can never produce (content lines
# carry a leading +/-/space).
_DIFF_RECORD_RE = re.compile(r"^diff --git (.+)$", re.MULTILINE)
_DIFF_NEW_FILE_RE = re.compile(r"^new file mode ", re.MULTILINE)
_DIFF_RENAME_FROM_RE = re.compile(r"^rename from (.+)$", re.MULTILINE)


def _unquote_diff_path(raw: str) -> str | None:
    """One side of a `diff --git` header → repo-relative path, or None when
    the rendering is ambiguous. Git C-quotes paths containing non-ASCII or
    special characters; unquoted paths containing SPACES are genuinely
    ambiguous on this line (`a/x y.py b/x y.py` has no unique split), so those
    resolve via the record body instead of a guess."""
    raw = raw.strip()
    if raw.startswith('"'):
        end = raw.find('"', 1)
        while end > 0 and raw[end - 1] == "\\":
            end = raw.find('"', end + 1)
        if end < 0:
            return None
        try:
            decoded = (
                raw[1:end]
                .encode("latin-1", "backslashreplace")
                .decode("unicode_escape")
                .encode("latin-1")
                .decode("utf-8")
            )
        except (UnicodeDecodeError, UnicodeEncodeError):
            return None
        return decoded[2:] if decoded.startswith(("a/", "b/")) else decoded
    if " " in raw:
        return None  # ambiguous split; the caller falls back
    return raw[2:] if raw.startswith(("a/", "b/")) else raw


def modified_existing_tests_from_diff(
    diff_text: object,
) -> tuple[tuple[str, ...], int]:
    """(subjects, undecidable) — `modified_existing_tests` derived from a
    unified diff instead of a worktree, for the merge-side collector and the
    reviewer notice, which hold the revision-checked diff and no checkout.

    A record counts when it touches a path that existed at base (anything but
    `new file mode`) and looks like a test — covering binary, rename-away,
    mode-only and `-diff`-suppressed records that emit no `--- a/` line. A
    rename's OLD path is the subject: renaming a test out of the test tree
    removes it from the suite as surely as deleting it.

    `undecidable` counts records whose old path could not be resolved
    (ambiguous unquoted spaces with no `rename from`/`--- a/` witness). It is
    never silently zero-valued: callers treat non-zero as WITHHELD, because
    "we could not tell what this touched" must not read as "it touched no
    tests"."""
    if not isinstance(diff_text, str):
        return (), 1  # unreadable ⇒ undecidable, never "nothing to declare"
    subjects: dict[str, None] = {}
    undecidable = 0
    records = list(_DIFF_RECORD_RE.finditer(diff_text))
    for index, match in enumerate(records):
        start = match.end()
        end = (
            records[index + 1].start()
            if index + 1 < len(records)
            else len(diff_text)
        )
        body = diff_text[start:end]
        if _DIFF_NEW_FILE_RE.search(body):
            continue  # added file: nothing existed at base
        old = None
        rename = _DIFF_RENAME_FROM_RE.search(body)
        if rename:
            old = rename.group(1).strip()
        else:
            header = match.group(1)
            # `a/<p> b/<p>` for the common same-path case: take the a-side by
            # splitting at the LAST ` b/` so paths containing " b/" resolve.
            split = header.rfind(" b/")
            candidate = header[:split] if split > 0 else header
            old = _unquote_diff_path(candidate)
            if old is None:
                minus = re.search(
                    r"^--- (?:a/)?(.+)$", body, re.MULTILINE
                )
                if minus and minus.group(1) != "/dev/null":
                    old = minus.group(1).rstrip("\t")
        if old is None:
            undecidable += 1
            continue
        if looks_like_test(old):
            subjects.setdefault(old)
    return tuple(subjects), undecidable


def check_tests_immutable(
    changed: Iterable[str],
    exists_at_base: Callable[[str], bool],
    additive_only: Iterable[str] = (),
) -> GateResult:
    """Existing-test edits are AUTO-DECLARED exemption requests, bounded by
    scope. `exists_at_base(path)` reports whether the file existed at the
    base ref.

    Within the scope bound, the edit passes THIS check — visible in the diff,
    framed for the reviewer by the exemption notice — but authorizes nothing:
    enforcement sits at MERGE, where auto-merge is withheld until REVIEW_BOT
    grants each modified file against the current patch id (exemptions.py).
    Beyond the bound the gate denies: a mass rewrite is not a correction, and
    an unbounded auto-declaration would hand the reviewer an unreviewable
    hazard list. The former trailer path (deny undeclared edits, teach the
    `Gate-Exemption:` syntax) produced the #126/#163 comply-how loops — the
    fixer could not act on its own judgment; now it can, and the reviewer
    still explicitly approves every touched test file."""
    subjects = modified_existing_tests(changed, exists_at_base)
    # A path that cannot be written as a grant subject can never be granted,
    # so admitting it would guarantee a permanent withhold and a confusing
    # escalation later. Deny HERE, where the message can name the file and
    # the fix is cheap (round-4 MINOR 6).
    unrepresentable = [f for f in subjects if not valid_subject(f)]
    if unrepresentable:
        return deny(
            f"existing test file '{unrepresentable[0]}' was modified, but "
            "its path cannot be carried in a reviewer grant (allowed: "
            "letters, digits, and . _ / + - up to 200 characters). Restore "
            "that file and put the change in a test file whose path is "
            "within the charset, or rename the file in a separate PR"
        )
    if not check_declaration_scope(RULE_TESTS_IMMUTABLE, subjects):
        return deny(
            f"more than {MAX_TEST_IMMUTABILITY_EXCEPTIONS} existing test "
            "files modified/deleted — changing an existing test is allowed "
            "(the reviewer must explicitly approve each one before merge), "
            "but this many reads as a rewrite, not a correction; reduce the "
            "change's scope (or split it) so at most "
            f"{MAX_TEST_IMMUTABILITY_EXCEPTIONS} existing test files are "
            "touched, and prefer ADDING tests in new files wherever the old "
            "expectation is still right"
        )
    # Purely additive edits to an existing test file are the one shape the
    # reviewer refuses on POLICY, not judgment ("additive coverage belongs in
    # a new test file" — 2026-08-09 tauri batch: #620/#621/#622/#633 each
    # burned a paid round on exactly this). Denying here converts that round
    # into a free local move; a genuine correction (any removed/changed line)
    # still auto-declares and goes to the reviewer as before.
    additive = set(additive_only)
    additive_subjects = [f for f in subjects if f in additive]
    if additive_subjects:
        return deny(
            f"existing test file '{additive_subjects[0]}' was changed by "
            "ADDING lines only — additive coverage belongs in a NEW test "
            "file (the reviewer refuses tests-immutable grants for pure "
            "additions); move the new tests into a new file, and modify an "
            "existing test only where its old expectation is wrong"
        )
    return ALLOW


# ── ADR citation (check-pr.sh §9) ─────────────────────────────────────────────
# Ported from bash in wave P1-2 (review M6). It had NO Python equivalent, so on
# the DEFAULT SDK path — the one every Claude-driven fixer takes — this rule was
# dead, while plugin/AGENT.md told the agent "the PR gate enforces this". Docs
# that describe the union of two implementations, with no path satisfying them,
# are worse than either implementation alone.
@dataclass(frozen=True)
class Adr:
    """One architecture decision record, as the gate needs to see it.

    `filename` is the basename (`0003-storage-layer.md`); `governs` are the
    whitespace-separated paths from its `Governs:` line(s). Loading is the
    adapter's job (`adapters/governor.read_adrs`) — this stays a value."""

    filename: str
    governs: tuple[str, ...] = ()

    @property
    def id(self) -> str:
        """The leading digits of the filename (`0003-storage.md` → `0003`), which
        is what the PR body must cite. '' when the name doesn't start with digits
        — the loader's glob (`[0-9]*.md`) makes that unreachable in practice."""
        m = re.match(r"^([0-9]+)", self.filename)
        return m.group(1) if m else ""


_ADR_GOVERNS_RE = re.compile(r"^Governs:(.*)$", re.IGNORECASE | re.MULTILINE)


def parse_adr(filename: str, text: str) -> Adr:
    """An ADR file's basename + contents → the value the rule reads. Mirrors the
    bash `grep -i '^Governs:' | sed 's/^[Gg]overns://'` + word-split: EVERY
    `Governs:` line contributes, and each line's value is split on whitespace."""
    governs: list[str] = []
    for m in _ADR_GOVERNS_RE.finditer(text or ""):
        governs.extend(m.group(1).split())
    return Adr(filename=filename, governs=tuple(governs))


def adr_is_cited(body: str, adr: Adr) -> bool:
    """Does the PR body reference this ADR? Either `ADR <id>` (any case, with an
    optional space or hyphen: `adr-0003`, `ADR 0003`, `adr0003`) or the ADR's
    filename verbatim. The filename is matched LITERALLY — the bash interpolated
    it into a regex alternation, where `0003-foo.md` also matched `0003-fooXmd`;
    both stacks now use a literal compare (`grep -qiF`)."""
    hay = body or ""
    if adr.id and re.search(rf"adr[ -]?{re.escape(adr.id)}", hay, re.IGNORECASE):
        return True
    return adr.filename.lower() in hay.lower()


def check_adr_references(
    load_adrs: Callable[[], tuple[Adr, ...]], changed: Iterable[str], body: str
) -> GateResult:
    """§9: if the diff touches a path an ADR governs, the PR body must cite that
    ADR. Higher-order over an injected loader (the `check_tests` shape) because
    reading `docs/adr/` is I/O and this rule runs inside a hook: the loader is
    called ONLY when this check is reached, never at context-build time.

    A repo with no `docs/adr/` yields no ADRs and the rule is inert — that is the
    common case and must stay free. A governed path matches as a SUBSTRING of a
    changed path (bash `grep -qF`), so `Governs: src/auth` covers
    `src/auth/token.py`."""
    files = [f for f in changed if f]
    for adr in load_adrs():
        for path in adr.governs:
            if not any(path in f for f in files):
                continue
            if adr_is_cited(body, adr):
                continue
            return deny(
                f"diff touches {path} governed by ADR {adr.id} — "
                "reference it in the PR body"
            )
    return ALLOW


# ── project lint / typecheck at commit time (check-commit.sh:54-59) ───────────
def check_project_commands(
    lint_cmd: str, typecheck_cmd: str, run: Callable[[str], bool]
) -> GateResult:
    """The commit-time fast gates: run `LINT_CMD` then `TYPECHECK_CMD` in the
    worktree and DENY on a non-zero exit. Ported from bash in wave P1-2 (review
    M6) — the SDK path had no equivalent, so `bin/doctor.sh` printed
    "✓ LINT_CMD set" about a command nothing ran.

    Order matters and lives here, not in the caller: lint before typecheck, both
    before the far more expensive `check_red_is_red` (which runs the whole test
    suite). Either being unset skips it — an operator who configured neither pays
    nothing. Tests are deliberately NOT run here; a blocking commit hook that ran
    the suite would time out and serialise the agent (see check-commit.sh)."""
    if lint_cmd and not run(lint_cmd):
        return deny(f"Lint failed (LINT_CMD): {lint_cmd}")
    if typecheck_cmd and not run(typecheck_cmd):
        return deny(f"Typecheck failed (TYPECHECK_CMD): {typecheck_cmd}")
    return ALLOW


# ── test command resolution (_pr_common.sh:prc_resolve_test_cmd) ──────────────
def resolve_test_cmd(harness_test_cmd: str | None, env_test_cmd: str | None) -> str:
    """Prefer the OPERATOR's $TEST_CMD; fall back to the agent-declared
    `.harness/test-cmd` only when the operator set none. Strips blanks; returns
    '' when neither is set.

    The precedence used to be the other way round, and that made the gate
    self-defeating (P1R-18): `check_tests` is the push gate's only real teeth,
    the gated agent WRITES `.harness/test-cmd`, and it runs under
    bypassPermissions — so `echo true > .harness/test-cmd` disarmed §1 and §10
    (the anti-tautological-test check) in one line, without tripping any bypass
    detection, because the governor only inspects git/gh shapes.

    The old order was not accidental: AGENT.md asks the agent to declare a
    *scoped* command so the gate's revert-and-rerun is fast and hits the changed
    package in a monorepo. That intent survives — the marker is still the whole
    answer for a repo where the operator has no TEST_CMD (the test-discovery
    path that writes it exists for exactly those), and a scoped command a subject
    can forge is not a safety property to begin with. Where an operator command
    exists it is the authority; the cost is a slower gate run, which is the right
    trade against a one-line disarm."""
    for candidate in (env_test_cmd, harness_test_cmd):
        if candidate:
            first = next(
                (ln for ln in candidate.replace("\r", "").splitlines() if ln.strip()),
                "",
            )
            if first:
                return first
    return ""


# ── §1 tests + §10 red-fail (impure rules expressed over injected callables) ──
# The message the agent sees when no test command resolves. Single-sourced so §1
# and §10 give identical, actionable instructions. Keep in sync with
# _pr_common.sh:prc_check_tests / prc_check_red_fails.
NO_TEST_CMD_HINT = (
    "Write the exact command you used to run your tests into .harness/test-cmd "
    "(one line, non-watch, must exit), or have the operator set TEST_CMD. If this "
    "repo genuinely has no test suite, the operator sets ALLOW_UNTESTED=1 in its "
    "overlay config."
)


def check_tests(
    test_cmd: str, run_tests: Callable[[str], bool | None], allow_untested: bool = False
) -> GateResult:
    """§1: the test suite must pass. `run_tests(cmd) -> passed?` — tri-state:
    None means the adapter REFUSED to run the command because it would re-enter
    the gate (#63 recursion bound); that is a configuration defect, not a red
    suite, and gets its own DENY so the agent doesn't chase phantom failures.

    NO test command → DENY, not allow. An empty command used to pass silently,
    which made "this repo has no test command" indistinguishable from "the tests
    passed" — the gate exited 0 either way, and §10 (anti-tautological-test) was
    disabled along with it. Fail CLOSED and require a deliberate operator opt-out
    (`allow_untested`, from ALLOW_UNTESTED=1 in the repo overlay) for the genuinely
    test-less repo. The flag is an OPERATOR fact, never an agent-supplied one."""
    if not test_cmd:
        if allow_untested:
            return ALLOW  # caller warns; explicit opt-out for a test-less repo
        return deny(
            "no test command resolved (neither .harness/test-cmd nor TEST_CMD) — the "
            "test gate and the red-fail (anti-tautological-test) check cannot run, so "
            "the gate fails CLOSED instead of passing you silently. " + NO_TEST_CMD_HINT
        )
    passed = run_tests(test_cmd)
    if passed is None:
        return deny(
            f"GATE RECURSION: the test command re-entered the harness gate and was "
            f"refused (self-referential TEST_CMD/.harness/test-cmd — see #63); this "
            f"is a test-command configuration defect, NOT a failing suite: {test_cmd}"
        )
    return ALLOW if passed else deny(f"local test suite failed: {test_cmd}")


def gate_is_toothless(test_cmd: str) -> bool:
    """True when there is no test command — §1 and §10 have no teeth. Now only
    reachable with the ALLOW_UNTESTED opt-out (otherwise the gate denies); the
    caller emits the GATE WARNING. (Pure helper so the warning is testable.)"""
    return not test_cmd


# ── push-gate precondition: "does this branch have an open PR?" ───────────────
class OpenPrLookup(Enum):
    """Tri-state answer to the push gate's precondition (check-push.sh's
    `gh pr list --head <branch> --state open`).

    ERROR is the whole point of the type: the bash used to collapse a failed `gh`
    call into "" — the same value as "no PR" — so a network/auth blip skipped the
    ENTIRE push gate on a branch that DID have an open PR. The gate's own
    precondition was the bypass. An unknown state must be representable so it can
    be denied, hence a tri-state rather than a bool. The retry lives in
    `resolve_open_pr`."""

    OPEN = "open"
    NONE = "none"
    ERROR = "error"


def resolve_open_pr(
    probe: Callable[[], OpenPrLookup],
    attempts: int = 2,
    sleep: Callable[[], None] = lambda: None,
) -> OpenPrLookup:
    """Run the injected `probe` up to `attempts` times, returning the first
    non-ERROR answer (else ERROR). `sleep` is called BETWEEN attempts only — the
    delay is the adapter's (impure) business, defaulted to a no-op so the retry
    POLICY is pure and testable. Mirrors check-push.sh's two `gh pr list` calls
    with a sleep between: a transient blip is the common case and a needless
    denial costs the agent a turn, so retry before failing closed."""
    result = OpenPrLookup.ERROR
    for attempt in range(max(attempts, 1)):
        if attempt:
            sleep()
        result = probe()
        if result is not OpenPrLookup.ERROR:
            return result
    return result


class PushGateMode(Enum):
    """What the push gate does for one destination branch.

    * SKIP — ungoverned branch, or governed with NO open PR: the pre-PR TDD
      phase stays free and the create gate enforces later.
    * ENFORCE — governed + a confirmed open PR: the state-derived checks run and
      a denial is a real rule violation.
    * UNVERIFIED — governed, PR state UNKNOWN (the gh lookup failed even after a
      retry). The checks run ANYWAY and a clean push is allowed.

    UNVERIFIED exists because the previous policy (deny every push on ERROR) had
    a failure mode worse than the one it closed: during a GitHub rate limit it
    also denied the FIRST, pre-PR push, so `gh pr create` never happened and the
    resolve-retry counter walked a healthy issue to a human (P1R-12). Running
    the checks costs the agent a test run; denying the leg costs the issue. The
    property that matters is still fail-closed: an unknown-state push that FAILS
    any rule is denied, exactly as if the PR were known to be open."""

    SKIP = "skip"
    ENFORCE = "enforce"
    UNVERIFIED = "unverified"


def push_gate_mode(branch: str, lookup: OpenPrLookup) -> PushGateMode:
    """Tri-state lookup + governed-ness → what the gate does. Pure."""
    if not is_governed_branch(branch):
        return PushGateMode.SKIP
    if lookup is OpenPrLookup.OPEN:
        return PushGateMode.ENFORCE
    if lookup is OpenPrLookup.ERROR:
        return PushGateMode.UNVERIFIED
    return PushGateMode.SKIP


def check_push_precondition(
    branch: str, lookup: OpenPrLookup, checks_runnable: bool,
    policy: PushLookupPolicy = PushLookupPolicy.CHECK_AVAILABLE_FACTS,
) -> GateResult:
    """DENY a push whose PR state could not be determined AND whose state-derived
    checks cannot be run either — nothing at all would be verified, so the
    unknown state loses (a blocked push is recoverable; an ungated push is not).

    `checks_runnable` is a REQUIRED argument, not a defaulted one: the whole bug
    class this guard sits in is "the permissive value was the default and nobody
    populated it" (the original `has_open_pr: bool = False`). A caller that
    cannot say whether it will run the checks has to decide, in the open.

    Only governed branches are gated at all (an ungoverned branch never even
    calls gh in the bash), so a lookup error there is irrelevant."""
    if not isinstance(policy, PushLookupPolicy):
        return deny("malformed push lookup policy")
    if not is_governed_branch(branch):
        return ALLOW
    if lookup is OpenPrLookup.ERROR and policy is PushLookupPolicy.REQUIRE_VERIFIED:
        return deny(f"could not verify PR state for branch '{branch}' — the open-PR lookup failed twice (gh/network/auth error); check connectivity and retry the push shortly")
    if lookup is OpenPrLookup.ERROR and not checks_runnable:
        return deny(
            f"could not verify PR state for branch '{branch}' — the open-PR lookup "
            "failed (gh/network/auth error) even after a retry, and the state-derived "
            "checks cannot run here either (the base ref does not resolve), so nothing "
            "about this push would be verified. It fails CLOSED rather than going "
            "through unchecked. Check connectivity and 'gh auth status', then "
            "retry the push shortly."
        )
    return ALLOW


def unverified_push_denial(reason: str, branch: str) -> str:
    """Annotate a push denial that was reached with the PR state UNKNOWN, so the
    agent does not read it as "the gate is broken/offline": the rule it failed is
    a real one, and fixing it makes the push go through even while the lookup is
    still failing."""
    return (
        f"{reason} (the open-PR lookup for '{branch}' failed, so the gate ran its "
        "state-derived checks regardless of PR state — fix the above and the push is "
        "allowed even while the lookup is down)"
    )


# ── which branch(es) a push actually writes (P1R-10) ─────────────────────────
# `git rev-parse --abbrev-ref HEAD` returns the literal string "HEAD" on a
# detached HEAD — which is exactly the state a CONFLICTED REBASE leaves the
# worktree in, i.e. the one leg whose prompt tells the agent to force-push. An
# empty string means the rev-parse itself failed. Neither is a branch name.
_DETACHED_HEAD_NAMES = ("", "HEAD")
# Refspec destinations that mean "whatever branch is checked out".
_HEAD_ALIASES = ("HEAD", "@")


def head_is_detached(head_branch: str) -> bool:
    return (head_branch or "").strip() in _DETACHED_HEAD_NAMES


def resolve_push_targets(
    explicit_destinations: Iterable[str], head_branch: str
) -> tuple[str, ...] | None:
    """The branch(es) a push writes → what the gate must key on.

    `explicit_destinations` are the refspec destinations parsed out of the argv
    (`governor_policy.push_destination_refs`); `head_branch` is the worktree's
    checked-out branch. Returns the resolved destinations, or **None** when the
    destination cannot be determined at all.

    The gate used to key on `head_branch` unconditionally, which is wrong three
    ways (P1R-10): `git push origin HEAD:other-branch` was checked against the
    wrong branch; a push from a detached HEAD read as "ungoverned" and skipped
    the gate entirely on the rebase leg; and the pushed ref is the thing an open
    PR is attached to, not the thing that happens to be checked out. Keying on
    the DESTINATION is also what makes the gate's scope honest: a push to a ref
    no PR points at cannot update a PR, so it is not this gate's business.

    A bare `HEAD`/`@` destination (`git push origin HEAD`) means the current
    branch, so it resolves to `head_branch` — and is undeterminable when HEAD is
    detached, same as having no refspec at all."""
    resolved: list[str] = []
    for dst in explicit_destinations:
        if dst in _HEAD_ALIASES:
            if head_is_detached(head_branch):
                return None
            resolved.append(head_branch)
        elif dst:
            resolved.append(dst)
    if resolved:
        return tuple(dict.fromkeys(resolved))
    if head_is_detached(head_branch):
        return None
    return (head_branch,)


def check_push_target(targets: tuple[str, ...] | None) -> GateResult:
    """DENY a push whose destination the gate could not determine — a detached
    HEAD (mid-rebase, mid-bisect, or a failed rev-parse) with no explicit
    refspec. The gate cannot tell whether the destination is governed, so it
    cannot tell whether to enforce; skipping is the silent bypass this rule
    exists to close, and denying costs one turn plus a one-line instruction.

    Note the deny is nearly free in practice: `git push` with no refspec on a
    detached HEAD is an error in git itself ("You are not currently on a
    branch"). The agent's way out is the same refspec git would demand."""
    if targets is not None:
        return ALLOW
    return deny(
        "cannot determine which branch this push writes: HEAD is detached (a "
        "conflicted rebase, a bisect, or a failed rev-parse) and the command names "
        "no refspec, so the gate cannot tell whether the destination is a governed "
        "branch. Name the destination explicitly — e.g. "
        "'git push --force-with-lease origin HEAD:<your-branch>' — or finish/abort "
        "the rebase first ('git rebase --continue' / '--abort')."
    )


def check_red_is_red(
    commit_msg: str,
    test_cmd: str,
    run_tests: Callable[[str], TestExecution],
    enabled: bool = True,
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
    execution = run_tests(test_cmd)
    if execution.status is TestExecutionStatus.TEST_FAILED:
        return ALLOW
    if execution.status is TestExecutionStatus.TIMED_OUT:
        return deny(
            f"the test command timed out, so it cannot prove this red: commit "
            f"contains a failing test: {test_cmd}"
        )
    if execution.status is TestExecutionStatus.CANCELLED:
        return deny(
            f"the test command was cancelled, so it cannot prove this red: "
            f"commit contains a failing test: {test_cmd}"
        )
    if execution.status is TestExecutionStatus.INFRA_ERROR:
        return deny(
            f"test infrastructure failed, so it cannot prove this red: commit "
            f"contains a failing test. Check the test command and gate "
            f"configuration: {test_cmd}"
        )
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
    allow_untested: bool = False      # operator opt-out (ALLOW_UNTESTED=1)


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
    revert_and_run: Callable[[tuple[str, ...]], TestExecution],
) -> GateResult:
    """§10: with the implementation reverted, the NEW tests MUST fail (real red).
    Catches the tautological / wrong-layer test (#39 class).

    Skips (ALLOW) when: RED_MUST_FAIL=0 (explicit disable), base unresolvable, no
    new tests (§5 owns "must add a test"), or a pure-test diff (nothing to
    revert). NO test command is NOT a skip: it means this check never ran at all,
    which is exactly how #39 slipped through, so it DENIES unless the operator set
    `allow_untested` (ALLOW_UNTESTED=1) — same rule as §1, which denies first.
    `revert_and_run(impl_files) -> passed?` performs the transient revert + test
    run in the worktree and is responsible for restoring HEAD; it fails OPEN
    (returns False ⇒ red ⇒ ALLOW only via this function's logic) — see note below.
    A passing suite with the impl gone ⇒ the tests don't exercise the change ⇒ DENY.
    """
    if not inputs.red_must_fail:
        return ALLOW
    if not inputs.test_cmd:
        if inputs.allow_untested:
            return ALLOW
        return deny(
            "no test command resolved (neither .harness/test-cmd nor TEST_CMD) — the "
            "red-fail (anti-tautological-test) check cannot prove the new tests fail "
            "without the implementation. " + NO_TEST_CMD_HINT
        )
    if not inputs.base_resolvable:
        return deny(
            "the base revision cannot be resolved, so trusted red-test evidence "
            "cannot be produced; retry after restoring the base ref"
        )
    if not inputs.new_test_files:
        return ALLOW  # §5 owns the "must add a test" rule
    if not inputs.impl_files:
        return ALLOW  # pure-test diff: nothing to revert

    execution = revert_and_run(inputs.impl_files)
    if execution.status is TestExecutionStatus.INFRA_ERROR:
        return deny(
            "red-test verification hit an infrastructure error; only a genuine "
            "TEST_FAILED receipt is red evidence. Fix the verifier/test-command "
            "configuration and retry."
        )
    if execution.status is TestExecutionStatus.TIMED_OUT:
        return deny(
            "red-test verification timed out; a timeout is infrastructure failure, "
            "not evidence that the new test detects the reverted implementation"
        )
    if execution.status is TestExecutionStatus.CANCELLED:
        return deny(
            "red-test verification was cancelled; cancellation is not red evidence"
        )
    if execution.status is TestExecutionStatus.PASSED:
        return deny(
            "the new test(s) still PASS with the implementation reverted — they do "
            "not exercise the change (tautological or wrong-layer test). Assert on "
            "the observable behaviour the change produces, not on a constant that is "
            f"always true. New test file(s): {' '.join(inputs.new_test_files)}"
        )
    if execution.status is TestExecutionStatus.TEST_FAILED:
        return ALLOW
    return deny("red-test verification returned an unknown evidence status")
