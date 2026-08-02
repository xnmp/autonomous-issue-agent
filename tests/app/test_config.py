"""Config-from-env mapping."""

import json
import subprocess
from pathlib import Path

import pytest

from issue_agent.app.config import (
    HarnessConfig,
    from_env,
    generated_env_reference,
    render_agent_md,
)
from issue_agent.app.main import _load_agent_env
from issue_agent.domain.review import IdentityMode, ReviewMode


def test_metadata_backed_choices_drive_parsing_and_the_example():
    cfg = from_env(
        {
            "REPO": "o/r",
            "FIX_BOT": "fix-bot",
            "MERGE_METHOD": "octopus",
            "ARM_ON_OPEN": "1",
        }
    )
    assert cfg.merge_method == "squash"
    assert cfg.arm_on_open == "auto"
    assert {error.split()[0] for error in cfg.parse_errors} >= {
        "MERGE_METHOD",
        "ARM_ON_OPEN",
    }

    example = generated_env_reference()
    assert 'ARM_ON_OPEN="auto"  # choices: auto|0' in example
    assert 'MERGE_METHOD="squash"  # choices: squash|merge|rebase' in example


def test_review_mode_defaults_single():
    assert from_env({"REPO": "x"}).review_mode is ReviewMode.SINGLE
    assert from_env({"REPO": "x"}).review_scout_model == "sonnet"


def test_reviewer_runtime_defaults_to_codex_with_a_mid_tier_model():
    """The deployment default is visible in the task-producing config, not
    merely documented in an example overlay."""
    cfg = from_env({"REPO": "x"})

    assert cfg.reviewer_runtime == "codex"
    assert cfg.review_model == "gpt-5.6-terra"


def test_reviewer_runtime_accepts_an_explicit_claude_diversity_posture():
    cfg = from_env({"REPO": "x", "REVIEWER_RUNTIME": "claude"})

    assert cfg.reviewer_runtime == "claude"
    assert cfg.review_model == "opus"


def test_steward_runtime_defaults_to_claude_with_its_claude_model():
    """Back-compat: an overlay written before STEWARD_RUNTIME existed must
    resolve to exactly the runtime and model it resolved to before."""
    cfg = from_env({"REPO": "x"})

    assert cfg.steward_runtime.runtime == "claude"
    assert cfg.steward_runtime.triage_model == "sonnet"


def test_a_codex_steward_drops_the_claude_flavoured_default_model():
    """`sonnet` is an Anthropic alias Codex cannot resolve. Unset means "let
    the Codex runner choose", never "send the Claude default to Codex"."""
    cfg = from_env({"REPO": "x", "STEWARD_RUNTIME": "codex"})

    assert cfg.steward_runtime.runtime == "codex"
    assert cfg.steward_runtime.triage_model is None


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_an_explicit_steward_triage_model_survives_either_runtime(runtime):
    cfg = from_env({
        "REPO": "x", "STEWARD_RUNTIME": runtime,
        "STEWARD_TRIAGE_MODEL": "gpt-5.6-terra",
    })

    assert cfg.steward_runtime.triage_model == "gpt-5.6-terra"


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_an_explicitly_empty_triage_model_still_means_the_runtime_default(runtime):
    """`STEWARD_TRIAGE_MODEL=""` meant None (let the runtime choose) before the
    per-runtime default existed, and must keep meaning that. Only an ABSENT key
    takes the default — otherwise a deliberate empty silently becomes
    "sonnet"."""
    cfg = from_env({
        "REPO": "x", "STEWARD_RUNTIME": runtime, "STEWARD_TRIAGE_MODEL": "",
    })

    assert cfg.steward_runtime.triage_model is None


@pytest.mark.parametrize("value", ["glm", "gpt5"])
def test_steward_runtime_rejects_a_runtime_it_cannot_dispatch(value):
    """An unsupported value must be reported and fall back to claude, never
    silently select a runtime the steward has no binder for."""
    cfg = from_env({"REPO": "x", "FIX_BOT": "fix-bot", "STEWARD_RUNTIME": value})

    _assert_each_knob_flagged_once(cfg.validation_errors(), {"STEWARD_RUNTIME"})
    assert "must be one of ['claude', 'codex']" in cfg.validation_errors()[0]
    assert cfg.steward_runtime.runtime == "claude"


def test_an_empty_steward_runtime_reads_as_unset():
    """Every other knob in this table treats "" as "not configured"."""
    cfg = from_env({"REPO": "x", "FIX_BOT": "fix-bot", "STEWARD_RUNTIME": ""})

    assert cfg.validation_errors() == ()
    assert cfg.steward_runtime.runtime == "claude"


@pytest.mark.parametrize("mode", ["multi", "delegated"])
def test_codex_reviewer_rejects_modes_it_cannot_execute(mode):
    cfg = from_env({
        "REPO": "o/r", "FIX_BOT": "fix-bot", "REVIEWER_RUNTIME": "codex",
        "REVIEW_MODE": mode,
    })

    with pytest.raises(ValueError, match="REVIEW_MODE=single"):
        cfg.validate()


def test_review_mode_delegated_and_scout_override():
    c = from_env({"REPO": "x", "REVIEW_MODE": "delegated", "REVIEW_SCOUT_MODEL": "haiku"})
    assert c.review_mode is ReviewMode.DELEGATED
    assert c.review_scout_model == "haiku"


def test_removed_review_multi_agent_alias_is_ignored():
    assert from_env(
        {"REPO": "x", "REVIEW_MULTI_AGENT": "1"}
    ).review_mode is ReviewMode.SINGLE


def test_every_operator_field_has_env_and_coercion_metadata():
    from dataclasses import fields

    internal = {
        "arm_on_open_active",
        "agent_md",
        "control_authority_root",
        "repointed_from_deployments",
        "triage_prompt",
        "codex_cost_per_mtok_cached_in",
        "parse_errors",
        "repo_input",
    }
    for definition in fields(HarnessConfig):
        if definition.name in internal:
            assert not definition.metadata
            continue
        assert definition.metadata["env"]
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
    cfg = HarnessConfig(
        repo="o/harness",
        base_branch="canary/dashboard-e2e",
    )
    template = (
        Path(__file__).parents[2] / "plugin" / "AGENT.md"
    ).read_text()

    rendered = render_agent_md(template, cfg)

    assert "running headless on the `canary/dashboard-e2e` branch" in rendered
    assert "running headless on the `dev` branch" not in rendered
    assert "do NOT call `gh issue create`" in rendered
    assert "file an issue on" not in rendered


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


def test_custom_state_roots_keep_one_machine_accounting_authority(
    monkeypatch,
):
    monkeypatch.setattr(
        "issue_agent.app.config.machine_control_authority_root",
        lambda: "/machine/state",
    )

    first = from_env(
        {
            "REPO": "o/one",
            "STATE_DIR": "/srv/one/fix",
            "REVIEW_STATE_DIR": "/srv/one/review",
            "STEWARD_STATE_DIR": "/srv/one/steward",
        }
    )
    second = from_env(
        {
            "REPO": "o/two",
            "STATE_DIR": "/var/lib/two/fix",
            "REVIEW_STATE_DIR": "/var/lib/two/review",
            "STEWARD_STATE_DIR": "/var/lib/two/steward",
        }
    )

    assert first.control_authority_root == "/machine/state"
    assert second.control_authority_root == "/machine/state"


def test_explicit_repoint_history_is_normalized_and_deduplicated():
    raw = json.dumps(
        [
            {"repository": "Acme/App", "target_dir": "/srv/app-old"},
            {"repository": "acme/app", "target_dir": "/srv/app-old"},
            {"repository": "acme/app", "target_dir": "/srv/app-older"},
        ]
    )

    cfg = from_env({"REPO": "acme/app", "REPOINTED_FROM_DEPLOYMENTS": raw})

    assert cfg.repointed_from_deployments == (
        ("acme/app", "/srv/app-old"),
        ("acme/app", "/srv/app-older"),
    )


@pytest.mark.parametrize(
    "raw",
    (
        "not-json",
        "{}",
        '[{"repository":"o/r","target_dir":"relative"}]',
        '[{"repository":"","target_dir":"/srv/old"}]',
    ),
)
def test_malformed_repoint_history_fails_config_validation(raw):
    cfg = from_env(
        {
            "REPO": "o/r",
            "FIX_BOT": "fix-bot",
            "REPOINTED_FROM_DEPLOYMENTS": raw,
        }
    )

    with pytest.raises(ValueError, match="REPOINTED_FROM_DEPLOYMENTS"):
        cfg.validate()


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


@pytest.mark.parametrize(
    "repository",
    (
        "github.com/o/r",
        "https://github.com/o/r",
        "o/r.git",
        "o//r",
        "owner_name/repo",
    ),
)
def test_validate_rejects_repository_alias_and_url_shapes(repository):
    with pytest.raises(ValueError, match="REPO"):
        from_env({"REPO": repository}).validate()


def test_a_shell_command_is_logged_quoted_and_whole_when_it_fits():
    """#35: the boot line used to print the literal word `set` for a present
    TEST_CMD, which for `TEST_CMD='set -e; cargo test …'` is indistinguishable
    from a value truncated at the first space. It must show the command."""
    from issue_agent.app.config import describe_command

    out = describe_command("set -e; cargo test --quiet && cargo fmt --check")

    assert "set -e; cargo test --quiet && cargo fmt --check" in out
    assert out.startswith("'") or out.startswith('"')   # quoted, so spaces read
    assert "…" not in out


def test_a_long_shell_command_is_truncated_visibly_with_its_true_length():
    from issue_agent.app.config import describe_command

    out = describe_command("x" * 500, limit=10)

    assert "…" in out                 # the cut is announced, never silent
    assert "(len=500)" in out         # and the real size is always stated
    assert len(out) < 60


def test_an_unset_shell_command_is_reported_as_empty_not_as_quotes():
    from issue_agent.app.config import describe_command

    assert describe_command("") == "EMPTY"
    assert describe_command(None) == "EMPTY"
    assert describe_command("", empty="none") == "none"


def test_describe_shows_the_real_alert_and_edit_check_commands():
    c = from_env({"REPO": "o/r", "ALERT_CMD": "notify-send 'harness capped'",
                  "EDIT_CHECK_CMD": "cargo check --quiet"})
    d = c.describe()

    assert "cargo check --quiet" in d
    assert "notify-send 'harness capped'" in d or 'notify-send' in d


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
    c = from_env({"REPO": "x", "FIX_BOT": "fix-bot", "AGENT_MAX_RUNTIME_SECONDS": "0"})
    assert c.agent_max_runtime == 0.0
    assert c.validation_errors() == ()


def test_agent_max_runtime_empty_means_unset_not_zero():
    # review M2: an exported-but-empty numeric knob must mean UNSET (fall back
    # to the field default), never a different valid value ("0" = disabled).
    # Explicit "0" (above) and blank (here) are NOT the same thing.
    c = from_env({"REPO": "x", "FIX_BOT": "fix-bot", "AGENT_MAX_RUNTIME_SECONDS": ""})
    assert c.agent_max_runtime == 2700.0
    assert c.validation_errors() == ()


def test_valid_defaults_have_no_validation_errors():
    # REPO and FIX_BOT are both required for a "valid" config (review P1R-7 added
    # the FIX_BOT requirement) — this is the minimal env that passes.
    assert from_env({"REPO": "o/r", "FIX_BOT": "fix-bot"}).validation_errors() == ()


def _assert_each_knob_flagged_once(errors: tuple[str, ...], expected_knobs: set[str]) -> None:
    """T5 (review): membership + shape, not exact-tuple-and-order.

    The three tests below used to pin `validation_errors()`'s exact tuple AND
    its element order — coupling every one of them to `_coerce_int`'s literal
    wording and to whatever order `HarnessConfig`'s fields happen to be
    declared in, so reordering two dataclass fields (a pure refactor) would
    fail tests that have nothing to do with ordering. This asserts the
    CONTRACT instead: every invalid knob is named in some error (membership,
    checked as a set — the process lesson at review §5: set equality against
    an independently-derived expectation, not membership of hand-picked
    examples), and the error COUNT matches the knob count, so a knob that
    silently stopped being validated (or one validated twice) still fails."""
    flagged = {knob for knob in expected_knobs if any(knob in e for e in errors)}
    assert flagged == expected_knobs
    assert len(errors) == len(expected_knobs)


def test_validation_rejects_invalid_retry_caps_and_poll_seconds():
    c = from_env({
        "REPO": "x",
        "FIX_BOT": "fix-bot",
        "POLL_SECONDS": "0",
        "MAX_CI_RETRIES": "-1",
        "MAX_REVIEW_RETRIES": "-2",
        "MAX_REBASE_RETRIES": "-3",
        "REVIEW_MAX_REJECTIONS": "-4",
        "MAX_CONTRACT_ROUNDS": "-5",
    })

    _assert_each_knob_flagged_once(c.validation_errors(), {
        "POLL_SECONDS", "MAX_CI_RETRIES", "MAX_REVIEW_RETRIES",
        "MAX_REBASE_RETRIES", "REVIEW_MAX_REJECTIONS", "MAX_CONTRACT_ROUNDS",
    })


def test_validation_rejects_invalid_budgets():
    c = from_env({
        "REPO": "x",
        "FIX_BOT": "fix-bot",
        "MAX_BUDGET_USD": "0",
        "REVIEW_BUDGET_USD": "-1",
        "STEWARD_TRIAGE_BUDGET_USD": "0",
        "REVIEW_GATE_BUDGET_USD": "0",
        "REVIEW_DIMENSION_BUDGET_USD": "0",
        "REVIEW_VALIDATION_BUDGET_USD": "0",
        "JIRA_SYNC_BUDGET_USD": "0",
    })

    _assert_each_knob_flagged_once(c.validation_errors(), {
        "MAX_BUDGET_USD", "REVIEW_BUDGET_USD", "STEWARD_TRIAGE_BUDGET_USD",
        "REVIEW_GATE_BUDGET_USD", "REVIEW_DIMENSION_BUDGET_USD",
        "REVIEW_VALIDATION_BUDGET_USD", "JIRA_SYNC_BUDGET_USD",
    })


def test_validation_rejects_invalid_runtime_settings():
    c = from_env({
        "REPO": "x",
        "FIX_BOT": "fix-bot",
        "FIXER_RUNTIME": "python",
        "AGENT_IDLE_TIMEOUT_SECONDS": "-1",
        "AGENT_MAX_RUNTIME_SECONDS": "-1",
        "CODEX_TRANSPORT_RETRIES": "-1",
        "CODEX_BACKOFF_BASE_SECONDS": "0",
    })

    _assert_each_knob_flagged_once(c.validation_errors(), {
        "FIXER_RUNTIME", "AGENT_IDLE_TIMEOUT_SECONDS", "AGENT_MAX_RUNTIME_SECONDS",
        "CODEX_TRANSPORT_RETRIES", "CODEX_BACKOFF_BASE_SECONDS",
    })


def test_validate_raises_combined_error_message():
    c = from_env({"REPO": "x", "FIX_BOT": "fix-bot", "FIXER_RUNTIME": "python"})

    try:
        c.validate()
    except ValueError as exc:
        assert "FIXER_RUNTIME must be one of ['claude', 'codex', 'glm']" in str(exc)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("validate should reject invalid config")


def test_glm_is_an_accepted_fixer_runtime_when_a_profile_is_named():
    c = from_env({
        "REPO": "x", "FIX_BOT": "fix-bot",
        "FIXER_RUNTIME": "glm", "GLM_SETTINGS_FILE": "~/.cc-glm/settings.json",
    })

    assert c.fixer_runtime == "glm"
    assert c.glm_settings_file == "~/.cc-glm/settings.json"
    assert c.validation_errors() == ()


def test_glm_without_a_profile_is_a_config_error():
    """Unconfigured, the glm runtime is indistinguishable from plain Claude — it
    would quietly bill an Anthropic account instead of Z.ai."""
    c = from_env({"REPO": "x", "FIX_BOT": "fix-bot", "FIXER_RUNTIME": "glm"})

    _assert_each_knob_flagged_once(c.validation_errors(), {"GLM_SETTINGS_FILE"})


def test_glm_settings_file_is_irrelevant_on_other_runtimes():
    c = from_env({"REPO": "x", "FIX_BOT": "fix-bot", "FIXER_RUNTIME": "claude"})

    assert c.validation_errors() == ()


def test_jira_sync_without_project_key_stays_valid_and_inert():
    c = from_env({"REPO": "x", "FIX_BOT": "fix-bot", "JIRA_SYNC": "1"})

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
    c = from_env({"REPO": "x", "FIX_BOT": "fix-bot", "POLL_SECONDS": "45"})
    assert c.poll_seconds == 45
    assert c.validation_errors() == ()


def test_empty_spend_cap_falls_back_to_default_not_disabled():
    # review M2: `float(v or 0)` made an exported-but-empty ISSUE_SPEND_CAP_USD
    # silently mean "cap disabled" instead of "unset, use the default" —
    # describe() would then print issue=$0.00 as if that were intentional.
    c = from_env({"REPO": "x", "FIX_BOT": "fix-bot", "ISSUE_SPEND_CAP_USD": ""})
    assert c.issue_spend_cap_usd == 25.0
    assert c.validation_errors() == ()


def test_explicit_zero_spend_cap_still_means_disabled():
    # Anti-vacuity twin: an OPERATOR-CHOSEN 0 is a real, valid "off" and must
    # not be conflated with "unset" — only empty string means unset.
    c = from_env({"REPO": "x", "FIX_BOT": "fix-bot", "ISSUE_SPEND_CAP_USD": "0"})
    assert c.issue_spend_cap_usd == 0.0
    assert c.validation_errors() == ()


def test_empty_max_budget_falls_back_to_default_without_crashing():
    # review M2: MAX_BUDGET_USD= used to crash with a bare ValueError
    # (float("")). Must resolve to the documented default instead.
    c = from_env({"REPO": "x", "FIX_BOT": "fix-bot", "MAX_BUDGET_USD": ""})
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


# ── P1R-8: `_b` must strip+casefold and reject unrecognized tokens ───────────
@pytest.mark.parametrize(
    "token,expected",
    [
        ("False", False), ("FALSE", False), (" false ", False), ("No", False),
        ("Off", False), ("0", False), (" 0 ", False),
        ("True", True), ("TRUE", True), (" true ", True), ("Yes", True),
        ("On", True), ("1", True), (" 1 ", True),
    ],
)
def test_b_is_case_and_whitespace_insensitive(token, expected):
    # THE P1R-8 repro: AUTO_MERGE=False (capital F) used to compare unstripped
    # and case-sensitively against a raw ("0","false","no","off") tuple, miss
    # every one of them, and fall into "anything else is true" — turning
    # auto-merge ON when the operator asked for it OFF. Parametrized on one
    # knob per the acceptance note (full spelling set here, not every knob).
    assert from_env({"REPO": "x", "AUTO_MERGE": token}).auto_merge is expected


def test_b_records_a_validation_error_for_an_unrecognized_token_not_silent_truthy():
    # The rest of the fix: an unrecognized token must not silently default to
    # True (the old "anything not in the false set" behaviour) — it falls
    # back to the field default AND is reported, matching _coerce_int's
    # malformed-value contract.
    c = from_env({"REPO": "x", "FIX_BOT": "fix-bot", "AUTO_MERGE": "maybe"})
    assert c.auto_merge is True  # substituted the default (True), not a guess
    errors = c.validation_errors()
    assert any("AUTO_MERGE" in e and "maybe" in e for e in errors)


def test_b_well_formed_token_produces_no_validation_error():
    # Anti-vacuity twin: a recognized token must NOT be flagged.
    c = from_env({"REPO": "x", "FIX_BOT": "fix-bot", "AUTO_MERGE": "off"})
    assert c.auto_merge is False
    assert c.validation_errors() == ()


def test_b_absence_of_the_token_set_would_default_every_unrecognized_value_true():
    # ABSENCE test (review §5): proves the OLD bug shape directly — "anything
    # not in the false set" (no strip/casefold, no recognized-true-set) reads
    # "False" as truthy. If someone reverts `_b` to that shape, THIS assertion
    # (not just the happy-path parametrization above) documents the regression
    # it would reintroduce.
    old_buggy_contract = "False" not in ("0", "false", "no", "off")
    assert old_buggy_contract is True  # the bug: "False" reads as truthy
    assert from_env({"REPO": "x", "AUTO_MERGE": "False"}).auto_merge is False  # the fix


# ── P1R-9: non-finite floats must be rejected, not silently accepted ─────────
@pytest.mark.parametrize("bad", ["nan", "NaN", "inf", "-inf", "Infinity", "1e400"])
def test_coerce_float_rejects_non_finite_spend_cap(bad):
    # THE P1R-9 repro: ISSUE_SPEND_CAP_USD=nan used to pass `float()` cleanly,
    # and `_require_non_negative`'s `value < 0` is False for nan — so a nan
    # cap silently disabled the cap it names. `1e400` overflows float() to
    # `inf`, same failure mode via a spelling that looks like an ordinary number.
    c = from_env({"REPO": "x", "FIX_BOT": "fix-bot", "ISSUE_SPEND_CAP_USD": bad})
    assert c.issue_spend_cap_usd == 25.0  # substituted default, not nan/inf
    errors = c.validation_errors()
    assert any("ISSUE_SPEND_CAP_USD" in e and bad in e for e in errors)


def test_coerce_float_rejects_non_finite_wall_cap():
    # Same hole, the other cap the finding names: AGENT_MAX_RUNTIME_SECONDS=nan
    # used to make `cfg.agent_max_runtime > 0` False, disabling the wall cap.
    c = from_env({"REPO": "x", "FIX_BOT": "fix-bot", "AGENT_MAX_RUNTIME_SECONDS": "nan"})
    assert c.agent_max_runtime == 2700.0
    assert any("AGENT_MAX_RUNTIME_SECONDS" in e for e in c.validation_errors())


def test_coerce_float_still_accepts_ordinary_finite_values():
    # Anti-vacuity twin: a normal well-formed float must NOT be flagged.
    c = from_env({"REPO": "x", "FIX_BOT": "fix-bot", "ISSUE_SPEND_CAP_USD": "12.5"})
    assert c.issue_spend_cap_usd == 12.5
    assert c.validation_errors() == ()


def test_coerce_float_absence_of_isfinite_check_would_let_nan_through():
    # ABSENCE test (review §5): bare float() parses "nan" without raising —
    # proves the finite check, not the try/except, is what's doing the work.
    import math

    assert math.isnan(float("nan"))  # the old code path's blind spot
    assert not math.isfinite(float("nan"))  # …and what the fix now checks for


def test_poll_seconds_default_is_20_everywhere():
    # review S7: the dataclass default (was 60) used to disagree with
    # from_env's own literal ("20"). Both paths must now agree — from_env
    # reads its default off the dataclass field, so there is one source.
    assert HarnessConfig(repo="x").poll_seconds == 20
    assert from_env({"REPO": "x"}).poll_seconds == 20


def test_webhook_wake_is_off_by_default_and_polls_at_poll_seconds():
    c = from_env({"REPO": "x"})
    assert c.webhook_wake is False
    assert c.reconcile_seconds == 300
    assert c.tick_interval_seconds == c.poll_seconds == 20


def test_webhook_wake_demotes_the_poll_loop_to_the_reconcile_cadence():
    """The knob's whole observable effect: what a daemon sleeps between ticks.
    POLL_SECONDS is left alone so turning the knob back off restores it."""
    c = from_env({"REPO": "x", "WEBHOOK_WAKE": "1", "RECONCILE_SECONDS": "600"})
    assert c.webhook_wake is True
    assert c.poll_seconds == 20
    assert c.tick_interval_seconds == 600


def test_reconcile_seconds_must_be_positive():
    # 0 is not "disabled" here, it is a busy loop — same rule as POLL_SECONDS.
    errs = from_env({"REPO": "x", "RECONCILE_SECONDS": "0"}).validation_errors()
    assert any("RECONCILE_SECONDS" in e for e in errs)


def test_malformed_reconcile_seconds_falls_back_loudly():
    c = from_env({"REPO": "x", "RECONCILE_SECONDS": "soon"})
    assert c.reconcile_seconds == 300
    assert any("RECONCILE_SECONDS" in e and "soon" in e for e in c.validation_errors())


def test_describe_states_which_cadence_is_live():
    """Push mode changes what the interval MEANS; the boot line must not read
    like a 300s poll (the silent-default class this project keeps paying for)."""
    polling = from_env({"REPO": "x"}).describe()
    assert "poll=20s" in polling and "wake=webhook" not in polling
    pushed = from_env({"REPO": "x", "WEBHOOK_WAKE": "1"}).describe()
    assert "wake=webhook" in pushed and "reconcile=300s" in pushed


def test_state_dirs_default_per_repo():
    # review M5/D7: default STATE_DIR used to be per-role but not per-repo —
    # two repos' fix daemons shared issue-N.session and one resumed the
    # other's transcript. Assert the default is now namespaced by repo.
    #
    # R2-10: the join separator changed from "-" to "__" (acme-widget ->
    # acme__widget) to fix a collision ("/" and "-" both mapping to "-" let
    # `acme/app-b` and `acme-app/b` collide — see
    # test_repo_slug_does_not_collide_when_either_segment_has_a_hyphen below).
    # Nothing real is deployed yet, so this default-path shape change is safe.
    c = from_env({"REPO": "acme/widget"})
    assert c.state_dir.endswith("issue-agent/acme__widget")
    assert c.review_state_dir.endswith("review-agent/acme__widget")
    assert c.steward_state_dir.endswith("steward-agent/acme__widget")


def test_repo_slug_does_not_collide_when_either_segment_has_a_hyphen():
    # R2-10: the OLD `owner/name -> owner-name` join collided whenever EITHER
    # segment itself contained a hyphen, because "/" and "-" both mapped to
    # the same output character: `acme/app-b` and `acme-app/b` both produced
    # `acme-app-b`. Two genuinely distinct repos must get distinct state dirs.
    a = from_env({"REPO": "acme/app-b"})
    b = from_env({"REPO": "acme-app/b"})
    assert a.state_dir != b.state_dir
    assert a.review_state_dir != b.review_state_dir
    assert a.steward_state_dir != b.steward_state_dir


def test_repo_slug_is_case_insensitive_like_github_repo_identity():
    # R2-10: GitHub owner/repo identity is case-insensitive — `Acme/Widget`
    # and `acme/widget` name the SAME repo and must share ONE state dir, not
    # silently split it across two (the same failure shape M5/D7 closed for
    # genuinely distinct repos, but here for case variants of one repo).
    a = from_env({"REPO": "Acme/Widget"})
    b = from_env({"REPO": "acme/widget"})
    assert a.repo == b.repo == "acme/widget"
    assert a.repo_input == "Acme/Widget"
    assert a.state_dir == b.state_dir
    assert a.review_state_dir == b.review_state_dir
    assert a.steward_state_dir == b.steward_state_dir


def test_repo_slug_case_insensitivity_does_not_collapse_distinct_repos():
    # Anti-vacuity twin: casefolding must not make GENUINELY different repos
    # (not just case variants of the same one) collide.
    a = from_env({"REPO": "acme/widget"})
    b = from_env({"REPO": "acme/gizmo"})
    assert a.state_dir != b.state_dir


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


# ── M16: IDENTITY_MODE is an enum with a validation entry, not a bare string ──
# `IDENTITY_MODE=Three` used to sail through as a string and land BETWEEN the two
# guards that read it, which have OPPOSITE polarity: `!= "three"` disabled
# auto-merge while `== "solo"` left the reviewer enabled. The harness reviewed,
# approved, and never merged anything — with nothing in the log naming the cause.
def test_identity_mode_parses_to_the_enum():
    assert from_env({"REPO": "x", "IDENTITY_MODE": "solo"}).identity_mode is IdentityMode.SOLO
    assert from_env({"REPO": "x", "IDENTITY_MODE": "three"}).identity_mode is IdentityMode.THREE
    assert from_env({"REPO": "x"}).identity_mode is IdentityMode.THREE  # unset → default


def test_identity_mode_casing_typo_is_a_validation_error_not_a_silent_mode():
    c = from_env({"REPO": "x", "IDENTITY_MODE": "Three", "FIX_BOT": "f", "REVIEW_BOT": "r"})
    assert any("IDENTITY_MODE" in e and "Three" in e for e in c.validation_errors())
    # …and the value the daemons would run on is the SAFE default, not the typo:
    # nothing downstream may see a mode that matches neither guard.
    assert c.identity_mode is IdentityMode.THREE


def test_identity_mode_misspelling_is_a_validation_error_too():
    c = from_env({"REPO": "x", "IDENTITY_MODE": "thre", "FIX_BOT": "f", "REVIEW_BOT": "r"})
    assert any("IDENTITY_MODE" in e for e in c.validation_errors())


def test_identity_mode_whitespace_is_tolerated_not_flagged():
    # Anti-vacuity twin: a value that only differs by the whitespace a shell heredoc
    # or `export IDENTITY_MODE="solo "` leaves behind is NOT a misconfiguration.
    c = from_env({"REPO": "x", "IDENTITY_MODE": " solo "})
    assert c.identity_mode is IdentityMode.SOLO
    assert not any("IDENTITY_MODE" in e for e in c.validation_errors())


def test_identity_mode_bad_value_refuses_to_start():
    # validate() is what main.py calls: a mode nothing can act on must stop the
    # daemon at startup rather than produce a half-working harness.
    with pytest.raises(ValueError, match="IDENTITY_MODE"):
        from_env({"REPO": "x", "IDENTITY_MODE": "THREE"}).validate()


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


# ── P1R-6: trailing/leading whitespace on FIX_BOT must not defeat validation ──
def test_identity_cross_check_fires_despite_trailing_whitespace_on_fix_bot():
    # THE P1R-6 repro at the config layer: FIX_BOT='fix-bot ' (trailing space,
    # e.g. round-tripped through a shell env file/%q) used to defeat the
    # cross-check entirely — normalize_login didn't strip, so "fix-bot "
    # normalized to itself while "fix-bot[bot]" normalized to "fix-bot", and
    # the (unequal-looking) strings never tripped the self-approval guard.
    c = from_env({
        "REPO": "x", "IDENTITY_MODE": "three",
        "FIX_BOT": "fix-bot ", "REVIEW_BOT": "fix-bot[bot]",
    })
    errors = c.validation_errors()
    assert any("FIX_BOT" in e and "REVIEW_BOT" in e for e in errors)


def test_identity_cross_check_whitespace_does_not_flag_genuinely_distinct_bots():
    # Anti-vacuity twin: the whitespace-tolerant normalization must not start
    # flagging genuinely distinct identities just because one has padding.
    c = from_env({
        "REPO": "x", "IDENTITY_MODE": "three",
        "FIX_BOT": " fix-bot ", "REVIEW_BOT": "review-bot",
    })
    assert c.validation_errors() == ()


# ── P1R-7(b): FIX_BOT is now a required knob ──────────────────────────────────
def test_validation_requires_fix_bot_to_be_set():
    # Before this, validation_errors() had NO check for FIX_BOT at all — an
    # unset FIX_BOT sailed through validate() silently, and (pre-P1R-7(a))
    # `login_eq("", "")` being True meant an unresolvable PR author read as
    # the fixer's own.
    c = from_env({"REPO": "x"})
    assert any("FIX_BOT" in e for e in c.validation_errors())


def test_validation_requires_fix_bot_non_whitespace():
    # A whitespace-only FIX_BOT is functionally unset too.
    c = from_env({"REPO": "x", "FIX_BOT": "   "})
    assert any("FIX_BOT" in e for e in c.validation_errors())


def test_validation_does_not_flag_fix_bot_when_set():
    # Anti-vacuity twin.
    c = from_env({"REPO": "x", "FIX_BOT": "fix-bot"})
    assert not any("FIX_BOT" in e for e in c.validation_errors())


def test_validate_raises_when_fix_bot_unset():
    with pytest.raises(ValueError, match="FIX_BOT"):
        from_env({"REPO": "x"}).validate()


def test_absence_of_fix_bot_check_would_have_validated_silently():
    # ABSENCE test (review §5): isolates that NOTHING ELSE in
    # validation_errors() catches an unset FIX_BOT — only the dedicated check
    # added for P1R-7(b) does. If that check is ever removed, this assertion
    # (rather than just the happy-path tests above) makes the gap loud: the
    # remaining errors are empty even though FIX_BOT is unset.
    c = from_env({"REPO": "x"})  # FIX_BOT unset
    errors = c.validation_errors()
    non_fix_bot_errors = tuple(e for e in errors if "FIX_BOT" not in e)
    assert non_fix_bot_errors == ()  # nothing else would have caught this
    assert errors != ()  # …but the dedicated check does


def test_max_infra_deaths_default_and_override():
    assert from_env({"REPO": "x"}).max_infra_deaths == 5
    c = from_env({"REPO": "x", "MAX_INFRA_DEATHS": "2"})
    assert c.max_infra_deaths == 2
    assert c.fix_runtime.max_infra_deaths == 2


def test_sdk_max_buffer_default_clears_a_five_megabyte_read():
    # #13: the SDK's own 1MB ceiling is below one base64'd hi-DPI screenshot,
    # and blowing it kills the whole session's transport.
    c = from_env({"REPO": "x", "FIX_BOT": "fix-bot"})
    assert c.sdk_max_buffer_bytes >= 5 * 1024 * 1024 * 4 // 3
    assert c.validation_errors() == ()


def test_sdk_max_buffer_is_overridable_and_zero_means_the_sdk_default():
    assert from_env({"REPO": "x", "SDK_MAX_BUFFER_BYTES": "33554432"}).sdk_max_buffer_bytes == 33554432
    c = from_env({"REPO": "x", "FIX_BOT": "fix-bot", "SDK_MAX_BUFFER_BYTES": "0"})
    assert c.sdk_max_buffer_bytes == 0
    assert c.validation_errors() == ()


def test_negative_sdk_max_buffer_is_a_config_error():
    c = from_env({"REPO": "x", "FIX_BOT": "fix-bot", "SDK_MAX_BUFFER_BYTES": "-1"})
    assert any("SDK_MAX_BUFFER_BYTES" in e for e in c.validation_errors())


def test_max_infra_deaths_zero_is_a_valid_off_value():
    c = from_env({"REPO": "x", "FIX_BOT": "fix-bot", "MAX_INFRA_DEATHS": "0"})
    assert c.max_infra_deaths == 0
    assert c.validation_errors() == ()


def test_fix_dispatch_concurrency_defaults_to_two_and_can_go_serial():
    # Default 2 since 2026-08-02: one slot let a single ADDRESS_REVIEW leg
    # hold the deployment while other PRs' repairs queued behind it.
    assert from_env({"REPO": "x"}).fix_dispatch_concurrency == 2
    assert from_env({
        "REPO": "x",
        "FIX_DISPATCH_CONCURRENCY": "1",
    }).fix_dispatch_concurrency == 1


def test_fix_dispatch_concurrency_must_be_positive():
    cfg = from_env({
        "REPO": "x",
        "FIX_BOT": "fix-bot",
        "FIX_DISPATCH_CONCURRENCY": "0",
    })
    assert "FIX_DISPATCH_CONCURRENCY must be > 0" in cfg.validation_errors()


def test_codex_is_priced_out_of_the_box_so_the_spend_caps_are_not_blind():
    # A subscription bills $0, but the quota it draws down is real — so the
    # shipped rates are non-zero (synthetic dollars over real token volume) and
    # reach the runtime config the runner is built from.
    c = from_env({"REPO": "x"})
    assert c.codex_cost_per_mtok_in > 0
    assert c.codex_cost_per_mtok_out > c.codex_cost_per_mtok_in  # output costs more
    assert c.codex_runtime.cost_per_mtok_in == c.codex_cost_per_mtok_in
    assert c.codex_runtime.cost_per_mtok_out == c.codex_cost_per_mtok_out


def test_codex_cost_per_mtok_override():
    c2 = from_env({
        "REPO": "x", "FIX_BOT": "fix-bot",
        "CODEX_COST_PER_MTOK_IN": "3.00", "CODEX_COST_PER_MTOK_OUT": "15.00",
    })
    assert c2.codex_cost_per_mtok_in == 3.00
    assert c2.codex_cost_per_mtok_out == 15.00
    assert c2.codex_runtime.cost_per_mtok_out == 15.00
    assert c2.validation_errors() == ()


def test_codex_pricing_can_be_switched_off_explicitly():
    # "0" is a real operator choice (don't track codex spend), not a missing
    # value to be replaced by the default — and it must validate clean.
    c = from_env({
        "REPO": "x", "FIX_BOT": "fix-bot",
        "CODEX_COST_PER_MTOK_IN": "0", "CODEX_COST_PER_MTOK_OUT": "0",
        "ISSUE_SPEND_CAP_USD": "0", "DAILY_SPEND_CAP_USD": "0",
        "GLOBAL_DAILY_SPEND_CAP_USD": "0",
    })
    assert c.codex_cost_per_mtok_in == 0.0
    assert c.codex_cost_per_mtok_out == 0.0
    assert c.codex_runtime.cost_per_mtok_in == 0.0
    assert c.validation_errors() == ()


def test_armed_caps_require_pricing_for_subscription_runtimes_too():
    c = from_env({
        "REPO": "x",
        "FIX_BOT": "fix-bot",
        "FIXER_RUNTIME": "claude",
        "REVIEWER_RUNTIME": "claude",
        "CODEX_COST_PER_MTOK_IN": "0",
        "CODEX_COST_PER_MTOK_OUT": "0",
        "ISSUE_SPEND_CAP_USD": "5",
    })

    assert any(
        "armed spend caps require positive input/output pricing" in error
        for error in c.validation_errors()
    )


# ── #43: a PER-MODEL codex rate table ────────────────────────────────────────
# Routing moves price, not just token count, so a single flat rate makes the
# routed tiers indistinguishable in the ledger — which is exactly the split
# CLAUDE.md's routing guardrail asks for.


def test_the_rate_table_parses_a_model_per_entry():
    c = from_env({
        "REPO": "x", "FIX_BOT": "fix-bot",
        "CODEX_COST_PER_MTOK": "gpt-5.6-luna=1.25/10,gpt-5.6-terra=10/60",
    })
    assert c.codex_cost_per_mtok == (
        ("gpt-5.6-luna", 1.25, 10.0), ("gpt-5.6-terra", 10.0, 60.0),
    )
    assert c.codex_runtime.cost_per_mtok == c.codex_cost_per_mtok
    assert c.validation_errors() == ()


def test_an_unset_rate_table_is_empty_and_changes_nothing():
    """The knob is additive: an existing deployment prices exactly as before."""
    c = from_env({"REPO": "x", "FIX_BOT": "fix-bot"})
    assert c.codex_cost_per_mtok == ()
    assert c.validation_errors() == ()
    assert from_env({
        "REPO": "x", "FIX_BOT": "fix-bot", "CODEX_COST_PER_MTOK": "   ",
    }).codex_cost_per_mtok == ()


def test_whitespace_and_trailing_separators_in_the_rate_table_are_tolerated():
    c = from_env({
        "REPO": "x", "FIX_BOT": "fix-bot",
        "CODEX_COST_PER_MTOK": " gpt-5.6-luna = 1.25 / 10 , ,",
    })
    assert c.codex_cost_per_mtok == (("gpt-5.6-luna", 1.25, 10.0),)
    assert c.validation_errors() == ()


def test_a_malformed_rate_entry_is_named_and_fails_validation():
    """A silently-dropped entry is indistinguishable from a model the operator
    did not mean to override — both read as 'the fallback price', which is the
    bug this knob exists to fix."""
    c = from_env({
        "REPO": "x", "FIX_BOT": "fix-bot",
        "CODEX_COST_PER_MTOK": "gpt-5.6-luna,gpt-5.6-terra=10/60",
    })
    errors = c.validation_errors()
    assert any("gpt-5.6-luna" in e for e in errors), errors
    assert c.codex_cost_per_mtok == (("gpt-5.6-terra", 10.0, 60.0),), (
        "the well-formed entries survive"
    )


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
    c = from_env({
        "REPO": "x", "FIX_BOT": "fix-bot",
        "CODEX_COST_PER_MTOK": "gpt-5.6-luna=1.25/10,GPT-5.6-Luna=99/99",
    })
    assert c.codex_cost_per_mtok == (("gpt-5.6-luna", 1.25, 10.0),)
    assert any("more than once" in e for e in c.validation_errors())


# ── #29: describe() must not claim a bound codex cannot honour ───────────────


def test_describe_flags_the_fix_budget_as_unenforced_on_codex():
    """`fix_budget=$20.00` was printed on every codex boot while a resolve ran
    to $28.74 and exited `ok` — the startup log asserting a guarantee the
    runtime cannot make."""
    d = from_env({
        "REPO": "o/r", "FIXER_RUNTIME": "codex", "MAX_BUDGET_USD": "20",
        "AGENT_MAX_RUNTIME_SECONDS": "3600",
    }).describe()
    assert "fix_budget=$20.00" in d
    assert "NOT ENFORCED on codex" in d and "3600s" in d


def test_describe_says_nothing_bounds_a_codex_run_without_a_wall_cap():
    d = from_env({
        "REPO": "o/r", "FIXER_RUNTIME": "codex", "MAX_BUDGET_USD": "20",
        "AGENT_MAX_RUNTIME_SECONDS": "0",
    }).describe()
    assert "NOT ENFORCED on codex" in d
    assert "AGENT_MAX_RUNTIME_SECONDS is unset" in d


def test_describe_leaves_the_fix_budget_unqualified_on_the_sdk_runtime():
    """Anti-vacuity: claude DOES enforce the per-session budget, so the caveat
    must not follow the number everywhere."""
    d = from_env({"REPO": "o/r", "MAX_BUDGET_USD": "20"}).describe()
    assert "fix_budget=$20.00" in d
    assert "NOT ENFORCED" not in d


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


# --- AGENT_ENV file is the authority for harness knobs (#12) -----------------
# The relaunch loop re-sources the composed file into a long-lived shell, so a
# knob the operator DELETED is still exported from the previous reload. These
# pin the bidirectional contract: set AND unset both take effect.

def test_removed_knob_falls_back_to_default_not_inherited_value(tmp_path):
    env_file = tmp_path / "active.env"
    env_file.write_text('REPO=o/r\nDAILY_SPEND_CAP_USD=250\n')

    c = from_env({
        "AGENT_ENV": str(env_file),
        "REPO": "o/r",
        "FIX_MODEL": "gpt-5.6-sol",   # stale export from an earlier reload
        "DAILY_SPEND_CAP_USD": "150",
    })

    assert c.fix_model is None        # absent from the file → dataclass default
    assert c.daily_spend_cap_usd == 250.0


def test_file_value_wins_over_inherited_value(tmp_path):
    env_file = tmp_path / "active.env"
    env_file.write_text('REPO=o/r\nFIX_MODEL=haiku\nPOLL_SECONDS=45\n')

    c = from_env({
        "AGENT_ENV": str(env_file),
        "FIX_MODEL": "opus",
        "POLL_SECONDS": "20",
    })

    assert c.fix_model == "haiku"
    assert c.poll_seconds == 45


def test_shell_escaped_values_survive_the_file_read(tmp_path):
    # compose-env.sh writes `printf '%s=%q'`, so a real TEST_CMD/EDIT_CHECK_CMD
    # lands backslash-escaped. Mis-parsing one breaks every gate.
    env_file = tmp_path / "active.env"
    cmd = "set -e; (cd backend && uv run pytest) && pnpm exec tsc -b"
    quoted = subprocess.run(
        ["bash", "-c", 'printf "%s=%q" "$1" "$2"', "_", "EDIT_CHECK_CMD", cmd],
        capture_output=True, text=True, check=True,
    ).stdout
    env_file.write_text(f"REPO=o/r\n{quoted}\n")

    c = from_env({"AGENT_ENV": str(env_file)})

    assert c.edit_check_cmd == cmd


def test_unparseable_agent_env_file_falls_back_to_process_env(tmp_path):
    # Syntax the shell refuses (an unbalanced quote) must not reset the daemon
    # to defaults mid-reload.
    env_file = tmp_path / "active.env"
    env_file.write_text('REPO="o/r\nFIX_MODEL=haiku\n')

    c = from_env({"AGENT_ENV": str(env_file), "REPO": "o/inherited", "FIX_MODEL": "opus"})

    assert c.repo == "o/inherited"
    assert c.fix_model == "opus"


def test_missing_agent_env_file_falls_back_to_process_env(tmp_path):
    # A broken/absent watch file must never reset the daemon to defaults.
    c = from_env({
        "AGENT_ENV": str(tmp_path / "does-not-exist.env"),
        "REPO": "o/r",
        "FIX_MODEL": "haiku",
    })

    assert c.repo == "o/r"
    assert c.fix_model == "haiku"


def test_empty_agent_env_file_falls_back_to_process_env(tmp_path):
    # A file that assigns nothing reads as "every knob absent" — refuse it
    # rather than silently defaulting REPO/TEST_CMD and failing every gate.
    env_file = tmp_path / "active.env"
    env_file.write_text("# AUTO-GENERATED by bin/compose-env.sh — do not edit.\n")

    c = from_env({"AGENT_ENV": str(env_file), "REPO": "o/r", "FIX_MODEL": "haiku"})

    assert c.repo == "o/r"
    assert c.fix_model == "haiku"


def test_no_agent_env_keeps_the_pure_dict_mapping(tmp_path):
    c = from_env({"REPO": "o/r", "FIX_MODEL": "haiku"})
    assert c.repo == "o/r" and c.fix_model == "haiku"


# ── FIX_EFFORT / ESCALATE_ON_EVIDENCE (#16) ───────────────────────────────────
def test_fix_effort_parses_to_the_domain_enum():
    from issue_agent.domain.effort import Effort

    assert from_env({"REPO": "x", "FIX_EFFORT": "high"}).fix_effort is Effort.HIGH
    assert from_env({"REPO": "x", "FIX_EFFORT": " HIGH "}).fix_effort is Effort.HIGH


def test_unset_fix_effort_leaves_each_runtime_on_its_own_default():
    """Unset must mean "say nothing", not "medium" — pinning a value here would
    silently override an operator's CODEX_REASONING_EFFORT."""
    assert from_env({"REPO": "x"}).fix_effort is None
    assert from_env({"REPO": "x", "FIX_EFFORT": ""}).fix_effort is None


def test_a_misspelled_fix_effort_is_reported_and_falls_back():
    c = from_env({"REPO": "x", "FIX_EFFORT": "hihg"})
    assert c.fix_effort is None
    assert any("FIX_EFFORT" in e for e in c.validation_errors())


def test_escalation_defaults_on_and_can_be_switched_off():
    assert from_env({"REPO": "x"}).escalate_on_evidence is True
    assert from_env({"REPO": "x", "ESCALATE_ON_EVIDENCE": "0"}).escalate_on_evidence is False
    assert from_env({"REPO": "x", "ESCALATE_ON_EVIDENCE": "False"}).escalate_on_evidence is False


def test_fix_effort_reaches_the_fix_runtime_view():
    from issue_agent.domain.effort import Effort

    assert from_env({"REPO": "x", "FIX_EFFORT": "low"}).fix_runtime.effort is Effort.LOW
