# Manager-session issues report — 11–12 October 2026 (AEDT)

## 1. Executive summary

The session tried to move Tauri work from issue through implementation, independent review, CI and merge, while closing harness gaps encountered along the way.
Self-hosting on `xnmp/autonomous-issue-agent` ran early, then was stopped at the user's request; Ballast was also stopped later.
Useful target changes shipped, and many harness fixes shipped, but neither the blocked set nor the manager's workload converged.
Work repeatedly stopped for accounting uncertainty, missing release paths, label ownership, CI-policy errors, infrastructure failures and test flakes.
Several stops had no human-judgment kernel: the operator supplied knowledge or mechanical recovery that the harness should have handled.
Independent operator tests converted shared-build failures and load-sensitive tests into counted review rejections, creating paid repairs and manual verdict releases.
Machine self-clear removed an unanswered block on Tauri PR #1068 twice, buying a clearly redundant $13.712487 repair before budget stopped the loop.
Tauri #1102 lost two one-hour resume legs: $2.833707 on Codex, then $17.040507 estimated on Opus, after the harness discarded continuation provenance and consumed the agent's own comment as a reply.
The manager's concurrent full suites exhausted temporary storage and contributed to host load; later serialization and quiet-host rules were responses to damage already done.
Fresh reviews repeatedly found defects in proposed fixes, including two blocking regressions in the subtractive #1068 fix and another unsupported shell path in #298 increment 3.
The checked-out harness has 116,534 Python source lines and 148,404 Python test lines; its stop-state audit enumerates 34 entries, including duplicates and dead checks.
The user first reduced velocity, then froze intake, then ordered every harness daemon off and required block-by-block root-cause work with no new bookkeeping.
The final direction is simplification: derive workflow facts from GitHub, retain a small ledger for money, leases and deduplication, and reserve human stops for actual judgment.
The user and manager explicitly agreed that the harness is over-engineered, with band-aids layered on band-aids; the incident chains below support that assessment.
The harness remains off by user order. This report performs no recovery, deployment, merge, label change or database mutation.

## Evidence, scope and reading conventions

The primary source is the uncommitted running handover. It is cumulative, with earlier statements retained after later changes; its section headings are not a reliable event clock. This report covers the AEDT interval 2026-10-11 00:00 through the final report request on 2026-10-12, with October 10 and October 6–7 evidence included only where it explains a stop encountered during this session. AEDT is UTC+11. Raw forensic times marked `Z` are UTC.

GitHub status was read on 2026-10-12 beginning at approximately 10:24Z (21:24 AEDT), with final follow-up GETs during report verification. Repository measurements use the primary checkout at `8251a37`. No full test suite was run for this documentation task. Source reads excluded lines containing the user-prohibited secret markers. The transcript initially measured 43,151,063 bytes and 19,971 lines; it was searched and filtered, never dumped whole. The live manager transcript can continue growing.

Source abbreviations used as file-and-line citations:

| Ref | Source |
| --- | --- |
| **H** | [Running manager handover](2026-10-12-handover-manager-session.md), line numbers in the primary checkout. |
| **F** | [Field-verification ledger](../field-verification.md), especially working-tree lines 22–53 and `git diff HEAD -- docs/field-verification.md`. Rows can say **pending** even when a fix is merged. |
| **S** | [Stop-state consolidation audit](2026-10-11-stop-state-consolidation.md), written against `7c04d49`; its source pointers are historical. |
| **M** | [Manager reference](../reference/manager.md), especially lines 22–67 and 130–179. |
| **ADR11** | [Trusted execution and gate evidence](../archive/adr/0011-trusted-execution-and-gate-evidence.md), lines 38–64. |
| **T:L…** | Physical JSONL line in `/home/chong/.claude/projects/-home-chong-Repos-autonomous-issue-agent/712a8fde-092a-46b8-847b-6c8712ab57ea.jsonl`. This distinguishes actual user messages from tool results, cron prompts, compaction summaries and delegated reports. |
| **P/name:line** | File in `/tmp/claude-1000/-home-chong-Repos-autonomous-issue-agent/712a8fde-092a-46b8-847b-6c8712ab57ea/scratchpad/`. The `codex-*.md` files are task briefs; reproduced findings quoted inside them are review evidence, while requests to inspect a risk are not proof that it occurred. |
| **U** | [/tmp/pr-unblock-livelock-report.md](/tmp/pr-unblock-livelock-report.md). |
| **V** | [/tmp/test-failure-verdict-hold-report.md](/tmp/test-failure-verdict-hold-report.md). |
| **X** | [/tmp/issue1102-timeout-forensics.md](/tmp/issue1102-timeout-forensics.md). |
| **R** | [/tmp/issue298-inc3-review-fix-report.md](/tmp/issue298-inc3-review-fix-report.md). Other increment reports are named explicitly below. |
| **I** | [/tmp/block-inventory-2026-10-12.md](/tmp/block-inventory-2026-10-12.md), completed during this report's final verification. Read-only at `8251a37`; joins native logs, events, SQLite `mode=ro` and GitHub GETs. Its classification letters are its own, not this report's A–E groups. |

Issue numbers without a repository qualifier in the harness catalogue refer to `xnmp/autonomous-issue-agent`. Tauri issue/PR numbers are explicitly prefixed **Tauri**. **VERIFIED** means a source gives a reproduced behavior, a code trace plus recorded event, or an authoritative GitHub observation. It does not mean a merged correction has passed its specified live canary. **HYPOTHESIS** marks unresolved causal attribution. “Human kernel” describes the irreducible judgment, separately from surrounding mechanical gaps.

### Material disagreements between sources

1. **#1068 identity versus self-clear:** H:67 initially questioned the shared login. U:7–31 establishes that the unsolicited releases were machine self-clear, with no qualifying reply. The separate self-reply defect is #342 on Tauri #1102.
2. **#999/#1001 stop:** H:40 initially called synthetic verdict holds their blocker. V:14–21,85–97 and H:111–115 show that scheduling actually stopped on a later NHR label. GitHub identifies actor `xnmp`, not the process; the manager later attributes those labels to its October 7 operator session.
3. **#1102 handover/session:** H:93 says “Session: not captured.” X:70–74 shows A's session was captured, then B's handover overwrote it; B's SDK timeout path lost its session ID despite an existing native transcript.
4. **#1102 spend:** H:79's “$20.40 total” is not the sum of its two quoted resume amounts. X gives $19.874214 for those resumes. $20.40 may include earlier work, but that attribution is not established; it is not used as the resume subtotal.
5. **Latest review wins:** U:46 reports a favorable implementation-stage review; `P/unblock-review-output.txt:3–31` subsequently says **DO-NOT-SHIP**, with two reproduced regressions. V's favorable review is also superseded by `P/codex-vhold-fix.md:3–19`, a discoverability regression.
6. **#298 size:** H:70,130 retains “about +480.” R:159–161 and direct Git numstat give **+513** at `f832acb`; +480 describes the earlier allowance slice.
7. **Counts:** T:L19948 says 20 labels and 17 ADRs. The current enum has **19 labels**; `docs/adr` has **16 numbered ADRs plus README**, not 17 numbered decisions.
8. **Independent-test addition:** T:L19929 attributes **+841 src** to `0294b3e`. Direct Git measurement for that whole commit's `src/` diff is **1,099 insertions, 84 deletions, net +1,015**. The smaller figure may describe a subset; that subset is not specified.
9. **Event clocks:** H's “21:45 — harness OFF” heading conflicts with the actual shutdown instruction at T:L19785, **21:02:37 AEDT**. GitHub records Tauri #1074 merged **20:52:21** and #1090 **20:57:33**, earlier than H's approximate “21:2x” for #1074. `P/codex-1074.md:5` also uses `+10:00`; use its raw UTC evidence rather than that local conversion.
10. **Inventory refinements:** I:7–17 establishes additional causes not available in the running handover: review release wrote #1068/#1084 NHR from fix holds; #999's historical `NoLinkedPr` blocks handoff; #1001's native accounting interruption began with ENOSPC; #994 is excluded by contradictory lifecycle labels. I calls `1abdccf` “already fixed,” meaning a local candidate; its later DO-NOT-SHIP review remains controlling. I's “eight open labelled holds” excludes #994, which is a silent eligibility stop rather than a labelled hold.

## 2. Issue catalogue

### A. Harness gaps that blocked work

#### A01 — Shell-linked issue holds rejected as the wrong root variant (#253 → PR #256)

- **Symptom/evidence:** self review repeatedly logged `departed pull request control unavailable … wrong root variant`, once per 900-second reporting interval. F:22; T:L2223,3576.
- **Root cause — VERIFIED:** the linked-issue hold reader required primary resolve authority instead of accepting an issue shell aggregate. Migration/activation introduced a supported state the consumer could not read.
- **Human kernel:** none. **Lineage:** continuation activation and retained-root variants → incompatible hold reader → repeated departed-PR failures.
- **Disposition/cost:** `f8e213a`, PR #256 merged. F verifies absence of the error for over two hours; it explicitly does **not** separately prove a departed decision for a shell-linked PR. Lost wall time; no isolated dollar total.

#### A02 — Claude returned blocked while its own background child was pending (#255 → PR #263)

- **Symptom/evidence:** a delegated `local_agent`/`local_workflow` remained pending when the parent returned blocked. Tauri #1058's ADDRESS_REVIEW on PR #1070 produced event `infra.path.entered`, `background_child_pending`, at 06:53Z. F:53.
- **Root cause — VERIFIED:** the generic blocked outcome was treated as a human question without accounting for pending delegated work.
- **Human kernel:** none. **Lineage:** background delegation → terminal structured status → generic human-block projection.
- **Disposition/cost:** PR #263 (`6e6ea44`) reclassifies this as infrastructure recovery. Live verified: no human comment/blocked label, then APPROVE at 06:58Z. It still consumes infra allowance, rather than eliminating the premature parent return; no attributable spend total.

#### A03 — Missing normal repair return stranded terminal launched work (#240 → PR #248)

- **Symptom/evidence:** cancelled Tauri #1019 REBASE had no normal return; its owed repair stayed held. F:54 and H:39; prior pending-rebase starvation appears in F's #239 row.
- **Root cause — VERIFIED:** accounting disposition and workflow acknowledgment were separate, and terminal quiescent launched work lacked the acknowledgment path.
- **Human kernel:** none for acknowledgment. **Lineage:** durable continuation purchases → return receipt required → process loss → stranded purchase.
- **Disposition/cost:** PR #248 (`b4fb4a1`) adds `PRRepairUnreturned` acknowledgment, without inventing output or price. Native CI run `38087637351` passed; the ledger's live recovery row remains pending. Subsequent A04/A05 show the repair authority still had other dead ends.

#### A04 — `return_authority_unavailable` re-dispatched every tick (#262 → PR #268)

- **Symptom/evidence:** Tauri #1019 repeatedly refused ADDRESS_REVIEW without a provider launch; its infra count initially stayed at two. #262; T:L5665; F:23.
- **Root cause — VERIFIED:** durable pre-admission refusals did not enter the bounded per-issue recovery count.
- **Human kernel:** none for preventing repeat refusals. **Lineage:** repair purchase/return authority → pre-admission refusal → scheduler sees the same repair owed again.
- **Disposition/cost:** PR #268 (`b5c1e08`) introduced a per-issue, episode-scoped `level_state` refusal ledger and counted refusals toward INFRA. This stopped an unbounded retry class but created A06 and the migration complications in B. Refusals themselves cost $0; repeated orchestration and retained reservations still blocked useful work. F:23 remains pending.

#### A05 — Unknown accounting retained money and froze subsequent repair (PR #269; #301 → PR #317)

- **Symptom/evidence:** run `9fdbdc47` retained a $50 reservation and held Tauri #1019 after SIGTERM/accounting failure. F:24,37; H:12,39; #270.
- **Root cause — VERIFIED:** fail-closed pricing retained the full reservation, while purchase recovery required released/settled money. A terminal accounting fault therefore became workflow paralysis.
- **Human kernel:** real policy judgment about uncertain charges; none for leaving a completed run permanently wedged.
- **Lineage:** strict usage completeness → `accounting_unavailable` → retained reservation → repair acknowledgment withheld → A04.
- **Disposition/cost:** PR #269 (`f9bd28f`) let repair proceed while retaining money, creating daily-headroom pressure rather than solving pricing. PR #317 (`c5e2a9e`, schema 41) later settled terminal runs from best available per-request evidence. Tauri boot at 08:24 AEDT settled `9fdbdc47` at $0, journaled the gap, and raised charge count 1,392→1,393. The $0 is an **estimate**, not evidence no work was billed.

#### A06 — Infrastructure ceiling had no sanctioned exit (#271; #298 increment 3)

- **Symptom/evidence:** an NHR escalation had no gate; legacy unblock refused active resolve control, and typed re-arm required a gate that did not exist. S:14; H:21,39; #271.
- **Root cause — VERIFIED:** the fixer's counter ceiling and typed operator-control model did not share a release representation.
- **Human kernel:** granting more effort after actual repeated failure can be judgment; constructing a release for a mechanical admission failure is a gap.
- **Lineage:** A04's refusal ledger/INFRA ceiling → NHR label → retired legacy unblock → no typed exit.
- **Disposition/cost:** initial #271 candidate `5d258e6`, **+325 src**, held after failing the surface test. Replaced by #298 consolidation work, ultimately stopped. Tauri #994 was requeued by closing PR #1019, throwing away continuity instead of resolving the hold. No complete cost allocation.

#### A07 — Rebase and address-review counters had unusable reset/release rules

- **Symptom/evidence:** #1019 hit rebase **3/3** after infrastructure deaths; `reset_rebase_allowance` had no production caller. Review repair stopped at three attempts against the same review. S:16–17; H:39.
- **Root cause — VERIFIED:** durable counters had ceilings but no active-control release; rebase reset was dead code and review reset depended on a new review.
- **Human kernel:** additional spend or a genuine disputed review; none for an infra-caused allowance stop. **Lineage:** separate retry counters → independent breaker arms → label stop without typed reset.
- **Disposition/cost:** user chose close #1019/requeue Tauri #994, before later insisting PRs must be preserved. #298 proposed generic renewal and merge-only rebase reset; not deployed. Lost branch/repair effort is unquantified.

#### A08 — Machine self-clear ceiling was “report only, no exit”

- **Symptom/evidence:** Tauri #1084/#993 had `holds:["machine"]`, `self_clears:3`, `gates:[]`; legacy unblock refused. H:43; S:26; `/tmp/issue298-inc3-current-stop-report.md:117–123`.
- **Root cause — VERIFIED:** `SELF_CLEAR_CEILING=3` persisted a report marker but supplied no renewal operation.
- **Human kernel:** none inherent in exhausting a mechanical recovery loop; underlying budget/clarification might contain judgment.
- **Lineage:** automatic machine/budget release → loop protection → ceiling/report flag → dead end → +480 allowance slice → +513 revised slice → B09.
- **Disposition/cost:** #298 increment 3 attempted typed release; stopped before deployment. #1084 still carries NHR/blocked labels. Cost is repeated review/recovery plus manager intervention, not separately priced.

#### A09 — Test-only rejections offered synthetic verdict holds, misleading block diagnosis

- **Symptom/evidence:** review-control presented `ReviewVerdictHold` with only `release_verdict` for Tauri #999/#1001, despite `workflow_held=false`, `gates=[]`, execution failures zero and counted rejections below four. V:45–97.
- **Root cause — VERIFIED:** `_fixed_verdict_receipt` authenticates test-only rejection for legitimate release, but the selector indiscriminately put every such receipt into `confirmed_verdicts`; the pure view synthesized a hold. These choices were **not daemon admission inputs**.
- **Human kernel:** classifying an observed failure as flake may be judgment; synthetic hold creation is mechanical. **Lineage:** independent ordinary-test receipts → PR #281 release eligibility → selector conflates releasable rejection with model ESCALATE.
- **Disposition/cost:** local `454ff49`, net src 0, removes that projection edge. Not deployed; B12 records its discovery regression. It does not clear manual NHR or fix the older loops.

#### A10 — Older manually parked PRs still required operator intervention

- **Symptom/evidence:** Tauri #999's REBASE repeated `repair handoff has unsupported source, branch or classification`; #1001 ADDRESS_REVIEW returned `accounting_unavailable`, then `return_authority_unavailable`. Their NHR additions were 2026-10-07 00:52:00Z and 04:37:32Z. H:112–115; V:85–86.
- **Root cause — VERIFIED for historical loops/labels; HYPOTHESIS for recurrence on current dev:** those failures motivated operator parking; no matching durable harness NHR command was found. Login attribution alone cannot distinguish operator from agent.
- **Human kernel:** deliberate parking was an operator decision, but the failures requiring it were gaps. **Lineage:** handoff/accounting authority failures → manual NHR → multiple possible label owners → misleading synthetic hold diagnosis.
- **Disposition/cost:** removal was classifier-blocked; no release was performed before shutdown. Both PRs remain open, untouched since October 7. Their historical costs are not isolated here.

#### A11 — Escalation stripped the review trigger; removing NHR did not restore eligibility

- **Symptom/evidence:** Tauri #1074 was clean, approved and green; after authorized NHR removal it lacked `ai-pr-open`, so review/merge selection ignored it. H:103–108. Similar label loss appears for #1065 in `P/codex-1065.md:5–8`.
- **Root cause — VERIFIED:** escalation changed `ai-pr-open → needs-human-review`, while later recovery removed only NHR. No owner restored the earlier trigger.
- **Human kernel:** granting another allowance may be judgment; restoring eligibility afterward is not. **Lineage:** labels used both as projection and selection authority → several escalation writers → partial release.
- **Disposition/cost:** manager manually restored `ai-pr-open`; GitHub confirms #1074 merged at 09:52:21Z. #298 release restoration was proposed, then stopped. Delay and repeat supervision; dollars not isolated.

#### A12 — Free CI rerun required a paid dispatch slot (PR #289; #291)

- **Symptom/evidence:** Tauri #1074, approved and required-green with one advisory macOS failure, had no rerun action while RESOLVE #1081 occupied the sole slot and reload drained admission. Constructed identical inputs: slots 0→`[]`, slots 1→`RerunFailedChecks`. `P/codex-free-rerun.md:21–55`; F:34.
- **Root cause — VERIFIED:** capacity gated remedy selection, even for a non-agent action. The watchdog's `merge_state` signature reset on UNSTABLE→UNKNOWN→UNSTABLE, explaining absent stall reports.
- **Human kernel:** none for executing the existing rerun policy. **Lineage:** one `decide_fix` path mixes remedies and paid admission → free action starves behind unrelated work.
- **Disposition/cost:** PR #289 (`833ccfe`) decoupled free reruns from paid capacity. F:34's live saturation canary remains pending. Hosted CI still consumes runner time; “free” means no paid model leg.

#### A13 — Queued CI was treated as executing/stalled (PR #283; #282)

- **Symptom/evidence:** hosted-runner queues were cancelled/escalated as hangs, including Tauri #1065/#1070. F:28; `P/codex-1065.md:5`.
- **Root cause — VERIFIED:** deadline calculation included queue time instead of actual execution start. **Human kernel:** none.
- **Lineage:** CI timeout → cancel/rerun latch → ambiguous captured-run recovery → queue saturation wrongly reaches NHR.
- **Disposition/cost:** PR #283 (`39b01aa`) exempts QUEUED-only runs. Residual captured-run/attempt/waiting-state bugs were filed as #282 (A14). Live hosted saturation proof remained pending; cancelled queue position and additional CI runs cost time.

#### A14 — CI cancellation reconciled the head, not the captured run attempt (#282)

- **Symptom/evidence:** a newer same-head run could cause “cancelled run never completed” or hide the original executing run; replay after rerun could cancel attempt 2. WAITING/REQUESTED/PENDING with no start inherited creation time. #282; `P/codex-review-ci2.md:8–14`.
- **Root cause — VERIFIED by review probes:** cancellation capture lacked sufficient `(run_id, run_attempt)` semantics in reconciliation and observation.
- **Human kernel:** none. **Lineage:** per-head cancel/rerun latch + durable replay → superseded observations/attempt reuse.
- **Disposition/cost:** filed, unfixed; queued correction deliberately did not absorb the whole class. Possible wasted CI/repair spend, no live dollar quantification.

#### A15 — One free rerun per head forced paid questions for different flaky jobs (#279)

- **Symptom/evidence:** same Tauri #1068 head first failed macOS launch-smoke, then Ubuntu smoke; both had base/other-branch evidence. PR_RESUME asks charged $3.01 then $3.80. #279.
- **Root cause — VERIFIED:** `PRS.ci_rerun` was keyed per head, so the second job had no unused rerun even though it was a different failure.
- **Human kernel:** deciding a failure is pre-existing/flaky; the actual rerun is mechanical. **Lineage:** rerun loop bounding → overly coarse per-head latch → paid question/owner round trip.
- **Disposition/cost:** filed, not fixed. Proposed reuse of the existing latch per job/head. Reported $6.81 is recorded cumulative-charge evidence and may be overstated by #287; do not add it as clean incremental spend.

#### A16 — Held PR could not retrigger CI under the current workflow (#254)

- **Symptom/evidence:** harness PR #248's earlier hosted CI failed on infrastructure/billing; the native workflow needed a fresh run at the approved head. Close/reopen retrigger and some restart/merge actions were classifier-blocked. T:L1539,1587–1599,2120; #254.
- **Root cause — VERIFIED:** CI bucket logic recognized failure, not billing/startup failure; held lifecycle had no free retrigger using the changed workflow.
- **Human kernel:** runner installation/admin merge policy is judgment; rerunning a valid approved head after infrastructure failure is mechanical.
- **Lineage:** hosted billing failure → lifecycle hold → no current-workflow trigger → close/reopen workaround. **Disposition/cost:** native CI ultimately passed and PR #248 merged; #254 remains open. Delay is established, model cost not isolated.

#### A17 — Advisory CI blocked merges; required-check policy was not authoritative (#303 → PR #319)

- **Symptom/evidence:** approved Tauri #1074 stalled with all six required contexts green and advisory `rust (macos-latest)` red/UNSTABLE. `P/codex-1074.md:3–17`; H:20.
- **Root cause — VERIFIED:** harness merge/remedy decisions inferred aggregate CI and used ignore rules rather than GitHub's declared required checks.
- **Human kernel:** repo governance over required contexts; none after that policy exists. **Lineage:** all-check gating → ignore regex/advisory inference/UNSTABLE refresh → duplicate merge-policy authority.
- **Disposition/cost:** PR #319 (`42bddd8`, net src −28) reads required policy; empty policy gates all checks. Keifu protection was set by user decision. Four review rounds corrected B25 onward. F:39 live canary pending; A18 was a shipped regression.

#### A18 — Conflict rebase waited for CI evidence the conflicting PR could not produce (PR #338)

- **Symptom/evidence:** Tauri #1068 stalled **499 minutes** with conflicts and no head checks; #1090 was also affected. F:50; H:54.
- **Root cause — VERIFIED:** PR #319's blanket CI requalification ran before REBASE, while GitHub could not run pull-request checks for the conflicting head.
- **Human kernel:** none. **Lineage:** required-check policy correction → gate applied to every paid repair → conflict prevents evidence → repair cannot remove conflict.
- **Disposition/cost:** PR #338 (`b069f13`) selects/revalidates conflict rebase using fresh conflict and lifecycle facts. Verified dispatch #1068 at 19:20:52, #1090 at 19:29. #1068 later became conflicting again; that does not negate the verified repair dispatch.

#### A19 — Machine self-clear answered an independent-test question with green GitHub CI

- **Symptom/evidence:** Tauri #1068 ADDRESS_REVIEW blocked at 20:01:10 ($13.441554); unsolicited release at 20:07:52; second repair at 20:09:54 ($13.712487); second release 20:11:32; third admission budget-rejected at 20:13:40. First actual operator answer: 20:14:09. U:11–23, sequences **17463,17482–17485,17494,17507–17510,17519**.
- **Root cause — VERIFIED:** paid agent `blocker="checks"` minted `RecordedMachineBlock`; `machine_clear_evidence` treated same-head green hosted CI as enough to clear the agent's question about failed independent tests. Provenance proved who emitted the claim, not that its semantics matched the release predicate.
- **Human kernel:** classifying the test failure and granting spend; none for repeated unanswered releases. **Lineage:** agent blocker taxonomy → machine-release authority → hosted-check predicate substitutes for independent-test judgment.
- **Disposition/cost:** local `1abdccf`, net src −66, removes taxonomy/minting/predicates. **DO-NOT-SHIP** after B10/B11. The window's two priced legs total **$27.154041**; **$13.712487** is clearly redundant. The third was $0, not another charged leg.

#### A20 — Budget planner/executor disagreement masqueraded as a stale head

- **Symptom/evidence:** after cap increase, Tauri #1068 repeatedly reported expected revision `1791ab86…` not matching remote, although its PR head was exactly `1791ab86…`. H:106.
- **Root cause — VERIFIED by the completed inventory:** budget planning ignored NHR (`self_clear.py:274`), execution rejected NHR (`effects.py:1941`), and generic error formatting (`effects.py:688`) falsely reported the unchanged SHA as stale. I:48. The handover's stale-revision hypothesis is superseded.
- **Human kernel:** cap increase was judgment; retrying an unchanged refused decision is not. **Lineage:** head-fenced durable relabel → budget self-clear retries same refusal without an effective bound.
- **Disposition/cost:** unfixed, stopped. I proposes one owning eligibility rule and deletion of external-machine-hold→NHR amplification. Existing effect retries waited 1/3/9 seconds; none explains why unchanged forbidden actions kept being planned every tick. The obstruction incidentally prevented renewed A19 entry; no paid cost shown for these ticks.

#### A21 — Closed issue did not cancel its in-flight paid leg (#280)

- **Symptom/evidence:** user cancelled Tauri #1021 soon after RESUME run `344cf4435b9f4995ab019ae69a06285a` started at 21:05:50 AEDT; issue became not-planned, run stayed running. #280; T:L7927.
- **Root cause — VERIFIED:** owned cancellation was wired to role shutdown, not issue closure observation; return could still publish against closed work.
- **Human kernel:** cancelling the issue; none for enforcing the decision. **Lineage:** durable dispatch/continuation → role-scoped cancellation only → external issue state changes are ignored mid-leg.
- **Disposition/cost:** filed, unfixed. Continued spend was possible/observed as running, but the final incremental amount is not given.

#### A22 — Blocked RESUME discarded runtime/model/session provenance (#341)

- **Symptom/evidence:** Tauri #1102 A used Codex Sol, B cold-started Opus although all four issue tier routes were Codex. X:55–66; H:89–102; #341.
- **Root cause — VERIFIED:** every BLOCKED RESUME set session to None; fresh reply config then used `FIXER_RUNTIME`/bare `FIX_MODEL`, bypassing tier selection. A productive watchdog termination shared the policy introduced for short self-blocking Tauri #604 sessions.
- **Human kernel:** model/spend policy; none for obeying it. **Lineage:** repeated self-blocking fix → blanket session forfeiture → missing continuation provenance → deploy-default cold start.
- **Disposition/cost:** #341 filed, open. B cost $17.040507 estimated, about six times A's $2.833707, but this is not proof equal work on Codex would have cost A's amount.

#### A23 — Agent's progress comment became the “human reply” (#342)

- **Symptom/evidence:** A consumed real reply at 06:08:09Z, then posted progress at 06:45:34Z as `xnmp`; B's prompt fenced that same body as `untrusted_human_reply_json`. X:76–80, A rollout line 802; #342.
- **Root cause — VERIFIED:** shared-login reply protocol accepted comments after the old needs-input prompt; consuming a reply did not move the boundary beyond the consuming leg's run window. No fresh prompt followed the timeout.
- **Human kernel:** none for distinguishing a run's own update from a later answer. **Lineage:** same-login operator compatibility → temporal prompt boundary → mid-leg publications fall beyond stale boundary → self-resume.
- **Disposition/cost:** #342 filed, open; temporal run-window exclusion proposed. This triggered B's $17.040507 leg; count that cost once, not separately under A22 and A23.

#### A24 — Timeouts were opaque; handover erased useful evidence (#340)

- **Symptom/evidence:** dashboard/events could not explain what two hour-long #1102 legs were doing. Claude session null; latest handover said no structured dead-end/verification receipt. X:68–88; #340.
- **Root cause — VERIFIED:** session copied only from terminal `ResultMessage`; timeout summary filtered out; one per-issue handover overwritten atomically; unconditional boilerplate; Claude-only transcript locator; generic trace messages overwrote substantive activity.
- **Human kernel:** whether to fund another flake investigation; none for visibility. **Lineage:** terminal-only capture + replacement handover + telemetry filtering → missing causal history → manual transcript forensics.
- **Disposition/cost:** #340 filed, open. A died during its final **24m15s of hosted-CI polling**; B died waiting for GDB/debuginfod after repeated 503/download waits. Two resumes cost $19.874214; forensic manager effort is unpriced.

#### A25 — Authorized CI investigation commands were denied by the governor

- **Symptom/evidence:** Tauri #1102's literal `gh run rerun … --failed` and `gh workflow run …` both denied as outside portable read/create allowlist; the diagnostic qualification required ten serial CI runs. `/tmp/issue-1102-findings.md:26–35`.
- **Root cause — VERIFIED:** portable static command allowlist excluded authorized workflow operations; the error suggested literal executable/argv even though both already used them.
- **Human kernel:** permission to launch CI, supplied in the task; none for the redundant literal-argv demand. **Lineage:** advisory shell parser made a deny authority → missing capability → issue parked for operator dispatch.
- **Disposition/cost:** operator involvement/diagnostic work continued through allowed paths; no distinct filed harness number found. Target root cause still unresolved. Agent paid time spent negotiating tooling is unquantified.

#### A26 — Departed-PR reads retried forever under blocked/activation-required review authority (#321)

- **Symptom/evidence:** departed-PR control failures reappeared on each due pass without consuming a retry ceiling; #321; H:29.
- **Root cause — VERIFIED in filed code audit:** reader cannot record a departed decision when family authority is blocked, so failure remains perpetually due. Distinct from A01's wrong root variant.
- **Human kernel:** genuine imported authority approval may require judgment; unbounded rereads do not. **Lineage:** migration barriers + departed-reader dedup only on success → forever-retry branch.
- **Disposition/cost:** filed, open. Journal/read cost and manager attention; no isolated model charge.

#### A27 — Review release amplified remaining mechanical fix holds into NHR

- **Symptom/evidence:** Tauri #1068 release receipt `902856c31…`, review seq **6782**, added NHR at 09:15:17Z after request `765d3cfc…`; #1084 receipt `ed1d1e2b…`, review seq **6173**, added NHR at 2026-10-11 23:11:29Z. I:7–8,48.
- **Root cause — VERIFIED:** review release `_plan(held=True)` (`review_workflow_policy.py:942,1483`) projected unrelated remaining fix holds as human attention. These were not new model disputes.
- **Human kernel:** none for multiplying a machine stop; review/flake judgment had already been supplied. **Lineage:** independent test release → preserve fix holds → shared NHR projection → budget executor then refuses self-clear (A20).
- **Disposition/cost:** inventory recommends deleting external-machine-hold→NHR projection, retaining real owner causes. Unfixed/stopped. This now identifies the previously uncertain #1068 NHR writer; shared login alone could not do so.

#### A28 — Historical `NoLinkedPr` permanently blocked fresh #999 repair

- **Symptom/evidence:** #999's REBASE handoff refused repeatedly at `workflow_control.py:145`; source run `a47061ca…` records **NoLinkedPr**, while a PR was subsequently published. I:9; fix seq **10238**.
- **Root cause — VERIFIED inventory trace:** optional historical continuity became mandatory handoff authority; the later real PR cannot satisfy an immutable earlier absent-link record. Earlier contradictory subagent permission/review requirements and accounting deaths prevented a newer authoritative return.
- **Human kernel:** no unresolved product/policy dispute demonstrated. **Lineage:** exact historical linkage/provenance proof → fresh repair bound to old absent relation → unsupported-handoff refusal → manual NHR.
- **Disposition/cost:** inventory proposes fresh current-PR execution after money/active-work reconciliation, preserving immutable historical receipts rather than inventing linkage. Unfixed. No isolated cost.

#### A29 — ENOSPC cut #1001's native rollout, then accounting authority stopped its successor

- **Symptom/evidence:** Tauri PR #1001 links issue **#938**; heartbeat write failed at 2026-10-07 04:17:27Z, native rollout `01a11428…:768` contains a cut record. Fix seq **10807** accounting-unavailable, **10814** return-authority-unavailable. I:10.
- **Root cause — VERIFIED recorded disk failure/cut evidence:** ENOSPC interrupted native accounting; outstanding repair admission refused subsequent work. General malformed-middle parsing class is C02, but this identifies the actual parked PR's initiating failure.
- **Human kernel:** none. **Lineage:** shared temporary storage → partial native write → strict accounting teardown → unacknowledged repair → manual NHR.
- **Disposition/cost:** still parked; current accounting fixes may address portions, but no end-to-end recovery proved. No isolated bill.

#### A30 — Requeued #994 was silently excluded by contradictory labels and retained attempt debt

- **Symptom/evidence:** issue #994 has `ai-ready + ai-pr-open` but no open PR after #1019 closed; `lifecycle.py:118` excludes fresh resolve. Infra escalation fix seq **13325** had removed the PR trigger; later cleanup required that trigger. I:11,54–56.
- **Root cause — VERIFIED:** requeue added ready without removing old PR-open label, cleanup depended on an escalation-removed label, and launched unreturned/cancelled repairs were charged as rebase attempts (seq **13029/13152/13231**, `control_store.py:8816`).
- **Human kernel:** user already chose fresh resolve; mechanics and false attempt debt require none. **Lineage:** A03 recovery treats launched as attempt → rebase debt reaches three → escalation strips trigger → cleanup misses terminal PR → contradictory issue labels exclude requested work.
- **Disposition/cost:** inventory proposes earlier terminal cleanup/current-state eligibility and removal of unconditional attempt charging; no code change in this report. Branch remains, no provider dispatch; previous repair spend unallocated.

#### A31 — Provider overload consumed quality allowance and left old #710 held

- **Symptom/evidence:** issue #710 remains NHR/ResolveCeiling with blocked imported issue recovery/steward. All three original native failures were **server_overloaded**, first rollout `01a09da2…:77`; rotated fix lines **9452/9569/9641**, seq **9696**. I:14,22.
- **Root cause — VERIFIED native error evidence:** provider capacity failures became `agent_error`/resolve attempt debt; migration faithfully preserved the wrong exhausted gate.
- **Human kernel:** none for provider overload; more model spend is a separate policy question. **Lineage:** generic native terminal error → quality counter → persistent ceiling → historical migration barrier.
- **Disposition/cost:** still OPEN/NHR; inventory proposes native infrastructure classification and removal of subject-level infra human stops. No new product defect or current-session paid leg claimed.

#### A32 — Provider quota failure became quality debt on #696; implementation already merged

- **Symptom/evidence:** #696 remains NHR/ResolveCeiling and imported recovery/steward blocks; first native failure **usage_limit_exceeded** at rollout `01a085d2…:850`, rotated fix **8364/8389/8413/8433**. Implementation subsequently merged as Tauri PR #738. I:15,22.
- **Root cause — VERIFIED:** native quota error collapsed into generic failure; wrapper traceback later obscured cause; imported chronology retained exhausted resolve state despite shipped implementation.
- **Human kernel:** authorized Mac access/spend or qualification-policy decision is real; obtaining already-required measurements is mechanical once access exists. **Lineage:** quota error→attempt ceiling→migration block→human stop conflates implementation and qualification.
- **Disposition/cost:** OPEN/NHR; request only the actual missing access/policy decision. Old spend not measured here.

#### A33 — Resource-failed #1010 remained operator-parked

- **Symptom/evidence:** #1010 has NHR + ai-parked; explicit command `041ec1c8…`, executor `operator:phase5-oom-disposition`, at October 8 02:03:15Z. Kernel OOM at 01:25:39 followed earlier SIGKILL fix seq **11261**, cancellation **11317**. I:13.
- **Root cause — VERIFIED:** resource failure led to explicit operator park; not an unresolved product decision about fullscreen PDF navigation.
- **Human kernel:** parking was intentional historical operational control; no current product judgment is established. **Lineage:** host resource failure → cancellation/park → no automatic clearance of intentional operator disposition.
- **Disposition/cost:** still OPEN/parked; do not unpark without resolving owner intent and resource cause. Inventory classifies mechanical root, but this report does not infer permission to reverse old park. Historical cost unisolated.

#### A34 — Infra breaker counted deterministic refusals and concealed their causes (#329)

- **Symptom/evidence:** #994's breaker text said agent died five times. Counts two through five actually included one ambiguous-accounting REBASE, one external SIGTERM and two identical prelaunch purchase refusals; four earlier refusals were uncounted before #262. [#329](https://github.com/xnmp/autonomous-issue-agent/issues/329).
- **Root cause — VERIFIED filed event/journal audit:** `_return_refused` conflated deterministic allowance state with transient infra; completion summary filtering dropped signal/exception/refusal detail; bare counter could not reconstruct causes. The old refusal ledger retained only latest detail.
- **Human kernel:** true allowance/spend decision may exist; knowing whether a provider ran or a purchase is permanently consumed is mechanical. **Lineage:** A04 adds refusal counting → deterministic refusal consumes infra allowance → generic “died” message → operator asked to recover wrong subsystem.
- **Disposition/cost:** filed/open. Reuse completion evidence and classify/check earlier; no new cause ledger added by this report. Prelaunch refusals $0, repeated diagnosis/retained capacity expensive but unallocated.

### B. Harness defects found in fixes and reviews

The following entries are **distinct reproduced defects**, including pre-ship candidates; they are not claims that every defect reached production. Unless a row says otherwise, their human-judgment kernel is **none**, root-cause confidence is **VERIFIED by the cited review/reproduction**, and there is **no measured live dollar loss**. Their cost impact is extra implementation/review/testing work and potential wrong admission, stuck recovery or accounting. This common metadata applies to each row. Repeated quotations of the same finding in later briefs are counted once.

#### B01–B09 — #298 consolidation/release defects

| Entry | Symptom and verified root cause | Evidence and band-aid lineage | What was done |
| --- | --- | --- | --- |
| B01 | Deleting inert PR repair fields changed immutable activation-plan digests and admitted-purchase wire identity; unchanged requests became conflicts. | `/tmp/issue298-inc3-original-stop-report.md:12–56`; frozen repr/encoding became proof authority, so dead behavior could not simply be deleted. | Stopped; retained identity-bearing fields/codecs while deleting enforcement. Local dead-code step −56 src. |
| B02 | Renewal of CI advanced generation and silently erased unrelated INFRA refusal debt: `(CI, deaths, refusals)=(3,1,2)→(0,1,0)`. | `/tmp/issue298-inc3-current-stop-report.md:28–55`; #268's separate generation-scoped ledger → component renewal. | Missing prerequisite exposed; later folded refusals into issue recovery in migration 42. |
| B03 | INFRA renewal retained allowance-owned NHR when only automatic budget/machine blockers remained; sufficient-headroom self-clear refused. | `P/r298c-review-findings.md:3–4`; R:20–46. Multiple label owners + SELF_CLEAR-only exception in projection. | Corrected locally in `f832acb`; not deployed. |
| B04 | Same-head PR relink after acceptance released another issue's attention. | `P/r298c-review-findings.md:6–7`; R:55–79. Head/state/label fence omitted accepted issue ownership. | Local correction revalidates both linkage directions and authority span. |
| B05 | One malformed orphan/negative-key ledger row aborted migration 42 globally. | `P/r298c-review-findings.md:9–10`; R:81–108. Refusal-ledger replacement decoded every payload before applicability. | Local correction discards provably unreachable rows transactionally before payload decoding. |
| B06 | Alias key `"01"` became issue 1 and added five refusals that the old exact-key reader ignored. | `P/r298c-review-findings.md:12–13`; R:110–125. Migration normalized previously inert data. | Exact repository/canonical text matching restored locally. |
| B07 | Empty `PRRepairState` on first merged cleanup never reset linked rebase debt. | `P/r298c-review-findings.md:15–16`; R:127–155. New merge-only reset used continuity inequality as a first-retirement proxy. | Existing dedup storage used for one-time cleanup evidence; local only. |
| B08a | RESOLVE re-arm removed attention while unrenewed INFRA remained exhausted. | R:48–53. Primary release did not share exhaustion predicate. | Corrected locally using shared predicate. |
| B08b | PR-only stale cleanup rejected by unconditional issue-linkage checking. | R:146–150. New ownership fence applied even where no issue action existed. | Ownership checked when issue-side action present; local only. |
| B09 | Secondary-only `IssueWorkflowShell` with active issue recovery lost ceiling escalation: exhausted CI selected FIX_CI, then admission refused. | R:213–245; `workflow_rows.py:1643`, candidate `fix.py:1461`, reproduction 1 failed/1 passed. New allowance reader required `WorkflowSnapshot` although activation supported the shell. | Fresh review **DO-NOT-SHIP**. Follow-up shape decision and shell work stopped by user; `f832acb` retained. |

Additional pre-ship consolidation findings are indexed individually as B67–B74 below. Their common root was treating a generic reset/projection as independent of existing ownership, receipt generation and merge obligations.

#### B10–B12 — New dead ends introduced by the latest subtractive fixes

| Entry | Symptom and verified root cause | Evidence and lineage | What was done |
| --- | --- | --- | --- |
| B10 | Historical executing/unknown machine-clear command reconciled as unknown forever; the new pending-command guard then suppressed valid operator replies. | `P/unblock-review-output.txt:5–11`; candidate `effects.py:1927`, `control_store.py:6821`. Revoking execution permission also revoked readback/reconciliation semantics. | `1abdccf` **DO-NOT-SHIP**. Proposed separation of permission to execute from retirement of already-attempted effects; no accepted fix. |
| B11 | Reservation retired budget recovery before provider launch; spawn `FileNotFoundError` consumed the reply and left an unlaunched blocked PR with no recoverable refusal. | `P/unblock-review-output.txt:13–19`; candidate `control_store.py:5945`. Early retirement added to prevent stale budget authority clearing a later question. | **DO-NOT-SHIP**; reproduction passed on dev and failed on candidate. Retire at captured-return settlement proposed, not shipped. |
| B12 | Removing synthetic test holds also removed the GET-discoverable release for already manually parked test rejections; saved internal IDs were needed to recover. | `P/codex-vhold-fix.md:5–19`; candidate `review_workflow_control.py:497`. Cosmetic selector correction removed compatibility discovery. | **SHIP-WITH-CHANGES**, then proposed release-button/park-aware patch stopped. `454ff49` remains local. Test had reconstructed gate ID internally, missing the actual operator outcome. |

#### B13–B27 — Accounting fixes required repeated corrections

| Entry | Symptom and verified root cause | Evidence/lineage | Disposition |
| --- | --- | --- | --- |
| B13 | A cumulative observation partially covered by a delayed receipt was discarded entirely, losing known spend and allowing live cap overrun. | `P/codex-301-v3.md:5`; 400→200 micros; 1,550 work under 1,450 cap settled 1,350. Receipt/counter reconciliation replaced strict completeness. | Corrected before PR #317. |
| B14 | Meter discovery failure no longer stopped live execution once terminal estimate logic was reused mid-run. | `P/codex-301-v2.md:26`; 1,224 work under 1,200 cap, reported 1,150. Best-guess terminal policy incorrectly weakened active authority. | Live stop retained; fixed before #317. |
| B15 | Recovery estimate $0 conflicted with late actual $1.25 return, losing both money correction and output. | `P/codex-301-v2.md:28`; equality-only settlement convergence after abandoned-run recovery. | Replaceable recovery-origin estimate semantics, schema 41; fixed before #317. |
| B16 | PR repair unreturned acknowledgment still rejected late $1.25 return even after generic settlement convergence was fixed. | `P/codex-301-v3.md:7`; repair return capture rejected before correction. A03 acknowledgment + estimated settlement created a second ordering. | PR repair ordering corrected before #317. |
| B17 | Mixed receipted and cumulative-only evidence dropped known root spend. | `P/codex-301-v2.md:30`; 400→200 micros; any receipt disabled aggregate remainder fallback. | Per-request plus remainder rule before #317. |
| B18 | Copied parent history was billed again to child. | `P/codex-301-v2.md:32`; 274→474 micros; ancestor exclusion deleted during streamed pricing rewrite. | Inherited-history exclusion restored before #317. |
| B19 | Completed request without usage/receipt contributed zero with no gap journal, falsely looking measured. | `P/codex-301-v2.md:34`; completion evidence removed from parser. | Gap evidence retained before #317. |
| B20 | Cumulative evidence followed by two legacy receipts double-charged, dependent on arrival order. | `P/codex-301-v3.md:9`; 400→800 micros; one-receipt matching against whole streamed contribution. | Corrected before #317. |
| B21 | Version-1 recovery ignored usable usage/frozen scalar rates. | `P/codex-301-v3.md:11`; 100 input at $2/M yielded zero instead of 200 micros. Missing child metadata was treated as missing all evidence. | Legacy fallback corrected before #317. |
| B22 | Mixed provider-dollar and usage evidence threw away known provider dollars. | `P/codex-301-v4.md:3`; 900+200→400 micros; live work 2,250 under cap 2,000 settled 1,550. All-or-nothing hierarchy instead of per-request hierarchy. | Corrected before #317. |
| B23 | Older estimated receipt replay overwrote a later normal return also priced as estimate. | `P/codex-301-v4.md:5`; 1.00→1.25→1.00 dollars, all accepted. Price basis incorrectly determined replacement authority. | Separate recovery origin introduced; fixed before #317. |
| B24 | Complete final JSON record lacking newline settled zero. | `P/codex-301-v4.md:7`; valid 100-input receipt lost because any tail was called partial. | Terminal complete-tail parsing corrected before #317. |
| B25 | Scalar compatibility runner omitted native-child spend and could exceed caps. | `P/codex-301-v5.md:5`; same 1,224/1,200/1,150 reproduction. `rate_table=None` selected parent-only pricing. | Final review required deletion if no live composition needed it; final #317 landed after correction. Conditional compatibility defect, not evidence deployed daemon passed None. |
| B26 | Version-2 recovery with null native table ignored retained scalar pricing. | `P/codex-301-v5.md:7`; zero versus 200 micros. Different supported historical shape from B21. | Corrected before #317; review found no such rows in live inventory. |
| B27 | Completion telemetry called estimated settlement `configured_rate_card`. | `P/codex-301-v5.md:9`; money/gap journal correct, cost mix wrong. | Actual basis propagated before #317. |

The earlier Claude cumulative-cost candidate and interrupted-request rewrite findings are separately indexed B75–B81 below. Subsequent best-guess policy deliberately permits journaled partial terminal evidence; an earlier “settles without complete receipt” criticism must not be presented as a violation of the later #301 policy. Missing middle records became separate #277.

#### B28–B44 — Required-check refactor defects and CI-policy compatibility

All rows below are **VERIFIED constructed/live-query review findings**, have **no human kernel**, and were corrected before PR #319 except the later conflict deadlock A18. No actual red-CI merge is established. The common lineage is replacing aggregate CI with declared policy while keeping old durable remediation captures, latches and compatibility behavior.

| Entry | Symptom/root cause | Evidence | Disposition |
| --- | --- | --- | --- |
| B28 | Empty/unprotected policy judged green test-merge rollup despite red head and emitted auto-merge. | `P/codex-303-v2.md:7–8`, `github_ci.py:115`. | Empty-policy head parity restored. |
| B29 | Captured rerun run-set no longer matched new required policy; exact-run guard refused, same-head refusal became unknown indefinitely. | `P/codex-303-v2.md:10–11`, `effects.py:1952,1994`. | Definitively obsolete debt terminally settled. |
| B30 | Persisted advisory escalation bypassed current policy, parked required-green or unreadable CI. | `P/codex-303-v2.md:13–14`, `effects.py:1990`. | Durable execution joined current requalification. |
| B31 | External pending StatusContext lost timestamp, so no stall deadline. | `P/codex-303-v2.md:16–17`, `github_ci.py:85`. | Timestamp retained. |
| B32 | Empty-policy report grouping kept old failure alongside newer success and bought FIX_CI rather than merging. | `P/codex-303-v2.md:19–20`, `github_ci.py:162`. | Latest-report compatibility restored. |
| B33 | Supported non-check rules such as required deployments made green CI unavailable. | `P/codex-303-v2.md:22–23`, `github_ci.py:55`. | Overbroad whitelist removed. |
| B34 | Different workflows with identical names/events/jobs collapsed, hiding a required failure. | `P/codex-303-v2.md:27`. | Identity preserved across workflows. |
| B35 | Rollup disappeared between reads and became Observed(empty)/Clear. | `P/codex-303-v2.md:28`. | Disappearing evidence kept unavailable. |
| B36 | Rate-limit 403 containing the Pro phrase became “no rulesets.” | `P/codex-303-v2.md:29`. | Exact no-rulesets classification, not substring. |
| B37 | Merge-revision Action run repeatedly failed head-SHA rerun verifier. | `P/codex-303-v2.md:30`. | Revision-aware remediation capture/validation. |
| B38 | Real GraphQL query selected nonexistent `CheckRun.createdAt`; every PR observation unavailable. | `P/codex-303-v4.md:10–11`; live query on harness #318. | Exact query probed against GitHub and corrected. |
| B39 | Declared policy always judged head even when test-merge checks existed, allowing wrong merge attempt or unnecessary repair. | `P/codex-303-v4.md:13–19`. | Declared policy uses test-merge when reported, head otherwise; empty policy keeps prior head rule. Manager's earlier “always head” instruction was wrong. |
| B40 | Temporary unreadable CI permanently failed escalation but its captured row still consumed the latch. | `P/codex-303-v4.md:21–22`, `effects.py:1943`, `roles.py:1990`. | Unavailable stays owed/transient. |
| B41 | Blanket Waiting guard suppressed a requested PR_RESUME and left valid human reply unconsumed. | `P/codex-303-v4.md:24–25`, candidate workflow `fix.py:1637`. | Human reply resume preserved while CI waits. |
| B42 | Same-head post-cancel success/skipped/empty reports enabled merge without proving cancelled work ran. | `P/codex-303-v4.md:29`. | Cancellation aftermath guard restored; final rebase also needed dropped guard restored (H:20). |
| B43 | Duplicate jobs within one workflow collapsed onto newer success. | `P/codex-303-v4.md:30`. | Distinct job/check-run identities preserved. |
| B44a | Waiting required check accepted stale advisory escalation despite no current stall. | `P/codex-303-v5.md:2–3`, `effects.py:1955`; any Waiting accepted instead of current stall predicate. | Current pure stall eligibility reused before #319. |
| B44b | Obsolete/refused escalation suppressed a future real required stall. | `P/codex-303-v5.md:5–6`, `roles.py:1989`; rejected capture counted as successful latch. | Success-only latch derivation before #319. |

#### B45–B57 — Dashboard projection and evidence-cleanup defects

| Entry | Symptom and verified root cause | Evidence/lineage | Disposition |
| --- | --- | --- | --- |
| B45 | Older pending feed survived newer same-head merged observation, leaving CI-running on done cards. | `P/codex-dashci-v2.md:5`; snapshots picked stale feed. | Corrected before PR #316. |
| B46 | 101-check matrix became unknown while 100 checks showed running. | `P/codex-dashci-v2.md:7`; completeness guard required every truncated check. | Indicator derived at producer/aggregate seam before #316. |
| B47 | Malformed ignore regex blanked legacy board/detail. | `P/codex-dashci-v2.md:9`; unchecked regex evaluation in API fallback. | Corrected before #316; ignore knob later removed by #319. |
| B48 | Check-name truncation removed ignored suffix, changing policy. | `P/codex-dashci-v2.md:11`; presentation truncation became policy input. | Corrected before #316. |
| B49 | Steward's checks-unknown observation erased known same-head running facts; unchanged fixer reread emitted no restoration. | `P/codex-dashci-v3.md:4`; last publisher wins on partial observations. | Local indicator correction before #316; broader feed race filed #315. |
| B50 | Removing/tombstoning newer merged feed resurrected older pending CI. | `P/codex-dashci-v3.md:6`; absence exposed stale surviving feed. | Corrected before #316. |
| B51 | Late permanently retired feed ignored by fold still affected API's independent history scan. | `P/codex-dashci-v4.md:4`; second projection authority. | Fold filtering reused before #316. |
| B52 | Existing schema-v2 observations without newly added `ci_state` lost running indicator. | `P/codex-dashci-v4.md:6`; new producer field assumed on legacy events. | Legacy interpretation corrected before #316. |
| B53 | Closed/ai-done issue hid CI on its still-open PR. | `P/codex-dashci-v4.md:8`; issue completion mistaken for PR/check completion. | Corrected before #316. |
| B54 | Delayed older feed overwrote fresher same-head CI; unchanged reread did not emit correction. | #315; T's reproduced adapter→reporter→fold→HTTP review, `projection.py:42`. | Broader temporal merge defect filed/open; not fully solved by #316. |
| B55 | Dashboard feature accidentally rolled back owned-session discovery: two owned plus 511 unrelated rollouts became unavailable. | T's dashboard review; `codex_child_usage.py:178`; #310 isolation/discovery seam touched outside scope. | Unrelated rollback rejected; #310 landed. |
| B56a | Stdout escape state consumed stderr failure text. | `P/codex-readable-v2.md:23–24`; one sanitizer state crossed independent streams. | Corrected before #288. |
| B56b | Oversized unfinished escape discarded visible bytes differently by capture chunk. | `P/codex-readable-v2.md:26–32`; incomplete sequence handling violated bounded streaming contract. | Corrected before #288. |
| B57a | Marker removal erased legitimate human review examples. | `P/codex-readable-v2.md:34–43`; broad replacement instead of exact harness marker line. | Exact marker stripping before #288. |
| B57b | Reused legacy summaries still contained ANSI. | `P/codex-readable-v2.md:45–46`; cleanup only applied to fresh path. | Legacy reuse cleaned before #288. |
| B57c | Control-string payloads survived ANSI cleanup. | `P/codex-readable-v2.md:48`; sanitizer recognized incomplete control grammar. | Corrected before #288. |

#### B58–B66 — Gate removals, body completion and verification weaknesses

| Entry | Symptom and verified root cause | Evidence/lineage | Disposition |
| --- | --- | --- | --- |
| B58 | Red-fail skipped new tests in existing files, allowing post-green assertion weakening to pass with implementation reverted. | `P/codex-325-fix.md:5,10`, `governor.py:484`. Immutable-test gate previously masked gap in revert verification. | Changed tests preserved during revert execution before PR #333. |
| B59 | Historical grant-only downgrade copied forever onto rebased heads, with no fresh reviewer. | `P/codex-325-fix.md:12`, workflow `review.py:621`. Removing active grant authority did not retire patch-id reuse of its old rejection. | Legacy downgrade recognition/fresh review before #333. |
| B60 | Multi-review skip classifier approved weakened test without modified-test notice or specialist review. | `P/codex-325-fix.md:14`, `review.py:761`. Notice attached after deciding to skip. | Notice and full-review requirement moved earlier before #333. |
| B61 | Colocated `src/widget.test.ts`/`.spec.ts` edits omitted. | `P/codex-325-fix.md:16`, `gate.py:628`. Test-name classifier narrower than conventional paths. | Conventional filename recognition added before #333; target `test_support` gap remains #334. |
| B62 | Current architecture/operator docs still described removed exemption/trailer authority. | `P/codex-325-fix.md:18`. Removal left documentation as competing authority. | Docs/manifest updated before #333. |
| B63a | Setext AC heading was not recognized, so completion duplicated the section. | T:L15274; H:22. Markdown completion parser recognized only one heading syntax. | Corrected before #331; part of eight parser regressions. |
| B63b | Indented code examples counted as real heading/closing link. | T:L15274; parser did not exclude that code form. | Corrected before #331. |
| B63c | AC subheadings ended extraction and produced an empty acceptance section despite publication success. | T's #327 review, `pr_body.py:66`; any heading ended parent section. | Equal/higher-level heading boundary before #331. |
| B64 | Naively suppressing “deliberately waiting” STALL lines hid rejected dispatches, orphaned rebase returns and undrivable holds indefinitely. | `P/codex-296-v2.md:1`; previous `wip/296-suppression-attempt`. Visibility fix removed failure evidence rather than explaining it. | Replaced by annotations in PR #318, not suppression. |
| B65 | Capacity-free rerun refactor also escalated issueless PR awaiting re-review on busy tick, removing reviewer eligibility. | T's free-rerun review, candidate `fix.py:1969`. Capacity gate removal moved a different escalation branch into always-active selection. | Fixed before PR #289; no evidence deployed. |
| B66 | Conflict-rebase tests became vacuous: unknown-mergeability fixtures exited before transport-death/inactive-component assertions. | T's rebase review; `test_fix_workflow.py:735`, `test_pr_repair_workflow.py:63`. New early conflict check made old setup stop exercising intended behavior. | Fixtures made CONFLICTING; final #338 re-review. |

#### B67–B86 — Additional distinct review and verification findings

All rows retain this section's default: VERIFIED in the cited report/reproduction, no irreducible human kernel, no measured live dollar loss unless specified. “Prior candidate” means corrected/rejected before that draft shipped, not a present production finding.

| Entry | Symptom/root cause | Evidence and lineage | Disposition |
| --- | --- | --- | --- |
| B67 | Boot refusal fold crossed deployment scope. | `P/codex-review-consolidation2.md:18`; shared DB migration assumed one deployment. | Prior consolidation correction; no deployed corruption established. |
| B68 | Renewal accepted while PR-target repair still ran. | Same prior review summary; reset ignored owned active repair. | Prior candidate corrected. |
| B69 | Head-fenced relabel stranded accepted renewal after head changed. | Same review summary; acceptance and projection generations diverged without usable continuation. | Prior candidate corrected using existing recovery lifecycle. |
| B70 | Rebase reset lacked proven mergeability/terminal evidence. | Same review summary; reset treated weak world fact as permission to refund debt. | Prior candidate corrected; later rule became first merged cleanup. |
| B71 | Issueless-PR deletion lost auto-merge behavior. | Same review summary; “dead” escalation removal changed branch precedence. | Prior candidate corrected; optional fold later deferred. |
| B72 | Retained CompletedRearm verified against later binding and broke next renewal. | T:L7736, `workflow_policy.py:495`, `workflow_control.py:1784`; durable old receipt assumed current generation. | Reproduced pre-ship consolidation defect, later corrected. |
| B73 | Drain query omitted PR repair result namespaces, allowing renewal before classification; rebase charge became stale and stayed zero instead of one. | T:L7736, `workflow_control.py:1367`; queried resolve returns instead of `pr-repair-result`/`continuation-return`. | Pre-ship consolidation correction. |
| B74 | Quiescence depended on current linkage, so closed/relinked admitted PR repair escaped checks and lost eventual debt. | T:L7736, `workflow_control.py:1731`; current GitHub relationship substituted for pending admission ownership. | Pre-ship correction; no actual live lost charge proved. |
| B75 | Missing Claude baseline before launch stranded reserved leg. | #287, `P/codex-review-claude-cost.md`, T:L9424; new transcript reader bypassed proven-unlaunched release. | Candidate DO-NOT-SHIP, parked. |
| B76 | Missing terminal receipt/baseline after launch created permanent accounting hold; resolve/review lost return authority. | #287; Claude has no equivalent remeter reconciler, new baseline dependency became workflow authority. | Candidate rejected. |
| B77 | Baseline parser accepted cost-state that Claude restore rejected, subtracting a different baseline. | #287; duplicate restore schema/interpretation. | Candidate rejected. |
| B78 | Valid zero-cost Claude resume classified unknown. | #287; positive-cost test confused zero with absent evidence. | Candidate rejected. |
| B79 | Redelivered response in a later leg window charged two runs. | T:L8156, `codex_child_usage.py:456`; prewindow receipts discarded before dedup, reproduced $0.006800 twice. | Receipt rewrite corrected before #284. |
| B80 | Completed root turn without receipt settled despite then-current complete-evidence requirement. | T:L8156, `codex_child_usage.py:479`; terminal interrupted-request permission applied too broadly. | Corrected under #284 policy; #301 subsequently changed terminal estimate policy deliberately. |
| B81 | Receipt recovery still required root counters. | T's #284 review, `spend_recovery.py:199`; new receipt pricing kept old counter prerequisite. | Corrected in candidate before merge. |
| B82 | Installer test cleanup killed parent but leaked grandchildren. | T:L15875, `tests/adapters/test_service_installers.py:1220`; test supervision did not own full process tree. | Corrected during installer reviews before #332. |
| B83 | Broad #298 test run omitted architecture manifest entry for new modules. | `/tmp/issue298-inc3-fix-report.md:43`; domain additions not registered. | Fixed `5d55dae`, source unchanged. |
| B84 | Twelve #298 fixture/assertion contracts still assumed separate refusal ledger/unfenced action or invalid review timestamp. | Same report:17–44; one group of stale test contracts, not twelve demonstrated production regressions. | Reproduced 13 total failures/88 passes including B83; focused corrections, net src 0. Later P1s still real. |
| B85 | App/domain resolve-rearm modules with same basename collided in combined multiprocessing collection. | `/tmp/issue298-inc3-final-report.md:191–195`; import/test collection boundary. | Separate normal-mode suite passed; no runtime failure claimed. |
| B86 | Test-only rejection retained through execution renewal blocked changed-head PR. | T:L8442; a releasable rejection was carried as generic hold instead of retaining only actual ESCALATE. | Candidate corrected with ESCALATE-only retention before #281; later selector A09 was a different edge. |

#### B87–B90 — #298 increment 1 changed transport compatibility

These are VERIFIED comparative probes against dev, no human kernel, no measured live spend. The common lineage is replacing several old request names with a unified `release` transport while changing semantics that the transport was meant to preserve. All were corrected before PR #314; canonical release alone passing tests was insufficient.

| Entry | Symptom/root cause | Evidence | Disposition |
| --- | --- | --- | --- |
| B87 | Legacy request-rearm ID X and retry-authorization ID X were valid independent namespaces; new cross-namespace check rejected retry 409 and stored no grant. | `P/codex-298-v2.md:5`, `workflow_commit.py:222`. | Separate legacy namespaces retained. |
| B88 | Already-open dashboard client rejected upgraded response `actions[].operation="release"` as invalid, hiding recovery controls. | `P/codex-298-v2.md:7`, `review_control.py:160`; real old-client Vitest. | Response compatibility restored. |
| B89 | Guidance whitespace normalization no longer replayed same ID; persisted audit changed RequestRearm to ReleaseGate plus transport digest. | `P/codex-298-v2.md:9`, `resolve_contracts.py:147`; transport fingerprint replaced domain identity. | Original normalized replay/audit contract retained. |
| B90 | Cancelled/replaced review request A replayed successfully after continuation B, instead of previous rejected 422. | `P/codex-298-v2.md:11`, `review_workflow_control.py:1956`; unified path bypassed current-intent check. | Intent/replay compatibility restored before #314. |

### C. Cost and spend issues

#### C01 — Codex usage completeness rejected harmless re-emission (#261 → PR #267)

**Symptom/evidence:** completed useful Codex repair retained its quote because a repeated identical `token_count` broke observation/receipt reconciliation. #261; T:L5665. **Root cause — VERIFIED:** duplicate accounting observations treated as new incompatible usage. **Human kernel:** none for idempotent reads; genuine uncertain spend policy remains judgment. **Lineage:** strict native usage authority → duplicate observation → unpriceable run → A04/A05. **Disposition/cost:** PR #267 (`67a3563`) supports idempotent re-emission and bounded terminal remetering; session summary records run `3c18a59b` settling **$0.713348** at 04:20Z, then #1019 repair admitted 04:21:02Z. This did not resolve every #247 ambiguity.

#### C02 — Native usage ambiguity, partial middle records and discovery bounds (#247, #277; PRs #284/#310/#317)

**Symptom/evidence:** #247's parent had 133 observations/130 receipts, owned review child 18/15, others 3/3 and 7/7. #277 found **34/3,987** rollouts with non-trailing malformed records; kill followed by resume made a partial tail a middle record. **Root cause — VERIFIED:** strict full-file parsing preceded leg-window filtering; discovery bounded unrelated files before finding owned evidence; incomplete/ambiguous native evidence could not settle. **Human kernel:** pricing incomplete evidence; parsing unrelated or interrupted writes is mechanical. **Lineage:** exact receipt completeness → retained quote → recovery parser → later resumed writes poison old/new legs. **Disposition/cost:** #277 open; #284 implemented receipt-window sum/interrupted request zero; #310 owned-session discovery and HOME isolation; #317 terminal best-guess settlement. Historical quoted $50 exposures are reservations, not bills. Pending field canaries do not prove every malformed/ambiguous case fixed.

The distinct defects within that accounting seam are:

| Entry | Verified root and lineage | Judgment, disposition and cost |
| --- | --- | --- |
| C02a — Ambiguous complete native usage | Receipt/observation ownership disagreement on parent plus owned child, #247; complete-session authority refused partial reconciliation. | Actual incomplete-price policy is judgment; #317 permits terminal estimates, not fabricated completeness. $50 retained quote per affected run, no clean incremental bill here. |
| C02b — Partial middle record poisons resumed session | Interrupted last write becomes interior after resume; parser fails before selecting current leg's window. #277. | No kernel in correctly scoping interrupted data. OPEN; #284/#317 do not by themselves prove every non-trailing case repaired. |
| C02c — Owned rollout lost behind unrelated backlog | Discovery applies max-files before session ownership; ambient HOME also affected tests. PR #310, F:35. | No kernel; owned-session selection/HOME isolation merged, ten-leg field proof pending. Prevents spurious unavailable accounting, dollar saving unmeasured. |

#### C03 — Claude cumulative session dollars charged once per leg (#287; #292)

**Symptom/evidence:** 10 of 11 same-session resumes among 122 audited runs were charged cumulative totals. Four legs actually ~$2.015/$0.657/$0.339/$0.787 were booked ~$2.015/$2.672/$3.011/$3.797. [#287](https://github.com/xnmp/autonomous-issue-agent/issues/287); T:L9424. **Root cause — VERIFIED:** ResultMessage `total_cost_usd` is restored session cumulative cost; runner/accounting treated it as a leg amount. **Human kernel:** choosing pricing fallback; no kernel in duplicate charging. **Lineage:** provider-dollar preference → session resume/fork → leg ledger books cumulative figure. **Disposition/cost:** candidate rejected for B's four new hold/parser defects. Open; about **$27 of $104 booked since October 1** was estimated overstatement, not fresh cash waste caused by this session. #292 proposes unified usage/rate-card pricing.

#### C04 — Reservation contention and actual exhaustion shared a human-budget status (#258/#272)

**Symptom/evidence:** at 00:03Z Tauri #994 and Ballast #62 were refused under global $80 with committed ~$52, mostly another $50 reservation, despite little settled spend. #1049 later saw $150 repo headroom $32/$27.66, below its $50 quote; its comment falsely said it had spent its own leg cap. **Root cause — VERIFIED:** admission refusal maps to `MAX_BUDGET_STATUS`; shared active exposure and settled exhaustion collapse into same projection. **Human kernel:** raising caps; none for waiting for another active reservation. **Lineage:** maximum reservation + multiple deployments → human budget block → self-clear → headroom lost before next dispatch → refuse/clear loop. **Disposition/cost:** caps raised global 80→500, repo 80→150→300; #272 +89-src draft held/superseded. #1049's repeat routes were about **$0.30 each**; denied provider legs were $0. No net total established.

#### C05 — Stopped deployment kept orphan reservations (#297)

**Symptom/evidence:** self run `57b4f084` retained $50 after self-hosting stopped; recovery depended on that deployment booting. H:12; T:L9424,11161,12856. **Root cause — VERIFIED:** recovery ownership/liveness coupled to the stopped deployment, though the money pool was shared. **Human kernel:** whether to restart a stopped deployment; none for reclaiming terminal exposure. **Lineage:** per-deployment recovery + global admission pool → stopped role continues blocking healthy repos. **Disposition/cost:** #297 filed; no verified final disposition of self reservation in the read sources. Tauri `9fdbdc47` settled, so do not keep describing both reservations as active.

#### C06 — Config/cap restart lost an unpublished review and repurchased it (#273/#293)

**Symptom/evidence:** cap-change restart three seconds after leg completion dropped unpublished verdict; review was repurchased for **$0.84**. T:L7686; #273. **Root cause — VERIFIED:** launch-time config required restarting the trio, with completion/publication not fully quiescent. **Human kernel:** cap/model changes; mechanical synchronized reinstall is a gap. **Lineage:** immutable installed snapshot → whole-trio restart → completion race → repeat review. **Disposition/cost:** operator began waiting for an idle window; #273 live caps and #293 graceful snapshot reload filed. $0.84 is the specifically identified redundant review, distinct from #1068.

#### C07 — Router bought too expensive a tier for pattern-following work (PR #285)

**Symptom/evidence:** user questioned why “About” command Tauri #755 needed Sol; T:L9084. **Root cause — VERIFIED in task/route change:** router over-weighted repository breadth rather than existing-pattern simplicity. **Human kernel:** desired cost/quality tradeoff; none for obeying explicit route policy. **Lineage:** tier classification → stronger model for routine edits. **Disposition/cost:** PR #285 (`8223e5b`) routes well-specified pattern-following changes to trivial; ten-route/first-pass live guardrail still pending (F:29). Dollar saving not measured.

#### C08 — Model/effort configuration did not match operator intent (#290/#305)

**Symptom/evidence:** reviewer model appeared in shared config, repo config, installed overlay, generated copy and old fallback; installed `CODEX_MODEL` still gpt-5.6-sol. Per-tier Sol@xhigh could not be represented; a feature branch was started then cancelled. H:7,11; T:L9890–10073,10225–10229. **Root cause — VERIFIED:** runtime/model/effort chosen by several paths and stale installed snapshots; continuity legitimately kept old sessions even after defaults changed. **Human kernel:** model/effort policy. **Lineage:** config layering + resume provenance + escalation ladder → unclear effective selection → proposed extra suffix → user retraction. **Disposition/cost:** reviewer Sol/high; #290 legacy fallback removed via #322, review default refreshed via #311; tier-effort work stopped, #305 filed. No measured waste total; do not call legitimate Codex continuity itself a defect.

#### C09 — Every PR absorbed a red base branch (#313)

**Symptom/evidence:** pre-existing CI failures drove FIX_CI and escalations for unrelated PRs; H:29; #313. **Root cause — VERIFIED class in filed evidence:** repair attribution did not compare failing head with base/known shared failure. **Human kernel:** genuine ambiguous flake versus regression; shared known base failure is mechanical attribution. **Lineage:** all PR CI reds treated as repair owed → repeated paid legs per PR. **Disposition/cost:** filed/open. Tauri #997's FIX_CI alone recorded **$8.12** on smoke-flake work; #1074 consumed three CI-fix attempts. Not all target reds were flakes: `P/codex-1065.md:7` identified a branch-specific empty-folder cue regression.

#### C10 — Productive investigations spent wall budget waiting for CI/downloads

**Symptom/evidence:** #1102 A's last 24m15s polled run `38214005566`; B built Docker ~14m14s, repaired runtime setup ~8m26s, ran concurrent full loops ~21m12s and waited ~20m26s for debugger downloads, with overlaps. X:14–53. **Root cause — VERIFIED activities; HYPOTHESIS optimal scheduling:** a paid interactive leg held a wall-clock slot across slow external work; there was no recorded operator activity view or successful phased handoff. **Human kernel:** continue spending on non-reproducing flake. **Lineage:** monolithic leg + one-hour wall cap + no durable useful progress summary → timeouts/reconstruction. **Disposition/cost:** left parked; #340/#341/#342 filed. Not every dollar of $19.874214 was wasted: A built reviewed diagnostics and committed/pushed part of them; B obtained cores without resolving root cause.

### D. Target-repository issues

#### D01 — Shared Cargo output race (#312 → Tauri #1093/PR #1097)

**Symptom/evidence:** independent operator tests on #1084/#1068/#1075 failed with missing tauri rlibs/E0463, despite clean reviewer findings; counted rejections needed releases/re-arm. H:30,41; #312. **Root cause — VERIFIED forensic diagnosis:** vendored wry `build.rs` emitted rerun directives only on Android; Linux fell back to whole-package mtime fingerprinting. Different worktree checkout mtimes sharing `CARGO_TARGET_DIR` rebuilt wry→tauri→tauri-plugin-log and removed output under concurrent doctest. **Human kernel:** classifying captured failure as infra; underlying race is mechanical. **Lineage:** shared build cache + worktree differences + independent/gate concurrent tests → compiler infrastructure failure becomes rejection. **Disposition/cost:** Tauri #1093 made build-script tracking unconditional; PR #1097 merged 16:53:07 AEDT. Harness #312 remains open pending a clean week; root target fix does not independently prove infra exits no longer count as rejection. Repeated review/repair costs unallocated.

#### D02 — Linux native smoke renderer/session loss (Tauri #1102)

**Symptom/evidence:** different specs failed on unrelated branches while reruns/dev passed; run/job/artifact refs include `38200468596/114652602969/11725682080`, `38202213032/114657859850/11726831089`, `38190363505/114640215248/11724051959`, `38187934800/114614242219/11720862503`. `/tmp/issue-1102-findings.md:9–20`.

**Root cause — VERIFIED symptom, HYPOTHESIS initiating cause:** renderer termination coincided with stalled WebDriver commands; it is not proven whether a native crash/hang initiated the error or termination followed automation timeout. The apparent “new window path never ready” was misleading: initial origin navigation and second token acknowledgment had completed, no child was opened, and `navigateTo` wrapped driver failure as status-bar-not-shown. Do not present the original readiness-race hypothesis as established.

**Human kernel:** whether further costly diagnostics are worthwhile; what evidence a hosted runner can expose. **Lineage:** broad native smoke on every branch → flaky failure → paid CI repair/ceiling → diagnostic issue → A22/A23 recovery bugs. **Disposition/cost:** parked `ai-blocked`; diagnostics green on hosted Ubuntu/Windows captured no original core. Corrected local baseline had 1/40 native focus failure, zero session-loss failures. Opus produced three SIGABRT cores from six full iterations, but mixed setup/profile failures do not prove original cause. No target root fix or ten-consecutive-run qualification.

#### D03 — CPU-heavy Vitest used 5-second defaults (Tauri #1104)

**Symptom/evidence:** #1088 falsely rejected twice: registry churn at `tests/plugins/registry-churn.test.ts:87`, native-suite TypeScript compiler test ~3.1s idle exceeded 5s under load. **Root cause — VERIFIED timeout; HYPOTHESIS exact share caused by manager load:** deadline was a performance test unintentionally applied to correctness work. **Human kernel:** preserving proof while changing hang guard. **Lineage:** shared CPU + independent tests → timeout → counted rejection. **Disposition/cost:** #1104 open, asserts unchanged; requested load-loop verification. No demonstrated post-fix saving.

#### D04 — Git-graph complexity guard was a 150ms wall-clock assertion (Tauri #1105)

**Symptom/evidence:** #1074 test at `tests/domain/git-graph-branch-line.test.ts:293` measured **178.9ms**, failing `<150`. **Root cause — VERIFIED brittle contract; HYPOTHESIS exact slowdown:** absolute timing cannot distinguish scheduler contention from superlinear algorithm. **Human kernel:** choice of complexity proof. **Lineage:** performance guard + shared host → unrelated PR rejection. **Disposition/cost:** #1105 open; operation count/ratio plus reverted-quadratic proof proposed. Paid false-repair cost not separately measured.

#### D05 — Watch-ack timeout poisoned sibling test (Tauri #1106)

**Symptom/evidence:** #1068 recursive write acknowledgment timed out at `test_support/watch_observation.rs:935`; first streaming test panic poisoned shared mutex and failed its sibling. Both passed 5/5 alone. **Root cause — VERIFIED fixed 5s timeout and poison propagation; HYPOTHESIS contention's precise causal share:** wall-time load sensitivity amplified one failure into two. **Human kernel:** classifying infra/flake before release. **Lineage:** short hang guard + shared test lock + host contention → independent rejection → A19 repair loop. **Disposition/cost:** #1106 open; longer hang guard and poison-safe helper proposed without production watcher changes. First #1068 blocked repair $13.441554 is associated with this failure; redundant second leg counted under A19.

#### D06 — PTY cwd test missed actual command output (Tauri #1107)

**Symptom/evidence:** #1074 failure captured only `pwd && exit` and bracketed-paste prompt, no cwd text; another macOS failure said shell never exited. F:25; #1107; `P/codex-free-rerun.md:55`. **Root cause — HYPOTHESIS:** reader deadline, shell readiness/order or scheduling; one busy-host observation does not establish which. **Human kernel:** none after reproduction. **Lineage:** bounded wall-time PTY test → load/readiness → unrelated review/CI failure. **Disposition/cost:** #1107 open; explicitly requested 50 idle/50 loaded runs before deciding fix. Preserve distinction from proven Cargo race.

#### D07 — Review worktree missed TypeScript dependency

**Symptom/evidence:** one #1088 operator-test rejection lacked `node_modules/typescript`; T:L18970's week audit. **Root cause — VERIFIED missing environment input; HYPOTHESIS provisioning path:** exact install mechanism not traced in that source. **Human kernel:** none. **Lineage:** fresh review worktree + local operator test authority → environment failure counted as PR rejection. **Disposition/cost:** classification recorded, no distinct corrective PR cited; one rejection, dollar cost not isolated.

#### D08 — Tauri required too much hosted CI for unrelated changes

**Symptom/evidence:** 12 workflows; image-crop ~54 runner-min across three OS despite only ~1/8 sampled PRs touching crop/codec; required webkit four shards ~107 runner-min; Windows smoke median 38m, p90 47m. `P/ci-analysis-summary.md:3–19`.
- **Root cause — VERIFIED trigger/cost sample:** broad path triggers, expensive advisory matrices, insufficient docs-only short circuit. **Human kernel:** repository CI coverage/cost policy; user approved items 1,2,5 only.
- **Lineage:** all source changes invoke platform matrices → queues/long CI → A13/A17/C10 paid waiting and repairs.
- **Disposition/cost:** Tauri PR **#1108**, `ci/trim-pr-workflows`, open at read time. Narrows image-crop paths, moves macOS crop to nightly/dispatch, adds required-context-preserving docs-only gating and bounds perf/installer jobs. Caching was **not added**: author says some caches already exist and setup-bun lacks cache input. Claimed savings ~54 runner-min on non-crop changes, ~88 minutes wall on docs-only; estimates, not field measurements. Required/platform jobs were still running/queued; no merge claimed.

#### D09 — Codex guidance truncation/stale duplicated target instructions

**Symptom/evidence:** Tauri AGENTS ~31KB stale copy of CLAUDE ~40KB; **36 warnings since October 7** under 32KB guidance budget. H:83; T:L18166. **Root cause — VERIFIED size/duplication; HYPOTHESIS consequences per individual leg:** stale file consumed most cap, authoritative guidance could be truncated. **Human kernel:** target governance/doc consolidation. **Lineage:** duplicated standing instructions grow → runtime cap → stale partial policy. **Disposition/cost:** user explicitly dropped this work. Pointer/symlink/trim recommendation recorded; no target change. Unmeasured prompt/rework cost.

#### D10 — Modified assertions in `test_support` escaped notice (#334)

**Symptom/evidence:** fixer for Tauri #1093 widened unrelated `test_support/file_mutation.rs` assertion; reviewer accepted exact production-warning match but no modified-test notice identified it. H:45. **Root cause — VERIFIED:** path classifier did not recognize non-test-named support files. **Human kernel:** whether assertion change is justified belongs in review; identifying changed test behavior is mechanical. **Lineage:** replacing test-grant chain with diff-derived notice → residual path-name classifier blind spot. **Disposition/cost:** #334 open; no evidence the widened assertion was harmful. Distinct from blanket claim agents weakened tests unchecked.

### E. Operational and manager-process issues

| Entry | Symptom/evidence | Root cause and human kernel | Band-aid lineage; disposition and cost |
| --- | --- | --- | --- |
| E01 — Classifier-blocked ordinary management | Native runner installer denied as persistence; PR #248 close/reopen/merge/restart and even a read-only status denied; #993 reply denied; #1068 dashboard release and #999/#1001 NHR removal denied. T:L1345,1587,2120,7861,18902,19728; H:62,115. | **VERIFIED** separate auto-mode action classifier. Permission for live changes can be judgment; repeated mechanical recovery denied after task direction increased operator burden. | Automation has no sanctioned capability for required management → user runs command. No bypass claimed. Existing manager reference's older raw-API examples did not guarantee current approval. No priced total. |
| E02 — Four full suites filled tmpfs | Four concurrent `make test` runs exhausted **6.3GB** XDG runtime tmpfs; one agent reported 61 failures/19 errors, clean in isolation/rerun. T:L3836,4213,5665. | **VERIFIED** shared temporary capacity, over-broad parallel validation. No human kernel. | Agent fan-out + every branch runs full suite → ENOSPC/SQLite/temp errors → rerun on `/tmp`. Later serialized; elapsed/model supervision cost unpriced. |
| E03 — `/tmp` workaround also exhausted storage and changed Git discovery | #298 inc2 first run exhausted /tmp capacity/inodes; worktree-internal fixture root caused outside-repo CLI test to discover repo; disk run interrupted with unnamed failure. `/tmp/298-inc2-handover.md:35–51`. | **VERIFIED** storage/fixture placement; unnamed disk-run failure **HYPOTHESIS**, not attributed to source. No human kernel. | tmpfs workaround → slower disk/inside-checkout fixtures → test-context defect → more reruns. Final green gate proved 3,843 domain/4,178 adapters/3,709 app/123 hooks. |
| E04 — Manager's own tests caused contention during target review | Load reached ~110; #1068 failed while manager suites, #1102 gate and #1090 rebase overlapped. T:L11161,18970; H:57–62. | **VERIFIED overlap/high load; HYPOTHESIS exact rejection attribution.** No inherent kernel; flake classification still judgment. | Fix-on-the-spot + many concurrent validation legs → timing failures → paid repairs/release. User approved quiet-host full suites and target #1104–#1107; host-wide lock deferred as added surface. |
| E05 — Resource rule breached even after focused-test limit | #298 dead-code step ran 4-worker and 2-worker selections briefly together. `/tmp/issue298-inc3-current-stop-report.md:113–115`. | **VERIFIED** overlapping commands exceeded machine-wide four-worker rule. No kernel. | Per-task limits did not enforce global concurrency. Disclosed; no observed damage tied to that exact overlap. |
| E06 — Self-matching `pkill -f` killed manager shells repeatedly | T:L1111 (October 10 context),5665,6990,18196; compactions inconsistently say once/twice. | **VERIFIED** pattern appeared in invoking shell argv. No kernel; source counts disagree. | Attempted cleanup of concurrent test/watch jobs killed its controller. PID/owned task stop adopted, mistake recurred. Interrupted commands/verification, unpriced. Not evidence this caused unknown SIGTERM #270. |
| E07 — Shell/tool mistakes and backup selection | Foreground `&` mishap, zsh didn't split `$runs`/`$U`, `echo` mangled JSON, lexical sort selected wrong WIP backup. T:L9424,11161,13354,13956,19148. | **VERIFIED recorded manager errors.** No kernel. | High task count + shell orchestration/backup surgery → failed commands or misleading empty reads. Corrected using explicit IDs/safe serialization/specific backup; no established data loss. |
| E08 — Hot reload silently stopped units | Stale canary drop-ins cleared force-restart/used `Restart=no`; finite-window drop-ins stopped roles at 03:37, not intended completed work. H:7; #265; T:L5665. | **VERIFIED** effective systemd policy disagreed with exit-87 contract. Human kernel: window duration; missing restart is mechanical. | Live HEAD reload + layered systemd overrides → exit 87 becomes silent stop. Old drop-ins removed; open-ended tauri resumed 08:24. Doctor gap #265 open. |
| E09 — Installed snapshots drifted from edited source config | Service used installed ~/.config snapshot; edits to repo/global/generated files didn't necessarily update it; stale model pin corrected by reinstall. H:7; T:L304,9890–9901. | **VERIFIED** multiple copied layers and install-time model/config capture. Model/cap policy is judgment. | Config corrections require whole-trio restart → C06; legacy fallback removed #322, #290 still broader/open. |
| E10 — Dashboard restart/JSON errors | Restart on every harness HEAD change created ~30s outage and raw JSON parse errors; reviewer display default stale. PRs #286/#311; F:32. | **VERIFIED** reload coupling and response handling. No kernel. | Global live HEAD + UI transport → frequent restart→bad response. Parse errors translated; RestartSec shortened to 3s, not full decoupling (#294). Field restart canary pending. |
| E11 — Installer refused its own dashboard listener | MainPID 1947924 was uv, listener child 1947940 on 8321; installer called it foreign. H:9; F:42; #245. | **VERIFIED live reproduction:** PID equality used instead of unit cgroup ownership. No kernel. | Launcher wrapper → child owns socket → anti-foreign-listener check false positive. #332 `5cca2a0` merged; next live reinstall proof pending. |
| E12 — Dashboard GraphQL 502/504s blanked GitHub facts | Full 200-PR all-state query with comments/rollups/reviews failed ~11s on almost every poll. F:48; H:53. | **VERIFIED** heavy query fan-out; no kernel. | Rich status dashboard → expensive historical query → no facts → more operator probing. #337 split open/full ~2.3s plus history/light ~4.2s; first live `gh_ok:true`. Remaining 1,000-issue query measured 27.8s; not all API timeout risk eliminated. |
| E13 — Released escalation remained on board | Typed release removed NHR remotely but ActionApplied lacked label moves, so board still said Needs you/Escalated for #1056/#1068. F:49. | **VERIFIED** event projection lacked semantic transition. No kernel. | Durable effects separate from telemetry fold → stale synthetic human task. #339 `8251a37` carries relabel moves; next live typed release canary pending. |
| E14 — Routine testing refusal reported as failed GitHub review POST | #320; H:29; journal stage blamed remote POST when ordinary-test stage denied approval. | **VERIFIED filed stage attribution**, no kernel. | Independent-test approval receipt→generic submission failure log→operator diagnoses wrong subsystem. Filed/open; no cost isolated. |
| E15 — No durable gate-denial census | 122,458 journal lines and 25.4k events lacked gate-deny event; sample only recoverable from 74 Claude fix/12 review transcripts. T:L14815. | **VERIFIED audit limits**, no kernel. | Advisory log callable + model-visible denial→no durable count→manual sample. No new instrumentation shipped; do not claim sample equals all-session totals. |
| E16 — Manager jumped to runtime hypothesis before reproducing | Initially framed #261 as obsolete Codex continuity and proposed fresh configured runtime. User challenged at T:L3801,3867,3927; correct issue was duplicate metering. | **VERIFIED mistaken diagnosis/process;** no kernel in replacing a working runtime to mask accounting. | Default model change + resumed Codex→correlation mistaken for cause→wrong fix brief. Reproduced meter defect, retained Codex, shipped #267. User explicitly demanded more diligence. |
| E17 — Fix briefs increased surface despite standing rule | #262/#269 merged with new state; #271 +325 and #272 +89 initial drafts added more; user challenged at T:L6203. S:3; M:65–67. | **VERIFIED rule failure**, no kernel unless unavoidable growth explicitly justified. | Incident→counter/hold→new release gap→another variant. PR #276 codified surface rule; #298 slice still grew +513 and stopped. |
| E18 — Running dev advanced under open target PRs | Many harness merges/hot reloads; manager preserved staged architecture WIP via inverse/3-way apply; #298 rebased as dev advanced. H:42; T:L9424; `/tmp/issue298-inc3-final-report.md:8–11`. | **VERIFIED mutable deployment base**, governance kernel about release cadence. | Self-management + live checkout HEAD→rebases/reviews/restarts contend with product work. #304 immutable release filed; no immutable cutover. No proven WIP loss. |
| E19 — Self-hosting was stopped, but architecture goal kept demanding long unattended proof | Early self canary/native CI recovered #253; user stopped self at **13:02 AEDT Oct 11**, T:L3523. Transcript also contains repetitive stop-hook feedback for unmet architecture/48h goal. | **VERIFIED scope/goal tension**, human kernel: choose deployment and stop. | Long architecture acceptance objective + blocked prerequisites → repeated goal/poll noise rather than productive acceptance. No claim two ≥48h windows completed. |
| E20 — Ballast bootstrap could not self-fix its harness prerequisite | User noted Ballast #67 prevented running harness, then requested outside-harness fixes; T:L5223–5245. Old installer invocation restarted default runner, killing #266 CI; cosmetic cwd `find` warning. T:L5665; F:52. | **VERIFIED bootstrap/installer events.** Host package/runner install is a real permission kernel. | Harness needed target gate→target prerequisite queued to same unusable harness; single-profile installer touched other runner. #266 adds hardened Ballast profile/refusal; user installed runner; Ballast later disabled. CI rerun/time cost, no model total. |
| E21 — Idle/wall limits and missing pinned companion obscured runtime availability | User challenged “900s timeout” T:L8224; #993 pin lacked adjacent `codex-code-mode-host`, fixed by symlink (T:L7861). #1102 later hit 3,600s wall cap, a different limit. | **VERIFIED deployment artifact/limits;** kernel: timeout policy. | Pinning executable without companion→runtime refusal→human unblock; fixed wall/idle conflation prompted explanation/change. No comprehensive runtime-packaging PR cited. |
| E22 — Manager juggled too much and intake outpaced clearance | User “truckload blocked” T:L7630, “reduce velocity” L9084, “9 things blocked…manager…too much” L19402. | **VERIFIED user direction and backlog symptoms; HYPOTHESIS exact optimal WIP limit.** Human kernel: prioritize/scope. | Multiple targets + paid work + fixing harness + UI changes + suites + cron→manager becomes indispensable scheduler/recovery API. Intake frozen by removing ai-ready #1055/#1081; all daemons then disabled, poll cron deleted. No new admission-limit mechanism added. |
| E23 — Evidence quality/verification lag | Numerous merged rows still pending; some “verified” rows prove absence of errors or first GET only, not full contract. H:16; F:22–53. | **VERIFIED ledger statuses**, no kernel. | Rapid fixes before live trigger→next incident assumes unproven recovery works→repeat investigations. Preserve pending verdicts; this report does not upgrade them. |
| E24 — Standing prompt/form gates consumed effort with little outcome evidence | Sample: 25 AC-heading denials (~17/74 sessions), ~37 static-command denials (~30), 6 existing-test denials, 3 unread screenshots, 1 duplicate image. T:L14815. | **VERIFIED sampled denials; HYPOTHESIS overall value/cost.** Review/trust policy is judgment. | Rule duplication/form receipts→mechanical retries→more model tokens. #331 removes body/visual bookkeeping; #333 removes grant chain; #335 trims prompt. User retained red-first discipline and did not approve broad governor/self-review removal. |
| E25 — Terminal PRs still required imported execution authority | PRs #679/#725 merged, #727 closed; control DB already records ReviewTerminal, yet execution counter read precedes candidacy/terminal filtering. Inventory counted **322 messages per PR**. I:16,20; `roles.py:2921`, `review_runtime.py:162`. | **VERIFIED inventory/code trace**, no human kernel. Historical chronology was required to decide nothing should run. | Migration compatibility + late terminal check→false holds/noise; delete counter reads for terminal/ineligible PRs. Filed noise class #264/#295 partly fixed, this residual unfixed. No paid leg needed. |
| E26 — Nonadopted Dependabot PR caused irrelevant departed authority read | PR #987 open, adoption disabled, absent from feed; archived DB decision still Oct5. I:17,20; **79** rate-limited departed errors overall. | **VERIFIED unnecessary lookup; HYPOTHESIS sole PR #987 attribution** because journal omits target. No review owed, no kernel. | Execution-backed external holds read before candidacy→migration exception→no scheduled-read advancement→repeat anonymous errors. #321 class, unfixed. |
| E27 — Unattributed external SIGTERM on useful Codex work (#270) | Run `9fdbdc47` on #1019 exited −15 at **15:27:15 AEDT Oct11** while `.harness/test-cmd` reached Playwright [1/7]. Watchdog explicitly says not its kill. #270. | **VERIFIED signal; HYPOTHESIS sender**. Earlyoom/kernel OOM, reload, target signal code and manager kill commands ruled out in inspected interval; distinct runner UID could not send it. No human kernel in attribution. | Missing signal-send provenance→unexplained infra death→accounting retained quote→A04–A07. Instrumentation proposed, no sender found; do not attribute it to E06. |
| E28 — Harness timing tests themselves flaked under load | Shutdown deadlines depended on real wall clock (#257); native cap clean-exit raced watchdog (#309). T:L5665; PRs #259/#309. | **VERIFIED test races**, no kernel. | Tight timing + parallel suites→false infrastructure failures→reruns. Injected clock for shutdown, synchronized cap case; merged. Costs unpriced. |
| E29 — Settlement transaction read time more than once | Millisecond difference between charge settled_at and reservation updated_at made migration canary reject historical owner attribution. T:L1175,1345; F's #249 timestamp row. | **VERIFIED deterministic tick-clock reproduction**, no kernel. | Timestamp equality used as proof identity→multiple clock reads within one transaction→valid settlement fails compatibility. `33898d4` uses one instant; merged, live settlement proof pending. |
| E30 — SIGTERM handler ownership polluted callers/teardown taxonomy | Role main left signal state in test/caller process; early restoration would make second teardown SIGTERM exit 143 instead of stable TRANSIENT. T:L1251,1306,1345; F's #249 drain row. | **VERIFIED tests/review**, no kernel. | Process supervision global handler→reuse in tests/callers→caller exit/cleanup effects; simplistic restoration breaks daemon teardown. `2fb5499` scopes caller handling while daemon owns handler through exit; merged, real stop live proof pending. |

## 3. Band-aid lineage analysis

These chains separate original legitimate aims from mechanisms that created the next incident. An arrow means a demonstrated causal dependency unless explicitly labelled inference.

### 3.1 Independent tests became an additional execution/recovery system

```text
Trust concern: agent-written claims/markers cannot authorize approval (ADR11)
  → 2026-10-05 independent operator tests, 0294b3e
    (manager quoted +841; whole-commit measured src net +1,015)
  → exact head/patch/config/tree receipts and execution episodes
  → unavailable-evidence backoff, ceilings and Renew execution holds
  → separate test-only rejection release (PR #281)
  → release-eligible receipts interpreted as confirmed ESCALATE verdicts
  → synthetic dashboard verdict holds (#999/#1001; local 454ff49)
  → removing them hides historical parked-PR release (B12)

Concurrent local tests + shared Cargo output + short correctness timeouts
  → infrastructure/load failure overrides clean model approval
  → REQUEST_CHANGES counts toward 4-rejection ceiling
  → paid ADDRESS_REVIEW concludes no change + operator release/re-arm
  → #1068 blocked question + machine self-clear (chain 3.2)
```

ADR11's unavailable-execution renewal is distinct from an ordinary trusted test failure: the latter should request changes, not automatically demand a human. V:29–38 makes that distinction explicit. The system nevertheless made a low-quality local failure authoritative enough to spend review allowance. Removing independent tests is a **trust-policy choice** (#326/#307), not authorization to accept agent-provided test claims.

### 3.2 Machine self-clear created both a paid loop and a non-releasable ceiling

```text
Agent structured blocker taxonomy (checks/publication)
  → harness-owned RecordedMachineBlock minted from paid admission provenance
  → same-head green hosted CI permits machine release
  → #1068's unanswered independent-test question cleared twice
  → $13.712487 redundant ADDRESS_REVIEW; third dispatch budget-refused
  → self-clear quota protects against repetition (ceiling 3)
  → stop #14: report flag + NHR, no exit (#1084/#993)
  → #298 inc3 generic typed cause/renewal (+480)
  → migration/projection repairs (+33, total +513)
  → secondary-only shell lacks readable primary snapshot (B09)
```

The subtractive `1abdccf` candidate removes the machine-release mint. To preserve old command/budget behavior it also added pending-command guarding and reservation-time refusal retirement. Fresh review found **both** created recovery dead ends (B10/B11). This is the pattern recurring inside a deletion fix: removing permission cannot erase the ability to reconcile old effects, and “successful reservation” cannot stand for “provider actually ran.”

### 3.3 NHR has multiple writers, and projection doubles as authority

```text
Counter ceilings, review escalation, CI escalation, operator parking
  → all write the same needs-human-review label
  → some remove ai-pr-open; others retain it
  → manual NHR removal cannot identify underlying owner/counter
  → #1074 approved+green remains invisible after removal
  → hand-restored ai-pr-open permits merge
  → typed release variants/fences required to retain unrelated holds
  → #298 inc2 shares 11 formatting planners (−21), ownership still separate
  → #298 inc3 adds generic renewal but label/link/remaining-gate defects arise
  → label transition missing from event fold leaves stale dashboard escalation
  → PR #339 adds label moves to receipt telemetry
```

The planner refactor did not unify authority. `/tmp/298-inc2-inventory.md:11–27` deliberately retains separate guarded durable writers, workflow projections, self-clear, legacy operators and imports. One formatting helper cannot solve several owners of an effective stop flag. Proposed #306 made SQLite sole authority; the later minimal direction instead derives workflow from GitHub and minimizes parallel local lifecycle state. Those are different architectural proposals, not one already-approved design.

### 3.4 Strict accounting became a workflow blocker, then a growing recovery subsystem

```text
Require exact native usage/receipt completeness
  → harmless duplicate/missing/partial evidence classified unpriceable (#247/#261/#277)
  → retain $50 maximum quote
  → repair purchase cannot acknowledge/continue (#240/#248)
  → return_authority_unavailable every tick (#262)
  → new episode-scoped refusal ledger + INFRA ceiling (#268)
  → ceiling no typed exit (#271)
  → allow acknowledgment while money retained (#269)
  → reduced daily headroom, budget false messages/self-clear loops (#258/#272)
  → best-guess terminal settlement (#301/#317)
  → late-return, partial/duplicate receipt, live-cap and estimate-origin fixes (B13–B27)
  → #298 migration 42 folds refusal debt; compatibility/migration edge defects
```

Money admission and process supervision are still valuable. The unnecessary coupling is “cannot price precisely” becoming “cannot progress or release a finished workflow.” #317 separates terminal estimate correction from live cap enforcement; no report should turn its $0 evidence-gap settlement into verified zero billed spend.

### 3.5 CI policy duplication led to conflict and stale-remediation deadlocks

```text
All-check gating + ignore/advisory inference + UNSTABLE handling
  → required-green PR waits on advisory flake (#1074)
  → free rerun also waits on paid capacity (#289)
  → required-check rewrite (#303/#319)
  → old durable run captures/latches now conflict with new policy
  → several pre-ship current-policy/replay/revision fixes (B28–B44)
  → shipped blanket requalification waits for checks before REBASE
  → conflict prevents checks, #1068 stalls 499 min
  → #338 earlier conflict check removes deadlock
```

The same head is not enough identity for an action against a particular CI run attempt. #282 remains the distinct unresolved class even after queued-only handling and required-policy corrections.

### 3.6 Cold-start recovery recreated context and changed provider

```text
Earlier short self-blocking sessions (Tauri #604)
  → every blocked RESUME forfeits session
  → wall cap is mapped into blocked, even after productive hour
  → retained session/runtime provenance absent
  → fresh-reply task bypasses issue tier route, uses Claude SDK default Opus
  → old prompt still defines reply boundary
  → agent progress comment qualifies as its own answer
  → #1102 second hour ($17.040507), no new operator instruction
  → terminal-only session capture + overwritten boilerplate handover
  → three new issues (#340–#342) plus transcript forensics
```

### 3.7 Enforcement mechanisms hid gaps in their replacements

The immutable-test/grant/HMAC chain prevented edits that red-fail did not know how to verify. Deleting it uncovered additive changes in existing test files, stale grant-only rejection reuse and skip-review paths without notices. #333 ultimately removed **2,021 src lines** while preserving reverted-implementation verification; #334 identifies the remaining `test_support` notice gap. The PR-body/visual-evidence chain produced many formatting/read-count denials; #331 replaced body denials with completion at publication and deleted screenshot receipt bookkeeping, but its new Markdown parser needed Setext/code/subsection fixes. Deletion reduced the surface; correct outcome-based checks still needed actual behavioral verification.

### 3.8 Management activity worsened the failures it was fixing

Parallel branches each ran full suites → tmpfs/full-/tmp errors → disk-backed retries and test-context failures → sustained host load → target timing failures → paid repair/rejection ceilings → more gap branches and suites. This feedback loop is **verified for overlap/storage** and **partly hypothesized for exact failure attribution**. “One full suite at a time” was insufficient while target gates and native CI were also heavy; the later quiet-host check accounted for those. An additional lock was deliberately deferred because it adds another mechanism.

## 4. Quantitative picture

### Code and state surface

Counts include blank/comment lines and tracked Python files, measured in the working checkout. They are not semantic LOC and exclude dashboard TypeScript, shell hooks and non-Python tests.

| Measure | Result | Method/limit |
| --- | ---: | --- |
| Python src LOC | **116,534** in **213 files** | `git ls-files 'src/*.py'` piped through `wc -l`. Git pathspec includes nested tracked files. |
| Python test LOC | **148,404** in **347 files** | Same method for `tests/*.py`. |
| Combined Python | **264,938** | Test/src ratio **1.27**. High volume does not establish outcome coverage; B12/B66 are counterexamples. |
| Stop inventory | **34 entries** | S:13–46. Includes duplicate checks, automatic fact-based holds, dead ceilings and proposed #272 headroom hold; not 34 independently reachable human terminal states. |
| Named label enum | **19** | `domain/labels.py:16–80`, including 3 priority labels. Nine in `LIFECYCLE_LABELS`, three advisory. Not a census of all target GitHub labels. |
| Numbered ADRs | **16** | 0001–0016, plus directory README. Some describe superseded mechanisms. |
| Src-touching commits since Sep 1 | **245** | Default current-HEAD ancestry: `git log --oneline --since=2026-09-01 -- src`. Excludes unmerged candidate branches. |
| Conventional `fix` subjects | **48/245 = 19.6%** | Exact subject prefix `fix:`/`fix(…`. Conservative reproducible lower-bound proxy, not full repair share. |
| Subjects literally naming fix/recovery/hardening | **64/245 = 26.1%** | Case-insensitive substring `fix`, `recover` or `hardening`. More direct requested churn proxy, still misses hardening titled as authority/verification. |
| Broad control/recovery/hardening subject proxy | **186/245 = 75.9%** | Case-insensitive match: fix, recover, hardening, guard, fence, fail-closed, refusal, hold, reconcile, retain, revalidate, verify, bounded, bound, audit, witness, receipt, provenance, attribute, settle, activation, archive, checkpoint, census, import, authority. Includes new foundational machinery, so **not** an audited “75.9% bug-fix” claim. |

The history is dominated by lifecycle authority, receipt verification, archive census, recovery and migration subjects. A strict functional classification would require reviewing every diff; the two explicit proxies above establish the scale without pretending commit names prove net product value. The manager's “a careful version might be 10–20k lines” (T:L19948) was a design opinion, not an empirical industry benchmark.

| Largest source modules | LOC |
| --- | ---: |
| `adapters/control_store.py` | 11,786 |
| `app/roles.py` | 3,847 |
| `app/cli.py` | 3,663 |
| `app/dashweb/snapshots.py` | 3,106 |
| `adapters/review_workflow_control.py` | 3,010 |
| `adapters/codex_runner.py` | 2,561 |
| `adapters/effects.py` | 2,499 |
| `domain/dashboard.py` | 2,442 |
| `adapters/github.py` | 2,440 |
| `domain/fix.py` | 2,100 |
| `domain/review_workflow_policy.py` | 2,040 |
| `app/workflows/fix.py` | 1,999 |

Largest Python tests: `tests/app/test_cli.py` 3,769; `tests/adapters/test_control_store.py` 3,731; `tests/app/test_dashweb_snapshots.py` 3,712; `tests/app/test_roles.py` 3,419; `tests/domain/test_fix.py` 3,198.

### Measured source deltas and mechanism changes

| Change | Net src | Mechanisms removed/added |
| --- | ---: | --- |
| Independent test/gate commit `0294b3e` | **+1,015** measured whole-commit | Adds independent execution/evidence and gate integration; quoted +841 subset unresolved. |
| #271 initial release fix | **+325** | New cause-specific gate/re-arm; rejected/held. |
| #272 initial headroom fix | **+89** | Added hold with no offset deletion; rejected/held. |
| #298 inc2 / PR #323 | **−21** | Eleven duplicate action constructions → one helper; authority mechanisms retained. |
| #303 / PR #319 | **−28** | Removes ignore/advisory inference and refresh behavior; required policy becomes governing input. |
| #327 / PR #331 | **−418** measured | 199 additions/617 deletions; removes visual/body policing, adds body completion. Earlier estimates “about −440” superseded. |
| #325 / PR #333 | **−2,021** measured | 116 additions/2,137 deletions; removes immutable-test/grants/HMAC/trailer hold chain. |
| #298 inc3 early slice | **+480** | Separate refusal ledger/dead enforcement/report path removed; generic typed allowance renewal/receipt variants added. |
| #298 inc3 at `f832acb` | **+513** measured | 1,003 additions/490 deletions; +33 review repair beyond initial slice. Stopped, not shipped. |
| #1068 `1abdccf` | **−66** measured | 35 additions/101 deletions; machine mint/predicates removed, guards/early budget retirement added. DO-NOT-SHIP. |
| Verdict view `454ff49` | **0** | 1 insertion/1 deletion; removes one synthetic projection edge. Local/cosmetic with recovery-discovery defect. |

Source lines do not substitute for a mechanism audit: a negative diff can add a dangerous guard (B10/B11), while preserving inert identity fields can be necessary until their proof consumers are retired (B01).

### Redundant spend, investigation spend and longer-window samples

| Amount | What it supports | Treatment |
| --- | --- | --- |
| **$13.712487** | #1068 second ADDRESS_REVIEW after unanswered question | Clearly redundant paid repair. U:16,23. |
| **$13.441554** | #1068 first repair blocked on load-sensitive unrelated operator test | Documented false-rejection-associated repair; not assumed wholly devoid of useful analysis. |
| **$0.84** | Lost unpublished review repurchased after cap restart | Identified redundant review. T:L7686/#273. |
| **$14.552487** | $13.712487 + $0.84 | Conservative identified redundant-leg subtotal; not full session waste. |
| **$27.154041** | Both charged repairs in livelock window | Includes first leg; do not add to its component rows. Third refusal $0. |
| **$19.874214** | #1102 resume A+B | 2.833707 + 17.040507 estimated; investigations partly useful, not all called waste. |
| **$8.12** | #997 FIX_CI on shared smoke trouble | Known flake-repair example; exact avoidable fraction unmeasured. |
| **$6.81 booked** | #1068 two earlier flaky-job rerun asks | Subject to cumulative Claude overcharge; not clean incremental dollars. |
| **~$27 overstatement** | Claude accounting audit since Oct 1 | Reporting error within ~$104 booked, not new provider spend. |
| **$50 each** | Tauri `9fdbdc47`, self `57b4f084` quoted exposure | Admission reservations, not charges; Tauri settled estimate at boot. |

No authoritative whole-session cost-per-merge, cash total, or net waste percentage can be computed from these incident excerpts without a normalized ledger audit. The read-only task did not inspect/mutate the live DB. Pricing includes provider dollars, frozen-rate charges and estimates; the known Claude overcharge makes unqualified totals misleading.

The October 1 onward audit at T:L14815 reports 572 starts/564 completions and ADDRESS_REVIEW 151 runs/~$199 versus RESOLVE 144/~$217, FIX_CI 43/~$63, RESUME+PR_RESUME+REBASE ~62/~$59. Router 164/~$6.50, 130 effort escalations, 11 reaching ceiling. These are **multi-repo, longer-window samples**, not this session's spend. They show repair cost comparable to initial implementation, but do not prove the repairs were unnecessary.

The operator-test failure audit estimated ~5 timing-sensitive and 2–3 shared-Cargo failures among 14 rejections across nine Tauri PRs, plus one missing dependency and ~6 unclassified signatures (T:L18970). Approximate buckets overlap/inconsistently sum; they are not a validated partition. The manager explicitly labelled idle-host causal attribution a hypothesis. Hosted Tauri CI runs on GitHub runners; local manager CPU cannot directly explain hosted runner slowness, though it can affect local operator/gate evidence and increase queued CI work.

## 5. Current state

### Harness and candidate branches

- **Daemons off by user order:** H:123–136 records tauri fix, review, steward and dashboard disabled/stopped, self/Ballast/Keifu already disabled, no issue-agent timers/processes, poll cron deleted. The native **system CI runner remains up**, distinct from harness admission. This report did not restart or change any service.
- **Primary dev HEAD:** `8251a37`; session changes merged to dev, handover says main not pushed. No claim the three candidate branches are deployed.
- **`fix/pr-unblock-livelock` at `1abdccf`:** −66 src; removes machine self-clear mint. Latest unseeded review **DO-NOT-SHIP**, B10/B11. Prior 1,034 focused tests and typechecks do not override its two failing adversarial reproductions (2 failed/2 passed; 345 repository tests passed). Full suite not run under manager's focused-test restriction.
- **`fix/test-failure-verdict-hold` at `454ff49`:** net src 0, cosmetic view correction; actual NHR stops remain. Fresh review **SHIP-WITH-CHANGES** for GET discovery. Proposed follow-up stopped as added surface. The implementation report says 340 focused cases had passing results across sandbox/non-sandbox runs; no full suite.
- **`refactor/298-inc3-allowances-fix` at `f832acb`:** initial five P1/P2 findings corrected, 1,983 focused tests and pyrefly passed; complete slice **+513 src** and **DO-NOT-SHIP** on secondary-only shell. Subsequent shell-state work stopped; no approved cutover or canary.
- **Read-only block inventory:** `/tmp/block-inventory-2026-10-12.md` **arrived during final verification and is incorporated** as source I. It identifies eight open labelled holds, #994's silent lifecycle exclusion, intentionally frozen intake and terminal/ineligible review noise. Its −20 to −350 class-size estimates overlap and are not measured implementation diffs. Its statement that the −66 livelock fix is “already fixed” does not override the candidate's DO-NOT-SHIP review.

### Tauri items, distinguished from stale handover statements

| Item | Last session cause/action | GitHub state at report read |
| --- | --- | --- |
| PR **#1068**, issue **#1056** | False operator rejection, conflict stall, machine-clear livelock, budget hold, planner/executor disagreement | **OPEN**, head `1791ab86…`; `ai-blocked`, `ai-pr-open`, NHR, budget review; dirty/conflicting. Earlier $130 lifetime budget had $46.28 left for $50 quote; user already raised it to $500. Inventory finds no remaining dispute, so current label mechanics are not a new budget judgment. |
| PR **#1084**, issue **#993** | Cargo race rejection released, then self-clear ceiling 3 with no typed exit | **OPEN**, `ai-blocked`, `ai-pr-open`, NHR; dirty. #1097 fixed target race; allowance exit not deployed. |
| PR **#999**, issue **#755** | Historical handoff loop, manual NHR; synthetic release choice misleading | **OPEN**, ai-pr-open + NHR; unchanged since Oct 7. Do not claim ordinary test selector fix resolves it. |
| PR **#1001**, issue **#938** | ENOSPC-cut native accounting/return-authority loop, manual NHR | **OPEN**, ai-pr-open + NHR; unchanged since Oct 7. Inventory establishes linkage and the initiating disk failure. |
| Issue **#994**, former PR **#1019** | Closed old PR/requeued; stale PR-open label excludes requested fresh resolve; rebase debt 3 | **OPEN**, ai-ready + ai-pr-open, no open PR. Inventory verifies contradictory-label eligibility and cleanup gap. |
| Issue **#1102** | Two resumes timed out; last progress is not a new operator answer | **OPEN**, ai-triage + ai-blocked, parked; target root cause/qualification unresolved. |
| Issues **#1055/#1081** | Intake frozen; both had waited on #1090 | **OPEN**, ai-triage, no ai-ready. #1090 merged later; user freeze still applies. |
| PR **#1074**, issue **#1059** | User-authorized NHR removal + manager restored ai-pr-open | **MERGED 20:52:21 AEDT Oct 12**. Not currently blocked. |
| PR **#1090** | Conflict rebase after #338 | **MERGED 20:57:33 AEDT Oct 12**. Not currently blocked. |
| PR **#997** | Additional spend judgment, hosted crash-diagnostic question | **MERGED 13:43:05 AEDT Oct 12**. Not currently blocked. |
| PR **#1097**, issue **#1093** | wry build fingerprint race fix, macOS test review, flaky smoke reruns | **MERGED 16:53:07 AEDT Oct 12**. Clean-week verification for harness #312 still outstanding. |
| Issues **#1104–#1107** | Target test robustness filed | **OPEN**, ai-triage; not promoted under freeze. |
| Issues **#1010/#710/#696** | Historical resource park/provider-overload/provider-quota stops examined by inventory | **OPEN/NHR**; #1010 also ai-parked. #710 has no current genuine decision shown; #696 may need Mac access/spend/qualification policy despite implementation merged. |
| PR **#1108**, CI trim | User-approved delegated task allowed to continue after daemon shutdown | **OPEN/MERGEABLE** on final GET; both platform smoke, launch, codecs and frontend-checks green; Chromium 1, WebKit 3/4 and rust still in progress. No merged timestamp. |

Harness **#340, #341, #342 are OPEN**, filed at 20:47:52, 20:48:55 and 21:01:06 AEDT respectively. Many issues with merged implementation PRs also remain OPEN (#301/#303/#325/#327/#328/#330, for example): issue status is not a synonym for code merge or field verification. This report makes no GitHub closure changes.

## 6. Lessons and recommendations

### Minimal reconciler direction

Start from the user-visible job: observe an issue/PR, run a bounded agent, publish, observe CI, obtain review and merge; track spend. The industry controller pattern repeatedly observes actual state and takes actions to bring it toward desired state. Kubernetes' [controller documentation](https://kubernetes.io/docs/concepts/architecture/controller/) describes that control-loop shape. Applying it here is a design recommendation, not a claim this harness needs Kubernetes or its entire authority machinery.

Keep pure domain decisions, process-group supervision, wall/spend bounds and admission/settlement. Derive current issue, PR, review, checks and merge state from GitHub each tick. Keep SQLite for what those remote facts cannot supply: money, leases/owned execution, and deduplication of actions/replies. Avoid maintaining a second complete workflow truth with its own counters, episodes, gates, imported histories and independently authoritative labels.

Make each action idempotent under its actual subject: a PR head for review/publication, a specific run **attempt** for CI cancellation, an issue/PR and run window for reply consumption. On uncertainty, read the remote outcome before repeating the action; that does not require replaying a separate lifecycle history for every target. Use bounded backoff for transient inability to observe/act. A transient retry ceiling should produce a concrete diagnostic; it should not silently invent a new human-judgment requirement.

Limit terminal controller outcomes to **merged**, **needs-human for a genuine decision**, and **budget**. “CI running,” “conflict needs rebase,” “dependency still open,” “temporary read unavailable,” and “money estimate pending correction” are derived nonterminal conditions. They should not each acquire a label writer, durable hold, bespoke re-arm and migration. Honor cancellation/closure before another publication or admission.

A single human-stop writer should state the decision needed in plain language and preserve the active work's discoverability. A budget decision is legitimate; removing a label, clearing a counter, reconstructing an ID and restoring a trigger are avoidable mechanics around it. A release should make the intended next observable action possible, not merely return 202 or clear an internal flag.

This direction still needs an explicit trust choice: current docs say SQLite is sole lifecycle authority and independent operator receipts authorize approval. #306's earlier “labels only projection” proposal differs from the later GitHub-derived minimal design. Resolve that choice in a short design before deleting current enforcement; do not combine both authorities during a long transitional period. Keep required CI and independent code review as outcomes, and decide specifically whether local ordinary tests add sufficient protection to justify their duplicate execution system.

### Candidate deletions, ranked by incident coverage

Counts below are conservative **distinct catalogue classes affected**, not dollars saved or a promise all underlying target defects disappear. Candidate sets overlap, so do not sum them. Ranking is a reasoned counterfactual from the chains above. Not all deletions are already authorized; trust-policy changes remain decisions.

| Rank | Delete/fold candidate | Classes it would remove or prevent | Estimated coverage and constraint |
| --- | --- | --- | --- |
| 1 | Parallel local lifecycle/re-arm/execution-history authority; derive ordinary progress from GitHub, retain only minimal money/lease/dedup ledger | Shell-reader/activation incompatibility, stranded returns, missing typed exits, label restoration, departed retries, refusal-ledger migration and many #298 projection/compatibility bugs | **At least 12** classes (A01,A03,A04,A06–A11,A26,B01–B09 families). Largest structural effect; retire proof consumers before identity fields, preserving money/owned active runs during cutover. |
| 2 | Always-run independent operator-test subsystem when declared required CI supplies equivalent coverage | Execution episodes/renewals, synthetic test holds, duplicate resource contention, infra failures counted as code review, release-discovery compatibility | **At least 8** classes (A09,A19 trigger,D01,D03–D07,E04). Does not fix flaky CI itself or real assertion defects. User trust decision #326 remains pending; compare coverage before deletion. |
| 3 | Machine self-clear taxonomy/mint/predicates and dedicated self-clear quota/report state | Unanswered-question loop, stop #14, restoration interactions, generic re-arm expansion caused by that stop | **At least 5** classes (A08,A19,B03,B09 family,B10). Most immediate deletion; first repair the old-attempt reconciliation flaw rather than shipping 1abdccf unchanged. Budget admission can wait on current headroom before recording a block. |
| 4 | Permanent unknown-cost workflow holds and transcript-baseline pricing authority; unify per-request best evidence + replaceable recovery estimates | Exact-usage freezes, retained quote/repair paralysis, spurious infra refusals, budget pressure, Claude baseline dead ends | **At least 5** classes (A04,A05,C01–C05 families). #317 already removes much; retain live cap enforcement, truthful estimate basis and correction finality. |
| 5 | Per-cause NHR writers/selection gates; one decision-owned human stop and observation-driven action selection | Invisible approved PR after unlabel, wrong owner clearing attention, stale board release, manual parking ambiguity | **At least 4** classes (A10,A11,B03/B04,E13). Sharing formatter in #323 alone is insufficient. |
| 6 | Test-grant/HMAC/immutable-test approval chain; PR body and screenshot receipt policing | Grant dead ends, copied obsolete rejection, form denials, screenshot bookkeeping and prompt repetition | **At least 4** classes (B58–B63 families,E24). Already largely deleted by #331/#333; keep actual red-fail, code review and visual outcome inspection. Do not reinstall removed controls to patch notice gaps. |
| 7 | Config duplicates, legacy fallback and deployment from mutable working HEAD | Stale models, cap-change repurchased review, restart loss, drop-in drift, dashboard churn | **At least 4** classes (C06,C08,E08–E10,E18). #322 only partial. Immutable release/snapshot ownership direction #304/#290; avoid another independent override layer. |
| 8 | Broad old CI-policy inference and per-head remediation special cases | Advisory failures blocking merge, queue cancellation, job-specific rerun asks, conflict waiting for impossible checks | **At least 4** classes (A12–A18 families). Use one current required-policy rule, actual run-attempt identity; no added “flake classifier” or retry counter per incident. |
| 9 | Paid-session CI polling and redundant prompt boilerplate | Hour spent waiting, opacity/reconstruction, repeated model input, manager monitoring pressure | **At least 3** classes (C10,E15,E24). Preserve useful task progress/transcript locator through existing result fields; #335 prompt trim already landed. |

### Next work, with the harness kept off

1. Use the completed read-only block inventory to map every current stop to **actual judgment**, **budget**, or **mechanical gap**. For #999/#1001, distinguish manual labels from historical `NoLinkedPr`/ENOSPC authority failures; for #1068, distinguish machine release from self-reply and planner/executor label disagreement. Include old #1010/#710/#696 holds and #994's silent exclusion, not just the newest PRs.
2. Resolve B10/B11 before considering the #1068 fix shippable. A negative LOC diff is insufficient evidence of reduced failure surface. Preserve old-command reconciliation and proven-unlaunched budget recovery without another counter/hold.
3. Write the minimal authority/trust design and a keep/delete/fold inventory before continuing the +513 allowance slice. #298's comprehensive typed release was a rational response to missing exits, but it recreates the machinery now under question.
4. Finish target-side robustness and CI-trim verification under the approved scope. #1102 needs native causal evidence, not a readiness tweak inferred from wrapped errors. #1107 requires reproduction; hosted flakes must not be attributed to local host contention without evidence.
5. Validate changes through real outcomes: fresh reply causes exactly one resumed leg; failed spawn leaves recoverable budget state; recovered PR is selectable and eventually merges; docs-only CI emits every required context; load tests preserve assertions. Retire tests whose sole contract is a deleted mechanism.
6. On a future user-authorized restart, keep intake frozen until blocked work clears; use quiet-host full validation and a small WIP set. Measure avoided repair spend, operator interventions, recurrence and merges per deployment day. Do not add a host lock, counter, flag or classifier merely because another stop recurs.

## 7. Appendix — issue and PR index

Status is GitHub-observed where fetched, otherwise explicitly historical/source-reported. “Implemented” does not close an OPEN issue or upgrade pending field evidence. Links use repository-qualified numbers to prevent harness/target collisions.

### Harness issues created since October 10, plus prerequisites cited

| Issue | One-line status |
| --- | --- |
| [#239](https://github.com/xnmp/autonomous-issue-agent/issues/239) | Historical pending-rebase starvation correction; field canary proved disjoint resolve could proceed, not stranded-repair recovery. |
| [#240](https://github.com/xnmp/autonomous-issue-agent/issues/240) | Terminal launched repair recovery implemented in #248; field recovery still pending in ledger. |
| [#241](https://github.com/xnmp/autonomous-issue-agent/issues/241) | Historical legacy run attribution correction; live conservation evidence pending. |
| [#244](https://github.com/xnmp/autonomous-issue-agent/issues/244) | CLOSED; retained review markers/full-root activation corrected, broader live proof pending. |
| [#245](https://github.com/xnmp/autonomous-issue-agent/issues/245) | OPEN; dashboard-child ownership fix merged as #332, live reinstall pending. |
| [#246](https://github.com/xnmp/autonomous-issue-agent/issues/246) | CLOSED; frozen operator test command reaches Codex governor, native publication verified. |
| [#247](https://github.com/xnmp/autonomous-issue-agent/issues/247) | OPEN; recurring native usage ambiguity, subsequently addressed in parts by accounting fixes. |
| [#249](https://github.com/xnmp/autonomous-issue-agent/issues/249) | CLOSED; native CI runner plus settlement timestamp/signal-handler corrections. |
| [#253](https://github.com/xnmp/autonomous-issue-agent/issues/253) | CLOSED; shell-linked issue hold read fixed by #256. |
| [#254](https://github.com/xnmp/autonomous-issue-agent/issues/254) | OPEN; held infrastructure-failed PR lacks free current-workflow retrigger. |
| [#255](https://github.com/xnmp/autonomous-issue-agent/issues/255) | CLOSED; pending background child block reclassified by #263, live verified. |
| [#257](https://github.com/xnmp/autonomous-issue-agent/issues/257) | CLOSED; shutdown test deadlines made clock-driven by #259. |
| [#258](https://github.com/xnmp/autonomous-issue-agent/issues/258) | OPEN; active-reservation contention misclassified as budget exhaustion. |
| [#260](https://github.com/xnmp/autonomous-issue-agent/issues/260) | CLOSED; hardened Ballast runner profile #266 installed/verified. |
| [#261](https://github.com/xnmp/autonomous-issue-agent/issues/261) | CLOSED; identical Codex usage re-emission fixed by #267. |
| [#262](https://github.com/xnmp/autonomous-issue-agent/issues/262) | CLOSED; refusal ceiling #268 merged; live trigger row pending, downstream exit gap persists. |
| [#264](https://github.com/xnmp/autonomous-issue-agent/issues/264) | OPEN; terminal-PR hold noise, part of class reduced by #308. |
| [#265](https://github.com/xnmp/autonomous-issue-agent/issues/265) | OPEN; doctor misses effective exit-87 restart override. |
| [#270](https://github.com/xnmp/autonomous-issue-agent/issues/270) | OPEN; #1019 SIGTERM sender unknown; no causal fix claimed. |
| [#271](https://github.com/xnmp/autonomous-issue-agent/issues/271) | OPEN; infra-ceiling no typed exit; +325 draft held, consolidation stopped. |
| [#272](https://github.com/xnmp/autonomous-issue-agent/issues/272) | OPEN; pre-admission refusal misreported as spent leg cap; +89 draft superseded. |
| [#273](https://github.com/xnmp/autonomous-issue-agent/issues/273) | OPEN; live cap changes without whole-trio restart. |
| [#274](https://github.com/xnmp/autonomous-issue-agent/issues/274) | CLOSED; failure-tail evidence via #275/#278. |
| [#277](https://github.com/xnmp/autonomous-issue-agent/issues/277) | OPEN; malformed middle rollout record poisons usage read. |
| [#279](https://github.com/xnmp/autonomous-issue-agent/issues/279) | OPEN; one free rerun per head rather than failing job. |
| [#280](https://github.com/xnmp/autonomous-issue-agent/issues/280) | OPEN; issue close does not cancel active owned leg. |
| [#282](https://github.com/xnmp/autonomous-issue-agent/issues/282) | OPEN; CI cancel capture/run-attempt and nonexecuting waiting-state defects. |
| [#287](https://github.com/xnmp/autonomous-issue-agent/issues/287) | OPEN; cumulative Claude dollars overcharged; baseline candidate rejected. |
| [#290](https://github.com/xnmp/autonomous-issue-agent/issues/290) | OPEN; one config source; legacy fallback part merged #322. |
| [#291](https://github.com/xnmp/autonomous-issue-agent/issues/291) | OPEN; remedy selection/paid admission separation, partial #289. |
| [#292](https://github.com/xnmp/autonomous-issue-agent/issues/292) | OPEN; unified per-leg Claude usage pricing proposal. |
| [#293](https://github.com/xnmp/autonomous-issue-agent/issues/293) | OPEN; graceful launch-snapshot reload proposal. |
| [#294](https://github.com/xnmp/autonomous-issue-agent/issues/294) | OPEN; dashboard reload decoupling; #311 only shortened outage. |
| [#295](https://github.com/xnmp/autonomous-issue-agent/issues/295) | OPEN; journal noise corrected by #308, field trigger pending. |
| [#296](https://github.com/xnmp/autonomous-issue-agent/issues/296) | OPEN; suppression rejected, stall explanation #318 merged/live verified. |
| [#297](https://github.com/xnmp/autonomous-issue-agent/issues/297) | OPEN; retired paths and stopped-deployment orphan money. |
| [#298](https://github.com/xnmp/autonomous-issue-agent/issues/298) | OPEN; inc1/2 merged, inc3 +513 candidate stopped/DO-NOT-SHIP. |
| [#299](https://github.com/xnmp/autonomous-issue-agent/issues/299) | OPEN; reviewer→fixer evidence via SQLite proposal, not implemented as separate channel. |
| [#300](https://github.com/xnmp/autonomous-issue-agent/issues/300) | OPEN; continuation-activation migration scaffolding retirement proposal. |
| [#301](https://github.com/xnmp/autonomous-issue-agent/issues/301) | OPEN; best-evidence settlement #317 merged, Tauri boot verified. |
| [#302](https://github.com/xnmp/autonomous-issue-agent/issues/302) | OPEN; one supervisor rather than N×3 units, proposal only. |
| [#303](https://github.com/xnmp/autonomous-issue-agent/issues/303) | OPEN; required-check gating #319 merged, conflict regression fixed #338. |
| [#304](https://github.com/xnmp/autonomous-issue-agent/issues/304) | OPEN; immutable daemon release proposal. |
| [#305](https://github.com/xnmp/autonomous-issue-agent/issues/305) | OPEN; one tier runtime/model/effort routing table proposal. |
| [#306](https://github.com/xnmp/autonomous-issue-agent/issues/306) | OPEN; earlier SQLite-only lifecycle authority proposal; differs from final minimal direction. |
| [#307](https://github.com/xnmp/autonomous-issue-agent/issues/307) | OPEN; investigate CI replacing independent operator tests. |
| [#312](https://github.com/xnmp/autonomous-issue-agent/issues/312) | OPEN; target Cargo race corrected #1097, clean-week criterion pending. |
| [#313](https://github.com/xnmp/autonomous-issue-agent/issues/313) | OPEN; red base repair charged to every PR. |
| [#315](https://github.com/xnmp/autonomous-issue-agent/issues/315) | OPEN; older feed observations overwrite newer world facts. |
| [#320](https://github.com/xnmp/autonomous-issue-agent/issues/320) | OPEN; ordinary-test refusal mislogged as review POST failure. |
| [#321](https://github.com/xnmp/autonomous-issue-agent/issues/321) | OPEN; departed reads retry under blocked/activation-required authority. |
| [#325](https://github.com/xnmp/autonomous-issue-agent/issues/325) | OPEN; approval-chain deletion #333 merged, live field test pending. |
| [#326](https://github.com/xnmp/autonomous-issue-agent/issues/326) | OPEN; required CI trust/local-test scope awaits user policy decision. |
| [#327](https://github.com/xnmp/autonomous-issue-agent/issues/327) | OPEN; form/evidence bookkeeping removal #331 merged. |
| [#328](https://github.com/xnmp/autonomous-issue-agent/issues/328) | OPEN; fixer prompt trim #335 merged; input-cost guardrail pending. |
| [#329](https://github.com/xnmp/autonomous-issue-agent/issues/329) | OPEN; infra breaker hides concrete causes and counts deterministic allowance refusals. |
| [#330](https://github.com/xnmp/autonomous-issue-agent/issues/330) | OPEN; issues board #336 merged, live follow-up pending. |
| [#334](https://github.com/xnmp/autonomous-issue-agent/issues/334) | OPEN; test-support assertion edits missing modified-test notice. |
| [#340](https://github.com/xnmp/autonomous-issue-agent/issues/340) | OPEN; timeout session/activity/handover visibility. |
| [#341](https://github.com/xnmp/autonomous-issue-agent/issues/341) | OPEN; forfeited RESUME bypasses tier route. |
| [#342](https://github.com/xnmp/autonomous-issue-agent/issues/342) | OPEN; agent's mid-leg comment consumed as human reply. |

### Harness merged PRs since October 10

Every row below is **MERGED** per GitHub GET; field status is separately qualified.

| PR | One-line outcome |
| --- | --- |
| [#248](https://github.com/xnmp/autonomous-issue-agent/pull/248) | Recover terminal launched repairs without normal returns; live recovery pending. |
| [#250](https://github.com/xnmp/autonomous-issue-agent/pull/250) | Native self-hosted CI; actual runner jobs verified. |
| [#251](https://github.com/xnmp/autonomous-issue-agent/pull/251) | Cgroup OOM/peak terminal evidence; successful hot reload verified. |
| [#252](https://github.com/xnmp/autonomous-issue-agent/pull/252) | Runner-install resume/TMPDIR documentation. |
| [#256](https://github.com/xnmp/autonomous-issue-agent/pull/256) | Shell-linked issue holds; error disappearance verified. |
| [#259](https://github.com/xnmp/autonomous-issue-agent/pull/259) | Clock-driven shutdown tests. |
| [#263](https://github.com/xnmp/autonomous-issue-agent/pull/263) | Pending background child becomes infra recovery; live verified. |
| [#266](https://github.com/xnmp/autonomous-issue-agent/pull/266) | Hardened second Ballast CI runner profile; installed/verified. |
| [#267](https://github.com/xnmp/autonomous-issue-agent/pull/267) | Idempotent Codex usage re-emission/remetering. |
| [#268](https://github.com/xnmp/autonomous-issue-agent/pull/268) | Bounded refusal ledger/infra ceiling; downstream release gap. |
| [#269](https://github.com/xnmp/autonomous-issue-agent/pull/269) | Repair proceeds with retained unknown money; terminal pricing later superseded by #317. |
| [#275](https://github.com/xnmp/autonomous-issue-agent/pull/275) | Operator test output tail; failure summary live verified. |
| [#276](https://github.com/xnmp/autonomous-issue-agent/pull/276) | Surface-area rule added to governing docs/manager workflow. |
| [#278](https://github.com/xnmp/autonomous-issue-agent/pull/278) | Larger bounded evidence tail; compiler-output live canary pending. |
| [#281](https://github.com/xnmp/autonomous-issue-agent/pull/281) | Same-head test-only rejection release; #1075 re-review/merge verified. |
| [#283](https://github.com/xnmp/autonomous-issue-agent/pull/283) | Queued CI not timed as executing; live saturation canary pending. |
| [#284](https://github.com/xnmp/autonomous-issue-agent/pull/284) | In-window Codex receipt pricing, interrupted request zero. |
| [#285](https://github.com/xnmp/autonomous-issue-agent/pull/285) | Trivial routing for existing-pattern changes; live route sample pending. |
| [#286](https://github.com/xnmp/autonomous-issue-agent/pull/286) | Dashboard restart/unreachable handling replaces raw JSON errors. |
| [#288](https://github.com/xnmp/autonomous-issue-agent/pull/288) | Readable review evidence/internal-ID cleanup; body verified, fixer text pending. |
| [#289](https://github.com/xnmp/autonomous-issue-agent/pull/289) | Free CI rerun independent of paid capacity; saturated live case pending. |
| [#308](https://github.com/xnmp/autonomous-issue-agent/pull/308) | Absent-ref/terminal-target noise reduced; live observation pending. |
| [#309](https://github.com/xnmp/autonomous-issue-agent/pull/309) | Native cap clean-exit test synchronization. |
| [#310](https://github.com/xnmp/autonomous-issue-agent/pull/310) | Owned rollout discovery/HOME isolation; ten-leg live sample pending. |
| [#311](https://github.com/xnmp/autonomous-issue-agent/pull/311) | Dashboard RestartSec 3 and reviewer default refresh; next restart proof pending. |
| [#314](https://github.com/xnmp/autonomous-issue-agent/pull/314) | Unified typed release transport, compatibility corrected before merge. |
| [#316](https://github.com/xnmp/autonomous-issue-agent/pull/316) | Current-head CI-running indicator after repeated projection reviews. |
| [#317](https://github.com/xnmp/autonomous-issue-agent/pull/317) | Best evidence terminal settlement/schema 41; stuck Tauri reservation boot-settled. |
| [#318](https://github.com/xnmp/autonomous-issue-agent/pull/318) | Explains deliberate deferrals in STALL lines; live verified. |
| [#319](https://github.com/xnmp/autonomous-issue-agent/pull/319) | Required-check CI gate; rebase deadlock corrected by #338. |
| [#322](https://github.com/xnmp/autonomous-issue-agent/pull/322) | Legacy agent.env fallback removed; actual unset-env probe verified. |
| [#323](https://github.com/xnmp/autonomous-issue-agent/pull/323) | Shared attention planner −21 src; live mutation proof pending. |
| [#324](https://github.com/xnmp/autonomous-issue-agent/pull/324) | Catppuccin Latte theme; served bundle/screenshot verified. |
| [#331](https://github.com/xnmp/autonomous-issue-agent/pull/331) | Complete PR bodies, delete visual/form receipts; −418 src. |
| [#332](https://github.com/xnmp/autonomous-issue-agent/pull/332) | Dashboard cgroup listener ownership; live reinstall pending. |
| [#333](https://github.com/xnmp/autonomous-issue-agent/pull/333) | Test grant chain deletion; −2,021 src, outcome safeguards retained. |
| [#335](https://github.com/xnmp/autonomous-issue-agent/pull/335) | Prompt trim 2,465→1,442 words (41.5% reduction for measured trimmed baseline). |
| [#336](https://github.com/xnmp/autonomous-issue-agent/pull/336) | Needs you/Working/Blocked board, requested delegated implementation. |
| [#337](https://github.com/xnmp/autonomous-issue-agent/pull/337) | Split heavy PR queries; first `gh_ok:true`, longer observation pending. |
| [#338](https://github.com/xnmp/autonomous-issue-agent/pull/338) | Conflict rebase before impossible CI evidence; dispatch live verified. |
| [#339](https://github.com/xnmp/autonomous-issue-agent/pull/339) | Durable relabel telemetry carries label moves; next release proof pending. |

### Tauri issues and PRs mentioned

| Item | One-line status |
| --- | --- |
| [Issue #604](https://github.com/xnmp/tauri-explorer/issues/604) | Historical short self-blocking incident that motivated blanket RESUME session forfeiture; current GitHub status not fetched. |
| [Issue #755](https://github.com/xnmp/tauri-explorer/issues/755) / [PR #999](https://github.com/xnmp/tauri-explorer/pull/999) | About command; PR OPEN/manual NHR, old handoff loop; #755 route cost questioned. |
| [Issue #993](https://github.com/xnmp/tauri-explorer/issues/993) / [PR #1084](https://github.com/xnmp/tauri-explorer/pull/1084) | Issue/PR OPEN; popup position work blocked by machine self-clear ceiling/NHR/conflict. |
| [Issue #994](https://github.com/xnmp/tauri-explorer/issues/994) / [PR #1019](https://github.com/xnmp/tauri-explorer/pull/1019) | Issue OPEN/requeued; PR closed by user choice, old infra/rebase debt unresolved in sources. |
| [Issue #995](https://github.com/xnmp/tauri-explorer/issues/995) / [PR #1065](https://github.com/xnmp/tauri-explorer/pull/1065) | Video length Details column; PR MERGED 2026-10-11 23:16:31Z; earlier queued escalation/tool denial and a real branch smoke regression. |
| [PR #1001](https://github.com/xnmp/tauri-explorer/pull/1001) | OPEN/manual NHR; config watch retarget work, historical accounting/return-authority loop. |
| [Issue #1021](https://github.com/xnmp/tauri-explorer/issues/1021) | Closed not-planned at user request; active RESUME initially continued (#280). |
| [Issue #1049](https://github.com/xnmp/tauri-explorer/issues/1049) | Historical budget-refusal/self-clear route loop; session summary says later closed duplicate. |
| [Issue #1055](https://github.com/xnmp/tauri-explorer/issues/1055) | OPEN; intake-frozen, ai-ready removed. |
| [Issue #1056](https://github.com/xnmp/tauri-explorer/issues/1056) / [PR #1068](https://github.com/xnmp/tauri-explorer/pull/1068) | OPEN; startup crash notice work, paid unblock loop/current human+budget holds. |
| [Issue #1058](https://github.com/xnmp/tauri-explorer/issues/1058) / [PR #1070](https://github.com/xnmp/tauri-explorer/pull/1070) | Video fullscreen work; PR MERGED, background-child classification live canary. |
| [Issue #1059](https://github.com/xnmp/tauri-explorer/issues/1059) / [PR #1074](https://github.com/xnmp/tauri-explorer/pull/1074) | Video attachments; PR MERGED 2026-10-12 09:52:21Z after label restoration. |
| [PR #1075](https://github.com/xnmp/tauri-explorer/pull/1075) | MERGED 2026-10-11 11:22:50Z; preview tile alignment, same-head release canary. |
| [Issue #1081](https://github.com/xnmp/tauri-explorer/issues/1081) | OPEN; trunk-aware git-graph classification, intake-frozen, ai-ready removed. |
| [PR #1088](https://github.com/xnmp/tauri-explorer/pull/1088) | MERGED 2026-10-11 23:47:31Z; git-graph PR actions, operator-test flakes/dependency failure and releases. |
| [PR #1090](https://github.com/xnmp/tauri-explorer/pull/1090) | MERGED 2026-10-12 09:57:33Z; merge-base feature, #338 rebase dispatch. |
| [Issue #1093](https://github.com/xnmp/tauri-explorer/issues/1093) / [PR #1097](https://github.com/xnmp/tauri-explorer/pull/1097) | Vendored wry fingerprint fix; PR MERGED, one-week harness verification outstanding. |
| [PR #997](https://github.com/xnmp/tauri-explorer/pull/997) | MERGED 2026-10-12 02:43:05Z; tooltip shortcuts, budget and native smoke repair/diagnostics. |
| [Issue #1102](https://github.com/xnmp/tauri-explorer/issues/1102) | OPEN/ai-blocked; Linux smoke renderer loss, diagnostic work but no proven native root fix. |
| [Issue #1104](https://github.com/xnmp/tauri-explorer/issues/1104) | OPEN/ai-triage; Vitest correctness-test hang guards. |
| [Issue #1105](https://github.com/xnmp/tauri-explorer/issues/1105) | OPEN/ai-triage; git-graph complexity test replace 150ms absolute budget. |
| [Issue #1106](https://github.com/xnmp/tauri-explorer/issues/1106) | OPEN/ai-triage; watch acknowledgment timeout/shared mutex poison. |
| [Issue #1107](https://github.com/xnmp/tauri-explorer/issues/1107) | OPEN/ai-triage; reproduce/fix PTY cwd missing-output failure. |
| [PR #1108](https://github.com/xnmp/tauri-explorer/pull/1108) | OPEN/MERGEABLE on final GET; rust/WebKit/one Chromium shard still running, caching item skipped by author. |
| [Issue #938](https://github.com/xnmp/tauri-explorer/issues/938) | OPEN/ai-pr-open; linked #1001, ENOSPC-cut accounting identified by inventory. |
| [Issue #1010](https://github.com/xnmp/tauri-explorer/issues/1010) | OPEN/NHR/ai-parked; explicit historical OOM disposition, no product dispute established. |
| [Issue #710](https://github.com/xnmp/tauri-explorer/issues/710) | OPEN/NHR; three provider-overload returns incorrectly consumed resolve allowance and survive migration. |
| [Issue #696](https://github.com/xnmp/tauri-explorer/issues/696) / [PR #738](https://github.com/xnmp/tauri-explorer/pull/738) | Issue OPEN/NHR; quota-error debt imported; implementation MERGED per inventory, Mac qualification/access may remain. |
| [PR #679](https://github.com/xnmp/tauri-explorer/pull/679) | MERGED per inventory; terminal review repeatedly reads blocked historical execution authority. |
| [PR #725](https://github.com/xnmp/tauri-explorer/pull/725) | MERGED per inventory; same irrelevant terminal execution read/noise. |
| [PR #727](https://github.com/xnmp/tauri-explorer/pull/727) | CLOSED per inventory; terminal truth consulted after blocked counter read. |
| [PR #987](https://github.com/xnmp/tauri-explorer/pull/987) | OPEN Dependabot, adoption disabled; inferred source of anonymous departed authority failures, not review owed. |

### Other repository items

| Item | One-line status |
| --- | --- |
| [Ballast #67](https://github.com/xnmp/ballast/issues/67) | Bootstrap prerequisite prevented harness from fixing its own target prerequisite; user requested outside-harness work; current status not fetched. |
| [Ballast #62](https://github.com/xnmp/ballast/issues/62) / [PR #63](https://github.com/xnmp/ballast/pull/63) | ADDRESS_REVIEW hit shared global reservation contention in #258; final target status not fetched. |

### Source artifact index

Read and used: H; working diff and dated F rows; S; ADR11; CLAUDE.md; M; transcript actual user messages and compactions (T:L3576,5665,7686,9424,11161,13354,15305,17232,19148); U; V; X; `/tmp/issue-1102-findings.md`; `/tmp/298-inc2-inventory.md`; `/tmp/298-inc2-handover.md`; all six `/tmp/issue298-inc3-*.md` reports (original/current/stop, final, fix, review-fix); `P/ci-analysis-summary.md`; `P/unblock-review-output.txt`; `P/r298c-review-findings.md`; task/review briefs matching `P/codex-*.md` searched for scope, findings, net source and disposition. The stop reports overlap and describe different branch stages; they are not six separate incidents. The dated October 11/12 field rows are uncommitted additions: `git show HEAD:docs/field-verification.md` had no matching October 11/12 date row. “Merged code” and “committed evidence row” therefore remain separate claims.

The completed `/tmp/block-inventory-2026-10-12.md` was read in full after its arrival and incorporated as I. It supplied the additional held-item roots and current-state corrections above; the briefing `P/codex-inventory.md` alone was not treated as findings. No report task read or changed the live database: this report cites the inventory's prior read-only forensic queries.
