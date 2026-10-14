# ADR 0001 — Trust boundary and privileged effects

Status: Accepted
Date: 2026-07-30
Governs: src/issue_agent/adapters/operator_effects.py src/issue_agent/adapters/act.py src/issue_agent/domain/trust.py src/issue_agent/domain/sandbox.py src/issue_agent/adapters/leg_sandbox.py

## Decision

Agent prompts, repository text, CI output, and agent-written files are
untrusted claims. They are encoded through `domain/trust.py`; they never
directly authorize publication. GitHub mutations go through the idempotent act
layer (`adapters/act.py`): each write is either naturally idempotent and read
back, or claims an idempotency key in SQLite before writing and confirms it
after a GitHub read-back. An unknown remote outcome is read back before any
retry, never blindly repeated.

The shell governor and hooks were deleted in slice 6a (2026-10). Credential
placement and read-back are the authority. Until agents run without
publication credentials, supported repositories remain trusted-inbound only.

Slice 5b (2026-10-13) amends this for the fix role. It queues no
`LifecycleCommand`s: `adapters/act.py` executes its derived Actions directly.
Each write is naturally idempotent (a label set, a merge or update-branch
bound to the decided head, a PR opened only when the branch has no open one)
or claims an idempotency key in SQLite before writing and confirms it only
after a GitHub read-back. A failed read-back raises and the next tick
re-derives from GitHub. The agent pushes its own branch with git; the publish
broker is deleted (refactor decision Q11). Slice 5c extends this to the
review role: its one write, the bot's review of a head, claims
`review:{pr}:{head}` and first reads back whether the bot already reviewed
that commit, so a crash after submission never posts a second review.

Slice 5d-1 (2026-10-13) removes the durable-command path for every writer.
There is no `LifecycleCommand`, command queue or effect receipt. Operator
writes (`adapters/operator_effects.py`) are label moves through the same
`act.relabel` the fix role uses, plus the CLI's bootstrap steps, each read
before and read back after; the dashboard changes labels only (refactor
decision Q19).

The leg sandbox (#413, 2026-10-14) makes credential placement enforceable.
Every paid leg, Claude or Codex, runs under bubblewrap with a private HOME and
PID namespace and only the mounts `domain/sandbox.py` plans from facts the
harness holds (checkout, git metadata, runtime binary and session store,
toolchains on PATH). The repository's config, hooks and checkout links never
change on the host, because the harness runs git in every checkout outside the
sandbox: a review leg sees them read-only, a fix leg through an overlay its
writes vanish with.
A fix leg's one credential is `FIX_BOT_TOKEN`, passed in its environment; a
review or light leg holds none; the review bot's token and App key never leave
the act layer. Paid legs are refused when bwrap is missing or cannot create a
sandbox: there is no unsandboxed mode and no per-repo knob. The sandbox
replaces the "trusted inbound content only" operator constraint and the
fixer's tool deny list; what a sandboxed leg can still reach is listed in
`docs/open/residuals.md` §1.1.

## Consequences

- Application orchestration cannot invoke gateway write methods directly.
- Agent-authored marker files are claims, not sufficient trusted evidence.
- A later separate effect broker would wrap the act layer, not a command queue.

Screenshot-read receipts and visual-evidence bookkeeping were superseded by #327
(2026-10-12): the reviewer judges screenshot evidence; the 700KB image limit remains.
