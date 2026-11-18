"""B01 参考实现命令行入口。

命令：
- `generate`：从内置参考设计生成 R1/S1/C1/design_entries/plan/traceability（固定参考包）
- `plan`：从任意规格（JSON/Markdown）动态生成 plan.json 与 traceability.json
- `validate`：校验 plan.json（通用结构/覆盖/DAG/版本）
- `baseline`：T01 基线登记
- `all`：生成 + 校验 + 基线
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .baseline import BaselineError, register_baseline
from .design import C1_CONTRACT, DESIGN_ENTRIES, R1, S1
from .dynamic_planner import build_plan_from_spec, build_traceability as build_dynamic_traceability
from .plan_validator import validate_plan, validate_traceability
from .spec_input import SpecError, load_spec
from .task_planner import build_plan, build_traceability

DOCS = Path(__file__).resolve().parents[2] / "docs"
FIXTURES = DOCS / "fixtures"
DEFAULT_SPEC = FIXTURES / "campus-lost-found.spec.json"


def _write_json(name: str, payload: Any, *, directory: Path | None = None) -> Path:
    target_dir = directory or DOCS
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _read_json(name: str) -> Any:
    return json.loads((DOCS / name).read_text(encoding="utf-8"))


def cmd_generate(_: argparse.Namespace) -> int:
    plan = build_plan()
    written = [
        _write_json("r1.json", R1),
        _write_json("s1.json", S1),
        _write_json("c1.json", C1_CONTRACT),
        _write_json("design_entries.json", DESIGN_ENTRIES),
        _write_json("plan.json", plan),
        _write_json("traceability.json", build_traceability(plan)),
    ]
    for path in written:
        print(f"generated {path.relative_to(DOCS.parent)}")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    try:
        spec = load_spec(args.spec)
    except SpecError as error:
        print(f"规格载入失败：{error}", file=sys.stderr)
        return 2
    previous = None
    previous_path = Path(args.previous_plan) if args.previous_plan else DOCS / "plan.json"
    if previous_path.is_file():
        try:
            previous = json.loads(previous_path.read_text(encoding="utf-8")).get("plan_version")
        except json.JSONDecodeError:
            previous = None
    plan = build_plan_from_spec(spec, plan_version=args.plan_version, previous_plan_version=previous)
    traceability = build_dynamic_traceability(plan, spec)
    out_dir = Path(args.out_dir) if args.out_dir else DOCS
    plan_path = _write_json(args.plan_name, plan, directory=out_dir)
    trace_path = _write_json(args.traceability_name, traceability, directory=out_dir)
    report = validate_plan(plan)
    trace_report = validate_traceability(plan, traceability)
    print(f"spec {spec.source_format} project={spec.project_id} requirements={len(spec.requirements)} "
          f"design_entries={len(spec.design_entries)}")
    print(f"plan {plan['plan_version']} tasks={len(plan['tasks'])} -> {plan_path}")
    print(f"traceability -> {trace_path}")
    print(f"validate ok={report.ok} traceability ok={trace_report.ok}")
    for reason in report.reasons + trace_report.reasons:
        print(f"  - {reason}", file=sys.stderr)
    return 0 if report.ok and trace_report.ok else 1


def cmd_validate(args: argparse.Namespace) -> int:
    plan_arg = getattr(args, "plan", None)
    plan = _read_json("plan.json") if not plan_arg else json.loads(Path(plan_arg).read_text(encoding="utf-8"))
    report = validate_plan(plan)
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    trace_arg = getattr(args, "traceability", None)
    if trace_arg:
        trace_path = Path(args.traceability)
        trace = json.loads(trace_path.read_text(encoding="utf-8"))
        trace_report = validate_traceability(plan, trace)
        print(json.dumps(trace_report.as_dict(), ensure_ascii=False, indent=2))
        return 0 if report.ok and trace_report.ok else 1
    return 0 if report.ok else 1


def cmd_baseline(args: argparse.Namespace) -> int:
    try:
        manifest = register_baseline(
            plan=_read_json("plan.json"),
            contract=_read_json("c1.json"),
            r1=_read_json("r1.json"),
            s1=_read_json("s1.json"),
            allow_reference=args.allow_reference,
        )
    except BaselineError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    path = _write_json("baseline_manifest.json", manifest)
    print(f"frozen {path.relative_to(DOCS.parent)} status={manifest['status']}")
    return 0


def cmd_all(args: argparse.Namespace) -> int:
    rc = cmd_generate(args)
    if rc:
        return rc
    rc = cmd_validate(args)
    if rc:
        return rc
    return cmd_baseline(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="b01", description="B01 SpecPlannerAgent 参考实现")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("generate", help="生成 R1/S1/C1/设计条目/计划/追溯 JSON").set_defaults(func=cmd_generate)

    plan_cmd = sub.add_parser("plan", help="从任意规格动态生成计划与追溯表")
    plan_cmd.add_argument("--spec", default=str(DEFAULT_SPEC), help="规格文件（JSON 或 Markdown）")
    plan_cmd.add_argument("--out-dir", default=None, help="输出目录，默认 b01-spec-planner/docs")
    plan_cmd.add_argument("--plan-name", default="plan.json")
    plan_cmd.add_argument("--traceability-name", default="traceability.json")
    plan_cmd.add_argument("--plan-version", default=None, help="显式指定计划版本")
    plan_cmd.add_argument("--previous-plan", default=None, help="上一版计划文件，用于版本递增")
    plan_cmd.set_defaults(func=cmd_plan)

    validate_cmd = sub.add_parser("validate", help="校验任务计划")
    validate_cmd.add_argument("--plan", default=None, help="计划文件，默认 docs/plan.json")
    validate_cmd.add_argument("--traceability", default=None, help="同时校验追溯表")
    validate_cmd.set_defaults(func=cmd_validate)

    baseline = sub.add_parser("baseline", help="执行 T01 基线登记")
    baseline.add_argument(
        "--allow-reference",
        action="store_true",
        help="允许在无真实六人确认时生成 REFERENCE_FROZEN 参考基线",
    )
    baseline.set_defaults(func=cmd_baseline)

    all_cmd = sub.add_parser("all", help="生成、校验并冻结参考基线")
    all_cmd.add_argument("--allow-reference", action="store_true")
    all_cmd.set_defaults(func=cmd_all)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
