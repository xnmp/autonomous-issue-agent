"""Existing-test detection in a diff, as the reviewer's notice reads it."""

import pytest

from issue_agent.domain.diff_tests import looks_like_test, modified_existing_tests_from_diff


def test_rust_test_files_are_recognized_as_tests():
    """Rust integration tests are bare .rs files under tests/ (each compiled
    as its own crate by cargo) — keifu #155's tests/help_popup_scroll_test.rs
    was missed by the .py-only tests/ arm (2026-08-01)."""
    assert looks_like_test("tests/help_popup_scroll_test.rs")
    assert looks_like_test("tests/scroll.rs")
    assert looks_like_test("src/app/mouse_test.rs")
    assert not looks_like_test("src/app/mouse.rs")
    assert not looks_like_test("tests/fixtures/data.json")


@pytest.mark.parametrize("record,path", [
    ("diff --git a/tests/test_old.py b/tests/test_old.py\n--- a/tests/test_old.py\n+++ b/tests/test_old.py\n", "tests/test_old.py"),
    ("diff --git a/tests/test_gone.py b/tests/test_gone.py\ndeleted file mode 100644\n", "tests/test_gone.py"),
    ("diff --git a/tests/test_old.py b/docs/moved.py\nrename from tests/test_old.py\nrename to docs/moved.py\n", "tests/test_old.py"),
    ("diff --git a/tests/test_binary.py b/tests/test_binary.py\nBinary files differ\n", "tests/test_binary.py"),
    ("diff --git a/tests/test_mode.py b/tests/test_mode.py\nold mode 100644\nnew mode 100755\n", "tests/test_mode.py"),
    ('diff --git "a/tests/test_caf\\303\\251.py" "b/tests/test_caf\\303\\251.py"\n', "tests/test_café.py"),
    ('diff --git "a/tests/test_caf\\303\\251.py" b/docs/moved.py\nrename from "tests/test_caf\\303\\251.py"\n', "tests/test_café.py"),
    ("diff --git a/tests/my test.py b/tests/my test.py\nBinary files differ\n", "tests/my test.py"),
    ("diff --git a/tests/@scope/test_old.py b/tests/@scope/test_old.py\n", "tests/@scope/test_old.py"),
])
def test_existing_test_listing_covers_git_record_shapes(record, path):
    assert modified_existing_tests_from_diff(record) == (path,)


@pytest.mark.parametrize("diff", [None, 42, b"diff --git a/tests/test_x.py b/tests/test_x.py", [], "", "not a diff", 'diff --git "unterminated\n'])
def test_existing_test_listing_tolerates_absent_and_malformed_diff(diff):
    assert modified_existing_tests_from_diff(diff) == ()


def test_existing_test_listing_excludes_new_files_and_source_and_deduplicates():
    diff = (
        "diff --git a/tests/test_new.py b/tests/test_new.py\nnew file mode 100644\n"
        "diff --git a/src/main.py b/src/main.py\n"
        "diff --git a/tests/test_b.py b/tests/test_b.py\n"
        "diff --git a/tests/test_a.py b/tests/test_a.py\n"
        "diff --git a/tests/test_b.py b/tests/test_b.py\n"
    )
    assert modified_existing_tests_from_diff(diff) == ("tests/test_b.py", "tests/test_a.py")


def test_existing_test_listing_has_no_grant_scope_or_path_limits():
    paths = tuple(f"tests/@scope/{'x' * 250}/test_{n}.py" for n in range(1000))
    diff = "".join(f"diff --git a/{p} b/{p}\n" for p in paths)
    assert modified_existing_tests_from_diff(diff) == paths


@pytest.mark.parametrize("suffix", ["test.js", "spec.js", "test.jsx", "spec.ts", "test.tsx", "spec.mjs", "test.cjs", "spec.mts", "test.cts"])
def test_colocated_javascript_test_edits_are_prominent(suffix):
    path = f"src/widget.{suffix}"
    diff = (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            "@@ -1 +1 @@\n-expect(render()).toEqual(42)\n+expect(true).toBe(true)\n")
    assert modified_existing_tests_from_diff(diff) == (path,)


@pytest.mark.parametrize("path", ["src/widget.ts", "src/widget.test.ts.map", "src/widget.spec.json"])
def test_colocated_test_detection_excludes_source_and_artifacts(path):
    assert modified_existing_tests_from_diff(f"diff --git a/{path} b/{path}\n") == ()

