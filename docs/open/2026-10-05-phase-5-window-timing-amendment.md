# Approved Phase 5 timing amendment

Current status: bounded-window and standing accounting approvals cover this
five-period allocation. The [execution record](2026-10-05-phase-5-live-execution.md)
records the completed preparation and two partial attempts. The October 5
14:30 release ended with a reboot; the replacement 23:00 release was stopped
on October 6 at 00:23:08 after a functional budget-recovery planner failure.
All role services and all window/canary timers are inactive. Neither attempt
supplies full 48-hour acceptance. The schedules below are cancelled historical
controls, not future authorization to catch up. Two fresh full intervals need
new period arithmetic and a final profile/source/cohort freeze, including the
requested configurable model tiers. Prior charges remain counted against the
approved aggregate bound; no allowance or held workflow is reset.

## Reason and cancelled historical schedule

The installed role units allow 90 seconds for control-group shutdown. Stopping
at 23:58 before a midnight endpoint shortens continuous operation and cannot
prove the approved full 48-hour acceptance. Stopping at midnight preserves the
nominal interval, but a delayed stop or new admission while draining can enter
another UTC budget period. Timer accuracy controls coalescing; it does not
guarantee exact execution. The installed version is systemd 262; see its
[timer contract](https://github.com/systemd/systemd/blob/v262/man/systemd.timer.xml)
and [service shutdown contract](https://github.com/systemd/systemd/blob/v262/man/systemd.service.xml).

The cancelled release used the existing services and fixed-date systemd timers:

| Stage | Accepted UTC interval or cutoff |
| --- | --- |
| One-tick canary | Completed October 5, 10:59–11:04; obsolete 22:55 timer stopped |
| Interrupted attempt | October 5 14:30 through 22:01:23; partial coverage only |
| Replacement attempt | October 5 23:00 through October 6 00:23:08; partial coverage only |
| Partial stopped readout | October 6 after all roles stopped; preserved SQL, journals and cohort exports |
| Cancelled window 2 | October 7 23:45 through October 9 23:45; never armed |

The approved protocol requires simultaneous stops of all three roles **at each future window endpoint**,
then retain the existing bounded cancellation, draining and receipts. The
90-second shutdown bound fits within the same-day margins at both endpoints. Record actual timing;
the nominal schedule alone proves neither coverage nor stopping.

The cancelled full-window schedule spanned the October 5, 6 and 7 budget periods, then October
7, 8 and 9. The canary and interrupted attempt share October 5; both full windows share October 7.
Those were five distinct USD 25 global periods: **USD 125 maximum aggregate
new configured-card admissions**, under the existing shared-authority and
no-extra-period conditions. Each cancelled window's standalone maximum was USD 75,
rather than USD 50; those maxima cannot be added because October 7 is shared.
Historical unknown exposure remains outside this new-admission bound.

## Conditions retained for a future release

- Freeze the accepted Linux/x86_64 runtime/model profile and accounting limits
  for both future windows after the requested tier configuration is complete.
  Keep keifu stopped and boot autostart disabled. Preserve historical holds
  and all prior charges; revised dates must retain an explicitly verified
  aggregate bound.
- Complete the retained-obligation decision, live activation, current-target
  qualification and source/configuration/cohort freeze before arming a start.
- Arm and read back the correct future end timer before its corresponding
  start. Set `AccuracySec=1us`, `RandomizedDelaySec=0`,
  `RandomizedOffsetSec=0` and `Persistent=false`. Never catch up a missed start.
- The completed canary used `ISSUE_AGENT_MAX_TICKS=1`, with automatic restart and forced
  reload restart disabled for that temporary run. Its historical absolute stop
  timer remains inactive; full windows require ordinary bounded supervision.
- Leave the second future window unarmed until the first complete stopped
  readout, halt adjudication and profile check finish. Do not assume a proposed
  gap is sufficient. Missed readiness requires postponement and new period
  arithmetic; never compress or skip the readout.
- Preserve the no-touch rule. Partial starts, clock/host disruption, missed
  stops or post-deadline paid admissions invalidate the coverage claim. Record
  start/ready times, stop requests, last admissions, final settlements, empty
  process groups and actual coverage. The same-day margin protects the period
  bound; it does not excuse an admission after the observation deadline.
- Export all admitted bindings and shared/unassigned spend, inherited work,
  holds and interventions. Retain the three-merge target and explicitly report
  unmet targets, missing comparable baseline and unknown recurrence coverage.

The exact offline unit drafts and hash manifest are at
`.worktrees/phase5-preflight/replacement-release-controls/`; the earlier controls
remain archived separately. Host verification and actual manager readback must
precede replacement admission. Independent review accepted the period
arithmetic and nominal intervals with the conditions above. The earlier
23:58-cutoff draft is classified as a shorter observation and must not be
installed as completed Phase 5 acceptance. This amendment adds no production
code, controller, queue or worker. Actual live window evidence remains required.

The calendars above are cancelled historical evidence. A future release requires
a replacement exact schedule and allocation calculation before admission;
actual runtime evidence remains required. The retained-obligation dispositions were already approved and applied;
historical actual spend remains unknown.
