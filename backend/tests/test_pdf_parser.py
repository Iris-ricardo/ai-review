"""Integration tests for PDF parser.

Two paths, in order of preference: (1) LibreOffice conversion of sample_proposal.docx → PDF → IR; (2) fallback
reportlab-generated test_document.pdf → IR (no LO). Both assert pages≥8, headings, budget table, valid bboxes, signature page."""
import sys
import shutil
from pathlib import Path

import fitz
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.parser import pdf_parser
from app.services.parser.pdf_parser import PDFParser

# ── Paths ────────────────────────────────────────────────
EVAL_DIR = Path(__file__).resolve().parent.parent.parent / "eval"
SAMPLE_DOCX = EVAL_DIR / "samples" / "sample_proposal.docx"
TEST_PDF = EVAL_DIR / "samples" / "test_document.pdf"


def test_pymupdf_span_flags_map_serif_without_false_bold():
    serif_flag = getattr(fitz, "TEXT_FONT_SERIFED", 4)
    font = pdf_parser._font_info_from_span({
        "font": "SimSun",
        "size": 12,
        "flags": serif_flag,
        "color": 0,
    })
    unnamed_serif = pdf_parser._font_info_from_span({
        "font": "Unknown",
        "size": 12,
        "flags": serif_flag,
        "color": 0,
    })

    assert font.name == "SimSun"
    assert font.bold is False
    assert font.italic is False
    assert unnamed_serif.name == "宋体"


def test_pymupdf_span_flags_map_bold_italic_and_monospace():
    flags = (
        getattr(fitz, "TEXT_FONT_BOLD", 16)
        | getattr(fitz, "TEXT_FONT_ITALIC", 2)
        | getattr(fitz, "TEXT_FONT_MONOSPACED", 8)
    )
    font = pdf_parser._font_info_from_span({
        "font": "",
        "size": 11,
        "flags": flags,
        "color": 0x112233,
    })

    assert font.name == "等线"
    assert font.bold is True
    assert font.italic is True
    assert font.color == 0x112233


def _lo_available() -> bool:
    from app.services.parser.converter import _find_libreoffice
    return _find_libreoffice() is not None


def _resolve_pdf_path(work_dir: Path) -> Path:
    """Get a PDF path: LO-converted DOCX if possible, else reportlab test PDF."""
    if _lo_available() and SAMPLE_DOCX.exists():
        from app.services.parser.converter import convert_docx_to_pdf
        try:
            copied_docx = work_dir / SAMPLE_DOCX.name
            shutil.copy2(SAMPLE_DOCX, copied_docx)
            return convert_docx_to_pdf(str(copied_docx))
        except Exception as e:
            # LO present but broken (missing DLLs etc.) — fall back
            import warnings
            warnings.warn(f"LibreOffice conversion failed, using fallback PDF: {e}")
    if TEST_PDF.exists():
        return TEST_PDF
    pytest.skip("No PDF source available (no LibreOffice and no test_document.pdf)")


# ── Tests ─────────────────────────────────────────────────
class TestPDFParser:
    """Parse a PDF and verify IR quality regardless of PDF source."""

    @pytest.fixture(scope="class")
    def ir(self, tmp_path_factory):
        pdf_path = _resolve_pdf_path(tmp_path_factory.mktemp("pdf-parser"))
        parser = PDFParser()
        return parser.parse(str(pdf_path))

    def test_pages_at_least_8(self, ir):
        assert ir.meta.pages >= 8, f"Expected ≥8 pages, got {ir.meta.pages}"

    def test_all_level1_headings_found(self, ir):
        h1_texts = [b.text for b in ir.headings(level=1)]
        h1_joined = " | ".join(h1_texts)
        required = [
            ("立项依据", "立项依据/研究背景"),
            ("研究目标", "研究目标与内容"),
            ("研究方法", "研究方法与技术路线"),
            ("研究基础", "研究基础/工作条件"),
            ("经费预算", "经费预算"),
            ("参考文献", "参考文献"),
        ]
        for keyword, label in required:
            found = any(keyword in t for t in h1_texts)
            assert found, f"Missing level-1 heading '{label}' in: {h1_joined}"

    def test_budget_table_identified(self, ir):
        """Budget must be a structured 'table' block with non-empty table_data.

        PyMuPDF page.find_tables() yields row×col arrays for M2 budget_check arithmetic."""
        tables = ir.tables()
        assert len(tables) >= 1, f"Expected ≥1 tables, got {len(tables)}"

        budget_table = None
        for t in tables:
            if t.table_data:
                # Look for budget-related keywords in header row or first column
                for row in t.table_data:
                    if any(kw in str(row) for kw in ["经费", "预算", "合计", "科目"]):
                        budget_table = t
                        break
            if budget_table:
                break

        assert budget_table is not None, (
            f"Budget table not found among {len(tables)} table(s). "
            f"Table headers: {[t.table_data[0] if t.table_data else 'N/A' for t in tables]}"
        )
        assert budget_table.table_data is not None
        assert len(budget_table.table_data) >= 2, (
            f"Budget table has only {len(budget_table.table_data)} row(s)"
        )
        # Verify at least one numeric cell exists (for M2 arithmetic)
        has_numeric = any(
            any(_looks_numeric(c) for c in row)
            for row in budget_table.table_data
        )
        assert has_numeric, "Budget table should contain numeric amounts"


    def test_all_blocks_have_valid_page_and_bbox(self, ir):
        for b in ir.blocks:
            assert b.page is not None, f"Block {b.id} has None page"
            assert b.page >= 1, f"Block {b.id} has invalid page: {b.page}"
            assert b.page <= ir.meta.pages, \
                f"Block {b.id} page {b.page} > total pages {ir.meta.pages}"
            assert b.bbox.x0 <= b.bbox.x1, \
                f"Block {b.id} bbox x0({b.bbox.x0}) > x1({b.bbox.x1})"
            assert b.bbox.y0 <= b.bbox.y1, \
                f"Block {b.id} bbox y0({b.bbox.y0}) > y1({b.bbox.y1})"

    def test_signature_page_exists(self, ir):
        last_page = ir.meta.pages
        last_page_blocks = ir.blocks_on_page(last_page)
        last_page_text = " ".join(b.text for b in last_page_blocks)
        sig_keywords = ["签字", "盖章"]
        found_any = any(kw in last_page_text for kw in sig_keywords)
        assert found_any, \
            f"Last page ({last_page}) missing signature keywords. Text: {last_page_text[:200]}"

    def test_first_page_text_no_garbled(self, ir):
        """Ensure Chinese text on page 1 is readable (no mojibake)."""
        page1_text = " ".join(b.text for b in ir.blocks_on_page(1))
        # Should contain readable Chinese characters
        chinese_chars = [c for c in page1_text if '一' <= c <= '鿿']
        assert len(chinese_chars) >= 5, \
            f"Page 1 has only {len(chinese_chars)} Chinese chars — possible encoding issue"
        # Should NOT contain common mojibake patterns
        bad = ["夃", "쐀", "鐢", "Garbled"]
        for pattern in bad:
            assert pattern not in page1_text, \
                f"Page 1 contains garbled text pattern: {pattern}"


def _looks_numeric(val: str) -> bool:
    try:
        float(str(val).replace(",", "").replace("，", ""))
        return True
    except (ValueError, TypeError):
        return False
