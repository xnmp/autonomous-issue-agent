"""Live knobs: the ADR 0007 amendment's per-tick settings.

A knob marked `live` (dispatch-only: routes, dispatch concurrency, the issue
and daily caps, the issue pickup) changes without a restart. Each tick's
observation calls `refresh()`, which re-reads the composed env file only when
its bytes changed, validates the candidate with `validation_errors()`, and on
success swaps in a new immutable snapshot: the boot snapshot with the
candidate's live knobs (`config.with_live`). Readers take `current`, a whole
snapshot, and pass its values on; nothing in the composed graph mutates.

An invalid candidate keeps the previous snapshot and is logged once per
file hash. Non-live knobs never change here: `ReloadWatch` restarts for
them (it fingerprints only the non-live part, `config.reload_fingerprint`).
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable

from ..adapters.env_file import source_env_file
from .config import LIVE_KNOBS, HarnessConfig, from_env, with_live


def _digest(path: str) -> str:
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return ""


def _unlogged(_message: str) -> None:
    """A static config has nothing to log."""


def read_candidate(path: str) -> HarnessConfig:
    """The config the env file at `path` describes, the file the authority.
    Strict: a file that cannot be sourced raises. `from_env`'s AGENT_ENV path
    falls back to the inherited environment, which under point-at.sh still
    holds every launch-time knob, so a sourcing timeout on a loaded host would
    silently revert a lowered cap."""
    inherited = {key: value for key, value in os.environ.items() if key != "AGENT_ENV"}
    return from_env(source_env_file(path, {**inherited, "AGENT_ENV": path}, assigned_only=True))


class LiveConfig:
    """The latest valid snapshot. `path` "" is a static config (no env file)."""

    def __init__(
        self,
        boot: HarnessConfig,
        path: str = "",
        *,
        role: str = "fix",
        read: Callable[[str], HarnessConfig] = read_candidate,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._boot = boot
        self._current = boot
        self._path = path
        self._read = read
        self._log = log or _unlogged
        self._role = role
        self._bound = boot.runtimes(role)
        self._seen = _digest(path) if path else ""
        self._unreadable = ""  # the digest last logged unreadable: retried each tick, logged once

    @property
    def current(self) -> HarnessConfig:
        return self._current

    def refresh(self) -> HarnessConfig:
        """Re-read the env file if its bytes changed; the snapshot to use."""
        if not self._path:
            return self._current
        digest = _digest(self._path)
        if digest == self._seen:
            return self._current
        try:
            raw = self._read(self._path)
            candidate = with_live(self._boot, raw)
        except Exception as exc:  # noqa: BLE001 - a bad file keeps the running settings
            # Not marked seen: a transient failure (a bash timeout under load)
            # is retried next tick instead of pinning the old values until
            # the file changes again.
            if self._unreadable != digest:
                self._unreadable = digest
                self._log(f"live settings: {self._path} unreadable, keeping the current ones (retrying): {exc}")
            return self._current
        self._seen = digest
        errors = list(dict.fromkeys((*raw.validation_errors(), *candidate.validation_errors())))
        if unbound := sorted(r.value for r in candidate.runtimes(self._role) - self._bound):
            errors.append(f"needs runtime(s) {', '.join(unbound)} this process did not bind "
                          "(applies after the restart that change triggers)")
        if errors:
            self._log("live settings: candidate refused, keeping the current ones: " + "; ".join(errors))
            return self._current
        changed = [f"{env}={getattr(candidate, name)!r}" for name, env in LIVE_KNOBS.items()
                   if getattr(candidate, name) != getattr(self._current, name)]
        if changed:
            self._log("live settings applied: " + ", ".join(changed))
        self._current = candidate
        return candidate
