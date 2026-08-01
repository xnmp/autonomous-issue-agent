# Duplicate-launcher detection shared by point-at.sh and its regression tests.
#
# Prints the pids of OTHER live point-at launchers for the given repo name,
# one per line; prints nothing when this process is the only launcher.
#
# Excluding $$/$PPID is NOT enough: `$(...)`, pipelines and `&&` lists all fork
# subshells that INHERIT our argv, so /proc shows them as extra
# "point-at.sh <name>" processes under fresh pids. Matching those made the
# guard fire on literally every first run. Ancestors bite the same way from
# the other direction: `systemd-inhibit ... bash point-at.sh <name>` or
# `sh -c '... point-at.sh <name>'` puts the launch string in every wrapper's
# argv, so the guard saw its own grandparent and refused the first run
# (observed 2026-08-01). A genuine second launcher shares neither our ancestor
# chain nor us as a parent, so drop candidates in our own lineage — and any
# that exited between pgrep and the check, since a dead process is not a
# running duplicate.
duplicate_launcher_pids() {
  _dlg_name="$1"
  _dlg_lineage=" $$ "
  _dlg_a="$PPID"
  while [ -n "$_dlg_a" ] && [ "$_dlg_a" != "0" ] && [ "$_dlg_a" != "1" ]; do
    _dlg_lineage="${_dlg_lineage}${_dlg_a} "
    _dlg_a="$(ps -o ppid= -p "$_dlg_a" 2>/dev/null | tr -d '[:space:]' || true)"
  done
  for _dlg_p in $(pgrep -f "point-at\.sh ${_dlg_name}([[:space:]]|\$)" 2>/dev/null || true); do
    # In our own ancestor chain ⇒ a wrapper quoting the launch command, not a
    # second launcher. Only the candidate itself is lineage-checked: a genuine
    # duplicate started from the SAME terminal shares an ancestor with us, so
    # excluding parents-in-lineage would hide it.
    case "$_dlg_lineage" in *" $_dlg_p "*) continue ;; esac
    _dlg_pp="$(ps -o ppid= -p "$_dlg_p" 2>/dev/null | tr -d '[:space:]' || true)"
    # empty ⇒ exited between pgrep and now (not a live duplicate);
    # $$ ⇒ a subshell we forked (inherits our argv), not a launcher.
    if [ -z "$_dlg_pp" ] || [ "$_dlg_pp" = "$$" ]; then
      continue
    fi
    printf '%s\n' "$_dlg_p"
  done
}
