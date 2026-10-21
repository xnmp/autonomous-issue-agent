# Runtime capability and authority matrix

This is the deployment truth, not a security claim inferred from hooks.

Every leg of either runtime runs under bubblewrap (#413, ADR 0001): the
filesystem, not the environment allowlist or a tool deny list, is the
credential boundary.

| Runtime | Sandbox/tool surface | Resume | Budget evidence | Command governor | Agent publication credentials | Supported inbound trust |
|---|---|---|---|---|---|---|
| Claude | bwrap leg sandbox; fix legs write their checkout, review legs read it; SDK tool allow/deny for light legs | yes: the session store for the checkout is mounted read-write | provider result/usage; native limit | none (deleted in slice 6a) | fix legs: `FIX_BOT_TOKEN` in env (GH_TOKEN/GITHUB_TOKEN, git helper); review and light legs: none | see residuals §1.1 |
| Codex | bwrap leg sandbox around codex's own `workspace-write`/`read-only` mode; child process group | yes: the harness CODEX_HOME is mounted read-write | rollout counters priced by configured card | none (deleted in slice 6a) | as Claude | see residuals §1.1 |

What a leg sees (`domain/sandbox.py`):

| Visible | Access |
|---|---|
| its checkout: a fix leg's is a clone of its own (`clone --reference`), with its own refs, packed-refs, reflogs and new objects; a review leg's is a worktree pinned at the reviewed head | fix: read-write; review/light: read-only |
| a fix clone's `config`, `hooks/` and `commondir` (written `.`, the clone itself, at every launch: git follows a commondir in any gitdir, and the harness runs git in the clone outside the sandbox); its `.git` is a mount point the leg cannot replace | read-only (`push -u` and `branch -D` still succeed, printing that config is unwritable; residuals §2) |
| the repository's object store, which a fix clone borrows (`objects/info/alternates`, checked at launch to name exactly it); for a review worktree, the repository's whole `.git` | read-only (GIT-OBJECTS closed: review checkouts read these objects); the repository's refs, packed-refs and config are not visible to a fix leg |
| `/dev/shm` | its own empty tmpfs |
| the runtime binary; `/usr`, `/etc` (whole: operator secrets live in HOME, root-only files stay unreadable), `/opt`, `/nix`, `/sys`, the resolver; toolchains on PATH, `~/.rustup`, `~/.cargo/bin`, `~/.cargo/config.toml` (its linker and rustflags hash every artifact in a shared `CARGO_TARGET_DIR`), `~/.bun/bin`, uv's Pythons, `~/.nvm`, Playwright browsers | read-only |
| `~/.cargo/registry`, `~/.cargo/git`, `~/.cache/uv`, `~/.bun/install/cache`, `~/.npm` | per-leg overlay (writes discarded) |
| the runtime session store (`~/.claude/projects/<checkout>`; the harness CODEX_HOME and the `auth.json` it links), `CARGO_TARGET_DIR`, codex's per-run scratch | read-write (codex's login too: refresh rotates it) |
| the rest of Claude's config dir `~/.claude` (login, settings, hooks, history, other projects' transcripts) | not mounted: a Claude leg authenticates with its own `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN`, and a launch without one is refused |
| HOME, `/tmp/.X11-unix` (so a leg's own Xvfb has a socket directory) | empty, leg-private tmpfs |
| `/tmp`, `/var/tmp` | read-write, empty and leg-private: two fresh 0700 directories on disk under the deployment's process registry (`leg-tmp/<daemon>/<run>`), removed with the run's process group or by recovery once their daemon is dead. A tmpfs here was charged to the unit's memory (#470). They take the state filesystem's mount flags: a noexec state directory refuses every leg |
| GPU render nodes (`/dev/dri/renderD*`, never the display `card*` nodes): NVIDIA's EGL library aborts Xvfb and WebKitGTK without one | device |
| network | on (no egress filter) |

Not visible: `config/` and the rest of the harness checkout, the operator's
HOME (`~/.config/gh`, `~/.ssh`, keyrings, `~/.claude.json`, settings, other
sessions), state and control dirs (SQLite), other checkouts' files, other
repositories, `/run` (D-Bus, agent and Docker sockets; a bus on an abstract
socket stays reachable, residuals §1.1), the host's X and Wayland sockets, and
host processes
(`--unshare-pid`, so no `/proc/<daemon>/environ`). Git in a fix leg keeps its
own maintenance: a gc in a clone repacks and prunes only the clone's own objects.

| Concern | Authority | Failure posture |
|---|---|---|
| Paid admission | SQLite reservations and charges | unavailable/corrupt/unpriceable with an armed cap blocks new paid work |
| Remote mutation | idempotent act layer: naturally idempotent write plus read-back, or a SQLite idempotency key claimed before the write and confirmed after read-back | unknown outcome is read back, never blindly repeated |
| Leg sandbox | a bwrap probe before every launch; the mount plan from harness-held facts | missing/unusable bwrap or an unsafe mount refuses the leg before spawn (`RUNTIME_UNAVAILABLE`, a $0 machine end under the backoff) |
| Run/process ownership | SQLite lease plus PID/PGID/start-identity receipts; bwrap inside the registered group | boot recovery kills/reclaims before admission; killing bwrap collapses the sandbox's PID namespace |
| Deduplication | namespaced SQLite idempotency keys | conflicts are loud; journal tails are never authority |
| Gate evidence | GitHub state, operator config | indeterminate is deny; worktree marker forgery has no authority |
| Logs/dashboard | plain per-role JSONL log, SQLite legs/money tables, GitHub | may be incomplete; never changes control decisions |

Fix agents push their own branch with plain `git push` (refactor decision
Q11), authenticated by `FIX_BOT_TOKEN` through an environment-config git
helper; nothing is written to the checkout's git config. The harness opens the
PR from a pushed `fix/<issue>-` branch owed one (ahead of base, no open PR),
once, through `adapters/act.py`, which records the branch first: only a
recorded branch's open PR is the harness's (ADR 0018). A fix leg can still run
`gh pr create` or `gh pr edit` with its token; a PR the agent opened is adopted
(recorded, no second one opened) while its head is the leg's push. `FIX_BOT_TOKEN`'s scope is therefore what a
hijacked fix leg can do: it must not carry Administration or a branch
protection bypass.
A registered supervisor ancestor keeps the run's accounting identity while
provider and target-command environments omit it. The review bot's token and
GitHub App key stay with the act layer, outside every sandbox.

Without bubblewrap (macOS, or a host that forbids unprivileged user
namespaces) paid legs are refused; there is no unsandboxed mode.
