"""Reviewer context must make every test exception impossible to overlook."""

from issue_agent.app.planners.tasks import (
    ReviewTaskConfig,
    build_re_review_task,
    build_review_task,
)


_DIFF = """\
diff --git a/.harness/test-immutability-exceptions b/.harness/test-immutability-exceptions
new file mode 100644
--- /dev/null
+++ b/.harness/test-immutability-exceptions
@@ -0,0 +1 @@
+124 | tests/test_cost.py | old assertion ignores cached tokens | tests/test_cost_regression.py
"""


def test_exception_is_loud_in_initial_and_reviewer_full_diff_contexts():
    cfg = ReviewTaskConfig(repo="o/r", review_prompt="round=$ROUND_NUMBER")
    tasks = (
        build_review_task(100, "title", "body", _DIFF, cfg),
        build_re_review_task(100, 2, "old", "new", _DIFF, "session", cfg),
    )

    for task in tasks:
        assert "TEST IMMUTABILITY EXCEPTION" in task.prompt
        assert "issue #124" in task.prompt
        assert "tests/test_cost.py" in task.prompt
        assert "old assertion ignores cached tokens" in task.prompt
        assert "tests/test_cost_regression.py" in task.prompt
