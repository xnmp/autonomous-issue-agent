"""Doctor checks against the INSTALLED service definitions.

Interactive doctor validates the operator's shell environment, but installed
services run with the unit's baked env — a different PATH and a config
snapshot copied at install time. The 2026-08-10 relaunch hit both halves of
that gap: units installed before PATH baking ran systemd's minimal PATH, so
codex resolved interactively but not for the daemons (tauri #650 burned its
resolve budget on "codex not found"), and re-composed config never reaches
running services until `install-service` is re-run. These checks read what is
actually installed — required binaries come from the INSTALLED snapshot when
it is readable, because that is the config the services really run — so
doctor's verdict is about the daemons' environment, not this shell's.

Every check degrades to a finding, never an exception: a malformed unit is
itself a red flag worth reporting, and doctor must keep rendering the rest.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import shutil
from dataclasses import dataclass

from .codex_resolution import Finding
from .config import fix_runtime_names_from_env

RUNTIME_KEYS = ("FIXER_RUNTIME", "REVIEWER_RUNTIME")
# Roles shell out to these regardless of runtime; a service PATH that cannot
# resolve them fails at dispatch time with the same burned-budget signature
# as #650. uv is exempt: ExecStart bakes @UV_BIN@ as an absolute path.
_ALWAYS_REQUIRED = ("git", "gh")
_EXPECTED_ROLES = ("fix-watcher", "review-watcher")
_SYSTEMD_USER_DIR = "~/.config/systemd/user"
_LAUNCHD_DIR = "~/Library/LaunchAgents"
_TRANSIENT_MARKER = f"{os.sep}.claude{os.sep}worktrees{os.sep}"
_MAX_DRIFT_KEYS_SHOWN = 5
_MAX_PATH_SHOWN = 160


@dataclass(frozen=True)
class InstalledUnit:
    path: str
    """The unit/plist file this was parsed from."""
    baked_path: str | None
    """The PATH the service runs with; None when the unit bakes none."""
    agent_env: str | None
    """The installed config snapshot the service reads; None when unset."""
    working_dir: str | None = None
    """The checkout the service executes from; None when unset."""


def parse_systemd_unit(text: str) -> InstalledUnit:
    """Environment/WorkingDirectory out of a unit file.

    Handles the forms our installer writes plus systemd's documented ones:
    multiple (optionally quoted) assignments per `Environment=` line, and a
    bare `Environment=` reset clearing everything set so far. Anything more
    exotic (specifier expansion, UnsetEnvironment=) is out of scope — the
    installer never emits it, and a hand-edited unit that uses it will show
    up as the absence finding, not a silent wrong verdict."""
    env: dict[str, str] = {}
    working_dir: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("WorkingDirectory="):
            working_dir = line[len("WorkingDirectory="):].strip() or None
        if not line.startswith("Environment="):
            continue
        rest = line[len("Environment="):].strip()
        if not rest:
            env.clear()
            continue
        try:
            assignments = shlex.split(rest)
        except ValueError:
            assignments = [rest]
        for assignment in assignments:
            key, sep, value = assignment.partition("=")
            if sep and key:
                env[key] = value
    return InstalledUnit(
        path="",
        baked_path=env.get("PATH"),
        agent_env=env.get("AGENT_ENV"),
        working_dir=working_dir,
    )


def parse_launchd_plist(payload: bytes) -> InstalledUnit:
    data = plistlib.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("plist root is not a dict")
    env = data.get("EnvironmentVariables")
    env = env if isinstance(env, dict) else {}
    working = data.get("WorkingDirectory")
    baked = env.get("PATH")
    agent_env = env.get("AGENT_ENV")
    return InstalledUnit(
        path="",
        baked_path=str(baked) if baked is not None else None,
        agent_env=str(agent_env) if agent_env is not None else None,
        working_dir=str(working) if working is not None else None,
    )


def env_file_assignments(text: str) -> dict[str, str]:
    """KEY → raw value line of a composed snapshot; comments/blanks skipped.

    Textual on purpose: the drift comparison only needs "same or different",
    and sourcing the installed copy through a shell to find that out would
    execute a file the operator may not have written."""
    assignments: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, sep, value = line.partition("=")
        if sep and key and not key.strip().startswith("#"):
            assignments[key.strip()] = value
    return assignments


def _unquoted(value: str) -> str:
    """A %q-escaped snapshot value as the daemons would receive it."""
    try:
        parts = shlex.split(value)
    except ValueError:
        return value
    return parts[0] if len(parts) == 1 else value


def snapshot_drift_keys(installed_text: str, composed_text: str) -> tuple[str, ...]:
    """Key NAMES that differ between the installed and composed snapshots.

    Names only — snapshot values carry tokens and must never reach doctor
    output."""
    installed = env_file_assignments(installed_text)
    composed = env_file_assignments(composed_text)
    return tuple(sorted(
        key
        for key in installed.keys() | composed.keys()
        if installed.get(key) != composed.get(key)
    ))


def _selected_runtime_binaries(env: dict) -> tuple[tuple[str, str], ...]:
    """(binary, level) pairs the given config's runtimes will exec.

    codex is an error when missing — there is no bundled fallback. claude is
    a warning unless CLAUDE_BIN pins it: the SDK can fall back to its bundled
    binary, so absence from PATH is suspicious but not fatal."""
    runtimes = {
        _unquoted((env.get(key) or "").strip()).lower() for key in RUNTIME_KEYS
    } | fix_runtime_names_from_env(env)
    binaries: list[tuple[str, str]] = []
    codex_pin = _unquoted((env.get("CODEX_BIN") or "").strip())
    if "codex" in runtimes or codex_pin:
        binaries.append((codex_pin or "codex", "error"))
    claude_pin = _unquoted((env.get("CLAUDE_BIN") or "").strip())
    if "claude" in runtimes or claude_pin:
        binaries.append((claude_pin or "claude", "error" if claude_pin else "warn"))
    return tuple(binaries)


def discover_installed_units(
    name: str,
    *,
    systemd_dir: str | None = None,
    launchd_dir: str | None = None,
) -> tuple[str, ...]:
    unit_dir = os.path.expanduser(systemd_dir or _SYSTEMD_USER_DIR)
    plist_dir = os.path.expanduser(launchd_dir or _LAUNCHD_DIR)
    found: list[str] = []
    for directory, suffix in ((unit_dir, f"-{name}.service"),
                              (plist_dir, f".{name}.plist")):
        try:
            entries = sorted(os.listdir(directory))
        except OSError:
            continue
        found += [
            os.path.join(directory, entry)
            for entry in entries
            if entry.startswith("issue-agent") and entry.endswith(suffix)
        ]
    return tuple(found)


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            return stream.read()
    except OSError:
        return None


def _parse_unit(path: str) -> InstalledUnit | None:
    if path.endswith(".plist"):
        try:
            with open(path, "rb") as stream:
                unit = parse_launchd_plist(stream.read())
        except Exception:  # noqa: BLE001 — any parse failure is one finding
            return None
    else:
        text = _read(path)
        if text is None:
            return None
        try:
            unit = parse_systemd_unit(text)
        except Exception:  # noqa: BLE001
            return None
    return InstalledUnit(
        path=path,
        baked_path=unit.baked_path,
        agent_env=unit.agent_env,
        working_dir=unit.working_dir,
    )


def _missing_roles(unit_paths: tuple[str, ...], name: str) -> tuple[str, ...]:
    present = {
        role
        for path in unit_paths
        for role in _EXPECTED_ROLES
        if os.path.basename(path).startswith(f"issue-agent-{role}")
    }
    return tuple(role for role in _EXPECTED_ROLES if role not in present)


def installed_service_findings(
    name: str,
    composed: dict,
    active_env_path: str,
    *,
    systemd_dir: str | None = None,
    launchd_dir: str | None = None,
    which=shutil.which,
) -> tuple[Finding, ...]:
    """Findings about the services actually installed for this deployment.

    No installed services is not a finding — foreground `issue-agent start`
    is a supported mode. A PARTIAL trio is: one surviving unit means the
    other roles silently do not run."""
    findings: list[Finding] = []
    unit_paths = discover_installed_units(
        name, systemd_dir=systemd_dir, launchd_dir=launchd_dir
    )
    if not unit_paths:
        return ()
    missing_roles = _missing_roles(unit_paths, name)
    if missing_roles:
        findings.append(Finding(
            "error",
            f"installed services are missing the {', '.join(missing_roles)} "
            f"role(s) for '{name}' — the deployment runs partially. Re-run "
            "`issue-agent install-service`."))
    snapshots_checked: set[str] = set()
    for path in unit_paths:
        unit = _parse_unit(path)
        basename = os.path.basename(path)
        if unit is None:
            findings.append(Finding(
                "warn", f"{basename}: unreadable/malformed — could not verify "
                        "the service environment"))
            continue
        if unit.working_dir and (
            _TRANSIENT_MARKER in unit.working_dir + os.sep
            or not os.path.isdir(unit.working_dir)
        ):
            findings.append(Finding(
                "error",
                f"{basename}: WorkingDirectory "
                f"{unit.working_dir[:_MAX_PATH_SHOWN]} is a transient or "
                "missing checkout — the service runs the wrong (or no) code. "
                "Re-run `issue-agent install-service` from the canonical "
                "checkout."))
        if unit.baked_path is None:
            # The #650 class verbatim: no baked PATH means the supervisor's
            # minimal default, where interactive-only binaries do not resolve.
            findings.append(Finding(
                "error",
                f"{basename}: no PATH baked into the unit — the service runs "
                "with the supervisor's minimal PATH and dispatch dies on "
                "binaries your shell resolves (#650). Re-run `issue-agent "
                "install-service`."))
        if unit.agent_env is None:
            findings.append(Finding(
                "error",
                f"{basename}: no AGENT_ENV baked into the unit — the service "
                "boots without its config snapshot. Re-run `issue-agent "
                "install-service`."))
            continue
        first_sight = unit.agent_env not in snapshots_checked
        installed_text = _read(unit.agent_env)
        if installed_text is None:
            if first_sight:
                snapshots_checked.add(unit.agent_env)
                findings.append(Finding(
                    "error",
                    f"{basename}: installed config snapshot "
                    f"{unit.agent_env[:_MAX_PATH_SHOWN]} is missing/unreadable "
                    "— the service cannot boot. Re-run `issue-agent "
                    "install-service`."))
            continue
        # Binary requirements come from the config the SERVICE reads — the
        # installed snapshot — not the freshly composed one: after an operator
        # edits the overlay, the running services still exec the old runtime
        # (adversarial review 2026-08-10 F4).
        service_env: dict = env_file_assignments(installed_text)
        if unit.baked_path is not None:
            required = tuple(
                (binary, "error") for binary in _ALWAYS_REQUIRED
            ) + _selected_runtime_binaries(service_env or composed)
            for binary, level in required:
                if not which(binary, path=unit.baked_path):
                    findings.append(Finding(
                        level,
                        f"{basename}: '{binary}' does not resolve under the "
                        "service's baked PATH — the daemon will fail at "
                        "dispatch, not at start. Re-run `issue-agent "
                        "install-service` from a shell where it resolves, "
                        "or pin an absolute path in the overlay."))
        if not first_sight:
            continue
        snapshots_checked.add(unit.agent_env)
        composed_text = _read(active_env_path)
        if composed_text is None:
            continue
        drifted = snapshot_drift_keys(installed_text, composed_text)
        if drifted:
            shown = ", ".join(drifted[:_MAX_DRIFT_KEYS_SHOWN])
            more = len(drifted) - _MAX_DRIFT_KEYS_SHOWN
            suffix = f" (+{more} more)" if more > 0 else ""
            findings.append(Finding(
                "warn",
                f"installed service config "
                f"{unit.agent_env[:_MAX_PATH_SHOWN]} differs from the freshly "
                f"composed snapshot on: {shown}{suffix} — running services "
                "keep the OLD values until you re-run `issue-agent "
                "install-service`."))
    return tuple(findings)
