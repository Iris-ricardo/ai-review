"""PDF parser using PyMuPDF (fitz): text blocks with positional info → unified DocumentIR.
Heading detection uses three signals in priority order: (a) line-start numbering regex
(HEADING_PATTERNS) → heading with the regex level; else (b) bold AND (c) font size above the
body main size (mode of span sizes) AND line < 30 chars → level-2 heading; else paragraph."""
from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter
from pathlib import Path
from statistics import median
from typing import List, Optional, Tuple

import fitz  # PyMuPDF

from app.core.config import get_settings
from app.schemas.ir import (
    BBox,
    Block,
    DocMeta,
    DocumentIR,
    FontInfo,
    IR_SCHEMA_VERSION,
    LayoutInfo,
    PageMeta,
)
from .base import BaseParser
from . import parser_fingerprint
from .page_quality import (
    OCR_DISABLED,
    OCR_FAILED,
    OCR_INSUFFICIENT,
    OCR_NOT_ATTEMPTED,
    OCR_SKIPPED_LIMIT,
    OCR_SUCCESS,
    PageSignals,
    classify_page,
)

logger = logging.getLogger(__name__)

# ── 文本/图像提取 flags（B4/C7） ──────────────────────────
# 缺 TEXT_PRESERVE_IMAGES 时 get_text("dict") 不返回 image 块（图片检测恒空、图像触发式 OCR 成死代码、扫描页无法识别），故两个 flag 必须同时保留。
TEXT_FLAGS = fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_PRESERVE_IMAGES

# ── PyMuPDF font flags ───────────────────────────────────
# 优先用库常量；数值回退值与 PyMuPDF 文档的 span flag 位一致，兼容旧版本。
FONT_FLAG_ITALIC = getattr(fitz, "TEXT_FONT_ITALIC", 2)
FONT_FLAG_SERIF = getattr(fitz, "TEXT_FONT_SERIFED", 4)
FONT_FLAG_MONO = getattr(fitz, "TEXT_FONT_MONOSPACED", 8)
FONT_FLAG_BOLD = getattr(fitz, "TEXT_FONT_BOLD", 16)

# ── Heading numbering patterns ── (priority order) ───────
# Each tuple: (compiled_regex, level)
HEADING_PATTERNS: List[Tuple[re.Pattern, int]] = [
    # "一、..." or "一." → level 1
    (re.compile(r"^[一二三四五六七八九十]+[、.]"), 1),
    # "（一）..." → level 2
    (re.compile(r"^（[一二三四五六七八九十]+）"), 2),
    # "1.1 xxx" or "2.3. xxx" → level 3 (subsection)
    # Must have at least one dot-separated sub-number; trailing space optional
    (re.compile(r"^\d+\.\d+(?:\.\d+)*"), 3),
    # "1. xxx" or "1、xxx" → level 2
    # Single number with . or 、 separator; must NOT be followed by digit (not "1.5")
    (re.compile(r"^\d+[\.、](?!\d)"), 2),
    # "(1) xxx" → level 2
    (re.compile(r"^\(\d+\)"), 2),
]

# Maximum character length for a line to be considered a heading
# (when using bold+size fallback, not regex)
HEADING_MAX_LENGTH = 30


def _overlaps_any(bbox: BBox, table_bboxes: List[BBox], margin: float = 2.0) -> bool:
    """Return True if bbox overlaps any table bbox (with a small margin)."""
    for tb in table_bboxes:
        if (
            bbox.x1 > tb.x0 - margin
            and bbox.x0 < tb.x1 + margin
            and bbox.y1 > tb.y0 - margin
            and bbox.y0 < tb.y1 + margin
        ):
            return True
    return False


def _bbox_intersects(first: BBox, second: BBox) -> bool:
    return (
        first.x0 < second.x1
        and second.x0 < first.x1
        and first.y0 < second.y1
        and second.y0 < first.y1
    )


def _image_area_ratio(image_boxes: List[BBox], width: float, height: float) -> float:
    """图像块面积之和 / 页面面积（封顶 1.0）。"""
    page_area = max(1.0, float(width) * float(height))
    total = 0.0
    for box in image_boxes:
        total += max(0.0, box.x1 - box.x0) * max(0.0, box.y1 - box.y0)
    return min(1.0, total / page_area)


def _has_unread_image_region(
    image_boxes: List[BBox],
    text_boxes: List[BBox],
    width: float,
    height: float,
    *,
    region_ratio: float = 0.5,
) -> bool:
    """是否存在“大块图像区域且区域内没有任何数字文字”；区域内文字重叠即视为已有可读内容，
    小图标（面积不足阈值）忽略。
    """
    page_area = max(1.0, float(width) * float(height))
    for image in image_boxes:
        area = max(0.0, image.x1 - image.x0) * max(0.0, image.y1 - image.y0)
        if area / page_area < region_ratio:
            continue
        if not any(_bbox_intersects(image, text) for text in text_boxes):
            return True
    return False


def _ocr_page_blocks(page, config) -> tuple[str, List[dict], int]:
    """对单页执行 OCR，返回 (state, ocr_text_blocks, chars)，state ∈ page_quality 的
    OCR_SUCCESS / OCR_INSUFFICIENT / OCR_FAILED。“OCR 没报错”不等于“读出了正文”，字数不足仍按
    未读取处理（R02）；本函数是 OCR 与解析器唯一的接缝，便于免 tesseract 验证三条分支。
    """
    try:
        textpage = page.get_textpage_ocr(
            language=config.OCR_LANGUAGE,
            dpi=max(72, config.OCR_DPI),
            full=True,
            tessdata=config.TESSDATA_PREFIX or None,
        )
        ocr_blocks = page.get_text(
            "dict",
            flags=TEXT_FLAGS,
            textpage=textpage,
        )["blocks"]
        chars = len("".join(
            span.get("text", "")
            for block in ocr_blocks if block.get("type") == 0
            for line in block.get("lines", [])
            for span in line.get("spans", [])
        ).strip())
        if chars >= max(1, config.OCR_MIN_PAGE_CHARS):
            return OCR_SUCCESS, ocr_blocks, chars
        return OCR_INSUFFICIENT, ocr_blocks, chars
    except Exception as exc:  # noqa: BLE001 —— 缺运行时/语言包不能让整篇解析失败
        logger.warning("OCR unavailable; using digital text only: %s", exc)
        return OCR_FAILED, [], 0


def _extract_table_rows(table):
    """对单个表格执行结构提取（唯一接缝，便于测试注入失败，见 R04）。"""
    return table.extract()


def _normalize_table_rows(rows) -> tuple[List[List[str]], List[str]]:
    """把 PyMuPDF ``table.extract()`` 的二维数组归一化为字符串矩阵（R04）：``None`` 单元格
    （合并空位/空单元格）→ ``""`` 保持列位置、避免列错位（不写字面 ``"None"``）；不足行按最宽行
    补 ``""``；``None`` 行按空行处理；第二项为降级/说明信息（合并单元格占位属正常语义，不告警）。
    """
    warnings: List[str] = []
    if rows is None:
        return [], ["表格未提取到任何行"]
    width = 0
    for row in rows:
        if row is None:
            continue
        width = max(width, len(list(row)))
    if width == 0:
        return [], ["表格未提取到任何单元格"]
    normalized: List[List[str]] = []
    ragged_rows = 0
    none_rows = 0
    for row in rows:
        if row is None:
            normalized.append([""] * width)
            none_rows += 1
            continue
        cells = list(row)
        if len(cells) < width:
            ragged_rows += 1
        normalized.append([
            "" if index >= len(cells) or cells[index] is None else str(cells[index])
            for index in range(width)
        ])
    if none_rows:
        warnings.append(f"{none_rows} 行为空行，已按空单元格占位")
    if ragged_rows:
        warnings.append(f"{ragged_rows} 行长度不足最宽行，已补齐空单元格")
    return normalized, warnings


def _guess_font_name(fontname: str, flags: int) -> str:
    """Heuristic to pick a readable font name."""
    if fontname and fontname not in ("", "Unknown"):
        return fontname
    if flags & FONT_FLAG_MONO:
        return "等线"
    if flags & FONT_FLAG_SERIF:
        return "宋体"
    return "黑体"


def _font_info_from_span(span: dict) -> FontInfo:
    """Convert a PyMuPDF text span into the shared font descriptor."""
    flags = int(span.get("flags", 0) or 0)
    return FontInfo(
        name=_guess_font_name(str(span.get("font", "")), flags),
        size=round(float(span.get("size", 0) or 0), 1),
        bold=bool(flags & FONT_FLAG_BOLD),
        italic=bool(flags & FONT_FLAG_ITALIC),
        color=int(span.get("color", 0) or 0),
    )


def _group_visual_lines(lines: List[dict]) -> List[List[dict]]:
    """Merge content-stream fragments that occupy the same visual line: fragments with
    vertical overlap ratio >= 0.5 belong to one row, read left to right; distinct rows stay
    top to bottom. Otherwise PyMuPDF keeps content-stream order and scrambles split CJK lines.
    """
    fragments = [
        line for line in lines
        if any(span.get("text", "").strip() for span in line.get("spans", []))
    ]
    fragments.sort(key=lambda line: (
        (line["bbox"][1] + line["bbox"][3]) / 2,
        line["bbox"][0],
    ))

    rows: List[dict] = []
    for fragment in fragments:
        x0, y0, x1, y1 = (float(value) for value in fragment["bbox"])
        matching_row = None
        for row in rows:
            overlap = min(y1, row["y1"]) - max(y0, row["y0"])
            min_height = min(max(y1 - y0, 0.01), max(row["y1"] - row["y0"], 0.01))
            if overlap > 0 and overlap / min_height >= 0.5:
                matching_row = row
                break

        if matching_row is None:
            rows.append({
                "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                "fragments": [fragment],
            })
        else:
            matching_row["x0"] = min(matching_row["x0"], x0)
            matching_row["y0"] = min(matching_row["y0"], y0)
            matching_row["x1"] = max(matching_row["x1"], x1)
            matching_row["y1"] = max(matching_row["y1"], y1)
            matching_row["fragments"].append(fragment)

    rows.sort(key=lambda row: (row["y0"], row["x0"]))
    for row in rows:
        row["fragments"].sort(key=lambda line: (line["bbox"][0], line["bbox"][1]))
    return [row["fragments"] for row in rows]


# ── Raw block collected during first pass ────────────────
class _RawBlock:
    __slots__ = (
        "page", "text", "fonts", "bbox", "has_bold", "avg_size", "layout"
    )
    def __init__(
        self,
        page: int,
        text: str,
        fonts: List[FontInfo],
        bbox: BBox,
        layout: LayoutInfo,
    ):
        self.page = page
        self.text = text
        self.fonts = fonts
        self.bbox = bbox
        self.has_bold = any(f.bold for f in fonts)
        self.avg_size = sum(f.size for f in fonts) / max(len(fonts), 1)
        self.layout = layout


# ── PDF Parser ───────────────────────────────────────────
class PDFParser(BaseParser):
    """Parse a PDF file into DocumentIR using PyMuPDF (two passes).
    Pass 1: tables via find_tables() + text spans/image blocks; span sizes give the body
    main size, and blocks overlapping table bboxes are skipped (the structured table block
    represents them). Pass 2: classify heading/paragraph via three-signal detection."""

    def parse(self, file_path: str | Path) -> DocumentIR:
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"PDF not found: {file_path}")

        doc = fitz.open(str(file_path))
        try:
            total_pages = len(doc)
            all_font_sizes: List[float] = []
            raw_blocks: List[_RawBlock] = []
            image_blocks: List[Block] = []
            table_blocks: List[Block] = []
            all_fonts: dict[str, FontInfo] = {}
            page_block_ids: dict[int, List[str]] = {}  # page_num → block IDs
            block_idx = 0
            total_word_count = 0
            ocr_pages: List[int] = []
            page_image_counts: dict[int, int] = {}  # page → image 块数量（B4）
            # R02：逐页质量信号（图像覆盖 / 文字位置 / OCR 结果）
            page_image_boxes: dict[int, List[BBox]] = {}
            page_text_boxes: dict[int, List[BBox]] = {}
            page_ocr_state: dict[int, str] = {}
            page_ocr_chars: dict[int, int] = {}
            doc_warnings: List[str] = []  # R04：表格等结构解析降级信息
            ocr_available = bool(get_settings().OCR_ENABLED)
            
            # ── Pass 1: extract tables, images, and text spans ──
            for page_num in range(total_pages):
                page = doc[page_num]
                page_block_ids.setdefault(page_num + 1, [])
            
                # ── 1a. Extract structured tables ────────────
                table_bboxes: List[BBox] = []  # track table areas to skip text
                tables_on_page = page.find_tables()
                if tables_on_page.tables:
                    for table in tables_on_page.tables:
                        tb = table.bbox  # (x0, y0, x1, y1)
                        tbbox = BBox(x0=tb[0], y0=tb[1], x1=tb[2], y1=tb[3])
                        try:
                            rows = _extract_table_rows(table)
                        except Exception as exc:  # noqa: BLE001 —— 单表失败不放弃整篇
                            warning = f"第{page_num + 1}页表格结构提取失败：{exc}"
                            doc_warnings.append(warning)
                            logger.warning("%s", warning)
                            rows = None
                        normalized, normalization_warnings = _normalize_table_rows(rows)
                        if not normalized:
                            # 无法可靠解析 → 明确降级信息，且不把该区域文字吞掉
                            # （不加入 table_bboxes，后续文字块仍按段落保留）。
                            b_id = f"b{block_idx}"
                            block_idx += 1
                            table_blocks.append(Block(
                                id=b_id,
                                page=page_num + 1,
                                type="table",
                                table_data=None,
                                text="",
                                bbox=tbbox,
                                parse_warnings=normalization_warnings
                                or [f"第{page_num + 1}页表格未能解析出单元格"],
                            ))
                            page_block_ids[page_num + 1].append(b_id)
                            continue
                        table_bboxes.append(tbbox)
                        # Build flat text for searching（空/合并单元格占位为空串）
                        flat = " | ".join(" ".join(row) for row in normalized)
                        b_id = f"b{block_idx}"
                        block_idx += 1
                        table_blocks.append(Block(
                            id=b_id,
                            page=page_num + 1,
                            type="table",
                            table_data=normalized,
                            text=flat,
                            bbox=tbbox,
                            parse_warnings=normalization_warnings,
                        ))
                        page_block_ids[page_num + 1].append(b_id)
                        total_word_count += len(flat.replace(" ", ""))
            
                # ── 1b. Extract text and image blocks ──────────
                text_blocks = page.get_text(
                    "dict", flags=TEXT_FLAGS
                )["blocks"]
                digital_chars = len("".join(
                    span.get("text", "")
                    for block in text_blocks if block.get("type") == 0
                    for line in block.get("lines", [])
                    for span in line.get("spans", [])
                ).strip())
                config = get_settings()
                has_image = any(block.get("type") != 0 for block in text_blocks)
                # OCR 状态必须按页记录结果质量：OCR 未报错不等于读出了正文（R02）
                ocr_state = OCR_NOT_ATTEMPTED
                ocr_chars = 0
                needs_ocr = has_image and digital_chars < max(0, config.OCR_MIN_PAGE_CHARS)
                if needs_ocr:
                    if not ocr_available:
                        ocr_state = OCR_DISABLED
                    elif len(ocr_pages) >= max(0, config.OCR_MAX_PAGES):
                        ocr_state = OCR_SKIPPED_LIMIT
                    else:
                        ocr_state, ocr_text_blocks, ocr_chars = _ocr_page_blocks(
                            page, config
                        )
                        if ocr_state == OCR_SUCCESS:
                            original_images = [
                                block for block in text_blocks if block.get("type") != 0
                            ]
                            text_blocks = original_images + [
                                block for block in ocr_text_blocks
                                if block.get("type") == 0
                            ]
                            ocr_pages.append(page_num + 1)
                            logger.info("OCR extracted PDF page %s", page_num + 1)
                        elif ocr_state == OCR_FAILED:
                            # 缺运行时影响整篇文档，后续页不再重复尝试
                            ocr_available = False
                        else:
                            logger.warning(
                                "OCR produced only %s chars on page %s",
                                ocr_chars,
                                page_num + 1,
                            )
                page_ocr_state[page_num + 1] = ocr_state
                page_ocr_chars[page_num + 1] = ocr_chars
            
                for blk in text_blocks:
                    if blk.get("type") != 0:  # image block
                        b_id = f"b{block_idx}"
                        block_idx += 1
                        img_bytes = blk.get("image", None)
                        img_hash = None
                        if img_bytes:
                            img_hash = hashlib.md5(img_bytes).hexdigest()[:8]
                        image_bbox = BBox(
                            x0=blk["bbox"][0], y0=blk["bbox"][1],
                            x1=blk["bbox"][2], y1=blk["bbox"][3],
                        )
                        image_blocks.append(Block(
                            id=b_id,
                            page=page_num + 1,
                            type="image",
                            bbox=image_bbox,
                            image_hash=img_hash,
                        ))
                        page_image_boxes.setdefault(page_num + 1, []).append(image_bbox)
                        page_block_ids[page_num + 1].append(b_id)
                        page_image_counts[page_num + 1] = (
                            page_image_counts.get(page_num + 1, 0) + 1
                        )
                        continue
            
                    # ── Process each line inside the text block ──
                    block_text_lines: List[str] = []
                    block_fonts: List[FontInfo] = []
                    line_bboxes: List[tuple[float, float, float, float]] = []
                    block_bbox = BBox(x0=1e9, y0=1e9, x1=0, y1=0)
            
                    for visual_line in _group_visual_lines(blk.get("lines", [])):
                        line_text = ""
                        visual_spans = [
                            span
                            for line in visual_line
                            for span in line.get("spans", [])
                        ]
                        visual_spans.sort(key=lambda span: (
                            span.get("bbox", (0, 0, 0, 0))[0],
                            span.get("bbox", (0, 0, 0, 0))[1],
                        ))
                        for span in visual_spans:
                            span_text = span.get("text", "")
                            line_text += span_text
                            fi = _font_info_from_span(span)
                            block_fonts.append(fi)
                            all_font_sizes.append(fi.size)
                            if fi.name not in all_fonts:
                                all_fonts[fi.name] = fi
            
                        l_bbox = (
                            min(line["bbox"][0] for line in visual_line),
                            min(line["bbox"][1] for line in visual_line),
                            max(line["bbox"][2] for line in visual_line),
                            max(line["bbox"][3] for line in visual_line),
                        )
                        if line_text.strip():
                            line_bboxes.append(tuple(float(item) for item in l_bbox))
                        block_bbox.x0 = min(block_bbox.x0, l_bbox[0])
                        block_bbox.y0 = min(block_bbox.y0, l_bbox[1])
                        block_bbox.x1 = max(block_bbox.x1, l_bbox[2])
                        block_bbox.y1 = max(block_bbox.y1, l_bbox[3])
                        block_text_lines.append(line_text.strip())
            
                    full_text = "".join(block_text_lines)
                    if not full_text.strip():
                        continue
            
                    bbox = block_bbox if block_bbox.x0 < 1e8 else BBox(x0=0, y0=0, x1=0, y1=0)
            
                    # Skip text block if it overlaps any table area
                    if _overlaps_any(bbox, table_bboxes):
                        continue
            
                    raw_blocks.append(_RawBlock(
                        page=page_num + 1,
                        text=full_text,
                        fonts=block_fonts,
                        bbox=bbox,
                        layout=self._layout_from_pdf_lines(
                            line_bboxes,
                            bbox,
                            float(page.rect.width),
                        ),
                    ))
                    page_text_boxes.setdefault(page_num + 1, []).append(bbox)
            
            # Compute body main font size = mode of all span sizes
            body_main_size = self._compute_body_main_size(all_font_sizes)
            logger.debug(f"Body main font size (mode): {body_main_size}pt")
            
            # ── Pass 2: classify text blocks ──────────────────
            blocks: List[Block] = list(image_blocks) + list(table_blocks)
            
            for raw in raw_blocks:
                block_type, level = self._classify_with_signals(
                    raw.text, raw.fonts, raw.has_bold, raw.avg_size, body_main_size
                )
            
                dominant_font = raw.fonts[0] if raw.fonts else FontInfo(size=body_main_size)
            
                b_id = f"b{block_idx}"
                block_idx += 1
            
                blocks.append(Block(
                    id=b_id,
                    page=raw.page,
                    type=block_type,
                    level=level,
                    text=raw.text,
                    bbox=raw.bbox,
                    font=dominant_font,
                    layout=raw.layout,
                ))
                page_block_ids.setdefault(raw.page, []).append(b_id)
                total_word_count += len(raw.text.replace("\n", "").replace(" ", ""))
            
            # All consumers receive a stable reading order.  Earlier versions
            # placed every image/table before all text, breaking section scans.
            blocks.sort(key=lambda b: (
                b.page or 0,
                b.bbox.y0,
                b.bbox.x0,
                b.id,
            ))
            page_block_ids = {
                page: [block.id for block in blocks if block.page == page]
                for page in range(1, total_pages + 1)
            }

            # Collect page dimensions before closing
            page_dims: List[Tuple[float, float]] = []
            for p in range(total_pages):
                rect = doc[p].rect
                page_dims.append((rect.width, rect.height))
            
        finally:
            doc.close()

        # Build page meta with per-page extraction state (B4/C9/R02)
        page_meta_list: List[PageMeta] = []
        blocks_by_page: dict[int, List[Block]] = {}
        for block in blocks:
            blocks_by_page.setdefault(block.page or 0, []).append(block)
        page_config = get_settings()
        for p in range(1, total_pages + 1):
            w, h = page_dims[p - 1]
            page_blocks = blocks_by_page.get(p, [])
            page_chars = sum(
                len(block.text.strip()) for block in page_blocks
            )
            image_count = page_image_counts.get(p, 0)
            image_boxes = page_image_boxes.get(p, [])
            ratio = _image_area_ratio(image_boxes, w, h)
            unread_region = _has_unread_image_region(
                image_boxes, page_text_boxes.get(p, []), w, h
            )
            status, reason = classify_page(
                PageSignals(
                    page_number=p,
                    digital_chars=page_chars,
                    image_count=image_count,
                    image_area_ratio=ratio,
                    has_unread_image_region=unread_region,
                    ocr_state=page_ocr_state.get(p, OCR_NOT_ATTEMPTED),
                    ocr_chars=page_ocr_chars.get(p, 0),
                ),
                ocr_min_chars=max(1, page_config.OCR_MIN_PAGE_CHARS),
            )
            page_meta_list.append(PageMeta(
                page_number=p,
                width=w,
                height=h,
                block_ids=page_block_ids.get(p, []),
                status=status,
                text_chars=page_chars,
                reason=reason,
                image_area_ratio=round(ratio, 4),
                ocr_attempted=p in page_ocr_state
                and page_ocr_state[p] in {
                    OCR_SUCCESS, OCR_INSUFFICIENT, OCR_FAILED, OCR_SKIPPED_LIMIT,
                },
            ))

        return DocumentIR(
            meta=DocMeta(
                filename=file_path.name,
                pages=total_pages,
                word_count=total_word_count,
                fonts=list(all_fonts.values()),
                page_meta=page_meta_list,
                ocr_pages=ocr_pages,
                parse_warnings=doc_warnings,
                schema_version=IR_SCHEMA_VERSION,
                parser_fingerprint=parser_fingerprint(),
            ),
            blocks=blocks,
        )

    # ── Private helpers ──────────────────────────────────

    @staticmethod
    def _compute_body_main_size(all_sizes: List[float]) -> float:
        """Return the mode of font sizes, or 12.0 if no data."""
        if not all_sizes:
            return 12.0
        # Round to 0.5pt for mode counting (handles minor variations)
        rounded = [round(s * 2) / 2 for s in all_sizes]
        counter = Counter(rounded)
        most_common = counter.most_common(1)
        return most_common[0][0] if most_common else 12.0

    @staticmethod
    def _layout_from_pdf_lines(
        lines: List[tuple[float, float, float, float]],
        bbox: BBox,
        page_width: float,
    ) -> LayoutInfo:
        """Derive measurable layout signals from positioned PDF text lines."""
        line_spacing = None
        first_indent = None
        if len(lines) >= 2:
            y_positions = sorted(line[1] for line in lines)
            gaps = [current - previous for previous, current in zip(y_positions, y_positions[1:])]
            positive = [gap for gap in gaps if gap > 0]
            if positive:
                line_spacing = round(float(median(positive)), 2)
            first_indent = round(lines[0][0] - min(line[0] for line in lines[1:]), 2)

        width = max(0.0, bbox.x1 - bbox.x0)
        center_delta = abs(((bbox.x0 + bbox.x1) / 2) - (page_width / 2))
        if width < page_width * 0.8 and center_delta <= 10:
            alignment = "center"
        elif width < page_width * 0.65 and bbox.x1 >= page_width - 36:
            alignment = "right"
        else:
            alignment = "left"

        return LayoutInfo(
            alignment=alignment,
            line_count=len(lines),
            line_spacing_pt=line_spacing,
            first_line_indent_pt=first_indent,
        )

    @classmethod
    def _classify_with_signals(
        cls,
        text: str,
        fonts: List[FontInfo],
        has_bold: bool,
        avg_size: float,
        body_main_size: float,
    ) -> Tuple[str, int]:
        """Classify block type using three-signal heading detection; returns (block_type, level)."""
        stripped = text.strip()

        # ── 信号(a) 前置防护：纯数字/标点碎片不做标题 ──────────
        # “3640.” 这类参考文献换行碎片会被 ^\d+\. 误判成标题并引发连环误报；整行仅数字/标点 → 按正文处理。
        if re.fullmatch(r"[\d\s\.、\-–—:;,()%]+", stripped):
            return ("paragraph", 0)

        # ── Signal (a): heading numbering regex ──────────
        for pattern, level in HEADING_PATTERNS:
            if pattern.match(stripped):
                return ("heading", level)

        # ── Signals (b) + (c): bold AND larger-than-body AND short ──
        if (
            has_bold
            and avg_size > body_main_size
            and len(stripped) < HEADING_MAX_LENGTH
        ):
            return ("heading", 2)

        return ("paragraph", 0)
