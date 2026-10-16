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
from collections.abc import Mapping
from dataclasses import MISSING, asdict, dataclass, field, fields, replace
from enum import Enum

from ..adapters.control_path import machine_control_authority_root
from ..adapters.env_file import source_env_file
from ..domain.agent_task import RuntimeName
from ..domain.effort import Effort
from ..domain.identity import account_eq
from ..domain.contracts import Money
from ..domain.pricing import (
    CLAUDE_ALIASES,
    CLAUDE_CACHE_WRITE_MULTIPLIER,
    CLAUDE_DEFAULT_MODEL_RATES,
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
from ..domain.world import Pickup


def deployment_identity_for_target(repository: str, target_dir: str, role: str) -> str:
    """Stable identity for one repository checkout and daemon role."""
    checkout = os.path.realpath(target_dir) if target_dir else ""
    return hashlib.sha256(f"{repository}:{checkout}:{role}".encode()).hexdigest()[:16]


def owner_identity_for_target(repository: str, target_dir: str, role: str) -> str:
    return f"{repository}:{role}:{deployment_identity_for_target(repository, target_dir, role)}"


def _knob(default, *, env: str = "", coerce: str = "", choices: tuple[str, ...] = (), live: bool = False):
    """An operator knob: env name defaults to the field name upper-cased.
    `live`: a dispatch-only knob a running daemon re-reads each tick (ADR 0007
    amendment, `LiveConfig`); every other knob takes effect by hot reload."""
    metadata: dict[str, object] = {"coerce": coerce or _default_coercion(default)}
    if env:
        metadata["env"] = env
    if choices:
        metadata["choices"] = choices
    if live:
        metadata["live"] = True
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

# The review role runs one slot-holding leg at a time (`compose_role` capacity).
REVIEW_DISPATCH_CAPACITY = 1
# FIX_DISPATCH_CONCURRENCY changes without a restart, so a daemon's dispatch
# pool is sized once for this bound (`tick.Daemon`).
MAX_DISPATCH_CONCURRENCY = 16


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
    # Slot-holding fix legs (all but STEWARD and TRIAGE, which have their own equal ceiling)
    # run concurrently up to this bound (one leg per issue). 2 since 2026-08-02:
    # with one slot a single ADDRESS_REVIEW leg held the whole deployment while
    # other PRs' repairs queued.
    fix_dispatch_concurrency: int = _knob(2, live=True)
    auto_merge: bool = _knob(True)
    merge_method: str = _knob("squash", coerce="choice", choices=("squash", "merge", "rebase"))
    # The fix role's default model on FIXER_RUNTIME (blank: the runtime's own).
    fix_model: str | None = _knob(None, live=True)
    # The reviewer when the reviewed head's fixer tier is unknown or that tier
    # has no FIX_MODEL_<TIER> spec. With tier specs set, review runs at the
    # fixer's tier (owner, 2026-10-13); independence is the separate leg and
    # REVIEW_BOT identity. Claude reviewers default to opus.
    review_model: str | None = _knob("gpt-6.1-sol", live=True)
    # Routing: the `runtime:model[:effort]` each triage tier's fix legs run on, e.g.
    # `codex:gpt-6-luna:low` or `claude:claude-haiku-5-5`. Blank keeps the fixer
    # runtime with FIX_MODEL for that tier.
    fix_model_low: str = _knob("", live=True)
    fix_model_med: str = _knob("", live=True)
    fix_model_high: str = _knob("", live=True)
    fix_model_xhigh: str = _knob("", live=True)
    # The TRIAGE leg's `runtime:model[:effort]`. Blank runs it on FIX_MODEL_LOW's route.
    triage_model: str = _knob("", live=True)
    # The STEWARD leg's `runtime:model[:effort]`. Blank runs it on TRIAGE_MODEL's route.
    steward_model: str = _knob("", live=True)
    # TRIAGE and STEWARD each reserve this, not MAX_BUDGET_USD: a cheap
    # classifier must not hold a fix leg's reservation against the daily caps.
    triage_budget_usd: float = _knob(2.0)
    fix_budget_usd: float = _knob(10.0, env="MAX_BUDGET_USD")
    # $15 since 2026-08-09: sol reviews at ~2.5x terra's token rates.
    review_budget_usd: float = _knob(15.0)
    review_prompt: str = _knob("", coerce="stripped")
    harness_root: str = _knob("", env="HARNESS_ROOT_DIR")
    fixer_runtime: RuntimeName = _knob(RuntimeName.CLAUDE, coerce="runtime", choices=_RUNTIMES)
    reviewer_runtime: RuntimeName = _knob(RuntimeName.CODEX, coerce="runtime", choices=_RUNTIMES)
    codex_bin: str = _knob("codex")
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
    # USD/MTok the harness prices tokens at. Synthetic on a subscription, but
    # the quota is real: these let the spend caps bound token volume.
    # `CODEX_COST_PER_MTOK='model=IN/OUT[/CACHED_IN],...'` prices per model
    # (#43) and wins over the shipped Codex and Claude rates; cached input
    # defaults to a tenth of input and must be positive when given (#89).
    # Unnamed Codex models use the two scalars; an unnamed Claude model has
    # no price and is refused admission (#389).
    codex_cost_per_mtok_in: float = _knob(5.0)
    codex_cost_per_mtok_out: float = _knob(30.0)
    codex_cost_per_mtok: tuple[tuple[str, float, float], ...] = _knob((), coerce="rate_table")
    # Aggregate caps (0 disables): lifetime per issue, per repo per day,
    # host-global per day. SQLite reservations enforce all three atomically.
    issue_spend_cap_usd: float = _knob(25.0, live=True)
    daily_spend_cap_usd: float = _knob(700.0, live=True)
    global_daily_spend_cap_usd: float = _knob(0.0)
    # Which open issues the fixer works: `ready` those a human marked
    # ai-ready; `open` every open issue except needs-human ones and the
    # harness's own gap reports (`domain.state.picked_up`).
    issue_pickup: str = _knob(Pickup.READY.value, coerce="choice", choices=tuple(p.value for p in Pickup), live=True)
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

    @property
    def pickup(self) -> Pickup:
        return Pickup(self.issue_pickup)

    @property
    def dispatch_slots(self) -> int:
        """Heavy dispatch slots across both roles: what the board's agent lane fills."""
        return self.fix_dispatch_concurrency + REVIEW_DISPATCH_CAPACITY

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
        if account_eq(self.fix_bot, self.review_bot):
            errors.append("FIX_BOT and REVIEW_BOT normalize to the same identity "
                          "(GitHub rejects self-approval with a 422)")
        for name, value in (("POLL_SECONDS", self.poll_seconds),
                            ("FIX_DISPATCH_CONCURRENCY", self.fix_dispatch_concurrency),
                            ("MAX_BUDGET_USD", self.fix_budget_usd),
                            ("TRIAGE_BUDGET_USD", self.triage_budget_usd),
                            ("REVIEW_BUDGET_USD", self.review_budget_usd)):
            if value <= 0:
                errors.append(f"{name} must be > 0")
        if self.fix_dispatch_concurrency > MAX_DISPATCH_CONCURRENCY:
            errors.append(f"FIX_DISPATCH_CONCURRENCY must be <= {MAX_DISPATCH_CONCURRENCY}")
        for name, value in (("AGENT_IDLE_TIMEOUT_SECONDS", self.agent_idle_timeout),
                            ("AGENT_MAX_RUNTIME_SECONDS", self.agent_max_runtime),
                            ("CODEX_COST_PER_MTOK_IN", self.codex_cost_per_mtok_in),
                            ("CODEX_COST_PER_MTOK_OUT", self.codex_cost_per_mtok_out),
                            ("ISSUE_SPEND_CAP_USD", self.issue_spend_cap_usd),
                            ("DAILY_SPEND_CAP_USD", self.daily_spend_cap_usd),
                            ("GLOBAL_DAILY_SPEND_CAP_USD", self.global_daily_spend_cap_usd)):
            if value < 0:
                errors.append(f"{name} must be >= 0")
        for name, runtime, model in (("FIX_MODEL", self.fixer_runtime, self.fix_model),
                                     ("REVIEW_MODEL", self.reviewer_runtime, self.review_model)):
            try:
                ModelRoute(runtime, (model or "").strip() or None)
            except ValueError as exc:
                errors.append(f"{name} {model!r} on {runtime.value}: {exc}")
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
        # Admission refuses a Claude leg on a model with no harness rate (#389):
        # refuse the configuration instead of parking every item it dispatches.
        card = rate_table(self, RuntimeName.CLAUDE.value) if any(cap > 0 for cap in caps) else None
        routes = (*(self.fix_route(t) for t in TIER_LADDER), *(self.review_route(t) for t in (None, *TIER_LADDER)),
                  self.triage_route(), self.steward_route())
        if card is not None and (unrated := sorted({r.model for r in routes if r.model
                                                    and r.runtime is RuntimeName.CLAUDE
                                                    and not card.priceable(r.model)})):
            errors.append(f"armed spend caps need a harness rate for every Claude model; {', '.join(unrated)} has "
                          "none: add it to CODEX_COST_PER_MTOK or disable every spend cap")
        return tuple(errors)

    def _route_specs(self) -> tuple[tuple[str, str], ...]:
        return (("FIX_MODEL_LOW", self.fix_model_low), ("FIX_MODEL_MED", self.fix_model_med),
                ("FIX_MODEL_HIGH", self.fix_model_high), ("FIX_MODEL_XHIGH", self.fix_model_xhigh),
                ("TRIAGE_MODEL", self.triage_model), ("STEWARD_MODEL", self.steward_model))

    def _tier_route(self, tier: Tier) -> ModelRoute | None:
        return _route(dict(zip(TIER_LADDER, (self.fix_model_low, self.fix_model_med, self.fix_model_high,
                                             self.fix_model_xhigh)))[tier])

    def fix_route(self, tier: Tier) -> ModelRoute:
        """The route of a fix leg at `tier`: its FIX_MODEL_<TIER> spec, else
        FIXER_RUNTIME with FIX_MODEL. No effort leaves the runtime's own
        default (CODEX_REASONING_EFFORT on codex)."""
        return self._tier_route(tier) or _default_route(self.fixer_runtime, self.fix_model)

    def review_route(self, tier: Tier | None) -> ModelRoute:
        """The route of a REVIEW at the fixer's `tier` (owner, 2026-10-13: the
        reviewer matches the fixer's tier). No tier, or no spec for it, keeps
        REVIEWER_RUNTIME with REVIEW_MODEL."""
        route = self._tier_route(tier) if tier is not None else None
        return route or _default_route(self.reviewer_runtime, self.review_model)

    def triage_route(self) -> ModelRoute:
        return _route(self.triage_model) or self.fix_route(Tier.LOW)

    def steward_route(self) -> ModelRoute:
        return _route(self.steward_model) or self.triage_route()

    def runtimes(self, role: str) -> frozenset[RuntimeName]:
        """Every runtime a role may dispatch a leg on."""
        if role == "fix":
            return frozenset({self.fixer_runtime, self.triage_route().runtime, self.steward_route().runtime,
                              *(self.fix_route(t).runtime for t in TIER_LADDER)})
        return frozenset({self.reviewer_runtime, *(self.review_route(t).runtime for t in TIER_LADDER)})

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

        def effort(e: Effort | None) -> str:
            return f":{e.value}" if e else ""

        def spec(route: ModelRoute) -> str:
            return f"{route.runtime.value}:{model(route.model)}{effort(route.effort)}"

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
            f"dispatch_concurrency={self.fix_dispatch_concurrency} {fix_budget}",
            "routes: " + " ".join(f"{name}={spec(route)}" for name, route in (
                *((t.value, self.fix_route(t)) for t in TIER_LADDER), ("steward", self.steward_route()), ("triage", self.triage_route())))
            + f" triage_budget=${self.triage_budget_usd:.2f} (each of steward, triage)",
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
                         ("fix", "STEWARD"): Money.from_major("USD", cfg.triage_budget_usd),
                         ("review", "REVIEW"): Money.from_major("USD", cfg.review_budget_usd)})


def _default_route(runtime: RuntimeName, model: str | None) -> ModelRoute:
    """A role's default route. An invalid pair (validate() reports it) drops
    the model so describe() and the route accessors still answer."""
    try:
        return ModelRoute(runtime, (model or "").strip() or None)
    except ValueError:
        return ModelRoute(runtime, None)


def _route(spec: str) -> ModelRoute | None:
    try:
        return parse_model_route(spec) if spec.strip() else None
    except ValueError:
        return None


def rate_table(cfg: HarnessConfig, runtime_name: str) -> RateTable | None:
    """The one rate card the live budget kill, settlement and recovery price
    with. Codex carries its shipped per-model defaults and prices unnamed
    models at the scalar rates. Claude carries its list rates and prices no
    unnamed model: a CLI's fallback price for a model newer than it is never
    trusted (#389), so an unrated model blocks admission instead."""
    micros = usd_per_million_to_micros
    cached = dict(cfg.codex_cost_per_mtok_cached_in)
    codex = runtime_name == RuntimeName.CODEX.value
    try:
        if codex:
            fallback: ModelRates | None = ModelRates(
                micros(cfg.codex_cost_per_mtok_in), micros(cfg.codex_cost_per_mtok_out),
                micros(cfg.codex_cost_per_mtok_in / 10), micros(cfg.codex_cost_per_mtok_in))
            rates = {
                model: ModelRates(micros(rate_in), micros(rate_out),
                                  micros(cached.get(model, default_cache_read_rate(model, rate_in))),
                                  micros(default_cache_write_rate(model, rate_in)))
                for model, rate_in, rate_out in with_default_model_rates(cfg.codex_cost_per_mtok)
            }
        else:
            fallback = None
            # Operator rows win over the shipped list rates, so a new model is
            # priceable before the defaults catch up.
            rows = {model: (rate_in, rate_out, read) for model, rate_in, rate_out, read in CLAUDE_DEFAULT_MODEL_RATES}
            rows.update({model: (rate_in, rate_out, cached.get(model, default_cache_read_rate(model, rate_in)))
                         for model, rate_in, rate_out in cfg.codex_cost_per_mtok})
            rows.update({alias: rows[model] for alias, model in CLAUDE_ALIASES if alias not in rows})
            rates = {model: ModelRates(micros(rate_in), micros(rate_out), micros(read),
                                       micros(rate_in * CLAUDE_CACHE_WRITE_MULTIPLIER))
                     for model, (rate_in, rate_out, read) in rows.items()}
    except ValueError:
        return None
    # Shipped defaults change without an overlay edit, so a receipt names the
    # effective prices, not just the operator's input snapshot.
    version = hashlib.sha256(json.dumps({
        "config": cfg.config_version,
        "rates": {model.strip().casefold(): asdict(value) for model, value in rates.items()},
        "fallback": asdict(fallback) if fallback is not None else None,
    }, sort_keys=True).encode()).hexdigest()[:16]
    if fallback is not None:
        return RateTable("codex-config", version, "USD", rates, UnknownModelPolicy.USE_FALLBACK, fallback)
    return RateTable(f"{runtime_name}-list-rates", version, "USD", rates, UnknownModelPolicy.REJECT)


def supervision_dir(cfg: HarnessConfig, role: str) -> str:
    """Private process registry for one (repo, checkout, role) deployment."""
    root = cfg.state_dir_for(role)
    if not root:
        raise ValueError("process supervisor state directory is required")
    return os.path.join(root, "process-supervision", deployment_identity_for_target(cfg.repo, cfg.target_dir, role))


def _env_name(name: str) -> str:
    return str(_FIELDS[name].metadata.get("env") or name.upper())


def render_agent_md(text: str, cfg: HarnessConfig) -> str:
    """Expand the AGENT.md template's config variables: GPT models take a
    literal `$VAR` at face value (failure-modes #7)."""
    return text.replace("$BASE_BRANCH", cfg.base_branch).replace("$HARNESS_REPO", cfg.repo)


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


# Knobs folded into the route knobs. A set one is a startup error, never a
# silent no-op: the operator's intent would otherwise stop applying.
_REMOVED_KNOBS = {
    "CODEX_MODEL": "set FIX_MODEL (the default fixer's model on FIXER_RUNTIME) or a FIX_MODEL_<TIER> route",
    "FIX_EFFORT": "put the effort in the route: FIX_MODEL_<TIER>=runtime:model:effort",
}


def from_env(env: dict | None = None) -> HarnessConfig:
    """Build one immutable snapshot from the metadata-backed config table."""
    e = _authoritative_env(env if env is not None else os.environ)
    errors: list[str] = [f"{knob} was removed: {use} (unset it in the shell environment too, not only the env file)" for knob, use in _REMOVED_KNOBS.items()
                         if str(e.get(knob) or "").strip()]
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


# ── live knobs (ADR 0007 amendment) ──────────────────────────────────────────
# field name -> env name of every live knob.
LIVE_KNOBS: Mapping[str, str] = {item.name: _env_name(item.name) for item in fields(HarnessConfig)
                                 if item.metadata.get("live")}
LIVE_ENV_NAMES = frozenset(LIVE_KNOBS.values())


def with_live(boot: HarnessConfig, candidate: HarnessConfig) -> HarnessConfig:
    """`boot` with `candidate`'s live knobs: the snapshot a tick runs under.
    The candidate's parse errors come along, so a malformed value in the file
    invalidates it instead of silently reading as the default."""
    return replace(boot, parse_errors=candidate.parse_errors,
                   **{name: getattr(candidate, name) for name in LIVE_KNOBS})


def restart_part(path: str, inherited: Mapping[str, str], role: str) -> dict[str, str]:
    """What in an env file needs a restart: every assignment but a live
    knob's, and the runtimes `role` binds (a live route onto a runtime the
    process did not bind needs one), as `{name: value}`. The deploy manifest
    lists knob names, so a newly set live knob is dropped from it too."""
    assigned = source_env_file(path, inherited, assigned_only=True)
    kept = {key: value for key, value in assigned.items() if key not in LIVE_ENV_NAMES}
    if "HARNESS_DEPLOY_KNOBS" in kept:
        kept["HARNESS_DEPLOY_KNOBS"] = " ".join(sorted(set(kept["HARNESS_DEPLOY_KNOBS"].split()) - LIVE_ENV_NAMES))
    kept[f"({role} runtimes)"] = " ".join(sorted(name.value for name in from_env(assigned).runtimes(role)))
    return kept


def reload_fingerprint(path: str, inherited: Mapping[str, str], role: str) -> str:
    """The identity of `restart_part`: `ReloadWatch` restarts when it changes."""
    return hashlib.sha256(json.dumps(sorted(restart_part(path, inherited, role).items())).encode()).hexdigest()
