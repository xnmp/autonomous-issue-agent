"""Which `codex` binary will the DAEMONS actually execute?

2026-08-02 (keifu): the daemons moved from a foreground shell to systemd user
units. systemd's minimal PATH has no ~/.local/bin, so a bare `codex` resolved to
the system package 0.142.3 instead of the operator's 0.147.0 — OpenAI's backend
400s clients that old ("requires a newer version of Codex"), every codex leg died
on spawn, and the retry ladders escalated several issues to needs-human-review.
The wizard reported "codex: on PATH" throughout, because `shutil.which` answered
for the OPERATOR'S shell, which is not the context that matters.

So resolution is reported as evidence, not as a boolean: the effective path, its
version, and every other codex on the search path with its version. There are no
hardcoded version floors here — the harness cannot know what the provider will
reject next. Only DISCOVERED versions are compared, and the actionable finding is
always the same one: pin CODEX_BIN to an absolute path so the daemon's PATH
cannot choose for you.

The analysis is a pure function over discovered candidates; only
`codex_resolution_report` touches PATH or spawns anything.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable

# Bounds (ADR 0012: every scan carries a ceiling). A pathological PATH must not
# turn `doctor` into a filesystem crawl, and each probe is a process spawn.
MAX_SCAN_DIRS = 64
MAX_VERSION_PROBES = 5
VERSION_TIMEOUT_SECONDS = 5.0
# `codex --version` is one short line; anything past this is not version output
# and parsing it is only a way to burn time on a hostile or wedged binary.
MAX_VERSION_OUTPUT_CHARS = 4096

# Directories a daemon's PATH may lack but an operator's shell usually has (the
# keifu case), plus the NixOS system profile the minimal PATH *does* contain.
EXTRA_SEARCH_DIRS = ("~/.local/bin", "/run/current-system/sw/bin")

# The lookbehind keeps 'codex-cli2 0.147.0' from reporting version '2': a digit
# run glued to a name is part of the name, not the version.
_VERSION_RE = re.compile(r"(?<![0-9A-Za-z.])(\d+(?:\.\d+)*)([0-9A-Za-z.\-+]*)")


@dataclass(frozen=True)
class CodexCandidate:
    """One distinct codex executable found on the search path. `version` is ''
    when the binary would not report one."""

    path: str
    version: str


@dataclass(frozen=True)
class Finding:
    level: str  # "warn" | "error"
    message: str


@dataclass(frozen=True)
class CodexResolutionReport:
    codex_bin: str
    """CODEX_BIN as configured — what the daemon will hand to exec."""
    resolved_path: str
    """The executable that name resolves to here, symlinks followed; '' if none."""
    version: str
    pinned: bool
    """True when codex_bin is an absolute path, i.e. PATH cannot reinterpret it."""
    candidates: tuple[CodexCandidate, ...]
    findings: tuple[Finding, ...]
    recommended_bin: str
    """Absolute path of the newest codex found — what CODEX_BIN should be."""

    @property
    def healthy(self) -> bool:
        return not self.findings

    @property
    def summary(self) -> str:
        if not self.resolved_path:
            return f"{self.codex_bin} (unresolved)"
        return f"{self.resolved_path} ({self.version or 'version unknown'})"


def parse_codex_version(output: str) -> str:
    """The version out of `codex --version` ('codex-cli 0.147.0'), or '' when the
    output is not version-shaped. Tolerant by design: an unparseable version is
    reported as unknown, never as an exception on a doctor run."""
    head = (output or "")[:MAX_VERSION_OUTPUT_CHARS]
    for line in head.splitlines():
        match = _VERSION_RE.search(line)
        if match:
            return (match.group(1) + match.group(2)).strip(".-+")
    return ""


def version_key(version: str) -> tuple | None:
    """Sort key for two DISCOVERED versions, or None when incomparable.

    The numeric core dominates: 0.147.0-alpha.4 is newer than 0.146.0, because
    the provider gates on the core it shipped. Within one core a release beats
    its own prereleases, and prereleases order by their trailing numbers."""
    core_text = version.strip()
    if not core_text:
        return None
    match = _VERSION_RE.match(core_text)
    if not match:
        return None
    core = tuple(int(part) for part in match.group(1).split("."))
    suffix = match.group(2)
    is_release = 0 if suffix else 1
    suffix_nums = tuple(int(n) for n in re.findall(r"\d+", suffix))
    return (core, is_release, suffix_nums)


def analyse_codex_resolution(
    codex_bin: str,
    resolved_path: str,
    version: str,
    candidates: tuple[CodexCandidate, ...],
) -> CodexResolutionReport:
    """Pure: findings implied by an already-discovered resolution."""
    pinned = bool(codex_bin) and os.path.isabs(codex_bin)
    findings: list[Finding] = []

    if not resolved_path:
        findings.append(Finding(
            "error",
            f"codex binary {codex_bin!r} does not resolve to an executable here. "
            "Every codex leg dies on spawn. Install it and set CODEX_BIN to its "
            "absolute path.",
        ))
    elif not version:
        findings.append(Finding(
            "error",
            f"{resolved_path} would not report a version (`{resolved_path} "
            "--version` failed or printed nothing) — it may be broken, wedged, or "
            "not codex at all.",
        ))

    newest = _newest(candidates)
    recommended = newest.path if newest else resolved_path

    if resolved_path and not pinned:
        findings.append(Finding(
            "warn",
            f"CODEX_BIN={codex_bin!r} is a bare name resolved through PATH. The "
            f"daemons under systemd/launchd get a MINIMAL PATH (no ~/.local/bin) "
            f"and may execute a DIFFERENT codex than this shell's "
            f"{resolved_path}. Pin it: CODEX_BIN={recommended or resolved_path}",
        ))

    if newest and resolved_path and newest.path != resolved_path:
        here = version_key(version)
        there = version_key(newest.version)
        if there is not None and (here is None or there > here):
            findings.append(Finding(
                "warn",
                f"a newer codex exists elsewhere: {newest.path} is "
                f"{newest.version}, but {codex_bin!r} resolves to {resolved_path} "
                f"({version or 'version unknown'}). The provider 400s clients it "
                f"considers too old, and that failure looks like an infra death on "
                f"every leg. Set: CODEX_BIN={newest.path}",
            ))

    return CodexResolutionReport(
        codex_bin=codex_bin,
        resolved_path=resolved_path,
        version=version,
        pinned=pinned,
        candidates=candidates,
        findings=tuple(findings),
        recommended_bin=recommended,
    )


def codex_resolution_report(
    codex_bin: str,
    *,
    probe: Callable[[str], str] | None = None,
    path_env: str | None = None,
    extra_dirs: tuple[str, ...] = EXTRA_SEARCH_DIRS,
) -> CodexResolutionReport:
    """Resolve `codex_bin`, version it, scan the search path for shadowing
    installs, and analyse the result. `probe` returns a binary's raw
    `--version` output (injected in tests; defaults to spawning it)."""
    name = codex_bin or "codex"
    ask = probe if probe is not None else _probe_version
    resolved = _resolve(name, path_env)

    seen: set[str] = set()
    candidates: list[CodexCandidate] = []
    if resolved:
        seen.add(resolved)
        candidates.append(CodexCandidate(resolved, parse_codex_version(ask(resolved))))

    for path in _search_path_codexes(path_env, extra_dirs):
        if path in seen:
            continue
        seen.add(path)
        if len(candidates) >= MAX_VERSION_PROBES:
            break
        candidates.append(CodexCandidate(path, parse_codex_version(ask(path))))

    version = candidates[0].version if resolved and candidates else ""
    return analyse_codex_resolution(name, resolved, version, tuple(candidates))


def _resolve(codex_bin: str, path_env: str | None) -> str:
    """The executable the name denotes, symlinks followed. An absolute CODEX_BIN
    is taken as given — that is the whole point of pinning it — while a bare name
    goes through PATH exactly as exec would."""
    if os.path.isabs(codex_bin):
        return (
            os.path.realpath(codex_bin)
            if os.path.isfile(codex_bin) and os.access(codex_bin, os.X_OK)
            else ""
        )
    found = shutil.which(codex_bin, path=path_env)
    return os.path.realpath(found) if found else ""


def _search_path_codexes(path_env: str | None, extra_dirs: tuple[str, ...]) -> list[str]:
    """Distinct `codex` executables across PATH plus the directories a daemon's
    PATH is most likely to be missing. Deduped by realpath, so a symlink farm
    pointing at one install is one candidate."""
    raw = os.environ.get("PATH", "") if path_env is None else path_env
    directories = [d for d in raw.split(os.pathsep) if d]
    directories.extend(os.path.expanduser(d) for d in extra_dirs)

    found: list[str] = []
    seen_dirs: set[str] = set()
    seen: set[str] = set()
    for directory in directories[:MAX_SCAN_DIRS]:
        if directory in seen_dirs:
            continue
        seen_dirs.add(directory)
        candidate = os.path.join(directory, "codex")
        if not (os.path.isfile(candidate) and os.access(candidate, os.X_OK)):
            continue
        real = os.path.realpath(candidate)
        if real in seen:
            continue
        seen.add(real)
        found.append(real)
    return found


def _probe_version(path: str) -> str:
    try:
        completed = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=VERSION_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (completed.stdout or completed.stderr or "")[:MAX_VERSION_OUTPUT_CHARS]


def _newest(candidates: tuple[CodexCandidate, ...]) -> CodexCandidate | None:
    ranked = [(version_key(c.version), c) for c in candidates]
    comparable = [(key, c) for key, c in ranked if key is not None]
    if not comparable:
        return candidates[0] if candidates else None
    return max(comparable, key=lambda pair: pair[0])[1]
