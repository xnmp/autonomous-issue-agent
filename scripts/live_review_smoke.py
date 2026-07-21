"""Live end-to-end smoke of the multi-agent reviewer.

Drives the REAL AgentDispatcher + REAL AgentRunner (live SDK, real LLM legs)
with a fake gateway that serves a diff carrying two PLANTED defects:
  1. a SQL-injection (string-formatted query on user input)  → security should flag
  2. a tautological test (asserts a literal against itself)  → test-integrity should flag
Plus one clean function so the reviewer must NOT invent issues everywhere.

Expected: gate=reviewable, security + test-integrity raise blocking findings,
validators confirm them, synthesis → REQUEST_CHANGES naming the two files.

Run:  uv run --with claude-agent-sdk python scripts/live_review_smoke.py
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "src")

from issue_agent.app.dispatch import AgentDispatcher, DispatchConfig
from issue_agent.app.execute import Executor
from issue_agent.adapters.agent_runner import AgentRunner
from issue_agent.app.planners.tasks import ReviewTaskConfig
from issue_agent.domain.actions import AgentKind, RunAgent
from issue_agent.domain.review import ReviewMode
from issue_agent.domain.world import PRView

PLANTED_DIFF = '''\
diff --git a/app/users.py b/app/users.py
index 1111111..2222222 100644
--- a/app/users.py
+++ b/app/users.py
@@ -10,6 +10,14 @@ import sqlite3
 def get_conn():
     return sqlite3.connect("app.db")

+def find_user(conn, username):
+    # PLANTED DEFECT 1: SQL injection — username interpolated into the query.
+    cur = conn.cursor()
+    cur.execute("SELECT * FROM users WHERE name = '%s'" % username)
+    return cur.fetchone()
+
+def add(a, b):
+    return a + b
diff --git a/tests/test_users.py b/tests/test_users.py
index 3333333..4444444 100644
--- a/tests/test_users.py
+++ b/tests/test_users.py
@@ -1,2 +1,9 @@
 import app.users as users
+
+def test_add():
+    # PLANTED DEFECT 2: tautological — asserts a literal against itself,
+    # never calls users.add, so it passes even if add() is deleted.
+    result = 4
+    assert result == 4
+
+def test_find_user_smoke():
+    assert users is not None
'''


class FakeGW:
    def __init__(self):
        self.submitted = None

    def get_pr(self, n):
        return PRView(n, head_ref_oid="deadbeef", title="Add user lookup + helper",
                      body="Adds find_user() and an add() helper, with tests.")

    def pr_diff(self, n):
        return PLANTED_DIFF

    def submit_review(self, pr, event, body):
        self.submitted = (event, body)
        print(f"\n{'='*70}\nSUBMITTED REVIEW  event={event}\n{'='*70}\n{body}\n{'='*70}")
        return True

    def relabel(self, *a):
        print(f"relabel: {a}")


class MemStore:
    def __init__(self):
        self.d = {}

    def get_pr_session(self, p): return self.d.get(("s", p), "")
    def save_pr_session(self, p, s): self.d[("s", p)] = s
    def get_last_sha(self, p): return self.d.get(("sha", p), "")
    def set_last_sha(self, p, v): self.d[("sha", p)] = v
    def get_round(self, p): return self.d.get(("r", p), 1)
    def set_round(self, p, n): self.d[("r", p)] = n


async def main() -> int:
    log = lambda m: print(f"[log] {m}")
    runner = AgentRunner(log=log, debug=False)
    cfg = DispatchConfig(
        review=ReviewTaskConfig("demo/repo", review_prompt=""),
        review_mode=ReviewMode.MULTI,
    )
    gw = FakeGW()
    store = MemStore()
    executor = Executor(gw, log=log)
    d = AgentDispatcher(cfg, gw, store, None, runner, executor)

    await d.dispatch(RunAgent(AgentKind.REVIEW, 1))

    if gw.submitted is None:
        print("\nRESULT: FAIL — no review submitted")
        return 1
    event, body = gw.submitted
    # Both planted defects must be independently caught — an OR here would let the
    # SQLi alone satisfy the check and silently assert nothing about test-integrity
    # (a finding the reviewer itself raised on this script). Require BOTH seams.
    # Match the full path — "users.py" is a substring of "test_users.py", so a
    # body naming only the test file would falsely satisfy both seams.
    caught_sqli = "app/users.py" in body
    caught_taut = "test_users" in body
    ok = event == "REQUEST_CHANGES" and caught_sqli and caught_taut
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'} — event={event}, "
          f"sqli(users.py)={'yes' if caught_sqli else 'NO'}, "
          f"tautological-test(test_users)={'yes' if caught_taut else 'NO'}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
