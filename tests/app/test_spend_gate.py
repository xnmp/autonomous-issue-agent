"""The daily spend caps: which one tripped, over whose spend (#32), and the
pre-observe tick gate that reads them (#27)."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.adapters.journal import sum_today
from issue_agent.app.spend_gate import (
    build_daily_spend_gate,
    daily_cap_breach,
    read_daily_cap_breach,
)

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)


def _ledger(tmp_path, *entries) -> str:
    path = str(tmp_path / "spend.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")
    return path


def _spend(cost, repo="o/r", at=NOW, **extra):
    e = {"ts": at.isoformat(), "event": "spend", "issue": 5, "cost_usd": cost}
    if repo is not None:
        e["repo"] = repo
    e.update(extra)
    return e


# ── sum_today scoping (#32) ───────────────────────────────────────────────────
def test_sum_today_scoped_to_a_repo_ignores_its_neighbours_spend(tmp_path):
    """The bug that motivated #32: keifu had spent $0 and was blocked outright on
    tauri-explorer's $709, because the knob is per-repo but the ledger is not."""
    path = _ledger(
        tmp_path,
        _spend(709.55, repo="xnmp/tauri-explorer"),
        _spend(2.0, repo="xnmp/keifu"),
    )
    assert sum_today(path, lambda: NOW, repo="xnmp/keifu") == pytest.approx(2.0)
    assert sum_today(path, lambda: NOW, repo="xnmp/tauri-explorer") == pytest.approx(709.55)
    # None = every repo, which is what a genuinely global cap wants.
    assert sum_today(path, lambda: NOW) == pytest.approx(711.55)


def test_an_entry_without_a_repo_counts_globally_but_charges_no_deployment(tmp_path):
    """Legacy/truncated lines are still real money off the box, but attributing
    them to whichever deployment happens to ask would be a fabrication."""
    path = _ledger(tmp_path, _spend(4.0, repo=None), _spend(1.0, repo="o/r"))
    assert sum_today(path, lambda: NOW, repo="o/r") == pytest.approx(1.0)
    assert sum_today(path, lambda: NOW) == pytest.approx(5.0)


def test_scoping_still_respects_the_utc_day_window(tmp_path):
    path = _ledger(
        tmp_path,
        _spend(9.0, at=NOW - timedelta(days=1)),
        _spend(3.0, at=NOW),
    )
    assert sum_today(path, lambda: NOW, repo="o/r") == pytest.approx(3.0)


def test_a_malformed_ledger_line_does_not_break_a_scoped_sum(tmp_path):
    path = str(tmp_path / "spend.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{not json\n")
        fh.write(json.dumps(_spend(2.0)) + "\n")
        fh.write("\n")
    assert sum_today(path, lambda: NOW, repo="o/r") == pytest.approx(2.0)


# ── daily_cap_breach (pure) ───────────────────────────────────────────────────
def _breach(**kw):
    args = dict(repo="o/r", repo_spent=0.0, repo_cap=0.0,
                global_spent=0.0, global_cap=0.0)
    args.update(kw)
    return daily_cap_breach(**args)


def test_no_breach_under_both_caps():
    assert _breach(repo_spent=10.0, repo_cap=50.0,
                   global_spent=100.0, global_cap=700.0) is None


def test_zero_disables_a_cap_however_large_the_spend():
    assert _breach(repo_spent=9_999.0, repo_cap=0.0,
                   global_spent=9_999.0, global_cap=0.0) is None


def test_a_cap_trips_exactly_at_the_boundary():
    assert _breach(repo_spent=50.0, repo_cap=50.0) is not None


def test_the_repo_cap_names_the_repo_and_the_per_repo_knob():
    b = _breach(repo="xnmp/keifu", repo_spent=60.0, repo_cap=50.0)
    assert b.scope == "daily-repo"
    assert "xnmp/keifu" in b.message and "DAILY_SPEND_CAP_USD" in b.message
    assert "GLOBAL_DAILY_SPEND_CAP_USD" not in b.message
    assert "00:00 UTC" in b.message and "no labels changed" in b.message


def test_the_global_cap_names_the_global_knob():
    b = _breach(global_spent=800.0, global_cap=700.0)
    assert b.scope == "daily-global"
    assert "GLOBAL_DAILY_SPEND_CAP_USD" in b.message and "ALL REPOS" in b.message


def test_the_global_cap_is_reported_first_when_both_are_tripped():
    """Raising this deployment's overlay knob will not clear a box-wide halt, so
    naming the per-repo cap there sends the operator to the wrong file."""
    b = _breach(repo_spent=60.0, repo_cap=50.0, global_spent=800.0, global_cap=700.0)
    assert b.scope == "daily-global" and b.cap == 700.0


def test_the_block_key_carries_the_day_the_scope_and_the_cap():
    """The dedup must expire with the condition (it heals at 00:00 UTC) and must
    distinguish the two scopes, or the second cap to trip would be silent."""
    repo_key = _breach(repo_spent=60.0, repo_cap=50.0).block_key("2026-07-28")
    global_key = _breach(global_spent=800.0, global_cap=700.0).block_key("2026-07-28")
    assert repo_key != global_key
    assert "2026-07-28" in repo_key and "50" in repo_key
    assert repo_key != _breach(repo_spent=60.0, repo_cap=50.0).block_key("2026-07-29")


# ── read_daily_cap_breach (over a real ledger) ────────────────────────────────
def _read(path, **kw):
    args = dict(repo="o/r", repo_cap=0.0, global_cap=0.0, now_fn=lambda: NOW)
    args.update(kw)
    return read_daily_cap_breach(path, **args)


def test_the_repo_cap_is_read_against_this_repos_spend_only(tmp_path):
    path = _ledger(tmp_path, _spend(709.55, repo="other/repo"), _spend(1.0, repo="o/r"))
    assert _read(path, repo_cap=50.0) is None, \
        "a neighbour's spend must not pause this deployment"


def test_the_global_cap_trips_even_when_this_repo_is_idle(tmp_path):
    path = _ledger(tmp_path, _spend(709.55, repo="other/repo"))
    assert _read(path, repo_cap=50.0, global_cap=700.0).scope == "daily-global"


def test_the_breach_clears_at_the_utc_rollover(tmp_path):
    """The whole reason the daily block changes no labels: it heals by itself."""
    path = _ledger(tmp_path, _spend(60.0, repo="o/r", at=NOW))
    assert _read(path, repo_cap=50.0) is not None
    assert _read(path, repo_cap=50.0, now_fn=lambda: NOW + timedelta(days=1)) is None


def test_neither_cap_armed_reads_nothing(tmp_path):
    path = _ledger(tmp_path, _spend(9_999.0))
    assert _read(path) is None


def test_an_empty_ledger_trips_nothing(tmp_path):
    assert _read(_ledger(tmp_path), repo_cap=50.0, global_cap=700.0) is None


def test_a_missing_ledger_file_trips_nothing(tmp_path):
    assert _read(str(tmp_path / "absent.jsonl"), repo_cap=50.0, global_cap=700.0) is None


def test_read_daily_cap_breach_without_a_ledger_path_is_inert():
    assert read_daily_cap_breach("", repo="o/r", repo_cap=50.0, global_cap=1.0) is None


# ── build_daily_spend_gate (the Daemon.tick_gate, #27) ────────────────────────
def test_the_gate_reports_the_block_message_while_a_cap_is_tripped(tmp_path):
    path = _ledger(tmp_path, _spend(60.0, repo="o/r"))
    gate = build_daily_spend_gate(
        path, repo="o/r", repo_cap=50.0, global_cap=0.0, now_fn=lambda: NOW
    )
    assert "DAILY_SPEND_CAP_USD" in gate()


def test_the_gate_clears_at_the_utc_rollover(tmp_path):
    """A gate that never cleared would be an off switch, not a pause."""
    path = _ledger(tmp_path, _spend(60.0, repo="o/r", at=NOW))
    clock = {"now": NOW}
    gate = build_daily_spend_gate(
        path, repo="o/r", repo_cap=50.0, global_cap=0.0, now_fn=lambda: clock["now"]
    )
    assert gate() is not None
    clock["now"] = NOW + timedelta(days=1)
    assert gate() is None


def test_no_gate_is_built_when_neither_cap_is_armed(tmp_path):
    """An unarmed daemon must not pay a ledger read per poll."""
    path = _ledger(tmp_path, _spend(9_999.0))
    assert build_daily_spend_gate(path, repo="o/r", repo_cap=0.0, global_cap=0.0) is None


def test_no_gate_is_built_without_a_ledger():
    assert build_daily_spend_gate("", repo="o/r", repo_cap=50.0, global_cap=0.0) is None
