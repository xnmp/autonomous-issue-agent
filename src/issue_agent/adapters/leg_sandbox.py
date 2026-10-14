"""Launch a paid leg inside bubblewrap (gap 24, #413; ADR 0001).

The runtime adapters hand their provider command here; `launch` resolves the
host facts (the checkout's git metadata, the runtime binary, the build cache),
builds the pure mount plan (`domain.sandbox`) and renders it as a `bwrap`
argv. The process supervisor still registers the launcher before exec, so the
sandbox sits inside the registered process group:

    supervised launcher (pgid leader) -> bwrap -> [pid namespace] provider ...

`--new-session` moves the provider out of that group, so group signals reach
only bwrap. bwrap does not forward SIGTERM: it dies, and `--die-with-parent`
plus the PID namespace take every sandboxed process down with it. A teardown is
therefore always a SIGKILL inside the sandbox.

A fix leg's one GitHub credential (FIX_BOT_TOKEN) travels in its environment,
never in argv or on disk; git reads it through a helper given as environment
config. Nothing here logs or returns a token in a loggable value.
"""

from __future__ import annotations

import glob
import os
import shutil
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from ..domain.sandbox import (
    Access, GitLink, LegFacts, Mount, MountPlan, UnsafeMount, claude_session_store, exposes, leg_mount_plan,
    protected_paths,
)

_NAMESPACES = ("--die-with-parent", "--unshare-pid", "--new-session", "--unshare-ipc", "--unshare-uts",
               "--unshare-cgroup-try")
_PROBE_TIMEOUT_S = 10.0
_GITFILE_LIMIT = 4096
# A Claude leg authenticates with its own credential; it never reads the operator's login.
_KEY_AUTH = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")

# Environment git config: never written into the checkout. The empty helper
# drops any helper /etc/gitconfig names; a publishing leg then answers GitHub
# from $GH_TOKEN. push.default=current: what a leg writes to the repository
# config vanishes with it, so `git push -u` cannot record an upstream for the
# next leg. No auto-maintenance: it would repack and prune the object store
# other checkouts share; the harness's own git, outside the sandbox, keeps it.
# No pruning fetch (origin's own setting wins over fetch.prune): a
# remote-tracking ref is usually packed, and a leg cannot
# rewrite packed-refs (`domain.sandbox`), so the prune would fail the fetch.
_GIT_CONFIG = (("credential.helper", ""), ("maintenance.auto", "false"), ("gc.auto", "0"), ("fetch.prune", "false"),
               ("remote.origin.prune", "false"))
_PUBLISH_GIT_CONFIG = (
    ("credential.https://github.com.helper",
     '!f() { test "$1" = get && echo username=x-access-token && echo "password=$GH_TOKEN"; }; f'),
    ("push.default", "current"),
)


class SandboxRefused(RuntimeError):
    """The leg cannot be sandboxed; it must not run."""


@dataclass(frozen=True)
class Launch:
    argv: tuple[str, ...]
    # Merged into the provider's environment. Holds the credential when the
    # leg publishes: never log it.
    env: Mapping[str, str]
    # Loggable: no paths a token could hide in, no credential.
    summary: str


def _read_small(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as stream:
        return stream.read(_GITFILE_LIMIT).strip()


def _gitfile_target(dot_git: str) -> str:
    content = _read_small(dot_git)
    if not content.startswith("gitdir:"):
        raise SandboxRefused(f"{dot_git} is not a git link")
    target = content.removeprefix("gitdir:").strip()
    return os.path.realpath(os.path.join(os.path.dirname(dot_git), target))


def repository_common_dir(repository: str) -> str:
    """The target repository's `.git` directory, read without running git."""
    dot = os.path.join(repository, ".git")
    if os.path.isdir(dot) and not os.path.islink(dot):
        return os.path.realpath(dot)
    if os.path.isfile(dot):
        gitdir = _gitfile_target(dot)
        return os.path.realpath(os.path.join(gitdir, _read_small(os.path.join(gitdir, "commondir"))))
    raise SandboxRefused(f"target {repository} has no git repository")


def git_link(cwd: str, repository: str) -> GitLink | None:
    """The checkout's git metadata, or None when `cwd` is not a checkout.

    The leg can write its checkout, so its `.git` link is a claim: it must name
    `<target .git>/worktrees/<name>`, whose own back-link and common-dir link
    name this checkout and the target. A link that does not is refused, never
    mounted (it could point the mount, and the harness's own git, anywhere)."""
    dot = os.path.join(cwd, ".git")
    if not os.path.lexists(dot):
        return None
    common = repository_common_dir(repository)
    if os.path.islink(dot) or not os.path.isfile(dot):
        raise SandboxRefused(f"{cwd} is not a harness worktree of {repository}")
    try:
        own = _gitfile_target(dot)
        back = os.path.realpath(_read_small(os.path.join(own, "gitdir")))
        shared = os.path.realpath(os.path.join(own, _read_small(os.path.join(own, "commondir"))))
    except OSError as exc:
        raise SandboxRefused(f"{cwd}'s git link is unreadable ({type(exc).__name__})") from exc
    if (os.path.dirname(own) != os.path.join(common, "worktrees") or back != os.path.realpath(dot)
            or shared != common):
        raise SandboxRefused(f"{cwd}'s git link does not resolve to a worktree of {repository}")
    return GitLink(common, own)


def _render(mount: Mount, protected: Sequence[str]) -> tuple[str, ...]:
    path = mount.path
    if mount.access is Access.PRIVATE:
        return ("--tmpfs", path)
    # A top-level system link (/bin -> usr/bin) stays a link.
    if mount.optional and os.path.dirname(path) == "/" and os.path.islink(path):
        return ("--symlink", os.readlink(path), path)
    if not os.path.exists(path):  # absent, or a dangling link
        if mount.optional:
            return ()
        raise SandboxRefused(f"{path} is missing")
    # bwrap follows a source link, so the plan's protection is re-checked
    # where the path really leads (a PATH entry linked to ~/.ssh).
    if exposed := exposes(os.path.realpath(path), protected):
        if mount.optional:
            return ()
        raise SandboxRefused(f"{path} leads to {exposed}, which no leg may see")
    if mount.access is Access.READ_ONLY:
        return ("--ro-bind", path, path)
    if mount.access is Access.READ_WRITE:
        return ("--bind", path, path)
    if mount.access is Access.DEVICE:
        return ("--dev-bind", path, path)
    return ("--overlay-src", path, "--tmp-overlay", path) if os.path.isdir(path) else ()


def bwrap_arguments(plan: MountPlan, protected: Sequence[str]) -> tuple[str, ...]:
    """The plan as bwrap options. The root stays a read-only tmpfs, so
    nothing outside the mounts exists for the leg."""
    args: list[str] = [*_NAMESPACES, "--dev", "/dev", "--proc", "/proc"]
    for mount in plan.mounts:
        args += _render(mount, protected)
    args += ["--remount-ro", "/"]
    for name, value in plan.env:
        args += ["--unsetenv", name] if value is None else ["--setenv", name, value]
    return (*args, "--chdir", plan.cwd)


def _empty_worktree_config(path: str) -> None:
    """Mounted read-only, so it must exist, and empty: the harness owns its
    worktrees and sets nothing there, so any content is a leg's from before
    the mount (a hooksPath or fsmonitor the harness's git would run). A link,
    FIFO or hard link there is tampering; it is never followed or waited on."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644)
    except OSError as exc:
        raise SandboxRefused(f"{path} is not a plain file ({exc.strerror})") from exc
    try:
        status = os.fstat(fd)
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            raise SandboxRefused(f"{path} is not a plain file")
        os.ftruncate(fd, 0)
    finally:
        os.close(fd)


def _ensure_packed_refs(common: str) -> None:
    """A fix leg sees `packed-refs` read-only, so it must exist: without it
    the leg's `git pack-refs` would pack into its overlay and delete the
    host's loose refs. An empty one is what git reads when there is none."""
    path = os.path.join(common, "packed-refs")
    try:
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644))
    except FileExistsError:
        pass
    except OSError as exc:
        raise SandboxRefused(f"{path} cannot be created ({exc.strerror})") from exc
    if os.path.islink(path) or not os.path.isfile(path):
        raise SandboxRefused(f"{path} is not a plain file")


# The repository config that keeps every git on the host, the harness's and
# the operator's, from packing refs: a fix leg reads packed-refs as of its
# launch, so a pack while it runs moves its branch out of the live refs/ and
# the leg loses it (tauri #1106, 2026-10-15: the auto-maintenance a fetch
# starts runs pack-refs once 16 refs are loose; gc.packRefs alone misses it).
_LOOSE_REFS = (("gc.packRefs", "false"), ("maintenance.pack-refs.enabled", "false"))


def _keep_refs_loose(common: str, run: Callable[..., subprocess.CompletedProcess]) -> None:
    """Set each key in the repository config, then check the value git
    actually uses (an include or the main checkout's config.worktree can
    override the file); a leg does not start on a repository that may pack."""
    config = os.path.join(common, "config")

    def git(*args: str) -> subprocess.CompletedProcess:
        return run(["git", *args], capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S)

    try:
        for key, value in _LOOSE_REFS:
            if (git("config", "--file", config, "--get", key).stdout or "").strip() != value:
                done = git("config", "--file", config, key, value)
                if done.returncode != 0:
                    raise SandboxRefused(f"cannot set {key}={value} in {config}: "
                                         f"{(done.stderr or '').strip()[:200]}")
            effective = (git("--git-dir", common, "config", "--get", key).stdout or "").strip()
            if effective != value:
                raise SandboxRefused(f"{key} is {effective!r} for {common} despite its config (an include or "
                                     f"config.worktree overrides it); a fix leg needs {value}")
    except (OSError, subprocess.SubprocessError) as exc:
        raise SandboxRefused(f"cannot read {config} ({type(exc).__name__})") from exc


def _git_config_env(entries: Sequence[tuple[str, str]]) -> dict[str, str]:
    env = {"GIT_CONFIG_COUNT": str(len(entries))}
    for index, (key, value) in enumerate(entries):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


def _claude_config(env: Mapping[str, str]) -> str:
    return os.path.abspath(env.get("CLAUDE_CONFIG_DIR")
                           or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".claude"))


def claude_runtime_state(env: Mapping[str, str], cwd: str) -> tuple[str, ...]:
    """Claude's session store for `cwd`, created: the only part of the
    operator's `~/.claude` a leg sees, so `--resume` finds its session. Refuses
    a leg with no credential of its own."""
    if not any(env.get(name) for name in _KEY_AUTH):
        raise SandboxRefused(
            "a Claude leg needs its own credential: CLAUDE_CODE_OAUTH_TOKEN (run `claude setup-token`, store it "
            "as ~/.config/credstore.encrypted/CLAUDE_CODE_OAUTH_TOKEN, HARNESS.md \"Secrets as systemd "
            "credentials\"), ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN")
    try:
        store = claude_session_store(_claude_config(env), os.path.realpath(cwd))
    except UnsafeMount as exc:
        raise SandboxRefused(str(exc)) from exc
    os.makedirs(store, mode=0o700, exist_ok=True)
    return (store,)


def codex_runtime_state(env: Mapping[str, str], cwd: str) -> tuple[str, ...]:
    """The harness CODEX_HOME (sessions, config) and the operator login its
    `auth.json` links to. Codex's default `~/.codex` is never mounted.

    The login is bound as a single file:
    codex rewrites `auth.json` in place (truncate and write, codex-rs
    login/src/auth/storage.rs), so its inode never changes under a running
    leg. A codex that replaces it by rename needs the login directory."""
    configured = env.get("CODEX_HOME", "")
    if not configured:
        raise SandboxRefused("a codex leg needs the harness CODEX_HOME")
    home = os.path.abspath(os.path.join(cwd, configured))
    auth = os.path.join(home, "auth.json")
    return (home, os.path.realpath(auth)) if os.path.islink(auth) and os.path.isfile(auth) else (home,)


class LegSandbox:
    """One role's sandbox: its facts are fixed at composition, each launch
    adds the leg's own (checkout, runtime, state)."""

    def __init__(
        self,
        *,
        home: str,
        repository: str,
        writable: bool,
        credential: str | None = None,
        protected: Sequence[str] = (),
        bwrap: str = "bwrap",
        which: Callable[..., str | None] = shutil.which,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ):
        self.home = os.path.realpath(home)
        self.repository = repository
        self.writable = writable
        self._credential = (credential or "").strip() or None
        self.protected = tuple(os.path.realpath(p) for p in protected if p)
        self._bwrap = bwrap
        self._which = which
        self._run = run

    def __repr__(self) -> str:
        return (f"LegSandbox(repository={self.repository!r}, writable={self.writable}, "
                f"credential={'present' if self._credential else 'absent'})")

    @property
    def has_credential(self) -> bool:
        return self._credential is not None

    def _binary(self) -> str | None:
        return self._which(self._bwrap)

    def preflight(self) -> str | None:
        """Why legs cannot be sandboxed here, or None. Probes the namespaces
        and the overlay the launch uses; there is no unsandboxed fallback."""
        binary = self._binary()
        if binary is None:
            return (f"bubblewrap ({self._bwrap}) is not installed; every leg runs under it "
                    "(macOS has none: docs/open/residuals.md)")
        true = self._which("true") or "/bin/true"
        probe = [binary, *_NAMESPACES, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
                 "--tmpfs", "/tmp", "--overlay-src", os.path.dirname(true), "--tmp-overlay", "/tmp/probe",
                 "--", true]
        try:
            done = self._run(probe, capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError) as exc:
            return f"bubblewrap probe failed to run ({type(exc).__name__})"
        if done.returncode != 0:
            detail = (done.stderr or "").strip().splitlines()
            if any("Unknown option" in line for line in detail):
                # Ubuntu 24.04 ships 0.9.0, which predates the overlays.
                return (f"bubblewrap is too old ({detail[-1][:120]}); legs need 0.10 or newer for "
                        "--overlay-src")
            return (f"bubblewrap cannot create a sandbox here (exit {done.returncode}: "
                    f"{(detail[-1] if detail else 'no message')[:200]}); unprivileged user namespaces "
                    "must be enabled")
        return None

    def posture(self) -> str:
        """One line for the journal and doctor: what a leg starts under."""
        reason = self.preflight()
        if reason is not None:
            return f"unavailable: {reason}"
        try:
            version = self._run([self._binary() or self._bwrap, "--version"], capture_output=True,
                                text=True, timeout=_PROBE_TIMEOUT_S).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            version = ""
        return f"bwrap {version.removeprefix('bubblewrap').strip() or 'unknown version'}"

    def launch(
        self,
        command: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        runtime_state: Sequence[str] = (),
        scratch: Sequence[str] = (),
        read_only: bool = False,
    ) -> Launch:
        """`command` wrapped in bwrap, plus the environment it adds. Raises
        `SandboxRefused` when the leg cannot run sandboxed."""
        if not command:
            raise SandboxRefused("no command to sandbox")
        if (reason := self.preflight()) is not None:
            raise SandboxRefused(reason)
        binary = command[0] if os.path.isabs(command[0]) else self._which(command[0], path=env.get("PATH"))
        if not binary or not os.path.isfile(binary):
            raise SandboxRefused(f"runtime binary {command[0]!r} does not resolve")
        support = tuple(dict.fromkeys((os.path.dirname(os.path.realpath(binary)),
                                       os.path.dirname(os.path.abspath(binary)))))
        binary = os.path.realpath(binary)
        root = os.path.realpath(cwd)
        build = env.get("CARGO_TARGET_DIR", "")
        build_cache: tuple[str, ...] = ()
        if os.path.isabs(build):
            os.makedirs(build, exist_ok=True)
            build_cache = (os.path.realpath(build),)
        writes = self.writable and not read_only
        link = git_link(root, self.repository)
        if link is not None and writes:
            _ensure_packed_refs(link.common_dir)
            _keep_refs_loose(link.common_dir, self._run)
            _empty_worktree_config(os.path.join(link.worktree_dir, "config.worktree"))
        try:
            plan = leg_mount_plan(LegFacts(
                home=self.home, cwd=root, writable=writes,
                git=link, runtime_binary=binary, runtime_support=support,
                runtime_state=tuple(os.path.abspath(p) for p in runtime_state),
                search_path=tuple(env.get("PATH", "").split(os.pathsep)), build_cache=build_cache,
                scratch=tuple(os.path.abspath(p) for p in scratch), protected=self.protected,
                devices=tuple(sorted(glob.glob("/dev/dri/renderD*"))),
            ))
        except UnsafeMount as exc:
            raise SandboxRefused(str(exc)) from exc
        argv = (self._binary() or self._bwrap, *bwrap_arguments(plan, protected_paths(self.home, self.protected)),
                "--", binary, *command[1:])
        credential = self._credential if plan.publishes else None
        added = _git_config_env(_GIT_CONFIG + (_PUBLISH_GIT_CONFIG if plan.publishes else ()))
        if credential:
            added.update({"GH_TOKEN": credential, "GITHUB_TOKEN": credential})
        writable = sum(1 for m in plan.mounts if m.access is Access.READ_WRITE)
        summary = (f"bwrap: {'writes' if plan.publishes else 'reads'} its checkout; {writable} read-write "
                   f"mount(s); GitHub credential {'present' if credential else 'absent'}")
        return Launch(argv, added, summary)


__all__ = [
    "Launch", "LegSandbox", "SandboxRefused", "bwrap_arguments", "claude_runtime_state",
    "codex_runtime_state", "git_link", "repository_common_dir",
]
