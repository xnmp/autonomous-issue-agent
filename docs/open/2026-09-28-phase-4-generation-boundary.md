# Phase 4 — execution state across rejection generation advances

Date: 2026-09-28 Australia/Sydney. Pure domain model and design finding; no
production path or execution-family activation changed. Read with the [execution design](2026-09-27-phase-4-review-execution-design.md)
and [productive settlement handover](2026-09-28-phase-4-post-productive-settlement-handover.md).

## Reproduced blocker

An active execution component is bound to the rejection workflow's generation.
Three pure rejection transitions advance that generation while retaining the
old component: `request_review_rearm`, `continue_review_rearm`, and the ceiling
release in `reconcile_review_ceiling`. A temporary public SQLite-control
regression opened a ceiling with a confirmed copied escalation, then called
`request_rearm`. It raised `ValueError: execution component disagrees with the
review workflow binding` from `ReviewWorkflowSnapshot.__post_init__` before
acceptance. The test was removed after reproduction because the branch must
remain green until the complete transition is implemented. No live execution
family is activated.

The straightforward binding replacement is incorrect. The execution checkpoint
still starts at sequence zero, while a finalized old-generation outcome can
already have changed failures, pin deferrals, session, or the productive
watermark. Replaying that suffix under a new binding currently ignores **all**
older-generation outcomes, so canonical reload loses the state being carried.
Applying all old outcomes instead lets a round that settles after rearm change
the new allowance. An old round may still be open at the rearm; moving the
contiguous checkpoint across it falsely declares it finalized and the loader
correctly refuses that state. The existing test helper
`_advance_generation_with_open_round` manually resets to the initial state and
therefore does not exercise preservation of an already counted failure.

## Required model

Introduce a typed, immutable **generation boundary**, separate from the
contiguous finalized checkpoint. It records the old and new bindings, the
last allocated round sequence at acceptance, the source-verified execution
state carried into the new generation, the exact finalized suffix outcomes
applied before acceptance, and an immutable receipt identity. Mint the boundary
in the same SQLite transaction as each rejection generation advance. Carry
failures, pin deferrals, displayed round, and productive watermark. Clear the
reusable reviewer session because rearm owes a fresh review. Keep execution
epoch and episode unchanged; rejection rearm must not renew the execution
allowance.

Project rounds allocated after the newest boundary from its carried state.
Rounds allocated before it but finalized later settle charges and retain their
verified outcome receipts as audit only. Retain earlier boundaries so exact
replay can distinguish outcomes applied before each advance from late ones,
including repeated rearm and release. Do not infer that distinction from the
current row status, wall-clock timestamps, or generation alone. The pure
projection should verify each boundary against the immediately preceding
projection and the captured finalized outcome identities. The adapter must
mint the **complete** applied set from all finalized source receipts at
acceptance, including outcomes with no allowance effect; the pure function
cannot infer when a later-settled receipt was written. The boundary records the
exact prior and new workflow revisions and advancing mutation. Its receipt
must bind those values to the accepted transition.

Canonical reads must verify the exact boundary receipt, the matching
rejection revision and execution episode, the allocated sequence, every
uncompacted round source, and the bounded suffix projection. Open older rounds
remain outside the contiguous checkpoint. Current reads may continue to
validate the entire bounded suffix while the source-backed activation/import
anchor and compaction audit contract are built. An ordinary hot read must not
eventually walk lifetime history; the existing 4,096-round suffix ceiling
remains an activation gate until compaction is finished. A checkpoint must
never use a generation boundary as proof that an older open round finalized.

## Acceptance cases

1. Public SQLite-control tests for all three advancing transitions retain the
   active execution binding, failures, pin streak, and productive watermark.
   A launched reusable session becomes fresh; an imported session is treated
   under the same fresh-review rule.
2. A failure finalized before rearm survives restart and a second generation
   advance. A failure or productive result from an old open round that closes
   after rearm remains audit only. New-generation outcomes still update state.
3. Open earlier rounds prevent contiguous checkpoint advancement without
   preventing authorized rejection recovery. Their later closure cannot change
   the carried state or its boundary receipt.
4. Missing, changed, duplicated, foreign-episode, future-generation, or
   out-of-order boundary and source receipts fail closed. Replay of the exact
   operator command is inert; interrupted acceptance rolls back boundary,
   workflow, intent, commands, and outbox together.
5. A bounded canonical read, prefix compaction, and recovery scan remain
   bounded after many rounds and repeated generation advances. Compare full
   replay with replay from a source-backed compacted prefix.

The pure `ReviewExecutionGenerationBoundary`, carry, and replay functions now
model this distinction without making an open prefix into a checkpoint. Tests
cover prior debits, late old outcomes, multiple advances, session clearing,
malformed sources, and replay from a verified prefix inside a boundary. The
closed workflow codec also round-trips the boundary and rejects missing
transition fields. This is a model-and-codec checkpoint: adapters do not mint boundary receipts or attach
them to the active aggregate yet. Generation-aware compaction, source-set
completeness, activation/import anchoring, and the three advancing transitions
remain implementation work.

Independent GPT-6 Sol review confirmed that rebinding alone loses prior debits
or admits late old-generation effects. Its first code pass found a compacted
replay bug and future-binding omission; both have regressions. A later pass
identified revision provenance and quadratic interval checks, now addressed
in the pure model. Final re-review found no remaining medium or high defect
in this model scope.
