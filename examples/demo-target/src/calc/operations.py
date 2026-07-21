"""Pure arithmetic operations.

NOTE for the fix agent: `divide` currently has a known bug (it does not handle
division by zero) — that is issue #1's job to fix, test-first. Do not "fix" it
pre-emptively without writing the failing regression test first; the commit-order
hook enforces red-before-green.
"""
from __future__ import annotations


def add(a: float, b: float) -> float:
    return a + b


def subtract(a: float, b: float) -> float:
    return a - b


def multiply(a: float, b: float) -> float:
    return a * b


def divide(a: float, b: float) -> float:
    # BUG (issue #1): raises an unhelpful ZeroDivisionError with no context, and
    # the contract for b == 0 is undefined. The fix should make this explicit.
    return a / b
