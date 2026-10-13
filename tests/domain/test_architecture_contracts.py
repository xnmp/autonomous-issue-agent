"""Mechanized architecture rules that are too semantic for import-linter."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
DOMAIN = ROOT / "src" / "issue_agent" / "domain"
APP = ROOT / "src" / "issue_agent" / "app"
PLUGIN = ROOT / "plugin"
PRIVILEGED_EFFECT_ADAPTERS = frozenset(
    {
        Path("src/issue_agent/adapters/github.py"),
        Path("src/issue_agent/adapters/operator_effects.py"),
    }
)

REMOTE_WRITES = frozenset(
    {
        "close_issue",
        "comment",
        "create_pr",
        "delete_branch",
        "enable_auto_merge",
        "merge_pr",
        "push",
        "relabel",
        "cancel_workflow_runs",
        "rerun_failed_checks",
        "submit_review",
        "update_branch",
    }
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


def test_app_cannot_gate_mutating_port_calls_on_runtime_protocol_probes() -> None:
    """A structural runtime probe must never turn a durable write into a skip."""

    mutation_prefixes = (
        "abandon_",
        "bump_",
        "claim",
        "clear_",
        "drain_",
        "enqueue_",
        "execute_",
        "flush_",
        "prune_",
        "receipt",
        "record_",
        "recover_",
        "release",
        "remove",
        "replace_",
        "reserve",
        "retire_",
        "save_",
        "set_",
        "settle",
    )
    ports_tree = ast.parse(
        (DOMAIN / "ports.py").read_text(),
        filename=str(DOMAIN / "ports.py"),
    )
    mutating_ports = {
        node.name
        for node in ports_tree.body
        if isinstance(node, ast.ClassDef)
        and any(
            isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
            and member.name.startswith(mutation_prefixes)
            for member in node.body
        )
    }

    violations: list[str] = []
    for path in _python_files(APP):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "isinstance"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Name)
                and node.args[1].id in mutating_ports
            ):
                violations.append(
                    f"{path.relative_to(ROOT)}:{node.lineno} {node.args[1].id}"
                )
    assert violations == []


def test_every_domain_port_is_type_checked_or_explicitly_exempted() -> None:
    """A new Protocol cannot bypass the seam-sensitive Pyrefly gate."""

    ports_tree = ast.parse(
        (DOMAIN / "ports.py").read_text(),
        filename=str(DOMAIN / "ports.py"),
    )
    protocols = {
        node.name
        for node in ports_tree.body
        if isinstance(node, ast.ClassDef)
        and any(
            isinstance(base, ast.Name) and base.id == "Protocol"
            for base in node.bases
        )
    }
    conformance_path = ROOT / "tests" / "typecheck" / "port_conformance.py"
    conformance_tree = ast.parse(
        conformance_path.read_text(),
        filename=str(conformance_path),
    )

    def declared(name: str) -> set[str]:
        for node in conformance_tree.body:
            if (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == name
                    for target in node.targets
                )
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "frozenset"
                and len(node.value.args) == 1
                and isinstance(node.value.args[0], ast.Set)
            ):
                return {
                    item.value
                    for item in node.value.args[0].elts
                    if isinstance(item, ast.Constant)
                    and isinstance(item.value, str)
                }
        raise AssertionError(f"{name} must be one literal frozenset")

    covered = declared("COVERED_PORTS")
    checked = {
        node.returns.id
        for node in conformance_tree.body
        if isinstance(node, ast.FunctionDef)
        and isinstance(node.returns, ast.Name)
    }

    assert covered == protocols
    assert covered == checked


def _literal_argv(node: ast.List | ast.Tuple) -> tuple[str, ...]:
    values = []
    for item in node.elts:
        if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
            break
        values.append(item.value)
    return tuple(values)


def _is_remote_mutation(argv: tuple[str, ...]) -> bool:
    if argv[:3] in {
        ("gh", "issue", "edit"),
        ("gh", "label", "create"),
        ("gh", "webhook", "forward"),
    }:
        return True
    if argv[:2] == ("git", "push"):
        return True
    return (
        argv[:2] == ("gh", "api")
        and "-X" in argv
        and any(method in argv for method in ("POST", "PATCH", "PUT", "DELETE"))
    )


def test_remote_cli_mutations_live_only_in_privileged_effect_adapters() -> None:
    assert _is_remote_mutation(("gh", "issue", "edit", "1"))
    assert _is_remote_mutation(("gh", "api", "-X", "DELETE", "repos/o/r/hooks/1"))
    assert _is_remote_mutation(("git", "push", "origin", "branch"))
    violations: list[str] = []
    roots = (
        ROOT / "src" / "issue_agent" / "app",
        ROOT / "src" / "issue_agent" / "adapters",
    )
    for root in roots:
        for path in _python_files(root):
            relative = path.relative_to(ROOT)
            if relative in PRIVILEGED_EFFECT_ADAPTERS:
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.List, ast.Tuple)):
                    continue
                argv = _literal_argv(node)
                if _is_remote_mutation(argv):
                    violations.append(f"{relative}:{node.lineno} {' '.join(argv)}")
    command = re.compile(
        r"\b(gh\s+(?:issue\s+edit|label\s+create|"
        r"webhook\s+forward|api\s+-X\s+(?:POST|PATCH|PUT|DELETE))|git\s+push)\b"
    )
    for directory in (ROOT / "bin", ROOT / "plugin"):
        for path in sorted(directory.rglob("*.sh")):
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                # Narrow, per-LINE exemption for a preflight capability probe.
                # Pinned three ways — the marker, the FILE it may appear in,
                # and the endpoint it may target — so the marker cannot be
                # pasted onto an arbitrary mutation, in doctor.sh or anywhere
                # else, to buy an exemption it was never reviewed for.
                if (
                    "harness:preflight-probe" in stripped
                    and path.name == "doctor.sh"
                    and re.search(r'repos/\$REPO/labels', stripped)
                ):
                    continue
                # It creates and immediately deletes a throwaway repo label to
                # prove the review identity can write BEFORE any paid leg runs
                # (keifu, 2026-08-01 — a scope-less PAT was otherwise only
                # discovered when a paid verdict 403'd on every tick). It
                # touches no issue, PR or branch, carries no lifecycle meaning,
                # and no daemon tick can reach it, so the durability and
                # authority guarantees this contract protects do not apply. A
                # real lifecycle mutation added to doctor.sh still fails here.
                if command.search(stripped):
                    violations.append(
                        f"{path.relative_to(ROOT)}:{lineno} {stripped}"
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
        "supervision",
        "dashboard",
    ):
        assert required in architecture
    for runtime in ("claude", "codex"):
        assert runtime in matrix
    for authority in (
        "paid admission",
        "remote mutation",
        "run/process ownership",
        "deduplication",
        "gate evidence",
        "logs/dashboard",
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
    for subtier in ("planners", "dashweb"):
        assert f"`app/{subtier}/`" in architecture


def test_every_adr_is_referenced_by_a_governed_source_file() -> None:
    decisions = (ROOT / "docs" / "adr").glob(
        "[0-9][0-9][0-9][0-9]-*.md"
    )
    for adr in sorted(decisions):
        decision = adr.read_text()
        match = re.match(r"# ADR ([0-9]{4})\b", decision)
        assert match is not None, f"{adr.name} has no ADR id"
        assert not re.search(r"^Status: Superseded\b", decision, re.MULTILINE), (
            f"{adr.name} is superseded: move it to docs/archive/adr/"
        )
        marker = f"ADR {match.group(1)}"
        governs = next(
            (
                line.removeprefix("Governs: ")
                for line in decision.splitlines()
                if line.startswith("Governs: ")
            ),
            "",
        )
        source_files = tuple(
            ROOT / token
            for token in governs.split()
            if token.startswith("src/") and token.endswith(".py")
        )
        assert source_files, f"{adr.name} governs no source file"
        assert all(path.is_file() for path in source_files), f"{adr.name} governs a missing source file"
        assert any(
            path.is_file() and marker in path.read_text()
            for path in source_files
        ), f"{adr.name} is not referenced by a governed source file"


def test_prompt_budget_evidence_matches_current_instruction_file() -> None:
    prompt = (ROOT / "CLAUDE.md").read_text()
    evidence = (
        ROOT / "docs" / "reference" / "prompt-budget.md"
    ).read_text()
    assert f"{len(prompt.encode()):,}" in evidence
    assert f"{len(prompt.split()):,}" in evidence
