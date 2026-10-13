"""The import contract that keeps the core from importing the dashboard
breaks on a core-to-dashboard import inside one layer, not only across
layers (a source that contains a forbidden module is skipped by
import-linter, which once let `app.fix -> app.dashweb` through)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

grimp = pytest.importorskip("grimp")
configuration = pytest.importorskip("importlinter.configuration")
from importlinter.contracts.forbidden import ForbiddenContract  # noqa: E402

NAME = "The core does not import the dashboard"
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def contract():
    configuration.configure()
    spec = next(
        c
        for c in tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["importlinter"]["contracts"]
        if c["name"] == NAME
    )
    options = {key: value for key, value in spec.items() if key not in ("name", "type")}

    def kept(*edges: tuple[str, str]) -> bool:
        graph = grimp.build_graph("issue_agent")  # `check` removes the ignored imports from it
        for importer, imported in edges:
            graph.add_import(importer=importer, imported=imported)
        return ForbiddenContract(NAME, {"root_packages": ["issue_agent"]}, options).check(graph, verbose=False).kept

    return kept


def test_the_tree_as_committed_keeps_it(contract):
    assert contract()


@pytest.mark.parametrize(
    "importer,imported",
    [
        ("issue_agent.app.fix", "issue_agent.app.dashweb.service"),
        ("issue_agent.app.tick", "issue_agent.app.dashboard"),
        ("issue_agent.adapters.github", "issue_agent.adapters.dashweb.legs"),
        ("issue_agent.adapters.control_store", "issue_agent.domain.dashboard"),
        ("issue_agent.domain.state", "issue_agent.domain.dashboard"),
        ("issue_agent.app.planners.tasks", "issue_agent.app.dashweb.views"),
    ],
)
def test_a_core_import_of_the_dashboard_breaks_it(contract, importer, imported):
    assert not contract((importer, imported))
