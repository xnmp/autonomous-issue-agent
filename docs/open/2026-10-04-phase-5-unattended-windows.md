# Phase 5 unattended-window proposal

**Current checkpoint — October 5:** the later standing accounting approval
covered retained-obligation dispositions and the five-period timing allocation.
Live schema-40/all-family activation and the eight lower-cap/profile overrides
are applied, with historical holds preserved. Current target qualification is
at `af06e086`. The bounded intentional-cancellation canary ran and settled
USD 0.632080; no unattended window has started. Its actual enriched admission
fingerprint is `9cbb861835df4825` (launch file `f9e073a4b2d58c23`). Follow the
[live execution record](2026-10-05-phase-5-live-execution.md); the original
midnight proposal and preparation inventories below are historical evidence.

Status: the user approved this bounded proposal on 2026-10-04 at approximately
22:51 UTC, releasing the deployment pause within its scope. **No canary or
unattended window has started.** The preconditions below still apply. This
approval does not authorize inventing usage or clearing unresolved historical
accounting. An exceptional disposition of retained obligations requires its
own concrete operator decision.

The existing [48-hour protocol](soak-protocol-unattended-48h.md) supplies the
no-touch rule and halt adjudication. Its 2026-08-02 approval and old keifu
prerequisites do not authorize this deployment. This proposal governs release,
profile, cost and cohort selection. The new measurement export supplies
prospective observations, exposure and coverage. Controlled replays do not
substitute for these windows. Retain the protocol's three-merge throughput
target and report an unmet target explicitly. Its quality comparison requires
a comparable baseline; the historical 69 paid legs and two observed merges do
not establish one. Missing baseline or recurrence coverage remains unknown.

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
| Steward promotion | Disabled during each window |
| Adopt unlabeled PRs | Disabled |
| Fix and triage task model | Explicit `gpt-5.6-sol` |

Retain lower existing leg/role ceilings. A configured maximum reservation
larger than the remaining cap blocks admission; it is not permission to raise
the cap. Aligning each window to two complete UTC budget periods bounds its
new configured-card admissions at USD 50. Two windows plus a canary in one
additional period have a combined upper bound of USD 125. Outstanding
pre-window obligations and other deployments must be inventoried first;
this is not a bound on unaccounted historical obligations or a statement about
the user's subscription bill.

The USD 125 bound requires the same USD 25 global cap for every participating
owner, at most five admitted UTC budget periods, and no new paid admission
during analysis or beyond the window deadline while draining. A concurrent
deployment using a different global cap is outside that bound. Verify those
conditions before release; a later admission period requires a revised bound.

The proposed configuration change uses these existing keys:

```sh
DAILY_SPEND_CAP_USD=25
GLOBAL_DAILY_SPEND_CAP_USD=25
ISSUE_SPEND_CAP_USD=25
FIX_DISPATCH_CONCURRENCY=1
STEWARD_PROMOTE=0
REVIEW_ADOPT_UNLABELED=0
FIX_MODEL=gpt-5.6-sol
STEWARD_TRIAGE_MODEL=gpt-5.6-sol
```

Apply them only to the explicitly authorized launch configuration and record
its resolved version. The shared global limit covers other deployments using
the same authority; their obligations belong in the pre-window inventory.
Steward triage and grooming remain enabled so their unanswered questions,
holds and effort remain observable. New ready-queue promotion is disabled;
the initial resolve queue is selected before the no-touch interval.

The proposed caps are much lower than the retained repository override of
USD 700/day. Budget halts are valid measured outcomes. Low exposure or a
budget-limited sample must not be presented as evidence of improved quality
or cost per merge.

## Preconditions before live admission

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
- Record the exact approved configuration diff, selected work and copied-data
  preflight evidence before applying the approved release. Existing service
  autostart remains disabled unless explicitly included in the release.

## Observation and halts

Freeze the complete initial issue/PR inventory and the selected initial
resolve queue. Use the existing `selection: new_admissions` cohort: include
every actual admission in the repository during the window, stratified by
family and observed input/difficulty. Record inherited bindings explicitly
when selecting inherited exposure. The initial inventory is a baseline,
not a runtime allowlist. Steward triage/groom may observe new inputs; retain
those admissions and their spend in the readout. An external change to resolve
eligibility or review trigger labels is a protocol deviation requiring a stop
and adjudication, not permission to silently exclude the resulting work.

No operator
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
The proposed in-memory configuration explicitly fixes both task models and
disables promotion and unlabeled review adoption. Its version is
`f9e073a4b2d58c23`, with no configuration validation errors; it has not been
written to the launch snapshot. All actual admission profiles still need to
match the accepted launch version.
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
positive reconciliation or a specifically authorized exceptional disposition.
The configured target checkout has existing edits; executor verification uses
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

No dates, accepted starting revisions, selected issue set, successful family
activation/rollback, coverage declaration or live readout has been recorded.
These remain required before Phase 5's unattended-measurement acceptance. The
historical 69 paid legs and two observed merges are not a comparable baseline
denominator.

### 2026-10-04 preparation audit

The accepted harness revision is `0294b3e`. A read-only GitHub snapshot at
14:55–14:57 UTC found 26 open issues, two open PRs and remote `dev` at
`4eefa9080f1ae9d3a35b12137369096717ffb5d0`. That newer target revision has
not inherited the ordinary-command qualification of `7d13f05`; freeze and
qualify the actual selected target revision before release.

The five current `ai-ready` issues need an explicit initial queue decision:

| Issue | Initial inventory classification |
| --- | --- |
| [#523](https://github.com/xnmp/tauri-explorer/issues/523) | Substantial Git classification feature; ordinary Git tests and screenshots required |
| [#531](https://github.com/xnmp/tauri-explorer/issues/531) | Merge-base navigation feature; screenshot required |
| [#526](https://github.com/xnmp/tauri-explorer/issues/526) | Broader PR-action feature; scope and external capability qualification pending |
| [#610](https://github.com/xnmp/tauri-explorer/issues/610) | AI-generated columns feature; scope and provider capability qualification pending |
| [#710](https://github.com/xnmp/tauri-explorer/issues/710) | Windows-only smoke problem, outside this profile; retained retry count 3 equals ceiling 3, with no renewal proposed |

These are inventory facts, not accepted admission decisions or difficulty
scores. Do not infer visual capability from the ordinary unit-test command.
Open PRs [#986](https://github.com/xnmp/tauri-explorer/pull/986) (human image
editor work) and [#987](https://github.com/xnmp/tauri-explorer/pull/987)
(Dependabot) have no review trigger label in this snapshot. The proposed
unlabeled-adoption setting excludes them from new automatic review; retain
them in the starting inventory and account for any inherited binding separately.

At 15:01 UTC, the existing fix and steward activation rehearsal commands
refused their private copies on the same three reservations and publication
listed above. Successful activation/rollback is still unproven. A read-only
check across the shared authority found no active runs, leases or dispatch
claims; its three active reservations are the retained USD 22.75 maximum
exposure. All four protected target services were inactive and disabled.

The existing spend reconciler's read-only input path found no valid retained
evidence for the triage/groom runs. Resolve retains a configured session
reference, but its trusted usage read returns unavailable. All three runs are
abandoned with `dead_lease`, no provider receipt ID and no settled charge.
These observations do not authorize zero-cost settlement or release.

Independent review accepted this document as a preparation draft; the user's
subsequent approval authorizes the bounded proposal. Starting admission still
requires resolved obligations, copied activation/rollback, explicit starting
queue and inherited-binding decisions, qualification of the selected target,
the exact launch/profile freeze, dates and coverage.

### Approved preflight continuation — 2026-10-04 22:51 UTC onward

The harness remains at `74219a7`; all six installed tauri-explorer/keifu role
services remain stopped and disabled. The original authority is still schema
18 and has not been migrated or written. A fresh GitHub inventory found 26
open issues, one open PR (#987, Dependabot, no review trigger), and remote
`dev` at `32bd6b6642bcda4b4e2b5b8f4ffb0bda7735d86f`. PR #986 has merged.
The five ready issues remain #523, #531, #526, #610 and #710; this inventory
does not itself settle the initial queue or capability qualification.

The same retained ordinary command passed against a clean private checkout
of `32bd6b6` at 23:03:23–23:05:01 UTC in 98,745 ms. It used the proposed
resolved configuration `f9e073a4b2d58c23`, retained the 600-second deadline,
proved equal pre/post committed/index/tracked-tree identity, and left no
orphans. Cargo used the retained shared build-cache path
`/home/chong/.cache/issue-agent/xnmp-tauri-explorer/cargo-target`; registry and
Bun cache overrides were private. Its test-configuration digest is
`665c85173bfcf4036d2eeb2a85871c7ef0895bdd19b9de9e3f50f87cbfe4906e`.
This is ordinary-command qualification, not provider or live window acceptance.
The private native build finished at 23:17:55 UTC in approximately 12.5 minutes,
with embedded frontend assets and both E2E hook gates. Binary SHA-256:
`1abc9a02f5ab73c9aca8cebe0c12daea9ea0fcf46f69f44428ac2bb7c42729f8`.
One native qualification cycle passed at 23:20:15–23:20:22 UTC on an isolated
Xvfb/openbox display and disposable configuration/data profile. All four
existing scenarios passed: window/workspace, plugin churn, theme/accessibility
and zoom, and preview/native input. Display scale was 1; an actual completion
screenshot was retained and no owned application process remained. This is
bounded native capability qualification, not a four-hour target soak or either
48-hour harness window.

Read-only recovery investigation confirms unavailable usage for all three
retained runs. Resolve's exact Codex session ended with a quota refusal;
`token_count.info` is absent, so the refusal is not measured zero usage.
The host boot at 2026-10-03 03:46:08 UTC is later than all three runs and
proves their original local processes are terminal. The USD 22.75 maximum
exposure remains reserved. Generic explicit unknown-capacity release preserves
unknown spend, but prevents later normal settlement through the existing API;
that accounting disposition was not part of the bounded-window approval.

The old publication's retained stdout records a successful branch push. Its
expected commit is an ancestor of the final head of merged PR #623; the
captured checkout and remote branch are now absent. Replaying it could recreate
an obsolete branch. A copied-data rehearsal is preparing an explicit obsolete
command retirement with the complete original receipt archived, without claiming
a new success or inventing an exhausted retry ceiling. Neither exceptional
disposition has been applied to live state.

The corrected copied-data recovery rehearsal succeeded with unchanged original
authority/configuration/source bytes: three unknown reservations released and
the exact obsolete publication retired through existing public APIs; no charge,
historical success or extra retry was invented. It preserved run outcomes,
maximum exposure, command payload/owner/attempt count, unrelated SQL rows and
five durable new events. Resolve activation then imported the three actual
legacy records (#680, #696, #710), preserving their exhausted holds. The first
fix and steward attempts refused 297 and 165 historical unbound runs
respectively, preventing fresh-target activation. The
[retained-import brief](2026-10-04-phase-5-retained-import-brief.md) scopes the
bounded prerequisite, which now passes current-source private all-family
qualification. All six family epochs reopen, all historical fix/steward targets
remain held, and original saved-plan retries are inert after all-family
activation. Historical SQL/accounting and publication rows match the recovered
source; genuine PR #581 observation retains its exact repair hold across
restart. Independent review accepted the candidate. The third preflight began
before the final guard centralization and did not capture initial loaded-source
hashes; final postactivation qualification used current source and archived its
hashes. The final complete gate passed at 2026-10-05 01:35:22 UTC in 15 minutes
30 seconds, with 10,529 domain/adapter/app cases, 123 hook checks, parity, four
architecture contracts and both type checks. Dashboard API coverage executed;
all 22 frozen source/test hashes matched afterward. The two fixture corrections
preserve the original rollback assertions and optional-dashboard contract.

Compatible current code reopened the complete stopped authority successfully.
Predecessor `74219a7` is incompatible after deferred repair activation; rollback
means remaining stopped or using compatible accepted code, without downgrading
SQL or restoring legacy authority. All recovery dispositions above occurred on
private copies. Their live equivalents still require the explicit
[retained-obligation decision](2026-10-05-phase-5-retained-obligations.md).

### Initial queue and finite window controls

The 2026-10-05 00:24 UTC read-only inventory contains 27 open issues and open
PRs #987 and #990, both without review triggers. Remote `dev` advanced to
`bde6081d304890e7a3080bd17466543f41862328` through PR #988. Its ordinary
command passed in 20,176 ms at 00:25:18–00:25:38 UTC with equal pre/post tree
proof, configuration `f9e073a4b2d58c23` and test digest
`f52aa96f4516027d431e9158c72ea297de071afe2ca0c4082034126e4c6217ae`.
Final private-registry and host-process readback found no surviving test
processes. The helper's `remaining_orphans` field lists a stale registration
reaped by `recover_orphans`, whose return value denotes recovered IDs;
the separate final readback confirms an empty registry and launch-intent set.
The intervening target diff changes only the Arch installer and its evidence;
native application and qualification sources match the earlier qualified tree.
The native binary was qualified at `32bd6b6`, not rebuilt at `bde6081`.

Select #755 for the canary and #848, #985, #938 and #786 as the subsequent
resolve pool. These existing requests have no recorded linked execution or
canonical issue workflow in the qualified private authority. Before labeling,
repeat the exact target/history check against live authority and inventory.
Retain the five old ready issues' sourced holds; do not renew or clear them.
No target labels have changed during preparation.

| Issue | Bounded requested outcome | Provisional difficulty and evidence |
| --- | --- | --- |
| [#755](https://github.com/xnmp/tauri-explorer/issues/755) | About palette command displays the actual application version | Small UI change; actual palette/version outcome and screenshot |
| [#848](https://github.com/xnmp/tauri-explorer/issues/848) | Back/Forward hints reflect effective bindings, including rebinding | Small UI change; navigation outcome, rebinding and screenshot |
| [#985](https://github.com/xnmp/tauri-explorer/issues/985) | Ctrl+E opens the existing editor for the selected image | Small UI change; native keyboard/editor outcome without a paid image request |
| [#938](https://github.com/xnmp/tauri-explorer/issues/938) | Config watching survives retargeting into a nested watched root | Medium backend fix; actual Linux notification outcome |
| [#786](https://github.com/xnmp/tauri-explorer/issues/786) | Git-watch cancellation tests establish their scheduling precondition deterministically | Small test correction; preserve real cancellation behavior and prove the regression assertion |

These are operator selection estimates, not admitted difficulty facts or a
comparable baseline. Record actual admission classifications in each readout.
Include any unfinished canary binding explicitly as inherited work at the first
window start. Select subsequent work before the no-touch interval, and report
an empty or budget-limited queue honestly. The three-merge target is unchanged.

Use the installed, currently disabled role units for finite observation;
keep keifu stopped and service autostart disabled. The generic installer enables
autostart, so running it unchanged would exceed this release scope. The canary
uses the existing `ISSUE_AGENT_MAX_TICKS=1` control and bounded draining. Freeze
the ordinary launch snapshot before the first window, with no canary tick limit.
Schedule starts and simultaneous role stops with the existing systemd service
manager, at exact UTC boundaries. Verify the end timer is armed before paid
start. The executable freeze must name each installed unit and timer, its UTC
dates, `AccuracySec`, `RandomizedDelaySec` and persistence, and demonstrate
completed admission stops by the budget boundary; a requested stop alone is
insufficient. Default timer coalescing is not that proof. Missing supervision,
a missed boundary or a host reboot invalidates the window and requires
adjudication. An early stop reports its shorter actual coverage, not a completed
48-hour run. Stop closes admission before bounded teardown;
do not admit in an extra UTC period while reading results. No new controller or
worker is required. Read-only twenty-minute supervision may observe caps,
service health and holds, but may not steer paid work during the no-touch
interval. A necessary intervention ends and classifies that window.

### Prepared timing amendment — not yet approved

The installed 90-second shutdown bound exposes a timing limit in the midnight
proposal: an early stop shortens coverage, while an end-time stop can drain
into another UTC period. The independently reviewed
[timing amendment](2026-10-05-phase-5-window-timing-amendment.md) prepares two
complete 48-hour intervals with same-day teardown margins and five shared
global budget periods. It preserves USD 125 aggregate but changes starting
times and standalone window ceilings. It has not been approved, installed or
armed; neither draft timers nor qualification supply actual window acceptance.
