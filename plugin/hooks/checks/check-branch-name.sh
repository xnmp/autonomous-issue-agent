#!/usr/bin/env bash
# Governed: `git checkout -b <name>` / `git switch -c <name>`.
# Receives the subcommand-onward tokens as argv (already parsed by the governor;
# no re-parsing of the raw command string here).
#
# The generated domain policy keeps creation and publication on the same types.
set -uo pipefail
. "$(dirname "$0")/_branch_policy.sh"

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

if [[ ! "$name" =~ $HARNESS_ISSUE_BRANCH_RE ]]; then
  echo "Branch name '$name' is invalid."
  echo "Use a supported conventional <type>/<issue>-<desc>, e.g. fix/42-null-token."
  exit 1
fi
exit 0
