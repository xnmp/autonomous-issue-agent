# Approved Phase 5 timing amendment

Current status: bounded-window and standing accounting approvals cover this
five-period allocation. The [execution record](2026-10-05-phase-5-live-execution.md)
records completed recovery, activation, qualification, canary, full tests and
hosted CI. Independent review accepted advancing the four existing calendars
from the original 23:00/23:45 schedule to the times below. This preserves the
USD 125 combined new-admission bound, configured profile, all USD 25 caps and
two complete 48-hour intervals. The obsolete canary timer is stopped. Window 1's
future stop is armed; its start requires the final evidence freeze. Both window-2
timers remain inactive until the first stopped readout is adjudicated.

## Reason and concrete schedule

The installed role units allow 90 seconds for control-group shutdown. Stopping
at 23:58 before a midnight endpoint shortens continuous operation and cannot
prove the approved full 48-hour acceptance. Stopping at midnight preserves the
nominal interval, but a delayed stop or new admission while draining can enter
another UTC budget period. Timer accuracy controls coalescing; it does not
guarantee exact execution. The installed version is systemd 262; see its
[timer contract](https://github.com/systemd/systemd/blob/v262/man/systemd.timer.xml)
and [service shutdown contract](https://github.com/systemd/systemd/blob/v262/man/systemd.service.xml).

Use the existing services and fixed-date systemd start/stop timers:

| Stage | Accepted UTC interval or cutoff |
| --- | --- |
| One-tick canary | Completed October 5, 10:59–11:04; obsolete 22:55 timer stopped |
| Window 1 | October 5 14:30 through October 7 14:30; exactly 48 hours |
| Stopped readout | October 7 after all roles and children stop; finish before the next release |
| Window 2 | Conditional October 7 15:15 through October 9 15:15; exactly 48 hours |

Request simultaneous stops of all three roles **at each window endpoint**,
then retain the existing bounded cancellation, draining and receipts. The
90-second shutdown bound fits within the same-day margins at both endpoints. Record actual timing;
the nominal schedule alone proves neither coverage nor stopping.

Window 1 spans the October 5, 6 and 7 budget periods. Window 2 spans October
7, 8 and 9. The canary shares October 5 and both windows share October 7.
These are five distinct USD 25 global periods: **USD 125 maximum aggregate
new configured-card admissions**, under the existing shared-authority and
no-extra-period conditions. Each window's standalone maximum becomes USD 75,
rather than USD 50; those maxima cannot be added because October 7 is shared.
Historical unknown exposure remains outside this new-admission bound.

## Release and evidence conditions

- Keep the Linux/x86_64 Codex profile, models, queue policy and all eight
  approved configuration overrides. Keep keifu stopped and boot autostart
  disabled. Preserve sourced historical holds and accounting evidence.
- Complete the retained-obligation decision, live activation, current-target
  qualification and source/configuration/cohort freeze before arming a start.
- Arm and read back the correct future end timer before its corresponding
  start. Set `AccuracySec=1us`, `RandomizedDelaySec=0`,
  `RandomizedOffsetSec=0` and `Persistent=false`. Never catch up a missed start.
- The canary uses `ISSUE_AGENT_MAX_TICKS=1`, with automatic restart and forced
  reload restart disabled for that temporary run. One tick does not replace
  its absolute stop timer. Restore the ordinary units before window 1.
- Leave window 2 unarmed until the first stopped readout, halt adjudication and
  unchanged-profile check finish. Forty-five minutes is available, not assumed
  sufficient. If readiness misses October 7 15:15, postpone and recalculate the five-period
  bound before release; never compress or skip readout.
- Preserve the no-touch rule. Partial starts, clock/host disruption, missed
  stops or post-deadline paid admissions invalidate the coverage claim. Record
  start/ready times, stop requests, last admissions, final settlements, empty
  process groups and actual coverage. The same-day margin protects the period
  bound; it does not excuse an admission after the observation deadline.
- Export all admitted bindings and shared/unassigned spend, inherited work,
  holds and interventions. Retain the three-merge target and explicitly report
  unmet targets, missing comparable baseline and unknown recurrence coverage.

The exact offline unit drafts and hash manifest are at
`.worktrees/phase5-preflight/earlier-release-controls/`. Host `systemd-analyze --user verify`
accepted all ten service/timer files. Independent review accepted the period
arithmetic and nominal intervals with the conditions above. The earlier
23:58-cutoff draft is classified as a shorter observation and must not be
installed as completed Phase 5 acceptance. This amendment adds no production
code, controller, queue or worker. Actual live window evidence remains required.

Dates are prospective; actual runtime evidence remains required. A missed first
start requires a replacement exact schedule and five-period calculation before
release. The retained-obligation dispositions were already approved and applied;
historical actual spend remains unknown.
