"""R04 回归测试：合并单元格/空单元格/不规则行不得让表格解析失败。

原缺陷：PyMuPDF ``table.extract()`` 对合并单元格返回 ``None``，直接写入 ``Block.table_data`` 触发 ValidationError，整篇解析失败且无降级信息。
覆盖：归一化（None/空单元格/不规则行/整行 None，列不漂移、不出现 "None"）；真实 PDF 提取（横纵合并/空单元格/跨页重复表头/预算表）；彻底失败降级为明确信息 + 保留文字 + 预算转人工。"""
from __future__ import annotations

import time
from pathlib import Path

import fitz
import pytest
from starlette.testclient import TestClient

from app.main import app
from app.schemas.ir import DocumentIR
from app.services.parser.pdf_parser import PDFParser, _normalize_table_rows
from app.services.rules.engine import RuleEngine

client = TestClient(app)


def _run_review(pdf_path: str, timeout: float = 60.0) -> dict:
    with open(pdf_path, "rb") as handle:
        response = client.post(
            "/api/v1/documents",
            files={"file": (Path(pdf_path).name, handle, "application/pdf")},
        )
    assert response.status_code == 200, response.text
    document_id = response.json()["document_id"]
    created = client.post(
        "/api/v1/reviews",
        data={"document_id": document_id, "ruleset_id": "campus", "use_ai": "false"},
    )
    assert created.status_code == 200, created.text
    review_id = created.json()["review_id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = client.get(f"/api/v1/reviews/{review_id}").json()
        if payload.get("status") in {"done", "failed", "cancelled"}:
            return payload
        time.sleep(0.05)
    raise AssertionError("review did not finish in time")

# ── 1. 归一化函数 ─────────────────────────────────────────

def test_none_cells_become_empty_string_without_column_shift():
    rows, warnings = _normalize_table_rows([
        ["科目", "金额", "备注"],
        ["管理费", "2.4", None],      # 合并单元格空位
        ["材料费", "10.0", "含耗材"],
    ])
    assert rows[1] == ["管理费", "2.4", ""]
    assert len({len(row) for row in rows}) == 1  # 列数一致
    assert "None" not in " ".join(" ".join(row) for row in rows)
    assert warnings == []  # 合并单元格占位属正常语义，不告警


def test_ragged_and_none_rows_are_reported():
    rows, warnings = _normalize_table_rows([
        ["A", "B", "C"],
        ["1", "2"],                   # 不规则行 → 补空
        None,                         # 整行 None → 空行
    ])
    assert rows[1] == ["1", "2", ""]
    assert rows[2] == ["", "", ""]
    assert any("长度不足" in w for w in warnings)
    assert any("空行" in w for w in warnings)


def test_non_string_cells_are_stringified():
    rows, _ = _normalize_table_rows([["项目", "金额"], ["合计", 30.0]])
    assert rows[1][1] == "30.0"


def test_empty_input_yields_warning():
    rows, warnings = _normalize_table_rows(None)
    assert rows == [] and warnings


# ── 2. 真实 PDF 表格提取 ─────────────────────────────────

def _draw_table(page, x0, y0, cell_w, cell_h, rows, cols,
                skip_vlines=(), skip_hlines=()):
    """用 ruling lines 画表格网格，并在单元格内写入文本。

    skip_vlines / skip_hlines: {(row_index, col_index)} 该单元格左侧竖线 / 上方横线不画（横向合并 / 纵向合并）。"""
    width = cell_w * cols
    height = cell_h * rows
    shape = page.new_shape()
    for col in range(cols + 1):
        for row in range(rows):
            if col in {c + 1 for r, c in skip_vlines if r == row}:
                continue
            x = x0 + col * cell_w
            shape.draw_line(fitz.Point(x, y0 + row * cell_h),
                            fitz.Point(x, y0 + (row + 1) * cell_h))
    for row in range(rows + 1):
        for col in range(cols):
            if row >= 1 and (row - 1, col) in skip_hlines:
                continue
            y = y0 + row * cell_h
            shape.draw_line(fitz.Point(x0 + col * cell_w, y),
                            fitz.Point(x0 + (col + 1) * cell_w, y))
    shape.finish(color=(0, 0, 0), width=0.8)
    shape.commit()


def _fill_cells(page, x0, y0, cell_w, cell_h, values):
    for row_index, row in enumerate(values):
        for col_index, text in enumerate(row):
            if text:
                page.insert_text(
                    (x0 + col_index * cell_w + 4, y0 + row_index * cell_h + cell_h * 0.7),
                    text, fontname="china-s", fontsize=9,
                )


def _parse_pdf_path(tmp_path, build, name="table.pdf") -> str:
    doc = fitz.open()
    build(doc)
    out = tmp_path / name
    doc.save(out)
    doc.close()
    return str(out)


def _parse_pdf(tmp_path, build, name="table.pdf") -> DocumentIR:
    return PDFParser().parse(_parse_pdf_path(tmp_path, build, name))


def _tables_data(ir: DocumentIR):
    return [block.table_data for block in ir.tables()]


def test_plain_table_extracts_as_strings(tmp_path):
    def build(doc):
        page = doc.new_page()
        _draw_table(page, 72, 100, 120, 30, rows=3, cols=3)
        _fill_cells(page, 72, 100, 120, 30,
                    [["科目", "金额", "备注"], ["管理费", "2.4", "按规定"], ["合计", "30.0", ""]])

    ir = _parse_pdf(tmp_path, build)
    tables = _tables_data(ir)
    assert tables and tables[0]
    assert all(isinstance(cell, str) for row in tables[0] for cell in row)
    assert all("None" != cell for row in tables[0] for cell in row)


def test_horizontal_merged_cell_keeps_columns(tmp_path):
    """横向合并：extract 会给出 None；列位置必须保持，不得错位。"""
    def build(doc):
        page = doc.new_page()
        _draw_table(page, 72, 100, 120, 30, rows=3, cols=3,
                    skip_vlines={(1, 0)})
        _fill_cells(page, 72, 100, 120, 30,
                    [["科目", "金额", "备注"],
                     ["管理费合并两列", "", "按规定"],
                     ["合计", "30.0", ""]])

    ir = _parse_pdf(tmp_path, build, "merge_h.pdf")
    table = _tables_data(ir)[0]
    widths = {len(row) for row in table}
    assert len(widths) == 1, f"合并单元格导致列数不一致：{widths}"
    assert all(isinstance(cell, str) for row in table for cell in row)
    # 备注列仍位于第 3 列（未被合并内容挤走）
    assert any("按规定" in row[-1] for row in table)


def test_vertical_merged_cell_keeps_rows(tmp_path):
    def build(doc):
        page = doc.new_page()
        _draw_table(page, 72, 100, 120, 30, rows=3, cols=3,
                    skip_hlines={(1, 0)})
        _fill_cells(page, 72, 100, 120, 30,
                    [["类别", "明细", "金额"],
                     ["人工费", "研究生", "5.0"],
                     ["", "专家咨询", "3.0"]])

    ir = _parse_pdf(tmp_path, build, "merge_v.pdf")
    table = _tables_data(ir)[0]
    assert len(table) >= 3
    assert all(len(row) == 3 for row in table)
    assert any("专家咨询" in row[1] for row in table)


def test_empty_cells_normalized_to_empty_string(tmp_path):
    def build(doc):
        page = doc.new_page()
        _draw_table(page, 72, 100, 120, 30, rows=3, cols=2)
        _fill_cells(page, 72, 100, 120, 30,
                    [["科目", "金额"], ["材料费", ""], ["合计", "30.0"]])

    ir = _parse_pdf(tmp_path, build, "empty.pdf")
    table = _tables_data(ir)[0]
    flat = " ".join(" ".join(row) for row in table)
    assert "None" not in flat
    assert any(cell == "" for row in table for cell in row)


def test_two_tables_on_two_pages(tmp_path):
    def build(doc):
        for page_index in range(2):
            page = doc.new_page()
            _draw_table(page, 72, 100, 120, 30, rows=2, cols=2)
            _fill_cells(page, 72, 100, 120, 30,
                        [["项目", "金额"], [f"第{page_index + 1}页项", "1.0"]])

    ir = _parse_pdf(tmp_path, build, "twopage.pdf")
    tables = ir.tables()
    assert len(tables) == 2
    assert {table.page for table in tables} == {1, 2}
    assert all(table.table_data for table in tables)


def test_budget_table_with_merged_header_runs_rule_engine(tmp_path):
    """带合并单元格的预算表：规则引擎不得抛 ValidationError。"""
    def build(doc):
        page = doc.new_page()
        _insert_body_text(page)
        _draw_table(page, 72, 300, 120, 30, rows=4, cols=3,
                    skip_vlines={(0, 0)})
        _fill_cells(page, 72, 300, 120, 30,
                    [["经费预算表（合并表头）", "", ""],
                     ["科目", "金额", "备注"],
                     ["管理费", "2.4", "≤10%"],
                     ["合计", "30.0", ""]])

    ir = _parse_pdf(tmp_path, build, "budget_merged.pdf")
    engine = RuleEngine(
        "ruleset.yaml",
        ruleset_text=(
            "ruleset: test\nname: t\nrules:\n"
            "- id: T1\n  type: budget_check\n  severity: error\n"
            "  params:\n    max_ratio: {管理费: 0.1}\n    total_field: 合计|总计\n"
        ),
    )
    issues = engine.run(ir)  # 不应抛异常
    assert isinstance(issues, list)


def test_merged_table_document_completes_formal_review(tmp_path):
    """正式流程：带合并单元格预算表的 PDF 必须能走完审查（不得 ValidationError）。"""
    def build(doc):
        page = doc.new_page()
        _insert_body_text(page)
        _draw_table(page, 72, 300, 120, 30, rows=4, cols=3,
                    skip_vlines={(0, 0)})
        _fill_cells(page, 72, 300, 120, 30,
                    [["经费预算表（合并表头）", "", ""],
                     ["科目", "金额", "备注"],
                     ["管理费", "2.4", "≤10%"],
                     ["合计", "30.0", ""]])

    path = _parse_pdf_path(tmp_path, build, "budget_merged_flow.pdf")
    review = _run_review(path)
    assert review["status"] == "done", review
    assert "ValidationError" not in (review.get("error") or "")


def _insert_body_text(page):
    page.insert_textbox(
        fitz.Rect(40, 40, 555, 200),
        "本项目预算说明正文。" * 8, fontname="china-s", fontsize=11,
    )


# ── 3. 解析彻底失败：明确降级，不伪装成“没有表格” ─────────

def test_table_extract_failure_degrades_explicitly(tmp_path, monkeypatch):
    def build(doc):
        page = doc.new_page()
        _insert_body_text(page)
        _draw_table(page, 72, 300, 120, 30, rows=3, cols=2)
        _fill_cells(page, 72, 300, 120, 30,
                    [["科目", "金额"], ["管理费", "2.4"], ["合计", "30.0"]])

    def _boom(table, *args, **kwargs):
        raise RuntimeError("simulated extract failure")

    from app.services.parser import pdf_parser
    monkeypatch.setattr(pdf_parser, "_extract_table_rows", _boom)
    ir = _parse_pdf(tmp_path, build, "degraded.pdf")

    degraded = [block for block in ir.tables() if block.parse_warnings]
    assert degraded, "解析失败必须留下明确的降级信息"
    assert degraded[0].table_data is None
    assert ir.meta.parse_warnings, "文档级降级信息必须保留"
    # 该区域文字不得被吞掉（表格 bbox 未加入跳过集合 → 仍作为段落保留）
    assert any("管理费" in block.text for block in ir.paragraphs())
    # 预算检查必须转人工确认，而不是断言“未找到预算表”
    engine = RuleEngine(
        "ruleset.yaml",
        ruleset_text=(
            "ruleset: test\nname: t\nrules:\n"
            "- id: T1\n  type: budget_check\n  severity: error\n"
            "  params:\n    max_ratio: {管理费: 0.1}\n    total_field: 合计|总计\n"
        ),
    )
    issues = engine.run(ir)
    assert issues
    assert all(issue.confidence == "manual_required" for issue in issues)
    assert not any("未找到经费预算表" in issue.message for issue in issues)
