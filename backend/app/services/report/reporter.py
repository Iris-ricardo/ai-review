"""Generate cached Chinese review reports with reportlab."""
from __future__ import annotations

import os
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence

import fitz
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.core.config import get_settings
from app.services.report.atomic_replace import atomic_replace, path_lock


SYSTEM_NAME = "AI 项目申报书智能形式审查系统"
FOOTER_TEXT = "本报告由系统自动生成，AI 结论建议人工复核"


def _font_candidates() -> list[Path]:
    """候选字体路径：显式配置优先，其后按平台列出常见路径（B6/C13）。

    显式配置 REPORT_FONT_PATH 插在最前；显式配置无效时仍回退到平台默认，而不是直接失败。"""
    settings = get_settings()
    candidates: list[Path] = []
    explicit = str(settings.REPORT_FONT_PATH or "").strip()
    if explicit:
        candidates.append(Path(explicit))
    if os.name == "nt":
        candidates.extend([
            Path("C:/Windows/Fonts/msyh.ttc"),
            Path("C:/Windows/Fonts/simsun.ttc"),
            Path("C:/Windows/Fonts/simhei.ttf"),
        ])
    else:
        # R13 复核：reportlab 只支持 TrueType(glyf) 轮廓，Noto CJK 的 OTC 是 CFF 轮廓（注册会抛
        # "postscript outlines are not supported"），故 TrueType 中文字体排在前面，Noto 只留后备。
        candidates.extend([
            Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
            Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
            Path("/usr/share/fonts/truetype/arphic/uming.ttc"),
            Path("/usr/share/fonts/truetype/arphic/ukai.ttc"),
            Path("/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"),
            Path("/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc"),
        ])
    return candidates


def _register_from_candidates(candidates: Sequence[Path]) -> str:
    """逐一尝试注册字体；全部失败时列出已检查路径。不带注册表短路（可测）。"""
    font_name = "M4Chinese"
    errors: list[str] = []
    for path in candidates:
        if not path.is_file():
            errors.append(f"{path}: 文件不存在")
            continue
        try:
            pdfmetrics.registerFont(TTFont(font_name, str(path), subfontIndex=0))
            return font_name
        except Exception as exc:
            errors.append(f"{path}: {exc}")
    raise RuntimeError(
        "未找到可用中文字体（已检查路径：" + "；".join(errors) + "）"
    )


def register_chinese_font() -> str:
    """注册第一个可用的中文字体；只有实际注册成功才算可用。

    失败时错误信息只列字体候选路径，不泄露无关目录。"""
    font_name = "M4Chinese"
    if font_name in pdfmetrics.getRegisteredFontNames():
        return font_name
    return _register_from_candidates(_font_candidates())


def _footer(canvas, doc, font_name: str) -> None:
    canvas.saveState()
    canvas.setFont(font_name, 8)
    canvas.setFillColor(colors.HexColor("#666666"))
    canvas.drawCentredString(A4[0] / 2, 12 * mm, FOOTER_TEXT)
    canvas.restoreState()


def _paragraph(text: object, style: ParagraphStyle) -> Paragraph:
    safe = str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return Paragraph(safe, style)


def _cached_report_is_valid(path: Path) -> bool:
    """判断已存在的报告是否可安全当作缓存复用（R15）。

    只认“能打开 + 首页中文校验通过 + 非空”；损坏/半成品一律视为无效并触发重新生成。"""
    if not path.exists() or path.stat().st_size <= 0:
        return False
    try:
        verify_report_chinese(path)
    except Exception:  # noqa: BLE001 —— 任何解析/校验失败都视为缓存无效
        return False
    return True


def generate_review_report(
    review: Mapping,
    document_name: str,
    ruleset_name: str,
    output_path: str | Path,
) -> Path:
    """Generate the review report PDF, returning a validated cached file when present."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    # R15：同一输出路径串行化，避免并发请求互相覆盖/互删临时文件
    with path_lock(output):
        if _cached_report_is_valid(output):
            return output
        return _build_review_report(
            review, document_name, ruleset_name, output
        )


def _build_review_report(
    review: Mapping,
    document_name: str,
    ruleset_name: str,
    output: Path,
) -> Path:
    font_name = register_chinese_font()
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ChineseTitle", parent=styles["Title"], fontName=font_name,
        fontSize=22, leading=30, alignment=TA_CENTER, textColor=colors.HexColor("#1F2937"),
    )
    heading_style = ParagraphStyle(
        "ChineseHeading", parent=styles["Heading2"], fontName=font_name,
        fontSize=14, leading=20, spaceAfter=8, textColor=colors.HexColor("#1F2937"),
    )
    body_style = ParagraphStyle(
        "ChineseBody", parent=styles["BodyText"], fontName=font_name,
        fontSize=9, leading=14,
    )
    center_style = ParagraphStyle(
        "ChineseCenter", parent=body_style, alignment=TA_CENTER, fontSize=11, leading=18,
    )

    issues: Sequence[Mapping] = review.get("issues", [])
    errors = sum(1 for issue in issues if issue.get("severity") == "error")
    warnings = sum(1 for issue in issues if issue.get("severity") == "warning")
    infos = sum(1 for issue in issues if issue.get("severity") == "info")
    conclusion = review.get("conclusion") or (
        "pass" if errors == 0 else "needs_revision"
    )
    conclusion_color = colors.HexColor({
        "pass": "#16803C",
        "needs_revision": "#C62828",
        "incomplete": "#B26A00",
    }.get(conclusion, "#B26A00"))
    conclusion_label = {
        "pass": "通过",
        "needs_revision": "需要修改",
        "incomplete": "审查不完整，不能判定通过",
    }.get(conclusion, str(conclusion))
    conclusion_style = ParagraphStyle(
        "Conclusion", parent=center_style, fontSize=18, leading=24,
        textColor=conclusion_color,
    )
    review_time = review.get("completed_at") or review.get("created_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    story = [
        Spacer(1, 36 * mm),
        _paragraph(SYSTEM_NAME, title_style),
        Spacer(1, 20 * mm),
        _paragraph(f"文档名：{document_name}", center_style),
        _paragraph(f"规则集：{ruleset_name}", center_style),
        _paragraph(f"审查时间：{review_time}", center_style),
        Spacer(1, 14 * mm),
        _paragraph(f"审查结论：{conclusion_label}", conclusion_style),
        PageBreak(),
        _paragraph("统计概览", heading_style),
    ]

    overview = Table(
        [["严重级", "error", "warning", "info"], ["数量", errors, warnings, infos]],
        colWidths=[35 * mm, 30 * mm, 30 * mm, 30 * mm],
    )
    overview.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font_name),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEF5")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#AAB4C0")),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.extend([overview, Spacer(1, 8 * mm), _paragraph("分检查器统计", heading_style)])

    checker_counts = Counter(issue.get("checker") or "unknown" for issue in issues)
    checker_rows = [["检查器", "问题数"]] + [[name, count] for name, count in sorted(checker_counts.items())]
    if len(checker_rows) == 1:
        checker_rows.append(["无", 0])
    checker_table = Table(checker_rows, colWidths=[90 * mm, 35 * mm], repeatRows=1)
    checker_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font_name),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEF5")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#AAB4C0")),
        ("ALIGN", (1, 0), (1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.extend([checker_table, Spacer(1, 10 * mm), _paragraph("问题明细", heading_style)])

    detail_rows = [[
        _paragraph("编号", body_style), _paragraph("严重级", body_style),
        _paragraph("页码", body_style), _paragraph("描述", body_style),
        _paragraph("建议", body_style), _paragraph("来源", body_style),
    ]]
    for index, issue in enumerate(issues, start=1):
        source = "AI" if issue.get("confidence") == "ai" or issue.get("layer") == "llm" else "规则"
        detail_rows.append([
            _paragraph(index, body_style),
            _paragraph(issue.get("severity", ""), body_style),
            _paragraph(issue.get("page") or "文档级", body_style),
            _paragraph(issue.get("message", ""), body_style),
            _paragraph(issue.get("suggestion", ""), body_style),
            _paragraph(source, body_style),
        ])
    if len(detail_rows) == 1:
        detail_rows.append([
            _paragraph("-", body_style), _paragraph("-", body_style),
            _paragraph("-", body_style), _paragraph("未发现问题", body_style),
            _paragraph("-", body_style), _paragraph("规则", body_style),
        ])
    detail_table = Table(
        detail_rows,
        colWidths=[10 * mm, 16 * mm, 15 * mm, 54 * mm, 54 * mm, 15 * mm],
        repeatRows=1,
    )
    detail_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font_name),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DCE6F1")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#AAB4C0")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (0, 0), (2, -1), "CENTER"),
        ("ALIGN", (5, 0), (5, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(detail_table)

    # R15：每次生成使用**本次调用独有**的临时文件（同目录，保证 os.replace 原子），
    # 不再共用固定 `<output>.tmp` —— 并发下载不会互相删除/覆盖对方的中间文件。
    fd, temp_name = tempfile.mkstemp(
        prefix=f"{output.stem}.", suffix=".tmp", dir=str(output.parent)
    )
    os.close(fd)
    temporary = Path(temp_name)
    doc = SimpleDocTemplate(
        str(temporary), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=18 * mm, bottomMargin=22 * mm,
        title=f"{document_name} 审查报告", author=SYSTEM_NAME,
    )
    footer = lambda canvas, report_doc: _footer(canvas, report_doc, font_name)
    # B6/C14 + R15：先写本次独有临时文件 → 校验通过 → 再原子替换；
    # 失败只清理本次自己的临时文件，此前有效报告保持不变。
    try:
        doc.build(story, onFirstPage=footer, onLaterPages=footer)
        verify_report_chinese(temporary)
        atomic_replace(temporary, output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return output


def verify_report_chinese(pdf_path: str | Path) -> str:
    """Extract and validate first-page Chinese text from a generated report."""
    doc = fitz.open(str(pdf_path))
    try:
        text = doc[0].get_text()
    finally:
        doc.close()
    if "项目申报书" not in text or "审查结论" not in text:
        raise RuntimeError("审查报告首页中文提取失败或出现乱码")
    return text
