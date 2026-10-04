# Phase 5 operator-absent replay catalog

Source corpus: [pre-overhaul observational baseline](../reference/architecture-overhaul-baseline-2026-09-14.json). Incident labels below preserve that corpus's audit/escaped distinction. These are bounded transport/provider simulations through production daemon, durable executor and SQLite authority, with `PersistentGitHub` preserving remote facts across restart. They are not live-window evidence or exact reproduction of every original target detail.

| Family / source | Actual composed cases | Observable outcome |
| --- | --- | --- |
| held-target-dispatch / audit | `test_partial_relabel_preserves_hold_and_never_pays_after_restart` | Partial label success survives process loss; human hold remains; provider never runs. |
| stale-completion-disposition / audit | `test_hold_added_during_resolve_completion_survives_restart`; `test_paid_attempt_settlement_is_conserved_when_completion_receipt_is_lost` | New human hold survives obsolete completion; receipt-loss replay reaches verified disposition with one original paid attempt/charge and no repurchase. |
| branch-publication-identity / escaped | `test_recorded_publication_identity_replay_is_bounded_without_operator` (mismatched/matched twins) | Matched context pays once and observes green remote checks. Mismatched context refuses before paid work and reaches a durable actionable PR hold across restart with zero provider calls/charges. |
| repair-context-revision / escaped | `test_recorded_repair_context_replay_uses_current_head_after_restart` | A legitimate free head-A rerun is settled; after restart head B gets its own one verified rerun. Actual paid repair sees pinned B log/current check and consumed-B context plus stale A history. Remote checks finish green; one charge remains and no active reservation survives. |

All cases live in `tests/app/test_durable_workflow_composition.py`; no separate replay runner or authority was added. The persistent transport stores exact head/run logs and run-attempt observations in its existing settings table. Tests advance rerun attempts explicitly after queue acceptance, preserving GitHub's asynchronous confirmation boundary.

Run:

```sh
uv run pytest tests/app/test_durable_workflow_composition.py -q
```

The publication refusal is harness-captured before admission and reuses the durable PR recovery comment/label effect. Additional `test_publication_refusal_*` cases verify unsent restart drain, changed head/branch/generation/unique linkage refusal, persisted unavailable-read backoff across restart, visible evidence/uncertainty gates at the existing ceiling and recovery closing only its own gate, distinct prior CI cause, provider status unable to mint a harness remedy, and comment-before-head-change withholding the stale label. Matched publication and fresh head-B repair retain one conserved charge; no synthetic paid failure or new counter is recorded.

Passing controlled cases establishes those stated outcomes only. It does not release the deployment pause or accept repeated unattended windows. Root owns the separate supported-profile/window proposal.

Independent review passed all eighteen publication recovery contracts on
2026-10-04. The complete milestone gate passed at 12:22 UTC: 10,341 Python
cases, hook checks, parity, architecture contracts and both type checks.
Publication retry grants
remain unsupported; the interval between a fresh read and GitHub mutation
remains a remote API race.
