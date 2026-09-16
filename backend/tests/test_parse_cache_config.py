"""R11：解析缓存键必须包含影响解析结果的运行时配置（OCR 等）。

缺陷：`parser_fingerprint()` 只哈希解析器代码文件，OCR_ENABLED / OCR_DPI / OCR_LANGUAGE 变化不会使
缓存 IR 失效，也不改变审查任务复用键，于是「关 OCR 的无文本结果」在开启 OCR 后仍被复用，用户看到旧结论。"""
from __future__ import annotations

import threading
import uuid

from starlette.testclient import TestClient

from app.api import routes
from app.core.config import get_settings
from app.main import app
from app.schemas.ir import IR_SCHEMA_VERSION, DocumentIR
from app.services import review_store
from app.services.parser import cached_ir_is_current, parser_fingerprint

client = TestClient(app)


def _stamped_ir(fingerprint: str) -> DocumentIR:
    return DocumentIR.model_validate({
        "meta": {
            "filename": "scan.pdf",
            "pages": 1,
            "page_meta": [],
            "schema_version": IR_SCHEMA_VERSION,
            "parser_fingerprint": fingerprint,
        },
        "blocks": [],
    })


def _cached_document() -> str:
    document_id = uuid.uuid4().hex[:12]
    data = {
        "id": document_id,
        "filename": "synthetic.pdf",
        "file_path": "unused.pdf",
        "file_type": "pdf",
        "size": 10,
        "sha256": uuid.uuid4().hex * 2,
    }
    review_store.create_document(data)
    routes._documents[document_id] = data
    return document_id


def test_fingerprint_is_stable_for_same_config():
    assert parser_fingerprint() == parser_fingerprint()
    assert parser_fingerprint()


def test_fingerprint_changes_when_ocr_toggle_changes(monkeypatch):
    settings = get_settings()
    before = parser_fingerprint()
    monkeypatch.setattr(settings, "OCR_ENABLED", not bool(settings.OCR_ENABLED))
    assert parser_fingerprint() != before


def test_fingerprint_changes_when_ocr_parameters_change(monkeypatch):
    settings = get_settings()
    before = parser_fingerprint()
    monkeypatch.setattr(settings, "OCR_DPI", int(settings.OCR_DPI) + 37)
    monkeypatch.setattr(settings, "OCR_LANGUAGE", f"{settings.OCR_LANGUAGE}+test")
    monkeypatch.setattr(settings, "TESSDATA_PREFIX", f"{settings.TESSDATA_PREFIX}-test")
    assert parser_fingerprint() != before


def test_fingerprint_changes_when_converter_path_changes(monkeypatch):
    settings = get_settings()
    before = parser_fingerprint()
    monkeypatch.setattr(settings, "SOFFICE_PATH", f"{settings.SOFFICE_PATH}-alt")
    assert parser_fingerprint() != before


def test_fingerprint_changes_when_ocr_page_budget_changes(monkeypatch):
    settings = get_settings()
    before = parser_fingerprint()
    monkeypatch.setattr(settings, "OCR_MAX_PAGES", int(settings.OCR_MAX_PAGES) + 5)
    monkeypatch.setattr(settings, "OCR_MIN_PAGE_CHARS", int(settings.OCR_MIN_PAGE_CHARS) + 5)
    assert parser_fingerprint() != before


def test_cached_ir_is_stale_after_ocr_config_change(monkeypatch):
    ir = _stamped_ir(parser_fingerprint())
    assert cached_ir_is_current(ir) is True
    settings = get_settings()
    monkeypatch.setattr(settings, "OCR_ENABLED", not bool(settings.OCR_ENABLED))
    assert cached_ir_is_current(ir) is False


def test_review_is_not_reused_after_parse_config_change(monkeypatch):
    """同一文档在解析配置变化后再提交，必须新建任务而不是复用旧任务。"""
    started = threading.Event()
    release = threading.Event()
    document_id = _cached_document()

    def slow_review(review_id, doc_id, ruleset_id):
        started.set()
        release.wait(timeout=5)

    monkeypatch.setattr(routes, "_execute_review", slow_review)
    try:
        first = client.post(
            "/api/v1/reviews",
            data={"document_id": document_id, "ruleset_id": "campus", "use_ai": "false"},
        )
        assert first.status_code == 200
        assert started.wait(timeout=2)

        monkeypatch.setattr(get_settings(), "OCR_ENABLED", not bool(get_settings().OCR_ENABLED))

        second = client.post(
            "/api/v1/reviews",
            data={"document_id": document_id, "ruleset_id": "campus", "use_ai": "false"},
        )
        assert second.status_code == 200
        assert second.json()["reused"] is False
        assert second.json()["review_id"] != first.json()["review_id"]
    finally:
        release.set()
