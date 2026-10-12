"""Offline review-rejection family import and activation I/O; ADR 0015.

The only live reader of the retired `pr-N.last-sha`, `pr-N.patch-id` and
`pr-N.operator-guidance` files. It never writes them. Activation opens
existing authority without creating or migrating it, holds the three
exclusive deployment barriers, revalidates the sources (files and fresh
GitHub reads) outside SQL, and commits epoch, imported workflows, ceiling
gates and import decisions in one short transaction. No dispatch decision is
recorded: the review daemon records its first from its own read.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Callable
from contextlib import ExitStack, closing

from ..domain.resolve_activation import DeploymentInventory, ExistingActivation, UnmappedLegacyEntry
from ..domain.labels import Label
from ..domain.review_activation import (
    LEGACY_GUIDANCE, LEGACY_LAST_SHA, LEGACY_PATCH_ID, LEGACY_SUFFIXES, MAX_IMPORTED_PULL_REQUESTS,
    OpenPullRequests, OpenPullRequestsListed, OpenPullRequestsUnavailable, ImportedReviewTarget,
    LegacyReviewEntry, LegacyReviewRecord, ReviewActivationCommitted, ReviewActivationDecision,
    ReviewActivationPlan, ReviewFamilyActive, imported_snapshot, imports_digest,
)
from ..domain.workflow_identity import PullRequestRef, WorkflowFamily, WorkflowKey, canonical_workflow_repository
from ..domain.review_rejection import is_commit_sha
from .control_store import SQLiteControlStore
from .deployment_authority import deployment_effect_barrier
from .resolve_activation import (
    LegacyAbsent, LegacyFile, LegacyRoots, _printable, _read_legacy_file, distinct_roots, family_inventory,
)
from .workflow_codec import encode_workflow
from .workflow_rows import WorkflowAuthorityError, persist_workflow_snapshot, workflow_scope, insert_workflow_component, encode_workflow_episodes

LEGACY_REVIEW_NAME = re.compile(r"pr-(.*)\.(" + "|".join(re.escape(s) for s in LEGACY_SUFFIXES) + r")", re.DOTALL)
_PR_NUMBER = re.compile(r"[1-9][0-9]{0,18}")
_ROLES = ("fix", "review")
_FAMILY = WorkflowFamily.REVIEW_REJECTIONS.value
_PLAN_NAMESPACE = "review-activation-plan"


def read_open_pull_requests(gateway: object, repository: str) -> OpenPullRequests:
    """Complete REST pagination, without the legacy listing's silent limit.

    Only the operator importer needs every open PR, including unlabelled
    held PRs. A failed page or malformed response never proves absence.
    """
    try:
        reader = getattr(gateway, "_gh_json", None)
        if not callable(reader):
            return OpenPullRequestsUnavailable("gateway cannot read a complete open pull request listing")
        pages = reader(["api", "--paginate", "--slurp",
                        f"repos/{repository}/pulls?state=open&per_page=100"])
        if not isinstance(pages, list) or not pages or any(not isinstance(page, list) for page in pages):
            return OpenPullRequestsUnavailable("open pull request pagination is malformed")
        numbers: list[int] = []
        for page in pages:
            for row in page:
                if not isinstance(row, dict) or type(row.get("number")) is not int or row.get("state") != "open":
                    return OpenPullRequestsUnavailable("open pull request listing contains a malformed row")
                numbers.append(row["number"])
                if len(numbers) > MAX_IMPORTED_PULL_REQUESTS:
                    return OpenPullRequestsUnavailable("open pull request listing exceeds the activation bound")
        if len(set(numbers)) != len(numbers):
            return OpenPullRequestsUnavailable("open pull request listing contains duplicate rows")
        return OpenPullRequestsListed(tuple(sorted(numbers)))
    except sqlite3.Error:
        raise
    except Exception as exc:  # noqa: BLE001 - remote failures are unavailable evidence
        return OpenPullRequestsUnavailable(_printable(f"{type(exc).__name__}: {exc}")[:512])


class ReviewActivationRefused(WorkflowAuthorityError):
    pass


class ReviewActivationSourcesChanged(ReviewActivationRefused):
    """Revalidated sources, reads or inventory no longer produce the reviewed plan."""


def read_legacy_review_state(state_dir: str, root: str = "review") -> tuple[LegacyReviewEntry, ...]:
    """Every retired review key in one labelled state root; read-only."""
    try:
        names = sorted(entry.name for entry in os.scandir(state_dir))
    except FileNotFoundError:
        return ()
    except OSError as exc:
        return (UnmappedLegacyEntry("<state-dir>", _printable(f"state directory unreadable: {exc.strerror or exc}"), root),)
    files: dict[int, dict[str, LegacyFile]] = {}
    unmapped: list[UnmappedLegacyEntry] = []
    for name in names:
        match = LEGACY_REVIEW_NAME.fullmatch(name)
        if match is None:
            continue
        number, suffix = match.groups()
        if _PR_NUMBER.fullmatch(number) is None or int(number) > 2**63 - 1:
            unmapped.append(UnmappedLegacyEntry(_printable(name), "not a canonical positive pull request number", root))
            continue
        files.setdefault(int(number), {})[suffix] = _read_legacy_file(os.path.join(state_dir, name))
    records: list[LegacyReviewEntry] = [
        LegacyReviewRecord(pr, found.get(LEGACY_LAST_SHA, LegacyAbsent()), found.get(LEGACY_PATCH_ID, LegacyAbsent()),
                           found.get(LEGACY_GUIDANCE, LegacyAbsent()), root)
        for pr, found in sorted(files.items())
    ]
    return tuple(records + unmapped)


def read_legacy_review_roots(roots: LegacyRoots) -> tuple[LegacyReviewEntry, ...]:
    """Every root that ever held review state; one pull request in two roots is
    ambiguous and blocks activation rather than choosing either."""
    return tuple(entry for label, path in distinct_roots(roots) for entry in read_legacy_review_state(path, label))


def _recorded(db: sqlite3.Connection, repository: str) -> tuple[ExistingActivation | None, tuple[ImportedReviewTarget, ...]]:
    row = db.execute(
        "SELECT epoch, source_digest, inventory_digest FROM workflow_authority_epochs WHERE repository=? AND family=?",
        (repository, _FAMILY),
    ).fetchone()
    if row is None:
        return None, ()
    decisions: list[ImportedReviewTarget] = []
    try:
        for item in db.execute(
            "SELECT * FROM workflow_activation_imports WHERE repository=? AND family=? ORDER BY target_number",
            (repository, _FAMILY),
        ):
            decision = ImportedReviewTarget.parse(item["decision_json"])
            if (item["target_kind"], item["target_number"], item["epoch"], item["source_digest"]) != (
                "pr", decision.pr, row["epoch"], row["source_digest"],
            ):
                raise ValueError("import decision index disagrees with its activation")
            decisions.append(decision)
        evidence = db.execute(
            "SELECT ceiling, plan_digest FROM workflow_activation_evidence WHERE repository=? AND family=? AND epoch=?",
            (repository, _FAMILY, row["epoch"]),
        ).fetchone()
        if evidence is None:
            raise ValueError("activation lacks its recorded ceiling and plan digest")
        imports = tuple(decisions)
        metadata = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
            (_PLAN_NAMESPACE, repository, str(row["epoch"])),
        ).fetchone()
        if metadata is None:
            raise ValueError("activation lacks its retained plan inputs")
        raw = metadata["payload_json"]
        if not isinstance(raw, str) or len(raw) > 8192:
            raise ValueError("retained plan inputs must be bounded text")
        inputs = json.loads(raw)
        if not isinstance(inputs, dict) or set(inputs) != {"review_bot", "trigger_label"}:
            raise ValueError("retained plan inputs have the wrong fields")
        recorded_plan = ReviewActivationPlan(
            repository, row["epoch"], evidence["ceiling"], inputs["review_bot"], Label(inputs["trigger_label"]),
            row["source_digest"], row["inventory_digest"], imports,
        )
        if recorded_plan.plan_digest != evidence["plan_digest"]:
            raise ValueError("retained import decisions disagree with the confirmed plan digest")
        return ExistingActivation(
            row["epoch"], row["source_digest"], row["inventory_digest"], imports_digest(imports),
            evidence["ceiling"], evidence["plan_digest"],
        ), imports
    except (ValueError, TypeError) as exc:
        raise WorkflowAuthorityError(f"invalid review activation evidence: {exc}") from exc


def read_review_inventory(
    db: sqlite3.Connection, repository: str,
) -> tuple[DeploymentInventory, tuple[ImportedReviewTarget, ...]]:
    """The deployment inventory with the review family's retained authority, and its recorded imports."""
    repository = canonical_workflow_repository(repository)
    activation, recorded = _recorded(db, repository)
    return family_inventory(db, repository, WorkflowFamily.REVIEW_REJECTIONS, activation), recorded


class SQLiteReviewActivation:
    """Operator-only activation; the daemons never construct this."""

    def __init__(self, control: SQLiteControlStore) -> None:
        if not isinstance(control, SQLiteControlStore):
            raise ValueError("activation requires the existing control store")
        self._control = control

    def inventory(self, repository: str) -> tuple[DeploymentInventory, tuple[ImportedReviewTarget, ...]]:
        self._control.require_initialized()
        with closing(self._control._connect()) as db:
            db.execute("BEGIN")
            try:
                return read_review_inventory(db, repository)
            finally:
                db.rollback()

    def activate(
        self, plan: ReviewActivationPlan, *, revalidate: Callable[[], ReviewActivationDecision],
    ) -> ReviewActivationCommitted:
        if not isinstance(plan, ReviewActivationPlan):
            raise ValueError("activation requires a validated plan")
        # Initialization has a different lock order; activation never creates,
        # adopts or migrates authority while holding these barriers.
        self._control.require_initialized()
        with ExitStack() as barriers:
            for role in _ROLES:
                barriers.enter_context(deployment_effect_barrier(self._control.path, plan.repository, role))
            # Fresh files and GitHub reads under the barriers, with no SQL open.
            fresh = revalidate()
            if isinstance(fresh, ReviewFamilyActive) and fresh.activation.plan_digest == plan.plan_digest:
                replay = True
            elif isinstance(fresh, ReviewActivationPlan) and fresh == plan:
                replay = False
            else:
                raise ReviewActivationSourcesChanged(
                    "legacy sources, pull request reads or deployment inventory changed since the plan was reviewed")
            with self._control.transaction() as db:
                current, _recorded_imports = read_review_inventory(db, plan.repository)
                if current.activation is not None:
                    if replay and plan.matches(current.activation):
                        return ReviewActivationCommitted(plan, idempotent=True)
                    raise ReviewActivationRefused("review family is already active with a different plan")
                if replay:
                    raise ReviewActivationRefused("review activation disappeared while revalidating")
                if not current.quiescent or current.digest != plan.inventory_digest:
                    raise ReviewActivationSourcesChanged(
                        "deployment inventory changed before commit: " + "; ".join(current.blockers()))
                if current.retained_targets:
                    raise ReviewActivationRefused("workflow authority is retained without an activation")
                self._commit(db, plan)
        return ReviewActivationCommitted(plan)

    def _commit(self, db: sqlite3.Connection, plan: ReviewActivationPlan) -> None:
        db.execute(
            "INSERT INTO workflow_authority_epochs VALUES (?, ?, ?, ?, ?, ?)",
            (plan.repository, _FAMILY, plan.epoch, plan.source_digest, plan.inventory_digest,
             self._control._now().isoformat()),
        )
        db.execute(
            "INSERT INTO workflow_activation_evidence VALUES (?, ?, ?, ?, ?)",
            (plan.repository, _FAMILY, plan.epoch, plan.ceiling, plan.plan_digest),
        )
        # Review imports depend on the bot and trigger configuration. Retain
        # those inputs so replay can verify every original decision against
        # the confirmed digest after configuration or workflow state changes.
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
            (_PLAN_NAMESPACE, plan.repository, str(plan.epoch), self._control._now().isoformat(),
             json.dumps({"review_bot": plan.review_bot, "trigger_label": plan.trigger_label.value},
                        sort_keys=True, separators=(",", ":"))),
        )
        held = baselines = guided = 0
        for target in plan.imports:
            snapshot = imported_snapshot(plan, target)
            from .continuation_activation import retain_deferred_pr_repair
            snapshot = retain_deferred_pr_repair(db, snapshot)
            scope = workflow_scope(WorkflowKey(plan.repository, PullRequestRef(target.pr)))
            db.execute(
                "INSERT INTO workflows VALUES (?, ?, ?, ?, ?, ?)",
                (*scope, snapshot.revision.fence.generation, snapshot.revision.version,
                 encode_workflow_episodes(snapshot)),
            )
            insert_workflow_component(db, snapshot.revision.fence.key, snapshot.authority, snapshot.episode.episode_id)
            if snapshot.gates:
                persist_workflow_snapshot(db, snapshot)
                held += 1
            baselines += target.baseline is not None
            guided += bool(target.guidance)
            db.execute(
                "INSERT INTO workflow_activation_imports VALUES (?, ?, ?, ?, ?, ?, ?)",
                (plan.repository, _FAMILY, plan.epoch, *scope[1:], plan.source_digest, target.canonical()),
            )


__all__ = [
    "LEGACY_REVIEW_NAME", "ReviewActivationRefused", "ReviewActivationSourcesChanged", "SQLiteReviewActivation",
    "read_legacy_review_roots", "read_legacy_review_state",
    "read_open_pull_requests", "read_review_inventory",
]
