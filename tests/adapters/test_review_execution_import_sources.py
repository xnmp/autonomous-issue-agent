"""Activation-source scope keeps remote-effect barriers over complete attribution."""

from dataclasses import replace

import pytest

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.deployment_authority import deployment_effect_authority
from issue_agent.adapters.review_execution_activation import (
    CompleteLegacyExecutionRootFiles, read_complete_legacy_execution_root_files,
)
from issue_agent.adapters.review_execution_counter_links import read_complete_legacy_open_counter_links
from issue_agent.adapters.review_execution_import_sources import (
    BlockedLegacyExecutionCounterSources, HeldLegacyExecutionCounterSources,
    held_legacy_execution_counter_sources,
)
from issue_agent.domain.review_execution_counter_mapping import CompleteLegacyOpenCounterLinks
from issue_agent.domain.world import IssueView, PRView

HEAD = "a" * 40


class Gateway:
    repo = "o/r"

    def __init__(self):
        self.views = {
            9: PRView(9, head_ref_name="fix/7-work", head_ref_oid=HEAD),
            10: PRView(10, head_ref_name="work/topic", head_ref_oid=HEAD),
        }

    def _gh_json(self, _args):
        return [[{"number": number, "state": "open"} for number in sorted(self.views)]]

    def get_review_pull_request(self, number):
        return self.views[number]

    def get_review_linked_issue(self, number):
        return IssueView(number)

    def review_issue_blockers(self, _number):
        return ()


@pytest.fixture
def sources(tmp_path):
    root = tmp_path / "review"
    root.mkdir()
    (root / "issue-7.infra-deaths").write_text("2")
    (root / "issue--10.infra-deaths").write_text("3")
    roots = (("review", str(root)),)
    control = SQLiteControlStore(str(tmp_path / "control.db"))
    control.initialize()
    gateway = Gateway()
    files = read_complete_legacy_execution_root_files(roots)
    links = read_complete_legacy_open_counter_links(control, gateway, "o/r")
    assert isinstance(files, CompleteLegacyExecutionRootFiles)
    assert isinstance(links, CompleteLegacyOpenCounterLinks)
    return control, gateway, roots, files, links


def test_complete_counter_sources_keep_all_effect_barriers_through_sql_scope(sources):
    control, gateway, roots, files, links = sources
    with control._connect() as db:
        before = tuple(db.iterdump())
    with held_legacy_execution_counter_sources(control, gateway, "o/r", roots, files, links) as held:
        assert isinstance(held, HeldLegacyExecutionCounterSources)
        assert {(item.counter.source.key, item.pr) for item in held.mapping.assignments} == {(7, 9), (-10, 10)}
        assert not held.mapping.unassigned
        assert len(held.source_digest) == 64
        for role in ("fix", "review", "steward"):
            with pytest.raises(BlockingIOError):
                with deployment_effect_authority(control.path, "o/r", role, blocking=False):
                    pass
        with control.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM workflows").fetchone()[0] == 0
    for role in ("fix", "review", "steward"):
        with deployment_effect_authority(control.path, "o/r", role, blocking=False):
            pass
    with control._connect() as db:
        assert tuple(db.iterdump()) == before


@pytest.mark.parametrize("change", ("file", "link", "repository", "ambiguous_issue"))
def test_counter_source_scope_blocks_changed_or_ambiguous_evidence(sources, change):
    control, gateway, roots, files, links = sources
    if change == "file":
        from pathlib import Path
        (Path(roots[0][1]) / "issue-7.infra-deaths").write_text("4")
    elif change == "link":
        gateway.views[9] = replace(gateway.views[9], head_ref_oid="b" * 40)
    elif change == "repository":
        gateway.repo = "another/repo"
    else:
        gateway.views[10] = replace(gateway.views[10], head_ref_name="fix/7-other")
        links = read_complete_legacy_open_counter_links(control, gateway, "o/r")
        assert isinstance(links, CompleteLegacyOpenCounterLinks)
    with held_legacy_execution_counter_sources(control, gateway, "o/r", roots, files, links) as held:
        assert isinstance(held, BlockedLegacyExecutionCounterSources)
        assert held.reason
        with pytest.raises(BlockingIOError):
            with deployment_effect_authority(control.path, "o/r", "review", blocking=False):
                pass
    with deployment_effect_authority(control.path, "o/r", "review", blocking=False):
        pass
