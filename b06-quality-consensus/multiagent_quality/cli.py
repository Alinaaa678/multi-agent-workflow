import argparse,json,sys
from pathlib import Path
from .consensus import evaluate_consensus
from .plan import validate_plan
def main():
    parser=argparse.ArgumentParser();sub=parser.add_subparsers(dest='cmd',required=True)
    for name in ('consensus','plan'):
        p=sub.add_parser(name);p.add_argument('input');p.add_argument('--requirements',nargs='*',default=[])
    a=parser.parse_args();data=json.loads(Path(a.input).read_text(encoding='utf8'))
    result=evaluate_consensus(data) if a.cmd=='consensus' else validate_plan(data,a.requirements)
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result.get('accepted',result.get('can_publish',False)) else 2
if __name__=='__main__':sys.exit(main())
