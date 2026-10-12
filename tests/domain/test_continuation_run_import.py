import json
import pytest
from issue_agent.domain.continuation_import import parse_legacy_continuation_run
from issue_agent.domain.workflow_identity import IssueRef, PullRequestRef, WorkflowFamily


def parsed(**changes):
    payload = {'repository':'o/r','role':'fix','kind':'RESOLVE','number':7,'issue':7,'breaker_key':7}
    payload.update(changes)
    return parse_legacy_continuation_run(run_id='run',owner_id='o/r:'+str(payload['role'])+':owner',
        kind=payload['kind'],dispatch_id=json.dumps(payload),workload_repository='o/r')


def test_issue_and_pr_histories_keep_distinct_targets_and_linkage():
    issue=parsed()
    pr=parsed(kind='FIX_CI',number=9)
    unlinked=parsed(kind='REBASE',number=9,issue=None,breaker_key=-9)
    assert (issue.key.target,issue.family)==(IssueRef(7),WorkflowFamily.ISSUE_RECOVERY)
    assert (pr.key.target,pr.linked_issue)==(PullRequestRef(9),IssueRef(7))
    assert unlinked.key.target==PullRequestRef(9) and unlinked.linked_issue is None


@pytest.mark.parametrize('change',[{'number':True},{'number':None},{'number':0},{'number':-7},
 {'number':2**63},{'issue':True},{'issue':None},{'issue':8},{'breaker_key':True},
 {'breaker_key':-7},{'kind':'REVIEW'},{'role':'other'},{'repository':'other/repo'},
 {'kind':'REBASE','number':9,'issue':7,'breaker_key':-9},
 {'kind':'REBASE','number':9,'issue':None,'breaker_key':-8},
 {'kind':'REBASE','number':9,'issue':None,'breaker_key':0}])
def test_contradictory_identity_never_attributes_history(change):
    with pytest.raises(ValueError):parsed(**change)


@pytest.mark.parametrize('raw',['null','[]','{"repository":"o/r","repository":"x/y"}',
 '{"x":NaN}','{"x":Infinity}','{' ,'x'*65537])
def test_malformed_dispatch_fails_as_value_error(raw):
    with pytest.raises(ValueError):
        parse_legacy_continuation_run(run_id='run',owner_id='o/r:fix:owner',kind='RESOLVE',
            dispatch_id=raw,workload_repository='o/r')


@pytest.mark.parametrize('field,value',[('owner_id','o/r:steward:owner'),('owner_id','o/r:fix:'),
 ('kind','ROUTE'),('workload_repository','x/y'),('run_id',None)])
def test_row_and_accounting_identity_must_agree(field,value):
    kwargs=dict(run_id='run',owner_id='o/r:fix:owner',kind='RESOLVE',workload_repository='o/r',
                dispatch_id=json.dumps(dict(repository='o/r',role='fix',kind='RESOLVE',number=7,issue=7,breaker_key=7)))
    kwargs[field]=value
    with pytest.raises(ValueError):parse_legacy_continuation_run(**kwargs)


@pytest.mark.parametrize('kind,number,issue,counter', [
    ('RESOLVE', 7, 7, 7), ('ROUTE', 7, 7, 7), ('ADDRESS_REVIEW', 9, 7, 7),
])
def test_older_dispatch_without_repository_uses_agreeing_owner_and_accounting(kind, number, issue, counter):
    result = parse_legacy_continuation_run(
        run_id='run', owner_id='o/r:fix:owner', kind=kind, workload_repository='o/r',
        dispatch_id=json.dumps(dict(role='fix', kind=kind, number=number, issue=issue, breaker_key=counter)),
    )
    assert result.key.repository == 'o/r'
    assert result.key.target == (PullRequestRef(number) if kind == 'ADDRESS_REVIEW' else IssueRef(issue))


@pytest.mark.parametrize('field,value', [
    ('workload_repository', ''), ('workload_repository', 'x/y'), ('owner_id', 'x/y:fix:owner'),
])
def test_missing_repository_cannot_replace_missing_or_conflicting_corroboration(field, value):
    arguments = dict(run_id='run', owner_id='o/r:fix:owner', kind='RESOLVE', workload_repository='o/r',
                     dispatch_id=json.dumps(dict(role='fix', kind='RESOLVE', number=7, issue=7, breaker_key=7)))
    arguments[field] = value
    with pytest.raises(ValueError):
        parse_legacy_continuation_run(**arguments)


@pytest.mark.parametrize('repository', [None, '', False, []])
def test_present_repository_must_be_valid_even_when_owner_and_accounting_agree(repository):
    with pytest.raises(ValueError):
        parsed(repository=repository)
