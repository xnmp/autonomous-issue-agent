"""Dashboard application service: read, derive, shape.

Per deployment it reads the launch configuration the daemons loaded, the
observation the roles read (`GitHubGateway.build_world`) plus the two GitHub
lists that observation leaves out, and the control store through one
read-only connection per request (`ReadOnlyControlStore`). It derives every
item with `domain.state.derive` over the local facts each role builds
(`app.tick.Facts`, with that role's leg price), so the board shows what the
daemons will do, not a reconstruction. An unreadable store derives nothing:
the board says the store is unavailable rather than guess.

The only writes are label changes through `operator_effects` (refactor
decision Q19): promote adds `ai-ready`, unblock removes `needs-human`.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ...adapters.control_path import control_store_path
from ...adapters.control_store import ControlStoreError, Lease
from ...adapters.dashweb.readonly_store import ReadOnlyControlStore
from ...adapters.dashweb import ghdata, system, usage, verification
from ...adapters.dashweb.legs import read_legs
from ...adapters.github import GitHubGateway
from ...adapters.operator_effects import GitHubOperatorEffects, build_operator_effects
from ...domain import dashboard as dash
from ...domain.backoff import LegEnd
from ...domain.contracts import daily_budget_scope
from ...domain.repository import canonical_repository
from ...domain.labels import Label
from ...domain.state import DerivedState, Harness, ItemKey, derive
from ...domain.world import WorldState
from .. import config
from ..config import HarnessConfig
from ..tick import Facts, headroom, lifetime_capped
from . import views
from .ports import GitHubQueries

HARNESS_ROOT = str(Path(__file__).resolve().parents[4])
OBSERVE_TTL_S = 20.0
CONFIG_TTL_S = 60.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _detail(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:400]


def daemon_control_path(cfg: HarnessConfig) -> str:
    """The control store the deployment's daemons share (as `tick` opens it)."""
    return control_store_path((cfg.state_dir, cfg.review_state_dir), fallback_state_dir=cfg.state_dir,
                              authority_root=cfg.control_authority_root)


def _role_facts(cfg: HarnessConfig, store: ReadOnlyControlStore, leg_usd: float,
                now: Callable[[], datetime]) -> Facts:
    """The local facts one role builds, priced at its leg (`tick` wiring).
    Nothing is in flight in this process; leases come from the store."""
    return Facts(store, cfg.repo, active=tuple, headroom=headroom(cfg, store, leg_usd),
                 lifetime_capped=lifetime_capped(cfg, store), log=_unlogged, now=now)


def _unlogged(_message: str) -> None:
    """`Facts` logs an unreadable lifetime spend; the board already shows its
    effect (paid legs wait on headroom), and the daemons log it themselves."""


@dataclass(frozen=True)
class Deployment:
    name: str
    cfg: HarnessConfig
    harness: Harness | None
    control_path: str
    problem: str = ""
    store_problem: str = ""  # the control path could not be resolved


@dataclass(frozen=True)
class Caps:
    """Today's committed spend (settled + reserved, by `reserved_at` in the
    UTC day) for the repository and for every repository: the snapshots the
    core's `tick.headroom` compares against the caps. `headroom` is whether
    each role's next leg fits, as that role decides it. None is unreadable."""

    repository_usd: float | None
    global_usd: float | None
    headroom: Mapping[str, bool]


@dataclass(frozen=True)
class StoreRead:
    """One request's read of the control store: the repository's legs, its
    leases, its caps and, when there is a world to derive over, each item's
    state as its role sees it. `problem` set means nothing was read."""

    legs: tuple[dash.LegRow, ...] = ()
    states: Mapping[ItemKey, DerivedState] | None = None
    item_legs: Mapping[ItemKey, tuple[LegEnd, ...]] = field(default_factory=dict)
    leases: tuple[Lease, ...] = ()
    caps: Caps | None = None
    problem: str = ""


@dataclass(frozen=True)
class Observation:
    """`failed` is a GitHub read that failed; truncation notices are only
    warnings (`merged_partial` marks the merged-PR list's)."""

    world: WorldState | None
    issues: object
    merged: object
    observed_at: datetime
    warnings: tuple[str, ...] = ()
    failed: bool = False
    merged_partial: bool = False


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
            self._harvest(entry, now)
            due = entry.stale or entry.value is None or now - entry.refreshed_at >= self.ttl_s
            if entry.future is None and due:
                entry.future, entry.stale = self.executor.submit(load), False
            return entry.value

    def cached(self, key: str) -> Observation | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            self._harvest(entry, self.clock())
            return entry.value

    @staticmethod
    def _harvest(entry: _Entry, now: float) -> None:
        if entry.future is None or not entry.future.done():
            return
        try:
            entry.value = entry.future.result()
        except Exception as exc:  # noqa: BLE001 - a failed read degrades
            entry.value = Observation(None, None, None, _utcnow(), (f"GitHub unavailable ({_detail(exc)})",),
                                      failed=True)
        entry.refreshed_at, entry.future = now, None

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
            harness: Harness | None = Harness(fix_bot=cfg.fix_bot, review_bot=cfg.review_bot)
        except ValueError as exc:
            harness, problem = None, problem or f"harness identity incomplete: {exc}"
        try:
            path, store_problem = self.control_path(cfg), ""
        except Exception as exc:  # noqa: BLE001 - shown as "store unavailable"; GitHub and writes still work
            path, store_problem = "", f"control store path unresolved ({_detail(exc)})"
        return Deployment(name, cfg, harness, path, problem, store_problem)

    # ── reads ──
    def store(self, dep: Deployment, world: WorldState | None = None) -> StoreRead:
        """One read-only connection: the legs and leases, and with a world
        each item's derived state and the caps. Any failure is "store
        unavailable", never a guess."""
        if dep.store_problem:
            return StoreRead(problem=dep.store_problem)
        try:
            with ReadOnlyControlStore(dep.control_path) as store:
                legs = read_legs(store, dep.cfg.repo)
                repository = canonical_repository(dep.cfg.repo) if dep.cfg.repo else ""
                leases = tuple(lease for lease in store.leases() if lease.repository == repository)
                facts = {role: _role_facts(dep.cfg, store, usd, self.now_fn)
                         for role, usd in (("fix", dep.cfg.fix_budget_usd), ("review", dep.cfg.review_budget_usd))}
                caps = self._caps(dep.cfg, store, facts)
                if world is None or dep.harness is None:
                    return StoreRead(legs, leases=leases, caps=caps)
                fix, review = facts["fix"](world), facts["review"](world)
                states = dash.role_states(derive(world, fix, dep.harness), derive(world, review, dep.harness))
                return StoreRead(legs, states, fix.legs, leases, caps)
        except (ControlStoreError, sqlite3.Error, OSError) as exc:
            return StoreRead(problem=f"control store unavailable: {_detail(exc)}")

    def _caps(self, cfg: HarnessConfig, store: ReadOnlyControlStore, facts: Mapping[str, Facts]) -> Caps:
        now = self.now_fn()

        def committed(repository: str, dimension: str) -> float | None:
            snapshot = store.snapshot(daily_budget_scope(repository, dimension, "USD", now, ZoneInfo("UTC")))
            return snapshot.committed.micros / 1e6 if snapshot.disposition.value == "available" else None

        return Caps(committed(cfg.repo, "repository") if cfg.repo else None, committed("*", "global"),
                    {role: role_facts.headroom() for role, role_facts in facts.items()})

    def legs(self, dep: Deployment) -> tuple[tuple[dash.LegRow, ...], list[str]]:
        read = self.store(dep)
        return read.legs, [read.problem] if read.problem else []

    def _observe(self, dep: Deployment) -> Observation:
        warnings: list[str] = []
        world: WorldState | None
        failed = False
        try:
            world = self.gateway_factory(dep.cfg.repo).build_world(dep.cfg.base_branch)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - shown as a warning, retried next TTL
            world, failed = None, True
            warnings.append(f"GitHub observation failed ({_detail(exc)})")
        issues, issues_error = self.gh.issues(dep.cfg.repo)
        merged, merged_error = self.gh.merged_prs(dep.cfg.repo)
        errors = [m for m in (issues_error, merged_error) if m]
        warnings += errors
        if isinstance(issues, list) and len(issues) >= ghdata.ISSUE_HISTORY_LIMIT:
            warnings.append(f"issue history limited to {ghdata.ISSUE_HISTORY_LIMIT} records")
        partial = isinstance(merged, list) and len(merged) >= ghdata.MERGED_PR_LIMIT
        if partial:
            warnings.append(f"merged PRs limited to the newest {ghdata.MERGED_PR_LIMIT}; totals undercount")
        return Observation(world, issues, merged, self.now_fn(), tuple(warnings), failed or bool(errors), partial)

    def observation(self, dep: Deployment) -> Observation | None:
        if dep.problem:
            return None
        return self._observations.get(dep.name, lambda: self._observe(dep))

    def _items(self, dep: Deployment, obs: Observation | None, read: StoreRead) -> tuple[views.Item, ...] | None:
        if obs is None or obs.world is None or dep.harness is None or read.states is None:
            return None
        return views.items(obs.world, read.states, read.legs, dep.harness, read.item_legs)

    def _read(self, name: str, wait: bool = False):
        """The deployment, its observation and one store read. `wait` reads
        GitHub now (the one-shot renders); polls take the cached observation."""
        dep = self.deployment(name)
        warnings = [dep.problem] if dep.problem else []
        obs = self._observe(dep) if wait and not dep.problem else self.observation(dep)
        read = self.store(dep, obs.world if obs else None)
        warnings += list(obs.warnings if obs else ())
        if obs is None and not dep.problem:
            warnings.append("reading GitHub…")
        return dep, obs, read, warnings

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
            obs = self._observations.cached(name)
            read = self.store(dep, obs.world if obs else None)
            stamps = [leg.at for leg in read.legs]
            merged = views.merged_harness_prs(obs.merged) if obs else ()
            beats = system.role_beats(_state_dirs(dep.cfg))
            roles = [self._role(role, beats.get(role), live.get(name, set()), scan_ok, now) for role in dash.ROLE_NAMES]
            # The board's own rows, so the sidebar and the board agree.
            open_items = (len(views.board_rows(obs.world, read.states))
                          if obs is not None and obs.world is not None and read.states is not None else None)
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
    def _role(role: str, beat: system.Beat | None, live: set[str], scan_ok: bool, now: datetime) -> dict:
        """A role's health: its process (the process table) and its last
        heartbeat. A heartbeat in a terminal state, or older than the bound,
        is stale."""
        age = (now - beat.at).total_seconds() if beat is not None else None
        return {
            "role": role,
            "alive": (role in live) if scan_ok else None,
            "stale": beat is None or beat.state not in ("running", "draining")
            or age is None or not 0 <= age <= dash.STALE_AFTER_S,
            "heartbeat_ts": beat.at.isoformat() if beat is not None else None,
            "state": beat.state if beat is not None else None,
            "in_flight": [{"kind": kind, "number": number} for kind, number in beat.in_flight] if beat else [],
        }

    def usage(self) -> dict:
        def config_for(name: str) -> tuple[str, float, float]:
            cfg = self.deployment(name).cfg
            return cfg.repo, cfg.daily_spend_cap_usd, cfg.global_daily_spend_cap_usd

        return usage.usage_payload(self.names(), config_for)

    def verification(self) -> dict:
        return verification.verification_payload(verification.default_path(self.harness_root))

    def issues(self, name: str, wait: bool = False) -> dict:
        dep, obs, read, warnings = self._read(name, wait)
        legs = read.legs
        found = self._items(dep, obs, read)
        gh_issues = ghdata.index_by_number(obs.issues) if obs else {}
        merged = views.merged_harness_prs(obs.merged) if obs else ()
        active = {item.issue for item in found or ()}
        now = self.now_fn()
        return {
            "in_flight": [views.issue_card(item, dep.cfg.repo, gh_issues.get(item.issue)) for item in found or ()],
            "done": views.done_cards(merged, legs, dep.cfg.repo, gh_issues),
            "backlog": views.backlog_cards(gh_issues, active, now) if obs and obs.issues is not None else [],
            "gh_ok": obs is None or not obs.failed,
            "observed_at": obs.observed_at.isoformat() if obs else None,
            "notifications": [asdict(n) for n in dash.notifications(
                legs, ((item.issue, item.status) for item in found or () if isinstance(item.status, dash.NeedsYou)))],
            "warnings": warnings,
            "store_error": read.problem or None,
        }

    def issue(self, name: str, number: int) -> dict | None:
        dep, obs, read, warnings = self._read(name)
        models = system.leg_models(_state_dirs(dep.cfg))
        legs = read.legs
        found = self._items(dep, obs, read)
        item = next((i for i in found or () if i.issue == number), None)
        merged = views.merged_harness_prs(obs.merged) if obs else ()
        gh_issue = ghdata.index_by_number(obs.issues).get(number) if obs else None
        if obs is not None and item is None and gh_issue is None and not any(
                leg.issue == number for leg in legs) and not any(m.issue == number for m in merged):
            return None
        detail = views.issue_detail(number, dep.cfg.repo, item, legs, merged, gh_issue, models)
        detail["warnings"] = warnings
        detail["store_error"] = read.problem or None
        return detail

    def overview(self, name: str, timezone_name: str | None = None) -> dict:
        dep, obs, read, warnings = self._read(name)
        legs = read.legs
        found = self._items(dep, obs, read)
        live, scan_ok = self.proc_scan()
        now = self.now_fn()
        beats = system.role_beats(_state_dirs(dep.cfg))
        merged = views.merged_harness_prs(obs.merged) if obs else ()
        gh_issues = ghdata.index_by_number(obs.issues) if obs else {}
        needs = views.attention(found or (), gh_issues)
        m = dash.metrics(
            legs, merged_prs=len({x.pr for x in merged}) if obs and obs.merged is not None else None,
            open_items=len(found) if found is not None else None,
            needs_human=len(needs) if found is not None else None,
            merged_prs_partial=bool(obs and obs.merged_partial),
        )
        cfg = dep.cfg
        caps = read.caps
        return {
            "metrics": asdict(m),
            "caps": {
                "issue_cap_usd": cfg.issue_spend_cap_usd or None,
                "daily_cap_usd": cfg.daily_spend_cap_usd or None,
                "global_daily_cap_usd": cfg.global_daily_spend_cap_usd or None,
                "today_usd": _round(caps.repository_usd) if caps else None,
                "global_today_usd": _round(caps.global_usd) if caps else None,
                "headroom": [{"role": role, "leg_usd": usd, "fits": caps.headroom.get(role) if caps else None}
                             for role, usd in (("fix", cfg.fix_budget_usd), ("review", cfg.review_budget_usd))],
            },
            "health": [self._role(role, beats.get(role), live.get(name, set()), scan_ok, now)
                       for role in dash.ROLE_NAMES],
            "leases": views.lease_rows(read.leases, legs),
            "needs_attention": needs,
            "health_trend": views.health_trend(obs.issues, _local_timezone(timezone_name)) if obs else [],
            "spend_per_hour": [asdict(b) for b in dash.spend_per_hour(legs, now)],
            "warnings": warnings,
            "store_error": read.problem or None,
        }

    # ── label writes ──
    def promote(self, name: str, number: int) -> tuple[bool, str]:
        """Add `ai-ready` to an open issue the last observation listed (the
        backlog the operator promoted from); a PR or a closed issue is refused."""
        obs = self._observations.cached(name) if self.known(name) else None
        row = ghdata.index_by_number(obs.issues).get(number) if obs is not None else None
        if self.known(name) and (row is None or not ghdata.is_open(row)):
            return False, f"#{number} is not an open issue in the last GitHub read"
        return self._write(name, lambda effects: effects.set_issue_labels(number, add=(Label.AI_READY.value,)))

    def unblock(self, name: str, number: int) -> tuple[bool, str]:
        """Remove `needs-human` from the issue and its open harness PRs
        (`move_hold` reads both fresh and refuses a closed issue)."""
        return self._write(name, lambda effects: effects.move_hold(number, park=False, channel="dashboard"))

    def _write(self, name: str, apply: Callable[[GitHubOperatorEffects], tuple[bool, str]]) -> tuple[bool, str]:
        if not self.known(name):
            return False, f"unknown deployment '{name}'"
        repo = self.deployment(name).cfg.repo
        if not repo:
            return False, f"no REPO configured for deployment '{name}'"
        result = apply(self.effects_factory(repo, identity=f"{repo}:dashboard"))
        self._observations.invalidate(name)
        return result


def _state_dirs(cfg: HarnessConfig) -> dict[str, str]:
    return {"fix": cfg.state_dir, "review": cfg.review_state_dir}


def _round(usd: float | None) -> float | None:
    return round(usd, 6) if usd is not None else None


def _local_timezone(name: str | None):
    if name and len(name) <= 100:
        try:
            return ZoneInfo(name)
        except (ValueError, ZoneInfoNotFoundError):
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc
