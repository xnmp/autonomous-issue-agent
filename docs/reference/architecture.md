# Architecture — how the harness works now

Current as of 2026-07-26. This supersedes the numbered `0N-*.md` specs in
`docs/archive/`, which describe the **original bash design**: the scripts they
document still exist under `agent/ review/ steward/` but are legacy, and on the
default path none of them run.

Operator instructions live in [HARNESS.md](../../HARNESS.md); how to point the
harness at a repo is in [CLAUDE.md](../../CLAUDE.md).

## The shape

Three role daemons — **fix**, **review**, **steward** — each poll GitHub and
drive Claude Agent SDK sessions. Each is assembled in `app/roles.py` from four
parts:

```
observe   gateway.build_world()      the GitHub slice this role needs
decide    a PURE function            world + config + state → list[Action]
execute   Executor.execute()         the ONE place writes happen
dispatch  AgentDispatcher            RunAgent → an actual paid agent session
```

The layering is hexagonal and **machine-enforced** (import-linter, two contracts
in `make test`): `domain/` is pure — no I/O, no env, no clock — `adapters/` is
I/O, `app/` is wiring. New pure logic goes in `domain/`; new I/O goes behind an
existing adapter or an injected callable.

## The five properties worth preserving

These are what the 2026-07-25 review identified as load-bearing. A refactor that
breaks one of them is a regression even if the tests pass.

1. **Decisions are data.** `decide_*` returns `Action` tuples; nothing in
   `domain/` performs a write. This is why adding escalation alerting and the
   journal were one-place changes.
2. **One write funnel.** Every GitHub write goes through `Executor.execute`.
   Escalation detection (any relabel to `needs-human-review` → journal event +
   `ALERT_CMD`) lives there, so new escalation paths get alerting for free. The
   review found the one leak — review-side escalations bypassing it — and it was
   closed rather than the rule relaxed.
3. **Impure rules are higher-order.** `check_tests(test_cmd, run_tests, …)` is
   the template: pure policy, unit-tested, with ~3 lines of shell-out injected
   from an adapter.
4. **"I couldn't look" is not "nothing to do."** `GhUnavailable` and
   `OpenPrLookup`'s tri-state exist so an unreachable API can never be mistaken
   for an empty world. Per-item fetch isolation (`_safe_get_pr`/`_safe_get_issue`)
   drops one bad item, not the whole poll — but propagates systemic failures
   like rate limits, which would otherwise fake an empty world.
5. **Incident comments.** Comments here carry the failure they prevent, often
   with a PR number. They are the densest defence in the codebase. When you move
   code, move its comment verbatim.

## Enforcement: two stacks, one spec

The fixer is governed at two levels:

- **The governor hook** (`adapters/governor.py` + `plugin/hooks/bash_governor.py`)
  inspects every Bash call the agent makes. Its parsing core
  (`governor_policy.py`) is *shared* between the Python and bash entry points,
  which is why that half provably cannot drift.
- **The gates** (`domain/gate.py` for the SDK path, `plugin/hooks/checks/*.sh`
  for codex mode) enforce the PR/push/commit contract: base branch, acceptance
  criteria, test runs, red-before-green, evidence images, ADR citations,
  lint/typecheck.

Those two gate implementations shared a *spec*, not code, and had already
drifted when the review checked. They are now pinned by
`tests/test_gate_parity.py`: 35 golden cases run through **both** stacks in real
scratch git repos, asserting identical verdicts. Three legitimate divergences
are asserted explicitly rather than dropped. **If you add a gate rule, add a
parity case** — a set-equality test over rule families will fail otherwise.

## Money safety

Every paid dispatch is bounded by layered breakers, because the failure mode
that matters is not "wrong answer" but "loop that bills forever":

- per-leg retry counters (`ci`, `review`, `rebase`, `resolve`) → escalate at cap
- `MAX_INFRA_DEATHS` / `MAX_UNPRODUCTIVE_REVIEW_ROUNDS` — a *separate, higher*
  ceiling for sessions that die before returning a result, which deliberately do
  not count as attempts
- `AGENT_MAX_RUNTIME_SECONDS` wall cap, checked between events
- spend caps over the cost ledger — **see
  [../open/residuals.md](../open/residuals.md) §1.1: these are inert on plans
  that report $0**

Counters decay on any leg that returns a result, so the ceilings bound
*consecutive* failure rather than accumulating over a healthy issue's lifetime.

`tests/domain/test_convergence.py` is the regression guard for this whole class:
each scenario drives a real `Daemon` tick loop over the real workflow, dispatcher,
executor and store, and asserts a frozen world reaches a steady state without
buying sessions. **Any new dispatch leg or retry counter needs a row there.**

## Where the detail lives

| Topic | File |
|---|---|
| Operating it | [HARNESS.md](../../HARNESS.md) |
| Pointing it at a repo, config layout | [CLAUDE.md](../../CLAUDE.md) |
| Codex runtime | [codex-runner.md](codex-runner.md) |
| 110 recorded failure modes | [failure-modes.md](failure-modes.md) |
| What's still open | [../open/residuals.md](../open/residuals.md) |
| How it got here | [../history/](../history/) |
