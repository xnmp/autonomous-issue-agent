# Manager handover — tauri-explorer 8-issue batch, 2026-08-08

Supervising session: interactive (Fable), launch 01:27Z, batch drained ~05:40Z.
Target: xnmp/tauri-explorer, base `dev`. Fixer codex/gpt-5.6-sol-config →
subscription default terra observed on legs; reviewer codex/terra; steward
codex. Dashboard served 127.0.0.1:8321 throughout.

## Outcome

**10 merged / 10 attempted** (8-issue batch + 2 re-armed leftovers); the last
(#581) landed ~06:00Z after the operator BOM fix cleared its Windows-only CI
failure.

| Issue | PR | Result |
|---|---|---|
| #602 markdown colours | 612 | merged 02:xx, 2 rounds |
| #605 config autoreload | 613 | merged 05:0x after ceiling → scope-note re-arm |
| #611 vitest teardown flake (filed at launch) | 614 | merged 05:1x after two ceilings → scoped re-arms |
| #600 ctrl+p lag | 615 | merged |
| #603 recycle bin sidebar | 616 | merged |
| #606 copy path menu | 617 | merged |
| #608 terminal keybinding | 618 | merged |
| #609 inbox git misdetection | 619 | merged |
| #495 preview fit (leftover, re-armed) | 579 | merged 01:26 at boot |
| #508 windows install (leftover, re-armed) | 581 | merged 06:0x after operator BOM fix 7ca69d44; issue 508 closed ai-done |

Spend (synthetic units, from events-v2 accounting): **$49.41 total, $5.49 per
merged PR** (9). Mix: fix 78.7% / review 21.3%; ADDRESS_REVIEW $18.21 (36.9%),
RESOLVE $10.22, RE_REVIEW $8.63 (40 legs), FIX_CI $5.29, REBASE $4.70, REVIEW
$1.87, PR_RESUME $0.31, ROUTE $0.17. First-pass approval **2/9 (22%)** —
guardrail note: down vs prior batches; the two wedged PRs (613/614) burned 7
and 11 review rounds. Issue-to-merge wall: 48–222 min. Escalation ladder fired
14× (all effort medium→high); review skips by patch-id 4.

Net issues on the harness repo: +3 filed (#176, #177, #178), 0 closed.
Recurrence: none observed (no reopened/refiled defects from today's merges).

## Harness gaps found (and disposition)

1. **Stale install blocks launch** — doctor error: `issue-agent-publish` not on
   PATH. Fixed on the spot with `make install`. Doctor behaved correctly.
2. **`GATE_TEST_FAILURE_EXIT_CODES` unset for cargo (would deadlock Rust
   seams)** — cargo test fails with exit 101; default trusts only `1`. Fixed
   pre-launch in the overlay: `GATE_TEST_FAILURE_EXIT_CODES="1,101"`. Both
   parsers accept comma lists. Two Rust-seam merges (#619, part of #613)
   happened under it.
3. **Reviewer blind to operator scope; rejection loops to ceiling**
   (PRs 613/614: reviewer demanded live-native-app evidence unproducible on
   this box; operator PR comments never reach review prompts). **Fixed in
   harness mid-batch (user-directed): commit 17d20a2** — the dashboard unblock
   now carries an optional operator guidance note (absent=keep, ""=clear,
   text=set; 4000-char ceiling; `<<<` fence-marker ban; UTF-8 pinned store I/O)
   persisted per-PR and injected into later review prompts as harness-owned
   instruction. Adversarially reviewed (13 findings, all applied or explicitly
   accepted); `make test`/`make typecheck` green. Verified live same day: both
   wedged PRs converged after scoped re-arms. API-only lever for now (SPA
   unchanged); MULTI-mode gate+dimension legs carry the note, validators
   deliberately not.
4. **Review legs race fixer pushes** — 4 `submit_review` permanent failures
   "review head changed before submit" (~$2-4 each wasted). Fence is correct;
   dispatch-time coordination is the gap. Filed **#176**.
5. **Free CI rerun lookup broken** — `ci_rerun` with `run_ids=[]`,
   `rerun_checks` dies "GitHub did not confirm the requested remote state" and
   re-fires across ticks (no ceiling). Filed **#177**; ledger row #38 marked
   MISBEHAVED.
6. **Publication test-file gate blocked a net-compliant diff** (PR 614,
   2 modified existing test files, under the ≤3 ceiling; push later drained on
   its own). Transient/stale-range suspicion. Filed **#178**.
7. **Dashboard under the assistant sandbox cannot write to GitHub** (h2
   "stream CANCEL" on every gh write from the spawned server). Operational
   note, not a harness defect: run `issue-agent dashboard` outside sandboxed
   shells.
8. **`RE_REVIEW` dispatch "conflicting durable effect receipt"** (PR 618,
   permanent after 4 attempts) — did NOT wedge the PR (later reviews
   proceeded); left unfiled as a one-off, watch for recurrence.

## Operator actions (unblocks / interventions)

- Re-armed leftovers #579/#581 pre-batch by raw-API label swap (before the
  dashboard path was in play). #579 merged at boot; #581 entered FIX_CI.
- Answered blocked-leg clarifications on PR 613 (scoped e2e evidence to
  operator post-verification) — fixer resumed (#50/#55 paths verified).
- Human review at both circuit-breaker escalations: dispatched independent
  adversarial reviews (opus), which found real defects the harness reviewer's
  stated reasons missed (613: same-writer queued-write race in persisted.ts;
  614: per-file fix not class fix — 3/22 runs still red; later tip 20/20 red
  from a drain-hook deadlock vs fake timers). Posted concrete operator
  findings on both PRs; scoped re-arms with the new guidance notes.
- Three dashboard unblocks (issues 611×2, 605, 508) — rearm/reviewer-identity
  rows VERIFIED.
- Diagnosed and hand-fixed PR 581's `invocation` failure: BOM-less UTF-8
  `windows_install.ps1` parsed as ANSI by Windows PowerShell 5.1 → em-dash
  mojibake → assertion diff. Operator commit 7ca69d44 (BOM prepend only) after
  the fixer's CI-retry ceiling exhausted; check went green.
- Cleared stale `needs-human-review` on merged PR 614; restored dirty evidence
  PNGs in the target checkout pre-launch; filed target issue #611 for the gate
  probe flake (fixed by the batch itself via PR 614).

## Loose ends for the operator

- **PR 581 / issue 508**: confirm auto-merge landed (was green+approved with
  webkit/frontend in flight at handover). If merged, issue 508 should close
  ai-done.
- **Stale worktree/branch `fix/482-*`** in the target checkout (prior
  abandoned attempt, has a commit, no PR) plus ~10 dead `fix/*` local branches
  from old batches — GC when convenient.
- **tauri dev not promoted to main** — today's 9 merges live on target `dev`.
- **Reviewer runtime = codex/terra same family as fixer** (shared.env
  `FIXER_RUNTIME` default now covers review): decorrelation between author and
  reviewer models is gone. Worth an explicit decision; the wedges today were
  reviewer-behavior driven.
- Harness `dev` is 4 commits ahead of origin (17d20a2 feature, 25d8533 +
  ledger/handover docs) — unpushed, per usual practice.
- User standing instruction honored but never triggered: switch fixer to
  Claude if codex subscription limits bite (no rate-limit signature appeared).

## Ledger

Verdicts ticked in docs/field-verification.md (same commit as this handover):
VERIFIED — #16 ladder, #95/#96 deferrals, #55, #50, #56, #33 doctor pin,
rearm trio (9e70231/cf47719/d92fe6c), dashboard rows, scope-note row (new).
MISBEHAVED — #38 free rerun (→#177). Still pending: #62 ceiling, #79 >64KB
line, #29 budget wall, #54, #39, #57.
