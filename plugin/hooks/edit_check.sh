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

# Target-owned command → same #63 defenses as TEST_CMD (env scrub + re-entrancy
# bound), via the shared wrapper. rc 213 = it re-entered the gate; block with
# the real cause rather than a fake check failure.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
[ -f "$HERE/checks/_pr_common.sh" ] && . "$HERE/checks/_pr_common.sh"
if declare -f prc_run_test_cmd >/dev/null; then
  out=$(prc_run_test_cmd "$EDIT_CHECK_CMD" 2>&1); rc=$?
else
  out=$(eval "$EDIT_CHECK_CMD" 2>&1); rc=$?
fi
if [ "$rc" -eq "${PRC_GATE_RECURSION_RC:-213}" ]; then
  echo "GATE RECURSION: EDIT_CHECK_CMD re-entered the harness gate and was refused (see #63): $EDIT_CHECK_CMD" >&2
  echo "$out" >&2
  exit 2
fi
if [ "$rc" -ne 0 ]; then
  echo "Edit check failed (EDIT_CHECK_CMD): $EDIT_CHECK_CMD" >&2
  echo "$out" >&2
  exit 2
fi
exit 0
