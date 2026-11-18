"""B01 通用规格输入：任意系统的设计规格 JSON/Markdown 归一化。

职责边界：
- 本模块只做“读入 + 结构化 + 校验”，不生成业务规则，不写死校园失物招领。
- 校园失物招领只是 fixture（见 docs/fixtures/），不是唯一输入格式。
- 归一化结果供 `dynamic_planner` 推导任务，供 `plan_validator` 与 B06 只读校验。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

SPEC_SCHEMA_VERSION = "b01.spec.v1"

REQUIRED_TOP_LEVEL = ("spec_version", "contract_version", "requirements")

REQUIREMENT_BUCKETS = ("需求", "requirements", "requirement")
DESIGN_BUCKETS = ("设计条目", "design", "design entries", "design_entries")
CONTRACT_BUCKETS = ("契约", "contract", "接口")


class SpecError(ValueError):
    """规格输入不合法：结构缺失、类型错误或引用断裂。"""


@dataclass
class SpecDocument:
    """归一化后的规格文档。"""

    spec_version: str
    contract_version: str
    project_id: str
    title: str
    requirements: list[dict[str, Any]]
    design_entries: list[dict[str, Any]]
    contract: dict[str, Any]
    source_format: str
    source_sha256: str
    schema_version: str = SPEC_SCHEMA_VERSION
    warnings: list[str] = field(default_factory=list)

    def requirement_ids(self) -> list[str]:
        return [item["req_id"] for item in self.requirements]

    def design_ids(self) -> list[str]:
        return [item["design_id"] for item in self.design_entries]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "spec_version": self.spec_version,
            "contract_version": self.contract_version,
            "project_id": self.project_id,
            "title": self.title,
            "requirements": self.requirements,
            "design_entries": self.design_entries,
            "contract": self.contract,
            "source_format": self.source_format,
            "source_sha256": self.source_sha256,
            "warnings": list(self.warnings),
        }


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SpecError(f"{name} 必须是非空字符串")
    return value.strip()


def _string_list(value: Any, name: str, *, allow_empty: bool = True) -> list[str]:
    if value is None:
        value = []
    if not isinstance(value, list):
        raise SpecError(f"{name} 必须是字符串数组")
    items: list[str] = []
    seen: set[str] = set()
    for raw in value:
        item = _text(raw, f"{name}[]")
        if item in seen:
            raise SpecError(f"{name} 存在重复项: {item}")
        seen.add(item)
        items.append(item)
    if not items and not allow_empty:
        raise SpecError(f"{name} 不能为空")
    return items


def _normalize_requirement(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise SpecError(f"requirements[{index}] 必须是对象")
    req_id = _text(raw.get("req_id"), f"requirements[{index}].req_id")
    title = _text(raw.get("title"), f"requirements[{index}].title")
    description = raw.get("description") or title
    if not isinstance(description, str):
        raise SpecError(f"requirements[{index}].description 必须是字符串")
    priority = raw.get("priority") or "must"
    if priority not in ("must", "should", "may"):
        raise SpecError(f"requirements[{index}].priority 必须是 must|should|may")
    return {
        "req_id": req_id,
        "title": title,
        "description": description.strip() or title,
        "priority": priority,
        "acceptance": _string_list(raw.get("acceptance"), f"requirements[{index}].acceptance"),
        "constraints": _string_list(raw.get("constraints"), f"requirements[{index}].constraints"),
    }


def _normalize_design_entry(raw: Any, index: int, requirement_ids: set[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise SpecError(f"design_entries[{index}] 必须是对象")
    design_id = _text(raw.get("design_id"), f"design_entries[{index}].design_id")
    module = _text(raw.get("module"), f"design_entries[{index}].module")
    req_refs = _string_list(raw.get("req_refs"), f"design_entries[{index}].req_refs", allow_empty=False)
    unknown = sorted(set(req_refs) - requirement_ids)
    if unknown:
        raise SpecError(f"design_entries[{index}] 引用了未知需求: {unknown}")
    return {
        "design_id": design_id,
        "module": module,
        "req_refs": req_refs,
        "contract_refs": _string_list(raw.get("contract_refs"), f"design_entries[{index}].contract_refs"),
        "test_refs": _string_list(raw.get("test_refs"), f"design_entries[{index}].test_refs"),
        "rule": str(raw.get("rule") or module),
    }


def _normalize_contract(raw: Any, contract_version: str, *, warnings: list[str]) -> dict[str, Any]:
    if raw is None:
        warnings.append("规格未提供 contract，按空契约处理")
        return {"contract_version": contract_version, "operations": [], "errors": {}, "field_bounds": {}}
    if not isinstance(raw, dict):
        raise SpecError("contract 必须是对象")
    declared = raw.get("contract_version")
    if declared and declared != contract_version:
        raise SpecError(f"contract.contract_version={declared} 与顶层 {contract_version} 不一致")
    operations_raw = raw.get("operations") or []
    if not isinstance(operations_raw, list):
        raise SpecError("contract.operations 必须是数组")
    operations: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for index, op in enumerate(operations_raw):
        if not isinstance(op, dict):
            raise SpecError(f"contract.operations[{index}] 必须是对象")
        name = _text(op.get("name"), f"contract.operations[{index}].name")
        if name in seen_names:
            raise SpecError(f"contract.operations 操作名重复: {name}")
        seen_names.add(name)
        operations.append(
            {
                "name": name,
                "method": _text(op.get("method") or "POST", f"contract.operations[{index}].method").upper(),
                "path": _text(op.get("path"), f"contract.operations[{index}].path"),
                "authenticated": bool(op.get("authenticated", True)),
                "required": _string_list(op.get("required"), f"contract.operations[{index}].required"),
                "query": _string_list(op.get("query"), f"contract.operations[{index}].query"),
                "status": int(op.get("status") or 200),
                "data": str(op.get("data") or "Null"),
            }
        )
    errors = raw.get("errors") or {}
    if not isinstance(errors, dict):
        raise SpecError("contract.errors 必须是对象")
    normalized_errors: dict[str, int] = {}
    for code, status in errors.items():
        _text(code, "contract.errors 键")
        if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
            raise SpecError(f"contract.errors[{code}] 必须是合法 HTTP 状态码")
        normalized_errors[code] = status
    field_bounds = raw.get("field_bounds") or {}
    if not isinstance(field_bounds, dict):
        raise SpecError("contract.field_bounds 必须是对象")
    return {
        "contract_version": contract_version,
        "prefix": str(raw.get("prefix") or ""),
        "operations": operations,
        "errors": normalized_errors,
        "field_bounds": field_bounds,
        "response_envelope": raw.get("response_envelope") or {},
    }


def normalize_spec(
    raw: dict[str, Any],
    *,
    source_format: str = "json",
    source_bytes: bytes | None = None,
) -> SpecDocument:
    """把已解析的规格对象归一化并校验。"""
    if not isinstance(raw, dict):
        raise SpecError("规格必须是 JSON 对象")
    warnings: list[str] = []
    for name in REQUIRED_TOP_LEVEL:
        if name not in raw:
            raise SpecError(f"规格缺少必填字段 {name}")
    spec_version = _text(raw.get("spec_version"), "spec_version")
    contract_version = _text(raw.get("contract_version"), "contract_version")
    project_id = _text(raw.get("project_id") or raw.get("title") or "project", "project_id")
    title = str(raw.get("title") or project_id)

    requirements_raw = raw.get("requirements")
    if not isinstance(requirements_raw, list) or not requirements_raw:
        raise SpecError("requirements 必须是非空数组")
    requirements: list[dict[str, Any]] = []
    seen_req: set[str] = set()
    for index, item in enumerate(requirements_raw):
        requirement = _normalize_requirement(item, index)
        if requirement["req_id"] in seen_req:
            raise SpecError(f"需求编号重复: {requirement['req_id']}")
        seen_req.add(requirement["req_id"])
        requirements.append(requirement)

    entries_raw = raw.get("design_entries") or []
    if not isinstance(entries_raw, list):
        raise SpecError("design_entries 必须是数组")
    design_entries: list[dict[str, Any]] = []
    seen_design: set[str] = set()
    for index, item in enumerate(entries_raw):
        entry = _normalize_design_entry(item, index, seen_req)
        if entry["design_id"] in seen_design:
            raise SpecError(f"设计条目编号重复: {entry['design_id']}")
        seen_design.add(entry["design_id"])
        design_entries.append(entry)

    contract = _normalize_contract(raw.get("contract"), contract_version, warnings=warnings)

    if source_bytes is None:
        source_bytes = json.dumps(raw, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return SpecDocument(
        spec_version=spec_version,
        contract_version=contract_version,
        project_id=project_id,
        title=title,
        requirements=requirements,
        design_entries=design_entries,
        contract=contract,
        source_format=source_format,
        source_sha256=sha256_bytes(source_bytes),
        warnings=warnings,
    )


# --------------------------------------------------------------------------
# Markdown 规格输入
# --------------------------------------------------------------------------

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_KV = re.compile(r"^([^:：]+)[:：]\s*(.*)$")


def _split_sections(text: str) -> list[tuple[str, int, list[str]]]:
    """把 Markdown 按标题切成 (标题, 级别, 该节正文行)。"""
    sections: list[tuple[str, int, list[str]]] = []
    current: tuple[str, int, list[str]] | None = None
    for line in text.splitlines():
        match = _HEADING.match(line)
        if match:
            if current is not None:
                sections.append(current)
            current = [match.group(2).strip(), len(match.group(1)), []]
        elif current is not None:
            current[2].append(line)
    if current is not None:
        sections.append(current)
    return [(item[0], item[1], item[2]) for item in sections]


def parse_markdown_spec(text: str) -> dict[str, Any]:
    """把 Markdown 规格解析成规格对象（供 normalize_spec 使用）。

    约定（最小可用子集，不引入额外依赖）：

    ```markdown
    # 项目标题
    - spec_version: S1-v1.0
    - contract_version: C1-v1.0
    - project_id: demo

    ## 需求
    ### R-001 注册登录
    - 描述：用户可以注册与登录
    - 优先级：must
    - 验收：重复账号被拒绝

    ## 设计条目
    ### DS-AUTH-01 身份模块
    - 需求：R-001
    - 契约：register
    - 测试：TC-AUTH-01
    - 规则：凭据不得明文存储

    ## 契约
    ### register
    - method: POST
    - path: /api/v1/auth/register
    - authenticated: false
    - required: account, password
    - error: DUPLICATE_ACCOUNT=409
    ```
    """
    sections = _split_sections(text)
    if not sections:
        raise SpecError("Markdown 规格为空或不包含任何标题")

    title = sections[0][0]
    meta: dict[str, str] = {}
    for line in sections[0][2]:
        bullet = _BULLET.match(line)
        if not bullet:
            continue
        match = _KV.match(bullet.group(1).strip())
        if match:
            meta[match.group(1).strip().lower()] = match.group(2).strip()

    requirements: list[dict[str, Any]] = []
    design_entries: list[dict[str, Any]] = []
    operations: list[dict[str, Any]] = []
    errors: dict[str, int] = {}

    bucket: str | None = None
    entry: dict[str, Any] | None = None
    for heading, level, body_lines in sections[1:]:
        if level == 2:
            bucket = heading.strip().lower()
            entry = None
            continue
        if bucket is None:
            continue
        if level == 3:
            entry = None
            parts = heading.split(None, 1)
            if bucket in REQUIREMENT_BUCKETS:
                entry = {
                    "req_id": parts[0],
                    "title": parts[1] if len(parts) > 1 else parts[0],
                    "description": "",
                    "priority": "must",
                    "acceptance": [],
                    "constraints": [],
                }
                requirements.append(entry)
            elif bucket in DESIGN_BUCKETS:
                entry = {
                    "design_id": parts[0],
                    "module": parts[1] if len(parts) > 1 else parts[0],
                    "req_refs": [],
                    "contract_refs": [],
                    "test_refs": [],
                    "rule": "",
                }
                design_entries.append(entry)
            elif bucket in CONTRACT_BUCKETS:
                entry = {
                    "name": heading.strip(),
                    "method": "POST",
                    "path": "",
                    "authenticated": True,
                    "required": [],
                    "status": 200,
                    "data": "Null",
                }
                operations.append(entry)
            # 不 continue：本节的 `- key: value` 正文行在同一 section 内，需要继续解析。
        if entry is None:
            continue
        for line in body_lines:
            bullet = _BULLET.match(line)
            if not bullet:
                continue
            _apply_markdown_field(entry, bucket, bullet.group(1).strip(), errors)

    for requirement in requirements:
        if not requirement["description"]:
            requirement["description"] = requirement["title"]
    for operation in operations:
        if not operation["path"]:
            raise SpecError(f"契约操作 {operation['name']} 缺少 path")

    return {
        "spec_version": meta.get("spec_version", ""),
        "contract_version": meta.get("contract_version", ""),
        "project_id": meta.get("project_id", title),
        "title": title,
        "requirements": requirements,
        "design_entries": design_entries,
        "contract": {
            "contract_version": meta.get("contract_version", ""),
            "operations": operations,
            "errors": errors,
            "field_bounds": {},
        },
    }


def _split_values(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _apply_markdown_field(entry: dict[str, Any], bucket: str, body: str, errors: dict[str, int]) -> None:
    """把一条 `- key: value` 写入对应的条目。"""
    match = _KV.match(body)
    if not match:
        return
    key_lower = match.group(1).strip().lower()
    value = match.group(2).strip()
    if bucket in REQUIREMENT_BUCKETS:
        if key_lower in ("描述", "description"):
            entry["description"] = value
        elif key_lower in ("标题", "title"):
            entry["title"] = value
        elif key_lower in ("优先级", "priority"):
            entry["priority"] = value
        elif key_lower in ("验收", "acceptance"):
            entry["acceptance"].append(value)
        elif key_lower in ("约束", "constraint", "constraints"):
            entry["constraints"].append(value)
    elif bucket in DESIGN_BUCKETS:
        if key_lower in ("需求", "req", "req_refs", "requirement"):
            entry["req_refs"].extend(_split_values(value))
        elif key_lower in ("契约", "contract", "contract_refs"):
            entry["contract_refs"].extend(_split_values(value))
        elif key_lower in ("测试", "test", "test_refs"):
            entry["test_refs"].extend(_split_values(value))
        elif key_lower in ("规则", "rule"):
            entry["rule"] = value
        elif key_lower in ("模块", "module"):
            entry["module"] = value
    elif bucket in CONTRACT_BUCKETS:
        if key_lower == "method":
            entry["method"] = value.upper()
        elif key_lower == "path":
            entry["path"] = value
        elif key_lower in ("authenticated", "认证"):
            entry["authenticated"] = value.lower() in ("true", "yes", "1", "需要")
        elif key_lower in ("required", "必填"):
            entry["required"].extend(_split_values(value))
        elif key_lower == "status":
            entry["status"] = int(value)
        elif key_lower == "data":
            entry["data"] = value
        elif key_lower in ("error", "错误"):
            code, _, status = value.partition("=")
            if code.strip() and status.strip():
                errors[code.strip()] = int(status.strip())


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def load_spec_from_text(
    text: str,
    *,
    spec_format: str | None = None,
    default_versions: dict[str, str] | None = None,
) -> SpecDocument:
    """从文本载入规格；`spec_format` 为 `json` 或 `markdown`。"""
    raw_bytes = text.encode("utf-8")
    chosen = (spec_format or "").lower()
    if chosen not in ("json", "markdown"):
        chosen = "json" if text.lstrip().startswith("{") else "markdown"
    if chosen == "json":
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as error:
            raise SpecError(f"规格 JSON 解析失败: {error}") from error
    else:
        parsed = parse_markdown_spec(text)
        defaults = default_versions or {}
        for key in ("spec_version", "contract_version"):
            if not parsed.get(key):
                fallback = defaults.get(key)
                if not fallback:
                    raise SpecError(f"Markdown 规格缺少 {key}（可在文首用 `- {key}: ...` 声明）")
                parsed[key] = fallback
        if not parsed.get("contract", {}).get("contract_version"):
            parsed["contract"]["contract_version"] = parsed["contract_version"]
    return normalize_spec(parsed, source_format=chosen, source_bytes=raw_bytes)


def load_spec(path: str | Path, *, spec_format: str | None = None) -> SpecDocument:
    """从文件载入规格；扩展名 .md/.markdown 视为 Markdown，其余按内容判断。"""
    file_path = Path(path)
    if not file_path.is_file():
        raise SpecError(f"规格文件不存在: {file_path}")
    text = file_path.read_text(encoding="utf-8")
    chosen = spec_format
    if chosen is None and file_path.suffix.lower() in (".md", ".markdown"):
        chosen = "markdown"
    return load_spec_from_text(text, spec_format=chosen)


def verify_requirement_coverage(requirements: Iterable[str], covered: Iterable[str]) -> list[str]:
    """返回未被覆盖的需求编号（升序）。"""
    return sorted(set(requirements) - set(covered))
