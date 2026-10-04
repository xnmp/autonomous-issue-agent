#!/usr/bin/env bash
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 1
[ -r "$HERE/_pr_common.sh" ] || { echo "Gate blocked: harness transport unavailable" >&2; exit 1; }
. "$HERE/_pr_common.sh"
_prc_policy commit-order "$@"
