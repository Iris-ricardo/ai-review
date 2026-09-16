"""Parameterized unit tests for PDF heading detection (three-signal algorithm).

Signal (a): numbering regex patterns → heading with correct level. Signals (b)+(c): bold + larger than body + short →
heading level 2. Non-heading: long text, no numbering, not bold, body-size font → paragraph."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.parser.pdf_parser import PDFParser
from app.schemas.ir import FontInfo


# ── Helpers ──────────────────────────────────────────────
def _fonts(size: float = 12.0, bold: bool = False) -> list:
    return [FontInfo(name="宋体", size=size, bold=bold)]


# ── Test cases for Signal (a): numbering regex ───────────
# (text, expected_type, expected_level)
REGEX_CASES = [
    # Chinese numbered headings → level 1
    ("一、研究背景与研究意义", "heading", 1),
    ("二、研究目标", "heading", 1),
    ("三. 研究方法", "heading", 1),
    ("十、结论", "heading", 1),
    ("十二、附录", "heading", 1),

    # Chinese parenthesized → level 2
    ("（一）理论基础", "heading", 2),
    ("（三）实验设计", "heading", 2),

    # Numeric sub-section "1.1 xxx" → level 3
    ("1.1 研究背景", "heading", 3),
    ("2.3 实验方法", "heading", 3),
    ("3.1.2 数据分析", "heading", 3),

    # Numeric "1. xxx" or "1、xxx" → level 2
    ("1. 研究内容", "heading", 2),
    ("3、技术路线", "heading", 2),

    # Parenthesized "(1) xxx" → level 2
    ("(1) 数据采集", "heading", 2),
    ("(5) 结果分析", "heading", 2),
]

# ── Test cases for Signals (b)+(c) fallback ──────────────
BOLD_SIZE_CASES = [
    # (text, size, bold, body_main_size, expected_type, expected_level)
    # Bold + larger than body + short → heading level 2
    ("研究背景", 16.0, True, 12.0, "heading", 2),
    ("关键技术", 14.0, True, 10.5, "heading", 2),
    # Bold but NOT larger than body → paragraph
    ("研究背景", 10.0, True, 12.0, "paragraph", 0),
    # Larger but NOT bold → paragraph
    ("研究背景", 16.0, False, 12.0, "paragraph", 0),
    # Bold + larger but TOO LONG → paragraph
    ("这是一个非常长的文本段落内容超过了三十个字符限制所以不会被认为是标题",
     16.0, True, 12.0, "paragraph", 0),
]

# ── Test cases for plain paragraphs ──────────────────────
PARAGRAPH_CASES = [
    "城市交通拥堵问题日益严峻，准确预测交通流量是智能交通系统的核心环节。",
    "近年来，图卷积网络（GCN）和Transformer架构在处理时空数据方面取得了显著进展。",
    "本课题旨在构建一种新型的交通流预测模型。",
]


class TestHeadingDetectionRegex:
    """Test Signal (a): numbering regex matching."""

    @pytest.mark.parametrize("text,expected_type,expected_level", REGEX_CASES)
    def test_regex_heading(self, text, expected_type, expected_level):
        block_type, level = PDFParser._classify_with_signals(
            text=text,
            fonts=_fonts(size=12.0, bold=False),  # No bold needed when regex matches
            has_bold=False,
            avg_size=12.0,
            body_main_size=12.0,
        )
        assert block_type == expected_type, f"'{text}' → expected {expected_type}, got {block_type}"
        assert level == expected_level, f"'{text}' → expected level {expected_level}, got {level}"


class TestHeadingDetectionBoldSize:
    """Test Signals (b)+(c): bold + larger-than-body + short."""

    @pytest.mark.parametrize(
        "text,size,bold,body_main,expected_type,expected_level",
        BOLD_SIZE_CASES,
    )
    def test_bold_size_fallback(
        self, text, size, bold, body_main, expected_type, expected_level
    ):
        block_type, level = PDFParser._classify_with_signals(
            text=text,
            fonts=_fonts(size=size, bold=bold),
            has_bold=bold,
            avg_size=size,
            body_main_size=body_main,
        )
        assert block_type == expected_type, f"'{text[:20]}...' → expected {expected_type}, got {block_type}"
        assert level == expected_level, f"'{text[:20]}...' → expected level {expected_level}, got {level}"


class TestHeadingDetectionParagraphs:
    """Test that normal paragraphs are NOT classified as headings."""

    @pytest.mark.parametrize("text", PARAGRAPH_CASES)
    def test_normal_paragraph(self, text):
        block_type, level = PDFParser._classify_with_signals(
            text=text,
            fonts=_fonts(size=12.0, bold=False),
            has_bold=False,
            avg_size=12.0,
            body_main_size=12.0,
        )
        assert block_type == "paragraph", f"Long text should be paragraph, got {block_type}"
        assert level == 0

    def test_paragraph_with_bold_but_body_size(self):
        """Bold text at body size should NOT be a heading."""
        block_type, level = PDFParser._classify_with_signals(
            text="研究背景",  # Short but at body size
            fonts=_fonts(size=12.0, bold=True),
            has_bold=True,
            avg_size=12.0,
            body_main_size=12.0,
        )
        assert block_type == "paragraph"


class TestBodyMainSize:
    """Test the body main font size computation (mode)."""

    def test_mode_simple(self):
        sizes = [12.0, 12.0, 12.0, 14.0, 16.0]
        result = PDFParser._compute_body_main_size(sizes)
        assert result == 12.0

    def test_mode_rounding(self):
        """Slightly different sizes should be rounded to same bucket."""
        sizes = [12.0, 12.1, 11.9, 12.2, 14.0]
        result = PDFParser._compute_body_main_size(sizes)
        assert result == 12.0  # 12.0/12.1/11.9/12.2 all round to 12.0

    def test_mode_empty(self):
        assert PDFParser._compute_body_main_size([]) == 12.0

    def test_mode_single_value(self):
        assert PDFParser._compute_body_main_size([10.5]) == 10.5

    def test_pdf_line_layout_signals(self):
        from app.schemas.ir import BBox

        layout = PDFParser._layout_from_pdf_lines(
            [(72, 100, 500, 112), (96, 118, 500, 130), (72, 136, 500, 148)],
            BBox(x0=72, y0=100, x1=500, y1=148),
            595.28,
        )
        assert layout.line_count == 3
        assert layout.line_spacing_pt == 18
        assert layout.first_line_indent_pt == 0
