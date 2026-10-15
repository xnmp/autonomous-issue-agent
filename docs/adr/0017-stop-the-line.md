# ADR 0017 — Fix a red base forward, first

Status: Accepted
Date: 2026-10-15
Governs: src/issue_agent/domain/policy.py src/issue_agent/domain/ci.py src/issue_agent/domain/state.py
  src/issue_agent/adapters/github_ci.py

## Context

A workflow failing on the base branch filed one issue per workflow for life
(#408), reopened when it failed again, and waited for a human to ready it.
Meanwhile #313 kept PRs whose failures were the base's waiting uncharged, and
branch protection kept them from merging (a PR's own CI includes the base's
breakage). What was missing was getting the base fixed fast (tauri-explorer,
2026-10-14). Three designs that also paused merges and dispatch behind a
latch, with a flake re-run, each drew new blockers in review: the pause added
risk and little the required checks did not already give.

## Decision

- **One base-CI issue per workflow per red streak**, from the base's
  completed push runs: `CI: <workflow> failing on <base> since <short head>`,
  named and keyed by the streak's first failing run. Green closes the open
  one. There is no reopen: once an issue filed for the streak is closed while
  it stays red (a human, or `Closes #` on a fix's merge), the first failing
  run that completes at or after that close files the next issue. The green
  close before a streak never holds it back, so whenever a workflow's latest
  completed run is red its issue is open or filed. An open issue whose
  harness PR merged (into a non-default base, where `Closes #` closes
  nothing) has ended once a failing run created at or after the merge has
  completed. That run files the next issue first; the ended issue is closed
  only on a later tick that sees the next issue open, so no write order
  leaves a red workflow without an open issue. A run
  created before the merge never tested the fix, so it neither closes the
  issue nor files the next one; if the merge's own run passes, green closes
  the issue as fixed. `needs-human` keeps the issue open: a human's reopen
  alone is not stable, since the post-merge failure that closed it persists
  and would close it again. An open issue from before (no
  `since`) counts as its workflow's current streak.
- **A required workflow's issue is the harness's to fix.** A workflow is
  required when one of its check runs on the base tip carries a context the
  branch's protection or rules require. Its open issue is readied
  (`ai-ready`), served ahead of everything else, and resolved without
  STEWARD, TRIAGE or a blocker. Its PR (by the issue number in its branch) is
  served first too and charged every base failure: it must fix them (with
  two red required workflows, another workflow's too). While the required workflows
  cannot be read, nothing is readied; the issue waits for a human.
  Non-required workflows file their issue as before and are not readied.
- **No pause, no flake guard.** Other work goes on; #313's `charge`/BaseRed is
  unchanged. A flaky required test that turns the base red is a defect the
  fix agent makes deterministic (#1165).
- **Human off-switch**: `needs-human` on the issue; the harness then does not
  ready it.

## Provenance and authority

Inputs: completed push runs on the base and the tip's check runs (GitHub
Actions, under the repository's control); the branch protection and rules
(repository admins); base-CI issues, kept only when `FIX_BOT` authored them.
New privileged effect: `ai-ready` on the harness's own base-CI issue of a
required workflow. Spend stays under the existing caps.

## Removed

The reopen path and `harness_closed` (and its close-event read); the operator
step of readying a red base's issue.

## Crash points

The filing is keyed by the streak's title, and act reads the token's own issue
listing before filing again, so a replay or a lagging search files once. The
close and the label are read back. A crash between any two re-derives what is
left next tick (`tests/domain/test_base_ci.py`, `tests/app/test_fix_role.py`).
A failed action does not stop the tick's later actions, so the close of a
merged fix's issue is never emitted beside the filing that replaces it: it
waits until that issue is observed open. If the filing is lost (an error, a
kill), the old issue stays open and the filing is derived again under its
key. If the filing lands and the close is lost, the next tick closes the old
issue and files nothing more. Closing first would strand the base: once
closed, the old issue drops out of the merged-PR read, and the post-merge
run completed before the close, so nothing would file.

## Amendment (2026-10-15, #442, #449): one base observation

Base health is one `BaseHealth` value per tick. Its parts are still read
apart, because they answer different questions: the tip's check rollup
(job-level, what #313's `charge` matches a PR's failures against) and the
push-run listing (run-level history, what streaks are cut from). They now
identify a failure the same way:

- An Actions job on the tip is base evidence only from a **push** run that is
  running or ended red or green. On a fast-forwarded tip the rollup also
  lists the PR's `pull_request` runs, and a cancelled run's aggregate jobs
  report FAILURE where the listing has no verdict (tauri-explorer `156de47`,
  2026-10-15). A settled BaseRed on an Actions job is so backed by a red
  push run, hence an issue; while that run (or its re-run) is in flight the
  issue may not be filed yet. Accepted trade-off: a job that really failed
  before its run was cancelled is the PR's, and a repair may be paid for it.
  Commit statuses and other apps' checks are charged as before and have no
  issue.
- **Rule 1: green closes only when settled.** The harness closes a streak
  issue on green only when that green run is the workflow's newest-created
  run on the base: no run created after it is queued, running or being
  re-run (the listing keeps those, without a verdict). Otherwise it waits
  for the newer run. Before, a re-run dropped out of the completed listing
  and an older green closed the issue; and a green finishing before an
  overlapping, newer red run closed it too.
- **Rule 2: every close anchors the same way.** After any close of a streak
  issue (the harness's green close, `Closes #`, a human, the merged-fix
  close; a legacy title too), a red run files the next issue only if it was
  created at or after the last close, or its head is the commit that closed
  the issue (a merged PR's merge commit, or a pushed commit, read inside the
  issue search: GitHub may date the close after that commit's run was
  created). Rule 1 means no newer run existed at a green close, so the close
  time is the right anchor for every kind; this supersedes the
  completion-time rule above. Incident: tauri #1150, a legacy issue closed by
  #1178's merge at 11:55:46Z; the next tick refiled the streak as #1179 from
  a run of `e6daf55`. A human's close of a red streak waits for a run created
  after it, not a re-run of an older one.
- An open base-CI issue whose workflow is neither in the listed runs nor
  among the repository's active workflows is **closed** (renamed, removed or
  disabled), unless it carries `needs-human`. The 100 listed runs span hours,
  so a path-filtered workflow is often absent from them. A workflow listing
  that names a workflow apart from its runs on the base (a rename not yet on
  the branch the listing reads) is unread, and unread workflows close
  nothing.

Provenance: the workflow listing (`actions/workflows`) is the repository's
workflow files, under write access. The close is read back like the green
close and re-derived from the next observation if lost.
