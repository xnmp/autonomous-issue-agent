#!/usr/bin/env bash

# Pin nested CLI calls to the harness project. The launcher may be invoked from
# a target that has its own pyproject.toml or even an older issue-agent package.
run_harness_issue_agent() (
  : "${HARNESS_ROOT:?HARNESS_ROOT must identify the harness checkout}"
  cd "$HARNESS_ROOT" || return 1
  uv run --quiet issue-agent "$@"
)
