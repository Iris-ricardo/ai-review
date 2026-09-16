"""B-corp: 解析器对“纯数字碎片”不判标题（自检样本池发现的缺陷回归）。

参考文献换行产生的 “3640.” / “4525.” / “17815.” 类独立文本行会被 HEADING_PATTERNS 的 ^\\d+\\.
匹配成标题，导致 heading_numbering 连环误报（eval 旧样本 20/20 每份命中即源于此）。"""
from __future__ import annotations

from app.services.parser.pdf_parser import PDFParser


def test_pure_numeric_fragments_are_paragraphs():
    parser = PDFParser()
    cases = ["3640.", "4525.", "17815.", "1234-1241.", "2018: 3634-3640.", "9."]
    for text in cases:
        kind, _level = parser._classify_with_signals(text, [], False, 12.0, 12.0)
        assert kind == "paragraph", f"{text!r} 不应判为标题"


def test_real_numbered_headings_still_classified():
    parser = PDFParser()
    assert parser._classify_with_signals("一、立项依据与研究背景", [], False, 12.0, 12.0)[0] == "heading"
    assert parser._classify_with_signals("2.1 研究目标", [], False, 12.0, 12.0)[0] == "heading"
    assert parser._classify_with_signals("（一）研究目标", [], False, 12.0, 12.0)[0] == "heading"
