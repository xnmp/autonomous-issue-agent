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
| its checkout; for a fix leg also the repository's `objects/`, `refs/`, `logs/` and its own `worktrees/<name>/` | fix: read-write; review/light: read-only |
| the repository's `.git` (config, hooks, other checkouts' links), but for objects, refs and reflogs | fix: per-leg overlay (git's lock files, `push -u`'s config and a scratch `git worktree add` work; nothing reaches the host); review/light: read-only |
| the repository's `objects/`, `refs/`, `logs/` | fix: read-write, live (the host's ref changes and lock files are the leg's); review/light: read-only |
| the repository's `packed-refs` (created empty when absent) | read-only as of launch (a leg's `pack-refs` fails instead of pruning the host's loose refs; the harness's git never packs refs) |
| the checkout's `.git` link and its own `commondir`, `gitdir` and `config.worktree` (the harness's git reads it as config where `extensions.worktreeConfig` is on; emptied at every launch, since the harness sets nothing there) | read-only |
| `/dev/shm` | its own empty tmpfs |
| the runtime binary; `/usr`, `/etc` (whole: operator secrets live in HOME, root-only files stay unreadable), `/opt`, `/nix`, `/sys`, the resolver; toolchains on PATH, `~/.rustup`, `~/.cargo/bin`, `~/.cargo/config.toml` (its linker and rustflags hash every artifact in a shared `CARGO_TARGET_DIR`), `~/.bun/bin`, uv's Pythons, `~/.nvm`, Playwright browsers | read-only |
| `~/.cargo/registry`, `~/.cargo/git`, `~/.cache/uv`, `~/.bun/install/cache`, `~/.npm` | per-leg overlay (writes discarded) |
| the runtime session store and login (`~/.claude/projects/<checkout>`; the harness CODEX_HOME and the `auth.json` it links), `CARGO_TARGET_DIR`, codex's per-run scratch | read-write (the login too: refresh rotates it; residuals §1.1) |
| Claude's config dir `~/.claude`, when it holds an OAuth login and the leg has no key or token of its own: bound whole so the login the host renames and the refresh lock stay shared; only `.credentials.json`, `.oauth_refresh.lock(.owner)` and `.storage-write.lock` show through | read-write directory; every other entry present at launch hidden (empty), Claude's settings, memory and extension names first created empty and inert where absent; a host rename of a hidden entry lifts its mask, residuals §1.1 |
| HOME, `/tmp` (with an empty `/tmp/.X11-unix`, so a leg's own Xvfb has a socket directory), `/var/tmp` | empty, leg-private |
| GPU render nodes (`/dev/dri/renderD*`, never the display `card*` nodes): NVIDIA's EGL library aborts Xvfb and WebKitGTK without one | device |
| network | on (no egress filter) |

Not visible: `config/` and the rest of the harness checkout, the operator's
HOME (`~/.config/gh`, `~/.ssh`, keyrings, `~/.claude.json`, settings, other
sessions), state and control dirs (SQLite), other checkouts' files, other
repositories, `/run` (D-Bus, agent and Docker sockets; a bus on an abstract
socket stays reachable, residuals §1.1), the host's X and Wayland sockets, and
host processes
(`--unshare-pid`, so no `/proc/<daemon>/environ`). Git in a leg runs no
auto-maintenance (`gc.auto=0`, `maintenance.auto=false` in its environment), so
it never repacks the object store other checkouts share.

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
once, through `adapters/act.py`. A fix leg can still run `gh pr create` or
`gh pr edit` with its token; a PR the agent opened is simply observed and the
harness opens no second one. `FIX_BOT_TOKEN`'s scope is therefore what a
hijacked fix leg can do: it must not carry Administration or a branch
protection bypass.
A registered supervisor ancestor keeps the run's accounting identity while
provider and target-command environments omit it. The review bot's token and
GitHub App key stay with the act layer, outside every sandbox.

Without bubblewrap (macOS, or a host that forbids unprivileged user
namespaces) paid legs are refused; there is no unsandboxed mode.
