"""
Unified Intermediate Representation (IR) for project proposals.

Common JSON structure produced by all parsers (PDF, DOCX) and consumed by
downstream rule-checkers, LLM inspectors and report generators.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

# IR 结构版本：解析结果持久化到 documents.ir_json，与 parser_fingerprint 一起用于缓存失效
# （v3 起含逐页质量字段与表格降级信息，旧缓存必须重新解析）。
IR_SCHEMA_VERSION = "ir-v3-20260910"

# ── 逐页读取状态（R02）──
# ok/empty/ocr 已读取；scanned_*/ocr_insufficient/failed/skipped_limit 未读取，mixed_image_unread 可疑 → 均不得判为通过。
PAGE_STATUS_OK = "ok"
PAGE_STATUS_EMPTY = "empty"
PAGE_STATUS_OCR = "ocr"
PAGE_STATUS_SCANNED_NO_TEXT = "scanned_no_text"
PAGE_STATUS_SCANNED_MINIMAL_TEXT = "scanned_minimal_text"
PAGE_STATUS_OCR_INSUFFICIENT = "ocr_insufficient"
PAGE_STATUS_OCR_FAILED = "ocr_failed"
PAGE_STATUS_OCR_SKIPPED_LIMIT = "ocr_skipped_limit"
PAGE_STATUS_MIXED_IMAGE_UNREAD = "mixed_image_unread"

# 明确“未读取”：必须阻止审查通过（正式流程按既有 OCR 提示路径失败）
UNREAD_PAGE_STATUSES = frozenset({
    PAGE_STATUS_SCANNED_NO_TEXT,
    PAGE_STATUS_SCANNED_MINIMAL_TEXT,
    PAGE_STATUS_OCR_INSUFFICIENT,
    PAGE_STATUS_OCR_FAILED,
    PAGE_STATUS_OCR_SKIPPED_LIMIT,
})

# 读取可疑（图像区域未能确认是否含正文）：不得判定通过，但也不硬失败，
# 由正式流程转为 manual_required → 结论 incomplete。
UNCERTAIN_PAGE_STATUSES = frozenset({
    PAGE_STATUS_MIXED_IMAGE_UNREAD,
})

INCOMPLETE_PAGE_STATUSES = UNREAD_PAGE_STATUSES | UNCERTAIN_PAGE_STATUSES



# ── Font descriptor ──────────────────────────────────────
class FontInfo(BaseModel):
    name: str = ""
    size: float = 0.0
    bold: bool = False
    italic: bool = False
    color: int = 0  # sRGB packed int, 0 = default


class LayoutInfo(BaseModel):
    """Paragraph-level layout signals available to deterministic checkers."""

    alignment: str = ""
    line_count: int = 0
    line_spacing_pt: Optional[float] = None
    line_spacing_multiple: Optional[float] = None
    first_line_indent_pt: Optional[float] = None
    left_indent_pt: Optional[float] = None
    right_indent_pt: Optional[float] = None
    space_before_pt: Optional[float] = None
    space_after_pt: Optional[float] = None


# ── Bounding box ─────────────────────────────────────────
class BBox(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float


# ── Single block (heading / paragraph / table / image) ───
BlockType = Literal["heading", "paragraph", "table", "image", "header", "footer", "list"]


class Block(BaseModel):
    id: str  # unique within document, e.g. "b1", "b2"
    page: Optional[int] = None  # 1-indexed; None when parsed from DOCX (no page info)
    type: BlockType
    level: int = 0  # heading level (1-6), 0 for non-headings
    text: str = ""  # plain text content
    bbox: BBox = Field(default_factory=lambda: BBox(x0=0, y0=0, x1=0, y1=0))
    font: FontInfo = Field(default_factory=FontInfo)
    layout: LayoutInfo = Field(default_factory=LayoutInfo)
    # rows × cols, populated for tables；合并单元格占位为空字符串 ""（不写 "None"），
    # 解析彻底失败时保持 None，并在 parse_warnings 中给出降级原因（R04）。
    table_data: Optional[List[List[str]]] = None
    image_hash: Optional[str] = None  # perceptual hash for image blocks
    # 结构化块解析降级信息（如表格提取失败），供下游检查器转为人工确认项（R04）
    parse_warnings: List[str] = Field(default_factory=list)

    @property
    def word_count(self) -> int:
        """Rough Chinese word count (character-based)."""
        return len(self.text.replace("\n", "").replace(" ", ""))


# ── Page metadata ────────────────────────────────────────
class PageMeta(BaseModel):
    page_number: int
    width: float
    height: float
    block_ids: List[str] = Field(default_factory=list)
    # 逐页抽取状态（B4/R02）：状态语义见模块顶部常量；空白页不等于失败，
    # 任何 INCOMPLETE_PAGE_STATUSES 中的状态都必须传递给完整性判断。
    status: str = ""
    text_chars: int = 0
    reason: str = ""
    # 图像覆盖比例（图像块面积之和 / 页面面积，封顶 1.0），用于区分
    # “正文+小图标”与“整页扫描”；旧缓存缺省 0.0 → 由 schema 版本失效重解析。
    image_area_ratio: float = 0.0
    ocr_attempted: bool = False


# ── Document metadata ────────────────────────────────────
class DocMeta(BaseModel):
    filename: str = ""
    pages: int = 0
    word_count: int = 0
    fonts: List[FontInfo] = Field(default_factory=list)
    page_meta: List[PageMeta] = Field(default_factory=list)
    ocr_pages: List[int] = Field(default_factory=list)
    # 文档级解析降级信息（如某页表格结构提取失败）
    parse_warnings: List[str] = Field(default_factory=list)
    # 缓存失效依据（B4/C8）：解析时写入，读取时与当前版本比对
    schema_version: str = ""
    parser_fingerprint: str = ""


# ── Top-level IR ─────────────────────────────────────────
class DocumentIR(BaseModel):
    meta: DocMeta = Field(default_factory=DocMeta)
    blocks: List[Block] = Field(default_factory=list)

    # ── Convenience helpers ──────────────────────────────
    def blocks_on_page(self, page: int) -> List[Block]:
        return [b for b in self.blocks if b.page == page]

    def blocks_without_page(self) -> List[Block]:
        """Return blocks that have no page number (e.g., from DOCX parser)."""
        return [b for b in self.blocks if b.page is None]

    def blocks_by_type(self, block_type: BlockType) -> List[Block]:
        return [b for b in self.blocks if b.type == block_type]

    def headings(self, level: Optional[int] = None) -> List[Block]:
        hs = [b for b in self.blocks if b.type == "heading"]
        if level is not None:
            hs = [b for b in hs if b.level == level]
        return hs

    def paragraphs(self) -> List[Block]:
        return self.blocks_by_type("paragraph")

    def tables(self) -> List[Block]:
        return self.blocks_by_type("table")

    def find_text(self, needle: str, fuzzy: bool = False) -> List[Block]:
        """Find blocks whose text matches needle.

        Args:
            needle: Text to search for; fuzzy: ignore whitespace in the match.
        """
        results: List[Block] = []
        for b in self.blocks:
            if fuzzy:
                # Strip all whitespace for fuzzy matching
                haystack = b.text.replace("\n", "").replace(" ", "").replace("　", "")
                n = needle.replace("\n", "").replace(" ", "").replace("　", "")
                if n in haystack:
                    results.append(b)
            else:
                if needle in b.text:
                    results.append(b)
        return results

    @property
    def total_pages(self) -> int:
        return self.meta.pages

    # ── 逐页读取完整性（R02）────────────────────────────
    def unread_pages(self) -> List[int]:
        """明确未被读取的页（必须阻止通过）。"""
        return [
            meta.page_number
            for meta in self.meta.page_meta
            if meta.status in UNREAD_PAGE_STATUSES
        ]

    def uncertain_pages(self) -> List[int]:
        """读取可疑、需人工确认的页（不得判定通过，但不硬失败）。"""
        return [
            meta.page_number
            for meta in self.meta.page_meta
            if meta.status in UNCERTAIN_PAGE_STATUSES
        ]

    def page_read_incomplete(self, page: int) -> bool:
        """该页是否“未读取或读取可疑”——规则检查器据此避免确定性误判。"""
        for meta in self.meta.page_meta:
            if meta.page_number == page:
                return meta.status in INCOMPLETE_PAGE_STATUSES
        return False

    def has_incomplete_pages(self) -> bool:
        return bool(self.unread_pages() or self.uncertain_pages())
