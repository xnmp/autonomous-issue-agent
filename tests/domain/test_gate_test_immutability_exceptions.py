"""Behavioral contract for audited corrections to wrong existing tests."""

from issue_agent.domain.gate import (
    MAX_TEST_IMMUTABILITY_EXCEPTIONS,
    TEST_IMMUTABILITY_EXCEPTIONS,
    check_tests_immutable,
    partition_changed,
)


def _base(path: str) -> bool:
    return path == "tests/test_app.py"


def _changed(*paths: str) -> list[str]:
    return [*paths, TEST_IMMUTABILITY_EXCEPTIONS]


def test_matching_issue_scoped_exception_allows_an_existing_test_correction():
    result = check_tests_immutable(
        _changed("tests/test_app.py"),
        _base,
        exists_at_head=lambda path: path == "tests/test_app.py",
        issue_number=124,
        exception_manifest=(
            "124 | tests/test_app.py | old expectation encodes the pricing bug |\n"
        ),
    )

    assert result.ok


def test_absent_mismatched_and_malformed_declarations_fail_closed():
    changed = _changed("tests/test_app.py")
    exists_at_head = lambda path: path == "tests/test_app.py"

    assert not check_tests_immutable(
        changed, _base, exists_at_head=exists_at_head, issue_number=124
    ).ok
    mismatch = check_tests_immutable(
        changed,
        _base,
        exists_at_head=exists_at_head,
        issue_number=124,
        exception_manifest="124 | tests/test_other.py | wrong expectation |\n",
    )
    malformed = check_tests_immutable(
        changed,
        _base,
        exists_at_head=exists_at_head,
        issue_number=124,
        exception_manifest="124 | tests/test_app.py | missing final field\n",
    )
    unicode_issue = check_tests_immutable(
        changed,
        _base,
        exists_at_head=exists_at_head,
        issue_number=124,
        exception_manifest="１２４ | tests/test_app.py | Unicode issue digits |\n",
    )

    assert not mismatch.ok
    assert not malformed.ok and "malformed" in malformed.reason
    assert not unicode_issue.ok and "invalid issue" in unicode_issue.reason


def test_declaration_must_name_the_current_branch_issue():
    result = check_tests_immutable(
        _changed("tests/test_app.py"),
        _base,
        exists_at_head=lambda _path: True,
        issue_number=125,
        exception_manifest="124 | tests/test_app.py | old expectation was wrong |\n",
    )

    assert not result.ok
    assert "issue #125" in result.reason


def test_inherited_manifest_is_inactive_on_the_next_issue_branch():
    inherited = (
        "124 | tests/test_app.py | old expectation was wrong on issue 124 |\n"
    )

    ordinary_change = check_tests_immutable(
        ["src/app.py"],
        _base,
        issue_number=125,
        exception_manifest=inherited,
    )
    undeclared_test_change = check_tests_immutable(
        ["tests/test_app.py"],
        _base,
        exists_at_head=lambda _path: True,
        issue_number=125,
        exception_manifest=inherited,
    )

    assert ordinary_change.ok
    assert not undeclared_test_change.ok
    assert "existing test file" in undeclared_test_change.reason


def test_deletion_requires_a_new_changed_replacement_test():
    manifest = (
        "124 | tests/test_app.py | moved focused coverage "
        "| tests/test_app_regression.py\n"
    )
    changed = _changed("tests/test_app.py", "tests/test_app_regression.py")

    allowed = check_tests_immutable(
        changed,
        _base,
        exists_at_head=lambda path: path == "tests/test_app_regression.py",
        issue_number=124,
        exception_manifest=manifest,
    )
    denied = check_tests_immutable(
        _changed("tests/test_app.py"),
        _base,
        exists_at_head=lambda _path: False,
        issue_number=124,
        exception_manifest="124 | tests/test_app.py | wrong expectation |\n",
    )

    assert allowed.ok
    assert not denied.ok and "replacement" in denied.reason


def test_deletion_rejects_unchanged_or_non_test_replacements():
    changed = _changed("tests/test_app.py", "tests/test_old.py", "src/app.py")
    base = lambda path: path in {"tests/test_app.py", "tests/test_old.py"}
    head = lambda path: path in {"tests/test_old.py", "src/app.py"}

    unchanged = check_tests_immutable(
        changed,
        base,
        exists_at_head=head,
        issue_number=124,
        exception_manifest=(
            "124 | tests/test_app.py | moved coverage | tests/test_old.py\n"
        ),
    )
    non_test = check_tests_immutable(
        changed,
        base,
        exists_at_head=head,
        issue_number=124,
        exception_manifest="124 | tests/test_app.py | moved coverage | src/app.py\n",
    )

    assert not unchanged.ok
    assert not non_test.ok


def test_declared_corrected_test_stays_present_during_implementation_revert():
    tests, implementation = partition_changed(
        ["src/app.py", "tests/test_app.py", TEST_IMMUTABILITY_EXCEPTIONS],
        lambda path: path in {"src/app.py", "tests/test_app.py"},
        declared_test_paths=("tests/test_app.py",),
    )

    assert tests == ("tests/test_app.py",)
    assert implementation == ("src/app.py", TEST_IMMUTABILITY_EXCEPTIONS)


def test_unwired_head_resolver_loses_the_exception_instead_of_skipping_deletion():
    """The deletion branch must fail CLOSED when its resolver is not wired.

    `exists_at_head` gates the strictest rule in this check — a declared
    deletion has to name a replacement test. A permissive default let a caller
    that forgot the callback delete an existing test with no replacement.
    """
    changed = _changed("tests/test_app.py", TEST_IMMUTABILITY_EXCEPTIONS)
    manifest = "124 | tests/test_app.py | expectation was wrong |\n"

    unwired = check_tests_immutable(
        changed, _base, issue_number=124, exception_manifest=manifest
    )
    wired = check_tests_immutable(
        changed,
        _base,
        exists_at_head=lambda _path: True,
        issue_number=124,
        exception_manifest=manifest,
    )

    assert not unwired.ok
    assert "replacement" in unwired.reason
    assert wired.ok


def test_one_change_may_not_except_more_tests_than_the_bound():
    """Per-file scoping bounds WHICH tests; this bounds HOW MANY.

    Without it a single manifest neutralises the immutability gate for a whole
    change, leaving reviewer judgement as the only remaining backstop.
    """
    over = MAX_TEST_IMMUTABILITY_EXCEPTIONS + 1
    paths = [f"tests/test_{i}.py" for i in range(over)]
    manifest = "".join(f"124 | {path} | expectation was wrong |\n" for path in paths)
    at_bound = "".join(
        f"124 | {path} | expectation was wrong |\n"
        for path in paths[:MAX_TEST_IMMUTABILITY_EXCEPTIONS]
    )
    base = set(paths).__contains__
    head = lambda _path: True  # noqa: E731

    denied = check_tests_immutable(
        _changed(*paths, TEST_IMMUTABILITY_EXCEPTIONS),
        base,
        exists_at_head=head,
        issue_number=124,
        exception_manifest=manifest,
    )
    allowed = check_tests_immutable(
        _changed(*paths[:MAX_TEST_IMMUTABILITY_EXCEPTIONS], TEST_IMMUTABILITY_EXCEPTIONS),
        base,
        exists_at_head=head,
        issue_number=124,
        exception_manifest=at_bound,
    )

    assert not denied.ok
    assert "too many test-immutability exceptions" in denied.reason
    assert allowed.ok


def test_inherited_manifest_must_be_replaced_and_the_denial_says_so():
    """Appending to another issue's manifest is the second use of this feature.

    The denial has to name the remedy: the inherited lines are audit history in
    git and must be replaced, not extended.
    """
    changed = _changed("tests/test_app.py", TEST_IMMUTABILITY_EXCEPTIONS)
    appended = check_tests_immutable(
        changed,
        _base,
        exists_at_head=lambda _path: True,
        issue_number=124,
        exception_manifest=(
            "13 | tests/test_legacy.py | an earlier issue corrected this |\n"
            "124 | tests/test_app.py | expectation was wrong |\n"
        ),
    )
    replaced = check_tests_immutable(
        changed,
        _base,
        exists_at_head=lambda _path: True,
        issue_number=124,
        exception_manifest="124 | tests/test_app.py | expectation was wrong |\n",
    )

    assert not appended.ok
    assert "replace its contents with declarations for #124 only" in appended.reason
    assert replaced.ok
