"""Revision-bound GitHub required-check observation (Contents/Metadata:read)."""

from urllib.parse import quote
from dataclasses import replace

from ..domain.ci import GREEN_CONCLUSIONS, RED_CONCLUSIONS, parse_timestamp

from ..domain.checks import BaseIssue, BaseRun, Check, CIObservation, Observed, RequiredCheck, Unavailable
from ..domain.identity import login_eq

_METADATA = """query($owner:String!,$repo:String!,$number:Int!){repository(owner:$owner,name:$repo){
 pullRequest(number:$number){id headRefOid baseRefName
 potentialMergeCommit{id oid statusCheckRollup{contexts{totalCount}}}
 commits(last:1){nodes{commit{id oid statusCheckRollup{state}}}}}}}"""
_REPORTS = """query($owner:String!,$repo:String!,$number:Int!,$commit:ID!,$pr:ID!,$endCursor:String){
 repository(owner:$owner,name:$repo){pullRequest(number:$number){id headRefOid baseRefName
 potentialMergeCommit{id oid statusCheckRollup{contexts{totalCount}}}}}
 node(id:$commit){... on Commit{oid statusCheckRollup{contexts(first:100,after:$endCursor){
 totalCount pageInfo{hasNextPage endCursor} nodes{__typename
 ... on CheckRun{id databaseId name status conclusion startedAt detailsUrl isRequired(pullRequestId:$pr)
 checkSuite{app{databaseId} workflowRun{runAttempt createdAt}}}
 ... on StatusContext{id createdAt context state targetUrl isRequired(pullRequestId:$pr)}}}}}}}"""


def _requirements(items, app_key):
    if not isinstance(items, list):
        raise ValueError("required contexts are absent or malformed")
    return frozenset(RequiredCheck(item["context"], None if item.get(app_key) in (None, -1) else item[app_key])
                     for item in items)


def required_policy(repo, base, query):
    branch = query(["api", f"repos/{repo}/branches/{quote(base, safe='')}"])
    if not isinstance(branch, dict) or type(branch.get("protected")) is not bool:
        raise ValueError("branch protection summary is unavailable")
    protection = branch.get("protection")
    policy = frozenset()
    if branch["protected"] or protection is not None:
        if not isinstance(protection, dict) or "required_status_checks" not in protection:
            raise ValueError("branch protection summary is incomplete")
        required = protection["required_status_checks"]
        if required is not None:
            if not isinstance(required, dict) or not isinstance(required.get("contexts"), list):
                raise ValueError("classic required-check policy is malformed")
            bound = _requirements(required["checks"], "app_id") if "checks" in required else frozenset()
            policy = bound | frozenset(RequiredCheck(name) for name in required["contexts"]
                                      if name not in {r.context for r in bound})
    pages = query(["api", "--paginate", "--slurp", f"repos/{repo}/rules/branches/{quote(base, safe='')}?per_page=100"])
    if not isinstance(pages, list) or not pages:
        raise ValueError("ruleset pagination is unavailable")
    for index, page in enumerate(pages):
        if not isinstance(page, list) or index < len(pages)-1 and len(page) != 100:
            raise ValueError("ruleset page is malformed")
        for rule in page:
            if not isinstance(rule, dict) or not isinstance(rule.get("type"), str) or not rule["type"]:
                raise ValueError("branch rule is malformed")
            if rule["type"] == "required_status_checks":
                policy |= _requirements(rule["parameters"]["required_status_checks"], "integration_id")
    return policy


def _report(node):
    kind = node["__typename"]
    if type(node["isRequired"]) is not bool or not isinstance(node["id"], str) or not node["id"]:
        raise ValueError("report eligibility or identity is malformed")
    if kind == "CheckRun":
        state, conclusion = node["status"], node["conclusion"]
        if state not in {"QUEUED", "IN_PROGRESS", "WAITING", "PENDING", "REQUESTED", "COMPLETED"}:
            raise ValueError("unknown check status")
        if state == "COMPLETED" and conclusion not in {"SUCCESS", "NEUTRAL", "SKIPPED", "FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STALE", "STARTUP_FAILURE"}:
            raise ValueError("unknown check conclusion")
        app = node["checkSuite"]["app"]
        app_id = app["databaseId"] if app is not None else None
        run = node["checkSuite"].get("workflowRun") or {}
        attempt, created = run.get("runAttempt"), run.get("createdAt")
        url = node["detailsUrl"]
        if node["startedAt"] is not None and not isinstance(node["startedAt"], str):
            raise ValueError("check start time is malformed")
        name, start = node["name"], node["startedAt"] or ""
        bucket = "pending" if state != "COMPLETED" else "pass" if conclusion in {"SUCCESS", "NEUTRAL", "SKIPPED"} else "fail"
        state = conclusion if state == "COMPLETED" else state
    elif kind == "StatusContext":
        state, name, start, app_id, url = node["state"], node["context"], node["createdAt"], None, node["targetUrl"]
        attempt = created = None
        if state not in {"EXPECTED", "PENDING", "SUCCESS", "ERROR", "FAILURE"}:
            raise ValueError("unknown commit status")
        bucket = "pass" if state == "SUCCESS" else "pending" if state in {"EXPECTED", "PENDING"} else "fail"
    else:
        raise ValueError("unsupported report type")
    if url is not None and not isinstance(url, str):
        raise ValueError("report URL is malformed")
    import re
    match = re.search(r"/actions/runs/([0-9]+)(?:/|$)", url or "")
    if kind == "CheckRun" and match:  # an Actions job: when its run began, as for every job of the run
        start = created if isinstance(created, str) else ""
    check_id = node.get("databaseId") if kind == "CheckRun" else None
    if isinstance(check_id, str) and check_id.isascii() and check_id.isdecimal():
        check_id = int(check_id)
    return Check(name, state, bucket, start, int(match[1]) if match else None,
                 node["isRequired"], app_id, "check" if kind == "CheckRun" else "status",
                 check_id if type(check_id) is int and check_id > 0 else None,
                 attempt if match and type(attempt) is int and attempt > 0 else None)


def base_reports(nodes) -> tuple[Check, ...]:
    """The base tip's reports. A branch commit has no PR to be required for:
    the PR's own policy decides which of its failures matter (#313). An
    Actions job counts only from a push run running or ended red or green,
    as base-CI issues do (#442): another event's run tested a PR, and the
    failed jobs of a cancelled run are no verdict."""
    if not isinstance(nodes, list):
        raise ValueError("base reports are malformed")
    return tuple(_report({**node, "isRequired": False}) for node in nodes if _the_bases(node))


def _the_bases(node) -> bool:
    suite = node.get("checkSuite") if isinstance(node, dict) else None
    if not isinstance(suite, dict) or not isinstance(run := suite.get("workflowRun"), dict):
        return True  # a commit status or another app's check run (`_report` validates it)
    ended = suite.get("conclusion")
    return run.get("event") == "push" and (ended is None or str(ended).lower() in RED_CONCLUSIONS | GREEN_CONCLUSIONS)


def base_runs(payload, base: str) -> tuple[BaseRun, ...]:
    """One page of the base branch's push runs, completed or running (no
    conclusion: a newer run still running holds a green close, #442). A
    malformed row makes the page unread: dropping it could make an older run
    the latest."""
    rows = payload.get("workflow_runs") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise ValueError("base run listing is absent or malformed")
    rows = [r for r in rows if (r.get("head_branch"), r.get("event")) == (base, "push")]
    if any(parse_timestamp(str(r.get("created_at"))) is None or parse_timestamp(str(r.get("updated_at"))) is None
           or not isinstance(r.get("conclusion") or "", str) for r in rows):
        raise ValueError("base run conclusion or times are malformed")
    return tuple(BaseRun(r.get("name"), r.get("id"), r.get("html_url"), r.get("head_sha"),
                         (r.get("conclusion") or "unknown").lower() if r.get("status") == "completed" else "",
                         r["created_at"], r["updated_at"]) for r in rows)


def base_issues(search, filer: str) -> tuple[BaseIssue, ...]:
    """The issues `filer` opened in one page of issue-search results, newest
    first: one issue per red streak accumulates (#402). A page shorter than
    the count says (up to 100) is unread."""
    nodes = search.get("nodes") if isinstance(search, dict) else None
    count = search.get("issueCount") if isinstance(search, dict) else None
    if (not isinstance(nodes, list) or type(count) is not int or len(nodes) != min(count, 100)
            or any(not isinstance(n, dict) or type(n.get("number")) is not int or not isinstance(n.get("title"), str)
                   or n.get("state") not in ("OPEN", "CLOSED") or n["state"] == "CLOSED" and not n.get("closedAt")
                   for n in nodes)):
        raise ValueError("issue search is absent, malformed or short")
    return tuple(BaseIssue(n["number"], n["title"], "" if n["state"] == "OPEN" else n["closedAt"], _closer(n))
                 for n in nodes if login_eq(str((n.get("author") or {}).get("login") or ""), filer))


def _closer(node: dict) -> str:
    """The commit that closed an issue (#442), "" when absent or unreadable."""
    events = (node.get("timelineItems") or {}).get("nodes") if isinstance(node.get("timelineItems"), dict) else None
    closer = events[-1].get("closer") if isinstance(events, list) and events and isinstance(events[-1], dict) else None
    if not isinstance(closer, dict):
        return ""
    oid = (closer.get("mergeCommit") or {}).get("oid") if isinstance(closer.get("mergeCommit"), dict) else closer.get("oid")
    return oid if isinstance(oid, str) and oid else ""


def active_workflows(payload, listing) -> frozenset[str] | None:
    """The names of the repository's active workflows, from one page of its
    listing; None (no issue is closed as orphaned, #449) when a row is
    malformed, the page is short of the count, or a workflow is named apart
    from its runs in the base's run `listing` (its name may be another
    branch's: a rename on its way)."""
    rows = payload.get("workflows") if isinstance(payload, dict) else None
    if (not isinstance(rows, list) or type(payload.get("total_count")) is not int or len(rows) != payload["total_count"]
            or any(not isinstance(r, dict) or not isinstance(r.get("name"), str) for r in rows)):
        return None
    named = {r.get("path"): r["name"] for r in rows}
    if any(named.get(r.get("path"), r.get("name")) != r.get("name") for r in listing["workflow_runs"] if r.get("event") == "push"):
        return None
    return frozenset(r["name"] for r in rows if r.get("state") == "active")


def required_workflows(policy: frozenset[RequiredCheck], nodes: list) -> frozenset[str]:
    """The workflows of the base tip's check runs that a required context
    names (#402): whose base-CI issue the harness readies."""
    contexts = {r.context for r in policy}
    return frozenset(name for n in nodes if isinstance(n, dict) and n.get("__typename") == "CheckRun"
                     and n.get("name") in contexts
                     and isinstance(name := (((n.get("checkSuite") or {}).get("workflowRun") or {})
                                             .get("workflow") or {}).get("name"), str) and name)


def _checked_merge(selected):
    merge = selected["potentialMergeCommit"]
    if merge is None or merge["statusCheckRollup"] is None:
        return None
    count = merge["statusCheckRollup"]["contexts"]["totalCount"]
    if type(count) is not int or count < 0:
        raise ValueError("test-merge check count is malformed")
    return merge if count else None


def _current(repo, reports, query):
    """The reports without jobs of an earlier attempt of their run (owner, PR
    #403), which the rollup lists until the latest attempt's replace them: each
    reads as pending while that attempt has no jobs or unfinished ones. The
    run's other checks began when its latest attempt did (`Check.started_at`)."""
    old, settling, begun = {}, {}, {}
    for run, attempt in sorted({(c.run_id, c.run_attempt) for c in reports if c.kind == "check" and (c.run_attempt or 1) > 1}):
        pages = query(["api", "--paginate", "--slurp", f"repos/{repo}/actions/runs/{run}/jobs?filter=all&per_page=100"])
        if not isinstance(pages, list) or not pages or not all(isinstance(p, dict) and isinstance(p.get("jobs"), list)
                                                               for p in pages):
            raise ValueError("re-run jobs are unavailable")
        jobs = [job for page in pages for job in page["jobs"]]
        if any(not isinstance(job, dict) or any(type(job.get(k)) is not int or job[k] <= 0 for k in ("id", "run_attempt"))
               for job in jobs):
            raise ValueError("re-run job identity is malformed")
        old[run] = frozenset(job["id"] for job in jobs if job["run_attempt"] < attempt)
        latest = [job for job in jobs if job["run_attempt"] == attempt]
        settling[run] = not latest or any(job.get("status") != "completed" for job in latest)
        # The attempt began when its jobs were created (copies included); unreadable reads as "".
        begun[run] = min((at if isinstance(at := job.get("created_at"), str) else "" for job in latest), default="")

    def superseded(check):
        return check.kind == "check" and check.run_id in old and check.check_id in old[check.run_id]

    return tuple(replace(c, started_at=begun[c.run_id]) if c.kind == "check" and c.run_id in begun else c
                 for c in reports if not superseded(c)) + tuple(
        replace(c, state="QUEUED", bucket="pending", check_id=None) for c in reports
        if superseded(c) and settling[c.run_id])


def observe_checks(repo: str, pr, query) -> CIObservation:
    try:
        policy = required_policy(repo, pr.base_ref_name, query)
        by_context = {}
        for requirement in policy:
            by_context.setdefault(requirement.context, set()).add(requirement.app_id)
        owner, name = repo.split("/", 1)
        args = ["api", "graphql", "-f", f"owner={owner}", "-f", f"repo={name}", "-F", f"number={pr.number}"]
        raw = query([*args, "-f", f"query={_METADATA}"])
        if not isinstance(raw, dict) or raw.get("errors"):
            raise ValueError("PR check revision is unavailable")
        selected = raw["data"]["repository"]["pullRequest"]
        if (selected["headRefOid"] != pr.head_ref_oid or selected["baseRefName"] != pr.base_ref_name
                or not isinstance(selected["id"], str) or not selected["id"]):
            raise ValueError("PR moved during check observation")
        commit = selected["commits"]["nodes"][0]["commit"]
        if commit["oid"] != pr.head_ref_oid:
            raise ValueError("head commit does not match PR")
        if policy:
            commit = _checked_merge(selected) or commit
        if any(not isinstance(commit[key], str) or not commit[key] for key in ("id", "oid")):
            raise ValueError("tested commit identity is malformed")
        initial_rollup = commit["statusCheckRollup"]
        pages = query([*args, "--paginate", "--slurp", "-f", f"commit={commit['id']}",
                       "-f", f"pr={selected['id']}", "-f", f"query={_REPORTS}"])
        if not isinstance(pages, list) or not pages:
            raise ValueError("check rollup pagination is unavailable")
        reports, seen_ids, cursors, count = {}, set(), set(), None
        for index, page in enumerate(pages):
            if not isinstance(page, dict) or page.get("errors"):
                raise ValueError("check rollup page is unavailable")
            current = page["data"]["repository"]["pullRequest"]
            if any(current[key] != selected[key] for key in ("id", "headRefOid", "baseRefName")) or page["data"]["node"]["oid"] != commit["oid"]:
                raise ValueError("PR/revision changed during pagination")
            if policy and _checked_merge(current) != _checked_merge(selected):
                raise ValueError("test-merge revision changed during pagination")
            rollup = page["data"]["node"]["statusCheckRollup"]
            if rollup is None:
                if len(pages) != 1 or initial_rollup is not None:
                    raise ValueError("rollup disappeared during pagination")
                break
            connection = rollup["contexts"]
            total, info, nodes = connection["totalCount"], connection["pageInfo"], connection["nodes"]
            if (type(total) is not int or total < 0 or count is not None and total != count
                    or not isinstance(nodes, list) or type(info["hasNextPage"]) is not bool
                    or info["hasNextPage"] != (index < len(pages)-1)):
                raise ValueError("partial or inconsistent check pagination")
            cursor = info["endCursor"]
            if cursor is not None and (not isinstance(cursor, str) or not cursor):
                raise ValueError("check pagination cursor is malformed")
            if info["hasNextPage"] and (not isinstance(cursor, str) or not cursor or cursor in cursors or not nodes):
                raise ValueError("check pagination cursor is malformed")
            cursors.add(cursor)
            count = total
            for node in nodes:
                check = _report(node)
                if check.kind == "status" and check.required:
                    matching = by_context.get(check.name, set())
                    if len(matching) == 1:
                        check = replace(check, app_id=next(iter(matching)))
                    elif any(app_id is not None for app_id in matching):
                        raise ValueError("required status app provenance is ambiguous")
                if node["id"] in seen_ids:
                    raise ValueError("duplicate check across pages")
                seen_ids.add(node["id"])
                key = (check.kind, check.name, check.app_id)
                if policy and check.kind == "check":
                    key += (node["id"],)
                if check.kind == "check":
                    identity = node["databaseId"]
                    if isinstance(identity, str) and identity.isascii() and identity.isdecimal():
                        identity = int(identity)
                    if type(identity) is not int or identity <= 0:
                        raise ValueError("check execution identity is malformed")
                else:
                    identity = parse_timestamp(node["createdAt"])
                    if identity is None:
                        raise ValueError("status creation time is malformed")
                score = (identity, check.started_at) if check.kind == "check" else (identity, "")
                previous = reports.get(key)
                if policy and previous is not None and score == previous[0] and check != previous[1]:
                    raise ValueError("ambiguous latest report")
                if previous is None or score > previous[0]:
                    reports[key] = (score, check)
        if count is not None and len(seen_ids) != count:
            raise ValueError("check pagination omitted reports")
        reports = tuple(check for _, check in reports.values())
        if any(c.required and policy and not (None in by_context.get(c.name, ())
                or c.app_id in by_context.get(c.name, ())) for c in reports):
            raise ValueError("required reports contradict policy inventory")
        reports = _current(repo, reports, query)
        if required_policy(repo, pr.base_ref_name, query) != policy:
            raise ValueError("required policy changed during observation")
        return Observed(pr.head_ref_oid, commit["oid"], policy, reports)
    except Exception as exc:
        return Unavailable(f"required-check observation: {type(exc).__name__}: {str(exc)[:240]}")
