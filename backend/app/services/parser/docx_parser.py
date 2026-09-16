"""DOCX parser using python-docx — **downgraded to eval / text-level fallback** (kept for eval /
error-injection, the no-LibreOffice text fallback, and conformance checks needing no page info).
Production reviews are PDF only (DOCX → converter.py → PDFParser) for accurate page numbers/bboxes;
blocks here have **page=None** + estimated bboxes — do NOT rely on them for annotation."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List

from docx import Document as DocxDocument
from docx.oxml.ns import qn

from app.schemas.ir import BBox, Block, DocMeta, DocumentIR, FontInfo, LayoutInfo
from .base import BaseParser

logger = logging.getLogger(__name__)

# Approximate page dimensions for A4 in points (used only for rough bbox estimates)
A4_WIDTH_PT = 595.0
A4_HEIGHT_PT = 842.0


class DocxParser(BaseParser):
    """Parse a DOCX file into DocumentIR using python-docx; all blocks have page=None
    (no page estimation). Use convert_docx_to_pdf() + PDFParser for page-accurate results.
    """

    def parse(self, file_path: str | Path) -> DocumentIR:
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"DOCX not found: {file_path}")

        doc = DocxDocument(str(file_path))
        blocks: list[Block] = []
        all_fonts: dict[str, FontInfo] = {}
        block_idx = 0
        total_word_count = 0

        body = doc.element.body
        paragraphs = doc.paragraphs
        para_idx = 0

        for child in body:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag

            if tag == "p":  # paragraph
                if para_idx >= len(paragraphs):
                    para_idx += 1
                    continue
                para = paragraphs[para_idx]
                para_idx += 1

                text = para.text or ""
                if not text.strip():
                    continue

                font_info = self._extract_font_info(para)
                if font_info.name not in all_fonts:
                    all_fonts[font_info.name] = font_info

                block_type, level = self._classify_paragraph(para, font_info)

                b_id = f"b{block_idx}"
                block_idx += 1

                blocks.append(Block(
                    id=b_id,
                    page=None,  # No page estimation — use PDF pipeline for pages
                    type=block_type,
                    level=level,
                    text=text,
                    bbox=BBox(x0=0, y0=0, x1=A4_WIDTH_PT, y1=20),
                    font=font_info,
                    layout=self._extract_layout_info(para),
                ))
                total_word_count += len(text.replace("\n", "").replace(" ", ""))

            elif tag == "tbl":  # table
                table_data = self._extract_table(child)
                if table_data:
                    b_id = f"b{block_idx}"
                    block_idx += 1
                    flat_text = " | ".join(" ".join(row) for row in table_data)
                    blocks.append(Block(
                        id=b_id,
                        page=None,  # No page estimation
                        type="table",
                        table_data=table_data,
                        text=flat_text,
                        bbox=BBox(x0=0, y0=0, x1=A4_WIDTH_PT, y1=20),
                    ))
                    total_word_count += len(flat_text)

        return DocumentIR(
            meta=DocMeta(
                filename=file_path.name,
                pages=0,  # Unknown — use PDF pipeline for page count
                word_count=total_word_count,
                fonts=list(all_fonts.values()),
                page_meta=[],
            ),
            blocks=blocks,
        )

    # ── Private helpers ──────────────────────────────────

    @staticmethod
    def _extract_font_info(para) -> FontInfo:
        """Extract font information from a paragraph."""
        font_name = "宋体"
        font_size = 12.0
        bold = False
        italic = False

        if para.style and para.style.font:
            sf = para.style.font
            if sf.name:
                font_name = sf.name
            if sf.size:
                font_size = sf.size.pt
            bold = sf.bold or False
            italic = sf.italic or False

        for run in para.runs:
            rf = run.font
            if rf.name:
                font_name = rf.name
            if rf.size:
                font_size = rf.size.pt
            if rf.bold:
                bold = True
            if rf.italic:
                italic = True

        return FontInfo(name=font_name, size=round(font_size, 1), bold=bold, italic=italic)

    @staticmethod
    def _extract_layout_info(para) -> LayoutInfo:
        """Extract exact paragraph layout values when DOCX is parsed directly."""
        paragraph_format = para.paragraph_format
        style_format = para.style.paragraph_format if para.style else None

        def effective(name):
            value = getattr(paragraph_format, name, None)
            if value is None and style_format is not None:
                value = getattr(style_format, name, None)
            return value

        def points(value):
            if value is None:
                return None
            pt = getattr(value, "pt", None)
            return round(float(pt), 2) if pt is not None else None

        alignment_value = para.alignment
        if alignment_value is None and style_format is not None:
            alignment_value = style_format.alignment
        alignment_map = {0: "left", 1: "center", 2: "right", 3: "justify"}

        line_spacing = effective("line_spacing")
        line_spacing_pt = points(line_spacing)
        line_spacing_multiple = None
        if line_spacing is not None and line_spacing_pt is None:
            try:
                line_spacing_multiple = round(float(line_spacing), 2)
            except (TypeError, ValueError):
                pass

        return LayoutInfo(
            alignment=alignment_map.get(int(alignment_value), "")
            if alignment_value is not None else "",
            line_count=max(1, len(para.text.splitlines())),
            line_spacing_pt=line_spacing_pt,
            line_spacing_multiple=line_spacing_multiple,
            first_line_indent_pt=points(effective("first_line_indent")),
            left_indent_pt=points(effective("left_indent")),
            right_indent_pt=points(effective("right_indent")),
            space_before_pt=points(effective("space_before")),
            space_after_pt=points(effective("space_after")),
        )

    @staticmethod
    def _classify_paragraph(para, font_info: FontInfo) -> tuple[str, int]:
        """Classify paragraph type and heading level."""
        style_name = (para.style.name if para.style else "Normal").lower()

        if "heading" in style_name or "标题" in style_name:
            level = 1
            for i in range(1, 7):
                if str(i) in style_name:
                    level = i
                    break
            return "heading", level

        text = para.text.strip() if para.text else ""
        if len(text) <= 60 and font_info.size >= 14 and not text.endswith("。"):
            if text.startswith(("一、", "二、", "三、", "四、", "五、", "六、", "七、", "八、", "九、", "十、")):
                return "heading", 1
            import re
            if re.match(r"^\d+\.\d+\.?\s", text):
                return "heading", 3
            if re.match(r"^\d+\.\s", text) or re.match(r"^\(\d+\)", text):
                return "heading", 2
            return "heading", 2

        return "paragraph", 0

    @staticmethod
    def _extract_table(table_element) -> List[List[str]] | None:
        """Extract table data from XML element."""
        rows = table_element.findall(qn("w:tr"))
        if not rows:
            return None

        data: List[List[str]] = []
        for row in rows:
            cells = row.findall(qn("w:tc"))
            row_data: List[str] = []
            for cell in cells:
                paras = cell.findall(qn("w:p"))
                cell_text = ""
                for p in paras:
                    texts = [t.text or "" for t in p.findall(".//" + qn("w:t"))]
                    cell_text += "".join(texts)
                row_data.append(cell_text.strip())
            data.append(row_data)
        return data if data else None
