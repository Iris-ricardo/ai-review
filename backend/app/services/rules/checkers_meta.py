"""
Meta checkers: attachment_checklist, signature_page, cross_field_consistency.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import List

from app.schemas.ir import INCOMPLETE_PAGE_STATUSES, DocumentIR
from .base_checker import BaseChecker, register_checker
from .checkers_fields import (
    FieldMatch,
    extract_field_matches,
    is_empty_field_value,
)
from .rule_schema import RuleDef, RuleIssue


class FieldValueNormalizationError(ValueError):
    """A document value cannot be converted using a valid normalization mode."""


def _manual_issue(rule: RuleDef, checker: str, message: str, suggestion: str) -> RuleIssue:
    return RuleIssue(
        rule_id=rule.id,
        severity="info",
        message=message,
        suggestion=suggestion,
        confidence="manual_required",
        layer="rule",
        checker=checker,
    )


def _incomplete_pages(ir: DocumentIR) -> list[int]:
    """未完整读取或读取可疑的页码（规则据此避免确定性误判）。"""
    return [
        meta.page_number
        for meta in ir.meta.page_meta
        if meta.status in INCOMPLETE_PAGE_STATUSES
    ]


def _pages_text(pages: list[int]) -> str:
    return "、".join(str(page) for page in pages)


@register_checker("attachment_checklist")
class AttachmentChecklistChecker(BaseChecker):
    """Check that listed attachments appear somewhere in the document."""

    checker_name = "attachment_checklist"

    def check(self, rule: RuleDef, ir: DocumentIR) -> List[RuleIssue]:
        expected = rule.params.get("expected", [])
        issues: List[RuleIssue] = []
        all_text = " ".join(b.text for b in ir.blocks)

        for att in expected:
            # Fuzzy match — check if key chars appear anywhere
            if not self._fuzzy_find(att, all_text):
                incomplete = _incomplete_pages(ir)
                if incomplete:
                    # 有页面没读出来时，“没找到附件”无法确定 → 人工确认
                    issues.append(_manual_issue(
                        rule,
                        self.checker_name,
                        f"「{att}」未在已读取文本中出现，但第 {_pages_text(incomplete)} 页"
                        "未完整读取，无法确认是否缺失",
                        f"请先完成 OCR 或人工确认{att}是否已附",
                    ))
                else:
                    issues.append(self._issue(
                        rule,
                        message=f"附件清单中「{att}」未在正文中出现",
                        suggestion=f"请确认是否已附上{att}",
                    ))
        if expected and not issues:
            issues.append(_manual_issue(
                rule,
                self.checker_name,
                "已找到附件清单文字，但系统无法证明附件文件真实存在",
                "请人工核对提交材料中的实际附件文件",
            ))
        return issues

    @staticmethod
    def _fuzzy_find(needle: str, haystack: str) -> bool:
        # Remove whitespace and compare
        n = needle.replace(" ", "").replace("\n", "")
        h = haystack.replace(" ", "").replace("\n", "")
        if len(n) >= 3 and n in h:
            return True
        # Try individual chars
        if len(n) >= 4:
            chars_found = sum(1 for c in n if c in h)
            return chars_found >= len(n) * 0.7
        return False


@register_checker("signature_page")
class SignaturePageChecker(BaseChecker):
    """Check that the document has a signature/seal page."""

    checker_name = "signature_page"

    def check(self, rule: RuleDef, ir: DocumentIR) -> List[RuleIssue]:
        issues: List[RuleIssue] = []
        total = ir.meta.pages
        # Check last 2 pages (signature info often spans 2 pages)
        last_pages = range(max(1, total - 1), total + 1)
        last_text = " ".join(
            b.text for p in last_pages for b in ir.blocks_on_page(p)
        )

        sig_keywords = rule.params.get("keywords", ["签字", "盖章"])
        found = [kw for kw in sig_keywords if kw in last_text]
        unread_last = [
            page for page in last_pages if ir.page_read_incomplete(page)
        ]

        if len(found) < len(sig_keywords):
            missing = set(sig_keywords) - set(found)
            if unread_last:
                # 末页未读出时无法断定关键字缺失（扫描页可能印有签章文字）
                issues.append(_manual_issue(
                    rule,
                    self.checker_name,
                    f"末页（第 {_pages_text(unread_last)} 页）未完整读取，"
                    f"无法确认签字盖章关键字是否缺失：{missing}",
                    "请先完成 OCR 或人工确认末页签字盖章内容",
                ))
            else:
                issues.append(self._issue(
                    rule,
                    message=f"末页缺少签字盖章关键字：{missing}",
                    page=total,
                    suggestion="请确认申报书末两页包含完整的签字盖章内容",
                ))
            return issues

        # 关键字齐全：存在图片也不能证明签章真实（校徽/插图/空白图同样会被
        # 抽成 image 块）——真实性系统无法验证，必须保留人工确认项（R03）。
        images = [
            block for page in last_pages for block in ir.blocks_on_page(page)
            if block.type == "image"
        ]
        if images:
            issues.append(_manual_issue(
                rule,
                self.checker_name,
                f"已找到签字盖章文字，并检测到 {len(images)} 个图像块，"
                "但系统无法验证图像是否为真实签章（不排除校徽、插图或空白图片）",
                "请人工核验签字与盖章的真实性、完整性和清晰度",
            ))
        else:
            issues.append(_manual_issue(
                rule,
                self.checker_name,
                "已找到签字盖章文字，但未检测到可验证的签章图像",
                "请人工确认签字和盖章是否真实、完整且清晰",
            ))

        return issues


@register_checker("cross_field_consistency")
class CrossFieldConsistencyChecker(BaseChecker):
    """Compare repeated fields and explicitly paired fields after normalization."""

    checker_name = "cross_field_consistency"

    def check(self, rule: RuleDef, ir: DocumentIR) -> List[RuleIssue]:
        fields = rule.params.get("fields", [])
        comparisons = rule.params.get("comparisons", [])
        if not isinstance(fields, list) or not isinstance(comparisons, list):
            raise ValueError("fields and comparisons must be lists")
        issues: List[RuleIssue] = []
        extracted: dict[str, list[FieldMatch]] = {}

        for field_cfg in fields:
            if not isinstance(field_cfg, dict):
                raise ValueError("Every field configuration must be an object")
            field_name = str(field_cfg.get("name", "")).strip()
            if not field_name:
                raise ValueError("Consistency field requires a non-empty name")
            matches = self._extract(field_cfg, ir)
            extracted[field_name] = matches

            validation_pattern = str(field_cfg.get("validation_pattern", "")).strip()
            if validation_pattern and matches:
                try:
                    validation_compiled = re.compile(validation_pattern)
                except re.error as exc:
                    raise ValueError(
                        f"Invalid validation pattern for {field_name}: {exc}"
                    ) from exc
                invalid = [m for m in matches if not validation_compiled.fullmatch(m.value)]
                if invalid:
                    match = invalid[0]
                    issues.append(self._issue(
                        rule,
                        message=f"「{field_name}」格式不符合规则：{match.value}",
                        page=match.page,
                        block_id=match.block_id,
                        evidence=match.evidence,
                        suggestion=f"请检查{field_name}格式和长度",
                    ))

            minimum = max(2, int(field_cfg.get("min_occurrences", 2)))
            if len(matches) < minimum:
                continue
            try:
                agrees = self._matches_agree(matches, field_cfg)
            except FieldValueNormalizationError as exc:
                issues.append(self._issue(
                    rule,
                    message=f"「{field_name}」无法进行一致性比较：{exc}",
                    page=matches[0].page,
                    block_id=matches[0].block_id,
                    evidence=matches[0].evidence,
                    suggestion=f"请检查{field_name}的填写格式",
                ))
                continue
            if not agrees:
                values = list(dict.fromkeys(match.value for match in matches))
                issues.append(self._issue(
                    rule,
                    message=f"「{field_name}」在多处取值不一致：{' / '.join(values[:4])}",
                    page=matches[0].page,
                    block_id=matches[0].block_id,
                    evidence="；".join(match.evidence for match in matches[:3]),
                    suggestion="请统一封面、正文、表格和附件中的字段取值",
                ))

        for comparison in comparisons:
            if not isinstance(comparison, dict):
                raise ValueError("Every comparison must be an object")
            left_name, left = self._comparison_side(
                comparison.get("left"), extracted, ir
            )
            right_name, right = self._comparison_side(
                comparison.get("right"), extracted, ir
            )
            if not left or not right:
                continue
            config = {
                "normalize": comparison.get("normalize", "text"),
                "tolerance": comparison.get("tolerance", 0),
            }
            try:
                agrees = self._comparison_agrees(left, right, config)
            except FieldValueNormalizationError as exc:
                issues.append(self._issue(
                    rule,
                    message=f"字段间无法进行一致性比较：{exc}",
                    page=right[0].page,
                    block_id=right[0].block_id,
                    evidence=f"{left[0].evidence}；{right[0].evidence}",
                    suggestion="请检查关联字段的填写格式",
                ))
                continue
            if agrees:
                continue
            issues.append(self._issue(
                rule,
                message=(
                    f"字段间取值不一致：「{left_name}」为 {left[0].value}，"
                    f"「{right_name}」为 {right[0].value}"
                ),
                page=right[0].page,
                block_id=right[0].block_id,
                evidence=f"{left[0].evidence}；{right[0].evidence}",
                suggestion=str(
                    comparison.get("suggestion", "请核对并统一关联字段")
                ),
            ))

        return issues

    @classmethod
    def _extract(cls, field: dict, ir: DocumentIR) -> list[FieldMatch]:
        _, labeled = extract_field_matches(field, ir)
        matches = [match for match in labeled if not is_empty_field_value(match.value)]
        pattern = str(field.get("pattern", "")).strip()
        if pattern:
            try:
                compiled = re.compile(pattern)
            except re.error as exc:
                raise ValueError(
                    f"Invalid extraction pattern for {field.get('name', '')}: {exc}"
                ) from exc
            for block in ir.blocks:
                for result in compiled.finditer(block.text):
                    if "value" in result.groupdict():
                        value = result.group("value")
                    elif result.groups():
                        value = result.group(1)
                    else:
                        value = result.group(0)
                    matches.append(FieldMatch(
                        value=value.strip(),
                        page=block.page,
                        block_id=block.id,
                        evidence=result.group(0)[:200],
                    ))
        unique: list[FieldMatch] = []
        seen: set[tuple[str, int | None, str | None]] = set()
        for match in matches:
            key = (match.value, match.page, match.block_id)
            if key not in seen:
                seen.add(key)
                unique.append(match)
        return unique

    @classmethod
    def _comparison_side(
        cls,
        side: object,
        extracted: dict[str, list[FieldMatch]],
        ir: DocumentIR,
    ) -> tuple[str, list[FieldMatch]]:
        if isinstance(side, str):
            return side, extracted.get(side, [])
        if isinstance(side, dict):
            name = str(side.get("name", "")).strip()
            if not name:
                raise ValueError("Comparison side requires a field name")
            return name, cls._extract(side, ir)
        raise ValueError("Comparison left/right must be a field name or object")

    @classmethod
    def _matches_agree(cls, matches: list[FieldMatch], config: dict) -> bool:
        mode = str(config.get("normalize", "text"))
        values = [cls._normalize(match.value, mode) for match in matches]
        if mode == "number":
            tolerance = Decimal(str(config.get("tolerance", 0)))
            return max(values) - min(values) <= tolerance
        return len(set(values)) <= 1

    @classmethod
    def _comparison_agrees(
        cls,
        left: list[FieldMatch],
        right: list[FieldMatch],
        config: dict,
    ) -> bool:
        compared = 0
        first_error: FieldValueNormalizationError | None = None
        for left_match in left:
            for right_match in right:
                try:
                    if cls._matches_agree([left_match, right_match], config):
                        return True
                    compared += 1
                except FieldValueNormalizationError as exc:
                    first_error = first_error or exc
        if compared:
            return False
        if first_error is not None:
            raise first_error
        return True

    @staticmethod
    def _normalize(value: str, mode: str = "text"):
        normalized = unicodedata.normalize("NFKC", value).strip()
        if mode == "number":
            compact = re.sub(r"[,，￥¥元\s]", "", normalized)
            multiplier = Decimal(10000) if compact.endswith("万") else Decimal(1)
            compact = compact.removesuffix("万元").removesuffix("万")
            try:
                return Decimal(compact) * multiplier
            except InvalidOperation as exc:
                raise FieldValueNormalizationError(
                    f"无法识别数字“{value}”"
                ) from exc
        if mode == "date":
            match = re.fullmatch(
                r"(\d{4})\s*(?:年|[-/.])\s*(\d{1,2})\s*(?:月|[-/.])\s*(\d{1,2})\s*日?",
                normalized,
            )
            if not match:
                raise FieldValueNormalizationError(f"无法识别日期“{value}”")
            try:
                return date(*(int(part) for part in match.groups())).isoformat()
            except ValueError as exc:
                raise FieldValueNormalizationError(
                    f"日期无效“{value}”"
                ) from exc
        if mode == "phone":
            digits = re.sub(r"\D", "", normalized)
            return digits[2:] if len(digits) == 13 and digits.startswith("86") else digits
        if mode == "list":
            items = re.split(r"[、,，;；/\s]+", normalized)
            return tuple(sorted(item.casefold() for item in items if item))
        if mode not in {"text", "casefold"}:
            raise ValueError(f"Unsupported consistency normalization: {mode}")
        compact = re.sub(r"[\s\u3000]+", "", normalized)
        compact = re.sub(r"[，,。；;：:‘’“”\"']+$", "", compact)
        return compact.casefold() if mode == "casefold" else compact
