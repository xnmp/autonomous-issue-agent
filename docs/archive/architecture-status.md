# Architecture Status

This repository is in a migration from a Bash-owned harness to a Python-owned
harness. The target architecture is Python workflow orchestration with shell kept
only where the runtime requires it.

## Current Runtime State

- Python role entrypoints are the default watcher runtime in Makefile,
  systemd, and launchd wiring.
- Shell remains available for compatibility entrypoints, Git hooks, installers,
  and operational glue scripts.
- Python contains the workflow, domain, adapter, runner, planner, config, and
  lifecycle seams.
- Codex execution is expected to remain a subprocess call to `codex exec`; that
  does not require lifecycle orchestration to stay in Bash. Codex and Claude both
  satisfy the Python `AgentRuntime` contract.
- Git hooks remain shell entrypoints where Git requires them, but policy is now
  shared through the Python governor policy module used by hook, SDK, and Codex
  paths.

## Target Runtime State

- Python is the source of truth for polling, lifecycle decisions, retries,
  worktree setup, GitHub transitions, state markers, task planning, and runner
  dispatch.
- Shell watcher targets are compatibility wrappers that load shell-style config
  and delegate to Python role entrypoints.
- Codex and Claude are adapters behind the Python `AgentRuntime` interface.
- Installers, service files, and hook wrappers may stay shell-based when they are
  primarily operating-system or Git integration glue.

## Refactor Rule

Preserve behavior before changing structure. Add characterization or contract
tests at the intended seam first, refactor behind that seam second, and remove
old brittle tests only after equivalent behavior coverage exists.

## Not Yet Implemented

- No open architecture-roadmap items are tracked here; future work should start
  from a new review or ADR rather than re-opening completed migration phases.

## Implemented Refactor Slices

- Characterization and contract tests protect fix workflow behavior and shared
  `AgentRuntime` result semantics.
- Issue/PR lifecycle value objects centralize label-backed phases, retry breaker
  policy, and PR human-escalation actions.
- Fix, review, steward, and Jira workflow modules own role dispatch behavior;
  `AgentDispatcher` is a thin router/composer.
- Agent task planning, JSON schemas, MCP wiring, model/budget selection, and
  tool policy live in `issue_agent.app.planners`, not the domain package.
- Python watcher entrypoints are the default in Makefile and service templates;
  shell compatibility targets delegate to the Python runtime.
- Governor policy is centralized in `issue_agent.adapters.governor_policy` and
  re-exported by the shell hook wrapper.
- Harness config is grouped into repository, path, role, Jira, and Codex runtime
  projections with deterministic validation at startup.
- GitHub, store, worktree, runner, and Jira seams have focused contract tests for
  command construction, malformed state, worktree idempotency, result mapping,
  and sync outcomes.
- Workflow seam tests now own review, multi-review, delegated review, fix,
  split-authoring, CI-fix MCP, steward triage, and Jira sync behavior formerly
  duplicated through dispatcher tests.
- Dispatcher construction no longer carries stale repo/fix-bot compatibility
  fields; role-specific config is projected into workflow config objects.
- Python entrypoints load `AGENT_ENV` shell-style config files, preserving the
  existing operator config format used by service units and compatibility wrappers.
