# ADR 0007 — Configuration snapshot and reload

Status: Accepted
Date: 2026-08-01
Governs: src/issue_agent/app/config.py src/issue_agent/app/main.py src/issue_agent/app/live_config.py src/issue_agent/adapters/reload.py src/issue_agent/adapters/config_files.py src/issue_agent/app/settings.py bin/point-at.sh config/

## Context

Daemon, hook, subprocess, and publication behavior previously read overlapping
ambient variables at different times. A composed deployment could therefore
validate one base branch or test command while a later child silently used a
default or stale inherited value.

## Decision

One composed `AGENT_ENV` file is the operator authority for a daemon boot. Its
values override inherited values. For `HarnessConfig` knobs, removal from the
file restores the declared default across a relaunch. Frozen dataclass field
definitions are the single mapping: each field's default plus its metadata for
env name, coercion, choices, and generated examples.
`HarnessConfig.validation_errors()` owns range and cross-field validation.
Configuration is validated before adapter construction and the resulting
snapshot hash is recorded on reservations and runs that need
historical identity.

After validation, application code passes snapshot values explicitly. Ambient
environment is updated once at the process boundary only because legacy shell
adapters inherit it; paid providers and target commands receive narrower
environments under ADR 0011. Operational paths do not independently fall back
to another base branch, repository, model, authority path, or state path.

Hot reload is a new process, never an in-place object-graph mutation. Harness
code changes are detected from committed HEAD, while the gitignored composed
env is content-hashed. A valid change closes paid admission, lets active work
drain while observation continues, and restarts between ticks. Invalid
candidate configuration does not replace the running snapshot.

### Amendment 2026-10-15: live knobs and dashboard settings

The owner edits operational settings per repo from the dashboard and expects
them to apply at the next tick, without a restart or a drain. A narrow, typed
exception carries that:

- A `HarnessConfig` field whose metadata is `live` is a dispatch-only knob:
  the model routes (`FIX_MODEL`, `FIX_MODEL_<TIER>`, `TRIAGE_MODEL`,
  `STEWARD_MODEL`, `REVIEW_MODEL`), `FIX_DISPATCH_CONCURRENCY` (at most
  `MAX_DISPATCH_CONCURRENCY`, the size of a daemon's lazily filled dispatch
  pool), `ISSUE_SPEND_CAP_USD`, `DAILY_SPEND_CAP_USD` and `ISSUE_PICKUP`.
  Nothing else is live.
- Each tick's observation re-reads the same composed `AGENT_ENV` file,
  only when its bytes changed (`LiveConfig.refresh`). The candidate is parsed
  by `from_env` and validated with `validation_errors()`, both as a whole and
  as the boot snapshot with its live knobs (`with_live`). Valid: that merged
  value becomes the current snapshot, a new immutable object. The re-read is
  strict: a file that cannot be sourced keeps the current snapshot and is
  retried next tick, never read back from the inherited environment (which
  under `point-at.sh` holds every launch-time value). Invalid, or
  needing a runtime the process did not bind: the previous snapshot stays and
  the refusal is logged once per file hash.
- Readers take one whole snapshot: the tick's observation (pickup), local
  facts (headroom, lifetime cap), dispatch capacity, and each leg's route and
  admission caps (`AdmissionCaps`, read once per leg before its reservation
  transaction). Nothing in the composed graph is mutated; the reservation
  transaction itself is unchanged, so no new crash point exists.
- A leg's model is its live route's. No runtime keeps a boot default model
  (the codex runner used to bake in the boot `FIX_MODEL`/`REVIEW_MODEL`, so
  an unset applied only after a restart); a route without a model means the
  runtime's own default.
- The snapshot hash a leg ran under is the effective snapshot's
  `config_version` (it covers the live values): the leg's environment carries
  it as before, and its receipt now records it as `config`. The charge's
  rate-card version still names the boot snapshot's rates, which are not
  live.
- `ReloadWatch` fingerprints the env file's restart-requiring part only:
  every assignment but a live knob's (the deploy manifest without live
  names) plus the runtimes the role binds (`reload_fingerprint`). A live-only
  change never reloads; any other change, including a live route onto an
  unbound runtime, takes the drain-and-restart path above.
- The dashboard writes settings by editing `config/shared.env` or
  `config/repos/<name>.env` in place (the last top-level `KEY=` or
  `export KEY=` line, comments and order kept; a missing key is appended;
  an indented or compound assignment is refused). It composes the candidate
  with the real `bin/compose-env.sh` in a private scratch directory and
  validates the result with `from_env`/`validation_errors()`. It refuses,
  naming the keys, when the candidate's restart-requiring part
  (`config.restart_part`, what `ReloadWatch` fingerprints) differs from the
  running `.active`: a pending hand edit, or a route onto an unbound runtime,
  would otherwise ship with the save and drain and restart the daemons while
  the dashboard said "next tick". `.active` stays recomposed only by the
  operator's launch for such changes. That refusal applies to the deployment
  being edited; another deployment with such a difference (a pending edit, or
  a stopped run's stale `.active`) is held back instead: its composed files
  are left as they are, the save names it, and it takes the change at its
  next launch (#466). `ISSUE_PICKUP=open` is refused when the
  repository has `FIX_MAX_ITEMS` open issues or more, or they cannot be
  counted. The write is atomic, keeps the file's mode, and is refused if the
  file changed since it was read (a concurrent hand edit is never
  overwritten; the rollback checks the same way). Then it runs
  `bin/compose-env.sh` for every
  affected launched deployment and publishes the composed file byte for byte
  to any installed service snapshot whose REPO and TARGET_DIR match. A
  failed compose restores the source file and re-applies (recomposes and
  republishes) every deployment already applied, so an installed service
  never runs a refused value. Only live knobs are writable and
  shown, so no secret is ever read or written; each change is journaled
  (who, key, old and new value) to the deployment's fix journal. The write
  inherits the dashboard's loopback binding and `x-issue-agent-dashboard`
  header guard.

`ISSUE_PICKUP=open` moves a trust boundary (ADR 0001): `ai-ready` was the
human act that authorised paid legs on an issue. In open mode every open
issue is the harness's to work, except `needs-human` ones (parked) and the
harness's own gap reports (`domain.state.harness_gap`, so its reports never
feed back into legs). Whoever can open an issue on the target can then start
paid legs that hold `FIX_BOT_TOKEN` and read that issue's untrusted text, and
a fix leg can open issues that the next tick takes up. The owner accepts this
per deployment; the bounds are the per-issue and daily spend caps and the
existing leg sandbox. It belongs on repositories whose issue authors the
owner trusts. Open mode observes every open issue under the same
`FIX_MAX_ITEMS` bound as ready mode: a repository with more open issues than
that fails its fix observation every tick (merges and repairs included), so
the dashboard refuses open mode at that count; a hand edit or a backlog that
grows past it afterwards still stalls until the pickup returns to `ready`. The steward still orders only `ai-ready`
issues.

Exit: the exception is the `live` metadata. Removing a knob's flag returns it
to restart semantics; removing every flag deletes the mechanism.

### Amendment 2026-10-18: explicit configuration scope

The configuration service is application-level, shared by the dashboard and
`issue-agent config get|set|unset`. A repository name selects its overlay;
absence selects shared defaults. The CLI requires either `--name` or
`--shared`, without discovering a target checkout. Shared operations require
no deployment, including when there are no configured repositories. Only the
existing live-setting allowlist is exposed.

Shared candidates are sourced privately as shared configuration, without an
overlay or inherited `AGENT_ENV`. `HarnessConfig.settings_errors()` validates
their parsing, ranges, routes, pricing and cross-field constraints; boot
`validation_errors()` additionally requires the deployment repository and fixer
identity. This removes the arbitrary deployment validation anchor without
inventing a repository identity or weakening launch validation. A shared
candidate cannot hide an invalid default behind a repository override.

The same transaction then validates each launched deployment as before. For a
shared save, every deployment with pending restart-required changes is held,
including the one displaying Settings in the dashboard; there is no privileged
anchor. A repository-scoped save still refuses such changes in its selected
deployment. Source replacement, composition, publication, rollback and per-
deployment journal receipts otherwise retain the existing contract.

A kernel-released advisory lock in `config/.settings.lock` now encloses the
whole read/validate/write/publish transaction for both entry points. It replaces
the dashboard instance's thread-only settings lock, so a CLI save cannot race
a dashboard save. The lock contains no configuration or secrets and is never
used as a PID or liveness record. Manual edits retain expected-content checks.
No parallel propagation implementation, repository override or runtime setting
was added.

## Consequences

- A boot log can describe the exact effective snapshot operators are running.
- Adding a knob requires one metadata entry and validation at the boundary.
- A coherent commit is the code reload unit; uncommitted edits never enter a
  live daemon through hot reload.
- Secret placement and child capability are separate from configuration
  selection; a value in the snapshot is not automatically child-visible.
