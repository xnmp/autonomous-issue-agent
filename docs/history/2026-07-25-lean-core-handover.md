# Handover — lean core implemented (2026-07-25)

Audience: the next working session (human or agent). Prerequisite reading:
[IMPROVEMENT-PLAN.md](2026-07-25-improvement-plan.md) (the audit + full reference design —
its appendices are still the evidence base) and
[FAILURE-MODES.md](../reference/failure-modes.md) (now 106 entries). This document is the
delta: what was actually built, the decisions made *during* implementation
that the plan doesn't record, the known gaps, and how to decide what to do
next.

> **Correction notice (2026-07-25, later the same day).** An adversarial
> review ([REVIEW-2026-07-25-adversarial.md](2026-07-25-adversarial-review.md))
> audited this handover against the tree and **falsified three of its claims**;
> corrections are marked **[CORRECTED]** inline below. The remediation plan is
> [PLAN-2026-07-25-review-remediation.md](2026-07-25-remediation-plan.md).
>
> **Status update (2026-07-26).** The plan's P0 and P1 waves have landed —
> `44fbd16` (P0-A), `9ecb0ae` (P0-B), `299c825` (P1-1) — followed by a
> **second, independent adversarial re-review of that work**
> ([REVIEW-2026-07-26-p0-rereview.md](2026-07-26-p0-rereview.md)), which
> confirmed the headline repros closed but found 5 further P0s (two of them
> *introduced by* the remediation) and 18 P1s. Those are fixed in `f0b4040`
> (P0R) and `cebf088` (P1R). Each [CORRECTED] note below now carries the commit
> that closed it. §7's "point at 2–3 real repos and let the ledger run" is
> **no longer blocked on correctness work**, but read
> [REVIEW-2026-07-26-p0-rereview.md](2026-07-26-p0-rereview.md) §4
> (accepted residuals) and §5 (the process finding) before doing it: the
> `sh -c` governor bypass is still open by design, and the ledger still cannot
> see spend on plans that report $0 (it now warns loudly instead of lying).

## 1. What landed (all on `main`, each leg committed only after full `make test`)

| Commit | Content |
|---|---|
| `8fa32a5` | The improvement plan itself |
| `7541fa2` (leg 1) | Phase-0 wave 1: V1 resume-dedup write side, V2 ADDRESS_REVIEW supersession gate + review-keyed breaker, `Review.commit_id`, gate test-run timeouts, fail-closed check-push + fail-closed TEST_CMD (`ALLOW_UNTESTED`), list-truncation warnings, token-mint retry, pgrep anchor, docs statuses |
| `8900497` (leg 2) | ExitReason taxonomy + infra-death dispatch policy, wall-clock caps (both runners), FIXER_DENY, AGENT.md resume-skip + validator diff-scoping, screenshot-per-AC rule, T3 convergence property test **+ the three bugs it found** (resolve breaker #104, automerge marker #105, empty-reply strand #106), GhUnavailable rate-limit detection, systemd-first + logs out of /tmp, config print + fail-loud REPO |
| leg 3 (this commit) | P1-lite: JSONL journal, cost ledger (incl. cache-token capture), per-issue + global-daily spend caps, escalation/alert hook, `issue-agent status` v2, this handover |

Every plan lean-core item is done. Everything else in the plan is
**trigger-gated backlog** — §4's table maps each item to the observation that
justifies building it. Do not build backlog items without their trigger.

## 2. Design decisions made during implementation (not in the plan)

These are the load-bearing choices a future refactor must not silently undo:

- **Review-retry counter is keyed to the review's `submitted_at`**
  (`FixState.review_retries: dict[key, (review_at, count)]`). A count stored
  under an older review reads as 0. There is deliberately **no
  clear-on-success** — supersession (a new review) is the only reset. Both
  sides (decide read, workflow write) use the *same selector*
  (`latest_other_review`, CHANGES_REQUESTED only). **[CORRECTED]** The claim
  "T3's mirror will fail if either side drifts" is false: T3's `_after_run`
  hand-mirrors the workflow, so it guards the decide/**read** side only —
  deleting the workflow's own breaker bump left T3 10/10 green
  (mutation-proven). `tests/adapters/test_fix_workflow.py` is the actual
  write-side guard; every new counter needs a test there, not just a T3 row.
- **A superseded CHANGES_REQUESTED review falls through to FIX_CI** — while
  awaiting re-review, a CI failure on the new head is still the fixer's job.
- **Infra deaths (`killed_externally`, `transport_death`) are not attempts**:
  no breaker increments, no relabel — the issue stays `ai-working` and the
  reaper settles it. But **ground truth wins over cause of death**: if a PR
  actually opened, it gets labeled even when the session died (deliberate
  deviation from the plan's "return [] unconditionally"). **[REVIEW NOTE]**
  the review's C3 shows the raise-path variant (AgentRunner pre-result death)
  is also unrecorded — no journal event, no ledger line, invisible to the
  loop detector — so the reap→redispatch cycle is unbounded, unbilled, and
  undetectable. Remediation plan D2 adds pre-await attempt recording and a
  separate `MAX_INFRA_DEATHS` ceiling without changing this rule.
- **`AgentRunner`'s pre-result exception path still raises** (it *is* the
  transport-death signal there); only CodexRunner returns classified error
  results. The `asyncio.wait` observation pattern in the runner must never be
  replaced with `wait_for` — the long comment at agent_runner.py:311-329
  documents the result-delivery drop that caused.
- **Wall cap checks between events, not only on slice expiry** — a fast-
  streaming runaway would otherwise never trip it.
- **Resolve breaker counts REQUEUE outcomes only** (error, or claimed-done
  with no PR). Blocked doesn't count (human engaged); infra death doesn't
  count. Escalation happens decide-side from `ai-ready`, via
  `issue_escalation` (new domain object).
- **Spend caps enforce at two different strengths**: per-issue lifetime cap →
  one-shot escalation to `needs-human-review` (state change, stops
  re-dispatch); global daily cap → skip-with-loud-log only, zero GitHub
  writes, self-heals at 00:00 UTC (accepted relabel churn via the reaper).
  `DispatchConfig` cap defaults are 0 (inert) — the operator defaults (25/50)
  live in `HarnessConfig`, so hand-built configs/tests keep old behavior.
- **`JournalingRuntime` wraps the runner** (decorator over `AgentRuntime`) and
  the dispatcher sets `(role, kind, number, issue)` context before each
  dispatch — multi-leg review runs emit one `agent_run` per leg with shared
  context, giving per-leg cost attribution without touching workflows.
- **Escalation detection lives in `Executor.execute`** (the single write
  funnel): any Relabel adding `needs-human-review` emits `escalation` +
  fires `ALERT_CMD`. New escalation paths get alerting for free.
- **Per-AC evidence contract** (both gate sides share it): AC section = heading
  starting "Acceptance Criteria"; items = `- [ ]`/`- [x]` task lines; when
  `.harness/visual-ac` is declared, require read+committed evidence images ≥
  AC-item count AND that many distinct `evidence/*.png|jpg|…` references in
  the PR body. 0 parseable items → falls back to the old ≥1 rule (a parse
  failure can never deny on its own).
- **`GhUnavailable` classification is pattern-ordered** (rate-limit → server →
  network) and deliberately narrow: bare 403 is a permission answer, not
  infra; `could not resolve host` (not bare `could not resolve`) because gh's
  GraphQL 404 says "Could not resolve to a PullRequest". `_safe_get_pr`
  isolates only *timeouts* per-PR; rate limits propagate (they'd fail every
  PR identically → empty world).
- **`FIXER_DENY`** (tasks.py): WebFetch/WebSearch, curl/wget, `gh auth`,
  `gh secret`, `gh api` (blanket — finer policy is P3.2's job), `.env*`/
  `.pem`/`id_rsa*` reads. `Task` is NOT denied (adversarial-review subagent
  needs it). Under `bypassPermissions`, `disallowed_tools` is the only policy
  the SDK honors — settings.json deny rules are dead on the SDK path.

## 3. New env knobs

**[CORRECTED — fixed in `44fbd16`, hardened in `f0b4040`]** "all documented in
`config/agent.env.example`" was false: `MAX_RESOLVE_RETRIES` and `LOG_DIR` were
missing from that file, and `compose-env.sh`'s hand-maintained allow-list
dropped these plus ~18 other documented knobs on the systemd path (review C5).
Both are fixed: the knobs are documented, and `compose-env.sh` no longer has an
allow-list at all — it emits the union of an `env -0` pre/post diff and the
names the config files textually assign. The re-review found the *first* version
of that fix silently dropped any var whose composed value matched the launching
shell's (P0R-1, a regression that made `TEST_CMD` vanish under systemd and the
gate fail closed on every push); `f0b4040` closes it, and the test asserting
"a var the files assign is still emitted" is the guard.

| Knob | Default | Meaning |
|---|---|---|
| `MAX_RESOLVE_RETRIES` | 3 | Resolve-leg breaker → needs-human-review |
| `AGENT_MAX_RUNTIME_SECONDS` | 2700 | Wall cap per session, both runners (0=off) |
| `GATE_TEST_TIMEOUT_S` | 600 | Gate test-run timeout; timeout = FAIL |
| `GATE_GH_RETRY_SLEEP` | 2 | Push-gate retry gap before fail-closed deny |
| `ALLOW_UNTESTED` | unset | Operator opt-out of fail-closed tests gate; auto-written to overlays for test-less repos |
| `ISSUE_SPEND_CAP_USD` | 25 | Lifetime per-issue spend → escalate (0=off) |
| `DAILY_SPEND_CAP_USD` | 50 | Global daily cap across repos → skip dispatch (0=off) |
| `ALERT_CMD` | "" | Shell cmd fed event JSON on stdin (spend_block, escalation) — e.g. `notify-send harness` or an ntfy curl |
| `LOG_DIR` | `$XDG_STATE_HOME/issue-agent/logs` | Foreground-mode log dir (0700); systemd units use journald |

## 4. Observability: what exists now and where

- **Per-repo journal**: `$STATE_DIR/journal.jsonl` — events `agent_run`
  (role/kind/number/issue/model/duration/cost/status/exit_reason/usage with
  the four cache-token counters), `action`, `escalation`, `spend_block`,
  `gh_unavailable`.
- **Global spend ledger**: `~/.local/state/issue-agent/spend.jsonl`
  (`repo/issue/kind/cost_usd` per run) — the daily cap and cross-repo
  accounting read this.
- **`issue-agent status`**: now shows today's global spend vs cap, last 5
  agent runs, and warnings (repeated same-target dispatches within an hour,
  spend blocks today, escalations in 24h). Log tail falls back to the old
  /tmp path for pre-move daemons. **[REVIEW NOTE — fixed in `9ecb0ae`]** it
  read the *fix* daemon's journal only, so review/steward runs, their spend
  blocks and their loops were invisible (M11); it now merge-sorts all three
  role journals by `ts`. Review-side escalations bypassed the executor funnel
  entirely, emitting no `escalation` event and no `ALERT_CMD` (M10); they now
  route through it, so they alert like every other breaker.
- **What the journal cannot tell you (read before trusting it).** Runs that
  cost `$0.00` write no ledger line at all — true for subscription-plan Claude
  and for Codex without `CODEX_COST_PER_MTOK_*` — so on those deployments the
  spend caps cannot fire for *any* loop shape. `cebf088` makes the harness say
  so out loud (one warning when settled runs keep costing $0 while a cap is
  armed) rather than reporting a reassuring $0.00. A session the daemon did not
  outlive (SIGKILL/OOM/`systemctl restart`) still reaches no bump site; it
  leaves an unsettled `agent_run_start`, which `f0b4040` teaches the decide side
  to count, so it is now bounded even though it remains unbilled.
- **No rotation yet** — journals grow unbounded. Fine for months at current
  volume; add rotation when a file is annoying, not before.

## 5. Known gaps / small follow-ups (each deliberate, none dangerous)

1. **launchd installer** still has both bugs its systemd twin had fixed:
   /tmp logs and fixed unit names that collide across repos. Fix when a mac
   deployment matters.
2. **Python push gate — WIRED as of `9ecb0ae`, corrected in `cebf088`.**
   **[CORRECTED]** the original claim that "the *bash* push gate is the live
   one" was false — the bash checkers run only under `FIXER_RUNTIME=codex`, so
   on the default SDK path **nothing** gated pushes to an open PR and every
   ADDRESS_REVIEW / FIX_CI push ran with no test/commit-log/evidence gate at
   all. `9ecb0ae` wired a lazy `gh pr list` probe into `GateContext.open_pr`
   (tri-state; unwired default and probe errors both fail closed; zero gh calls
   on non-push commands). The re-review then found three ways it still didn't
   bite, all fixed in `cebf088`: it keyed on the worktree's HEAD branch rather
   than the **pushed ref** (so a conflicted rebase — literally the REBASE leg's
   normal state — reported branch `HEAD`, was read as ungoverned, and skipped
   the gate); the probe omitted `--repo`, so on a multi-remote target gh
   answered about the wrong repository and reported "no PR"; and the ERROR arm
   denied *clean* pushes, so a rate limit walked healthy issues to a human.
   **Still open by design:** `sh -c 'git push …'` and friends classify as
   nothing and bypass the gate entirely (M9 / backlog P3.2) — now recorded as
   `xfail(strict=True)` tests rather than prose.
3. **Commit-time red-check** (`check-commit.sh` / `check_red_is_red`) still
   silently no-ops with no test command — the PR/push gates now deny, so the
   window is one commit, not a merge. Close it opportunistically.
4. **`--body-file` PR bodies** aren't parsed by the *Python* §7d per-AC rule
   (pre-existing extractor limitation; bash side handles it). Codex/legacy
   path is fully covered.
5. **Per-issue spend escalation always removes `ai-working`** — on a
   review-side block the issue may carry `ai-pr-open`; the remove is a no-op
   and the needs-human-review add still lands. Cosmetic.
6. **`latest_review_body`** (resume prompt input) is still author-agnostic —
   it can quote the fixer's own review body. Harmless today; unify with
   `latest_other_review` if it ever confuses a resume.
7. **Resolve-retry counter persists** across a PR-closed-unmerged requeue
   (conservative: fewer retries after a human closed the PR). Clear it in
   cleanup if that ever annoys.
8. **Q4/Q5 of Appendix G remain open** (Codex context balloon; App
   Checks:Read scope). The ledger now measures the first directly.

## 6. How to run it now (operational changes)

- Real operation: `issue-agent install-service` / `make install-systemd` —
  units are now **per-repo named** (`issue-agent-<role>-<name>.service`),
  config snapshotted to `~/.config/issue-agent/<name>.env` (0600), journald
  logging, `Restart=always`/`RestartSec=30`. Foreground `issue-agent start`
  is the dev/first-run mode.
- On first `start` against a test-less repo, the overlay gets
  `ALLOW_UNTESTED=1` written automatically (loud banner still fires); remove
  it when tests exist.
- Every daemon start prints its resolved config (one grep-able block) and
  fails loud on empty `REPO`.

## 7. Next phase: how to choose the work

**Point the harness at 2–3 real repos under systemd and let the ledger run
for a week.** Then walk the plan §4 backlog table top-down and fire only the
rows whose trigger has data behind it. The likely first candidates, with
their evidence source:

1. **Cache hit-rate** (plan §6, open Q3): `agent_run.usage.cache_read_input_tokens`
   — if ~0 across runs, prompt caching isn't working and P4.9 (cross-leg
   cache sharing) jumps the queue; if healthy, P4.9 drops.
2. **Model routing P4.1**: from the ledger, median cost of `trivial`-labeled
   issues — if ≳$1, revive `build_router_task` (still dead code, kept
   deliberately) with per-dispatch `FixTaskConfig`.
3. **Review cost P4.2-full/P4.3**: if review legs are a top-2 ledger line,
   scope dimension specialists (validators are already scoped) and adopt
   persistent review sessions.
4. **Reconciliation/leases P2.1–2.3**: only if a stranded-label or
   state-drift incident recurs *despite* systemd + reaper.
5. **Governor tiers P3.2**: before onboarding any repo with untrusted inbound
   issues — `sh -c`/`python -c` payloads are still unparsed (known bypass,
   Appendix C).
6. **Legacy deletion P6.1** (~1.6k LOC bash + 18K fakes): opportunistic;
   `make lint` still shellchecks it all.

The convergence suite (`tests/domain/test_convergence.py`) is the regression
guard for the anti-loop class — any new dispatch leg or retry counter must add
a scenario row there. **[CORRECTED — fixed in `1968888`]** the original claim
that the mirror "will catch read/write drift" was mutation-proven false:
`_after_run` re-implemented the workflow by hand and could not detect drift in
it (deleting the workflow's own breaker bump left it 10/10 green). D13 has now
replaced it: each row drives a real `Daemon` tick loop over the real
`FixWorkflow` → `AgentDispatcher` → `Executor` → `Store`, with `roles._fix_state`
as the read side, so both halves are production code and the only remaining
double is a gateway that mutates an in-memory world the way GitHub would. The
same mutation now FAILS the suite (12 dispatches instead of the capped 3), so a
new leg no longer needs a separate write-side test purely to catch drift —
though `tests/adapters/test_fix_workflow.py` is still where leg-specific
persistence belongs.

**Two mechanisms this codebase now has that it lacked, and should not lose:**
`tests/test_gate_parity.py` runs golden cases through BOTH enforcement stacks
and fails on disagreement — the sync mechanism that replaces "keep in sync"
comments, which had already drifted every time the review checked. And the
convergence suite above is now an oracle rather than a mirror. Both exist
because a hand-maintained restatement of behaviour will drift, silently, and
the drift is only ever found by someone deliberately looking.

## 8. Execution pattern that worked (for the next big push)

Fable owned the domain seams (decide/outcome/workflow semantics, ExitReason
and journal schema design, all cross-cutting wiring); waves of 3–6
Opus/Sonnet subagents ran on **strictly disjoint file sets** with a
conventions brief in every prompt; each leg was committed only after the full
`make test` (hooks + orchestration + domain + adapters + import-linter) was
green. Agent reports flagged out-of-scope follow-ups instead of editing
beyond their file list — those became the orchestrator's queue.
