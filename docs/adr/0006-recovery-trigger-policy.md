# ADR 0006 — Recovery trigger policy

Status: Accepted
Date: 2026-08-01
Governs: src/issue_agent/domain/recovery.py src/issue_agent/app/recovery.py src/issue_agent/app/tick.py src/issue_agent/app/main.py

## Context

Recovery's durable state machines and idempotent reconciliation were already
centralized, but their invocation was not. Boot recovery ran during dependency
construction as well as through a separate pre-adapter process reaper;
suspend-wake and periodic reservation recovery were independent callbacks in
the daemon loop. A missing periodic edge stranded retained reservations, while
a wake edge wired to unconditional process reaping killed live paid work.

The defect class is therefore trigger-to-operation composition, not ownership
of process, lease, reservation, or spend state.

## Decision

Recovery has three triggers (`domain/recovery.py`); the coordinator
(`app/recovery.py`) maps each to its arms in one fixed order (slice 9 folded
the former arm/plan tables into that mapping):

| Trigger | Process arm | Other arms | Reservation grace |
|---|---|---|---|
| Boot | reap all owned legacy and current processes | reclaim leases, validate worktrees, sweep reservations | immediate |
| Wake | reap abandoned processes only | reclaim leases, validate worktrees, sweep reservations | configured stale grace |
| Periodic | none | reclaim leases and sweep reservations | configured stale grace |

Boot has two ordered phases under the same trigger. Its process arm reaps the
legacy and current-deployment registries before adapter
construction. Lease, worktree, spend, and reservation reconciliation
runs explicitly after the application graph and legacy-spend migration
complete, and before readiness is reported. Recovery does not run as a
constructor side effect, and a production role without its trigger hook fails
startup rather than reporting readiness.

Grace in the domain is semantic (`immediate` or `stale`); application
configuration supplies the stale duration. Periodic failure is logged and the
control loop continues. Boot and wake retain their fail-closed behavior.

Operation-local reconcile-first protocols for durable effects and accounting
remain with those operations. GitHub/network fail-soft polling and spend-cap
admission are steady-state loop policy, not recovery triggers.

## Rejected alternatives

- A stateful resume manager owning processes, money, effects, network, and
  admission would duplicate existing authorities and blur fail-closed recovery
  with fail-soft polling.
- Calling the complete boot recovery sequence for every trigger would make a
  wake or periodic resync destructive to live paid work.
- Keeping separate wake and periodic callbacks would leave the trigger-to-arm
  mapping implicit and permit the same missing-edge and wrong-arm regressions.

## Consequences

- Adding a trigger requires an explicit coordinator mapping and composition coverage.
- Boot, wake, and periodic behavior are covered through observable
  orchestration outcomes.
- The coordinator remains stateless orchestration over existing durable state;
  no new recovery authority or state machine is introduced.
