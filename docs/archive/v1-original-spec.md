# Autonomous Issue Resolution with Claude Code

Local workflow for autonomously resolving GitHub issues, with a headless loop that only pauses for human input when genuinely stuck. The full lifecycle spans from issue creation to PR merge.

## Design

Two independent systems coordinating through GitHub:

```
FIX AGENT (machine A)                    REVIEW AGENT (machine B)
─────────────────────                    ────────────────────────
Issue arrives                            
  → resolve headless                     
  → open PR                              PR appears
                                           → review headless
  review arrives                           → approve or request changes
  → address feedback headless            
  → push fixes                           new commits detected
                                           → re-review headless
  CI fails                                 → approve or request changes
  → fix CI headless                      
  → push                                 ...loop...

                    PR approved + merged
                    Both agents clean up.
```

Both agents are always headless. They talk to each other through GitHub reviews/comments. Neither knows the other is an agent. A human can substitute for either at any point.

## Full Lifecycle

```
┌─────────────────────────────────────────────────────────────┐
│                                                             │
│  PHASE 1: Resolve Issue                                     │
│                                                             │
│  claude -p (headless)                                       │
│    ↓                                                        │
│  PR opened? ──yes──→ PHASE 2                                │
│    ↓ no                                                     │
│  Post question to GitHub issue                              │
│    ↓                                                        │
│  Human replies (any human, async)                           │
│    ↓                                                        │
│  claude -p --resume (headless)                              │
│    ↓                                                        │
│  └──→ loop until PR opened                                  │
│                                                             │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  PHASE 2: CI Checks                                         │
│                                                             │
│  Watcher polls CI status on open PRs                        │
│    ↓                                                        │
│  Checks pass? ──yes──→ wait for review                      │
│    ↓ no                                                     │
│  claude -p --resume with failure log (headless)             │
│    ↓                                                        │
│  Pushes fix → CI re-runs                                    │
│    ↓                                                        │
│  └──→ loop (max 3 attempts, then mark blocked)              │
│                                                             │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  PHASE 3: Address PR Review                                 │
│                                                             │
│  Reviewer comments / requests changes                       │
│    ↓                                                        │
│  claude -p --resume (headless)                              │
│    ↓                                                        │
│  Pushes fixes                                               │
│    ↓                                                        │
│  Still needs input? → ask on PR → human replies → resume    │
│    ↓                                                        │
│  └──→ loop until reviewer approves                          │
│                                                             │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  PHASE 4: Merge + Cleanup                                   │
│                                                             │
│  PR approved + merged (by human or auto-merge)              │
│    ↓                                                        │
│  Watcher detects merge                                      │
│    ↓                                                        │
│  Remove worktree, delete session file, remove labels        │
│    ↓                                                        │
│  Done.                                                      │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

## Fix Agent Specification

The fix agent is a Claude plugin (`.claude/` directory) that can be dropped into any repo. All enforcement is via Claude hooks — no GitHub Actions required.

### Plugin Structure

```
.claude/
  settings.json          # Hooks configuration
  hooks/
    check-branch-name.sh # On branch creation
    check-commit.sh      # On every commit (lint, typecheck, tests)
    check-pr.sh          # On PR creation (all gates)
  AGENT.md               # Agent behavioral instructions (appended to system prompt)
```

### Agent Flow (DDD + TDD)

```
1. Create branch: feat/<issue>-description or fix/<issue>-description
2. Post test plan comment on issue (what will be tested + proof type)
3. Write interfaces / types (domain-first design)
4. Write tests (commit with red: prefix — tests expected to fail)
5. Implement (commit with green: prefix — tests now pass)
6. Collect proof artifacts → evidence/ directory
7. Update docs if needed
8. Adversarial self-review (inline subagent)
9. Open PR
```

### Commit Structure

| Commit prefix | Contains | Hook enforcement |
|--------------|----------|-----------------|
| `plan:` | Interfaces, types, test plan | Lint + typecheck only |
| `red:` | Failing tests (TDD red step) | Lint + typecheck only (tests skipped) |
| `green:` | Implementation making tests pass | Lint + typecheck + tests must pass |
| `evidence:` | Screenshots, test output in `evidence/` | Lint only |
| `docs:` | Documentation updates | Lint only |

### Hook Timing

| When | What fires | Checks |
|------|-----------|--------|
| Branch creation (`git checkout -b`, `git switch -c`) | `check-branch-name.sh` | Matches `feat/<issue>-*` or `fix/<issue>-*` |
| Every commit (`git commit`) | `check-commit.sh` | Lint + typecheck always. Tests unless `red:` or `plan:` or `evidence:` or `docs:` prefix. |
| PR creation (`gh pr create`) | `check-pr.sh` | See full checklist below |

### PR Creation Gate (check-pr.sh)

All checks must pass or the PR is blocked:

1. **Unit tests pass** — runs test suite one final time
2. **PR body contains `## Acceptance Criteria`**
3. **Issue has a `## Test Plan` comment** — posted by agent before coding
4. **`feat/` branches** — diff includes new file in e2e test directory
5. **`fix/` branches** — diff includes new file in regression test directory
6. **`evidence/` directory exists and is non-empty** — proof artifacts committed
7. **Adversarial subagent passes** — inline subagent checks:
   - Screenshots show feature working (not error state)
   - Evidence matches acceptance criteria
   - Diff contains nothing unrelated/suspicious

### Test Plan & Proof of Work

The agent posts a `## Test Plan` comment on the issue before writing code:

```markdown
## Test Plan

**Tests:**
- [ ] E2e test for login redirect after OAuth callback
- [ ] Unit test for token refresh logic

**Proof type:** Screenshots (UI change)

**Evidence will include:**
- Screenshot of successful OAuth redirect
- Screenshot of error state handling
```

If the agent decides tests aren't needed (config change, docs-only), it justifies:

```markdown
## Test Plan

**Tests:** None required — this is a configuration-only change (updating env vars).

**Justification:** No logic modified. Existing e2e tests cover the affected behavior.

**Proof type:** Test output (existing tests still pass)
```

The review agent can challenge a "no tests needed" justification.

### Adversarial Pre-PR Review (Inline Subagent)

Runs inside the fix agent session as the last step before PR creation. Gets:
- The full diff
- Contents of `evidence/` (screenshots)
- The acceptance criteria from the issue

Answers three binary questions:
1. Do the screenshots show the feature/fix working (not an error state)?
2. Does the evidence match what the acceptance criteria describe?
3. Is there anything in the diff that looks unrelated/suspicious?

If any answer is "no" → returns a one-line reason → agent must fix before retrying.

### Skills Available to Fix Agent

- `/diagnose` — structured debugging using logs. Use when tests fail unexpectedly or behavior is wrong. Follows: reproduce → minimise → hypothesise → instrument → fix → regression-test.
- Research subagents — for understanding unfamiliar code areas before making changes

### Logging Requirements

The agent adds structured logging to all new code. This is both for observability and to support `/diagnose` when debugging.

```
## Logging Rules
- Add structured logging at domain boundaries:
  - Inputs/outputs of public methods
  - State transitions (e.g., order status changes)
  - Error paths (with context about what was attempted)
- Use appropriate log levels: debug for flow tracing, info for business events, warn for recoverable issues, error for failures
- Include correlation IDs where applicable (request ID, user ID, entity ID)
- Never log secrets, tokens, passwords, or PII
- Format: structured JSON logs (not console.log("something happened"))
```

### Docs as First-Class Objects

Every directory can have a `CLAUDE.md` that documents:
- **Responsibilities** — what this module/folder owns
- **Gotchas** — non-obvious constraints, edge cases, "why it's like this"
- **Dependencies** — what this module depends on and what depends on it
- **Common patterns** — how to add new things following existing conventions

The fix agent updates these as it learns:

```
## Documentation Rules
- Before working in a directory, read its CLAUDE.md if one exists.
- If you discover something non-obvious while working (a hidden constraint, a surprising dependency,
  why something is implemented in a counterintuitive way, a gotcha that cost you debugging time),
  update that folder's CLAUDE.md.
- Do NOT document obvious things (what the code does — the code says that).
- DO document WHY things are the way they are, especially if you almost did it wrong first.
- If no CLAUDE.md exists in a folder you're significantly modifying, create one.
- Doc updates go in a commit with prefix 'docs:'.
```

The review agent checks for this as part of its review:
- If a complex change touches a directory with no `CLAUDE.md`, flag it.
- If the agent clearly learned something non-obvious (visible from the commit history — e.g., a failed approach then a fix), but didn't document it, request the doc update.

### Agent Prompt (--append-system-prompt)

```bash
AGENT_PROMPT="You are an autonomous issue-resolving agent running in headless mode.

## Workflow
1. Create branch: feat/$ISSUE_NUMBER-<short-desc> or fix/$ISSUE_NUMBER-<short-desc>
2. Read the issue. If acceptance criteria are missing, generate them and post as a comment.
3. Post a ## Test Plan comment on the issue declaring what you'll test and your proof type.
4. Read CLAUDE.md files in the directories you'll be working in.
5. Write domain interfaces/types first. Commit with prefix 'plan:'.
6. Write tests. Commit with prefix 'red:' (tests may fail at this point).
7. Implement. Commit with prefix 'green:' (tests must pass).
8. Collect proof: screenshots (agent-browser) or test output → evidence/ directory. Commit with prefix 'evidence:'.
9. Update folder CLAUDE.md files with anything non-obvious you learned. Commit with prefix 'docs:'.
10. Open PR with '## Acceptance Criteria' in the body and 'Fixes #$ISSUE_NUMBER'.

## Debugging
- When tests fail unexpectedly or behavior is wrong, use /diagnose.
- /diagnose follows: reproduce → minimise → hypothesise → instrument → fix → regression-test.
- Add targeted logging/instrumentation BEFORE changing logic when the root cause is unclear.

## Logging
- Add structured logging at domain boundaries (inputs/outputs of public methods, state transitions, error paths).
- Use log levels appropriately: debug for flow, info for business events, warn for recoverable, error for failures.
- Include correlation IDs (request ID, entity ID) where applicable.
- Format as structured JSON. Never log secrets or PII.

## Documentation
- Read folder CLAUDE.md files before working in a directory.
- If you learn something non-obvious (a gotcha, a hidden constraint, why something is counterintuitive), update that folder's CLAUDE.md.
- If no CLAUDE.md exists in a folder you significantly modify, create one with: responsibilities, gotchas, dependencies, common patterns.
- Do NOT document the obvious. Document the WHY, especially things you almost got wrong.

## Constraints
- Do not ask unnecessary questions. If you truly cannot proceed, state exactly what you need and stop.
- Do not open a PR unless all hooks pass.
- Do not skip, disable, or modify existing tests.
- Do not refactor unrelated code.
- If tests aren't needed, justify in the test plan. The review agent will evaluate your justification.
- If you hit a harness problem (hooks blocking valid actions, prompts causing confusion), report it to the harness repo and continue with a workaround.

## Test Rules
$TEST_RULES"
```

### Repo-Specific Checks (SonarQube, etc.)

The plugin doesn't replace a repo's existing CI gates — it supplements them. After the PR is opened, the repo's own checks (SonarQube, linting, CI pipelines) will run. The fix agent's watcher already handles this via `fix-ci.sh` — if any check fails, the agent resumes and fixes.

To make the agent aware of repo-specific checks, add them to the repo's `CLAUDE.md`:

```markdown
## CI Checks

After opening a PR, these external checks must pass:
- SonarQube quality gate (no new critical issues, coverage >= 80%)
- GitHub Actions CI (build + integration tests)

If SonarQube fails, check the issues at: <sonarqube-url>/project/issues?id=<project-key>
```

The agent reads this from `CLAUDE.md` and knows what to expect. The watcher's CI polling loop (`fix-ci.sh`) handles the rest — it doesn't care whether the failure is from the plugin's hooks or SonarQube.

### Evidence Cleanup

The `evidence/` directory is committed to the branch for reviewability. On merge, it is auto-deleted (a post-merge hook or the cleanup script removes it so it doesn't land on main).

### Configuration: .claude/settings.json

```json
{
  "permissions": {
    "allow": ["Bash", "Read", "Edit", "Write"]
  },
  "hooks": {
    "Bash": {
      "pre": [
        {
          "match": "git checkout -b|git switch -c",
          "command": [".claude/hooks/check-branch-name.sh"],
          "blocking": true
        },
        {
          "match": "git commit",
          "command": [".claude/hooks/check-commit.sh"],
          "blocking": true
        },
        {
          "match": "gh pr create",
          "command": [".claude/hooks/check-pr.sh"],
          "blocking": true
        }
      ]
    }
  }
}
```

## Key Behavior: `claude -p` Never Pauses

`claude -p` always runs to completion and exits. It does not block for input.

| Scenario | What happens |
|----------|--------------|
| Permission blocked | Exits non-zero. Solved by `--permission-mode bypassPermissions`. |
| Needs clarification | Outputs its question as final response, exits **0**. |

Detecting "stuck" means inspecting the output, not exit codes.

## Key Primitives

| What | How |
|------|-----|
| Autonomous run | `claude -p "..." --permission-mode bypassPermissions --worktree` |
| Capture session | `--output-format json` → extract `session_id` |
| Resume headless | `claude -p "..." --resume <session_id>` |
| Cost/safety cap | `--max-turns 50 --max-budget-usd 2.00` |

## Scripts

### resolve-issue.sh — Initial headless resolution

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="owner/repo"
ISSUE_NUMBER="$1"
ISSUE_BODY="$(gh issue view "$ISSUE_NUMBER" --repo "$REPO" --json title,body --jq '.title + "\n\n" + .body')"
STATE_DIR="$HOME/.local/state/issue-agent"
mkdir -p "$STATE_DIR"

AGENT_PROMPT="You are an autonomous issue-resolving agent. Do not ask unnecessary questions. If you truly cannot proceed, state exactly what you need and stop. Do not open a PR unless tests pass. Include 'Fixes #$ISSUE_NUMBER' in the PR body."

# Run headless
result=$(claude -p \
  --permission-mode bypassPermissions \
  --worktree "issue-$ISSUE_NUMBER" \
  --append-system-prompt "$AGENT_PROMPT" \
  --max-turns 50 \
  --output-format json \
  "Resolve GitHub issue #$ISSUE_NUMBER in $REPO.

$ISSUE_BODY")

session_id=$(echo "$result" | jq -r '.session_id')
response=$(echo "$result" | jq -r '.result')

# Save session for future resumes
echo "$session_id" > "$STATE_DIR/issue-$ISSUE_NUMBER.session"

# Did it open a PR?
if echo "$response" | grep -qiE "opened (a )?pull request|opened PR|created PR"; then
  echo "✓ Issue #$ISSUE_NUMBER resolved. PR opened."
  gh issue edit "$ISSUE_NUMBER" --repo "$REPO" --remove-label "ai-working" --add-label "ai-pr-open"
  exit 0
fi

# Stuck — post question to GitHub
gh issue comment "$ISSUE_NUMBER" --repo "$REPO" --body "$(cat <<EOF
**Agent needs input:**

$response

---
_Reply to unblock. Any team member can answer._
EOF
)"

gh issue edit "$ISSUE_NUMBER" --repo "$REPO" --remove-label "ai-working" --add-label "ai-blocked"
echo "⏸ Issue #$ISSUE_NUMBER blocked. Question posted."
```

### resume-issue.sh — Feed human reply back (headless)

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="owner/repo"
ISSUE_NUMBER="$1"
STATE_DIR="$HOME/.local/state/issue-agent"
SESSION_FILE="$STATE_DIR/issue-$ISSUE_NUMBER.session"

[ -f "$SESSION_FILE" ] || { echo "No session for issue #$ISSUE_NUMBER"; exit 1; }
session_id=$(cat "$SESSION_FILE")

# Get the latest human comment
human_reply=$(gh issue view "$ISSUE_NUMBER" --repo "$REPO" --json comments \
  --jq '.comments[-1].body')

gh issue edit "$ISSUE_NUMBER" --repo "$REPO" --remove-label "ai-blocked" --add-label "ai-working"

# Resume headless
result=$(claude -p \
  --permission-mode bypassPermissions \
  --max-turns 50 \
  --output-format json \
  --resume "$session_id" \
  "Human replied with:

$human_reply

Continue resolving the issue. Open a PR when done. If you still need more info, state what you need.")

response=$(echo "$result" | jq -r '.result')

if echo "$response" | grep -qiE "opened (a )?pull request|opened PR|created PR"; then
  echo "✓ Issue #$ISSUE_NUMBER resolved after clarification."
  gh issue edit "$ISSUE_NUMBER" --repo "$REPO" --remove-label "ai-working" --add-label "ai-pr-open"
  exit 0
fi

# Still stuck
gh issue comment "$ISSUE_NUMBER" --repo "$REPO" --body "$(cat <<EOF
**Agent still needs input:**

$response
EOF
)"
gh issue edit "$ISSUE_NUMBER" --repo "$REPO" --remove-label "ai-working" --add-label "ai-blocked"
```

### fix-ci.sh — Fix failing CI checks (headless)

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="owner/repo"
PR_NUMBER="$1"
STATE_DIR="$HOME/.local/state/issue-agent"
MAX_CI_RETRIES=3

# Find linked issue
ISSUE_NUMBER=$(gh pr view "$PR_NUMBER" --repo "$REPO" --json body \
  --jq '.body' | grep -oP '#\K[0-9]+' | head -1)

SESSION_FILE="$STATE_DIR/issue-$ISSUE_NUMBER.session"
[ -f "$SESSION_FILE" ] || { echo "No session for PR #$PR_NUMBER"; exit 1; }
session_id=$(cat "$SESSION_FILE")

# Get the retry count (prevent infinite fix loops)
RETRY_FILE="$STATE_DIR/issue-$ISSUE_NUMBER.ci-retries"
retries=$(cat "$RETRY_FILE" 2>/dev/null || echo 0)

if [ "$retries" -ge "$MAX_CI_RETRIES" ]; then
  echo "⚠ CI fix attempts exhausted for PR #$PR_NUMBER ($MAX_CI_RETRIES tries)"
  gh pr comment "$PR_NUMBER" --repo "$REPO" --body "$(cat <<EOF
**Agent gave up fixing CI after $MAX_CI_RETRIES attempts.**

Manual intervention needed. The failing checks are:

$(gh pr checks "$PR_NUMBER" --repo "$REPO" --failed)
EOF
)"
  gh pr edit "$PR_NUMBER" --repo "$REPO" --add-label "ai-blocked"
  exit 1
fi

# Get failed check details
failed_checks=$(gh pr checks "$PR_NUMBER" --repo "$REPO" --failed)
failed_log=""

# Try to get the log of the first failed check
failed_run_id=$(gh run list --repo "$REPO" --branch "issue-$ISSUE_NUMBER" \
  --status failure --json databaseId --jq '.[0].databaseId' 2>/dev/null || echo "")

if [ -n "$failed_run_id" ]; then
  failed_log=$(gh run view "$failed_run_id" --repo "$REPO" --log-failed 2>/dev/null | tail -100)
fi

gh pr edit "$PR_NUMBER" --repo "$REPO" --add-label "ai-working"

# Resume headless with CI failure context
result=$(claude -p \
  --permission-mode bypassPermissions \
  --max-turns 40 \
  --output-format json \
  --resume "$session_id" \
  "CI checks are failing on the PR. Fix them and push.

Failed checks:
$failed_checks

Failure log (last 100 lines):
$failed_log

Instructions:
- Diagnose the failure from the log output.
- Fix the code (not the tests, unless the tests are wrong).
- Run tests locally to verify before pushing.
- Do not disable or skip tests.
- If the failure is a flaky test or infrastructure issue you cannot fix, explain why and stop.")

response=$(echo "$result" | jq -r '.result')

if echo "$response" | grep -qiE "pushed|fixed|passing"; then
  echo "✓ PR #$PR_NUMBER CI fix pushed (attempt $((retries + 1)))."
  echo $((retries + 1)) > "$RETRY_FILE"
  gh pr edit "$PR_NUMBER" --repo "$REPO" --remove-label "ai-working"
  exit 0
fi

# Could not fix — mark blocked
echo $((retries + 1)) > "$RETRY_FILE"
gh pr comment "$PR_NUMBER" --repo "$REPO" --body "$(cat <<EOF
**Agent could not fix CI (attempt $((retries + 1))/$MAX_CI_RETRIES):**

$response
EOF
)"
gh pr edit "$PR_NUMBER" --repo "$REPO" --remove-label "ai-working" --add-label "ai-blocked"
```

### address-review.sh — Handle PR review comments (headless)

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="owner/repo"
PR_NUMBER="$1"
STATE_DIR="$HOME/.local/state/issue-agent"

# Find the linked issue number from PR body or branch name
ISSUE_NUMBER=$(gh pr view "$PR_NUMBER" --repo "$REPO" --json body \
  --jq '.body' | grep -oP '#\K[0-9]+' | head -1)

SESSION_FILE="$STATE_DIR/issue-$ISSUE_NUMBER.session"
[ -f "$SESSION_FILE" ] || { echo "No session for PR #$PR_NUMBER"; exit 1; }
session_id=$(cat "$SESSION_FILE")

# Get review comments
review_comments=$(gh pr view "$PR_NUMBER" --repo "$REPO" --json reviews,comments \
  --jq '
    [.reviews[] | select(.state=="CHANGES_REQUESTED" or .state=="COMMENTED")] |
    sort_by(.submittedAt) | last |
    if . then .body else empty end
  ')

# Get inline comments if any
inline_comments=$(gh api "repos/$REPO/pulls/$PR_NUMBER/comments" \
  --jq '[.[] | select(.in_reply_to_id == null)] | sort_by(.created_at) |
    .[-5:] | .[] | "File: \(.path):\(.line)\n\(.body)\n"')

gh pr edit "$PR_NUMBER" --repo "$REPO" --add-label "ai-working"

# Resume headless
result=$(claude -p \
  --permission-mode bypassPermissions \
  --max-turns 50 \
  --output-format json \
  --resume "$session_id" \
  "PR reviewer left feedback. Address it and push fixes.

Review comments:
$review_comments

Inline comments:
$inline_comments

Instructions:
- Address each piece of feedback.
- Push the fixes.
- If reviewer's request is unclear, state what you need to know and stop.")

response=$(echo "$result" | jq -r '.result')

if echo "$response" | grep -qiE "pushed|addressed|fixed|updated"; then
  echo "✓ PR #$PR_NUMBER review addressed."
  gh pr edit "$PR_NUMBER" --repo "$REPO" --remove-label "ai-working"
  exit 0
fi

# Stuck on review feedback
gh pr comment "$PR_NUMBER" --repo "$REPO" --body "$(cat <<EOF
**Agent needs clarification on review:**

$response

---
_Reply to unblock._
EOF
)"
gh pr edit "$PR_NUMBER" --repo "$REPO" --remove-label "ai-working" --add-label "ai-blocked"
```

### cleanup.sh — Post-merge cleanup

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="owner/repo"
ISSUE_NUMBER="$1"
STATE_DIR="$HOME/.local/state/issue-agent"

# Remove worktree
if git worktree list | grep -q "issue-$ISSUE_NUMBER"; then
  git worktree remove ".claude/worktrees/issue-$ISSUE_NUMBER" --force 2>/dev/null || true
fi

# Remove session file
rm -f "$STATE_DIR/issue-$ISSUE_NUMBER.session"

# Clean up labels
gh issue edit "$ISSUE_NUMBER" --repo "$REPO" --remove-label "ai-pr-open" --add-label "ai-done" 2>/dev/null || true

echo "✓ Issue #$ISSUE_NUMBER cleaned up."
```

### watcher.sh — The main event loop

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="owner/repo"
BOT_ACCOUNT="your-bot-account"  # or your own username if running as yourself
STATE_DIR="$HOME/.local/state/issue-agent"
mkdir -p "$STATE_DIR"

echo "Watching $REPO for issues and PR activity..."

while true; do

  # ── NEW ISSUES ──
  new_issues=$(gh issue list --repo "$REPO" --label "ai-ready" --json number --jq '.[].number')
  for issue in $new_issues; do
    echo "→ New issue #$issue"
    gh issue edit "$issue" --repo "$REPO" --remove-label "ai-ready" --add-label "ai-working"
    ./resolve-issue.sh "$issue" &
  done

  # ── BLOCKED ISSUES WITH HUMAN REPLIES ──
  blocked_issues=$(gh issue list --repo "$REPO" --label "ai-blocked" --json number --jq '.[].number')
  for issue in $blocked_issues; do
    [ -f "$STATE_DIR/issue-$issue.session" ] || continue
    last_author=$(gh issue view "$issue" --repo "$REPO" --json comments \
      --jq '.comments[-1].author.login')
    if [ "$last_author" != "$BOT_ACCOUNT" ]; then
      echo "→ Human replied on issue #$issue"
      ./resume-issue.sh "$issue" &
    fi
  done

  # ── PR REVIEW COMMENTS ──
  open_prs=$(gh pr list --repo "$REPO" --label "ai-pr-open" --json number --jq '.[].number')
  for pr in $open_prs; do
    # Get latest activity author
    last_author=$(gh api "repos/$REPO/pulls/$pr/reviews" \
      --jq 'sort_by(.submitted_at) | last | .user.login' 2>/dev/null || echo "")
    if [ -n "$last_author" ] && [ "$last_author" != "$BOT_ACCOUNT" ]; then
      echo "→ Review on PR #$pr"
      ./address-review.sh "$pr" &
    fi
  done

  # ── FAILING CI ON OPEN PRS ──
  for pr in $open_prs; do
    # Check if CI has finished and failed
    ci_status=$(gh pr checks "$pr" --repo "$REPO" --json state --jq '
      if [.[] | select(.state == "PENDING")] | length > 0 then "pending"
      elif [.[] | select(.state == "FAILURE")] | length > 0 then "failure"
      else "ok" end
    ' 2>/dev/null || echo "pending")

    if [ "$ci_status" = "failure" ]; then
      # Only trigger if not already working on it
      is_working=$(gh pr view "$pr" --repo "$REPO" --json labels \
        --jq '[.labels[].name] | any(. == "ai-working")')
      if [ "$is_working" != "true" ]; then
        echo "→ CI failing on PR #$pr"
        ./fix-ci.sh "$pr" &
      fi
    fi
  done

  # ── MERGED PRS → CLEANUP ──
  # Check recently closed issues that were ai-managed
  done_issues=$(gh issue list --repo "$REPO" --label "ai-pr-open" --state closed \
    --json number --jq '.[].number' 2>/dev/null)
  for issue in $done_issues; do
    echo "→ Cleaning up issue #$issue"
    ./cleanup.sh "$issue"
  done

  sleep 60
done
```

---

## Review Agent (separate system)

A second agent that reviews PRs. Runs independently — could be on a different machine. Communicates with the fix agent purely through GitHub (review comments, approvals, change requests).

### Interaction Model

```
Fix agent pushes code
  → review watcher detects new commits on ai-pr-open PRs
  → claude -p reviews the diff (headless)
  → Posts review: approve OR request changes
  → fix watcher detects review → resumes fix agent
  → fix agent pushes → review watcher re-reviews
  → loop until approved
```

### State

Each PR gets its own review session, stored separately from the fix session:

```
~/.local/state/review-agent/
  pr-42.session       # review agent's session ID
  pr-42.last-sha      # last commit SHA the reviewer saw (prevents re-reviewing same commit)
```

### review-pr.sh — Initial review of a new PR

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="owner/repo"
PR_NUMBER="$1"
STATE_DIR="$HOME/.local/state/review-agent"
mkdir -p "$STATE_DIR"

# Get PR metadata
pr_data=$(gh pr view "$PR_NUMBER" --repo "$REPO" --json title,body,headRefName,baseRefName,headRefOid)
pr_title=$(echo "$pr_data" | jq -r '.title')
pr_body=$(echo "$pr_data" | jq -r '.body')
head_sha=$(echo "$pr_data" | jq -r '.headRefOid')
base_branch=$(echo "$pr_data" | jq -r '.baseRefName')
head_branch=$(echo "$pr_data" | jq -r '.headRefName')

# Get the diff
pr_diff=$(gh pr diff "$PR_NUMBER" --repo "$REPO")

# Get list of changed files
changed_files=$(gh pr view "$PR_NUMBER" --repo "$REPO" --json files --jq '.files[].path')

REVIEW_PROMPT="You are a code reviewer. Review this PR thoroughly but constructively.

Focus on:
- Correctness bugs and logic errors
- Missing edge cases
- Security issues
- Whether tests adequately cover the changes
- Whether the change actually resolves what the PR claims

Do NOT nitpick:
- Style preferences already covered by linters
- Naming opinions unless genuinely confusing
- Minor formatting

If the PR is good, approve it. If it needs changes, request changes with specific, actionable feedback. Do not leave vague comments.

Output your review as a structured decision:
- Start with APPROVE or REQUEST_CHANGES on the first line
- Then your review body (markdown)"

result=$(claude -p \
  --permission-mode bypassPermissions \
  --max-turns 30 \
  --output-format json \
  --append-system-prompt "$REVIEW_PROMPT" \
  "Review PR #$PR_NUMBER: $pr_title

Description:
$pr_body

Base branch: $base_branch
Changed files:
$changed_files

Diff:
$pr_diff")

session_id=$(echo "$result" | jq -r '.session_id')
response=$(echo "$result" | jq -r '.result')

# Save session and SHA
echo "$session_id" > "$STATE_DIR/pr-$PR_NUMBER.session"
echo "$head_sha" > "$STATE_DIR/pr-$PR_NUMBER.last-sha"

# Parse the review decision
if echo "$response" | head -1 | grep -qi "APPROVE"; then
  review_event="APPROVE"
  review_body=$(echo "$response" | tail -n +2)
else
  review_event="REQUEST_CHANGES"
  review_body=$(echo "$response" | tail -n +2)
fi

# Submit the review via GitHub API
gh api "repos/$REPO/pulls/$PR_NUMBER/reviews" \
  --method POST \
  -f event="$review_event" \
  -f body="$review_body"

echo "✓ PR #$PR_NUMBER reviewed: $review_event"
```

### re-review-pr.sh — Re-review after new commits

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="owner/repo"
PR_NUMBER="$1"
STATE_DIR="$HOME/.local/state/review-agent"
SESSION_FILE="$STATE_DIR/pr-$PR_NUMBER.session"
ROUND_FILE="$STATE_DIR/pr-$PR_NUMBER.round"

[ -f "$SESSION_FILE" ] || { echo "No review session for PR #$PR_NUMBER"; exit 1; }
session_id=$(cat "$SESSION_FILE")

# Track review rounds
round=$(cat "$ROUND_FILE" 2>/dev/null || echo 1)
round=$((round + 1))
echo "$round" > "$ROUND_FILE"

# Get current head SHA
head_sha=$(gh pr view "$PR_NUMBER" --repo "$REPO" --json headRefOid --jq '.headRefOid')
last_sha=$(cat "$STATE_DIR/pr-$PR_NUMBER.last-sha" 2>/dev/null || echo "")

# Get diff since last review
if [ -n "$last_sha" ]; then
  new_diff=$(gh api "repos/$REPO/compare/$last_sha...$head_sha" \
    --jq '.files[] | "--- \(.filename)\n\(.patch // "")\n"' 2>/dev/null || gh pr diff "$PR_NUMBER" --repo "$REPO")
else
  new_diff=$(gh pr diff "$PR_NUMBER" --repo "$REPO")
fi

# Resume the review session with new context
result=$(claude -p \
  --permission-mode bypassPermissions \
  --max-turns 30 \
  --output-format json \
  --resume "$session_id" \
  "The author pushed new commits in response to your review.
This is review round $round of 5.

New changes since your last review ($last_sha → $head_sha):

$new_diff

Re-review. Consider whether your previous feedback was addressed.
Output: APPROVE, REQUEST_CHANGES, or ESCALATE (if round 5 and still not satisfied) on line 1, then your review body.")

response=$(echo "$result" | jq -r '.result')

# Update last-seen SHA
echo "$head_sha" > "$STATE_DIR/pr-$PR_NUMBER.last-sha"

# Parse the decision
decision=$(echo "$response" | head -1 | tr '[:lower:]' '[:upper:]')
review_body=$(echo "$response" | tail -n +2)

if echo "$decision" | grep -qi "ESCALATE"; then
  # Round limit reached — hand off to human
  gh api "repos/$REPO/pulls/$PR_NUMBER/reviews" \
    --method POST \
    -f event="COMMENT" \
    -f body="This PR has been through $round review rounds without resolution. Requesting human review.

$review_body"

  gh pr edit "$PR_NUMBER" --repo "$REPO" --add-label "needs-human-review" --remove-label "ai-pr-open"
  echo "⚠ PR #$PR_NUMBER escalated to human after $round rounds."
  exit 0
fi

if echo "$decision" | grep -qi "APPROVE"; then
  review_event="APPROVE"
else
  review_event="REQUEST_CHANGES"
fi

gh api "repos/$REPO/pulls/$PR_NUMBER/reviews" \
  --method POST \
  -f event="$review_event" \
  -f body="$review_body"

echo "✓ PR #$PR_NUMBER re-reviewed (round $round): $review_event"
```

### review-watcher.sh — Independent event loop for the review agent

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="owner/repo"
STATE_DIR="$HOME/.local/state/review-agent"
mkdir -p "$STATE_DIR"

echo "Review agent watching $REPO for PRs to review..."

while true; do

  # ── NEW PRS NEEDING REVIEW ──
  open_prs=$(gh pr list --repo "$REPO" --label "ai-pr-open" --json number --jq '.[].number')
  for pr in $open_prs; do
    session_file="$STATE_DIR/pr-$pr.session"

    if [ ! -f "$session_file" ]; then
      # Never reviewed — initial review
      echo "→ New PR #$pr needs review"
      ./review-pr.sh "$pr" &
    else
      # Already reviewed — check if new commits since last review
      current_sha=$(gh pr view "$pr" --repo "$REPO" --json headRefOid --jq '.headRefOid')
      last_sha=$(cat "$STATE_DIR/pr-$pr.last-sha" 2>/dev/null || echo "")

      if [ "$current_sha" != "$last_sha" ]; then
        echo "→ PR #$pr has new commits, re-reviewing"
        ./re-review-pr.sh "$pr" &
      fi
    fi
  done

  # ── CLEANUP MERGED/CLOSED PRS ──
  for session_file in "$STATE_DIR"/pr-*.session; do
    [ -f "$session_file" ] || continue
    pr_number=$(basename "$session_file" | grep -oP '\d+')
    pr_state=$(gh pr view "$pr_number" --repo "$REPO" --json state --jq '.state' 2>/dev/null || echo "UNKNOWN")
    if [ "$pr_state" = "MERGED" ] || [ "$pr_state" = "CLOSED" ]; then
      echo "→ PR #$pr_number is $pr_state, cleaning up review session"
      rm -f "$STATE_DIR/pr-$pr_number.session" "$STATE_DIR/pr-$pr_number.last-sha"
    fi
  done

  sleep 60
done
```

### Review Agent Specification

#### Review Dimensions (priority order)

| # | Dimension | What it checks |
|---|-----------|---------------|
| 1 | Correctness | Logic bugs, edge cases, race conditions, off-by-one errors |
| 2 | Tests match claims | Do the tests actually cover what the test plan comment says? |
| 3 | AC satisfied | Does evidence/ match the acceptance criteria in the PR body? |
| 4 | Security | OWASP top 10, injection, auth bypass, secrets in code |
| 5 | Architecture | Follows existing patterns, no unnecessary coupling, respects ARCHITECTURE.md |
| 6 | Proof of work valid | evidence/ shows real app state, not error pages or unrelated screens |
| 7 | Documentation | If complex change or non-obvious learning, was folder CLAUDE.md updated? |
| 8 | Logging | Are domain boundaries instrumented with structured logging? |

**Does NOT check:** Style, naming, formatting (linter's job).

#### Capabilities

- **Can read:** diff, evidence/ artifacts, issue context, CLAUDE.md, ARCHITECTURE.md
- **Can run:** tests and the app (if needed to verify behavior, not to recreate evidence)
- **Cannot:** modify code, push commits, merge PRs
- **Trusts:** evidence is not fabricated (no need to reproduce screenshots)

#### Constraints (prompt-only, no hooks)

- **5 review rounds max.** After 5 REQUEST_CHANGES without approval, post "needs human review" and label `needs-human-review`. Stop.
- **No re-raising resolved issues.** If you requested a change and it was addressed, acknowledge and move on.
- **Specific, actionable feedback only.** "This nil check should handle empty arrays" not "needs better error handling."
- **Approve when good enough.** Perfection is not the bar.
- **Reference prior reviews.** Session is resumed via `--resume`, so you have full history.

#### Context Sources

| Source | Purpose |
|--------|---------|
| `CLAUDE.md` | Project conventions, test commands |
| `ARCHITECTURE.md` | Module boundaries, patterns, dependency rules |
| PR diff | The actual code change |
| `evidence/` directory | Proof artifacts (screenshots, test output) |
| Issue + test plan comment | What was promised vs what was delivered |
| Surrounding files (on demand) | Read files referenced in the diff for context |

#### Review Agent Prompt (--append-system-prompt)

```bash
REVIEW_PROMPT="You are an autonomous code review agent. You review PRs for correctness, test quality, security, and architecture.

## Review Dimensions (check in this order)
1. Correctness — logic bugs, edge cases, race conditions
2. Tests match claims — do tests cover what the ## Test Plan comment promises?
3. AC satisfied — does evidence/ match ## Acceptance Criteria in PR body?
4. Security — OWASP top 10, injection, auth bypass, secrets in code
5. Architecture — follows patterns in ARCHITECTURE.md, no unnecessary coupling
6. Proof of work — evidence/ shows real working app state

## Rules
- Do NOT comment on style, naming, or formatting. The linter handles that.
- Be specific and actionable. Every REQUEST_CHANGES comment must say exactly what to fix.
- If you requested a change and it was addressed in new commits, acknowledge it.
- Do not re-raise resolved issues.
- Approve when the PR meets the bar. Perfection is not required.
- You may run the app or tests if something looks suspicious, but trust committed evidence.
- You have full history of prior reviews via session resume. Reference your past comments.

## Round Limit
- You are on review round \$ROUND_NUMBER of 5.
- If this is round 5 and you still cannot approve, output ESCALATE on line 1 instead of REQUEST_CHANGES.

## Output Format
First line must be exactly one of: APPROVE, REQUEST_CHANGES, or ESCALATE
Then your review body in markdown."
```

#### Escalation (round 5+)

When the reviewer outputs `ESCALATE`:
- Post a comment: "This PR has been through 5 review rounds without resolution. Requesting human review."
- Add label `needs-human-review`
- Remove label `ai-pr-open`
- Stop reviewing this PR

#### No Hooks Required

The review agent is read-mostly. Its constraints are entirely in the prompt:
- Review dimensions are prioritized in the prompt
- Round cap is tracked by the watcher (passes round number to the prompt)
- Escalation behavior is prompt-driven
- No tool restrictions needed beyond what `--permission-mode` already handles

---

## Architecture Gardener Agent

A daily scheduled `claude -p` that scans the codebase for tech debt, coupling, and improvement opportunities. Posts issues — does not fix anything itself.

### Cadence

Daily (for testing phase). Can be reduced to weekly once stable.

```bash
# cron or launchd — runs once daily
0 3 * * * cd /path/to/repo && ./gardener.sh
```

### Output: GitHub Issues with Severity Tiers

| Tier | Label | Examples | Triage |
|------|-------|---------|--------|
| `trivial` | `ai-suggestion` + `trivial` | Remove dead code, unused imports, fix typo in log message | Auto-promotes to `ai-ready` |
| `minor` | `ai-suggestion` + `minor` | Extract duplicated logic into helper, rename confusing module | Quick human glance to approve |
| `major` | `ai-suggestion` + `major` | Introduce new abstraction, restructure module boundaries, decouple subsystems | Needs human discussion before proceeding |

### Deduplication

Stateless — no session persistence. Before posting, the gardener searches existing open `ai-suggestion` issues to avoid duplicates:

```bash
existing=$(gh issue list --repo "$REPO" --label "ai-suggestion" --state open --json title --jq '.[].title')
# Skip if a similar suggestion already exists
```

### gardener.sh

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="owner/repo"
HARNESS_REPO="owner/harness"

result=$(claude -p \
  --permission-mode bypassPermissions \
  --max-turns 40 \
  --output-format json \
  --append-system-prompt "You are an architecture gardener agent. Your job is to scan the codebase and identify improvements.

## What to look for
1. Tech debt: dead code, unused dependencies, outdated patterns
2. Coupling: modules that depend on each other's internals
3. Missing abstractions: duplicated logic that should be extracted
4. Architectural drift: code that violates patterns documented in ARCHITECTURE.md
5. Testability: code that is hard to test due to tight coupling or global state

## What NOT to report
- Style issues (linter's job)
- Performance micro-optimizations without evidence of a problem
- Things that would require a major rewrite with unclear benefit

## Output format
For each suggestion, output a JSON object on its own line:
{\"tier\": \"trivial|minor|major\", \"title\": \"...\", \"body\": \"...\", \"files\": [\"path/to/relevant/file.ts\"]}

## Deduplication
These issues already exist (do not re-suggest):
\$EXISTING_ISSUES

## Context
Read ARCHITECTURE.md and CLAUDE.md for documented patterns and conventions." \
  "Scan the codebase for architectural improvements. Check for tech debt, coupling, missing abstractions, and drift from documented patterns.")

response=$(echo "$result" | jq -r '.result')

# Parse each suggestion and post as an issue
echo "$response" | grep '^{' | while IFS= read -r suggestion; do
  tier=$(echo "$suggestion" | jq -r '.tier')
  title=$(echo "$suggestion" | jq -r '.title')
  body=$(echo "$suggestion" | jq -r '.body')
  files=$(echo "$suggestion" | jq -r '.files | join(", ")')

  # Check for duplicate
  if gh issue list --repo "$REPO" --label "ai-suggestion" --state open --json title \
    --jq '.[].title' | grep -qiF "$title"; then
    echo "  ⊘ Skipping duplicate: $title"
    continue
  fi

  gh issue create --repo "$REPO" \
    --title "$title" \
    --label "ai-suggestion,$tier" \
    --body "$(cat <<EOF
$body

**Affected files:** $files
**Tier:** $tier

---
_Posted by architecture gardener agent._
EOF
)"

  echo "  ✓ Posted: [$tier] $title"
done

# Auto-promote trivial suggestions
trivial_issues=$(gh issue list --repo "$REPO" --label "ai-suggestion,trivial" --state open --json number --jq '.[].number')
for issue in $trivial_issues; do
  gh issue edit "$issue" --repo "$REPO" --add-label "ai-ready" --remove-label "ai-suggestion"
  echo "  ↑ Auto-promoted trivial issue #$issue to ai-ready"
done
```

### Harness Issue Reporting

Any agent (fix, review, gardener) can report problems with the harness itself. They post issues directly to the harness repo:

```bash
# Any agent can do this when it hits a harness problem:
gh issue create --repo "$HARNESS_REPO" \
  --title "Hook can't parse compound bash commands (git add && git commit)" \
  --label "bug" \
  --body "$(cat <<EOF
## Problem
The check-commit.sh hook fails when the agent runs \`git add -A && git commit -m "..."\`
because the match pattern only looks at the first command in a compound expression.

## Context
- Agent: fix-agent
- Issue being worked: #42
- Session: $session_id

## Suggested fix
Hooks need tree-sitter or AST parsing to decompose compound bash commands
before matching against patterns.
EOF
)"
```

This is baked into the agent prompt:

```
If you encounter a problem with the harness (hooks blocking valid actions,
prompts that cause confusion, missing capabilities), report it:
gh issue create --repo <HARNESS_REPO> --title "..." --label "bug" --body "..."
Then continue with a workaround if possible.
```

---

## Master Agent (On-Demand Query)

Not a persistent process — an on-demand `claude -p` you invoke to ask about the state of work.

### What It Reads

| Source | What it provides |
|--------|-----------------|
| `~/.claude/projects/<hash>/*.jsonl` | Full session transcripts (token usage, tool calls, timing) |
| GitHub issues/PRs (`gh` CLI) | What's open, closed, merged, blocked |
| `~/.local/state/issue-agent/` | Active fix sessions |
| `~/.local/state/review-agent/` | Active review sessions |

### Usage

```bash
# "What happened today?"
claude -p \
  --permission-mode bypassPermissions \
  --max-turns 20 \
  "Summarize agent activity in the last 24 hours.

Check:
- GitHub issues labeled ai-done that were closed today
- Open PRs with ai-pr-open label and their review status
- Issues currently ai-blocked
- Any harness issues raised today

Report: issues resolved, PRs opened/merged, blockers, total estimated token spend."

# "What's stuck?"
claude -p \
  --permission-mode bypassPermissions \
  --max-turns 10 \
  "List all currently blocked work:
- Issues with ai-blocked label
- PRs with needs-human-review label
- Any CI failures that have exhausted retries

For each, summarize what's needed to unblock."
```

### No Persistent State

The master agent reads everything fresh each invocation. It has no session to resume — each query is independent. The state IS GitHub + the log files. There's nothing to manage.

---

## System Architecture (Complete)

```
┌──────────────────────────────────────────────────────────────────────┐
│  ORCHESTRATION (stateless scripts, no Claude sessions)               │
│                                                                      │
│  watcher.sh          — fix agent event loop (issues, CI, reviews)    │
│  review-watcher.sh   — review agent event loop (new PRs, commits)    │
│  gardener.sh         — daily cron (codebase scan → post suggestions) │
│                                                                      │
└───────────────────────────────┬──────────────────────────────────────┘
                                │ spawns headless sessions
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│  AGENTS (claude -p, always headless, communicate via GitHub)         │
│                                                                      │
│  Fix Agent        — resolves issues, opens PRs, fixes CI/review      │
│  Review Agent     — reviews PRs, approves or requests changes        │
│  Gardener Agent   — scans codebase, posts improvement suggestions    │
│  Master Agent     — on-demand query interface (no orchestration)     │
│                                                                      │
└───────────────────────────────┬──────────────────────────────────────┘
                                │ read/write
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│  STATE (all external, no agent owns state)                           │
│                                                                      │
│  GitHub (issues, PRs, labels, reviews, comments)                     │
│  ~/.local/state/issue-agent/   (session IDs, retry counters)         │
│  ~/.local/state/review-agent/  (session IDs, SHAs, round counters)   │
│  ~/.claude/projects/*/         (session JSONL transcripts)           │
│  Harness repo                  (issues reported by agents)           │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

### Label Taxonomy

| Label | Meaning | Who sets it |
|-------|---------|-------------|
| `ai-ready` | Fix agent should pick this up | Human or auto-promoted from `trivial` |
| `ai-working` | Fix agent is currently running | Watcher |
| `ai-blocked` | Agent needs human input | Agent (via watcher) |
| `ai-pr-open` | PR created, awaiting review | Watcher |
| `ai-done` | Issue fully resolved and merged | Watcher (cleanup) |
| `ai-suggestion` | Gardener suggestion, needs triage | Gardener |
| `trivial` / `minor` / `major` | Suggestion severity | Gardener |
| `needs-human-review` | Review agent escalated after 5 rounds | Review watcher |
| `harness` | Issue with the harness itself (on harness repo) | Any agent |
| `P1` / `P2` / `P3` | Priority (P1 dispatched first) | Human |
| `area/*` | Module area (for concurrency locking) | Human or agent |
| `flaky-test-exemption` | PR exempted from known-flaky test failures | Fix agent |

## Operational Safeguards

### 1. Concurrency Conflict Prevention

Two issues touching the same files → merge conflicts when the second PR merges. The watcher prevents this with area-based locking:

```bash
# In watcher.sh, before dispatching:
LOCK_DIR="$STATE_DIR/locks"
mkdir -p "$LOCK_DIR"

# Extract affected areas from issue labels (e.g., area/auth, area/payments)
areas=$(gh issue view "$issue" --repo "$REPO" --json labels \
  --jq '[.labels[].name | select(startswith("area/"))] | join(",")')

# Check if any area is currently locked
locked=false
for area in $(echo "$areas" | tr ',' '\n'); do
  if [ -f "$LOCK_DIR/$area.lock" ]; then
    echo "  ⊘ Issue #$issue blocked: $area is locked by issue #$(cat "$LOCK_DIR/$area.lock")"
    locked=true
    break
  fi
done

if [ "$locked" = "false" ]; then
  # Lock areas and dispatch
  for area in $(echo "$areas" | tr ',' '\n'); do
    echo "$issue" > "$LOCK_DIR/$area.lock"
  done
  ./resolve-issue.sh "$issue" &
fi
```

Cleanup releases locks when the PR merges:
```bash
# In cleanup.sh:
for lock in "$LOCK_DIR"/*.lock; do
  [ -f "$lock" ] || continue
  if [ "$(cat "$lock")" = "$ISSUE_NUMBER" ]; then
    rm "$lock"
  fi
done
```

If issues don't have `area/` labels, the watcher can fall back to sequential dispatch (one fix agent at a time).

### 2. Session Context Bloat

After many `--resume` cycles, the session grows large (expensive, slower, risk of hitting context limits). Use `--fork-session` to start fresh with a summary:

```bash
# In resume-issue.sh / address-review.sh, after N resumes:
RESUME_COUNT_FILE="$STATE_DIR/issue-$ISSUE_NUMBER.resumes"
resume_count=$(cat "$RESUME_COUNT_FILE" 2>/dev/null || echo 0)
resume_count=$((resume_count + 1))
echo "$resume_count" > "$RESUME_COUNT_FILE"

if [ "$resume_count" -ge 5 ]; then
  # Fork: start a fresh session with summary context instead of full history
  result=$(claude -p \
    --permission-mode bypassPermissions \
    --max-turns 50 \
    --output-format json \
    --worktree "issue-$ISSUE_NUMBER" \
    "You are continuing work on issue #$ISSUE_NUMBER. Here is the current state:

Branch: $(git -C ".claude/worktrees/issue-$ISSUE_NUMBER" branch --show-current)
Files changed: $(git -C ".claude/worktrees/issue-$ISSUE_NUMBER" diff --stat main)

Latest context:
$human_reply

Continue resolving the issue. Open a PR when done.")

  # Save new session ID
  new_session_id=$(echo "$result" | jq -r '.session_id')
  echo "$new_session_id" > "$SESSION_FILE"
  echo 0 > "$RESUME_COUNT_FILE"
fi
```

### 3. Flaky Test Handling

Before spending retries on a CI failure, check if the failing test also fails on main (flaky):

```bash
# In fix-ci.sh, before attempting a fix:

# Extract failing test names from the log
failing_tests=$(echo "$failed_log" | grep -oP '(?<=FAIL )\S+' || echo "")

# Check if any of these fail on main too
for test in $failing_tests; do
  if grep -q "$test" .flaky-tests 2>/dev/null; then
    echo "  ⊘ Known flaky test: $test (skipping)"
    continue
  fi

  # Check recent main branch CI for this test
  main_failures=$(gh run list --repo "$REPO" --branch main --status failure \
    --json name --jq '.[0:5] | .[].name' 2>/dev/null || echo "")

  if echo "$main_failures" | grep -q "$test"; then
    echo "  ⚠ Flaky test detected: $test (also fails on main)"
    # Retry once
    # If still fails → add to .flaky-tests and open separate issue
    echo "$test" >> .flaky-tests
    git add .flaky-tests && git commit -m "chore: mark $test as flaky"
    gh issue create --repo "$REPO" \
      --title "Fix flaky test: $test" \
      --label "fix,ai-ready,trivial" \
      --body "This test intermittently fails on main. Detected by fix-ci.sh while working on PR #$PR_NUMBER."
  fi
done
```

Escalation:
1. Test fails → **retry once**
2. Still fails + also fails on main → **add to `.flaky-tests`**, open a separate `fix` issue, exempt this PR
3. Fails but does NOT fail on main → **it's a real failure**, fix agent must address it

### 4. Bootstrap Script (New Repo Setup)

First-time setup when the plugin is dropped into a new repo:

```bash
#!/usr/bin/env bash
set -euo pipefail
# bootstrap.sh — run once when adding the plugin to a new repo

REPO="owner/repo"

echo "Bootstrapping agent harness for $REPO..."

# 1. Create labels
for label in ai-ready ai-working ai-blocked ai-pr-open ai-done ai-suggestion \
  trivial minor major needs-human-review; do
  gh label create "$label" --repo "$REPO" 2>/dev/null || true
done

# 2. Generate ARCHITECTURE.md if missing
if [ ! -f ARCHITECTURE.md ]; then
  claude -p \
    --permission-mode bypassPermissions \
    --max-turns 30 \
    "Analyze this codebase and generate an ARCHITECTURE.md.

Include:
- High-level module map (what each top-level directory owns)
- Key dependencies between modules
- Data flow (how a request moves through the system)
- Patterns in use (DI, event-driven, repository pattern, etc.)
- Module boundaries that should not be crossed

Output as markdown. Be concise — this is a reference doc, not a tutorial." \
    > ARCHITECTURE.md
  echo "  ✓ Generated ARCHITECTURE.md"
fi

# 3. Seed folder CLAUDE.md files for top-level directories
for dir in src/*/; do
  [ -d "$dir" ] || continue
  if [ ! -f "$dir/CLAUDE.md" ]; then
    claude -p \
      --permission-mode bypassPermissions \
      --max-turns 10 \
      "Look at the files in $dir and generate a CLAUDE.md for this directory.

Include:
- Responsibilities (what this module owns)
- Key files and their roles
- Common gotchas (anything non-obvious from the code)
- How to add new things following existing patterns

Be concise. Only document what isn't obvious from reading the filenames." \
      > "$dir/CLAUDE.md"
    echo "  ✓ Generated $dir/CLAUDE.md"
  fi
done

# 4. Create .flaky-tests if missing
touch .flaky-tests

# 5. Add to .gitignore
echo -e "\n# Agent harness\n.claude/worktrees/" >> .gitignore

echo "✓ Bootstrap complete."
```

### 5. Priority Ordering

The watcher dispatches `ai-ready` issues in priority order:

```bash
# In watcher.sh, replace the simple issue list:
new_issues=$(gh issue list --repo "$REPO" --label "ai-ready" --json number,labels \
  --jq '
    [.[] | {
      number,
      priority: (
        if (.labels | map(.name) | any(. == "P1")) then 1
        elif (.labels | map(.name) | any(. == "P2")) then 2
        elif (.labels | map(.name) | any(. == "P3")) then 3
        else 4 end
      )
    }] | sort_by(.priority) | .[].number
  ')
```

Issues without a priority label default to lowest priority (P4). Add `P1`, `P2`, `P3` to the label taxonomy.

## Notification (how you know on your phone)

The agent posts GitHub comments when stuck. You get notified via:
- **GitHub mobile app** → push notifications on issue/PR comments
- **Slack GitHub integration** → route `ai-blocked` label to a channel
- **Email** → default GitHub notification

No Claude-specific notification needed. GitHub IS the notification layer.

## One-Time Setup

1. Authenticate `gh` CLI: `gh auth login`
2. Run `./bootstrap.sh` (creates labels, generates ARCHITECTURE.md, seeds folder docs)
3. Set up GitHub mobile notifications
4. Set `BOT_ACCOUNT` in `watcher.sh` to your GitHub username
5. Configure `REPO` and `HARNESS_REPO` in all scripts

## CLI Reference

| Flag | Purpose |
|------|---------|
| `--permission-mode bypassPermissions` | No permission prompts |
| `--worktree <name>` | Isolate in a git worktree |
| `--max-turns <n>` | Safety cap on turns |
| `--max-budget-usd <n>` | Cost cap |
| `--output-format json` | Structured output with session_id |
| `--resume <session_id>` | Resume previous session |

## Properties

- **Always headless.** No interactive sessions, no Remote Control.
- **Four agent roles.** Fix, review, gardener, master — each independent, each talks through GitHub.
- **Stateless orchestration.** Watcher scripts have no intelligence. They poll, detect events, and spawn `claude -p`.
- **Any human can substitute.** A human reviewer replaces the review agent. A human fixing code replaces the fix agent. The watcher doesn't care.
- **Separate machines.** Each watcher is a standalone process. Run them on different machines, same machine, or mix with GHA.
- **Self-improving.** Agents report harness problems to the harness repo. Humans (or eventually agents) fix the harness.
- **Full context preserved.** `--resume` carries the entire conversation per agent.
- **Lifecycle ends at merge.** PR merged = both agents clean up their sessions.
- **Single primitive.** Detect event → `claude -p` → check if done → loop.
- **Plugin-shaped.** The whole thing is a `.claude/` directory you drop into any repo.

## State Layout

```
# Fix agent
~/.local/state/issue-agent/
  issue-42.session       # fix agent session ID
  issue-42.ci-retries    # CI fix attempt counter

# Review agent
~/.local/state/review-agent/
  pr-57.session          # review agent session ID
  pr-57.last-sha         # last reviewed commit SHA
  pr-57.round            # review round counter

# Gardener agent — no persistent state (stateless)

# Master agent — no persistent state (reads fresh each time)

# Session transcripts (automatic, managed by Claude Code)
~/.claude/projects/<hash>/
  <session-id>.jsonl     # full transcript per session
```
