"""#124 reviewer-granted test-immutability exemption: grant grammar + merge rule.

The parser reads UNTRUSTED text (anyone can comment on a PR), so its tests are
mostly adversarial: digit spoofing, malformed lines, junk input. The merge
predicate is fail-closed on every axis except the anti-vacuity one: a PR with
no declarations must be completely unaffected.
"""

from builders import *  # noqa: F401,F403  (sys.path setup)

from issue_agent.domain.exemptions import (
    MAX_TEST_IMMUTABILITY_EXCEPTIONS,
    ExemptionGrant,
    ExemptionFacts,
    check_declaration_scope,
    exemption_permits_merge,
    parse_exemption_grants,
)
from issue_agent.domain.review import IdentityMode


GRANT = "test-immutability-exemption: granted issue=89 file=tests/adapters/test_codex_runner.py patch=abc123"


# ── grant grammar ─────────────────────────────────────────────────────────────
def test_parses_the_canonical_grant_line():
    assert parse_exemption_grants(GRANT) == (
        ExemptionGrant(89, "tests/adapters/test_codex_runner.py", "abc123"),
    )


def test_grants_are_found_anywhere_in_a_body_one_per_line():
    body = (
        "Looks correct — the old assertion encoded the defect.\n"
        "\n"
        f"{GRANT}\n"
        "test-immutability-exemption: granted issue=89 file=tests/test_b.py patch=abc123\n"
        "Please also rename the helper.\n"
    )
    grants = parse_exemption_grants(body)
    assert [g.file for g in grants] == [
        "tests/adapters/test_codex_runner.py",
        "tests/test_b.py",
    ]
    assert all(g.issue == 89 and g.patch == "abc123" for g in grants)


def test_leading_and_trailing_whitespace_is_tolerated():
    assert parse_exemption_grants(f"   {GRANT}  \t") != ()


def test_unicode_digit_spoof_in_issue_is_rejected():
    # Python's \d and int() both accept Unicode digits — the adversarial case
    # from PR #134's review. Arabic-Indic and fullwidth digits must not parse.
    for spoof in ("١٢٣", "１２３", "8٩"):
        line = f"test-immutability-exemption: granted issue={spoof} file=tests/t.py patch=abc"
        assert parse_exemption_grants(line) == ()


def test_malformed_lines_are_ignored_not_guessed_at():
    malformed = [
        "test-immutability-exemption: granted issue=89 file=tests/t.py",  # no patch
        "test-immutability-exemption: granted file=tests/t.py patch=a issue=89",  # order
        "test-immutability-exemption: denied issue=89 file=tests/t.py patch=a",
        "test-immutability-exemption granted issue=89 file=tests/t.py patch=a",  # no colon
        f"> {GRANT}",  # markdown-quoted (someone quoting a grant is not granting)
        f"{GRANT} please",  # trailing prose
        "test-immutability-exemption: granted issue= file=tests/t.py patch=a",
        "test-immutability-exemption: granted issue=89 file= patch=a",
        "test-immutability-exemption: granted issue=9999999999 file=t.py patch=a",  # >9 digits
    ]
    for line in malformed:
        assert parse_exemption_grants(line) == (), line
    # A malformed line does not poison well-formed neighbours.
    body = malformed[0] + "\n" + GRANT
    assert len(parse_exemption_grants(body)) == 1


def test_parser_is_total_on_junk_input():
    for junk in (None, 42, b"granted", ["x"], ""):
        assert parse_exemption_grants(junk) == ()


# ── merge withholding predicate ──────────────────────────────────────────────
def _permits(facts, **kw):
    kw.setdefault("issue_number", 89)
    kw.setdefault("identity_mode", IdentityMode.THREE)
    kw.setdefault("fix_bot", "fix-bot")
    kw.setdefault("review_bot", "review-bot")
    return exemption_permits_merge(facts, **kw)


DECLARED = ExemptionFacts(
    declared=("tests/test_a.py",),
    grants=(ExemptionGrant(89, "tests/test_a.py", "patch-1"),),
    patch_id="patch-1",
)


def test_no_declarations_means_completely_unaffected():
    # Anti-vacuity twin: even in solo mode with no identities at all, an
    # undeclared PR passes — the rule must never block the ordinary path.
    empty = ExemptionFacts()
    assert _permits(empty) is True
    assert _permits(empty, identity_mode="solo", fix_bot="", review_bot="") is True
    assert _permits(empty, issue_number=None) is True


def test_matching_grant_permits_merge():
    assert _permits(DECLARED) is True


def test_declared_without_grant_withholds():
    assert _permits(ExemptionFacts(declared=("tests/test_a.py",),
                                       patch_id="patch-1")) is False


def test_every_declared_file_needs_its_own_grant():
    two = ExemptionFacts(
        declared=("tests/test_a.py", "tests/test_b.py"),
        grants=(ExemptionGrant(89, "tests/test_a.py", "patch-1"),),
        patch_id="patch-1",
    )
    assert _permits(two) is False


def test_grant_for_wrong_file_issue_or_patch_does_not_authorize():
    for grant in (
        ExemptionGrant(89, "tests/test_OTHER.py", "patch-1"),
        ExemptionGrant(90, "tests/test_a.py", "patch-1"),
        ExemptionGrant(89, "tests/test_a.py", "patch-STALE"),
    ):
        facts = ExemptionFacts(declared=("tests/test_a.py",),
                                   grants=(grant,), patch_id="patch-1")
        assert _permits(facts) is False, grant


def test_new_push_voids_an_existing_grant():
    # Same grant, but the PR's diff moved on: patch id no longer matches.
    moved = ExemptionFacts(declared=DECLARED.declared,
                               grants=DECLARED.grants, patch_id="patch-2")
    assert _permits(moved) is False


def test_no_exemption_path_outside_three_identity():
    # IdentityMode has no SINGLE member; a raw "single" string still reaches the
    # comparison via hand-built configs and must withhold the same way.
    for mode in (IdentityMode.SOLO, "solo", "single"):
        assert _permits(DECLARED, identity_mode=mode) is False


def test_never_self_authorize():
    assert _permits(DECLARED, fix_bot="same-bot", review_bot="same-bot") is False
    # [bot]-suffix asymmetry must not defeat the sameness check (login_eq).
    assert _permits(DECLARED, fix_bot="same-bot", review_bot="same-bot[bot]") is False


def test_unknown_identities_fail_closed():
    assert _permits(DECLARED, fix_bot="") is False
    assert _permits(DECLARED, review_bot="") is False


def test_unverifiable_facts_fail_closed():
    assert _permits(DECLARED, issue_number=None) is False
    no_patch = ExemptionFacts(declared=DECLARED.declared,
                                  grants=DECLARED.grants, patch_id="")
    assert _permits(no_patch) is False


def test_scope_bound_withholds_regardless_of_grants():
    files = tuple(f"tests/test_{i}.py" for i in range(MAX_TEST_IMMUTABILITY_EXCEPTIONS + 1))
    facts = ExemptionFacts(
        declared=files,
        grants=tuple(ExemptionGrant(89, f, "patch-1") for f in files),
        patch_id="patch-1",
    )
    assert _permits(facts) is False
    assert check_declaration_scope(files) is False
    assert check_declaration_scope(files[:MAX_TEST_IMMUTABILITY_EXCEPTIONS]) is True


def test_extra_grants_for_undeclared_files_change_nothing():
    padded = ExemptionFacts(
        declared=DECLARED.declared,
        grants=DECLARED.grants + (ExemptionGrant(89, "tests/test_z.py", "patch-1"),),
        patch_id="patch-1",
    )
    assert _permits(padded) is True
