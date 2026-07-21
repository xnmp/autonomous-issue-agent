#!/usr/bin/env bash
# PostToolUse(Edit|Write|…) hook entry point.
# Runs a fast deterministic edit-time check ($EDIT_CHECK_CMD) so type/lint errors
# surface to the agent immediately instead of at commit/CI. Exit 0 = allow; exit 2
# = block (stderr is shown to the agent so it fixes in-session).
#
# Blank $EDIT_CHECK_CMD → no-op (allow). The command must be FAST (it runs after
# every edit) — a typecheck or lint, NOT the test suite.
set -uo pipefail

if [ -z "${EDIT_CHECK_CMD:-}" ]; then
  exit 0
fi

out=$(eval "$EDIT_CHECK_CMD" 2>&1)
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "Edit check failed (EDIT_CHECK_CMD): $EDIT_CHECK_CMD" >&2
  echo "$out" >&2
  exit 2
fi
exit 0
