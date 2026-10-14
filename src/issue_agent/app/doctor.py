"""Offline doctor checks over the composed env and the installed services.

`bin/doctor.sh` probes GitHub (each token's viewer login and write access).
These checks need no network: which credential each role resolves to, which
codex binary the daemons will exec, and whether the installed services run the
environment and snapshot this checkout composes. Every check returns findings
and never raises; nothing here prints token material.

Incidents: keifu 2026-08-02 (fixer on the ambient human credential; a bare
`codex` resolving to a provider-refused version under systemd's minimal PATH);
the 2026-08-10 relaunch (units without a baked PATH, tauri #650; installed
snapshots that re-composition never reaches).
"""

from __future__ import annotations

import os
import plistlib
import shlex
import shutil
from dataclasses import dataclass
from typing import Callable

from ..adapters.credentials import SECRET_NAMES, stored_names


@dataclass(frozen=True)
class Finding:
    level: str  # "info" | "warn" | "error"
    message: str


def _unquoted(value: str) -> str:
    try:
        parts = shlex.split(value)
    except ValueError:
        return value
    return parts[0] if len(parts) == 1 else value


_ROUTES = ("FIX_MODEL_LOW", "FIX_MODEL_MED", "FIX_MODEL_HIGH", "FIX_MODEL_XHIGH", "TRIAGE_MODEL",
           "STEWARD_MODEL")


def _runtimes(env: dict) -> set[str]:
    """The role runtimes plus every runtime a `runtime:model` route names."""
    roles = {_unquoted(str(env.get(k) or "").strip()).lower() for k in ("FIXER_RUNTIME", "REVIEWER_RUNTIME")}
    routes = {_unquoted(str(env.get(k) or "").strip()).partition(":")[0].lower() for k in _ROUTES
              if ":" in str(env.get(k) or "")}
    return (roles | routes) - {""}


def _runtime_binaries(env: dict) -> tuple[tuple[str, str], ...]:
    """(binary, level) the configured runtimes exec. claude is a warning unless
    pinned: the SDK falls back to its bundled binary."""
    runtimes = _runtimes(env)
    out: list[tuple[str, str]] = []
    codex = _unquoted(str(env.get("CODEX_BIN") or "").strip())
    if "codex" in runtimes or codex:
        out.append((codex or "codex", "error"))
    claude = _unquoted(str(env.get("CLAUDE_BIN") or "").strip())
    if "claude" in runtimes or claude:
        out.append((claude or "claude", "error" if claude else "warn"))
    return tuple(out)


def codex_findings(env: dict, which: Callable[..., str | None] = shutil.which) -> tuple[Finding, ...]:
    """A bare CODEX_BIN resolves through the daemons' PATH, which under
    systemd/launchd is minimal and may pick a different codex than this shell."""
    codex = _unquoted(str(env.get("CODEX_BIN") or "").strip()) or "codex"
    if not any(binary == codex for binary, _ in _runtime_binaries(env)):
        return ()
    resolved = (codex if os.path.isabs(codex) and os.access(codex, os.X_OK)
                else "" if os.path.isabs(codex) else which(codex) or "")
    if not resolved:
        return (Finding("error", f"codex binary {codex!r} does not resolve to an executable; "
                                 "every codex leg dies on spawn. Set CODEX_BIN to its absolute path."),)
    if not os.path.isabs(codex):
        return (Finding("warn", f"CODEX_BIN={codex!r} resolves through PATH (here: {resolved}); "
                                "services get a minimal PATH and may exec a different codex. "
                                f"Pin it: CODEX_BIN={resolved}"),)
    return ()


def secret_source_findings(env: dict, stored: tuple[str, ...]) -> tuple[Finding, ...]:
    """Where each secret comes from: a systemd credential (an encrypted file
    on this host, delivered by the service units) or the env file. Both is an
    error: two sources of truth for one secret. Values are never shown."""
    findings: list[Finding] = []
    for name in SECRET_NAMES:
        inline = bool(str(env.get(name) or "").strip())
        if name in stored and inline:
            findings.append(Finding("error", f"{name} is set in both a systemd credential and the env file; "
                                             "delete the plaintext line."))
        elif name in stored:
            findings.append(Finding("info", f"{name}: systemd credential"))
        elif inline:
            findings.append(Finding("info", f"{name}: env"))
    return tuple(findings)


def identity_findings(env: dict, stored: tuple[str, ...] = ()) -> tuple[Finding, ...]:
    """The two-identity model: the fixer and the reviewer each on their own
    credential (a GitHub App or REVIEW_BOT_TOKEN for review). `stored` names
    secrets held as systemd credentials: present, though not in `env`."""
    findings: list[Finding] = []
    fix = str(env.get("FIX_BOT_TOKEN") or "")
    review = str(env.get("REVIEW_BOT_TOKEN") or "")
    has_fix = bool(fix) or "FIX_BOT_TOKEN" in stored
    has_review = bool(review) or "REVIEW_BOT_TOKEN" in stored
    app = [str(env.get(k) or "") for k in ("GH_APP_ID", "GH_APP_INSTALL_ID", "GH_APP_KEY_FILE")]
    for name, value in (("FIX_BOT_TOKEN", fix), ("REVIEW_BOT_TOKEN", review)):
        if value and not value.strip():
            findings.append(Finding("error", f"{name} is whitespace: it is sent as a bearer token "
                                             "and GitHub 401s every call. Unset it or set a real token."))
    if not has_fix:
        findings.append(Finding("error", "FIX_BOT_TOKEN is not set: fix legs run sandboxed with no GitHub "
                                         "credential and cannot push or read CI logs (#413)."))
    if any(app) and not all(app):
        findings.append(Finding("error", "GH_APP_ID, GH_APP_INSTALL_ID and GH_APP_KEY_FILE must be set together."))
    uses_app = all(app)
    if uses_app:
        key = app[2]
        home = str(env.get("HOME") or "")
        key = home + key[1:] if home and key.startswith("~") else os.path.expanduser(key)
        if not os.access(key, os.R_OK):
            findings.append(Finding("error", f"GH_APP_KEY_FILE {app[2]!r} is not readable: "
                                             "the reviewer cannot mint its token."))
    elif (review and review == fix) or (not has_review and not has_fix):
        findings.append(Finding("error", "the fixer and the reviewer resolve to the same credential: "
                                         "GitHub 422s a review by the PR's author, so nothing can merge. "
                                         "Give the reviewer its own token or a GitHub App."))
    return tuple(findings)


_ROLES = ("fix-watcher", "review-watcher")
_TRANSIENT = f"{os.sep}.claude{os.sep}worktrees{os.sep}"


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            return stream.read()
    except OSError:
        return None


def parse_unit(path: str) -> tuple[dict[str, str], str | None] | None:
    """(Environment, WorkingDirectory) of a systemd unit or launchd plist."""
    try:
        if path.endswith(".plist"):
            with open(path, "rb") as stream:
                data = plistlib.loads(stream.read())
            if not isinstance(data, dict):
                return None
            env, working = data.get("EnvironmentVariables"), data.get("WorkingDirectory")
            return ({str(k): str(v) for k, v in (env if isinstance(env, dict) else {}).items()},
                    str(working) if working is not None else None)
        text = _read(path)
        if text is None:
            return None
        env, working = {}, None
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("WorkingDirectory="):
                working = line.partition("=")[2].strip() or None
            elif line.startswith("Environment="):
                rest = line.partition("=")[2].strip()
                if not rest:
                    env.clear()  # a bare `Environment=` resets
                for assignment in shlex.split(rest):
                    key, sep, value = assignment.partition("=")
                    if sep and key:
                        env[key] = value
        return env, working
    except Exception:  # noqa: BLE001 — a malformed unit is itself the finding
        return None


def env_assignments(text: str) -> dict[str, str]:
    """KEY → raw value of a snapshot, textually (never sourced)."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip().removeprefix("export ")
        key, sep, value = line.partition("=")
        if sep and key and not line.startswith("#"):
            out[key.strip()] = value
    return out


def service_findings(
    name: str, active_env_path: str, *, systemd_dir: str = "~/.config/systemd/user",
    launchd_dir: str = "~/Library/LaunchAgents", which: Callable[..., str | None] = shutil.which,
) -> tuple[Finding, ...]:
    """The installed units' baked PATH, snapshot and checkout. No units is not
    a finding: foreground `issue-agent start` is supported."""
    units: list[str] = []
    for directory, suffix in ((systemd_dir, f"-{name}.service"), (launchd_dir, f".{name}.plist")):
        try:
            entries = sorted(os.listdir(os.path.expanduser(directory)))
        except OSError:
            continue
        units += [os.path.join(os.path.expanduser(directory), e) for e in entries
                  if e.startswith("issue-agent") and e.endswith(suffix)]
    if not units:
        return ()
    findings: list[Finding] = []
    missing = [r for r in _ROLES if not any(os.path.basename(u).startswith(f"issue-agent-{r}") for u in units)]
    if missing:
        findings.append(Finding("error", f"installed services lack the {', '.join(missing)} role(s) "
                                         f"for '{name}'. Re-run `issue-agent install-service`."))
    reinstall = "Re-run `issue-agent install-service`."
    drift_checked, unreadable = False, set[str]()
    for unit in units:
        base = os.path.basename(unit)
        parsed = parse_unit(unit)
        if parsed is None:
            findings.append(Finding("warn", f"{base}: unreadable or malformed."))
            continue
        env, working = parsed
        if working and (_TRANSIENT in working + os.sep or not os.path.isdir(working)):
            findings.append(Finding("error", f"{base}: WorkingDirectory {working[:160]} is a transient "
                                             f"or missing checkout. {reinstall}"))
        path, snapshot = env.get("PATH"), env.get("AGENT_ENV")
        if path is None:
            findings.append(Finding("error", f"{base}: no baked PATH; the service runs the supervisor's "
                                             f"minimal PATH (#650). {reinstall}"))
        if snapshot is None:
            findings.append(Finding("error", f"{base}: no AGENT_ENV; the service boots without config. {reinstall}"))
            continue
        installed = _read(snapshot)
        if installed is None:
            if snapshot in unreadable:
                continue
            unreadable.add(snapshot)
            findings.append(Finding("error", f"{base}: installed snapshot {snapshot[:160]} is unreadable; "
                                             f"the service cannot boot. {reinstall}"))
            continue
        if path is not None:
            service_env = env_assignments(installed)
            for binary, level in (("git", "error"), ("gh", "error"), *_runtime_binaries(service_env)):
                if not which(binary, path=path):
                    findings.append(Finding(level, f"{base}: '{binary}' does not resolve under the service's "
                                                   f"PATH; dispatch will fail. {reinstall}"))
        composed = _read(active_env_path)
        if drift_checked or composed is None:
            continue
        drift_checked = True
        old, new = env_assignments(installed), env_assignments(composed)
        drifted = sorted(k for k in old.keys() | new.keys() if old.get(k) != new.get(k))
        if drifted:  # key names only: values carry tokens
            more = f" (+{len(drifted) - 5} more)" if len(drifted) > 5 else ""
            findings.append(Finding("warn", f"installed config differs from the composed snapshot on "
                                            f"{', '.join(drifted[:5])}{more}; services keep the old values. "
                                            f"{reinstall}"))
    return tuple(findings)


def sandbox_findings(probe: Callable[[], str | None] | None = None) -> tuple[Finding, ...]:
    """Whether paid legs can run: every leg runs under bubblewrap, and with
    none usable they are refused (no unsandboxed fallback, #413)."""
    if probe is None:
        from ..adapters.leg_sandbox import LegSandbox

        probe = LegSandbox(home=os.path.expanduser("~"), repository=os.getcwd(), writable=False).preflight
    reason = probe()
    if reason is not None:
        return (Finding("error", f"every paid leg is refused: {reason}"),)
    return (Finding("info", "every leg runs under bubblewrap: private HOME, no harness checkout or "
                            "state dirs, its checkout plus toolchains; network on"),)


def doctor_findings(name: str, composed: dict, active_env_path: str) -> tuple[tuple[str, tuple[Finding, ...]], ...]:
    """The table of offline checks `issue-agent doctor` renders."""
    return (
        ("leg sandbox", sandbox_findings()),
        ("secrets", secret_source_findings(composed, stored_names(os.path.expanduser("~")))),
        ("identity", identity_findings(composed, stored_names(os.path.expanduser("~")))),
        ("codex binary", codex_findings(composed)),
        ("installed services", service_findings(name, active_env_path)),
    )
