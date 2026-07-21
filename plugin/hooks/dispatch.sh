#!/usr/bin/env bash
# PreToolUse(Bash) hook entry point.
# Reads the hook JSON on stdin and hands it to the tree-sitter governor.
# Exit 0 = allow the Bash call; exit 2 = block it (stderr is shown to the agent).
#
# The governor is a PEP 723 script: `uv run` self-bootstraps tree-sitter on the
# first call (~90-130ms warm thereafter). If `uv` is unavailable we fail OPEN
# (exit 0) rather than wedge the agent — the PR gate + CI + reviewer are the
# real security boundary; this hook is a fast local nudge.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v uv >/dev/null 2>&1; then
  echo "bash_governor: uv not found on PATH; allowing (gate degraded)" >&2
  exit 0
fi

exec uv run --quiet "$HERE/bash_governor.py"
