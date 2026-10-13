# ADR 0007 — Configuration snapshot and reload

Status: Accepted
Date: 2026-08-01
Governs: src/issue_agent/app/config.py src/issue_agent/app/main.py src/issue_agent/adapters/reload.py bin/point-at.sh config/

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

## Consequences

- A boot log can describe the exact effective snapshot operators are running.
- Adding a knob requires one metadata entry and validation at the boundary.
- A coherent commit is the code reload unit; uncommitted edits never enter a
  live daemon through hot reload.
- Secret placement and child capability are separate from configuration
  selection; a value in the snapshot is not automatically child-visible.
