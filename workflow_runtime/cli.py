"""`python -m workflow_runtime` 的命令行入口。

子命令：
- `pipeline`   ：跑完整闭环演示（默认 `success` 场景）
- `scheduler`  ：只跑轻量 DAG 调度器（旧行为，读取 plan.json）

保持向后兼容：`python -m workflow_runtime <plan.json> --run` 仍等价于 `scheduler`。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .scheduler import TaskScheduler


def _scheduler(args: argparse.Namespace) -> int:
    scheduler = TaskScheduler.from_file(args.plan)
    if args.run:
        print(json.dumps(scheduler.run(), ensure_ascii=False, indent=2))
    else:
        print(
            json.dumps(
                {
                    "plan_version": scheduler.plan.get("plan_version"),
                    "ready": scheduler.ready(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0


def _pipeline(args: argparse.Namespace) -> int:
    from .pipeline import main as pipeline_main

    forwarded: list[str] = ["pipeline", "--scenario", args.scenario, "--spec", args.spec]
    if args.state_dir:
        forwarded += ["--state-dir", args.state_dir]
    if args.run_id:
        forwarded += ["--run-id", args.run_id]
    if args.json:
        forwarded.append("--json")
    return pipeline_main(forwarded)


def main(argv: list[str] | None = None) -> int:
    arguments = list(argv) if argv is not None else sys.argv[1:]
    if arguments and arguments[0].endswith(".json"):
        # 兼容旧用法：python -m workflow_runtime <plan.json> [--run]
        parser = argparse.ArgumentParser(prog="workflow_runtime", description="轻量 DAG 调度演示")
        parser.add_argument("plan", help="B01 生成的 plan.json")
        parser.add_argument("--run", action="store_true", help="执行演示调度")
        return _scheduler(parser.parse_args(arguments))

    parser = argparse.ArgumentParser(prog="workflow_runtime", description="B01-B06 第二阶段闭环入口")
    sub = parser.add_subparsers(dest="command")
    pipe = sub.add_parser("pipeline", help="跑完整闭环演示")
    pipe.add_argument("--scenario", default="success", choices=["success", "retry", "blocked", "evidence"])
    pipe.add_argument("--spec", default="b01-spec-planner/docs/fixtures/campus-lost-found.spec.json")
    pipe.add_argument("--state-dir", default=None)
    pipe.add_argument("--run-id", default=None)
    pipe.add_argument("--json", action="store_true")
    pipe.set_defaults(func=_pipeline)

    sched = sub.add_parser("scheduler", help="只跑轻量 DAG 调度器")
    sched.add_argument("plan", help="B01 生成的 plan.json")
    sched.add_argument("--run", action="store_true")
    sched.set_defaults(func=_scheduler)

    args = parser.parse_args(arguments or ["pipeline"])
    if not hasattr(args, "func"):
        args = parser.parse_args(["pipeline"])
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
