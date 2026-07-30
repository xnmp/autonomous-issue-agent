#!/usr/bin/env bash
# Governed: `git checkout -b <name>` / `git switch -c <name>`.
# Receives the subcommand-onward tokens as argv (already parsed by the governor;
# no re-parsing of the raw command string here).
#
# Enforces: branch names must be feat|fix|chore|refactor/<issue>-… so the PR gate
# and orchestration can derive the issue number and required-test class.
# Keep this prefix set in sync with domain.refs.
set -uo pipefail

name=""
for ((i = 1; i <= $#; i++)); do
  case "${!i}" in
    -b | -c)
      j=$((i + 1))
      name="${!j:-}"
      break
      ;;
  esac
done

# Not a branch-creating form (governor shouldn't route these, but be safe).
[ -n "$name" ] || exit 0

if [[ ! "$name" =~ ^(feat|fix|chore|refactor)/[0-9]+- ]]; then
  echo "Branch name '$name' is invalid."
  echo "Use <type>/<issue>-<desc> where type is feat|fix|chore|refactor, e.g. fix/42-null-token."
  exit 1
fi
exit 0
