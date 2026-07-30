"""Mechanized architecture rules that are too semantic for import-linter."""

from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).parents[2]
DOMAIN = ROOT / "src" / "issue_agent" / "domain"
APP = ROOT / "src" / "issue_agent" / "app"
PLUGIN = ROOT / "plugin"

REMOTE_WRITES = frozenset(
    {
        "add_blocked_by",
        "close_issue",
        "comment",
        "create_pr",
        "delete_branch",
        "enable_auto_merge",
        "merge_pr",
        "push",
        "relabel",
        "rerun_failed_checks",
        "submit_review",
        "update_branch",
    }
)
MIGRATION_FLAGS = frozenset(
    {"CONTROL_STORE_MODE", "TRUSTED_PUBLISH", "EVENT_FORMAT", "SUPERVISION_NOTIFY"}
)


def _python_files(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(root.rglob("*.py")))


def _receiver_name(node: ast.expr) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def test_domain_has_no_behavioral_io_or_ambient_clock() -> None:
    violations: list[str] = []
    for path in _python_files(DOMAIN):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = _receiver_name(node.func)
            if called in {
                "datetime.now",
                "datetime.utcnow",
                "open",
                "os.open",
                "subprocess.run",
                "subprocess.Popen",
            }:
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno} {called}")
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and _receiver_name(node).startswith("os.environ")
            ):
                violations.append(
                    f"{path.relative_to(ROOT)}:{node.lineno} os.environ"
                )
    assert violations == []


def test_plugin_python_cannot_import_application_or_adapters() -> None:
    violations: list[str] = []
    for path in _python_files(PLUGIN):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            modules: tuple[str, ...] = ()
            if isinstance(node, ast.Import):
                modules = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = (node.module,)
            for module in modules:
                if module.startswith(("issue_agent.app", "issue_agent.adapters")):
                    violations.append(
                        f"{path.relative_to(ROOT)}:{node.lineno} {module}"
                    )
    assert violations == []


def test_app_gateway_usage_is_read_only() -> None:
    violations: list[str] = []
    for path in _python_files(APP):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(
                node.func, ast.Attribute
            ):
                continue
            receiver = _receiver_name(node.func.value)
            if (
                node.func.attr in REMOTE_WRITES
                and receiver.rsplit(".", 1)[-1] in {"gateway", "_gateway"}
            ):
                violations.append(
                    f"{path.relative_to(ROOT)}:{node.lineno} "
                    f"{receiver}.{node.func.attr}"
                )
    assert violations == []


def test_app_has_no_lambda_noop_defaults() -> None:
    violations: list[str] = []
    for path in _python_files(APP):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Lambda)
                and isinstance(node.body, ast.Constant)
                and node.body.value is None
            ):
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert violations == []


def test_self_authored_markers_are_not_gate_evidence() -> None:
    forbidden = ("ac-posted", "test-plan-posted", "adversarial-verdict")
    checked = (
        *_python_files(APP),
        *_python_files(ROOT / "src" / "issue_agent" / "adapters"),
        *(ROOT / "plugin" / "hooks" / "checks").glob("*.sh"),
    )
    violations = [
        f"{path.relative_to(ROOT)}: {marker}"
        for path in checked
        for marker in forbidden
        if marker in path.read_text()
    ]
    assert violations == []


def test_workflows_use_the_single_run_leg_path() -> None:
    violations: list[str] = []
    workflow_root = APP / "workflows"
    for path in _python_files(workflow_root):
        if path.name == "base.py":
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run"
                and _receiver_name(node.func.value).endswith("runner")
            ):
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert violations == []


def test_every_live_migration_flag_has_an_executioner() -> None:
    manifest_path = ROOT / "docs" / "reference" / "migration-flags.json"
    manifest = json.loads(manifest_path.read_text())
    assert set(manifest) <= MIGRATION_FLAGS
    for flag, record in manifest.items():
        assert record["owner"]
        assert int(record["removal_issue"]) > 0
        assert record["target_date"]
        assert record["promotion_metric"]
        assert record["rollback"]

    live = {
        flag
        for path in _python_files(ROOT / "src")
        for flag in MIGRATION_FLAGS
        if flag in path.read_text()
    }
    assert live == set(manifest)


def test_architecture_docs_name_authoritative_subsystems() -> None:
    architecture = (
        ROOT / "docs" / "reference" / "architecture.md"
    ).read_text().casefold()
    matrix = (
        ROOT / "docs" / "reference" / "capability-authority-matrix.md"
    ).read_text().casefold()
    for required in (
        "domain/",
        "adapters/",
        "app/",
        "sqlite",
        "effect",
        "schema-v2",
        "supervision",
        "dashboard",
    ):
        assert required in architecture
    for runtime in ("claude", "codex", "glm"):
        assert runtime in matrix
    for authority in (
        "paid admission",
        "remote mutation",
        "run/process ownership",
        "deduplication",
        "gate evidence",
        "telemetry/dashboard",
    ):
        assert authority in matrix
