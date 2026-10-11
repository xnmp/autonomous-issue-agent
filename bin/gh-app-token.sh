#!/usr/bin/env bash
# Mint a short-lived GitHub App installation token from the App ID + private key.
#
# GitHub App installation tokens expire after ~1 hour, so a daemon must re-mint.
# This script caches the token to a file and only re-mints when <5 min remain,
# making it cheap to call on every gh invocation.
#
# Required env (exported by sourcing bin/compose-env.sh <name>):
#   GH_APP_ID          numeric App ID (shown on the App's settings page after creation)
#   GH_APP_KEY_FILE    path to the downloaded .pem private key
#   GH_APP_INSTALL_ID  installation id (see `resolve install id` below)
# Optional:
#   GH_APP_TOKEN_CACHE token cache path (default: ~/.local/state/issue-agent-bot/app-token.json)
#
# Resolve install id once (uses a freshly minted JWT):
#   gh api /app/installations --jq '.[] | {id, org:.account.login}'
# or for a single org install: the id in the org's app settings URL.
#
# Usage:
#   TOKEN="$(bin/gh-app-token.sh)"        # prints a valid installation token
set -euo pipefail

: "${GH_APP_ID:?set GH_APP_ID}"
: "${GH_APP_KEY_FILE:?set GH_APP_KEY_FILE (path to .pem)}"
: "${GH_APP_INSTALL_ID:?set GH_APP_INSTALL_ID}"
CACHE="${GH_APP_TOKEN_CACHE:-$HOME/.local/state/issue-agent-bot/app-token.json}"
mkdir -p "$(dirname "$CACHE")"

now="$(date +%s)"

# Reuse cached token if it has >5 min of life left.
if [ -f "$CACHE" ]; then
  exp="$(jq -r '.expires_epoch // 0' "$CACHE" 2>/dev/null || echo 0)"
  if [ "$exp" -gt "$((now + 300))" ]; then
    jq -r '.token' "$CACHE"
    exit 0
  fi
fi

# --- Build a GitHub App JWT (RS256), valid for ~9 minutes. ---
b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }
header='{"alg":"RS256","typ":"JWT"}'
iat="$((now - 60))"        # backdate 60s for clock skew
exp_jwt="$((now + 540))"   # max 10 min; use 9
payload="{\"iat\":${iat},\"exp\":${exp_jwt},\"iss\":\"${GH_APP_ID}\"}"
unsigned="$(printf '%s' "$header" | b64url).$(printf '%s' "$payload" | b64url)"
sig="$(printf '%s' "$unsigned" | openssl dgst -sha256 -sign "$GH_APP_KEY_FILE" | b64url)"
jwt="${unsigned}.${sig}"

# --- Exchange JWT for an installation access token. ---
resp="$(curl -fsS -X POST \
  -H "Authorization: Bearer ${jwt}" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  "https://api.github.com/app/installations/${GH_APP_INSTALL_ID}/access_tokens")"

token="$(printf '%s' "$resp" | jq -r '.token')"
expires="$(printf '%s' "$resp" | jq -r '.expires_at')"   # ISO8601
if [ -z "$token" ] || [ "$token" = "null" ]; then
  printf 'gh-app-token: failed to mint token: %s\n' "$resp" >&2
  exit 1
fi

# date parsing differs on macOS (BSD) vs Linux (GNU); try both. The timestamp
# is UTC ("…Z"), so BOTH branches must interpret it as UTC: BSD `date -j -f`
# uses the local tz unless -u is given (a UTC+N machine would otherwise shift
# the epoch N hours early, making the cache permanently look expired).
exp_epoch="$(date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "$expires" +%s 2>/dev/null \
          || date -u -d "$expires" +%s 2>/dev/null || echo "$((now + 3300))")"

umask 077
printf '{"token":"%s","expires_at":"%s","expires_epoch":%s}\n' \
  "$token" "$expires" "$exp_epoch" > "$CACHE"
printf '%s\n' "$token"
