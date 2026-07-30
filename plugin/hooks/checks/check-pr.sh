#!/usr/bin/env bash
# Governed: `gh pr create …`. The full pre-PR gate.
# Receives the whole `gh pr create` invocation tokens as argv.
#
# Re-derives truth from git + files rather than trusting commit prefixes, so a
# mislabeled commit cannot sneak through. Each check echoes a precise reason and
# exits 1 to block PR creation.
set -uo pipefail
fail() { echo "PR blocked: $1"; exit 1; }
# shellcheck source=_pr_common.sh
. "$(dirname "$0")/_pr_common.sh"

branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
base="${BASE_BRANCH:-dev}"

# Resolve the base ref, then the TWO distinct ranges it feeds (review M6):
# three dots for `git diff` (what this branch changed), two dots for `git log`
# (the commits this branch adds). Sharing one three-dot range made the log check
# see the BASE branch's own commits once the base advanced, blocking every
# legitimate PR. See _pr_common.sh's header for the full incident.
baseref="$(prc_resolve_base_ref)"
[ -n "$baseref" ] || fail "cannot resolve base branch '$base' to compute the diff"
diff_range="$baseref...HEAD"
log_range="$baseref..HEAD"

# ── 0. Extract PR title/body/base from argv (supports common gh flag spellings) ─
#       and reject --web (browser PR creation evades the gate's body checks).
body=""
pr_base=""
for ((i = 1; i <= $#; i++)); do
  case "${!i}" in
    --web)             fail "browser PR creation (--web) bypasses the gate; use a non-interactive 'gh pr create'" ;;
    --body | -b)       j=$((i + 1)); body="${!j:-}" ;;
    --body=*)          body="${!i#--body=}" ;;
    --body-file | -F)  j=$((i + 1)); f="${!j:-/dev/null}"; body="$(cat "$f" 2>/dev/null || true)" ;;
    --body-file=*)     f="${!i#--body-file=}"; body="$(cat "$f" 2>/dev/null || true)" ;;
    --base | -B)       j=$((i + 1)); pr_base="${!j:-}" ;;
    --base=*)          pr_base="${!i#--base=}" ;;
  esac
done

# ── 0b. PR MUST target BASE_BRANCH. `gh pr create` with no --base defaults to the
#        repo's DEFAULT branch (often main), not BASE_BRANCH (dev) — which routes
#        the PR through the wrong branch's CI and breaks auto-merge. Require an
#        explicit --base equal to BASE_BRANCH so the agent can't open against the
#        default branch by omission. (Observed live: #49 opened against main.)
[ -n "$pr_base" ] \
  || fail "PR has no --base; it must explicitly target '$base' (e.g. 'gh pr create --base $base …'). Without --base, gh defaults to the repo's default branch."
[ "$pr_base" = "$base" ] \
  || fail "PR base is '$pr_base' but must be '$base' (BASE_BRANCH). Re-create with '--base $base'."

# ── ORDER (review M21, mirrored from adapters/governor.py:_pr_gate) ────────────
# CHEAP structural checks (pure string/git-metadata arithmetic) run FIRST and
# short-circuit; the two EXPENSIVE checks that shell out to the full test command
# — §1 and §7c, the latter also mutating the worktree — run LAST. §1 used to run
# here, second, so a trivial denial (a wrong --base, a missing AC heading) still
# cost a full suite run before the agent saw it. The Python gate already ordered
# it this way; keeping the two identical is what tests/test_gate_parity.py pins,
# so a multi-violation case reports the SAME first reason on both stacks.

# The changed-file list every structural rule below reads (three-dot: only what
# this branch changed).
changed=$(git diff --name-only "$diff_range" 2>/dev/null || echo "")

# ── 2. PR body carries acceptance criteria ─────────────────────────────────────
printf '%s' "$body" | grep -qF '## Acceptance Criteria' \
  || fail "PR body is missing the '## Acceptance Criteria' section"

# ── 3. PR body links the issue with a closing keyword (Fixes/Closes/Resolves #N)
#       so issue_for_pr() / GitHub's closing graph can connect them. ────────────
printf '%s' "$body" | grep -qiE '(fix(es|ed)?|close[sd]?|resolve[sd]?) +#[0-9]+' \
  || fail "PR body must link the issue, e.g. 'Fixes #<issue>'"

# ── 7d. Visual acceptance criteria ⇒ ONE read+committed screenshot PER AC item.
#
# Runs among the CHEAP checks (it is pure body/marker/diff arithmetic), well
# before §1/§7c — a missing screenshot must not cost a full test run. Its slot
# here mirrors adapters/governor.py:_pr_gate's cheap_checks tuple exactly.
#
# Two layers, in this order:
#   FLOOR (unconditional): the agent declared a visual criterion
#   (.harness/visual-ac), so ≥1 screenshot image must be COMMITTED under
#   evidence/, and EVERY committed image must have been READ by the agent
#   (basename recorded in the harness-owned $HARNESS_SCREENSHOTS_READ receipt
#   by the PostToolUse(Read) hook) — an unread capture is unverified proof.
#   PER-AC (additive, only when the body's AC section parses into N ≥ 1 items):
#   N read+committed images AND N distinct evidence/ paths referenced in the
#   body. One shot for a five-criterion PR proves one criterion, and a shot
#   nobody links is invisible to the reviewer who must check each against its AC.
# An unparseable AC section counts 0 and therefore only applies the floor — a
# parse miss must never DENY on its own.
# Keep in sync with domain/gate.py:check_visual_evidence().

# Count the AC checklist items in the PR body. Contract (kept in sync with
# domain/gate.py:count_ac_items()): the SECTION starts at a heading line reading
# "Acceptance Criteria" (any level, any case) and ends at the next heading line
# or EOF; an ITEM is a markdown task-list line `- [ ]` / `- [x]` (also `*`/`+`),
# any indentation. Prose bullets are NOT items → 0 → "unparseable, floor only".
count_ac_items() {  # count_ac_items "<pr-body>"
  printf '%s\n' "$1" | awk '
    tolower($0) ~ /^[[:space:]]*#+[[:space:]]*acceptance criteria/ { in_ac = 1; next }
    in_ac && /^[[:space:]]*#+[[:space:]]/                          { in_ac = 0; next }
    in_ac && /^[[:space:]]*[-*+][[:space:]]+\[[ xX]\]/             { n++ }
    END { print n + 0 }'
}

if [ -f .harness/visual-ac ]; then
  # Committed screenshot images in the diff (same extension set as §7 /
  # domain/gate.py:IMAGE_EXTS).
  shots=$(printf '%s\n' "$changed" | grep -iE '^evidence/.*\.(png|jpg|jpeg|gif|webp)$' || true)
  [ -n "$shots" ] \
    || fail "a visual acceptance criterion was declared (.harness/visual-ac) but no screenshot is committed under evidence/ — capture one with agent-browser"
  [ -n "${HARNESS_SCREENSHOTS_READ:-}" ] \
    || fail "trusted screenshot-read receipt is unavailable — the harness cannot verify visual evidence"
  [ -r "$HARNESS_SCREENSHOTS_READ" ] \
    || fail "trusted screenshot-read receipt is unavailable — open each evidence image with the Read tool before opening the PR"

  unread=""
  while IFS= read -r shot; do
    [ -n "$shot" ] || continue
    b="$(basename "$shot")"
    grep -qxF "$b" "$HARNESS_SCREENSHOTS_READ" 2>/dev/null || unread="$unread $b"
  done <<< "$shots"
  [ -z "$unread" ] \
    || fail "committed screenshot(s) were never read/verified by the agent:$unread — open each with the Read tool and confirm it shows the criterion met before opening the PR"

  ac_items=$(count_ac_items "$body")
  if [ "$ac_items" -gt 0 ]; then
    n_shots=$(printf '%s\n' "$shots" | grep -c '.')
    [ "$n_shots" -ge "$ac_items" ] \
      || fail "$ac_items AC items but only $n_shots read+committed screenshots — capture one per AC item and reference each in the PR body (name the file after the criterion it proves, e.g. evidence/ac-2-empty-state.png)"
    # Distinct evidence/ image paths REFERENCED in the body (kept in sync with
    # domain/gate.py:_EVIDENCE_REF_RE).
    n_refs=$(printf '%s\n' "$body" \
      | grep -oiE 'evidence/[A-Za-z0-9._/-]+\.(png|jpg|jpeg|gif|webp)' \
      | sort -fu | grep -c '.')
    [ "${n_refs:-0}" -ge "$ac_items" ] \
      || fail "$ac_items AC items but the PR body references only ${n_refs:-0} evidence/ image path(s) — embed each screenshot next to its AC item (e.g. '![AC 2](evidence/ac-2-empty-state.png)' under that checklist item); a screenshot nobody links is invisible to the reviewer"
  fi
fi

# ── 5. Branch type ⇒ required new test in the diff (derive from git, not prefix) ─
case "$branch" in
  feat/*)
    # Prefer an explicit e2e/integration test, but accept any conventional test
    # file path (test_*.py, *_test.go, tests/…) so projects without "e2e" in the
    # filename are not falsely blocked.
    printf '%s\n' "$changed" | grep -qE '(e2e|integration).*\.(spec|test)\.|(^|/)test_.*\.py|.*_test\.(py|go|ts|js)|tests?/.*\.(py|spec|test)' \
      || fail "feat/ branch must add an e2e/integration (or conventional) test (none found in the diff)" ;;
  fix/*)
    printf '%s\n' "$changed" | grep -qE "$PR_TEST_FILE_RE" \
      || fail "fix/ branch must add a regression test (none found in the diff)" ;;
esac

# ── 6/7/7b. Commit hygiene + evidence + test-immutability (shared, state-derived) ─
prc_check_commit_log
prc_check_no_evidence "$changed"
prc_check_no_generated_instructions "$changed"
prc_check_tests_immutable "$changed"

# ── 9. ADR reference: if the diff touches a module an ADR governs, cite it ──────
#
# Ported to domain/gate.py:check_adr_references() (review M6): this rule had NO
# Python equivalent, so on the DEFAULT SDK path it never ran at all while
# AGENT.md told the fixer "the PR gate enforces this". Both stacks now implement
# it and tests/test_gate_parity.py runs the same fixtures through both.
if [ -d docs/adr ]; then
  for adr in docs/adr/[0-9]*.md; do
    [ -e "$adr" ] || continue
    governs=$(grep -i '^Governs:' "$adr" 2>/dev/null | sed 's/^[Gg]overns://') || true
    [ -n "$governs" ] || continue
    adr_file="$(basename "$adr")"
    id="$(printf '%s' "$adr_file" | grep -oE '^[0-9]+')"
    for path in $governs; do
      # -F: treat the governed path as a literal, not a regex (a '.' in a path
      # must not match arbitrary chars, and a lone '.' must not match everything).
      if printf '%s\n' "$changed" | grep -qF "$path"; then
        # Two SEPARATE greps, not one alternation: the filename arm has to be
        # -F (literal) for the same reason the path match above does — it used
        # to be interpolated into a -E alternation, where `0003-foo.md` matches
        # `0003-fooXmd`. The id arm stays -E because `adr[ -]?<id>` IS a pattern.
        if printf '%s' "$body" | grep -qiE "adr[ -]?$id"; then continue; fi
        if printf '%s' "$body" | grep -qiF "$adr_file"; then continue; fi
        fail "diff touches $path governed by ADR $id — reference it in the PR body"
      fi
    done
  done
fi

# ── EXPENSIVE (last, per the ORDER note above): both shell out to the resolved
#    test command; §7c additionally reverts + restores the worktree. ────────────

# ── 1. Tests pass once, locally, before the PR (shared with check-push.sh).
#       Fails CLOSED when no test command resolves — "untested" must not look
#       like "green" — unless the operator opted out with ALLOW_UNTESTED=1. ────
prc_check_tests

# ── 7c. The new tests must ACTUALLY fail without the implementation (real red).
#        Create-only: proves the tests exercise the change (anti-tautology). ─────
prc_check_red_fails "$changed"

exit 0
