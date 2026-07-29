"""Contracts for keeping the domain test tier dependency-free."""

from __future__ import annotations

import ast
from pathlib import Path


DOMAIN_TESTS = Path(__file__).parent


def _issue_agent_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names if alias.name.startswith("issue_agent."))
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("issue_agent."):
            imports.add(node.module)
    return imports


def test_domain_tier_imports_only_domain_modules() -> None:
    """`make test-domain` must stay runnable without application extras."""
    non_domain_imports = {
        path.name: sorted(
            imported
            for imported in _issue_agent_imports(path)
            if not imported.startswith("issue_agent.domain")
        )
        for path in DOMAIN_TESTS.glob("test_*.py")
    }

    assert non_domain_imports == {name: [] for name in non_domain_imports}
