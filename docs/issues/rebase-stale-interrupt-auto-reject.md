# Stale-interrupt auto-reject on a resumed session parks a mergeable PR

**Observed:** 2026-07-26, xnmp/tauri-explorer PR #534 (issue #506), fix daemon,
CLI 2.1.220, session `4c2a5177-d68a-4a98-9c7a-1d7c51f6d811` (third `--resume`
of that session; journal dispatch `6230758799dc`, 18.7s, status=blocked).

## What happened

The REBASE leg's first tool call was auto-rejected by the CLI **53ms** after it
was issued:

- `08:24:05.683` assistant emits Bash tool_use (read-only `git fetch` +
  divergence inspection)
- `08:24:05.736` tool_result (is_error): *"The user doesn't want to take this
  action right now. STOP what you are doing and wait for the user to tell you
  how to proceed."*

53ms rules out our PreToolUse governor (it shells out to git several times), a
permission prompt, and a human. The string sits next to `[Request interrupted
by user]` / `[Request interrupted by user for tool use]` in the CLI binary —
it is the **interrupt auto-rejection** injected when a tool call arrives while
the turn is already in an aborted state. The agent, resumed and unattended,
took it literally, stopped, and the structured-output enforcer extracted
`status=blocked` — so the harness labeled a trivially-rebaseable PR
(two both-append doc-file conflicts) `ai-blocked`.

Ruled out by evidence:

- **Governor deny with dropped reason** — the same transcript shows a real
  governor denial (leg 2's `run_in_background` attempt) arriving with the full
  crafted message; deny plumbing is intact.
- **Budget** — the leg's budget attachment shows $19.47 of $20 remaining.
- **Our runner** — nothing in `agent_runner.py` interrupts; wall/idle caps
  were nowhere near.

## Suspected mechanism (unproven)

Stale interrupt/abort state surviving `--resume`. Both prior legs of this
session ended via the CLI's `[structured-output-enforce]` mechanism, which
appears interrupt-based. Counter-evidence: the #537 rebase (session
`c4ae98b9…`) also resumed an enforce-terminated session the same hour and its
first tool call ran fine — so the trigger is not merely resume-after-enforce,
or it is racy.

## Mitigations in the harness (2026-07-26)

1. `plugin/AGENT.md` ("No interactive user exists", which every leg carries,
   resumed ones included, since 2026-10-14 replaced `_RESUME_REMINDER`): the agents are told this exact message in an
   unattended session is a runtime glitch — retry once, block only on a second
   rejection, and keep treating reasoned denials as real.
2. `domain/outcome.py`: a blocked rebase/fix-ci now posts the agent's `needs`
   to the PR, so the next occurrence is visible on the PR instead of vanishing.

## If it recurs

The `needs` comment will quote the rejection text. Check whether the first
tool call after resume was again rejected in <100ms (transcript timestamps in
`~/.claude/projects/<worktree-slug>/<session>.jsonl`). If yes, consider filing
against the CLI with both transcripts, and/or having the runner start rebase
legs WITHOUT resume (fresh session + context re-injection) as a workaround.
