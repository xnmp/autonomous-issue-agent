# Phase 5/6 execution

**Current live checkpoint — October 5:** offline Phase 5 and the declared
Phase 6 contracts are accepted. All 37 Phase 4 tasks and the epic are closed;
the schema-40/family cutover, retained-obligation dispositions and approved
lower-cap configuration are applied. The bounded cancellation canary settled
USD 0.632080, all roles stopped, and its measurement-reader correction has been
independently reviewed. Current qualification and the remaining two-window
acceptance are in the
[live execution record](2026-10-05-phase-5-live-execution.md). Earlier
deployment-pause statements below describe their original checkpoints.

The architecture plan is [2026-09-14-architecture-implementation.md](2026-09-14-architecture-implementation.md). Phase 4 is accepted at `0518c27`; its roadmap/evidence packaging is `34a622c`. The user authorized continuing Phases 5 and 6 on 2026-10-04.

Work proceeds in three coherent implementation milestones. These are local work units, not new remote issues or a new workflow engine.

1. **Phase 5 measurement and replay:** add prospective incident/intervention records and comparable-cohort readouts using existing events, SQL runs/admissions/charges, dedupe and outbox. Include external interventions, diagnosis, mechanical actions, product decisions, unknown duration/coverage, and parked/abandoned/unmerged work. Replay selected recorded families through existing durable composition without an operator. Prepare a supported-profile unattended-window manifest with explicit halts and caps.
2. **Phase 6 gate consolidation:** migrate demonstrated shell/Python policy duplication to the existing Python policy core and thin shell transport, starting with commit-prefix and TDD ordering and then the remaining duplicated pure decisions. Retain independently expected verdicts, parity, evidence requirements and documented capability differences; delete the migrated shell decisions. Generated branch vocabulary already shares its domain source and is not a new consolidation achievement. Any duplicated decision left behind must be enumerated; a partial migration does not by itself accept the whole phase. Do not redesign runtime registries or add general policy machinery.
3. **Phase 6 independent test execution:** extend the existing trusted executor/review path with harness-owned execution evidence tied to the exact revision, command/configuration and actual outcome. Source review and execution remain separate. Changed revision, untrusted claims, timeout and infrastructure failure cannot count as passing tests.

Each milestone needs meaningful behavioral contracts, an independent adversarial review, and both final pyrefly checks after the last source/test edit. Every selective coherent commit requires a complete green `make test`. Phase 6's two reviewed milestones share one final full gate and commit; an intermediate commit is unnecessary. Keep the original scale tests. Routine full gates leave `TMPDIR` unset and use writable runtime scratch where available. A documented fixture-only correction does not require another production redesign or review loop.

## Conventions

- Domain logic is pure and immutable; adapters own I/O; app composes the existing runtime.
- Reuse existing SQL runs/admissions/charges/dedupe/outbox and durable commands. Observation records never authorize lifecycle actions, grants or spending. No second queue, authority, incident platform or diagnostic agent leg.
- Root decides shared vocabulary, cohort denominator/unknown semantics, receipt identity and integration points before implementation. One Sol implementation agent works serially; reviewers remain independent.
- Historical baseline missing fields stay unknown. Dashboard-only intervention counts retain their existing meaning; prospective annotations must not double-count the original operator action.
- Cohort exposures use the existing admitted binding identity (family, target, generation and authority/episode identity), excluding mutable aggregate version. Review execution also retains its execution epoch/episode. Family exposure counts are separate; one charge is counted once even when its run participates in multiple bindings. Missing profile or outcome evidence is visible coverage, not an implicit exclusion or a zero.
- Use `uv` and pyrefly; preserve unrelated dirty checkpoint documents and screenshots. No blanket staging, push, target mutations or remote issue changes.
- If a worktree is used, verify its merge base against the real target tip before starting and again before integration.

## Acceptance boundary

The plan's Protected window explicitly keeps deployments paused and requires an explicit release before actual live soak evidence. Continuing implementation does not silently release that pause. Build and verify offline work first, then present a concrete bounded live-window proposal for approval. Passing controlled replays does not accept Phase 5's repeated unattended measurement requirement. Phase 6's declared policy and execution contracts can be verified offline; any measured automation proposal must name its cause, smaller alternative, metric, guardrails and next experiment.

Standard references guide the small implementation: [Google SRE, Eliminating Toil](https://sre.google/workbook/eliminating-toil/) for measuring human effort and starting with observed causes; [in-toto Test Result predicate](https://github.com/in-toto/attestation/blob/main/spec/predicates/test-result.md) for separating tested subject/configuration/outcome from source-review claims. These references do not introduce a package or attestation service dependency.

## Phase 6 integration constraints

Reuse the existing domain gate functions and worktree/governor adapters. Shell transport may gather facts or invoke the harness-owned Python entry point; it must not retain a second copy of a migrated decision. Missing policy code or malformed policy input denies the governed operation. Capability differences remain explicit facts at composition, not agent-selectable environment bypasses. Preserve cheap checks before suite execution and the existing bounded, scrubbed process executor. Update parity cases to exercise real transports with independently stated expected verdicts; agreement alone is insufficient.

Independent execution evidence names the exact tested commit, configured command/scope, executor and configuration/environment policy, invocation and classified outcome. The harness mints it after actual bounded execution in the pinned review checkout. Check revision and tracked-tree integrity before and after execution; changed or unavailable evidence cannot support approval. An operator-authorized test waiver is a separate outcome and never a passing test. Evidence cannot come from a model verdict or a worktree marker.

Enforce that contract at the common approval/submission boundary, including copied rebase approvals and nonbehavioral shortcuts. Reuse existing round, command, receipt and retry/hold handling rather than adding a test queue or paid leg. Source-review verdict and test execution remain separate facts. Specify restart/idempotency and configuration-change behavior before implementing the shared seam; update ADR 0011 for the resulting ordinary-test evidence contract.

## Current status

Phase 5's observation recording, SQL cohort export and dashboard readout are
implemented and independently reviewed. The replay catalog uses the existing
durable composition suite. The reproduced publication-identity refusal loop
now reuses existing durable escalation and bounded reconciliation. Measurement
and eighteen publication recovery contracts pass independent review. The
complete gate passed at 2026-10-04 12:22 UTC: 3,620 domain, 3,376 adapter and
3,345 app cases, 123 hook checks, parity, four architecture contracts and both
type checks. The offline Phase 5 milestone is verified at `eb5941f`. Live
windows and whole Phase 5 acceptance remain pending.

Phase 6 gate consolidation is independently reviewed and integrated. The approved
transport reuses the existing
worktree context and governor compositions, with closed operation/capability
facts and an absolute harness-owned Python entry. Shell decisions and their
fallbacks are deleted; missing policy code denies the operation. Commit checks
become lazy and ordered so an invalid prefix cannot execute project commands.
Independent review reproduced failed Git collectors allowing a PR, legitimate
instruction-file deletion being denied, an inherited unchecked red-test restore
failure, malformed statistics erasing test-policy subjects, and failed base-file
reads masquerading as absence. Root also reproduced an SDK malformed-policy
configuration bypass. All corrections pass independent verification: 186 final
boundary cases, alongside the earlier policy/transport and hook suites. The gate
slice is accepted with the combined Phase 6 full gate recorded below. ADR 0011
records its single policy source and explicit retained transport capabilities;
launcher preflight execution remains outside this slice.

Phase 6's independent design review identified two enforcement points:
`app/review_runtime.py:submit` (including copied approvals) and
`adapters/effects.py:_submission_applicability` before any proven-absent approval
write. Already-present reviews reconcile before considering new configuration.
Reuse existing round authority and command registration to bind a completed
ordinary-test receipt to exact revision, configuration and invocation. A changed
head or configuration cannot reuse passing authorization. A completed failure
remains stable; interruption without a completed receipt remains unavailable.

Run tests after source-review children finish and before publication, with
pre/post revision and tracked-tree checks. Reuse process registration, group
teardown and orphan recovery; cancellation must finish teardown before returning.
Actual assertion failures follow the existing request-changes path; unavailable
execution follows existing bounded evidence schedules/holds. This introduces
neither another paid leg nor another queue.

The tested subject excludes mutable aggregate version and round identity;
it includes exact revision, patch, rejection/execution authority and frozen
configuration. Original round and invocation remain receipt provenance. A
compatible new round reuses completed passing, failed or explicitly waived
evidence; interrupted or unavailable execution cannot imply a pass. Only the
operator-configured test command authorizes ordinary approval evidence.

Ordinary-test retry history and release checks must bind to that full subject.
After the ceiling, the existing execution-renewal action authorizes one fresh
episode for the selected test hold once old work has durably settled. Other
holds and spend authority remain intact. Late old-configuration results cannot
clear a current hold. Synchronous reconciliation can retire an obsolete head's
question or inspect completed evidence; it must never execute tests or treat a
successful trailer read as test evidence.

The ordinary-test receipt implementation is integrated after final independent
acceptance: 163 domain/executor/SQL/writer contracts and 17 composed/adversarial
cases passed on the normal host. The implementation's affected snapshot passed
1,195 cases, its latest executor regressions passed 33 cases, and both final
pyrefly checks and architecture/layout checks passed. Two legacy approval
fixtures now freeze real operator test commands while preserving their original
approval, accounting, claim and recovery assertions; all eight affected cases
and 228 neighboring module cases pass, with independent fixture review.

The combined Phase 6 full `make test` passed at 2026-10-04 14:52 UTC: 3,643
domain, 3,429 adapter and 3,370 app cases (10,442 total), 123 hook checks, parity,
four architecture contracts and both final type checks. Phase 6's declared policy
and independent-execution contracts are accepted locally. The deployment pause
remains in force, and Phase 5 still requires actual live windows.

The receipt slice adds 841 and removes 58 source lines across sixteen files,
including two small domain/executor modules. It reuses existing SQL dedupe,
round authority, schedules, holds, supervision and execution renewal. It adds
no schema, queue, service, paid test leg, operator action or grant class.
Across the complete Phase 6 production diff (`src/issue_agent` and
`plugin/hooks/checks`), 1,127 source lines are added and 1,140 removed: a net
reduction of thirteen lines while adding independent execution enforcement.

Independent review reproduced and verified corrections for hidden index flags,
Git stat-cache suppression, executable/symlink metadata, ambient Git scope,
cancellation during teardown, inherited paid-run authority, bootstrap failure
classification and copied-test deferral. Input proof uses a private fresh Git
index with fixed observation settings; the launcher scrubs inherited authority
and preserves actual target exits separately from infrastructure failure.
The approach uses Git's existing [alternate-index mechanism](https://git-scm.com/docs/git-read-tree).

Residual limits remain the trusted operator command and Git filter semantics,
the existing remote read/write interval, and the supported supervision profile.
The retained target's first qualification timed out after 600 seconds during
native compilation and completed bounded teardown. The final reviewed executor
then passed the exact same retained command in 258,591 ms with the warmed
private cache, equal pre/post tree proof and no orphan processes. This is an
isolated ordinary-command qualification. Phase 5's live migration, canary and
actual unattended measurement remain pending.
