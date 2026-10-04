# Phase 5 unattended-window proposal

Status: prepared for review; **not authorized or started**. The architecture
plan's Protected window still applies. This document is not a deployment
command or permission to activate live data.

The existing [48-hour protocol](soak-protocol-unattended-48h.md) supplies the
no-touch rule and halt adjudication. The new measurement export supplies
prospective observations, exposure and coverage. Controlled replays do not
substitute for these windows.

## Proposed scope and cost bound

Use `xnmp/tauri-explorer` on Linux/x86_64 with the configured `dev` base,
Codex runtime, `gpt-5.6-sol` implementer and `webkitgtk` evidence stack.
The retained target test command is
`set -e; bunx vitest run && (cd src-tauri && cargo test --quiet)`.
Interactive macOS and Windows qualification remain outside this profile.
Freeze and record the resolved configuration, all role models/runtimes,
review mode, harness commit and target starting commit before admission.
Those facts must be checked against the resolved configuration; this proposal
does not treat a repository overlay alone as the deployed profile.

First run one bounded canary tick with the accepted migration and supervision
preflight. Then run two 48-hour windows, starting at **00:00 UTC**, with
readout and a stopped deployment between them. Freeze the same supported
profile for both; a code/configuration change starts a different cohort and
cannot be quietly folded into the comparison.

Proposed operator overrides, using existing admission controls:

| Control | Proposed value |
| --- | --- |
| Repository daily cap | USD 25 |
| Shared global daily cap | USD 25 |
| Per-issue cap | USD 25 |
| Fix dispatch concurrency | 1 |
| Maximum resolve-leg budget | USD 20 (current target value) |

Retain lower existing leg/role ceilings. A configured maximum reservation
larger than the remaining cap blocks admission; it is not permission to raise
the cap. Aligning each window to two complete UTC budget periods bounds its
new configured-card admissions at USD 50. Two windows plus a canary in one
additional period have a combined upper bound of USD 125. Outstanding
pre-window obligations and other deployments must be inventoried first;
this is not a bound on unaccounted historical obligations or a statement about
the user's subscription bill.

The proposed configuration change uses these existing keys:

```sh
DAILY_SPEND_CAP_USD=25
GLOBAL_DAILY_SPEND_CAP_USD=25
ISSUE_SPEND_CAP_USD=25
FIX_DISPATCH_CONCURRENCY=1
```

Apply them only to the explicitly authorized launch configuration and record
its resolved version. The shared global limit covers other deployments using
the same authority; their obligations belong in the pre-window inventory.

The proposed caps are much lower than the retained repository override of
USD 700/day. Budget halts are valid measured outcomes. Low exposure or a
budget-limited sample must not be presented as evidence of improved quality
or cost per merge.

## Preconditions before requesting release

- Complete the offline Phase 5/6 implementation, independent review and full
  test gates; record the accepted source revision.
- Snapshot retained authority and telemetry without altering their source.
  Exercise required schema/family activation and rollback on isolated copies.
  Resolve conflicting owners and outstanding command/spend obligations;
  preserve uncertain outcomes instead of clearing them.
- Verify the real supported profile and test executor in an isolated checkout.
  All paid roles use the same SQL accounting authority and shared global cap.
- Freeze the initial issue/PR inventory, difficulty strata, inherited-work
  selection, workflow binding references, cohort specification and event
  offsets. Include parked, abandoned and unmerged work.
- Present the exact configuration diff, selected work and copied-data
  preflight evidence for explicit release. Existing service autostart remains
  disabled unless that release explicitly includes it.

## Observation and halts

For each window, admit only the frozen initial eligible work. No operator
comments, relabels, closes, dashboard commands, configuration changes or
manual restarts during the window. Read-only monitoring is allowed; existing
supervision may recover a crash. An emergency ends the window and is recorded
as a finding. Stop admission at the end and use existing bounded draining and
handover; do not keep the paid deployment running during analysis.

Record every halt as capability, policy, product judgment, budget, infrastructure,
uncertain external outcome, or safety. Include time first held, unresolved age,
diagnosis/mechanical effort, interruptions, direct GitHub/service/config work,
incident family and audit-versus-escaped provenance. Record measured zero only
when the relevant coverage was actually complete; otherwise report unknown
or an observed floor.

Export each exact cohort and its observation cutoff using the same readout as
the dashboard. Report distinct admitted bindings by family, the explicitly
selected primary family, all/shared/unassigned spend, distinct PR outcomes,
first-review approval including unmerged PRs, recurrence observation coverage,
human effort and time held. Adjudicate halts against the existing replay corpus.
Any automation proposal must name a measured cause, a smaller alternative,
outcome metric, spend/quality guardrails and the next experiment. No diagnostic
leg or general policy system is preselected.

## Acceptance status

The 2026-10-04 11:06 UTC copied-data preflight resolved the retained launch
snapshot and current shared/repository configuration to the same version,
`c312385cee5ebd45`. All configured roles use Codex; review mode is `single`;
the Codex/review model is `gpt-5.6-sol`. The fix and triage task model fields are
unset, so their actual per-run models still need frozen admission evidence.
The retained caps are repository USD 700/day, global USD 1500/day and issue
USD 250. Proposed lower caps above have not been applied.

The existing resolve rehearsal copied retained schema 18 authority and legacy
sources into `/tmp/phase56-tauri-resolve-rehearsal-20261004T110629`, migrated
the copy to schema 39, and refused activation. Its report confirms matching
copied sources and unchanged original files. No active run, lease or dispatch
claim was found, but quiescence is blocked by these existing obligations:

| Obligation | Retained evidence |
| --- | --- |
| TRIAGE `1cd6cf8bf1b344d4955ed3815a565821` | Abandoned/dead lease; USD 0.75 maximum reservation; no settled charge; reconcile-only |
| GROOM `727ead9129d6444497d6303f00b57056` | Abandoned/dead lease; USD 2 maximum reservation; no settled charge; reconcile-only |
| RESOLVE `4856475277024e529ebb5eb9728bead3` | Abandoned/dead lease; USD 20 maximum reservation; trusted usage/provider charge unavailable; reconcile-only |
| Publication `a1280b335028132d66d5c0a530fd3d58185caed2a35c830e3314853f49f9de6a` | Retryable failure for `fix/598-miller-column-cache-doesnt-get-invalidat`; one attempt |

USD 22.75 is retained maximum exposure, not measured spend. These obligations
were preserved; the preflight did not clear them or claim settlement. Resolve
and subsequent family activation/rollback must be rehearsed again after
positive reconciliation evidence is available. The configured target checkout
also has existing edits on `fix/crop-editor-ux`; executor verification uses
an isolated checkout pinned to the selected revision.

The 2026-10-04 offline executor qualification used a private checkout of
configured `dev` at `7d13f05d465358c1a29acaaeeff654d50c13c50c`, with copied
dependency/build caches and private cache paths. The first attempt exposed
missing copied website dependencies and sandbox-denied local sockets. After
copying those dependencies and permitting local sockets, the exact retained
test command reached native compilation but exceeded its configured 600-second
limit. The executor recorded `UNAVAILABLE` / `TIMED_OUT`, preserved equal
pre/post tracked-tree proof and completed process teardown with no remaining
orphans. This first run verifies bounded failure handling.

The final reviewed executor then ran the same retained command with the warmed
private cache at 2026-10-04 13:35–13:39 UTC. It passed in 258,591 ms, recorded
equal pre/post committed/index/tracked-tree proof, and left no orphan processes.
The tested configuration digest was
`67a3122de6b766194cb5df5ce1208872dbbf40fc22615de1c1353e259ed87ab3`.
This qualifies the ordinary test command in the isolated Linux checkout;
it does not supply migration, paid-provider, canary or unattended-window
acceptance. The original checkout and live authority were unchanged.

No dates, accepted starting revisions, selected issue set, successful family
activation/rollback, coverage declaration or live readout has been recorded.
These remain required before Phase 5's unattended-measurement acceptance. The
historical 69 paid legs and two observed merges are not a comparable baseline
denominator.
