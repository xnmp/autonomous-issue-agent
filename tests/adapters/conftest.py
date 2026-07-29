"""Make src/ importable for the adapter tests."""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SRC = os.path.join(_ROOT, "src")
_DOMAIN_TESTS = os.path.join(_ROOT, "tests", "domain")
if _DOMAIN_TESTS not in sys.path:
    sys.path.insert(0, _DOMAIN_TESTS)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
