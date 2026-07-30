"""Behavioral contract for audited corrections to wrong existing tests."""

from issue_agent.domain.gate import (
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


def test_declared_corrected_test_stays_present_during_implementation_revert():
    tests, implementation = partition_changed(
        ["src/app.py", "tests/test_app.py", TEST_IMMUTABILITY_EXCEPTIONS],
        lambda path: path in {"src/app.py", "tests/test_app.py"},
        declared_test_paths=("tests/test_app.py",),
    )

    assert tests == ("tests/test_app.py",)
    assert implementation == ("src/app.py", TEST_IMMUTABILITY_EXCEPTIONS)
