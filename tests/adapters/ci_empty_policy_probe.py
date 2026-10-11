import json
from issue_agent.adapters.github import GitHubGateway
from issue_agent.domain.ci import ci_state
from issue_agent.domain.fix import FixConfig, FixState, decide_fix
from issue_agent.domain.world import WorldState
from issue_agent.domain.actions import Target
from issue_agent.domain.self_clear import MachineBlock, MachineBlocker

def report(name='unit', identity=10, conclusion='SUCCESS', workflow='CI', event='push', state='COMPLETED', start=None):
    return {'__typename':'CheckRun','id':str(identity),'databaseId':identity,'name':name,
        'status':state,'conclusion':conclusion,'startedAt':start,'detailsUrl':f'https://github.com/o/r/actions/runs/{identity}/job/1',
        'isRequired':False,'checkSuite':{'app':{'databaseId':1},'workflowRun':{'event':event,'workflow':{'name':workflow}}}}

def run_case(case, *, head_nodes, merge_nodes=None, merge_state='CLEAN', spent=False, retries=0, blocked=False):
    metadata={'id':'pr-id','headRefOid':'head','baseRefName':'dev',
        'potentialMergeCommit':None if merge_nodes is None else {'id':'merge-id','oid':'merge-sha','statusCheckRollup':{'state':'SUCCESS'}},
        'commits':{'nodes':[{'commit':{'id':'head-id','oid':'head','statusCheckRollup':None}}]}}
    pr={'number':7,'headRefName':'fix/7-example','headRefOid':'head','baseRefName':'dev','author':{'login':'fix-bot'},
        'state':'OPEN','mergeable':'MERGEABLE','mergeStateStatus':merge_state,'labels':[{'name':'ai-pr-open'}],
        'reviews':[{'state':'APPROVED','author':{'login':'review-bot'},'commit':{'oid':'head'}}], 'body':'Closes #7'}
    if blocked:
        pr['labels'].append({'name':'ai-blocked'})
    def runner(argv, token):
        if argv[1:3]==['pr','view']: return json.dumps(pr)
        if any('/rules/branches/' in a for a in argv): return '[[]]'
        if any('/branches/' in a for a in argv): return '{"protected":false}'
        if 'graphql' in argv:
            if '--paginate' not in argv: return json.dumps({'data':{'repository':{'pullRequest':metadata}}})
            is_merge='commit=merge-id' in argv
            nodes=merge_nodes if is_merge else head_nodes
            return json.dumps([{'data':{'repository':{'pullRequest':{k:metadata[k] for k in ['id','headRefOid','baseRefName']}},
                'node':{'oid':'merge-sha' if is_merge else 'head','statusCheckRollup':{'contexts':{'totalCount':len(nodes),
                'pageInfo':{'hasNextPage':False,'endCursor':None},'nodes':nodes}}}}}])
        if any('/check-runs' in a for a in argv):
            return json.dumps([{'check_runs':[{'id':c['databaseId'],'name':c['name'],'app':{'id':1},'status':c['status'].lower(),
                'conclusion':(c['conclusion'] or '').lower(),'started_at':c['startedAt'],'details_url':c['detailsUrl']} for c in head_nodes if c['__typename']=='CheckRun']}])
        if any('/statuses' in a for a in argv):
            return json.dumps([[{'context':c['context'],'state':c['state'].lower(),'created_at':c['createdAt'],'target_url':c['targetUrl']}
                for c in head_nodes if c['__typename']=='StatusContext']])
        if any('/actions/runs/' in a for a in argv): return 'failure' if '--jq' in argv else '{}'
        raise AssertionError(argv)
    gateway=GitHubGateway('o/r',runner=runner)
    view=gateway.get_pr(7)
    observation=view.ci if hasattr(view,'ci') else view.checks
    state=FixState(ci_rerun_head={7:'head'} if spent else {}, ci_retries={7:retries},
        machine_blocks={7:MachineBlock(7,Target.PR,7,7,MachineBlocker.CHECKS,'head')} if blocked else {})
    actions=decide_fix(WorldState(prs=(view,),now='2026-10-12T02:00:00Z'),FixConfig(fix_bot='fix-bot'),state,fixer_busy=False)
    return {'case':case,'ci':ci_state(observation).value,'checks':[[c.name,c.bucket,c.run_id] for c in view.checks],
        'actions':[type(a).__name__+(':'+a.kind.name if hasattr(a,'kind') else '') for a in actions]}

def cases():
    return [
        run_case('empty-policy-red-head-green-merge',head_nodes=[report(conclusion='FAILURE')],merge_nodes=[report(identity=11,event='pull_request')],merge_state='UNSTABLE'),
        run_case('same-name-distinct-workflows',head_nodes=[report(conclusion='FAILURE',workflow='CI-linux'),report(identity=11,workflow='CI-mac')],spent=True),
        run_case('same-name-distinct-events',head_nodes=[report(conclusion='FAILURE',event='pull_request'),report(identity=11,event='push')],spent=True),
        run_case('all-queued',head_nodes=[report(state='QUEUED',conclusion=None,start='2020-01-01T00:00:00Z')]),
        run_case('latest-rerun',head_nodes=[report(conclusion='FAILURE'),report(identity=11)],spent=True),
        run_case('empty-reports',head_nodes=[]),
        run_case('empty-reports-with-retry-debt',head_nodes=[],retries=2),
        run_case('empty-reports-check-blocker',head_nodes=[],retries=2,blocked=True),
        run_case('empty-policy-hooks',head_nodes=[],merge_state='HAS_HOOKS'),
        run_case('empty-policy-unstable',head_nodes=[report()],merge_state='UNSTABLE'),
        run_case('external-status-stalled',head_nodes=[{'__typename':'StatusContext','id':'status-1','context':'external-CI',
            'state':'PENDING','createdAt':'2026-10-12T00:00:00Z','targetUrl':None,'isRequired':False}]),
    ]


if __name__ == '__main__':
    for case in cases():
        print(json.dumps(case))
