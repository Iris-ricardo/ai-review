"""
Structure checkers: required_sections, section_order.
"""
from __future__ import annotations

import re
from typing import List

from app.schemas.ir import DocumentIR, Block
from .base_checker import BaseChecker, register_checker
from .rule_schema import RuleDef, RuleIssue


@register_checker("required_sections")
class RequiredSectionsChecker(BaseChecker):
    """Check that all required sections exist and meet minimum word counts."""

    checker_name = "required_sections"

    def check(self, rule: RuleDef, ir: DocumentIR) -> List[RuleIssue]:
        sections = rule.params.get("sections", [])
        issues: List[RuleIssue] = []
        heading_texts = [h.text for h in ir.headings()]

        for sec in sections:
            title_pattern = sec.get("title", "")
            min_words = sec.get("min_words", 0)
            aliases = title_pattern.split("|")

            # Find matching heading
            matched_heading = None
            for alias in aliases:
                for ht in heading_texts:
                    if alias in ht:
                        matched_heading = ht
                        break
                if matched_heading:
                    break

            if matched_heading is None:
                issues.append(self._issue(
                    rule,
                    message=f"缺少必填章节：{aliases[0]}",
                    suggestion=f"请添加「{aliases[0]}」章节",
                ))
                continue

            # If min_words specified, check word count in blocks after heading
            if min_words > 0 and matched_heading:
                word_count = self._count_words_after_heading(ir, matched_heading)
                if word_count < min_words:
                    issues.append(self._issue(
                        rule,
                        message=f"章节「{matched_heading}」字数不足（{word_count}字 < {min_words}字）",
                        suggestion=f"请扩充内容至{min_words}字以上",
                    ))

        return issues

    @staticmethod
    def _count_words_after_heading(ir: DocumentIR, heading_text: str) -> int:
        """Count words after a heading until next heading of same or higher level."""
        heading_idx = None
        heading_level = 1  # default
        for i, b in enumerate(ir.blocks):
            if b.type == "heading" and heading_text in b.text:
                heading_idx = i
                heading_level = b.level
                break
        if heading_idx is None:
            return 0
        count = 0
        for b in ir.blocks[heading_idx + 1:]:
            # Stop at same-or-higher level heading (not sub-headings)
            if b.type == "heading" and b.level <= heading_level:
                break
            count += b.word_count
        return count


@register_checker("section_order")
class SectionOrderChecker(BaseChecker):
    """Check that sections appear in the expected order."""

    checker_name = "section_order"

    def check(self, rule: RuleDef, ir: DocumentIR) -> List[RuleIssue]:
        expected = rule.params.get("expected_order", [])
        heading_texts = [h.text for h in ir.headings(level=1)]

        # Extract the order of matched sections
        found_order: List[str] = []
        for ht in heading_texts:
            for i, pattern in enumerate(expected):
                if pattern in ht and pattern not in found_order:
                    found_order.append(pattern)

        if len(found_order) < 2:
            return []

        # Check relative ordering
        issues: List[RuleIssue] = []
        expected_positions = [expected.index(pattern) for pattern in found_order]
        for i in range(1, len(expected_positions)):
            if expected_positions[i] < expected_positions[i - 1]:
                pattern = found_order[i]
                expected_idx = expected_positions[i]
                issues.append(self._issue(
                    rule,
                    message=f"章节顺序不符合预期：「{pattern}」应在第{expected_idx+1}位",
                    suggestion="请按申报指南要求的顺序调整章节",
                ))
                break  # One ordering issue is enough

        return issues
