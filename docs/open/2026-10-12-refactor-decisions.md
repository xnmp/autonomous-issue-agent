# Refactor decisions (answers to the audit's questions)

These answer the questions in `/tmp/refactor-audit.md` §5. Each answer is the minimal option, chosen by the manager under the refactor goal. Items marked **owner** are flagged for the owner, who may overturn them; work proceeds on the stated default.

1. **Harness PR identity.** A PR is the harness's if its head branch matches the existing `fix/<issue>-` pattern. There's no new prefix and no cutover change. Author can't be used, because FIX_BOT is the operator's own login on tauri.
2. **Review loop bound.** Count the bot's CHANGES_REQUESTED reviews per PR, read from GitHub. At 3, apply `needs-human` (a dispute). Nothing is stored.
3. **Attempt keys.** Count per `(item, action)`: the harness legs that ended as agent attempts since the latest *reset event* seen on GitHub. A reset event is a non-harness comment, review or push. This keeps FIX_CI bounded, since harness pushes don't reset the count.
4. **Attempts storage.** Derive from the `legs` table, with no attempts table. **Tables: 3**: legs+money, leases, idempotency. The design page is updated.
5. **Money at cutover.** The legs and money ledger is kept. Only workflow tables are dropped, so daily and lifetime spend carry over.
6. **SQLite-only stops at cutover.** Drop them. They are non-judgment blocks by construction. At deployment, the manager strips the old hold labels and re-applies `needs-human` only to items with a genuine judgment kernel (today, #696).
7. **Spend stops.** Hitting the daily or global cap is a silent wait, because time clears it. The per-issue lifetime cap gets `needs-human`, because a grant is a judgment.
8. **Reply detection.** A reply is a comment newer than both the harness's `needs-human` comment and the end of the last leg, and not written by the harness. Harness comments carry a hidden marker. Removing the label also releases the item.
9. **Processes.** Keep the two roles, fix and review. They have different GitHub identities, and review independence matters. The steward goes.
10. **Merge.** Merge directly with `--match-head-commit` when the PR is approved and green. If it's BEHIND but not conflicted, and the branch protection is strict, call GitHub's update-branch (free; no agent).
11. **Publication.** The agent pushes its branch with git. The harness opens or updates the PR itself, idempotently. The durable publish broker is deleted.
12. **Runtimes.**
    - GLM (`claude_profile`) is deleted.
    - Codex native sub-agents are disabled and their child-usage accounting deleted. Independent review is its own leg.
    - **Owner:** the ROUTE classifier and difficulty ladder are deleted, leaving one configured model per role. This removes a cost lever (luna for trivial issues). Revisit once there is cost data.
13. **Extras: all deleted.**
    - Blocked-by dependency holds. **Owner:** keifu used these.
    - Path-overlap holds and predicted paths.
    - The duplicate-PR guard. It is replaced by the existence of a branch-pattern PR.
    - CI free rerun, cancel-stalled and the pending deadline. Flakes are the repo's to fix.
    - FIX_MCP_CONFIG and CI-history prompt context.
    - Multi and delegated review.
    - External-PR review (`ai-review`, adoption).
    - P1–P3 ordering. Issues are taken FIFO by number.
    - Webhook wake and hot reload. Deploys restart through systemd.
14. **Slice order:** 2, 3, 7, 6, 8a, then 5a–5d (with the two-label contract built in), then the slice 4 residue, then 8b, then **9 (adapter trim, about −5.4k)** to reach 15k.
15. **Metrics** are computed from `legs` plus GitHub. The schema-v2 events and outbox go; the journal remains for logs.
16. **Systemic faults.** Per-item exponential backoff, derived from `legs`, plus the money caps. No global breaker.
17. **Deployment fencing.** Only `single_instance` stays.
18. **ADRs.** Supersede 0004, 0009–0016 and part of 0001. Keep slimmed 0002, 0003 and 0005–0008. Rewrite CLAUDE.md's "durable commands and receipts" architecture rules to the design (**owner**, because it changes the repo's standing instructions).
19. **Dashboard writes.** Park means `needs-human`. Priority labels are gone. The provider usage meters remain dashboard-side, outside the core budget.
20. **Daemons stay off** for the whole refactor, until the owner says otherwise.
21. **Attempt runs.** Attempts for an act are the tail run of its agent legs after the reset floor. Machine legs are skipped, not run-breakers. Any other act's agent leg ends the run, so alternating REBASE/REVIEW on a churny base never caps. REVIEW counts only legs on the current head; reviewing a new head is progress. `LegEnd.head` carries the dispatch head, read from the run's `dispatch_id` JSON (no new column).
22. **Human CHANGES_REQUESTED.** A non-harness reviewer whose latest APPROVED/CHANGES_REQUESTED/DISMISSED review is CHANGES_REQUESTED blocks merge. On the current head it dispatches ADDRESS_REVIEW (an attempt, not a bot round). On an older head the bot still reviews the new head, but approval waits instead of merging. A later approval or dismissal by the same reviewer clears it.
23. **Review rounds since reset.** Bot CHANGES_REQUESTED reviews are counted since the latest reset event (Q3), including a released `needs-human` comment. A lifetime count would re-stop a PR the moment a reply released it.
24. **Push provenance.** A push is read from the head commit's author login and date in the observe GraphQL query. A push by a harness login is not a reset event.
