"""R15 回归测试：报告与批注 PDF 的并发生成、缓存校验与失败保护。

原缺陷：同一输出固定使用 ``<output>.tmp``，并在开头 ``unlink`` 它 —— 并发请求会互删对方的中间
文件、互相覆盖；且“文件存在即视为有效缓存”，损坏结果会被永久复用。"""
from __future__ import annotations

import threading
from pathlib import Path

import fitz
import pytest

from app.services.report.annotator import generate_annotated_pdf
from app.services.report import reporter


def _review_payload() -> dict:
    return {
        "status": "done",
        "conclusion": "incomplete",
        "completed_at": "2026-09-10 12:00:00",
        "issues": [
            {
                "rule_id": "T1",
                "checker": "layout_check",
                "severity": "warning",
                "page": 1,
                "bbox": [40, 40, 200, 80],
                "message": "示例问题",
                "suggestion": "示例建议",
            }
        ],
    }


def _source_pdf(tmp_path) -> Path:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(
        fitz.Rect(40, 40, 555, 700),
        "本页为批注样本正文。" * 20,
        fontname="china-s",
        fontsize=11,
    )
    path = tmp_path / "source.pdf"
    doc.save(path)
    doc.close()
    return path


def _assert_valid_pdf(path: Path) -> None:
    assert path.exists() and path.stat().st_size > 0
    doc = fitz.open(str(path))
    try:
        assert doc.page_count > 0
    finally:
        doc.close()


def _leftover_temps(directory: Path) -> list[str]:
    return [p.name for p in directory.glob("*.tmp")]


def test_concurrent_report_generation_is_safe(tmp_path):
    output = tmp_path / "report.pdf"
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            reporter.generate_review_report(
                _review_payload(), "样本.docx", "campus", output
            )
        except BaseException as exc:  # noqa: BLE001 —— 收集线程内异常
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    _assert_valid_pdf(output)
    reporter.verify_report_chinese(output)
    assert _leftover_temps(tmp_path) == []


def test_corrupted_cache_is_regenerated(tmp_path):
    output = tmp_path / "report.pdf"
    output.write_bytes(b"not a pdf at all")
    reporter.generate_review_report(_review_payload(), "样本.docx", "campus", output)
    _assert_valid_pdf(output)
    reporter.verify_report_chinese(output)


def test_valid_cache_is_reused_without_rebuild(tmp_path, monkeypatch):
    """已存在且校验通过的报告作为缓存复用，不再重建。"""
    output = tmp_path / "report.pdf"
    reporter.generate_review_report(_review_payload(), "样本.docx", "campus", output)
    before = output.read_bytes()

    def _boom(*_args, **_kwargs):
        raise RuntimeError("should not rebuild")

    monkeypatch.setattr(reporter.SimpleDocTemplate, "build", _boom)
    reused = reporter.generate_review_report(
        _review_payload(), "样本.docx", "campus", output
    )
    assert reused == output
    assert output.read_bytes() == before


def test_failed_regeneration_keeps_existing_file_untouched(tmp_path, monkeypatch):
    """缓存无效（损坏）时重新生成失败 → 原有文件保持原样，且不留临时文件。"""
    output = tmp_path / "report.pdf"
    sentinel = b"corrupted cache payload"
    output.write_bytes(sentinel)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("build failed")

    monkeypatch.setattr(reporter.SimpleDocTemplate, "build", _boom)
    with pytest.raises(RuntimeError):
        reporter.generate_review_report(_review_payload(), "样本.docx", "campus", output)

    assert output.read_bytes() == sentinel  # 未被删除、未被半成品覆盖
    assert _leftover_temps(tmp_path) == []


def test_concurrent_annotation_generation_is_safe(tmp_path):
    source = _source_pdf(tmp_path)
    output = tmp_path / "annotated.pdf"
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            generate_annotated_pdf(source, _review_payload()["issues"], output)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    _assert_valid_pdf(output)
    assert _leftover_temps(tmp_path) == []


def test_failed_annotation_preserves_previous_file(tmp_path, monkeypatch):
    source = _source_pdf(tmp_path)
    output = tmp_path / "annotated.pdf"
    generate_annotated_pdf(source, _review_payload()["issues"], output)
    before = output.read_bytes()

    def _boom(*_args, **_kwargs):
        raise RuntimeError("save failed")

    monkeypatch.setattr(fitz.Document, "save", _boom)
    with pytest.raises(RuntimeError):
        generate_annotated_pdf(source, _review_payload()["issues"], output)

    assert output.read_bytes() == before
    assert _leftover_temps(tmp_path) == []
