# ADR 0001 — Trust boundary and privileged effects

Status: Accepted
Date: 2026-07-30
Governs: src/issue_agent/app/identity.py src/issue_agent/adapters/operator_effects.py src/issue_agent/adapters/act.py src/issue_agent/adapters/github.py src/issue_agent/domain/gaps.py src/issue_agent/domain/trust.py src/issue_agent/domain/sandbox.py src/issue_agent/adapters/leg_sandbox.py

## Decision

Agent prompts, repository text, CI output, and agent-written files are
untrusted claims. They are encoded through `domain/trust.py`; they never
directly authorize publication. GitHub mutations go through the idempotent act
layer (`adapters/act.py`): each write is either naturally idempotent and read
back, or claims an idempotency key in SQLite before writing and settles it on
GitHub's answer (as amended by #440 below). An unknown remote outcome is read
back before any retry, never blindly repeated.

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

Amended by #472 (2026-10-16): a bot change request on a head stands only
while it is no older than the PR body's last edit (GitHub `lastEditedAt`), so
a fixer may answer it in the description alone; an approval stands through
edits. The review write's identity is therefore the head plus the edit the
review judged: the leg reads the body and its edit time in one GitHub read,
records that time with the verdict on its receipt, reads back a bot verdict
on the head no older than it, and resubmits a stored verdict only when the
recorded time still equals GitHub's. Provenance of the new input: anyone who
can edit the PR (the fix bot, collaborators, GitHub Apps) moves
`lastEditedAt`, and GitHub stamps it. It can only make a review owed, never
make or keep a verdict, so it cannot produce a merge.

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
sandbox: a review leg sees them read-only, and a fix leg sees none of them.
Per-leg fix clones (2026-10-15) amend #417/#419: a fix leg works in a clone of
origin that borrows the repository's objects read-only (`clone --reference`)
and keeps its own refs, packed-refs and new objects, so it cannot rewrite
objects a review checks out (GIT-OBJECTS) and a host ref pack cannot hide its
branch (tauri #1106). The overlay, the read-only packed-refs, the
repository-config writes that stopped ref packing and their refusal are gone.
The clone's `config`, `hooks/` and `commondir` (written `.` at each launch) are
read-only to the leg and `.git` is a mount point, because the harness's git
runs there. Crash points: a clone is built at `<path>.partial` and renamed
into place, and removal renames it there before deleting, so a kill at any
step leaves only scratch the next clone replaces; a restarted daemon reuses
clones on disk at the same path, so Claude's cwd-keyed session store resumes
(`tests/adapters/test_worktree_identity.py`).
A leg's `/tmp` and `/var/tmp` (#470, 2026-10-16) are fresh 0700 directories on
disk, `leg-tmp/<launching daemon>/<run>/{tmp,var-tmp}` in the deployment's
process registry (ADR 0005), not tmpfs: a tmpfs is charged to the unit's
memory, and one leg's Chrome traces OOM-killed the fix unit. The launch refuses
a path that already exists, would expose a protected one, or sits on a noexec
filesystem. Crash points: a refused launch removes what it made; a launch that
fails before its process group exists ends the run through `kill_tree`, as does
every leg end and every reap, once no process of the run can run (directories a
leg made read-only included); a full disk that refuses the cancellation receipt
still kills a registered group and frees its directory. Every recovery, boot
first, removes the directories of a dead launching daemon whose run holds no
registration, before it reaps, so a crash's leftover frees the disk the reaping
writes to; that covers a retired registry too. Only a tree deeper than the open
file limit (raised to the hard limit at daemon start) survives, with a log line
(`tests/adapters/test_leg_sandbox.py`, `tests/adapters/test_process_supervisor.py`,
the runner tests). The trade: a runaway leg's scratch is now bounded by the
state filesystem's free space, not the unit's memory, and that filesystem holds
the control store. The checkout and `CARGO_TARGET_DIR` already write the same
disk unbounded, so the exposure is not new in kind.
A fix leg's one credential is `FIX_BOT_TOKEN`, passed in its environment; a
review or light leg holds none; the review bot's token and App key never leave
the act layer. Secrets reach the daemons as systemd encrypted
credentials where the host has them (`ImportCredential=` of
`~/.config/credstore.encrypted/NAME`, decrypted into
`$CREDENTIALS_DIRECTORY` for that unit alone; `FIX_BOT_TOKEN` on the fix unit,
`REVIEW_BOT_TOKEN` on the review unit): config load fills each secret from
there, refuses a secret that also appears in the env file or that exists in the
credstore yet never arrived (no fallback to the ambient login), and the credential
directory never enters a leg's environment. Claude legs authenticate with their
own `CLAUDE_CODE_OAUTH_TOKEN` (the one secret on both units, passed to Claude
legs only) and is required: a Claude leg with no such credential (that or
`ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`) is refused, so the operator's
`~/.claude` is never mounted beyond the leg's own session store. launchd and
foreground runs keep env-file secrets.
Role identity (#443, 2026-10-15) is resolved once at boot by
`app/identity.py`, fail-closed. The fix role acts on `FIX_BOT_TOKEN`; the
review role on its GitHub App when the key is readable, else
`REVIEW_BOT_TOKEN`. A role with no credential refuses to start: no daemon
runs on the operator's ambient `gh` login, which only operator CLI writes use.
The role then asks GitHub (`viewer`) who the credential is and refuses unless
the answer is a well-formed login equal to its configured `FIX_BOT` /
`REVIEW_BOT`, since keyed writes are read back by that author (#424). The
verified spelling becomes the role's own login in `Harness`; the sibling's
login stays configuration, because a role holds no credential to verify the
other's. Crash points: none; the check runs before any lease or write, and a
refusal exits `INVALID_CONFIG` (`tests/app/test_role_identity.py`).
Paid legs are refused when bwrap is missing or cannot create a
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

Harness gaps (2026-10-15) add the harness's one write outside the target
repository: the fix role reports a harness defect into the harness's own
repository (its checkout's GitHub origin) with `FIX_BOT_TOKEN`, through the
same act layer (`ReportGap`). The key,
`fix-report:gap:<harness repo>:<target>:<item>:<gap>`, is claimed in SQLite
before the write and settled by GitHub's answer (#440); a
confirmed key reaches policy as a local fact and is never asked for again, and
a new key is refused past `GAP_REPORTS_PER_DAY` claims in a UTC day. No
agent-written text is published (owner, 2026-10-15): an agent's
`harness_issue` is an untrusted claim that `domain/gaps.py` bounds and that
stays on the harness host (the leg receipt and the journal). The issue and its
comments hold harness facts only: the gap kind, the target and item, the leg
id, kind and runtime, and a machine end's class from a fixed vocabulary
(`outcome.MACHINE_ENDS`), never a raw error string. A non-public target is
called only "a private target" (no name, digest or link; the repository stays
in the local dedup key), so no visibility comparison is needed. A filed gap never queues itself: today only `ai-ready` queues an
issue, and an automatic pickup mode must exclude FIX_BOT's issues titled
`Harness gap:` (GitHub sets the author and the harness writes the title, so
neither is forgeable; `tests/domain/test_gaps.py` pins it per mode), since a
leg's output must not dispatch more legs; a human's `ai-ready` is a deliberate
act and is honoured. Filing is off, read at each daemon start, when the
checkout has no GitHub origin or the fix credential cannot file issues there
(no push, issues off, archived: a clone of someone else's harness files
nothing upstream); unreadable facts mean off. A report GitHub refuses (a 4xx,
as on a locked issue or with issues disabled) settles its key with one log
line; no answer stays retryable. Crash points: `tests/adapters/test_act.py` (a
claim that never landed files; a lost filing reply is found by its body; a
lost comment reply is read back).

Typed write answers (#440, 2026-10-15) make GitHub's answer to a write its
receipt. The gateway sends each act-layer write that creates or requests
something (comment, issue, review, blocked-by link, run cancel and re-run,
update-branch) as `gh api --include` and reads the HTTP status and headers,
not gh's error text: a 2xx is `Accepted`, carrying the created object's id
(comment id, issue number, review id); a 4xx, or a GraphQL answer with
errors, is `Refused(status, message)`; no status line, a 5xx, a 401 or a rate
limit raises `GhUnavailable`, the NoAnswer outcome. The comment, issue, gap
report and blocked-by comment share one `GitHubAct.keyed_write(key, present,
write)`: claim the key; for a key claimed but never settled (a crash or no
answer) or settled but since deleted (#395), read GitHub through `present`
and write only if absent; then settle on the answer. No read follows an
answer, so a read that lags the write (#433) cannot fail it. A refusal settles
the key with one log line, and act does not retry it; derive may ask again
when its observation lacks the effect, and then the settled-but-absent path
writes once per ask. The naturally idempotent writes (labels, merge,
close/reopen, PR open) keep their read-backs, and keys and the store are
unchanged (ADR 0003). Crash points, each a test in
`tests/adapters/test_act.py`: before the POST (the claim stands and the next
tick writes once), POST answered but not confirmed (the next tick reads it
back and does not write), POST unanswered (landed: read back and not written
again; not landed: written on the next tick), and confirm (a replay of a
settled key that GitHub still shows writes nothing).

The automatic pickup mode exists since 2026-10-15: `ISSUE_PICKUP=open`, a live
per-deployment knob (ADR 0007 amendment, which records the trust it moves). It
excludes the gap reports above through `domain.state.harness_gap`; a human's
`ai-ready` on one is still honoured.

Account recognition (#481, 2026-10-16) binds author trust to GitHub's account
kind plus login. Adapters validate `author { __typename login }` (GraphQL),
`user { type login }` (REST), or gh's typed `is_bot` projection into an
immutable `Account`; missing, malformed or oversized authors grant no
identity. GraphQL Bot `steward` and REST Bot `steward[bot]` encode the same
account, distinct from User `steward`. Issue observations retain the typed
account; reviews, comments and pushes retain its canonical spelling, with
`[bot]` for Bot accounts. Identity-bearing single-item reads use GraphQL
rather than gh's lossy review-author projection. Configuration and the
credential's `viewer.login` use the full account spelling (`xnmp-fixer` for
a User PAT, `steward[bot]` for an app); boot rejects a different account kind
as well as a different login. Sibling-role configuration keeps that spelling. Concrete `CommitAuthor.user`
also uses full login spelling, even for app bots whose concrete type is User.
This replaces suffix-stripping comparisons for base-CI issue selection and
close evidence, architecture-pass discovery and dispatch, gap-report pickup
exclusion, harness comment provenance and human replies/resets, review
verdicts and rejection ceilings, and comment/review write read-backs. CLI and shell doctor
credential checks and self-approval configuration validation use the same
exact account rule. No state, gate, knob or retry mechanism is added. The
regressions exercise these boundaries offline in `tests/adapters/test_github_identity.py`
and the existing identity, observation and role tests; live GitHub verification
is outside this offline change.
