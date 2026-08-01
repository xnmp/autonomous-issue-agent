"""Single shell-compatible environment-file loader."""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Mapping

_ASSIGNMENT = re.compile(
    r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)=", re.MULTILINE
)
_ASSIGNMENT_SPACING = re.compile(
    r"^(\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$"
)


def _normalize_assignment_spacing(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        match = _ASSIGNMENT_SPACING.match(line)
        lines.append(
            f"{match.group(1)}={match.group(2)}" if match is not None else line
        )
    return "\n".join(lines) + "\n"


def source_env_file(
    path: str,
    inherited: Mapping[str, str],
    *,
    assigned_only: bool = False,
    normalize_spacing: bool = False,
    timeout_seconds: float = 30,
) -> dict[str, str]:
    try:
        raw = open(path, encoding="utf-8", errors="replace").read()
    except OSError as exc:
        raise ValueError(f"could not read environment file {path!r}: {exc}") from exc
    if normalize_spacing:
        raw = _normalize_assignment_spacing(raw)
    assigned = {match.group(1) for match in _ASSIGNMENT.finditer(raw)}
    if not assigned:
        raise ValueError(f"environment file {path!r} assigns no variables")
    try:
        process = subprocess.run(
            [
                "bash",
                "-c",
                'set -a; source "$1"; set +a; exec "$2" -0',
                "issue-agent-env",
                "/dev/stdin" if normalize_spacing else path,
                # env(1) resolved from OUR PATH: `inherited` may carry no
                # PATH, and bash's fallback path has no env on NixOS.
                shutil.which("env") or "env",
            ],
            # Same for bash itself (argv[0] stays "bash" for callers/tests
            # that key on it; executable= only overrides the binary).
            executable=shutil.which("bash"),
            env=dict(inherited),
            input=raw.encode() if normalize_spacing else None,
            check=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"could not source environment file {path!r}: {exc}") from exc
    result: dict[str, str] = {}
    for item in process.stdout.split(b"\0"):
        if not item or b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        name = key.decode(errors="replace")
        if not assigned_only or name in assigned:
            result[name] = value.decode(errors="replace")
    return result
