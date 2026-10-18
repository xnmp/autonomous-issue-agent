"""Live settings shared by the CLI and dashboard (ADR 0007).

ADR 0007 amendment. Only `live` knobs are shown or written, so no secret is
ever read back or accepted. A write edits the one source file the operator
chose (the repo overlay, or shared.env for every deployment). The candidate
is composed in a scratch directory and validated with the daemons' own
parser. Repo saves refuse pending restart changes; shared saves leave those
deployments' running snapshots unchanged (`_vet`). An atomic source write
(refused over a concurrent hand edit)
recomposes each affected launched deployment with `bin/compose-env.sh`,
publishes the result to its installed service snapshot, and journals the
change. The running daemons pick it up at their next tick (`LiveConfig`).
"""

from __future__ import annotations

import getpass
import os
import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields

from ..adapters import config_files
from ..adapters.env_file import source_env_file
from ..domain.agent_task import RuntimeName
from ..domain.pricing import CLAUDE_ALIASES, CLAUDE_DEFAULT_MODEL_RATES, with_default_model_rates
from ..domain.routing import RUNTIME_EFFORTS, TIER_LADDER, ModelRoute, Tier
from ..domain.world import Pickup
from ..adapters.github import FIX_MAX_ITEMS
from .config import LIVE_KNOBS, HarnessConfig, from_env, restart_part

SCOPES = ("repo", "shared")


@dataclass(frozen=True)
class Spec:
    """How the dashboard presents one live knob."""

    key: str
    group: str  # routes | dispatch | caps
    kind: str  # route | model | int | usd | choice
    label: str
    help: str


_TIER_HELP = "Fix legs at this triage tier, and reviews of their heads. Blank: FIX_MODEL on the fixer runtime."
SPECS: tuple[Spec, ...] = (
    Spec("FIX_MODEL", "routes", "model", "Default fix model",
         "The fixer runtime's model when a tier has no route. Blank: the runtime's own default."),
    *(Spec(f"FIX_MODEL_{tier.name}", "routes", "route", f"Tier {tier.value}", _TIER_HELP) for tier in TIER_LADDER),
    Spec("TRIAGE_MODEL", "routes", "route", "Triage", "The TRIAGE leg. Blank: the low tier's route."),
    Spec("STEWARD_MODEL", "routes", "route", "Steward", "The STEWARD leg. Blank: the triage route."),
    Spec("REVIEW_MODEL", "routes", "model", "Default review model",
         "Reviews of a head whose tier has no route, on the reviewer runtime."),
    Spec("ISSUE_PICKUP", "dispatch", "choice", "Issue pickup",
         "ready: only issues marked ai-ready. open: every open issue except needs-human ones and the "
         "harness's own gap reports, so anyone who can open an issue can start paid legs; only for "
         "repositories with trusted issue authors and at most 1000 open issues."),
    Spec("FIX_DISPATCH_CONCURRENCY", "dispatch", "int", "Fix legs at once",
         "Slot-holding fix legs that may run concurrently (one per issue)."),
    Spec("ISSUE_SPEND_CAP_USD", "caps", "usd", "Per-issue cap (USD)", "Lifetime spend per issue. 0 disables."),
    Spec("DAILY_SPEND_CAP_USD", "caps", "usd", "Daily cap (USD)", "This repository's spend per UTC day. 0 disables."),
)
_SPECS = {spec.key: spec for spec in SPECS}
_FIELD = {env: name for name, env in LIVE_KNOBS.items()}


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return format(value, "g")
    return str(getattr(value, "value", value))


def _route_text(route: ModelRoute) -> str:
    return ":".join(part for part in (route.runtime.value, route.model or "(runtime default)",
                                      route.effort.value if route.effort else "") if part)


def _effective(cfg: HarnessConfig, key: str) -> str | None:
    """The route a leg configured by `key` runs on now, after fallbacks."""
    if key.startswith("FIX_MODEL_"):
        return _route_text(cfg.fix_route(Tier[key.removeprefix("FIX_MODEL_")]))
    routes = {"TRIAGE_MODEL": cfg.triage_route, "STEWARD_MODEL": cfg.steward_route,
              "REVIEW_MODEL": lambda: cfg.review_route(None)}
    if key == "FIX_MODEL":
        return f"{cfg.fixer_runtime.value}:{cfg.fix_model or '(runtime default)'}"
    return _route_text(routes[key]()) if key in routes else None


def _models(cfg: HarnessConfig) -> dict[str, list[str]]:
    operator = [row[0] for row in cfg.codex_cost_per_mtok]
    claude = [row[0] for row in CLAUDE_DEFAULT_MODEL_RATES] + [alias for alias, _ in CLAUDE_ALIASES]
    codex = [row[0] for row in with_default_model_rates(cfg.codex_cost_per_mtok)]
    return {RuntimeName.CLAUDE.value: sorted(set(claude + operator)), RuntimeName.CODEX.value: sorted(set(codex))}


@dataclass(frozen=True)
class Files:
    """One deployment's config sources and composed outputs."""

    harness_root: str
    name: str

    @property
    def shared(self) -> str:
        return os.path.join(self.harness_root, "config", "shared.env")

    @property
    def overlay(self) -> str:
        return os.path.join(self.harness_root, "config", "repos", f"{self.name}.env")

    @property
    def active(self) -> str:
        return os.path.join(self.harness_root, "config", f".active.{self.name}.env")

    def path(self, scope: str) -> str:
        return self.overlay if scope == "repo" else self.shared


def _source(files: Files, key: str) -> str:
    for scope in SCOPES:
        path = files.path(scope)
        if os.path.isfile(path) and config_files.assigns(config_files.read_text(path), key):
            return scope
    return "default"


def read_settings(cfg: HarnessConfig, files: Files) -> dict:
    """Every live knob's effective value (what the composed snapshot holds)
    and the file it comes from."""
    defaults = {item.name: item.default for item in fields(HarnessConfig)}
    rows = []
    for spec in SPECS:
        name = _FIELD[spec.key]
        rows.append({
            "key": spec.key, "group": spec.group, "kind": spec.kind, "label": spec.label, "help": spec.help,
            "value": _text(getattr(cfg, name)), "source": _source(files, spec.key),
            "default": _text(defaults[name]),
            "choices": [p.value for p in Pickup] if spec.kind == "choice" else None,
            "runtime": ({"FIX_MODEL": cfg.fixer_runtime, "REVIEW_MODEL": cfg.reviewer_runtime}[spec.key].value
                        if spec.kind == "model" else None),
            "effective": _effective(cfg, spec.key),
        })
    return {
        "settings": rows,
        "runtimes": [r.value for r in RuntimeName],
        "efforts": {r.value: [e.value for e in RUNTIME_EFFORTS[r]] for r in RuntimeName},
        "models": _models(cfg),
        "files": {"repo": files.overlay, "shared": files.shared},
        "warnings": [],
    }


@dataclass(frozen=True)
class WriteResult:
    ok: bool
    detail: str
    warnings: tuple[str, ...] = ()


def _vet(files: Files, target: str, after: str, key: str, value: str | None, scope: str,
         environ: Mapping[str, str], open_issues: Callable[[str], int | None], *, staging: str,
         home: str, edited: bool) -> tuple[str, str, bool]:
    """`(refusal, warning, held)` for one deployment if `target` held
    `after`; `held` leaves that deployment's composed files as they are. The
    candidate is composed with the real `compose-env.sh` in a scratch
    directory and parsed the daemons' way. Refused: an invalid result; a
    repo value that would not read back; a change to anything but live knobs
    in a launched deployment (a pending hand edit would ship with this save
    and restart its daemons, so it waits for the operator's own launch);
    `ISSUE_PICKUP=open` past the fix observation's item bound."""
    deployment = files.name
    try:
        shared, overlay = (after if path == target else config_files.read_text(path)
                           for path in (files.shared, files.overlay))
    except (OSError, ValueError) as exc:
        return f"refused, {deployment}: {exc}", "", False
    # The staged files hold the shared tokens: private and never left behind.
    try:
        scratch = tempfile.mkdtemp(prefix="issue-agent-settings-", dir=_staging_root(environ, staging))
    except OSError as exc:
        return f"refused, {deployment}: no private staging directory: {exc}", "", False
    try:
        staged, problem = config_files.compose_staged(files.harness_root, deployment, shared, overlay, environ,
                                                      scratch)
        if problem:
            return f"refused, {deployment}: {problem}", "", False
        running = (*((files.active,) if os.path.isfile(files.active) else ()),
                   *config_files.service_snapshots(deployment, home))
        env = config_files.sourced((staged,), environ)
        pending = _pending(staged, running, environ)
    except ValueError as exc:
        return f"refused, {deployment}: {exc}", "", False
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    cfg = from_env(env)
    if errors := cfg.validation_errors():
        return f"refused, {deployment} would be invalid: " + "; ".join(errors), "", False
    if pending and not edited:
        # #466: another deployment's pending edit (or a stopped run's stale
        # `.active`) holds only that deployment back; the shared file still saves.
        return "", (f"{deployment} has changes that need a restart ({', '.join(pending)}); "
                    f"it takes this save at its next `issue-agent start`"), True
    if pending:
        return (f"refused, {deployment}: saving would also apply changes that need a restart "
                f"({', '.join(pending)}); roll those out with `issue-agent start` first"), "", False
    if key == "ISSUE_PICKUP" and cfg.pickup is Pickup.OPEN:
        count = open_issues(deployment)
        if count is None:
            return f"refused, {deployment}: its open issues could not be counted", "", False
        if count >= FIX_MAX_ITEMS:
            return (f"refused, {deployment}: {count}+ open issues; the fix observation reads at most "
                    f"{FIX_MAX_ITEMS - 1}"), "", False
    if value is not None and env.get(key) != value:
        if scope == "repo":
            return f"{key} would read back as {env.get(key)!r}, not {value!r}", "", False
        return "", f"{deployment}: config/repos/{deployment}.env still sets {key}={env.get(key)!r}", False
    return "", "", False


def _staging_root(environ: Mapping[str, str], state_dir: str) -> str:
    """A private directory for staged config (it holds tokens): the user's
    runtime directory (tmpfs) when set and writable, else a 0700 directory
    in the deployment's state dir. Never TMPDIR or /tmp, which are disk on
    macOS and WSL."""
    runtime = environ.get("XDG_RUNTIME_DIR", "")
    if runtime and os.path.isdir(runtime) and os.access(runtime, os.W_OK | os.X_OK):
        return runtime
    root = os.path.join(state_dir, "settings-staging")
    os.makedirs(root, mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    return root


def _pending(staged: str, running: tuple[str, ...], environ: Mapping[str, str]) -> list[str]:
    """The restart-requiring names that differ between a candidate and each
    file a daemon runs from: `.active` (`issue-agent start`) and any installed
    service snapshot, which `publish` overwrites (none if never launched)."""
    names: set[str] = set()
    for path in running:
        for role in ("fix", "review"):
            new, old = restart_part(staged, environ, role), restart_part(path, environ, role)
            names |= {name for name in new.keys() | old.keys() if new.get(name) != old.get(name)}
    return sorted(names)


def read_setting(harness_root: str, name: str | None, key: str, environ: Mapping[str, str]) -> str:
    """Read a live default, or a named deployment's effective value; never a secret."""
    if key not in _SPECS:
        raise ValueError(f"{key!r} is not a live setting")
    if name is None:
        env = source_env_file(os.path.join(harness_root, "config", "shared.env"), environ, assigned_only=True)
        env.pop("AGENT_ENV", None)
    else:
        if name not in config_files.deployment_names(os.path.join(harness_root, "config", "repos")):
            raise ValueError(f"unknown deployment '{name}'")
        env = config_files.deployment_env(harness_root, name, environ)
    cfg = from_env(env)
    if errors := cfg.settings_errors():
        raise ValueError("invalid configuration: " + "; ".join(errors))
    return _text(getattr(cfg, _FIELD[key]))


def write_setting(harness_root: str, name: str | None, key: str, value: str | None, *,
                  environ: Mapping[str, str], open_issues: Callable[[str], int | None],
                  operator: str = "") -> WriteResult:
    """None selects shared defaults; a name selects exactly one repository overlay."""
    if key not in _SPECS:
        return WriteResult(False, f"{key!r} is not a live setting")
    try:
        with config_files.settings_lock(harness_root):
            names = config_files.deployment_names(os.path.join(harness_root, "config", "repos"))
            if name is not None and name not in names:
                return WriteResult(False, f"unknown deployment '{name}'")
            return _write_setting(harness_root, name, key, value, names=names,
                                  load=lambda deployment: from_env(config_files.deployment_env(
                                      harness_root, deployment, environ)), environ=environ,
                                  home=environ.get("HOME") or os.path.expanduser("~"),
                                  open_issues=open_issues, operator=operator)
    except (OSError, ValueError) as exc:
        return WriteResult(False, str(exc))


def _write_setting(
    harness_root: str,
    name: str | None,
    key: str,
    value: str | None,
    *,
    names: tuple[str, ...],
    load: Callable[[str], HarnessConfig],
    environ: Mapping[str, str],
    home: str,
    open_issues: Callable[[str], int | None],
    operator: str = "",
) -> WriteResult:
    scope = "shared" if name is None else "repo"
    target = (os.path.join(harness_root, "config", "shared.env") if name is None
              else Files(harness_root, name).overlay)
    try:
        before = config_files.read_text(target)
        after = config_files.set_assignment(before, key, value)
    except (OSError, ValueError) as exc:
        return WriteResult(False, str(exc))
    if name is None:
        scratch = tempfile.mkdtemp(prefix="issue-agent-settings-", dir=_staging_root(
            environ, os.path.join(home, ".local", "state", "issue-agent")))
        try:
            staged = os.path.join(scratch, "shared.env")
            with open(staged, "w", encoding="utf-8") as handle:
                handle.write(after)
            candidate = source_env_file(staged, environ, assigned_only=True)
            candidate.pop("AGENT_ENV", None)
            if errors := from_env(candidate).settings_errors():
                return WriteResult(False, "refused, shared defaults would be invalid: " + "; ".join(errors))
            if value is not None and candidate.get(key) != value:
                return WriteResult(False, f"{key} would not read back as {value!r}")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    affected = ((name,) if name is not None
                else tuple(n for n in names if os.path.isfile(Files(harness_root, n).active)))
    warnings: list[str] = []
    held: set[str] = set()
    for deployment in affected:
        refusal, warning, hold = _vet(Files(harness_root, deployment), target, after, key, value, scope, environ,
                                open_issues, staging=load(deployment).state_dir, home=home,
                                edited=deployment == name)
        if refusal:
            return WriteResult(False, refusal)
        if hold:
            held.add(deployment)
        warnings += [warning] if warning else []
    affected = tuple(n for n in affected if n not in held)
    old = {deployment: _text(getattr(load(deployment), _FIELD[key])) for deployment in affected}
    try:
        config_files.write_text(target, after, expected=before)
    except (OSError, ValueError) as exc:
        return WriteResult(False, f"{target} not written: {exc}")

    def apply(deployment: str) -> tuple[str, list[str]]:
        """Compose `deployment` and publish it to its service snapshots."""
        if problem := config_files.compose(harness_root, deployment, environ):
            return problem, []
        return "", config_files.publish(Files(harness_root, deployment).active,
                                        config_files.service_snapshots(deployment, home), environ)

    composed: list[str] = []
    for deployment in affected:
        if not os.path.isfile(Files(harness_root, deployment).active):
            continue  # never launched: nothing running reads a composed file
        problem, published = apply(deployment)
        if problem:
            return WriteResult(False, f"{deployment}: {problem}; " + _roll_back(target, before, after, composed, apply))
        composed.append(deployment)
        warnings += published
    who = operator or _operator()
    for deployment in affected:
        try:
            config_files.journal_change(load(deployment).state_dir, {
                "operator": who, "deployment": deployment, "key": key, "old": old.get(deployment, ""),
                "new": "" if value is None else value, "scope": scope, "file": target})
        except OSError as exc:
            warnings.append(f"{deployment}: setting applied, but its change journal could not be written: {exc}")
    shown = "(unset)" if value is None else repr(value)
    return WriteResult(True, f"{key}={shown} in {os.path.relpath(target, harness_root)}; "
                             f"{'applies at the next tick' if composed else 'applies at the next launch'}",
                       tuple(warnings))


def _roll_back(target: str, before: str, after: str, composed: list[str],
               apply: Callable[[str], tuple[str, list[str]]]) -> str:
    """Restore the source file, then recompose AND republish every deployment
    already composed: an installed service reads its snapshot, so a
    recomposed `.active` alone would leave it running the refused value."""
    try:
        config_files.write_text(target, before, expected=after)
    except (OSError, ValueError) as exc:
        return f"{target} NOT restored ({exc}); fix it by hand"
    stuck = [f"{done}: {problem or '; '.join(published)}" for done in composed
             for problem, published in [apply(done)] if problem or published]
    return f"{target} restored" + (f"; not re-applied: {', '.join(stuck)}" if stuck else "")


def _operator() -> str:
    try:
        return f"{getpass.getuser()} (dashboard)"
    except (OSError, KeyError):
        return "dashboard"
