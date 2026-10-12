"""Review-owned rejection-ceiling operator composition for the local operator transport (ADR 0015).

The dashboard never mints a review command owner or calls operator effects
for this family: commands are accepted by the review daemon's own
`SQLiteReviewWorkflowControl`, bound to the review owner, reading GitHub
read-only through the review identity (App, configured review token, then the
ambient mode). The review daemon's drain executes every accepted obligation.
"""

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import os
from types import MappingProxyType

from ..adapters.control_path import control_store_path
from ..adapters.control_store import SQLiteControlStore
from ..adapters.env_file import source_env_file
from ..adapters.github import GitHubGateway
from ..adapters.review_workflow_control import SQLiteReviewWorkflowControl
from ..adapters.store import Store
from ..domain.deployment import canonical_deployment_name
from .config import HarnessConfig, from_env, owner_identity_for_target
from .review_identity import review_identity
from .review_runtime import configured_review_control


def load_operator_configuration(name: str, *, harness_root: str) -> tuple[HarnessConfig, Mapping[str, str]]:
    """Mutation scope comes from one launch snapshot, never per-field overlay fallbacks."""
    if not isinstance(name, str) or not name or name != canonical_deployment_name(name) or name in (".", ".."):
        raise ValueError("operator control requires a canonical deployment name")
    snapshot = os.path.join(harness_root, "config", f".active.{name}.env")
    env = source_env_file(snapshot, os.environ, assigned_only=True)
    env.pop("AGENT_ENV", None)
    if not env.get("REPO") or not env.get("TARGET_DIR"):
        raise ValueError("operator control requires explicit repository and checkout in the active snapshot")
    host = env.get("GH_HOST", "github.com")
    if host != os.environ.get("GH_HOST", "github.com"):
        raise ValueError("operator GitHub host differs from the deployment snapshot")
    cfg = from_env(env)
    cfg.validate()
    if not cfg.repo or not cfg.target_dir or not os.path.isabs(cfg.target_dir):
        raise ValueError("operator control requires an explicit repository checkout")
    return cfg, MappingProxyType(env)


def open_review_operator(
    name: str, *, harness_root: str, control_db_path: str = "",
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    gateway: Callable[[HarnessConfig, Mapping[str, str]], object] | None = None,
) -> SQLiteReviewWorkflowControl:
    """Operator composition from the deployment's launch snapshot.

    ``gateway`` replaces only the remote read transport (acceptance tests);
    configuration, authority path, owner binding and the counting rule are
    always derived here, exactly as the review daemon derives them.
    """
    cfg, env = load_operator_configuration(name, harness_root=harness_root)
    if "review" not in env.get("ROLES", "fix review").split():
        raise ValueError("review control requires the configured review role")
    path = control_store_path((cfg.state_dir, cfg.review_state_dir),
                              authority_root=cfg.control_authority_root)
    if control_db_path and os.path.realpath(control_db_path) != os.path.realpath(path):
        raise ValueError("operator and deployment disagree on control authority")
    control = SQLiteControlStore(path, now=now)
    control.require_initialized()
    owner = owner_identity_for_target(cfg.repo, cfg.target_dir, "review")
    control.bind_active_owner(owner)
    remote = gateway(cfg, env) if gateway is not None else GitHubGateway(
        cfg.repo, review_identity(dict(env)), review_pr_label=cfg.review_trigger_label,
        review_adopt_unlabeled=cfg.review_adopt_unlabeled,
    )
    store = Store(cfg.review_state_dir, runtime=cfg.reviewer_runtime.value)
    return configured_review_control(cfg, control, remote, store, owner)
