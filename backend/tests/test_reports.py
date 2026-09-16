from __future__ import annotations

from pathlib import Path
import sys

import fitz

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings
from app.services.parser import get_parser
from app.services.parser.converter import convert_docx_to_pdf
from app.services.report.annotator import generate_annotated_pdf
from app.services.report.reporter import generate_review_report, verify_report_chinese
from app.services.rules.engine import RuleEngine


def test_annotator_sabotage_annotation_count_matches_bbox_issues(
    tmp_path: Path, monkeypatch,
):
    from eval.make_sabotage import make_sabotaged

    monkeypatch.setenv("LLM_API_KEY", "")
    get_settings.cache_clear()
    sabotaged_docx = make_sabotaged(tmp_path / "sabotaged_budget.docx")
    source_pdf = convert_docx_to_pdf(sabotaged_docx)
    ir = get_parser(str(source_pdf)).parse(str(source_pdf))
    ruleset = Path(__file__).resolve().parents[2] / "rules" / "campus.yaml"
    issues = RuleEngine(ruleset).run(ir)

    block_map = {block.id: block for block in ir.blocks}
    for issue in issues:
        block = block_map.get(issue.block_id)
        if block is not None:
            issue.page = issue.page or block.page
            issue.bbox = issue.bbox or [
                block.bbox.x0, block.bbox.y0, block.bbox.x1, block.bbox.y1,
            ]

    output = tmp_path / "annotated.pdf"
    generate_annotated_pdf(source_pdf, [issue.model_dump() for issue in issues], output)

    expected = sum(1 for issue in issues if issue.page is not None and issue.bbox)
    doc = fitz.open(str(output))
    try:
        actual = sum(1 for page in doc for _ in (page.annots() or []))
    finally:
        doc.close()
    assert expected > 0
    assert actual == expected

    first_mtime = output.stat().st_mtime_ns
    generate_annotated_pdf(source_pdf, [], output)
    assert output.stat().st_mtime_ns > first_mtime
    regenerated = fitz.open(str(output))
    try:
        assert sum(1 for page in regenerated for _ in (page.annots() or [])) == 0
    finally:
        regenerated.close()


def test_reporter_first_page_chinese_self_check(tmp_path: Path):
    review = {
        "created_at": "2026-07-13 10:30:00",
        "issues": [{
            "rule_id": "C007",
            "checker": "budget_check",
            "severity": "error",
            "page": 9,
            "message": "预算勾稽不平",
            "suggestion": "请检查预算合计",
            "confidence": "deterministic",
            "layer": "rule",
        }],
    }
    output = tmp_path / "report.pdf"

    generate_review_report(review, "测试申报书.docx", "校级课题申报书", output)
    first_page_text = verify_report_chinese(output)

    assert "AI 项目申报书智能形式审查系统" in first_page_text
    assert "测试申报书" in first_page_text
    assert "需要修改" in first_page_text


def test_annotator_summary_uses_compact_builtin_chinese_font(tmp_path: Path):
    source = tmp_path / "source.pdf"
    source_doc = fitz.open()
    source_doc.new_page()
    source_doc.save(source)
    source_doc.close()
    output = tmp_path / "annotated.pdf"
    generate_annotated_pdf(
        source,
        [{
            "rule_id": "C010",
            "severity": "info",
            "page": None,
            "bbox": None,
            "message": "附件真实性需要人工确认",
            "suggestion": "请核对原始附件",
        }],
        output,
    )
    doc = fitz.open(output)
    try:
        summary_text = doc[0].get_text()
    finally:
        doc.close()
    assert "审查摘要" in summary_text
    assert "附件真实性需要人工确认" in summary_text
    assert output.stat().st_size < 1_000_000
