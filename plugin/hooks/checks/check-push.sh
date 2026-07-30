#!/usr/bin/env bash
# Governed: `git push` — but ONLY enforces when the current branch already has an
# OPEN PR. This re-runs the state-derived gate on address-review pushes (new
# commits added to a PR after creation), which the create-time gate never sees.
#
# Deliberately NOT enforced when there is no open PR yet: the first pushes while
# building a branch are normal; check-pr.sh gates the PR at creation time.
#
# Shares its checks with check-pr.sh via _pr_common.sh (tests, commit hygiene,
# evidence, test-immutability). Body/marker/adversarial checks are create-only.
set -uo pipefail
fail() { echo "Push blocked: $1"; exit 1; }
# shellcheck source=_pr_common.sh
. "$(dirname "$0")/_pr_common.sh"

base="${BASE_BRANCH:-dev}"

for arg in "$@"; do
  case "$arg" in
    --all|--mirror)
      fail "bulk push modes (--all/--mirror) cannot be validated against one checkout HEAD; push explicit HEAD:<destination> refspecs instead"
      ;;
  esac
done

previous_arg=""
for arg in "$@"; do
  config_override=""
  if [ "$previous_arg" = "-c" ]; then
    config_override="$arg"
  fi
  case "$arg" in
    -cremote.*.push=*) config_override="${arg#-c}" ;;
    --config-env=remote.*.push=*) config_override="${arg#--config-env=}" ;;
  esac
  case "$config_override" in
    remote.*.push=*)
      fail "command-local remote.*.push overrides cannot be validated; use an explicit HEAD:<destination> refspec"
      ;;
  esac
  previous_arg="$arg"
done

prc_check_push_destination() {
  local branch="$1"
  local source="$2"
  local pr="" pr_rc=1 baseref diff_range log_range changed
  local source_commit head_commit

  # Only governed-shape branches carry harness work; anything else is not this
  # gate's business, but later destinations in the same push still are.
  case "$branch" in
    feat/*|fix/*|chore/*|refactor/*) : ;;
    *) return 0 ;;
  esac

  # Is there an OPEN PR for this destination? If not, this is a pre-PR push.
  # Lookup failures retry once and then fail closed.
  for _attempt in 1 2; do
    pr=$(gh pr list --head "$branch" --state open --json number --jq '.[0].number // ""' 2>/dev/null)
    pr_rc=$?
    [ "$pr_rc" -eq 0 ] && break
    [ "$_attempt" = 1 ] && sleep "${GATE_GH_RETRY_SLEEP:-2}"
  done
  [ "$pr_rc" -eq 0 ] \
    || fail "could not verify PR state for branch '$branch' — 'gh pr list' failed twice (gh/network/auth error). The push gate needs to know whether an open PR exists, so it fails CLOSED rather than letting an unverified push through. Check connectivity and 'gh auth status', then retry the push shortly."
  [ -n "$pr" ] || return 0

  source_commit=$(git rev-parse --verify --quiet "${source}^{commit}" 2>/dev/null) \
    || fail "source ref '${source:-"(empty)"}' does not resolve to checked-out HEAD — check out that source or push HEAD:$branch"
  head_commit=$(git rev-parse --verify --quiet "HEAD^{commit}" 2>/dev/null) \
    || fail "source ref '${source:-"(empty)"}' does not resolve to checked-out HEAD — check out that source or push HEAD:$branch"
  [ "$source_commit" = "$head_commit" ] \
    || fail "source ref '${source:-"(empty)"}' does not resolve to checked-out HEAD — check out that source or push HEAD:$branch"

  # Resolve the base once per governed destination. The working-tree state is
  # shared, while the issue identity and open-PR lookup belong to the ref being
  # written.
  baseref="$(prc_resolve_base_ref)"
  [ -n "$baseref" ] || return 0
  diff_range="$baseref...HEAD"
  log_range="$baseref..HEAD"
  changed=$(git diff --name-only "$diff_range" 2>/dev/null || echo "")

  prc_check_commit_log
  prc_check_no_evidence "$changed"
  prc_check_no_generated_instructions "$changed"
  prc_check_tests_immutable "$changed"
  prc_check_tests
}

prc_add_push_refspec() {
  local body="${1#+}" source destination
  case "$body" in
    *'*'*) fail "wildcard push refspecs cannot be validated against one checkout HEAD; push explicit HEAD:<destination> refspecs instead" ;;
  esac
  if printf '%s' "$body" | grep -q ':'; then
    source="${body%%:*}"
    destination="${body#*:}"
  else
    source="$body"
    destination="$body"
  fi
  [ -z "$delete_mode" ] || source=""
  destination="${destination#refs/heads/}"
  case "$destination" in
    HEAD|@) destination="$checked_out_branch" ;;
  esac
  sources+=("$source")
  destinations+=("$destination")
}

checked_out_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
sources=()
destinations=()
remote_seen=""
remote_name=""
delete_mode=""
for arg in "$@"; do
  case "$arg" in
    --delete|-d|-[^-]*d*) delete_mode=1 ;;
  esac
done
for arg in "$@"; do
  [ "$arg" = "push" ] && continue
  case "$arg" in -*) continue ;; esac
  if [ -z "$remote_seen" ]; then
    remote_seen=1
    case "$arg" in
      +*|*:*) prc_add_push_refspec "$arg" ;;
      *) remote_name="$arg" ;;
    esac
    continue
  fi
  prc_add_push_refspec "$arg"
done

if [ "${#destinations[@]}" -eq 0 ]; then
  if [ -z "$remote_name" ]; then
    for key in \
      "branch.${checked_out_branch}.pushRemote" \
      "remote.pushDefault" \
      "branch.${checked_out_branch}.remote"; do
      remote_name=$(git config --get "$key" 2>/dev/null)
      config_rc=$?
      [ "$config_rc" -le 1 ] \
        || fail "refspec-less push configuration cannot be validated; push explicit HEAD:<destination> refspecs instead"
      [ -z "$remote_name" ] || break
    done
  fi
  remote_name="${remote_name:-origin}"

  mirror=$(git config --bool --get "remote.${remote_name}.mirror" 2>/dev/null)
  config_rc=$?
  [ "$config_rc" -le 1 ] \
    || fail "refspec-less push configuration cannot be validated; push explicit HEAD:<destination> refspecs instead"
  [ "$mirror" != "true" ] \
    || fail "refspec-less push configuration cannot be validated; push explicit HEAD:<destination> refspecs instead"

  configured=$(git config --get-all "remote.${remote_name}.push" 2>/dev/null)
  config_rc=$?
  [ "$config_rc" -le 1 ] \
    || fail "refspec-less push configuration cannot be validated; push explicit HEAD:<destination> refspecs instead"
  if [ "$config_rc" -eq 0 ]; then
    while IFS= read -r configured_refspec; do
      [ -z "$configured_refspec" ] || prc_add_push_refspec "$configured_refspec"
    done <<< "$configured"
  else
    push_default=$(git config --get "push.default" 2>/dev/null)
    config_rc=$?
    [ "$config_rc" -le 1 ] \
      || fail "refspec-less push configuration cannot be validated; push explicit HEAD:<destination> refspecs instead"
    push_default="${push_default:-simple}"
    case "$push_default" in
      current|simple) : ;;
      *) fail "refspec-less push configuration cannot be validated; push explicit HEAD:<destination> refspecs instead" ;;
    esac
  fi
fi
if [ "${#destinations[@]}" -eq 0 ]; then
  sources=("HEAD")
  destinations=("$checked_out_branch")
fi
for index in "${!destinations[@]}"; do
  prc_check_push_destination "${destinations[$index]}" "${sources[$index]}"
done

exit 0
