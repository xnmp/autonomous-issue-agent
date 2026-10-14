"""The filesystem a paid leg sees: a declarative mount plan (gap 24, #413; ADR 0001).

Every leg runs as the operator's Unix user, so before the sandbox a leg could
read `config/shared.env` (the review bot's token), `~/.config/gh` (the owner's
admin login) or `/proc/<daemon>/environ`. The plan names what a leg may see,
built from facts the harness already holds: the leg's checkout, its git
metadata, the runtime binary and session store, and the toolchains on PATH.
Everything else (the operator's HOME, the harness checkout, state dirs, other
checkouts) is absent. The launch adapter renders the plan for bubblewrap.

Pure: absolute paths in, mounts out. Whether a path exists on this host is the
adapter's question; mounts that differ across hosts are `optional`.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum


class Access(Enum):
    READ_ONLY = "ro"
    READ_WRITE = "rw"
    # The host's content is visible; the leg's writes vanish with the leg.
    THROWAWAY = "overlay"
    # An empty, leg-private directory.
    PRIVATE = "tmpfs"
    # Host device nodes, usable as on the host.
    DEVICE = "dev"


@dataclass(frozen=True)
class Mount:
    path: str
    access: Access
    # Skipped when the host lacks the path (distribution layouts differ). A
    # required mount the host lacks refuses the leg.
    optional: bool = False


@dataclass(frozen=True)
class GitLink:
    """A checkout's git metadata, validated by the adapter against the target
    repository: `common_dir` is the repository's `.git`, `worktree_dir` this
    checkout's `<common>/worktrees/<name>`."""

    common_dir: str
    worktree_dir: str


@dataclass(frozen=True)
class LegFacts:
    home: str
    cwd: str
    # The leg edits its checkout and publishes (a fix leg). A reviewer or a
    # read-only light leg sees its checkout read-only, with no credential.
    writable: bool
    git: GitLink | None
    runtime_binary: str
    # The directories the runtime ships its helpers in (codex's code-mode host
    # beside its binary). Read-only, dropped when one would expose a protected path.
    runtime_support: tuple[str, ...] = ()
    # The runtime's session store and login, which `--resume` needs.
    runtime_state: tuple[str, ...] = ()
    search_path: tuple[str, ...] = ()
    # A build cache outside the checkout the leg's env names (CARGO_TARGET_DIR).
    build_cache: tuple[str, ...] = ()
    # Per-run files the harness hands the runtime (codex's schema and output).
    scratch: tuple[str, ...] = ()
    # The host's GPU render nodes. Xvfb and WebKitGTK load its EGL vendor
    # libraries even when rendering in software, and NVIDIA's aborts without
    # one, so no native E2E display starts. Render nodes only: never the
    # display (card) nodes the compositor drives.
    devices: tuple[str, ...] = ()
    # Never mounted, nor any directory holding one: HOME, the harness checkout,
    # state and control dirs. A mount strictly inside one is allowed.
    protected: tuple[str, ...] = ()


@dataclass(frozen=True)
class MountPlan:
    # Applied in order: a parent before anything inside it.
    mounts: tuple[Mount, ...]
    cwd: str
    # (name, value); None unsets. Never a credential: bwrap's argv is readable.
    env: tuple[tuple[str, str | None], ...]
    publishes: bool


class UnsafeMount(ValueError):
    """A required mount would expose a protected path, or is not absolute."""


# Read-only system roots. /run is absent (D-Bus, keyring and agent sockets)
# except the resolver and NixOS's system profile.
SYSTEM_ROOTS = (
    "/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/etc", "/opt", "/nix", "/sys",
    "/run/current-system", "/run/wrappers", "/run/systemd/resolve",
)

# Toolchain installs under HOME. Read-only: a leg that could rewrite a compiler
# or interpreter would poison the operator's own builds outside the sandbox.
# Cargo's config is among them: its linker and rustflags are part of every
# artifact's hash, so a leg without it rebuilt a shared CARGO_TARGET_DIR from
# scratch (tauri probe, 2026-10-14). Its tokens live in credentials.toml.
HOME_TOOLCHAINS = (
    ".rustup", ".cargo/bin", ".cargo/config.toml", ".cargo/config", ".bun/bin", ".local/bin",
    ".local/share/uv/python", ".nvm", ".npm-global", ".deno/bin", ".nix-profile", ".cache/ms-playwright",
)

# Download caches: a per-leg overlay. Reads hit the operator's cache; a leg's
# writes (a crate or wheel it fetched, or tampered with) never reach the
# operator's own builds. The price is re-downloading what a leg adds.
HOME_CACHES = (".cargo/registry", ".cargo/git", ".cache/uv", ".bun/install/cache", ".npm")

# Secret-bearing HOME directories: no PATH entry may mount one or its parent.
HOME_SECRETS = (
    ".ssh", ".gnupg", ".config", ".local/share", ".local/state", ".password-store", ".aws",
    ".docker", ".kube", ".netrc", ".git-credentials", ".cargo/credentials.toml", ".cargo/credentials",
    ".claude.json",
)

# Temp dirs point inside the sandbox; XDG overrides fall back to HOME, which is private.
LEG_ENV: tuple[tuple[str, str | None], ...] = (
    ("TMPDIR", "/tmp"), ("TMP", "/tmp"), ("TEMP", "/tmp"),
    ("XDG_CACHE_HOME", None), ("XDG_CONFIG_HOME", None), ("XDG_DATA_HOME", None),
)

# The GitHub credentials a leg may hold. Only a publishing leg keeps the first
# two (its one token, set by the launcher); the sandbox unsets the rest.
PUBLICATION_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")

_RENDER_NODE = re.compile(r"/dev/dri/renderD[0-9]+")

# Claude Code's project-directory name limit; longer paths get a hash suffix
# whose function the CLI does not publish (CLI 2.1.299).
_CLAUDE_PROJECT_NAME_LIMIT = 200


def _normal(path: str) -> str:
    if not isinstance(path, str) or not path.startswith("/") or "\0" in path:
        raise UnsafeMount(f"mount path {path!r} is not absolute")
    return posixpath.normpath(path)


def _contains(parent: str, child: str) -> bool:
    return parent == child or parent == "/" or child.startswith(parent.rstrip("/") + "/")


def exposes(path: str, protected: Iterable[str]) -> str:
    """The protected path a mount of `path` would expose (`path` is it or
    holds it), or ""."""
    return next((p for p in protected if _contains(path, p)), "")


def protected_paths(home: str, extra: Iterable[str] = ()) -> tuple[str, ...]:
    """HOME, its secret-bearing directories, and the harness's own paths."""
    home = _normal(home)
    return tuple(dict.fromkeys([home, *(posixpath.join(home, s) for s in HOME_SECRETS),
                                *(_normal(p) for p in extra if p)]))


def claude_session_store(config_dir: str, cwd: str) -> str:
    """Where Claude Code keeps the sessions of a process started in `cwd`
    (`<config>/projects/<cwd, non-alphanumerics as '-'>`)."""
    name = re.sub(r"[^A-Za-z0-9]", "-", _normal(cwd))
    if len(name) > _CLAUDE_PROJECT_NAME_LIMIT:
        raise UnsafeMount(f"checkout path is longer than Claude's {_CLAUDE_PROJECT_NAME_LIMIT}-character "
                          "session-store name; resume could not find its session")
    return posixpath.join(_normal(config_dir), "projects", name)


def leg_mount_plan(facts: LegFacts) -> MountPlan:
    """The mounts for one leg. Raises `UnsafeMount` when a required mount
    would expose a protected path; optional ones that would are dropped."""
    home = _normal(facts.home)
    protected = protected_paths(home, facts.protected)
    cwd = _normal(facts.cwd)
    writable = Access.READ_WRITE if facts.writable else Access.READ_ONLY

    required: list[Mount] = [Mount(_normal(facts.runtime_binary), Access.READ_ONLY)]
    required += [Mount(_normal(p), Access.READ_WRITE) for p in (*facts.runtime_state, *facts.build_cache,
                                                                 *facts.scratch)]
    required.append(Mount(cwd, writable))
    if facts.git is not None:
        common, own = _normal(facts.git.common_dir), _normal(facts.git.worktree_dir)
        # The repository's config, hooks and other checkouts' links never
        # change on the host: the harness runs git in every checkout outside
        # the sandbox, so a leg that could write them could run code there. A
        # fix leg's git still writes beside them (packed-refs.lock on every
        # commit, a scratch `git worktree add`), so its writes there land in
        # an overlay that vanishes with the leg (probe, 2026-10-14).
        required.append(Mount(common, Access.THROWAWAY if facts.writable else Access.READ_ONLY))
        if facts.writable:
            required += [Mount(posixpath.join(common, d), Access.READ_WRITE) for d in ("objects", "refs")]
            required.append(Mount(own, Access.READ_WRITE))
            # Its own links and config.worktree, which the harness's git reads
            # as repository config (hooksPath, fsmonitor) wherever the repository
            # enables extensions.worktreeConfig, stay read-only.
            required += [Mount(posixpath.join(own, f), Access.READ_ONLY)
                         for f in ("commondir", "gitdir", "config.worktree")]
            required.append(Mount(posixpath.join(cwd, ".git"), Access.READ_ONLY))
    for mount in required:
        if exposed := exposes(mount.path, protected):
            raise UnsafeMount(f"{mount.access.value} mount {mount.path} would expose {exposed}")

    optional = [Mount(p, Access.READ_ONLY, optional=True) for p in SYSTEM_ROOTS]
    optional += [Mount(_normal(p), Access.READ_ONLY, optional=True) for p in facts.runtime_support]
    optional += [Mount(posixpath.join(home, p), Access.READ_ONLY, optional=True) for p in HOME_TOOLCHAINS]
    optional += [Mount(posixpath.normpath(p), Access.READ_ONLY, optional=True)
                 for p in facts.search_path if isinstance(p, str) and p.startswith("/") and "\0" not in p]
    optional += [Mount(posixpath.join(home, p), Access.THROWAWAY, optional=True) for p in HOME_CACHES]
    optional += [Mount(p, Access.DEVICE, optional=True) for p in facts.devices
                 if isinstance(p, str) and _RENDER_NODE.fullmatch(p)]
    if facts.git is not None and facts.writable:
        optional.append(Mount(posixpath.join(_normal(facts.git.common_dir), "logs"), Access.READ_WRITE,
                              optional=True))
    optional = [m for m in optional if not exposes(m.path, protected)]

    # /tmp/.X11-unix exists, as on a host, so a leg's own Xvfb listens on a
    # socket file in its private /tmp, not only on the abstract socket every
    # leg and the host share. /dev/shm is its own tmpfs, as on a host: bwrap's
    # /dev makes it a plain directory, and tauri's tests rely on the mount.
    private = [Mount(home, Access.PRIVATE), Mount("/tmp", Access.PRIVATE), Mount("/tmp/.X11-unix", Access.PRIVATE),
               Mount("/var/tmp", Access.PRIVATE), Mount("/dev/shm", Access.PRIVATE)]
    # One mount per path, the first named winning: required facts over
    # optional conventions. A stable depth sort puts parents first and, at one
    # depth, private directories before binds.
    chosen: dict[str, Mount] = {}
    for mount in (*private, *required, *optional):
        chosen.setdefault(mount.path, mount)
    ordered = sorted(chosen.values(), key=lambda m: (0 if m.path == "/" else m.path.count("/"),
                                                     m.access is not Access.PRIVATE))
    # An optional mount inside one with the same or write access adds nothing
    # and could narrow it (a PATH entry inside the checkout would turn that
    # directory read-only).
    def covered(m: Mount) -> bool:
        return m.optional and any(
            p.path != m.path and _contains(p.path, m.path) and p.access in (m.access, Access.READ_WRITE)
            for p in ordered)

    ordered = [m for m in ordered if not covered(m)]
    credentials = PUBLICATION_ENV[2:] if facts.writable else PUBLICATION_ENV
    return MountPlan(tuple(ordered), cwd, LEG_ENV + tuple((name, None) for name in credentials),
                     publishes=facts.writable)


__all__ = [
    "Access", "GitLink", "LegFacts", "Mount", "MountPlan", "UnsafeMount", "claude_session_store", "exposes",
    "leg_mount_plan", "protected_paths",
]
