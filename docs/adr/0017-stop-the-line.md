# ADR 0017 — Fix a red base forward, first

Status: Accepted
Date: 2026-10-15
Governs: src/issue_agent/domain/policy.py src/issue_agent/domain/ci.py src/issue_agent/domain/state.py

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
