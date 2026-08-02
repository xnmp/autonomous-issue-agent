# Running notes for final report (structural vs incidental)

## Harness defects found

1. **Steward runtime hardcoded to claude** (roles.py:2525-2536, no STEWARD_RUNTIME).
   Class: STRUCTURAL — runtime selection was bolted on per-role (fix binder table,
   review binder table, steward forgotten). No single "role → runtime" seam; each
   role re-implements binding. Fix in flight (Opus subagent): STEWARD_RUNTIME +
   binder mirroring fix/review.

2. **Dashboard/status blind to daemons' journals for hand-written overlays**
   (cli.py:1741 journal_paths fallback `<role>-<name>` vs config.py:1414
   _default_state_dir `<role-dir>/<repo_slug>`).
   Class: STRUCTURAL — duplicate derivation of the same path in two layers with
   different fallbacks; only agrees when synth_repo_env wrote explicit keys.
   Observed: dashboard "today $0.57" (SQLite, correct) alongside "no runs / 0
   events" (JSONL, wrong path). Symptom class: telemetry split-brain.

3. **Steward SDK leg ERRORED on 2026-08-01 canary** (error_during_execution,
   stop_reason=tool_use, aborted_streaming, 19 turns, 88s). Single occurrence.
   Class: TBD (likely INCIDENTAL runtime-SDK flake; path retired if steward moves
   to codex). Watch for recurrence.

4. **Dashboard north-star gap**: probe hit rate + predicted-path hit rate are in
   CLAUDE.md's primary metrics but not implemented in domain/dashboard.py or
   dashweb. Class: aspirational/not-yet-wired, note only.

## Environment facts
- Box clock is UTC; local ~UTC+10. "Today" boundaries in dashboard are UTC.
- $0.57 settled spend on 2026-08-01 = prior canary (incl. errored steward leg).
- Base chong-dev CI green as of start. CI matrix includes macos/windows jobs the
  local gate cannot reproduce (accepted risk, CI_IGNORE_CHECKS available).
- codex has no native budget flag; harness enforces via streamed rollout usage.

## Timeline
- 00:26 local Aug 2: first `issue-agent start` reached daemon launch; stopped
  cleanly pre-triage to land STEWARD_RUNTIME first. No paid legs fired.

## Fixes landed (pending adversarial review before commit)
- STEWARD_RUNTIME knob (Opus subagent impl): binder registry, per-runtime model
  fallback, cwd threading (codex containment requires explicit cwd), jira-sync
  inert under codex (CodexRunner has no MCP), accounting labeled with real
  runtime. All suites green (domain 696, adapters 880, app 1448+, hooks 92,
  import-linter 4/4, pyrefly 0).
- journal_paths fallback (mine): read-side now derives via new public
  config.default_state_dir(REPO, role_dir) when overlay lacks explicit keys;
  synth `<role>-<name>` only when REPO absent too. Live validation: dashboard
  went from "0 events/no runs + $0.57" to "20/20/30 events, TRIAGE $0.57
  (100%, 1)" — split-brain resolved.
- Subagent-discovered latent defects (would have burned paid legs):
  (a) codex steward would crash at first dispatch on missing cwd;
  (b) jira-sync under codex would run without its MCP server (silent, paid).
  Class: STRUCTURAL — capability assumptions (MCP, cwd containment) are
  per-runtime but were implicit in the claude-only steward path.

## Adversarial review round 1: FAIL (2 blockers, 1 major) — all pre-deployment catches
- B1: read-only governor profile is reviewer-shaped, not role-shaped; triage's
  instructed tool surface (gh issue list/search, rg, git log) all exit 2.
  Class: STRUCTURAL — capability profiles hardcoded per consumer, no
  role-parameterisation seam in the shared gate stack.
- B2: binding.ready only gates the LOG LINE; TARGET_DIR-less codex steward
  dispatches paid legs on a bare ungoverned CodexRunner while logging
  triage=off. Class: STRUCTURAL — same "advisory signal not wired to
  authority" pattern as the governor-is-advisory residual; readiness must gate
  capability, not narration.
- M3: codex _strict_schema forces optional fields required, inverting the
  documented anti-fabrication design of GROOM/TRIAGE schemas (classifier under
  pressure invents matches that pass the whitelist guard → false recurrence
  labels pollute the recurrence guardrail). Class: STRUCTURAL — schema
  transforms are a per-runtime cross-cutting concern applied blind to schema
  intent.
- Also: STEWARD_TRIAGE_MODEL="" semantics drift (minor), journal_paths still
  blind if config lives one layer up in shared.env (minor — extends fix B),
  TARGET_DIR-typo not caught at boot (minor).
- Meta: all three serious findings are invisible to the full green test suite
  (4k+ tests) — they live at seams between components each side tests in
  isolation (governor vs planner tool surface; log line vs dispatch config;
  schema transform vs schema intent).

## Live deployment defects (post-restart, all-codex posture)
5. **Bot PAT lacks Issues:Write** — every steward GitHub write 403s (labels,
   comments; GraphQL and REST). Reconstructed PAT (2026-08-01) narrower than
   the lost original. Doctor WARNED (fine-grained scopes unreadable) but only
   a warning; collaborator check passed. Class: INCIDENTAL (credential), but
   exposes STRUCTURAL gap: no write-probe in preflight for the review
   identity (probe idea: create+delete a repo label — exercises Issues:Write
   without touching issues). Operator notified via push. Admissions held:
   same missing scope would break reviewer PR comments → PRs would pile up
   unreviewable.
6. **Unbounded remote-write retry loop** — decide re-emits denied
   Comment/Label actions every 20s tick; 175+ failures, no backoff/ceiling/
   escalation journal event. Violates ADR 0012's own rule. Class: STRUCTURAL —
   reconcile-loop design treats every failure as transient; no "persistently
   denied" classification. Fix in design.
7. Triage leg #34 (issue 34, vague scope) burned full $0.75 → error, "will
   retry" — checking ceiling. Other 3 triage legs OK at $0.13-0.28: codex
   triage viable. Verdicts held locally when GitHub writes fail — pipeline
   degrades to operator-driven admission (good design, previously unexercised).

## Retry-path map (explorer findings, file:line in transcript)
- Denied gh writes → EffectStatus.RETRYABLE_FAILURE durable commands, replayed
  EVERY tick by drain_pending_commands; attempts column tracked but never
  compared to a ceiling; PERMANENT_FAILURE unreachable from this path; bare
  except-continue swallows drain errors. Class: STRUCTURAL — outbox pattern
  implemented without its poison-message half.
- Triage verdicts are NOT first-class durable state: content lives only in the
  pending comment commands + log; "awaiting human promote" is a log line.
  set_triage_at marker skipped on error paths → budget-exhausted triage
  re-buys a full-price leg per sweep, unbounded (budget_exhausted deliberately
  not an infra death; no strike counter for steward legs). Class: STRUCTURAL —
  the resolve family got budget_actions/NEEDS_BUDGET_REVIEW; steward legs
  never inherited the pattern (same class as STEWARD_RUNTIME omission: role
  added later, cross-cutting pattern not applied).
- issue-agent ready/triage CLI run under OPERATOR gh auth (operator_effects,
  identity repo:operator:channel) — independent of bot PAT. Admission path
  clear once verdicts reviewed.
- On PAT re-mint: pending outbox commands (labels + proposal comments) will
  drain and post — self-healing, EXCEPT the unbounded retry meanwhile.
Fix batch A/B/C delegated (effect retry ceiling + escalation; triage budget
strike ceiling; doctor live write-probe via create+delete repo label).

## Triage sweep results (all-codex steward, $5.57 incl. canary+retry, 18 legs)
- 7 ready: 16, 18, 25, 143, 159, 161, 162. 9 needs-clarification: 34, 124,
  125, 126, 130, 132, 136, 142, 157. #124 recurrence_of #119 / resolved_by
  PR #150. Verdict quality looks strong (checked against issue bodies).
- Cost: $0.11-0.69/leg successful; one budget-error retry (#34). Codex triage
  ≈ 2-4x cheaper than $0.75 cap on most issues.
- Heartbeat "woke after 84-108s" 3x — box load during codex legs + subagents;
  benign so far, watch.
- Verdicts extracted from outbox (34 retryable_failure commands, 16 issues,
  all comment+label pairs intact) — outbox-as-verdict-store worked, but only
  because the incident left commands unposted; NOT a design guarantee
  (verdict content has no first-class durable home; report item).

## Second cascade + verdict quality findings
8. **Rate-limit cascade**: 175+ denied GraphQL writes exhausted xnmp-bot's
   hourly GraphQL quota → doctor's GraphQL-backed read check misreported
   "cannot access — invite as collaborator" → restart blocked with WRONG
   operator instruction. Fixed (08219fb): REST read + rate-limit-honest
   message + test. Class: STRUCTURAL (two instances now of "diagnostic
   conflates causes": doctor collaborator-vs-scope, doctor access-vs-quota).
   Taxonomy note: rate-limited writes classify as infra → unknown_outcome →
   correctly exempt from the refusal ceiling (round-2 design validated by
   real incident within the hour).
9. **#124 recurrence false positive**: classifier flagged recurrence_of #119 /
   resolved_by PR #150 — semantically unrelated (signature-status docs vs
   graph artifacts). In-set fabrication: whitelist membership ≠ semantic
   match. Bounded to advisory noise by design (label removable). Occurred
   WITH the nullable-schema fix live. Guardrail impact: recurrences metric
   polluted by 1. Gap: no operator cancel primitive for queued advisory
   writes — post-hoc cleanup only.
10. Steward questions address the ISSUE AUTHOR (= operator here, xnmp).
   9 issues blocked on product/repro input only the user can give — correct
   harness behavior, autonomy floor reached. Decision sheet for user in
   final report.

## Batch-run findings (post-restart, PRs flowing)
11. **Fix-role admission rejection loop** (filed harness #155): #16 resolve
   $5.86 + $20 address-review reservation > $25 issue cap → per-tick
   rejection loop, no escalation, deterministic. Systemic: any resolve >
   (cap - max charge) strands on first REQUEST_CHANGES. Workaround:
   ISSUE_SPEND_CAP_USD=35 in overlay. Class: STRUCTURAL — third instance of
   "loop without ceiling/escalation" (outbox retries, steward stalls, now
   fix-role admission); budget_actions exists but only covers leg outcomes,
   not admission. Handbook followed: filed, not self-modified (ai PRs open).
12. **Router budget claude-shaped**: ROUTER_BUDGET_USD=0.10 default killed
   2/2 codex route legs (~$0.18 settled each, tier=unknown = paid, no
   classification). Overlay now 0.50. Class: STRUCTURAL pattern — budget
   defaults assume claude economics; codex synthetic pricing needs
   per-runtime defaults (same family: triage $0.75 nearly too small for #34).
13. First PR: #171 (issue #16) opened at $5.86; codex review r1
   REQUEST_CHANGES in 39s at $0.17 (rejection 1/4). Review pipeline works
   end-to-end on the classic PAT.
