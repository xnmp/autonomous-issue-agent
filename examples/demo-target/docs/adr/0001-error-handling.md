# 0001. Arithmetic errors are explicit ValueErrors
Status: Accepted
Governs: src/calc/operations.py
Context: Callers need to distinguish a domain error (e.g. divide by zero) from a
programming error. A bare ZeroDivisionError leaks an implementation detail and
is easy to swallow.
Decision: Domain-invalid inputs raise `ValueError` with a message that names the
operation and the offending argument. We do not return sentinel values (inf/nan/None).
Consequences: Callers catch `ValueError` for recoverable domain errors. Any PR
that touches `src/calc/operations.py` must reference this ADR (the PR gate enforces it).
