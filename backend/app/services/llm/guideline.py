from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import yaml

from app.schemas.ir import DocumentIR
from app.services.llm.client import LLMUnavailableError, call_json
from app.services.parser.pdf_parser import PDFParser

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    '从申报指南文本中抽取形式性、可机器核查的要求，只输出 JSON'
    '{"requirements":[{"kind":"page_limit|word_limit|required_section|font|'
    'budget_ratio|duration|attachment|required_field|field_format|'
    'heading_numbering|layout","detail":{...}}]}。'
    'required_field 的 detail 使用 {"names":[...]}；field_format 使用'
    '{"name":"字段名","format":"email|cn_mobile|phone|date|cn_id|number|project_code"}。'
    '忽略研究方向、选题'
    '范围等内容性要求。没有则输出 {"requirements":[]}。'
)

CHUNK_SIZE = 4000
SUPPORTED_FIELD_FORMATS = {
    "email",
    "cn_mobile",
    "phone",
    "date",
    "cn_id",
    "number",
    "project_code",
}


def extract_draft_from_pdf(pdf_path: str | Path) -> tuple[str, list[dict[str, Any]]]:
    ir = PDFParser().parse(pdf_path)
    text = _ir_text(ir)
    requirements: list[dict[str, Any]] = []
    for chunk in _chunks(text, CHUNK_SIZE):
        result = call_json(SYSTEM_PROMPT, chunk)
        for item in result.get("requirements", []):
            if isinstance(item, dict):
                requirements.append(item)
    return build_draft_yaml(requirements)


def build_draft_yaml(requirements: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    rules: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    seen: set[str] = set()

    for requirement in requirements:
        key = _dedupe_key(requirement)
        if key in seen:
            continue
        seen.add(key)

        rule = _map_requirement(requirement, len(rules) + 1)
        if rule is None:
            logger.warning("Dropped unmappable guideline requirement: %s", requirement)
            dropped.append(requirement)
            continue
        rules.append(rule)

    draft = {
        "ruleset": "generated_from_guideline_v1",
        "name": "Generated from Guideline",
        "description": "Draft ruleset generated from uploaded guideline.",
        "rules": rules,
    }
    return yaml.safe_dump(draft, allow_unicode=True, sort_keys=False), dropped


def _ir_text(ir: DocumentIR) -> str:
    blocks = sorted(ir.blocks, key=lambda b: (b.page or 0, b.bbox.y0, b.bbox.x0, b.id))
    return "\n".join(b.text for b in blocks if b.text.strip())


def _chunks(text: str, size: int) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size) if text[i:i + size].strip()]


def _dedupe_key(requirement: dict[str, Any]) -> str:
    return json.dumps(requirement, ensure_ascii=False, sort_keys=True, default=str)


def _map_requirement(requirement: dict[str, Any], index: int) -> dict[str, Any] | None:
    kind = requirement.get("kind")
    detail = requirement.get("detail") or {}
    if not isinstance(detail, dict):
        detail = {}

    rule_id = f"G{index:03d}"
    if kind == "page_limit":
        max_pages = _first_int(detail, "max_pages", "pages", "limit")
        if max_pages is None:
            return None
        return _rule(rule_id, "page_limit", "error", {"max_pages": max_pages})

    if kind == "word_limit":
        max_words = _first_int(detail, "max_words", "words", "limit")
        if max_words is None:
            return None
        return _rule(rule_id, "word_limit", "warning", {"max_words": max_words})

    if kind == "required_section":
        sections = _string_list(detail.get("sections") or detail.get("required") or detail.get("names") or detail.get("section"))
        if not sections:
            return None
        return _rule(
            rule_id,
            "required_sections",
            "error",
            {"sections": [{"title": section} for section in sections]},
        )

    if kind == "font":
        params: dict[str, Any] = {}
        font = detail.get("body_font") or detail.get("font") or detail.get("name")
        size = _first_number(detail, "body_size", "size", "font_size")
        if font:
            params["body_font"] = str(font)
        if size is not None:
            params["body_size"] = size
        if not params:
            return None
        return _rule(rule_id, "font_check", "warning", params)

    if kind == "budget_ratio":
        item = detail.get("item") or detail.get("name") or detail.get("category")
        ratio = _first_number(detail, "max_ratio", "ratio", "limit")
        if item is None or ratio is None:
            return None
        return _rule(rule_id, "budget_check", "error", {"max_ratio": {str(item): ratio}})

    if kind == "duration":
        months = _first_int(detail, "expected_months", "months", "duration_months")
        if months is None:
            return None
        return _rule(rule_id, "date_check", "warning", {"expected_months": months})

    if kind == "attachment":
        expected = _string_list(detail.get("expected") or detail.get("attachments") or detail.get("names") or detail.get("name"))
        if not expected:
            return None
        return _rule(rule_id, "attachment_checklist", "info", {"expected": expected})

    if kind == "required_field":
        names = _string_list(
            detail.get("names") or detail.get("fields") or detail.get("name")
        )
        if not names:
            return None
        return _rule(
            rule_id,
            "required_fields",
            "error",
            {"fields": [{"name": name} for name in names]},
        )

    if kind == "field_format":
        name = detail.get("name") or detail.get("field")
        format_name = detail.get("format")
        pattern = detail.get("pattern")
        if not name or not (format_name or pattern):
            return None
        field: dict[str, Any] = {"name": str(name)}
        if format_name:
            normalized_format = str(format_name).strip()
            if normalized_format not in SUPPORTED_FIELD_FORMATS:
                return None
            field["format"] = normalized_format
        else:
            field["pattern"] = str(pattern)
            if detail.get("expected"):
                field["expected"] = str(detail["expected"])
        return _rule(rule_id, "field_format", "warning", {"fields": [field]})

    if kind == "heading_numbering":
        params: dict[str, Any] = {
            "require_numbering": bool(detail.get("require_numbering", True)),
            "allow_mixed_styles": bool(detail.get("allow_mixed_styles", False)),
        }
        allowed_styles = detail.get("allowed_styles")
        if isinstance(allowed_styles, dict):
            params["allowed_styles"] = allowed_styles
        return _rule(rule_id, "heading_numbering", "warning", params)

    if kind == "layout":
        size = str(detail.get("page_size", detail.get("size", "A4"))).upper()
        if size not in {"A4", "A3", "LETTER"}:
            return None
        orientation = str(detail.get("orientation", "portrait"))
        if orientation not in {"portrait", "landscape", "any"}:
            return None
        params = {"page": {"size": size, "orientation": orientation}}
        margins = detail.get("margins_mm") or detail.get("margins")
        body = detail.get("body")
        headings = detail.get("headings")
        if isinstance(margins, dict):
            params["margins_mm"] = margins
        if isinstance(body, dict):
            params["body"] = body
        if isinstance(headings, dict):
            params["headings"] = headings
        return _rule(rule_id, "layout_check", "warning", params)

    logger.warning("Dropped unsupported guideline requirement kind: %s", requirement)
    return None


def _rule(rule_id: str, rule_type: str, severity: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": rule_id,
        "type": rule_type,
        "severity": severity,
        "description": "Generated from guideline",
        "params": params,
    }


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return [str(value)]


def _first_int(detail: dict[str, Any], *keys: str) -> int | None:
    value = _first_value(detail, *keys)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_number(detail: dict[str, Any], *keys: str) -> int | float | None:
    value = _first_value(detail, *keys)
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def _first_value(detail: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in detail:
            return detail[key]
    return None
