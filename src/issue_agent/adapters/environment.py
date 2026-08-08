"""Boot probe for the harness-attested environment manifest.

The I/O edge only: read the daemon's own environment and PATH and hand the
readings to `domain.environment`, which owns every rule about what they mean.

Incident (2026-08-08, tauri PRs #613/#614): the reviewer demanded live
native-app evidence — screenshots of a running GUI — that the deployment
physically cannot produce (no display, no WebDriver). The fixer's "impossible
here" went out on the operator-block channel the reviewer never reads, so the
loop rejected to its round ceiling twice. Both sides now get the same attested
readings instead of arguing about them through channels that do not connect.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping

from ..domain.environment import (
    PROBED_BINARIES,
    EnvironmentManifest,
    manifest,
    observe,
)

# Successful probes only (see `boot_manifest`), keyed by declared stack.
_CACHE: dict[str, EnvironmentManifest] = {}


def probe_manifest(
    declared_stack: str = "",
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> EnvironmentManifest:
    """Read environment + PATH, render the manifest.

    Returns an EMPTY manifest when the probe cannot run. Absent must not read as
    "everything is possible": a false attestation is worse than none, so a
    failed probe says nothing at all and both sides keep their prior behaviour.
    """
    try:
        env = dict(os.environ if environ is None else environ)
        present = {name: which(name) is not None for name in PROBED_BINARIES}
        return manifest(observe(env, present), declared_stack)
    except Exception:
        return EnvironmentManifest()


def boot_manifest(declared_stack: str = "") -> EnvironmentManifest:
    """The manifest for THIS daemon process, probed once per declared stack.

    Cached because it is a boot-time fact and roles are recomposed on every
    hot-reload. Only a SUCCESSFUL probe is cached: a transient failure (an
    unreadable PATH, a filesystem blip) must not silence the manifest for the
    life of the process — the next call probes again."""
    cached = _CACHE.get(declared_stack)
    if cached is not None:
        return cached
    probed = probe_manifest(declared_stack)
    if probed.attested:
        _CACHE[declared_stack] = probed
    return probed


def reset_boot_manifest_cache() -> None:
    """Drop the boot cache. For tests and for a re-exec that stays in-process."""
    _CACHE.clear()
