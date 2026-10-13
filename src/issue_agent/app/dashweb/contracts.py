"""Validated HTTP response contracts for the interactive dashboard.

The shapes mirror `domain.dashboard`'s read model; `generate:api` turns them
into the frontend's types.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(extra="allow")


class Warned(Contract):
    warnings: list[str]


class RoleHealth(Contract):
    role: str
    # From the process table; None when foreign processes are unreadable.
    alive: bool | None
    # No running heartbeat within the staleness bound.
    stale: bool
    heartbeat_ts: str | None


class RepoSummary(Contract):
    name: str
    repo: str | None
    target_dir: str | None
    is_self: bool
    active: bool | None
    roles: list[RoleHealth]
    frecency: float
    last_activity_ts: str | None
    # From the last GitHub observation, None until one is cached.
    in_flight: int | None
    done: int | None


class ReposPayload(Warned):
    repos: list[RepoSummary]


class ClaudeUsage(Contract):
    five_hour_pct: float | None
    five_hour_resets_at: str | None
    seven_day_pct: float | None
    seven_day_resets_at: str | None
    source: str | None
    error: str | None


class CodexUsage(Contract):
    weekly_pct: float | None
    window_minutes: int | None
    resets_at: str | None
    plan: str | None
    source: str | None
    sampled_at: str | None
    error: str | None


class HarnessRepoSpend(Contract):
    name: str
    repo: str | None = None
    today_usd: float | None
    daily_cap_usd: float | None


class HarnessUsage(Contract):
    global_daily_cap_usd: float | None
    today_all_repos_usd: float | None
    per_repo: list[HarnessRepoSpend]


class UsagePayload(Warned):
    claude: ClaudeUsage | None
    codex: CodexUsage | None
    harness: HarnessUsage | None


class VerificationRow(Contract):
    id: str | None
    change: str | None
    shipped: str | None
    status: str | None
    evidence: str | None
    raw: str
    reason: str | None = None


class VerificationPayload(Warned):
    rows: list[VerificationRow]


class KindCost(Contract):
    kind: str
    usd: float
    runs: int


class BasisCost(Contract):
    basis: str
    usd: float


class Metrics(Contract):
    # None when GitHub was not read: unknown, not zero.
    merged_prs: int | None
    # GitHub listed only the newest merged PRs: merged_prs is a lower bound
    # and cost_per_merged_pr_usd an upper one.
    merged_prs_partial: bool = False
    open_items: int | None
    needs_human: int | None
    total_cost_usd: float
    cost_per_merged_pr_usd: float | None
    by_basis: list[BasisCost]
    by_kind: list[KindCost]
    first_pass_approvals: int
    first_pass_changes: int
    first_pass_rate: float | None
    rounds_per_pr: float | None
    legs: int
    machine_legs: int
    today_usd: float


class Caps(Contract):
    issue_cap_usd: float | None
    daily_cap_usd: float | None
    global_daily_cap_usd: float | None
    today_usd: float | None


class HealthTrendDay(Contract):
    day: str
    opened: int
    closed: int
    net_open: int


class SpendBucket(Contract):
    hour_start_iso: str
    usd: float


StopReasonId = Literal["question", "attempts", "review-rounds", "lifetime-cap", "workflow-path", "labelled"]


class AttentionItem(Contract):
    issue: int
    title: str | None = None
    reason: StopReasonId
    headline: str
    detail: str | None = None


class OverviewPayload(Warned):
    metrics: Metrics | None
    caps: Caps | None
    health: list[RoleHealth]
    needs_attention: list[AttentionItem] = []
    health_trend: list[HealthTrendDay] = []
    spend_per_hour: list[SpendBucket] = []
    # Why the control store could not be read; no board is derived then.
    store_error: str | None = None


class BoardNeedsYou(Contract):
    status: Literal["needs_you"]
    reason: StopReasonId
    headline: str
    # What releases the stop, in one line.
    hint: str
    # The stop comment (or the agent's question). Agent prose: clients
    # render it as text, never as Markdown.
    detail: str | None
    # Where a reply releases the stop.
    target: Literal["issue", "pr"]
    since_ts: str | None


class BoardWorking(Contract):
    status: Literal["working"]
    activity: Literal["agent", "queued", "ci", "merge", "update", "open_pr", "release"]
    act: str | None
    headline: str
    reason: str
    since_ts: str | None


class BoardWaiting(Contract):
    status: Literal["waiting"]
    on: Literal["branch-protection", "human-review", "headroom", "backoff", "github"]
    headline: str
    reason: str
    since_ts: str | None
    until_ts: str | None = None


BoardStatus = Annotated[
    BoardNeedsYou | BoardWorking | BoardWaiting,
    Field(discriminator="status"),
]

StageId = Literal["resolve", "ci", "review", "merge"]


class StageRail(Contract):
    current: StageId
    # A stage past `current` but at or before `furthest` was reached earlier
    # and rewound (e.g. a CI fix after review).
    furthest: StageId
    resolve_attempts: int
    review_rounds: int
    ci_runs: int
    steps: int
    # A leg is running now.
    live: bool


class TrackChip(Contract):
    kind: str
    outcome: Literal["running", "passed", "changes_requested", "asked", "failed", "done"]
    # Consecutive identical legs folded into this one.
    count: int


class StageVisit(Contract):
    stage: StageId
    steps: list[TrackChip]


CiState = Literal["passing", "failing", "pending"]


class IssueCard(Contract):
    """One board row: a harness item, by its issue."""

    issue: int
    title: str | None
    url: str | None
    labels: list[str] = []
    pr: int | None
    pr_url: str | None
    # None when unread or for another head: no guessed green.
    pr_ci_state: CiState | None = None
    board: BoardStatus
    action: Literal["unblock", "open"] | None = None
    rail: StageRail
    history: list[StageVisit] = []
    cost_usd: float
    estimated: bool
    legs: int
    started_ts: str | None
    last_ts: str | None
    leg_running: bool


class DoneCard(Contract):
    """A merged harness PR."""

    issue: int
    title: str | None
    url: str | None
    pr: int
    pr_url: str | None
    merged_ts: str | None
    cost_usd: float
    legs: int
    review_rounds: int
    labels: list[str] = []


class BacklogCard(Contract):
    issue: int
    title: str | None
    url: str | None
    body: str | None
    labels: list[str]
    age_days: int | None
    age_s: int | None = None
    comments: int | None
    issue_type: str | None = None
    difficulty: str | None = None
    impact: str | None = None


class ToastNotification(Contract):
    id: str
    ts: str
    severity: Literal["info", "attention"]
    title: str
    detail: str | None = None
    issue: int | None = None
    pr: int | None = None


class IssuesPayload(Warned):
    in_flight: list[IssueCard]
    done: list[DoneCard]
    backlog: list[BacklogCard]
    gh_ok: bool
    # When the GitHub observation the board derives from was taken; None
    # while the first one is still being read.
    observed_at: str | None = None
    notifications: list[ToastNotification] = []
    # Why the control store could not be read; no board is derived then.
    store_error: str | None = None


class TokenUsage(Contract):
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int


class IssueStep(Contract):
    """One leg."""

    idx: int
    leg_id: str
    kind: str
    role: str | None
    runtime: str | None
    state: Literal["reserved", "running", "ended", "released"]
    # carried: copied from the legacy store at the cutover; money history only.
    outcome: Literal["running", "passed", "changes_requested", "asked", "failed", "done", "carried"]
    started_ts: str | None
    ended_ts: str | None
    duration_s: float | None
    cost_usd: float
    estimated: bool
    pricing_basis: str | None
    end_reason: str | None
    session_id: str | None
    usage: TokenUsage | None
    head: str | None
    pr: int | None
    # Agent-authored and bounded: render as text.
    summary: str | None = None
    question: str | None = None
    verdict: str | None = None
    verdict_excerpt: str | None = None


class PullRequestLink(Contract):
    number: int
    url: str | None
    state: Literal["open", "merged"]
    ci_state: CiState | None = None


class IssueDetailPayload(Warned):
    issue: int
    title: str | None
    url: str | None
    labels: list[str] = []
    # None when the issue is not a harness item right now.
    board: BoardStatus | None = None
    action: Literal["unblock", "open"] | None = None
    done: bool
    # The issue or its PR carries `needs-human`: the Unblock control.
    held_for_human: bool = False
    pull_requests: list[PullRequestLink]
    cost_usd: float
    estimated: bool
    rail: StageRail | None = None
    history: list[StageVisit] = []
    steps: list[IssueStep]
    # Why the control store could not be read; no board is derived then.
    store_error: str | None = None


class ActionResult(Contract):
    ok: bool
    detail: str

