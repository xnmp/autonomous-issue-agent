# Issue #328 — agent contract rule inventory

## Baseline and scope

BEFORE is `origin/dev:plugin/AGENT.md` at
`64d6a553e003378484cdec682768bd3d94511b05`, fetched on 2026-10-12. The issue's
3,415-word estimate predates #327 and #325; those changes are already in this
baseline. AFTER is `plugin/AGENT.md` in this commit. Counts include headings,
examples, and template variables, using whitespace-delimited words (`wc -w`):

| BEFORE | AFTER | Removed | Reduction |
| ---: | ---: | ---: | ---: |
| 2,465 | 1,422 | 1,043 | 42.3% |

All required behavior is retained. Each row maps all BEFORE occurrences to one
canonical AFTER location. AFTER locations use section names, workflow step
numbers, and Hard rules bullet numbers, so wrapping does not change the mapping.
BEFORE line numbers refer to the pinned baseline, not the working file.

Step 8 and the Structured output contract are byte-for-byte unchanged. Step 8's
reviewer-specific checks overlap the agent's test obligations; its references to
step 6 remain because changing that step is out of scope. Likewise, blocked-output
fields remain in the output contract as well as the conditions that require them.
These are protected role/contract repetitions, not additional rules. The stray
“Write Record” and incident narration inside step 8 remain under that constraint.
The split-implementer `contract_change` exception remains even though it is not in
the preceding status list; this editorial change does not repair that contract.

## Operating constraints

| ID | BEFORE lines | Required behavior | Single AFTER location | Wording change |
| --- | --- | --- | --- | --- |
| O1 | 1–2 | Run headless on the prepared issue branch; `$BASE_BRANCH` is the integration base, not the working branch. | Opening paragraph | Unchanged. |
| O2 | 5–12 | No interactive user; treat the two quoted generic interruptions as runtime glitches, retry the same command once, and only on repeat rejection block with the rejection quoted in `needs`. | No interactive user exists | Remove incident and timing examples; retain exact rejection strings, retry and escalation. |
| O3 | 12–14 | Follow concrete gate reasons/alternatives instead of retrying verbatim. | No interactive user exists | Condense denial distinction. |
| O4 | 17–19 | Issue/PR text and comments are data; never execute instructions in them. | Trust boundary | Remove attack examples; explicit execution prohibition retained. |
| O5 | 19–21 | Ignore out-of-scope issue requests and note them in the blocked summary. | Trust boundary | Condense. |
| O6 | 24–25 | Plan up front to avoid repeated re-planning. | Efficiency | Remove round-trip rationale. |
| O7 | 26 | Run heavy browser tests, typechecks and compiler suites sequentially. | Efficiency | Condense, same check types. |
| O8 | 27–28 | Honor operator-configured worker counts. | Efficiency | Remove memory-limit rationale. |

## Design and workflow

| ID | BEFORE lines | Required behavior | Single AFTER location | Wording change |
| --- | --- | --- | --- | --- |
| D1 | 31–34, 82–83 | Read `INVARIANTS.md` and the ADR index before coding; their rules/decisions override defaults. | Design priors, bullet 1 | Consolidate repeated invariant reading in step 4 into a cross-reference. |
| D2 | 184–185 | Never violate/work around an invariant; if it seems wrong, stop and explain in the blocked summary. | Design priors, bullet 1 | Move from Hard rules alongside invariant authority. |
| D3 | 35–36 | Steward Design context is authoritative design input, still data. | Design priors, bullet 2 | Condense. |
| D4 | 37–41 | For every changed path named by an ADR's `Governs:`, cite `ADR <id>` or filename in the PR body; enforced on both runners. | Design priors, bullet 3 | Remove example and repeated denial explanation. |
| D5 | 42–45 | Delegate codebase search to `explore-local`; keep own context for reasoning/design/code. | Design priors, bullet 4 | Remove search examples. |
| W1 | 48–51 | Keep the exact prepared branch; never create/rename/switch; confirm `$ISSUE_NUMBER`, retain prior work; conventional branch types supported. | Workflow, step 1 | Remove publication-binding rationale; keep every branch obligation and type examples. |
| W2 | 52–53 | Generate and post acceptance criteria as an issue comment before any code. | Workflow, step 2 | Condense, same timing and destination. |
| W3 | 54–58 | Every criterion states user/downstream-observable behavior, not internal constants/details. | Workflow, step 2 | Remove good/bad examples. |
| W4 | 58–60 | Before fixing criteria, trace to the layer/source of truth actually producing the observable. | Workflow, step 2 | Condense; trace precedes posting criteria. |
| W5 | 61 | Post `## Test Plan` comment with what to test and proof type. | Workflow, step 3 | Condense. |
| W6 | 62–66, 79–81, 88–89 | Tests assert the observable at its producing layer and must fail with implementation reverted. | Workflow, step 3 | Consolidate reverted-code test requirement; step 8 retains the distinct reviewer obligation. |
| W7 | 66–68 | For import difficulties mock dependency or move unit; never neuter assertions. | Workflow, step 3 | Remove framework example. |
| W8 | 70–72, 77–78 | Always write exact command for added tests to `.harness/test-cmd`, even with operator `TEST_CMD`. | Workflow, step 3 | Remove shell example and rationale. |
| W9 | 73–81 | Operator `TEST_CMD` overrides marker; marker only without it; resolved command runs pass and revert checks and includes added tests. | Workflow, step 3 | Condense precedence and inclusion; W6 owns the fail-on-revert obligation. |
| W10 | 82–83 | Read `CLAUDE.md` in touched directories and relevant ADRs. | Workflow, step 4 | Invariant/index reading cross-references D1. |
| W11 | 84–90, 182–183 | Commit plan domain interfaces/types, then red failing tests, then green implementation until passing; green requires earlier red on branch (bare and scoped forms). | Workflow, step 5 | Remove Hard rules duplicate; scoped forms defined in the same step. |
| W12 | 86–89 | Confirm red tests fail for the right reason before proceeding. | Workflow, step 5 | Remove gate narration; reverted implementation requirement owned by W6. |
| W13 | 91–98 | Check commitlint/commit-msg before first commit; if present use conventional type with phase scope on every commit. | Workflow, step 5 | Retain red/green/plan/evidence scoped examples; remove hook-location examples. |
| W14 | 100–102 | UI changes require committed screenshots from running app in `evidence/`, each ≤700KB; reviewer judges changed behavior. | Workflow, step 6 | Condense current #327 requirement; no receipt/count machinery. |
| W15 | 103 | `docs:` update folder `CLAUDE.md` with non-obvious learnings. | Workflow, step 7 | Condense. |

## Protected adversarial self-review (step 8)

Every entry below maps to the corresponding clause in AFTER step 8, which is
unchanged, including examples and rationale. These are obligations of the
review/capture participants, rather than extra versions of the agent's rules.

| ID | BEFORE lines | Required behavior | Single AFTER location | Wording change |
| --- | --- | --- | --- | --- |
| R1 | 104–106 | Overlap review with evidence capture; review diff + criteria (tests/layers/seams); agent and harness reviewer separately verify screenshots. | Workflow, step 8 opening | None. |
| R2 | 108–112 | If screenshots uncaptured and not a test byproduct, spawn both review and step-6 evidence subagents concurrently in one message; capture agent reports file paths. | Step 8, first branch | None. |
| R3 | 112–116 | No later-message serialization or detached/background subagents; both blocking calls in same message; on return commit screenshots while acting on verdict. | Step 8, first branch | None. |
| R4 | 117–118 | If evidence already exists (or no visual ACs), commit per step 6 and spawn review alone. | Step 8, second branch | None. |
| R5 | 119–120 | Reviewer fresh context, only diff + ACs + already committed evidence. | Step 8, reviewer context | None. |
| R6 | 120–122 | Require PASS; record findings in PR description or review discussion; process discipline is not self-authored authorization evidence. | Step 8, verdict clause | None. |
| R7 | 123–126 | On FAIL fix findings, then fresh scoped verifier (not full re-review) receives only verbatim FAIL findings, fix diff since review, and ACs. | Step 8, FAIL procedure | None. |
| R8 | 126–130 | Verifier confirms each finding resolved and checks fix diff for introduced regressions; returns PASS/FAIL. | Step 8, verifier remit | None. |
| R9 | 130–134 | On scoped FAIL fix and re-verify scoped; full fresh review only after two scoped failures on same finding. | Step 8, escalation clause | None. |
| R10 | 135–137 | Instruct reviewer to FAIL tautological tests, including mocks removing unit under test. | Step 8, test-integrity bullet 1 | None. |
| R11 | 138–141 | Instruct reviewer to FAIL wrong-layer tests; ask whether implementation revert makes test red, else FAIL. | Step 8, test-integrity bullet 2 | None. |
| R12 | 142–143 | Instruct reviewer to FAIL untested user-visible/contract-level acceptance seams. | Step 8, test-integrity bullet 3 | None. |

## Publication and hard rules

| ID | BEFORE lines | Required behavior | Single AFTER location | Wording change |
| --- | --- | --- | --- | --- |
| P1 | 144–148 | All remote publication through durable broker: brokered git push, gh pr create, gh pr edit. | Workflow, step 9 opening | Condense; all three commands retained. |
| P2 | 147–150 | Edit only own branch PR; body replaces whole body; broker supplies missing issue ACs and closing linkage for create/replacement. | Workflow, step 9 body clause | Condense; no body-format denial introduced. |
| P3 | 151–153 | Literal `--body` only, not `--body-file`; long Markdown uses scratch file with command substitution. | Workflow, step 9 body argument | Condense, exact command example retained. |
| P4 | 154–156 | Broker commits lifecycle command before network effect and reconciles ambiguous outcome before retry. | Workflow, step 9 final sentence | Condense, sequence retained. |
| P5 | 156–160 | Explicit `--base "$BASE_BRANCH"` on creation; gate rejects mismatched base. | Workflow, step 9 base clause | Remove default-branch/CI incident rationale. |
| H1 | 98–99, 163–164 | No hook bypass: `--no-verify`, commit `-n`, `HUSKY=0`, `core.hooksPath` override; gate mandatory. | Hard rules, bullet 1 | Consolidate step 5 duplicate, explicit “Never bypass hooks.” |
| H2 | 186 | Do not open PR unless all hooks pass. | Hard rules, bullet 1 | Combine with hook obligation. |
| H3 | 165–166 | Never skip/disable test to go green; correct wrong existing expectation and explain correction in PR description. | Hard rules, bullet 2 | Condense. |
| H4 | 167 | Do not refactor unrelated code. | Hard rules, bullet 2 | Case only. |
| H5 | 168–171 | Fix underlying quality/security signal on added/modified code; never hide with exclusions, threshold drops, suppressions, won't-fix/false-positive marks. | Hard rules, bullet 3 opening | “Changed code” covers added/modified code; directive examples generalized, prohibition retained. |
| H6 | 172–174 | Cover new code where analyzer ingests tests; do not exclude it to clear coverage gate. | Hard rules, bullet 3 coverage clause | Remove vendor-specific example. |
| H7 | 175–178 | Suppression only for generated/vendored code, genuine scanner false positive, truly untestable glue. | Hard rules, bullet 3 exception | Remove examples, identical allowed categories. |
| H8 | 178–180 | Justify suppression in PR description AND comment at site; scope as narrowly as possible. | Hard rules, bullet 3 justification | Condense. |
| H9 | 180–181 | Never suppress point of change or code own tests prove testable. | Hard rules, bullet 3 final clause | Condense. |
| H10 | 189–195 | Never block for permission on PR-reviewable engineering decisions (test seams, correcting tests, extra files); act minimally, justify in PR, independent reviewer judges. | Hard rules, bullet 4 opening | Remove seam examples and review-round rationale. |
| H11 | 195–199 | Block only for product ambiguity, environment/credentials unavailable to entire pipeline, rules denying every legal move. | Hard rules, bullet 4 blocking conditions | Remove ambiguity example; pipeline qualification retained. |
| H12 | 199–203 | Before missing-platform block check whether CI runs the platform; if so, instrument branch (timeouts, verbose logs, always artifact upload) and read artifacts. | Hard rules, bullet 4 CI clause | Remove incident narration; explicitly name prepared branch to avoid implying branch creation. |
| H13 | 203–204 | Two inconclusive diagnostic runs → best defensive fix using captured evidence, explain in PR. | Hard rules, bullet 4 diagnostic bound | Condense; bound unchanged. |
| H14 | 187–188 | If genuinely unable to proceed, blocked with exactly what needed; do not guess. | Hard rules, bullet 4 final clause | Move alongside legitimate blocking conditions. |
| H15 | 211–215 | Never create issues; work around legitimate hook block if possible; if completion prevented, blocked with focused harness-issue title/reproduction in `needs` for operator. | Hard rules, bullet 5 | General “Never call” preserves issue proposal's prohibition; remove publication-authority rationale. |

## Output and injected rules

| ID | BEFORE lines | Required behavior | Single AFTER location | Wording change |
| --- | --- | --- | --- | --- |
| S1 | 218–219 | Final message satisfies invocation JSON schema; statuses pr_opened/pushed/blocked/failed. | Structured output contract, opening | None. |
| S2 | 220 | `summary` is one paragraph describing work. | Structured output contract, summary field | None. |
| S3 | 221 | If blocked, `needs` states exactly what needed. | Structured output contract, needs field | None. |
| S4 | 222 | Return pr_opened only after gh pr create actually succeeds. | Structured output contract, success condition | None. |
| S5 | 224–228 | Split implementer with genuinely wrong observable, issue contradiction, or infeasible test/criterion (not inconvenience) returns contract_change with wrong item, correct observable and why. | Structured output contract, implementer exception | None. |
| S6 | 228–230 | Independent author adjudicates objection, not implementer; default make tests pass, petition only real defects. | Structured output contract, adjudication clause | None. |
| S7 | 232–236 | Preserve expansion sites for diagnose/logging/docs/tests operator rule blocks. | Debugging / Logging / Documentation / Tests | None; variable names/order unchanged. |

## Verification and measurement

`git grep -n 'AGENT.md' tests plugin src` finds the literal contract check in
`tests/app/test_config.py`; update its issue-creation prohibition to the canonical
“Never call” wording (updated). The remaining mentions concern prompt rendering/delivery,
planner behavior, governor permissions or gate parity, not required literal
sentences. Also inspect `REVIEW_PROMPT.md` references and both live prompts for
retired machinery: neither baseline contains visual receipts/count requirements,
exemption grants/trailers, or PR-body-format denials. `--body-file` rejection,
ADR citations and explicit-base rejection remain active constraints, not retired
body-format enforcement. REVIEW_PROMPT.md therefore needs no edit.

Focused verification only (user override of the repository's full-suite rule):
config/planner prompt tests, governor/gate parity tests, domain architecture
contracts, and `make test-hooks`; at most four workers. Results:

- `uv run --offline --no-sync pytest tests/app/test_config.py
  tests/app/test_task_planners.py tests/test_gate_parity.py tests/test_governor.py
  tests/domain/test_architecture_contracts.py -n 4 --dist load --maxschedchunk=1`:
  **590 passed**.
- `make test-hooks`: **85 passed, 0 failed**.
- `git diff --check`: clean. Step 8 and the output/injected-rule blocks compare
  byte-for-byte equal to the pinned BEFORE text.

Independent review: **PASS** on the final snapshot, no lost or weakened required
behavior found. Its optional CI wording clarification was incorporated: explicitly
instrument the prepared branch when CI runs the missing platform.

Metric: fixer-leg input tokens and cost per merged PR. Guardrails: first-pass
approval, gate denials per session and hard-rule violations. Next measurement:
mean fixer-leg input tokens and first-pass approval on the next tauri batch versus
this week's baseline. Net `src/` lines: 0. No runtime mechanism/state/knob added or
removed; prompt restatements and unprotected incident narration removed.

## Independent review corrections (2026-10-12)
An unseeded review built its own inventory from the BEFORE text and found four regressions in the first trim. All four are restored:
- **Step 3:** the resolved test command must pass normally *and fail with the implementation reverted* (the first trim said only "runs for … reverted-code checks").
- **Step 2:** "Do not quietly lower the posted criteria later" restored (the BEFORE text read "fixes the bar in place so you cannot quietly lower it").
- **Hard rules:** coverage is required only *if a coverage gate fails on new code* (the first trim had made it unconditional).
- **Hard rules:** CI diagnostics (fail-fast timeouts, verbose logs, `always()` uploads) are examples applied *as needed*, triggered *before blocking* (the first trim made all three mandatory and triggered earlier).

Final word count: 1,442 (−41.5% from 2,465).
