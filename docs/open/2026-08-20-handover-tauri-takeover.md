# Manager handover — tauri-explorer takeover, 2026-08-20

Managed queue drained at 01:39Z (11:39 AEST). The fix, review, steward, and
dashboard services remain active under systemd. At handover there are no open
`ai-ready`, `ai-pr-open`, `ai-blocked`, or `needs-human-review` items.

## Outcome

| issue | PR | outcome |
|---|---|---|
| #672 recycle bin on Arch Linux | #673 | New report groomed, classified as a recurrence of #660, routed to the capability ceiling, implemented test-first, approved on round 1, and merged as `3c1a121` |
| #666 CSV previews | #667 | Seven-day inherited block recovered; rebased, repaired after fresh CI exposed shared-fixture interference, independently re-approved, and merged as `5f3c506` |

Both issues closed `COMPLETED` and the harness removed their worktrees and
lifecycle labels/state. Remote `dev` ended at `5f3c506`.

- Merges: **2/2** managed items.
- Spend: **$10.071678**, or **$5.035839/merge**, across eight nonzero-cost
  legs. The six Claude steward attempts terminated on quota before useful
  output and recorded $0.
- First-pass approval: **1/1** for the newly opened PR (#673). PR #667 arrived
  already approved; its repaired head was approved by its first takeover
  re-review, so it is not counted as a new-PR first-pass sample.
- Wall time: #672 issue-to-merge **77m32s**. #666 full issue-to-merge
  **6d23h54m07s**; takeover guidance-to-merge **2h12m50s**.
- Routing escalations: three — two effort notches on #666 and one
  capability-ceiling route on recurrent issue #672.
- Recurrences: **1** (#672 against #660).
- Harness issue delta: **+1 open, 0 closed** (#193).

| paid leg | runs | cost | mix |
|---|---:|---:|---:|
| PR_RESUME | 1 | $0.985409 | 9.78% |
| GROOM | 1 | $0.189422 | 1.88% |
| TRIAGE | 1 | $0.355917 | 3.53% |
| ROUTE | 1 | $0.013945 | 0.14% |
| RESOLVE | 1 | $5.842278 | 58.01% |
| REVIEW | 1 | $0.473353 | 4.70% |
| FIX_CI | 1 | $1.681078 | 16.69% |
| RE_REVIEW | 1 | $0.530276 | 5.27% |

The $/merge result is slightly below the 2026-08-08 batch's $5.49, but this
takeover excludes #667's work before the window and the sample is only two
merges. The 100% first-pass rate applies to one new PR, so it is evidence of no
regression, not a trend.

## Supervision record

### PR #667 recovery

The inherited head was approved but `ai-blocked` after frontend and WebKit CI
timed out. Current `dev` and PR #671 had passed the same long suites, so the
manager posted scoped proceed/rebase guidance. PR_RESUME pushed `6f712db`; one
free rerun requested runs `32313416186` and `32313416190`, but the long suites
again cancelled.

After PR #673 moved the base, FIX_CI found PR-attributable interference: CSV
fixtures had been added to the shared `/home/user` mock directory consumed by
unrelated browser tests. It isolated them under `/home/csv-preview` and
removed incidental formatter churn. On head `2c931ebb`, frontend passed in
16m54s and WebKit passed in 31m35s. A separate Ubuntu watcher assertion failed
once; the harness used its one free same-head rerun (`32319150352`), the run
passed, and `ci.flake.detected.v1` fired at 01:38:49Z without another FIX_CI
leg.

### Issue #672 path

Initial grooming suggested #660/#662 might already resolve the report. The
attachment showed v1.8.1 still failing on `trash:///`, so the manager recorded
that product distinction and cleared the provisional labels. Codex triage
then marked the report ready and recurrent. The live ceiling event at
23:38:03Z carried `recurrence=true`, `ceiling=true`, `standard`→`complex`,
`model=gpt-5.6-sol`, and `to_effort=high`. The resulting fix opens the absolute
Freedesktop Trash `files` path through `xdg-open`; its tests, docs, and ADR
merged on first review.

## Operator actions: judgment versus gap

| action | classification | disposition |
|---|---|---|
| Replied to #667's stale CI block and authorized a fresh-base retry | Gap; no irreducible judgment kernel. Base health, check age, and bounded rerun/repair are machine-observable | Existing #172 tracks flake classification and bounded recovery |
| Determined that #672's versioned screenshot was a distinct recurrence, not evidence of resolution | Judgment. The kernel was product/evidence interpretation across the prior fix and the user's observed release | Decision recorded on the issue; the harness performed subsequent triage/routing |
| Switched the quota-exhausted steward from Claude to Codex and reinstalled service snapshots | Gap; provider availability and config propagation are machine-observable | Live evidence added to #129 (quota handling) and #165 (overlay/XDG snapshot split) |
| Restarted the idle fix watcher once to test its earlier failed stop | Gap diagnostic. The requested drain reproducibly exited status 1 | Filed #193 with the journal evidence; the replacement watcher is healthy |

The temporary `STEWARD_RUNTIME=codex` overlay knob increases operator surface.
It is only a failover until #129 supplies automatic, provenance-journaled
provider handling. The service reinstall was necessary because installed
units consume the private XDG snapshot rather than the workspace's composed
env; #165 is the structural fix.

## Harness gaps and live-verification verdicts

- [#129](https://github.com/xnmp/autonomous-issue-agent/issues/129): Claude
  returned HTTP 429 while surfacing a nominal success envelope, producing
  three verdict-less strikes before escalation. Live evidence was added.
- [#165](https://github.com/xnmp/autonomous-issue-agent/issues/165): editing
  the repo overlay/composed env did not affect installed services until the
  official `install-service` path refreshed the XDG snapshot. Live evidence
  was added.
- [#193](https://github.com/xnmp/autonomous-issue-agent/issues/193): a clean,
  idle fix-watcher restart reproducibly enters `draining` and exits status 1.
  Systemd successfully starts the replacement.
- Field ledger #62 is now VERIFIED by #672's `ceiling=true` recurrence route.
- The Codex instruction-budget row is VERIFIED against #672's stored prompt
  and generated config; its old `AGENTS.override.md` artifact was superseded
  by the provider-neutral prompt boundary.
- Dashboard-under-systemd remains pending overall: the unit is installed, and
  its exit-87 source hot-reload was verified immediately after this handover
  commit; host reboot remains unverified.
- The existing free-rerun verdict was reconfirmed by #667: both requests
  carried non-empty run IDs, and the same-head green rerun emitted the flake
  event without paid repair.

## Loose ends

- Remove the temporary `STEWARD_RUNTIME=codex` override after the reported
  Claude quota reset at **2026-08-22 18:00 AEST**, or when #129 supersedes it.
- Five older `ai-triage` issues remain steward-owned: #610, #531, #526, #523,
  and #514. They are not in the managed drain set until promotion.
- Open PRs #668/#669 are Dependabot-owned; #638 is human-owned; #634 remains
  `ai-parked`. None was adopted or modified.
- The target main checkout was deliberately not pulled over unrelated user
  work. It remains at `0f25eb7` with modified `AGENTS.md` and untracked
  `docs/AI-native-ideas.md`, `docs/gotcha-study/`, and
  `screenshots/_issue-refs/`; remote `dev` is `5f3c506`.
- All installed services are active. A requested fix-watcher stop still
  records failure until #193 is resolved, despite a healthy replacement.
- This docs-only handover commit is local to harness `dev` until the operator
  chooses to push it.
