# Review agent returns $0.00 "no ResultMessage" — SDK 0.2.110 background-Task stream never yields a terminal ResultMessage

**RESOLVED (2026-07-25):** the fix this doc proposes — denying `Bash(run_in_background=true)` — is live: `src/issue_agent/adapters/governor.py:343-350` rejects it unconditionally in the `PreToolUse(Bash)` hook, before the command-content gate. Denying other background-capable tools (e.g. `Monitor`, `Agent`) fixer-side is tracked separately as its own item, not this one — see `docs/IMPROVEMENT-PLAN.md` P0.10/P3.1.

**Labels:** bug, reviewer, agent-runtime, sdk-upgrade, HIGH

## Summary

After `claude-agent-sdk` was (re)installed at **0.2.110** (2026-07-21 09:46, via
`make point-at` → `uv sync`), the review agent frequently returns
`is_error=True, cost=$0.00, summary="no ResultMessage from query()"` despite
spending real tokens. The PR gets no verdict; the daemon retries the whole review
from a cold process every poll (352 eventually squeaked through; 354's re-review
failed 5+ consecutive times and had to be merged by hand).

**This corrects an earlier misdiagnosis** (see `persistent-review-session.md` §Impact,
which blamed a "gateway transport drop / `Stream closed`"). That was wrong —
disproven below. The real cause is the SDK's new **background-Task** message flow.

## Root cause (evidence-backed, not guessed)

`AgentRunner.run` iterates the `query()` async stream and captures the terminal
`ResultMessage` (reads `structured_output` + `total_cost_usd`). In SDK 0.2.110 the
review agent spawns **background Tasks** (the SDK's async-agent feature — new
`TaskStartedMessage` / `TaskProgressMessage` / `TaskUpdatedMessage` /
`TaskNotificationMessage` types, plus `ResultMessage.deferred_tool_use` and a
`task_budget` option). On the affected runs the stream **ends cleanly**
(`StopAsyncIteration`, NO exception) with a `TaskNotificationMessage` as the last
message and **no `ResultMessage` ever emitted**. The runner's
`isinstance(message, ResultMessage)` never matches → `final` stays `None` → the
"no ResultMessage" error. `cost=$0.00` because only `ResultMessage` carries
`total_cost_usd`, and it never arrived.

Instrumented `agent_runner.py` to log message-count + last-kind on the clean-end
path (and to route the bundled CLI's stderr into our log). Findings:

| Repro (PR #352 review, real task) | Result |
|---|---|
| `max_budget_usd=$8` | drop @ **103** msgs, last kind = `UserMessage` |
| `max_budget_usd=$50` (6×) | drop @ **267** msgs, last kind = `TaskProgressMessage` |
| debug per-message trace | `TaskStarted → TaskProgress ×N → TaskUpdated → TaskNotification`, then stream ends; last = `TaskNotificationMessage` |
| bundled-CLI stderr capture (`ClaudeAgentOptions.stderr`) | **0 lines** — CLI printed nothing |

### What this rules OUT (each by experiment)

- **NOT a gateway transport drop / HTTP error.** The new `stderr` capture shows the
  CLI emits **nothing** to stderr on these runs — no `response.failed`, no HTTP
  status, no reconnect loop. The stream ends *cleanly* (StopAsyncIteration), not via
  the exception branch (`agent_runner.py` "transport died before any ResultMessage"
  never logs). A gateway drop would raise or print.
- **NOT the budget cap.** Raising `$8 → $50` did not fix it — it ran 2.6× more
  messages (103 → 267) and dropped the *same way*. Budget only changes how far it
  gets before the Task flow terminates the stream sans ResultMessage.
- **NOT idle watchdog / SIGKILL / OOM.** Clean StopAsyncIteration, `hung=False`, no
  IDLE line, 57–61% mem free.
- **NOT the resumed session alone.** Clearing 354's saved review session (forcing a
  fresh round-1 `review`, no `resume`) still dropped — a fresh `query()` hits it too.
  (Resume raises the probability by making the transcript longer / more Task-prone,
  which is why re-review failed more consistently than round-1.)

### Why "now"

SDK 0.2.110 (installed today) introduced the background-Task message family. The
`Task` tool is enabled by the `claude_code` system-prompt **preset** — NOT by our
`allowed_tools` (`REVIEW_TOOLS` does not list `Task`, yet `TaskStartedMessage`
appears). So the agent can spawn background tasks regardless of our allowlist, and
the runner (written for a pre-Task SDK) doesn't understand the new terminator.

## Fix (CONFIRMED against SDK 0.2.110 docs — Python + TypeScript reference)

**There is NO single disable flag.** Confirmed non-solutions:
- `CLAUDE_CODE_ENABLE_TASKS=0` — wrong knob; only reverts `TaskCreate/Update/List`
  back to the old `TodoWrite` tool. Does NOT stop background execution.
- `task_budget={"total": N}` — an API-side *token* budget (beta header
  `task-budgets-2026-03-13`); caps spend, does not disable background tasks.
- No documented `ClaudeAgentOptions(disable_background_tasks=…)` boolean.

Background execution is triggered by **`Bash` with `run_in_background=true`** and the
**`Monitor`** tool (subagents via `Agent` are synchronous and terminate before the
parent result — not the culprit). The fix is to **block those**, plus defensively
filter background-follow-up results:

1. **Block background-spawning tools** (primary). Review is read-only-ish but needs
   `Bash(git diff *)`, so we can't blanket-block Bash — block `Monitor` + reject
   background Bash via a `PreToolUse` hook, and add `Monitor`/`Agent` to
   `disallowed_tools`:
   ```python
   # in build_option_kwargs / the review task
   disallowed_tools=[..., "Monitor", "Agent"]
   # + a PreToolUse hook (compose with the governor's existing matcher):
   async def _block_background_bash(input_data, tool_use_id, context):
       if input_data.get("tool_input", {}).get("run_in_background"):
           return {"decision": "block", "reason": "background bash disabled (single-shot agent)"}
       return {}
   # hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[_block_background_bash])]}
   ```
   The fixer/steward paths that DON'T need Bash-in-background can just add
   `disallowed_tools=["Bash","Monitor","Agent"]` if their tasks allow it.

2. **Filter background-follow-up results in `AgentRunner.run`** (defense in depth).
   Per the docs, a background-task follow-up emits a `ResultMessage` with
   `origin == {"kind": "task-notification"}` (and the spawning turn ends with
   `terminal_reason == "background_requested"`). So: capture ResultMessages but
   SKIP any whose `origin.kind == "task-notification"`; keep the one that answers
   the prompt. Also log `terminal_reason` + `origin` on the clean-end path (already
   partly done) so a future recurrence is self-explaining.

   NOTE — open discrepancy: the docs say `ResultMessage` is ALWAYS the terminator,
   yet our Python 0.2.110 trace showed the stream ending via `StopAsyncIteration`
   with the last message a `TaskNotificationMessage` and NO ResultMessage at all
   (not even a `background_requested` one). So on this build the terminal result may
   only arrive if the session is RESUMED after the background task completes. Blocking
   background spawn (#1) sidesteps this entirely; #2 handles the documented shape.

3. **Retry-on-clean-end** stopgap only — the workflow already retries next poll;
   prefer #1 which removes the cause.

**Recommended:** #1 (block `Monitor` + background-Bash hook) as the real fix +
#2's origin-filter as a guard. Both are small, local to the runtime edge.

## Already landed this session (diagnostics only — safe, tested)

In `src/issue_agent/adapters/agent_runner.py` (25 unit tests green):
- Log message-count + last-message-kind on the clean-end-no-ResultMessage path
  (was silent — the whole reason this was undiagnosable).
- Wire `ClaudeAgentOptions.stderr` → our log (guarded by `_option_field_names()` for
  older SDK builds) so the bundled CLI's own errors surface next to the $0.00.

These are pure instrumentation — they don't change control flow. The actual fix
(#1/#2 above) is NOT yet applied.

## Impact

- Reviewer can't reliably deliver a verdict → PRs stall un-reviewed; auto-merge
  never arms. Blocks the whole review→merge automation the harness exists for.
- Wasted spend: each dropped review burns full token cost (103–267 messages) for
  zero recorded result, then retries cold.
- Also affects the fixer/steward runtime paths (same `AgentRunner`), any time an
  agent uses the preset's background-Task feature.
