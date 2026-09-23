# Phase 3 integration handover — 2026-09-14

## Stop condition and checkpoint

The user requested: **“go to the next checkpoint and then write a handover doc.”**
Implementation is stopped at a **working-tree checkpoint**, not a release or an
accepted phase. Do not resume implementation, agents, deployment or live migration
without new user direction. Incomplete goal automation is not permission to undo
this explicit stop.

- Repository: `/home/chong/Repos/autonomous-issue-agent`.
- Branch: `dev`; last committed revision: `ee7de45`.
- Current integration changes are preserved **uncommitted**, including new test
  helpers. No commit was made because a required crash-recovery contract is red.
- Phases 1–2 are accepted; phase 3 is incomplete. Phases 4–6 have not started.
- Full plan: [architecture implementation phases](2026-09-14-architecture-implementation.md).
- Authority proposal: [ADR 0015](../adr/0015-generation-fenced-gate-rearm.md), still proposed.
- Original review: `/tmp/architecture-review-2026-09-14/report.md`.
- Longer implementation history: `/tmp/architecture-phase3-checkpoint-2026-09-14.md`.
- Phase agent handover: `/tmp/phase3-agent-checkpoint-2026-09-14.md`.

## Protected operational state

Read-only service verification at this checkpoint confirmed all four units are
disabled with `MainPID=0`:

| Unit | Recorded state |
| --- | --- |
| `issue-agent-fix-watcher-xnmp-tauri-explorer.service` | failed, not running |
| `issue-agent-review-watcher-xnmp-tauri-explorer.service` | inactive |
| `issue-agent-steward-xnmp-tauri-explorer.service` | inactive |
| `issue-agent-dashboard.service` | failed, not running |

The recorded failed states were not reset. No live schema initialization,
activation, paid harness work, target checkout change, remote GitHub write, or
service restart was performed during this checkpoint. Existing live state and
queued publication were retained. The canonical live database is
`/home/chong/.local/state/issue-agent-control/control.db`; tests use isolated
databases. Do not run the global `~/.local/bin/issue-agent` shim as a shortcut:
it was previously found to point to another checkout.

Preserved target decisions, not newly posted remotely:

- #696: blocked pending a real interactive Mac; instrumentation already merged.
- #523: add visible branch-status badges.
- #514: Gravatar only as an explicit opt-in, off by default with privacy disclosure.
- #722: the progress-bar issue concerns **pasting an image copied from another app**.
- #723: retained screenshot `/tmp/tauri-723-attachment.png` shows an unsupported
  `trash:///` URL. The shared attachment-fetch gap remains recorded, not fixed.

## Integration completed in the dirty tree

### One selected resolve authority

`domain/fix.py` replaces `FixState.resolve_retries` and `last_resolve_error` with
`resolve_workflows: Mapping[int, WorkflowRead]`. SQL owns resolve allowance,
gates, guidance and continuation. Missing/unavailable authority is never a fresh
allowance. Selection filters held candidates before priority selection, allowing
a healthy lower-priority issue to proceed. Scheduler issue effects and selected
dispatches retain the observed workflow binding; the prerequisite claim retains
the identical binding.

Unavailable issue authority suppresses its complete cleanup arm, not only its
remote command. Existing PR-only work remains in its separate family. An open
SQL resolve-ceiling gate projects `needs-human-review` from active ready/working
labels. Active workers defer that projection; held issues are not reaped back to
ready. A ready label cannot renew the episode.

`app/roles.py` injects a bound `SQLiteWorkflowControl` into the actual fix daemon.
`_fix_state` initializes only a proven absent issue under an already activated
epoch and a timestamped observation. Per-target discovery/read failures produce
`WorkflowUnavailable` and preserve healthy peers. Existing PR-linked authority
can be read when the issue is omitted from the world; that absence never
initializes an unseen issue.

`app/dispatch.py` passes the captured `RunAgent.workflow` to selected handlers.
`WorkflowAuthorityError` is reported as a hold, not charged as an infrastructure
death. Missing/stale/uncaptured authority tests assert no provider invocation,
no lifecycle write, and no infra charge.

### Workflow continuation and handoff

`app/workflows/fix.py` uses the SQL episode for selected resolve/resume session,
model, guidance, failures and authored checkpoint. Legacy selected writes are
removed. Accounting-owned result provenance is used across ROUTE, RESOLVE,
AUTHOR, IMPLEMENT, ADJUDICATE and selected RESUME. Runtime enums are normalized
before session persistence. Missing resolve evidence remains advisory for
unselected PR effort selection; selected paid authority is checked separately.

PR-present issue resume changes the actual paid context to `PR_RESUME` under
verified PR linkage, while issue follow-ups retain their separately captured
issue binding. It does not charge the resolve family for PR repair.

PR resume prefers its existing runtime-compatible legacy repair session, then
a compatible SQL resolve session. The scheduler uses the same presence rule.
The red-CI path uses the same session/config pair. After an actual PR-resume
result, its **new repair-owned** session/model are saved together; this does not
restore selected resolve file writes. `<runtime-default>` prevents a stale model
file from changing the transcript model on the next repair. Missing/incompatible
handoff neither consumes the human reply nor starts a paid run.

### Author applicability and runtime context

`domain.agent_task.run_input_fingerprint` hashes authoritative prompt, system
instruction and schema, excluding provider selection and cost. Accounting
captures it in admission metadata; resolve admission/replay validates it. An
authored checkpoint requires that receipt. Before reusing it and before paying
IMPLEMENT, the workflow rebuilds the current authoritative AUTHOR input and
compares fingerprints. Changed human answers require a fresh author.

`RuntimePlan.context_prefix` and `context_suffix` keep execution enrichment
separate from authoritative intent. Handover uses the prefix; repository
guidance uses the suffix. Providers still receive the rendered combined prompt.
This fixes both scratch-induced applicability drift and doubled handover text
from composing repository guidance over an already rendered prompt.

## Verification and review

The final broad checkpoint run is `make -k test`, so independent targets continue
after a failure. Log: `/tmp/phase3-checkpoint-full-test.log`.

**Final broad result: one known failing application test; all other targets passed.**
Domain: 1,364 passed. Adapters: 1,460 passed. Application: 2,394 passed, 1 failed.
Configuration, governor, residual registry, hooks, environment composition,
point-at, parity and all four import contracts passed. Both Pyrefly passes
reported 0 errors. Exit status was 2 because of the single application failure
named below. `git diff --check` is clean.

This command is diagnostic, not permission to commit a red tree. The last green
full suite belongs to committed `ee7de45`, with log
`/tmp/phase3-continuation-full-final.log`.

Focused frozen-source evidence includes:

- 372 phase-owned app/policy/control tests passed; nine-file Pyrefly: 0 errors.
- 214 GitHub adapter/world-composition tests passed.
- 32 durable composition and convergence tests passed, including the previously
  missing remote `needs-human-review` outcome and a ceiling-disabled unbounded
  loop control.
- 77 discovery/context/infra tests passed, including real insertion/evidence
  trigger failures, rollback, healthy-peer dispatch and retry after repair.
- 28 infra-storm/domain-characterization/budget-reply tests passed.
- Parent targeted integration retains the known post-run-read failure rather
  than weakening its assertion. Log: `/tmp/phase3-integration-current.log`.

Fresh independent review reproduced and prompted fixes for discovery failure
isolation, doubled handover enrichment and resolve-to-PR session handoff. The
discovery/context regressions were mutation-checked against the old behavior.
Composed testing separately exposed and corrected runtime enum serialization and
missing remote ceiling projection. **These bounded checks do not establish full
phase acceptance or full adversarial approval of the unfinished vertical slice.**

## Required next work — not started at this checkpoint

The enabled regression
`tests/app/test_dispatch.py::test_resolve_mid_dispatch_failure_persists_the_session_before_the_gh_read_fails`
fails because a paid result settles before `pr_for_issue`/`get_issue` completes,
but continuation is recorded only afterward. A remote read failure therefore
loses the returned session from durable workflow state.

Do not substitute unknown linkage with absence, finalize a provisional
BLOCKED/STAGE_PROGRESSED result, disable the regression, or simply pay another
leg. Final result deduplication would otherwise prevent correct later
classification, or the replacement would spend money without resolving the
known uncertainty.

The proposed smallest repair, reviewed but **not implemented**, is:

1. An immutable normalized returned-outcome receipt under the existing
   repository/run identity, using existing receipt storage. Retain original
   supplied issue inputs and the returned runtime/session/model needed by replay.
2. Capture real admitted/settled provenance and open an exact-run observation
   gate atomically. **Keep the pending session in the raw receipt; leave episode
   continuation and its admission watermark unchanged until final classification.**
3. Reconcile incomplete receipts through bounded, read-only fresh issue/linkage
   observations. One pure stage-aware completion decision must serve both the
   immediate path and recovery.
4. Atomically record one final classification, update continuation/strike once,
   close only the matching observation gate, and insert durable follow-up
   commands. No remote reads between final classification and durable commands.
5. Retire superseded receipts explicitly: no stale effects, new allowance or
   refund; retain the original receipt and exclude retired entries from scans.

Independent design-review constraints:

- Pre-capture session watermark advancement would stop final blocked RESUME
  from clearing the same run's session. Keep raw and finalized evidence distinct.
- IMPLEMENT `contract_change` still requires linkage observation: an actual PR
  takes precedence. Do not reset/adjudicate before verifying this.
- Preserve AUTHOR retry/block, productive and blocked RESUME differences,
  changed requirements, PR-first outcomes, infra/budget exclusions, and
  closed-without-PR cleanup in the shared completion decision.
- ADJUDICATE continuation also consumes verdict/rationale, uphold note and
  dispute history. A stage receipt alone does not prove split-workflow crash
  recovery; explicitly retain its minimal typed context or state the narrower
  scope. Avoid paying IMPLEMENT again merely to rediscover an existing objection.
- Capture after `_run_logged` protects subsequent observation failures, not
  process death between settlement and capture. State that boundary accurately
  and decide the required atomicity before implementation.
- Accounting can return an unsettled retained-reservation result. That must
  remain fail-closed, not mint continuation as if normal settlement succeeded.
- Reuse existing admission, receipt, gate and outbox infrastructure. Do not add
  another queue, independent pending flag, allowance or generic workflow engine.

Required tests: both post-run reads fail independently; restart yields one
classification/strike/command set without another paid leg; blocked RESUME clears
its session after replay; contract-change plus real PR never resets/adjudicates;
unrelated gates survive; stale generations retire without effects; unsettled
results remain held.

The parent owns shared core types, ports, codec/schema and cross-cutting seams.
Only after those are agreed should the phase agent implement policy/adapter/app
work. Operator re-arm/retry, scoped activation/import, complete legacy retirement,
full composed recovery and the second selected family remain unfinished. Live
soak requires separate release of the user's deployment pause.

## Agents and resumption conventions

- `phase_3_gates` (Astra): source frozen, stopped/interrupted after its handover.
- `review_routing`: completed; no active edits.
- `worktree_investigation`: completed read-only design review; no active edits.
- No new phase agents were started. Do not falsely complete the original
  six-phase goal or the phase-3 goal; they are intentionally unfinished.

On a future explicit resume: read `CLAUDE.md`, this handover, the phase plan and
ADR0015, then inspect the dirty diff. Preserve all changes. Use `uv`, Pyrefly and
`apply_patch`. Parent owns shared seams; bounded agents receive the phase plan's
conventions brief. Obtain independent adversarial review and a green complete
`make test` before committing. Never restart services to test this checkpoint.
