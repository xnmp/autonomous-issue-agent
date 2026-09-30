"""Global archive identities become usable only after a complete stable census."""

from datetime import timedelta
import sqlite3

import pytest

from issue_agent.adapters.review_archive_identity import (
    advance_review_archive_identity_audit, matching_review_archive_identity_rowids,
    require_review_archive_identity_audit,
)
from issue_agent.adapters.workflow_codec import encode_workflow
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from issue_agent.domain.review_archive_identity import (
    BlockedReviewArchiveIdentities, CompletedReviewArchiveIdentities, ReviewArchiveIdentityCursor,
    ScanningReviewArchiveIdentities,
)
from tests.adapters.test_review_execution_control import rig


def _finish(store, now):
    for calls in range(1, 65):
        with store.transaction() as db:
            state = advance_review_archive_identity_audit(db, now=now)
        if isinstance(state, CompletedReviewArchiveIdentities):
            return state, calls
    raise AssertionError("identity census did not complete")


def _insert(db, *, rowid=None, namespace="foreign", key="key"):
    db.execute(
        "INSERT INTO dedupe_keys(rowid,namespace,scope,key,claimed_at,payload_json) "
        "VALUES (?,?,?,?,?,?)",
        (rowid, namespace, "o/r", key, "2026-09-29T00:00:00+00:00", "{}"),
    )


def test_paged_projection_indexes_exact_and_negative_rowids(rig):
    store = rig[0]
    now = store.now()
    with store.transaction() as db:
        _insert(db, rowid=-99, namespace="review-round-preflight", key="negative")
        for index in range(150):
            _insert(db, namespace="foreign", key=f"irrelevant:{index}")
    state, calls = _finish(store, now)
    assert calls > 3
    assert sum(state.counts) >= 151
    with store.transaction() as db:
        assert require_review_archive_identity_audit(db, now=now) == state
        assert matching_review_archive_identity_rowids(
            db, "dedupe_keys", ("review-round-preflight", "o/r", "negative"),
        ) == [-99]
        assert len(matching_review_archive_identity_rowids(
            db, "dedupe_keys", ("foreign", "o/r", "irrelevant:0"),
        )) == 1
        with pytest.raises(WorkflowAuthorityError, match="match bound"):
            matching_review_archive_identity_rowids(
                db, "dedupe_keys", ("foreign", "o/r", "irrelevant:"), prefix_last=True,
            )


def test_changed_source_restarts_incomplete_projection_and_requires_new_completion(rig):
    store = rig[0]
    now = store.now()
    with store.transaction() as db:
        first = advance_review_archive_identity_audit(db, now=now)
        assert isinstance(first, ScanningReviewArchiveIdentities)
        _insert(db, namespace="foreign", key="late")
        with pytest.raises(WorkflowAuthorityError, match="incomplete or stale"):
            require_review_archive_identity_audit(db, now=now)
    completed, _ = _finish(store, now)
    assert completed.source_versions != first.source_versions
    with store.transaction() as db:
        assert len(matching_review_archive_identity_rowids(
            db, "dedupe_keys", ("foreign", "o/r", "late"),
        )) == 1


@pytest.mark.parametrize("namespace", ["\u00a0alias\u00a0", b"alias"])
def test_unicode_whitespace_and_blob_aliases_fail_selected_lookup(rig, namespace):
    store = rig[0]
    now = store.now()
    with store.transaction() as db:
        _insert(db, namespace=namespace, key="same")
    _finish(store, now)
    with store.transaction() as db:
        with pytest.raises(WorkflowAuthorityError, match="aliased"):
            matching_review_archive_identity_rowids(
                db, "dedupe_keys", ("alias", "o/r", "same"),
            )


def test_oversized_identity_blocks_until_changed_source_is_repaired(rig):
    store = rig[0]
    now = store.now()
    with store.transaction() as db:
        _insert(db, namespace="too-long", key="x" * 513)
    for _ in range(8):
        with store.transaction() as db:
            state = advance_review_archive_identity_audit(db, now=now)
        if isinstance(state, BlockedReviewArchiveIdentities):
            break
    else:
        raise AssertionError("oversized identity was not rejected")
    with store.transaction() as db:
        assert advance_review_archive_identity_audit(db, now=now) == state
        with pytest.raises(WorkflowAuthorityError):
            require_review_archive_identity_audit(db, now=now)
        db.execute("DELETE FROM dedupe_keys WHERE namespace='too-long'")
    repaired, _ = _finish(store, now + timedelta(minutes=1))
    assert repaired.source_versions != state.prior.source_versions


def test_completed_projection_expires_and_restarts_at_deadline(rig):
    store = rig[0]
    now = store.now()
    completed, _ = _finish(store, now)
    with store.transaction() as db:
        with pytest.raises(WorkflowAuthorityError, match="stale"):
            require_review_archive_identity_audit(db, now=completed.next_due_at)
        fresh = advance_review_archive_identity_audit(db, now=completed.next_due_at)
    assert isinstance(fresh, ScanningReviewArchiveIdentities)
    assert fresh.started_at == completed.next_due_at


@pytest.mark.parametrize("operation", ["update", "delete", "replace"])
def test_member_tamper_invalidates_complete_projection_until_full_rebuild(rig, operation):
    store = rig[0]
    now = store.now()
    with store.transaction() as db:
        _insert(db, namespace="selected", key="member")
    completed, _ = _finish(store, now)
    with store.transaction() as db:
        source_rowid = matching_review_archive_identity_rowids(
            db, "dedupe_keys", ("selected", "o/r", "member"),
        )[0]
        if operation == "update":
            db.execute(
                "UPDATE review_archive_identity_members SET first='hidden' "
                "WHERE source_table='dedupe_keys' AND source_rowid=?", (source_rowid,),
            )
        elif operation == "delete":
            db.execute(
                "DELETE FROM review_archive_identity_members "
                "WHERE source_table='dedupe_keys' AND source_rowid=?", (source_rowid,),
            )
        else:
            db.execute(
                "INSERT OR REPLACE INTO review_archive_identity_members "
                "SELECT source_table,source_rowid,snapshot_sha256,'hidden',second,third,"
                "types_json,values_json FROM review_archive_identity_members "
                "WHERE source_table='dedupe_keys' AND source_rowid=?", (source_rowid,),
            )
        with pytest.raises(WorkflowAuthorityError, match="stale"):
            require_review_archive_identity_audit(db, now=now)
        with pytest.raises(WorkflowAuthorityError, match="stale"):
            matching_review_archive_identity_rowids(
                db, "dedupe_keys", ("selected", "o/r", "member"),
            )
    repaired, _ = _finish(store, now + timedelta(minutes=1))
    assert repaired.source_versions == completed.source_versions
    with store.transaction() as db:
        assert matching_review_archive_identity_rowids(
            db, "dedupe_keys", ("selected", "o/r", "member"),
        ) == [source_rowid]


def test_missing_or_overflowed_projection_witness_refuses_mutation(rig):
    store = rig[0]
    now = store.now()
    _finish(store, now)
    with store.transaction() as db:
        db.execute(
            "UPDATE review_archive_source_versions SET version=9223372036854775806 "
            "WHERE source='identity_members' AND scope=X''",
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE review_archive_identity_members SET first=first")
        db.execute(
            "DELETE FROM review_archive_source_versions "
            "WHERE source='identity_members' AND scope=X''",
        )
        with pytest.raises(WorkflowAuthorityError, match="witness is absent"):
            require_review_archive_identity_audit(db, now=now)
        with pytest.raises(sqlite3.IntegrityError, match="witness is absent"):
            db.execute("UPDATE review_archive_identity_members SET first=first")


def test_rolled_back_member_change_preserves_completed_authority(rig):
    store = rig[0]
    now = store.now()
    completed, _ = _finish(store, now)
    with pytest.raises(RuntimeError, match="abort"):
        with store.transaction() as db:
            db.execute("UPDATE review_archive_identity_members SET first='temporary'")
            raise RuntimeError("abort")
    with store.transaction() as db:
        assert require_review_archive_identity_audit(db, now=now) == completed


def test_forged_completed_cursor_without_a_scan_or_receipt_cannot_authorize_lookup(rig):
    store = rig[0]
    now = store.now()
    with store.transaction() as db:
        rows = db.execute(
            "SELECT source,version FROM review_archive_source_versions "
            "WHERE scope=X'' AND source IN ('all_round_rows','lifecycle_rows','identity_members')",
        ).fetchall()
        versions = {row[0]: row[1] for row in rows}
        forged = CompletedReviewArchiveIdentities(
            (versions["all_round_rows"], versions["lifecycle_rows"]),
            versions["identity_members"], (0, 0, 0), now, now + timedelta(hours=1),
        )
        db.execute(
            "INSERT INTO level_state(namespace,scope,key,payload_json,updated_at) "
            "VALUES (?,?,?,?,?)",
            ("review-archive-identity-scan-v1", "global", "identities",
             encode_workflow(ReviewArchiveIdentityCursor(forged, versions["identity_members"])),
             now.isoformat()),
        )
        with pytest.raises(WorkflowAuthorityError, match="incomplete or stale"):
            require_review_archive_identity_audit(db, now=now)
        with pytest.raises(WorkflowAuthorityError, match="incomplete or stale"):
            matching_review_archive_identity_rowids(
                db, "dedupe_keys", ("foreign", "o/r", "missing"),
            )
    completed, _ = _finish(store, now)
    assert sum(completed.counts) > 0


def test_forged_final_page_cursor_cannot_skip_alias_census(rig):
    store = rig[0]
    now = store.now()
    with store.transaction() as db:
        _insert(db, namespace="\u00a0alias\u00a0", key="selected")
        rows = db.execute(
            "SELECT source,version FROM review_archive_source_versions "
            "WHERE scope=X'' AND source IN ('all_round_rows','lifecycle_rows','identity_members')",
        ).fetchall()
        versions = {row[0]: row[1] for row in rows}
        upper = db.execute("SELECT MAX(rowid) FROM dispatch_claims").fetchone()[0]
        forged = ScanningReviewArchiveIdentities(
            (versions["all_round_rows"], versions["lifecycle_rows"]),
            versions["identity_members"], "dispatch_claims", upper, upper, (0, 0, 0),
            now, now + timedelta(hours=1),
        )
        db.execute(
            "INSERT INTO level_state(namespace,scope,key,payload_json,updated_at) "
            "VALUES (?,?,?,?,?)",
            ("review-archive-identity-scan-v1", "global", "identities",
             encode_workflow(ReviewArchiveIdentityCursor(forged, versions["identity_members"])),
             now.isoformat()),
        )
    with store.transaction() as db:
        first = advance_review_archive_identity_audit(db, now=now)
    assert isinstance(first, ScanningReviewArchiveIdentities)
    assert first.counts[0] > 0
    completed, _ = _finish(store, now)
    assert completed.counts[0] > 0
    with store.transaction() as db:
        with pytest.raises(WorkflowAuthorityError, match="aliased"):
            matching_review_archive_identity_rowids(
                db, "dedupe_keys", ("alias", "o/r", "selected"),
            )


@pytest.mark.parametrize("operation", ["update", "delete", "replace"])
def test_changed_completion_receipt_invalidates_then_rebuilds_certification(rig, operation):
    store = rig[0]
    now = store.now()
    completed, _ = _finish(store, now)
    with store.transaction() as db:
        row = db.execute(
            "SELECT projection_version FROM review_archive_identity_certifications "
            "WHERE round_rows_version=? AND lifecycle_version=?",
            completed.source_versions,
        ).fetchone()
        assert row is not None
        version = row[0]
        if operation == "update":
            db.execute(
                "UPDATE review_archive_identity_certifications SET state_json='{}' "
                "WHERE round_rows_version=? AND lifecycle_version=? AND projection_version=?",
                (*completed.source_versions, version),
            )
        elif operation == "delete":
            db.execute(
                "DELETE FROM review_archive_identity_certifications "
                "WHERE round_rows_version=? AND lifecycle_version=? AND projection_version=?",
                (*completed.source_versions, version),
            )
        else:
            db.execute(
                "INSERT OR REPLACE INTO review_archive_identity_certifications "
                "(round_rows_version,lifecycle_version,projection_version,state_json) "
                "VALUES (?,?,?,'{}')", (*completed.source_versions, version),
            )
        with pytest.raises(WorkflowAuthorityError, match="incomplete or stale"):
            require_review_archive_identity_audit(db, now=now)
    repaired, _ = _finish(store, now + timedelta(minutes=1))
    assert repaired.source_versions == completed.source_versions
    with store.transaction() as db:
        assert require_review_archive_identity_audit(
            db, now=now + timedelta(minutes=1),
        ) == repaired


@pytest.mark.parametrize("lost_cursor", [False, True])
def test_orphan_member_from_old_cohort_cannot_survive_rebuild_lookup(rig, lost_cursor):
    store = rig[0]
    now = store.now()
    completed, _ = _finish(store, now)
    with store.transaction() as db:
        digest = db.execute(
            "SELECT snapshot_sha256 FROM review_archive_identity_members "
            "WHERE source_table='dedupe_keys' LIMIT 1",
        ).fetchone()[0]
        db.execute(
            "INSERT INTO review_archive_identity_members"
            "(source_table,source_rowid,snapshot_sha256,first,second,third,types_json,values_json) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("dedupe_keys", 10**12, digest, "ghost", "o/r", "absent",
             encode_workflow(("text", "text", "text")),
             encode_workflow(("ghost", "o/r", "absent"))),
        )
        with pytest.raises(WorkflowAuthorityError, match="incomplete or stale"):
            matching_review_archive_identity_rowids(
                db, "dedupe_keys", ("ghost", "o/r", "absent"),
            )
        if lost_cursor:
            db.execute(
                "DELETE FROM level_state WHERE namespace='review-archive-identity-scan-v1' "
                "AND scope='global' AND key='identities'",
            )
    repaired, _ = _finish(store, now)
    assert repaired.source_versions == completed.source_versions
    assert repaired.projection_epoch > completed.projection_epoch
    with store.transaction() as db:
        assert matching_review_archive_identity_rowids(
            db, "dedupe_keys", ("ghost", "o/r", "absent"),
        ) == []
        assert db.execute(
            "SELECT COUNT(*) FROM review_archive_identity_members "
            "WHERE source_table='dedupe_keys' AND source_rowid=?", (10**12,),
        ).fetchone()[0] == 1
