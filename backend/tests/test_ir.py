"""
Unit tests for DocumentIR schema and convenience methods.
"""
import pytest
from app.schemas.ir import BBox, Block, DocMeta, DocumentIR, FontInfo, PageMeta


class TestDocumentIR:
    """Test the DocumentIR data structure and helpers."""

    def _make_ir(self) -> DocumentIR:
        """Build a minimal DocumentIR with known blocks."""
        blocks = [
            Block(id="b0", page=1, type="heading", level=1,
                  text="一、立项依据", bbox=BBox(x0=0, y0=0, x1=100, y1=20)),
            Block(id="b1", page=1, type="paragraph", text="这是立项依据的内容。",
                  bbox=BBox(x0=0, y0=30, x1=100, y1=50)),
            Block(id="b2", page=1, type="heading", level=2,
                  text="1.1 研究背景", bbox=BBox(x0=0, y0=60, x1=100, y1=80)),
            Block(id="b3", page=1, type="paragraph", text="研究背景正文。",
                  bbox=BBox(x0=0, y0=90, x1=100, y1=110)),
            Block(id="b4", page=2, type="heading", level=1,
                  text="二、研究内容", bbox=BBox(x0=0, y0=0, x1=100, y1=20)),
            Block(id="b5", page=2, type="table", text="预算表内容",
                  table_data=[["科目", "金额"], ["设备费", "10"]]),
            Block(id="b6", page=2, type="paragraph", text="包含图1和图2的引用。"),
        ]
        meta = DocMeta(
            filename="test.pdf", pages=2, word_count=50,
            page_meta=[
                PageMeta(page_number=1, width=595, height=842, block_ids=["b0","b1","b2","b3"]),
                PageMeta(page_number=2, width=595, height=842, block_ids=["b4","b5","b6"]),
            ]
        )
        return DocumentIR(meta=meta, blocks=blocks)

    def test_blocks_on_page(self):
        ir = self._make_ir()
        assert len(ir.blocks_on_page(1)) == 4
        assert len(ir.blocks_on_page(2)) == 3
        assert len(ir.blocks_on_page(99)) == 0

    def test_blocks_by_type(self):
        ir = self._make_ir()
        assert len(ir.blocks_by_type("heading")) == 3
        assert len(ir.blocks_by_type("paragraph")) == 3
        assert len(ir.blocks_by_type("table")) == 1
        assert len(ir.blocks_by_type("image")) == 0

    def test_headings(self):
        ir = self._make_ir()
        assert len(ir.headings()) == 3
        assert len(ir.headings(level=1)) == 2
        assert len(ir.headings(level=2)) == 1

    def test_tables(self):
        ir = self._make_ir()
        assert len(ir.tables()) == 1
        assert ir.tables()[0].table_data is not None

    def test_find_text_exact(self):
        ir = self._make_ir()
        results = ir.find_text("立项依据")
        assert len(results) >= 1
        assert results[0].id == "b0"

    def test_find_text_fuzzy(self):
        ir = self._make_ir()
        results = ir.find_text("图 1", fuzzy=True)
        assert len(results) >= 1

    def test_find_text_no_match(self):
        ir = self._make_ir()
        results = ir.find_text("不存在的内容XYZ123")
        assert len(results) == 0

    def test_total_pages(self):
        ir = self._make_ir()
        assert ir.total_pages == 2

    def test_word_count_property(self):
        b = Block(id="x", page=1, type="paragraph", text="你好世界 hello world")
        # Chinese chars counted; spaces stripped
        assert b.word_count == len("你好世界helloworld")
