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


def test_app_cannot_construct_the_non_durable_effect_adapter() -> None:
    """Compatibility effects are test tooling, never a production fallback."""

    violations: list[str] = []
    for path in _python_files(APP):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            imported: tuple[str, ...] = ()
            if isinstance(node, ast.Import):
                imported = tuple(alias.name for alias in node.names)
            elif node.module:
                imported = tuple(alias.name for alias in node.names)
            if any(
                name.endswith("ImmediateGitHubEffectExecutor")
                for name in imported
            ):
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert violations == []


def test_orchestration_cannot_read_telemetry_for_control_state() -> None:
    """JSONL readers are confined to presentation and migration adapters."""

    reader_names = frozenset(
        {
            "read_events",
            "read_events_tail",
            "sum_today",
            "ci_flake_keys",
            "ci_pending_keys",
            "duplicate_pr_keys",
            "groomed_keys",
            "recurrence_keys",
        }
    )
    control_paths = (
        APP / "execute.py",
        APP / "loop.py",
        APP / "roles.py",
        APP / "dispatch.py",
        *_python_files(APP / "workflows"),
    )
    violations: list[str] = []
    for path in control_paths:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            imported = {alias.name for alias in node.names}
            forbidden = sorted(imported & reader_names)
            if forbidden:
                violations.append(
                    f"{path.relative_to(ROOT)}:{node.lineno} {forbidden}"
                )
    assert violations == []


def test_application_event_producers_cannot_emit_free_form_names() -> None:
    violations: list[str] = []
    for path in _python_files(APP):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "emit"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert violations == []


def test_legacy_jsonl_compatibility_is_read_only() -> None:
    journal = (
        ROOT / "src" / "issue_agent" / "adapters" / "journal.py"
    )
    tree = ast.parse(journal.read_text(), filename=str(journal))
    locator = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "LegacyJournalLocator"
    )
    assert not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "emit"
        for node in locator.body
    )
    assert not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "event_line"
        for node in tree.body
    )


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


def test_architecture_manifest_names_every_top_level_module_and_app_subtier() -> None:
    architecture = (
        ROOT / "docs" / "reference" / "architecture.md"
    ).read_text()
    for tier in ("domain", "adapters", "app"):
        for path in sorted((ROOT / "src" / "issue_agent" / tier).glob("*.py")):
            if path.name == "__init__.py":
                continue
            assert f"`{tier}/{path.name}`" in architecture
    for subtier in ("planners", "workflows", "dashweb"):
        assert f"`app/{subtier}/`" in architecture


def test_prompt_budget_evidence_matches_current_instruction_file() -> None:
    prompt = (ROOT / "CLAUDE.md").read_text()
    evidence = (
        ROOT / "docs" / "reference" / "prompt-budget.md"
    ).read_text()
    assert f"{len(prompt.encode()):,}" in evidence
    assert f"{len(prompt.split()):,}" in evidence
