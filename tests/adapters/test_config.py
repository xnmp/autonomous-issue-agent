"""Config-from-env mapping."""

from issue_agent.app.config import HarnessConfig, from_env, render_agent_md
from issue_agent.app.main import _load_agent_env
from issue_agent.domain.review import ReviewMode


def test_review_mode_defaults_single():
    assert from_env({"REPO": "x"}).review_mode is ReviewMode.SINGLE
    assert from_env({"REPO": "x"}).review_scout_model == "sonnet"


def test_review_mode_delegated_and_scout_override():
    c = from_env({"REPO": "x", "REVIEW_MODE": "delegated", "REVIEW_SCOUT_MODEL": "haiku"})
    assert c.review_mode is ReviewMode.DELEGATED
    assert c.review_scout_model == "haiku"


def test_review_mode_legacy_multi_agent_alias():
    assert from_env({"REPO": "x", "REVIEW_MULTI_AGENT": "1"}).review_mode is ReviewMode.MULTI
    # explicit REVIEW_MODE beats the alias
    assert from_env(
        {"REPO": "x", "REVIEW_MULTI_AGENT": "1", "REVIEW_MODE": "single"}
    ).review_mode is ReviewMode.SINGLE


def test_render_agent_md_expands_and_strips():
    cfg = HarnessConfig(repo="o/harness", base_branch="dev")
    md = ("branch off $BASE_BRANCH, file on $HARNESS_REPO, issue $ISSUE_NUMBER\n"
          "$DIAGNOSE_RULES\n$LOGGING_RULES\n$DOC_RULES\n$TEST_RULES")
    out = render_agent_md(md, cfg)
    assert "$BASE_BRANCH" not in out and "dev" in out
    assert "$HARNESS_REPO" not in out and "o/harness" in out
    assert "$DIAGNOSE_RULES" not in out  # optional rule blocks stripped
    assert "$ISSUE_NUMBER" in out  # left literal — agent fills it per-issue


def test_defaults():
    c = from_env({"REPO": "o/r"})
    assert c.repo == "o/r"
    assert c.base_branch == "dev"
    assert c.identity_mode == "three"
    assert c.auto_merge is True
    assert c.steward_triage is True   # default-on; promote stays human
    assert c.steward_promote is False
    assert c.poll_seconds == 20
    assert c.review_trigger_label == "ai-pr-open"  # default unchanged
    # Jira sync defaults ON but inert (no project key), MCP spec path defaulted.
    assert c.jira_sync is True
    assert c.jira_project_key == ""
    assert c.jira_mcp_config == "config/mcp/atlassian.json"
    assert c.jira_mcp_tools == "mcp__atlassian__*"


def test_review_trigger_label_override():
    c = from_env({"REPO": "o/ext", "REVIEW_TRIGGER_LABEL": "ai-review"})
    assert c.review_trigger_label == "ai-review"


def test_bool_parsing():
    assert from_env({"REPO": "x", "AUTO_MERGE": "0"}).auto_merge is False
    assert from_env({"REPO": "x", "STEWARD_TRIAGE": "1"}).steward_triage is True
    assert from_env({"REPO": "x", "STEWARD_TRIAGE": "0"}).steward_triage is False  # default-on, opt-out
    assert from_env({"REPO": "x", "CLOSE_ISSUE_ON_MERGE": "false"}).close_issue_on_merge is False
    assert from_env({"REPO": "x", "JIRA_SYNC": "0"}).jira_sync is False   # default-on, opt-out
    assert from_env({"REPO": "x", "JIRA_PROJECT_KEY": "ABC"}).jira_project_key == "ABC"


def test_models_and_budgets():
    c = from_env({"REPO": "x", "FIX_MODEL": "opus", "REVIEW_BUDGET_USD": "2.5",
                  "STEWARD_TRIAGE_MODEL": "sonnet"})
    assert c.fix_model == "opus"
    assert c.review_budget_usd == 2.5
    assert c.steward_triage_model == "sonnet"


def test_empty_fix_model_is_none():
    assert from_env({"REPO": "x", "FIX_MODEL": ""}).fix_model is None


def test_ci_ignore_and_method():
    c = from_env({"REPO": "x", "CI_IGNORE_CHECKS": "Format.*", "MERGE_METHOD": "merge"})
    assert c.ci_ignore_checks == "Format.*"
    assert c.merge_method == "merge"


def test_edit_check_cmd():
    assert from_env({"REPO": "x"}).edit_check_cmd == ""  # default off
    assert from_env({"REPO": "x", "EDIT_CHECK_CMD": "npx tsc --noEmit"}).edit_check_cmd == "npx tsc --noEmit"


def test_retry_caps():
    c = from_env({"REPO": "x", "MAX_CI_RETRIES": "5", "MAX_REVIEW_RETRIES": "2",
                  "MAX_RESOLVE_RETRIES": "4"})
    assert c.max_ci_retries == 5
    assert c.max_review_retries == 2
    assert c.max_resolve_retries == 4


def test_validate_fails_loud_on_empty_repo():
    # Silent-default class (#50): an empty REPO must die at startup with a
    # pointed message, not surface as an opaque gh error on the first poll.
    import pytest

    with pytest.raises(ValueError, match="REPO"):
        from_env({}).validate()


def test_describe_reports_effective_values_and_no_secrets():
    c = from_env({"REPO": "o/r", "MAX_RESOLVE_RETRIES": "2",
                  "FIX_BOT_TOKEN": "sekret-token"})
    d = c.describe()
    assert "repo=o/r" in d
    assert "resolve=2" in d
    assert "sekret" not in d


def test_grouped_config_preserves_flat_env_mapping():
    c = from_env({
        "REPO": "o/r",
        "BASE_BRANCH": "main",
        "TARGET_DIR": "/repo",
        "STATE_DIR": "/state/fix",
        "REVIEW_STATE_DIR": "/state/review",
        "STEWARD_STATE_DIR": "/state/steward",
        "WORKTREE_ROOT": "/worktrees",
        "HARNESS_ROOT_DIR": "/harness",
        "FIX_BOT": "fixer",
        "MAX_BUDGET_USD": "4.25",
        "REVIEW_MODE": "delegated",
        "REVIEW_TRIGGER_LABEL": "ai-review",
        "STEWARD_OWNER": "@org/team",
        "JIRA_PROJECT_KEY": "ABC",
        "CODEX_BIN": "codex-dev",
        "CODEX_MODEL": "gpt-next",
    })

    assert c.repository.repo == "o/r"
    assert c.repository.base_branch == "main"
    assert c.repository.target_dir == "/repo"
    assert c.runtime_paths.state_dir == "/state/fix"
    assert c.runtime_paths.review_state_dir == "/state/review"
    assert c.runtime_paths.steward_state_dir == "/state/steward"
    assert c.runtime_paths.worktree_root == "/worktrees"
    assert c.runtime_paths.harness_root == "/harness"
    assert c.fix_runtime.bot == "fixer"
    assert c.fix_runtime.budget_usd == 4.25
    assert c.review_runtime.mode is ReviewMode.DELEGATED
    assert c.review_runtime.trigger_label == "ai-review"
    assert c.steward_runtime.owner == "@org/team"
    assert c.jira_runtime.sync is True
    assert c.jira_runtime.project_key == "ABC"
    assert c.codex_runtime.binary == "codex-dev"
    assert c.codex_runtime.model == "gpt-next"
    # Both session caps reach the codex runtime through the same grouped config the
    # Claude runner reads off HarnessConfig — one knob, both runtimes.
    assert c.codex_runtime.idle_timeout == c.agent_idle_timeout
    assert c.codex_runtime.max_runtime == c.agent_max_runtime


def test_agent_max_runtime_defaults_and_parses():
    assert from_env({"REPO": "x"}).agent_max_runtime == 2700.0
    assert from_env({"REPO": "x", "AGENT_MAX_RUNTIME_SECONDS": "600"}).agent_max_runtime == 600.0


def test_agent_max_runtime_zero_disables_the_cap():
    # 0 is the documented opt-out (a repo whose test suite alone outruns any sane
    # ceiling). It must parse as a valid config, not a rejected one — the runner
    # wiring turns 0 into "no cap".
    c = from_env({"REPO": "x", "AGENT_MAX_RUNTIME_SECONDS": "0"})
    assert c.agent_max_runtime == 0.0
    assert c.validation_errors() == ()


def test_agent_max_runtime_empty_means_unset_not_zero():
    # review M2: an exported-but-empty numeric knob must mean UNSET (fall back
    # to the field default), never a different valid value ("0" = disabled).
    # Explicit "0" (above) and blank (here) are NOT the same thing.
    c = from_env({"REPO": "x", "AGENT_MAX_RUNTIME_SECONDS": ""})
    assert c.agent_max_runtime == 2700.0
    assert c.validation_errors() == ()


def test_valid_defaults_have_no_validation_errors():
    assert from_env({"REPO": "o/r"}).validation_errors() == ()


def test_validation_rejects_invalid_retry_caps_and_poll_seconds():
    c = from_env({
        "REPO": "x",
        "POLL_SECONDS": "0",
        "MAX_CI_RETRIES": "-1",
        "MAX_REVIEW_RETRIES": "-2",
        "MAX_REBASE_RETRIES": "-3",
        "REVIEW_MAX_REJECTIONS": "-4",
        "MAX_CONTRACT_ROUNDS": "-5",
    })

    assert c.validation_errors() == (
        "POLL_SECONDS must be > 0",
        "MAX_CI_RETRIES must be >= 0",
        "MAX_REVIEW_RETRIES must be >= 0",
        "MAX_REBASE_RETRIES must be >= 0",
        "REVIEW_MAX_REJECTIONS must be >= 0",
        "MAX_CONTRACT_ROUNDS must be >= 0",
    )


def test_validation_rejects_invalid_budgets():
    c = from_env({
        "REPO": "x",
        "MAX_BUDGET_USD": "0",
        "REVIEW_BUDGET_USD": "-1",
        "STEWARD_TRIAGE_BUDGET_USD": "0",
        "REVIEW_GATE_BUDGET_USD": "0",
        "REVIEW_DIMENSION_BUDGET_USD": "0",
        "REVIEW_VALIDATION_BUDGET_USD": "0",
        "JIRA_SYNC_BUDGET_USD": "0",
    })

    assert c.validation_errors() == (
        "MAX_BUDGET_USD must be > 0",
        "REVIEW_BUDGET_USD must be > 0",
        "STEWARD_TRIAGE_BUDGET_USD must be > 0",
        "REVIEW_GATE_BUDGET_USD must be > 0",
        "REVIEW_DIMENSION_BUDGET_USD must be > 0",
        "REVIEW_VALIDATION_BUDGET_USD must be > 0",
        "JIRA_SYNC_BUDGET_USD must be > 0",
    )


def test_validation_rejects_invalid_runtime_settings():
    c = from_env({
        "REPO": "x",
        "FIXER_RUNTIME": "python",
        "AGENT_IDLE_TIMEOUT_SECONDS": "-1",
        "AGENT_MAX_RUNTIME_SECONDS": "-1",
        "CODEX_TRANSPORT_RETRIES": "-1",
        "CODEX_BACKOFF_BASE_SECONDS": "0",
    })

    assert c.validation_errors() == (
        "FIXER_RUNTIME must be 'claude' or 'codex'",
        "AGENT_IDLE_TIMEOUT_SECONDS must be >= 0",
        "AGENT_MAX_RUNTIME_SECONDS must be >= 0",
        "CODEX_TRANSPORT_RETRIES must be >= 0",
        "CODEX_BACKOFF_BASE_SECONDS must be > 0",
    )


def test_validate_raises_combined_error_message():
    c = from_env({"REPO": "x", "FIXER_RUNTIME": "python"})

    try:
        c.validate()
    except ValueError as exc:
        assert str(exc) == "invalid harness config: FIXER_RUNTIME must be 'claude' or 'codex'"
    else:  # pragma: no cover - assertion branch
        raise AssertionError("validate should reject invalid config")


def test_jira_sync_without_project_key_stays_valid_and_inert():
    c = from_env({"REPO": "x", "JIRA_SYNC": "1"})

    assert c.jira_runtime.sync is True
    assert c.jira_runtime.project_key == ""
    assert c.validation_errors() == ()


def test_malformed_numeric_does_not_traceback_and_lands_in_validation_errors():
    # review M2: POLL_SECONDS=abc used to raise a bare ValueError deep inside
    # int() — construction itself must never crash; the field falls back to
    # its dataclass default and the problem surfaces as a readable, fail-loud
    # validation entry instead.
    c = from_env({"REPO": "x", "POLL_SECONDS": "abc"})
    assert c.poll_seconds == 20  # substituted the default, not a traceback
    errors = c.validation_errors()
    assert any("POLL_SECONDS" in e and "abc" in e for e in errors)


def test_well_formed_numeric_produces_no_parse_error():
    # Anti-vacuity twin of the above: a well-formed value must NOT be flagged.
    c = from_env({"REPO": "x", "POLL_SECONDS": "45"})
    assert c.poll_seconds == 45
    assert c.validation_errors() == ()


def test_empty_spend_cap_falls_back_to_default_not_disabled():
    # review M2: `float(v or 0)` made an exported-but-empty ISSUE_SPEND_CAP_USD
    # silently mean "cap disabled" instead of "unset, use the default" —
    # describe() would then print issue=$0.00 as if that were intentional.
    c = from_env({"REPO": "x", "ISSUE_SPEND_CAP_USD": ""})
    assert c.issue_spend_cap_usd == 25.0
    assert c.validation_errors() == ()


def test_explicit_zero_spend_cap_still_means_disabled():
    # Anti-vacuity twin: an OPERATOR-CHOSEN 0 is a real, valid "off" and must
    # not be conflated with "unset" — only empty string means unset.
    c = from_env({"REPO": "x", "ISSUE_SPEND_CAP_USD": "0"})
    assert c.issue_spend_cap_usd == 0.0
    assert c.validation_errors() == ()


def test_empty_max_budget_falls_back_to_default_without_crashing():
    # review M2: MAX_BUDGET_USD= used to crash with a bare ValueError
    # (float("")). Must resolve to the documented default instead.
    c = from_env({"REPO": "x", "MAX_BUDGET_USD": ""})
    assert c.fix_budget_usd == 10.0
    assert c.validation_errors() == ()


def test_malformed_max_budget_is_reported_not_raised():
    c = from_env({"REPO": "x", "MAX_BUDGET_USD": "ten dollars"})
    assert c.fix_budget_usd == 10.0
    assert any("MAX_BUDGET_USD" in e for e in c.validation_errors())


def test_empty_auto_merge_uses_default_on_not_off():
    # review M2: _b treated an exported-but-empty value as False, silently
    # flipping a default-ON flag off.
    assert from_env({"REPO": "x", "AUTO_MERGE": ""}).auto_merge is True


def test_explicit_zero_auto_merge_still_disables():
    # Anti-vacuity twin: "0" is a real, explicit opt-out and must still work.
    assert from_env({"REPO": "x", "AUTO_MERGE": "0"}).auto_merge is False


def test_poll_seconds_default_is_20_everywhere():
    # review S7: the dataclass default (was 60) used to disagree with
    # from_env's own literal ("20"). Both paths must now agree — from_env
    # reads its default off the dataclass field, so there is one source.
    assert HarnessConfig(repo="x").poll_seconds == 20
    assert from_env({"REPO": "x"}).poll_seconds == 20


def test_state_dirs_default_per_repo():
    # review M5/D7: default STATE_DIR used to be per-role but not per-repo —
    # two repos' fix daemons shared issue-N.session and one resumed the
    # other's transcript. Assert the default is now namespaced by repo.
    c = from_env({"REPO": "acme/widget"})
    assert c.state_dir.endswith("issue-agent/acme-widget")
    assert c.review_state_dir.endswith("review-agent/acme-widget")
    assert c.steward_state_dir.endswith("steward-agent/acme-widget")


def test_state_dirs_do_not_collide_across_repos():
    # The actual finding, reproduced directly: two repos must not resolve to
    # the same default state dir.
    a = from_env({"REPO": "acme/widget"})
    b = from_env({"REPO": "acme/gadget"})
    assert a.state_dir != b.state_dir
    assert a.review_state_dir != b.review_state_dir
    assert a.steward_state_dir != b.steward_state_dir


def test_explicit_state_dir_still_wins_over_the_per_repo_default():
    # Anti-vacuity twin: an operator who set STATE_DIR explicitly is
    # unaffected by the new default.
    c = from_env({"REPO": "acme/widget", "STATE_DIR": "/custom/state"})
    assert c.state_dir == "/custom/state"


def test_empty_repo_falls_back_to_a_fixed_slug_not_a_shared_default():
    # No REPO set at all — handle gracefully rather than crashing on the
    # slug computation; falls back to a fixed "unknown" component.
    c = from_env({})
    assert c.state_dir.endswith("issue-agent/unknown")


def test_identity_cross_check_fires_on_normalized_equal_bots_under_three_mode():
    # review C1/D1 belt-and-braces: a raw string compare would miss
    # `fix-bot` vs `fix-bot[bot]` and let a self-approving pair through,
    # which GitHub rejects with a 422 on every approval, forever.
    c = from_env({
        "REPO": "x", "IDENTITY_MODE": "three",
        "FIX_BOT": "fix-bot", "REVIEW_BOT": "fix-bot[bot]",
    })
    errors = c.validation_errors()
    assert any("FIX_BOT" in e and "REVIEW_BOT" in e for e in errors)


def test_identity_cross_check_does_not_fire_on_distinct_bots():
    # Anti-vacuity twin: genuinely distinct identities must not be flagged.
    c = from_env({
        "REPO": "x", "IDENTITY_MODE": "three",
        "FIX_BOT": "fix-bot", "REVIEW_BOT": "review-bot",
    })
    assert c.validation_errors() == ()


def test_identity_cross_check_does_not_fire_outside_three_mode():
    # Same normalized-equal bots, but not in three-identity mode — the
    # self-approval trap only applies when review is expected to be a
    # distinct identity from the fixer.
    c = from_env({
        "REPO": "x", "IDENTITY_MODE": "solo",
        "FIX_BOT": "fix-bot", "REVIEW_BOT": "fix-bot[bot]",
    })
    assert c.validation_errors() == ()


def test_from_env_leaves_bot_values_as_written_display_form():
    # D1: from_env does NOT normalize the stored values themselves — only
    # validation_errors() compares normalized forms. describe()/gh calls see
    # exactly what the operator wrote.
    c = from_env({"REPO": "x", "FIX_BOT": "Fix-Bot[bot]"})
    assert c.fix_bot == "Fix-Bot[bot]"


def test_max_infra_deaths_default_and_override():
    assert from_env({"REPO": "x"}).max_infra_deaths == 5
    c = from_env({"REPO": "x", "MAX_INFRA_DEATHS": "2"})
    assert c.max_infra_deaths == 2
    assert c.fix_runtime.max_infra_deaths == 2


def test_max_infra_deaths_zero_is_a_valid_off_value():
    c = from_env({"REPO": "x", "MAX_INFRA_DEATHS": "0"})
    assert c.max_infra_deaths == 0
    assert c.validation_errors() == ()


def test_codex_cost_per_mtok_defaults_and_override():
    c = from_env({"REPO": "x"})
    assert c.codex_cost_per_mtok_in == 0.0
    assert c.codex_cost_per_mtok_out == 0.0
    assert c.codex_runtime.cost_per_mtok_in == 0.0
    c2 = from_env({
        "REPO": "x", "CODEX_COST_PER_MTOK_IN": "3.00", "CODEX_COST_PER_MTOK_OUT": "15.00",
    })
    assert c2.codex_cost_per_mtok_in == 3.00
    assert c2.codex_cost_per_mtok_out == 15.00
    assert c2.codex_runtime.cost_per_mtok_out == 15.00
    assert c2.validation_errors() == ()


def test_agent_env_file_is_loaded_with_shell_expansion(tmp_path):
    env_file = tmp_path / "agent.env"
    env_file.write_text(
        'REPO="o/r"\n'
        'TARGET_DIR="$HOME/repo"\n'
        'OPENAI_API_KEY="${ANTHROPIC_AUTH_TOKEN:-${OPENAI_API_KEY:-}}"\n'
    )

    env = _load_agent_env({
        "AGENT_ENV": str(env_file),
        "HOME": "/home/alice",
        "ANTHROPIC_AUTH_TOKEN": "pk-from-shell",
        "OPENAI_API_KEY": "sk-inherited",
    })

    assert env["REPO"] == "o/r"
    assert env["TARGET_DIR"] == "/home/alice/repo"
    assert env["OPENAI_API_KEY"] == "pk-from-shell"
