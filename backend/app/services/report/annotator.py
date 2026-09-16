"""Generate annotated review PDFs with PyMuPDF."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Iterable, Mapping

import fitz

from app.services.report.atomic_replace import atomic_replace, path_lock


_HIGHLIGHT_COLORS = {
    "error": (1.0, 0.0, 0.0),
    "warning": (1.0, 0.85, 0.0),
    "info": (0.2, 0.55, 1.0),
}


def _wrapped(text: str, width: int = 42) -> list[str]:
    text = text.replace("\r", " ").replace("\n", " ")
    return [text[i:i + width] for i in range(0, len(text), width)] or [""]


def _insert_summary_pages(doc: fitz.Document, issues: list[Mapping]) -> int:
    page_count = 0
    page = None
    y = 0
    fontname = "china-s"

    def next_page():
        nonlocal page_count, page, y
        page = doc.new_page(
            pno=page_count,
            width=fitz.paper_rect("a4").width,
            height=fitz.paper_rect("a4").height,
        )
        page_count += 1
        page.insert_font(fontname=fontname)
        y = 64
        page.insert_text((64, y), "审查摘要", fontname=fontname, fontsize=18)
        y += 34

    next_page()
    for index, issue in enumerate(issues, start=1):
        line = f"{index}. [{issue.get('rule_id', '')}] {issue.get('message', '')}"
        for part in _wrapped(line):
            if y > page.rect.height - 54:  # type: ignore[union-attr]
                next_page()
            page.insert_text((64, y), part, fontname=fontname, fontsize=10)
            y += 16
    return page_count


def generate_annotated_pdf(
    source_pdf: str | Path,
    issues: Iterable[Mapping],
    output_path: str | Path,
) -> Path:
    """Create an annotated PDF atomically from the immutable review result."""
    source = Path(source_pdf)
    output = Path(output_path)
    if not source.exists():
        raise FileNotFoundError(f"Source PDF not found: {source}")

    output.parent.mkdir(parents=True, exist_ok=True)
    # R15：同一输出路径串行化，避免并发生成互相覆盖临时文件
    with path_lock(output):
        return _build_annotated_pdf(source, issues, output)


def _build_annotated_pdf(
    source: Path,
    issues: Iterable[Mapping],
    output: Path,
) -> Path:
    issue_list = list(issues)
    document_issues = [issue for issue in issue_list if issue.get("page") is None]

    doc = fitz.open(str(source))
    try:
        page_offset = _insert_summary_pages(doc, document_issues) if document_issues else 0
        for issue in issue_list:
            page_number = issue.get("page")
            bbox = issue.get("bbox")
            if page_number is None or not bbox or len(bbox) != 4:
                continue

            page_index = int(page_number) - 1 + page_offset
            if page_index < 0 or page_index >= len(doc):
                continue
            page = doc[page_index]
            rect = fitz.Rect(*bbox) & page.rect
            if rect.is_empty or rect.is_infinite:
                continue

            annot = page.add_highlight_annot(rect)
            color = _HIGHLIGHT_COLORS.get(issue.get("severity"), _HIGHLIGHT_COLORS["info"])
            annot.set_colors(stroke=color)
            annot.set_opacity(0.45)
            message = issue.get("message", "")
            suggestion = issue.get("suggestion", "")
            annot.set_info(content=f"[{issue.get('rule_id', '')}] {message}｜建议：{suggestion}")
            annot.update()

        # R15：本次调用独有的临时文件（同目录 → os.replace 原子），
        # 并发下载不会互相删除/覆盖对方的中间文件；失败只清理自己的临时文件。
        fd, temp_name = tempfile.mkstemp(
            prefix=f"{output.stem}.", suffix=".tmp", dir=str(output.parent)
        )
        os.close(fd)
        temporary = Path(temp_name)
        try:
            doc.save(str(temporary), garbage=4, deflate=True, deflate_fonts=True)
            _verify_annotated_pdf(temporary)
            atomic_replace(temporary, output)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    finally:
        doc.close()
    return output


def _verify_annotated_pdf(path: Path) -> None:
    """发布前的完整性校验：能打开、页数 > 0、且不是空文件。"""
    if not path.exists() or path.stat().st_size <= 0:
        raise RuntimeError(f"批注 PDF 生成失败（空文件）：{path}")
    probe = fitz.open(str(path))
    try:
        if probe.page_count <= 0:
            raise RuntimeError(f"批注 PDF 页数为 0：{path}")
    finally:
        probe.close()
