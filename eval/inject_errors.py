"""Inject reproducible form-review errors into the clean sample DOCX.

Usage: python inject_errors.py --errors 3,5,9 --out injected.docx
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Callable

from docx import Document
from docx.document import Document as DocumentType
from docx.enum.text import WD_BREAK


DEFAULT_SOURCE = Path(__file__).resolve().parent / "samples" / "sample_proposal.docx"
# 注入 #5 的正文身份信息。名称取值须与封面完全一致（"张三"），否则
# cross_field_consistency 会判定「项目负责人」取值不一致；单位等附加信息
# 放独立段落且不以别名/姓名开头，避免被当成该字段的取值。
IDENTITY_TEXT = "申请人：张三"
IDENTITY_DETAIL = "张三为华南理工大学在职教师，研究方向为智能交通。"
PROJECT_TITLE = "基于深度学习的城市交通流预测方法研究"
CHANGED_PROJECT_TITLE = "基于深度学习的城市交通流预测方法研宄"

GROUND_TRUTH = {
    # 1：删除“研究方法”节 → 正文章节 一、二、四…（三 缺失）→ heading_numbering
    # 会确定性报 1 条“编号不连续”（2 后出现 四、），属真实副作用，需标注。
    1: {"checker": "required_sections", "severity": "error",
        "extra": [{"checker": "heading_numbering", "severity": "warning", "instances": 1}]},
    2: {"checker": "page_limit", "severity": "error"},
    3: {"checker": "budget_check", "severity": "error"},
    # 注入 #4（管理费 8%→20%）会同时造成两条独立真命中：①占比超上限；②分项合计
    # 因此变 33.6 与总额 30 勾稽不平。实例级口径下期望数必须为 2。
    # （与 #3 不同现：见 run_eval._sample_errors 的互斥组合约束。）
    4: {"checker": "budget_check", "severity": "error", "instances": 2},
    5: {"checker": "anonymity_check", "severity": "error"},
    6: {"checker": "figure_table_numbering", "severity": "warning"},
    # 7：交换“二、研究目标”与“三、研究方法”两节但保留编号 → 标题顺序 一、三、二、四…
    # heading_numbering 确定性报 3 条“编号不连续”，属真实副作用，需标注。
    7: {"checker": "section_order", "severity": "warning",
        "extra": [{"checker": "heading_numbering", "severity": "warning", "instances": 3}]},
    8: {"checker": "cross_field_consistency", "severity": "warning"},
    # 注入 #9（调换起止时间）会同时触发两条独立的 date_check 问题：
    # ①“开始不早于结束”异常；②与指南要求 24 个月不一致。两者都是真实命中，
    # 实例级口径下期望数必须为 2，否则第二条会被误算成 FP。
    9: {"checker": "date_check", "severity": "warning", "instances": 2},
    10: {"checker": "signature_page", "severity": "error"},
}


def expected_issues_for(
    error_numbers: list[int], *, include_anonymity: bool = True
) -> list[dict[str, str]]:
    """按注入编号返回历史/实例口径期望（同 (checker,severity) 计数合并）。
    默认 1 实例；`instances` 多条真命中，`extra` 额外真实副作用；身份口径见
    :func:`expected_instances_for`。
    """
    merged: dict[tuple[str, str], int] = {}
    for number in error_numbers:
        item = GROUND_TRUTH[number]
        if item["checker"] == "anonymity_check" and not include_anonymity:
            continue
        key = (item["checker"], item["severity"])
        merged[key] = merged.get(key, 0) + int(item.get("instances", 1))
        for extra in item.get("extra", []):
            extra_key = (extra["checker"], extra["severity"])
            merged[extra_key] = merged.get(extra_key, 0) + int(extra.get("instances", 1))
    expected: list[dict[str, str]] = []
    for (checker, severity), count in merged.items():
        expected.extend([{"checker": checker, "severity": severity}] * count)
    return expected


# ── R09：实例身份标注（target 目标字段 + anchors 证据锚点，逐条 why 写明依据）──
# 锚点只能来自注入语义与检查器消息模板契约，不得读取实际输出反填；锚点要求「全部出现」
# 而非 message 全等（允许措辞变化）；page 仅作位置提示，不参与配对。
INSTANCE_ANNOTATIONS: dict[int, list[dict]] = {
    1: [
        {"checker": "required_sections", "severity": "error",
         "target": "章节「研究方法与技术路线」",
         "anchors": ["研究方法"],
         "why": "注入①删除整节；required_sections 消息模板『缺少必填章节：{aliases[0]}』，"
                "锚点取自被删章节名（别名首项包含「研究方法」）"},
        {"checker": "heading_numbering", "severity": "warning",
         "target": "标题编号连续性（删除整节的副作用）",
         "anchors": ["标题编号不连续"],
         "why": "删掉第三节后编号跳号；heading_numbering 消息模板『标题编号不连续：A 后出现 B』"},
    ],
    2: [
        {"checker": "page_limit", "severity": "error",
         "target": "总页数上限",
         "anchors": ["总页数"],
         "why": "注入②插入 8 页；page_limit 消息模板『总页数超出限制（N页 > M页）』"},
    ],
    3: [
        {"checker": "budget_check", "severity": "error",
         "target": "预算表勾稽（合计 30.0→25.0）",
         "anchors": ["分项合计"],
         "why": "注入③只改合计行；budget_check 勾稽分支消息模板『预算勾稽不平：分项合计 X ≠ 总额 Y』"},
    ],
    4: [
        {"checker": "budget_check", "severity": "error",
         "target": "预算表「管理费」占比",
         "anchors": ["管理费", "占比"],
         "why": "注入④把管理费 2.4→6.0（超占比上限）；消息模板『「{category}」占比 x% 超过上限 y%』"},
        {"checker": "budget_check", "severity": "error",
         "target": "预算表勾稽（管理费变大导致分项合计≠总额）",
         "anchors": ["分项合计"],
         "why": "同一次注入的第二条独立真命中；消息模板同③（与①的锚点互斥，不会互相顶替）"},
    ],
    5: [
        {"checker": "anonymity_check", "severity": "error",
         "target": "正文身份泄露（申请人：张三）",
         "anchors": ["张三"],
         "why": "注入⑤插入『申请人：张三』与身份说明段；LLM 检查器的 evidence 必须逐字引用原文"},
    ],
    6: [
        {"checker": "figure_table_numbering", "severity": "warning",
         "target": "图2 题注",
         "anchors": ["图2"],
         "why": "注入⑥只删图2 题注（正文仍引用）；消息模板『图题注缺失：{missing_captions}』"},
    ],
    7: [
        {"checker": "section_order", "severity": "warning",
         "target": "章节顺序（二、研究目标 与 三、研究方法 互换）",
         "anchors": ["章节顺序"],
         "why": "注入⑦交换两节并保留编号；section_order 消息模板"
                "『章节顺序不符合预期：「{pattern}」应在第N位』"},
        *[
            {"checker": "heading_numbering", "severity": "warning",
             "target": f"标题编号连续性（交换副作用 #{index}）",
             "anchors": ["标题编号不连续"],
             "why": "交换后编号序列出现 3 处跳号；消息模板同①的副作用锚点"}
            for index in (1, 2, 3)
        ],
    ],
    8: [
        {"checker": "cross_field_consistency", "severity": "warning",
         "target": "封面与正文项目名称取值不一致（研究→研宄）",
         "anchors": ["不一致"],
         "why": "注入⑧只改封面标题一字；消息模板『「{field_name}」在多处取值不一致：…』"},
    ],
    9: [
        {"checker": "date_check", "severity": "warning",
         "target": "研究期限起止顺序（结束年份改 2025）",
         "anchors": ["不早于结束日期"],
         "why": "注入⑨把结束 2028年8月→2025年8月；消息模板"
                "『研究期限日期异常：开始日期 Y年M月 不早于结束日期』"},
        {"checker": "date_check", "severity": "warning",
         "target": "研究期限与指南月数不一致",
         "anchors": ["与指南要求"],
         "why": "同一次注入派生的第二条：消息模板『研究期限 N 个月，与指南要求的 M 个月不一致』"
                "（与①锚点互斥）"},
    ],
    10: [
        {"checker": "signature_page", "severity": "error",
         "target": "签字盖章页",
         "anchors": ["签字盖章"],
         "why": "注入⑩删除签字盖章页；消息模板『末页缺少签字盖章关键字：{missing}』"},
    ],
}


def expected_instances_for(
    error_numbers: list[int], *, include_anonymity: bool = True
) -> list[dict]:
    """按注入编号返回**带实例身份**的期望（R09 身份口径）。
    身份 = checker + severity + target + anchors；实例数须与
    :func:`expected_issues_for` 一致（有测试守护，防止两套口径漂移）。
    """
    instances: list[dict] = []
    for number in sorted(set(error_numbers)):
        for item in INSTANCE_ANNOTATIONS.get(number, []):
            if item["checker"] == "anonymity_check" and not include_anonymity:
                continue
            instances.append(dict(item))
    return instances


def annotation_evidence(error_numbers: list[int]) -> list[dict]:
    """标注依据清单（人工核对用）：注入编号 → 身份、锚点、理由。"""
    rows: list[dict] = []
    for number in sorted(set(error_numbers)):
        for item in INSTANCE_ANNOTATIONS.get(number, []):
            rows.append({
                "injector": number,
                "checker": item["checker"],
                "severity": item["severity"],
                "target": item["target"],
                "anchors": list(item["anchors"]),
                "locatable": item.get("locatable", True),
                "why": item["why"],
            })
    return rows


def _remove_paragraph(paragraph) -> None:
    parent = paragraph._element.getparent()
    if parent is not None:
        parent.remove(paragraph._element)


def _replace_in_paragraph(paragraph, old: str, new: str) -> bool:
    if old not in paragraph.text:
        return False
    for run in paragraph.runs:
        if old in run.text:
            run.text = run.text.replace(old, new, 1)
            return True
    full_text = paragraph.text.replace(old, new, 1)
    paragraph.clear()
    paragraph.add_run(full_text)
    return True


def inject_delete_research_method(doc: DocumentType) -> None:
    """① Delete the complete research-method section."""
    paragraphs = list(doc.paragraphs)
    start = next(i for i, p in enumerate(paragraphs) if "研究方法与技术路线" in p.text)
    end = next(
        (i for i in range(start + 1, len(paragraphs))
         if paragraphs[i].style.name == "Heading 1"),
        len(paragraphs),
    )
    for paragraph in paragraphs[start:end]:
        _remove_paragraph(paragraph)


def inject_exceed_page_limit(doc: DocumentType) -> None:
    """② Copy a paragraph across added pages until the campus page limit is exceeded."""
    source_text = next(p.text for p in doc.paragraphs if p.text.startswith("城市交通拥堵问题"))
    signature = next((p for p in doc.paragraphs if "签 字 盖 章 页" in p.text), None)
    insertion_point = signature._p if signature is not None else None
    if signature is not None:
        paragraphs = list(doc.paragraphs)
        signature_index = next(
            index for index, paragraph in enumerate(paragraphs)
            if paragraph._p is signature._p
        )
        for index in range(signature_index - 1, -1, -1):
            if paragraphs[index]._p.xpath(".//w:br[@w:type='page']"):
                insertion_point = paragraphs[index]._p
                break
    for _ in range(8):
        page_break = doc.add_paragraph()
        page_break.add_run().add_break(WD_BREAK.PAGE)
        copied = doc.add_paragraph(source_text)
        if insertion_point is not None:
            insertion_point.addprevious(page_break._p)
            insertion_point.addprevious(copied._p)


def inject_wrong_budget_total(doc: DocumentType) -> None:
    """③ Change the budget total to an arithmetically incorrect value."""
    for table in doc.tables:
        for row in table.rows:
            if "合计" not in " ".join(cell.text for cell in row.cells):
                continue
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    if _replace_in_paragraph(paragraph, "30.0", "25.0"):
                        return
    raise RuntimeError("Budget total 30.0 was not found")


def inject_management_fee_20_percent(doc: DocumentType) -> None:
    """④ Set the management fee to 20 percent of the total budget."""
    for table in doc.tables:
        for row in table.rows:
            if "管理费" not in " ".join(cell.text for cell in row.cells):
                continue
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    if _replace_in_paragraph(paragraph, "2.4", "6.0"):
                        return
    raise RuntimeError("Management fee 2.4 was not found")


def inject_identity_disclosure(doc: DocumentType) -> None:
    """⑤ Insert applicant identity information into the body."""
    heading = next(p for p in doc.paragraphs if p.style.name == "Heading 1")
    paragraph = doc.add_paragraph(IDENTITY_TEXT)
    detail = doc.add_paragraph(IDENTITY_DETAIL)
    heading._p.addnext(detail._p)
    heading._p.addnext(paragraph._p)


def inject_delete_figure2_caption(doc: DocumentType) -> None:
    """⑥ Delete only the Figure 2 caption line, leaving its body reference."""
    paragraph = next(p for p in doc.paragraphs if p.text.strip().startswith("图2 "))
    _remove_paragraph(paragraph)


def inject_swap_level1_sections(doc: DocumentType) -> None:
    """⑦ Swap two adjacent level-one sections while preserving their content."""
    body = doc.element.body
    children = list(body)
    headings = [p for p in doc.paragraphs if p.style.name == "Heading 1"]
    first = next(p for p in headings if "研究目标与内容" in p.text)
    second = next(p for p in headings if "研究方法与技术路线" in p.text)
    after_second = next(p for p in headings if "预期成果与创新点" in p.text)
    first_index = children.index(first._p)
    second_index = children.index(second._p)
    end_index = children.index(after_second._p)
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


def inject_cover_title_typo(doc: DocumentType) -> None:
    """⑧ Change one character in the cover project title only."""
    for paragraph in doc.paragraphs:
        if _replace_in_paragraph(paragraph, PROJECT_TITLE, CHANGED_PROJECT_TITLE):
            return
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    if _replace_in_paragraph(paragraph, PROJECT_TITLE, CHANGED_PROJECT_TITLE):
                        return
    raise RuntimeError("Cover project title was not found")


def inject_end_before_start(doc: DocumentType) -> None:
    """⑨ Make the research-period end date earlier than its start date."""
    for paragraph in doc.paragraphs:
        if "研究期限" in paragraph.text and _replace_in_paragraph(
            paragraph, "2028年8月", "2025年8月"
        ):
            return
    raise RuntimeError("Research-period end date was not found")


def inject_delete_signature_page(doc: DocumentType) -> None:
    """⑩ Delete the signature and seal page, including its leading page break."""
    paragraphs = list(doc.paragraphs)
    title_index = next(i for i, p in enumerate(paragraphs) if "签 字 盖 章 页" in p.text)
    start_index = title_index
    for index in range(title_index - 1, -1, -1):
        if paragraphs[index]._p.xpath(".//w:br[@w:type='page']"):
            start_index = index
            break
    body = doc.element.body
    start_element = paragraphs[start_index]._p
    children = list(body)
    element_index = children.index(start_element)
    for child in children[element_index:]:
        if child.tag.endswith("}sectPr"):
            continue
        body.remove(child)


INJECTORS: dict[int, Callable[[DocumentType], None]] = {
    1: inject_delete_research_method,
    2: inject_exceed_page_limit,
    3: inject_wrong_budget_total,
    4: inject_management_fee_20_percent,
    5: inject_identity_disclosure,
    6: inject_delete_figure2_caption,
    7: inject_swap_level1_sections,
    8: inject_cover_title_typo,
    9: inject_end_before_start,
    10: inject_delete_signature_page,
}


def inject_errors(
    source_path: str | Path,
    error_numbers: list[int],
    output_path: str | Path,
) -> tuple[Path, list[dict[str, str]]]:
    source = Path(source_path)
    output = Path(output_path)
    if not source.exists():
        raise FileNotFoundError(f"Source DOCX not found: {source}")
    invalid = sorted(set(error_numbers) - set(INJECTORS))
    if invalid:
        raise ValueError(f"Unknown error numbers: {invalid}")

    doc = Document(str(source))
    execution_order = sorted(error_numbers, key=lambda number: 0 if number == 10 else 1)
    for number in execution_order:
        INJECTORS[number](doc)
    output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output))

    ground_truth = expected_issues_for(error_numbers)
    gt_path = output.with_suffix(".ground_truth.json")
    gt_path.write_text(json.dumps(ground_truth, ensure_ascii=False, indent=2), encoding="utf-8")

    # R09：身份标注单独记录（人工可核对的样本依据），与旧的扁平期望文件并存
    instances_path = output.with_suffix(".ground_truth_instances.json")
    instances_path.write_text(
        json.dumps(
            {
                "instances": expected_instances_for(error_numbers),
                "evidence": annotation_evidence(error_numbers),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return output, ground_truth


def main() -> int:
    parser = argparse.ArgumentParser(description="Inject form-review errors into a DOCX")
    parser.add_argument("--errors", required=True, help="Comma-separated error numbers, e.g. 3,5,9")
    parser.add_argument("--out", required=True, type=Path, help="Output DOCX path")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="Clean source DOCX")
    args = parser.parse_args()
    numbers = [int(value.strip()) for value in args.errors.split(",") if value.strip()]
    output, _ = inject_errors(args.source, numbers, args.out)
    print(output)
    print(output.with_suffix(".ground_truth.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
