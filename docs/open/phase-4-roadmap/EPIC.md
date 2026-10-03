# Phase 4 roadmap: simplify authority, then complete continuation migrations

Published: [tracking epic #199](https://github.com/xnmp/autonomous-issue-agent/issues/199). All 37 issues accepted locally; all seven phase-4 outcome gates accepted. Phases 5/6 remain pending.
Target repository: [xnmp/autonomous-issue-agent](https://github.com/xnmp/autonomous-issue-agent).
Baseline: `e077ce3`. Runtime cutovers are accepted only as recorded below.

The user requested a substantial reduction of supporting machinery and a
finite roadmap implementable by Sol subagents. Complete **step 0.5, S1–S7**,
before remaining phase-4 migration work.

The [roadmap and deletion inventory](../2026-09-30-phase-4-simplification-roadmap.md)
and [ADR 0016](../../adr/0016-phase-4-current-state-authority.md) define the fixed
design. Replace lifetime archive proof with SQLite current state, atomic exact
receipts and bounded unresolved work. Keep money, ordered outcomes, generation
fences, remote effect readback and unrelated holds. Ambiguous historical targets
remain durably blocked; do not invent missing chronology. Periodic forensic
verification of retired history is explicitly removed from online admission.

The completed roadmap must cover review import/runtime/recovery/operator/dashboard,
consumed issue/PR replies, repair continuity and three allowances, infra episodes,
machine/budget/shared self-clear state, PR remediation, both steward legs and
the final cross-family offline cutover. Each issue owns an observable outcome
or a bounded prerequisite with a named consumer. A1/A2 require actual complete
acceptance; closed tickets alone do not establish phase completion.

This epic adds no new phase. Phases 1–3 stay accepted; step 0.5 is accepted; the seven
phase-4 outcome gates are accepted. Phase 5 measurements and phase 6 policy
consolidation are outside this roadmap.

## Implementation issues

- [x] [S1: Replace lifetime replay with a generation-local ordered reducer](https://github.com/xnmp/autonomous-issue-agent/issues/200) — depends on none
- [x] [S2: Persist current execution state and exact receipts atomically](https://github.com/xnmp/autonomous-issue-agent/issues/201) — depends on [S1](https://github.com/xnmp/autonomous-issue-agent/issues/200)
- [x] [S3: Use the ordered writer for finalization and generation transitions](https://github.com/xnmp/autonomous-issue-agent/issues/202) — depends on [S2](https://github.com/xnmp/autonomous-issue-agent/issues/201)
- [x] [S4: Switch all review admission boundaries to current state](https://github.com/xnmp/autonomous-issue-agent/issues/203) — depends on [S3](https://github.com/xnmp/autonomous-issue-agent/issues/202)
- [x] [S5: Delete the retired archive proof modules and codecs](https://github.com/xnmp/autonomous-issue-agent/issues/204) — depends on [S4](https://github.com/xnmp/autonomous-issue-agent/issues/203)
- [x] [S6: Retire archive trigger maintenance through a forward migration](https://github.com/xnmp/autonomous-issue-agent/issues/205) — depends on [S5](https://github.com/xnmp/autonomous-issue-agent/issues/204)
- [x] [S7: Accept step 0.5 with long-history and failure equivalence evidence](https://github.com/xnmp/autonomous-issue-agent/issues/206) — depends on [S6](https://github.com/xnmp/autonomous-issue-agent/issues/205)
- [x] [R1: Classify review migration per target and keep ambiguous history blocked](https://github.com/xnmp/autonomous-issue-agent/issues/207) — depends on [S7](https://github.com/xnmp/autonomous-issue-agent/issues/206)
- [x] [R2: Publish mixed review migration decisions atomically](https://github.com/xnmp/autonomous-issue-agent/issues/208) — depends on [R1](https://github.com/xnmp/autonomous-issue-agent/issues/207)
- [x] [R3: Expose offline review execution inventory, shadow and rehearsal commands](https://github.com/xnmp/autonomous-issue-agent/issues/209) — depends on [R2](https://github.com/xnmp/autonomous-issue-agent/issues/208)
- [x] [R4: Drive every review mode through durable execution rounds](https://github.com/xnmp/autonomous-issue-agent/issues/210) — depends on [R3](https://github.com/xnmp/autonomous-issue-agent/issues/209)
- [x] [R5: Recover unfinished review rounds through the same authority](https://github.com/xnmp/autonomous-issue-agent/issues/211) — depends on [R4](https://github.com/xnmp/autonomous-issue-agent/issues/210)
- [x] [R6: Complete execution-ceiling and verdict-release operator transactions](https://github.com/xnmp/autonomous-issue-agent/issues/212) — depends on [R5](https://github.com/xnmp/autonomous-issue-agent/issues/211)
- [x] [R7: Show execution state and recovery results in the existing review panel](https://github.com/xnmp/autonomous-issue-agent/issues/213) — depends on [R6](https://github.com/xnmp/autonomous-issue-agent/issues/212)
- [x] [R8: Accept the complete offline review execution slice and retire Store APIs](https://github.com/xnmp/autonomous-issue-agent/issues/214) — depends on [R7](https://github.com/xnmp/autonomous-issue-agent/issues/213)
- [x] [C1: Add fixed continuation component contracts without a new workflow engine](https://github.com/xnmp/autonomous-issue-agent/issues/215) — depends on [R8](https://github.com/xnmp/autonomous-issue-agent/issues/214)
- [x] [C2: Persist fixed components and bind them to existing paid admission](https://github.com/xnmp/autonomous-issue-agent/issues/216) — depends on [C1](https://github.com/xnmp/autonomous-issue-agent/issues/215)
- [x] [C3: Implement bounded fix/steward import decisions and copied rehearsal](https://github.com/xnmp/autonomous-issue-agent/issues/217) — depends on [C2](https://github.com/xnmp/autonomous-issue-agent/issues/216)
- [x] [F1: Consume issue replies atomically with resume admission](https://github.com/xnmp/autonomous-issue-agent/issues/218) — depends on [C3](https://github.com/xnmp/autonomous-issue-agent/issues/217)
- [x] [T1: Move triage baseline and stall episode to a durable steward component](https://github.com/xnmp/autonomous-issue-agent/issues/219) — depends on [C3](https://github.com/xnmp/autonomous-issue-agent/issues/217)
- [x] [F3: Move CI repair continuation to SQL as the first repair lane](https://github.com/xnmp/autonomous-issue-agent/issues/220) — depends on [F1](https://github.com/xnmp/autonomous-issue-agent/issues/218)
- [x] [T2: Move groom baseline, content and stall episode to SQL](https://github.com/xnmp/autonomous-issue-agent/issues/221) — depends on [T1](https://github.com/xnmp/autonomous-issue-agent/issues/219)
- [x] [F4: Move CI repair allowance and reset boundary to the fix component](https://github.com/xnmp/autonomous-issue-agent/issues/222) — depends on [F3](https://github.com/xnmp/autonomous-issue-agent/issues/220)
- [x] [F5: Move address-review continuation and review-timestamp allowance to SQL](https://github.com/xnmp/autonomous-issue-agent/issues/223) — depends on [F4](https://github.com/xnmp/autonomous-issue-agent/issues/222)
- [x] [F6: Move rebase allowance to SQL without changing branch policy](https://github.com/xnmp/autonomous-issue-agent/issues/224) — depends on [F5](https://github.com/xnmp/autonomous-issue-agent/issues/223)
- [x] [F2: Consume PR replies atomically with PR resume admission](https://github.com/xnmp/autonomous-issue-agent/issues/225) — depends on [F6](https://github.com/xnmp/autonomous-issue-agent/issues/224)
- [x] [F7: Unify fix infrastructure-death accounting and productive reset](https://github.com/xnmp/autonomous-issue-agent/issues/226) — depends on [F2](https://github.com/xnmp/autonomous-issue-agent/issues/225)
- [x] [F8: Persist machine block causes in the issue recovery component](https://github.com/xnmp/autonomous-issue-agent/issues/227) — depends on [F7](https://github.com/xnmp/autonomous-issue-agent/issues/226)
- [x] [T3: Migrate steward stalls and infrastructure recovery without lifetime recounts](https://github.com/xnmp/autonomous-issue-agent/issues/228) — depends on [T2](https://github.com/xnmp/autonomous-issue-agent/issues/221), [F7](https://github.com/xnmp/autonomous-issue-agent/issues/226)
- [x] [F9: Persist budget block causes from admission and returned outcomes](https://github.com/xnmp/autonomous-issue-agent/issues/229) — depends on [F8](https://github.com/xnmp/autonomous-issue-agent/issues/227)
- [x] [F10: Complete machine self-clear only after verified projection](https://github.com/xnmp/autonomous-issue-agent/issues/230) — depends on [F9](https://github.com/xnmp/autonomous-issue-agent/issues/229)
- [x] [F11: Complete budget self-clear with all applicable caps preserved](https://github.com/xnmp/autonomous-issue-agent/issues/231) — depends on [F10](https://github.com/xnmp/autonomous-issue-agent/issues/230)
- [x] [F12: Persist free CI rerun and cancel eligibility from existing commands](https://github.com/xnmp/autonomous-issue-agent/issues/232) — depends on [F11](https://github.com/xnmp/autonomous-issue-agent/issues/231)
- [x] [F13: Persist branch refresh and pending-CI escalation eligibility](https://github.com/xnmp/autonomous-issue-agent/issues/233) — depends on [F12](https://github.com/xnmp/autonomous-issue-agent/issues/232)
- [x] [F14: Route fix recovery operator/reset/cleanup through selected SQL components](https://github.com/xnmp/autonomous-issue-agent/issues/234) — depends on [F13](https://github.com/xnmp/autonomous-issue-agent/issues/233)
- [x] [A1: Accept fix and steward offline cutovers with SQL-preserving rollback](https://github.com/xnmp/autonomous-issue-agent/issues/235) — depends on [F14](https://github.com/xnmp/autonomous-issue-agent/issues/234), [T3](https://github.com/xnmp/autonomous-issue-agent/issues/228), [R8](https://github.com/xnmp/autonomous-issue-agent/issues/214)
- [x] [A2: Retire selected Store authority and accept phase 4 as one system](https://github.com/xnmp/autonomous-issue-agent/issues/236) — depends on [A1](https://github.com/xnmp/autonomous-issue-agent/issues/235)

## Acceptance and assignment rules

The coordinator supplies the accepted actual target tip and owns shared seams.
Do not parallelize tasks that edit the same core structs/codecs/schema/loaders.
Sol takes one dependency-ready issue with its full body and the shared contract.
Unknown architecture changes are escalated as a specific conflict, not silently
turned into another implementation subsystem. Record token ceiling before
dispatch and actual usage when available.

Each closure names an accepting commit, behavior evidence, deleted legacy paths,
independent review and full gate. Complete configured copied rehearsals, shadow
comparisons and SQL-preserving rollback before A2 accepts the phase. Services
remain paused; no live activation or target work is authorized.

Independent source/coverage review and an independent adversarial design/roadmap
review found no remaining blockers after corrections for blank replies,
shared self-clear ceiling, external marker retirement, blocked-target discovery,
stale settlement evidence and caught-error rollback. This accepts the design,
not production implementation.

## Publication

The user explicitly approved this epic and its 37 implementation issues.
Published and verified in `xnmp/autonomous-issue-agent` as
[epic #199](https://github.com/xnmp/autonomous-issue-agent/issues/199) and issues #200–#236. Exact titles and
bodies were checked against the approved publication payloads. Dependencies are
linked to actual issue numbers. No labels or assignees were added.
The [manifest](manifest.json) records every key, dependency and GitHub URL.

R1–R2 accepted locally in `35b7330`: fixed sourced migration decisions, mixed atomic publication, retained blockers and bounded inert original-plan retry. Final independent adversarial review clean. Full make test passed: 3335 domain (6.10s), 3212 adapters (1592.23s), 2931 app (358.45s), hooks/parity/architecture/pyrefly; /tmp/phase4-r12-make-test-final.log.

R3 accepted locally in `f155f63`: supported offline execution CLI and copied rehearsal; complete original-plan retry and source preservation. Final independent review clean (48 scoped cases/reproductions plus unchanged retained S7 test verification). Full make test passed: 3335 domain (5.68s), 3212 adapters (1511.73s), 2947 app (341.85s), hooks/parity/architecture/pyrefly. /tmp/phase4-r3-make-test-final.log. App count includes the restored original S7 imported-empty paid-failure/later-abandonment contract.

R4–R5 accepted locally in `98cdfc9`: all review modes use durable Current rounds, immutable child accounting and exact stopped conclusions; existing bounded recovery settles/reconciles without duplicate spend or debit. Final independent review clean. Complete make test passed: 3340 domain (5.84s), 3226 adapters (1579.47s), 2958 app (957.68s), hooks/parity/architecture and both pyrefly checks; /tmp/phase4-r45-make-test-final.log. Includes mandatory history scales and final reviewed admission/fixture state. Production +431/-277 across 15 existing files; no new storage/runtime mechanism.

R6–R7 accepted locally in `961828b`: fixed execution renewal and selected verdict release preserve allowances, exact retry, old-episode quiescence and sibling holds; Current execution and migration blockers feed the existing panel. Dashboard review file-counter reads removed. Final independent review clean: 357 Python contracts and 82 UI cases; corrected test-tier relocation independently 21 checks green. Complete make test passed: 3363 domain (5.70s), 3244 adapters (1600.60s), 2960 app (955.08s), hooks/parity/4 architecture contracts and both pyrefly checks; /tmp/phase4-r67-make-test-final2.log. UI 484 +3 build contracts, fresh full browser 26 (14.9s) green; agent-browser gut check passed.

R8 accepted locally in `7049f8b`: complete composed offline review matrix, supported/blocked shadow distinction and SQL-preserving restart/rollback. Four Store round/session APIs and review-only legacy resets retired; raw stopped import evidence preserved. Complete final reviewed make test passed: 3363 domain (5.58s), 3242 adapters (1510.02s), 2962 app (920.82s), hooks/parity/architecture and both pyrefly checks; /tmp/phase4-r8-make-test-final2.log. Independent complete diff review clean (20 scoped cases), final architecture inventory correction independently clean. Focused 132 composed/shadow/preflight, 110 retained reset/late-settlement/binary-fence and 63 Store/cutover checks passed. Unchanged accepted R7 browser 26 outcomes reused as authorized.

C1–C2 accepted locally in `c8aa2e6`: fixed typed secondary components and existing atomic paid admission/return, exact selected run receipts, explicit shell and forward schema36. Complete final reviewed make test passed: 3,390 domain (5.69s), 3,277 adapters (1528.76s), 2,962 app (947.04s), hooks, parity, four architecture contracts and both pyrefly checks; /tmp/phase4-c12-make-test-final3.log. Final independent joint review clean: 1,130 scoped checks plus final 72-case reproductions; final test narrowing independently clean. Complete cheap type, generated config and 22 layout/architecture checks passed before freezing the final gate.

C3–F1 accepted locally in `f2f570f`: bounded stopped fix/steward publication and exact atomic issue reply consumption, with unpaid ordered blank re-block recovery. Complete final reviewed make test passed: 3409 domain (5.70s), 3277 adapters (1612.09s), 3025 app (1049.00s), hooks/parity, four architecture contracts and both pyrefly checks; /tmp/phase4-c3f1-make-test-final4.log. Final independent production and fixture reviews clean; all 29 corrected affected-file cases and 406 existing accounted-helper callers passed before the frozen gate. Accepted 19/37; next dependency-ready issue T1.

T1 accepted locally in `8eb8e07`: durable triage input/stall and whole-verdict owed delivery, exact receipt reconciliation, immutable purchased owner plus existing successor recovery. Final reviewed full make test passed:3426 domain7.66s,3309 adapters3896.73s (both S7 scales),3035 app1872.57s, hooks/parity, architecture4 and both pyrefly0; /tmp/phase4-t1-make-test-final3.log. Independent final production/fixture review clean. Focused367 plus final210; post-quota affected files368 and corrected recovery10 green. Removed set_triage_at and runtime triage baseline/stall file reads/writes; stopped raw get_triage_at retained for import. Exact bounded existing command/effect receipts own verdict delivery; no new queue/schema/receipt family. Immutable purchase owner and existing retired-owner successor proof preserve adopted recovery. Reviewer found no unnecessary persisted state and confirmed shared candidate/permission/delivery-lane simplification. Accepted20/37; next T2.

T2 accepted locally in `01523a2`: SQL groom input/success signatures, selected stall episode and ordered receipt-backed delivery, with successful zero-effect no-finding and independent triage continuity. Final reviewed full make test passed: 3448 domain (6.03s), 3316 adapters (4102.59s, both S7 scales), 3044 app (1578.06s), hooks/parity, four architecture contracts and both pyrefly checks; /tmp/phase4-t2-make-test-final.log. Final independent review clean (115 focused plus 11 final corrective probes); complete final compatibility/role/stall/import 218 passed and selected contracts 267 passed. Production net -51 across 11 existing files; runtime groom file writers/counters and duplicate reset bookkeeping removed. Accepted 21/37; next dependency-ready issue F3.

F3 accepted locally in `0014d93`: FIX_CI SQL continuation/current remote branch and actual model, exact verified resolve handoff, and fixed durable result recovery on existing run/dedupe receipts. Final independently reviewed full make test passed: 3459 domain (6.37s), 3316 adapters (4228.20s, both mandatory scales), 3068 app (1880.84s), hooks/parity, four architecture contracts and both pyrefly checks; /tmp/phase4-f3-make-test-final.log. Final independent review clean (51 cases plus retained-authority corruption regression); 204 seam checks, 368 shared adapter/public checks and complete affected app files green. Production +498/-87 in 12 existing files; no new module/schema/queue. Accepted 22/37; next F4.

F4 accepted locally in `fb73547`: CI attempt/reset authority uses the issue SQL component with exact admitted/result/reset receipts, paired repair settlement and current linkage fencing; runtime CI counter file authority removed. Independent F4 review clean. Final combined make test passed: 3472 domain (5.72s), 3342 adapters (876.05s), 3093 app (738.10s), hooks/parity/architecture/both pyrefly checks; `/tmp/phase4-performance-final-full-gate.log`. Separate reviewed performance commit `d80cf5d` reduced the gate to 27m20s without weakening original scale counts. Accepted 23/37; next F5.

F2 accepted locally in `6db9f0e`: atomic independently keyed PR reply consumption, selected blank receipt provenance, paired issue reply settlement and compatible-session refusal. Final independent review clean, including selected-source/paired rollback findings and original-reader action equivalence. Complete affected files 702 passed plus seven new scoped refusal cases. Final frozen make test passed: 3,510 domain (6.23s), 3,335 adapters (917.29s), 3,178 app (863.20s), hooks/parity/four architecture contracts and both pyrefly checks. /tmp/phase4-f2-full-gate-final2.log. 2026-10-02 05:34:15–06:04:22 UTC, 1807.45s, exit 0. Source/test and old literal fixture hashes unchanged. Accepted 26/37; next F7, then T3, then F8.

F7 accepted locally in `9316de7`: one SQL issue infrastructure episode uses exact admitted return/reclaim receipts and captured policy, with durable non-infra reset and pre-F7 unknown-history refusal. Active fix file/lifetime arithmetic removed; fresh-ready refusal preserves healthy-peer progress before claim/worktree effects. Final independent review clean and complete make test green (3,513 domain, 3,347 adapters, 3,208 app; all final checks), 09:23:42–09:53:21 UTC, 29m38s. Original scale cases retained. Ordinary stall-source support remains pending F12/A1; no globally bounded historical-write claim. Accepted 27/37; next T3, then F8.

T3 accepted locally in `54f3c78`: independent SQL triage/groom infra episodes, exact return/reclaim refinement and new-human lane reset retain unknown money and paid-stall/delivery evidence. User-approved Jira legacy file/history authority remains explicit, with new SQL deaths decoupled and old Jira charging preserved. Final independent review clean; frozen full make test green (3,513 domain, 3,355 adapters, 3,220 app and all final checks), 10:55:02–11:27:01 UTC, 31m59s. Selected lane file/lifetime paths removed. Accepted 28/37; next F8.

F8 accepted locally in `6b3709d`: SQL exact machine holds and selected retirement, dual issue/PR stale return fences, bounded copied import/shadow and runtime Store machine API removal. Final independent review clean; full make test green3520domain/3355adapters/3223app and all final checks,18:19:10–18:52:41UTC2010.44s, `/tmp/phase4-f8-full-gate-final2.log`. Existing shared count/ceiling crash boundary remains assigned to F10/F11. Accepted29/37; nextF9.

F10 accepted locally in `c18c1bed5f53eca5b77a0a9a55d3d534dc31cc3d`: verified recovery effect atomically retires only the selected machine/budget hold and consumes one shared SQL count; exact current head/check/source and owner adoption preserve pending outcomes. Fixed count/ceiling import and once-only report retained. Independent corrected review clean; actual crash/partial/new-head/check/owner-adoption, alternating shared ceiling/report restart, external discard and caught-error rollback contracts green. Final frozen make test3557domain7.29s/3362adapters1087.04s/3246app1032.67s, hooks/parity/4architecture/both pyrefly0. /tmp/phase4-f10-full-final.log21:23:42.932102–21:59:35.470437UTC2152.54s exit0;20 source/test hashes unchanged. Original4097-generation576.84s/5000-round258.48s retained. Accepted31/37; next F11.

- F11 accepted `ce3549b62a578ad90fc6840f9a365046e35a2df5`: current all-cap budget proof, exact source recovery and one shared SQL ceiling;32/37 accepted. Frozen full gate green1754.96s. F12 next.

F12 accepted locally at `10f7e042`: exact existing CI command/head/run obligation and verified receipt authority; full make test green, 33/37 accepted. Ordinary stall-sig stopped-import gap remains A1.

F13 accepted locally at `f10cbc6c`: exact existing branch-refresh head/base and pending-CI escalation obligations/receipts, full make test green,34/37accepted.

F14 accepted locally in `f3bf33b1182dccabe9bf4b077d74f0d4af53492a`: exact fixed cleanup preflight/final revision guard preserves unsettled evidence and closed-unmerged debt; unsupported legacy counter grants refuse before effects, existing typed operator/productive resets preserved. Dashboard uses actual issue allowance/hold and PR continuity SQL authority; independent ordering/debt-display findings corrected. Full make test green1759.78s, all35hashes verified; dashboard20browser cases green. Accepted35/37; A1 copied activation/advisory source classification then A2 whole-phase acceptance remain. Jira exception preserved; phases5/6 pending.

A1 accepted locally in `b6687e726c330f54311f61766699df4344d098db`: configured copied fix and both steward lanes through actual paid dispatch/crash/restart and compatible archived F14 SQL rollback, original sources untouched. Advisory ci-history/stall-sig gap closed without authority import; unsupported session refusal preserved. Full make test green1708.76s;8hashes verified. Accepted36/37; A2whole-phase acceptance remains. No live deployment activated; Jira exception and phases5/6pending retained.

A2 accepted locally at `0518c278b7be323051df4631d6f203caf751d0fe`; final full gate and independent review passed. Phase4:37/37 issues,7/7 outcome gates. Phases5/6 remain pending.
