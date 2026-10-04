# Incident, intervention and cohort measurement

These local observations measure human effort and outcomes. They cannot release a hold, authorize a paid attempt, change workflow authority or activate a deployment. Historical missing values remain unknown; the September baseline is not a comparable prospective cohort.

Use an existing initialized control database. Recording does not initialize or migrate it:

```sh
issue-agent measurement record --database /path/control.db --deployment tauri-explorer --input intervention.json
issue-agent measurement export --database /path/control.db --repo xnmp/tauri-explorer --specification cohort.json > cohort-result.json
```

An external intervention input:

```json
{
  "kind": "intervention",
  "record_id": "service-diagnosis-20261004-01",
  "repository": "xnmp/tauri-explorer",
  "actor": "local-operator",
  "provenance": "operator timing and service logs",
  "occurred_at": "2026-10-04T01:00:00Z",
  "category": "diagnosis",
  "source": "service",
  "description": "Diagnosed a service startup failure",
  "duration_seconds": 120,
  "interruptions": 1
}
```

Categories are diagnosis/mechanical/product; sources are dashboard/GitHub/service/config/other. Duration and interruptions may be omitted: measured lower-bound totals and unknown counts stay separate. Optional `workflow_ref` is the `binding_ref` from an export; `event_ref` identifies the original dashboard event when annotating an existing action. The existing dashboard intervention metric keeps its dashboard-only meaning. Prospective summaries count annotations of the same event once; they do not add themselves to that legacy metric.

`incident` records require `family`, `finding_source` (audit/escaped), `classification` (systemic/incidental/mixed/unknown), `severity` (low/medium/high/critical), `first_seen` and 1–16 `evidence_refs`. Common identity/provenance/time fields match interventions. Optional `resolved_at`, proven `introduced_revision`/`fixed_revision`, and `workflow_ref` retain diagnosis without a mutable incident lifecycle. A later correction or resolution uses a new `record_id` and `original_record_id`; it does not create another incident in the summary. Duplicate identical IDs are idempotent; conflicting payloads fail and retain the original. Unresolved/cyclic references remain coverage gaps.

An explicit cohort specification:

```json
{
  "started_at": "2026-10-04T00:00:00Z",
  "ended_at": "2026-10-05T00:00:00Z",
  "observation_cutoff": "2026-10-06T00:00:00Z",
  "selection": "new_admissions",
  "primary_family": "resolve",
  "expected_config_version": "REPLACE_WITH_FROZEN_CONFIGURATION_VERSION",
  "supported_profile": {
    "deployment": "tauri-explorer",
    "runtime": "codex",
    "model": "REPLACE_WITH_ACTUAL_MODEL",
    "posture": "trusted inbound",
    "config_version": "REPLACE_WITH_FROZEN_CONFIGURATION_VERSION"
  }
}
```

The window is UTC and half-open. `include_inherited` adds only the explicitly listed `inherited_binding_refs` to new admissions in the window. Freeze that bounded list from exported references; missing requested bindings produce a coverage gap. It does not sweep completed historical work into a prospective cohort. The denominator is a full authoritative admitted binding, including family, target, generation, authority epoch and episode. Review execution retains its additional execution epoch/episode. Versions and paid-leg count are not workflow denominators. All families remain visible; headline outcome/activity denominators use the explicit primary family. Exported binding references let operators annotate actual admissions.

A `workflow_measurement` input uses common identity/provenance/time fields plus `run_id`, `binding_ref` and `evidence_refs`. The run/binding pair must match an authoritative admission. Optional `profile` requires all six fields: deployment/runtime/model/posture/difficulty/config_version. One binding observation supplies deployment/posture/difficulty; frozen SQL runtime/model/config inputs automatically cover other admitted runs. Runtime/model annotations are needed only for missing actual inputs. For a fixed supported mix, `supported_profile` may use bounded `runtimes`/`models` arrays instead of mutually exclusive singular keys, under a frozen config_version. Exported runtime/model/kind mix counts and difficulty strata retain the observed distribution; membership in overlapping sets does not establish equivalent workloads. These operator observations are checked against frozen admission config/runtime/model inputs; `<runtime-default>` does not establish the actual model. Optional `outcome` is either `{"state":"merged","pr_number":44,"merged_at":"2026-10-04T01:00:00Z"}` or an open/held/parked/abandoned state with optional proven `pr_number`. `first_pass_approved` requires a proven PR; `recurrence_observed` requires a merged PR. `first_review_status:"not_reviewed"` explicitly records a PR which has not yet received a first review; absent first-review facts remain unknown even with a completeness declaration. Held/parked outcomes may supply UTC `held_since`, no later than the observation, to measure held duration; missing start stays unknown. Later annotations retain previously recorded facts; corrections can explicitly replace known facts. Every asserted profile/outcome has evidence and provenance.

A `cohort_coverage` input uses common identity/provenance/time fields, exported `cohort_fingerprint`, `evidence_refs` and explicit booleans `human_complete`, `outcomes_complete`, `first_review_complete`, `recurrence_complete`. Complete recurrence coverage also requires positive `recurrence_horizon_seconds`. These are operator-reported completeness declarations. The fingerprint covers repository, exact window/cutoff, selection, primary family and expected profile. Missing coverage leaves observed floors and unknown complete rates.

Audit catches are separate from escaped incident and repeated-family rates; primary-family human activity is split into diagnosis/mechanical/product, with actions deduplicated across event annotations/corrections. Distinct merged PRs are deduplicated across bindings. First-pass quality counts distinct PRs with a first-review outcome, including unmerged/rejected work. Recurrence requires complete merged-outcome coverage and merged-PR observations covering the declared horizon. Complete spend-per-merge ratios also require every selected run to have accounting at cutoff; missing charges remain a gap. Conflicting PR observations remain gaps. Primary-family spend counts each attributed run charge once, including failed/unmerged work; shared bindings are identified explicitly. Other-family spend and unassigned/legacy unknown-repository spend are separate. Current SQL run states are labeled as a current snapshot; cutoff outcomes require observations.

The existing dashboard offers the same read-only export at `GET /api/repos/{name}/measurement?specification=<URL-encoded JSON specification>`. It performs no recording or activation. CLI exports are deterministic for an unchanged database and specification. Profile eligibility describes a documented comparable selection; it is not evidence of a trend, causal improvement or completed unattended acceptance. Live windows remain paused.
