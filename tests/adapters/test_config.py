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
    assert c.poll_seconds == 60
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
    c = from_env({"REPO": "x", "MAX_CI_RETRIES": "5", "MAX_REVIEW_RETRIES": "2"})
    assert c.max_ci_retries == 5
    assert c.max_review_retries == 2


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


def test_valid_defaults_have_no_validation_errors():
    assert from_env({"REPO": "o/r"}).validation_errors() == ()


def test_validation_rejects_invalid_retry_caps_and_poll_seconds():
    c = from_env({
        "REPO": "x",
        "POLL_SECONDS": "0",
        "MAX_CI_RETRIES": "-1",
        "MAX_REVIEW_RETRIES": "-2",
        "MAX_REBASE_RETRIES": "-3",
        "REVIEW_MAX_ROUNDS": "-4",
        "MAX_CONTRACT_ROUNDS": "-5",
    })

    assert c.validation_errors() == (
        "POLL_SECONDS must be > 0",
        "MAX_CI_RETRIES must be >= 0",
        "MAX_REVIEW_RETRIES must be >= 0",
        "MAX_REBASE_RETRIES must be >= 0",
        "REVIEW_MAX_ROUNDS must be >= 0",
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
        "CODEX_TRANSPORT_RETRIES": "-1",
        "CODEX_BACKOFF_BASE_SECONDS": "0",
    })

    assert c.validation_errors() == (
        "FIXER_RUNTIME must be 'claude' or 'codex'",
        "AGENT_IDLE_TIMEOUT_SECONDS must be >= 0",
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
