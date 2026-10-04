#!/usr/bin/env bash
# Policy and target execution live in the harness-owned Python package.
_prc_policy() {
  local checks_root harness_root interpreter entry
  checks_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || return 1
  harness_root="$(cd "$checks_root/../../.." && pwd)" || return 1
  interpreter="$harness_root/.venv/bin/python"
  entry="$harness_root/src/issue_agent/app/gate_check.py"
  if [ ! -x "$interpreter" ] || [ ! -r "$entry" ]; then
    echo "Gate blocked: harness-owned Python policy is unavailable" >&2
    return 1
  fi
  "$interpreter" -I "$entry" "$@"
}

prc_run_test_cmd() { _prc_policy run-target "$1"; }
