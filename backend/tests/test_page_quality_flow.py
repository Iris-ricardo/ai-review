"""R02 回归测试：扫描页/低文字页不得被误判为“已读取”。

三层验证：classify_page 对空白/数字正文/正文+校徽/整页扫描/扫描+页码/混合图文/OCR 成功、无效、失败、
超限各态给出状态与原因；真实 PDF 经 PDFParser 校验逐页状态与覆盖；正式流程断言未读取不得 done/pass，可疑 → incomplete。"""
from __future__ import annotations

import base64
import time
from pathlib import Path

import fitz
import pytest
from starlette.testclient import TestClient

from app.api import routes
from app.core.config import get_settings
from app.main import app
from app.schemas.ir import (
    PAGE_STATUS_EMPTY,
    PAGE_STATUS_MIXED_IMAGE_UNREAD,
    PAGE_STATUS_OCR,
    PAGE_STATUS_OCR_FAILED,
    PAGE_STATUS_OCR_INSUFFICIENT,
    PAGE_STATUS_OCR_SKIPPED_LIMIT,
    PAGE_STATUS_OK,
    PAGE_STATUS_SCANNED_MINIMAL_TEXT,
    PAGE_STATUS_SCANNED_NO_TEXT,
)
from app.services.parser.page_quality import (
    OCR_DISABLED,
    OCR_FAILED,
    OCR_INSUFFICIENT,
    OCR_NOT_ATTEMPTED,
    OCR_SKIPPED_LIMIT,
    OCR_SUCCESS,
    PageSignals,
    classify_page,
)
from app.services.parser.pdf_parser import PDFParser
from app.services import review_store

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

client = TestClient(app)


# ── 合成 PDF 工具 ────────────────────────────────────────

def _make_pdf(tmp_path: Path, pages: list[dict], name: str = "sample.pdf") -> str:
    """pages 规格：{'text'} → _TEXT_RECT 内自动换行的中文文本块（默认）；{'text','text_rect'} → 指定区域的中文文本块；
    {'text','text_at'} → 单行短文本（如扫描页页码）；{'image','image_rect'} → 图像块。"""
    doc = fitz.open()
    for spec in pages:
        page = doc.new_page()
        if spec.get("text"):
            if spec.get("text_at"):
                page.insert_text(
                    spec["text_at"], spec["text"], fontname="china-s", fontsize=11
                )
            else:
                page.insert_textbox(
                    fitz.Rect(*spec.get("text_rect", _TEXT_RECT)), spec["text"],
                    fontname="china-s", fontsize=11,
                )
        if spec.get("image"):
            rect = fitz.Rect(*spec.get("image_rect", (50, 50, 250, 250)))
            page.insert_image(rect, stream=spec["image"])
    out = tmp_path / name
    doc.save(out)
    doc.close()
    return str(out)


def _full_page_scan_page(number_text: str | None = None) -> dict:
    spec = {"image": PNG_1PX, "image_rect": (0, 0, 595, 842)}
    if number_text:
        spec["text"] = number_text
        spec["text_at"] = (300, 820)
    return spec


LONG_TEXT = "本项目围绕城市交通流预测方法开展研究，包含数据采集、模型训练与验证。" * 8
_TEXT_RECT = (40, 40, 555, 780)


# ── 1. 纯函数：全部验收类别 ──────────────────────────────

@pytest.mark.parametrize(
    "signals,expected",
    [
        # 正常空白页
        (PageSignals(1, digital_chars=0, image_count=0), PAGE_STATUS_EMPTY),
        # 正常数字文本页
        (PageSignals(1, digital_chars=800, image_count=0), PAGE_STATUS_OK),
        # 数字正文 + 小校徽（2% 覆盖）
        (PageSignals(1, digital_chars=800, image_count=1, image_area_ratio=0.02), PAGE_STATUS_OK),
        # 整页扫描图（无数字文字）
        (PageSignals(1, digital_chars=0, image_count=1, image_area_ratio=0.98),
         PAGE_STATUS_SCANNED_NO_TEXT),
        # 扫描正文 + 一个数字页码（R02 原始缺陷场景）
        (PageSignals(1, digital_chars=1, image_count=1, image_area_ratio=0.98),
         PAGE_STATUS_SCANNED_MINIMAL_TEXT),
        # 扫描正文 + 较长数字页眉
        (PageSignals(1, digital_chars=64, image_count=1, image_area_ratio=0.95),
         PAGE_STATUS_SCANNED_MINIMAL_TEXT),
        # 混合图文页：大量正文 + 大块无文字图像区域（可疑，需人工确认）
        (PageSignals(1, digital_chars=900, image_count=1, image_area_ratio=0.6,
                     has_unread_image_region=True), PAGE_STATUS_MIXED_IMAGE_UNREAD),
        # OCR 成功
        (PageSignals(1, digital_chars=0, image_count=1, image_area_ratio=0.98,
                     ocr_state=OCR_SUCCESS, ocr_chars=500), PAGE_STATUS_OCR),
        # OCR 返回但没有有效正文
        (PageSignals(1, digital_chars=0, image_count=1, image_area_ratio=0.98,
                     ocr_state=OCR_INSUFFICIENT, ocr_chars=3), PAGE_STATUS_OCR_INSUFFICIENT),
        # OCR 尝试失败
        (PageSignals(1, digital_chars=0, image_count=1, image_area_ratio=0.98,
                     ocr_state=OCR_FAILED), PAGE_STATUS_OCR_FAILED),
        # OCR 达到页数上限
        (PageSignals(1, digital_chars=2, image_count=1, image_area_ratio=0.98,
                     ocr_state=OCR_SKIPPED_LIMIT), PAGE_STATUS_OCR_SKIPPED_LIMIT),
        # OCR 关闭
        (PageSignals(1, digital_chars=1, image_count=1, image_area_ratio=0.98,
                     ocr_state=OCR_DISABLED), PAGE_STATUS_SCANNED_MINIMAL_TEXT),
        # 未触发 OCR 的普通页保持 ok
        (PageSignals(1, digital_chars=500, image_count=1, image_area_ratio=0.1,
                     ocr_state=OCR_NOT_ATTEMPTED), PAGE_STATUS_OK),
    ],
)
def test_classify_page_categories(signals, expected):
    status, reason = classify_page(signals)
    assert status == expected
    assert reason, "每个状态都必须带可解释原因"


def test_classify_ocr_success_below_min_chars_is_unread():
    status, _ = classify_page(
        PageSignals(1, digital_chars=0, image_count=1, image_area_ratio=0.9,
                    ocr_state=OCR_SUCCESS, ocr_chars=5),
        ocr_min_chars=50,
    )
    assert status == PAGE_STATUS_OCR_INSUFFICIENT


def test_classify_does_not_fail_small_images():
    """不得因为含图片就判失败（正常校徽/插图）。"""
    status, _ = classify_page(
        PageSignals(1, digital_chars=1200, image_count=3, image_area_ratio=0.15)
    )
    assert status == PAGE_STATUS_OK


# ── 2. 真实 PDF：逐页状态与覆盖校验 ──────────────────────

def test_scanned_page_with_digital_page_number_is_unread(tmp_path):
    """R02 缺陷复现：扫描页上只有一个数字页码。"""
    path = _make_pdf(tmp_path, [
        {"text": LONG_TEXT, "text_rect": _TEXT_RECT},
        _full_page_scan_page("2"),
    ])
    ir = PDFParser().parse(path)
    meta = ir.meta.page_meta[1]
    assert meta.status == PAGE_STATUS_SCANNED_MINIMAL_TEXT
    assert meta.text_chars == 1
    assert meta.image_area_ratio > 0.9
    with pytest.raises(RuntimeError, match="第2页"):
        routes._validate_page_coverage(ir)


def test_scanned_page_with_long_digital_header_is_unread(tmp_path):
    path = _make_pdf(tmp_path, [
        {"text": LONG_TEXT, "text_rect": _TEXT_RECT},
        _full_page_scan_page("第 2 页 共 20 页 项目申报书 2026 年度"),
    ])
    ir = PDFParser().parse(path)
    assert ir.meta.page_meta[1].status == PAGE_STATUS_SCANNED_MINIMAL_TEXT
    with pytest.raises(RuntimeError):
        routes._validate_page_coverage(ir)


def test_text_page_with_small_logo_stays_ok(tmp_path):
    """正文 + 小校徽：不能被误判为扫描页。"""
    path = _make_pdf(tmp_path, [
        {"text": LONG_TEXT, "text_rect": (40, 40, 555, 280),
         "image": PNG_1PX, "image_rect": (40, 300, 80, 340)},
    ])
    ir = PDFParser().parse(path)
    meta = ir.meta.page_meta[0]
    assert meta.status == PAGE_STATUS_OK
    assert meta.image_area_ratio < 0.1
    routes._validate_page_coverage(ir)  # 不应抛异常


def test_blank_page_still_empty(tmp_path):
    path = _make_pdf(tmp_path, [
        {"text": LONG_TEXT, "text_rect": _TEXT_RECT}, {},
    ])
    ir = PDFParser().parse(path)
    assert ir.meta.page_meta[1].status == PAGE_STATUS_EMPTY
    routes._validate_page_coverage(ir)


def test_mixed_page_with_large_unread_image_region_is_uncertain(tmp_path):
    """正文 + 大块无文字图像区域 → 可疑（不硬失败，但不得判通过）。"""
    path = _make_pdf(tmp_path, [
        {"text": LONG_TEXT, "text_rect": (40, 40, 555, 200),
         "image": PNG_1PX, "image_rect": (40, 220, 560, 830)},
    ])
    ir = PDFParser().parse(path)
    meta = ir.meta.page_meta[0]
    assert meta.status == PAGE_STATUS_MIXED_IMAGE_UNREAD
    routes._validate_page_coverage(ir)  # 可疑页不硬失败
    manual = routes._page_quality_manual_issues(ir)
    assert any(issue.confidence == "manual_required" for issue in manual)
    # 结论不能通过
    conclusion = routes.conclude_review(
        no_executable_rules=False, incomplete_rules=0,
        manual_required=len(manual), errors=0,
    )
    assert conclusion == "incomplete"


# ── 3. OCR 各分支（显式 monkeypatch，不依赖真实 tesseract）──

def _enable_ocr(monkeypatch, **attrs):
    settings = get_settings()
    monkeypatch.setattr(settings, "OCR_ENABLED", True)
    for key, value in attrs.items():
        monkeypatch.setattr(settings, key, value)
    return settings


def test_ocr_failed_marks_page_unread(tmp_path, monkeypatch):
    _enable_ocr(monkeypatch)

    def _boom(self, **kwargs):
        raise RuntimeError("No tessdata specified and Tesseract is not installed")

    monkeypatch.setattr(fitz.Page, "get_textpage_ocr", _boom)
    path = _make_pdf(tmp_path, [{"text": LONG_TEXT}, _full_page_scan_page("2")])
    ir = PDFParser().parse(path)
    meta = ir.meta.page_meta[1]
    assert meta.status == PAGE_STATUS_OCR_FAILED
    assert meta.ocr_attempted is True
    with pytest.raises(RuntimeError):
        routes._validate_page_coverage(ir)


def test_ocr_skipped_when_page_limit_reached(tmp_path, monkeypatch):
    _enable_ocr(monkeypatch, OCR_MAX_PAGES=0)
    path = _make_pdf(tmp_path, [{"text": LONG_TEXT}, _full_page_scan_page("2")])
    ir = PDFParser().parse(path)
    assert ir.meta.page_meta[1].status == PAGE_STATUS_OCR_SKIPPED_LIMIT


def test_ocr_returning_no_valid_text_is_unread(tmp_path, monkeypatch):
    """OCR 未报错但没有有效正文 → 仍按未读取处理，且不得并入正文字数。"""
    from app.services.parser import pdf_parser

    _enable_ocr(monkeypatch, OCR_MIN_PAGE_CHARS=20)
    monkeypatch.setattr(
        pdf_parser, "_ocr_page_blocks",
        lambda page, config: (OCR_INSUFFICIENT, [], 3),
    )
    path = _make_pdf(tmp_path, [{"text": LONG_TEXT}, _full_page_scan_page()])
    ir = PDFParser().parse(path)
    meta = ir.meta.page_meta[1]
    assert meta.status == PAGE_STATUS_OCR_INSUFFICIENT
    assert meta.text_chars == 0  # OCR 结果不足时不得并入正文
    with pytest.raises(RuntimeError):
        routes._validate_page_coverage(ir)


def test_ocr_success_page_is_readable(tmp_path, monkeypatch):
    """OCR 成功读出正文 → 该页可读取，覆盖校验放行。"""
    from app.services.parser import pdf_parser

    _enable_ocr(monkeypatch, OCR_MIN_PAGE_CHARS=20)
    ocr_text = "扫描页正文内容" * 12
    ocr_span = {"text": ocr_text, "size": 12, "font": "Helvetica", "flags": 0,
                "bbox": (72, 72, 400, 88)}
    ocr_line = {"bbox": (72, 72, 400, 88), "spans": [ocr_span]}
    ocr_block = {"type": 0, "bbox": (72, 72, 400, 88), "lines": [ocr_line]}
    monkeypatch.setattr(
        pdf_parser, "_ocr_page_blocks",
        lambda page, config: (OCR_SUCCESS, [ocr_block], len(ocr_text)),
    )
    path = _make_pdf(tmp_path, [{"text": LONG_TEXT}, _full_page_scan_page()])
    ir = PDFParser().parse(path)
    meta = ir.meta.page_meta[1]
    assert meta.status == PAGE_STATUS_OCR
    assert ir.meta.ocr_pages == [2]
    assert meta.text_chars == len(ocr_text)
    routes._validate_page_coverage(ir)


# ── 4. 正式流程：结论不得误通过 ──────────────────────────

def _upload(path: str, tmp_path: Path) -> str:
    with open(path, "rb") as handle:
        response = client.post(
            "/api/v1/documents",
            files={"file": (Path(path).name, handle, "application/pdf")},
        )
    assert response.status_code == 200, response.text
    return response.json()["document_id"]


def _run_review(document_id: str) -> dict:
    response = client.post(
        "/api/v1/reviews",
        data={"document_id": document_id, "ruleset_id": "campus", "use_ai": "false"},
    )
    assert response.status_code == 200, response.text
    review_id = response.json()["review_id"]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        payload = client.get(f"/api/v1/reviews/{review_id}")
        assert payload.status_code == 200
        data = payload.json()
        if data.get("status") in {"done", "failed", "cancelled"}:
            return data
        time.sleep(0.05)
    raise AssertionError("review did not finish in time")


def test_review_with_unread_scanned_page_does_not_pass(tmp_path):
    """扫描页仅一个数字页码：正式任务必须失败（不得 done/pass）。"""
    path = _make_pdf(tmp_path, [{"text": LONG_TEXT}, _full_page_scan_page("2")],
                     name="unread_scan.pdf")
    review = _run_review(_upload(path, tmp_path))
    assert review["status"] == "failed"
    # 失败原因必须可从数据库追溯，且不得写入“通过”的结果
    stored = review_store.get_review(review["review_id"])
    assert "未能提取到正文" in (stored.get("error") or "")
    assert not stored.get("result_json")


def test_review_with_uncertain_image_region_is_incomplete(tmp_path):
    """大块无文字图像区域：任务可完成，但结论必须 incomplete 且未完成有效审查。"""
    path = _make_pdf(tmp_path, [
        {"text": LONG_TEXT, "text_rect": (40, 40, 555, 200),
         "image": PNG_1PX, "image_rect": (40, 220, 560, 830)},
    ], name="uncertain.pdf")
    review = _run_review(_upload(path, tmp_path))
    assert review["status"] == "done"
    assert review["conclusion"] == "incomplete"
    assert review["review_complete"] is False
    assert review["manual_required_checks"] >= 1
    statuses = {item["status"] for item in review["page_quality"]}
    assert PAGE_STATUS_MIXED_IMAGE_UNREAD in statuses
