"""Crash-safe replacement: a reader sees the old bytes or the new, never a torn write."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write(path: str | os.PathLike[str], content: str | bytes, mode: int = 0o600) -> None:
    target = Path(path)
    data = content.encode("utf-8") if isinstance(content, str) else content
    fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
