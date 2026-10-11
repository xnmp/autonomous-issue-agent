#!/usr/bin/env bash
# Master agent — on-demand status query. Stateless; reads GitHub + state dirs
# fresh each time. Read-only tools. Prints the agent's summary to stdout.
#
# Usage:
#   bin/master.sh                 # "what happened in the last 24h?"
#   bin/master.sh "what's stuck?" # ad-hoc question
set -uo pipefail
# shellcheck disable=SC1090
set -a
. "${AGENT_ENV:?AGENT_ENV is unset or empty; source bin/compose-env.sh <name> (config/shared.env + config/repos/<name>.env)}" || exit
set +a
REPO="${REPO:?set REPO=owner/repo}"
PERMISSION_MODE="${PERMISSION_MODE:-auto}"

QUESTION="${1:-Summarize agent activity in the last 24h for $REPO: issues moved to ai-done,
open PRs and their review state, ai-blocked issues, needs-human-review PRs, CI retries
exhausted, and any harness issues raised. For each blocker, say what is needed to unblock.}"

claude -p --permission-mode "$PERMISSION_MODE" \
  --tools 'Read,Bash(gh *),Bash(cat *),Bash(ls *)' --disallowed-tools 'Edit,Write' \
  --max-turns 20 \
  "Repo: $REPO

$QUESTION"
