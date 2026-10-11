"""Revision-bound GitHub required-check observation (Contents/Metadata:read)."""

from urllib.parse import quote
from dataclasses import replace

from ..domain.ci import parse_timestamp

from ..domain.checks import Check, CIObservation, Observed, RequiredCheck, Unavailable

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
 checkSuite{app{databaseId}}}
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
        url = node["detailsUrl"]
        if node["startedAt"] is not None and not isinstance(node["startedAt"], str):
            raise ValueError("check start time is malformed")
        name, start = node["name"], node["startedAt"] or ""
        bucket = "pending" if state != "COMPLETED" else "pass" if conclusion in {"SUCCESS", "NEUTRAL", "SKIPPED"} else "fail"
        state = conclusion if state == "COMPLETED" else state
    elif kind == "StatusContext":
        state, name, start, app_id, url = node["state"], node["context"], node["createdAt"], None, node["targetUrl"]
        if state not in {"EXPECTED", "PENDING", "SUCCESS", "ERROR", "FAILURE"}:
            raise ValueError("unknown commit status")
        bucket = "pass" if state == "SUCCESS" else "pending" if state in {"EXPECTED", "PENDING"} else "fail"
    else:
        raise ValueError("unsupported report type")
    if url is not None and not isinstance(url, str):
        raise ValueError("report URL is malformed")
    import re
    match = re.search(r"/actions/runs/([0-9]+)(?:/|$)", url or "")
    return Check(name, state, bucket, start, int(match[1]) if match else None,
                 node["isRequired"], app_id, "check" if kind == "CheckRun" else "status")


def _checked_merge(selected):
    merge = selected["potentialMergeCommit"]
    if merge is None or merge["statusCheckRollup"] is None:
        return None
    count = merge["statusCheckRollup"]["contexts"]["totalCount"]
    if type(count) is not int or count < 0:
        raise ValueError("test-merge check count is malformed")
    return merge if count else None


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
        if required_policy(repo, pr.base_ref_name, query) != policy:
            raise ValueError("required policy changed during observation")
        return Observed(pr.head_ref_oid, commit["oid"], policy, reports)
    except Exception as exc:
        return Unavailable(f"required-check observation: {type(exc).__name__}: {str(exc)[:240]}")
