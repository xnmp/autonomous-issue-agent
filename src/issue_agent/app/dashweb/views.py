"""Shape the dashboard's payloads from one observation, its derived states
and the legs. No I/O: `service.Backend` reads, this assembles.

Every board status comes from `domain.state.derive` (via
`domain.dashboard.board_status`); nothing here decides lifecycle.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, tzinfo
from types import MappingProxyType

from ...adapters.control_store import Lease
from ...domain import dashboard as dash
from ...domain.actions import ISSUE_ACTS, AgentKind, Target
from ...domain.backoff import LegEnd
from ...domain.checks import BaseHealth
from ...domain.ci import RED_CONCLUSIONS, base_verdicts, parse_timestamp, workflow_issues
from ...domain.labels import Label
from ...domain.refs import harness_branch_issue
from ...domain.state import DerivedState, Harness, ItemKey
from ...domain.triage import Steward, Triage
from ...domain.world import IssueView, PRView, WorldState
from ...adapters.dashweb.ghdata import index_by_number, is_open, labels_of

_EXCERPT = dash.EXCERPT_CHARS
_ISSUE_KINDS = frozenset(kind.name for kind in ISSUE_ACTS)


def issue_url(repo: str, number: int) -> str:
    return f"https://github.com/{repo}/issues/{number}"


def pr_url(repo: str, number: int) -> str:
    return f"https://github.com/{repo}/pull/{number}"


def board_payload(status: dash.BoardStatus, repo: str) -> dict:
    """A board status as the API carries it: a blocked wait's headline links to its blocker."""
    out = asdict(status)
    if isinstance(status, dash.Waiting):
        blocker = status.blocker
        del out["blocker"]
        out["headline_url"] = issue_url(blocker.repository or repo, blocker.number) if blocker else None
    return out


def _iso(stamp: datetime | None) -> str | None:
    return stamp.astimezone(timezone.utc).isoformat() if stamp is not None else None


@dataclass(frozen=True)
class Item:
    """One harness item, by its issue: what derive decided and what it saw."""

    issue: int
    key: ItemKey
    state: DerivedState
    issue_view: IssueView | None
    pr: PRView | None
    status: dash.BoardStatus
    legs: tuple[dash.LegRow, ...]

    @property
    def held(self) -> bool:
        labels = (self.issue_view.labels if self.issue_view else frozenset()) | (
            self.pr.labels if self.pr else frozenset())
        return Label.NEEDS_HUMAN in labels


def board_rows(world: WorldState, keys: Iterable[ItemKey]) -> dict[int, tuple[ItemKey, PRView | None]]:
    """Derive's items grouped by issue: the board's rows, and every count of
    them. An open PR shadows its issue in derive; with two open harness PRs
    for one issue the newest is the row."""
    prs = {p.number: p for p in world.prs if p.state == "OPEN"}
    chosen: dict[int, tuple[ItemKey, PRView | None]] = {}
    for key in sorted(keys, key=lambda k: k.sort_key):
        pr = prs.get(key.number) if key.target is Target.PR else None
        issue = harness_branch_issue(pr.head_ref_name) if pr is not None else key.number
        if issue is None:
            continue
        current = chosen.get(issue)
        if current is None or current[1] is None or (pr is not None and pr.number > current[1].number):
            chosen[issue] = (key, pr)
    return dict(sorted(chosen.items()))


def items(
    world: WorldState, states: Mapping[ItemKey, DerivedState], legs: Iterable[dash.LegRow], harness: Harness,
    item_legs: Mapping[ItemKey, tuple[LegEnd, ...]], now: datetime,
    reload_held: Mapping[str, tuple[int, ...]] = MappingProxyType({}),
) -> tuple[Item, ...]:
    """One `Item` per board row. `item_legs` are the legs derive keyed to
    each item (`Local.legs`), whose newest end bounds the item's episode;
    `reload_held` is `dash.board_status`'s."""
    issues = {i.number: i for i in world.issues}
    rows = tuple(legs)
    out = []
    for issue, (key, pr) in board_rows(world, states).items():
        own = dash.issue_legs(rows, issue)
        view = issues.get(issue)
        comments = pr.comments if pr is not None else (view.comments if view else ())
        last_end = max((leg.ended_at for leg in item_legs.get(key, ())), default=None)
        status = dash.board_status(states[key], target="pr" if pr is not None else "issue", comments=comments,
                                   pr=pr, legs=own, last_end=last_end, harness=harness, now=now,
                                   reload_held=reload_held,
                                   ready_at=parse_timestamp(view.ai_ready_labeled_at) if view else None)
        out.append(Item(issue, key, states[key], view, pr, status, own))
    return tuple(out)


def _labels(item: Item, gh_issue: Mapping | None) -> list[str]:
    names = set(labels_of(dict(gh_issue))) if gh_issue else set()
    for labels in (item.issue_view.labels if item.issue_view else (), item.pr.labels if item.pr else ()):
        names.update(label.value for label in labels)
    return sorted(names)


def _title(issue: int, item_view: IssueView | None, gh_issue: Mapping | None, pr: PRView | None) -> str | None:
    for candidate in ((item_view.title if item_view else ""), str((gh_issue or {}).get("title") or ""),
                      (pr.title if pr else "")):
        if candidate:
            return candidate
    return None


def _spend(legs: Iterable[dash.LegRow]) -> tuple[float, bool]:
    counted = [leg for leg in legs if leg.counted]
    return round(sum(leg.cost_usd for leg in counted), 6), any(leg.estimated for leg in counted)


def issue_card(item: Item, repo: str, gh_issue: Mapping | None, models: Mapping[str, str] | None = None) -> dict:
    cost, estimated = _spend(item.legs)
    counted = [leg for leg in item.legs if leg.counted]
    triage = dash.issue_triage(item.legs)
    # The running leg's model, else the latest leg's; the legs table has the runtime only.
    shown = next((leg for leg in item.legs if leg.running), None) or max(counted, key=lambda leg: leg.at, default=None)
    routed = max((leg for leg in counted if leg.kind != AgentKind.TRIAGE.name and leg.tier),
                 key=lambda leg: leg.at, default=None)
    return {
        "issue": item.issue,
        "title": _title(item.issue, item.issue_view, gh_issue, item.pr),
        "url": issue_url(repo, item.issue),
        "labels": _labels(item, gh_issue),
        "pr": item.pr.number if item.pr else None,
        "pr_url": pr_url(repo, item.pr.number) if item.pr else None,
        "pr_ci_state": dash.ci_display(item.pr),
        "board": board_payload(item.status, repo),
        "action": dash.primary_action(item.status, held=item.held),
        "rail": asdict(dash.rail(item.state, has_pr=item.pr is not None, legs=item.legs)),
        "history": [asdict(visit) for visit in dash.history(item.legs)],
        "cost_usd": cost,
        "estimated": estimated,
        "legs": len(counted),
        "started_ts": _iso(counted[0].reserved_at) if counted else None,
        "last_ts": _iso(max(leg.at for leg in counted)) if counted else None,
        "leg_running": any(leg.running for leg in item.legs),
        "triage_tier": triage.tier.value if triage else None,
        "routed_tier": routed.tier if routed and (triage is None or routed.tier != triage.tier.value) else None,
        "model": (models or {}).get(shown.leg_id) if shown else None,
        "runtime": shown.runtime or None if shown else None,
    }


@dataclass(frozen=True)
class Merged:
    issue: int
    pr: int
    title: str
    url: str
    merged_at: str


def merged_harness_prs(payload: object) -> tuple[Merged, ...]:
    """Merged harness PRs (`fix/<issue>-`) from `gh pr list --state merged`,
    newest first."""
    out = []
    for number, row in index_by_number(payload).items():
        issue = harness_branch_issue(str(row.get("headRefName") or ""))
        if issue is not None:
            out.append(Merged(issue, number, str(row.get("title") or ""), str(row.get("url") or ""),
                              str(row.get("mergedAt") or "")))
    return tuple(sorted(out, key=lambda m: (parse_timestamp(m.merged_at) is not None, m.merged_at, m.pr),
                        reverse=True))


def done_cards(merged: Iterable[Merged], legs: Iterable[dash.LegRow], repo: str,
               gh_issues: Mapping[int, Mapping]) -> list[dict]:
    rows = tuple(legs)
    seen: set[int] = set()
    cards = []
    for m in merged:
        if m.issue in seen:
            continue
        seen.add(m.issue)
        own = dash.issue_legs(rows, m.issue)
        cost, _ = _spend(own)
        gh_issue = gh_issues.get(m.issue)
        cards.append({
            "issue": m.issue,
            "title": str((gh_issue or {}).get("title") or "") or m.title or None,
            "url": issue_url(repo, m.issue),
            "pr": m.pr,
            "pr_url": m.url or pr_url(repo, m.pr),
            "merged_ts": m.merged_at or None,
            "cost_usd": cost,
            "legs": sum(1 for leg in own if leg.counted),
            "review_rounds": sum(1 for leg in own if leg.counted and leg.kind == "REVIEW"),
            "labels": labels_of(dict(gh_issue)) if gh_issue else [],
        })
    return cards


_HELD = frozenset({Label.AI_READY.value, Label.NEEDS_HUMAN.value})


def backlog_cards(gh_issues: Mapping[int, Mapping], active: set[int], now: datetime) -> list[dict]:
    """Open issues the harness was not asked to work on."""
    cards = []
    for number, issue in sorted(gh_issues.items()):
        labels = labels_of(dict(issue))
        if not is_open(dict(issue)) or number in active or _HELD & set(labels):
            continue
        created = parse_timestamp(str(issue.get("createdAt") or ""))
        age = max(0, int((now - created).total_seconds())) if created else None
        comments = issue.get("comments")
        cards.append({
            "issue": number,
            "title": issue.get("title"),
            "url": issue.get("url"),
            "body": issue.get("body"),
            "labels": labels,
            "age_days": age // 86400 if age is not None else None,
            "age_s": age,
            "comments": len(comments) if isinstance(comments, list) else comments if type(comments) is int else None,
            "issue_type": dash.label_value(labels, ("type:",), exact=("bug", "feature")),
            "difficulty": dash.label_value(labels, ("difficulty:", "effort:")),
            "impact": dash.label_value(labels, ("impact:",)),
        })
    return cards


def step(idx: int, leg: dash.LegRow, model: str = "") -> dict:
    duration = ((leg.ended_at - leg.started_at).total_seconds()
                if leg.ended_at is not None and leg.started_at is not None else None)
    return {
        "idx": idx,
        "leg_id": leg.leg_id,
        "kind": leg.kind,
        "role": leg.role or None,
        "runtime": leg.runtime or None,
        "model": model or None,
        "tier": leg.tier or None,
        "state": leg.state,
        "outcome": leg.outcome,
        "started_ts": _iso(leg.started_at or leg.reserved_at),
        "ended_ts": _iso(leg.ended_at),
        "duration_s": max(0.0, duration) if duration is not None else None,
        "cost_usd": round(leg.cost_usd, 6),
        "estimated": leg.estimated,
        "pricing_basis": leg.basis or None,
        "end_reason": leg.end_reason or None,
        "session_id": leg.session_id or None,
        "usage": asdict(leg.tokens) if leg.tokens else None,
        "head": leg.head or None,
        "pr": leg.number if leg.kind not in _ISSUE_KINDS and leg.number > 0 else None,
        "summary": dash.bounded(leg.summary, _EXCERPT) or None,
        "question": dash.bounded(leg.question, dash.MESSAGE_CHARS) or None,
        "verdict": leg.verdict or None,
        "verdict_excerpt": dash.bounded(leg.verdict_body, _EXCERPT) or None,
    }


def issue_detail(
    number: int, repo: str, item: Item | None, legs: Iterable[dash.LegRow], merged: Iterable[Merged],
    gh_issue: Mapping | None, models: Mapping[str, str] | None = None,
) -> dict:
    """The issue page. Carried legs (pre-cutover money history, legacy kinds)
    are one `carried` total, not steps: no act of the design ran them."""
    own = dash.issue_legs(legs, number)
    carried = [leg for leg in own if leg.carried and leg.counted]
    merges = [m for m in merged if m.issue == number]
    cost, estimated = _spend(own)
    pulls = []
    if item is not None and item.pr is not None:
        pulls.append({"number": item.pr.number, "url": pr_url(repo, item.pr.number), "state": "open",
                      "ci_state": dash.ci_display(item.pr)})
    pulls += [{"number": m.pr, "url": m.url or pr_url(repo, m.pr), "state": "merged", "ci_state": None}
              for m in merges]
    done = bool(merges) and item is None
    title = _title(number, item.issue_view if item else None, gh_issue, item.pr if item else None) or (
        merges[0].title if merges else None)
    return {
        "issue": number,
        "title": title,
        "url": issue_url(repo, number),
        "labels": _labels(item, gh_issue) if item else (labels_of(dict(gh_issue)) if gh_issue else []),
        "board": board_payload(item.status, repo) if item else None,
        "action": dash.primary_action(item.status, held=item.held) if item else None,
        "done": done,
        "held_for_human": item.held if item else False,
        "pull_requests": pulls,
        "cost_usd": cost,
        "estimated": estimated,
        "rail": asdict(dash.rail(item.state if item else None, has_pr=bool(pulls), legs=own, done=done))
        if (item or own or merges) else None,
        "history": [asdict(visit) for visit in dash.history(own)],
        "steps": [step(idx, leg, (models or {}).get(leg.leg_id, ""))
                  for idx, leg in enumerate(leg for leg in own if not leg.carried)],
        "carried": {"legs": len(carried), "cost_usd": _spend(carried)[0]} if carried else None,
        "triage": _triage(dash.issue_triage(own), dash.issue_steward(own)),
    }


def _triage(triage: Triage | None, steward: Steward | None) -> dict | None:
    if triage is None:
        return None
    steward = steward or Steward()
    return {"tier": triage.tier.value, "acceptance_criteria": list(steward.acceptance_criteria),
            "duplicate_of": steward.duplicate_of, "duplicate_reason": steward.duplicate_reason or None,
            "rationale": triage.rationale or None}


def lease_rows(leases: Iterable[Lease], legs: Iterable[dash.LegRow]) -> list[dict]:
    """The `leases` table: which role holds which item, through which leg.
    A negative target is a PR with no resolvable issue."""
    by_id = {leg.leg_id: leg for leg in legs}
    rows = []
    for lease in leases:
        leg = by_id.get(str(lease.run_id)) if lease.run_id is not None else None
        rows.append({
            "role": lease.role,
            "issue": lease.target if lease.target > 0 else None,
            "pr": -lease.target if lease.target < 0 else (leg.number if leg and leg.kind not in _ISSUE_KINDS
                                                         and leg.number > 0
                                                         else None),
            "leg_id": str(lease.run_id) if lease.run_id is not None else None,
            "kind": leg.kind if leg else None,
            "leg_state": leg.state if leg else None,
            "pid": lease.pid,
            "acquired_ts": _iso(lease.acquired_at),
        })
    return rows


def attention(item_list: Iterable[Item], gh_issues: Mapping[int, Mapping]) -> list[dict]:
    """Needs-you items, longest wait first."""
    rows = [(item, item.status) for item in item_list if isinstance(item.status, dash.NeedsYou)]
    rows.sort(key=lambda pair: (pair[1].since_ts is None, pair[1].since_ts or "", pair[0].issue))
    return [{
        "issue": item.issue,
        "title": _title(item.issue, item.issue_view, gh_issues.get(item.issue), item.pr),
        "reason": status.reason,
        "headline": status.headline,
        "detail": status.detail,
    } for item, status in rows]


def base_failures(health: BaseHealth | None, repo: str) -> list[dict] | None:
    """The fix role's red base workflows, longest red first; None: unread."""
    if health is None or health.runs is None:
        return None
    return [{"workflow": latest.workflow, "since_ts": since.created_at, "run_url": latest.url,
             "issue_url": issue_url(repo, issue.number) if issue and not issue.closed_at else None}
            for latest, since, _ in sorted(base_verdicts(health.runs), key=lambda v: v[1].created_at)
            if latest.conclusion in RED_CONCLUSIONS
            for issue in [next((i for i in workflow_issues(health, latest.workflow) if not i.closed_at), None)]]


def health_trend(issues_payload: object, local_tz: tzinfo) -> list[dict]:
    pairs = [(str(i.get("createdAt") or ""), str(i.get("closedAt") or ""))
             for i in index_by_number(issues_payload).values()]
    return [asdict(day) for day in dash.health_trend(pairs, local_tz)]
