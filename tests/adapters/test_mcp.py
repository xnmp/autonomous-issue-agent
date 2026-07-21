"""FIX_MCP_CONFIG → McpServer attachment for the CI-fix leg (pure, faked loader)."""

from issue_agent.app.mcp import mcp_servers_for_checks, mcp_servers_from_file

_SONAR = '{"SonarQube": {"config": "config/mcp/sonarqube.json", "tools": "mcp__sonarqube__*"}}'
_FILE = {"mcpServers": {"sonarqube": {"type": "stdio", "command": "npx", "args": ["-y", "x"]}}}
_ATLAS = {"mcpServers": {"atlassian": {"type": "http", "url": "https://mcp.atlassian.com/v1/mcp"}}}


def _load_ok(path):
    return _FILE


def test_blank_config_is_empty():
    assert mcp_servers_for_checks("", ["Anything"]) == ()
    assert mcp_servers_for_checks("   ", ["Anything"]) == ()


def test_match_attaches_inner_spec_and_tools():
    servers = mcp_servers_for_checks(_SONAR, ["SonarQube Code Analysis"], "/root", load=_load_ok)
    assert len(servers) == 1
    s = servers[0]
    assert s.name == "sonarqube"
    assert s.config == _FILE["mcpServers"]["sonarqube"]   # the INNER spec, not the wrapper
    assert s.tools == "mcp__sonarqube__*"


def test_no_matching_check_attaches_nothing():
    assert mcp_servers_for_checks(_SONAR, ["Format Backend", "Build"], "/root", load=_load_ok) == ()


def test_regex_key_matches_substring():
    # the key is an extended regex tested against each check name.
    cfg = '{"Sonar|Wiz": {"config": "x.json", "tools": "mcp__t__*"}}'
    assert mcp_servers_for_checks(cfg, ["Wiz Scan"], load=_load_ok)
    assert mcp_servers_for_checks(cfg, ["Snyk"], load=_load_ok) == ()


def test_missing_file_is_skipped_not_raised():
    def boom(path):
        raise FileNotFoundError(path)

    logs = []
    out = mcp_servers_for_checks(_SONAR, ["SonarQube"], "/root", load=boom, log=logs.append)
    assert out == ()
    assert any("not found" in m for m in logs)


def test_invalid_json_config_ignored():
    assert mcp_servers_for_checks("{not json", ["X"]) == ()


def test_invalid_regex_key_skipped():
    cfg = '{"(unclosed": {"config": "x.json", "tools": "t"}}'
    # bad regex → skipped, no crash
    assert mcp_servers_for_checks(cfg, ["anything"], load=_load_ok) == ()


def test_relative_path_resolves_against_harness_root():
    seen = []

    def capture(path):
        seen.append(path)
        return _FILE

    mcp_servers_for_checks(_SONAR, ["SonarQube"], "/harness", load=capture)
    assert seen == ["/harness/config/mcp/sonarqube.json"]


# ── mcp_servers_from_file (the steward's Atlassian MCP loader) ──
def test_from_file_loads_spec_and_tools():
    servers = mcp_servers_from_file(
        "config/mcp/atlassian.json", "mcp__atlassian__*", "/root", load=lambda p: _ATLAS
    )
    assert len(servers) == 1
    assert servers[0].name == "atlassian"
    assert servers[0].config == _ATLAS["mcpServers"]["atlassian"]
    assert servers[0].tools == "mcp__atlassian__*"


def test_from_file_blank_path_is_empty():
    assert mcp_servers_from_file("", "mcp__atlassian__*") == ()


def test_from_file_missing_file_is_failsoft():
    def boom(path):
        raise FileNotFoundError(path)

    assert mcp_servers_from_file("nope.json", "t", load=boom) == ()


def test_from_file_resolves_relative_against_harness_root():
    seen = []
    mcp_servers_from_file("config/mcp/atlassian.json", "t", "/h",
                          load=lambda p: seen.append(p) or _ATLAS)
    assert seen == ["/h/config/mcp/atlassian.json"]
