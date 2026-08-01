# Maiden-voyage supervision runbook (for the babysitter)

**Date:** 2026-07-31 · **Applies to:** the first supervised foreground canary
**after** the Tier-1 remediation lands
(`docs/open/2026-07-31-codex-remediation-workorder.md`). **Companion:**
`docs/open/2026-07-31-implementation-review-for-fable.md` (why these risks exist).

You are the safety net for a system that resolves GitHub issues end-to-end with
**real money and real push access**. The cores are solid; the risk is
integration completeness. Your job is not to trust the harness — it is to watch
the specific surfaces that a green test suite could not vouch for, and to pull the
plug fast if any of the known failure signatures appear.

---

## 0. Go / no-go before you start

Do not launch until **all** of these are true. If any is false, stop and escalate.

- [ ] Tier-1 fixes (B1–B6) are merged to `dev` and `make test` + `make typecheck`
      are green on the exact checkout you will run.
- [ ] `make typecheck` actually runs the real gate (B8): confirm `preset =
      "basic"` is gone from `pyproject.toml` (or replaced by per-rule
      suppressions). If it is still `basic`, the ports are unenforced — no-go for
      unattended, acceptable only for a tightly-watched foreground run.
- [ ] You are running **foreground `point-at` / `issue-agent start`, NOT
      systemd.** Systemd (B10) is a separate blocker; do not use it for the maiden
      voyage.
- [ ] The machine **will not suspend/sleep** for the entire run. Disable sleep,
      lid-close suspend, and any power-nap. (B5: a suspend mid-flight can destroy
      paid work; even after the B5 fix, treat suspend as untested for voyage one.)
- [ ] **All spend caps are set before first admission** and you will **not** change
      which caps are armed while it runs (arming a cap mid-run has undefined
      interaction with in-flight reservations). Confirm `MAX_BUDGET_USD` /
      `FIX_BUDGET_USD` / daily cap are the values you intend.
- [ ] Target repo is one you can afford to have mutated: a scratch/self-authored
      repo, **not** something you care about, for voyage one.
- [ ] The issue is **trivial and self-authored** (the harness runs on trusted
      inbound content only — the governor is advisory).
- [ ] First run is single-tick: `ISSUE_AGENT_MAX_TICKS=1`. Do not go to a
      continuous loop until the single tick completes clean.
- [ ] **`control.db` backup taken and digested** immediately after any
      `migrate_legacy_spend` step, and the real `spend.jsonl` preserved read-only.
      There is no rollback path for the money cutover (CONTROL_STORE_MODE was never
      built), so this backup is your only recovery. Record the sha256.

Know where things live before you start:
- Control DB (authority for spend/caps/leases): `~/.local/state/issue-agent-control/control.db`
  — **machine-wide, outside the repo.** Treat it as precious. Never rsync/clean
  over it.
- Event log (telemetry): `events-v2.jsonl*` in the state dir.
- Launch snapshot: `config/.active.<name>.env`.

---

## 1. Launch sequence

1. Take + digest the `control.db` backup (above). Record `sha256`.
2. Confirm caps in `config/.active.<name>.env` are the intended values.
3. Start foreground, single tick:
   ```
   cd /path/to/target
   ISSUE_AGENT_MAX_TICKS=1 issue-agent start      # foreground; watch stdout
   issue-agent ready <issue-number>               # admit the trivial issue
   ```
4. Open the dashboard in a second terminal and keep it visible:
   ```
   issue-agent dashboard --text        # or --html; port 8321 for the web view
   ```
5. Tail the event log in a third:
   ```
   tail -f <state-dir>/events-v2.jsonl
   ```

You watch three things continuously: **stdout of the daemon**, **the dashboard**,
and **spend**. Do not walk away during voyage one.

---

## 2. What to watch, and the exact failure signatures

Each row is a known residual risk from the review. If you see the signature, take
the action. "Abort" = SIGTERM the daemon (see §4), then diagnose before any
restart.

| Surface | What healthy looks like | Failure signature → action |
|---|---|---|
| **Spend / caps** (B3, B4) | Available cap decreases only as legs actually charge; reservations settle after each leg | Cap total or spend **suddenly reads zero** → the control DB was recreated empty (B3). **Abort immediately**, do not let it admit paid work, restore from backup. Cap monotonically **drains toward EXHAUSTED with no matching charges** → retained reservations not reclaimed (B4). Abort; a restart runs `recover()` and reclaims — but confirm the B4 tick-sweep fix is actually in. |
| **Control DB file** | Present, non-trivial size, unchanged sha of the *backup* | `control.db` missing / 4 KB / mtime jumped unexpectedly → someone/something touched authority. Abort, restore, investigate. |
| **Dashboard** (B7) | Renders issue cards with stage, legs, model, cost; updates each tick | Dashboard **hard-crashes / stack trace** on `decode_event` → one bad event line (B7b). The daemon itself is unaffected but you are now flying blind — fall back to tailing `events-v2.jsonl` directly and abort at the first other anomaly. Cards render but **clocks / turns / usage / blocked-reason / badges are blank** → known v2 blindness (B7a) if unfixed; you lose those signals, lean harder on the raw event log. |
| **Governor / push gates** (B1, B2) | Only intended `git push` / `gh pr create` on the harness branch | **Any** push/PR to an unexpected ref or with `--no-verify`/`--force`, or any publish carrying a foreign `ISSUE_AGENT_RUN_ID` → the governor was bypassed (B1/B2 if unfixed). Abort instantly; this is the highest-severity signature. Watch the daemon stdout for gate DENY/ALLOW lines. |
| **Suspend / recovery** (B5, B6b) | No `agent_run_abandoned` unless a leg genuinely died; boot `recover()` completes | A burst of `agent_run_abandoned` + killed legs shortly after any timing hiccup → suspend-wake reaper (B5). `recover()` **raises / half-completes on boot** (orphan kills happened but the loop aborted) → unguarded telemetry emit (B6b). Abort; do not loop-restart into a half-recovered state. |
| **GitHub side effects** (B6a) | Each mutation applied exactly once | A GitHub mutation appears **twice** (duplicate PR/comment/label), or a reopened #64-style double-apply → receipt rollback under a large error body (B6a). Abort; reconcile GitHub state by hand before continuing. |
| **CI / merge** | PR opens, CI runs, review leg approves, merge to `dev` | Merge to any branch other than `dev`; merge without a passing review leg → stop and inspect the workflow. |

General rule: **any stack trace on stdout, any unexplained spend movement, or any
GitHub mutation you did not expect = abort first, diagnose second.** Money and
push access make "wait and see" the wrong default.

---

## 3. Cadence

- **Tick 1 (the `MAX_TICKS=1` run):** watch every step to completion. Confirm: one
  issue admitted, branch created, legs charged and settled, PR opened, review leg
  ran, merge to `dev`, spend reconciled (reservations all settled, cap math adds
  up against the charges).
- **Only if tick 1 is clean:** consider a short continuous run (drop
  `MAX_TICKS`), still foreground, still watched, still one trivial issue at a time.
  Do not add concurrency for voyage one.
- Check the spend reconciliation (`issue-agent dashboard` spend view, or the
  control-DB charge/reservation tables) after **every** leg, not just at the end.

---

## 4. Abort / kill switch

- **Graceful stop:** `SIGTERM` the foreground daemon (Ctrl-C once, or
  `kill -TERM <pid>`). This is the supported teardown — it stops admission,
  cancels/drains active work within a bound, writes handover/receipts, flushes the
  outbox, kills remaining process groups, and exits with a stable code. Give it the
  drain window; **do not** immediately `SIGKILL`.
- **Hard stop only if graceful hangs past the drain timeout:** a second Ctrl-C /
  `SIGTERM` triggers the hard `os._exit`. `SIGKILL` (`kill -9`) is last resort —
  it skips receipt writing and leaves reconciliation to the next boot's
  `recover()`.
- **After any abort, before restarting:** confirm `control.db` is intact (sha vs
  what you expect), confirm no orphaned codex/claude process trees survive
  (`pgrep -af codex`, `pgrep -af claude`), and let the next boot's `recover()` run
  to completion — watch that it does not raise (B6b). Do not loop-restart.
- **If spend/caps read zero or the DB is damaged:** do not restart into it. Restore
  the backup, then investigate.

---

## 5. When to stop the voyage entirely and escalate

Escalate to the maintainer (do not keep restarting) if any of these:
- The control DB was recreated empty or spend reset to zero (B3 materialized).
- A governor bypass was observed (unexpected push/PR/force, or foreign run-id
  publish) — B1/B2 materialized.
- A GitHub mutation double-applied (B6a materialized).
- Paid legs were killed by a recovery pass without a real fault (B5 materialized).
- `recover()` fails to complete on boot (B6b materialized).
- Two consecutive aborts from the same signature.

Any of these means a Tier-1 fix did not hold, and continuing risks money or repo
integrity.

---

## 6. What to capture for the post-voyage record

The current canary evidence is narrow (one repo, one runtime, no cap hit, no
concurrency, no fault injection). This run exists to widen it. Record:
- Wall time issue→merge; cost per merged PR; leg cost mix.
- Whether any cap was approached/hit and whether the reservation math stayed
  correct.
- Every abort: signature, what you saw, control-DB state, how recovery behaved.
- Dashboard fidelity: which fields rendered vs blank (validates B7 fix).
- Final spend reconciliation: reservations all settled, charges match cap
  movement, `control.db` sha vs backup.
- Confirm no orphaned process trees remained after clean shutdown.

Write it up as a dated handover in `docs/open/` and update `residuals.md` with
anything the voyage newly validated or newly exposed.

---

## Quick reference

- Start: `ISSUE_AGENT_MAX_TICKS=1 issue-agent start` then `issue-agent ready <n>`
- Watch: `issue-agent dashboard --text` + `tail -f events-v2.jsonl` + daemon stdout
- Stop: `kill -TERM <pid>` (let it drain) — never lead with `-9`
- Authority DB: `~/.local/state/issue-agent-control/control.db` (backup first)
- Golden rule: unexpected spend movement, any stack trace, or any GitHub mutation
  you did not expect → **abort, then diagnose.**
