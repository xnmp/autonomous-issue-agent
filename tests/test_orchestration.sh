#!/usr/bin/env bash
# Orchestration tests: exercise the fix + review scripts end-to-end with FAKE
# `gh` and `claude` on PATH (no network, no spend). Asserts that decisions come
# from ground truth (GitHub state) + envelope, never prose.
set -uo pipefail

# Every case creates its own deployment envelope; external deployment controls
# would otherwise change authentication paths or redirect sandbox worktrees.
for gate_knob in \
  TEST_CMD LINT_CMD TYPECHECK_CMD ALLOW_UNTESTED RED_MUST_FAIL \
  VERIFY_RED_AT_COMMIT BASE_BRANCH GATE_TEST_TIMEOUT_S GATE_GH_RETRY_SLEEP \
  GH_APP_ID GH_APP_PRIVATE_KEY GH_APP_INSTALLATION_ID TARGET_DIR AGENT_ENV; do
  unset "$gate_knob"
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FAKES="$ROOT/tests/fakes"
PASS=0; FAIL=0
ok() { PASS=$((PASS+1)); printf '  \033[32mok\033[0m   %s\n' "$1"; }
no() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; [ -n "${2:-}" ] && printf '       %s\n' "$2"; }

# Each test runs in a fresh sandbox: a git repo with an `origin` remote + dev
# branch, fakes on PATH, isolated STATE_DIR + fake state files.
SANDBOX_ROOT="$(mktemp -d)"
trap 'rm -rf "$SANDBOX_ROOT"' EXIT

new_sandbox() {
  local d; d="$(mktemp -d "$SANDBOX_ROOT/sbx.XXXXXX")"
  (
    # bare origin
    git init -q --bare "$d/origin.git"
    # working clone with a dev branch + base commit
    git clone -q "$d/origin.git" "$d/work" 2>/dev/null
    cd "$d/work"
    git config user.email t@t; git config user.name t; git config commit.gpgsign false
    git checkout -q -b dev
    echo seed > seed.txt; git add seed.txt
    git -c core.hooksPath=/dev/null commit -qm "chore: seed"
    git push -q origin dev 2>/dev/null
  )
  echo "$d"
}

# Build an env file + exported config for a sandbox.
mk_env() {  # mk_env <sandbox> <extra KEY=VAL ...>
  local d="$1"; shift
  export ISSUE_AGENT_FAKE_STATE="$d/gh.json"
  export ISSUE_AGENT_FAKE_CLAUDE="$d/claude.json"
  export STATE_DIR="$d/state"
  export AGENT_ENV="$d/agent.env"
  export PATH="$FAKES:$PATH"
  mkdir -p "$STATE_DIR"
  {
    echo "REPO=o/r"
    echo "BASE_BRANCH=dev"
    echo "FIX_BOT=fix-bot"
    echo "REVIEW_BOT=review-bot"
    echo "STEWARD_BOT=steward-bot"
    echo "IDENTITY_MODE=three"
    echo "STATE_DIR=$d/state"
    echo "REVIEW_STATE_DIR=$d/rstate"
    echo "WORKTREE_ROOT=$d/work/.wt"
    echo "PERMISSION_MODE=auto"
    echo "AUTO_MERGE=1"
    # FIX_MODEL set → route_model short-circuits (no extra router claude call), so
    # the scripted fake-claude queue stays one-item-per-invocation. A test can
    # override with a later 'FIX_MODEL=' (empty) to exercise the router path.
    echo "FIX_MODEL=sonnet"
    for kv in "$@"; do echo "$kv"; done
  } > "$AGENT_ENV"
}

seed_state() { printf '%s' "$1" > "$ISSUE_AGENT_FAKE_STATE"; }
seed_claude() { printf '%s' "$1" > "$ISSUE_AGENT_FAKE_CLAUDE"; }
ghq() { "$FAKES/gh" "$@"; }   # query fake state directly in assertions

# ──────────────────────────────────────────────────────────────────────────────
echo "== resolve-issue: opens PR → ai-pr-open =="
d=$(new_sandbox); mk_env "$d"
seed_state '{"issues":{"5":{"number":5,"title":"Null token","body":"crashes","labels":["ai-working"],"comments":[],"state":"open"}},"prs":{},"labels":[],"runs":{}}'
seed_claude '[{"envelope":{"is_error":false,"session_id":"s5","total_cost_usd":0.2,"structured_output":{"status":"pr_opened","summary":"opened"}},"effect":"open_pr","for_issue":5,"branch":"fix/5-null-token"}]'
( cd "$d/work" && FAKE_GH_LOGIN=fix-bot bash "$ROOT/agent/resolve-issue.sh" 5 ) >/dev/null 2>&1
ilabels=$(ghq issue view 5 --repo o/r --json labels --jq '[.labels[].name]|join(",")')
plabels=$(ghq pr view 100 --repo o/r --json labels --jq '[.labels[].name]|join(",")')
[[ "$ilabels" == *ai-pr-open* && "$ilabels" != *ai-working* ]] && ok "issue 5 → ai-pr-open" || no "issue 5 → ai-pr-open" "labels=$ilabels"
[[ "$plabels" == *ai-pr-open* ]] && ok "PR 100 labeled ai-pr-open" || no "PR 100 labeled ai-pr-open" "plabels=$plabels"
[ -s "$STATE_DIR/issue-5.session" ] && ok "session saved" || no "session saved"

# Bug 1 regression: agent opens the PR on a feat/ branch even though resolve-issue.sh
# defaulted the worktree to fix/. Discovery-by-issue-number must still find it; the
# OLD code (pr_for_branch "fix/10-…") returned "" and looped forever on ai-ready.
echo "== resolve-issue: PR on feat/ branch (mismatch) → issue AND PR get ai-pr-open =="
d=$(new_sandbox); mk_env "$d"
seed_state '{"issues":{"10":{"number":10,"title":"Add widget","body":"new feature","labels":["ai-working"],"comments":[],"state":"open"}},"prs":{},"labels":[],"runs":{}}'
seed_claude '[{"envelope":{"is_error":false,"session_id":"s10","total_cost_usd":0.3,"structured_output":{"status":"pr_opened","summary":"opened"}},"effect":"open_pr","for_issue":10,"branch":"feat/10-add-widget"}]'
( cd "$d/work" && FAKE_GH_LOGIN=fix-bot bash "$ROOT/agent/resolve-issue.sh" 10 ) >/dev/null 2>&1
ilabels=$(ghq issue view 10 --repo o/r --json labels --jq '[.labels[].name]|join(",")')
plabels=$(ghq pr view 100 --repo o/r --json labels --jq '[.labels[].name]|join(",")')
[[ "$ilabels" == *ai-pr-open* && "$ilabels" != *ai-working* ]] && ok "feat/ mismatch: issue 10 → ai-pr-open" || no "feat/ mismatch: issue 10 → ai-pr-open" "ilabels=$ilabels"
[[ "$plabels" == *ai-pr-open* ]] && ok "feat/ mismatch: PR gets ai-pr-open" || no "feat/ mismatch: PR gets ai-pr-open" "plabels=$plabels"

# Change C: chore/ branch is accepted and resolved the same way (no required test).
echo "== resolve-issue: PR on chore/ branch → issue AND PR get ai-pr-open =="
d=$(new_sandbox); mk_env "$d"
seed_state '{"issues":{"12":{"number":12,"title":"Update deps","body":"bump lodash","labels":["ai-working"],"comments":[],"state":"open"}},"prs":{},"labels":[],"runs":{}}'
seed_claude '[{"envelope":{"is_error":false,"session_id":"s12","total_cost_usd":0.1,"structured_output":{"status":"pr_opened","summary":"done"}},"effect":"open_pr","for_issue":12,"branch":"chore/12-update-deps"}]'
( cd "$d/work" && FAKE_GH_LOGIN=fix-bot bash "$ROOT/agent/resolve-issue.sh" 12 ) >/dev/null 2>&1
ilabels=$(ghq issue view 12 --repo o/r --json labels --jq '[.labels[].name]|join(",")')
plabels=$(ghq pr view 100 --repo o/r --json labels --jq '[.labels[].name]|join(",")')
[[ "$ilabels" == *ai-pr-open* ]] && ok "chore/ branch: issue 12 → ai-pr-open" || no "chore/ branch: issue 12 → ai-pr-open" "ilabels=$ilabels"
[[ "$plabels" == *ai-pr-open* ]] && ok "chore/ branch: PR gets ai-pr-open" || no "chore/ branch: PR gets ai-pr-open" "plabels=$plabels"

# Difficulty router path: FIX_MODEL="" forces route_model to make a real (fake)
# claude call. The queue serves the router classification FIRST, then the fix
# result. The extra call must not derail resolution → still reaches ai-pr-open.
echo "== resolve-issue: router classifies then resolves (two-call queue) =="
d=$(new_sandbox); mk_env "$d" "FIX_MODEL="
seed_state '{"issues":{"13":{"number":13,"title":"Tidy readme","body":"typo","labels":["ai-working"],"comments":[],"state":"open"}},"prs":{},"labels":[],"runs":{}}'
seed_claude '[{"envelope":{"is_error":false,"session_id":"r13","total_cost_usd":0.01,"structured_output":{"tier":"trivial"}},"effect":null},{"envelope":{"is_error":false,"session_id":"s13","total_cost_usd":0.1,"structured_output":{"status":"pr_opened","summary":"done"}},"effect":"open_pr","for_issue":13,"branch":"fix/13-tidy-readme"}]'
( cd "$d/work" && FAKE_GH_LOGIN=fix-bot bash "$ROOT/agent/resolve-issue.sh" 13 ) >/dev/null 2>&1
ilabels=$(ghq issue view 13 --repo o/r --json labels --jq '[.labels[].name]|join(",")')
plabels=$(ghq pr view 100 --repo o/r --json labels --jq '[.labels[].name]|join(",")')
[[ "$ilabels" == *ai-pr-open* && "$ilabels" != *ai-working* ]] && ok "router path: issue 13 → ai-pr-open" || no "router path: issue 13 → ai-pr-open" "ilabels=$ilabels"
[[ "$plabels" == *ai-pr-open* ]] && ok "router path: PR gets ai-pr-open" || no "router path: PR gets ai-pr-open" "plabels=$plabels"

echo "== resolve-issue: blocked → ai-blocked + comment =="
d=$(new_sandbox); mk_env "$d"
seed_state '{"issues":{"6":{"number":6,"title":"Vague","body":"do something","labels":["ai-working"],"comments":[],"state":"open"}},"prs":{},"labels":[],"runs":{}}'
seed_claude '[{"envelope":{"is_error":false,"session_id":"s6","total_cost_usd":0.1,"structured_output":{"status":"blocked","summary":"need detail","needs":"which endpoint?"}},"effect":null}]'
( cd "$d/work" && FAKE_GH_LOGIN=fix-bot bash "$ROOT/agent/resolve-issue.sh" 6 ) >/dev/null 2>&1
labels=$(ghq issue view 6 --repo o/r --json labels --jq '[.labels[].name]|join(",")')
ncom=$(ghq issue view 6 --repo o/r --json comments --jq '.comments|length')
[[ "$labels" == *ai-blocked* ]] && ok "issue 6 → ai-blocked" || no "issue 6 → ai-blocked" "labels=$labels"
[ "$ncom" -ge 1 ] && ok "blocking question posted" || no "blocking question posted" "ncom=$ncom"

echo "== resolve-issue: claude error → reset to ai-ready (never strand ai-working) =="
d=$(new_sandbox); mk_env "$d"
seed_state '{"issues":{"7":{"number":7,"title":"Boom","body":"x","labels":["ai-working"],"comments":[],"state":"open"}},"prs":{},"labels":[],"runs":{}}'
seed_claude '[{"envelope":{"is_error":true,"subtype":"error_max_turns","session_id":"s7"},"effect":null,"rc":1}]'
( cd "$d/work" && FAKE_GH_LOGIN=fix-bot bash "$ROOT/agent/resolve-issue.sh" 7 ) >/dev/null 2>&1
labels=$(ghq issue view 7 --repo o/r --json labels --jq '[.labels[].name]|join(",")')
[[ "$labels" == *ai-ready* && "$labels" != *ai-working* ]] && ok "error → ai-ready (unstranded)" || no "error → ai-ready" "labels=$labels"

echo "== resume-issue: human reply → PR opens on feat/ branch → issue AND PR get ai-pr-open =="
d=$(new_sandbox); mk_env "$d"
seed_state '{"issues":{"6":{"number":6,"title":"Vague","body":"x","labels":["ai-blocked"],"comments":[{"author":{"login":"alice"},"body":"use /v2/login"}],"state":"open"}},"prs":{},"labels":[],"runs":{}}'
echo "s6" > "$STATE_DIR/issue-6.session"
( cd "$d/work" && git checkout -q -b feat/6-vague 2>/dev/null; git checkout -q dev 2>/dev/null ) 2>/dev/null
seed_claude '[{"envelope":{"is_error":false,"session_id":"s6","total_cost_usd":0.2,"structured_output":{"status":"pr_opened","summary":"done"}},"effect":"open_pr","for_issue":6,"branch":"feat/6-vague"}]'
( cd "$d/work" && FAKE_GH_LOGIN=fix-bot bash "$ROOT/agent/resume-issue.sh" 6 ) >/dev/null 2>&1
ilabels=$(ghq issue view 6 --repo o/r --json labels --jq '[.labels[].name]|join(",")')
plabels=$(ghq pr view 100 --repo o/r --json labels --jq '[.labels[].name]|join(",")')
[[ "$ilabels" == *ai-pr-open* ]] && ok "resume feat/: issue → ai-pr-open" || no "resume feat/: issue → ai-pr-open" "ilabels=$ilabels"
[[ "$plabels" == *ai-pr-open* ]] && ok "resume feat/: PR gets ai-pr-open" || no "resume feat/: PR gets ai-pr-open" "plabels=$plabels"

echo "== review-pr: APPROVE recorded as review by review-bot =="
d=$(new_sandbox); mk_env "$d"
seed_state '{"issues":{},"prs":{"100":{"number":100,"state":"OPEN","title":"Fix #5","body":"## Acceptance Criteria\n- ok\n\nFixes #5","headRefName":"fix/5-x","headRefOid":"sha1","issue":5,"labels":["ai-pr-open"],"reviews":[],"checks":[]}},"labels":[],"runs":{}}'
seed_claude '[{"envelope":{"is_error":false,"session_id":"r100","total_cost_usd":0.05,"structured_output":{"decision":"APPROVE","body":"LGTM"}},"effect":null}]'
( cd "$d/work" && FAKE_GH_LOGIN=review-bot bash "$ROOT/review/review-pr.sh" 100 ) >/dev/null 2>&1
rev=$(ghq pr view 100 --repo o/r --json reviews --jq '.reviews[-1].state + " " + .reviews[-1].user.login')
[[ "$rev" == "APPROVED review-bot" ]] && ok "review-pr APPROVE by review-bot" || no "review-pr APPROVE" "rev=$rev"

echo "== watcher.maybe_automerge: approved+green → auto-merge enabled =="
d=$(new_sandbox); mk_env "$d"
seed_state '{"issues":{},"prs":{"100":{"number":100,"state":"OPEN","title":"t","body":"b","headRefName":"fix/5-x","headRefOid":"s","issue":5,"labels":["ai-pr-open"],"reviews":[{"state":"APPROVED","user":{"login":"review-bot"},"submitted_at":"2026-01-01T00:00:01Z","body":"ok"}],"checks":[{"name":"ci","bucket":"pass","state":"SUCCESS"}]}},"labels":[],"runs":{}}'
# call the watcher's maybe_automerge in isolation by sourcing the real lib
( cd "$d/work"
  source "$ROOT/agent/lib.sh"
  RUN_LOCK="$STATE_DIR/locks/fixer.run"
  source "$ROOT/agent/watcher-lib.sh"
  FAKE_GH_LOGIN=fix-bot maybe_automerge 100
) >/dev/null 2>&1
am=$(ghq pr view 100 --repo o/r --json labels --jq '.number')   # touch state
automerge=$(python3 -c "import json;print(json.load(open('$ISSUE_AGENT_FAKE_STATE'))['prs']['100'].get('automerge',False))")
[ "$automerge" = "True" ] && ok "auto-merge enabled on approved+green" || no "auto-merge enabled" "automerge=$automerge"

echo "== watcher.maybe_automerge: NOT approved → no merge =="
d=$(new_sandbox); mk_env "$d"
seed_state '{"issues":{},"prs":{"101":{"number":101,"state":"OPEN","headRefName":"fix/9-x","issue":9,"labels":["ai-pr-open"],"reviews":[{"state":"CHANGES_REQUESTED","user":{"login":"review-bot"},"submitted_at":"2026-01-01T00:00:01Z","body":"no"}],"checks":[{"name":"ci","bucket":"pass"}]}},"labels":[],"runs":{}}'
( cd "$d/work"
  source "$ROOT/agent/lib.sh"
  RUN_LOCK="$STATE_DIR/locks/fixer.run"
  source "$ROOT/agent/watcher-lib.sh"
  FAKE_GH_LOGIN=fix-bot maybe_automerge 101
) >/dev/null 2>&1
automerge=$(python3 -c "import json;print(json.load(open('$ISSUE_AGENT_FAKE_STATE'))['prs']['101'].get('automerge',False))")
[ "$automerge" = "False" ] && ok "no auto-merge when changes requested" || no "no auto-merge when changes requested" "automerge=$automerge"

echo "== solo mode: maybe_automerge skips (no self-approval) =="
d=$(new_sandbox); mk_env "$d" "IDENTITY_MODE=solo"
seed_state '{"issues":{},"prs":{"102":{"number":102,"state":"OPEN","headRefName":"fix/9-x","issue":9,"labels":["ai-pr-open"],"reviews":[{"state":"APPROVED","user":{"login":"human"},"submitted_at":"2026-01-01T00:00:01Z","body":"ok"}],"checks":[{"name":"ci","bucket":"pass"}]}},"labels":[],"runs":{}}'
( cd "$d/work"
  source "$ROOT/agent/lib.sh"
  RUN_LOCK="$STATE_DIR/locks/fixer.run"
  source "$ROOT/agent/watcher-lib.sh"
  FAKE_GH_LOGIN=fix-bot maybe_automerge 102
) >/dev/null 2>&1
automerge=$(python3 -c "import json;print(json.load(open('$ISSUE_AGENT_FAKE_STATE'))['prs']['102'].get('automerge',False))")
[ "$automerge" = "False" ] && ok "solo mode leaves merge to human" || no "solo mode leaves merge to human" "automerge=$automerge"

echo "== ci_state: pending takes precedence over a partial failure =="
ci_for() {  # ci_for '<checks-json-array>' [CI_IGNORE_CHECKS-regex]  → echoes ci_state
  local d; d=$(new_sandbox); mk_env "$d"
  seed_state "{\"issues\":{},\"prs\":{\"200\":{\"number\":200,\"state\":\"OPEN\",\"headRefName\":\"fix/1-x\",\"labels\":[\"ai-pr-open\"],\"checks\":$1}},\"labels\":[],\"runs\":{}}"
  ( cd "$d/work"; source "$ROOT/agent/lib.sh"; RUN_LOCK="$STATE_DIR/locks/fixer.run"
    source "$ROOT/agent/watcher-lib.sh"
    # inline VAR=val before a shell FUNCTION does not export to it (only to
    # external commands); set+export explicitly so ci_state sees it.
    export FAKE_GH_LOGIN=fix-bot CI_IGNORE_CHECKS="${2:-}"
    ci_state 200 ) 2>/dev/null
}
# A failed check WHILE another is still queued → pending (do NOT dispatch fix-ci).
r=$(ci_for '[{"name":"sonar","bucket":"fail","state":"COMPLETED"},{"name":"build","bucket":"pending","state":"QUEUED"}]')
[ "$r" = pending ] && ok "fail+queued → pending (waits for CI to settle)" || no "fail+queued → pending" "got=$r"
# All settled, one failed → failure (now it's a real, complete verdict).
r=$(ci_for '[{"name":"sonar","bucket":"fail","state":"COMPLETED"},{"name":"build","bucket":"pass","state":"SUCCESS"}]')
[ "$r" = failure ] && ok "fail+pass (settled) → failure" || no "fail+pass (settled) → failure" "got=$r"
# All green → ok; no checks → none.
r=$(ci_for '[{"name":"build","bucket":"pass","state":"SUCCESS"}]')
[ "$r" = ok ] && ok "all pass → ok" || no "all pass → ok" "got=$r"
r=$(ci_for '[]')
[ "$r" = none ] && ok "no checks → none" || no "no checks → none" "got=$r"

echo "== ci_state: CI_IGNORE_CHECKS drops stuck jobs from the decision =="
# The live #50 shape: Sonar failed, two Format Backend jobs queued forever, rest pass.
STUCK='[{"name":"SonarQube Code Analysis","bucket":"fail","state":"COMPLETED"},{"name":"Format Backend (3.11.x)","bucket":"pending","state":"QUEUED"},{"name":"Format Backend (3.12.x)","bucket":"pending","state":"QUEUED"},{"name":"Build and analyze","bucket":"pass","state":"SUCCESS"}]'
# Without the ignore → pending (queued wins) → deadlock.
r=$(ci_for "$STUCK")
[ "$r" = pending ] && ok "stuck jobs → pending without ignore" || no "stuck jobs → pending without ignore" "got=$r"
# Ignoring Format Backend → the queued jobs drop out → Sonar's real failure surfaces → failure (so fix-ci can fire).
r=$(ci_for "$STUCK" 'Format Backend')
[ "$r" = failure ] && ok "ignore stuck → real Sonar failure surfaces" || no "ignore stuck → real Sonar failure surfaces" "got=$r"
# Same but Sonar now green → ignoring the stuck jobs yields ok (so auto-merge can fire).
GREEN='[{"name":"SonarQube Code Analysis","bucket":"pass","state":"SUCCESS"},{"name":"Format Backend (3.11.x)","bucket":"pending","state":"QUEUED"},{"name":"Build and analyze","bucket":"pass","state":"SUCCESS"}]'
r=$(ci_for "$GREEN" 'Format Backend')
[ "$r" = ok ] && ok "ignore stuck + rest green → ok (auto-merge unblocks)" || no "ignore stuck + rest green → ok" "got=$r"
# A non-ignored real failure is still respected even with an ignore set.
r=$(ci_for '[{"name":"SonarQube Code Analysis","bucket":"fail","state":"COMPLETED"},{"name":"Build and analyze","bucket":"pass","state":"SUCCESS"}]' 'Format Backend')
[ "$r" = failure ] && ok "ignore does NOT mask a real failure" || no "ignore does NOT mask a real failure" "got=$r"

echo "== issue resolution: dev-targeted PR (no closing ref) falls back to branch =="
# A PR targeting dev has NO closingIssuesReferences (GitHub only populates that
# graph vs the default branch). fix-ci/address-review/cleanup must derive the
# issue from the head branch instead, or they bail "no linked issue".
d=$(new_sandbox); mk_env "$d"
# PR 300 has a fix/48- branch but NO "issue" field → fake emits empty closing refs.
seed_state '{"issues":{},"prs":{"300":{"number":300,"state":"OPEN","headRefName":"fix/48-my-app","labels":["ai-pr-open"]}},"labels":[],"runs":{}}'
res=$( cd "$d/work"; source "$ROOT/agent/lib.sh"; source "$ROOT/agent/watcher-lib.sh"
  export FAKE_GH_LOGIN=fix-bot
  fp=$(issue_for_pr 300); fb=$(issue_from_branch "$(branch_for_pr 300)")
  echo "for_pr=[$fp] from_branch=[$fb] resolved=[${fp:-$fb}]" ) 2>/dev/null
echo "$res" | grep -q 'for_pr=\[\]' && ok "issue_for_pr empty on dev-targeted PR (no closing ref)" || no "issue_for_pr empty on dev PR" "$res"
echo "$res" | grep -q 'resolved=\[48\]' && ok "falls back to issue 48 from the head branch" || no "branch fallback → 48" "$res"

echo "== FIX_MCP_CONFIG: CI-fix MCP attaches ONLY when the matching check is present =="
# A repo-agnostic conditional: the SonarQube MCP must attach to fix-ci only when
# the PR's CI actually has a SonarQube check. mk_env_mcp seeds the knob + a real
# (empty-but-present) mcp config file so the file-exists guard passes.
mcp_args_for() {  # mcp_args_for '<checks-json>'  → echoes the COMMON_ARGS, one per line
  local d; d=$(new_sandbox); mk_env "$d"
  printf '{"mcpServers":{}}' > "$d/work/mcp.json"   # the (present) MCP config file
  seed_state "{\"issues\":{},\"prs\":{\"400\":{\"number\":400,\"state\":\"OPEN\",\"headRefName\":\"fix/9-x\",\"labels\":[\"ai-pr-open\"],\"checks\":$1}}, \"labels\":[],\"runs\":{}}"
  ( cd "$d/work"; source "$ROOT/agent/lib.sh"; source "$ROOT/agent/watcher-lib.sh"
    # absolute config path so the file-exists guard passes regardless of _REPO_ROOT
    export FIX_MCP_CONFIG="{\"SonarQube\":{\"config\":\"$d/work/mcp.json\",\"tools\":\"mcp__sonarqube__*\"}}"
    export FAKE_GH_LOGIN=fix-bot
    common_claude_args 400
    printf '%s\n' "${COMMON_ARGS[@]}" ) 2>/dev/null
}
# Sonar check present → --mcp-config attached AND tool allow-listed in --settings.
args=$(mcp_args_for '[{"name":"SonarQube Code Analysis","bucket":"fail","state":"COMPLETED"}]')
echo "$args" | grep -q -- '--mcp-config' && ok "Sonar check present → --mcp-config attached" || no "Sonar present → --mcp-config" "args=$(echo "$args"|tr '\n' ' ')"
echo "$args" | grep -q 'mcp__sonarqube__\*' && ok "Sonar MCP tool allow-listed in --settings" || no "Sonar tool allow-listed" "args=$(echo "$args"|tr '\n' ' ')"
# No Sonar check → MCP NOT attached (repo-agnostic: a non-Sonar repo never loads it).
args=$(mcp_args_for '[{"name":"Build and analyze","bucket":"pass","state":"SUCCESS"}]')
echo "$args" | grep -q -- '--mcp-config' && no "no Sonar → MCP must NOT attach" "args=$(echo "$args"|tr '\n' ' ')" || ok "no Sonar check → MCP not attached"

echo "== FIX_MCP_CONFIG blank → never attaches (default) =="
d=$(new_sandbox); mk_env "$d"
seed_state '{"issues":{},"prs":{"401":{"number":401,"state":"OPEN","headRefName":"fix/9-x","labels":["ai-pr-open"],"checks":[{"name":"SonarQube Code Analysis","bucket":"fail","state":"COMPLETED"}]}},"labels":[],"runs":{}}'
args=$( cd "$d/work"; source "$ROOT/agent/lib.sh"; source "$ROOT/agent/watcher-lib.sh"
  export FAKE_GH_LOGIN=fix-bot; common_claude_args 401; printf '%s\n' "${COMMON_ARGS[@]}" ) 2>/dev/null
echo "$args" | grep -q -- '--mcp-config' && no "blank knob must not attach MCP" || ok "FIX_MCP_CONFIG blank → no MCP even with Sonar present"

echo "== cleanup: merged PR → issue ai-done AND closed =="
d=$(new_sandbox); mk_env "$d"
seed_state '{"issues":{"5":{"number":5,"labels":["ai-pr-open"],"comments":[],"state":"open"}},"prs":{"100":{"number":100,"state":"MERGED","issue":5,"headRefName":"fix/5-x","labels":[]}},"labels":[],"runs":{}}'
( cd "$d/work" && FAKE_GH_LOGIN=fix-bot bash "$ROOT/agent/cleanup.sh" 100 ) >/dev/null 2>&1
labels=$(ghq issue view 5 --repo o/r --json labels --jq '[.labels[].name]|join(",")')
[[ "$labels" == *ai-done* ]] && ok "merged → ai-done" || no "merged → ai-done" "labels=$labels"
istate=$(ghq issue view 5 --repo o/r --json state --jq '.state')
[[ "$istate" == "CLOSED" ]] && ok "merged → issue closed (non-default branch)" || no "merged → issue closed" "state=$istate"

echo "== cleanup: CLOSE_ISSUE_ON_MERGE=0 → ai-done but issue stays OPEN =="
d=$(new_sandbox); mk_env "$d"
seed_state '{"issues":{"5":{"number":5,"labels":["ai-pr-open"],"comments":[],"state":"open"}},"prs":{"100":{"number":100,"state":"MERGED","issue":5,"headRefName":"fix/5-x","labels":[]}},"labels":[],"runs":{}}'
( cd "$d/work" && CLOSE_ISSUE_ON_MERGE=0 FAKE_GH_LOGIN=fix-bot bash "$ROOT/agent/cleanup.sh" 100 ) >/dev/null 2>&1
istate=$(ghq issue view 5 --repo o/r --json state --jq '.state')
labels=$(ghq issue view 5 --repo o/r --json labels --jq '[.labels[].name]|join(",")')
[[ "$istate" == "OPEN" && "$labels" == *ai-done* ]] && ok "knob off → ai-done, stays open" || no "knob off → stays open" "state=$istate labels=$labels"

echo "== triage: blocked issue → asks creator, adds ai-triage, NOT promoted =="
d=$(new_sandbox); mk_env "$d" "STEWARD_TRIAGE=1" "STEWARD_PROMOTE=1" "STEWARD_STATE_DIR=$d/sstate"
seed_state '{"issues":{"7":{"number":7,"title":"feat: thing","body":"do a thing","labels":["enhancement"],"comments":[],"state":"open","updatedAt":"2026-06-25T01:00:00Z","author":{"login":"alice"}}},"prs":{},"labels":[],"runs":{}}'
seed_claude '[{"envelope":{"is_error":false,"session_id":"t7","total_cost_usd":0.02,"structured_output":{"verdict":"blocked","plain_summary":"We can make the errors friendlier.","acceptance_criteria":["map gdw_auth.py:447 raw error to friendly text"],"blocking_gaps":["Which database?"]}},"effect":null}]'
( cd "$d/work" && STEWARD_NO_LOOP=1 FAKE_GH_LOGIN=steward-bot . "$ROOT/steward/steward.sh"; triage_once ) >/dev/null 2>&1
labels=$(ghq issue view 7 --repo o/r --json labels --jq '[.labels[].name]|join(",")')
body=$(ghq issue view 7 --repo o/r --json comments --jq '.comments[-1].body')
[[ "$labels" == *ai-triage* && "$labels" != *ai-ready* ]] && ok "blocked → ai-triage, not promoted" || no "blocked → ai-triage" "labels=$labels"
[[ "$body" == *"@alice"* && "$body" == *"Which database?"* ]] && ok "blocked → @mentions creator with gaps" || no "blocked → @mention+gaps" "body=$body"
# The non-technical author must NOT see the technical AC on a blocked issue.
[[ "$body" != *"gdw_auth.py"* && "$body" != *"acceptance"* ]] && ok "blocked → technical AC withheld from author" || no "blocked → AC leaked to author" "body=$body"

echo "== triage: ready + STEWARD_PROMOTE=1 → promoted to ai-ready =="
d=$(new_sandbox); mk_env "$d" "STEWARD_TRIAGE=1" "STEWARD_PROMOTE=1" "STEWARD_STATE_DIR=$d/sstate"
seed_state '{"issues":{"7":{"number":7,"title":"feat: thing","body":"clear","labels":["enhancement"],"comments":[],"state":"open","updatedAt":"2026-06-25T01:00:00Z","author":{"login":"alice"}}},"prs":{},"labels":[],"runs":{}}'
seed_claude '[{"envelope":{"is_error":false,"session_id":"t7","total_cost_usd":0.02,"structured_output":{"verdict":"ready","acceptance_criteria":["Given X, when Y, then Z"],"blocking_gaps":[]}},"effect":null}]'
( cd "$d/work" && STEWARD_NO_LOOP=1 FAKE_GH_LOGIN=steward-bot . "$ROOT/steward/steward.sh"; triage_once ) >/dev/null 2>&1
labels=$(ghq issue view 7 --repo o/r --json labels --jq '[.labels[].name]|join(",")')
[[ "$labels" == *ai-ready* && "$labels" != *ai-triage* ]] && ok "ready+promote → ai-ready" || no "ready+promote → ai-ready" "labels=$labels"

echo "== triage: ready + STEWARD_PROMOTE=0 → AC posted, ai-triage, NOT ai-ready =="
d=$(new_sandbox); mk_env "$d" "STEWARD_TRIAGE=1" "STEWARD_PROMOTE=0" "STEWARD_STATE_DIR=$d/sstate"
seed_state '{"issues":{"7":{"number":7,"title":"feat: thing","body":"clear","labels":["enhancement"],"comments":[],"state":"open","updatedAt":"2026-06-25T01:00:00Z","author":{"login":"alice"}}},"prs":{},"labels":[],"runs":{}}'
seed_claude '[{"envelope":{"is_error":false,"session_id":"t7","total_cost_usd":0.02,"structured_output":{"verdict":"ready","plain_summary":"Error messages will be easier to read.","acceptance_criteria":["Given X, when Y, then Z"],"blocking_gaps":[]}},"effect":null}]'
( cd "$d/work" && STEWARD_NO_LOOP=1 FAKE_GH_LOGIN=steward-bot . "$ROOT/steward/steward.sh"; triage_once ) >/dev/null 2>&1
labels=$(ghq issue view 7 --repo o/r --json labels --jq '[.labels[].name]|join(",")')
body=$(ghq issue view 7 --repo o/r --json comments --jq '.comments[-1].body')
[[ "$labels" == *ai-triage* && "$labels" != *ai-ready* && "$body" == *"Given X"* ]] && ok "ready+no-promote → AC+ai-triage, human promotes" || no "ready+no-promote" "labels=$labels"
# READY: plain summary visible to author; technical AC tucked into a collapsed section.
[[ "$body" == *"easier to read"* && "$body" == *"<details>"* ]] && ok "ready → plain summary + AC hidden in <details>" || no "ready → plain+details" "body=$body"

echo "== triage: pipeline issue (ai-ready) is NOT re-triaged =="
d=$(new_sandbox); mk_env "$d" "STEWARD_TRIAGE=1" "STEWARD_PROMOTE=1" "STEWARD_STATE_DIR=$d/sstate"
seed_state '{"issues":{"7":{"number":7,"title":"x","body":"y","labels":["ai-ready"],"comments":[],"state":"open","updatedAt":"2026-06-25T01:00:00Z","author":{"login":"alice"}}},"prs":{},"labels":[],"runs":{}}'
seed_claude '[]'
( cd "$d/work" && STEWARD_NO_LOOP=1 FAKE_GH_LOGIN=steward-bot . "$ROOT/steward/steward.sh"; triage_once ) >/dev/null 2>&1
ncomments=$(ghq issue view 7 --repo o/r --json comments --jq '.comments|length')
[[ "$ncomments" == "0" ]] && ok "ai-ready issue skipped by triage" || no "ai-ready skipped" "comments=$ncomments"

echo "== triage: idempotent — our OWN comment must not re-trigger triage (no loop) =="
# Regression for the updatedAt feedback loop: the steward's comment bumps the
# issue's updatedAt, so a naive baseline (pre-action updatedAt) would mismatch on
# the next pass and re-comment forever. Two triage responses are queued; if the
# guard works, the 2nd triage_once sees recorded==live (we baked our own action in)
# and does NOTHING, leaving exactly one comment. The fake gh now bumps updatedAt
# on comment/edit (like real GitHub), so this genuinely exercises the loop.
d=$(new_sandbox); mk_env "$d" "STEWARD_TRIAGE=1" "STEWARD_PROMOTE=0" "STEWARD_STATE_DIR=$d/sstate"
seed_state '{"issues":{"7":{"number":7,"title":"x","body":"y","labels":["enhancement"],"comments":[],"state":"open","updatedAt":"2026-06-25T00:00:00Z","author":{"login":"alice"}}},"prs":{},"labels":[],"runs":{}}'
seed_claude '[{"envelope":{"is_error":false,"session_id":"t7a","total_cost_usd":0.02,"structured_output":{"verdict":"ready","acceptance_criteria":["AC"],"blocking_gaps":[]}},"effect":null},{"envelope":{"is_error":false,"session_id":"t7b","total_cost_usd":0.02,"structured_output":{"verdict":"ready","acceptance_criteria":["AC2"],"blocking_gaps":[]}},"effect":null}]'
( cd "$d/work" && STEWARD_NO_LOOP=1 FAKE_GH_LOGIN=steward-bot . "$ROOT/steward/steward.sh"; triage_once; triage_once ) >/dev/null 2>&1
ncomments=$(ghq issue view 7 --repo o/r --json comments --jq '.comments|length')
[[ "$ncomments" == "1" ]] && ok "own comment does NOT re-trigger triage (no feedback loop)" || no "triage feedback loop" "comments=$ncomments (expected 1)"

echo "== triage: a NEW human reply (later updatedAt) DOES re-trigger triage =="
# After the steward bounces a blocked issue, the author replies → updatedAt
# advances past our recorded baseline → triage runs again (the intended path).
d=$(new_sandbox); mk_env "$d" "STEWARD_TRIAGE=1" "STEWARD_PROMOTE=0" "STEWARD_STATE_DIR=$d/sstate"
seed_state '{"issues":{"7":{"number":7,"title":"x","body":"y","labels":["enhancement"],"comments":[],"state":"open","updatedAt":"2026-06-25T00:00:00Z","author":{"login":"alice"}}},"prs":{},"labels":[],"runs":{}}'
seed_claude '[{"envelope":{"is_error":false,"session_id":"t7a","total_cost_usd":0.02,"structured_output":{"verdict":"blocked","acceptance_criteria":["AC"],"blocking_gaps":["Which thing?"]}},"effect":null},{"envelope":{"is_error":false,"session_id":"t7b","total_cost_usd":0.02,"structured_output":{"verdict":"ready","acceptance_criteria":["AC2"],"blocking_gaps":[]}},"effect":null}]'
( cd "$d/work" && STEWARD_NO_LOOP=1 . "$ROOT/steward/steward.sh"
  export FAKE_GH_LOGIN=steward-bot
  triage_once                                          # bounce (steward comment)
  FAKE_GH_LOGIN=alice "$FAKES/gh" issue comment 7 --repo o/r --body "it's the blue one"  # human reply bumps updatedAt
  triage_once ) >/dev/null 2>&1                          # should re-triage → 2nd steward comment
nsteward=$(ghq issue view 7 --repo o/r --json comments --jq '[.comments[]|select(.author.login=="steward-bot")]|length')
[[ "$nsteward" == "2" ]] && ok "human reply re-triggers triage" || no "human reply re-triage" "steward comments=$nsteward (expected 2)"

echo "== triage: re-triage FEEDS the comment thread to the model (sees author's answer) =="
# Regression: triage_once previously passed only title+body, so a re-triage could
# not see the author's reply and re-asked the answered question forever. The
# claude prompt must now include the prior comments.
d=$(new_sandbox); mk_env "$d" "STEWARD_TRIAGE=1" "STEWARD_PROMOTE=0" "STEWARD_STATE_DIR=$d/sstate"
export ISSUE_AGENT_FAKE_CLAUDE_ARGLOG="$d/triage-args.log"
seed_state '{"issues":{"7":{"number":7,"title":"x","body":"y","labels":["enhancement"],"comments":[{"author":{"login":"steward-bot"},"body":"Which colour?"},{"author":{"login":"alice"},"body":"go with the blue one"}],"state":"open","updatedAt":"2026-06-25T00:00:00Z","author":{"login":"alice"}}},"prs":{},"labels":[],"runs":{}}'
seed_claude '[{"envelope":{"is_error":false,"session_id":"t7","total_cost_usd":0.02,"structured_output":{"verdict":"ready","plain_summary":"clear now","acceptance_criteria":["AC"],"blocking_gaps":[]}},"effect":null}]'
( cd "$d/work" && STEWARD_NO_LOOP=1 FAKE_GH_LOGIN=steward-bot . "$ROOT/steward/steward.sh"; triage_once ) >/dev/null 2>&1
# the prompt (last argv element logged) must carry the author's reply verbatim
promptline=$(grep -m1 'go with the blue one' "$ISSUE_AGENT_FAKE_CLAUDE_ARGLOG" 2>/dev/null)
[[ -n "$promptline" ]] && ok "re-triage prompt includes the author's reply (thread fed to model)" || no "thread not fed to model" "arglog=$(cat "$ISSUE_AGENT_FAKE_CLAUDE_ARGLOG" 2>/dev/null | head -c 200)"
unset ISSUE_AGENT_FAKE_CLAUDE_ARGLOG

echo "== triage: OFF by default → no action =="
d=$(new_sandbox); mk_env "$d" "STEWARD_STATE_DIR=$d/sstate"
seed_state '{"issues":{"7":{"number":7,"title":"x","body":"y","labels":["enhancement"],"comments":[],"state":"open","updatedAt":"2026-06-25T00:00:00Z","author":{"login":"alice"}}},"prs":{},"labels":[],"runs":{}}'
seed_claude '[]'
( cd "$d/work" && STEWARD_NO_LOOP=1 FAKE_GH_LOGIN=steward-bot . "$ROOT/steward/steward.sh"; triage_once ) >/dev/null 2>&1
ncomments=$(ghq issue view 7 --repo o/r --json comments --jq '.comments|length')
labels=$(ghq issue view 7 --repo o/r --json labels --jq '[.labels[].name]|join(",")')
[[ "$ncomments" == "0" && "$labels" != *ai-triage* ]] && ok "STEWARD_TRIAGE=0 → no-op" || no "triage off → no-op" "comments=$ncomments labels=$labels"

echo "== triage: uses STEWARD_TRIAGE_MODEL (cheaper model) for the classify call =="
d=$(new_sandbox); mk_env "$d" "STEWARD_TRIAGE=1" "STEWARD_PROMOTE=0" "STEWARD_STATE_DIR=$d/sstate" "STEWARD_TRIAGE_MODEL=sonnet" "STEWARD_MODEL=opus"
export ISSUE_AGENT_FAKE_CLAUDE_ARGLOG="$d/claude-args.log"
seed_state '{"issues":{"7":{"number":7,"title":"x","body":"y","labels":["enhancement"],"comments":[],"state":"open","updatedAt":"2026-06-25T00:00:00Z","author":{"login":"alice"}}},"prs":{},"labels":[],"runs":{}}'
seed_claude '[{"envelope":{"is_error":false,"session_id":"t7","total_cost_usd":0.02,"structured_output":{"verdict":"ready","acceptance_criteria":["AC"],"blocking_gaps":[]}},"effect":null}]'
( cd "$d/work" && STEWARD_NO_LOOP=1 FAKE_GH_LOGIN=steward-bot . "$ROOT/steward/steward.sh"; triage_once ) >/dev/null 2>&1
argline=$(grep -m1 'json-schema' "$ISSUE_AGENT_FAKE_CLAUDE_ARGLOG" 2>/dev/null || tail -1 "$ISSUE_AGENT_FAKE_CLAUDE_ARGLOG" 2>/dev/null)
{ echo "$argline" | grep -q '"--model"' && echo "$argline" | grep -q '"sonnet"' && ! echo "$argline" | grep -q '"opus"'; } \
  && ok "triage call uses --model sonnet (not opus STEWARD_MODEL)" || no "triage --model sonnet" "args=$argline"
unset ISSUE_AGENT_FAKE_CLAUDE_ARGLOG

echo "== cleanup: closed-unmerged PR → issue re-queued ai-ready =="
d=$(new_sandbox); mk_env "$d"
seed_state '{"issues":{"5":{"number":5,"labels":["ai-pr-open"],"comments":[],"state":"open"}},"prs":{"100":{"number":100,"state":"CLOSED","issue":5,"headRefName":"fix/5-x","labels":[]}},"labels":[],"runs":{}}'
( cd "$d/work" && FAKE_GH_LOGIN=fix-bot bash "$ROOT/agent/cleanup.sh" 100 ) >/dev/null 2>&1
labels=$(ghq issue view 5 --repo o/r --json labels --jq '[.labels[].name]|join(",")')
[[ "$labels" == *ai-ready* ]] && ok "closed-unmerged → ai-ready" || no "closed-unmerged → re-queued" "labels=$labels"

echo "== reaper: orphaned ai-working (no live fixer) → ai-ready =="
d=$(new_sandbox); mk_env "$d"
seed_state '{"issues":{"8":{"number":8,"labels":["ai-working"],"comments":[],"state":"open"}},"prs":{},"labels":[],"runs":{}}'
( cd "$d/work"
  source "$ROOT/agent/lib.sh"
  RUN_LOCK="$STATE_DIR/locks/fixer.run"
  source "$ROOT/agent/watcher-lib.sh"
  RUN_LOCK="$STATE_DIR/locks/fixer.run"; rm -f "$RUN_LOCK"
  FAKE_GH_LOGIN=fix-bot reap
) >/dev/null 2>&1
labels=$(ghq issue view 8 --repo o/r --json labels --jq '[.labels[].name]|join(",")')
[[ "$labels" == *ai-ready* && "$labels" != *ai-working* ]] && ok "reaper requeued orphan" || no "reaper requeued orphan" "labels=$labels"

echo "== re-review escalation at round cap → needs-human-review =="
d=$(new_sandbox); mk_env "$d" "REVIEW_MAX_ROUNDS=2"
seed_state '{"issues":{},"prs":{"100":{"number":100,"state":"OPEN","title":"t","body":"b","headRefName":"fix/5-x","headRefOid":"sha2","issue":5,"labels":["ai-pr-open"],"reviews":[],"checks":[]}},"labels":[],"runs":{}}'
mkdir -p "$d/rstate"; echo "r100" > "$d/rstate/pr-100.session"; echo "sha1" > "$d/rstate/pr-100.last-sha"; echo 1 > "$d/rstate/pr-100.round"
seed_claude '[{"envelope":{"is_error":false,"session_id":"r100","total_cost_usd":0.05,"structured_output":{"decision":"ESCALATE","body":"still wrong"}},"effect":null}]'
( cd "$d/work" && FAKE_GH_LOGIN=review-bot bash "$ROOT/review/re-review-pr.sh" 100 ) >/dev/null 2>&1
labels=$(ghq pr view 100 --repo o/r --json labels --jq '[.labels[].name]|join(",")')
[[ "$labels" == *needs-human-review* && "$labels" != *ai-pr-open* ]] && ok "escalate → needs-human-review" || no "escalate → needs-human-review" "labels=$labels"

echo "== reaper: orphaned PR with ai-working → label cleared (not sent to ai-ready) =="
d=$(new_sandbox); mk_env "$d"
seed_state '{"issues":{},"prs":{"100":{"number":100,"state":"OPEN","headRefName":"fix/5-x","issue":5,"labels":["ai-pr-open","ai-working"]}},"labels":[],"runs":{}}'
( cd "$d/work"
  source "$ROOT/agent/lib.sh"; RUN_LOCK="$STATE_DIR/locks/fixer.run"
  source "$ROOT/agent/watcher-lib.sh"; rm -f "$RUN_LOCK"
  FAKE_GH_LOGIN=fix-bot reap
) >/dev/null 2>&1
labels=$(ghq pr view 100 --repo o/r --json labels --jq '[.labels[].name]|join(",")')
[[ "$labels" == *ai-pr-open* && "$labels" != *ai-working* ]] && ok "reaper cleared PR ai-working, kept ai-pr-open" || no "reaper cleared PR ai-working" "labels=$labels"

echo "== cleanup: PR with no closing-ref falls back to branch name → issue ai-done =="
d=$(new_sandbox); mk_env "$d"
# PR 100 has NO issue closing reference, but head branch is fix/5-x → issue 5.
seed_state '{"issues":{"5":{"number":5,"labels":["ai-pr-open"],"comments":[],"state":"open"}},"prs":{"100":{"number":100,"state":"MERGED","headRefName":"fix/5-token","labels":["ai-pr-open"]}},"labels":[],"runs":{}}'
( cd "$d/work" && FAKE_GH_LOGIN=fix-bot bash "$ROOT/agent/cleanup.sh" 100 ) >/dev/null 2>&1
ilabels=$(ghq issue view 5 --repo o/r --json labels --jq '[.labels[].name]|join(",")')
plabels=$(ghq pr view 100 --repo o/r --json labels --jq '[.labels[].name]|join(",")')
[[ "$ilabels" == *ai-done* ]] && ok "cleanup branch-fallback → issue ai-done" || no "cleanup branch-fallback" "ilabels=$ilabels"
[[ "$plabels" != *ai-pr-open* ]] && ok "cleanup drops ai-pr-open from PR" || no "cleanup drops PR label" "plabels=$plabels"

echo "== re-review error → last-sha + round NOT advanced (no silent drop) =="
d=$(new_sandbox); mk_env "$d"
seed_state '{"issues":{},"prs":{"100":{"number":100,"state":"OPEN","headRefName":"fix/5-x","headRefOid":"sha2","issue":5,"labels":["ai-pr-open"]}},"labels":[],"runs":{}}'
mkdir -p "$d/rstate"; echo "r100" > "$d/rstate/pr-100.session"; echo "sha1" > "$d/rstate/pr-100.last-sha"; echo 1 > "$d/rstate/pr-100.round"
seed_claude '[{"envelope":{"is_error":true,"subtype":"error","session_id":"r100"},"effect":null,"rc":1}]'
( cd "$d/work" && FAKE_GH_LOGIN=review-bot bash "$ROOT/review/re-review-pr.sh" 100 ) >/dev/null 2>&1
sha_after=$(cat "$d/rstate/pr-100.last-sha"); round_after=$(cat "$d/rstate/pr-100.round")
[ "$sha_after" = "sha1" ] && [ "$round_after" = "1" ] && ok "errored re-review left sha+round unchanged" || no "errored re-review left sha+round unchanged" "sha=$sha_after round=$round_after"

echo "== address-review: exhausts retries on same review → needs-human-review =="
d=$(new_sandbox); mk_env "$d" "MAX_REVIEW_RETRIES=2"
seed_state '{"issues":{},"prs":{"100":{"number":100,"state":"OPEN","headRefName":"fix/5-x","issue":5,"labels":["ai-pr-open"],"reviews":[{"state":"CHANGES_REQUESTED","user":{"login":"review-bot"},"submitted_at":"2026-02-02T00:00:00Z","submittedAt":"2026-02-02T00:00:00Z","body":"fix it"}]}},"labels":[],"runs":{}}'
echo "sess5" > "$STATE_DIR/issue-5.session"
# pre-seed the retry counter at the cap for this review timestamp
printf '2026-02-02T00:00:00Z 2' > "$STATE_DIR/issue-5.review-retries"
seed_claude '[]'
( cd "$d/work" && FAKE_GH_LOGIN=fix-bot bash "$ROOT/agent/address-review.sh" 100 ) >/dev/null 2>&1
labels=$(ghq pr view 100 --repo o/r --json labels --jq '[.labels[].name]|join(",")')
[[ "$labels" == *needs-human-review* && "$labels" != *ai-pr-open* ]] && ok "address-review cap → needs-human-review" || no "address-review cap → needs-human-review" "labels=$labels"

echo "== blocked-issue resume fires once per reply (no infinite loop) =="
d=$(new_sandbox); mk_env "$d"
seed_state '{"issues":{"6":{"number":6,"labels":["ai-blocked"],"comments":[{"author":{"login":"alice"},"body":"use /v2","createdAt":"2026-03-03T00:00:00Z"}],"state":"open"}},"prs":{},"labels":[],"runs":{}}'
echo "sess6" > "$STATE_DIR/issue-6.session"
# Simulate the watcher's blocked-issue trigger logic (one poll pass, twice).
trigger_blocked() {
  last_at=$(ghq issue view 6 --repo o/r --json comments \
    --jq "[.comments[] | select(.author.login != \"fix-bot\" and (.author.login|endswith(\"[bot]\")|not))] | last | .createdAt // \"\"")
  seen_at=$(cat "$STATE_DIR/issue-6.resumed-at" 2>/dev/null || echo "")
  if [ "$last_at" != "$seen_at" ]; then echo "$last_at" > "$STATE_DIR/issue-6.resumed-at"; echo FIRE; else echo SKIP; fi
}
r1=$(trigger_blocked); r2=$(trigger_blocked)
[ "$r1" = FIRE ] && [ "$r2" = SKIP ] && ok "resume fires once, then skips same reply" || no "resume fires once" "r1=$r1 r2=$r2"

echo "== blocked address-review does NOT burn a retry (waits for human) =="
d=$(new_sandbox); mk_env "$d"
seed_state '{"issues":{},"prs":{"100":{"number":100,"state":"OPEN","headRefName":"fix/5-x","issue":5,"labels":["ai-pr-open"],"reviews":[{"state":"CHANGES_REQUESTED","user":{"login":"review-bot"},"submitted_at":"2026-02-02T00:00:00Z","submittedAt":"2026-02-02T00:00:00Z","body":"clarify?"}]}},"labels":[],"runs":{}}'
echo "sess5" > "$STATE_DIR/issue-5.session"
seed_claude '[{"envelope":{"is_error":false,"session_id":"sess5","structured_output":{"status":"blocked","summary":"which API?","needs":"endpoint"}},"effect":null}]'
( cd "$d/work" && FAKE_GH_LOGIN=fix-bot bash "$ROOT/agent/address-review.sh" 100 ) >/dev/null 2>&1
labels=$(ghq pr view 100 --repo o/r --json labels --jq '[.labels[].name]|join(",")')
# blocked → ai-blocked, and NO retry file written (so it didn't count as a failure)
[[ "$labels" == *ai-blocked* ]] && [ ! -f "$STATE_DIR/issue-5.review-retries" ] \
  && ok "blocked review → ai-blocked, no retry burned" \
  || no "blocked review → ai-blocked, no retry burned" "labels=$labels retryfile=$([ -f "$STATE_DIR/issue-5.review-retries" ] && echo yes || echo no)"

echo ""
printf 'orchestration: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
