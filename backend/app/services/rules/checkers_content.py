"""
Content checkers: figure_table_numbering, budget_check, date_check.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import List

from app.schemas.ir import DocumentIR
from .base_checker import BaseChecker, register_checker
from .rule_schema import RuleDef, RuleIssue


def _format_labels(label: str, numbers: List[int]) -> str:
    """把缺号数字渲染成可核对的「图2、图3」而不是 Python 列表 ``[2, 3]``。

    R09：实例定位核对需看出**具体是哪一个**图/表/编号，裸列表对人不可读、对评测不可锚定。"""
    return "、".join(f"{label}{number}" for number in numbers)


@register_checker("figure_table_numbering")
class FigureTableNumberingChecker(BaseChecker):
    """Check figure/table numbering continuity and cross-references."""

    checker_name = "figure_table_numbering"

    def check(self, rule: RuleDef, ir: DocumentIR) -> List[RuleIssue]:
        issues: List[RuleIssue] = []
        all_text = " ".join(b.text for b in ir.blocks)

        # Find all figure numbers in captions and references
        fig_nums = self._extract_numbers(all_text, r"图\s*(\d+)")
        table_nums = self._extract_numbers(all_text, r"表\s*(\d+)")

        issues.extend(self._check_continuity(rule, fig_nums, "图"))
        issues.extend(self._check_continuity(rule, table_nums, "表"))

        figure_captions = {
            int(match.group(1))
            for block in ir.blocks
            if (match := re.match(r"^\s*图\s*(\d+)\s*[^所示]", block.text))
        }
        missing_captions = sorted(set(fig_nums) - figure_captions)
        if figure_captions and missing_captions:
            issues.append(self._issue(
                rule,
                message=f"图题注缺失：{_format_labels('图', missing_captions)}",
                suggestion="请补充对应图的题注行",
            ))

        return issues

    @staticmethod
    def _extract_numbers(text: str, pattern: str) -> List[int]:
        return sorted(set(int(m) for m in re.findall(pattern, text)))

    def _check_continuity(
        self, rule: RuleDef, nums: List[int], label: str
    ) -> List[RuleIssue]:
        if not nums:
            return []
        issues: List[RuleIssue] = []
        expected = list(range(1, max(nums) + 1))
        missing = sorted(set(expected) - set(nums))
        if missing:
            issues.append(RuleIssue(
                rule_id=rule.id,
                severity=rule.severity,
                message=f"{label}编号不连续，缺少：{_format_labels(label, missing)}",
                suggestion=f"请检查{label}编号是否跳号",
                confidence="deterministic",
                layer="rule",
                checker=self.checker_name,
            ))
        return issues


@register_checker("budget_check")
class BudgetCheckChecker(BaseChecker):
    """Check budget table arithmetic and ratio limits.

    Validates: detail rows sum to the total row; ratio-capped categories
    (e.g. 管理费) stay within max ratio; amounts are numeric."""

    checker_name = "budget_check"

    def check(self, rule: RuleDef, ir: DocumentIR) -> List[RuleIssue]:
        max_ratio = rule.params.get("max_ratio", {})
        total_field = rule.params.get("total_field", "合计|总计")
        issues: List[RuleIssue] = []

        # Find budget table
        degraded = [t for t in ir.tables() if t.parse_warnings]
        budget_table = self._find_budget_table(ir)
        if budget_table is None:
            if degraded:
                first = degraded[0]
                pages = sorted({
                    t.page for t in degraded if t.page is not None
                })
                page_text = "、".join(str(p) for p in pages) or "未知页"
                issues.append(RuleIssue(
                    rule_id=rule.id,
                    severity="info",
                    message=(
                        f"存在 {len(degraded)} 处表格结构未能可靠解析"
                        f"（第 {page_text} 页），无法确认经费预算表内容"
                    ),
                    suggestion="请人工核对预算表及相关表格的实际内容",
                    confidence="manual_required",
                    layer="rule",
                    checker=self.checker_name,
                    page=first.page,
                    block_id=first.id,
                ))
            else:
                issues.append(self._issue(
                    rule,
                    message="未找到经费预算表",
                    suggestion="请确认申报书包含经费预算表",
                ))
            return issues

        if budget_table.parse_warnings:
            # 表格被解析出来但结构有降级（如空行补齐）：结论必须保留人工确认项
            issues.append(RuleIssue(
                rule_id=rule.id,
                severity="info",
                message=(
                    "预算表结构存在解析降级："
                    + "；".join(budget_table.parse_warnings[:2])
                ),
                suggestion="请人工核对预算表的合并单元格与空行",
                confidence="manual_required",
                layer="rule",
                checker=self.checker_name,
                page=budget_table.page,
                block_id=budget_table.id,
            ))

        rows = budget_table.table_data
        if not rows or len(rows) < 2:
            return issues

        # Parse amounts — find the column with numeric values
        amount_col = self._find_amount_column(rows)
        if amount_col is None:
            issues.append(self._issue(
                rule,
                message="预算表中未找到金额列",
                page=budget_table.page,
                block_id=budget_table.id,
                suggestion="请确保预算表包含数值金额列",
            ))
            return issues

        # Sum detail rows vs total
        detail_total = Decimal("0")
        total_row_val: Decimal | None = None
        unparsed_rows: list[str] = []
        total_pattern = re.compile(total_field)

        for row in rows[1:]:  # skip header
            # Check if this is the total row (any cell matches total keywords)
            row_text = " ".join(str(c) for c in row)
            if total_pattern.search(row_text):
                value = self._parse_amount(row[amount_col] if amount_col < len(row) else None)
                if value is None:
                    unparsed_rows.append(row_text[:80])
                else:
                    total_row_val = value
                continue

            val = self._parse_amount(row[amount_col] if amount_col < len(row) else None)
            if val is not None:
                detail_total += val
            elif amount_col < len(row) and str(row[amount_col] or "").strip() not in {"", "-", "—"}:
                unparsed_rows.append(row_text[:80])

        if total_row_val is not None and abs(detail_total - total_row_val) > Decimal("0.01"):
            issues.append(self._issue(
                rule,
                message=f"预算勾稽不平：分项合计 {detail_total:.1f} ≠ 总额 {total_row_val:.1f}",
                page=budget_table.page,
                block_id=budget_table.id,
                suggestion="请检查预算表各分项金额是否与合计一致",
            ))

        # Check category ratio limits
        for category, max_r in max_ratio.items():
            for row in rows:
                row_text = " ".join(str(c) for c in row)
                if category in row_text and total_row_val and total_row_val > 0:
                    cat_val = self._parse_amount(row[amount_col] if amount_col < len(row) else None)
                    if cat_val is not None:
                        ratio = cat_val / total_row_val
                        if ratio > Decimal(str(max_r)):
                            issues.append(self._issue(
                                rule,
                                message=f"「{category}」占比 {float(ratio):.1%} 超过上限 {max_r:.0%}",
                                page=budget_table.page,
                                block_id=budget_table.id,
                                suggestion=f"请将{category}调整至不超过{max_r:.0%}",
                            ))

        if unparsed_rows:
            issues.append(RuleIssue(
                rule_id=rule.id,
                severity="info",
                message=f"预算表有 {len(unparsed_rows)} 行金额无法可靠解析",
                evidence="；".join(unparsed_rows[:3]),
                suggestion="请人工核对金额格式、合并单元格和计算公式",
                confidence="manual_required",
                layer="rule",
                checker=self.checker_name,
                page=budget_table.page,
                block_id=budget_table.id,
            ))

        return issues

    @staticmethod
    def _find_budget_table(ir: DocumentIR):
        for t in ir.tables():
            if t.table_data:
                for row in t.table_data:
                    row_str = " ".join(str(c) for c in row)
                    if any(kw in row_str for kw in ["预算", "经费", "金额", "合计"]):
                        return t
        return None

    @staticmethod
    def _find_amount_column(rows: List[List[str]]) -> int | None:
        """Find which column contains numeric amounts.

        Prefers amount-keyword headers (金额, 万元, 经费, 费用), else the most-numeric column."""
        if not rows or not rows[0]:
            return None

        amount_keywords = ["金额", "万元", "万"]

        # First pass: check headers for amount keywords
        for col_idx, header in enumerate(rows[0]):
            header_str = str(header)
            if any(kw in header_str for kw in amount_keywords):
                return col_idx

        # Second pass: find most-numeric column (excluding first col which is often index)
        best_col = None
        best_count = 0
        for col_idx in range(len(rows[0])):
            numeric_count = 0
            for row in rows[1:]:
                if col_idx < len(row):
                    try:
                        value = BudgetCheckChecker._parse_amount(row[col_idx])
                        if value is None:
                            continue
                        numeric_count += 1
                    except (ValueError, TypeError, InvalidOperation):
                        pass
            # Prefer cols with high numeric density, skip pure-index cols
            if numeric_count > best_count and (col_idx > 0 or numeric_count < len(rows) - 1):
                best_count = numeric_count
                best_col = col_idx

        return best_col

    @staticmethod
    def _parse_amount(value) -> Decimal | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text or text in {"-", "—", "/"}:
            return None
        negative = text.startswith("(") and text.endswith(")")
        normalized = re.sub(r"[\s,，￥¥元]", "", text)
        normalized = normalized.replace("万元", "").replace("万", "")
        normalized = normalized.strip("()")
        match = re.fullmatch(r"[-+]?\d+(?:\.\d+)?", normalized)
        if not match:
            return None
        try:
            parsed = Decimal(normalized)
        except InvalidOperation:
            return None
        return -parsed if negative else parsed


@register_checker("date_check")
class DateCheckChecker(BaseChecker):
    """Check date validity: end > start, duration matches guide."""

    checker_name = "date_check"

    # Common Chinese date patterns
    DATE_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月")

    def check(self, rule: RuleDef, ir: DocumentIR) -> List[RuleIssue]:
        issues: List[RuleIssue] = []
        period_blocks = [
            block for block in ir.blocks
            if any(keyword in block.text for keyword in ["研究期限", "起止时间", "执行期限", "项目期限"])
        ]
        for block in period_blocks:
            dates = [(int(year), int(month)) for year, month in self.DATE_RE.findall(block.text)]
            if len(dates) >= 2:
                start, end = dates[0], dates[1]
            else:
                continue
            if start >= end:
                issues.append(self._issue(
                    rule,
                    message=f"研究期限日期异常：开始日期 {start[0]}年{start[1]}月 不早于结束日期",
                    page=block.page,
                    block_id=block.id,
                    suggestion="请修正研究起止日期",
                ))
                break

        # Check for research-period-context dates only
        expected_months = rule.params.get("expected_months", None)
        if expected_months:
            # Try to find dates near "研究期限" keyword
            period_dates = self._extract_period_dates(ir)
            if period_dates and len(period_dates) >= 2:
                ps = sorted(period_dates)
                actual = (ps[-1][0] - ps[0][0]) * 12 + (ps[-1][1] - ps[0][1]) + 1
                if actual != expected_months:
                    issues.append(self._issue(
                        rule,
                        message=f"研究期限 {actual} 个月，与指南要求的 {expected_months} 个月不一致",
                        suggestion="请按指南调整研究期限",
                    ))

        return issues

    def _extract_period_dates(self, ir: DocumentIR):
        """Extract dates that appear near '研究期限' keywords."""
        period_dates = []
        for b in ir.blocks:
            if any(kw in b.text for kw in ["研究期限", "起止时间", "执行期限", "项目期限"]):
                period_dates.extend(self.DATE_RE.findall(b.text))
        # Also check nearby blocks (±2)
        for i, b in enumerate(ir.blocks):
            if any(kw in b.text for kw in ["研究期限", "起止时间"]):
                for j in range(max(0, i - 2), min(len(ir.blocks), i + 3)):
                    period_dates.extend(self.DATE_RE.findall(ir.blocks[j].text))
        return [(int(y), int(m)) for y, m in period_dates]
