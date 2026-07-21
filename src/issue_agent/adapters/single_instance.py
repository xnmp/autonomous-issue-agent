"""Single-instance guard — one daemon per (role, repo).

Two fix daemons polling the same repo both grab `ai-ready` and dispatch in
parallel, breaking the single-fixer invariant (observed: #54/#58 dispatched
concurrently after an accidental double `point-at` launch). This is a
process-level lock, not a domain concern, so it lives in adapters.

Mechanism: an advisory `flock(LOCK_EX | LOCK_NB)` on a per-(role, repo) file.
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
    """Another daemon already holds the (role, repo) lock."""


def _lock_path(role: str, repo: str) -> str:
    """A stable path unique to (role, repo). repo is hashed so a slash-bearing
    `owner/name` is filesystem-safe and the path length stays bounded."""
    key = hashlib.sha1(f"{role}\0{repo}".encode()).hexdigest()[:16]
    safe_role = "".join(c if c.isalnum() else "-" for c in role)
    return os.path.join(tempfile.gettempdir(), f"issue-agent-{safe_role}-{key}.lock")


def acquire(role: str, repo: str) -> InstanceLock:
    """Take the (role, repo) lock or raise AlreadyRunning. Non-blocking: a second
    daemon fails fast instead of queueing behind the first."""
    path = _lock_path(role, repo)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
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
