# Approved Phase 5 timing amendment

Current status: bounded-window and standing accounting approvals cover this
five-period allocation. The [execution record](2026-10-05-phase-5-live-execution.md)
records the completed preparation and three partial attempts. The October 5
14:30 release ended with a reboot; the replacement 23:00 release was stopped
on October 6 at 00:23:08 after a functional budget-recovery planner failure.
The current-profile one-tick canary started at 02:32 UTC after qualification
and freeze, with the 02:40 UTC stop armed first. It stopped by 02:35 and
settled USD 2.582646. The mixed-profile attempt then ran from 03:00 to 03:27:09
and was stopped for publication and worker-authorization failures. Its
USD 6.215281 is settled; all paid roles and all five timers are off. No attempt
supplies full 48-hour acceptance. The schedules below are cancelled historical
controls, not future authorization to catch up. Known October 6 charges are
USD 10.738574; October 5/6 known charges total USD 31.259535. New future dates
and an allocation including preparation are required before arming a start.
Prior charges remain counted against the
approved aggregate bound; no allowance or held workflow is reset.

## Cancelled mixed-profile allocation — October 6

This superseded the older calendars below. The proposed first interval
was October 6 03:00 through October 8 03:00 UTC; the conditional second was
October 8 03:45 through October 10 03:45 UTC. Each is exactly 48 hours.
The first began but ended after 27 minutes; the second was never armed.
Both calendars are cancelled. The allocation below describes that release,
not a newly approved allowance. A future release requires the contract fixes,
qualification, freeze and new period arithmetic; it is never caught up.

There are five future UTC budget periods, October 6–10, plus the already-paid
October 5 period. The repository/global caps are now USD 20 per future day;
the issue cap remains USD 25, fix legs are bounded at USD 10 and review legs
at USD 5. October 6's known USD 4.523293, including the latest canary, is inside its
USD 20 allowance.
The combined ceiling is **USD 100 + USD 20.520961 = USD 120.520961**, within
the original approved USD 125 aggregate. The standalone windows each span
three USD 20 periods, so their USD 60 maxima cannot be added: October 8 is
shared. Other deployments remain stopped, and no new admission is permitted
in October 5 or October 11. Historical unknown spend remains unknown.

The four requested implementation tiers, preserved legacy PR transcript and
independent review/steward settings define a new cohort. The exact snapshot
version is `3d55a2921d284b9a`; its rendered version is `57d8e768fb21661a`.
Prior partial attempts remain separate evidence. Independent review accepted
this allocation and the existing stopped recovery path. Before lowering caps,
truthful current-USD-25 recovery cleared #755/#938; a separately recorded
operator disposition removed PR #997's budget labels and its current typed
marker. Ledger values and retry/infra counts were preserved. The two automatic
clears consumed their existing self-clear allowance once; no counter was reset.

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

### Reviewed future allocation, not armed — October 6

Known October 5 charges are USD 20.520961 and October 6 charges are USD 10.738574.
The prospective replacement uses an October 6 preparation cap of USD 13 and
five future UTC daily/global caps of USD 18: USD 123.520961 maximum admission
allocation including the earlier charges, leaving USD 1.479039 under USD 125.
October 6 preparation headroom is USD 2.261426; a proposed finite native
qualification uses at most four USD 0.25 quotes without automatic retries.
Unknown obligations stay retained and observed overrun charges remain counted.

Nominal full intervals are October 7 00:30–October 9 00:30 UTC and, conditionally,
October 9 01:15–October 11 01:15 UTC. Both include October 9, so the five shared
budget periods apply once. The second start requires the first full stopped
readout and adjudication. No preparation cap or new timer has been applied;
delayed readiness requires fresh dates, period arithmetic and source/profile
freezes. The cancelled calendars above cannot be reused or caught up.
