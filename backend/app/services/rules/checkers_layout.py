"""Heading-numbering and page-layout checkers."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, List

from app.schemas.ir import Block, DocumentIR
from .base_checker import BaseChecker, register_checker
from .checkers_format import FontCheckChecker
from .rule_schema import RuleDef, RuleIssue


@dataclass(frozen=True)
class HeadingNumber:
    style: str
    level: int
    numbers: tuple[int, ...]
    prefix: str


_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
    "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


def _chinese_number(value: str) -> int | None:
    if not value:
        return None
    if value == "十":
        return 10
    if "百" in value:
        left, right = value.split("百", 1)
        hundreds = _CN_DIGITS.get(left, 1) * 100
        remainder = _chinese_number(right) if right else 0
        return hundreds + (remainder or 0)
    if "十" in value:
        left, right = value.split("十", 1)
        tens = _CN_DIGITS.get(left, 1) * 10
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return tens + ones
    if all(char in _CN_DIGITS for char in value):
        if len(value) == 1:
            return _CN_DIGITS[value]
        return int("".join(str(_CN_DIGITS[char]) for char in value))
    return None


def parse_heading_number(text: str, single_arabic_level: int = 1) -> HeadingNumber | None:
    stripped = text.strip()
    patterns = (
        (r"^第\s*(\d+)\s*[章节篇]\s*", "chapter", 1),
        (r"^([一二三四五六七八九十百零〇两]+)[、．.]\s*", "chinese", 1),
        (r"^[（(]([一二三四五六七八九十百零〇两]+)[）)]\s*", "chinese_parenthesized", 2),
        (r"^[（(](\d+)[）)]\s*", "arabic_parenthesized", 2),
        (r"^(\d+(?:\.\d+)+)[．.、]?\s*", "decimal", 0),
        (r"^(\d+)[．.、]\s*", "arabic", single_arabic_level),
    )
    for pattern, style, configured_level in patterns:
        match = re.match(pattern, stripped)
        if match is None:
            continue
        raw = match.group(1)
        if style.startswith("chinese"):
            number = _chinese_number(raw)
            if number is None:
                return None
            numbers = (number,)
        else:
            numbers = tuple(int(part) for part in raw.split("."))
        level = len(numbers) if style == "decimal" else configured_level
        return HeadingNumber(style, level, numbers, match.group(0).strip())
    return None


@register_checker("heading_numbering")
class HeadingNumberingChecker(BaseChecker):
    """Validate heading hierarchy, sequence and numbering-style consistency."""

    checker_name = "heading_numbering"

    def check(self, rule: RuleDef, ir: DocumentIR) -> List[RuleIssue]:
        params = rule.params
        require_numbering = bool(params.get("require_numbering", False))
        allow_mixed = bool(params.get("allow_mixed_styles", False))
        single_level = int(params.get("single_arabic_level", 1))
        skip_pages = {int(page) for page in params.get("skip_pages", [1])}
        max_issues = max(1, int(params.get("max_issues", 20)))
        allowed_styles = params.get("allowed_styles", {})
        if not isinstance(allowed_styles, dict):
            raise ValueError("allowed_styles must be an object keyed by heading level")

        numbered: list[tuple[Block, HeadingNumber]] = []
        issues: List[RuleIssue] = []
        for heading in ir.headings():
            if heading.page in skip_pages:
                continue
            token = parse_heading_number(heading.text, single_level)
            if token is None:
                if require_numbering:
                    issues.append(self._heading_issue(
                        rule,
                        heading,
                        f"标题未使用可识别编号：{heading.text}",
                        "请按统一的标题编号体系补充编号",
                    ))
                continue
            numbered.append((heading, token))
            configured = allowed_styles.get(str(token.level), allowed_styles.get(token.level))
            if configured:
                styles = [configured] if isinstance(configured, str) else list(configured)
                if token.style not in styles:
                    issues.append(self._heading_issue(
                        rule,
                        heading,
                        f"第{token.level}级标题编号样式不符合要求：{token.prefix}",
                        f"允许的编号样式：{', '.join(str(item) for item in styles)}",
                    ))

        previous_level = 0
        last_at_level: dict[int, int] = {}
        style_at_level: dict[int, str] = {}
        sequence_at_context: dict[tuple[Any, ...], int] = {}
        reported_style_levels: set[int] = set()

        for heading, token in numbered:
            if previous_level and token.level > previous_level + 1:
                issues.append(self._heading_issue(
                    rule,
                    heading,
                    f"标题层级从第{previous_level}级跳到第{token.level}级：{token.prefix}",
                    "请补齐中间层级或调整标题级别",
                ))

            prior_style = style_at_level.get(token.level)
            if (
                prior_style
                and prior_style != token.style
                and not allow_mixed
                and token.level not in reported_style_levels
            ):
                issues.append(self._heading_issue(
                    rule,
                    heading,
                    f"第{token.level}级标题混用编号样式：{prior_style} / {token.style}",
                    "请统一同一层级的标题编号样式",
                ))
                reported_style_levels.add(token.level)
            style_at_level[token.level] = token.style

            for level in list(last_at_level):
                if level > token.level:
                    last_at_level.pop(level, None)

            if token.style == "decimal" and len(token.numbers) > 1:
                parent = last_at_level.get(token.level - 1)
                expected_parent = token.numbers[-2]
                if parent is not None and parent != expected_parent:
                    issues.append(self._heading_issue(
                        rule,
                        heading,
                        f"标题编号父级不匹配：{token.prefix}",
                        "请使小节编号与当前上级标题编号一致",
                    ))

            parent_context = tuple(
                last_at_level.get(level, 0) for level in range(1, token.level)
            )
            context = (token.level, parent_context)
            current_number = token.numbers[-1]
            previous_number = sequence_at_context.get(context)
            if previous_number is not None and current_number != previous_number + 1:
                issues.append(self._heading_issue(
                    rule,
                    heading,
                    f"标题编号不连续：{previous_number} 后出现 {token.prefix}",
                    f"请将本级编号调整为 {previous_number + 1}",
                ))
            sequence_at_context[context] = current_number
            last_at_level[token.level] = current_number
            previous_level = token.level

        return issues[:max_issues]

    def _heading_issue(
        self,
        rule: RuleDef,
        heading: Block,
        message: str,
        suggestion: str,
    ) -> RuleIssue:
        return self._issue(
            rule,
            message=message,
            page=heading.page,
            block_id=heading.id,
            evidence=heading.text,
            suggestion=suggestion,
        )


_PAGE_SIZES = {
    "A4": (595.28, 841.89),
    "A3": (841.89, 1190.55),
    "LETTER": (612.0, 792.0),
}
_PT_PER_MM = 72 / 25.4


@register_checker("layout_check")
class LayoutCheckChecker(BaseChecker):
    """Validate measurable page, margin, paragraph and heading layout signals."""

    checker_name = "layout_check"

    def check(self, rule: RuleDef, ir: DocumentIR) -> List[RuleIssue]:
        issues: List[RuleIssue] = []
        max_issues = max(1, int(rule.params.get("max_issues", 30)))
        issues.extend(self._check_pages(rule, ir))
        issues.extend(self._check_margins(rule, ir))
        issues.extend(self._check_body(rule, ir))
        issues.extend(self._check_headings(rule, ir))
        return issues[:max_issues]

    def _check_pages(self, rule: RuleDef, ir: DocumentIR) -> List[RuleIssue]:
        config = rule.params.get("page", {})
        if not config:
            return []
        if not isinstance(config, dict):
            raise ValueError("layout page configuration must be an object")
        size_name = str(config.get("size", "A4")).upper()
        expected = _PAGE_SIZES.get(size_name)
        if expected is None:
            raise ValueError(f"Unsupported page size: {size_name}")
        orientation = str(config.get("orientation", "portrait"))
        if orientation not in {"portrait", "landscape", "any"}:
            raise ValueError(f"Unsupported page orientation: {orientation}")
        tolerance = float(config.get("tolerance_pt", 12))
        expected_width, expected_height = expected
        if orientation == "landscape":
            expected_width, expected_height = expected_height, expected_width

        wrong: list[int] = []
        for page in ir.meta.page_meta:
            if orientation == "any":
                portrait_ok = (
                    abs(page.width - expected[0]) <= tolerance
                    and abs(page.height - expected[1]) <= tolerance
                )
                landscape_ok = (
                    abs(page.width - expected[1]) <= tolerance
                    and abs(page.height - expected[0]) <= tolerance
                )
                valid = portrait_ok or landscape_ok
            else:
                valid = (
                    abs(page.width - expected_width) <= tolerance
                    and abs(page.height - expected_height) <= tolerance
                )
            if not valid:
                wrong.append(page.page_number)
        if not wrong:
            return []
        return [self._issue(
            rule,
            message=f"页面尺寸或方向不符合 {size_name} {orientation} 要求：第{wrong}页",
            page=wrong[0],
            suggestion=f"请将页面设置为 {size_name}，方向为 {orientation}",
        )]

    def _check_margins(self, rule: RuleDef, ir: DocumentIR) -> List[RuleIssue]:
        config = rule.params.get("margins_mm", {})
        if not config:
            return []
        if not isinstance(config, dict):
            raise ValueError("margins_mm must be an object")
        excluded = {int(page) for page in config.get("exclude_pages", [])}
        failures: dict[str, list[int]] = {
            "left": [], "right": [], "top": [], "bottom": []
        }
        limits = {
            side: float(config.get(f"{side}_min", 0)) * _PT_PER_MM
            for side in failures
        }
        page_meta = {page.page_number: page for page in ir.meta.page_meta}
        for page_number, meta in page_meta.items():
            if page_number in excluded:
                continue
            blocks = [
                block for block in ir.blocks_on_page(page_number)
                if block.type in {"heading", "paragraph", "table", "list"}
                and block.text.strip()
                and block.bbox.x1 > block.bbox.x0
                and block.bbox.y1 > block.bbox.y0
            ]
            if not blocks:
                continue
            measured = {
                "left": min(block.bbox.x0 for block in blocks),
                "right": meta.width - max(block.bbox.x1 for block in blocks),
                "top": min(block.bbox.y0 for block in blocks),
                "bottom": meta.height - max(block.bbox.y1 for block in blocks),
            }
            for side, minimum in limits.items():
                if minimum > 0 and measured[side] + 1 < minimum:
                    failures[side].append(page_number)

        labels = {"left": "左", "right": "右", "top": "上", "bottom": "下"}
        return [
            self._issue(
                rule,
                message=f"第{pages}页{labels[side]}边距小于 {config[f'{side}_min']}mm",
                page=pages[0],
                suggestion=f"请将{labels[side]}边距调整到要求值以上",
            )
            for side, pages in failures.items() if pages
        ]

    def _check_body(self, rule: RuleDef, ir: DocumentIR) -> List[RuleIssue]:
        config = rule.params.get("body", {})
        if not config:
            return []
        if not isinstance(config, dict):
            raise ValueError("layout body configuration must be an object")
        paragraphs = [
            block for block in ir.paragraphs()
            if block.page not in set(ir.meta.ocr_pages) and block.font.size > 0
        ]
        if not paragraphs:
            return []
        issues: List[RuleIssue] = []
        sizes = Counter(round(block.font.size, 1) for block in paragraphs)
        dominant_size = sizes.most_common(1)[0][0]
        min_size = config.get("min_size")
        max_size = config.get("max_size")
        if min_size is not None and dominant_size < float(min_size):
            issues.append(self._issue(
                rule,
                message=f"正文主字号 {dominant_size}pt 小于要求 {min_size}pt",
                suggestion="请调整正文主字号",
            ))
        if max_size is not None and dominant_size > float(max_size):
            issues.append(self._issue(
                rule,
                message=f"正文主字号 {dominant_size}pt 大于要求 {max_size}pt",
                suggestion="请调整正文主字号",
            ))

        required_alignment = str(config.get("alignment", ""))
        if required_alignment:
            wrong = [
                block for block in paragraphs
                if block.layout.alignment and block.layout.alignment != required_alignment
            ]
            if wrong:
                issues.append(self._issue(
                    rule,
                    message=f"检测到 {len(wrong)} 个正文段落未按 {required_alignment} 对齐",
                    page=wrong[0].page,
                    block_id=wrong[0].id,
                    evidence=wrong[0].text,
                    suggestion=f"请将正文对齐方式统一为 {required_alignment}",
                ))

        issues.extend(self._check_paragraph_metric(
            rule, paragraphs, config, "line_spacing_pt", "行距", minimum_lines=2
        ))
        issues.extend(self._check_paragraph_metric(
            rule, paragraphs, config, "first_line_indent_pt", "首行缩进", minimum_lines=2
        ))
        return issues

    def _check_paragraph_metric(
        self,
        rule: RuleDef,
        paragraphs: list[Block],
        config: dict,
        metric: str,
        label: str,
        minimum_lines: int,
    ) -> List[RuleIssue]:
        expected = config.get(metric)
        minimum = config.get(metric.replace("_pt", "_min_pt"))
        maximum = config.get(metric.replace("_pt", "_max_pt"))
        if expected is None and minimum is None and maximum is None:
            return []
        tolerance = float(config.get(f"{metric}_tolerance", 2))
        wrong: list[Block] = []
        for block in paragraphs:
            value = getattr(block.layout, metric)
            if value is None or block.layout.line_count < minimum_lines:
                continue
            invalid = False
            if expected is not None and abs(value - float(expected)) > tolerance:
                invalid = True
            if minimum is not None and value < float(minimum) - tolerance:
                invalid = True
            if maximum is not None and value > float(maximum) + tolerance:
                invalid = True
            if invalid:
                wrong.append(block)
        if not wrong:
            return []
        return [self._issue(
            rule,
            message=f"检测到 {len(wrong)} 个正文段落的{label}不符合配置要求",
            page=wrong[0].page,
            block_id=wrong[0].id,
            evidence=wrong[0].text,
            suggestion=f"请统一正文{label}",
        )]

    def _check_headings(self, rule: RuleDef, ir: DocumentIR) -> List[RuleIssue]:
        configs = rule.params.get("headings", {})
        if not configs:
            return []
        if not isinstance(configs, dict):
            raise ValueError("layout headings configuration must be an object")
        issues: List[RuleIssue] = []
        for heading in ir.headings():
            config = configs.get(str(heading.level), configs.get(heading.level))
            if not config:
                continue
            if not isinstance(config, dict):
                raise ValueError("Each heading style must be an object")
            problems: list[str] = []
            required_font = str(config.get("font", ""))
            detected_font = FontCheckChecker._normalize_font(heading.font.name)
            if required_font and required_font not in detected_font:
                problems.append(f"字体为{heading.font.name or '未知'}，要求{required_font}")
            if config.get("size") is not None and abs(
                heading.font.size - float(config["size"])
            ) > float(config.get("size_tolerance", 0.5)):
                problems.append(f"字号为{heading.font.size}pt，要求{config['size']}pt")
            if config.get("min_size") is not None and heading.font.size < float(config["min_size"]):
                problems.append(f"字号{heading.font.size}pt过小")
            if config.get("max_size") is not None and heading.font.size > float(config["max_size"]):
                problems.append(f"字号{heading.font.size}pt过大")
            if config.get("bold") is True and not heading.font.bold:
                problems.append("未加粗")
            required_alignment = str(config.get("alignment", ""))
            if required_alignment and heading.layout.alignment != required_alignment:
                problems.append(f"未按{required_alignment}对齐")
            if problems:
                issues.append(self._issue(
                    rule,
                    message=f"第{heading.level}级标题版式不符合要求：{'；'.join(problems)}",
                    page=heading.page,
                    block_id=heading.id,
                    evidence=heading.text,
                    suggestion="请按指南统一该级标题版式",
                ))
        return issues
