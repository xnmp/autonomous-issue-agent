# Increment 8 continuation conventions

The parent owns CLI wiring, runtime cutover, shared ports and legacy deletion.
Activation implementation owns `domain/review_activation.py`,
`adapters/review_activation.py`, `app/review_activation.py` and their tests.
The control-hardening implementation owns review command acceptance and its
adapter tests; coordinate any shared signature change with the parent first.

Reuse immutable domain variants, existing SQLite workflow/activation tables,
deployment barriers in fix/review/steward order, and the existing durable
executor/outbox. No new queue or authority, no schema change without parent
agreement. Keep remote I/O outside SQL transactions. Mirror resolve activation
and its liveness checks; retain every source and decision in the digest.

Read CLAUDE.md, the review-slice handover, the review plan and ADR 0015.
Use uv, Pyrefly, isolated fixtures and apply_patch. No live services, target
checkout writes, paid runs, real-data activation, remote publication or agent
commits. Tests assert observable behavior, including malformed/absent inputs.
Do not spawn background child agents. Shared checkout edits are immediately
visible; avoid other assignments' files. A worktree must first verify its
merge-base against the current dev tip; the parent rechecks before integration.

Full make test and fresh independent adversarial review precede integration.
