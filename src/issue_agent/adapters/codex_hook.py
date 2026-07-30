"""Install the harness governor as a Codex **PreToolUse** hook in a worktree.

Codex hooks fire before each Bash tool call and pass JSON on stdin whose shape
is field-identical to Claude Code's (`tool_name`, `tool_input.command`, `cwd`),
so `bash_governor.py` runs UNMODIFIED — exit 2 = block, with stderr fed back to
the model. This module just writes the declaration Codex reads:

    <cwd>/.codex/config.toml      [[hooks.PreToolUse]] → gov.sh
    <cwd>/.codex/hooks/gov.sh     pipes stdin into `uv run … bash_governor.py`

Called once per worktree (alongside Worktrees.ensure), before CodexRunner.run.
The governor itself lives in the harness (plugin/hooks/bash_governor.py); we
point at that absolute path so there's a single source of truth.
"""

from __future__ import annotations

import os
import stat
import json
import subprocess
from collections.abc import Callable
from pathlib import Path

# Absolute path to the shared governor (…/src/issue_agent/adapters → repo root).
_HARNESS_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
GOVERNOR_PATH = os.path.join(_HARNESS_ROOT, "plugin", "hooks", "bash_governor.py")
REPOSITORY_INSTRUCTIONS_MAX_BYTES = 32 * 1024
_REPOSITORY_INSTRUCTIONS_HEADING = "\n\n# Repository instructions (CLAUDE.md)\n"
_AGENTS_INSTRUCTIONS_HEADING = "\n\n# Repository instructions (AGENTS.md)\n\n"
_GENERATED_INSTRUCTIONS_START = "<!-- issue-agent generated instructions: start -->"
_GENERATED_INSTRUCTIONS_END = "<!-- issue-agent generated instructions: end -->"


def _repository_instructions(
    cwd: str,
    *,
    max_bytes: int = REPOSITORY_INSTRUCTIONS_MAX_BYTES,
) -> tuple[str, bool]:
    """Return checked-out CLAUDE.md files in path order, bounded for prompt safety."""
    root = Path(cwd)
    tracked = subprocess.run(
        ["git", "-C", cwd, "ls-files", "--", "CLAUDE.md", "**/CLAUDE.md"],
        capture_output=True,
        text=True,
        check=False,
    )
    if tracked.returncode == 0:
        paths = [root / relative for relative in tracked.stdout.splitlines()]
    else:
        paths = [
            path for path in root.glob("**/CLAUDE.md")
            if ".git" not in path.parts
        ]
    paths.sort(key=lambda path: path.relative_to(root).as_posix())
    remaining = max_bytes
    sections: list[str] = []
    truncated = False
    for path in paths:
        relative = path.relative_to(root).as_posix()
        try:
            content = path.read_text()
        except (OSError, UnicodeError):
            continue
        section = f"\n\n## {relative}\n\n{content}"
        encoded = section.encode()
        if len(encoded) <= remaining:
            sections.append(section)
            remaining -= len(encoded)
            continue
        # Out of budget: cut at a paragraph boundary and leave a pointer, so the
        # model knows guidance exists beyond the cut and can read the file itself.
        pointer = (
            f"\n\n[Repository instructions truncated to fit the prompt budget — "
            f"read {relative} (and any later CLAUDE.md files) in the worktree "
            f"for the rest before making design decisions.]"
        )
        budget = remaining - len(pointer.encode())
        if budget > 0:
            head = encoded[:budget].decode(errors="ignore")
            cut = head.rfind("\n\n")
            if cut > 0:
                head = head[:cut]
            sections.append(head)
            sections.append(pointer)
        elif remaining >= len(pointer.encode()):
            sections.append(pointer)
        truncated = True
        break
    return "".join(sections), truncated


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


def _gov_script(governor_path: str, legacy_review_only: bool = False) -> str:
    # Mirrors plugin/hooks/dispatch.sh: fail OPEN if uv is missing (the PR gate +
    # CI + reviewer are the real boundary), else pipe stdin into the governor.
    legacy_comment = (
        "# Legacy ISSUE_AGENT_REVIEW_READ_ONLY=1 argument is ignored; policy is per-leg.\n"
        if legacy_review_only else ""
    )
    return f"""#!/usr/bin/env bash
# Codex PreToolUse hook → harness governor (auto-generated; do not edit).
{legacy_comment}# Read-only state is supplied only by the review child environment.
if ! command -v uv >/dev/null 2>&1; then
  echo "bash_governor: uv not found; allowing (gate degraded)" >&2
  exit 0
fi
exec uv run --quiet "{governor_path}"
"""


def _config_toml(
    gov_script_path: str,
    read_record_script_path: str,
    edit_check_script_path: str = "",
    developer_instructions: str = "",
) -> str:
    instructions = (
        f"developer_instructions = {json.dumps(developer_instructions, ensure_ascii=False)}\n"
        if developer_instructions else ""
    )
    toml = f"""# Auto-generated by issue_agent — governor as a PreToolUse hook.
{instructions}
[[hooks.PreToolUse]]
matcher = "^Bash$"

[[hooks.PreToolUse.hooks]]
type = "command"
command = "{gov_script_path}"
timeout = 30

[[hooks.PostToolUse]]
matcher = "^(Read|view_image)$"

[[hooks.PostToolUse.hooks]]
type = "command"
command = "{read_record_script_path}"
timeout = 15
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


_READ_RECORD_SCRIPT = r"""#!/usr/bin/env bash
# Codex PostToolUse(Read) hook → record evidence screenshots the agent looked at.
# Observe-only: always exit 0.
set -uo pipefail
payload=$(cat)
read -r fp cwd < <(printf '%s' "$payload" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    fp = (d.get("tool_input") or {}).get("file_path", "") or ""
    cwd = d.get("cwd", "") or "."
    print(fp, cwd)
except Exception:
    print("", ".")
' 2>/dev/null) || exit 0
[ -n "$fp" ] || exit 0
low=$(printf '%s' "$fp" | tr '[:upper:]' '[:lower:]')
case "$low" in
  *evidence/*.png|*evidence/*.jpg|*evidence/*.jpeg|*evidence/*.gif|*evidence/*.webp) ;;
  *) exit 0 ;;
esac
mkdir -p "$cwd/.harness" 2>/dev/null || exit 0
printf '%s\n' "$(basename "$fp")" >> "$cwd/.harness/screenshots-read" 2>/dev/null || exit 0
exit 0
"""


def _edit_check_script(edit_check_cmd: str) -> str:
    # Runs the project's fast check; exit 2 + stderr on failure feeds the error
    # back to the model (Codex block contract). Fail OPEN if the command is unset.
    return f"""#!/usr/bin/env bash
# Codex PostToolUse hook → edit-time check (auto-generated; do not edit).
out=$({edit_check_cmd} 2>&1)
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "Edit check failed ({edit_check_cmd}):" >&2
  echo "$out" >&2
  exit 2
fi
exit 0
"""


def write_hook_config(
    cwd: str, governor_path: str = GOVERNOR_PATH, agents_md: str = "",
    edit_check_cmd: str = "",
    review_only: bool = False,
    log: Callable[[str], None] | None = None,
) -> str:
    """Emit `<cwd>/.codex/config.toml` + `.codex/hooks/gov.sh` so a `codex exec`
    run in `cwd` enforces the governor on every Bash call. When `agents_md` is
    given, also write it to `<cwd>/AGENTS.override.md` — Codex's native override
    instructions file (verified auto-loaded by `codex exec`), which is how the
    fixer's DDD+TDD contract reaches GPT-5.5 (the Claude path injects the same
    text via the SDK system-prompt preset). Returns the config path. Idempotent.

    NOTE: AGENTS.override.md lands inside the worktree and is locally excluded;
    a target's tracked AGENTS.md remains byte-for-byte untouched. `review_only`
    remains a compatibility argument for callers from before review policy was
    made per-leg; it deliberately does not alter persistent hook contents."""
    codex_dir = os.path.join(cwd, ".codex")
    hooks_dir = os.path.join(codex_dir, "hooks")
    os.makedirs(hooks_dir, exist_ok=True)

    gov_script_path = os.path.join(hooks_dir, "gov.sh")
    with open(gov_script_path, "w") as f:
        f.write(_gov_script(governor_path, review_only))
    os.chmod(
        gov_script_path,
        os.stat(gov_script_path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH,
    )

    read_record_script_path = os.path.join(hooks_dir, "read_record.sh")
    with open(read_record_script_path, "w") as f:
        f.write(_READ_RECORD_SCRIPT)
    os.chmod(
        read_record_script_path,
        os.stat(read_record_script_path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH,
    )

    edit_check_script_path = ""
    if edit_check_cmd:
        edit_check_script_path = os.path.join(hooks_dir, "edit_check.sh")
        with open(edit_check_script_path, "w") as f:
            f.write(_edit_check_script(edit_check_cmd))
        os.chmod(
            edit_check_script_path,
            os.stat(edit_check_script_path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH,
        )

    agents_path = os.path.join(cwd, "AGENTS.override.md")
    target_agents_path = Path(cwd) / "AGENTS.md"
    try:
        target_agents = target_agents_path.read_text()
    except FileNotFoundError:
        target_agents = ""
    try:
        existing = Path(agents_path).read_text()
    except FileNotFoundError:
        existing = ""
    target_instructions = _without_generated_instructions(existing)
    generated = agents_md
    if target_agents:
        generated += _AGENTS_INSTRUCTIONS_HEADING + target_agents
    separator = "" if not target_instructions or target_instructions.endswith("\n") else "\n"
    fixed = (
        target_instructions + separator
        + _GENERATED_INSTRUCTIONS_START + "\n"
        + generated
        + _REPOSITORY_INSTRUCTIONS_HEADING
        + "\n" + _GENERATED_INSTRUCTIONS_END + "\n"
    )
    repository_instructions, truncated = _repository_instructions(
        cwd,
        max_bytes=max(
            0, REPOSITORY_INSTRUCTIONS_MAX_BYTES - len(fixed.encode())
        ),
    )
    if repository_instructions:
        generated += _REPOSITORY_INSTRUCTIONS_HEADING + repository_instructions
    tracked_override = subprocess.run(
        [
            "git", "-C", cwd, "ls-files", "--error-unmatch",
            "AGENTS.override.md",
        ],
        capture_output=True,
        check=False,
    ).returncode == 0
    developer_instructions = generated if tracked_override else ""
    if (agents_md or target_agents or repository_instructions) and not tracked_override:
        with open(agents_path, "w") as f:
            f.write(target_instructions)
            f.write(separator)
            f.write(f"{_GENERATED_INSTRUCTIONS_START}\n")
            f.write(generated)
            if generated and not generated.endswith("\n"):
                f.write("\n")
            f.write(f"{_GENERATED_INSTRUCTIONS_END}\n")
    config_path = os.path.join(codex_dir, "config.toml")
    with open(config_path, "w") as f:
        f.write(_config_toml(
            gov_script_path,
            read_record_script_path,
            edit_check_script_path,
            developer_instructions,
        ))
    if truncated:
        message = (
            "codex: repository CLAUDE.md instructions exceeded "
            f"{REPOSITORY_INSTRUCTIONS_MAX_BYTES} bytes; "
            "AGENTS.override.md section was truncated"
        )
        (log or print)(message)
    return config_path
