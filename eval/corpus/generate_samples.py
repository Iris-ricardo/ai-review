"""可运行样本池 v3：为 campus / dachuang / nsfc 生成自检样本（docx/pdf）+ 期望清单，默认输出 eval/corpus/output/；用法：--ruleset campus|dachuang|nsfc|all、--out DIR、--include/--exclude 文件主名（逗号分隔、可重复、不区分大小写）、--skip-check、--list。
产物：*_clean.docx（0 确定性 error/warning）、各“命中/边界” docx、pdf_*.pdf 解析状态反例（本身就是 PDF）、manifest.json（逐份期望）、selfcheck.txt（期望==实际）、.sample_pool_managed.json（受管标记）。
干净模板来自 eval/generate_sample_docx.py，命中样本复用 eval/inject_errors.py 已按实测校准的注入器；默认不调用模型，自检固定 OCR_ENABLED=0（离线确定，图像页统一归 scanned_no_text）。
写盘先在同卷暂存子目录构建、全部成功后发布，只清理能证明属于本工具的旧产物，绝不删除无关文件（详见 output_safety.py 与 README）。"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter
from functools import partial
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = PROJECT_ROOT / "eval"
BACKEND_DIR = PROJECT_ROOT / "backend"
sys.path.insert(0, str(EVAL_DIR))
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(PROJECT_ROOT))

import os  # noqa: E402
import re  # noqa: E402
import tempfile  # noqa: E402

# 让“output_safety.py”在作为脚本运行时也能被本模块导入
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 自检运行环境：只读规则目录；转换产物隔离；不调用外部 OCR/模型
os.environ.setdefault("RULES_DIR", str(PROJECT_ROOT / "rules"))
os.environ.setdefault("UPLOAD_DIR", str(tempfile.mkdtemp(prefix="corpus-tmp-")))
os.environ.setdefault("OUTPUT_DIR", str(tempfile.mkdtemp(prefix="corpus-tmp-")))
os.environ.setdefault("LLM_API_KEY", "")
os.environ.setdefault("OCR_ENABLED", "0")

from inject_errors import (  # noqa: E402
    GROUND_TRUTH,
    INJECTORS,
    expected_issues_for,
    inject_errors,
)
from docx import Document as DocxDocument  # noqa: E402
from docx.enum.text import WD_ALIGN_PARAGRAPH  # noqa: E402
from docx.oxml.ns import qn  # noqa: E402
from docx.oxml import OxmlElement  # noqa: E402
from docx.shared import Mm, Pt  # noqa: E402
from review_layers import (  # noqa: E402
    evaluate_rules,
    hit_signature,
    page_status_map,
    run_layers,
)

# 各注入样本的展示名与类别（确定性 corpus 默认剔除 #5：其目标 anonymity_check 属 AI）
CORPUS_V1 = [
    # (注入编号, 展示名, 类别/说明)
    (1, "required_sections_hit", "删除“研究方法”节 → 必填章节缺失 + 标题编号不连续"),
    (2, "page_limit_hit", "插入空白页 → 超过页数上限"),
    (3, "budget_check_hit_total", "预算总额与分项合计不符"),
    (4, "budget_check_hit_ratio", "管理费占比超限（同时致勾稽不平）"),
    (6, "figure_table_numbering_hit", "删除图2题注 → 图表编号不连续"),
    (7, "section_order_hit", "交换“研究目标/研究方法”两节（编号保留）"),
    (8, "cross_field_hit", "封面项目名称错字 → 跨字段不一致"),
    (9, "date_check_hit", "起止时间调换 → 日期异常 + 期限不符"),
    (10, "signature_page_hit", "删除签字盖章页"),
]

# 自实现 mutator（docx 级参数化，经验证后固化期望）
def _remove_paragraph(paragraph) -> None:
    parent = paragraph._element.getparent()
    if parent is not None:
        parent.remove(paragraph._element)


def _set_east_asia(run, name: str) -> None:
    rpr = run._element.get_or_add_rPr()
    rf = rpr.find(qn("w:rFonts"))
    if rf is None:
        rf = OxmlElement("w:rFonts")
        rpr.insert(0, rf)
    rf.set(qn("w:eastAsia"), name)


def _mut_font_body(copy_path: Path) -> None:
    """所有非标题段落的字体改为楷体 → 正文主字体非宋体。"""
    doc = DocxDocument(str(copy_path))
    changed = 0
    for p in doc.paragraphs:
        if p.style is not None and (p.style.name or "").startswith(("Heading", "Title")):
            continue
        if not p.text.strip():
            continue
        for run in p.runs:
            if not run.text.strip():
                continue
            _set_east_asia(run, "楷体")
        changed += 1
    assert changed > 5, f"font mutator 只改了 {changed} 个非空段落"
    doc.save(str(copy_path))


def _mut_missing_field(copy_path: Path, candidates: tuple[str, ...] = ("填表日期", "申请日期")) -> None:
    """删除封面中某条该规则集要求必填的字段行 → required_fields 报缺少必填字段。
    candidates 按规则集给：campus=填表日期，dachuang=指导教师，nsfc=申请代码。
    """
    doc = DocxDocument(str(copy_path))
    hit = False
    for p in list(doc.paragraphs):
        if any(candidate in p.text for candidate in candidates):
            _remove_paragraph(p)
            hit = True
            break
    assert hit, f"未找到字段行：{candidates}"
    doc.save(str(copy_path))


def _mut_bad_field_format(copy_path: Path) -> None:
    """把“联系电话/手机”一行的号码改成非法值 → field_format 报格式错误。
    通用化：不假设固定号码（campus=13800138000 / dachuang=13900139000 都能命中）。
    """
    import re as _re

    doc = DocxDocument(str(copy_path))
    hit = False
    pattern = _re.compile(r"1[3-9]\d{9}")
    for p in doc.paragraphs:
        if not any(kw in p.text for kw in ("联系电话", "手机")):
            continue
        for run in p.runs:
            if pattern.search(run.text):
                run.text = pattern.sub("12345", run.text)
                hit = True
    assert hit, "未找到联系电话号码"
    doc.save(str(copy_path))


def _mut_duplicate_heading(copy_path: Path) -> None:
    """文末追加重复的一级标题（普通段落样式，未加粗）→ heading_numbering 编号不连续 + layout_check 标题未加粗。
    通用化：复制该文档第一个一级标题的编号与文字（各规则集模板都能命中）。
    """
    doc = DocxDocument(str(copy_path))
    first_heading = None
    for p in doc.paragraphs:
        if p.style is not None and (p.style.name or "").startswith("Heading 1"):
            first_heading = p.text.strip()
            break
    assert first_heading, "未找到一级标题"
    doc.add_paragraph(f"{first_heading}（重复）")
    doc.save(str(copy_path))


def _mut_left_margin_small(copy_path: Path) -> None:
    """把整篇（唯一 section）左边距压到 10mm（下限 15mm）→ layout_check 左边距不足。"""
    doc = DocxDocument(str(copy_path))
    assert doc.sections, "样本模板没有 section"
    for section in doc.sections:
        section.left_margin = Mm(10)
    doc.save(str(copy_path))


def _mut_reference_format(copy_path: Path) -> None:
    """破坏参考文献条目的必需字段 → reference_format 命中（R16 修正后新增）：去掉前两条文献的 `[C]/[J]` 类型标识与 4 位年份。
    事实核对：检查器注册在 backend/app/services/llm/checkers/__init__.py:275（ReferenceFormatChecker），其“年份 / 文献类型标识”判定完全确定、不需要调用模型
    （semantic_review 为可选开关，三个规则集均为 false），因此本样本可确定性命中。"""
    import re as _re

    doc = DocxDocument(str(copy_path))
    in_references = False
    mutated = 0
    for p in doc.paragraphs:
        text = p.text.strip()
        if p.style is not None and (p.style.name or "").startswith("Heading 1"):
            in_references = "参考文献" in text
            continue
        if not in_references or not text:
            continue
        if not _re.match(r"^\[\d+\]", text):
            continue
        new_text = _re.sub(r"\[[JMCDBRNSP]\]", "", text, flags=_re.IGNORECASE)
        new_text = _re.sub(r"(?:19|20)\d{2}", "", new_text)
        if new_text == text:
            continue
        for run in p.runs:
            run.text = ""
        p.runs[0].text = new_text if p.runs else None
        if not p.runs:
            p.add_run(new_text)
        mutated += 1
        if mutated >= 2:
            break
    assert mutated == 2, f"reference_format mutator 只改了 {mutated} 条文献"
    doc.save(str(copy_path))


def _replace_paragraph_text(paragraph, old: str, new: str) -> bool:
    if old not in paragraph.text:
        return False
    for run in paragraph.runs:
        if old in run.text:
            run.text = run.text.replace(old, new, 1)
            return True
    text = paragraph.text.replace(old, new, 1)
    paragraph.clear()
    paragraph.add_run(text)
    return True


def _mut_wrong_period(copy_path: Path) -> None:
    """把“研究期限”的结束年份改到开始年份之前 → date_check 报 2 条
    （① 起止日期异常；② 与指南要求月数不一致）。dachuang/nsfc 通用。"""
    import re as _re

    doc = DocxDocument(str(copy_path))
    hit = False
    for p in doc.paragraphs:
        if "研究期限" not in p.text:
            continue
        dates = _re.findall(r"((?:19|20)\d{2})\s*年\s*(\d{1,2})\s*月", p.text)
        if len(dates) < 2:
            continue
        start_year, start_month = dates[0]
        old_end = f"{dates[1][0]}年{dates[1][1]}月"
        new_end = f"{int(start_year) - 1}年{start_month}月"
        if _replace_paragraph_text(p, old_end, new_end):
            hit = True
            break
    assert hit, "未找到可修改的研究期限"
    doc.save(str(copy_path))


def _mut_budget_mismatch(copy_path: Path) -> None:
    """把预算表“材料费”一行的金额 +1（不碰合计、不碰管理费占比）→
    budget_check 勾稽不平 1 条；不影响封面预算总额与合计的一致性。"""
    import re as _re

    doc = DocxDocument(str(copy_path))
    hit = False
    for table in doc.tables:
        for row in table.rows:
            row_text = " ".join(cell.text for cell in row.cells)
            if "材料费" not in row_text:
                continue
            for cell in row.cells:
                match = _re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*", cell.text)
                if not match:
                    continue
                old_value = match.group(1)
                new_value = (
                    f"{float(old_value) + 1:.1f}"
                    if "." in old_value
                    else str(int(old_value) + 1)
                )
                for paragraph in cell.paragraphs:
                    if _replace_paragraph_text(paragraph, old_value, new_value):
                        hit = True
            if hit:
                break
        if hit:
            break
    assert hit, "未找到预算表“材料费”金额单元格"
    doc.save(str(copy_path))


def _mut_delete_section(copy_path: Path, candidates: tuple[str, ...]) -> None:
    """删除一个必填一级章节（标题+正文）→ required_sections 缺失。
    连带副作用：标题编号跳号（如 二 → 四），heading_numbering 会如实报 1 条。
    """
    doc = DocxDocument(str(copy_path))
    paragraphs = list(doc.paragraphs)
    start = None
    for index, paragraph in enumerate(paragraphs):
        style = (paragraph.style.name or "") if paragraph.style is not None else ""
        if style.startswith("Heading 1") and any(
            candidate in paragraph.text for candidate in candidates
        ):
            start = index
            break
    assert start is not None, f"未找到待删除章节：{candidates}"
    end = len(paragraphs)
    for index in range(start + 1, len(paragraphs)):
        style = (paragraphs[index].style.name or "") if paragraphs[index].style else ""
        if style.startswith("Heading 1"):
            end = index
            break
    for paragraph in paragraphs[start:end]:
        _remove_paragraph(paragraph)
    doc.save(str(copy_path))


def _mut_swap_sections(copy_path: Path, first_heading_index: int = 1) -> None:
    """交换相邻两个一级章节（保留原编号）→ section_order 顺序不符。
    连带副作用：编号序列被打乱 → heading_numbering 如实报若干条“编号不连续”，条数取决于被交换位置（自检按作者期望逐条断言）。
    """
    doc = DocxDocument(str(copy_path))
    body = doc.element.body
    children = list(body)
    headings = [
        p for p in doc.paragraphs
        if p.style is not None and (p.style.name or "").startswith("Heading 1")
    ]
    index = first_heading_index
    assert len(headings) >= index + 3, f"一级章节不足：{len(headings)}"
    first, second, after = headings[index], headings[index + 1], headings[index + 2]
    first_index = children.index(first._p)
    second_index = children.index(second._p)
    end_index = children.index(after._p)
    reordered = (
        children[:first_index]
        + children[second_index:end_index]
        + children[first_index:second_index]
        + children[end_index:]
    )
    for child in list(body):
        body.remove(child)
    for child in reordered:
        body.append(child)
    doc.save(str(copy_path))


def _mut_delete_caption(copy_path: Path) -> None:
    """删除“图2”题注行 → figure_table_numbering 编号不连续。"""
    doc = DocxDocument(str(copy_path))
    for paragraph in list(doc.paragraphs):
        if paragraph.text.strip().startswith("图2 "):
            _remove_paragraph(paragraph)
            doc.save(str(copy_path))
            return
    raise AssertionError("未找到图2题注")


def _mut_cover_budget_mismatch(copy_path: Path) -> None:
    """只改封面“预算总额”数值（预算表内部仍勾稽平衡）→ 跨字段一致性不一致。"""
    import re as _re

    doc = DocxDocument(str(copy_path))
    pattern = _re.compile(r"(预算总额[：:]\s*)(\d+(?:\.\d+)?)")
    for paragraph in doc.paragraphs:
        match = pattern.search(paragraph.text)
        if not match:
            continue
        old_value = match.group(2)
        new_value = (
            f"{float(old_value) - 1000:.1f}"
            if "." not in old_value and float(old_value) > 2000
            else f"{float(old_value) - 1:.1f}"
        )
        if "." not in old_value:
            new_value = str(int(float(old_value)) - 1000)
        if _replace_paragraph_text(paragraph, f"{match.group(1)}{old_value}",
                                   f"{match.group(1)}{new_value}"):
            doc.save(str(copy_path))
            return
    raise AssertionError("未找到封面预算总额")


def _mut_insert_cover_budget(copy_path: Path) -> None:
    """在封面插入“预算总额”并与预算表合计不一致 → 跨字段一致性不一致。"""
    doc = DocxDocument(str(copy_path))
    anchor = None
    for paragraph in doc.paragraphs:
        if paragraph.text.startswith("申请代码："):
            anchor = paragraph
            break
    assert anchor is not None, "未找到申请代码锚点（封面结构已变化）"
    new_paragraph = anchor.insert_paragraph_before()
    anchor._p.addnext(new_paragraph._p)
    new_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = new_paragraph.add_run("预算总额：29.0万元")
    run.font.name = "宋体"
    run.font.size = Pt(14)
    _set_east_asia(run, "宋体")
    doc.save(str(copy_path))


def _mut_page_limit(copy_path: Path, extra_pages: int = 5) -> None:
    """重复插入“分页符 + 中性正文段落”撑爆页数上限（不新增标题、不引入关键词）。"""
    from docx.enum.text import WD_BREAK

    doc = DocxDocument(str(copy_path))
    source_text = None
    for paragraph in doc.paragraphs:
        if paragraph.text.startswith("校园快递最后一公里配送") or (
            "本项目" in paragraph.text and len(paragraph.text) > 60
        ):
            source_text = paragraph.text
            break
    assert source_text, "未找到可复制的正文段落"
    for _ in range(extra_pages):
        page_break = doc.add_paragraph()
        page_break.add_run().add_break(WD_BREAK.PAGE)
        doc.add_paragraph(source_text)
    doc.save(str(copy_path))


def _mut_word_limit(copy_path: Path, anchor_candidates: tuple[str, ...],
                    extra_chars: int = 4700) -> None:
    """追加高密度正文段落使**总字数超限而页数仍在上限内**。
    在“附件清单/签字页”之前插入，保证末两页仍保留签章关键字（否则会把 signature_page 的确定性缺失一并带出来）。
    """
    doc = DocxDocument(str(copy_path))
    anchor = None
    for paragraph in doc.paragraphs:
        if any(candidate in paragraph.text for candidate in anchor_candidates):
            anchor = paragraph
            break
    assert anchor is not None, f"未找到插入锚点：{anchor_candidates}"

    chunk = (
        "本项目围绕校园配送场景持续开展数据采集与算法验证工作，"
        "对配送时段、楼宇分布与取件行为进行统计建模，并逐项记录实验过程与结论。"
    )
    written = 0
    while written < extra_chars:
        paragraph = doc.add_paragraph()
        run = paragraph.add_run(chunk)
        run.font.name = "宋体"
        run.font.size = Pt(12)
        _set_east_asia(run, "宋体")
        anchor._p.addprevious(paragraph._p)
        written += len(chunk)
    doc.save(str(copy_path))


MUTATIONS = {
    "font_check_hit": (_mut_font_body, [{"checker": "font_check", "severity": "warning"}]),
    "required_fields_hit": (_mut_missing_field, [{"checker": "required_fields", "severity": "error"}]),
    "field_format_hit": (_mut_bad_field_format, [{"checker": "field_format", "severity": "warning"}]),
    "heading_numbering_hit": (_mut_duplicate_heading, [
        {"checker": "heading_numbering", "severity": "warning"},
        {"checker": "layout_check", "severity": "warning"},
    ]),
    "layout_margin_hit": (_mut_left_margin_small, [
        {"checker": "layout_check", "severity": "warning"},
    ]),
    "reference_format_hit": (_mut_reference_format, [
        {"checker": "reference_format", "severity": "warning"},
        {"checker": "reference_format", "severity": "warning"},
    ]),
    "date_check_hit": (_mut_wrong_period, [
        {"checker": "date_check", "severity": "warning"},
        {"checker": "date_check", "severity": "warning"},
    ]),
    "budget_check_hit": (_mut_budget_mismatch, [
        {"checker": "budget_check", "severity": "error"},
    ]),
}
MUTATION_NOTES = {
    "font_check_hit": "所有非标题段落改楷体 → 正文主字体非宋体",
    "required_fields_hit": "删除该规则集要求的必填字段 → 缺少必填字段",
    "field_format_hit": "联系电话改为 12345 → 格式非法",
    "heading_numbering_hit": "文末追加重复一级标题（普通段落、未加粗）→ 编号不连续 + 未加粗",
    "layout_margin_hit": "整篇左边距 31.7mm→10mm（下限 15mm）→ 页边距不足",
    "reference_format_hit": "前两条参考文献去掉类型标识与年份 → 2 条格式缺陷"
                            "（确定性分支，不调用模型）",
    "date_check_hit": "研究期限结束年份改到开始之前 → 起止异常 + 期限不符（2 条）",
    "budget_check_hit": "预算表“材料费”金额 +1 → 与合计勾稽不平（1 条）",
    "required_sections_hit": "删除一个必填一级章节 → 章节缺失 + 标题编号跳号",
    "section_order_hit": "交换第 2/3 个一级章节（保留编号）→ 顺序不符 + 编号不连续 ×3",
    "figure_table_numbering_hit": "删除图2题注 → 图表编号不连续",
    "cross_field_hit": "封面与预算表金额不一致 → 跨字段一致性告警",
    "page_limit_hit": "插入分页与正文撑爆页数上限（不新增标题、不引入关键词）",
    "word_limit_hit": "向立项依据追加高密度正文 → 章节字数超上限（nsfc 专属场景）",
}
# 各规则集的“规则特定”命中样本（作者口径期望，经自检实测校准）：
# campus 用已验证的注入器 + reference_format；扩展规则集按各自模板生成。
RULESET_EXTRA_MUTATIONS: dict[str, list[tuple[str, object, list[dict]]]] = {
    "campus": [
        ("reference_format_hit", _mut_reference_format, [
            {"checker": "reference_format", "severity": "warning"},
            {"checker": "reference_format", "severity": "warning"},
        ]),
    ],
    "dachuang": [
        ("required_sections_hit",
         partial(_mut_delete_section, candidates=("三、创新点",)), [
             {"checker": "required_sections", "severity": "error"},
             {"checker": "heading_numbering", "severity": "warning"},
         ]),
        ("section_order_hit", _mut_swap_sections, [
            {"checker": "section_order", "severity": "warning"},
            {"checker": "heading_numbering", "severity": "warning"},
            {"checker": "heading_numbering", "severity": "warning"},
            {"checker": "heading_numbering", "severity": "warning"},
        ]),
        ("cross_field_hit", _mut_cover_budget_mismatch, [
            {"checker": "cross_field_consistency", "severity": "warning"},
        ]),
        ("page_limit_hit", _mut_page_limit, [
            {"checker": "page_limit", "severity": "error"},
        ]),
    ],
    "nsfc": [
        ("required_sections_hit",
         partial(_mut_delete_section, candidates=("五、研究基础与工作条件",)), [
             {"checker": "required_sections", "severity": "error"},
             {"checker": "heading_numbering", "severity": "warning"},
         ]),
        ("section_order_hit", partial(_mut_swap_sections, first_heading_index=0), [
            {"checker": "section_order", "severity": "warning"},
            {"checker": "heading_numbering", "severity": "warning"},
            {"checker": "heading_numbering", "severity": "warning"},
        ]),
        ("figure_table_numbering_hit", _mut_delete_caption, [
            {"checker": "figure_table_numbering", "severity": "warning"},
        ]),
        ("cross_field_hit", _mut_insert_cover_budget, [
            {"checker": "cross_field_consistency", "severity": "warning"},
        ]),
        ("reference_format_hit", _mut_reference_format, [
            {"checker": "reference_format", "severity": "warning"},
            {"checker": "reference_format", "severity": "warning"},
        ]),
        # nsfc 的字数上限是**分章节**的（立项依据 ≤5000），且页数上限 40 页 →
        # 可以孤立命中 word_limit（campus/dachuang 是总字数上限，先撞页数上限）
        ("word_limit_hit",
         partial(_mut_word_limit, anchor_candidates=("二、研究目标与内容",),
                 extra_chars=4300), [
             {"checker": "word_limit", "severity": "warning"},
         ]),
    ],
}
# R16 事实核对：reference_format 并非“代码未实现”——检查器注册在 backend/app/services/llm/checkers/__init__.py（ReferenceFormatChecker），
# 年份/文献类型标识为确定性分支，semantic_review=false 时不外发材料；word_limit（campus 上限 15000 字）无法在 campus 模板上孤立命中（干净模板 7158 字、16 页，再填 7800+ 字必先突破 20 页上限），需在 dachuang/nsfc 专用短模板上补（见 README“待补”行）。

# PDF 解析状态类反例样本的确定性选页规则（镜像 pdf_parser 的标题判定，避免
# 被替换页含标题/表格/图注/预算关键词而引发无关规则命中）
_HEADING_LINE = re.compile(
    r"^[一二三四五六七八九十]+[、.]"
    r"|^（[一二三四五六七八九十]+）"
    r"|^\d+\.\d+(?:\.\d+)*"
    r"|^\d+[\.、](?!\d)"
    r"|^\(\d+\)"
)
_PLAIN_ONLY_KEYWORDS = [
    "图1", "图2", "表1", "参考文献", "签字", "盖章", "预算", "合计",
    "经费", "研究期限", "联系方式", "电子邮箱", "填表日期",
]


def clean_base_docx() -> Path:
    """返回（必要时重新生成）校准过的 campus 干净样本。"""
    target = EVAL_DIR / "samples" / "sample_proposal.docx"
    if not target.exists():
        import generate_sample_docx as gen
        gen.generate_sample(str(target))
    return target


def expectations_for(number: int) -> list[dict]:
    """注入编号 n 的确定性期望（剔除 AI/人工确认项后，仅 error/warning）。"""
    out = []
    for item in expected_issues_for([number], include_anonymity=False):
        if item["severity"] in {"error", "warning"}:
            out.append(item)
    return out


def compare_expectations(entry: dict, payload: dict) -> list[str]:
    """按 manifest 的作者期望逐层比对，返回问题描述（空列表=通过）。"""
    problems: list[str] = []
    parse = payload.get("parse", {})
    rules = payload.get("rules", {})
    task = payload.get("task", {})

    if entry.get("expected_parse_error"):
        if parse.get("ok"):
            problems.append("[解析层] 期望解析失败（正确拒绝），实际解析成功")
        return problems
    if not parse.get("ok"):
        problems.append(f"[解析层] 解析失败：{parse.get('error')}")
        return problems

    expected_hits = Counter(
        (item["checker"], item["severity"]) for item in entry.get("expected", [])
    )
    actual_hits = Counter(hit_signature(payload))
    if expected_hits != actual_hits:
        problems.append(
            f"[规则层] 命中不一致 期望={dict(expected_hits) or '{}'} "
            f"实际={dict(actual_hits) or '{}'}"
        )

    status_map = page_status_map(payload)
    for page, expected_status in (entry.get("expected_pages") or {}).items():
        actual = status_map.get(int(page))
        if actual != expected_status:
            problems.append(
                f"[解析层] 第{page}页状态期望 {expected_status}，实际 {actual}"
            )

    expected_manual_min = entry.get("expected_manual_min")
    if expected_manual_min is not None:
        manual_count = len(rules.get("manual", []))
        if manual_count < int(expected_manual_min):
            problems.append(
                f"[规则层] 人工确认项期望 ≥{expected_manual_min}，实际 {manual_count}"
            )

    expected_outcome = entry.get("expected_outcome") or {}
    for key, expected_value in expected_outcome.items():
        actual_value = task.get(key)
        if isinstance(expected_value, bool):
            mismatch = bool(actual_value) is not expected_value
        else:
            mismatch = actual_value != expected_value
        if mismatch:
            problems.append(
                f"[任务层] {key} 期望 {expected_value}，实际 {actual_value}"
            )
    return problems


def run_scenarios(clean_payload: dict) -> list[tuple[str, list[str]]]:
    """R10 验收场景：空规则集 / 规则异常（正确拒绝也是通过）。
    复用同一份干净样本 IR，不额外转换、不写库。
    """
    results: list[tuple[str, list[str]]] = []
    ir = clean_payload.get("_ir")
    if ir is None:
        return [("场景检查", ["无法获取样本 IR（内部错误）"])]

    # 场景 1：空规则集 → 不得判 pass，review_complete=False
    scenario, problems = "空规则集 → incomplete/未完成", []
    empty = evaluate_rules(ir, EMPTY_RULESET_TEXT)["summary"]
    if empty.get("no_executable_rules") is not True:
        problems.append(f"no_executable_rules 期望 True，实际 {empty.get('no_executable_rules')}")
    if empty.get("conclusion") != "incomplete":
        problems.append(f"结论期望 incomplete，实际 {empty.get('conclusion')}")
    if empty.get("review_complete") is not False:
        problems.append(f"review_complete 期望 False，实际 {empty.get('review_complete')}")
    results.append((scenario, problems))

    # 场景 2：检查器抛异常 → 规则失败被如实记录，结论 incomplete
    scenario, problems = "规则异常 → 失败被记录且结论 incomplete", []
    failed = evaluate_rules(ir, FAILING_RULESET_TEXT)
    summary = failed["summary"]
    if summary.get("failed_checks", 0) < 1:
        problems.append(f"failed_checks 期望 ≥1，实际 {summary.get('failed_checks')}")
    if summary.get("incomplete_rules", 0) < 1:
        problems.append(f"incomplete_rules 期望 ≥1，实际 {summary.get('incomplete_rules')}")
    if summary.get("conclusion") != "incomplete":
        problems.append(f"结论期望 incomplete，实际 {summary.get('conclusion')}")
    if summary.get("review_complete") is not False:
        problems.append(f"review_complete 期望 False，实际 {summary.get('review_complete')}")
    results.append((scenario, problems))
    return results


# ── PDF 反例/解析状态样本 ─────────────────────────────────
def build_blank_mid_pdf(source_pdf: Path, out_path: Path, after_page: int) -> None:
    """在源 PDF 的第 after_page 页之后插入一张真正空白的 PDF 页。"""
    import fitz

    src = fitz.open(str(source_pdf))
    try:
        total = len(src)
        if not 1 <= after_page < total:
            raise ValueError(f"after_page={after_page} 越界（共 {total} 页）")
        rect = src[0].rect
        dst = fitz.open()
        try:
            dst.insert_pdf(src, from_page=0, to_page=after_page - 1)
            dst.new_page(width=rect.width, height=rect.height)
            dst.insert_pdf(src, from_page=after_page, to_page=total - 1)
            dst.save(str(out_path), garbage=4, deflate=True)
        finally:
            dst.close()
    finally:
        src.close()


def _render_scan_png(text: str) -> bytes:
    """把一段普通正文渲染成图片字节（模拟扫描件），供插入为图像页。"""
    import fitz

    doc = fitz.open()
    try:
        page = doc.new_page()
        page.insert_textbox(
            fitz.Rect(50, 50, 545, 780), text, fontname="china-s", fontsize=11
        )
        return page.get_pixmap(dpi=150).tobytes("png")
    finally:
        doc.close()


SCAN_PAGE_TEXT = (
    "本页为扫描件示例：正文内容以图像形式保存，没有可复制的数字文字。"
    "形式审查系统在未启用 OCR 时无法读取本页正文，必须如实标记为读取不完整，"
    "而不能因为页面上存在少量数字（例如页码）就当作已读取。"
) * 6


def build_scanned_insert_pdf(
    source_pdf: Path,
    out_path: Path,
    *,
    after_page: int = 2,
    with_page_number: bool = False,
) -> int:
    """在源 PDF 的 after_page 之后插入一张“扫描页”（图像），返回其 1-based 页码。
    不破坏任何原有页面（样本 = 干净文档 + 一页扫描件），因此不会因删除正文而引入与解析状态无关的规则命中（任何长度的模板都适用）。
    """
    import fitz

    png = _render_scan_png(SCAN_PAGE_TEXT)
    src = fitz.open(str(source_pdf))
    try:
        total = len(src)
        if not 1 <= after_page < total:
            raise ValueError(f"after_page={after_page} 越界（共 {total} 页）")
        rect = src[0].rect
        dst = fitz.open()
        try:
            dst.insert_pdf(src, from_page=0, to_page=after_page - 1)
            new_page = dst.new_page(width=rect.width, height=rect.height)
            new_page.insert_image(new_page.rect, stream=png)
            if with_page_number:
                # 页码放在页边距以内（距底边约 50pt > 15mm 下限），避免引入
                # 与解析状态无关的 layout_check 边距命中
                new_page.insert_text((300, 790), str(after_page + 1), fontsize=11)
            dst.insert_pdf(src, from_page=after_page, to_page=total - 1)
            dst.save(str(out_path), garbage=4, deflate=True)
        finally:
            dst.close()
    finally:
        src.close()
    return after_page + 1


def build_scanned_mid_pdf(source_pdf: Path, out_path: Path, page_idx: int) -> None:
    """把第 page_idx 页（0-based）整页栅格化成一张图 → 该页变为图像页。
    解析时该页 digital 字符为 0 → OCR 关闭时归为 scanned_no_text（读取不完整），其余各页数字文本保持不变。
    """
    import fitz

    src = fitz.open(str(source_pdf))
    try:
        page = src[page_idx]
        pix = page.get_pixmap(dpi=150)
        png = pix.tobytes("png")
        rect = page.rect
        dst = fitz.open()
        try:
            for idx in range(len(src)):
                if idx == page_idx:
                    new_page = dst.new_page(width=rect.width, height=rect.height)
                    new_page.insert_image(new_page.rect, stream=png)
                else:
                    dst.insert_pdf(src, from_page=idx, to_page=idx)
            dst.save(str(out_path), garbage=4, deflate=True)
        finally:
            dst.close()
    finally:
        src.close()


def build_scanned_pagenum_pdf(
    source_pdf: Path, out_path: Path, page_idx: int, page_number_text: str = "2"
) -> None:
    """R02 缺陷场景：整页扫描图 + 一个数字页码。
    该页图像覆盖接近 100%、数字字符仅 1 个 → scanned_minimal_text（未读取），正式流程必须判失败，而不是因“有文字”当作已读取。
    """
    import fitz

    src = fitz.open(str(source_pdf))
    try:
        page = src[page_idx]
        pix = page.get_pixmap(dpi=150)
        png = pix.tobytes("png")
        rect = page.rect
        dst = fitz.open()
        try:
            for idx in range(len(src)):
                if idx == page_idx:
                    new_page = dst.new_page(width=rect.width, height=rect.height)
                    new_page.insert_image(new_page.rect, stream=png)
                    # 页码放在页边距以内（y=790 → 距底边约 50pt > 15mm 下限），
                    # 避免引入与 R02 场景无关的 layout_check 边距命中
                    new_page.insert_text((300, 790), page_number_text, fontsize=11)
                else:
                    dst.insert_pdf(src, from_page=idx, to_page=idx)
            dst.save(str(out_path), garbage=4, deflate=True)
        finally:
            dst.close()
    finally:
        src.close()


def build_damaged_pdf(source_pdf: Path, out_path: Path) -> None:
    """损坏文档样本：带 PDF 头但正文非法（MuPDF 无法解析）；不能按比例截断——实测部分截断会被 MuPDF 修复后“成功打开”，从而不再是损坏样本。
    “正确拒绝”本身就是期望结果（R10：不能强迫所有样本都 done/pass）。
    """
    out_path.write_bytes(
        b"%PDF-1.4\n"
        b"% corpus damaged sample: invalid body (deterministic)\n"
        b"this is intentionally not a valid pdf body\n"
        + b"\x00" * 64
        + b"\n%%EOF\n"
    )


def build_pdf_samples(clean_pdf: Path, out_dir: Path, ruleset: str = "campus") -> list[dict]:
    """基于干净 PDF 派生解析状态类样本，返回 manifest 条目（含逐页/任务层期望）。"""
    entries: list[dict] = []
    prefix = "" if ruleset == "campus" else f"{ruleset}_"

    blank_path = out_dir / f"{prefix}pdf_blank_mid.pdf"
    build_blank_mid_pdf(clean_pdf, blank_path, after_page=2)
    entries.append({
        "file": blank_path.name,
        "ruleset": ruleset,
        "class": "parse_status",
        "kind": "blank_page",
        "note": "干净 PDF 第 2 页后插入真正空白页 → page_status=empty（正常空白页，非失败），"
                "确定性 error/warning 0 命中（page_number_check 在 campus 为 info 级，不计入本清单）",
        "expected": [],
        "expected_pages": {3: "empty"},
        "expected_outcome": DOC_OK_OUTCOME,
    })

    scan_page = build_scanned_insert_pdf(
        clean_pdf, out_dir / f"{prefix}pdf_scanned_mid.pdf", after_page=2
    )
    entries.append({
        "file": f"{prefix}pdf_scanned_mid.pdf",
        "ruleset": ruleset,
        "class": "parse_status",
        "kind": "scanned_page",
        "note": f"第 2 页后插入一页扫描件（图像、无数字文字）→ 第 {scan_page} 页 "
                "page_status=scanned_no_text（读取不完整，正式任务失败）",
        "expected": [],
        "expected_pages": {scan_page: "scanned_no_text"},
        "expected_outcome": UNREAD_OUTCOME,
    })

    pagenum_page = build_scanned_insert_pdf(
        clean_pdf,
        out_dir / f"{prefix}pdf_scanned_pagenum.pdf",
        after_page=2,
        with_page_number=True,
    )
    entries.append({
        "file": f"{prefix}pdf_scanned_pagenum.pdf",
        "ruleset": ruleset,
        "class": "parse_status",
        "kind": "scanned_page_with_number",
        "note": f"R02 原始缺陷场景：第 2 页后插入扫描页并印一个数字页码“{pagenum_page}”"
                f"（text_chars=1）→ 第 {pagenum_page} 页 scanned_minimal_text；"
                "正式流程必须判失败，不得因“有文字”当作已读取",
        "expected": [],
        "expected_pages": {pagenum_page: "scanned_minimal_text"},
        "expected_outcome": UNREAD_OUTCOME,
    })

    damaged_path = out_dir / f"{prefix}pdf_damaged.pdf"
    build_damaged_pdf(clean_pdf, damaged_path)
    entries.append({
        "file": damaged_path.name,
        "ruleset": ruleset,
        "class": "parse_status",
        "kind": "damaged",
        "note": "损坏文档样本（确定的非法正文）：解析层必须拒绝；正确拒绝即为通过",
        "expected": [],
        "expected_parse_error": True,
    })
    return entries


PDF_SAMPLE_KEYS = (
    "pdf_blank_mid",
    "pdf_scanned_mid",
    "pdf_scanned_pagenum",
    "pdf_damaged",
)

# 作者口径的任务层期望（不是从实际输出反填）：三个规则集下签章/附件/页码都会产生 manual_required → 结论必为 incomplete、
# review_complete 必为 False（不得判 pass）；存在未读取页时，正式流程在完整性校验即失败（不会进入规则结算）。
DOC_OK_OUTCOME = {
    "validation": "ok",
    "conclusion": "incomplete",
    "review_complete": False,
}
UNREAD_OUTCOME = {
    "validation": "failed",
    "review_complete": False,
}
# 规则集 → 干净模板（campus 复用已验证的校级模板；dachuang/nsfc 由
# build_templates.py 生成并逐份校准为 0 确定性命中）
RULESET_TEMPLATES = {
    "dachuang": EVAL_DIR / "corpus" / "templates" / "dachuang_clean.docx",
    "nsfc": EVAL_DIR / "corpus" / "templates" / "nsfc_clean.docx",
}
SUPPORTED_RULESETS = ("campus", "dachuang", "nsfc")
# 与规则集无关的 mutation（模板都有：一级标题、正文段落、联系电话、填表日期、页边距）
RULESET_MUTATIONS = (
    "font_check_hit",
    "required_fields_hit",
    "field_format_hit",
    "heading_numbering_hit",
    "layout_margin_hit",
)
# 扩展规则集（dachuang/nsfc）的规则特定命中样本；campus 已有等价注入器样本
NON_CAMPUS_MUTATIONS = ("date_check_hit", "budget_check_hit")
# required_fields_hit 需要删除“该规则集确实要求”的字段（作者口径，非实测反填）
RULESET_REQUIRED_FIELD_TARGETS = {
    "campus": ("填表日期", "申请日期"),
    "dachuang": ("指导教师", "指导老师"),
    "nsfc": ("申请代码",),
}


def ruleset_base_docx(ruleset: str) -> Path:
    """取得（必要时生成）规则集对应的干净模板。"""
    if ruleset == "campus":
        return clean_base_docx()
    target = RULESET_TEMPLATES[ruleset]
    if not target.exists():
        import build_templates  # noqa: E402 —— 同目录模块

        target.parent.mkdir(parents=True, exist_ok=True)
        if ruleset == "dachuang":
            build_templates.build_dachuang_template(target)
        elif ruleset == "nsfc":
            build_templates.build_nsfc_template(
                build_templates.ensure_campus_template(), target
            )
        else:
            raise ValueError(f"未知规则集：{ruleset}")
    return target
# 场景规则集：无可执行规则 / 检查器抛异常（用于任务层“正确拒绝”验收）
EMPTY_RULESET_TEXT = "ruleset: empty\nname: 空规则集\nrules: []\n"
FAILING_RULESET_TEXT = (
    "ruleset: failing\nname: 规则异常\nrules:\n"
    "- id: X1\n  type: layout_check\n  severity: warning\n"
    "  params:\n    body:\n      min_size: not-a-number\n"
)


def _sample_stem(ruleset: str, kind: str) -> str:
    """campus 保持历史文件名；其它规则集加前缀（同一目录内不冲突）。"""
    if ruleset == "campus":
        return kind
    return f"{ruleset}_{kind}"


def available_samples(rulesets: tuple[str, ...] = ("campus",)) -> list[dict]:
    """全部可选样本注册表（供 --list 与 --include/--exclude 白名单匹配）。"""
    items: list[dict] = []
    for ruleset in rulesets:
        prefix = "" if ruleset == "campus" else f"{ruleset}_"
        clean_kind = "campus_clean" if ruleset == "campus" else "clean"
        items.append({
            "key": f"{prefix}{clean_kind}",
            "ruleset": ruleset,
            "class": "clean",
            "kind": "clean",
            "note": f"{ruleset} 干净基线（0 确定性命中；含人工确认项）",
        })
        for number, name, note in CORPUS_V1:
            if ruleset != "campus":
                continue  # 注入器为 campus 章节结构定制，其它规则集暂不套用
            items.append({"key": name, "ruleset": ruleset, "class": "hit",
                          "kind": f"injector#{number}", "note": note})
        for name in RULESET_MUTATIONS + tuple(
            entry[0] for entry in RULESET_EXTRA_MUTATIONS.get(ruleset, [])
        ) + (NON_CAMPUS_MUTATIONS if ruleset != "campus" else ()):
            items.append({
                "key": f"{prefix}{name}",
                "ruleset": ruleset,
                "class": "hit",
                "kind": "mutation",
                "note": MUTATION_NOTES[name],
            })
        for key, kind, note in (
            ("pdf_blank_mid", "blank_page",
             "干净 PDF 插入空白页（p3=empty，任务 validation=ok）"),
            ("pdf_scanned_mid", "scanned_page",
             "整页栅格化（scanned_no_text → 未读取，正式任务失败）"),
            ("pdf_scanned_pagenum", "scanned_page_with_number",
             "扫描页 + 数字页码“2”（scanned_minimal_text → 任务失败）"),
            ("pdf_damaged", "damaged", "损坏 PDF：解析层拒绝即通过"),
        ):
            items.append({
                "key": f"{prefix}{key}",
                "ruleset": ruleset,
                "class": "parse_status",
                "kind": kind,
                "note": note,
            })
    for item in items:
        ext = ".pdf" if item["kind"] in {
            "blank_page", "scanned_page", "scanned_page_with_number", "damaged",
        } else ".docx"
        item["file"] = f"{item['key']}{ext}"
    return items


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(EVAL_DIR / "corpus" / "output"),
                        help="样本输出目录")
    parser.add_argument("--skip-check", action="store_true", help="只生成，不自动自检")
    parser.add_argument("--include", action="append", default=[],
                        help="白名单：只生成这些样本（文件主名，逗号分隔可重复）")
    parser.add_argument("--exclude", action="append", default=[],
                        help="黑名单：从（白名单或全量）中排除这些样本")
    parser.add_argument("--list", action="store_true", help="列出可用样本后退出，不生成")
    parser.add_argument("--ruleset", action="append", default=[],
                        help="规则集：campus/dachuang/nsfc 或 all（可逗号分隔/重复）；默认 campus")
    args = parser.parse_args()

    def _tokens(values: list[str]) -> set[str]:
        tokens: set[str] = set()
        for value in values:
            for token in value.split(","):
                token = token.strip().lower()
                if token:
                    tokens.add(token)
        return tokens

    ruleset_tokens = _tokens(args.ruleset)
    if not ruleset_tokens or ruleset_tokens & {"all", "*"}:
        selected_rulesets: tuple[str, ...] = SUPPORTED_RULESETS if ruleset_tokens else ("campus",)
    else:
        unknown_rulesets = ruleset_tokens - set(SUPPORTED_RULESETS)
        if unknown_rulesets:
            raise SystemExit(
                f"未知规则集：{sorted(unknown_rulesets)}；支持 {SUPPORTED_RULESETS} 或 all"
            )
        selected_rulesets = tuple(r for r in SUPPORTED_RULESETS if r in ruleset_tokens)

    available = {item["key"]: item for item in available_samples(selected_rulesets)}
    if args.list:
        print("可用样本（--include / --exclude 用 file 主名，不区分大小写）：")
        for item in available.values():
            print(f"  {item['key']:<34} [{item['ruleset']}/{item['class']}/{item['kind']:<10}] "
                  f"{item['note']}")
        return 0

    include_tokens = _tokens(args.include)
    exclude_tokens = _tokens(args.exclude)
    known = set(available)
    unknown = (include_tokens | exclude_tokens) - known
    if unknown:
        raise SystemExit(
            f"未知样本名：{sorted(unknown)}。"
            "可用列表：python eval/corpus/generate_samples.py --list"
        )
    selected = (include_tokens or known) - exclude_tokens
    want = lambda key: key.lower() in selected  # noqa: E731
    selection_label = "all" if not (include_tokens or exclude_tokens) else ",".join(sorted(selected))
    run_checks = not args.skip_check

    known_files = {item["file"] for item in available.values()}
    if not selected:
        print("未选择任何样本（--list 查看可用清单）；输出目录未做任何改动。")
        return 0

    from output_safety import (  # noqa: E402
        OutputSafetyError,
        cleanup_staging,
        create_staging,
        preflight,
        publish,
        remove_stale_staging_dirs,
    )
    try:
        resolved, state, old_files = preflight(args.out, known_files)
    except OutputSafetyError as exc:
        print(f"输出目录检查未通过：{exc}")
        return 2
    display_dir = resolved
    # 生成器自有目录：先清掉历史崩溃遗留的暂存子目录（仅限有受管标记的目录）
    if state == "owned":
        remove_stale_staging_dirs(resolved)

    manifest: list[dict] = []
    out_dir: Path | None = None
    try:
        # 构建与自检都在同卷暂存子目录完成；任何失败都不触碰上一份有效产物
        out_dir = create_staging(resolved)

        for ruleset in selected_rulesets:
            base = ruleset_base_docx(ruleset)
            stem = lambda kind: _sample_stem(ruleset, kind)  # noqa: E731

            # 1) 干净样本
            if want(stem("campus_clean" if ruleset == "campus" else "clean")):
                clean_dst = out_dir / f"{stem('campus_clean' if ruleset == 'campus' else 'clean')}.docx"
                shutil.copy2(base, clean_dst)
                manifest.append({
                    "file": clean_dst.name,
                    "ruleset": ruleset,
                    "class": "clean",
                    "note": f"{ruleset} 干净模板（0 确定性 error/warning；含人工确认项）",
                    "expected": [],
                    "expected_outcome": DOC_OK_OUTCOME,
                    # 人工确认项验收：干净文档也必须有 manual_required（附件/签章/页码），
                    # 结论因此为 incomplete 而非 pass
                    "expected_manual_min": 1,
                })

            # 2) 注入样本（campus 专用注入器，其它规则集暂不套用）
            if ruleset == "campus":
                tmp_work = Path(tempfile.mkdtemp(prefix="corpus-gen-"))
                try:
                    for number, name, note in CORPUS_V1:
                        if not want(name):
                            continue
                        sample_docx = tmp_work / f"{name}.docx"
                        inject_errors(base, [number], sample_docx)
                        dst = out_dir / f"{name}.docx"
                        shutil.copy2(sample_docx, dst)
                        manifest.append({
                            "file": dst.name,
                            "ruleset": ruleset,
                            "class": "hit",
                            "injector": number,
                            "note": note,
                            "expected": expectations_for(number),
                            "expected_outcome": DOC_OK_OUTCOME,
                        })
                finally:
                    shutil.rmtree(tmp_work, ignore_errors=True)

            # 2b) 与规则集无关的 mutation 样本
            tmp_mut = Path(tempfile.mkdtemp(prefix="corpus-mut-"))
            try:
                extra_mutations = list(RULESET_EXTRA_MUTATIONS.get(ruleset, []))
                if ruleset != "campus":
                    extra_mutations += [
                        (name, MUTATIONS[name][0], MUTATIONS[name][1])
                        for name in NON_CAMPUS_MUTATIONS
                    ]
                for name, mutator, expected in [
                    (name, MUTATIONS[name][0], MUTATIONS[name][1])
                    for name in RULESET_MUTATIONS
                ] + extra_mutations:
                    key = stem(name)
                    if not want(key):
                        continue
                    if name == "required_fields_hit":
                        targets = RULESET_REQUIRED_FIELD_TARGETS[ruleset]
                        mutator = partial(_mut_missing_field, candidates=targets)
                    copy_path = tmp_mut / f"{key}.docx"
                    shutil.copy2(base, copy_path)
                    mutator(copy_path)
                    dst = out_dir / f"{key}.docx"
                    shutil.copy2(copy_path, dst)
                    manifest.append({
                        "file": dst.name,
                        "ruleset": ruleset,
                        "class": "hit",
                        "kind": "mutation",
                        "note": MUTATION_NOTES[name],
                        "expected": expected,
                        "expected_outcome": DOC_OK_OUTCOME,
                    })
            finally:
                shutil.rmtree(tmp_mut, ignore_errors=True)

            # 2c) PDF 解析状态类反例样本（先转出干净 PDF 作底稿，转换放临时目录）
            if any(want(stem(key)) for key in (
                "pdf_blank_mid", "pdf_scanned_mid", "pdf_scanned_pagenum", "pdf_damaged",
            )):
                tmp_base = Path(tempfile.mkdtemp(prefix="corpus-base-"))
                try:
                    from app.services.parser.converter import convert_docx_to_pdf  # noqa: E402
                    base_copy = tmp_base / f"{stem('clean')}.docx"
                    shutil.copy2(base, base_copy)
                    clean_pdf = convert_docx_to_pdf(base_copy)
                    for entry in build_pdf_samples(clean_pdf, out_dir, ruleset):
                        if want(Path(entry["file"]).stem):
                            manifest.append(entry)
                finally:
                    shutil.rmtree(tmp_base, ignore_errors=True)

        # 3) manifest 落盘（在暂存目录内）
        manifest_path = out_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps({"generated_at": "corpus-v8", "seed": "n/a(确定性注入)",
                        "rulesets": list(selected_rulesets),
                        "expected_scope": "规则层仅 error/warning（manual_required 不计）；"
                                          "另含逐页状态与任务层结论期望",
                        "selection": selection_label,
                        "note": "自检运行固定 OCR_ENABLED=0；pdf_* 为解析状态样本（直接 PDF）；"
                                "自检分解析/规则/任务三层，任务层复用正式流程的完整性校验与结算",
                        "samples": manifest},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 4) 分层自检（解析层 / 规则层 / 任务层），随后清掉暂存目录内不属于本次
        #    清单的残留（docx 转换的 .pdf 副产物等 —— 暂存目录归本工具所有）
        lines: list[str] = []
        lines.append(f"输出目录：{display_dir}")
        lines.append(f"本次选择：{selection_label}（共 {len(manifest)} 份）")
        all_ok = True
        clean_payload: dict | None = None
        if run_checks:
            for entry in manifest:
                payload = run_layers(out_dir / entry["file"], entry.get("ruleset", "campus"))
                if clean_payload is None and entry.get("class") == "clean":
                    clean_payload = payload  # 场景检查复用第一个干净样本的 IR
                problems = compare_expectations(entry, payload)
                all_ok &= not problems
                task = payload.get("task", {})
                parse = payload.get("parse", {})
                lines.append(
                    f"[{'OK ' if not problems else 'FAIL'}] {entry['file']}  "
                    f"解析={'ok' if parse.get('ok') else 'rejected'} "
                    f"命中={len(payload.get('rules', {}).get('hits', []))} "
                    f"人工={len(payload.get('rules', {}).get('manual', []))} "
                    f"任务={task.get('validation')}/{task.get('conclusion')}/"
                    f"complete={task.get('review_complete')}"
                )
                for problem in problems:
                    lines.append(f"      FAIL {problem}")
            # R10 验收：空规则集 / 规则异常 场景（复用同一份干净样本 IR，无需额外转换）
            if clean_payload is not None:
                for scenario, problems in run_scenarios(clean_payload):
                    all_ok &= not problems
                    lines.append(f"[{'OK ' if not problems else 'FAIL'}] 场景：{scenario}")
                    for problem in problems:
                        lines.append(f"      FAIL {problem}")
            summary = "自检全部通过" if all_ok else "自检存在 FAIL（请勿把失败样本当验收基线）"
        else:
            lines.append("--skip-check：本次未执行规则自检；期望见 manifest.json（供人工对照）")
            summary = "自检已跳过（--skip-check）"
        keep = {entry["file"] for entry in manifest} | {"manifest.json", "selfcheck.txt"}
        for item in out_dir.iterdir():
            if item.is_file() and item.name not in keep:
                item.unlink()

        lines.append(summary)
        (out_dir / "selfcheck.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("\n".join(lines))
        if not all_ok:
            cleanup_staging(out_dir)
            print(f"自检未通过：{display_dir} 中的上一份有效产物已保留（本次产物未发布）。")
            return 1
    except Exception as exc:  # noqa: BLE001 —— 转换/解析/生成任何一步失败都不得破坏旧产物
        if out_dir is not None:
            cleanup_staging(out_dir)
        print(f"生成/自检失败，已保留 {display_dir} 中的上一份有效产物：{exc}")
        return 1

    # 5) 发布：只替换能证明属于本工具的旧产物；无关文件一律保留
    try:
        produced = [entry["file"] for entry in manifest] + ["manifest.json", "selfcheck.txt"]
        publish(
            out_dir,
            resolved,
            old_files,
            produced,
            selection_label,
            note=f"previous_state={state}",
        )
    except OutputSafetyError as exc:
        cleanup_staging(out_dir)
        print(f"发布中止（{display_dir} 中的上一份有效产物已保留）：{exc}")
        return 1
    print(f"\n已生成 {len(manifest)} 份样本（{sum(1 for e in manifest if e['file'].endswith('.docx'))} docx + "
          f"{sum(1 for e in manifest if e['file'].endswith('.pdf'))} pdf）：打开 {display_dir} 下的文件即可人工验证；")
    print("期望与结果见 manifest.json 与 selfcheck.txt（该目录已标记为本工具的受管目录）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
