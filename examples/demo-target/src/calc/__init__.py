"""A tiny calculator service — the demo target the harness resolves issues against.

Deliberately small and pure so a headless fix agent can implement seeded issues
end-to-end (red → green) and a reviewer can verify behaviour from the diff alone.
"""
from .operations import add, subtract, multiply, divide

__all__ = ["add", "subtract", "multiply", "divide"]
