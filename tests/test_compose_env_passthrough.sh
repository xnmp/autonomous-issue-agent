#!/usr/bin/env bash
# TARGET_ENV_ pass-through contract (compose-env.sh): a var the overlay declares
# with the TARGET_ENV_ prefix must reach target commands — re-emitted
# un-prefixed in the active env and EXCLUDED from the HARNESS_DEPLOY_KNOBS
# scrub manifest — while every plain knob stays manifest-scrubbed. Incident:
# the manifest (74b75a1) silently stripped CARGO_TARGET_DIR from every gate
# child, cold-building each worktree from 2026-07-29 to 2026-08-10.
set -eu

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT

# Compose against a scratch config tree, not the repo's: the script derives its
# root from its own location, so a copy under $scratch is fully isolated.
# point-at.sh rides along because compose-env parses the scrub floor from it.
mkdir -p "$scratch/bin" "$scratch/config/repos"
cp "$ROOT/bin/compose-env.sh" "$ROOT/bin/point-at.sh" "$scratch/bin/"
cat > "$scratch/config/shared.env" <<'EOF'
SHARED_KNOB="from-shared"
EOF
cat > "$scratch/config/repos/tp.env" <<'EOF'
PLAIN_KNOB="scrub-me"
TARGET_ENV_CARGO_TARGET_DIR="/tmp/tp cache/with space"
# Pass-through must win when the same name is ALSO assigned plainly.
CONTESTED="plain-loses"
TARGET_ENV_CONTESTED="passthrough-wins"
TARGET_ENV_MULTILINE="line one
line two"
EOF

active="$scratch/config/.active.tp.env"
fail() { echo "FAIL: $1" >&2; exit 1; }

# ── Executed mode: the file contract ─────────────────────────────────────────
# The ambient prefixed vars exported here must be IGNORED: a pass-through
# declaration comes from the config files, not from whoever launched compose.
env TARGET_ENV_AMBIENT="not-a-declaration" TARGET_ENV_TEST_CMD="ambient-cannot-floor-abort" \
  bash "$scratch/bin/compose-env.sh" tp
[ -f "$active" ] || fail "no active env written"
grep -q "^AMBIENT=" "$active" && fail "ambient TARGET_ENV_ treated as a declaration"
grep -q "^TEST_CMD=" "$active" && fail "ambient TARGET_ENV_TEST_CMD re-emitted"

grep -q "^CARGO_TARGET_DIR=" "$active" \
  || fail "pass-through var not re-emitted un-prefixed"
grep -q "^TARGET_ENV_CARGO_TARGET_DIR=" "$active" \
  || fail "prefixed declaration missing from active env"

# Read the manifest as consumers do — by sourcing (%q escapes its spaces).
manifest="$(bash -c ". '$active' && printf %s \"\$HARNESS_DEPLOY_KNOBS\"")"
case " $manifest " in
  *" CARGO_TARGET_DIR "*) fail "pass-through name leaked into the scrub manifest" ;;
esac
case " $manifest " in
  *"TARGET_ENV_CARGO_TARGET_DIR"*) : ;;
  *) fail "prefixed declaration missing from the scrub manifest" ;;
esac
case " $manifest " in
  *" PLAIN_KNOB "*) : ;;
  *) fail "plain knob missing from the scrub manifest" ;;
esac
case " $manifest " in
  *" CONTESTED "*) fail "contested name must be pass-through, not scrubbed" ;;
esac

# Sourcing the active file must yield the value (quoting survives the %q trip).
got="$(bash -c ". '$active' && printf %s \"\$CARGO_TARGET_DIR\"")"
[ "$got" = "/tmp/tp cache/with space" ] || fail "value mangled: got '$got'"
got="$(bash -c ". '$active' && printf %s \"\$CONTESTED\"")"
[ "$got" = "passthrough-wins" ] || fail "contested value: got '$got'"
got="$(bash -c ". '$active' && printf %s \"\$MULTILINE\"")"
[ "$got" = "line one
line two" ] || fail "newline value mangled: got '$got'"

# ── Refusals: scrub-floor and shell-critical names abort composition ─────────
# The floor is the #63 defense's last line; PATH can hijack the gate wrappers'
# own `env` resolution. Both must be refused LOUDLY, in a file declaration.
for bad in TARGET_ENV_TEST_CMD TARGET_ENV_PATH TARGET_ENV_RTC_SCRUB_STATIC TARGET_ENV__CE_ACTIVE; do
  cat > "$scratch/config/repos/bad.env" <<EOF
$bad="/tmp/evil"
EOF
  echo "PRIOR_SNAPSHOT=1" > "$scratch/config/.active.bad.env"
  if bash "$scratch/bin/compose-env.sh" bad 2>/dev/null; then
    fail "$bad accepted — must abort composition"
  fi
  # Abort must be atomic: prior snapshot untouched, no half-written stage left
  # for a hot-reloading daemon to source.
  grep -q "^PRIOR_SNAPSHOT=1$" "$scratch/config/.active.bad.env" \
    || fail "$bad: abort clobbered the prior snapshot"
  stage_leftovers="$(ls "$scratch/config/.active.bad.env.tmp."* 2>/dev/null || true)"
  [ -z "$stage_leftovers" ] || fail "$bad: abort left stage file(s): $stage_leftovers"
done

# Sourced-mode refusal: the abort must return (not kill the caller), and the
# deferred export must NOT have fired for the refused name.
cat > "$scratch/config/repos/bad.env" <<'EOF'
TARGET_ENV_PATH="/evil"
EOF
out="$(bash -c "cd '$scratch' && if . bin/compose-env.sh bad 2>/dev/null; then echo composed; else echo refused; fi; printf '%s' \"\${PATH}\" | grep -q '^/evil$' && echo exported || echo suppressed")"
[ "$(echo "$out" | sed -n 1p)" = "refused" ] || fail "sourced-mode refusal did not return nonzero"
[ "$(echo "$out" | sed -n 2p)" = "suppressed" ] || fail "sourced-mode abort still exported the refused value"

# Ambiguous provenance: the launching shell already exports the file's exact
# value (a heredoc-shaped scan hit + ambient export is indistinguishable) —
# must abort loudly, neither passing the ambient value nor silently dropping.
cat > "$scratch/config/repos/bad.env" <<'EOF'
TARGET_ENV_AMBIG="same-value"
EOF
if env TARGET_ENV_AMBIG="same-value" bash "$scratch/bin/compose-env.sh" bad 2>/dev/null; then
  fail "ambiguous provenance accepted — must abort"
fi
# ...but an ambient var whose value the files CHANGE composes fine.
env TARGET_ENV_AMBIG="different-ambient" bash "$scratch/bin/compose-env.sh" bad \
  || fail "changed-value composition should succeed"
grep -q "^AMBIG=same-value$" "$scratch/config/.active.bad.env" \
  || fail "changed-value pass-through missing from active env"

# A truncated point-at.sh (unparsable scrub floor) must fail CLOSED.
sed '/RTC_SCRUB_STATIC=/d' "$ROOT/bin/point-at.sh" > "$scratch/bin/point-at.sh"
if bash "$scratch/bin/compose-env.sh" tp 2>/dev/null; then
  fail "compose succeeded without a parsable scrub floor"
fi
cp "$ROOT/bin/point-at.sh" "$scratch/bin/"

# ── Sourced mode: the probe-shell contract ───────────────────────────────────
# point-at's gate probe inherits from the composing shell, so the stripped name
# must be exported there and the manifest must not scrub it.
out="$(bash -c "cd '$scratch' && . bin/compose-env.sh tp \
  && printf '%s\n' \"\$CARGO_TARGET_DIR\" \
  && case \" \$HARNESS_DEPLOY_KNOBS \" in *' CARGO_TARGET_DIR '*) echo scrubbed ;; *) echo clean ;; esac")"
[ "$(echo "$out" | sed -n 1p)" = "/tmp/tp cache/with space" ] \
  || fail "sourced mode: stripped name not exported into the composing shell"
[ "$(echo "$out" | sed -n 2p)" = "clean" ] \
  || fail "sourced mode: exported manifest scrubs the pass-through name"

echo "compose-env TARGET_ENV_ pass-through contract holds"
