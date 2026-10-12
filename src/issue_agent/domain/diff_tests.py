"""Which existing test files a unified diff modifies or deletes.

The reviewer is told about these so it judges each change explicitly for
skipped or weakened coverage; the list is informational and never gates.
"""

from __future__ import annotations

import re

TEST_FILE_RE = re.compile(
    r"(e2e|integration|regression|test|spec).*\.(spec|test)\."
    r"|\.(spec|test)\.[cm]?[jt]sx?$"
    r"|(^|/)test_.*\.py"
    r"|.*_test\.(py|go|ts|js|rs)"
    # Rust integration tests are BY CONVENTION bare .rs files under tests/
    # (cargo compiles each as its own test crate); keifu #155 (2026-08-01).
    r"|tests?/.*\.(py|rs)"
)


def looks_like_test(path: str) -> bool:
    return bool(TEST_FILE_RE.search(path))


# Record headers (`diff --git`) rather than `--- a/` lines, because binary
# files, pure renames, mode-only changes, and paths suppressed by
# `.gitattributes -diff` emit NO `--- a/` line.
_DIFF_RECORD_RE = re.compile(r"^diff --git (.+)$", re.MULTILINE)
_DIFF_NEW_FILE_RE = re.compile(r"^new file mode ", re.MULTILINE)
_DIFF_RENAME_FROM_RE = re.compile(r"^rename from (.+)$", re.MULTILINE)


def _unquote_diff_path(raw: str, *, prefixed: bool = True) -> str | None:
    """Decode Git C-quoted paths; header paths carry an a/ or b/ prefix."""
    raw = raw.strip()
    if raw.startswith('"'):
        quoted = re.match(r'"((?:[^"\\]|\\.)*)"', raw)
        end = quoted.end() - 1 if quoted else -1
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
        return decoded[2:] if prefixed and decoded.startswith(("a/", "b/")) else decoded
    return raw[2:] if prefixed and raw.startswith(("a/", "b/")) else raw


def modified_existing_tests_from_diff(
    diff_text: object,
) -> tuple[str, ...]:
    """Existing test paths modified or deleted, in diff order, for review.

    The old side includes renames out of the suite. Record headers also cover
    binary, mode-only and attribute-suppressed changes without hunk headers.
    Unresolvable records carry no path.
    """
    if not isinstance(diff_text, str):
        return ()
    subjects: dict[str, None] = {}
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
            old = _unquote_diff_path(rename.group(1), prefixed=False)
        else:
            header = match.group(1)
            # `a/<p> b/<p>` for the common same-path case: take the a-side by
            # splitting at the LAST ` b/` so paths containing " b/" resolve.
            split = header.rfind(" b/")
            candidate = header[:split] if split > 0 else header
            old = _unquote_diff_path(candidate) if split > 0 or header.startswith('"') else None
            if old is None:
                minus = re.search(
                    r"^--- (.+)$", body, re.MULTILINE
                )
                if minus and minus.group(1) != "/dev/null":
                    old = _unquote_diff_path(minus.group(1).rstrip("\t"))
        if old is None:
            continue
        if looks_like_test(old):
            subjects.setdefault(old)
    return tuple(subjects)
