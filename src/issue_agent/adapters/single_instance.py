"""Single-instance guard — one daemon per repository checkout and role.

Two fix daemons polling the same repo both grab `ai-ready` and dispatch in
parallel, breaking the single-fixer invariant (observed: #54/#58 dispatched
concurrently after an accidental double `point-at` launch). This is a
process-level lock, not a domain concern, so it lives in adapters.

Mechanism: an advisory `flock(LOCK_EX | LOCK_NB)` in a private per-user runtime
directory. The directory is deliberately independent of configured state roots:
two overlays aimed at the same checkout must contend on the same lock.
flock is held by the OPEN FILE DESCRIPTION and released by the kernel when the
process exits — crash, kill -9, or clean shutdown alike. So unlike a pidfile
holding a written PID, there is no stale-lock problem: a dead daemon's lock is
already gone by the time the next one starts. We keep the fd open for the life
of the process (stashed on the returned handle) so the lock persists; closing it
(or exiting) frees it.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass


@dataclass
class InstanceLock:
    """Held lock. Keep the instance alive for the process's lifetime (the open fd
    IS the lock); dropping it / exiting releases the lock."""

    path: str
    fd: int

    def release(self) -> None:
        """Explicitly release (normally unnecessary — process exit frees it)."""
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass


class AlreadyRunning(RuntimeError):
    """Another daemon already holds the deployment lock."""


class UnsafeLockFile(RuntimeError):
    """The lock path is not a regular file owned by this deployment."""


def _lock_path(role: str, repo: str, target_dir: str = "", state_dir: str = "") -> str:
    """Stable lock path unique to ``(role, repo, absolute target checkout)``."""
    if not repo.strip():
        raise ValueError("REPO must be non-empty before acquiring a daemon lock")
    checkout = os.path.realpath(target_dir) if target_dir else ""
    key = hashlib.sha256(f"{role}\0{repo}\0{checkout}".encode()).hexdigest()[:20]
    safe_role = "".join(c if c.isalnum() else "-" for c in role)
    del state_dir
    parent = os.path.join(tempfile.gettempdir(), f"issue-agent-{os.getuid()}-locks")
    return os.path.join(parent, f"issue-agent-{safe_role}-{key}.lock")


def _ensure_lock_parent(path: str) -> None:
    parent = os.path.dirname(path)
    try:
        os.mkdir(parent, 0o700)
    except FileExistsError:
        pass
    metadata = os.lstat(parent)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise UnsafeLockFile(f"daemon lock directory must be private and owned by this user: {parent}")


def acquire(role: str, repo: str, *, target_dir: str = "", state_dir: str = "") -> InstanceLock:
    """Take the deployment lock or fail without following a foreign symlink."""
    path = _lock_path(role, repo, target_dir, state_dir)
    _ensure_lock_parent(path)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EISDIR):
            raise UnsafeLockFile(f"unsafe daemon lock path: {path}") from exc
        raise
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise UnsafeLockFile(f"daemon lock is not a regular file: {path}")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        if e.errno in (errno.EWOULDBLOCK, errno.EACCES):
            raise AlreadyRunning(
                f"another '{role}' daemon is already running for {repo} "
                f"(lock held: {path}). Refusing to start a second — a duplicate "
                "would double-dispatch. Stop the running one first."
            ) from e
        raise
    # Record our pid for humans inspecting the lock file (NOT used for liveness —
    # flock is the source of truth). Best-effort.
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
    except OSError:
        pass
    return InstanceLock(path=path, fd=fd)
