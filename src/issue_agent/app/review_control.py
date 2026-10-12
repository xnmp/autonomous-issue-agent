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

from ..adapters.control_path import control_store_path
from ..adapters.control_store import SQLiteControlStore
from ..adapters.github import GitHubGateway
from ..adapters.review_workflow_control import SQLiteReviewWorkflowControl
from ..adapters.store import Store
from .config import HarnessConfig, owner_identity_for_target
from .resolve_control import load_operator_configuration
from .review_identity import review_identity
from .review_runtime import configured_review_control


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
