# PLAN — full remediation of the 2026-07-25 adversarial review

Audience: a fresh working session (orchestrator = Fable, implementation waves =
Opus/Sonnet subagents). This document is the complete handoff: context, the
design decisions already made (do not re-litigate them), wave-by-wave specs
with file ownership, and the verification protocol.

Prerequisite reading, in order:
1. [REVIEW-2026-07-25-adversarial.md](REVIEW-2026-07-25-adversarial.md) — the
   findings and their evidence. This plan references findings by ID (C1–C5,
   M1–M23, T1–T6, S1–S8) and does not restate evidence.
2. [HANDOVER-2026-07-25-lean-core.md](HANDOVER-2026-07-25-lean-core.md) — what
   the lean core is; **read the [CORRECTED] annotations**, three of its claims
   were falsified by the review.
3. `CLAUDE.md` (repo root) — how the harness works operationally.

## 0. Context snapshot (cold start)

- Repo: `autonomous-issue-agent`, branch `main`, tree clean at the leg-3
  commit (`feat(observability): P1-lite journal + cost ledger + …`). Full
  `make test` green: 848 pytest tests + `tests/test_governor.py` +
  the two bash suites + import-linter.
- The harness has **never run against a real repo**. The goal of this plan is
  to make that safe: the review's verdict was "architecture sound, no
  structural refactor, but a P0 list silently disables the harness or its
  safety layer".
- Facts a cold session must NOT assume from older docs:
  - The push gate on the default SDK path is covered by **nothing** (bash
    checkers run only under `FIXER_RUNTIME=codex`).
  - The T3 convergence suite guards the decide/**read** side only;
    `tests/adapters/test_fix_workflow.py` is the write-side guard.
  - The journal/ledger cannot currently see pre-result deaths (C3) or any
    Codex spend (M1); `issue-agent status` reads the fix journal only (M11).
- Scope of this plan: **every review finding**, sequenced P0 → P1 → P2.
  P0 must land (and pass re-review, §5) before `issue-agent start` /
  `make point-at` is pointed at any real repo.

## 1. Ground rules (the execution pattern that worked, plus review lessons)

1. **Fable owns the shared seams**; waves of 3–6 Opus/Sonnet subagents run on
   **strictly disjoint file sets** with the conventions brief (§2) in every
   prompt. Fable-owned in this plan: the C3 counter design (already specified
   in D2 — implementation may be delegated but Fable reviews the diff), and
   all of P2's structural work (S1 lifecycle, S2 FixLeg, M18 extraction).
2. **One commit per wave**, only after full `make test` is green. Do not
   batch waves into one commit — each wave is a revert point.
3. **Every behavioural fix lands with its regression test in the same leg.**
   For anything touching a retry counter or dispatch leg: a
   `test_convergence.py` scenario row **and** a `test_fix_workflow.py`
   write-side test (the review proved the row alone is insufficient).
4. **Preserve the review's §7 "healthy" list verbatim** — especially: domain
   purity (import-linter must stay green), the single write funnel (fixes
   route writes *into* `Executor`, never add new bypasses), incident comments
   (move with their code, unedited), fail-safe polarities on the review path.
5. **No opportunistic refactoring inside P0/P1 legs.** Structural change is
   P2, done deliberately. A P0 agent that sees an adjacent smell reports it in
   its final message; the orchestrator queues it.
6. Subagents must keep any child work **blocking** (known harness deadlock
   with background grandchildren). Worktree agents verify
   `git merge-base HEAD main` against main's real tip before starting; the
   orchestrator re-verifies before merging. If waves run sequentially with
   in-place edits (recommended — they're fast), worktrees are unnecessary.
7. Model routing: Opus for legs with judgement (A1, A3, B1, B2, P1-e, P1-f);
   Sonnet for mechanical/test-heavy legs (A2, A4, B3, B4, B5, P1-g, doc
   legs); Fable only for seam design and diff review.

## 2. Conventions brief (embed verbatim in every implementation-agent prompt)

- Hexagonal: `domain/` pure (no I/O, env, clock — import-linter enforces),
  `adapters/` I/O, `app/` wiring. New pure logic goes in `domain/`; new I/O
  behind an existing adapter or an injected callable.
- Decisions are data: return `Action` tuples; all GitHub writes go through
  `Executor.execute` (`app/execute.py`). Never call `gateway.relabel`
  directly from a workflow. Labels via `domain/labels.Label`, never raw
  strings.
- Impure gate rules follow the higher-order pattern: pure policy function
  taking injected callables (`check_tests(test_cmd, run_tests, …)` is the
  template).
- Error classification: `GhUnavailable` means "could not look", never "empty
  world". Per-item fetch isolation follows `_safe_get_pr`'s shape: isolate
  timeouts/ordinary errors per item, propagate systemic failures.
- Persistence: `Store` accessors; atomic tmp+`os.replace` writes; journal
  writes must never be able to fail a run (swallow + one stderr line; build
  the whole line before a single `write()`).
- Tests: behavioural — world-in → actions-out; assert on observable outcomes
  ("the runner never started", "the label moved"), not internals. Fakes must
  be able to *fail* like the real thing (raise `GhUnavailable`, timeout).
  Every negative test gets an anti-vacuity twin. Name tests after the
  incident/finding they guard (e.g. `# review C3`).
- Comments: keep the existing incident-comment style; when moving code, move
  its comment verbatim. New constraints get one comment stating the failure
  they prevent, citing the review ID.
- Env knobs: document new knobs in `config/agent.env.example`; after wave A
  lands, `compose-env.sh` emits everything automatically (no allow-list to
  edit).
- Report out-of-scope observations in your final message; do NOT edit files
  outside your assigned set.

## 3. Decisions already made (do not re-litigate; record deviations in §6)

- **D1 (C1)** New `src/issue_agent/domain/identity.py` with
  `normalize_login(login: str) -> str` (strip one trailing `[bot]`,
  casefold) and `login_eq(a, b) -> bool`. `review._login_eq` moves there
  (docstring verbatim); `ci.py:58`, `fix.py:335`, `fix.py:137`, and all
  `review.py` sites import it. Belt-and-braces: `HarnessConfig.from_env`
  leaves values as written (display) but `validation_errors()` warns when
  `FIX_BOT` and `REVIEW_BOT` normalize equal under `IDENTITY_MODE=three`.
  No `Login` value-object for now (too invasive for P0).
- **D2 (C3)** Three parts.
  (a) `JournalingRuntime.run` emits `agent_run_start`
  (role/kind/number/issue/model) *before* the await; the await is wrapped so
  an exception emits a settled `agent_run` with `status="raised"`,
  `is_error=True`, `exit_reason="transport_death"`, `cost_usd=0.0`, then
  **re-raises unchanged** (the crash-isolation contract at
  `agent_runner.py:522-531` stays intact).
  (b) `repeated_dispatch_warning` counts `agent_run_start` events too, so
  pre-result-death loops trip the detector.
  (c) New bounded infra-death counter: store suffix `infra-deaths` per issue.
  Bumped in `AgentDispatcher.dispatch` when the handler raises (bump →
  re-raise) AND in `FixWorkflow` when a returned result has
  `infra_death(exit_reason)`. Read into `FixState.infra_deaths`
  (`roles._fix_state`); `decide_fix` escalates via the existing
  `issue_escalation` when `infra_deaths >= cfg.max_infra_deaths`. New knob
  `MAX_INFRA_DEATHS`, default 5, 0=off;
  `DispatchConfig`-style inert default does not apply (this lives in
  `FixConfig` like `max_resolve_retries`). Cleared with the other issue state
  on merged-PR cleanup (D8). "Infra deaths are not attempts" stays true for
  the *retry* breakers; this is a separate, higher ceiling.
- **D3 (C5)** `compose-env.sh` stops allow-listing: capture the sourced
  subshell's environment with the same `env -0` technique `_load_agent_env`
  uses, diff against the pre-source environment, emit every var the two files
  defined (keep `%q` quoting). `app/main.py` calls `os.environ.update(env)`
  with the loaded dict before building anything (matches foreground
  semantics: overlay wins). Also in `main.py`: replace the `__dict__` rebuild
  with `dataclasses.replace`, load env once and pass it down (kills the
  double `bash -c` subprocess).
- **D4 (M10)** `ReviewWorkflow` gains an `executor` (constructor param,
  wired in `dispatch.py`); both escalation sites emit
  `pr_escalation(...)` actions through `executor.execute_all` using
  `Label.NEEDS_HUMAN_REVIEW`. `PrEscalation` gets a `from_label` param for
  the configurable removed label, mirroring `IssueEscalation`. Folding
  `submit_review` into the funnel is **P2**, not P0.
- **D5 (M11)** `cli.journal_path(name)` → `journal_paths(name) ->
  list[str]` over `STATE_DIR`/`REVIEW_STATE_DIR`/`STEWARD_STATE_DIR`
  (overlay values, with the synth defaults as fallback);
  `spend_status_lines` merge-sorts events by `ts`.
- **D6 (M13)** Two new gateway methods:
  `failed_run_log(pr) -> str` (log of the newest failed run for the PR's head,
  last 150 lines — port `agent/fix-ci.sh:42`'s `gh run view --log-failed`
  approach) and `review_comments(pr) -> str` (inline comments formatted
  `path:line (author): body`, port of `agent/address-review.sh:41`). Wired at
  `workflows/fix.py:236-239` and `:208-210`. Both fail soft: on any error
  return `""` (the prompt then renders what it does today — never let a log
  fetch kill a dispatch). Workflow-level tests assert the fetched text
  appears in `task.prompt`.
- **D7 (M5)** Nothing real is deployed, so change the default:
  `STATE_DIR` unset → `~/.local/state/issue-agent/<repo-slug>` where
  repo-slug = `owner-name` sanitized (same for the review/steward twins with
  their prefixes). `doctor.sh` additionally fails when two overlays under
  `config/repos/` resolve to the same state dir.
- **D8 (M4/M3)** `store.py` defines `_ISSUE_SUFFIXES` / `_PR_SUFFIXES`
  module constants; accessors and `clear_*` both derive from them (test
  asserts every suffix literal used by an accessor is in exactly one
  cleaner). Fix the `automerge` mismatch (it is a PR suffix). Wire cleanup:
  `Executor` gains an injected `clear_issue_state` called on the
  `RemoveWorktree` arm **for merged-PR cleanups only**; on a
  closed-unmerged requeue, clear `session`/`phase`/`resumed-at` but KEEP
  `resolve-retries` (preserves handover §5.7's deliberate conservatism —
  encode this distinction in `cleanup_actions` via a flag on the action or a
  second action; keep it minimal). `_write` gains `f.flush()` +
  `os.fsync` + pid-suffixed tmp name.
- **D9 (M22)** Delete the dead busy-marker mechanisms (the PR-side
  `ai-working` removes in `outcome.py`, `reap_actions`' PR arm, the
  `is_working` conjunct at `fix.py:455`, the `fixer_busy` plumbing) and
  document in `decide_fix`'s docstring that single-fixer is enforced by
  process topology (`single_instance` + synchronous dispatch). Revisit only
  with P2 parallelism. (P1 item.)
- **D10 (S3)** Prompts stay in Python (typed interpolation beats
  editability). The S3 split moves code between modules only.
- **D11 (M6/M7)** Strategy for the duplicated gate stack:
  P0 wires the push gate (`OpenPrLookup` probe). P1 fixes the bash three-dot
  bug, ports the ADR rule + lint/typecheck commit arms to the Python side,
  and adds a **parity test** (golden cases through `domain/gate.py` rules AND
  the bash checkers in a scratch repo, asserting identical verdicts — this is
  the sync mechanism the stack lacks). Full consolidation (bash shells out to
  Python, as `bash_governor.py` already does for the policy core) is P2,
  bundled with the M8/P6.1 legacy partition.
- **D12 (M1)** Codex cost: new knobs `CODEX_COST_PER_MTOK_IN` /
  `CODEX_COST_PER_MTOK_OUT` (USD per million tokens, default 0). When >0,
  `parse_codex_envelope` computes `cost_usd` from the envelope's token usage;
  when 0 and a spend cap is configured, `roles.py` logs ONE loud startup
  warning that caps cannot see codex spend. Budget flag: `build_codex_argv`
  has no SDK budget analogue — the wall cap remains the bound; note it in the
  same warning.
- **D13 (T-fixes)** `test_convergence._after_run` is replaced by driving the
  **real `FixWorkflow`** with `FakeGateway` + real `Store(tmp_path)` + a
  scripted runner; add a scenario row whose runner **raises** (guards C3).
  The module docstring and handover §7 instruction are corrected (already
  done for the handover).
- **D14 (M23)** Fence `title`/`body`/`diff` in `build_review_task`,
  `build_re_review_task`, and round-1 `build_delegated_review_task` with the
  existing `_fenced`; add one test iterating every `build_review*` builder
  asserting the fence marker wraps each untrusted field.

## 4. Wave plan

Waves are sequential; agents within a wave are parallel and own disjoint
files. "Files" lists ownership — an agent touches nothing outside its list
(tests included). Each wave ends: full `make test`, Fable diff review, commit.

### Wave P0-A — foundations (5 agents, parallel)

| Agent | Findings | Files owned |
|---|---|---|
| A1 | C2, M19, M13 (gateway half) | `adapters/github.py`, `tests/adapters/test_github.py` |
| A2 | M2, M5(D7), S7-POLL_SECONDS, env-example gaps | `app/config.py`, `tests/adapters/test_config.py`, `config/agent.env.example` |
| A3 | C3(a)(b) (D2), S7-journal-`__getattr__`, T6-atomicity-test | `adapters/journal.py`, `tests/adapters/test_journal.py` |
| A4 | C5 (D3), S7-`dataclasses.replace`, doctor shared-state check | `bin/compose-env.sh`, `app/main.py`, `bin/doctor.sh`, `tests/adapters/test_config.py::_load_agent_env` tests (coordinate: A2 owns the file — A4 hands its test cases to A2 or lands second) |
| A5 | C1 (D1) | `domain/identity.py` (new), `domain/ci.py`, `domain/fix.py` (comparison sites only), `domain/review.py`, `tests/domain/test_fix.py`, `tests/domain/test_review.py`, `tests/domain/test_ci.py` |

Per-item acceptance criteria:
- A1: every list call in `build_world`/`pr_for_issue` carries
  `--limit`+warning (add the `_gh_json` guard rejecting list subcommands
  without `--limit`); `_safe_get_issue` mirrors `_safe_get_pr` including the
  timeout-vs-rate-limit blast-radius test; the two new methods exist with
  error-path tests (gh failure → `""`).
- A2: malformed numerics land in `validation_errors()` (not tracebacks);
  empty string = unset for numerics AND `_b`; `from_env` defaults read from
  the dataclass fields (one source; fixes the 60-vs-20 POLL_SECONDS drift —
  decide which value wins: **20**, the production literal); state-dir
  defaults per D7; `MAX_RESOLVE_RETRIES`, `LOG_DIR`, `MAX_INFRA_DEATHS`,
  `CODEX_COST_PER_MTOK_*` documented in the example file.
- A3: mutation "delete the try/finally" fails a test; a raising inner runner
  yields `agent_run_start` + settled `agent_run(transport_death)` and
  re-raises; `repeated_dispatch_warning` trips on starts alone; multiline
  field → exactly one JSONL line.
- A4: a scratch shared.env+overlay pair with a knob OUTSIDE the old
  allow-list (e.g. `CI_IGNORE_CHECKS`) survives compose → `.active.env` →
  `_load_agent_env` → `os.environ`; doctor fails on two overlays sharing a
  state dir.
- A5: the review's C1 reproduction (world with `FIX_BOT='fix-bot[bot]'`) now
  yields the ADDRESS_REVIEW dispatch; a test pins `login_eq("a[bot]", "a")`
  and the three call sites via behaviour (not identity assertions).

### Wave P0-B — wiring (6 agents; B1 is Fable-designed, may be delegated with diff review)

| Agent | Findings | Files owned |
|---|---|---|
| B1 | C3(c) (D2), M10 (D4) | `app/dispatch.py`, `app/workflows/review.py`, `domain/lifecycle.py`, `domain/fix.py` (FixConfig/FixState fields + escalation read), `adapters/store.py` (infra-deaths accessor only), `app/roles.py` (`_fix_state` read), `tests/domain/test_convergence.py` (new rows), `tests/adapters/test_review_workflow.py`, `tests/adapters/test_dispatch.py` (handler tests only) |
| B2 | M13 (workflow half) | `app/workflows/fix.py`, `tests/adapters/test_fix_workflow.py` |
| B3 | M11 (D5), M12 | `app/cli.py`, `tests/adapters/test_cli.py` |
| B4 | M3, M4 (D8), S7-`execute_all`-isolation | `adapters/store.py` (coordinate with B1: B1 adds one accessor; B4 owns the rest — land B1 first), `app/execute.py`, `domain/fix.py::cleanup_actions`, `tests/adapters/test_store.py`, `tests/adapters/test_execute.py` |
| B5 | C4, T3 | `tests/adapters/test_roles.py` (new), `tests/adapters/test_dispatch.py` (FakeGateway `fail_on` — coordinate with B1; FakeGateway section only) |
| B6 | Push gate (D11 P0 half) | `adapters/worktree.py` (`build_gate_context` gh probe), `adapters/governor.py` (`GateContext.open_pr: OpenPrLookup`, `_push_gate` uses `push_gate_applies`/`resolve_open_pr`), `tests/adapters/test_governor_adapter.py`, `tests/adapters/test_worktree.py` |

Sequencing inside the wave: B1 lands first (touches shared files), then
B2–B6 in parallel.

Per-item acceptance criteria:
- B1: the C3 interleaving from the review, simulated with a raising fake
  runner, now (i) journals both events [A3], (ii) bumps `infra-deaths`,
  (iii) escalates to `needs-human-review` after `MAX_INFRA_DEATHS`, (iv)
  fires the escalation journal event + `ALERT_CMD` via the funnel. Review
  escalation test: rejection-ceiling escalation produces an `escalation`
  journal event (previously none). Convergence gains the raising-runner row.
- B2: workflow-level assertions that `failed_run_log`/`review_comments`
  content reaches `task.prompt`; gateway error → prompt renders without the
  section, dispatch survives.
- B3: status merges three journals by `ts` (fixture with events in all
  three); overlay resolution goes through the shell-semantics reader —
  the M12 repro table (trailing comment, spaces around `=`, `${VAR}`) all
  resolve correctly; `resolve_name` warns loudly before synthesizing a new
  overlay when an existing overlay matches `TARGET_DIR`.
- B4: fsync mutation caught (monkeypatch `os.replace` to raise post-write →
  previous value still readable — the honest version of the T2 tautology);
  suffix-coverage test (every accessor suffix in exactly one cleaner);
  merged-PR cleanup clears issue state incl. `infra-deaths` and
  `pr-N.automerge`; closed-unmerged keeps `resolve-retries`; `execute_all`
  failure in action N still executes N+1 and misattribution is gone (log
  names the failing action).
- B5: the review's M2-mutation (gut `_fix_state`) now fails tests; round-trip
  per counter (workflow write → `_fix_state` read → `decide_fix` sees it);
  the two `breaker_key` implementations agree on issueless PRs (explicit
  test); one mid-dispatch `GhUnavailable` test per workflow (money spent →
  session persisted, label recoverable).
- B6: with a fake probe returning a PR, pushing to the governed branch runs
  the push-gate rules; probe error → `OpenPrLookup.ERROR` → fail closed with
  the retry path `gate.py` already defines; probe says no PR → allowed.
  Delete the stale `--body-file` comment at `governor.py:97-98`.

**P0 exit gate:** all waves committed; §5 re-review clean; `bin/doctor.sh`
green against a scratch target repo; a manual systemd smoke test on the
scratch repo shows overlay knobs visible in a child process
(`ps eww`/`/proc/<pid>/environ`).

### Wave P1-1 — correctness follow-ons (parallel)

| Agent | Findings | Files owned |
|---|---|---|
| C1a | M14, M15, M16, D9(M22) | `domain/fix.py`, `domain/outcome.py`, `domain/lifecycle.py` (IdentityMode enum home: `domain/review.py` or new), `app/config.py` (validation entry), `tests/domain/*` incl. the missing fifth `test_followup.py` row |
| C1b | M17, M1 (D12) | `adapters/codex_runner.py`, `app/roles.py` (startup warning), `tests/adapters/test_codex_runner.py` |
| C1c | M20, M21 | `adapters/worktree.py`, `adapters/governor.py` (gate ordering only), tests |
| C1d | M23 (D14) | `app/planners/tasks.py` (fencing only), `tests/adapters/test_task_planners.py` |

Notes: C1a's automerge fix = move the 3c block to its own loop over
`open_prs` (the review reproduced idle-vs-busy divergence — that repro is
the test). M16: `IdentityMode` enum with `from_env` mirroring
`ReviewMode`, casing-typo test (`'Three'` → validation error, not silent
`[]`). C1c ordering: cheap structural checks before the two suite-running
checks; short-circuit on first denial (callers already use `reasons[0]`).

### Wave P1-2 — gate parity + test-suite repairs (parallel)

| Agent | Findings | Files owned |
|---|---|---|
| C2a | M6 (D11 P1 half) | `plugin/hooks/checks/*.sh` (three-dot split; drop rules ported away or keep both — parity test decides), `domain/gate.py` (+ADR rule, +lint/typecheck arms), `adapters/governor.py` (wire new rules), new `tests/test_gate_parity.py`, `plugin/AGENT.md` + `bin/doctor.sh` (make claims match reality) |
| C2b | T2, T4 | `tests/domain/test_fix.py` (closing-ref test), `domain/gate.py` coordination with C2a (AC-heading single-source, `None` contract, evidence-ref intersection — C2a owns the file; C2b writes specs+tests, C2a implements, or run C2b after C2a) |
| C2c | T5, D13 | `tests/adapters/test_config.py` (assertion style), `tests/adapters/test_task_planners.py` (prompt-substring pruning), `tests/adapters/test_runtime_entrypoints.py` (render-then-parse), `tests/domain/test_convergence.py` (real-workflow `_after_run`) |
| C2d | M9 | `tests/test_governor.py` (strict xfail bypass block) |

Sequencing: C2a before C2b (same file). The parity test is the wave's
centerpiece: golden cases (good PR, bad base, missing AC, three-dot
base-advanced repo, evidence rules) run through both stacks; disagreement =
failure. It permanently replaces the "keep in sync" comments as the sync
mechanism.

### Wave P2 — structural (sequential legs, Fable-designed; execute only after ≥1 week of real-repo operation unless noted)

1. **S1 lifecycle transitions** (Fable): `transition(phase, event) ->
   Relabel` as the only constructor of lifecycle relabels; `IssuePhase`
   becomes load-bearing; fixes handover-gap-#5's guess-the-label shape.
2. **S2 FixLeg descriptor** (Fable): co-locate kind/cap/counter-key/tail/
   counts-as-attempt; collapses the 9-file leg shotgun; convergence +
   write-guard become structural.
3. **M18 runner extraction**: one `observe_with_caps` helper; contract test
   extended to cap/budget/exit-reason parity through a fake clock; fix both
   timeout paths dropping `session_id`.
4. **S3/S4 module splits**: diff-slicer → `domain/diff.py`; deny/tool lists →
   `planners/policy.py`; builders by role; cli → `cli/{test_discovery,
   status,overlay}.py`. Mechanical, Sonnet-able.
5. **S5 executor wiring** (`set_agent_runner` raising when unset), S6
   selector consolidation (`latest_review_body` → `domain/ci.py` with
   author filter, public `issue_for_pr`, shared `review_retry_key`,
   branch-prefix regex from `refs.BRANCH_PREFIXES`), S7 leftovers (enums for
   stringly state, `AgentResult` constructors, triage prose → structured
   intents, `world.py` policy extraction, app_token atomic write + memoise,
   lock out of `/tmp`, `parse_triage_verdict` in domain, dispatch handler
   exhaustiveness assert, `_run_logged` → `LoggingRuntime`).
6. **Ledger scaling**: rotation + a cached daily-total (the hot-path re-parse
   bites before disk does). Trigger: dispatch latency or file >~50MB.
7. **M8 + P6.1 legacy deletion** (now upgraded from "opportunistic": the
   bootstrapped governor fails open — a trap, not a fallback). Partition per
   the review: `agent/ review/ steward/ bin/bootstrap.sh
   tests/test_orchestration.sh tests/fakes/` are deletable now;
   `plugin/hooks/checks/` is NOT (live for codex) until C2a's parity/port
   work makes bash shell out to Python or codex mode is dropped.
8. **S8 + P2 options**: AGENT.md step references; review-state derivation
   from GitHub (`Review.commit_id` @ head OID) replacing
   `pr-N.last-sha`/`round`; `submit_review` through the funnel.

## 5. Verification protocol

- Per wave: full `make test` (includes import-linter; domain purity must stay
  machine-clean), then Fable reads the complete diff before commit.
- **After P0 (mandatory, user's high-risk rule): adversarial re-review** —
  2–3 Opus subagents on the P0 diff with the review doc as context, prompted
  to refute that each C-finding is actually closed (e.g. re-run the C1
  repro, the M2-gut mutation, the C3 interleaving). Findings verified before
  merge is declared done.
- Mutation spot-checks that MUST now fail (they all passed green pre-plan):
  M1 (delete workflow breaker bump), M2 (gut `_fix_state`), M3 (de-atomicize
  `_write`), M4 (reverse `_issue_for_pr` preference).
- P0 exit additionally requires the systemd smoke test (§4 P0 exit gate).
- Track completion against the review doc: every finding ID appears in
  exactly one wave above; when a wave commits, tick its IDs off in the
  commit message (e.g. `fix(p0-a): C2 M19 M13-gateway …`).

## 6b. EXECUTION RECORD (2026-07-26) — what actually happened

This plan was executed end-to-end. Commits, in order:

| Commit | Wave | Notes |
|---|---|---|
| `44fbd16` | P0-A | C1 C2 C3(a)(b) C5 M2 M5 M19 M13-gateway + S7/T6 |
| `9ecb0ae` | P0-B | C3(c) C4 M3 M4 M10 M11 M12 M13-workflow T3 + push gate |
| `299c825` | P1-1 | M14 M15 M16 M22 M17 M1 M20 M21 M23 |
| `f0b4040` | P0R | the 5 P0s found by the re-review of P0 |
| `cebf088` | P1R | the 18 P1s found by the re-review |
| `e9024ef`, `dd805eb` | P1-2a | M9 xfails, handover/FAILURE-MODES updates |
| `25e257a` | — | decay/read key-set gap found by the verification sweep |
| `1968888` | P1-2 | M6 gate parity, D13 convergence, T2/T5 repairs |

**The plan's §5 protocol worked, and its most important step was the one that
is easy to skip.** The P0 exit gate passed cleanly — all four mutation
spot-checks failed as required, doctor was green, the env-delivery smoke test
proved C5 closed. The adversarial re-review then found **five more P0s**, two of
which the P0 waves had *introduced*. A third such gap (between two fixes in the
same commit) was found later by a verification sweep. All three were **absence
bugs**: a var no longer emitted, a failure no longer counted, a key no longer
cleared. Mutation testing cannot catch these — it proves a guard fires when code
changes, never that a guard exists for a path nobody wrote a test for. If you
execute a plan like this again, budget for the re-review as a mandatory phase,
not a formality, and write ABSENCE tests as you go (remove the handling, assert
a loud failure) and set-equality assertions for anything that emits a set.

**Deviations from §3 worth carrying forward:**
- D5: `cli.journal_path` kept as a compat shim beside `journal_paths` (a test
  file no wave owned still calls it). Migrate and drop the shim.
- D9: `fixer_busy` and its reap gating were KEPT (the plan said delete); the
  parameter still truthfully gates reap during a dispatch tick, and its call
  site was outside the owning agent's file set.
- D2: infra deaths now DECAY on any leg that returns a result — the plan
  specified only the ceiling. Without decay it was a lifetime tally.
- The reviewer and steward got ceilings the plan never scoped (P0R-3); the
  review role's unbounded fan-out was the largest money exposure in the tree.
- P0R-5 was fixed at its root (the reaper repairs an `ai-working` issue that
  already has an open PR instead of requeueing it) rather than by adding a
  counter to the swallow path the symptom appeared on.

**Known-open, deliberately:** the `sh -c`/`bash -c`/`eval`/`xargs` governor
bypass (M9 — now five `xfail(strict=True)` tests with the root cause recorded
in the re-review doc §4); the three asserted parity divergences between the two
gate stacks; `$0`-cost plans still make the spend caps blind (the harness now
says so loudly); and P2 structural work (S1–S8, M18), which the plan gates on a
week of real-repo operation.

## 6. Open items for the executing session

- Record any deviation from §3 decisions here (or in the commit message) with
  one line of why — the next handover reads this section.
- After P0+P1: update HANDOVER's [CORRECTED] notes to "fixed in <commit>"
  and refresh FAILURE-MODES with new entries for C1–C3 (they are new
  failure-mode classes: silent identity mismatch, silent truncation,
  unbilled loop).
- Then resume the original operational plan: point at 2–3 real repos under
  systemd, let the (now trustworthy) ledger run a week, walk the
  IMPROVEMENT-PLAN §4 backlog by trigger.
