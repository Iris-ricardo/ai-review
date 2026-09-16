"""Configurable required-field and field-format checkers."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Callable, List

from app.schemas.ir import Block, DocumentIR
from .base_checker import BaseChecker, register_checker
from .rule_schema import RuleDef, RuleIssue


@dataclass(frozen=True)
class FieldMatch:
    value: str
    page: int | None
    block_id: str | None
    evidence: str


_EMPTY_MARKERS = {
    "",
    "请填写",
    "待填写",
    "未填写",
    "待补充",
    "暂缺",
}


def _aliases(field: dict) -> list[str]:
    name = str(field.get("name", "")).strip()
    if not name:
        raise ValueError("Field configuration requires a non-empty name")
    configured = field.get("aliases", [])
    if isinstance(configured, str):
        configured = configured.split("|")
    if not isinstance(configured, list):
        raise ValueError(f"Field aliases for {name} must be a list or string")
    values = [name, *(str(item).strip() for item in configured)]
    return list(dict.fromkeys(item for item in values if item))


def _is_empty(value: str) -> bool:
    stripped = value.strip()
    compact = re.sub(r"[\s_＿\-—–/\\.。·]+", "", stripped)
    return compact in _EMPTY_MARKERS


def is_empty_field_value(value: str) -> bool:
    """Public empty-value predicate shared by field-based checkers."""
    return _is_empty(value)


def _trim_value(value: str) -> str:
    value = value.strip().strip("|；;")
    # Stop before another labeled field when several fields share one line.
    parts = re.split(
        r"\s+(?=[A-Za-z\u4e00-\u9fff（）()]{2,20}\s*[：:])",
        value,
        maxsplit=1,
    )
    return parts[0].strip()


def _inline_match(block: Block, alias: str) -> tuple[bool, list[FieldMatch]]:
    label_seen = False
    matches: list[FieldMatch] = []
    pattern = re.compile(re.escape(alias) + r"\s*[：:]\s*([^\n]{0,160})")
    for result in pattern.finditer(block.text):
        label_seen = True
        value = _trim_value(result.group(1))
        matches.append(FieldMatch(
            value=value,
            page=block.page,
            block_id=block.id,
            evidence=result.group(0)[:200],
        ))
    return label_seen, matches


def _table_matches(block: Block, aliases: list[str]) -> tuple[bool, list[FieldMatch]]:
    if not block.table_data:
        return False, []
    label_seen = False
    matches: list[FieldMatch] = []
    for row in block.table_data:
        cells = [str(cell or "").strip() for cell in row]
        for index, cell in enumerate(cells):
            for alias in aliases:
                same_cell = re.fullmatch(
                    re.escape(alias) + r"\s*[：:]?\s*(.*)",
                    cell,
                )
                if same_cell is None:
                    continue
                label_seen = True
                value = _trim_value(same_cell.group(1))
                if _is_empty(value):
                    value = next(
                        (_trim_value(candidate) for candidate in cells[index + 1:] if not _is_empty(candidate)),
                        "",
                    )
                matches.append(FieldMatch(
                    value=value,
                    page=block.page,
                    block_id=block.id,
                    evidence=" | ".join(cells)[:200],
                ))
                break
    return label_seen, matches


def extract_field_matches(field: dict, ir: DocumentIR) -> tuple[bool, list[FieldMatch]]:
    aliases = _aliases(field)
    label_seen = False
    matches: list[FieldMatch] = []
    for block in ir.blocks:
        for alias in aliases:
            seen, inline = _inline_match(block, alias)
            label_seen = label_seen or seen
            matches.extend(inline)
        seen, table = _table_matches(block, aliases)
        label_seen = label_seen or seen
        matches.extend(table)

    unique: list[FieldMatch] = []
    seen_keys: set[tuple[str, int | None, str | None]] = set()
    for match in matches:
        key = (match.value, match.page, match.block_id)
        if key not in seen_keys:
            seen_keys.add(key)
            unique.append(match)
    return label_seen, unique


def _configured_fields(rule: RuleDef) -> list[dict]:
    fields = rule.params.get("fields", [])
    if not isinstance(fields, list):
        raise ValueError("Rule parameter 'fields' must be a list")
    if any(not isinstance(field, dict) for field in fields):
        raise ValueError("Every field configuration must be an object")
    return fields


@register_checker("required_fields")
class RequiredFieldsChecker(BaseChecker):
    """Require labeled values in paragraphs or table cells."""

    checker_name = "required_fields"

    def check(self, rule: RuleDef, ir: DocumentIR) -> List[RuleIssue]:
        issues: List[RuleIssue] = []
        for field in _configured_fields(rule):
            if field.get("required", True) is False:
                continue
            name = str(field.get("name", "")).strip()
            label_seen, matches = extract_field_matches(field, ir)
            valid = [match for match in matches if not _is_empty(match.value)]
            if valid:
                continue
            first = matches[0] if matches else None
            message = (
                f"必填字段「{name}」未填写"
                if label_seen else
                f"未找到必填字段「{name}」"
            )
            issues.append(self._issue(
                rule,
                message=message,
                page=first.page if first else None,
                block_id=first.block_id if first else None,
                evidence=first.evidence if first else "",
                suggestion=f"请在申报书中完整填写{name}",
            ))
        return issues


def _validate_email(value: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", value) is not None


def _validate_cn_phone(value: str) -> bool:
    normalized = re.sub(r"[\s()（）-]", "", value)
    return re.fullmatch(r"(?:\+?86)?1[3-9]\d{9}", normalized) is not None


def _validate_phone(value: str) -> bool:
    normalized = re.sub(r"[\s()（）-]", "", value)
    return (
        re.fullmatch(r"(?:\+?86)?1[3-9]\d{9}", normalized) is not None
        or re.fullmatch(r"0\d{9,11}", normalized) is not None
    )


def _date_parts(value: str) -> tuple[int, int, int] | None:
    horizontal_space = r"[ \t]*"
    patterns = (
        rf"(\d{{4}}){horizontal_space}年{horizontal_space}"
        rf"(\d{{1,2}}){horizontal_space}月{horizontal_space}"
        rf"(\d{{1,2}}){horizontal_space}日",
        rf"(\d{{4}}){horizontal_space}[-/.]{horizontal_space}"
        rf"(\d{{1,2}}){horizontal_space}[-/.]{horizontal_space}"
        rf"(\d{{1,2}})",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, value.strip())
        if match:
            return tuple(int(item) for item in match.groups())
    return None


def _validate_date(value: str) -> bool:
    parts = _date_parts(value)
    if parts is None:
        return False
    try:
        date(*parts)
    except ValueError:
        return False
    return True


def _validate_cn_id(value: str) -> bool:
    normalized = value.strip().upper()
    if re.fullmatch(r"\d{17}[0-9X]", normalized) is None:
        return False
    try:
        date(
            int(normalized[6:10]),
            int(normalized[10:12]),
            int(normalized[12:14]),
        )
    except ValueError:
        return False
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    check_codes = "10X98765432"
    checksum = sum(
        int(number) * weight
        for number, weight in zip(normalized[:17], weights)
    )
    expected = check_codes[checksum % 11]
    return normalized[-1] == expected


def _validate_number(value: str) -> bool:
    normalized = re.sub(r"[,，￥¥元\s]", "", value)
    normalized = normalized.removesuffix("万元").removesuffix("万")
    return re.fullmatch(r"[-+]?\d+(?:\.\d+)?", normalized) is not None


def _validate_project_code(value: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{3,39}", value.strip()) is not None


_FORMAT_VALIDATORS: dict[str, tuple[Callable[[str], bool], str]] = {
    "email": (_validate_email, "有效邮箱地址"),
    "cn_mobile": (_validate_cn_phone, "中国大陆手机号码"),
    "phone": (_validate_phone, "手机或固定电话号码"),
    "cn_id": (_validate_cn_id, "有效的18位身份证号码"),
    "date": (_validate_date, "有效日期，如 2026年6月30日"),
    "number": (_validate_number, "有效数字"),
    "project_code": (_validate_project_code, "4-40 位字母、数字、下划线或连字符"),
}


@register_checker("field_format")
class FieldFormatChecker(BaseChecker):
    """Validate already-populated labeled values against formats or regexes."""

    checker_name = "field_format"

    def check(self, rule: RuleDef, ir: DocumentIR) -> List[RuleIssue]:
        issues: List[RuleIssue] = []
        for field in _configured_fields(rule):
            name = str(field.get("name", "")).strip()
            _, matches = extract_field_matches(field, ir)
            populated = [match for match in matches if not _is_empty(match.value)]
            format_name = str(field.get("format", "")).strip()
            pattern = str(field.get("pattern", "")).strip()
            if not format_name and not pattern:
                raise ValueError(f"Field format rule for {name} requires format or pattern")
            if format_name:
                configured = _FORMAT_VALIDATORS.get(format_name)
                if configured is None:
                    raise ValueError(f"Unsupported field format: {format_name}")
                validator, expected = configured
            else:
                try:
                    compiled = re.compile(pattern)
                except re.error as exc:
                    raise ValueError(f"Invalid regex for field {name}: {exc}") from exc
                validator = lambda value, regex=compiled: regex.fullmatch(value) is not None
                expected = str(field.get("expected", "符合配置的格式"))

            for match in populated:
                if validator(match.value):
                    continue
                issues.append(self._issue(
                    rule,
                    message=f"字段「{name}」格式不正确：{match.value}",
                    page=match.page,
                    block_id=match.block_id,
                    evidence=match.evidence,
                    suggestion=f"请填写{expected}",
                ))
        return issues
