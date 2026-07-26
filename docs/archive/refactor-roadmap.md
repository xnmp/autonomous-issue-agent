# Python-Owned Harness Refactor Roadmap

The refactor is intentionally incremental. Each phase should keep the harness
working and preserve existing behavior unless a later PR explicitly changes a
documented interface.

## Phase 1: Characterize Current Behavior — Done

- Add workflow-level tests that assert durable outcomes rather than private
  choreography.
- Add runner contract tests for `AgentRuntime.run(task) -> AgentResult`.
- Keep existing detailed regression tests while new seams are introduced.

## Phase 2: Model Lifecycle Explicitly — Done

- Added issue and PR lifecycle value objects that normalize label-backed phases.
- Moved retry breaker and PR escalation invariants into lifecycle helpers while
  preserving existing actions.

## Phase 3: Extract Workflows — Done

- Moved fix, review, steward, and Jira dispatch branches out of the central
  dispatcher into role-specific workflow modules.
- Left the dispatcher as a thin router from `AgentKind` to workflow.
- Added role-specific workflow config objects so tests can target workflow seams
  directly instead of dispatcher internals.

## Phase 4: Move Agent Task Planning Out Of Domain — Done

- Moved prompts, JSON schemas, MCP wiring, model/budget selection, and tool
  policy into application planners and runner adapters.
- Kept domain focused on lifecycle, world snapshots, actions, labels, CI/review
  state interpretation, and outcomes.

## Phase 5: Make Python The Default Runtime — Done

- Makefile run targets and service units point at Python role entrypoints.
- Shell compatibility targets load the existing shell-style config files and
  delegate to Python role entrypoints.

## Phase 6: Consolidate Governor Policy — Done

- Kept shell hook wrappers where Git requires them.
- Centralized command parsing, bypass detection, and policy decisions so shell,
  Claude SDK, and Codex paths share one policy engine.

## Phase 7: Validate Config And Integrations — Done

- Split flat harness config into repository, path, role, Jira, and Codex runtime
  config groups.
- Added deterministic validation for retry caps, budgets, runtime names, and
  Codex transport settings.
- Added explicit Jira sync plan/outcome decisions for skip/create/transition,
  retry, and persist behavior.

## Phase 8: Adapter Seam Hardening — Done

- GitHub write command construction is pure and directly tested.
- Store state handles malformed numeric files with safe defaults.
- Worktree discovery uses exact issue-directory parsing rather than brittle
  substring matching.
- Claude and Codex result normalization share contract tests for structured
  success, blocked, error, and transport failure semantics.

## Phase 9: Remaining Cleanup

- Completed cleanup packaged into reviewable PRs/commits.
- Dispatcher tests now focus on route/composition smoke coverage; durable
  behavior assertions live at workflow/domain seams.
- Dispatcher config now carries only workflow-owned fields; future config
  additions should stay role-scoped rather than pass-through dispatcher fields.
- Continue running the full validation matrix before future architecture PRs.

## Test Migration Policy

- Keep tests that assert durable behavior or known regressions.
- Rewrite tests that only assert private structure once a replacement behavior
  test exists.
- Delete old tests only in the same change that proves equivalent coverage.
