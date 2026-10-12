"""Which credential each role runs on, and whose rate-limit pool it spends
(2026-08-02 keifu incidents).

Two config-class gaps doctor could not see:
  1. IDENTITY_MODE=three with FIX_BOT_TOKEN unset — the fixer silently acted as
     the operator's personal account and burned its GraphQL pool;
  2. fix and review on one credential at POLL_SECONDS=20 — one
     5,000-point/hour pool, two daemons, exhausted (issue #163).
"""

import argparse

import pytest

from issue_agent.app import cli
from issue_agent.app.identity_pools import (
    BURN_WARN_FRACTION,
    DEFAULT_POLL_SECONDS,
    GRAPHQL_HOURLY_POINTS,
    WORLD_BUILD_GRAPHQL_POINTS,
    estimated_points_per_hour,
    identity_pool_report,
    parse_poll_seconds,
    resolve_role_identities,
)


def pool_of(report, role):
    return next(r for r in report.roles if r.role == role)


def messages(report, level=None):
    return [
        f.message for f in report.findings if level is None or f.level == level
    ]


def fires_about(report, *needles, level=None):
    """Is there a finding naming all of `needles`? Behavioural: the check exists
    to TELL the operator something, so an assertion on it must read the text."""
    return any(
        all(n in m for n in needles) for m in messages(report, level)
    )


DISTINCT = {
    "IDENTITY_MODE": "three",
    "FIX_BOT": "human-operator",
    "FIX_BOT_TOKEN": "fix-secret",
    "REVIEW_BOT_TOKEN": "review-secret",
    "POLL_SECONDS": "60",
}

# fix and review resolve to one credential, so they spend one pool.
SHARED = {
    "IDENTITY_MODE": "three",
    "FIX_BOT": "human-operator",
    "FIX_BOT_TOKEN": "one-pat",
    "REVIEW_BOT_TOKEN": "one-pat",
    "POLL_SECONDS": "60",
}


# ── incident 1: the fixer falls through to ambient ────────────────────────────
def test_three_identity_mode_without_a_fixer_token_names_the_account_and_the_cost():
    report = identity_pool_report({
        "IDENTITY_MODE": "three",
        "FIX_BOT": "human-operator",
        "REVIEW_BOT_TOKEN": "review-secret",
        "POLL_SECONDS": "60",
    })
    assert pool_of(report, "fix").ambient
    assert fires_about(
        report, "FIX_BOT_TOKEN", "human-operator", "attributed", level="warn"
    ), messages(report)
    assert not report.healthy


def test_fixer_fallthrough_names_gh_auth_when_no_fixer_login_is_configured():
    report = identity_pool_report({"IDENTITY_MODE": "three", "POLL_SECONDS": "600"})
    assert fires_about(report, "gh auth login", level="warn")


def test_a_configured_fixer_token_is_silent_about_attribution():
    report = identity_pool_report(DISTINCT)
    assert not pool_of(report, "fix").ambient
    assert not fires_about(report, "FIX_BOT_TOKEN is not set")


def test_solo_mode_does_not_demand_a_fixer_token():
    """solo mode has no bot identities to attribute to — the human IS the
    reviewer, so ambient is the documented arrangement, not a finding."""
    report = identity_pool_report({
        "IDENTITY_MODE": "solo", "FIX_BOT": "human-operator", "POLL_SECONDS": "600",
    })
    assert pool_of(report, "fix").ambient
    assert not fires_about(report, "FIX_BOT_TOKEN is not set")


@pytest.mark.parametrize(
    "mode",
    [
        "three", " three ",            # the mode itself, trimmed
        None, "", "   ",               # absent/empty → the THREE default
        "Solo", "SOLO", "sOlO",        # case-SENSITIVE: these are NOT solo
        "Three", "THREE", "banana",    # unrecognised → the THREE default
    ],
)
def test_every_value_that_leaves_the_harness_in_three_mode_is_checked(mode):
    """`IdentityMode.from_env` is deliberately case-sensitive — the bash half
    compares `[ "$IDENTITY_MODE" = three ]` literally — and `config` defaults
    anything unrecognised to THREE. So `IDENTITY_MODE=Solo` really does run in
    three-identity mode. Folding case here would hand a clean bill of health to
    exactly the config that reproduces the keifu incident."""
    env = {"POLL_SECONDS": "600"}
    if mode is not None:
        env["IDENTITY_MODE"] = mode
    report = identity_pool_report(env)
    assert fires_about(report, "FIX_BOT_TOKEN is not set", level="warn")


@pytest.mark.parametrize("mode", ["solo", " solo ", "solo\n"])
def test_only_an_exactly_spelled_solo_suppresses_the_attribution_finding(mode):
    report = identity_pool_report({"IDENTITY_MODE": mode, "POLL_SECONDS": "600"})
    assert not fires_about(report, "FIX_BOT_TOKEN is not set")


# ── incident 2: shared pool and burn estimate ─────────────────────────────────
def test_a_shared_pool_at_60s_is_information_not_a_warning():
    report = identity_pool_report(SHARED)
    assert fires_about(report, "fix + review", "1,680 points/hour", level="info")
    assert not messages(report, "warn")
    assert report.healthy


def test_a_credential_per_role_at_a_sane_cadence_is_completely_silent():
    report = identity_pool_report({
        "IDENTITY_MODE": "three",
        "FIX_BOT_TOKEN": "fix-secret",
        "REVIEW_BOT_TOKEN": "review-secret",
        "POLL_SECONDS": "600",
    })
    assert not report.shared_pools
    assert not messages(report, "warn") and not messages(report, "error")
    assert report.healthy


def test_the_burn_estimate_names_its_assumptions():
    """A number an operator cannot re-derive is a number they cannot act on."""
    report = identity_pool_report({**SHARED, "POLL_SECONDS": "20"})
    assert fires_about(
        report, "2 roles", "180 ticks/hour",
        f"{WORLD_BUILD_GRAPHQL_POINTS} points per world build",
        f"{GRAPHQL_HOURLY_POINTS:,} GraphQL points/hour",
    )


def test_the_estimate_declares_itself_an_upper_bound_and_says_why():
    """Overclaiming a modelled number as a measured one is how a check earns its
    way onto an operator's ignore list. Both sources of slack are named: the 14
    points is the FIXER's world build, and the ADR 0010 probe answers unchanged
    ticks out of the separate REST bucket."""
    report = identity_pool_report({**SHARED, "POLL_SECONDS": "20"})
    assert fires_about(report, "Upper bound at POLL_SECONDS=20")
    assert fires_about(report, "REST quota — a separate bucket")


def test_only_the_warning_level_prescribes_a_remedy():
    hot = identity_pool_report({**SHARED, "POLL_SECONDS": "20"})
    cool = identity_pool_report(SHARED)
    assert fires_about(hot, "Raise POLL_SECONDS")
    assert not fires_about(cool, "Raise POLL_SECONDS")


# ── pool identity: unset, ambient, and duplicated secrets ─────────────────────
def test_two_unset_tokens_share_a_pool_because_both_fall_through_to_ambient():
    """The grouping is on the RESOLVED credential, not on "both vars are empty".
    Both ambient roles really are one pool and the finding must say so."""
    report = identity_pool_report({"IDENTITY_MODE": "solo", "POLL_SECONDS": "20"})
    assert [p.roles for p in report.shared_pools] == [("fix", "review")]
    assert fires_about(
        report, "fix + review", "all fall through to the ambient",
        "5,040 points/hour", level="warn",
    ), messages(report)


def test_an_unset_fixer_token_alongside_a_configured_reviewer_is_two_pools():
    """Ambient and REVIEW_BOT_TOKEN are different credentials; treating "unset"
    as equal to a real token would invent a shared pool that does not exist."""
    report = identity_pool_report({
        "IDENTITY_MODE": "solo", "REVIEW_BOT_TOKEN": "review-secret",
        "POLL_SECONDS": "20",
    })
    assert not report.shared_pools


def test_the_same_secret_in_two_vars_is_one_pool_and_is_named_as_such():
    report = identity_pool_report({
        "IDENTITY_MODE": "three",
        "FIX_BOT_TOKEN": "one-pat",
        "REVIEW_BOT_TOKEN": "one-pat",
        "POLL_SECONDS": "20",
    })
    assert [p.roles for p in report.shared_pools] == [("fix", "review")]
    assert fires_about(
        report, "FIX_BOT_TOKEN = REVIEW_BOT_TOKEN", "one and the same secret",
    ), messages(report)


def test_no_report_field_carries_the_secret_or_a_digest_that_confirms_it():
    """A repr of the report can reach a log or an event. The pool key must
    therefore be neither the secret nor an unsalted digest of it — the latter is
    a confirmation oracle for anyone holding a candidate PAT, and correlates the
    same credential across deployments."""
    import hashlib

    secret = "ghp_supersecret_value"
    env = {
        "IDENTITY_MODE": "three", "FIX_BOT_TOKEN": secret,
        "REVIEW_BOT_TOKEN": secret, "POLL_SECONDS": "20",
    }
    report = identity_pool_report(env)
    text = repr(report)
    assert secret not in text
    assert hashlib.sha256(secret.encode()).hexdigest()[:12] not in text
    # …while still answering the only question the key exists for.
    assert len({role.pool for role in report.roles}) == 1


@pytest.mark.parametrize("blank", ["   ", "\t", "\n"])
def test_a_whitespace_only_token_is_an_error_not_a_silent_fallback(blank):
    """`main` tests FIX_BOT_TOKEN with `or None`, so whitespace is CONFIGURED —
    it goes out as a bearer token and there is no fallback behind it."""
    report = identity_pool_report({
        "IDENTITY_MODE": "three", "FIX_BOT_TOKEN": blank, "POLL_SECONDS": "600",
    })
    assert fires_about(report, "FIX_BOT_TOKEN is set to whitespace", level="error")
    assert not report.healthy


# ── the App branch mirrors main._app_identity ─────────────────────────────────
def test_the_app_identity_is_used_only_when_its_key_file_is_readable():
    env = {
        "IDENTITY_MODE": "three", "FIX_BOT_TOKEN": "fix-secret",
        "GH_APP_ID": "1", "GH_APP_INSTALL_ID": "2",
        "GH_APP_KEY_FILE": "/keys/app.pem",
        "REVIEW_BOT_TOKEN": "review-secret", "POLL_SECONDS": "600",
    }
    live = resolve_role_identities(env, key_readable=lambda _: True)
    assert "GitHub App 1 (install 2)" in live[1].source

    missing = resolve_role_identities(env, key_readable=lambda _: False)
    assert missing[1].source == "REVIEW_BOT_TOKEN"


def test_the_app_key_is_resolved_against_the_daemon_home_not_doctors(tmp_path):
    """`GH_APP_KEY_FILE="~/..."` under a systemd unit expands against the SERVICE
    user. Expanding it against whoever ran `doctor` would report the App identity
    live while the daemons fall through to REVIEW_BOT_TOKEN, or the reverse."""
    service_home = tmp_path / "svc"
    (service_home / ".config").mkdir(parents=True)
    (service_home / ".config" / "app.pem").write_text("key")
    env = {
        "HOME": str(service_home),
        "GH_APP_ID": "1", "GH_APP_INSTALL_ID": "2",
        "GH_APP_KEY_FILE": "~/.config/app.pem",
        "REVIEW_BOT_TOKEN": "review-secret",
    }
    assert "GitHub App" in resolve_role_identities(env)[1].source
    assert resolve_role_identities({**env, "HOME": str(tmp_path / "someone-else")})[
        1
    ].source == "REVIEW_BOT_TOKEN"


def test_an_unreadable_app_key_with_no_bot_token_lands_on_ambient():
    """The exact fall-through main.py warns about: App configured, key absent,
    no static token — fix and review end up on the ambient credential."""
    roles = resolve_role_identities(
        {"GH_APP_ID": "1", "GH_APP_INSTALL_ID": "2", "GH_APP_KEY_FILE": "/gone.pem"},
        key_readable=lambda _: False,
    )
    assert all(role.ambient for role in roles)


@pytest.mark.parametrize(
    "env",
    [
        {"GH_APP_ID": "1", "GH_APP_INSTALL_ID": "2"},
        {"GH_APP_ID": "1", "GH_APP_KEY_FILE": "/keys/app.pem"},
        {"GH_APP_INSTALL_ID": "2", "GH_APP_KEY_FILE": "/keys/app.pem"},
        {"GH_APP_ID": "", "GH_APP_INSTALL_ID": "", "GH_APP_KEY_FILE": ""},
    ],
)
def test_a_partially_configured_app_is_not_an_app_identity(env):
    roles = resolve_role_identities(
        {**env, "REVIEW_BOT_TOKEN": "review-secret"}, key_readable=lambda _: True
    )
    assert roles[1].source == "REVIEW_BOT_TOKEN"


# ── POLL_SECONDS parsing ──────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw,expected,valid",
    [
        ("20", 20, True),
        (" 45 ", 45, True),
        ("abc", DEFAULT_POLL_SECONDS, False),
        ("", DEFAULT_POLL_SECONDS, False),
        (None, DEFAULT_POLL_SECONDS, False),
        ("   ", DEFAULT_POLL_SECONDS, False),
        ("0", DEFAULT_POLL_SECONDS, False),
        ("-5", DEFAULT_POLL_SECONDS, False),
        ("20.5", DEFAULT_POLL_SECONDS, False),
        ("1" * 400, int("1" * 400), True),
    ],
)
def test_poll_seconds_parsing_never_raises_on_operator_input(raw, expected, valid):
    assert parse_poll_seconds(raw) == (expected, valid)


def test_a_malformed_poll_seconds_still_produces_an_estimate_and_says_so():
    """An unparseable cadence must not delete the check that depends on it."""
    report = identity_pool_report({**SHARED, "POLL_SECONDS": "abc"})
    assert report.poll_seconds == DEFAULT_POLL_SECONDS
    assert fires_about(report, "'abc' is not a positive integer", "5,040 points/hour")


def test_an_absent_poll_seconds_says_it_assumed_the_default():
    env = {k: v for k, v in SHARED.items() if k != "POLL_SECONDS"}
    assert fires_about(identity_pool_report(env), "POLL_SECONDS is not set")


def test_a_non_string_poll_seconds_does_not_traceback():
    """`doctor` and the wizard may never crash on a config value, and `env` is
    not typed tightly enough to rule an int out."""
    assert parse_poll_seconds(45) == (45, True)
    assert parse_poll_seconds(0) == (DEFAULT_POLL_SECONDS, False)
    report = identity_pool_report({"IDENTITY_MODE": "three", "POLL_SECONDS": 60})
    assert report.poll_seconds == 60


def test_a_small_share_of_the_pool_is_not_rounded_away_to_zero_percent():
    """A '0%' printed beside a non-zero points/hour reads as a broken estimate."""
    report = identity_pool_report({**SHARED, "POLL_SECONDS": "3600"})
    assert fires_about(report, "0.6% of the pool"), messages(report)


def test_a_cadence_longer_than_an_hour_still_costs_one_build():
    """A daemon always builds its first world; zero would understate the floor."""
    assert estimated_points_per_hour(1, 99999) == WORLD_BUILD_GRAPHQL_POINTS
    assert estimated_points_per_hour(0, 20) == 0


def test_the_warn_threshold_is_a_fraction_of_the_documented_pool():
    below = estimated_points_per_hour(2, 65)
    assert below / GRAPHQL_HOURLY_POINTS <= BURN_WARN_FRACTION
    report = identity_pool_report({**SHARED, "POLL_SECONDS": "65"})
    assert not messages(report, "warn")


# ── doctor / wizard wiring ────────────────────────────────────────────────────
def reported(capsys) -> str:
    """Everything the operator sees. `ok`/`info` print to stdout and
    `warn`/`err` to stderr, so a check that only read one stream could pass
    while the finding it exists for was never emitted."""
    captured = capsys.readouterr()
    return captured.out + captured.err


def test_doctor_prints_a_line_per_role_and_every_finding(capsys):
    cli.report_identity_pools({
        "IDENTITY_MODE": "three", "FIX_BOT": "human-operator",
        "POLL_SECONDS": "20",
    })
    out = reported(capsys)
    assert "identity: fix → ambient `gh auth` credential" in out
    assert "identity: review → ambient `gh auth` credential" in out
    assert "FIX_BOT_TOKEN is not set" in out
    assert "share ONE GitHub rate-limit pool" in out


def test_doctor_reads_the_composed_snapshot_the_daemons_will_see(tmp_path, capsys):
    """The finding must describe AGENT_ENV, not the operator's shell — a doctor
    run that reports on the wrong env is the false green this check replaces."""
    snapshot = tmp_path / "active.env"
    snapshot.write_text('IDENTITY_MODE="three"\nFIX_BOT="from-snapshot"\n')
    cli._doctor_identity_pools(
        cli._composed_env({"AGENT_ENV": str(snapshot), "FIX_BOT": "from-shell"})
    )
    assert "'from-snapshot'" in reported(capsys)


def test_doctor_exits_nonzero_on_an_error_finding_even_when_doctor_sh_is_green(
    tmp_path, monkeypatch
):
    """doctor is used as a GATE (bin/point-at.sh, CI). A present-but-unusable
    fixer token 401s every call with no fallback; printing it red while exiting 0
    is the false green these checks exist to remove."""
    assert _doctor_rc(tmp_path, monkeypatch, {"FIX_BOT_TOKEN": "   "}, sh_rc=0) == 1


def test_doctor_stays_green_for_warning_level_identity_findings(tmp_path, monkeypatch):
    """The two incident checks are WARNINGS: the harness runs, it is just wrongly
    attributed or over budget. Failing the gate on them would block deployments
    that are merely suboptimal."""
    assert _doctor_rc(tmp_path, monkeypatch, {}, sh_rc=0) == 0


def test_doctor_preserves_a_failing_doctor_sh_exit_code(tmp_path, monkeypatch):
    assert _doctor_rc(tmp_path, monkeypatch, {"FIX_BOT_TOKEN": "   "}, sh_rc=2) == 2


def _doctor_rc(tmp_path, monkeypatch, extra: dict, *, sh_rc: int) -> int:
    # Identity findings are independent of the host's entrypoint installation.
    monkeypatch.setattr(cli, "SHARED_ENV", str(tmp_path / "absent.env"))
    for independent_check in ("entrypoint_drift", "check_overlay_mismatch"):
        monkeypatch.setattr(cli, independent_check, lambda *a, **k: "")
    monkeypatch.setattr(cli, "_resolve_context", lambda _a: ("o/r", str(tmp_path), "r"))
    monkeypatch.setattr(
        cli, "_composed_env", lambda _env: {"IDENTITY_MODE": "three", **extra}
    )
    monkeypatch.setattr(
        cli.subprocess, "run", lambda *a, **k: argparse.Namespace(returncode=sh_rc)
    )
    return cli.cmd_doctor(argparse.Namespace(name=None, repo=None))


def test_an_unsourceable_snapshot_falls_back_to_the_inherited_environment():
    env = {"AGENT_ENV": "/nonexistent/active.env", "IDENTITY_MODE": "three"}
    assert cli._composed_env(env) == env


def test_the_wizard_surfaces_the_same_two_facts_at_config_time(capsys):
    cli._wizard_pool_summary([
        ("IDENTITY_MODE", "three"),
        ("FIX_BOT", "human-operator"),
    ])
    out = reported(capsys)
    assert "FIX_BOT_TOKEN is not set" in out
    assert "fix + review" in out
    assert "POLL_SECONDS is not set" in out


def test_the_wizard_stops_flagging_attribution_once_a_fixer_token_is_collected(capsys):
    cli._wizard_pool_summary([
        ("IDENTITY_MODE", "three"),
        ("FIX_BOT_TOKEN", "fix-secret"),
        ("REVIEW_BOT_TOKEN", "review-secret"),
    ])
    out = reported(capsys)
    assert "FIX_BOT_TOKEN is not set" not in out
    assert "share ONE GitHub rate-limit pool" not in out
