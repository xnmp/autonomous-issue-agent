"""cc-cost bridge: one leg's session id → the per-turn cost chart, as HTML.

The chart generator is the operator's own script (`~/.claude/scripts/session_cost.py`),
which writes `$TMPDIR/session_cost.html`. So this module locates the transcript,
runs the script into a PRIVATE temp dir (never the shared /tmp: two concurrent
requests would race on one output filename), and hands the bytes back.

`session_id` is matched against a strict character class before it is ever put
in a path — it arrives from a URL.
"""

from __future__ import annotations

import glob
import os
import re
import subprocess
import tempfile

SESSION_ID_RE = r"^[A-Za-z0-9_-]+$"
COST_SCRIPT_TIMEOUT_S = 120.0
OUTPUT_NAME = "session_cost.html"

_SESSION_ID = re.compile(SESSION_ID_RE)


def valid_session_id(session_id: str) -> bool:
    return bool(session_id) and bool(_SESSION_ID.match(session_id))


def cost_script_path() -> str:
    return os.environ.get("CC_COST_SCRIPT") or os.path.expanduser(
        "~/.claude/scripts/session_cost.py"
    )


def transcript_path(session_id: str, projects_root: str | None = None) -> str:
    """The SDK transcript for a session, or "". Globbed because the project
    directory is derived from a cwd the harness does not track."""
    if not valid_session_id(session_id):
        return ""
    root = projects_root or os.path.expanduser("~/.claude/projects")
    matches = sorted(glob.glob(os.path.join(root, "*", f"{session_id}.jsonl")))
    return matches[0] if matches else ""


def cost_chart_command(script: str, transcript: str) -> list[str]:
    """The argv, built once so tests can assert it without executing it."""
    return ["python3", script, transcript]


def cost_chart_env(tmpdir: str, base_env: dict | None = None) -> dict:
    return {
        **(os.environ if base_env is None else base_env),
        "SESSION_COST_OPEN": "0",
        "TMPDIR": tmpdir,
    }


def render_cost_chart(
    session_id: str, projects_root: str | None = None, run=None
) -> tuple[str, str]:
    """(html, error). An error is a message the UI shows inline — the chart is a
    convenience, and a missing cc-cost install must not read as a broken leg."""
    if not valid_session_id(session_id):
        return "", "invalid session id"
    script = cost_script_path()
    if not os.path.isfile(script):
        return "", f"cc-cost script not found at {script} (set CC_COST_SCRIPT)"
    transcript = transcript_path(session_id, projects_root)
    if not transcript:
        return "", f"no transcript found for session {session_id}"
    runner = run or _run_cost_script
    try:
        return runner(script, transcript)
    except Exception as exc:  # noqa: BLE001
        return "", f"cost chart generation failed ({type(exc).__name__})"


def _run_cost_script(script: str, transcript: str) -> tuple[str, str]:
    with tempfile.TemporaryDirectory(prefix="issue-agent-cc-cost-") as tmpdir:
        proc = subprocess.run(
            cost_chart_command(script, transcript),
            env=cost_chart_env(tmpdir),
            capture_output=True,
            text=True,
            timeout=COST_SCRIPT_TIMEOUT_S,
        )
        out = os.path.join(tmpdir, OUTPUT_NAME)
        if not os.path.isfile(out):
            detail = (proc.stderr or proc.stdout or "").strip()[:400]
            return "", f"cc-cost produced no chart ({detail or 'no output'})"
        with open(out, encoding="utf-8", errors="replace") as fh:
            return fh.read(), ""
