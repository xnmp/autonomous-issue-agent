#!/usr/bin/env bash
set -euo pipefail

# Keep pytest's retained directories local to one full run. On Linux the user
# runtime directory is memory-backed and avoids shared /tmp quota contention.
if [[ -n ${TMPDIR:-} ]]; then
    test_parent=$TMPDIR
elif [[ -n ${XDG_RUNTIME_DIR:-} && -d $XDG_RUNTIME_DIR && -w $XDG_RUNTIME_DIR ]]; then
    test_parent=$XDG_RUNTIME_DIR
else
    test_parent=/tmp
fi
test_scratch=$(mktemp -d "$test_parent/issue-agent-tests.XXXXXXXX")
trap 'rm -rf -- "$test_scratch"' EXIT
export TMPDIR=$test_scratch

stop_suite() {
    trap '' INT TERM
    kill -s "$1" -- "-$test_pid" 2>/dev/null || true
    wait "$test_pid" || true
    exit "$2"
}

# Give make and its children one process group so cancelling this wrapper also
# stops the suite before its scratch directory is removed.
set -m
"$@" &
test_pid=$!
set +m
trap 'stop_suite INT 130' INT
trap 'stop_suite TERM 143' TERM
wait "$test_pid"
