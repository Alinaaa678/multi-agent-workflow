"""B01-D12/D13/D14：通用规格输入、动态计划与输入快照。"""

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from b01.dynamic_planner import (
    build_plan_from_spec,
    build_traceability,
    build_tasks_from_spec,
    next_plan_version,
    topological_sort,
)
from b01.plan_validator import validate_plan, validate_traceability
from b01.spec_input import (
    SpecError,
    load_spec,
    load_spec_from_text,
    normalize_spec,
    parse_markdown_spec,
    verify_requirement_coverage,
)

PACKAGE = Path(__file__).resolve().parents[1]
FIXTURES = PACKAGE / "docs" / "fixtures"
CAMPUS_JSON = FIXTURES / "campus-lost-found.spec.json"
LAB_MARKDOWN = FIXTURES / "lab-equipment-loan.spec.md"


def minimal_spec(**overrides):
    spec = {
        "spec_version": "S9-v1.0",
        "contract_version": "C9-v1.0",
        "project_id": "tiny",
        "title": "极小系统",
        "requirements": [{"req_id": "R-1", "title": "只有一条需求", "priority": "must"}],
        "design_entries": [
            {"design_id": "DS-1", "module": "核心", "req_refs": ["R-1"], "rule": "必须可运行"}
        ],
        "contract": {
            "contract_version": "C9-v1.0",
            "operations": [{"name": "ping", "method": "GET", "path": "/api/v1/ping"}],
            "errors": {"INTERNAL_ERROR": 500},
        },
    }
    spec.update(overrides)
    return spec


class SpecInputTests(unittest.TestCase):
    def test_json_fixture_is_normalized(self):
        spec = load_spec(CAMPUS_JSON)
        self.assertEqual(spec.source_format, "json")
        self.assertEqual(spec.project_id, "campus-lost-found")
        self.assertEqual(len(spec.requirements), 5)
        self.assertEqual(len(spec.design_entries), 6)
        self.assertEqual(len(spec.contract["operations"]), 11)
        self.assertEqual(len(spec.source_sha256), 64)

    def test_markdown_fixture_is_normalized(self):
        spec = load_spec(LAB_MARKDOWN)
        self.assertEqual(spec.source_format, "markdown")
        self.assertEqual(spec.project_id, "lab-equipment-loan")
        self.assertEqual([item["req_id"] for item in spec.requirements], ["EQ-01", "EQ-02", "EQ-03"])
        self.assertEqual(
            [item["name"] for item in spec.contract["operations"]],
            ["register_equipment", "create_loan", "return_loan"],
        )
        self.assertEqual(spec.contract["errors"]["DUPLICATE_EQUIPMENT"], 409)
        # 全角冒号与半角冒号都应被接受。
        self.assertEqual(spec.design_entries[0]["module"], "设备管理")
        self.assertEqual(spec.design_entries[0]["test_refs"], ["TC-EQ-REG-01"])

    def test_format_is_detected_from_content(self):
        text = "# 标题\n- spec_version: S3-v1.0\n- contract_version: C3-v1.0\n\n## 需求\n### R-1 甲\n- 描述：甲\n"
        spec = load_spec_from_text(text)
        self.assertEqual(spec.source_format, "markdown")
        self.assertEqual(spec.spec_version, "S3-v1.0")

    def test_missing_required_top_level_field_is_rejected(self):
        raw = minimal_spec()
        del raw["contract_version"]
        with self.assertRaises(SpecError):
            normalize_spec(raw)

    def test_unknown_design_requirement_reference_is_rejected(self):
        raw = minimal_spec()
        raw["design_entries"][0]["req_refs"] = ["R-404"]
        with self.assertRaises(SpecError):
            normalize_spec(raw)

    def test_duplicate_requirement_ids_are_rejected(self):
        raw = minimal_spec()
        raw["requirements"].append({"req_id": "R-1", "title": "重复"})
        with self.assertRaises(SpecError):
            normalize_spec(raw)

    def test_contract_version_drift_is_rejected(self):
        raw = minimal_spec()
        raw["contract"]["contract_version"] = "C-OTHER"
        with self.assertRaises(SpecError):
            normalize_spec(raw)

    def test_invalid_priority_is_rejected(self):
        raw = minimal_spec()
        raw["requirements"][0]["priority"] = "critical"
        with self.assertRaises(SpecError):
            normalize_spec(raw)

    def test_markdown_contract_operation_without_path_is_rejected(self):
        text = (
            "# 标题\n- spec_version: S3-v1.0\n- contract_version: C3-v1.0\n\n"
            "## 需求\n### R-1 甲\n- 描述：甲\n\n"
            "## 契约\n### broken\n- method: POST\n"
        )
        with self.assertRaises(SpecError):
            parse_markdown_spec(text)

    def test_requirement_coverage_helper(self):
        self.assertEqual(verify_requirement_coverage(["A", "B", "C"], ["A", "C"]), ["B"])
        self.assertEqual(verify_requirement_coverage(["A"], ["A"]), [])


class DynamicPlannerTests(unittest.TestCase):
    def setUp(self):
        self.spec = load_spec(CAMPUS_JSON)
        self.plan = build_plan_from_spec(self.spec)

    def test_generated_plan_passes_general_validation(self):
        report = validate_plan(self.plan)
        self.assertTrue(report.ok, report.reasons)
        self.assertEqual(report.topological_order, self.plan["topological_order"])

    def test_plan_is_not_fixed_to_t01_t11(self):
        """任务数量由规格推导：更小的规格必须产出更少的任务。"""
        small = load_spec(LAB_MARKDOWN)
        small_plan = build_plan_from_spec(small)
        self.assertNotEqual(len(self.plan["tasks"]), len(small_plan["tasks"]))
        self.assertEqual(len(set(small_plan["topological_order"])), len(small_plan["tasks"]))

    def test_every_requirement_is_covered(self):
        covered = {ref for task in self.plan["tasks"] for ref in task["req_refs"]}
        self.assertEqual(sorted(covered), sorted(self.spec.requirement_ids()))

    def test_input_snapshot_records_source_hash(self):
        snapshot = self.plan["input_snapshot"]
        self.assertEqual(snapshot["spec_sha256"], self.spec.source_sha256)
        self.assertEqual(snapshot["spec_version"], "S1-v1.0")
        self.assertEqual(snapshot["requirement_count"], 5)
        self.assertEqual(snapshot["design_entry_count"], 6)

    def test_plan_version_format_and_increment(self):
        self.assertEqual(self.plan["plan_version"], "PLAN-campus-lost-found-v1.0")
        self.assertEqual(next_plan_version("PLAN-campus-lost-found-v1.0", "campus-lost-found"), "PLAN-campus-lost-found-v1.1")
        self.assertEqual(next_plan_version("PLAN-x-v1.9", "x"), "PLAN-x-v2.0")
        self.assertEqual(next_plan_version(None, "x"), "PLAN-x-v1.0")

    def test_changed_spec_yields_new_version_and_hash(self):
        changed = load_spec_from_text(
            json.dumps({**json.loads(CAMPUS_JSON.read_text(encoding="utf-8")), "title": "改名后的系统"}),
            spec_format="json",
        )
        new_plan = build_plan_from_spec(changed, previous_plan_version=self.plan["plan_version"])
        self.assertEqual(new_plan["plan_version"], "PLAN-campus-lost-found-v1.1")
        self.assertNotEqual(new_plan["input_snapshot"]["spec_sha256"], self.plan["input_snapshot"]["spec_sha256"])

    def test_traceability_matches_plan(self):
        trace = build_traceability(self.plan, self.spec)
        report = validate_traceability(self.plan, trace)
        self.assertTrue(report.ok, report.reasons)
        self.assertEqual(trace["uncovered_requirements"], [])
        self.assertTrue(all(item["task_refs"] for item in trace["items"]))

    def test_cycle_in_derived_tasks_is_detected(self):
        tasks = build_tasks_from_spec(self.spec)
        broken = copy.deepcopy(tasks)
        broken[0]["depends_on"] = [broken[-1]["task_id"]]
        with self.assertRaises(ValueError):
            topological_sort(broken)

    def test_minimal_spec_still_produces_valid_plan(self):
        plan = build_plan_from_spec(normalize_spec(minimal_spec()))
        report = validate_plan(plan)
        self.assertTrue(report.ok, report.reasons)
        self.assertGreaterEqual(len(plan["tasks"]), 4)  # 1 实现 + 1 契约 + 1 验收 + 集成 + 交付

    def test_spec_without_design_entries_uses_contract_and_requirements(self):
        raw = minimal_spec()
        raw["design_entries"] = []
        plan = build_plan_from_spec(normalize_spec(raw))
        report = validate_plan(plan)
        self.assertTrue(report.ok, report.reasons)
        self.assertTrue(any(task["capability"] == "QA-01" for task in plan["tasks"]))

    def test_spec_without_contract_operations_warns_but_plans(self):
        raw = minimal_spec()
        raw["contract"] = None
        spec = normalize_spec(raw)
        self.assertTrue(spec.warnings)
        plan = build_plan_from_spec(spec)
        self.assertTrue(validate_plan(plan).ok)


if __name__ == "__main__":
    unittest.main()
