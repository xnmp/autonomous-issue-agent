# Handover: tauri-explorer drain, 2026-10-15/16

Manager session. Goal: "after the current round of harness fixes merge,
supervise the harness through the remaining tauri explorer issues (except for
the ones that can't be done yet)". The harness round has merged. The drain ran
until every remaining item was waiting on the owner.

## State at hand-off

- **Daemons:** running (systemd user units, xnmp-tauri-explorer). The dashboard
  is on :8321. dev is at 3ea4c116 plus this doc, and is unpushed to main.
- **Routes (tauri):**

  | Tier | Route |
  |---|---|
  | low | codex luna:high |
  | med | codex gpt-6.1-sol:high (repo overlay, set from the dashboard) |
  | high | claude opus:medium |
  | xhigh | codex astra:high (shared.env) |

  Concurrency is 2.
- **Temporary drop-in:**
  `~/.config/systemd/user/issue-agent-fix-watcher-xnmp-tauri-explorer.service.d/zzz-leg-memory.conf`
  sets `MemoryMax=16G` and `OOMPolicy=continue`. Remove it when #470's
  per-leg memory bound lands.

## Tauri results

Merged (12 PRs):

| PR | For |
|---|---|
| #1178 | #1150 |
| #1180 | #1179 |
| #1184 | #927 |
| #1185 | #1081 |
| #1186 | #1010 |
| #1187 | #1100 |
| #1182 | #847 |
| #1189 | #1188 |
| #1190 | #1171 |
| #1191 | #1174 |
| #1192 | #1169 |
| #1193 | #1173 |

#1172 was closed by the owner's PR #1177.

**Spend** (journal `agent_run`, both roles, since 2026-10-15T11:21Z):

- **Total:** $115.13. By leg: resolve $86.54 (19), address-review $13.53 (10),
  review $8.69 (15), fix-ci $4.70 (3), steward $1.50, triage $0.18.
- **Cost per merged PR:** about $9.60 all-in. About $5.80 excluding the four
  items still open, which account for $44.60: #863 $27.64 + #1183 $13.12,
  #793 $2.48, #1170 $1.29, #1133 $0.55.
- **Outliers:**
  - #863/#1183: $40.76 for a Windows trash-inventory feature. It is still
    unmerged, at a workflow stop.
  - #1173: $13.69. The resolve hit the 3600 s wall. The PR was opened from the
    pushed partial work (publish-first, #427), then needed one fix-ci and was
    approved first pass.
  - #1100: $13.36.

## Waiting on the owner (judgment, not gaps)

| Item | What's needed |
|---|---|
| tauri PR #1181 (#793) | Approve the workflow-path stop: a FAT32 VHD diskpart step in `rust-platforms.yml`. |
| tauri PR #1183 (#863) | Approve the workflow-path stop: adds `files::trash_inventory::` to the Windows filter in `e2e-tauri.yml`. |
| tauri #1170 | Redeploy the website report relay to production Vercel; the deployed relay rejects MP4 ("Attachment type is invalid"). The error-message half is on branch `fix/1170-…`. |
| tauri #1133 | needs-human: remove only `launch-smoke` from dev's required checks. The workflow change is committed locally in the preserved clone `.claude/worktrees/issue-1133` (5ac48c05, unpushed). Replying on the issue resumes the leg. |
| tauri #1175, #1176 | Not readied. They edit the same workflows as #1133; ready them after #1133 lands. |
| harness #469 | Accept the cargo-xwin / MS SDK licence, or decline (no Windows SDK in the sandbox). |

Skipped as not doable yet: #1137, #1123, #1047, #983, #976, #936, #931, #781,
#710, #696.

## Harness changes this session

| PR | Change | Net src |
|---|---|---|
| #461 (#402) | base-CI streak issues; required workflows auto-ready | +96 |
| #463 (#440) | typed writes | +22 |
| #464 (#441) | one attempt rule | +49 |
| #465 | dashboard Settings: live knobs, no restart | +889 |
| #467 (#442) | one BaseHealth; refiling anchored on what ended the issue; orphan close | +111 |
| #468 (#448) | NeedsHuman/Wait per-reason variants | |
| #471 | AGENT.md edits the PR body via REST | |
| #473 (#472) | re-review after a description-only answer; verdict pinned to head + edit | |
| #474 (#470 root cause) | leg `/tmp`/`/var/tmp` on disk, not RAM tmpfs | +183 |

Notes on #474:

- Two independent review rounds.
- The full-disk crash points are tested.
- Verified live: the #1169 leg's bwrap binds `leg-tmp/<pid>-<ticks>/<run>`.
- Residual row added to residuals.md §2.

## Operator actions: judgment vs gap

| Action | Verdict |
|---|---|
| OOM drop-in on the fix unit | **Gap**. Root cause fixed in #474. Per-leg memory bound open as #470. |
| Med route set at repo scope (shared scope refused) | **Gap** #466: a stale `.active` of a non-running deployment blocks shared saves. |
| Held duplicate base-CI issue #1179 with needs-human | **Gap**, fixed in #467 (refiling anchor). |
| Readied #1169/#1173 only after #1174 merged (lag cluster serialised) | **Partly gap**. The ordering was a judgment, but GitHub "blocked by" links would have let the harness wait itself; prefer them next time. |
| Workflow stops, relay redeploy, branch protection, licence | **Judgment** (repo governance / external systems). |

## Open harness issues worth doing next

- #470: a per-leg memory bound; then remove the drop-in.
- #460: auto-ready a non-required base workflow's issue at 3 of its last 10 runs
  red.
- #466: a stale `.active` blocks shared-scope settings saves.
- #462: converge the gh failure classifiers.
- #458: multi-operator coordination by GitHub assignee, with a GitHub-built
  timeline.
- #459: the fixer runs affected tests.

Gap reports from blocked legs land as "Seen again" comments on #469, whose
title is generic. The #1133 report there was a judgment block, not a defect;
ignore it.
