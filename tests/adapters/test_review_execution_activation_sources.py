"""Review execution legacy file inventory is bounded and read-only."""

from issue_agent.adapters.review_execution_activation import read_legacy_execution_roots, read_legacy_execution_state
from issue_agent.domain.resolve_activation import (
    MAX_LEGACY_FILE_BYTES, LegacyAbsent, LegacyPresent, LegacyUnreadable, UnmappedLegacyEntry,
)
from issue_agent.domain.review_execution_activation import LegacyExecutionCounterRecord, LegacyExecutionPRRecord


def test_inventory_keeps_both_counter_namespaces_and_runtime_session_bytes(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "pr-7.session").write_text("codex:session-1")
    (state / "pr-7.round").write_text("4")
    (state / "pr-8.round").write_text("2")
    (state / "issue-99.infra-deaths").write_text("1")
    (state / "issue--7.infra-deaths").write_text("3")
    (state / "pr-7.last-sha").write_text("separate rejection source")
    (state / "issue-99.resolve-retries").write_text("separate resolve source")
    before = {path.name: (path.stat().st_mtime_ns, path.read_bytes()) for path in state.iterdir()}

    entries = read_legacy_execution_state(str(state))

    assert {path.name: (path.stat().st_mtime_ns, path.read_bytes()) for path in state.iterdir()} == before
    prs = {entry.pr: entry for entry in entries if isinstance(entry, LegacyExecutionPRRecord)}
    counters = {entry.key: entry for entry in entries if isinstance(entry, LegacyExecutionCounterRecord)}
    assert set(prs) == {7, 8}
    assert isinstance(prs[7].session, LegacyPresent) and prs[7].session.text == "codex:session-1"
    assert isinstance(prs[7].round, LegacyPresent) and prs[7].round.text == "4"
    assert prs[8].session == LegacyAbsent()
    assert set(counters) == {-7, 99}
    assert isinstance(counters[-7].infra_deaths, LegacyPresent) and counters[-7].infra_deaths.text == "3"
    assert isinstance(counters[99].infra_deaths, LegacyPresent) and counters[99].infra_deaths.text == "1"


def test_unreadable_files_remain_distinct_from_absent_files(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "pr-7.session").write_bytes(b"\xff")
    (state / "pr-8.session").write_bytes(b"x" * (MAX_LEGACY_FILE_BYTES + 1))
    (state / "pr-9.round").mkdir()
    (state / "pr-10.round").symlink_to(state / "pr-7.session")
    (state / "issue--10.infra-deaths").write_bytes(b"\xff")

    entries = read_legacy_execution_state(str(state))
    prs = {entry.pr: entry for entry in entries if isinstance(entry, LegacyExecutionPRRecord)}
    counters = {entry.key: entry for entry in entries if isinstance(entry, LegacyExecutionCounterRecord)}
    assert prs[7].session == LegacyUnreadable("not valid UTF-8")
    assert isinstance(prs[8].session, LegacyUnreadable) and "exceeds" in prs[8].session.reason
    assert prs[9].round == LegacyUnreadable("not a regular file")
    assert prs[10].round == LegacyUnreadable("symbolic link")
    assert counters[-10].infra_deaths == LegacyUnreadable("not valid UTF-8")
    assert prs[7].round == LegacyAbsent()


def test_malformed_legacy_names_remain_visible_and_other_names_are_ignored(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    malformed = (
        "pr-.session", "pr-0.round", "pr-07.session", "pr--7.round", "pr-9223372036854775808.session",
        "issue-.infra-deaths", "issue-0.infra-deaths", "issue--0.infra-deaths",
        "issue-+7.infra-deaths", "issue-07.infra-deaths", "issue--07.infra-deaths",
        "issue-9223372036854775808.infra-deaths", "issue--9223372036854775808.infra-deaths",
    )
    for name in malformed:
        (state / name).write_text("1")
    (state / "issue-7.infra-deaths.tmp").write_text("incomplete write")
    (state / "pr-7.review-retries").write_text("other family")

    entries = read_legacy_execution_state(str(state))

    assert {entry.name for entry in entries if isinstance(entry, UnmappedLegacyEntry)} == set(malformed)
    assert all(isinstance(entry, UnmappedLegacyEntry) for entry in entries)


def test_roots_keep_cross_root_duplicates_but_remove_physical_alias(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "pr-7.session").write_text("codex:old")
    (second / "pr-7.session").write_text("codex:new")
    (first / "issue--7.infra-deaths").write_text("1")
    (second / "issue--7.infra-deaths").write_text("2")
    (tmp_path / "alias").symlink_to(first)

    entries = read_legacy_execution_roots((("first", str(first)), ("alias", str(tmp_path / "alias")),
                                           ("second", str(second))))

    assert [(entry.root, entry.pr) for entry in entries if isinstance(entry, LegacyExecutionPRRecord)] == [
        ("first", 7), ("second", 7),
    ]
    assert [(entry.root, entry.key) for entry in entries if isinstance(entry, LegacyExecutionCounterRecord)] == [
        ("first", -7), ("second", -7),
    ]


def test_missing_and_non_directory_roots_are_explicit(tmp_path):
    assert read_legacy_execution_state(str(tmp_path / "missing")) == ()
    bad = tmp_path / "bad"
    bad.write_text("not a directory")
    (entry,) = read_legacy_execution_state(str(bad))
    assert isinstance(entry, UnmappedLegacyEntry)
    assert entry.root == "review" and "unreadable" in entry.reason
