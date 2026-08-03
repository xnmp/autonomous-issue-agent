"""Validated HTTP response contracts for the interactive dashboard."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class Contract(BaseModel):
    model_config = ConfigDict(extra="allow")


class Warned(Contract):
    warnings: list[str]


class RoleHealth(Contract):
    role: str
    alive: bool | None
    stale: bool
    last_event_ts: str | None
    last_event: str | None
    events: int
    log_path: str | None


class RepoSummary(Contract):
    name: str
    repo: str | None
    target_dir: str | None
    is_self: bool
    active: bool | None
    roles: list[RoleHealth]
    frecency: float
    last_activity_ts: str | None
    in_flight: int
    done: int


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


class InterventionCount(Contract):
    # `name` is one of domain.dashboard.INTERVENTION_CLASSES — rearm, policy,
    # or other. Not typed as a Literal: a class this build does not know must
    # still render, not fail the whole overview payload.
    name: str
    count: int


class Metrics(Contract):
    done_issues: int
    in_flight_issues: int
    total_cost_usd: float | None
    cost_per_done_usd: float | None
    first_pass_approvals: int | None
    first_pass_changes: int | None
    first_pass_rate: float | None
    rounds_per_pr: float | None
    escalations: int
    recurrences: int
    spend_blocks: int
    today_usd: float | None
    by_kind: list[KindCost]
    interventions_total: int = 0
    # None when nothing merged — "nobody intervened" and "nothing to divide by"
    # must not both render as 0.
    interventions_per_merged_pr: float | None = None
    interventions_by_class: list[InterventionCount] = []


class CostMix(Contract):
    resolve_usd: float | None
    review_usd: float | None
    repair_usd: float | None
    steward_usd: float | None


class Caps(Contract):
    issue_cap_usd: float | None
    daily_cap_usd: float | None
    today_usd: float | None


class HealthTrendDay(Contract):
    day: str
    opened: int
    closed: int
    net_open: int
    recurrences: int
    first_observation: bool


class SpendBucket(Contract):
    hour_start_iso: str
    usd: float


class OverviewPayload(Warned):
    metrics: Metrics | None
    cost_mix: CostMix | None
    caps: Caps | None
    health: list[RoleHealth]
    unattributed: list[dict[str, Any]]
    health_trend: list[HealthTrendDay]
    first_observed_day: str | None = None
    spend_per_hour: list[SpendBucket] = []


class CardStep(Contract):
    kind: str
    state: str
    status: str | None
    cost_usd: float | None
    # Consecutive same-kind attempts folded into this episode (retries).
    count: int = 1


class ModelBadge(Contract):
    model: str
    effort: str | None


class IssueCard(Contract):
    issue: int
    title: str | None
    url: str | None
    # Presentational only, and optional: a deployment that cannot read labels
    # (no GitHub, no projected facts) still renders a card.
    labels: list[str] = []
    pr: int | None
    pr_url: str | None
    pr_state: str | None
    pr_review_state: Literal["mergeable", "changes_requested"] | None = None
    # None when the PR has no checks or the rollup was unreadable: the chip
    # must show no CI signal rather than a guessed green.
    pr_ci_state: Literal["passing", "failing", "pending"] | None = None
    stage: str | None
    attention_reason: str | None = None
    escalation_reason: str | None = None
    done: bool
    steps: list[CardStep]
    cost_usd: float | None
    estimated: bool
    projected_cost_usd: float | None
    projected_model: str | None = None
    started_ts: str | None
    last_ts: str | None
    elapsed_s: float | None
    working_s: float | None
    idle_s: float | None
    idle_active: bool
    # A paid leg is executing right now. The client tick predicate reads this
    # directly: human_action alone must not dead-stop both clocks while a leg
    # is visibly spending (keifu #142/#181, 2026-08-03).
    leg_running: bool = False
    human_action: Literal["merge-ready", "awaiting-reply", "escalated"] | None = None
    human_wait_since_ts: str | None = None
    human_wait_s: float | None = None
    projected_eta_s: float | None
    models: list[str]
    model_badges: list[ModelBadge] = []
    rounds: int | None
    flags: list[str]
    issue_verification: str | None = None
    pr_verification: str | None = None
    issue_verification_reason: str | None = None
    pr_verification_reason: str | None = None
    merged_ts: str | None = None


class PendingCard(Contract):
    issue: int
    title: str | None
    url: str | None
    # The drill-down renders the issue text, same as backlog rows.
    body: str | None = None
    priority: Literal["P1", "P2", "P3"] | None
    labels: list[str]
    held: bool
    hold_reason: str | None
    overlap: Any | None


class BacklogCard(Contract):
    issue: int
    title: str | None
    url: str | None
    body: str | None
    labels: list[str]
    recurrence: bool
    recurrence_count: int
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
    pending: list[PendingCard]
    done: list[IssueCard]
    backlog: list[BacklogCard]
    gh_ok: bool
    notifications: list[ToastNotification] = []


class TokenUsage(Contract):
    input_tokens: int | None
    output_tokens: int | None
    cache_read_input_tokens: int | None
    cache_creation_input_tokens: int | None


class IssueStep(Contract):
    idx: int
    kind: str
    role: str | None
    ts: str | None
    state: str
    status: str | None
    exit_reason: str | None
    duration_s: float | None
    cost_usd: float | None
    estimated: bool
    model: str | None
    effort: str | None = None
    session_id: str | None
    turns: int | None
    usage: TokenUsage | None
    has_transcript: bool
    note: str | None = None


class PullRequestLink(Contract):
    number: int
    url: str | None
    state: str | None


class IssueDetailPayload(Warned):
    issue: int
    title: str | None
    url: str | None
    stage: str | None
    attention_reason: str | None = None
    escalation_reason: str | None = None
    done: bool
    prs: list[int]
    pull_requests: list[PullRequestLink]
    cost_usd: float | None
    elapsed_s: float | None
    working_s: float | None
    idle_s: float | None
    idle_active: bool
    # See IssueCard.leg_running.
    leg_running: bool = False
    steps: list[IssueStep]
    events: list[dict[str, Any]]


class StepEventsPayload(Warned):
    events: list[dict[str, Any]]


class ActionResult(Contract):
    ok: bool
    detail: str
