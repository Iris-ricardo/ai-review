"""
Format checkers: page_limit, word_limit, font_check, page_number_check.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import List

from app.schemas.ir import DocumentIR
from .base_checker import BaseChecker, register_checker
from .rule_schema import RuleDef, RuleIssue


@register_checker("page_limit")
class PageLimitChecker(BaseChecker):
    """Check overall or per-section page limits."""

    checker_name = "page_limit"

    def check(self, rule: RuleDef, ir: DocumentIR) -> List[RuleIssue]:
        max_pages = rule.params.get("max_pages", None)
        issues: List[RuleIssue] = []

        if max_pages is not None and ir.meta.pages > max_pages:
            issues.append(self._issue(
                rule,
                message=f"总页数超出限制（{ir.meta.pages}页 > {max_pages}页）",
                page=ir.meta.pages,
                suggestion=f"请缩减内容至{max_pages}页以内",
            ))

        # Per-section page limits
        section_limits = rule.params.get("section_limits", {})
        for sec_title, sec_max in section_limits.items():
            for h in ir.headings():
                if sec_title in h.text:
                    start_page = h.page
                    # Find next level-1 heading
                    end_page = ir.meta.pages
                    for h2 in ir.headings(level=1):
                        if h2.page > start_page:
                            end_page = h2.page - 1
                            break
                    section_pages = end_page - start_page + 1
                    if sec_max is not None and section_pages > sec_max:
                        issues.append(self._issue(
                            rule,
                            message=f"章节「{h.text}」页数超限（{section_pages}页 > {sec_max}页）",
                            page=h.page,
                            suggestion=f"请缩减至{sec_max}页以内",
                        ))
                    break

        return issues


@register_checker("word_limit")
class WordLimitChecker(BaseChecker):
    """Check overall or per-section word count limits."""

    checker_name = "word_limit"

    def check(self, rule: RuleDef, ir: DocumentIR) -> List[RuleIssue]:
        max_words = rule.params.get("max_words", None)
        issues: List[RuleIssue] = []

        if max_words is not None and ir.meta.word_count > max_words:
            issues.append(self._issue(
                rule,
                message=f"总字数超出限制（{ir.meta.word_count}字 > {max_words}字）",
                suggestion=f"请缩减至{max_words}字以内",
            ))

        section_limits = rule.params.get("section_limits", {})
        for sec_title, sec_max in section_limits.items():
            for h in ir.headings():
                if sec_title in h.text:
                    wc = self._count_section_words(ir, h)
                    if wc > sec_max:
                        issues.append(self._issue(
                            rule,
                            message=f"章节「{h.text}」字数超限（{wc}字 > {sec_max}字）",
                            page=h.page,
                            suggestion=f"请缩减至{sec_max}字以内",
                        ))
                    break

        return issues

    @staticmethod
    def _count_section_words(ir: DocumentIR, heading_block) -> int:
        start_idx = ir.blocks.index(heading_block)
        count = 0
        for b in ir.blocks[start_idx + 1:]:
            if b.type == "heading" and b.level <= heading_block.level:
                break
            count += b.word_count
        return count


@register_checker("font_check")
class FontCheckChecker(BaseChecker):
    """Verify body font, size, and line spacing requirements."""

    checker_name = "font_check"

    # Font name normalization: PDF-embedded names → Chinese standard names
    FONT_ALIASES = {
        "SimSun": "宋体", "NSimSun": "宋体", "宋体": "宋体",
        "SimHei": "黑体", "Hei": "黑体", "黑体": "黑体",
        "KaiTi": "楷体", "KaiTi_GB2312": "楷体", "楷体": "楷体",
        "FangSong": "仿宋", "FangSong_GB2312": "仿宋", "仿宋": "仿宋",
        "MicrosoftYaHei": "微软雅黑",
    }

    def check(self, rule: RuleDef, ir: DocumentIR) -> List[RuleIssue]:
        required_font = rule.params.get("body_font", "宋体")
        required_size = rule.params.get("body_size", 12)
        issues: List[RuleIssue] = []

        # Determine body font from IR metadata (most frequent font)
        body_font_name = self._dominant_font(ir)
        # Normalize font names for comparison
        normalized = self._normalize_font(body_font_name)
        # Determine body size from all paragraph blocks
        para_sizes = [
            b.font.size for b in ir.blocks
            if b.type == "paragraph"
            and b.page not in set(ir.meta.ocr_pages)
            and b.font.size > 0
        ]
        body_size = (
            Counter(round(s, 1) for s in para_sizes).most_common(1)[0][0]
            if para_sizes else 0
        )

        if normalized and required_font not in normalized:
            issues.append(self._issue(
                rule,
                message=f"正文字体不符合要求（检测为{body_font_name}，要求{required_font}）",
                suggestion=f"请将正文字体改为{required_font}",
            ))

        if body_size and abs(body_size - required_size) > 0.5:
            issues.append(self._issue(
                rule,
                message=f"正文字号不符合要求（检测为{body_size}pt，要求{required_size}pt）",
                suggestion=f"请将正文字号改为{required_size}pt",
            ))

        if ir.meta.ocr_pages:
            issues.append(RuleIssue(
                rule_id=rule.id,
                severity="info",
                message=f"第 {ir.meta.ocr_pages} 页使用 OCR，无法可靠验证原始字体和字号",
                suggestion="请人工核对扫描页的原始版式",
                confidence="manual_required",
                layer="rule",
                checker=self.checker_name,
            ))

        return issues

    @staticmethod
    def _dominant_font(ir: DocumentIR) -> str:
        names = [
            b.font.name for b in ir.blocks
            if b.type == "paragraph"
            and b.page not in set(ir.meta.ocr_pages)
            and b.font.name
        ]
        if not names:
            return ""
        return Counter(names).most_common(1)[0][0]

    @classmethod
    def _normalize_font(cls, name: str) -> str:
        cleaned = re.sub(r"^[A-Z]{6}\+", "", name or "").replace(" ", "")
        if cleaned in cls.FONT_ALIASES:
            return cls.FONT_ALIASES[cleaned]
        for alias, normalized in cls.FONT_ALIASES.items():
            if alias in cleaned:
                return normalized
        return cleaned


@register_checker("page_number_check")
class PageNumberCheckChecker(BaseChecker):
    """Check that pages are numbered and consecutive."""

    checker_name = "page_number_check"

    def check(self, rule: RuleDef, ir: DocumentIR) -> List[RuleIssue]:
        issues: List[RuleIssue] = []
        # Empty pages are a separate structural signal.
        for p in range(1, ir.meta.pages + 1):
            if not ir.blocks_on_page(p):
                issues.append(self._issue(
                    rule,
                    message=f"第{p}页无内容",
                    page=p,
                    suggestion="请检查文档是否有空白页",
                ))

        # Check page count matches page_meta.
        if len(ir.meta.page_meta) != ir.meta.pages:
            issues.append(self._issue(
                rule,
                message=f"页码元数据不匹配（meta有{len(ir.meta.page_meta)}条，实际{ir.meta.pages}页）",
            ))

        allow_front = int(rule.params.get("allow_unumbered_front_pages", 1))
        detected: list[tuple[int, int]] = []
        page_heights = {meta.page_number: meta.height for meta in ir.meta.page_meta}
        for block in ir.blocks:
            if block.page is None:
                continue
            height = page_heights.get(block.page, 0)
            text = block.text.strip()
            match = re.fullmatch(r"[-—–\s]*([0-9]{1,4})[-—–\s]*", text)
            if match and height and block.bbox.y0 >= height * 0.82:
                detected.append((block.page, int(match.group(1))))

        required_pages = list(range(allow_front + 1, ir.meta.pages + 1))
        if required_pages and not detected:
            issues.append(RuleIssue(
                rule_id=rule.id,
                severity="info",
                message="未检测到可验证的页脚页码",
                suggestion="请人工确认页码是否存在且连续",
                confidence="manual_required",
                layer="rule",
                checker=self.checker_name,
            ))
            return issues

        by_page = dict(detected)
        missing = [page for page in required_pages if page not in by_page]
        if missing:
            issues.append(self._issue(
                rule,
                message=f"以下页面未检测到页码：{missing}",
                suggestion="请补充缺失页码并检查页脚位置",
            ))
        sequence = [by_page[page] for page in required_pages if page in by_page]
        if len(sequence) >= 2 and any(
            current != previous + 1
            for previous, current in zip(sequence, sequence[1:])
        ):
            issues.append(self._issue(
                rule,
                message=f"页码不连续：{sequence}",
                suggestion="请重新设置连续页码",
            ))

        return issues
