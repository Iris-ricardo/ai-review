"""
Unit tests for DOCX parser.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.parser.docx_parser import DocxParser


class TestDocxParser:
    """Test the DOCX → IR parser."""

    def test_parse_sample_proposal(self, sample_docx_path: Path):
        """Parse the sample proposal DOCX and verify IR structure — DocxParser is a
        text-level fallback (pages None, empty page_meta); use converter + PDFParser
        for page-accurate IR."""
        parser = DocxParser()
        ir = parser.parse(str(sample_docx_path))

        # Basic sanity
        assert ir.meta.pages == 0  # No page estimation
        assert ir.meta.word_count > 100
        assert len(ir.blocks) > 0

        # Should have headings
        headings = ir.headings()
        assert len(headings) > 0, "Should have extracted at least some headings"

        # Should have paragraphs
        paragraphs = ir.paragraphs()
        assert len(paragraphs) > 0

        # Every block should have an id; page is None (no page estimation)
        for b in ir.blocks:
            assert b.id, f"Block missing id: {b}"
            assert b.page is None, f"Block {b.id} page should be None, got {b.page}"
            assert b.type in ("heading", "paragraph", "table", "image", "header", "footer", "list"), \
                f"Block {b.id} has invalid type: {b.type}"

        # Meta page_meta is empty (docx_parser doesn't estimate pages)
        assert ir.meta.page_meta == []

        # Check word count is non-zero
        assert ir.meta.word_count > 0

    def test_parse_empty_path(self):
        parser = DocxParser()
        with pytest.raises(FileNotFoundError):
            parser.parse("/nonexistent/file.docx")

    def test_ir_find_headings(self, sample_docx_path: Path):
        parser = DocxParser()
        ir = parser.parse(str(sample_docx_path))

        # Check that key sections can be found
        found = ir.find_text("立项依据", fuzzy=True)
        assert len(found) >= 1, "Should find '立项依据' somewhere"

        found = ir.find_text("经费预算", fuzzy=True)
        assert len(found) >= 1, "Should find '经费预算' somewhere"

        found = ir.find_text("参考文献", fuzzy=True)
        assert len(found) >= 1, "Should find '参考文献' somewhere"

    def test_ir_blocks_have_font_info(self, sample_docx_path: Path):
        parser = DocxParser()
        ir = parser.parse(str(sample_docx_path))
        for b in ir.blocks[:10]:  # sample first 10
            assert b.font.name, f"Block {b.id} missing font name"
            assert b.font.size > 0, f"Block {b.id} missing font size"

    def test_paragraph_layout_is_preserved(self, sample_docx_path: Path):
        ir = DocxParser().parse(str(sample_docx_path))
        paragraph = ir.paragraphs()[0]
        assert paragraph.layout.line_count >= 1
        assert paragraph.layout.alignment in {"", "left", "center", "right", "justify"}
