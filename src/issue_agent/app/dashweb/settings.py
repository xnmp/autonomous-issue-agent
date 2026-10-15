"""Dashboard settings: the live knobs, read and written per deployment.

ADR 0007 amendment. Only `live` knobs are shown or written, so no secret is
ever read back or accepted. A write edits the one source file the operator
chose (the repo overlay, or shared.env for every deployment), validates the
whole composed candidate with the daemons' own parser before an atomic write,
recomposes each affected launched deployment with `bin/compose-env.sh`,
publishes the result to its installed service snapshot, and journals the
change. The running daemons pick it up at their next tick (`LiveConfig`).
"""

from __future__ import annotations

import getpass
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields

from ...adapters import config_files
from ...domain.agent_task import RuntimeName
from ...domain.pricing import CLAUDE_ALIASES, CLAUDE_DEFAULT_MODEL_RATES, with_default_model_rates
from ...domain.routing import RUNTIME_EFFORTS, TIER_LADDER, ModelRoute, Tier
from ...domain.world import Pickup
from ..config import LIVE_KNOBS, HarnessConfig, from_env

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


def _candidate_errors(files: Files, texts: Mapping[str, str], environ: Mapping[str, str]) -> tuple[dict, list[str]]:
    """The composed candidate env and its validation errors, the source files
    replaced by `texts` (path -> content) and sourced like compose-env.sh."""
    with tempfile.TemporaryDirectory(prefix="issue-agent-settings-") as scratch:
        paths = []
        for index, path in enumerate((files.shared, files.overlay)):
            staged = os.path.join(scratch, f"{index}.env")
            with open(staged, "w", encoding="utf-8") as handle:
                handle.write(texts[path] if path in texts else config_files.read_text(path))
            paths.append(staged)
        try:
            env = config_files.sourced(tuple(paths), environ)
        except ValueError as exc:
            return {}, [str(exc)]
    return env, list(from_env(env).validation_errors())


def write_setting(
    harness_root: str,
    name: str,
    key: str,
    value: str | None,
    scope: str,
    *,
    names: tuple[str, ...],
    load: Callable[[str], HarnessConfig],
    environ: Mapping[str, str],
    home: str,
    operator: str = "",
) -> WriteResult:
    """Set (or with None unset) one live knob in the repo overlay or shared.env."""
    if key not in _SPECS:
        return WriteResult(False, f"{key!r} is not a dashboard setting")
    if scope not in SCOPES:
        return WriteResult(False, f"scope must be one of {list(SCOPES)}")
    files = Files(harness_root, name)
    target = files.path(scope)
    try:
        before = config_files.read_text(target)
        after = config_files.set_assignment(before, key, value)
    except (OSError, ValueError) as exc:
        return WriteResult(False, str(exc))
    affected = (name,) if scope == "repo" else tuple(n for n in names if os.path.isfile(Files(harness_root, n).active))
    warnings: list[str] = []
    for deployment in dict.fromkeys((name, *affected)):
        candidate = Files(harness_root, deployment)
        env, errors = _candidate_errors(candidate, {target: after}, environ)
        if errors:
            return WriteResult(False, f"refused, {deployment} would be invalid: " + "; ".join(errors))
        if value is not None and env.get(key) != value:
            if scope == "repo":
                return WriteResult(False, f"{key} would read back as {env.get(key)!r}, not {value!r}")
            warnings.append(f"{deployment}: config/repos/{deployment}.env still sets {key}={env.get(key)!r}")
    old = {deployment: _text(getattr(load(deployment), _FIELD[key])) for deployment in affected}
    try:
        config_files.write_text(target, after)
    except OSError as exc:
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
            return WriteResult(False, f"{deployment}: {problem}; " + _roll_back(target, before, composed, apply))
        composed.append(deployment)
        warnings += published
    who = operator or _operator()
    for deployment in affected:
        config_files.journal_change(load(deployment).state_dir, {
            "operator": who, "deployment": deployment, "key": key, "old": old.get(deployment, ""),
            "new": "" if value is None else value, "scope": scope, "file": target})
    shown = "(unset)" if value is None else repr(value)
    return WriteResult(True, f"{key}={shown} in {os.path.relpath(target, harness_root)}; "
                             f"{'applies at the next tick' if composed else 'applies at the next launch'}",
                       tuple(warnings))


def _roll_back(target: str, before: str, composed: list[str],
               apply: Callable[[str], tuple[str, list[str]]]) -> str:
    """Restore the source file, then recompose AND republish every deployment
    already composed: an installed service reads its snapshot, so a
    recomposed `.active` alone would leave it running the refused value."""
    try:
        config_files.write_text(target, before)
    except OSError as exc:
        return f"{target} NOT restored ({exc}); fix it by hand"
    stuck = [f"{done}: {problem or '; '.join(published)}" for done in composed
             for problem, published in [apply(done)] if problem or published]
    return f"{target} restored" + (f"; not re-applied: {', '.join(stuck)}" if stuck else "")


def _operator() -> str:
    try:
        return f"{getpass.getuser()} (dashboard)"
    except (OSError, KeyError):
        return "dashboard"
