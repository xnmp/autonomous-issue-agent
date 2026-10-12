"""Shared fix-owned re-arm composition for daemon and local operator transport."""

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import os
from types import MappingProxyType

from ..adapters.control_path import control_store_path
from ..adapters.control_store import SQLiteControlStore
from ..adapters.env_file import source_env_file
from ..adapters.github import GitHubGateway, Identity
from ..adapters.store import Store
from ..adapters.worktree import Worktrees
from ..adapters.workflow_control import SQLiteWorkflowControl
from ..domain.continuation_components import SelectedIssueRecovery
from ..domain.deployment import canonical_deployment_name
from ..domain.labels import Label
from ..domain.ports import FixWorktreePort, WorkflowGitHubReads
from ..domain.workflow import ObservedIssue
from ..domain.workflow_identity import WorkflowWriter
from .config import HarnessConfig, from_env, owner_identity_for_target
from .workflows.resolve_rearm import ResolveRearmWorkflow


def _ignore_log(_message: str) -> None:
    pass


def compose_resolve_rearm(
    cfg: HarnessConfig, control: SQLiteControlStore, gateway: WorkflowGitHubReads,
    worktrees: FixWorktreePort, store: Store, *,
    now: Callable[[], datetime], log: Callable[[str], None], approved_host: str = "github.com",
) -> ResolveRearmWorkflow:
    owner = owner_identity_for_target(cfg.repo, cfg.target_dir, "fix")

    def external_holds(observed: ObservedIssue) -> tuple[str, ...]:
        # Fail closed on exactly what the fixer holds an issue on (unselected
        # recovery authority or unreadable leg history), so a re-arm never
        # "succeeds" for an issue the fixer will keep holding.
        if not isinstance(control.read_issue_recovery(observed.key), SelectedIssueRecovery):
            raise ValueError("issue recovery authority is inactive or migration blocked")
        control.fix_leg_ends(observed.key.repository, observed.key.target.number)
        return tuple(label.value for label in (Label.AI_BLOCKED, Label.AI_PARKED, Label.NEEDS_BUDGET_REVIEW)
                     if label in observed.issue.labels)

    workflow = SQLiteWorkflowControl(control, WorkflowWriter(cfg.repo, "fix", owner),
                                     resolve_ceiling=cfg.max_resolve_retries,
                                     max_contract_rounds=cfg.max_contract_rounds)
    return ResolveRearmWorkflow(workflow, gateway, worktrees, now, log,
                                approved_host=approved_host, external_holds=external_holds)


def load_operator_configuration(name: str, *, harness_root: str) -> tuple[HarnessConfig, Mapping[str, str]]:
    """Mutation scope comes from one launch snapshot, never per-field overlay fallbacks."""
    if not isinstance(name, str) or not name or name != canonical_deployment_name(name) or name in (".", ".."):
        raise ValueError("resolve re-arm requires a canonical deployment name")
    snapshot = os.path.join(harness_root, "config", f".active.{name}.env")
    env = source_env_file(snapshot, os.environ, assigned_only=True)
    env.pop("AGENT_ENV", None)
    if not env.get("REPO") or not env.get("TARGET_DIR"):
        raise ValueError("resolve re-arm requires explicit repository and checkout in the active snapshot")
    host = env.get("GH_HOST", "github.com")
    if host != os.environ.get("GH_HOST", "github.com"):
        raise ValueError("operator GitHub host differs from the deployment snapshot")
    cfg = from_env(env)
    cfg.validate()
    if not cfg.repo or not cfg.target_dir or not os.path.isabs(cfg.target_dir):
        raise ValueError("resolve re-arm requires an explicit repository checkout")
    return cfg, MappingProxyType(env)


def open_resolve_operator(
    name: str, *, harness_root: str, control_db_path: str = "",
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    log: Callable[[str], None] = _ignore_log,
    gateway: Callable[[HarnessConfig, Mapping[str, str]], WorkflowGitHubReads] | None = None,
    worktrees: Callable[[HarnessConfig], FixWorktreePort] | None = None,
) -> ResolveRearmWorkflow:
    """Operator composition from the deployment's launch snapshot.

    ``gateway``/``worktrees`` replace only the remote transport and checkout
    adapters (acceptance tests); configuration, authority path and owner
    binding are always derived here.
    """
    cfg, env = load_operator_configuration(name, harness_root=harness_root)
    if "fix" not in env.get("ROLES", "fix review steward").split():
        raise ValueError("resolve re-arm requires the configured fix role")
    host = env.get("GH_HOST", "github.com")
    path = control_store_path((cfg.state_dir, cfg.review_state_dir, cfg.steward_state_dir),
                              authority_root=cfg.control_authority_root)
    if control_db_path and os.path.realpath(control_db_path) != os.path.realpath(path):
        raise ValueError("operator and deployment disagree on control authority")
    control = SQLiteControlStore(path, now=now)
    control.require_initialized()
    control.bind_active_owner(owner_identity_for_target(cfg.repo, cfg.target_dir, "fix"))
    remote = gateway(cfg, env) if gateway is not None else GitHubGateway(
        cfg.repo, Identity(static_token=env.get("FIX_BOT_TOKEN") or None),
    )
    checkout = worktrees(cfg) if worktrees is not None else Worktrees(cfg.target_dir, cfg.worktree_root, cfg.base_branch)
    store = Store(cfg.state_dir, runtime=cfg.fixer_runtime.value)
    return compose_resolve_rearm(cfg, control, remote, checkout, store, now=now, log=log,
                                  approved_host=host)
