"""Environment → immutable config snapshot under ADR 0007.

Pure mapping `dict → HarnessConfig` so tests construct it without os.environ —
with one deliberate exception: an env carrying `AGENT_ENV` is re-resolved
against that FILE first (`_authoritative_env`, #12), because the inherited
environment of point-at.sh's relaunch loop cannot represent a knob's removal.
Every operator knob's env name, coercion and choices live in its field's
metadata, which also renders config/agent.env.example.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import MISSING, asdict, dataclass, field, fields
from enum import Enum

from ..adapters.control_path import machine_control_authority_root
from ..adapters.env_file import source_env_file
from ..domain.agent_task import RuntimeName
from ..domain.effort import EFFORT_LADDER, Effort, parse_effort
from ..domain.identity import normalize_login
from ..domain.contracts import Money
from ..domain.pricing import (
    BudgetPolicy,
    ModelRates,
    RateTable,
    UnknownModelPolicy,
    default_cache_read_rate,
    default_cache_write_rate,
    parse_model_rate_table,
    usd_per_million_to_micros,
    with_default_model_rates,
)
from ..domain.repository import canonical_repository, repository_validation_error
from ..domain.routing import TIER_LADDER, ModelRoute, Tier, parse_model_route


def deployment_identity_for_target(repository: str, target_dir: str, role: str) -> str:
    """Stable identity for one repository checkout and daemon role."""
    checkout = os.path.realpath(target_dir) if target_dir else ""
    return hashlib.sha256(f"{repository}:{checkout}:{role}".encode()).hexdigest()[:16]


def owner_identity_for_target(repository: str, target_dir: str, role: str) -> str:
    return f"{repository}:{role}:{deployment_identity_for_target(repository, target_dir, role)}"


def _knob(default, *, env: str = "", coerce: str = "", choices: tuple[str, ...] = ()):
    """An operator knob: env name defaults to the field name upper-cased."""
    metadata: dict[str, object] = {"coerce": coerce or _default_coercion(default)}
    if env:
        metadata["env"] = env
    if choices:
        metadata["choices"] = choices
    return field(default=default, metadata=metadata)


def _default_coercion(default) -> str:
    if isinstance(default, bool):
        return "bool"
    if isinstance(default, int):
        return "int"
    if isinstance(default, float):
        return "float"
    return "optional_string" if default is None else "string"


_RUNTIMES = tuple(item.value for item in RuntimeName)


@dataclass(frozen=True)
class HarnessConfig:
    repo: str = _knob("")
    base_branch: str = _knob("dev")
    target_dir: str = _knob("")
    fix_bot: str = _knob("")
    review_bot: str = _knob("my-reviewer-bot[bot]")
    # Blank → ~/.local/state/<role>/<repo slug> (see `default_state_dir`).
    state_dir: str = _knob("")
    review_state_dir: str = _knob("")
    worktree_root: str = _knob(".claude/worktrees")
    poll_seconds: int = _knob(20)
    # Fix legs on different issues run concurrently up to this bound (one leg
    # per issue). 2 since 2026-08-02: with one slot a single ADDRESS_REVIEW
    # leg held the whole deployment while other PRs' repairs queued.
    fix_dispatch_concurrency: int = _knob(2)
    auto_merge: bool = _knob(True)
    merge_method: str = _knob("squash", coerce="choice", choices=("squash", "merge", "rebase"))
    fix_model: str | None = _knob(None)
    # Base reasoning effort for fix legs (#16). Unset leaves each runtime on
    # its own default (CODEX_REASONING_EFFORT for codex, the SDK's for Claude).
    fix_effort: Effort | None = _knob(None, coerce="effort", choices=tuple(e.value for e in EFFORT_LADDER))
    # The reviewer when the reviewed head's fixer tier is unknown or that tier
    # has no FIX_MODEL_<TIER> spec. With tier specs set, review runs at the
    # fixer's tier (owner, 2026-10-13); independence is the separate leg and
    # REVIEW_BOT identity. Claude reviewers default to opus.
    review_model: str | None = _knob("gpt-6.1-sol")
    # Routing: the `runtime:model` each triage tier's fix legs run on, e.g.
    # `codex:gpt-6-luna` or `claude:claude-haiku-5-5`. Blank keeps the fixer
    # runtime with FIX_MODEL (codex: CODEX_MODEL) for that tier.
    fix_model_low: str = _knob("")
    fix_model_med: str = _knob("")
    fix_model_high: str = _knob("")
    fix_model_xhigh: str = _knob("")
    # The TRIAGE leg's `runtime:model`. Blank runs it on FIX_MODEL_LOW's route.
    triage_model: str = _knob("")
    # TRIAGE reserves this, not MAX_BUDGET_USD: a cheap classifier must not
    # hold a fix leg's reservation against the daily caps.
    triage_budget_usd: float = _knob(2.0)
    fix_budget_usd: float = _knob(10.0, env="MAX_BUDGET_USD")
    # $15 since 2026-08-09: sol reviews at ~2.5x terra's token rates.
    review_budget_usd: float = _knob(15.0)
    review_prompt: str = _knob("", coerce="stripped")
    harness_root: str = _knob("", env="HARNESS_ROOT_DIR")
    fixer_runtime: RuntimeName = _knob(RuntimeName.CLAUDE, coerce="runtime", choices=_RUNTIMES)
    reviewer_runtime: RuntimeName = _knob(RuntimeName.CODEX, coerce="runtime", choices=_RUNTIMES)
    codex_bin: str = _knob("codex")
    codex_model: str = _knob("")  # blank → CodexRunner default
    codex_provider_id: str = _knob("openai-custom")  # NOT "openai": reserved by codex-cli ≥0.142
    codex_provider_name: str = _knob("OpenAI")
    # Blank = codex's built-in provider + `codex login` OAuth. Set only for an
    # OpenAI-compatible gateway, which then needs CODEX_ENV_KEY's variable set.
    codex_base_url: str = _knob("")
    codex_env_key: str = _knob("OPENAI_API_KEY")
    codex_wire_api: str = _knob("responses", coerce="choice", choices=("responses", "chat"))
    codex_sandbox: str = _knob(
        "danger-full-access", coerce="choice", choices=("read-only", "workspace-write", "danger-full-access"))
    # codex exec's own default is low (2026-07-27); "" leaves codex alone.
    codex_reasoning_effort: str = _knob(
        "medium", coerce="choice", choices=("", "low", "medium", "high", "xhigh"))
    # Max seconds between stream events before a run is hung. Must exceed the
    # longest single tool call (the SDK Bash tool caps a foreground command at
    # ~10 min). 0 disables.
    agent_idle_timeout: float = _knob(900.0, env="AGENT_IDLE_TIMEOUT_SECONDS")
    # Wall cap per session: a stream limping after suspend/resume resets the
    # idle clock forever (FAILURE-MODES #60). 0 disables.
    agent_max_runtime: float = _knob(2700.0, env="AGENT_MAX_RUNTIME_SECONDS")
    # USD/MTok used when a provider reports no billed cost. Synthetic on a
    # subscription, but the quota is real: these let the spend caps bound
    # token volume. `CODEX_COST_PER_MTOK='model=IN/OUT[/CACHED_IN],...'` prices
    # per model (#43); cached input defaults to a tenth of input and must be
    # positive when given (#89). Unnamed models use the two scalars.
    codex_cost_per_mtok_in: float = _knob(5.0)
    codex_cost_per_mtok_out: float = _knob(30.0)
    codex_cost_per_mtok: tuple[tuple[str, float, float], ...] = _knob((), coerce="rate_table")
    # Aggregate caps (0 disables): lifetime per issue, per repo per day,
    # host-global per day. SQLite reservations enforce all three atomically.
    issue_spend_cap_usd: float = _knob(25.0)
    daily_spend_cap_usd: float = _knob(700.0)
    global_daily_spend_cap_usd: float = _knob(0.0)
    # ── internal (no env knob) ──
    agent_md: str = ""
    codex_cost_per_mtok_cached_in: tuple[tuple[str, float], ...] = ()
    control_authority_root: str = ""
    # Malformed values from_env replaced with defaults, surfaced by validate().
    parse_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "repo", canonical_repository(self.repo))

    @property
    def config_version(self) -> str:
        """Stable identity of the immutable snapshot used by one admission."""
        return hashlib.sha256(repr(self).encode()).hexdigest()[:16]

    def state_dir_for(self, role: str) -> str:
        return {"fix": self.state_dir, "review": self.review_state_dir}[role]

    def validation_errors(self) -> tuple[str, ...]:
        errors: list[str] = list(self.parse_errors)
        if not self.repo.strip():
            errors.append("REPO must be set (owner/name) — is the repo overlay composed?")
        elif detail := repository_validation_error(self.repo):
            errors.append(f"REPO {self.repo!r} {detail}")
        if not self.fix_bot.strip():
            # Without it the fixer's own PRs/reviews can never be identified.
            errors.append("FIX_BOT must be set")
        if normalize_login(self.fix_bot) == normalize_login(self.review_bot):
            errors.append("FIX_BOT and REVIEW_BOT normalize to the same identity "
                          "(GitHub rejects self-approval with a 422)")
        for name, value in (("POLL_SECONDS", self.poll_seconds),
                            ("FIX_DISPATCH_CONCURRENCY", self.fix_dispatch_concurrency),
                            ("MAX_BUDGET_USD", self.fix_budget_usd),
                            ("TRIAGE_BUDGET_USD", self.triage_budget_usd),
                            ("REVIEW_BUDGET_USD", self.review_budget_usd)):
            if value <= 0:
                errors.append(f"{name} must be > 0")
        for name, value in (("AGENT_IDLE_TIMEOUT_SECONDS", self.agent_idle_timeout),
                            ("AGENT_MAX_RUNTIME_SECONDS", self.agent_max_runtime),
                            ("CODEX_COST_PER_MTOK_IN", self.codex_cost_per_mtok_in),
                            ("CODEX_COST_PER_MTOK_OUT", self.codex_cost_per_mtok_out),
                            ("ISSUE_SPEND_CAP_USD", self.issue_spend_cap_usd),
                            ("DAILY_SPEND_CAP_USD", self.daily_spend_cap_usd),
                            ("GLOBAL_DAILY_SPEND_CAP_USD", self.global_daily_spend_cap_usd)):
            if value < 0:
                errors.append(f"{name} must be >= 0")
        for name, value in self._route_specs():
            if value.strip():
                try:
                    parse_model_route(value)
                except ValueError as exc:
                    errors.append(f"{name} {value!r}: {exc}")
        caps = (self.issue_spend_cap_usd, self.daily_spend_cap_usd, self.global_daily_spend_cap_usd)
        if any(cap > 0 for cap in caps) and not self.codex_budget_priceable:
            errors.append("armed spend caps require positive input/output pricing; "
                          "set CODEX_COST_PER_MTOK_IN/OUT or disable every spend cap")
        return tuple(errors)

    def _route_specs(self) -> tuple[tuple[str, str], ...]:
        return (("FIX_MODEL_LOW", self.fix_model_low), ("FIX_MODEL_MED", self.fix_model_med),
                ("FIX_MODEL_HIGH", self.fix_model_high), ("FIX_MODEL_XHIGH", self.fix_model_xhigh),
                ("TRIAGE_MODEL", self.triage_model))

    def _tier_route(self, tier: Tier) -> ModelRoute | None:
        return _route(dict(zip(TIER_LADDER, (self.fix_model_low, self.fix_model_med, self.fix_model_high,
                                             self.fix_model_xhigh)))[tier])

    def fix_route(self, tier: Tier) -> tuple[RuntimeName, str | None]:
        """(runtime, model) for a fix leg at `tier`. An unset or invalid spec
        (validate() reports it) keeps today's single configured fixer."""
        route = self._tier_route(tier)
        return (route.runtime, route.model) if route is not None else (self.fixer_runtime, self.fix_model)

    def review_route(self, tier: Tier | None) -> tuple[RuntimeName, str | None]:
        """(runtime, model) for a REVIEW at the fixer's `tier` (owner,
        2026-10-13: the reviewer matches the fixer's tier). No tier, or no
        spec for it, keeps REVIEWER_RUNTIME with REVIEW_MODEL."""
        route = self._tier_route(tier) if tier is not None else None
        return (route.runtime, route.model) if route is not None else (self.reviewer_runtime, self.review_model)

    def triage_route(self) -> tuple[RuntimeName, str | None]:
        route = _route(self.triage_model)
        return (route.runtime, route.model) if route is not None else self.fix_route(Tier.LOW)

    def runtimes(self, role: str) -> frozenset[RuntimeName]:
        """Every runtime a role may dispatch a leg on."""
        if role == "fix":
            return frozenset({self.fixer_runtime, self.triage_route()[0],
                              *(self.fix_route(t)[0] for t in TIER_LADDER)})
        return frozenset({self.reviewer_runtime, *(self.review_route(t)[0] for t in TIER_LADDER)})

    def validate(self) -> None:
        if errors := self.validation_errors():
            raise ValueError("invalid harness config: " + "; ".join(errors))

    @property
    def codex_budget_priceable(self) -> bool:
        """Every model path has complete dollar pricing: one zero would let a
        routed model spend outside the meter."""
        return (self.codex_cost_per_mtok_in > 0 and self.codex_cost_per_mtok_out > 0
                and all(i > 0 and o > 0 for _, i, o in self.codex_cost_per_mtok)
                and all(rate > 0 for _, rate in self.codex_cost_per_mtok_cached_in)
                and not any(e.startswith("CODEX_COST_PER_MTOK ") for e in self.parse_errors))

    def describe(self) -> str:
        """Resolved config printed at daemon start: the silent-default class
        (#16/#49/#50/#78) is only diagnosable when effective values are visible.
        Never includes secrets."""
        def model(m: str | None) -> str:
            return m or "(sdk default)"

        fix_budget = f"fix_budget=${self.fix_budget_usd:.2f}"
        if self.fixer_runtime is RuntimeName.CODEX:
            # codex has no budget flag: the runner kills a leg whose priced
            # rollout usage crosses it, which needs complete pricing (#29).
            fix_budget += (" (harness-enforced on codex)" if self.codex_budget_priceable
                           else " (NOT ENFORCED on codex: input and output prices must be set)")
        global_cap = f"${self.global_daily_spend_cap_usd:.2f}" if self.global_daily_spend_cap_usd > 0 else "off"
        return "\n".join([
            f"repo={self.repo or '(UNSET)'} base={self.base_branch} poll={self.poll_seconds}s",
            f"fixer={self.fixer_runtime.value} fix_model={model(self.fix_model)} "
            f"fix_effort={getattr(self.fix_effort, 'value', None) or '(runtime default)'} "
            f"dispatch_concurrency={self.fix_dispatch_concurrency} {fix_budget}",
            "routes: " + " ".join(f"{name}={route[0].value}:{model(route[1])}" for name, route in (
                *((t.value, self.fix_route(t)) for t in TIER_LADDER), ("triage", self.triage_route())))
            + f" triage_budget=${self.triage_budget_usd:.2f}",
            f"reviewer={self.reviewer_runtime.value} review_model={model(self.review_model)} "
            f"review_budget=${self.review_budget_usd:.2f}",
            f"timeouts: idle={self.agent_idle_timeout:.0f}s wall={self.agent_max_runtime:.0f}s",
            f"spend_caps: issue=${self.issue_spend_cap_usd:.2f} "
            f"daily/{self.repo or 'repo'}=${self.daily_spend_cap_usd:.2f} daily/global={global_cap} "
            f"state={self.state_dir or '(unset)'} "
            f"control={self.control_authority_root or '(deployment state parent)'} "
            f"target={self.target_dir or '(cwd)'}",
        ])


_FIELDS = {item.name: item for item in fields(HarnessConfig)}


def budget_policy(cfg: HarnessConfig) -> BudgetPolicy:
    """The per-leg maximum each paid act reserves."""
    fix = Money.from_major("USD", cfg.fix_budget_usd)
    limits = {("fix", kind): fix for kind in ("RESOLVE", "ADDRESS_REVIEW", "FIX_CI", "REBASE")}
    return BudgetPolicy({**limits, ("fix", "TRIAGE"): Money.from_major("USD", cfg.triage_budget_usd),
                         ("review", "REVIEW"): Money.from_major("USD", cfg.review_budget_usd)})


def _route(spec: str) -> ModelRoute | None:
    try:
        return parse_model_route(spec) if spec.strip() else None
    except ValueError:
        return None


def rate_table(cfg: HarnessConfig, runtime_name: str) -> RateTable | None:
    """The one rate card the live budget kill, settlement and recovery price
    with. Codex also carries the shipped per-model defaults; other runtimes
    price synthetically from the configured rates alone."""
    micros = usd_per_million_to_micros
    try:
        fallback = ModelRates(micros(cfg.codex_cost_per_mtok_in), micros(cfg.codex_cost_per_mtok_out),
                              micros(cfg.codex_cost_per_mtok_in / 10), micros(cfg.codex_cost_per_mtok_in))
        cached = dict(cfg.codex_cost_per_mtok_cached_in)
        codex = runtime_name == RuntimeName.CODEX.value
        rates = {
            model: ModelRates(micros(rate_in), micros(rate_out),
                              micros(cached.get(model, default_cache_read_rate(model, rate_in))),
                              micros(default_cache_write_rate(model, rate_in)))
            for model, rate_in, rate_out in (
                with_default_model_rates(cfg.codex_cost_per_mtok) if codex else cfg.codex_cost_per_mtok)
        }
    except ValueError:
        return None
    # Shipped defaults change without an overlay edit, so a receipt names the
    # effective prices, not just the operator's input snapshot.
    version = hashlib.sha256(json.dumps({
        "config": cfg.config_version,
        "rates": {model.strip().casefold(): asdict(value) for model, value in rates.items()},
        "fallback": asdict(fallback),
    }, sort_keys=True).encode()).hexdigest()[:16]
    card = "codex-config" if codex else f"{runtime_name}-synthetic-config"
    return RateTable(card, version, "USD", rates, UnknownModelPolicy.USE_FALLBACK, fallback)


def supervision_dir(cfg: HarnessConfig, role: str) -> str:
    """Private process registry for one (repo, checkout, role) deployment."""
    root = cfg.state_dir_for(role)
    if not root:
        raise ValueError("process supervisor state directory is required")
    return os.path.join(root, "process-supervision", deployment_identity_for_target(cfg.repo, cfg.target_dir, role))


def _env_name(name: str) -> str:
    return str(_FIELDS[name].metadata.get("env") or name.upper())


def render_agent_md(text: str, cfg: HarnessConfig) -> str:
    """Expand the AGENT.md template's config $VARs (GPT models take a literal
    `$DIAGNOSE_RULES` at face value) and strip the optional rule blocks.
    $ISSUE_NUMBER stays literal: the prompt fills it per issue."""
    repl = {"$BASE_BRANCH": cfg.base_branch, "$HARNESS_REPO": cfg.repo,
            "$DIAGNOSE_RULES": "", "$LOGGING_RULES": "", "$DOC_RULES": "", "$TEST_RULES": ""}
    for k, v in repl.items():
        text = text.replace(k, v)
    return text


def generated_env_reference() -> str:
    """Render the complete operator env template from field metadata."""
    lines = ["# Generated from HarnessConfig metadata; do not hand-edit.",
             "# Set REPO and FIX_BOT before starting a daemon.", ""]
    for definition in fields(HarnessConfig):
        metadata = definition.metadata
        if not metadata:
            continue
        default = definition.default
        if default is MISSING or default is None or isinstance(default, tuple):
            rendered = ""
        elif isinstance(default, bool):
            rendered = "1" if default else "0"
        elif isinstance(default, Enum):
            rendered = str(default.value)
        else:
            rendered = str(default)
        choices = metadata.get("choices")
        suffix = "  # choices: " + "|".join(choices) if choices else f"  # {metadata['coerce']}"
        lines.append(f"{_env_name(definition.name)}={json.dumps(rendered, ensure_ascii=False)}{suffix}")
    return "\n".join(lines) + "\n"


# Explicit spellings: an unrecognized token falls back to the default with a
# validation error instead of silently reading truthy (review P1R-8).
_TRUE_TOKENS = frozenset({"1", "true", "yes", "on"})
_FALSE_TOKENS = frozenset({"0", "false", "no", "off"})


def _parse(raw: str, key: str, definition, errors: list[str]) -> object:
    """One non-empty env value through its field's coercion. A malformed value
    falls back to the default and is recorded, so config construction never
    tracebacks on operator input (review M2)."""
    default = definition.default
    coercion = definition.metadata["coerce"]
    choices = definition.metadata.get("choices", ())
    token = raw.strip()

    def bad(expected: str) -> object:
        shown = getattr(default, "value", default)
        errors.append(f"{key} must be {expected}, got {raw!r} — using default {shown!r}")
        return default

    if coercion == "bool":
        folded = token.casefold()
        return (True if folded in _TRUE_TOKENS else False if folded in _FALSE_TOKENS
                else bad(f"one of {sorted(_TRUE_TOKENS | _FALSE_TOKENS)}"))
    if coercion == "int":
        try:
            return int(token)
        except ValueError:
            return bad("an integer")
    if coercion == "float":
        # NaN/inf pass every range check downstream, silently disabling a cap.
        try:
            value = float(token)
        except ValueError:
            return bad("a number")
        return value if math.isfinite(value) else bad("a finite number")
    if coercion == "effort":
        return parse_effort(token, None) or bad(f"one of {list(choices)}")
    if coercion in ("choice", "runtime"):
        if token not in choices:
            return bad(f"one of {list(choices)}")
        return RuntimeName(token) if coercion == "runtime" else token
    if coercion == "stripped":
        return token
    if coercion == "optional_string":
        return raw or None
    return raw


def _repo_slug(repo: str) -> str:
    """`owner/name` → a collision-free state-dir component. "_" is illegal in
    an owner login, so the first "__" is always the owner/name boundary (R2-10:
    a "-" join collided `acme/app-b` with `acme-app/b`). Casefolded: GitHub
    names are case-insensitive identity."""
    repo = (repo or "").strip()
    return repo.replace("/", "__").casefold() if repo else "unknown"


def default_state_dir(repo: str, role_dir: str) -> str:
    """The state dir a daemon derives with no explicit STATE_DIR/
    REVIEW_STATE_DIR (per repo, review M5/D7). Read-side callers use it too, so
    they land on the directory the daemon writes (keifu 2026-08-02)."""
    return os.path.expanduser(f"~/.local/state/{role_dir}/{_repo_slug(canonical_repository(repo))}")


# Names an env file textually ASSIGNS (bin/compose-env.sh's anchor).
_ASSIGNMENT_RE = re.compile(r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)=", re.MULTILINE)


def _file_env(path: str, inherited: dict) -> dict[str, str] | None:
    """`{name: value}` for every variable the env file ASSIGNS, sourced under
    `inherited` so `${FOO:-x}` resolves; None when unreadable or empty (an
    empty read would silently reset every knob to its default)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            assigned = {m.group(1) for m in _ASSIGNMENT_RE.finditer(stream.read())}
        if not assigned:
            return None
        return source_env_file(path, inherited, assigned_only=True)
    except Exception:  # noqa: BLE001 — config load must not die on a bad file
        return None


def _authoritative_env(e: dict) -> dict:
    """AGENT_ENV's FILE is the authority (#12): a knob it assigns wins, a knob
    it does not is absent (the field default applies). point-at.sh's relaunch
    loop re-sources into one long-lived shell, which can overwrite but never
    unset, so a knob deleted from .active.env stayed inherited (2026-07-27).
    No AGENT_ENV or an unreadable file: the passed env unchanged."""
    path = e.get("AGENT_ENV", "")
    if not path or not os.access(path, os.R_OK):
        return e
    parsed = _file_env(path, e)
    return e if parsed is None else parsed


def from_env(env: dict | None = None) -> HarnessConfig:
    """Build one immutable snapshot from the metadata-backed config table."""
    e = _authoritative_env(env if env is not None else os.environ)
    errors: list[str] = []
    values: dict[str, object] = {}
    for definition in fields(HarnessConfig):
        if not definition.metadata:
            continue
        key = _env_name(definition.name)
        raw = e.get(key)
        if definition.metadata["coerce"] == "rate_table":
            table, cached, problems = parse_model_rate_table(raw or "")
            errors.extend(f"{key} {problem} — ignoring it" for problem in problems)
            values[definition.name] = table
            values["codex_cost_per_mtok_cached_in"] = cached
        elif raw is not None and (str(raw).strip() or definition.metadata["coerce"] in ("string", "optional_string")):
            values[definition.name] = _parse(str(raw), key, definition, errors)
    repo = canonical_repository(str(values.get("repo", "")).strip())
    values["repo"] = repo
    values["state_dir"] = values.get("state_dir") or default_state_dir(repo, "issue-agent")
    values["review_state_dir"] = values.get("review_state_dir") or default_state_dir(repo, "review-agent")
    if not e.get("REVIEW_MODEL"):
        claude = values.get("reviewer_runtime") is RuntimeName.CLAUDE
        values["review_model"] = "opus" if claude else _FIELDS["review_model"].default
    values["control_authority_root"] = machine_control_authority_root()
    values["parse_errors"] = tuple(errors)
    return HarnessConfig(**values)
