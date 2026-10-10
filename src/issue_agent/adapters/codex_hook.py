"""Install ADR 0011's advisory Codex **PreToolUse** hook in a worktree.

Codex hooks fire before each Bash tool call and pass JSON on stdin whose shape
is field-identical to Claude Code's (`tool_name`, `tool_input.command`, `cwd`),
so `bash_governor.py` runs UNMODIFIED — exit 2 = block, with stderr fed back to
the model. This module just writes the declaration Codex reads:

    <state>/codex-hooks/<key>/config.toml  [[hooks.PreToolUse]] → gov.sh
    <state>/codex-hooks/<key>/hooks/gov.sh pipes stdin into the governor

Called once per worktree (alongside Worktrees.ensure), before CodexRunner.run.
The governor itself lives in the harness (plugin/hooks/bash_governor.py); we
point at that absolute path so there's a single source of truth.
"""

from __future__ import annotations

import os
import stat
import json
import hashlib
import secrets
import shlex
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from .repository_guidance import RepositoryGuidanceRuntime
from .runtime_plan import AgentRuntime, RuntimePlan
from ..domain.outcome import AgentResult

# Absolute path to the shared governor (…/src/issue_agent/adapters → repo root).
_HARNESS_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
GOVERNOR_PATH = os.path.join(_HARNESS_ROOT, "plugin", "hooks", "bash_governor.py")
EDIT_CHECK_PATH = os.path.join(_HARNESS_ROOT, "plugin", "hooks", "edit_check.sh")
_GENERATED_INSTRUCTIONS_START = "<!-- issue-agent generated instructions: start -->"
_GENERATED_INSTRUCTIONS_END = "<!-- issue-agent generated instructions: end -->"


def evidence_receipt_path(state_dir: str, cwd: str) -> str:
    """Harness-owned screenshot-read receipt for one exact worktree."""

    resolved = os.path.realpath(cwd)
    key = Path(resolved).name + "-" + hashlib.sha256(
        resolved.encode()
    ).hexdigest()[:12]
    return os.path.join(state_dir, "evidence", key, "screenshots-read")


def _hook_key(cwd: str) -> str:
    resolved = os.path.realpath(cwd)
    return Path(resolved).name + "-" + hashlib.sha256(
        resolved.encode()
    ).hexdigest()[:12]


def _without_generated_instructions(content: str) -> str:
    """Remove this harness's prior block while preserving target-owned content."""
    start = content.find(_GENERATED_INSTRUCTIONS_START)
    # The copied repository instructions may themselves document our marker.
    # Our delimiter is always the final END because the generated block is
    # appended at EOF; rfind prevents embedded copies from truncating the strip.
    end = content.rfind(_GENERATED_INSTRUCTIONS_END)
    if start < 0 or end < 0:
        return content
    suffix = content[end + len(_GENERATED_INSTRUCTIONS_END):]
    return content[:start] + suffix.removeprefix("\n")


def _gov_script(
    governor_path: str,
    review_only: bool = False,
    base_branch: str = "",
    repository: str = "",
    test_failure_exit_codes: str = "",
    test_cmd: str = "",
) -> str:
    gate_base = (
        f"export BASE_BRANCH={shlex.quote(base_branch)}"
        if base_branch
        else ""
    )
    # The agent env is allowlist-scrubbed (child_env), so the governor cannot
    # read the deployment repo from ambient env — bake it into the generated
    # hook, where the daemon's authority already writes BASE_BRANCH. Without
    # it the own-repo `--repo` comment form stays denied (keifu #156/#158,
    # 2026-08-01).
    gate_repo = (
        f"export REPO={shlex.quote(repository)}" if repository else ""
    )
    # Same class as BASE_BRANCH/REPO: the operator's trusted red-test exit
    # codes are daemon-env authority the scrub rightly withholds from the
    # agent, so the commit-time red verify defaulted to '1' regardless of the
    # overlay — a cargo (101) red commit was denied as INFRA_ERROR while
    # doctor validated the daemon env and reported the knob covered (tauri
    # #604, 2026-08-09). Baked here, in the harness-generated hook the agent
    # cannot edit, never via an agent-writable marker: this value DEFINES
    # what counts as red evidence.
    gate_red_codes = (
        "export GATE_TEST_FAILURE_EXIT_CODES="
        f"{shlex.quote(test_failure_exit_codes)}"
        if test_failure_exit_codes
        else ""
    )
    return f"""#!/usr/bin/env bash
# Codex PreToolUse hook → harness governor (auto-generated; do not edit).
if ! command -v uv >/dev/null 2>&1; then
  echo "bash_governor: uv not found; refusing ungoverned command" >&2
  exit 2
fi
{('export ISSUE_AGENT_REVIEW_READ_ONLY=1' if review_only else '')}
# Gate-local operator authority: target commands never inherit these.
{gate_base}
{gate_repo}
{gate_red_codes}
# Freeze operator test authority here; the paid child's environment cannot
# replace it. Target markers retain their existing no-operator fallback.
export TEST_CMD={shlex.quote(test_cmd)}
# Codex PostToolUse executes inside the paid child and cannot mint trusted
# visual evidence. Remove an ambient/exported path on every governed command.
unset HARNESS_SCREENSHOTS_READ
exec uv run --quiet "{governor_path}"
"""


def _config_toml(
    gov_script_path: str,
    edit_check_script_path: str = "",
    developer_instructions: str = "",
) -> str:
    instructions = (
        f"developer_instructions = {json.dumps(developer_instructions, ensure_ascii=False)}\n"
        if developer_instructions else ""
    )
    toml = f"""# Auto-generated by issue_agent — governor as a PreToolUse hook.
{instructions}
# Target AGENTS.md and fallback project documents are delivered separately as
# untrusted user-task data. Zero disables Codex's native developer-authority
# project document loading for this checkout.
project_doc_max_bytes = 0

[[hooks.PreToolUse]]
matcher = "^Bash$"

[[hooks.PreToolUse.hooks]]
type = "command"
command = "{gov_script_path}"
timeout = 30
"""
    if edit_check_script_path:
        # PostToolUse edit-time check. Codex edits via apply_patch; Claude via
        # Edit/Write — match all so the check fires regardless of the edit tool.
        toml += f"""
[[hooks.PostToolUse]]
matcher = "^(Edit|Write|MultiEdit|apply_patch)$"

[[hooks.PostToolUse.hooks]]
type = "command"
command = "{edit_check_script_path}"
timeout = 120
"""
    return toml


def _edit_check_script(edit_check_cmd: str) -> str:
    return f"""#!/usr/bin/env bash
# Codex PostToolUse hook → edit-time check (auto-generated; do not edit).
export EDIT_CHECK_CMD={json.dumps(edit_check_cmd)}
exec {json.dumps(EDIT_CHECK_PATH)}
"""


def _link_codex_auth(codex_dir: str, auth_file: str) -> None:
    """Expose one trusted Codex login to the isolated external CODEX_HOME.

    The link lives in harness state, never in the target checkout. A link keeps
    credential rotation authoritative at the operator's normal Codex profile
    and avoids accumulating state-owned copies of a bearer credential.
    """

    if not auth_file:
        return
    source = os.path.abspath(auth_file)
    if not os.path.isfile(source):
        return
    destination = os.path.join(codex_dir, "auth.json")
    if os.path.realpath(source) == os.path.realpath(destination):
        return
    temporary = os.path.join(
        codex_dir,
        f".auth.json.{os.getpid()}.{secrets.token_hex(6)}.tmp",
    )
    try:
        os.symlink(source, temporary)
        os.replace(temporary, destination)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def write_hook_config(
    cwd: str, governor_path: str = GOVERNOR_PATH, agents_md: str = "",
    edit_check_cmd: str = "",
    review_only: bool = False,
    base_branch: str = "",
    repository: str = "",
    log: Callable[[str], None] | None = None,
    state_dir: str = "",
    auth_file: str = "",
) -> str:
    """Emit an external CODEX_HOME containing the governor and trusted contract.

    Target-owned instruction files remain untouched and are not loaded through
    Codex's developer-authority project-document channel. ``auth_file``, when
    present, is linked into the state-owned CODEX_HOME so an isolated OAuth run
    uses the operator's existing login without copying it into the checkout.
    Returns the config path. Idempotent.
    """
    if not state_dir:
        raise OSError("STATE_DIR is required for Codex hook containment")
    key = _hook_key(cwd)
    codex_dir = os.path.join(state_dir, "codex-hooks", key)
    hooks_dir = os.path.join(codex_dir, "hooks")
    os.makedirs(hooks_dir, exist_ok=True)
    os.chmod(codex_dir, stat.S_IRWXU)
    _link_codex_auth(codex_dir, auth_file)

    gov_script_path = os.path.join(hooks_dir, "gov.sh")
    # Read at write time from the daemon env (the composed snapshot's
    # authority), mirroring worktree.py's Python-gate read of the same knob —
    # one source of truth for what counts as trusted red on both stacks.
    with open(gov_script_path, "w") as f:
        f.write(_gov_script(
            governor_path,
            review_only,
            base_branch,
            repository,
            test_failure_exit_codes=os.environ.get(
                "GATE_TEST_FAILURE_EXIT_CODES", ""
            ),
            test_cmd=os.environ.get("TEST_CMD", ""),
        ))
    os.chmod(
        gov_script_path,
        os.stat(gov_script_path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH,
    )
    stale_read_hook = os.path.join(hooks_dir, "read_record.sh")
    try:
        os.unlink(stale_read_hook)
    except FileNotFoundError:
        pass

    edit_check_script_path = ""
    if edit_check_cmd:
        edit_check_script_path = os.path.join(hooks_dir, "edit_check.sh")
        with open(edit_check_script_path, "w") as f:
            f.write(_edit_check_script(edit_check_cmd))
        os.chmod(
            edit_check_script_path,
            os.stat(edit_check_script_path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH,
        )

    config_path = os.path.join(codex_dir, "config.toml")
    with open(config_path, "w") as f:
        f.write(_config_toml(
            gov_script_path,
            edit_check_script_path,
            agents_md,
        ))
    return config_path


class HookPreparingRuntime(AgentRuntime):
    """Install immutable hook configuration outside the target worktree."""

    def __init__(
        self,
        inner: AgentRuntime,
        prepare: Callable[[str], str],
    ):
        self.inner = inner
        self.prepare = prepare

    async def run(self, task: RuntimePlan) -> AgentResult:
        if not task.cwd:
            raise RuntimeError("Codex containment requires an explicit cwd")
        config_path = self.prepare(task.cwd)
        prepared = replace(
            task,
            env={**task.env, "CODEX_HOME": os.path.dirname(config_path)},
        )
        return await RepositoryGuidanceRuntime(self.inner).run(prepared)
