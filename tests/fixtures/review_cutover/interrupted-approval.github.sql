-- Captured from origin/dev 8251a3764c35d82c89338e2c910aa6ed7bd0d886 (schema 41).
INSERT INTO "calls" VALUES('["submit_review", 7, "APPROVE", "38cb3395762c439062d884b0f9d5bd4872414a19"]');
INSERT INTO "clock" VALUES(0);
INSERT INTO "diffs" VALUES('38cb3395762c439062d884b0f9d5bd4872414a19','diff --git a/change0.py b/change0.py
--- a/change0.py
+++ b/change0.py
@@ -1,1 +1,2 @@
 context
+change0 = True
');
INSERT INTO "diffs" VALUES('96ad8de0ebd764f6e70b0c481ea0dcd94bd4ade0','diff --git a/change1.py b/change1.py
--- a/change1.py
+++ b/change1.py
@@ -1,1 +1,2 @@
 context
+change1 = True
');
INSERT INTO "diffs" VALUES('eb735cba61c1f478f16256ae1235c585f93426a2','diff --git a/change2.py b/change2.py
--- a/change2.py
+++ b/change2.py
@@ -1,1 +1,2 @@
 context
+change2 = True
');
INSERT INTO "diffs" VALUES('c260b19a233ea2f213a9b98769700606b7127de8','diff --git a/change3.py b/change3.py
--- a/change3.py
+++ b/change3.py
@@ -1,1 +1,2 @@
 context
+change3 = True
');
INSERT INTO "issues" VALUES(1,'{"number": 1, "title": "Add thing", "body": "", "labels": [], "comments": [], "updated_at": "", "author": "", "state": "open", "state_reason": "", "blocked_by_open": false, "open_blocker_numbers": [], "declared_paths": [], "predicted_paths": []}');
INSERT INTO "prs" VALUES(7,'{"number": 7, "head_ref_name": "fix/1-thing", "head_ref_oid": "38cb3395762c439062d884b0f9d5bd4872414a19", "base_ref_name": "dev", "title": "Add thing", "body": "Adds the thing.", "labels": ["ai-pr-open"], "comments": [], "reviews": [], "ci": {"head": "38cb3395762c439062d884b0f9d5bd4872414a19", "tested_revision": "38cb3395762c439062d884b0f9d5bd4872414a19", "policy": [], "reports": []}, "state": "OPEN", "closing_issue": 1, "changed_files": [], "mergeable": "", "merge_state": "", "is_draft": false, "author": "fix-bot", "base_ref_oid": "acb1e42d936063b8a72091d3455e4cc8e4ac3fb5", "content_known": true}');
INSERT INTO "reviews" VALUES(1,7,'submitted-0','review-bot[bot]','APPROVED','38cb3395762c439062d884b0f9d5bd4872414a19','2026-10-12T11:14:50.317806+00:00','verdict 0: approve

<!-- issue-agent-review-command id=f31a5e3647bbcf6da9652b6479c5bca05c4f69356d5012de31964d4ce399de73 -->');
