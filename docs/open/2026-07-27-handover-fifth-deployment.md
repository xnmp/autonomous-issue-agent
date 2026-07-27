# Handover — fifth deployment session (five-issue batch, codex/sol, first repair-leg day)

Written 2026-07-27 at the end of the babysit session. Outcome: **5/5 merged**
(tauri-explorer #519→PR550, #507→PR549, #529→PR551, #545→PR552, #547→PR553);
lifetime scoreboard 18. First deployment to exercise every repair-leg type
(fix-ci, address-review, re-review) and the first on `gpt-5.6-sol`
(verified end-to-end: resolves, repairs, approvals; auth = chonw89@gmail.com
plus plan — the id_token email claim is still the check that matters).

## Where things stand

- **Daemons: RUNNING** against xnmp/tauri-explorer, foreground `make point-at`
  in a background shell. Local main = `be49b1d` (merge of PR #18). Queue idle.
- **Caps (`config/.active.env`): issue $250 / daily $700** — raised three
  times mid-batch because sol's synthetic meter runs ~2.5–3× the Claude
  pricing the caps were sized for. Deliberate, documented in-file, but WRONG
  UNIT — see #22 before the next deployment day; consider resetting to taste.
- **Routing is un-pinned** (FIX_MODEL absent) and — post-#12 — absence now
  actually reaches the daemons. Effort defaults to medium
  (`CODEX_REASONING_EFFORT`, c995e55).

## What landed on the harness TODAY (none field-tested beyond first contact)

1. **Codex accounting chain**: 066e570 (rollout-split usage after codex-cli
   0.145 broke stderr parsing) + 27565d9 (per-leg deltas on resumed
   sessions). Ledger is truthful from ~08:15Z onward; earlier entries carry
   phantoms (#529 fix-ci +$31, #545 address +$37) — exclude when computing
   today's $/merge.
2. **Evidence-image commit gate** (2bcb99d): >700KB staged/evidence images
   block at `git commit` with an ffmpeg remedy. `EVIDENCE_MAX_IMAGE_KB`.
   Never fired live yet (landed after the incident it prevents).
3. **Review resilience** (2c187c0): 16MB SDK buffer; "exceeded maximum buffer
   size" deaths saturate the retry counter and escalate immediately.
4. **Env-file authority** (7f0fbf6, #12): knob REMOVALS apply on reload.
   Residual: `_load_agent_env` still stale-merges non-config env
   (TEST_CMD etc.) — commented on #12.
5. **Escalation ladder** (#16, PR #18, merged be49b1d): recurrence/retries/
   review-rounds escalate effort→tier, journaled as `route_escalation`.
   ACTIVE (`escalate_on_evidence=True`) and has NEVER seen a live dispatch —
   first-contact item #1 for the next batch.

## What the batch surfaced (filed, open)

- #17 spend-cap escalation comments not deduped (3 identical in 90s on PR552).
- #20 repair legs re-run full evidence capture ($146/28.9M-token leg on the
  TRIVIAL issue — the single biggest cost item of the day).
- #21 follow-up legs dispatch unrouted at CODEX_MODEL (sol) regardless of tier.
- #22 caps should be token-denominated on subscription runtimes (3 manual
  raises today = the babysitting loop to kill; prereq for #11).
- #23 re-review of an already-reviewed head SHA (PR553 was double-reviewed
  after a spend-block retry — redundant $1.42 + artificial round inflation,
  which now also feeds #16's signals).
- #24 serial fixer: ~25 min of approved PRs queued behind an unrelated
  resolve. Sequenced LAST — measure #20–#23 first.
- Also open from this session: #11 babysitter role, #14 gardener, #15 issue
  grooming; #1 webhooks predates it.

## Next-deployment watch list (in firing order)

1. **Escalation first contact**: a REQUEST_CHANGES round or recurrence should
   produce a `route_escalation` journal event and a stronger follow-up leg.
   Verify the event names the leg and only what actually ran.
2. **First-pass approval vs medium effort**: today was 2/5 first-pass at
   effort=low (nobody had pinned it; codex exec defaults LOW). The
   medium-effort + ladder combo is the live experiment against that number.
   Split by tier per CLAUDE.md before crediting either change.
3. **Evidence gate first fire**: expect a blocked commit + self-service
   downscale in a fixer transcript, not an escalation.
4. **Ledger sanity**: repair legs should ledger single-digit-to-$20 deltas,
   never the resolve's magnitude again.

## Gotchas (new + carried)

- Codex-cli 0.145: usage is TWO lines on stderr ("tokens used\n1,820");
  rollout files under `~/.codex/sessions/YYYY/MM/DD/` are the structured
  truth (`token_count` events, cumulative). Effort default is LOW.
- The wrapper-shell stale-export bug is fixed for HarnessConfig knobs only —
  do not manage TEST_CMD/gate env by deletion until #12-residual lands.
- Escalation "to a human" = a PR comment; labels stay. Raising the cap and
  letting reload re-dispatch IS the designed unblock.
- rtk hook mangles some compound greps — when a grep on journals returns
  rtk's own help text, re-run via a python heredoc (Read-based filters).
- Evidence e2e legs open REAL app windows on the operator's desktop
  (`cargo-target/release/tauri-explorer /tmp`) — cosmetic, but say so before
  the operator notices ghost windows.
- Carried: pgrep char-class breaker; gh label search staleness; stream-vs-
  result usage skew; UTC-day cap window; worktree forensics paths — see the
  fourth handover (now in docs/history/).
