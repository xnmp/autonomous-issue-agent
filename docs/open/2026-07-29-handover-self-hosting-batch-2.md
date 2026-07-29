# Handover — self-hosting batch 2: 5/5 merged in 2h23m, phantom-$153 mystery solved (2026-07-29)

Second deployment against itself, run end-to-end by an autonomous manager
session (launch → babysit on the 15-min cadence → unstick → readout). All five
issues merged to `dev`; **dev is NOT yet promoted to main** — that and the
other loose ends are in §5.

## 1. Batch result

| issue | PR | path |
|---|---|---|
| #44 overlap-serializer predicted paths | #74 | 1 review round (terra); merged 09:24Z, ~52 min issue→merge |
| #62 escalation ceiling at the difficult end | #76 | 1 review round (terra); merged 09:50Z |
| #64 durable lifecycle writes | #77 | routed sol; 2 review rounds + 1 REBASE (after #74 moved dev); merged 10:10Z |
| #73 dashboard net-open/recurrence panel | #78 | 1 review round (terra); first address leg died to the #79 crash, retry clean; merged 10:19Z |
| #75 codex telemetry blindness (filed mid-batch) | #80 | **1st-pass APPROVE** (sol/high, 3rd attempt after two phantom kills); merged 10:55Z |

Batch surgery: #71 was swapped out pre-dispatch for #75 once #75 proved to be
its root cause (no sid → nothing to resume); #71 re-arms next batch on top of
#80's discovery mechanism. #67 got already-fixed-by-#63 evidence commented;
#46 evidence stands from batch 1 — both still need an operator close.

**Numbers**: wall clock 08:31Z launch → 10:55Z last merge = **2h23m for 5
merges**, zero human interventions beyond the manager session itself. Real
claude dollars (review side): $9.43 REVIEW + $8.10 RE_REVIEW = **$17.52 ≈
$3.50/merge**. Codex-side dollars for this batch are **garbage** (see §2): $0
on real legs, $153.00 phantoms on #75's — read this batch's codex ledger rows
as noise, not a token meter. Repair legs: 5 ADDRESS_REVIEW + 1 REBASE + 0
FIX_CI. **First-pass approval 1/5** (batch 1: 4/4) — flag, not alarm: four of
five PRs were harness-internal behavior changes and the reviewer's objections
were substantive each time; watch the next batch before reading it as a
guardrail move.

**Net open**: opened 2 (#75, #79) − closed 5 = **−3** (−5 once the operator
closes #46/#67). Recurrence: 0.

## 2. THE story — codex telemetry was never real, and $153 was never spent

Three findings that are one finding:

1. **codex-cli 0.146.0 prints no `session id:` header.** `_session_id_from_lines`
   returned "" on every leg (0 matches in the whole deployment log), so rollout
   lookup, usage, pricing, ledger rows, the #29 budget enforcement AND resume
   were all silently dead — while startup asserted "enforcement ACTIVE".
   #44's real 619s resolve journaled `usage=null cost_usd=0.0`. Filed as #75.
2. **`parse_codex_usage(stderr)` prices ECHOED text.** It scans the whole
   transcript, so when #75's own fixer worked on the budget-test fixtures
   (`_MODERN_USAGE_LINE`), the parser matched the echoed fixture and billed the
   leg **exactly $153.00 — twice, once at 81 seconds**. `budget_exhausted`
   kills, phantom $306, issue-cap block, `needs-human-review`. CONFIRMED
   post-batch with the arithmetic: the fixture at
   `tests/adapters/test_codex_budget_enforcement.py:244` echoes
   `output=5,100,000`, and 5.1M x sol's $30/MTok = **$153.00 exactly** (its
   `input=5M/output=100k` sibling prices to the tests' $28.00). Batch 1's
   "$153 sol leg" (the #29 demo, `tokens_in: 0` in its ledger row) was the
   same echo — the #29 fixer WROTE these fixtures — i.e. **that $153 was
   never real either**; all four $153 rows in spend.jsonl are the one bug.
3. **The unstick that worked**: evidence + fix requirements commented on #75
   (rollout `session_meta` as primary source, footer-anchored fallback only,
   plausibility guard, echo regression test), `CODEX_ENFORCE_BUDGET=0` +
   raised issue cap as a named-executioner workaround in self.env, relabel
   `ai-ready`. The escalation ladder (already at notches=2 from the two
   failures) redispatched sol/high and the fix merged first-pass as PR #80.
   Workaround retired same-day post-merge; overlay now carries only a note.

Ledger hygiene: three $153.00 rows sit against #75 in `spend.jsonl`
(10:10/10:19/10:46Z). They are fixture echoes — exclude from any spend
analysis.

## 3. Verifications this batch (ledger updated)

- **#16 escalation ladder: VERIFIED** — first firings ever after 8
  deployments: #64 ADDRESS_REVIEW (review_rounds=2, effort medium→high) and
  #75 RESOLVE retries (notches 1 then 2). Evidence commented on #41. All
  firings came from pre-existing #16 code (the daemons never reloaded
  mid-batch); #62's ceiling change is live only after promotion.
- **#61: VERIFIED** — gate probe green on plain `make test`; the `uv sync`
  prefix is gone from self.env permanently.
- **#63 defenses: held** — second self-hosted launch, zero recursion.
- **#29 kill path: MISBEHAVED** — fires mechanically, measurement spoofable
  (§2); superseded by #75/#80, live confirmation pending.
- **#44's own feature** (predicted-path serialization) merged but its live
  evidence (a hold event) waits for a future overlapping batch.

## 4. New filed backlog

- **#79** — stderr `readline()` dies on >64KB lines (`Separator is not
  found`); killed #73's first address leg, retry succeeded. Class fix
  spelled out in the issue. Good next-batch candidate.
- **#71** (re-arm), **#69** (unblocked now that #64 merged) — with #79 that's
  three strong candidates; #15/#24/#22 remain from the standing backlog.

## 5. Loose ends for the operator

1. **Promote dev → main** (5 squash merges + this docs commit) and pull into
   the main checkout before any relaunch — the daemons ran pre-#80 code all
   batch, so codex telemetry stays broken until promotion.
2. **First post-promotion codex leg is the #75/#80 verification**: expect a
   plausible nonzero ledgered cost and a real `session_id`. Ledger row waiting.
3. **Close #46 and #67** (evidence commented on both; label surgery reserved).
4. **Daemons are DOWN** (stopped clean at 10:57Z after the batch; queue
   empty). Relaunch = `make point-at REPO=self`; target clone was hard-reset
   post-history-rewrite and is clean at origin/dev.
5. The batch-1 loose end "populate CODEX_ENFORCE_BUDGET" was done, then
   revealed as insufficient (§2) — treat enforcement as unverified until the
   post-promotion leg confirms real usage numbers.
6. CLAUDE.md gotcha update candidate: raw `gh api` label add/remove and
   PR-close all work from assistant sessions; only `gh issue edit`/`close`
   wrappers are blocked. The "label surgery belongs to the daemons" line is
   now fully stale for removals AND additions.
