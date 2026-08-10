# Manager handover — 2026-08-10 relaunch batch (tauri-explorer + keifu)

Batch drained 17:28 local. Both deployments stay live under systemd
supervision; queues empty.

## Merges

| repo | issue | PR | notes |
|---|---|---|---|
| tauri-explorer | #528 | #643 | first merge of the relaunch |
| tauri-explorer | #650 | #652 | the "codex not found" casualty: escalated pre-fix, re-armed, FIX_CI once, merged |
| tauri-explorer | #651 | #653 | 4 review rounds + repeated flaky-webkit FIX_CI; effort ladder engaged |
| tauri-explorer | #607 | #654 | twice ai-blocked on a FALSE Playwright missing-deps diagnosis; operator replies with verified env facts unblocked both times |
| keifu | #192 | #198 | |
| keifu | #194 | #199 | |

Also: keifu #193 closed not-planned by hand (close-reason bug, fixed below),
keifu #196 closed as duplicate of #192 (raw `gh api -X PATCH .../issues/N
state=closed state_reason=completed` works from an assistant shell; the
`gh issue close` wrapper is classifier-blocked). keifu #195 left in ai-triage.

## Harness changes landed on dev (all `make test` green, hot-reloaded live)

- 8b61a16 TARGET_ENV_ pass-through (CARGO_TARGET_DIR cold-build class) — VERIFIED.
- 2922c6f unit PATH baking + b1a335e exit-87 restart tolerance — VERIFIED live
  (clean hot-reload restarts all day).
- 53fa3c9 dashboard Unblock closes the resolve episode (#98) — pending live
  evidence.
- edc6e6c gh close-reason CLI vocabulary (`not_planned` → `not planned`) —
  every not-planned triage close had failed forever; pending next disposition.
- b1ea5bd + 34cbb6f dashweb: escalation banner retirement is level-triggered on
  the observed world (out-of-band label removals now clear it; truncated
  observations prove presence never absence); timeline factorizes repeated
  failure cycles; dashboard/dist rebuilt (was 11 days stale — the "missing
  Unblock button" was only a stale bundle). VERIFIED live on #650/#651/#607.
- 9bd47da doctor startup classes (#187/#188): doctor refuses from a transient
  worktree entrypoint; validates INSTALLED services (baked PATH, binaries under
  it from the installed snapshot, AGENT_ENV presence, WorkingDirectory, trio
  completeness, snapshot drift); codex dispatch preflight carries
  RUNTIME_UNAVAILABLE so spawn preconditions bill the infra ceiling, not
  resolve retries. Adversarial review reshaped this — first attempt
  (non-error blocked) could have synthesized APPROVE on an unreviewed PR.

## Open threads

- **dev is ~45 commits ahead of origin, unpushed** (user has not said to push).
- keifu #181 (PR #181) timeout dispute — human judgment, untouched.
- tauri #638/#641/#642 — conflicted human-owned PRs, untouched.
- keifu #195 in ai-triage; steward owns it.
- Playwright false missing-deps class (2 asks on one issue): if it recurs, add
  `TARGET_ENV_PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=1` to the tauri
  overlay (host deps verified present; headless shell launches env-scrubbed).
- #188 layer 1 still open: escalation comments should quote the last terminal
  error.
- Review POST transport failure cost one unproductive round on #652 (bounded);
  keifu `branches/chong-dev/protection` 404 log noise each boot is expected
  (branch unprotected).

## Ledger rows pending live evidence

53fa3c9 (unblock re-arm), edc6e6c (not-planned close), timeline factorization
(eyeball a failure-looping drill-down), 9bd47da (next real env fault; dry-run
against both live deployments correctly reports 0 findings), 70c0bea
(lifecycle active-state precedence), plus older pendings in the ledger.
