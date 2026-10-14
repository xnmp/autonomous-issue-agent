# Demo target — calculator service

A minimal, pure-Python package used to exercise the autonomous issue agent
end-to-end. Keep it tiny: the point is to watch the issue → branch → red → green
→ PR → review → merge loop, not to build a real product.

## Layout
- `src/calc/operations.py` — pure arithmetic functions. No I/O, no globals.
- `tests/` — pytest. `test_operations.py` is the baseline; agents ADD files here.

## Conventions (the fix agent must follow these)
- Read `INVARIANTS.md` before coding: its rules override your defaults. If one
  seems wrong, block and explain; never violate or work around it.
- Tests are pytest, discovered as `tests/test_*.py` or `*_regression.py`.
- Functions are pure and fully type-annotated.
- A `fix/` branch must add a regression test that fails before the fix and passes
  after (red → green; the commit-order hook enforces it).
- Do not modify or delete `test_operations.py`.

## Commands (also set in config/repos/<name>.env for this target)
- Tests:     `pytest -q`
- Lint:      `python -m pyflakes src tests`  (if installed; else a no-op)
- Typecheck: (none configured for the demo)
