"""Machine-readable checker catalog and ruleset validation.

Deliberately explicit: it is both the backend validation contract and the
schema the administrator UI renders forms from."""
from __future__ import annotations

import re
from typing import Any

from .base_checker import list_registered_checker_types
from .rule_schema import RuleDef, RuleSet


def _entry(
    label: str,
    group: str,
    description: str,
    properties: dict[str, dict[str, Any]] | None = None,
    required: list[str] | None = None,
    *,
    ai: bool = False,
) -> dict[str, Any]:
    return {
        "label": label,
        "group": group,
        "description": description,
        "ai": ai,
        "schema": {
            "type": "object",
            "properties": properties or {},
            "required": required or [],
            "additionalProperties": True,
        },
    }


STRING_LIST = {"type": "array", "items": {"type": "string"}}
POSITIVE_INT = {"type": "integer", "minimum": 1}
FIELD_ITEMS = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "title": "字段名称"},
            "aliases": {**STRING_LIST, "title": "字段别名"},
            "required": {"type": "boolean", "default": True, "title": "必填"},
            "normalize": {
                "type": "string",
                "enum": ["text", "casefold", "phone", "date", "number", "list"],
            },
            "format": {
                "type": "string",
                "enum": ["email", "cn_mobile", "phone", "date", "cn_id", "number", "project_code"],
            },
            "pattern": {"type": "string", "format": "regex"},
        },
        "required": ["name"],
    },
}


CHECKER_CATALOG: dict[str, dict[str, Any]] = {
    "required_sections": _entry("必填章节", "结构", "检查必需章节及最低字数", {
        "sections": {"type": "array", "items": {"type": "object"}, "title": "章节要求"},
    }, ["sections"]),
    "section_order": _entry("章节顺序", "结构", "检查章节出现顺序", {
        "expected_order": {**STRING_LIST, "title": "预期顺序"},
    }, ["expected_order"]),
    "page_limit": _entry("页数限制", "篇幅", "检查总页数及分章节页数", {
        "max_pages": {**POSITIVE_INT, "title": "最大页数"},
        "section_limits": {"type": "object", "title": "章节限制"},
    }, ["max_pages"]),
    "word_limit": _entry("字数限制", "篇幅", "检查总字数及分章节字数", {
        "max_words": {**POSITIVE_INT, "title": "最大字数"},
        "section_limits": {"type": "object", "title": "章节限制"},
    }),
    "font_check": _entry("字体字号", "版式", "检查正文字体与字号", {
        "body_font": {"type": "string", "default": "宋体", "title": "正文字体"},
        "body_size": {"type": "number", "minimum": 1, "default": 12, "title": "正文字号"},
    }),
    "figure_table_numbering": _entry("图表编号", "结构", "检查图表编号连续性"),
    "budget_check": _entry("预算勾稽", "财务", "检查预算合计与科目比例", {
        "max_ratio": {"type": "object", "title": "科目最高占比"},
        "total_field": {"type": "string", "default": "合计|总计", "format": "regex", "title": "合计字段"},
    }),
    "date_check": _entry("研究期限", "一致性", "检查起止日期和预期月数", {
        "expected_months": {**POSITIVE_INT, "title": "预期月数"},
    }),
    "page_number_check": _entry("页码连续性", "版式", "检查页码缺失和断档", {
        "allow_unnumbered_front_pages": {"type": "integer", "minimum": 0, "default": 1},
    }),
    "attachment_checklist": _entry("附件清单", "完整性", "检查预期附件名称", {
        "expected": {**STRING_LIST, "title": "附件名称"},
    }, ["expected"]),
    "signature_page": _entry("签字盖章页", "完整性", "检查签字盖章关键词", {
        "keywords": {**STRING_LIST, "default": ["签字", "盖章"], "title": "关键词"},
    }),
    "cross_field_consistency": _entry("跨字段一致性", "一致性", "比较同名或成对字段", {
        "fields": {**FIELD_ITEMS, "title": "重复字段"},
        "comparisons": {"type": "array", "items": {"type": "object"}, "title": "字段对比较"},
    }),
    "required_fields": _entry("必填字段", "完整性", "检查字段缺失或空白", {
        "fields": {**FIELD_ITEMS, "title": "字段列表"},
    }, ["fields"]),
    "field_format": _entry("字段格式", "格式", "检查联系方式、日期和编号格式", {
        "fields": {**FIELD_ITEMS, "title": "字段列表"},
    }, ["fields"]),
    "heading_numbering": _entry("标题编号", "结构", "检查标题层级、编号连续性和样式", {
        "skip_pages": {"type": "array", "items": {"type": "integer"}},
        "require_numbering": {"type": "boolean", "default": False},
        "allow_mixed_styles": {"type": "boolean", "default": False},
        "allowed_styles": {"type": "object"},
        "max_issues": POSITIVE_INT,
    }),
    "layout_check": _entry("页面版式", "版式", "检查纸张、页边距、正文和标题版式", {
        "page": {"type": "object", "title": "页面"},
        "margins_mm": {"type": "object", "title": "页边距"},
        "body": {"type": "object", "title": "正文"},
        "headings": {"type": "object", "title": "标题"},
        "max_issues": POSITIVE_INT,
    }),
    "anonymity_check": _entry("匿名信息 AI 检查", "AI", "检查正文身份泄露", {
        "cover_pages": {"type": "array", "items": {"type": "integer"}},
        "max_chunk_chars": POSITIVE_INT,
    }, ai=True),
    "title_content_match": _entry("标题内容 AI 匹配", "AI", "检查章节标题与正文是否明显不符", {
        "max_section_chars": POSITIVE_INT,
        "max_chunk_chars": POSITIVE_INT,
    }, ai=True),
    "cross_consistency_semantic": _entry("语义一致性 AI 检查", "AI", "检查期限、经费和人员语义矛盾", {
        "max_chunk_chars": POSITIVE_INT,
    }, ai=True),
    "reference_format": _entry("参考文献格式", "格式", "确定性格式检查，可选 AI 复核", {
        "semantic_review": {"type": "boolean", "default": False},
        "max_chunk_chars": POSITIVE_INT,
    }),
}


def rule_requires_ai(rule: RuleDef) -> bool:
    """Return whether executing this concrete rule may transmit document text."""
    definition = CHECKER_CATALOG.get(rule.type, {})
    if definition.get("ai"):
        return True
    return bool(
        rule.type == "reference_format"
        and rule.params.get("semantic_review", False)
    )


def checker_catalog() -> list[dict[str, Any]]:
    registered = set(list_registered_checker_types())
    catalog = [
        {"type": type_name, **definition}
        for type_name, definition in CHECKER_CATALOG.items()
        if type_name in registered
    ]
    # Third-party/test checkers remain manageable instead of silently
    # disappearing from the catalog; their parameters use an open object.
    for type_name in sorted(registered - CHECKER_CATALOG.keys()):
        catalog.append({
            "type": type_name,
            **_entry(type_name, "扩展", "扩展检查器（开放参数）"),
        })
    return catalog


def validate_ruleset(data: dict[str, Any]) -> tuple[RuleSet | None, list[dict], list[dict]]:
    errors: list[dict] = []
    warnings: list[dict] = []
    try:
        ruleset = RuleSet.model_validate(data)
    except Exception as exc:
        return None, [{"path": "$", "message": str(exc)}], warnings

    seen: set[str] = set()
    for index, rule in enumerate(ruleset.rules):
        base_path = f"rules[{index}]"
        if rule.id in seen:
            errors.append({"path": f"{base_path}.id", "message": f"规则 ID 重复：{rule.id}"})
        seen.add(rule.id)
        definition = CHECKER_CATALOG.get(rule.type)
        if definition is None and rule.type in set(list_registered_checker_types()):
            definition = _entry(rule.type, "扩展", "扩展检查器（开放参数）")
        if definition is None:
            errors.append({"path": f"{base_path}.type", "message": f"未知检查器：{rule.type}"})
            continue
        schema = definition["schema"]
        for name in schema.get("required", []):
            if name not in rule.params:
                errors.append({"path": f"{base_path}.params.{name}", "message": "缺少必填参数"})
        _validate_value(rule.params, schema, f"{base_path}.params", errors)
        if rule_requires_ai(rule) and rule.enabled:
            warnings.append({"path": base_path, "message": "该规则会调用外部 AI，请确认授权、时延和成本"})
        if rule.type == "field_format":
            for field_index, field in enumerate(rule.params.get("fields", [])):
                if not field.get("format") and not field.get("pattern"):
                    errors.append({
                        "path": f"{base_path}.params.fields[{field_index}]",
                        "message": "字段格式规则必须配置 format 或 pattern",
                    })
        if rule.type == "layout_check":
            for section in ("body",):
                values = rule.params.get(section, {})
                minimum = values.get("min_size")
                maximum = values.get("max_size")
                minimum_valid = (
                    isinstance(minimum, (int, float))
                    and not isinstance(minimum, bool)
                )
                maximum_valid = (
                    isinstance(maximum, (int, float))
                    and not isinstance(maximum, bool)
                )
                if minimum is not None and not minimum_valid:
                    errors.append({
                        "path": f"{base_path}.params.{section}.min_size",
                        "message": "字号必须是数字",
                    })
                if maximum is not None and not maximum_valid:
                    errors.append({
                        "path": f"{base_path}.params.{section}.max_size",
                        "message": "字号必须是数字",
                    })
                if (
                    minimum_valid
                    and maximum_valid
                ):
                    if minimum > maximum:
                        errors.append({"path": f"{base_path}.params.{section}", "message": "最小字号不能大于最大字号"})
    if not ruleset.rules:
        warnings.append({"path": "rules", "message": "规则集当前没有规则"})
    return ruleset, errors, warnings


def _validate_value(value: Any, schema: dict[str, Any], path: str, errors: list[dict]) -> None:
    expected = schema.get("type")
    valid = True
    if expected == "object":
        valid = isinstance(value, dict)
    elif expected == "array":
        valid = isinstance(value, list)
    elif expected == "string":
        valid = isinstance(value, str)
    elif expected == "integer":
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif expected == "number":
        valid = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif expected == "boolean":
        valid = isinstance(value, bool)
    if not valid:
        errors.append({"path": path, "message": f"参数类型应为 {expected}"})
        return
    if "enum" in schema and value not in schema["enum"]:
        errors.append({"path": path, "message": f"参数必须是：{', '.join(map(str, schema['enum']))}"})
    if isinstance(value, (int, float)) and "minimum" in schema and value < schema["minimum"]:
        errors.append({"path": path, "message": f"参数不能小于 {schema['minimum']}"})
    if schema.get("format") == "regex" and isinstance(value, str):
        try:
            re.compile(value)
        except re.error as exc:
            errors.append({"path": path, "message": f"正则表达式无效：{exc}"})
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for key, child in value.items():
            if key in properties:
                _validate_value(child, properties[key], f"{path}.{key}", errors)
    if isinstance(value, list) and "items" in schema:
        for index, child in enumerate(value):
            _validate_value(child, schema["items"], f"{path}[{index}]", errors)
