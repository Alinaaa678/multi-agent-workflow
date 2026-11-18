import argparse, json
from .scheduler import TaskScheduler

def main():
    p=argparse.ArgumentParser(description="通用开发任务 DAG 编排器")
    p.add_argument("plan", help="B01 生成的 plan.json")
    p.add_argument("--run", action="store_true", help="执行演示调度")
    args=p.parse_args(); s=TaskScheduler.from_file(args.plan)
    if args.run: print(json.dumps(s.run(),ensure_ascii=False,indent=2))
    else: print(json.dumps({"plan_version":s.plan.get("plan_version"),"ready":s.ready()},ensure_ascii=False,indent=2))
if __name__ == "__main__": main()
