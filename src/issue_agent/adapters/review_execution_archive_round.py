"""Bounded source observation for one finalized execution round."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import json
import sqlite3

from ..domain.review_execution import (
    CopiedReviewProgress, PaidReviewProgress, ReviewRoundIdentity,
    review_generation_chain_digest, same_execution_episode,
)
from ..domain.review_rounds import (
    FinalizedReviewRound, ReviewRoundFacts, ReviewRoundPlan, ReviewSubmissionRegistration,
    StaleReviewProductivitySettlement, ReviewSubmissionConfirmed, finalize_review_round,
)
from ..domain.workflow_identity import WorkflowFence, WorkflowRevision
from ..domain.review_workflow import (
    BoundReviewSubmission, PullRequestState, RecordedPullRequest, ReviewedBaseline,
    ReviewWorkflowSnapshot,
)
from ..domain.review_workflow_policy import (
    OBSERVATION_MAX_AGE_SECONDS, CeilingProductivityDecision, open_review_ceiling,
)
from ..domain.workflow import (
    ObservedTransition, WorkflowChanged, WorkflowMutation, WorkflowRejected,
    WorkflowUnchanged,
)
from .review_execution_activation import (
    BlockedReviewGenerationArchivePage, read_checkpoint_round_by_sequence,
    read_review_generation_archive_page,
)
from .review_execution_archive_claim import observe_review_execution_archive_claim
from .review_round_receipt_sweep import read_classified_checkpoint_round_receipt_members
from .review_round_sources import read_review_round_sources
from .sql_read_budget import bounded_sql_source_reads
from .workflow_codec import decode_workflow, encode_workflow
from .workflow_rows import WorkflowAuthorityError


MAX_ARCHIVE_ROUND_LINEAGE_BYTES = 2**27
_MAX_FIELD_BYTES = 2**21
_TABLES = frozenset((
    "runs", "budget_reservations", "reservation_recovery", "reservation_limits",
    "quota_debits", "charges", "run_cancellation_intents",
    "lifecycle_commands", "effect_receipts", "workflow_evidence",
))
_CHILD_RECEIPTS = (
    "review-execution-admission", "review-execution-refusal",
    "review-execution-launch", "review-execution-preflight-stop",
    "review-execution-release", "review-execution-return",
)


@dataclass(frozen=True)
class ObservedReviewExecutionArchiveRound:
    round: ReviewRoundIdentity
    source_sha256: str
    source_bytes: int

    def __post_init__(self) -> None:
        if (not isinstance(self.round, ReviewRoundIdentity)
                or len(self.source_sha256) != 64
                or any(char not in "0123456789abcdef" for char in self.source_sha256)
                or type(self.source_bytes) is not int
                or not 0 < self.source_bytes <= MAX_ARCHIVE_ROUND_LINEAGE_BYTES):
            raise ValueError("archive round requires a bounded source fingerprint")


def _octets(column: str) -> str:
    return (f"ifnull(octet_length({column}),0)" if sqlite3.sqlite_version_info >= (3, 43, 0)
            else f"ifnull(length(CAST({column} AS BLOB)),0)")


class _Sources:
    def __init__(self, db: sqlite3.Connection, maximum: int, indexed_identities: bool) -> None:
        self.db = db
        self.maximum = maximum
        self.indexed_identities = indexed_identities
        self.used = 0
        self.entries: list[object] = []
        self.columns: dict[str, tuple[str, ...]] = {}

    def charge(self, size: int) -> None:
        if type(size) is not int or size < 0 or self.used + size > self.maximum:
            raise WorkflowAuthorityError("archive round exceeds its cumulative byte bound")
        self.used += size

    def _columns(self, table: str) -> tuple[str, ...]:
        if table not in _TABLES:
            raise ValueError("archive round requested an unknown source table")
        if table not in self.columns:
            names = tuple(column[0] for column in self.db.execute(
                f"SELECT * FROM {table} LIMIT 0",
            ).description)
            if not names or any(not name.isidentifier() for name in names):
                raise WorkflowAuthorityError("archive round source schema is unreadable")
            self.columns[table] = names
        return self.columns[table]

    def exact_rowid(self, table: str, column: str, identity: str) -> int | None:
        if column not in self._columns(table) or not isinstance(identity, str):
            raise ValueError("archive round identity is invalid")
        rows = self.db.execute(
            f"SELECT rowid AS source_rowid,typeof({column}) AS identity_type FROM {table} "
            f"WHERE {column} IN (?,CAST(? AS BLOB)) LIMIT 2",
            (identity, identity),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1 or rows[0]["identity_type"] != "text":
            raise WorkflowAuthorityError("archive round source identity is aliased")
        return rows[0]["source_rowid"]

    def read(self, table: str, rowid: int) -> dict[str, object]:
        columns = self._columns(table)
        sizes = ",".join(f"{_octets(name)} AS size_{name}" for name in columns)
        types = ",".join(f"typeof({name}) AS type_{name}" for name in columns)
        metadata = self.db.execute(
            f"SELECT {sizes},{types} FROM {table} WHERE rowid=?", (rowid,),
        ).fetchone()
        if metadata is None:
            raise WorkflowAuthorityError("archive round source disappeared")
        total = 0
        for name in columns:
            size = metadata[f"size_{name}"]
            if (type(size) is not int or size > _MAX_FIELD_BYTES
                    or metadata[f"type_{name}"] not in {"text", "integer", "real", "null"}):
                raise WorkflowAuthorityError("archive round source field is malformed or oversized")
            total += size
        self.charge(total)
        row = self.db.execute(
            f"SELECT {','.join(columns)} FROM {table} WHERE rowid=?", (rowid,),
        ).fetchone()
        if row is None:
            raise WorkflowAuthorityError("archive round source changed during its read")
        values = dict(zip(columns, tuple(row), strict=True))
        self.entries.append((table, tuple((name, metadata[f"type_{name}"], values[name])
                                                 for name in columns)))
        return values

    def optional(self, table: str, identity: str) -> dict[str, object] | None:
        column = ("command_id" if table in {"lifecycle_commands", "effect_receipts"}
                  else "evidence_id" if table == "workflow_evidence" else "run_id")
        rowid = self.exact_rowid(table, column, identity)
        return self.read(table, rowid) if rowid is not None else None

    def rows_for_run(self, table: str, run_id: str) -> tuple[dict[str, object], ...]:
        if table not in {"reservation_limits", "quota_debits"}:
            raise ValueError("archive round requested an unknown multirow source")
        order = ("repository,dimension,period_id,currency" if table == "reservation_limits"
                 else "quota")
        rows = self.db.execute(
            f"SELECT rowid,typeof(run_id) AS identity_type FROM {table} "
            f"WHERE run_id IN (?,CAST(? AS BLOB)) ORDER BY {order} LIMIT 129",
            (run_id, run_id),
        ).fetchall()
        if len(rows) > 128 or any(row["identity_type"] != "text" for row in rows):
            raise WorkflowAuthorityError("archive round attached accounting source is unbounded or aliased")
        return tuple(self.read(table, row["rowid"]) for row in rows)

    def dedupe(self, namespace: str, repository: str, key: str) -> dict[str, object] | None:
        if self.indexed_identities:
            from .review_archive_identity import matching_review_archive_identity_rowids

            matching_review_archive_identity_rowids(self.db, "dedupe_keys", (namespace, repository, key))
        rows = self.db.execute(
            "SELECT rowid,typeof(namespace) AS namespace_type,typeof(scope) AS scope_type,"
            "typeof(key) AS key_type FROM dedupe_keys "
            "WHERE namespace IN (?,CAST(? AS BLOB)) "
            "AND scope IN (?,CAST(? AS BLOB)) AND key IN (?,CAST(? AS BLOB)) LIMIT 2",
            (namespace, namespace, repository, repository, key, key),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1 or any(rows[0][f"{name}_type"] != "text" for name in (
            "namespace", "scope", "key",
        )):
            raise WorkflowAuthorityError("archive round receipt identity is aliased")
        sizes = ",".join(f"{_octets(name)} AS size_{name}" for name in (
            "namespace", "scope", "key", "claimed_at", "reusable_after", "payload_json",
        ))
        row = self.db.execute(f"SELECT {sizes} FROM dedupe_keys WHERE rowid=?", (rows[0]["rowid"],)).fetchone()
        if row is None:
            raise WorkflowAuthorityError("archive round receipt disappeared")
        size = sum(row)
        if any(type(value) is not int or value > _MAX_FIELD_BYTES for value in row):
            raise WorkflowAuthorityError("archive round receipt exceeds its field bound")
        self.charge(size)
        source = self.db.execute(
            "SELECT namespace,scope,key,claimed_at,reusable_after,payload_json "
            "FROM dedupe_keys WHERE rowid=?", (rows[0]["rowid"],),
        ).fetchone()
        if (source is None or tuple(source[:3]) != (namespace, repository, key)
                or source[4] is not None or not isinstance(source[5], str)):
            raise WorkflowAuthorityError("archive round receipt is malformed")
        values = dict(zip(("namespace", "scope", "key", "claimed_at", "reusable_after", "payload_json"),
                          tuple(source), strict=True))
        self.entries.append(("dedupe_keys", tuple(values.items())))
        return values


def _observation_evidence(
    sources: _Sources, evidence_id: str, identity: ReviewRoundIdentity,
) -> RecordedPullRequest:
    source = sources.optional("workflow_evidence", evidence_id)
    key = identity.binding.rejection.fence.key
    if (source is None or source["kind"] != "observation" or source["command_id"] is not None
            or (source["repository"], source["target_kind"], source["target_number"])
            != (key.repository, "pr", key.target.number)
            or not isinstance(source["payload_json"], str)
            or evidence_id != "pr-observation:" + hashlib.sha256(
                source["payload_json"].encode()).hexdigest()):
        raise WorkflowAuthorityError("archive round lacks its exact productivity observation")
    return decode_workflow(source["payload_json"], RecordedPullRequest)


def _aware_stamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise WorkflowAuthorityError("archive round productivity time is malformed")
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise WorkflowAuthorityError("archive round productivity time is not aware")
    return stamp


def _nonbaseline_productivity(
    sources: _Sources, identity: ReviewRoundIdentity, facts: ReviewRoundFacts,
    outcome: object, revision: WorkflowRevision, command_id: str,
) -> None:
    repository = identity.binding.rejection.fence.key.repository
    round_id = identity.round_id
    candidates = {
        namespace: sources.dedupe(namespace, repository, round_id)
        for namespace in (
            "review-round-stale-productivity", "review-round-superseded-productivity",
            "review-round-ceiling-productivity",
        )
    }
    present = [(namespace, source) for namespace, source in candidates.items() if source is not None]
    if len(present) != 1:
        raise WorkflowAuthorityError("archive round lacks one exact productive settlement")
    namespace, receipt = present[0]
    assert receipt is not None
    if namespace == "review-round-stale-productivity":
        stale = decode_workflow(receipt["payload_json"], tuple | StaleReviewProductivitySettlement)
        if not isinstance(stale, StaleReviewProductivitySettlement):
            raise WorkflowAuthorityError("archive round legacy stale settlement lacks generation proof")
        old_generation = identity.binding.rejection.fence.generation
        settled_generation = stale.settled_revision.fence.generation
        if (stale.facts != facts or stale.outcome != outcome
                or stale.settled_revision.fence.key != revision.fence.key
                or stale.settled_revision.version > revision.version
                and stale.settled_revision.fence == revision.fence
                or settled_generation > revision.fence.generation
                or not same_execution_episode(stale.settled_binding, identity.binding)
                or settled_generation - old_generation > 4096):
            raise WorkflowAuthorityError("archive round stale witness disagrees with its retirement")
        generation = old_generation + 1
        preceding = None
        boundaries = []
        while generation <= settled_generation:
            page = read_review_generation_archive_page(
                sources.db, identity.binding, generation, settled_generation,
                preceding=preceding, max_source_bytes=sources.maximum - sources.used,
            )
            if isinstance(page, BlockedReviewGenerationArchivePage) or not page.sources:
                raise WorkflowAuthorityError("archive round stale generation proof is incomplete")
            for item in page.sources:
                sources.charge(item.source_bytes)
                sources.entries.append(("generation", item.generation, item.receipt_digest,
                                        item.accepted_namespace, item.accepted_key,
                                        item.accepted_digest))
                boundaries.append(item.boundary)
            generation = page.next_generation
            preceding = page.sources[-1].boundary
        if (len(boundaries) != settled_generation - old_generation
                or boundaries[-1].to_revision.version > stale.settled_revision.version
                or any(not same_execution_episode(boundary.before.binding, identity.binding)
                       or not same_execution_episode(boundary.after.binding, identity.binding)
                       for boundary in boundaries)
                or stale.crossing_digest != review_generation_chain_digest(
                    tuple(boundary.receipt_id for boundary in boundaries)
                )):
            raise WorkflowAuthorityError("archive round stale generation chain changed")
        return
    recorded = decode_workflow(receipt["payload_json"], tuple)
    if namespace == "review-round-superseded-productivity":
        if (len(recorded) != 3 or not isinstance(recorded[0], str)
                or not isinstance(recorded[1], WorkflowRevision)):
            raise WorkflowAuthorityError("archive round supersession witness is malformed")
        evidence_id, recorded_revision, settled_stamp = recorded
        observation = _observation_evidence(sources, evidence_id, identity).observation
        age = (_aware_stamp(settled_stamp) - observation.observed_at).total_seconds()
        if (observation.key != revision.fence.key
                or observation.head_sha == facts.plan.head_sha
                and observation.state is PullRequestState.OPEN
                or not 0 <= age <= OBSERVATION_MAX_AGE_SECONDS
                or recorded_revision.fence != identity.binding.rejection.fence
                or recorded_revision.version > revision.version
                and recorded_revision.fence == revision.fence):
            raise WorkflowAuthorityError("archive round supersession observation changed")
        return
    if (len(recorded) != 3 or not isinstance(recorded[0], CeilingProductivityDecision)
            or not isinstance(recorded[1], WorkflowRevision)):
        raise WorkflowAuthorityError("archive round ceiling witness is malformed")
    decision, recorded_revision, settled_stamp = recorded
    if (decision.escalation.escalate_cause != "ceiling"
            or str(decision.escalation.submission.command_id) != command_id
            or decision.escalation.submission.binding != identity.binding.rejection
            or decision.prior.revision.fence != identity.binding.rejection.fence
            or recorded_revision.fence != identity.binding.rejection.fence
            or recorded_revision.version > revision.version
            and recorded_revision.fence == revision.fence):
        raise WorkflowAuthorityError("archive round ceiling escalation changed")
    observation = _observation_evidence(sources, decision.observation_evidence_id,
                                        identity).observation
    settled_at = _aware_stamp(settled_stamp)
    if (observation.key != revision.fence.key
            or not 0 <= (decision.decided_at - observation.observed_at).total_seconds()
            <= OBSERVATION_MAX_AGE_SECONDS
            or not 0 <= (settled_at - observation.observed_at).total_seconds()
            <= OBSERVATION_MAX_AGE_SECONDS):
        raise WorkflowAuthorityError("archive round ceiling observation expired")
    replayed = open_review_ceiling(
        decision.prior, observation, now=decision.decided_at,
        rule=decision.configured_rule, ceiling=decision.configured_ceiling,
        escalation=decision.escalation, trigger_label=decision.trigger_label,
        executor_identity=decision.executor_identity,
        evidence_ids=(decision.observation_evidence_id,),
    )
    if (isinstance(replayed, WorkflowRejected)
            and (replayed != decision.refusal or recorded_revision != decision.prior.revision)
            or isinstance(replayed, WorkflowChanged)
            and (decision.refusal is not None or recorded_revision != replayed.snapshot.revision)
            or isinstance(replayed, WorkflowUnchanged)
            and (decision.refusal is not None or recorded_revision != decision.prior.revision)):
        raise WorkflowAuthorityError("archive round ceiling replay changed")


def observe_review_execution_archive_round(
    db: sqlite3.Connection, round_identity: ReviewRoundIdentity,
    *, max_source_bytes: int = MAX_ARCHIVE_ROUND_LINEAGE_BYTES,
    indexed_identities: bool = False,
) -> ObservedReviewExecutionArchiveRound:
    """Check one finalized round's retained local and external lineage."""
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or not isinstance(round_identity, ReviewRoundIdentity)
            or type(indexed_identities) is not bool
            or type(max_source_bytes) is not int
            or not 1 <= max_source_bytes <= MAX_ARCHIVE_ROUND_LINEAGE_BYTES):
        raise ValueError("archive round requires a transaction, round and bounded budget")
    try:
        with bounded_sql_source_reads(db, max_source_bytes) as budget:
            observed = _observe(db, round_identity, max_source_bytes, indexed_identities)
            return replace(observed, source_bytes=budget.source_bytes)
    except (ValueError, TypeError, KeyError, UnicodeError, sqlite3.Error) as exc:
        if isinstance(exc, WorkflowAuthorityError):
            raise
        raise WorkflowAuthorityError("archive round source is unreadable") from exc


def _observe(db: sqlite3.Connection, identity: ReviewRoundIdentity, maximum: int,
             indexed_identities: bool) -> ObservedReviewExecutionArchiveRound:
    sources = _Sources(db, maximum, indexed_identities)
    local = read_checkpoint_round_by_sequence(
        db, identity.binding, identity.sequence, max_source_bytes=maximum,
    )
    if local.outcome.round != identity:
        raise WorkflowAuthorityError("archive round sequence has another identity")
    sources.charge(local.source_bytes)
    classified = read_classified_checkpoint_round_receipt_members(
        db, identity, max_source_bytes=maximum - sources.used,
    )
    if classified.source.source != local.source:
        raise WorkflowAuthorityError("archive round receipt set changed during classification")
    sources.charge(classified.source_bytes)
    claim = observe_review_execution_archive_claim(
        db, identity, max_source_bytes=min(4 * 1024 * 1024, maximum - sources.used),
        indexed_identities=indexed_identities,
    )
    if claim.terminal_kind == "live":
        raise WorkflowAuthorityError("archive round parent claim has not terminated")
    sources.charge(claim.source_bytes)
    sources.entries.extend((("round", local.source.row_sha256, local.source.receipt_set_sha256),
                            ("claim", claim.source_sha256, claim.terminal_kind)))
    from .review_execution_activation import read_checkpoint_round_row_source
    row = read_checkpoint_round_row_source(db, identity, max_source_bytes=maximum - sources.used)
    values = dict(row.fields)
    if row.source_digest != local.source.row_sha256:
        raise WorkflowAuthorityError("archive round row changed during replay")
    sources.charge(sum(len(str(value).encode()) for value in values.values() if value is not None))
    plan = decode_workflow(values["plan_json"], ReviewRoundPlan)
    facts = decode_workflow(values["closure_json"], ReviewRoundFacts)
    if (facts.plan != plan or facts.plan.round != identity
            or not isinstance(finalize_review_round(facts), FinalizedReviewRound)
            or finalize_review_round(facts).outcome != local.outcome):
        raise WorkflowAuthorityError("archive round closure changed")
    repository = identity.binding.rejection.fence.key.repository
    workflow = db.execute(
        "SELECT generation,version FROM workflows WHERE repository=? AND target_kind='pr' AND target_number=?",
        (repository, identity.binding.rejection.fence.key.target.number),
    ).fetchone()
    if workflow is None or any(type(workflow[name]) is not int for name in ("generation", "version")):
        raise WorkflowAuthorityError("archive round lacks its workflow revision")
    revision = WorkflowRevision(WorkflowFence(identity.binding.rejection.fence.key, workflow["generation"]),
                                workflow["version"])
    command_sources: dict[str, tuple[dict[str, object], dict[str, object] | None]] = {}
    for submission in plan.submissions:
        registration_row = sources.dedupe("review-round-submission", repository, submission.command_id)
        if registration_row is None:
            raise WorkflowAuthorityError("archive round lost its planned submission")
        registration = decode_workflow(registration_row["payload_json"], ReviewSubmissionRegistration)
        if (registration.round != identity or registration.submission != submission
                or registration.owner_id != claim.owner_id
                or registration.claim_occurrence_id != claim.occurrence_id):
            raise WorkflowAuthorityError("archive round submission changed its claim")
        command = sources.optional("lifecycle_commands", submission.command_id)
        if command is None or command["repository"] != repository or command["executor_identity"] != claim.owner_id:
            raise WorkflowAuthorityError("archive round submission lost its command")
        effect = sources.optional("effect_receipts", submission.command_id)
        if (command["state"] not in {"succeeded", "permanent_failure"}
                or effect is None or effect["status"] != command["state"]):
            raise WorkflowAuthorityError("archive round submission has an unsettled effect")
        command_sources[submission.command_id] = command, effect
    if isinstance(local.outcome.disposition, (CopiedReviewProgress, PaidReviewProgress)):
        recorded_ids = tuple(
            member.key for member in classified.source.members
            if member.namespace == "review-recorded-submission"
        )
        if len(recorded_ids) == 1:
            recorded = sources.dedupe("review-recorded-submission", repository, recorded_ids[0])
            if recorded is None:
                raise WorkflowAuthorityError("archive round lost its recorded baseline")
            after_revision = decode_workflow(recorded["payload_json"], WorkflowRevision)
            if after_revision.version < 1:
                raise WorkflowAuthorityError("archive round recorded baseline has no predecessor")
            before_revision = WorkflowRevision(after_revision.fence, after_revision.version - 1)
            transition_key = hashlib.sha256(encode_workflow(before_revision).encode()).hexdigest()
            accepted = sources.dedupe("workflow-transition", repository, transition_key)
            if accepted is None:
                raise WorkflowAuthorityError("archive round lost its accepted review transition")
            payload = json.loads(accepted["payload_json"])
            if (not isinstance(payload, dict)
                    or json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
                    != accepted["payload_json"]
                    or payload.get("mutation") != WorkflowMutation.RECORD_REVIEW.value
                    or payload.get("expected") != encode_workflow(before_revision)
                    or not isinstance(payload.get("snapshot"), str)
                    or not isinstance(payload.get("audit"), str)):
                raise WorkflowAuthorityError("archive round accepted review is malformed")
            snapshot = decode_workflow(payload["snapshot"], ReviewWorkflowSnapshot)
            audit = decode_workflow(payload["audit"], ObservedTransition)
            command, effect = command_sources[recorded_ids[0]]
            command_payload = json.loads(command["payload_json"])
            baseline = snapshot.episode.continuation
            if (snapshot.revision != after_revision
                    or not isinstance(command_payload, dict)
                    or not isinstance(baseline, ReviewedBaseline)
                    or not isinstance(baseline.source, BoundReviewSubmission)
                    or str(baseline.source.command_id) != recorded_ids[0]
                    or baseline.source.binding != identity.binding.rejection
                    or baseline.head_sha != command_payload.get("head")
                    or baseline.patch_id != command_payload.get("patch_id")
                    or effect is None or effect["status"] != "succeeded"
                    or not isinstance(effect["remote_object_id"], str)
                    or len(audit.evidence_ids) < 2
                    or audit.evidence_ids[:2] != (
                        recorded_ids[0], f"github-review:{effect['remote_object_id']}",
                    )):
                raise WorkflowAuthorityError("archive round accepted review changed its submission")
        else:
            confirmed = tuple(
                item.plan.command_id for item in facts.submissions
                if isinstance(item.result, ReviewSubmissionConfirmed)
            )
            if len(confirmed) != 1:
                raise WorkflowAuthorityError("archive round lacks one confirmed submission")
            _nonbaseline_productivity(
                sources, identity, facts, local.outcome, revision, confirmed[0],
            )
    run_rows = db.execute(
        "SELECT rowid,typeof(review_round_id) AS identity_type FROM runs "
        "WHERE review_round_id IN (?,CAST(? AS BLOB)) ORDER BY run_id LIMIT ?",
        (identity.round_id, identity.round_id, len(plan.children) + 1),
    ).fetchall()
    if len(run_rows) > len(plan.children) or any(row["identity_type"] != "text" for row in run_rows):
        raise WorkflowAuthorityError("archive round has extra or aliased paid runs")
    for indexed in run_rows:
        run = sources.read("runs", indexed["rowid"])
        run_id = run["run_id"]
        if not isinstance(run_id, str) or run["review_round_id"] != identity.round_id:
            raise WorkflowAuthorityError("archive round paid run identity changed")
        if db.execute("SELECT 1 FROM leases WHERE run_id=?", (run_id,)).fetchone() is not None:
            raise WorkflowAuthorityError("archive round paid run retains a live lease")
        cancellation = sources.optional("run_cancellation_intents", run_id)
        if cancellation is not None and (
            cancellation["handover_written_at"] is None
            or cancellation["dispatch_id"] != run["dispatch_id"]
            or cancellation["kind"] != run["kind"]
        ):
            raise WorkflowAuthorityError("archive round cancellation intent has incomplete handover")
        for table in ("budget_reservations", "reservation_recovery", "charges"):
            source = sources.optional(table, run_id)
            if table in ("budget_reservations", "reservation_recovery") and source is None:
                raise WorkflowAuthorityError("archive round paid run lost accounting provenance")
        if not sources.rows_for_run("reservation_limits", run_id):
            raise WorkflowAuthorityError("archive round paid run lost its reservation limit")
        sources.rows_for_run("quota_debits", run_id)
        for namespace in _CHILD_RECEIPTS:
            if namespace != "review-execution-refusal":
                sources.dedupe(namespace, repository, run_id)
    for child in plan.children:
        key = json.dumps((identity.round_id, child.child_id), separators=(",", ":"))
        sources.dedupe("review-execution-child-admission", repository, key)
        sources.dedupe("review-execution-refusal", repository, key)
    from .review_execution_claims import load_review_preflight_record
    stored = load_review_preflight_record(db, repository, claim.owner_id, identity.round_id)
    if stored is None or stored[0].claim_occurrence_id != claim.occurrence_id:
        raise WorkflowAuthorityError("archive round preflight changed")
    reconstructed = read_review_round_sources(
        db, repository, claim.owner_id, stored[0], plan, facts.planning, revision,
    )
    if reconstructed != facts:
        raise WorkflowAuthorityError("archive round external sources disagree with closure")
    digest = hashlib.sha256(json.dumps(sources.entries, separators=(",", ":"),
                                       ensure_ascii=True).encode()).hexdigest()
    return ObservedReviewExecutionArchiveRound(identity, digest, sources.used)
