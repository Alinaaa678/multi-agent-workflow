import hashlib, json
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pytest
from multiagent_quality.consensus import evaluate_consensus, weighted_score
from multiagent_quality.evidence import sign_report, check_evidence
from multiagent_quality.plan import validate_plan
from multiagent_quality.runner import run_case
from multiagent_quality.validation import ContractError

ROLES=["product","user","tech","critic"]
HUMANS=[f"human-{i}" for i in range(6)]
def consensus(**overrides):
    data={"proposal_version":"P1","required_roles":ROLES,"required_reviewers":HUMANS,"round":1,"max_rounds":2,
      "stances":[{"role_id":r,"proposal_version":"P1","stance":"Agree"} for r in ROLES],
      "human_reviews":[{"reviewer_id":h,"proposal_version":"P1","decision":"APPROVE"} for h in HUMANS],
      "objections":[],"verification":{"required_checks_passed":True,"evidence_current":True}}
    data.update(overrides);return data
def test_unanimous_requires_all_humans():
    result=evaluate_consensus(consensus(human_reviews=[]))
    assert result["status"]=="NO_CONSENSUS" and not result["can_publish"]
def test_blocking_disagreement_wins_over_majority():
    s=consensus();s["stances"][0]={"role_id":"product","proposal_version":"P1","stance":"Disagree"}
    s["objections"]=[{"objection_id":"O1","proposal_version":"P1","status":"open","severity":"blocking","kind":"value","reason":"scope"}]
    assert evaluate_consensus(s)["status"]=="NO_CONSENSUS"
def test_fact_objection_requires_evidence():
    s=consensus(objections=[{"objection_id":"O1","proposal_version":"P1","status":"open","severity":"nonblocking","kind":"fact"}])
    assert evaluate_consensus(s)["status"]=="NEEDS_VERIFICATION"
def test_deferred_nonblocking_requires_six_signoffs():
    s=consensus(objections=[{"objection_id":"O1","proposal_version":"P1","status":"deferred","severity":"nonblocking","kind":"tradeoff","reason":"later","approved_by":HUMANS}])
    assert evaluate_consensus(s)["can_publish"]
def test_weighted_score_is_strict():
    assert weighted_score({"value":5,"cost":1},{"value":.7,"cost":.3})==pytest.approx(3.8)
    with pytest.raises(ContractError):weighted_score({"value":5},{"value":.5})
def test_plan_rejects_cycle_and_uncovered():
    plan={"spec_version":"S1","contract_version":"C1","tasks":[
      {"task_id":"A","goal":"a","req_refs":["R1"],"outputs":["a"],"depends_on":["B"],"acceptance":["x"],"required_test_ids":["T-A"]},
      {"task_id":"B","goal":"b","req_refs":["R1"],"outputs":["b"],"depends_on":["A"],"acceptance":["x"],"required_test_ids":["T-B"]}]}
    r=validate_plan(plan,["R1","R2"]);assert not r["accepted"];assert any("cycle" in x for x in r["reasons"])
def test_tampered_evidence_rejected(tmp_path):
    key=b"k"*32; log=tmp_path/"log.txt"; log.write_text("ok",encoding="utf8")
    report={"schema_version":1,"context":{"task_id":"T1","attempt_id":1},"started_at":"2026-11-13T00:00:00+00:00","finished_at":"2026-11-13T00:00:01+00:00","cases":[{"test_id":"C1","status":"PASS","exit_code":0,"log_path":"log.txt","log_sha256":hashlib.sha256(b"ok").hexdigest()}],"artifacts":[]}
    env=sign_report(report,key);report["context"]["attempt_id"]=2
    r=check_evidence(env,key=key,expected_context={"task_id":"T1","attempt_id":1},required_test_ids=["C1"],artifact_root=tmp_path,required_artifacts=[],now=datetime(2026,11,13,0,0,2,tzinfo=timezone.utc))
    assert not r["accepted"]
def test_runner_timeout_and_output_limit(tmp_path):
    r=run_case(["python","-c","import time;time.sleep(5)"],cwd=tmp_path,log_file=tmp_path/"timeout.log",timeout_seconds=1)
    assert r["status"]=="TIMEOUT"
    r=run_case(["python","-c","print('x'*10000)"],cwd=tmp_path,log_file=tmp_path/"limit.log",timeout_seconds=5,max_output_bytes=100)
    assert r["status"]=="OUTPUT_LIMIT"
