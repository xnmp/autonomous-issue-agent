# Invariants — calculator service

Checkable rules the fix and review agents MUST obey. These are the "different
prior" that defeats shared-model blind spots (see steward design).

1. **Operations are pure.** No I/O, no global mutable state, no logging side
   effects inside `src/calc/operations.py`. A function that touches the network,
   the filesystem, or a module-level mutable is a bug.
2. **Division by zero is an explicit, typed failure.** `divide(a, 0)` must raise
   `ValueError` with a message naming the operation — never a bare
   `ZeroDivisionError`, never `inf`/`nan`, never a silent `0`.
3. **Existing tests are immutable.** A change that edits or deletes a test in
   `tests/test_operations.py` is a bug; add new tests instead.
4. **Full type annotations.** Every public function keeps its parameter and
   return annotations.
