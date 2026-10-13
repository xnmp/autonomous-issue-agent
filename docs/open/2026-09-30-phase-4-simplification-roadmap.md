# Phase 4 — simplification and issue roadmap

Date: 2026-09-30. Baseline: `e077ce3`. **Step 0.5 and all seven Phase 4 outcome gates accepted locally.**
Published completion verified 2026-10-05: all 37 tasks #200–236 and epic #199
are closed. Independent audit verified their accepted commits on `dev`, A1/A2
rollback/removal evidence, the approved Jira exception and complete passing CI.
Phase 5's remaining live measurement is tracked in the
[execution record](2026-10-05-phase-5-live-execution.md).
Current checkpoint (2026-10-03): all 37 issues accepted locally through A2;
whole-system caller/removal review, copied rehearsal, dashboard and full gate passed. The
selected runtime families use current SQL authority; Jira's explicitly approved
existing counter path remains an exception. Phases 5/6 remain pending. Earlier
implementation checkpoints below are retained evidence, not remaining work.

This is the current phase-4 execution plan. It supersedes conflicting work
instructions in the September 27–30 checkpoint, compaction and convergence
documents. Preserve their evidence; do not implement their retired machinery.
The [six-phase plan](2026-09-14-architecture-implementation.md) still governs
the overall outcome. Phases 1–4 are accepted; phases 5/6 remain pending.

The user requested a substantial reduction of supporting machinery and a
finite GitHub issue roadmap implementable by Sol subagents. The architecture
decision is [ADR 0016](../archive/adr/0016-phase-4-current-state-authority.md).
Accepted implementation commits and current verification are recorded in the
[execution record](phase-4-roadmap/execution.json). All phase-4
outcomes and A2 acceptance are recorded there.

## Step 0.5 — replace and remove, before expanding phase 4

Complete S1–S7 before any remaining migration work. The gate is a tested
replacement of lifetime-history proof with transactionally maintained current
state and a bounded unresolved suffix, followed by deletion of the old runtime.
Adding a new abstraction or disabling the old gate without a replacement is
not completion.

Keep existing accounting, claims, launch/return evidence, typed round outcomes,
ordered result semantics, generation fences, durable commands/readback,
operator holds, import barriers, SQL byte bounds and fixture isolation.

Remove recurring archive certification from canonical reads, preflight,
reservation, launch and generation advance. Replace recursive generation
snapshots with one generation-local baseline and an immutable transition
receipt. Old open rounds still settle; their cutoff never asserts terminality.

The explicit trade-off: trusted harness-owned SQLite current state remains
authority. Periodic rediscovery of arbitrary out-of-band changes to *retired*
history is no longer an online admission guarantee. Active malformed authority,
conflicting selected receipts, stale owners/generations and unavailable money
still refuse. This is documented scope reduction, not equivalent forensic
coverage. No independent hostile-database security boundary existed here.

One-time stopped migration keeps complete bounded source classification and
source revalidation. It does not run a recurring global namespace census.
Ambiguous historical PRs remain durably blocked, as already directed; a
successful reconstruction of chronology never recorded is no longer required.

## Removal inventory

| Area | Treatment | Completion owner |
| --- | --- | --- |
| Current execution state, ordered outcomes, pending round sources | Keep rules; replace lifetime replay with bounded generation-local fold | S1–S4 |
| `review_execution_archive_*`, `review_execution_compaction*`, `review_execution_checkpoint_commit` | Delete after canonical writer/read/admission replacement | S5 |
| `review_checkpoint_archive*`, `review_checkpoint_chain`, `review_checkpoint_stream_replay`, `review_checkpoint_state_replay`, application/following-source proof modules | Delete historical checkpoint proof mechanism and codecs | S5 |
| `review_archive_identity`, `review_archive_source_versions`, round/receipt/lifecycle archive and initial-checkpoint-absence census modules | Delete recurring proof/certification machinery | S5 |
| Mixed `review_execution_activation.py` and generation helpers in `review_execution.py` | Remove archive-only code; preserve necessary bounded one-time migration readers | S5, R1 |
| Archive-only triggers/projection tables | Retire via forward migration; preserve applied migration definitions/digests and operational source data | S6 |
| Historical reset/session/death reconstruction lacking recorded chronology | Replace with target-scoped blocked decision and source reason | R1–R2 |
| Scratch mutation-stream, reverse-index and prefix-certification prototypes | Excluded from implementation; do not integrate | S7 |
| Child admission/launch/return, accounting, round finalization, submission registration, commands/effects | Keep; no broad deletion by filename prefix | All |

Do not delete financial, effect or immutable round evidence to reach a line
count target. Record removed modules/production lines and every remaining
caller at S7. Retired mechanism tests go with their mechanism; behavior tests
move to the replacement. No unbounded retention/GC project is added.

## Shared implementation contract

The coordinator owns schema, shared values/ports, codecs, canonical loaders
and admission signatures. S1–S4 form one ordered replacement, not concurrent
independent designs. C1–C3 extend those conventions to the remaining families:

- Keep the existing primary resolve/rejection bindings and `target_family()`
  selector. Add a closed set of typed issue recovery, PR repair/recovery and
  steward components under the existing target row and shared revision/fence.
- Each selected component carries an independent activation epoch/episode
  and ordered admission/result watermark. Inactive, blocked and active are
  distinct typed variants. Missing active state is corruption, not zero.
- Issue recovery owns the issue reply mark, existing issue-scoped CI,
  review-repair and rebase allowances, and applicable fix recovery state.
  PR repair/recovery owns PR reply consumption, current PR repair continuity
  and remediation state. Preserve current typed issue/PR breaker ownership;
  captured linkage cannot be reinterpreted after a result arrives.
- Steward has fixed triage and groom episode fields. A stewardship-only
  issue uses an inert typed issue shell with a real target revision and no
  resolve allowance. Later resolve activation must explicitly attach its
  actual authority; shell creation never fabricates a resolve episode.
- Persist components in the existing canonical aggregate plus its existing
  `workflow_components` identity index. Extend the fixed constraints/codecs
  once. No component registry, generic state machine, secondary event store,
  per-family archive verifier or extra queue.
- Reserve validates every selected component binding and atomically records
  reply/allowance consumption with the existing reservation. Refusal consumes
  neither. Existing blank replies use a separate unpaid no-op consumption
  receipt; the issue-side re-block projection remains durable. Returns retain
  existing run identity and exact idempotency receipt.
- Preserve policy: review-repair resets on a newer qualifying **review
  timestamp**, not a push; fix infra failures reset on an actual non-infra
  returned result; PR reply handling retains its compatible-session
  precondition. Consumed replies retain timestamp equality, not a new ID model.
  Machine and budget recovery retain one shared self-clear ceiling. Externally
  cleared markers retire only their matching cause without charging a self-clear.
- Migration reuses stopped barriers, bounded sources, current observations,
  exact original-plan retry and copied rehearsal. Known ambiguous targets
  remain typed blocked components. Incomplete scope/unknown cross-target
  ownership blocks its scope. Do not silently change the source universe.
- Remote projection remains an existing durable command. Queued delivery can
  suppress duplicate paid work without pretending delivery succeeded.

## Coverage and acceptance

The original seven outcome groups remain, with the unsupported historical
fixture and perpetual archive-certificate requirement explicitly replaced.
Step 0.5 is a prerequisite, not a newly completed phase or a claim of 1/7.

| Outcome | Issues | Required final evidence |
| --- | --- | --- |
| 0.5: simplify runtime authority | S1–S7 | Equivalent ordered behavior, bounded hot reads, retained settlement/recovery, removed proof subsystem, compatibility and full gate |
| 1: complete review import evidence | R1–R2 | Every known target imported or specifically blocked; complete scope; changed/malformed sources refuse |
| 2: atomic review authority | S2–S7, R2 | Atomic state/receipt publication, exact retry, current-state corruption refusal, no history-dependent admission |
| 3: complete offline review slice | R3–R8 | Scheduler, all runtime modes, accounting, recovery, operator and dashboard share SQL authority; shadow, rehearsal, rollback, retired Store paths |
| 4: replies and repair continuity | C1–C3, F1–F3, F5–F6, F14, A1 | Both reply scopes, compatible ordered continuation and current PR branch; no duplicated resume or ambiguous inheritance |
| 5: remaining fix recovery | F4–F14, A1 | Three allowances, infra episodes, machine/budget/self-clear, remediation and scoped operator/cleanup preserve unrelated state |
| 6: steward continuation | T1–T3, A1 | Independent triage/groom baseline, queued delivery, stalls and infra recovery across restart |
| 7: whole-system phase-4 acceptance | A1–A2 | Complete caller census, cross-family fault outcomes, old-writer fences, SQL-preserving rollback, independent review and full gate |

Excluded files must remain non-authoritative: `ci-history`, predicted paths
and telemetry-only `stall-sig`. Jira mirror context stays in its existing
integration; phase 4 adds no Jira feature. The existing automerge hint must
be proved redundant to durable effect receipts (or its authority read removed)
at A2. No new automerge policy is added. The existing accepted resolve and
rejection families are preserved.

A1/A2 require actual migration fixtures and configured copied rehearsals for
every selected family. A green unit suite or closed issue checklist alone
does not complete phase 4. Phases 5/6 and live experiments remain separate.

### Accepted step 0.5 evidence

S4/S5 removed 43 complete production modules (9,884 lines); the whole production
diff at their accepting commit removed a net 14,191 lines. Their complete file
inventory and retained behavior evidence are in the execution record. S6's
schema 34 migration retired 76 archive maintenance triggers and three rebuildable
projection/witness tables, preserving all operational source, money, effect,
round, and workflow rows and all historical migration sources/digests.

S7's first single sequential history completed 5,000 real public preflight,
claim-release, abandonment, and finalization transactions. At 10/1,000/5,000
rounds, canonical reads selected 15/15/15 rows and 5,746/5,750/5,750 bytes,
executing 2,731 VM instructions at every checkpoint. Indexed pending-range
searches preserved counters, exact receipts, and zero spending. Read times were
2.748/2.390/2.484 ms; total history time was 202.098 s. Timings are observations,
not machine-specific pass thresholds. The mandatory final gate also passed
the same history on the final candidate.

The first zero-round generation diagnostic reached 1,822 advances before it was
stopped after identifying a historical gate scan. At 10/1,000 advances, selected
rows stayed at 56 and bytes at 80,399/80,441, but VM work grew 7,093→16,993.
`EXPLAIN` showed the current-gate lookup scanning closed gates and sorting them;
the initially suspected latest-intent lookup already used its generation index.
Schema 35 adds only the partial current-gates index with the lookup's existing
scope, predicate, and order. The corrected run completed 4,097 zero-round
advances through real operator transactions. At 10/1,000/4,097 advances,
reads selected 56/56/56 rows and 80,399/80,441/80,443 bytes, with 6,998 VM
instructions at every checkpoint and indexed searches without sorting.
Read times were 9.428/9.401/9.654 ms; the history took 892.351 s. Exact raw
generation receipts grew to 4,097 without another round or financial row.
Independent review is clean. Final `make test` passed 3,323 domain, 3,247 adapter,
and 2,923 app tests, hooks, parity, architecture, and pyrefly. Its adapter tier
took 1,477.08 s including both long histories; no correctness contract was
changed to shorten that gate. The final generation scenario also verifies that
current holds refuse new preflight while old exact outcome retry remains inert.

The supported corruption boundary is active SQLite authority and its selected
immutable/live references: component identities, origin and current generation
receipts, current gates/latest intent, allocated pending sources, exact settlement
and retry facts, and live session/display/productivity watermarks. Malformed,
missing, conflicting, stale-owner/generation, unavailable-money, and unsupported
active prior components refuse new work. Existing held rounds can still settle
and retry exactly. Arbitrary out-of-band edits to unselected retired history are
not periodically rediscovered by online admission.

No runtime caller selects the legacy admission reader. Its only production
caller is the stopped bounded conversion in `workflow_rows.py`, invoked by the
existing stopped application conversion command. Raw source readers and legacy
decode DTOs remain for that compatibility and R1's target classification; no
archive/checkpoint/compaction proof module remains active. Nonempty chronology-
dependent imports remain specifically held until R1 replaces that restriction.
Scratch mutation-stream, reverse-index, and prefix-certification prototypes
remain excluded from this plan and have no implementation caller.

## Issue execution

The issue index below is the finite implementation backlog. Dependencies are
mandatory, and issue bodies contain scope, paths, behavior checks and deletion
obligations. The parent epic is a tracking/acceptance issue, not another coding
assignment. No issue is automatically marked ready for the running harness.

A Sol assignment takes one dependency-ready issue. Include its full body and
the accepted shared contract. Use the actual target tip; worktree agents verify
`git merge-base HEAD <target-branch>` against that tip before starting and the
coordinator rechecks before integration. Shared-file work is serialized; only
disjoint, dependency-ready work can proceed in parallel.

Before dispatch, record the issue's token ceiling chosen by the operator or
coordinator and its intended observable outcome. Record actual usage where the
runtime exposes it; unknown cost is unknown. Do not replace a consumed budget
with an unsupported rolling hour estimate. If the issue requires a new authority,
source census, state engine or changed acceptance contract, stop that issue and
return the concrete design conflict. New helpers must serve an existing issue;
no unsolicited supporting-framework work.

Use focused behavioral checks while editing; independent adversarial review
and full `make test` are required before a coherent commit. Relevant dashboard
work also needs existing browser outcome checks. Close an issue only with its
accepting commit, evidence, removed legacy paths and unresolved risks. Splitting
an issue retains all original acceptance obligations in the parent; a split
does not manufacture completion.

The existing two edited handover/board files are preserved. Do not integrate
scratch prototypes by copying their tree over this checkout. No service
restart, live migration, paid target work or push is authorized by this roadmap.

## Issue index

**Published:** [tracking epic #199](https://github.com/xnmp/autonomous-issue-agent/issues/199) and 37
implementation issues (#200–#236). The user explicitly approved the batch
after the initial automatic-review rejection. All published titles/bodies and
dependency URLs were verified against the approved payloads; no labels or
assignees were added. [S1 / #200](https://github.com/xnmp/autonomous-issue-agent/issues/200) is the first
implementation task. [Local issue copies](phase-4-roadmap/EPIC.md) and the
[manifest](phase-4-roadmap/manifest.json) retain the mapping.

Handover: independent source/coverage review and independent adversarial review
cleared the final design and issue bodies after corrections for blank replies,
shared self-clear ceilings, external-marker retirement, blocked-target discovery,
stale settlement evidence and caught-error rollback. Keys, topological
dependencies, paths and local links were validated; `git diff --check` passes.
No runtime code changed and no new runtime test or phase acceptance is claimed.
Preserve the two pre-existing edited board/handover files.

| Key | Outcome | Work | Depends on |
| --- | --- | --- | --- |
| [S1 · #200](https://github.com/xnmp/autonomous-issue-agent/issues/200) | 0.5 | Replace lifetime replay with a generation-local ordered reducer | — |
| [S2 · #201](https://github.com/xnmp/autonomous-issue-agent/issues/201) | 0.5 | Persist current execution state and exact receipts atomically | [S1 · #200](https://github.com/xnmp/autonomous-issue-agent/issues/200) |
| [S3 · #202](https://github.com/xnmp/autonomous-issue-agent/issues/202) | 0.5 | Use the ordered writer for finalization and generation transitions | [S2 · #201](https://github.com/xnmp/autonomous-issue-agent/issues/201) |
| [S4 · #203](https://github.com/xnmp/autonomous-issue-agent/issues/203) | 0.5 | Switch all review admission boundaries to current state | [S3 · #202](https://github.com/xnmp/autonomous-issue-agent/issues/202) |
| [S5 · #204](https://github.com/xnmp/autonomous-issue-agent/issues/204) | 0.5 | Delete the retired archive proof modules and codecs | [S4 · #203](https://github.com/xnmp/autonomous-issue-agent/issues/203) |
| [S6 · #205](https://github.com/xnmp/autonomous-issue-agent/issues/205) | 0.5 | Retire archive trigger maintenance through a forward migration | [S5 · #204](https://github.com/xnmp/autonomous-issue-agent/issues/204) |
| [S7 · #206](https://github.com/xnmp/autonomous-issue-agent/issues/206) | 0.5 | Accept step 0.5 with long-history and failure equivalence evidence | [S6 · #205](https://github.com/xnmp/autonomous-issue-agent/issues/205) |
| [R1 · #207](https://github.com/xnmp/autonomous-issue-agent/issues/207) | 1 | Classify review migration per target and keep ambiguous history blocked | [S7 · #206](https://github.com/xnmp/autonomous-issue-agent/issues/206) |
| [R2 · #208](https://github.com/xnmp/autonomous-issue-agent/issues/208) | 2 | Publish mixed review migration decisions atomically | [R1 · #207](https://github.com/xnmp/autonomous-issue-agent/issues/207) |
| [R3 · #209](https://github.com/xnmp/autonomous-issue-agent/issues/209) | 3 | Expose offline review execution inventory, shadow and rehearsal commands | [R2 · #208](https://github.com/xnmp/autonomous-issue-agent/issues/208) |
| [R4 · #210](https://github.com/xnmp/autonomous-issue-agent/issues/210) | 3 | Drive every review mode through durable execution rounds | [R3 · #209](https://github.com/xnmp/autonomous-issue-agent/issues/209) |
| [R5 · #211](https://github.com/xnmp/autonomous-issue-agent/issues/211) | 3 | Recover unfinished review rounds through the same authority | [R4 · #210](https://github.com/xnmp/autonomous-issue-agent/issues/210) |
| [R6 · #212](https://github.com/xnmp/autonomous-issue-agent/issues/212) | 3 | Complete execution-ceiling and verdict-release operator transactions | [R5 · #211](https://github.com/xnmp/autonomous-issue-agent/issues/211) |
| [R7 · #213](https://github.com/xnmp/autonomous-issue-agent/issues/213) | 3 | Show execution state and recovery results in the existing review panel | [R6 · #212](https://github.com/xnmp/autonomous-issue-agent/issues/212) |
| [R8 · #214](https://github.com/xnmp/autonomous-issue-agent/issues/214) | 3 | Accept the complete offline review execution slice and retire Store APIs | [R7 · #213](https://github.com/xnmp/autonomous-issue-agent/issues/213) |
| [C1 · #215](https://github.com/xnmp/autonomous-issue-agent/issues/215) | 4 | Add fixed continuation component contracts without a new workflow engine | [R8 · #214](https://github.com/xnmp/autonomous-issue-agent/issues/214) |
| [C2 · #216](https://github.com/xnmp/autonomous-issue-agent/issues/216) | 4 | Persist fixed components and bind them to existing paid admission | [C1 · #215](https://github.com/xnmp/autonomous-issue-agent/issues/215) |
| [C3 · #217](https://github.com/xnmp/autonomous-issue-agent/issues/217) | 4 | Implement bounded fix/steward import decisions and copied rehearsal | [C2 · #216](https://github.com/xnmp/autonomous-issue-agent/issues/216) |
| [F1 · #218](https://github.com/xnmp/autonomous-issue-agent/issues/218) | 4 | Consume issue replies atomically with resume admission | [C3 · #217](https://github.com/xnmp/autonomous-issue-agent/issues/217) |
| [T1 · #219](https://github.com/xnmp/autonomous-issue-agent/issues/219) | 6 | Move triage baseline and stall episode to a durable steward component | [C3 · #217](https://github.com/xnmp/autonomous-issue-agent/issues/217) |
| [F3 · #220](https://github.com/xnmp/autonomous-issue-agent/issues/220) | 4 | Move CI repair continuation to SQL as the first repair lane | [F1 · #218](https://github.com/xnmp/autonomous-issue-agent/issues/218) |
| [T2 · #221](https://github.com/xnmp/autonomous-issue-agent/issues/221) | 6 | Move groom baseline, content and stall episode to SQL | [T1 · #219](https://github.com/xnmp/autonomous-issue-agent/issues/219) |
| [F4 · #222](https://github.com/xnmp/autonomous-issue-agent/issues/222) | 5 | Move CI repair allowance and reset boundary to the fix component | [F3 · #220](https://github.com/xnmp/autonomous-issue-agent/issues/220) |
| [F5 · #223](https://github.com/xnmp/autonomous-issue-agent/issues/223) | 5 | Move address-review continuation and review-timestamp allowance to SQL | [F4 · #222](https://github.com/xnmp/autonomous-issue-agent/issues/222) |
| [F6 · #224](https://github.com/xnmp/autonomous-issue-agent/issues/224) | 5 | Move rebase allowance to SQL without changing branch policy | [F5 · #223](https://github.com/xnmp/autonomous-issue-agent/issues/223) |
| [F2 · #225](https://github.com/xnmp/autonomous-issue-agent/issues/225) | 4 | Consume PR replies atomically with PR resume admission | [F6 · #224](https://github.com/xnmp/autonomous-issue-agent/issues/224) |
| [F7 · #226](https://github.com/xnmp/autonomous-issue-agent/issues/226) | 5 | Unify fix infrastructure-death accounting and productive reset | [F2 · #225](https://github.com/xnmp/autonomous-issue-agent/issues/225) |
| [F8 · #227](https://github.com/xnmp/autonomous-issue-agent/issues/227) | 5 | Persist machine block causes in the issue recovery component | [F7 · #226](https://github.com/xnmp/autonomous-issue-agent/issues/226) |
| [T3 · #228](https://github.com/xnmp/autonomous-issue-agent/issues/228) | 6 | Migrate steward stalls and infrastructure recovery without lifetime recounts | [T2 · #221](https://github.com/xnmp/autonomous-issue-agent/issues/221), [F7 · #226](https://github.com/xnmp/autonomous-issue-agent/issues/226) |
| [F9 · #229](https://github.com/xnmp/autonomous-issue-agent/issues/229) | 5 | Persist budget block causes from admission and returned outcomes | [F8 · #227](https://github.com/xnmp/autonomous-issue-agent/issues/227) |
| [F10 · #230](https://github.com/xnmp/autonomous-issue-agent/issues/230) | 5 | Complete machine self-clear only after verified projection | [F9 · #229](https://github.com/xnmp/autonomous-issue-agent/issues/229) |
| [F11 · #231](https://github.com/xnmp/autonomous-issue-agent/issues/231) | 5 | Complete budget self-clear with all applicable caps preserved | [F10 · #230](https://github.com/xnmp/autonomous-issue-agent/issues/230) |
| [F12 · #232](https://github.com/xnmp/autonomous-issue-agent/issues/232) | 5 | Persist free CI rerun and cancel eligibility from existing commands | [F11 · #231](https://github.com/xnmp/autonomous-issue-agent/issues/231) |
| [F13 · #233](https://github.com/xnmp/autonomous-issue-agent/issues/233) | 5 | Persist branch refresh and pending-CI escalation eligibility | [F12 · #232](https://github.com/xnmp/autonomous-issue-agent/issues/232) |
| [F14 · #234](https://github.com/xnmp/autonomous-issue-agent/issues/234) | 5 | Route fix recovery operator/reset/cleanup through selected SQL components | [F13 · #233](https://github.com/xnmp/autonomous-issue-agent/issues/233) |
| [A1 · #235](https://github.com/xnmp/autonomous-issue-agent/issues/235) | 7 | Accept fix and steward offline cutovers with SQL-preserving rollback | [F14 · #234](https://github.com/xnmp/autonomous-issue-agent/issues/234), [T3 · #228](https://github.com/xnmp/autonomous-issue-agent/issues/228), [R8 · #214](https://github.com/xnmp/autonomous-issue-agent/issues/214) |
| [A2 · #236](https://github.com/xnmp/autonomous-issue-agent/issues/236) | 7 | Retire selected Store authority and accept phase 4 as one system | [A1 · #235](https://github.com/xnmp/autonomous-issue-agent/issues/235) |

A2 accepted locally at `0518c278b7be323051df4631d6f203caf751d0fe`; final full gate and independent review passed. Phase4:37/37 issues,7/7 outcome gates. Phases5/6 remain pending.
