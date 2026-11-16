"""Dependency-free B06 regression suite for restricted environments."""
import hashlib
import tempfile
import sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from multiagent_quality.consensus import evaluate_consensus, weighted_score
from multiagent_quality.evidence import sign_report, check_evidence
from multiagent_quality.plan import validate_plan
from multiagent_quality.runner import run_case
from multiagent_quality.validation import ContractError
ROLES=['product','user','tech','critic']; HUMANS=[f'human-{i}' for i in range(6)]
def cons():return {'proposal_version':'P1','required_roles':ROLES,'required_reviewers':HUMANS,'round':1,'max_rounds':2,'stances':[{'role_id':r,'proposal_version':'P1','stance':'Agree'} for r in ROLES],'human_reviews':[{'reviewer_id':h,'proposal_version':'P1','decision':'APPROVE'} for h in HUMANS],'objections':[],'verification':{'required_checks_passed':True,'evidence_current':True}}
def check(name,ok):
    if not ok:raise AssertionError(name)
def main():
    check('consensus',evaluate_consensus(cons())['can_publish'])
    s=cons();s['stances'][0]['stance']='Disagree';s['objections']=[{'objection_id':'O1','proposal_version':'P1','status':'open','severity':'blocking','kind':'value','reason':'scope'}]
    check('blocking opposition',evaluate_consensus(s)['status']=='NO_CONSENSUS')
    check('score',abs(weighted_score({'value':5,'cost':1},{'value':.7,'cost':.3})-3.8)<1e-9)
    p={'spec_version':'S1','contract_version':'C1','tasks':[{'task_id':'A','goal':'a','req_refs':['R1'],'outputs':['a'],'depends_on':['B'],'acceptance':['x'],'required_test_ids':['T-A']},{'task_id':'B','goal':'b','req_refs':['R1'],'outputs':['b'],'depends_on':['A'],'acceptance':['x'],'required_test_ids':['T-B']}]}
    check('plan cycle',not validate_plan(p,['R1','R2'])['accepted'])
    temp_root = Path(__file__).resolve().parents[1] / '.test-tmp'
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=temp_root) as td:
        root=Path(td);log=root/'log.txt';log.write_text('ok',encoding='utf8');key=b'k'*32
        report={'schema_version':1,'context':{'task_id':'T1','attempt_id':1},'started_at':'2026-11-13T00:00:00+00:00','finished_at':'2026-11-13T00:00:01+00:00','cases':[{'test_id':'C1','status':'PASS','exit_code':0,'log_path':'log.txt','log_sha256':hashlib.sha256(b'ok').hexdigest()}],'artifacts':[]}
        env=sign_report(report,key);check('evidence',check_evidence(env,key=key,expected_context=report['context'],required_test_ids=['C1'],artifact_root=root,required_artifacts=[],now=datetime(2026,11,13,0,0,2,tzinfo=timezone.utc))['accepted'])
        report['context']['attempt_id']=2;check('tamper',not check_evidence(env,key=key,expected_context={'task_id':'T1','attempt_id':1},required_test_ids=['C1'],artifact_root=root,required_artifacts=[],now=datetime(2026,11,13,0,0,2,tzinfo=timezone.utc))['accepted'])
        check('timeout',run_case(['python','-c','import time;time.sleep(5)'],cwd=root,log_file=root/'t.log',timeout_seconds=1)['status']=='TIMEOUT')
    print('B06 smoke: PASS')
if __name__=='__main__':main()
