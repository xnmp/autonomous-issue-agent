#!/usr/bin/env bash

timestamp_lines() {
  local line
  while IFS= read -r line || [ -n "$line" ]; do
    printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$line"
  done
}

wait_for_watchers() {
  local pid rc status=0
  for pid in "$@"; do
    rc=0
    wait "$pid" || rc=$?
    if [ "$rc" -ne 0 ]; then
      printf 'watcher process %s exited %s\n' "$pid" "$rc" >&2
      [ "$status" -ne 0 ] || status="$rc"
    fi
  done
  return "$status"
}
