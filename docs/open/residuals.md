# What is still open — 2026-07-30

The single answer to "what's left?". Everything here is **deliberately** open:
known, assessed, and not fixed for a stated reason. If you find something open
that is *not* on this list, that is a gap in this document, not an accepted risk.

Sources consolidated here (all now historical — do not re-read them for status):
the 2026-07-25 adversarial review, its 2026-07-26 re-review §4, the remediation
plan §6b, the lean-core handover §5, and the improvement plan's §4 backlog.

---

## 1. Blocking anything — before you point this at a repo you care about

The control-plane correctness blockers are closed. The remaining restrictions
are trust-boundary residuals: they matter whenever inbound repository content
is not trusted. Every named residual below is executable in
`tests/test_residuals.py`; the tests assert the desired closed behavior under
`xfail(strict=True)`, so fixing one requires removing or updating its registry
entry.

### 1.1 Publication credentials are still present in agent processes

SQLite reservations, commands, receipts, and process leases are authoritative.
Supported agent push/PR flows go through `issue-agent-publish`, with durable
commands, remote readback, and receipts. Publication environment variables are
removed from supervised child commands, but the model still runs as the daemon
OS account and inherits its home-directory surface. Environment scrubbing does
not prevent recovery through filesystem or local-socket access
(`CRED-HOME`). An agent-local hook therefore remains defense-in-depth, not an
authorization boundary.

The resolver denies parse errors, excessive syntax depth, functions, dynamic
command positions, supported interpreter `-c` forms, obscuring wrappers, and
all governed commands it recognizes. It does **not** close arbitrary indirect
execution. The accepted holes are:

- script-file execution through `bash file.sh` or `./file.sh`
  (`GOV-SCRIPT-FILE`);
- util-linux `script -c` (`GOV-SCRIPT-C`);
- `node`/`perl`/`ruby -e` (`GOV-INTERPRETER-E`);
- `git rebase --exec` (`GOV-REBASE-EXEC`).

**Consequence:** do not point this at a repository whose inbound issue, PR,
review, or CI text is supplied by people you do not trust. The trigger-gated
end state is a credential-free agent plus a separately privileged artifact
publisher; see ADR 0001 and the capability matrix.

### 1.2 Agent-written claims are not authorization evidence

The PR gate no longer reads `.harness/ac-posted`, `test-plan-posted`, or
`adversarial-verdict`. Acceptance criteria remain visible in the PR body,
test results come from the trusted executor, and adversarial review is process
discipline rather than self-attestation.

Visual-evidence marker/receipt bookkeeping was removed in #327; reviewers judge
screenshot outcomes. `.harness/test-cmd` remains an agent-selected
fallback when the operator supplies no `TEST_CMD` (`CLAIM-TEST-CMD`), but it
never supplies the execution result. That claim alone does not authorize
publication.

### 1.3 The first real GitHub canary is complete

The 2026-07-31 release canary used a self-authored issue in
`xnmp/autonomous-issue-agent`. It exercised durable label effects, paid Codex
admission and settlement, an active SIGTERM shutdown, handover persistence,
process cleanup, and the dashboard projection. The original issue labels were
restored and the final control-store check found no live runs, reservations,
or leases. The immutable receipt is
[live-release-canary-evidence.json](../reference/live-release-canary-evidence.json).

This was a real but narrow canary: one repository, Codex, and one self-authored
issue. It did not hit a configured cap, exercise concurrent dispatch, inject
disk/database/process faults, or boot the systemd units. Consequently it did
not expose the later-reviewed database-deletion, retained-reservation,
suspend-wake, dashboard-v2, or systemd-readiness defects. It closes only the
specific paths recorded in the receipt; it does not replace a bounded canary
for a different repository, runtime, service manager, or credential topology.
Start each new deployment with `ISSUE_AGENT_MAX_TICKS=1` and a trivial
self-authored issue.

---

## 2. Accepted divergences and known-imperfect behaviour

| Item | Why it's accepted | Where it bites |
|---|---|---|
| Push gate: bash denies on an unreadable PR state, Python runs the state checks and allows a clean push | P1R-12 was decided for the Python path only; changing the codex path's fail-closed stance needs its own call | codex mode only; asserted in `tests/test_gate_parity.py` |
| Commit-arm rule *order* differs between stacks (lint before ordering vs interleaved) | Verdicts are identical, only the reported reason differs | cosmetic; asserted as a parity divergence |
| `git -C /other/worktree push` is gated against *this* worktree's state | The destination branch is now correct (P1R-10); per-cwd context is a larger change | rare; documented in `_push_gate` |
| `resolve_test_cmd` prefers operator `TEST_CMD`; the agent's `.harness/test-cmd` is the fallback | P1R-18 — the gated subject must not choose the gate's teeth | a monorepo's scoped command is ignored when `TEST_CMD` is set |
| A human closing a PR unmerged leaves `resolve-retries` standing, which now also pins every later resolve to a pricier tier (#16) | `clear_issue_resume_state` keeps the counter deliberately — "fewer retries after a human closed the PR" — and the escalation ladder reads the same counter. The evidence is honest (an attempt did complete and did not ship); the cost consequence was decided for the retry budget, not for routing | an issue a human bounced resolves at a higher tier/effort until it merges; clear `<STATE_DIR>/issue-N.resolve-retries` to reset |
| `update_branch` can report failure for an update that landed: GitHub's update-branch is async, and `is_branch_updated` verifies before the ref moves (observed live on keifu #166, 2026-08-01 — "did not land" logged, merge commit already on the branch) | The marker semantics are safe either way (the attempt was made; a genuine failure re-arms on the next base advance), and tightening would mean polling inside the executor | the `branch_refresh` telemetry event says `ok=false` for a successful update; read it as "attempted", not as ground truth |
| Dashboard/systemd install has no multi-harness-root singleton ownership and no concurrent-install lock; uninstall does not restore a previously running manual dashboard | Adversarial review 2026-08-10 (fa3bfb0) accepted these: one operator, one harness checkout per host is the supported posture, and the port-conflict preflight already names the foreign owner | running `install-service` from two checkouts (or twice concurrently) can interleave unit writes; after uninstall the operator restarts a manual dashboard by hand |
| A standalone workflow command that exhausts its 3 authorized retries (each ending in an unknown outcome) keeps its `RetryAuthorizationRequired` gate with no further grant offered and no cancel path; the dashboard does not yet say the ceiling was reached | Three consecutive unknown outcomes on one command is an infrastructure fault, not a normal path; the bound exists so an operator cannot buy unbounded remote writes (ADR 0015 attempt bound). The gate still clears as soon as a harness read finds the effect present | trigger: first live `retry_authorization ceiling_reached` journal event → add an explicit operator abandon/retire command and a ceiling reason on the gate |
| A pending re-arm projection is fenced by its semantic hold guard (open state, state reason, harness-enum labels, linked PR — ADR 0015), not the raw issue revision: labels outside the harness label enum, e.g. a custom `on-hold`, no longer supersede it (the scheduler never honoured them), and `blocked_by_open` dependencies are enforced at dispatch, not by the hold guard | Comments, body edits and priority labels must not supersede an operator's re-arm; only facts that hold, release or terminate resolve work are decision inputs | an operator relying on a custom label to stop a granted re-arm must use an `ai-parked`/`ai-blocked` hold or cancel the re-arm instead |
| A review-family DISMISS_REVIEW that lands after the PR head moved (a push between the executor's guard check and its PUT) settles as success | Presence outranks the head fence in `dismissal_check`: the dismissed review is exactly the authorized one, so nothing extra was written, and the push itself supersedes the re-arm through DECISION_CHANGED. Re-checking the head after the write could only turn a correct effect into a false refusal | the receipt says succeeded although the head the dismissal was authorized against is gone; read the re-arm's DECISION_CHANGED stop, not the dismissal receipt, for the head move |
| A bound review submission is recognized only by its final-line `<!-- issue-agent-review-command id=… -->` marker on a review authored by the review identity; unbound (legacy) submissions still reconcile by exact event/body/head content | The marker is the standard idempotency key: time windows misattributed earlier/later reviews and broke under host clock drift (re-review 2026-09-26). Marker lines already in agent text are stripped before ours is appended, and a review id can settle at most one submission receipt | someone with write access editing the bot's review body to remove its last line makes that review look absent, so an unknown submission could post its verdict a second time (bounded by the re-execution ceiling); a refused pre-submission read of an unbound review logs `review read refused` and leaves the command pending under the reviewer's unproductive-round bound |
| With the review family active, a PR held by an ESCALATE verdict (`needs-human-review` from its settled escalation receipt) has no release path short of a new head: the verdict is the reviewed baseline, so the head is never re-bought and nothing re-arms it | Slice plan decision 3: that hold is external to the review projection and its release path is phase 4. The hold itself is durable (it commits with the baseline, is re-planned a bounded number of times when a hold-fact change refuses it, and surfaces as a `ReviewProjectionRefused` gate plus one escalation signal when that bound is spent) | a human clearing the label by hand leaves the PR unreviewed at that head until a push; trigger: phase 4 review release operation, or the first live operator request to re-review an escalated head |
| Review PR projections (escalation, restoration, re-arm, release) are fenced by hold facts only (`PULL_REQUEST_HOLD_FACT_LABELS`: lifecycle, trigger, opt-out and trusted hold labels, head, open state); advisory labels (`recurrence`, `maybe-resolved`), priority and steward triage labels never refuse them. An open ceiling's hold is restored at most `MAX_HOLD_RESTORES` times per gate, then journalled once as `hold_contested` | A projection refused by an unrelated label stranded a verdict escalation (re-review N1); unbounded restoration against an automation that removes the label was a write loop (N3) | an actor that keeps removing `needs-human-review` from a ceiling-held PR wins after three restorations; the gate still holds every review and the contest clears when the gate changes or releases |
| A machine failure with no stop state retries forever under the pure backoff (`domain/backoff.py`, 60 s doubling to 1 h), including a deterministic $0 pre-admission refusal such as a provider mismatch; the backoff hold is silent and survives an operator re-arm | Refactor decision 16 and design rule 3: machine failures never count as attempts and never escalate; only the money caps bound paid retries. This replaces ADR 0012's per-loop ceiling for fix dispatch | a config fault (provider mismatch, missing pricing) shows only as hourly refusal log lines and the stall watchdog; trigger: slice 5 `derive` surfaces `Backoff(until)` on the board |
| A pre-admission refusal records its zero-cost `runs` row in two transactions (`start_run` then `complete_run`); a crash or `SQLITE_BUSY` between them leaves an unspaced refusal and an inert `starting` row with no reservation | Rare, and harmless to money; the inert row only makes the activation/import quiescence checks report non-quiescent, and slice 5 deletes those tools | trigger: slice 5 store rewrite writes the refused leg in one statement; slice 5 attempt derivation must exclude `outcome='admission_refused'` rows |
| The resolve family still discards a provider-refused (codex overload/quota) session; only PR-repair legs resume it | `workflow_policy` nulls the session for every infra failure; slice 5 replaces that policy | a resolve run refused on its last turn restarts from scratch rather than resuming |
| Per-command reconciliation lock files (`<db>.remote-effect.<hash>.lock`) are never deleted | Deleting a flock file safely needs lock-then-verify-inode; only reconciliation-only commands create one, so growth is tiny | directory hygiene only; files are safe to delete while all daemons are stopped |

---

## 3. Trigger-gated backlog

Do **not** build these without their trigger — that rule is why the lean core
stayed lean. Full table in `docs/history/2026-07-25-improvement-plan.md` §4.

| Work | Trigger |
|---|---|
| P4.9 cross-leg prompt-cache sharing | `agent_run.usage.cache_read_input_tokens` ≈ 0 across real runs |
| P4.1 model routing (`build_router_task` is dead code kept for this) | median cost of `trivial`-labelled issues ≳ $1 |
| P4.2/P4.3 review cost work | review legs are a top-2 ledger line |
| P3.2 governor tiers | before onboarding any repo with untrusted inbound issues (see §1.2) |
| Webhook push (`gh webhook forward`) + poll demoted to slow reconciler (#1) | GraphQL rate limiting recurs despite POLL_SECONDS=60 and the ETag pre-check (first hit 2026-07-26, review+steward on one token at 20s polls) |

---

## 4. Structural debt (P2) — gated on a week of real operation

Remaining structural work is limited to the trigger-gated credential-free
publisher and further decomposition driven by a week of real operation.

Detail in the 2026-07-25 review §6. The plan deliberately gates all of it on
operating experience: none of it is required for correctness, and the shape of
the right abstraction depends on which legs actually get added.

Two smaller ones worth doing whenever the files are next touched:

- `build_gate_context` / `main.py` should take `lint_cmd`/`typecheck_cmd`/`adr_dir`
  directly, so `governor.complete_gate_context` can be deleted (it fills fields
  the builder leaves at named inert sentinels, so removing it is a no-op).
- Historical schema-v1 JSONL readers remain only as a read-only dashboard/status
  converter. They are never queried for admission, deduplication, process
  ownership, or effect authorization.

- `gh issue comment <n> --body …` is agent-allowlisted (bounded: plain body,
  numeric selector, ambient repo, no other flags/env — 2026-08-01, after the
  07-31 allowlist blocked the workflow's own mandatory acceptance-criteria
  step). Two accepted residuals within the trusted-inbound posture: comment
  BODIES are an unmoderated publication channel (a compromised leg could post
  file contents to its own issue), and agent-side comments bypass the
  durable-command/receipt funnel, so a retried leg can double-post. Trigger
  for promoting comments to a Governed kind with repo-pinned gating and
  dedup: onboarding any repo with untrusted inbound issues, or an observed
  double-posted criteria comment causing reviewer confusion.

- Automated merges on an UNPROTECTED base carry a base-advance TOCTOU: the
  exact-head approval/CI are validated against the observed base, and the
  executor binds only the head (`--match-head-commit`) — the base can advance
  between observation and merge, landing head changes CI never saw against the
  new base. This was the accepted posture for every pre-2026-07-31 merge; the
  07-31 hardening closed it by requiring the strict server gate but thereby
  disabled merging wherever the base cannot carry verifiable protection
  (private free-plan repos 403 on the protection API), so the fallback was
  restored 2026-08-01. Where the base CAN carry strict protection, add it —
  the server then independently re-checks review/CI/up-to-date at merge time.
  Trigger for revisiting: a merge broken by a base that advanced after its
  review.

- Legacy issue-keyed review retry counters (pre-ADR-0008 writes) are still
  honored by `roles._review_unproductive_rounds`'s read union. A counter written under issue
  N for a PR that is later relinked away is never cleared by that PR's
  `_progress`, so a *different* PR later linked to issue N inherits it and its
  `unproductive_rounds` starts inflated. Pre-existing leakage; it drains as
  legacy counters clear on productive rounds. Trigger for a one-time counter
  migration: an observed premature `needs-human-review` escalation whose
  rejection count includes a stale issue-keyed entry (2026-08-01 adversarial
  review, P2-4).

- `unknown_outcome` lifecycle commands have NO retry ceiling: a command whose
  reconcile read permanently fails (e.g. the target issue was deleted) retries
  one gh call per tick forever, visibly (the drain logs it) but unbounded.
  Round-1 of the 2026-08-02 effect-retry work bounded this by accident via the
  shared attempts counter; round-2 correctly stopped counting outages toward
  the refusal ceiling and thereby unmasked it. Trigger: an `unknown_outcome`
  command older than a configured tick-age escalates like an exhausted one
  (ADR 0012 — the loop currently has an event only via drain logs, no ceiling).

- `harness_signal` (2026-08-02) lets a durable command bypass the optimistic
  revision guard. The only minter is `steward_stall_actions`, and the payload
  is written solely by harness code — but a paid child that opened
  `ISSUE_AGENT_CONTROL_DB` directly could forge a flagged row, and such a row
  now buys a guard bypass rather than just a write. Same accepted posture as
  the capability matrix ("children inherit publication capability; trusted
  inbound content only"); noted because the blast radius widened marginally.
  Trigger: any move to untrusted inbound content revisits this field first.

- The doctor Issues:Write live-probe (2026-08-02) only runs for
  `REVIEW_BOT_TOKEN` deployments; GitHub App-auth deployments (`GH_APP_ID`)
  skip it silently — an installation missing Issues:Write still surfaces only
  at the first denied write (now ceiling-bounded rather than looping).
  Trigger: first App-auth deployment adds an installation-permissions probe.

---

- The blocked-by dependency gate on the PR repair arm (2026-08-03) fails OPEN
  for one tick when the linked issue's own fetch times out and the issue drops
  from the world: "dropped" and "issueless PR" are indistinguishable at the
  decision layer, and holding issueless PRs forever is the worse failure. A
  blocker-READ failure alone now fails closed (issue marked blocked, no
  witnesses). Residual exposure = pre-feature behavior for that tick.
  Trigger: a paid dispatch observed into a known-open blocker's wedge.

The ADR 0014 exemption-collection residual is retired under #325. The
test-modification approval chain, merge withholding and MAC-key escalation
were removed; its counter trigger no longer applies.

- The unproductive-review-round breaker still reads the review role's
  `infra-deaths` files plus recovered run deaths from SQLite. Review activation
  imports rejection ceilings, baselines and guidance only: ADR 0015 explicitly
  separates this breaker from the rejection family. Moving its counter alone
  would leave resets and verdict-escalation release under another authority.
  Its existing ceiling remains enforced and named in external holds.
  The recovered-death query is currently owner-wide rather than bounded by a
  productive review reset, so historical recovered deaths can outlive the
  file-counter reset. The phase-4 migration must attribute immutable run IDs
  to failure episodes; copying the file value alone cannot repair this.
  Trigger: phase 4 migrates the breaker, consumed continuation state and its
  release operation together, with shadow comparison and writer fencing.

- Platform-invisible CI (2026-08-08) — the local gate runs on Linux, so
  windows-latest/macos-latest job failures can never be reproduced or fixed
  by evidence available to a local leg; each one costs either non-converging
  FIX_CI legs or an operator (tauri PR #581: a BOM/codepage failure only
  Windows PowerShell 5.1 could exhibit took the batch's single hardest human
  intervention). Doctor warns at launch; the environment manifest (85c98ff)
  now tells legs what they cannot reproduce so they reason from CI logs
  instead of attempting local repro. Accepted residual: true convergence
  needs paid platform runners or a repository policy that declares which
  checks are required (#303) — an
  operator decision per target, not a harness fix. Trigger: a second
  cross-platform non-convergence incident adds a FIX_CI prompt hint that
  names the unreproducible platform and caps local attempts at one leg
  before escalating with the CI log attached.

- Escalations on feed-tombstoned targets never retire (2026-08-10) — the
  dashboard's escalation banner retires on observations of the escalated
  target (b1ea5bd + observed-world level-trigger), but a target the role
  feeds stop selecting (`present: false` — e.g. an operator closes an
  escalated PR unmerged while its issue stays open) is never observed again,
  so its banner and Unblock affordance outlive the hold. Per-feed absence is
  not closure, so it must not retire anything. Trigger: a second stale
  banner traced to a tombstoned target adds an explicit terminal-state
  retirement (closed-PR observation or issue-close) distinct from feed
  absence.

## 5. Per-incident notes

`docs/open/issues/` holds three unresolved incident write-ups
(codex SIGKILL on e2e verify, persistent review sessions, review
`no ResultMessage` on background tasks). They predate the reviews above and
have not been re-validated against the current tree.
