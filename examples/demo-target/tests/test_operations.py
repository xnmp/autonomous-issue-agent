"""Baseline tests that already pass. The fix agent ADDS to these (it must not
modify or delete existing tests — the AGENT.md hard rules + reviewer enforce it)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from calc import add, subtract, multiply, divide  # noqa: E402


def test_add():
    assert add(2, 3) == 5


def test_subtract():
    assert subtract(5, 3) == 2


def test_multiply():
    assert multiply(4, 3) == 12


def test_divide_happy_path():
    assert divide(10, 2) == 5
