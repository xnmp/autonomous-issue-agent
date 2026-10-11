from issue_agent.app.config import from_env


def test_retired_screenshot_receipt_configuration_is_ignored():
    baseline = {"REPO": "o/r"}
    assert from_env({**baseline, "HARNESS_SCREENSHOTS_READ": "relative/retired"}) == from_env(baseline)
