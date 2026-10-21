# ADR 0018 — A PR is the harness's by the act layer's record

Status: Accepted
Date: 2026-10-21
Governs: src/issue_agent/domain/state.py src/issue_agent/adapters/act.py src/issue_agent/adapters/github.py
  src/issue_agent/app/tick.py src/issue_agent/domain/world.py

## Context

Refactor decision Q1 (2026-10-12) made an open PR the harness's when its head
branch matched `fix/<issue>-`. The author could not be used because FIX_BOT was
the operator's own login on tauri-explorer. A branch name is something anyone
with push access writes, and GitHub keeps no record of who chose it. The
harness repository had two PRs the operator opened by hand on `fix/<n>-`
branches (#145, #128), and the harness treated them as its own: it would have
rebased, repaired, reviewed and merged them. A fork's PR could use the same
name. Identity also tied workflow to naming, so the harness could not use other
branch prefixes. The owner wants identity to work with any account.

On GitHub, every field that does not depend on the account (branch, title,
body, labels) can be written by a collaborator, and the author is just the
token's account. The one fact nobody else can produce is that the harness
opened the PR, which the act layer does (Q11: the agent pushes, the harness
opens).

## Decision

- **The record.** `GitHubAct.open_pr` records the head branch in the control
  store's `idempotency` table (`fix-pr:<repository>:<branch>`, confirmed)
  **before** it opens the PR. Derive gets these records as `Local.owned`
  (`adapters.act.owned_branches`, read in `app/tick.Facts` for both roles and
  the dashboard). An open PR is a harness item only if its branch is recorded.
  The branch name now only says which issue the PR works on.
- **Adoption.** An open `fix/<n>-` PR with no record, whose head is a commit a
  harness leg on its issue or on it pushed (`LegEnd.end_head`, read by the
  harness from the remote, and different from the leg's start head), is the
  harness's own publication. That covers a PR opened before records existed
  and one a leg opened itself (the capability matrix allows `gh pr create`).
  The PR item derives `OpenPR(branch, head)` on its own, whatever its issue's
  state. The act layer records the branch only if the open PR is still at that
  head, and opens nothing. A leg that pushed nothing, such as a review, never
  adopts. Adoption exists because the alternatives are a hand-run migration at
  every cutover or a silent stall whenever a leg opens its own PR. It holds no
  state of its own, and once the PR is recorded it never fires again.
- **A stale publication never claims an existing PR.** `open_pr` looks for an
  open PR first. If one exists and the action carries no judged head, or the
  head has moved, the act layer logs `PR #N on <branch> is not the harness's;
  not recorded` and writes nothing. An adoption whose PR has closed since
  opens nothing. Otherwise it records, then opens.
- **The record precedes the open, by choice.** If a human opens a PR on the
  branch in the instant between the lookup and the harness's POST (GitHub
  refuses the POST), or after a crash between record and POST, that PR counts
  as the harness's. That is the exposure a human push onto a harness PR
  already has (pushes are accepted and reset attempts; review and required CI
  still gate the merge). The alternative, recording after the open, would
  leave a PR whose open answer was lost unrecorded and, after a capped
  RESOLVE, undriven.
- **Everything else is not the harness's.** An unrecorded, unadopted `fix/<n>-`
  PR still shadows its issue, just as a merged one does: the harness does no
  work on an issue that someone else's PR is working on.
- **Forks are dropped at the boundary.** The open-PR read and the act layer's
  lookup by branch both ask for `isCrossRepository`, and a PR that is not
  exactly `false` (a fork, or unknown) is never observed or found. A fork's
  branch can carry a recorded name, and the harness cannot push to it.

## Provenance

`Local.owned` is harness-owned: only the fix role's act layer writes it, in
SQLite, which is the authority for idempotency keys (ADR 0003). `end_head` is
the harness's read of the remote after a leg it ran. A human who pushes the
same commit to a branch of the same name gets the harness's content adopted,
which is harmless.

## Crash points

| Point | State left | Recovery | Test |
|---|---|---|---|
| Before the record | pushed branch, no PR, no record | derive owes `OpenPR` (unchanged) | `test_state_policy` publication tests |
| After the record, before the open | recorded branch, no PR | the branch is still observed with no PR, so `OpenPR` is owed again; the record is replayed | `test_the_record_lands_before_the_open_so_a_crash_mid_open_leaves_it`, `test_a_recorded_branch_whose_pr_never_opened_is_opened_again` |
| GitHub opened it, answer lost | recorded branch, open PR | the PR is an item; a replay on a stale observation finds the record and does nothing | `test_a_lost_answer_replayed_on_a_stale_observation_opens_nothing_and_says_nothing` |
| No record at all (cutover, a leg's own PR) | open PR, head pushed by a harness leg | adoption on the PR item | `test_an_unrecorded_pr_whose_head_a_resolve_leg_pushed_is_recorded_at_that_head`, `test_an_unrecorded_pr_whose_head_a_pr_stage_leg_pushed_is_recorded`, `test_adoption_does_not_depend_on_the_issue_being_an_item`, `test_an_open_pr_at_the_judged_harness_push_is_recorded_and_not_opened_again` |
| A human opens or pushes the PR between observe and act | open PR not at the judged head | not recorded, logged | `test_an_open_pr_someone_else_opened_or_pushed_is_never_recorded` |

## Consequences

- An unrecorded PR whose head no harness leg pushed is no longer driven: a
  human's `fix/<n>-` PR, or a harness PR that lost its record and was then
  pushed by someone other than the harness. This fails safe (no push, no
  merge). Its issue stays shadowed while it is open.
- The record is keyed by branch, not PR number, so it exists before the PR
  does. A PR a human later opens from a branch the harness published counts as
  the harness's. Its content is the harness's push, so that is correct.
- Accepted residuals, each failing safe (nothing pushed or merged):
  - A leg that opened its own PR and then ended as a machine end, or a store
    lost after a push from someone other than the harness, leaves an unrecorded
    PR nobody drives. Its issue stays shadowed. `leg_end` keeps `end_head` only
    for agent ends, and adoption reads only that. Exit: a human re-opens the PR
    from a harness push, or the issue is worked again once the PR closes.
  - An issue leg starts on no head. A RESOLVE that pushed nothing onto a
    branch someone else pushed (before or during the leg) reads as that
    commit's push, so an unrecorded PR at it is adopted and driven like a
    harness PR, review and required CI still gating its merge. It takes a
    human writing to the harness's own branch, the same exposure as above.
  - A pre-cutover harness PR whose head no leg pushed (last moved by the
    harness's free update-branch, or by a human) is not adopted, and its issue
    stays shadowed. Cutover check: for each deployment, every open `fix/<n>-`
    PR's head must match a leg's `end_head`, or its record is written by hand
    (`fix-pr:<repo>:<branch>`, confirmed). At this cutover ballast #86 and #82
    matched, and tauri-explorer had none open.
  - Operator park and unblock (`adapters/operator_effects.py`) and the
    dashboard's done cards still find PRs by `fix/<n>-` name. A `needs-human`
    write on a human's PR at the operator's own click, or a human merge shown
    as done, is operator-initiated or display only; no derive decision reads
    either. Exit: when either path is next
    touched, read it from the record.
- Cutover needs no migration for open harness PRs whose head is still a leg's
  push: they are adopted on the first tick (the cutover check above covers the
  rest).
- Surface: one `Local` field, one store read, a record and a head check in the
  act layer on an existing table, one adoption rule in derive, and one fork
  check at two boundaries. No new table, label, counter or lifecycle state. The
  durable record is new state, kept in the table that already holds idempotency
  keys. It removes the branch name's role as identity, and with it the old Q1
  constraint that FIX_BOT must differ from the operator for identity to be
  sound.
