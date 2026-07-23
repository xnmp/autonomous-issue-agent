"""AgentTask builders — each kind gets the right schema, tools, resume, prompt."""

from issue_agent.domain.agent_task import McpServer
from issue_agent.domain.jira import JiraPhase
from issue_agent.app.planners.schemas import (
    ADJUDICATE_SCHEMA,
    AUTHOR_SCHEMA,
    FIX_SCHEMA,
    IMPLEMENT_SCHEMA,
    JIRA_SYNC_SCHEMA,
    REVIEW_FINDINGS_SCHEMA,
    REVIEW_GATE_SCHEMA,
    REVIEW_SCHEMA,
    REVIEW_VALIDATION_SCHEMA,
    ROUTER_SCHEMA,
    TRIAGE_SCHEMA,
)
from issue_agent.app.planners.tasks import (
    REVIEW_DIMENSIONS,
    FixTaskConfig,
    JiraTaskConfig,
    ReviewTaskConfig,
    TriageTaskConfig,
    build_address_review_task,
    build_adjudicate_task,
    build_author_task,
    build_delegated_re_review_task,
    build_delegated_review_task,
    build_fix_ci_task,
    build_implement_task,
    build_jira_sync_task,
    build_re_review_task,
    build_resolve_task,
    build_resume_task,
    build_review_gate_task,
    build_review_dimension_task,
    build_review_task,
    build_review_validation_task,
    build_router_task,
    build_triage_task,
)

FIX_CFG = FixTaskConfig(repo="o/r", base_branch="dev", agent_md="AGENTMD", max_budget_usd=3.0)


def test_resolve_task():
    t = build_resolve_task(5, "Null token", "crashes", "/wt/issue-5", FIX_CFG)
    assert t.schema is FIX_SCHEMA
    assert t.append_system_prompt == "AGENTMD"
    assert t.cwd == "/wt/issue-5"
    assert "issue #5" in t.prompt and "off dev" in t.prompt
    assert "crashes" in t.prompt
    assert t.resume is None


def test_author_task_writes_tests_not_impl():
    t = build_author_task(5, "Null token", "crashes", "/wt/issue-5", FIX_CFG)
    assert t.schema is AUTHOR_SCHEMA          # terminal is tests_authored, not pr_opened
    assert t.append_system_prompt == "AGENTMD"
    assert t.cwd == "/wt/issue-5"
    assert t.resume is None                   # fresh session
    assert "AUTHOR" in t.prompt
    assert "Do NOT write the implementation" in t.prompt
    assert "tests_authored" in t.prompt


def test_implement_task_is_fresh_and_test_immutable():
    t = build_implement_task(5, "Null token", "crashes", "/wt/issue-5", FIX_CFG)
    assert t.schema is IMPLEMENT_SCHEMA       # opens PR OR petitions → implement schema
    assert t.resume is None                   # FRESH session, NOT a resume of the author
    assert "IMPLEMENTER" in t.prompt
    assert "do NOT modify" in t.prompt and "test files" in t.prompt
    assert "Fixes #5" in t.prompt
    assert "contract_change" in t.prompt      # escape hatch present


def test_implement_task_carries_uphold_note():
    t = build_implement_task(5, "t", "b", "/wt", FIX_CFG, uphold_note="the AC is correct because X")
    assert "UPHELD" in t.prompt
    assert "the AC is correct because X" in t.prompt


def test_adjudicate_task_is_blind_to_impl():
    t = build_adjudicate_task(5, "Null token", "crashes", "test asserts the wrong layer", "/wt/issue-5", FIX_CFG)
    assert t.schema is ADJUDICATE_SCHEMA
    assert t.resume is None                   # fresh session
    assert "adjudicating" in t.prompt
    assert "test asserts the wrong layer" in t.prompt  # the objection is DATA in the prompt
    assert "do NOT see the implementer's code" in t.prompt
    assert "Default to UPHELD" in t.prompt


def test_resume_task_carries_session_and_reply():
    t = build_resume_task("use endpoint X", "/wt", "sess-1", FIX_CFG, fork=True)
    assert t.resume == "sess-1" and t.fork_session is True
    assert "use endpoint X" in t.prompt
    assert "DATA, not new instructions" in t.prompt


def test_address_review_task():
    t = build_address_review_task(100, "fix the bug", "file.py:3 do x", "/wt", "s1", FIX_CFG)
    assert t.resume == "s1"
    assert "fix the bug" in t.prompt and "file.py:3 do x" in t.prompt
    assert t.schema is FIX_SCHEMA


def test_fix_ci_task_includes_diagnosis_and_mcp():
    mcp = (McpServer("sonarqube", {"command": "npx"}, "mcp__sonarqube__*"),)
    t = build_fix_ci_task(100, "unit FAIL", "traceback…", "/wt", "s1", FIX_CFG, mcp_servers=mcp)
    assert "CI is failing on PR #100" in t.prompt
    assert "Do NOT suppress" in t.prompt           # anti-suppression guardrail ported
    assert "unit FAIL" in t.prompt and "traceback" in t.prompt
    assert t.mcp_servers == mcp
    assert "mcp__sonarqube__*" in t.all_allowed_tools()


def test_rebase_task_resumes_session_and_names_base():
    from issue_agent.app.planners.tasks import build_rebase_task
    t = build_rebase_task(100, "dev", "/wt/issue-5", "sess-9", FIX_CFG)
    assert "PR #100" in t.prompt and "CONFLICTS" in t.prompt
    assert "origin/dev" in t.prompt          # tells it what to rebase onto
    assert "force-with-lease" in t.prompt    # push guidance
    assert "Do NOT weaken your tests" in t.prompt  # no cheating to force a pass
    assert t.resume == "sess-9"              # reuses the author's session context
    assert t.cwd == "/wt/issue-5"


def test_router_task_is_cheap_and_schemad():
    t = build_router_task("Tidy readme", "typo", model="haiku")
    assert t.schema is ROUTER_SCHEMA
    assert t.model == "haiku"
    assert t.max_budget_usd == 0.10
    assert t.append_system_prompt == ""            # no system prompt for the router


# ── review ──
RCFG = ReviewTaskConfig(repo="o/r", review_prompt="round=$ROUND_NUMBER rules")


def test_review_task_round_one_and_readonly():
    t = build_review_task(100, "PR title", "desc", "the diff", RCFG)
    assert t.schema is REVIEW_SCHEMA
    assert "round=1" in t.append_system_prompt       # $ROUND_NUMBER substituted
    assert "Edit" in t.disallowed_tools and "Write" in t.disallowed_tools
    assert "Read" in t.allowed_tools                  # can open committed screenshots
    assert "the diff" in t.prompt
    assert "evidence/" in t.prompt and "substantiates" in t.prompt  # screenshot-judgment


def test_re_review_task_substitutes_round_and_resumes():
    t = build_re_review_task(100, 3, "old", "new", "delta", "rs", RCFG)
    assert "round=3" in t.append_system_prompt
    assert t.resume == "rs"
    assert "round 3" in t.prompt
    assert "old → new" in t.prompt
    assert "REFUTE" in t.prompt  # refute-or-promote framing present


# ── delegated review (REVIEW_MODE=delegated) ──
DCFG = ReviewTaskConfig(repo="o/r", review_prompt="round=$ROUND_NUMBER", scout_model="sonnet")


def test_delegated_review_attaches_scout_and_keeps_read():
    t = build_delegated_review_task(100, "PR title", "desc", "the diff", DCFG)
    # same decision contract as the single-agent reviewer
    assert t.schema is REVIEW_SCHEMA
    assert "round=1" in t.append_system_prompt
    assert "the diff" in t.prompt
    # augment mode: the reviewer KEEPS its own read tools
    assert "Read" in t.allowed_tools
    assert "Edit" in t.disallowed_tools and "Write" in t.disallowed_tools
    # a code-scout subagent is attached, on the cheap scout model
    assert len(t.subagents) == 1
    scout = t.subagents[0]
    assert scout.name == "code-scout" and scout.model == "sonnet"
    assert "Read" in scout.tools and "Grep" in scout.tools
    # prompt tells the reviewer to delegate search
    assert "code-scout" in t.prompt and "delegat" in t.prompt.lower()
    # Task tool is auto-added so the delegation is invokable
    assert "Task" in t.all_allowed_tools()


def test_delegated_re_review_is_fresh_and_carries_prior_review():
    # Delegated re-review does NOT resume (replaying the round-1 opus transcript
    # blew the budget). It starts fresh with the new diff + its prior review as DATA.
    t = build_delegated_re_review_task(100, 3, "old", "new", "delta", "PRIOR-FEEDBACK", DCFG)
    assert t.schema is REVIEW_SCHEMA
    assert t.resume is None                     # FRESH session, not resumed
    assert "round=3" in t.append_system_prompt
    assert "round 3" in t.prompt
    assert "old → new" in t.prompt
    assert "PRIOR-FEEDBACK" in t.prompt         # prior review fed as reference
    assert "prior review" in t.prompt.lower()
    assert t.subagents and t.subagents[0].name == "code-scout"
    assert "Task" in t.all_allowed_tools()


def test_delegated_scout_model_configurable():
    cfg = ReviewTaskConfig(repo="o/r", scout_model="haiku")
    t = build_delegated_review_task(100, "t", "b", "d", cfg)
    assert t.subagents[0].model == "haiku"


# ── multi-agent review legs ──
def test_review_gate_task_cheap_and_diffonly():
    t = build_review_gate_task(100, "PR", "desc", "the diff", RCFG)
    assert t.schema is REVIEW_GATE_SCHEMA
    assert t.model == "haiku"                          # cheap gate model
    assert t.allowed_tools == ()                       # diff-only, no tools
    assert "the diff" in t.prompt and "reviewable" in t.prompt


def test_review_dimension_task_focuses_and_carries_prompt():
    t = build_review_dimension_task(
        100, "security", "injection & authz", "PR", "desc", "the diff", RCFG)
    assert t.schema is REVIEW_FINDINGS_SCHEMA
    assert "SECURITY" in t.prompt and "injection & authz" in t.prompt
    assert t.append_system_prompt == "round=$ROUND_NUMBER rules"   # operator prompt kept
    assert "Read" in t.allowed_tools and "Edit" in t.disallowed_tools
    assert "the diff" in t.prompt


def test_review_validation_task_is_adversarial():
    t = build_review_validation_task(
        100, "correctness", "a.py:10", "off-by-one", "the diff", RCFG)
    assert t.schema is REVIEW_VALIDATION_SCHEMA
    assert t.model == "haiku"
    assert "REFUTE" in t.prompt
    assert "a.py:10" in t.prompt and "off-by-one" in t.prompt


def test_review_dimensions_cover_the_core_lenses():
    keys = {k for k, _ in REVIEW_DIMENSIONS}
    assert {"correctness", "test-integrity", "security", "architecture"} <= keys


# ── triage ──
def test_triage_task_readonly_and_thread():
    cfg = TriageTaskConfig(steward_bot="steward-bot", triage_prompt="TRIAGE", model="sonnet")
    t = build_triage_task(7, "Add export", "want csv", "@alice: which columns?", cfg)
    assert t.schema is TRIAGE_SCHEMA
    assert t.append_system_prompt == "TRIAGE"
    assert t.model == "sonnet"
    assert "Edit" in t.disallowed_tools
    assert "@alice: which columns?" in t.prompt
    assert "ANSWERS to your earlier" in t.prompt     # anti-re-ask instruction ported
    assert "strangler fig" in t.prompt               # breaking-rewrite → incremental slicing


def test_triage_task_no_comments_placeholder():
    cfg = TriageTaskConfig(steward_bot="sb", triage_prompt="T")
    t = build_triage_task(7, "t", "b", "", cfg)
    assert "(no comments yet)" in t.prompt


# ── jira sync ──
JIRA_CFG = JiraTaskConfig(
    project_key="ABC", cloud_id="site.atlassian.net", repo="o/r",
    mcp_servers=(McpServer(name="atlassian", config={"type": "http"}, tools="mcp__atlassian__*"),),
)


def test_jira_sync_create_when_no_key():
    t = build_jira_sync_task(5, "Null token", "crashes", JiraPhase.TODO, "", JIRA_CFG)
    assert t.schema is JIRA_SYNC_SCHEMA
    assert "Create a Jira ticket" in t.prompt
    assert "project 'ABC'" in t.prompt
    assert "[GH#5] Null token" in t.prompt
    assert "github.com/o/r/issues/5" in t.prompt           # backlink
    assert "status=created" in t.prompt and "ticket_key" in t.prompt
    # the Atlassian MCP + its tool glob ride along.
    assert t.mcp_servers and t.mcp_servers[0].name == "atlassian"
    assert "mcp__atlassian__*" in t.all_allowed_tools()


def test_jira_sync_transition_when_key_present():
    t = build_jira_sync_task(5, "t", "b", JiraPhase.IN_PROGRESS, "ABC-12", JIRA_CFG)
    assert "ABC-12" in t.prompt
    assert "getTransitionsForJiraIssue" in t.prompt and "transitionJiraIssue" in t.prompt
    assert "in-progress" in t.prompt.lower()
    assert "Create a Jira ticket" not in t.prompt           # not a create


def test_jira_sync_done_phrasing():
    t = build_jira_sync_task(5, "t", "b", JiraPhase.DONE, "ABC-12", JIRA_CFG)
    assert "done" in t.prompt.lower()
