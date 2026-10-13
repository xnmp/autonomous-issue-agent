"""Config-from-env mapping."""

import subprocess
from pathlib import Path

import pytest

from dataclasses import fields

from issue_agent.app.config import HarnessConfig, from_env, generated_env_reference, render_agent_md
from issue_agent.app.main import _load_agent_env
from issue_agent.domain.effort import Effort


def test_metadata_backed_choices_drive_parsing_and_the_example():
    cfg = from_env({"REPO": "o/r", "FIX_BOT": "fix-bot", "MERGE_METHOD": "octopus"})
    assert cfg.merge_method == "squash"
    assert {error.split()[0] for error in cfg.parse_errors} >= {"MERGE_METHOD"}

    example = generated_env_reference()
    assert "ARM_ON_OPEN" not in example
    assert "CI_IGNORE_CHECKS" not in example
    assert 'MERGE_METHOD="squash"  # choices: squash|merge|rebase' in example


def test_reviewer_runtime_defaults_to_codex_at_the_fixer_tier_or_above():
    """The deployment default is visible in the task-producing config, not
    merely documented in an example overlay. Sol + $15 since 2026-08-09: the
    independent reviewer must be at least the fixer's tier, and the budget is
    sized for sol's token rates (terra-sized $8 clipped sol mid-review)."""
    cfg = from_env({"REPO": "x"})

    assert cfg.reviewer_runtime == "codex"
    assert cfg.review_model == "gpt-6.1-sol"
    assert cfg.review_budget_usd == 15.0


def test_every_operator_field_has_env_and_coercion_metadata():
    internal = {"agent_md", "control_authority_root", "codex_cost_per_mtok_cached_in", "parse_errors"}
    for definition in fields(HarnessConfig):
        if definition.name in internal:
            assert not definition.metadata
            continue
        assert definition.metadata["coerce"]


def test_checked_in_env_example_is_generated_from_metadata():
    path = Path(__file__).parents[2] / "config" / "agent.env.example"
    assert path.read_text() == generated_env_reference()


def test_render_agent_md_expands_and_strips():
    cfg = HarnessConfig(repo="o/harness", base_branch="dev")
    md = ("branch off $BASE_BRANCH, file on $HARNESS_REPO, issue $ISSUE_NUMBER\n"
          "$DIAGNOSE_RULES\n$LOGGING_RULES\n$DOC_RULES\n$TEST_RULES")
    out = render_agent_md(md, cfg)
    assert "$BASE_BRANCH" not in out and "dev" in out
    assert "$HARNESS_REPO" not in out and "o/harness" in out
    assert "$DIAGNOSE_RULES" not in out  # optional rule blocks stripped
    assert "$ISSUE_NUMBER" in out  # left literal — agent fills it per-issue


def test_rendered_agent_contract_names_the_configured_non_default_base():
    cfg = HarnessConfig(repo="o/harness", base_branch="canary/dashboard-e2e")
    rendered = render_agent_md((Path(__file__).parents[2] / "plugin" / "AGENT.md").read_text(), cfg)

    assert "running headless on a prepared issue branch" in rendered
    assert "The integration base is `canary/dashboard-e2e`" in rendered
    assert "$BASE_BRANCH" not in rendered
    assert "Never call `gh issue create`" in rendered
    assert "file an issue on" not in rendered


_VALID = {"REPO": "x", "FIX_BOT": "fix-bot"}
_NON_FINITE = ("nan", "NaN", "inf", "-inf", "Infinity", "1e400")


@pytest.mark.parametrize("knob,raw,field,value,flagged", [
    (None, None, "poll_seconds", 20, False),
    (None, None, "base_branch", "dev", False),
    ("FIX_MODEL", "opus", "fix_model", "opus", False),
    ("FIX_MODEL", "", "fix_model", None, False),
    # #16 escalation ladder: a valid token parses to the domain enum.
    ("FIX_EFFORT", "high", "fix_effort", Effort.HIGH, False),
    ("FIX_EFFORT", " Low ", "fix_effort", Effort.LOW, False),
    ("CODEX_COST_PER_MTOK_OUT", "15.00", "codex_cost_per_mtok_out", 15.0, False),
    ("REVIEW_BUDGET_USD", "2.5", "review_budget_usd", 2.5, False),
    ("MERGE_METHOD", "merge", "merge_method", "merge", False),
    # review M2: an exported-but-empty knob means UNSET (the field default),
    # never a different valid value; an explicit "0" is a real opt-out.
    ("AUTO_MERGE", "", "auto_merge", True, False),
    ("AUTO_MERGE", "0", "auto_merge", False, False),
    ("AUTO_MERGE", "off", "auto_merge", False, False),
    # An unrecognized token falls back to the default AND is reported, never
    # silently truthy (the old "anything not in the false set").
    ("AUTO_MERGE", "maybe", "auto_merge", True, True),
    ("AGENT_MAX_RUNTIME_SECONDS", "", "agent_max_runtime", 2700.0, False),
    ("AGENT_MAX_RUNTIME_SECONDS", "600", "agent_max_runtime", 600.0, False),
    ("AGENT_MAX_RUNTIME_SECONDS", "0", "agent_max_runtime", 0.0, False),  # the documented opt-out
    ("AGENT_MAX_RUNTIME_SECONDS", "nan", "agent_max_runtime", 2700.0, True),
    ("ISSUE_SPEND_CAP_USD", "", "issue_spend_cap_usd", 25.0, False),
    ("ISSUE_SPEND_CAP_USD", "0", "issue_spend_cap_usd", 0.0, False),
    ("ISSUE_SPEND_CAP_USD", "12.5", "issue_spend_cap_usd", 12.5, False),
    # P1R-9: nan passed float() and `value < 0`, silently disabling the cap it
    # names; 1e400 overflows to inf through an ordinary-looking spelling.
    *(("ISSUE_SPEND_CAP_USD", bad, "issue_spend_cap_usd", 25.0, True) for bad in _NON_FINITE),
    ("MAX_BUDGET_USD", "", "fix_budget_usd", 10.0, False),
    ("MAX_BUDGET_USD", "ten dollars", "fix_budget_usd", 10.0, True),
    # Construction never tracebacks on a malformed number (POLL_SECONDS=abc).
    ("POLL_SECONDS", "abc", "poll_seconds", 20, True),
    ("POLL_SECONDS", "45", "poll_seconds", 45, False),
])
def test_scalar_knobs_parse_absent_empty_and_malformed_values(knob, raw, field, value, flagged):
    c = from_env({**_VALID, **({knob: raw} if knob else {})})
    assert getattr(c, field) == value
    errors = c.validation_errors()
    if flagged:
        assert any(knob in e and raw in e for e in errors), errors
    else:
        assert errors == ()


@pytest.mark.parametrize("fix_bot,review_bot,flagged", [
    (None, None, True),  # P1R-7: an unset FIX_BOT used to sail through validate()
    ("   ", None, True),
    ("fix-bot", None, False),
    # review C1/D1: a raw compare misses fix-bot vs fix-bot[bot], a self-approving
    # pair GitHub rejects with a 422 on every approval; P1R-6: a trailing space
    # (shell round-trip) used to defeat the normalization.
    ("fix-bot", "fix-bot[bot]", True),
    ("fix-bot ", "fix-bot[bot]", True),
    ("fix-bot", "review-bot", False),
    (" fix-bot ", "review-bot", False),
])
def test_fix_bot_identity_validation(fix_bot, review_bot, flagged):
    env = {"REPO": "x", **({"FIX_BOT": fix_bot} if fix_bot is not None else {}),
           **({"REVIEW_BOT": review_bot} if review_bot else {})}
    errors = from_env(env).validation_errors()
    assert any("FIX_BOT" in e for e in errors) is flagged
    if review_bot and flagged:
        assert any("FIX_BOT" in e and "REVIEW_BOT" in e for e in errors)


def test_state_dirs_are_per_repo_case_insensitive_and_collision_free():
    """review M5/D7: two repos' daemons once shared one STATE_DIR and resumed each
    other's transcripts. R2-10: `owner/name -> owner-name` collided when a
    segment had a hyphen (acme/app-b vs acme-app/b); case variants of one
    GitHub repo must share a dir."""
    def dirs(repo):
        c = from_env({"REPO": repo})
        return c.state_dir, c.review_state_dir
    fix, review = dirs("acme/widget")
    assert fix.endswith("issue-agent/acme__widget") and review.endswith("review-agent/acme__widget")
    assert dirs("Acme/Widget") == dirs("acme/widget")
    for a, b in (("acme/app-b", "acme-app/b"), ("acme/widget", "acme/gizmo")):
        assert set(dirs(a)).isdisjoint(dirs(b))
    assert from_env({}).state_dir.endswith("issue-agent/unknown")
    assert from_env({"REPO": "acme/widget", "STATE_DIR": "/custom/state"}).state_dir == "/custom/state"
    # review S7: the dataclass default and from_env agree (one source).
    assert HarnessConfig(repo="x").poll_seconds == 20


def test_custom_state_roots_keep_one_machine_accounting_authority(monkeypatch):
    monkeypatch.setattr("issue_agent.app.config.machine_control_authority_root", lambda: "/machine/state")

    first = from_env({"REPO": "o/one", "STATE_DIR": "/srv/one/fix", "REVIEW_STATE_DIR": "/srv/one/review"})
    second = from_env({"REPO": "o/two", "STATE_DIR": "/var/lib/two/fix", "REVIEW_STATE_DIR": "/var/lib/two/review"})

    assert first.control_authority_root == second.control_authority_root == "/machine/state"


# Silent-default class (#50): an empty REPO must die at startup with a
# pointed message, not surface as an opaque gh error on the first poll.
@pytest.mark.parametrize("repository", (None, "github.com/o/r", "https://github.com/o/r", "o/r.git", "o//r",
                                        "owner_name/repo"))
def test_validate_rejects_an_empty_repository_and_alias_or_url_shapes(repository):
    with pytest.raises(ValueError, match="REPO"):
        from_env({} if repository is None else {"REPO": repository}).validate()


def test_describe_reports_effective_values_and_no_secrets():
    d = from_env({"REPO": "o/r", "POLL_SECONDS": "45", "FIX_BOT_TOKEN": "sekret-token"}).describe()
    assert "repo=o/r" in d
    assert "poll=45s" in d
    assert "sekret" not in d


def test_grouped_config_preserves_flat_env_mapping():
    c = from_env({
        "REPO": "o/r", "BASE_BRANCH": "main", "TARGET_DIR": "/repo", "STATE_DIR": "/state/fix",
        "REVIEW_STATE_DIR": "/state/review", "WORKTREE_ROOT": "/worktrees", "HARNESS_ROOT_DIR": "/harness",
        "FIX_BOT": "fixer", "MAX_BUDGET_USD": "4.25", "REVIEW_MODEL": "opus", "CODEX_BIN": "codex-dev",
        "CODEX_MODEL": "gpt-next",
    })

    assert (c.repo, c.base_branch, c.target_dir) == ("o/r", "main", "/repo")
    assert (c.state_dir, c.review_state_dir) == ("/state/fix", "/state/review")
    assert (c.worktree_root, c.harness_root) == ("/worktrees", "/harness")
    assert (c.fix_bot, c.fix_budget_usd, c.review_model) == ("fixer", 4.25, "opus")
    assert (c.codex_bin, c.codex_model) == ("codex-dev", "gpt-next")


@pytest.mark.parametrize("env,knobs", [
    ({"POLL_SECONDS": "0"}, {"POLL_SECONDS"}),
    ({"MAX_BUDGET_USD": "0", "REVIEW_BUDGET_USD": "-1"}, {"MAX_BUDGET_USD", "REVIEW_BUDGET_USD"}),
    ({"FIXER_RUNTIME": "python", "AGENT_IDLE_TIMEOUT_SECONDS": "-1", "AGENT_MAX_RUNTIME_SECONDS": "-1"},
     {"FIXER_RUNTIME", "AGENT_IDLE_TIMEOUT_SECONDS", "AGENT_MAX_RUNTIME_SECONDS"}),
    ({"FIX_DISPATCH_CONCURRENCY": "0"}, {"FIX_DISPATCH_CONCURRENCY"}),
    ({"FIX_EFFORT": "hihg"}, {"FIX_EFFORT"}),
])
def test_each_invalid_knob_is_flagged_exactly_once(env, knobs):
    # Membership and count, not the exact tuple or its order: a knob that
    # silently stopped being validated (or is validated twice) still fails.
    errors = from_env({"REPO": "x", "FIX_BOT": "fix-bot", **env}).validation_errors()
    assert {knob for knob in knobs if any(knob in e for e in errors)} == knobs
    assert len(errors) == len(knobs)
    with pytest.raises(ValueError, match=sorted(knobs)[0]):
        from_env({"REPO": "x", "FIX_BOT": "fix-bot", **env}).validate()


# ── P1R-8: `_b` must strip+casefold and reject unrecognized tokens ───────────
@pytest.mark.parametrize("token,expected", [
    *((token, False) for token in ("False", "FALSE", " false ", "No", "Off", "0", " 0 ")),
    *((token, True) for token in ("True", "TRUE", " true ", "Yes", "On", "1", " 1 ")),
])
def test_b_is_case_and_whitespace_insensitive(token, expected):
    # THE P1R-8 repro: AUTO_MERGE=False (capital F) used to compare unstripped
    # and case-sensitively against a raw ("0","false","no","off") tuple, miss
    # every one of them, and fall into "anything else is true" — turning
    # auto-merge ON when the operator asked for it OFF. Parametrized on one
    # knob per the acceptance note (full spelling set here, not every knob).
    assert from_env({"REPO": "x", "AUTO_MERGE": token}).auto_merge is expected


def test_codex_is_priced_out_of_the_box_so_the_spend_caps_are_not_blind():
    # A subscription bills $0, but the quota it draws down is real — so the
    # shipped rates are non-zero (synthetic dollars over real token volume) and
    # reach the runtime config the runner is built from.
    c = from_env({"REPO": "x"})
    assert c.codex_cost_per_mtok_in > 0
    assert c.codex_cost_per_mtok_out > c.codex_cost_per_mtok_in  # output costs more


def test_codex_pricing_can_be_switched_off_explicitly():
    # "0" is a real operator choice (don't track codex spend), not a missing
    # value to be replaced by the default — and it must validate clean.
    c = from_env({**_VALID, "CODEX_COST_PER_MTOK_IN": "0", "CODEX_COST_PER_MTOK_OUT": "0", "ISSUE_SPEND_CAP_USD": "0",
                  "DAILY_SPEND_CAP_USD": "0", "GLOBAL_DAILY_SPEND_CAP_USD": "0"})
    assert c.codex_cost_per_mtok_in == 0.0
    assert c.codex_cost_per_mtok_out == 0.0
    assert c.validation_errors() == ()


def test_armed_caps_require_pricing_for_subscription_runtimes_too():
    c = from_env({**_VALID, "FIXER_RUNTIME": "claude", "REVIEWER_RUNTIME": "claude", "CODEX_COST_PER_MTOK_IN": "0",
                  "CODEX_COST_PER_MTOK_OUT": "0", "ISSUE_SPEND_CAP_USD": "5"})

    assert any("armed spend caps require positive input/output pricing" in error for error in c.validation_errors())


# ── #43: a PER-MODEL codex rate table ────────────────────────────────────────
# Routing moves price, not just token count, so a single flat rate makes the
# routed tiers indistinguishable in the ledger — which is exactly the split
# CLAUDE.md's routing guardrail asks for.


@pytest.mark.parametrize("raw,table", [
    ("gpt-5.6-luna=1.25/10,gpt-5.6-terra=10/60", (("gpt-5.6-luna", 1.25, 10.0), ("gpt-5.6-terra", 10.0, 60.0))),
    (" gpt-5.6-luna = 1.25 / 10 , ,", (("gpt-5.6-luna", 1.25, 10.0),)),
    # The knob is additive: an existing deployment prices exactly as before.
    ("   ", ()),
    (None, ()),
])
def test_the_rate_table_parses_a_model_per_entry(raw, table):
    c = from_env({"REPO": "x", "FIX_BOT": "fix-bot", **({"CODEX_COST_PER_MTOK": raw} if raw is not None else {})})
    assert c.codex_cost_per_mtok == table
    assert c.validation_errors() == ()


def test_a_malformed_rate_entry_is_named_and_fails_validation():
    """A silently-dropped entry is indistinguishable from a model the operator
    did not mean to override — both read as 'the fallback price', which is the
    bug this knob exists to fix."""
    c = from_env({**_VALID, "CODEX_COST_PER_MTOK": "gpt-5.6-luna,gpt-5.6-terra=10/60"})
    errors = c.validation_errors()
    assert any("gpt-5.6-luna" in e for e in errors), errors
    assert c.codex_cost_per_mtok == (("gpt-5.6-terra", 10.0, 60.0),), "the well-formed entries survive"


@pytest.mark.parametrize("raw", [
    "gpt-5.6-luna=abc/10",     # non-numeric
    "gpt-5.6-luna=1.25",       # no output rate
    "gpt-5.6-luna=1.25/-3",    # negative
    "gpt-5.6-luna=nan/10",     # nan passes every downstream range check
    "gpt-5.6-luna=inf/10",
    "=1.25/10",                # no model
])
def test_malformed_rate_entries_never_reach_the_table(raw):
    c = from_env({"REPO": "x", "FIX_BOT": "fix-bot", "CODEX_COST_PER_MTOK": raw})
    assert c.codex_cost_per_mtok == ()
    assert any(raw.strip() in e for e in c.validation_errors()), c.validation_errors()


def test_a_model_priced_twice_keeps_the_first_and_says_so():
    c = from_env({**_VALID, "CODEX_COST_PER_MTOK": "gpt-5.6-luna=1.25/10,GPT-5.6-Luna=99/99"})
    assert c.codex_cost_per_mtok == (("gpt-5.6-luna", 1.25, 10.0),)
    assert any("more than once" in e for e in c.validation_errors())


def test_agent_env_file_is_loaded_with_shell_expansion(tmp_path):
    env_file = tmp_path / "agent.env"
    env_file.write_text('REPO="o/r"\nTARGET_DIR="$HOME/repo"\n'
                        'OPENAI_API_KEY="${ANTHROPIC_AUTH_TOKEN:-${OPENAI_API_KEY:-}}"\n')

    env = _load_agent_env({"AGENT_ENV": str(env_file), "HOME": "/home/alice", "ANTHROPIC_AUTH_TOKEN": "pk-from-shell",
                           "OPENAI_API_KEY": "sk-inherited"})

    assert (env["REPO"], env["TARGET_DIR"], env["OPENAI_API_KEY"]) == ("o/r", "/home/alice/repo", "pk-from-shell")


# --- AGENT_ENV file is the authority for harness knobs (#12) -----------------
# The relaunch loop re-sources the composed file into a long-lived shell, so a
# knob the operator DELETED is still exported from the previous reload. These
# pin the bidirectional contract: set AND unset both take effect.

def _from_file(tmp_path, content: str, **inherited):
    env_file = tmp_path / "active.env"
    env_file.write_text(content)
    return from_env({"AGENT_ENV": str(env_file), **inherited})


def test_removed_knob_falls_back_to_default_not_inherited_value(tmp_path):
    c = _from_file(tmp_path, "REPO=o/r\nDAILY_SPEND_CAP_USD=250\n", REPO="o/r",
                   FIX_MODEL="gpt-5.6-sol",  # stale export from an earlier reload
                   DAILY_SPEND_CAP_USD="150")

    assert c.fix_model is None        # absent from the file → dataclass default
    assert c.daily_spend_cap_usd == 250.0


def test_file_value_wins_over_inherited_value(tmp_path):
    c = _from_file(tmp_path, "REPO=o/r\nFIX_MODEL=haiku\nPOLL_SECONDS=45\n", FIX_MODEL="opus", POLL_SECONDS="20")

    assert (c.fix_model, c.poll_seconds) == ("haiku", 45)


def test_shell_escaped_values_survive_the_file_read(tmp_path):
    # compose-env.sh writes `printf '%s=%q'`, so a value with shell syntax
    # lands backslash-escaped.
    cmd = "set -e; (cd backend && uv run pytest) && pnpm exec tsc -b"
    quoted = subprocess.run(["bash", "-c", 'printf "%s=%q" "$1" "$2"', "_", "REVIEW_PROMPT", cmd],
                            capture_output=True, text=True, check=True).stdout

    assert _from_file(tmp_path, f"REPO=o/r\n{quoted}\n").review_prompt == cmd


@pytest.mark.parametrize("content", [
    'REPO="o/r\nFIX_MODEL=haiku\n',  # syntax the shell refuses must not reset the daemon mid-reload
    None,  # a broken/absent watch file
    "# AUTO-GENERATED by bin/compose-env.sh — do not edit.\n",  # assigns nothing: refuse, never default REPO
])
def test_an_unusable_agent_env_file_falls_back_to_process_env(tmp_path, content):
    env_file = tmp_path / "active.env"
    if content is not None:
        env_file.write_text(content)
    c = from_env({"AGENT_ENV": str(env_file), "REPO": "o/inherited", "FIX_MODEL": "opus"})
    assert (c.repo, c.fix_model) == ("o/inherited", "opus")
