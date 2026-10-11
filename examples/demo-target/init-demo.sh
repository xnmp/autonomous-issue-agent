#!/usr/bin/env bash
# Stand up the demo end-to-end so you can WATCH the loop on a throwaway GitHub repo.
#
#   DEMO_REPO=you/calc-demo bash examples/demo-target/init-demo.sh
#
# Steps:
#   1. Create the GitHub repo (private) if it doesn't exist, with a `dev` branch.
#   2. Push the demo target's contents (src/tests/docs/CLAUDE.md/INVARIANTS.md).
#   3. Run bin/bootstrap.sh to install the plugin + labels + gitignore.
#   4. Seed the demo issues (ai-ready).
#   5. Print how to start the watchers.
#
# Requires: gh authenticated (REPO is set from DEMO_REPO).
set -uo pipefail
HARNESS_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEMO_SRC="$HARNESS_ROOT/examples/demo-target"
DEMO_REPO="${DEMO_REPO:?set DEMO_REPO=owner/repo for the throwaway demo repo}"
BASE_BRANCH="${BASE_BRANCH:-dev}"
SCRATCH="$DEMO_SRC/.scratch/clone"

echo "Demo init for $DEMO_REPO"

# 1. Create the repo if missing.
if ! gh repo view "$DEMO_REPO" >/dev/null 2>&1; then
  gh repo create "$DEMO_REPO" --private --confirm >/dev/null 2>&1 \
    || gh repo create "$DEMO_REPO" --private -y >/dev/null 2>&1
  echo "  ✓ created repo $DEMO_REPO"
else
  echo "  • repo already exists"
fi

# 2. Materialise the demo content in a clean clone and push dev.
rm -rf "$SCRATCH"; mkdir -p "$SCRATCH"
git clone "https://github.com/$DEMO_REPO.git" "$SCRATCH" 2>/dev/null || {
  # empty repo → init locally and set the remote
  ( cd "$SCRATCH" && git init -q && git remote add origin "https://github.com/$DEMO_REPO.git" )
}
# copy demo payload (exclude scratch + scripts that only matter in the harness)
( cd "$DEMO_SRC" && tar --exclude=.scratch --exclude=init-demo.sh -cf - \
    src tests docs CLAUDE.md INVARIANTS.md pyproject.toml 2>/dev/null ) | ( cd "$SCRATCH" && tar -xf - )
(
  cd "$SCRATCH"
  git config user.email demo@example.com; git config user.name "demo"
  git checkout -q -B "$BASE_BRANCH"
  git add -A
  git -c core.hooksPath=/dev/null commit -qm "chore: seed demo calculator" 2>/dev/null || true
  git push -q -u origin "$BASE_BRANCH" 2>/dev/null || git push -q origin "$BASE_BRANCH"
)
echo "  ✓ pushed $BASE_BRANCH"

# 3. Bootstrap the harness into the clone, then push the installed .claude/.
REPO="$DEMO_REPO" SKIP_ARCHITECTURE=1 bash "$HARNESS_ROOT/bin/bootstrap.sh" "$SCRATCH"
(
  cd "$SCRATCH"
  git add -A
  git -c core.hooksPath=/dev/null commit -qm "chore: install agent harness plugin" 2>/dev/null || true
  git push -q origin "$BASE_BRANCH" 2>/dev/null || true
)
echo "  ✓ bootstrapped + pushed plugin"

# 4. Seed issues.
REPO="$DEMO_REPO" bash "$DEMO_SRC/seed-issues.sh"

cat <<EOF

✓ Demo ready on $DEMO_REPO (base branch: $BASE_BRANCH)
  Configure config/shared.env, then run issue-agent start from $SCRATCH.
  Set the repo overlay's BASE_BRANCH=$BASE_BRANCH and TEST_CMD='pytest -q'.
  Preflight:   issue-agent doctor --target-dir "$SCRATCH" --repo "$DEMO_REPO"
  Watch:       source bin/compose-env.sh <name> && bin/master.sh
EOF
