#!/usr/bin/env bash
# PostToolUse(Read) hook. When the agent Reads an evidence screenshot (an image
# under evidence/), it has loaded the pixels into its vision context — record the
# basename in the harness-owned receipt so the PR gate can require captured
# screenshots were actually LOOKED AT (check_visual_evidence).
#
# Observe-only: always exit 0 (never blocks). Reads the hook JSON on stdin.
set -uo pipefail

payload=$(cat)

# Extract tool_input.file_path and cwd without a hard jq dependency (python3 is
# already required by the SDK environment). Fail open on any parse error.
read -r fp cwd < <(printf '%s' "$payload" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    fp = (d.get("tool_input") or {}).get("file_path", "") or ""
    cwd = d.get("cwd", "") or "."
    print(fp, cwd)
except Exception:
    print("", ".")
' 2>/dev/null) || exit 0

[ -n "$fp" ] || exit 0

# image under evidence/ ?
low=$(printf '%s' "$fp" | tr '[:upper:]' '[:lower:]')
case "$low" in
  *evidence/*.png|*evidence/*.jpg|*evidence/*.jpeg|*evidence/*.gif|*evidence/*.webp) ;;
  *) exit 0 ;;
esac

[ -n "${HARNESS_SCREENSHOTS_READ:-}" ] || exit 0
mkdir -p "$(dirname "$HARNESS_SCREENSHOTS_READ")" 2>/dev/null || exit 0
printf '%s\n' "$(basename "$fp")" >> "$HARNESS_SCREENSHOTS_READ" 2>/dev/null || exit 0
exit 0
