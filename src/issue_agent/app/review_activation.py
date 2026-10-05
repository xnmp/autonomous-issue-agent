"""Offline operator activation of review authority; never dispatches paid work."""

from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from ..adapters.control_store import SQLiteControlStore
from ..adapters.control_path import control_store_path
from ..adapters.event_log import EventLog
from ..adapters.github import GitHubGateway, parse_review_pages
from ..adapters.resolve_activation import LegacyRoots, copy_for_rehearsal, distinct_roots, file_digests
from ..adapters.review_activation import (
    LEGACY_REVIEW_NAME, ReviewActivationRefused, ReviewActivationSourcesChanged, SQLiteReviewActivation,
    read_activation_commit_messages, read_legacy_review_roots, read_open_pull_requests,
)
from ..adapters.review_effects import read_pull_request
from ..adapters.review_linkage import read_review_linked_issue
from ..adapters.store import Store
from ..adapters.workflow_rows import load_workflow_row
from ..domain.events import Producer
from ..domain.labels import Label
from ..domain.resolve_activation import ActivationBlocked, DeploymentInventory, LegacyAbsent, LegacyFile, LegacyPresent, UnmappedLegacyEntry
from ..domain.review_activation import (
    LEGACY_SUFFIXES, LegacyHoldProvenance, LegacyReviewEntry, LegacyReviewRecord, OpenPullRequests, OpenPullRequestsListed,
    PullRequestRead, ReviewActivationDecision, ReviewActivationFacts, ReviewActivationPlan, ReviewFamilyActive,
    imported_snapshot, legacy_ceiling_hold, legacy_hold_provenance, plan_review_activation, shadow_compare,
)
from ..domain.workflow_identity import PullRequestRef, WorkflowKey, canonical_workflow_repository
from ..domain.review import ReviewConfig, ReviewExcluded, patch_signature, review_eligibility
from ..domain.review_linkage import LinkedIssueUnavailable, NoLinkedIssue, linked_issue_holds
from ..domain.review_exemptions import approval_grants_cover, review_declarations
from ..domain.review_workflow import CompleteReviews, ObservedPullRequest, PullRequestState, PullRequestUnavailable, SubmittedReviewState
from ..domain.identity import login_eq
from ..domain.fix import breaker_key
from ..domain.refs import issue_from_branch
from ..domain.world import PRView, Review
from .config import HarnessConfig, owner_identity_for_target
from .resolve_activation import ActivationNotConfirmed
from .resolve_control import load_operator_configuration
from .review_identity import review_identity

JsonReport = dict[str, object]


def unproductive_round_keys(pr) -> frozenset[int]:
    """Legacy attribution retained only for stopped one-time import."""
    return frozenset({breaker_key(pr), -pr.number})


def unproductive_rounds_reader(store, abandoned: Callable[[], Mapping[int, int]]) -> Callable[[object], int]:
    """Legacy file and recovered-death evidence for stopped one-time import only."""
    def read(pr: object) -> int:
        deaths = abandoned()
        return sum(store.get_infra_deaths(key) + deaths.get(key, 0) for key in unproductive_round_keys(pr))

    return read


@dataclass(frozen=True)
class ReviewActivationScope:
    name: str
    repository: str
    control_path: str
    legacy_roots: LegacyRoots
    event_dir: str
    ceiling: int
    review_bot: str
    trigger_label: Label
    gateway: object
    config: ReviewConfig | None = None
    review_owner: str = ""
    exemption_mac_key: str = field(default="", repr=False, compare=False)
    review_root: str = "review"


def activation_scope(
    name: str, *, harness_root: str, extra_roots: tuple[str, ...] = (),
    gateway: Callable[[HarnessConfig, Mapping[str, str]], object] | None = None,
) -> ReviewActivationScope:
    cfg, env = load_operator_configuration(name, harness_root=harness_root)
    if "review" not in env.get("ROLES", "fix review steward").split():
        raise ValueError("review activation requires the configured review role")
    path = control_store_path((cfg.state_dir, cfg.review_state_dir, cfg.steward_state_dir),
                              authority_root=cfg.control_authority_root)
    if not cfg.review_state_dir or not os.path.isabs(cfg.review_state_dir):
        raise ValueError("review activation requires an explicit absolute REVIEW_STATE_DIR")
    configured = tuple((role, directory) for role, directory in (
        ("fix", cfg.state_dir), ("review", cfg.review_state_dir), ("steward", cfg.steward_state_dir),
    ) if directory)
    for extra in extra_roots:
        if not os.path.isabs(extra) or not os.path.isdir(extra):
            raise ValueError(f"legacy state root must be an existing absolute directory: {extra}")
    roots = distinct_roots(configured + tuple((f"extra:{os.path.realpath(e)}", e) for e in extra_roots))
    remote = gateway(cfg, env) if gateway is not None else GitHubGateway(
        cfg.repo, review_identity(dict(env)), review_pr_label=cfg.review_trigger_label,
        review_adopt_unlabeled=cfg.review_adopt_unlabeled,
    )
    review = cfg.review_runtime
    config = ReviewConfig(review.bot, identity_mode=cfg.identity_mode, trigger_label=Label(review.trigger_label),
                          adopt_unlabeled=review.adopt_unlabeled, max_unproductive_rounds=review.max_unproductive_rounds,
                          max_rejections=review.max_rejections)
    return ReviewActivationScope(name, canonical_workflow_repository(cfg.repo), path, roots,
                                 cfg.review_state_dir, review.max_rejections, review.bot, Label(review.trigger_label), remote,
                                 config, owner_identity_for_target(cfg.repo, cfg.target_dir, "review"), cfg.exemption_mac_key,
                                 next(label for label, root in roots if os.path.realpath(root) == os.path.realpath(cfg.review_state_dir)))


@dataclass(frozen=True)
class Evaluation:
    records: tuple[LegacyReviewEntry, ...]
    inventory: DeploymentInventory
    listing: OpenPullRequests
    reads: tuple[PullRequestRead, ...]
    decision: ReviewActivationDecision


def _facts(scope: ReviewActivationScope, read: ObservedPullRequest, view: PRView) -> ReviewActivationFacts:
    config = scope.config or ReviewConfig(scope.review_bot, trigger_label=scope.trigger_label)
    roots = dict(scope.legacy_roots)
    state = Store(roots[scope.review_root])
    control = SQLiteControlStore(scope.control_path)
    rounds = unproductive_rounds_reader(state, lambda: control.abandoned_dispatch_counts(scope.review_owner))(view)
    eligibility = review_eligibility(view, config, rounds)
    excluded = eligibility.holds if isinstance(eligibility, ReviewExcluded) else ()
    evidence: list[object] = [view.author, view.is_draft, view.head_ref_name, rounds, config.identity_mode.value,
                              config.adopt_unlabeled, config.max_unproductive_rounds,
                              [(os.path.basename(path), digest) for path, digest in file_digests(
                                  tuple(state.issue_path(key, "infra-deaths")
                                        for key in sorted(unproductive_round_keys(view)))).items()]]
    non_resetting: tuple[str, ...] = ()
    provenance = LegacyHoldProvenance.UNPROVEN
    gateway = scope.gateway
    linked = read_review_linked_issue(control, gateway, read.key, view)
    if isinstance(linked, LinkedIssueUnavailable):
        raise ValueError(linked.reason)
    linked_holds = linked_issue_holds(linked)
    if isinstance(linked, NoLinkedIssue):
        evidence.append({"linked_issue": "absent"})
    else:
        issue = linked.issue
        evidence.append({
            "linked_issue": issue.number, "state": issue.state.lower(),
            "labels": sorted(label.value for label in issue.labels), "updated_at": issue.updated_at,
            "blocked_by_open": issue.blocked_by_open, "open_blocker_numbers": issue.open_blocker_numbers,
            "workflow_holds": linked.workflow_holds,
        })
    # The legacy counting rule only ignores a current-head approval proven
    # incomplete. Complete REST bodies replace the potentially truncated
    # GraphQL review list before authenticating grants.
    has_current_approval = isinstance(read.reviews, CompleteReviews) and any(
        review.state is SubmittedReviewState.APPROVED and review.commit_id == read.head_sha
        and login_eq(review.author, scope.review_bot) for review in read.reviews.reviews
    )
    reviews: list[Review] = []
    if isinstance(read.reviews, CompleteReviews) and (
        has_current_approval or legacy_ceiling_hold(read, scope.trigger_label, scope.review_bot)
    ):
        raw = getattr(gateway, "_gh_json")(["api", "--paginate", "--slurp",
                                            f"repos/{scope.repository}/pulls/{view.number}/reviews?per_page=100"])
        if parse_review_pages(raw) != read.reviews:
            raise ValueError("complete review evidence changed while verifying grants")
        bodies: list[tuple[str, str]] = []
        for page in raw:
            for row in page:
                if str(row["state"]).upper() == "PENDING":
                    continue
                if not isinstance(row.get("body"), str):
                    raise ValueError("complete review body is unavailable")
                bodies.append((str(row["id"]), row["body"]))
                user = row.get("user")
                author = str(user.get("login") or "ghost") if isinstance(user, dict) else "ghost"
                reviews.append(Review(str(row["state"]).upper(), author, row["submitted_at"], row["body"],
                                      row.get("commit_id") or ""))
        provenance = legacy_hold_provenance(read, scope.review_bot, tuple(bodies))
        evidence.append(raw)
    if has_current_approval:
        diff = getattr(gateway, "pr_diff")(view.number, view.base_ref_oid, view.head_ref_oid)
        if not view.base_ref_oid or (diff.base_oid, diff.head_oid) != (view.base_ref_oid, read.head_sha) or not diff.text.strip():
            raise ValueError("revision-bound exemption diff is unavailable")
        messages = read_activation_commit_messages(gateway, scope.repository, view.number, read.head_sha)
        declarations = review_declarations(diff.text, messages)
        if not approval_grants_cover(tuple(reviews), review_bot=scope.review_bot, declarations=declarations,
                                     issue_number=issue_from_branch(view.head_ref_name), patch_id=patch_signature(diff.text),
                                     mac_key=scope.exemption_mac_key):
            non_resetting = (read.head_sha,)
        evidence.extend([diff.base_oid, diff.head_oid, diff.text, messages, non_resetting])
    digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return ReviewActivationFacts(view.number, excluded, non_resetting, digest, provenance, linked_holds=linked_holds)


def evaluate(scope: ReviewActivationScope, activation: SQLiteReviewActivation) -> Evaluation:
    inventory, recorded = activation.inventory(scope.repository)
    # Retained activation evidence is permanent. Replays neither consult stale
    # files nor require GitHub availability after migration.
    if inventory.activation is not None:
        return Evaluation((), inventory, OpenPullRequestsListed(()), (),
                          ReviewFamilyActive(inventory.activation, recorded))
    records = read_legacy_review_roots(scope.legacy_roots)
    listing = read_open_pull_requests(scope.gateway, scope.repository)
    numbers = set(listing.numbers if isinstance(listing, OpenPullRequestsListed) else ())
    numbers.update(record.pr for record in records if isinstance(record, LegacyReviewRecord))
    reads: list[PullRequestRead] = []
    facts: list[ReviewActivationFacts] = []
    for number in sorted(numbers):
        read, view = read_pull_request(scope.gateway, WorkflowKey(scope.repository, PullRequestRef(number)),
                                       datetime.now(timezone.utc), reviews=True)
        if isinstance(read, ObservedPullRequest) and read.state is PullRequestState.OPEN:
            try:
                if not isinstance(view, PRView):
                    raise ValueError("complete pull request view is unavailable")
                facts.append(_facts(scope, read, view))
            except sqlite3.Error:
                raise
            except Exception as exc:  # noqa: BLE001 - evidence cannot be guessed at activation
                read = PullRequestUnavailable(read.key, read.observed_at, f"activation evidence: {type(exc).__name__}: {exc}"[:512])
        reads.append(read)
    decision = plan_review_activation(scope.repository, records, tuple(reads), listing, inventory,
                                      ceiling=scope.ceiling, review_bot=scope.review_bot, trigger_label=scope.trigger_label,
                                      facts=tuple(facts))
    return Evaluation(records, inventory, listing, tuple(reads), decision)


def _file(value: LegacyFile) -> JsonReport:
    if isinstance(value, LegacyPresent):
        return {"state": "present", "digest": value.digest}
    if isinstance(value, LegacyAbsent):
        return {"state": "absent"}
    return {"state": "unreadable", "reason": value.reason}


def report(scope: ReviewActivationScope, evaluation: Evaluation, *, shadow: bool) -> JsonReport:
    decision, inventory = evaluation.decision, evaluation.inventory
    plan = decision if isinstance(decision, ReviewActivationPlan) else None
    active = decision if isinstance(decision, ReviewFamilyActive) else None
    result: JsonReport = {
        "repository": scope.repository, "control_path": scope.control_path,
        "legacy_roots": [{"label": label, "path": path} for label, path in scope.legacy_roots],
        "ceiling": scope.ceiling, "review_bot": scope.review_bot,
        "status": "blocked" if isinstance(decision, ActivationBlocked) else "already_active" if active else "ready",
        "blockers": list(decision.reasons) if isinstance(decision, ActivationBlocked) else [],
        "records": [{"root": r.root, "pr": r.pr, "last_sha": _file(r.last_sha),
                     "patch_id": _file(r.patch_id), "guidance": _file(r.guidance)}
                    for r in evaluation.records if isinstance(r, LegacyReviewRecord)],
        "unmapped": [{"root": r.root, "name": r.name, "reason": r.reason}
                     for r in evaluation.records if isinstance(r, UnmappedLegacyEntry)],
        "inventory": {
            "quiescent": inventory.quiescent, "digest": inventory.digest,
            "active_runs": list(inventory.active_runs), "leases": list(inventory.leases),
            "dispatch_claims": list(inventory.dispatch_claims), "open_commands": list(inventory.open_commands),
            "active_reservations": list(inventory.active_reservations), "retained_targets": list(inventory.retained_targets),
            "activation": None if active is None else {
                "epoch": active.activation.epoch, "plan_digest": active.activation.plan_digest,
                "source_digest": active.activation.source_digest, "inventory_digest": active.activation.inventory_digest,
                "ceiling": active.activation.ceiling,
            },
        },
        "plan": None if plan is None and active is None else {
            "plan_digest": plan.plan_digest if plan else active.activation.plan_digest,
            "epoch": plan.epoch if plan else active.activation.epoch,
            "ceiling": plan.ceiling if plan else active.activation.ceiling,
            "source_digest": plan.source_digest if plan else active.activation.source_digest,
            "inventory_digest": plan.inventory_digest if plan else active.activation.inventory_digest,
            "imports_digest": plan.imports_digest if plan else active.activation.imports_digest,
            "imports": [json.loads(target.canonical()) for target in (plan.imports if plan else active.imports)],
        },
    }
    if shadow:
        rows = shadow_compare(plan, evaluation.records, evaluation.reads) if plan else ()
        result["shadow"] = [{"pr": r.pr, "legacy_dispatch": r.legacy_dispatch, "new_dispatch": r.new_dispatch,
                             "continuation": r.continuation, "ceiling_gate": r.ceiling_gate, "guidance": r.guidance,
                             "agrees": r.agrees, "paid_review_on_activation": r.paid_review_on_activation} for r in rows]
        result["paid_reviews_on_activation"] = [r.pr for r in rows if r.paid_review_on_activation]
    return result


def _bind_operator_feed(control: SQLiteControlStore, scope: ReviewActivationScope) -> None:
    events = EventLog(os.path.join(scope.event_dir, "events-v2.jsonl"),
                      Producer(f"operator:{scope.name}", scope.repository, "operator", os.getpid(), uuid.uuid4().hex))
    control.bind_event_outbox(events.prepare, events.append_committed,
                             line_limit=events.line_limit, destination=events.producer.feed_identity)


def _operator_activation(scope: ReviewActivationScope, *, journal: bool = False) -> SQLiteReviewActivation:
    control = SQLiteControlStore(scope.control_path)
    control.require_initialized()
    if journal:
        _bind_operator_feed(control, scope)
    return SQLiteReviewActivation(control)


def inspect(scope: ReviewActivationScope, *, shadow: bool) -> JsonReport:
    return report(scope, evaluate(scope, _operator_activation(scope)), shadow=shadow)


def activate(scope: ReviewActivationScope, *, confirm_plan_digest: str) -> JsonReport:
    activation = _operator_activation(scope, journal=True)
    evaluation = evaluate(scope, activation)
    decision = evaluation.decision
    if isinstance(decision, ActivationBlocked):
        return report(scope, evaluation, shadow=True)
    if isinstance(decision, ReviewFamilyActive):
        if confirm_plan_digest != decision.activation.plan_digest:
            raise ActivationNotConfirmed("confirmed plan digest differs from the recorded activation")
        result = report(scope, evaluation, shadow=False)
        result["status"] = "already_active_with_this_plan"
        return result
    if confirm_plan_digest != decision.plan_digest:
        raise ActivationNotConfirmed("confirmed plan digest differs from the freshly computed plan; review `shadow` again")
    committed = activation.activate(decision, revalidate=lambda: evaluate(scope, activation).decision)
    result = report(scope, evaluate(scope, activation), shadow=False)
    result["status"] = "already_active_with_this_plan" if committed.idempotent else "activated"
    return result


def rehearse(scope: ReviewActivationScope, *, copy_to: str) -> JsonReport:
    """Activate and verify an isolated copy, preserving original authority and files."""
    legacy = tuple(sorted(os.path.join(directory, entry.name) for _, directory in scope.legacy_roots
                          for entry in (os.scandir(directory) if os.path.isdir(directory) else ())
                          if entry.name.endswith(tuple(f".{suffix}" for suffix in (*LEGACY_SUFFIXES, "infra-deaths")))))
    originals = (scope.control_path, f"{scope.control_path}.authority",
                 os.path.join(scope.event_dir, "events-v2.jsonl"), *legacy)
    before = file_digests(originals)
    original_records = read_legacy_review_roots(scope.legacy_roots)
    copied = copy_for_rehearsal(scope.control_path, scope.legacy_roots, copy_to,
                                protected=(scope.event_dir,), legacy_name=re.compile(
                                    f"(?:{LEGACY_REVIEW_NAME.pattern})|(?:issue--?[0-9]+\\.infra-deaths)", re.DOTALL))
    control = SQLiteControlStore(copied.control_path)
    control.initialize()
    events = os.path.join(copy_to, "events")
    os.makedirs(events, mode=0o700, exist_ok=True)
    copied_scope = replace(scope, control_path=copied.control_path, legacy_roots=copied.roots, event_dir=events)
    _bind_operator_feed(control, copied_scope)
    activation = SQLiteReviewActivation(control)
    evaluation = evaluate(copied_scope, activation)
    result = report(copied_scope, evaluation, shadow=True)
    decision = evaluation.decision
    checks = {"copied_sources_match": evaluation.records == original_records}
    failures: list[str] = []
    if isinstance(decision, ReviewActivationPlan) and checks["copied_sources_match"]:
        def revalidate() -> ReviewActivationDecision:
            return evaluate(copied_scope, activation).decision

        checks["activated"] = not activation.activate(decision, revalidate=revalidate).idempotent
        with control.transaction() as db:
            for target in decision.imports:
                expected = imported_snapshot(decision, target)
                from ..adapters.continuation_activation import retain_deferred_pr_repair
                expected = retain_deferred_pr_repair(db, expected)
                if load_workflow_row(db, expected.revision.fence.key) != expected:
                    failures.append(f"pull request #{target.pr}: imported workflow differs from its decision")
        checks["imports_read_back"] = not failures
        checks["reactivation_idempotent"] = activation.activate(decision, revalidate=revalidate).idempotent
        different = replace(decision, source_digest="0" * 64 if decision.source_digest != "0" * 64 else "1" * 64)
        try:
            activation.activate(different, revalidate=lambda: different)
        except ReviewActivationSourcesChanged:
            checks["different_plan_refused"] = False
        except ReviewActivationRefused:
            checks["different_plan_refused"] = True
        else:
            checks["different_plan_refused"] = False
    checks["originals_unchanged"] = before == file_digests(originals)
    result.update({
        "status": "rehearsed" if isinstance(decision, ReviewActivationPlan) and all(checks.values()) else
                  "blocked" if isinstance(decision, ActivationBlocked) else
                  "already_active" if isinstance(decision, ReviewFamilyActive) and checks["originals_unchanged"] else "failed",
        "copy": {"control_path": copied.control_path,
                 "legacy_roots": [{"label": label, "path": path} for label, path in copied.roots]},
        "checks": checks, "verification_failures": failures,
    })
    return result
