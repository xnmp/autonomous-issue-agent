#!/usr/bin/env bash
# Compatibility wrapper for legacy watcher entrypoints. Python owns orchestration;
# this script only preserves shell-style env loading and the old make run-*-sh UX.
set -uo pipefail

role="${1:-}"
case "$role" in
  fix|review|steward) ;;
  *) echo "usage: $0 <fix|review|steward>" >&2; exit 2 ;;
esac

HARNESS_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="${AGENT_ENV:-$HARNESS_ROOT/config/agent.env}"

if [ -f "$CONFIG" ]; then
  # shellcheck disable=SC1090
  set -a; . "$CONFIG"; set +a
fi
export AGENT_ENV="$CONFIG"

cd "$HARNESS_ROOT"
exec uv run --extra sdk --with tree-sitter --with tree-sitter-bash \
  python -m issue_agent.app.main "$role"
