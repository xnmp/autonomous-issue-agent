#!/usr/bin/env bash
# Bootstrap the harness into a target repository.
#
#   bin/bootstrap.sh /path/to/target-repo
#
# Idempotent. Does:
#   1. Install the plugin payload (settings.json, hooks/, AGENT.md) into the
#      target repo's .claude/  (this is the committed ENFORCEMENT layer).
#   2. Create the GitHub label taxonomy.
#   3. Gitignore the harness artifacts (worktrees, evidence/, marker files).
#   4. Seed INVARIANTS.md + docs/adr/0000-template.md (the steward's priors).
#   5. Generate ARCHITECTURE.md if missing (read-only claude pass; skippable).
#   6. Validate identities per IDENTITY_MODE.
set -uo pipefail

HARNESS_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
_AGENT_ENV="${AGENT_ENV:-$HARNESS_ROOT/config/agent.env}"
if [ -f "$_AGENT_ENV" ]; then
  # shellcheck disable=SC1091
  set -a; . "$_AGENT_ENV"; set +a
fi

TARGET="${1:-.}"
TARGET="$(cd "$TARGET" && pwd)" || { echo "target repo not found: $1"; exit 1; }
REPO="${REPO:?set REPO=owner/repo in config/agent.env}"
IDENTITY_MODE="${IDENTITY_MODE:-three}"
SKIP_ARCH="${SKIP_ARCHITECTURE:-0}"

echo "Bootstrapping harness for $REPO into $TARGET"
echo "  identity mode: $IDENTITY_MODE"

# ── 1. Install the plugin payload into the target's .claude/ ──────────────────
mkdir -p "$TARGET/.claude/hooks/checks"
cp "$HARNESS_ROOT/plugin/settings.json"           "$TARGET/.claude/settings.json"
cp "$HARNESS_ROOT/plugin/AGENT.md"                "$TARGET/.claude/AGENT.md"
cp "$HARNESS_ROOT/plugin/hooks/bash_governor.py"  "$TARGET/.claude/hooks/bash_governor.py"
cp "$HARNESS_ROOT/plugin/hooks/dispatch.sh"       "$TARGET/.claude/hooks/dispatch.sh"
cp "$HARNESS_ROOT/plugin/hooks/edit_check.sh"     "$TARGET/.claude/hooks/edit_check.sh"
cp "$HARNESS_ROOT/plugin/hooks/read_record.sh"    "$TARGET/.claude/hooks/read_record.sh"
cp "$HARNESS_ROOT"/plugin/hooks/checks/*.sh       "$TARGET/.claude/hooks/checks/"
chmod +x "$TARGET/.claude/hooks/dispatch.sh" "$TARGET/.claude/hooks/edit_check.sh" "$TARGET/.claude/hooks/read_record.sh" "$TARGET/.claude/hooks/checks/"*.sh
echo "  ✓ plugin payload installed in .claude/"

# ── 2. Labels ─────────────────────────────────────────────────────────────────
for l in ai-triage ai-ready ai-working ai-blocked ai-pr-open ai-done ai-suggestion \
         trivial minor major needs-human-review needs-design invariant \
         P1 P2 P3 harness; do
  gh label create "$l" --repo "$REPO" >/dev/null 2>&1 || true
done
echo "  ✓ labels ensured"

# ── 3. Gitignore harness artifacts ────────────────────────────────────────────
GI="$TARGET/.gitignore"
ensure_ignore() { grep -qxF "$1" "$GI" 2>/dev/null || echo "$1" >> "$GI"; }
[ -f "$GI" ] || : > "$GI"
{ grep -qF '# Agent harness' "$GI" 2>/dev/null || printf '\n# Agent harness\n' >> "$GI"; }
ensure_ignore ".claude/worktrees/"
# evidence/: screenshot IMAGES are committed (visual proof renders in the PR's
# Files-changed tab); everything else under evidence/ (logs, dumps) stays ignored.
ensure_ignore "evidence/**"
ensure_ignore "!evidence/**/"
ensure_ignore "!evidence/**/*.png"
ensure_ignore "!evidence/**/*.jpg"
ensure_ignore "!evidence/**/*.jpeg"
ensure_ignore "!evidence/**/*.gif"
ensure_ignore "!evidence/**/*.webp"
# All .harness/ markers are ephemeral per-PR state (ac-posted, test-plan-posted,
# adversarial-verdict, test-cmd) — never committed. Ignore the whole dir.
ensure_ignore ".harness/"
# Codex-runtime scaffolding written per-worktree by the harness (the governor
# PreToolUse hook + AGENTS.md = the fixer contract). Harness-owned, never part of
# the PR — same rationale as .harness/.
ensure_ignore ".codex/"
ensure_ignore "AGENTS.md"
ensure_ignore "AGENTS.override.md"
echo "  ✓ .gitignore updated"

# ── 4. Seed steward priors ─────────────────────────────────────────────────────
if [ ! -f "$TARGET/INVARIANTS.md" ]; then
  cat > "$TARGET/INVARIANTS.md" <<'EOF'
# Invariants

Checkable rules the fix and review agents MUST obey. Each is a one-line,
falsifiable statement. The steward proposes additions (label `invariant`); a
human approves before they land here.

<!-- e.g. "All writes to the Order aggregate use version-checked updates; a bare UPDATE is a bug." -->
EOF
  echo "  ✓ INVARIANTS.md seeded"
fi
mkdir -p "$TARGET/docs/adr"
if [ ! -f "$TARGET/docs/adr/0000-template.md" ]; then
  cat > "$TARGET/docs/adr/0000-template.md" <<'EOF'
# NNNN. <title>
Status: Proposed | Accepted | Superseded
Governs: <space-separated path globs this ADR governs, e.g. src/orders/>
Context: <forces at play>
Decision: <what we chose>
Consequences: <trade-offs, what this rules out>
EOF
  echo "  ✓ docs/adr/0000-template.md seeded"
fi

# ── 5. ARCHITECTURE.md (read-only generation) ──────────────────────────────────
if [ "$SKIP_ARCH" != 1 ] && [ ! -f "$TARGET/ARCHITECTURE.md" ] && command -v claude >/dev/null 2>&1; then
  ( cd "$TARGET" && claude -p --permission-mode "${PERMISSION_MODE:-auto}" \
      --tools 'Read,Bash(git *),Bash(ls *),Bash(cat *)' --disallowed-tools 'Edit,Write' \
      --max-turns 30 \
      "Analyze this codebase and output an ARCHITECTURE.md: module map, key dependencies,
data flow, patterns in use, and boundaries that should not be crossed. Concise reference doc." \
      > "$TARGET/ARCHITECTURE.md" 2>/dev/null ) && echo "  ✓ ARCHITECTURE.md generated" \
      || { rm -f "$TARGET/ARCHITECTURE.md"; echo "  ⚠ ARCHITECTURE.md generation skipped/failed"; }
fi

# ── 6. Validate identities ─────────────────────────────────────────────────────
: "${FIX_BOT:?set FIX_BOT}"
if [ "$IDENTITY_MODE" = "three" ]; then
  : "${REVIEW_BOT:?set REVIEW_BOT}"; : "${STEWARD_BOT:?set STEWARD_BOT}"
  # FIX must differ from REVIEW (GitHub 422 self-approval) and from STEWARD.
  # REVIEW and STEWARD may share an account (the steward never approves PRs).
  if [ "$FIX_BOT" = "$REVIEW_BOT" ] || [ "$FIX_BOT" = "$STEWARD_BOT" ]; then
    echo "✗ IDENTITY_MODE=three requires FIX_BOT to differ from REVIEW_BOT and STEWARD_BOT"; exit 1
  fi
  if [ "$REVIEW_BOT" = "$STEWARD_BOT" ]; then
    echo "  ✓ two distinct identities: fix=$FIX_BOT, review+steward=$REVIEW_BOT"
  else
    echo "  ✓ three distinct identities: $FIX_BOT / $REVIEW_BOT / $STEWARD_BOT"
  fi
else
  echo "  ✓ solo identity mode: $FIX_BOT (review step is human; auto-merge stays human)"
fi

echo "✓ Bootstrap complete."
echo "  Next: commit the target repo's .claude/ + INVARIANTS.md + docs/adr/,"
echo "        then start the watchers (see ops/ or 'make run-fix')."
