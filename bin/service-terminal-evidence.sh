#!/bin/sh
# Journal record for one systemd role invocation, run as ExecStopPost.
#
# The manager supplies the result fields. ExecStopPost runs inside the unit's
# own cgroup, so that cgroup's hierarchical memory.events carries the unit's
# OOM counters for the whole invocation, including a kill that took the daemon
# down before it could log anything. The earlier exit 137 and multi-service
# stop could not be attributed for lack of exactly this evidence (Phase 5
# commissioning assessment). Values that cannot be read or parsed print as
# "unknown": a missing counter is an evidence gap, never zero.
#
# Usage: service-terminal-evidence.sh [cgroup-dir]

cgroup_dir=${1:-}
if [ -z "$cgroup_dir" ]; then
    relative=$(sed -n 's/^0:://p' /proc/self/cgroup 2>/dev/null)
    [ -n "$relative" ] && cgroup_dir="/sys/fs/cgroup$relative"
fi

# Kernel memory counters are u64: at most 20 digits. Anything else is unknown,
# never a truncated number.
u64() {
    case $1 in
        '' | *[!0-9]*) echo unknown ;;
        *) if [ ${#1} -le 20 ]; then echo "$1"; else echo unknown; fi ;;
    esac
}

counter() {
    value=
    if [ -n "$cgroup_dir" ] && [ -r "$cgroup_dir/memory.events" ]; then
        value=$(awk -v key="$1" '$1 == key && NF == 2 {print $2; exit}' \
            "$cgroup_dir/memory.events" 2>/dev/null)
    fi
    u64 "$value"
}

peak=''
if [ -n "$cgroup_dir" ] && [ -r "$cgroup_dir/memory.peak" ]; then
    peak=$(head -c 64 "$cgroup_dir/memory.peak" 2>/dev/null | tr -d '\n')
fi

printf 'issue_agent_service_terminal invocation=%s result=%s exit_code=%s exit_status=%s oom=%s oom_kill=%s memory_peak=%s\n' \
    "${INVOCATION_ID:-}" "${SERVICE_RESULT:-}" "${EXIT_CODE:-}" "${EXIT_STATUS:-}" \
    "$(counter oom)" "$(counter oom_kill)" "$(u64 "$peak")"
