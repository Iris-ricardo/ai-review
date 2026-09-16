"""B4: PDF 图片检测、逐页抽取状态、页面覆盖校验与 IR 缓存失效契约。

C7：提取 flags 缺 TEXT_PRESERVE_IMAGES → 图片块永不出现、OCR 触发失效；C9：逐页状态缺失，整篇字数掩盖“个别页未读出”；
C8：解析缓存键仅 document_id，无结构/解析器版本校验。"""
from __future__ import annotations

import base64

import fitz
import pytest

from app.api import routes
from app.schemas.ir import DocumentIR, IR_SCHEMA_VERSION
from app.services.parser import cached_ir_is_current, parser_fingerprint
from app.services.parser.pdf_parser import PDFParser, TEXT_FLAGS

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _make_pdf(tmp_path, pages) -> str:
    """pages: [{'text': ...} | {'image': bytes} | {}]，按顺序逐页生成 PDF。"""
    doc = fitz.open()
    for spec in pages:
        page = doc.new_page()
        if spec.get("text"):
            page.insert_text((72, 72), spec["text"])
        if spec.get("image"):
            page.insert_image(fitz.Rect(50, 50, 250, 250), stream=spec["image"])
    out = tmp_path / "sample.pdf"
    doc.save(out)
    doc.close()
    return str(out)


# ── C7：flags 与图片块 ───────────────────────────────────

def test_parser_flags_keep_images():
    assert TEXT_FLAGS & fitz.TEXT_PRESERVE_IMAGES, "提取 flags 必须保留图片块"


def test_image_page_yields_image_block_with_page(tmp_path):
    path = _make_pdf(tmp_path, [{"text": "封面文字"}, {"image": PNG_1PX}])
    ir = PDFParser().parse(path)
    images = [b for b in ir.blocks if b.type == "image"]
    assert len(images) == 1
    assert images[0].page == 2
    assert images[0].image_hash


# ── C9：逐页状态 ─────────────────────────────────────────

def test_text_page_status_ok_with_chars(tmp_path):
    path = _make_pdf(tmp_path, [{"text": "研究内容与方法说明"}])
    ir = PDFParser().parse(path)
    meta = ir.meta.page_meta[0]
    assert meta.status == "ok"
    assert meta.text_chars > 0


def test_blank_page_is_empty_not_failure(tmp_path):
    path = _make_pdf(tmp_path, [{"text": "第一页"}, {}])
    ir = PDFParser().parse(path)
    assert ir.meta.page_meta[1].status == "empty"
    assert ir.meta.page_meta[1].text_chars == 0


def test_image_only_page_is_scanned_no_text(tmp_path):
    path = _make_pdf(tmp_path, [{"text": "正文页"}, {"image": PNG_1PX}])
    ir = PDFParser().parse(path)
    assert ir.meta.page_meta[1].status == "scanned_no_text"


def test_page_coverage_rejects_unread_scanned_pages(tmp_path):
    path = _make_pdf(tmp_path, [{"text": "很多正文文字，足够超过最低字数阈值"}, {"image": PNG_1PX}])
    ir = PDFParser().parse(path)
    assert ir.meta.page_meta[1].status == "scanned_no_text"
    with pytest.raises(RuntimeError, match="第2页"):
        routes._validate_page_coverage(ir)


def test_page_coverage_allows_blank_and_text_pages(tmp_path):
    path = _make_pdf(tmp_path, [{"text": "足够长的正文文字内容用于测试"}, {}])
    ir = PDFParser().parse(path)
    routes._validate_page_coverage(ir)  # 不应抛异常


# ── C8：元数据盖章与缓存失效 ─────────────────────────────

def test_parse_stamps_schema_version_and_fingerprint(tmp_path):
    path = _make_pdf(tmp_path, [{"text": "hello world"}])
    ir = PDFParser().parse(path)
    assert ir.meta.schema_version == IR_SCHEMA_VERSION
    assert ir.meta.parser_fingerprint == parser_fingerprint()


def test_cached_ir_current_true_for_fresh_parse(tmp_path):
    path = _make_pdf(tmp_path, [{"text": "hello world"}])
    ir = PDFParser().parse(path)
    assert cached_ir_is_current(ir) is True


def test_cached_ir_stale_when_schema_version_changes(tmp_path):
    path = _make_pdf(tmp_path, [{"text": "hello world"}])
    ir = PDFParser().parse(path)
    ir.meta.schema_version = "ir-v1-legacy"
    assert cached_ir_is_current(ir) is False


def test_cached_ir_stale_when_parser_fingerprint_changes(tmp_path):
    path = _make_pdf(tmp_path, [{"text": "hello world"}])
    ir = PDFParser().parse(path)
    ir.meta.parser_fingerprint = "deadbeef"
    assert cached_ir_is_current(ir) is False


def test_legacy_cached_ir_without_stamp_is_stale(tmp_path):
    # 旧缓存 JSON 没有 schema_version/parser_fingerprint → 应重新解析
    ir = DocumentIR.model_validate({
        "meta": {"filename": "old.pdf", "pages": 1, "page_meta": []},
        "blocks": [],
    })
    assert cached_ir_is_current(ir) is False


def test_legacy_v2_cache_is_loadable_but_stale(tmp_path):
    """R02：新增逐页质量字段后，旧结构缓存必须可安全读取且判定失效。"""
    legacy = DocumentIR.model_validate({
        "meta": {
            "filename": "old-v2.pdf",
            "pages": 1,
            "schema_version": "ir-v2-20260909",
            "parser_fingerprint": "deadbeef",
            "page_meta": [{
                "page_number": 1, "width": 595, "height": 842,
                "status": "ok", "text_chars": 100,
            }],
        },
        "blocks": [],
    })
    # 旧 JSON 缺 image_area_ratio/ocr_attempted → 使用安全缺省值
    assert legacy.meta.page_meta[0].image_area_ratio == 0.0
    assert legacy.meta.page_meta[0].ocr_attempted is False
    assert legacy.meta.parse_warnings == []
    # 版本与指纹均不匹配当前解析器 → 必须重新解析，不能沿用旧结果
    assert cached_ir_is_current(legacy) is False
