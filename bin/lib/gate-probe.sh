#!/usr/bin/env bash

# Allocate below the checkout so package managers see the same ancestor-local
# dependency stores as the daemon's per-issue worktrees. The returned directory
# is empty and ready for `git worktree add`.
create_gate_probe_worktree_dir() {
  local target_dir="${1:-}"
  local worktree_root="${2-.claude/worktrees}"
  local probe_root

  [ -d "$target_dir" ] || return 1
  case "$worktree_root" in
    /*) probe_root="$worktree_root" ;;
    "") probe_root="$target_dir" ;;
    *) probe_root="$target_dir/$worktree_root" ;;
  esac
  mkdir -p "$probe_root" || return 1
  mktemp -d "$probe_root/gate-probe.XXXXXX"
}
