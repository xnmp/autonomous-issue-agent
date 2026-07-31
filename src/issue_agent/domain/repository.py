"""Canonical repository identity shared by authority and telemetry values."""

from __future__ import annotations

import re


_OWNER = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?")
_NAME = re.compile(r"[a-z0-9._-]{1,100}")


def canonical_repository(value: str) -> str:
    """GitHub's case-insensitive ``owner/name`` identity.

    Validation is separate so incomplete config can still be represented and
    reported by the normal startup diagnostics.
    """

    stripped = (value or "").strip()
    if stripped == "*":
        return stripped
    return stripped.casefold()


def repository_validation_error(value: str) -> str:
    """An operator-facing error, or empty when ``value`` is one canonical slug."""

    repository = canonical_repository(value)
    # Legacy tests and explicit non-GitHub adapters use a single opaque name.
    # Canonical GitHub aliases are rejected only once the value is slug-shaped.
    if "/" not in repository and "://" not in repository:
        return ""
    parts = repository.split("/")
    if len(parts) != 2 or not all(parts):
        return "must be exactly owner/name"
    owner, name = parts
    if _OWNER.fullmatch(owner) is None:
        return "has an invalid GitHub owner"
    if (
        _NAME.fullmatch(name) is None
        or name in {".", ".."}
        or name.endswith(".git")
    ):
        return "has an invalid or URL-shaped GitHub repository name"
    return ""


def repository_equal(left: str, right: str) -> bool:
    return canonical_repository(left) == canonical_repository(right)
