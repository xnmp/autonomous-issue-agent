"""Compatibility exports for the domain-owned untrusted-content boundary."""

from __future__ import annotations

from ..domain.trust import (
    Untrusted,
    as_untrusted,
    encode_untrusted,
    untrusted_block,
)

__all__ = [
    "Untrusted",
    "as_untrusted",
    "encode_untrusted",
    "untrusted_block",
]
