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
import selectors
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Callable

# Bounds (ADR 0012: every scan carries a ceiling). A pathological PATH must not
# turn `doctor` into a filesystem crawl, and each probe is a process spawn.
MAX_SCAN_DIRS = 64
MAX_VERSION_PROBES = 5
VERSION_TIMEOUT_SECONDS = 5.0
# `codex --version` is one short line. This bounds the probe AT THE PIPE, not
# after the fact: an adversarial or wedged binary that spews to stdout otherwise
# buffers into harness memory before anything truncates it (measured: 15.9GiB
# RSS before the timeout fired).
MAX_VERSION_OUTPUT = 4096

# Directories a daemon's PATH may lack but an operator's shell usually has (the
# keifu case), plus the NixOS system profile the minimal PATH *does* contain.
EXTRA_SEARCH_DIRS = ("~/.local/bin", "/run/current-system/sw/bin")

# A version has at least two dot-separated components, so a date banner
# ("codex 2026-08-02 nightly") is not mistaken for one. The lookbehind keeps
# 'codex-cli2 0.147.0' from reporting '2': a digit run glued to a name is part
# of the name. An optional leading 'v' accepts `v0.147.0`.
_VERSION_RE = re.compile(r"(?<![0-9A-Za-z.])v?(\d+(?:\.\d+)+)([0-9A-Za-z.\-+]*)")


@dataclass(frozen=True)
class CodexCandidate:
    """One distinct codex executable found on the search path.

    `path` is the STABLE path it was discovered at — the PATH entry, symlink
    intact. `real_path` is evidence only, and the key two discoveries are deduped
    on. Recommendations must never be a realpath: on NixOS that is
    /nix/store/<hash>/bin/codex, which the next garbage collection deletes, so
    pinning it writes the very failure mode this check exists to prevent into
    the config. `version` is '' when the binary would not report one."""

    path: str
    version: str
    real_path: str = ""


@dataclass(frozen=True)
class Finding:
    level: str  # "warn" | "error"
    message: str


@dataclass(frozen=True)
class CodexResolutionReport:
    codex_bin: str
    """CODEX_BIN as configured — what the daemon will hand to exec."""
    resolved_path: str
    """Where that name resolves here, as a STABLE path (symlink intact); '' if
    it resolves to nothing."""
    version: str
    pinned: bool
    """True when codex_bin is an absolute path, i.e. PATH cannot reinterpret it."""
    candidates: tuple[CodexCandidate, ...]
    findings: tuple[Finding, ...]
    recommended_bin: str
    """The path CODEX_BIN should hold. Never a realpath, and never a downgrade:
    when the effective binary's own version is unknown there is nothing to
    compare against, so it recommends staying put."""

    @property
    def healthy(self) -> bool:
        return not self.findings


def parse_codex_version(output: str) -> str:
    """The version out of `codex --version` ('codex-cli 0.147.0'), or '' when the
    output is not version-shaped. Tolerant by design: an unparseable version is
    reported as unknown, never as an exception on a doctor run."""
    head = (output or "")[:MAX_VERSION_OUTPUT]
    best = ""
    for line in head.splitlines():
        match = _VERSION_RE.search(line)
        if not match:
            continue
        found = (match.group(1) + match.group(2)).strip(".-+")
        # A line naming the tool is authoritative and ends the search; otherwise
        # the LAST plausible version wins, because banners and warnings print
        # before the version line, not after it.
        if "codex-cli" in line.lower():
            return found
        best = found
    return best


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

    here = version_key(version)
    newest = _newest(candidates)
    if resolved_path and here is None:
        # The effective binary's version is unknown (probe timed out, output was
        # garbage). Any parseable candidate would then compare as "newer" and the
        # recommendation would be an unwitting DOWNGRADE — pinning 0.100.0 over a
        # working 0.147.0 whose probe happened to time out. Unknown means no
        # switch: the error finding above is the whole verdict.
        recommended = resolved_path
        newest = None
    elif newest is not None:
        recommended = newest.path
    else:
        recommended = resolved_path

    if resolved_path and not pinned:
        findings.append(Finding(
            "warn",
            f"CODEX_BIN={codex_bin!r} is a bare name resolved through PATH. The "
            f"daemons under systemd/launchd get a MINIMAL PATH (no ~/.local/bin) "
            f"and may execute a DIFFERENT codex than this shell's "
            f"{resolved_path}. Pin it: CODEX_BIN={recommended or resolved_path}",
        ))

    if newest and resolved_path and newest.path != resolved_path:
        there = version_key(newest.version)
        if there is not None and here is not None and there > here:
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
        real = os.path.realpath(resolved)
        seen.add(real)
        candidates.append(
            CodexCandidate(resolved, parse_codex_version(ask(resolved)), real)
        )

    for path in _search_path_codexes(path_env, extra_dirs):
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        if len(candidates) >= MAX_VERSION_PROBES:
            break
        candidates.append(CodexCandidate(path, parse_codex_version(ask(path)), real))

    version = candidates[0].version if resolved and candidates else ""
    return analyse_codex_resolution(name, resolved, version, tuple(candidates))


def _resolve(codex_bin: str, path_env: str | None) -> str:
    """The path the name denotes, kept as the STABLE path exec would use. An
    absolute CODEX_BIN is taken as given — that is the whole point of pinning it
    — while a bare name goes through PATH exactly as exec would. Symlinks are
    deliberately NOT followed here: the resolved path is also what gets
    recommended back, and a /nix/store realpath is invalid after the next GC."""
    if os.path.isabs(codex_bin):
        return (
            codex_bin
            if os.path.isfile(codex_bin) and os.access(codex_bin, os.X_OK)
            else ""
        )
    return shutil.which(codex_bin, path=path_env) or ""


def _search_path_codexes(path_env: str | None, extra_dirs: tuple[str, ...]) -> list[str]:
    """`codex` executables across PATH plus the directories a daemon's PATH is
    most likely to be missing, as the STABLE paths they were found at.

    The extras come FIRST and the ceiling applies to PATH alone: appending them
    to a 64-entry PATH and then truncating would silently drop ~/.local/bin,
    which is the one directory the whole check exists to notice."""
    raw = os.environ.get("PATH", "") if path_env is None else path_env
    directories = [os.path.expanduser(d) for d in extra_dirs]
    directories.extend([d for d in raw.split(os.pathsep) if d][:MAX_SCAN_DIRS])

    found: list[str] = []
    seen_dirs: set[str] = set()
    for directory in directories:
        if directory in seen_dirs:
            continue
        seen_dirs.add(directory)
        candidate = os.path.join(directory, "codex")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            found.append(candidate)
    return found


def _probe_version(path: str) -> str:
    """`<path> --version`, bounded at the pipe and in its own process group.

    Every bound here is load-bearing against a binary that is not what it claims:
    stdout is read incrementally and abandoned at MAX_VERSION_OUTPUT rather than
    buffered whole, stdin is /dev/null so a probe can never eat the wizard's
    answers off the terminal, and the whole process GROUP is killed (the harness
    TEST_CMD standard) so a spawned grandchild cannot outlive the probe."""
    try:
        process = subprocess.Popen(
            [path, "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except (OSError, ValueError):
        return ""
    stream = process.stdout
    if stream is None:
        _kill_group(process)
        return ""
    chunks: list[bytes] = []
    total = 0
    deadline = time.monotonic() + VERSION_TIMEOUT_SECONDS
    try:
        os.set_blocking(stream.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(stream, selectors.EVENT_READ)
            while total < MAX_VERSION_OUTPUT:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    break
                data = stream.read(MAX_VERSION_OUTPUT - total)
                if not data:
                    break  # EOF, or a spurious readiness with nothing buffered
                chunks.append(data)
                total += len(data)
    except (OSError, ValueError):
        pass
    finally:
        _kill_group(process)
    return b"".join(chunks).decode("utf-8", errors="replace")


def _kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except OSError:
        pass
    if process.stdout is not None:
        try:
            process.stdout.close()
        except OSError:
            pass
    try:
        process.wait(timeout=VERSION_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def _newest(candidates: tuple[CodexCandidate, ...]) -> CodexCandidate | None:
    """The newest candidate whose version is actually KNOWN, or None. An
    unversionable binary is never a recommendation — it is a finding."""
    ranked = [(version_key(c.version), c) for c in candidates if version_key(c.version)]
    return max(ranked, key=lambda pair: pair[0])[1] if ranked else None
