"""Dashboard application service: read, derive, shape.

Per deployment it reads the launch configuration the daemons loaded, the
observation the roles read (`GitHubGateway.build_world`) plus the two GitHub
lists that observation leaves out, and the control store's `legs` and
`leases` read-only. It derives every item with `domain.state.derive` over the
same local facts the roles build (`app.tick.Facts`), so the board shows what
the daemons will do, not a reconstruction.

The only writes are label changes through `operator_effects` (refactor
decision Q19): promote adds `ai-ready`, park adds `needs-human`, unblock
removes it.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ...adapters.control_path import control_store_path
from ...adapters.control_store import SQLiteControlStore
from ...adapters.dashweb import ghdata, system, usage, verification
from ...adapters.dashweb.legs import LegsUnavailable, read_legs
from ...adapters.github import GitHubGateway
from ...adapters.operator_effects import GitHubOperatorEffects, build_operator_effects
from ...domain import dashboard as dash
from ...domain.labels import Label
from ...domain.refs import harness_branch_issue
from ...domain.state import Harness, Local, derive
from ...domain.world import WorldState
from .. import config
from ..config import HarnessConfig
from ..tick import Facts
from . import views
from .ports import GitHubQueries

HARNESS_ROOT = str(Path(__file__).resolve().parents[4])
OBSERVE_TTL_S = 20.0
LEGS_TTL_S = 3.0
CONFIG_TTL_S = 60.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _detail(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:400]


def daemon_control_path(cfg: HarnessConfig) -> str:
    """The control store the deployment's daemons share (as `roles` opens it)."""
    return control_store_path((cfg.state_dir, cfg.review_state_dir), fallback_state_dir=cfg.state_dir,
                              authority_root=cfg.control_authority_root)


@dataclass(frozen=True)
class Deployment:
    name: str
    cfg: HarnessConfig
    harness: Harness | None
    control_path: str
    problem: str = ""


@dataclass(frozen=True)
class Observation:
    world: WorldState | None
    issues: object
    merged: object
    observed_at: datetime
    warnings: tuple[str, ...] = ()


@dataclass
class _Entry:
    value: Observation | None = None
    refreshed_at: float = 0.0
    future: Future | None = None
    stale: bool = False


@dataclass
class _Observations:
    """Stale-while-revalidate per deployment: a GitHub observation can take
    seconds, so a poll returns the last one (None before the first) and a
    refresh runs in the background once it is older than the TTL."""

    ttl_s: float = OBSERVE_TTL_S
    clock: Callable[[], float] = time.monotonic
    executor: ThreadPoolExecutor = field(
        default_factory=lambda: ThreadPoolExecutor(max_workers=4, thread_name_prefix="dashboard-observe"))
    _entries: dict[str, _Entry] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self, key: str, load: Callable[[], Observation]) -> Observation | None:
        now = self.clock()
        with self._lock:
            entry = self._entries.setdefault(key, _Entry())
            if entry.future is not None and entry.future.done():
                try:
                    entry.value = entry.future.result()
                except Exception as exc:  # noqa: BLE001 - a failed read degrades
                    entry.value = Observation(None, None, None, _utcnow(), (f"GitHub unavailable ({_detail(exc)})",))
                entry.refreshed_at, entry.future = now, None
            due = entry.stale or entry.value is None or now - entry.refreshed_at >= self.ttl_s
            if entry.future is None and due:
                entry.future, entry.stale = self.executor.submit(load), False
            return entry.value

    def cached(self, key: str) -> Observation | None:
        with self._lock:
            entry = self._entries.get(key)
            return entry.value if entry else None

    def invalidate(self, key: str) -> None:
        with self._lock:
            if key in self._entries:
                self._entries[key].stale = True


class Backend:
    """Every collector the API exposes."""

    def __init__(
        self,
        harness_root: str | None = None,
        gh: GitHubQueries | None = None,
        *,
        gateway_factory: Callable[[str], object] = GitHubGateway,
        now_fn: Callable[[], datetime] = _utcnow,
        effects_factory=build_operator_effects,
        environ: Mapping[str, str] | None = None,
        observe_ttl_s: float = OBSERVE_TTL_S,
        observe_executor: ThreadPoolExecutor | None = None,
        proc_scan: Callable[[], tuple[dict[str, set[str]], bool]] = system.scan_proc_daemons,
        control_path: Callable[[HarnessConfig], str] = daemon_control_path,
    ):
        self.harness_root = harness_root or HARNESS_ROOT
        self.gh = gh or ghdata.GhReader()
        self.gateway_factory = gateway_factory
        self.now_fn = now_fn
        self.effects_factory = effects_factory
        self.environ = dict(environ if environ is not None else os.environ)
        self.proc_scan = proc_scan
        self.control_path = control_path
        self._observations = _Observations(ttl_s=observe_ttl_s, **(
            {"executor": observe_executor} if observe_executor is not None else {}))
        self._configs = ghdata.TtlCache(ttl_s=CONFIG_TTL_S)
        self._legs = ghdata.TtlCache(ttl_s=LEGS_TTL_S)
        self._self_repo = system.remote_repo(self.harness_root)

    # ── configuration ──
    @property
    def repos_dir(self) -> str:
        return os.path.join(self.harness_root, "config", "repos")

    def names(self) -> tuple[str, ...]:
        return system.deployment_names(self.repos_dir)

    def known(self, name: str) -> bool:
        """A deployment the operator configured. Every route gates on this:
        `name` reaches filesystem paths and a subprocess environment."""
        return name in self.names()

    def deployment(self, name: str) -> Deployment:
        return self._configs.get_or_load(name, lambda: self._load_deployment(name))

    def _load_deployment(self, name: str) -> Deployment:
        cfg = config.from_env(system.deployment_env(self.harness_root, name, self.environ))
        problem = "" if cfg.repo else f"no REPO configured for deployment '{name}'"
        try:
            harness: Harness | None = Harness(fix_bot=cfg.fix_runtime.bot, review_bot=cfg.review_runtime.bot)
        except ValueError as exc:
            harness, problem = None, problem or f"harness identity incomplete: {exc}"
        return Deployment(name, cfg, harness, self.control_path(cfg), problem)

    # ── reads ──
    def legs(self, dep: Deployment) -> tuple[tuple[dash.LegRow, ...], list[str]]:
        try:
            return self._legs.get_or_load((dep.control_path, dep.cfg.repo),
                                          lambda: read_legs(dep.control_path, dep.cfg.repo)), []
        except LegsUnavailable as exc:
            return (), [str(exc)]

    def _observe(self, dep: Deployment) -> Observation:
        warnings: list[str] = []
        world: WorldState | None
        try:
            world = self.gateway_factory(dep.cfg.repo).build_world(dep.cfg.base_branch)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - shown as a warning, retried next TTL
            world = None
            warnings.append(f"GitHub observation failed ({_detail(exc)})")
        issues, issues_error = self.gh.issues(dep.cfg.repo)
        merged, merged_error = self.gh.merged_prs(dep.cfg.repo)
        warnings += [m for m in (issues_error, merged_error) if m]
        if isinstance(issues, list) and len(issues) >= ghdata.ISSUE_HISTORY_LIMIT:
            warnings.append(f"issue history limited to {ghdata.ISSUE_HISTORY_LIMIT} records")
        if isinstance(merged, list) and len(merged) >= ghdata.MERGED_PR_LIMIT:
            warnings.append(f"merged PRs limited to the newest {ghdata.MERGED_PR_LIMIT}; totals undercount")
        return Observation(world, issues, merged, self.now_fn(), tuple(warnings))

    def observation(self, dep: Deployment) -> Observation | None:
        if dep.problem:
            return None
        return self._observations.get(dep.name, lambda: self._observe(dep))

    def _facts(self, dep: Deployment, warnings: list[str]) -> Callable[[WorldState], Local]:
        """The roles' own local facts (`tick.Facts`) over the shared store,
        with the fix role's headroom and lifetime-cap arithmetic. The store
        is opened without `initialize()`: the dashboard never creates or cuts
        over the authority."""
        # Private to roles.py; reported to slice 9 as the seam to make public.
        from ..roles import _control_spend_gate, _headroom, _lifetime_capped

        control = SQLiteControlStore(dep.control_path)
        return Facts(
            control, dep.cfg.repo, active=tuple,
            headroom=_headroom(_control_spend_gate(dep.cfg, control, dep.cfg.fix_budget_usd)),
            lifetime_capped=_lifetime_capped(dep.cfg, control), log=warnings.append, now=self.now_fn,
        )

    def _items(self, dep: Deployment, obs: Observation | None, legs, warnings: list[str]) -> tuple[views.Item, ...] | None:
        if obs is None or obs.world is None or dep.harness is None:
            return None
        try:
            local = self._facts(dep, warnings)(obs.world)
        except Exception as exc:  # noqa: BLE001 - derive without local facts, loudly
            warnings.append(f"control store unreadable; running legs and caps are not shown ({_detail(exc)})")
            local = Local(now=self.now_fn())
        return views.items(obs.world, derive(obs.world, local, dep.harness), legs, dep.harness)

    def _read(self, name: str, wait: bool = False):
        """The deployment, its legs and its observation. `wait` reads GitHub
        now (the one-shot renders); polls take the cached observation."""
        dep = self.deployment(name)
        warnings = [dep.problem] if dep.problem else []
        legs, legs_warnings = self.legs(dep)
        obs = self._observe(dep) if wait and not dep.problem else self.observation(dep)
        warnings += legs_warnings + list(obs.warnings if obs else ())
        if obs is None and not dep.problem:
            warnings.append("reading GitHub…")
        return dep, legs, obs, warnings

    # ── endpoints ──
    def repos(self) -> dict:
        live, scan_ok = self.proc_scan()
        now = self.now_fn()
        out, warnings = [], []
        for name in self.names():
            try:
                dep = self.deployment(name)
            except Exception as exc:  # noqa: BLE001 - one bad overlay must not hide the rest
                warnings.append(f"{name}: configuration unreadable ({_detail(exc)})")
                continue
            legs, _ = self.legs(dep)
            stamps = [leg.at for leg in legs]
            obs = self._observations.cached(name)
            merged = views.merged_harness_prs(obs.merged) if obs else ()
            beats = system.role_heartbeats({"fix": dep.cfg.state_dir, "review": dep.cfg.review_state_dir})
            roles = [self._role(role, beats.get(role), live.get(name, set()), scan_ok, now) for role in dash.ROLE_NAMES]
            open_items = None
            if obs is not None and obs.world is not None:
                open_items = len({i.number for i in obs.world.issues if i.state == "open" and i.has(Label.AI_READY)} | {
                    n for p in obs.world.prs if p.state == "OPEN" and (n := harness_branch_issue(p.head_ref_name))})
            out.append({
                "name": name,
                "repo": dep.cfg.repo or None,
                "target_dir": dep.cfg.target_dir or None,
                "is_self": bool(self._self_repo) and dep.cfg.repo.casefold() == self._self_repo.casefold(),
                "active": (name in live) if scan_ok else None,
                "roles": roles,
                "frecency": dash.frecency(stamps, now),
                "last_activity_ts": max(stamps).isoformat() if stamps else None,
                "in_flight": open_items,
                "done": len({m.issue for m in merged}) if obs is not None and obs.merged is not None else None,
            })
        top = max((repo["frecency"] for repo in out), default=0.0)
        for repo in out:
            repo["frecency"] = round(repo["frecency"] / top, 4) if top > 0 else 0.0
        out.sort(key=lambda repo: (-repo["frecency"], repo["name"]))
        return {"repos": out, "warnings": warnings}

    @staticmethod
    def _role(role: str, beat: datetime | None, live: set[str], scan_ok: bool, now: datetime) -> dict:
        age = (now - beat).total_seconds() if beat is not None else None
        return {
            "role": role,
            "alive": (role in live) if scan_ok else None,
            "stale": age is None or not 0 <= age <= dash.STALE_AFTER_S,
            "heartbeat_ts": beat.isoformat() if beat is not None else None,
        }

    def usage(self) -> dict:
        def config_for(name: str) -> tuple[str, float, float]:
            cfg = self.deployment(name).cfg
            return cfg.repo, cfg.daily_spend_cap_usd, cfg.global_daily_spend_cap_usd

        return usage.usage_payload(self.names(), config_for)

    def verification(self) -> dict:
        return verification.verification_payload(verification.default_path(self.harness_root))

    def issues(self, name: str, wait: bool = False) -> dict:
        dep, legs, obs, warnings = self._read(name, wait)
        found = self._items(dep, obs, legs, warnings)
        gh_issues = ghdata.index_by_number(obs.issues) if obs else {}
        merged = views.merged_harness_prs(obs.merged) if obs else ()
        active = {item.issue for item in found or ()}
        now = self.now_fn()
        return {
            "in_flight": [views.issue_card(item, dep.cfg.repo, gh_issues.get(item.issue)) for item in found or ()],
            "done": views.done_cards(merged, legs, dep.cfg.repo, gh_issues),
            "backlog": views.backlog_cards(gh_issues, active, now) if obs and obs.issues is not None else [],
            "gh_ok": obs is not None and not obs.warnings and found is not None,
            "observed_at": obs.observed_at.isoformat() if obs else None,
            "notifications": [asdict(n) for n in dash.notifications(
                legs, ((item.issue, item.status) for item in found or () if isinstance(item.status, dash.NeedsYou)))],
            "warnings": warnings,
        }

    def issue(self, name: str, number: int) -> dict | None:
        dep, legs, obs, warnings = self._read(name)
        found = self._items(dep, obs, legs, warnings)
        item = next((i for i in found or () if i.issue == number), None)
        merged = views.merged_harness_prs(obs.merged) if obs else ()
        gh_issue = ghdata.index_by_number(obs.issues).get(number) if obs else None
        if obs is not None and item is None and gh_issue is None and not any(
                leg.issue == number for leg in legs) and not any(m.issue == number for m in merged):
            return None
        detail = views.issue_detail(number, dep.cfg.repo, item, legs, merged, gh_issue)
        detail["warnings"] = warnings
        return detail

    def overview(self, name: str, timezone_name: str | None = None) -> dict:
        dep, legs, obs, warnings = self._read(name)
        found = self._items(dep, obs, legs, warnings)
        live, scan_ok = self.proc_scan()
        now = self.now_fn()
        beats = system.role_heartbeats({"fix": dep.cfg.state_dir, "review": dep.cfg.review_state_dir})
        merged = views.merged_harness_prs(obs.merged) if obs else ()
        gh_issues = ghdata.index_by_number(obs.issues) if obs else {}
        needs = views.attention(found or (), gh_issues)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)  # the caps' UTC day
        m = dash.metrics(
            legs, merged_prs=len({x.pr for x in merged}) if obs and obs.merged is not None else None,
            open_items=len(found) if found is not None else None,
            needs_human=len(needs) if found is not None else None, today_start=today,
        )
        cfg = dep.cfg
        return {
            "metrics": asdict(m),
            "caps": {
                "issue_cap_usd": cfg.issue_spend_cap_usd or None,
                "daily_cap_usd": cfg.daily_spend_cap_usd or None,
                "global_daily_cap_usd": cfg.global_daily_spend_cap_usd or None,
                "today_usd": m.today_usd,
            },
            "health": [self._role(role, beats.get(role), live.get(name, set()), scan_ok, now)
                       for role in dash.ROLE_NAMES],
            "needs_attention": needs,
            "health_trend": views.health_trend(obs.issues, _local_timezone(timezone_name)) if obs else [],
            "spend_per_hour": [asdict(b) for b in dash.spend_per_hour(legs, now)],
            "warnings": warnings,
        }

    # ── label writes ──
    def promote(self, name: str, number: int) -> tuple[bool, str]:
        return self._write(name, lambda effects: effects.set_issue_labels(number, add=("ai-ready",)))

    def park(self, name: str, number: int, channel: str = "api") -> tuple[bool, str]:
        """Hand an issue to a human: `needs-human` on the issue and its open
        harness PRs. Derive waits on the label until it is removed or a human
        replies to a stop."""
        return self._write(name, lambda effects: effects.move_hold(number, park=True, channel=channel))

    def unblock(self, name: str, number: int, channel: str = "api") -> tuple[bool, str]:
        """Remove `needs-human` from the issue and its open harness PRs."""
        return self._write(name, lambda effects: effects.move_hold(number, park=False, channel=channel))

    def _write(self, name: str, apply: Callable[[GitHubOperatorEffects], tuple[bool, str]]) -> tuple[bool, str]:
        if not self.known(name):
            return False, f"unknown deployment '{name}'"
        repo = self.deployment(name).cfg.repo
        if not repo:
            return False, f"no REPO configured for deployment '{name}'"
        result = apply(self.effects_factory(repo, identity=f"{repo}:dashboard"))
        self._observations.invalidate(name)
        return result


def _local_timezone(name: str | None):
    if name and len(name) <= 100:
        try:
            return ZoneInfo(name)
        except (ValueError, ZoneInfoNotFoundError):
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc
