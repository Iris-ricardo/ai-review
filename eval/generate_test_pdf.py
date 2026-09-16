"""Generate a test PDF with reportlab for PDF parser integration testing.

LibreOffice-independent path; includes headings, paragraphs, a budget table and a signature page. Usage: cd eval && python generate_test_pdf.py → eval/samples/test_document.pdf
"""
from __future__ import annotations

from pathlib import Path
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus.doctemplate import PageTemplate, BaseDocTemplate, Frame

# Try to register a CJK font; fall back to built-in if unavailable
_CJK_FONT = "Helvetica"
try:
    # Common Windows CJK font paths
    import os
    candidates = [
        "C:/Windows/Fonts/simsun.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/SimSun.ttf",
    ]
    for fp in candidates:
        if os.path.exists(fp):
            pdfmetrics.registerFont(TTFont("CJK", fp))
            _CJK_FONT = "CJK"
            break
except Exception:
    pass

_STYLES = getSampleStyleSheet()

# Custom styles
_heading1_style = ParagraphStyle(
    "CNHeading1", parent=_STYLES["Heading1"],
    fontName=_CJK_FONT, fontSize=16, spaceAfter=12, spaceBefore=20,
)
_heading2_style = ParagraphStyle(
    "CNHeading2", parent=_STYLES["Heading2"],
    fontName=_CJK_FONT, fontSize=14, spaceAfter=10, spaceBefore=16,
)
_body_style = ParagraphStyle(
    "CNBody", parent=_STYLES["Normal"],
    fontName=_CJK_FONT, fontSize=11, spaceAfter=8, leading=18,
    firstLineIndent=22,
)
_cover_style = ParagraphStyle(
    "Cover", parent=_STYLES["Normal"],
    fontName=_CJK_FONT, fontSize=14, alignment=TA_CENTER, spaceAfter=12,
)
_caption_style = ParagraphStyle(
    "Caption", parent=_STYLES["Normal"],
    fontName=_CJK_FONT, fontSize=10, alignment=TA_CENTER, spaceAfter=6,
)


def _h1(text: str) -> Paragraph:
    return Paragraph(text, _heading1_style)


def _h2(text: str) -> Paragraph:
    return Paragraph(text, _heading2_style)


def _p(text: str) -> Paragraph:
    return Paragraph(text, _body_style)


def _cover(text: str) -> Paragraph:
    return Paragraph(text, _cover_style)


def _cap(text: str) -> Paragraph:
    return Paragraph(text, _caption_style)


def generate_test_pdf(
    output_path: str = str(Path(__file__).parent / "samples" / "test_document.pdf"),
):
    """Generate a test PDF with headings, paragraphs, table, and signature page."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(
        str(output_path), pagesize=A4,
        topMargin=72, bottomMargin=72, leftMargin=72, rightMargin=72,
    )

    story = []

    # ── Cover page ──────────────────────────────────
    for _ in range(6):
        story.append(Spacer(1, 40))
    story.append(_cover("校级科研课题申报书"))
    story.append(Spacer(1, 20))
    story.append(_cover("课题名称：基于深度学习的城市交通流预测方法研究"))
    story.append(_cover("研究期限：2026年9月 — 2028年8月"))
    story.append(_cover("申报单位：计算机科学与技术学院"))
    story.append(_cover("课题负责人：张三"))
    story.append(PageBreak())

    # ── 一、立项依据 ────────────────────────────────
    story.append(_h1("一、立项依据与研究背景"))
    story.append(_p(
        "城市交通拥堵问题日益严峻，准确预测交通流量是智能交通系统的核心环节。"
        "传统统计方法如ARIMA在处理复杂时空依赖关系时存在明显不足，而深度学习方法"
        "在此领域展现出巨大潜力。"
    ))
    story.append(_p(
        "近年来，图卷积网络（GCN）和Transformer架构在处理时空数据方面取得了显著进展。"
        "然而，现有方法在城市路网拓扑结构动态变化、多尺度时空特征融合方面仍存在局限。"
        "本研究拟提出一种融合动态图卷积与自适应注意力机制的交通流预测新方法，"
        "以提升长期预测精度和计算效率。"
    ))
    story.append(_p(
        "国内外相关研究主要分为三类：一是基于循环神经网络的序列预测方法，"
        "能捕捉时间依赖但忽略空间拓扑；二是基于图卷积的时空预测方法，"
        "能建模路网结构但多为静态图；三是基于注意力机制的纯Transformer方法，"
        "计算开销大且需要大量训练数据。本研究将结合前三类方法的优势，"
        "提出轻量化的动态时空预测框架。"
    ))
    story.append(_p(
        "从实际应用价值来看，准确的交通流预测可以帮助交通管理部门提前制定管控策略，"
        "为出行者提供更优的路径规划建议，从而缓解拥堵、降低能耗、减少排放。"
        "据统计，城市交通拥堵每年造成的经济损失高达数千亿元，其中因预测不准确导致的"
        "无效调度和资源浪费占相当比例。因此，开展本研究具有重要的理论意义和现实价值。"
    ))
    story.append(_p(
        "从学术前沿来看，时空数据挖掘是当前人工智能领域的研究热点之一。"
        "NeurIPS、ICML、KDD等顶级会议每年都有大量相关工作发表，学术界对高效、"
        "准确的时空预测方法有着持续而强烈的需求。本研究旨在填补动态图建模与"
        "多尺度特征融合之间的研究空白，推动该领域的理论发展和技术进步。"
    ))

    # ── 二、研究目标与内容 ──────────────────────────
    story.append(_h1("二、研究目标与内容"))
    story.append(_h2("2.1 研究目标"))
    story.append(_p(
        "本课题旨在构建一种新型的交通流预测模型，实现动态邻接矩阵学习、"
        "多尺度时空注意力融合和轻量化推理的目标。"
    ))
    story.append(_h2("2.2 研究内容"))
    story.append(_p(
        "研究内容一：动态路网拓扑建模。针对现有方法使用静态邻接矩阵的问题，"
        "本研究将设计一种基于门控机制的动态图生成模块。"
    ))
    story.append(_p(
        "研究内容二：多尺度时空特征融合。本研究将设计分层时间卷积与跨尺度"
        "注意力机制，使模型对不同类型的交通模式变化都能做出准确响应。"
    ))
    story.append(_p(
        "研究内容三：轻量化推理与部署优化。通过知识蒸馏、模型剪枝和量化技术，"
        "将模型参数量和推理延迟降至实用水平。"
    ))
    story.append(_p(
        "研究内容四：大规模实验验证。在METR-LA、PEMS-BAY等公开数据集上进行充分实验。"
    ))

    # ── 三、研究方法与技术路线 ──────────────────────
    story.append(_h1("三、研究方法与技术路线"))
    story.append(_p(
        "本课题《基于深度学习的城市交通流预测方法研究》采用理论分析、模型设计、"
        "实验验证相结合的研究方法。"
    ))
    story.append(_cap("图1 研究技术路线示意图"))
    story.append(_p("本研究整体技术路线如图1所示。"))
    story.append(_cap("图2 核心模型架构图"))
    story.append(_p("核心模型架构如图2所示，包含动态图生成模块、多尺度注意力模块和预测解码器三个部分。"))
    story.append(_cap("图3 模型训练损失收敛曲线"))
    story.append(_p("模型训练过程中损失函数收敛曲线如图3所示。"))
    story.append(_cap("表1 模型参数配置表"))
    story.append(_p("模型各组件的参数配置如表1所示。"))
    story.append(_cap("表2 实验数据集统计信息"))
    story.append(_p("实验数据集统计信息如表2所示。"))

    # ── 四、预期成果与创新点 ────────────────────────
    story.append(_h1("四、预期成果与创新点"))
    story.append(_h2("4.1 预期成果"))
    story.append(_p(
        "（1）理论成果：提出一套完整的动态时空交通流预测理论框架，"
        "在高水平国际期刊和会议上发表学术论文3-5篇。"
    ))
    story.append(_p(
        "（2）技术成果：开发开源交通流预测工具包，提供完整的训练、评估和部署流程。"
    ))
    story.append(_p(
        "（3）应用成果：在合作城市的交通管理系统中进行试点应用，验证实际效果。"
    ))
    story.append(_p(
        "（4）人才培养：培养博士研究生1-2名，硕士研究生2-3名。"
    ))
    story.append(_h2("4.2 创新点"))
    story.append(_p(
        "创新点一：提出动态邻接矩阵学习方法，突破传统静态图卷积的局限，"
        "使模型能够自适应地捕捉路网拓扑的动态演化规律。"
    ))
    story.append(_p(
        "创新点二：设计多尺度时空注意力融合机制，通过分层时间卷积与跨尺度注意力的协同，"
        "实现对不同时间粒度交通模式的统一建模。"
    ))
    story.append(_p(
        "创新点三：提出面向边缘部署的模型轻量化方案，在保证预测精度的同时大幅降低计算开销。"
    ))

    # ── 五、研究基础与工作条件 ──────────────────────
    story.append(_h1("五、研究基础与工作条件"))
    story.append(_h2("5.1 研究基础"))
    story.append(_p(
        "课题负责人长期从事时空数据挖掘和智能交通系统研究，近五年来在IEEE TKDE、"
        "IEEE TITS、AAAI、KDD等高水平期刊和会议上发表相关论文20余篇，"
        "其中ESI高被引论文3篇。主持国家自然科学基金面上项目1项、青年项目1项，"
        "参与国家重点研发计划项目2项。在时空预测、图神经网络方面积累了丰富的研究经验。"
    ))
    story.append(_p(
        "团队成员包括副教授1名、讲师2名、博士生3名、硕士生5名，形成了结构合理的"
        "研究梯队。团队成员在深度学习、计算机视觉、数据挖掘等领域各有所长，"
        "具备完成本课题所需的各项技术能力。"
    ))
    story.append(_h2("5.2 工作条件"))
    story.append(_p(
        "所在实验室拥有GPU计算集群（8×NVIDIA A100），高性能存储服务器，"
        "以及完善的深度学习开发环境。与本地交通管理部门建立了长期合作关系，"
        "可获取真实的交通流数据用于模型训练和验证。学校图书馆订购了IEEE、ACM、"
        "Elsevier等主要学术数据库，文献获取方便。"
    ))
    story.append(_p(
        "实验室已建成一套完整的智能交通数据分析平台，集成了数据采集、清洗、"
        "标注、训练和评估等全流程工具链，为本课题的顺利实施提供了坚实的基础"
        "设施保障。此外，实验室与多家国内外知名高校和企业建立了长期合作关系，"
        "能够为本课题提供充足的学术交流和技术合作机会。"
    ))

    # ── 六、研究计划与进度安排 ──────────────────────
    story.append(_h1("六、研究计划与进度安排"))
    story.append(_p(
        "本课题研究期限为2026年9月至2028年8月，共计24个月。各阶段具体安排如下。"
    ))
    schedule_data = [
        ["阶段", "时间", "主要任务"],
        ["第一阶段", "2026年9月 - 2027年2月", "文献调研与需求分析"],
        ["第二阶段", "2027年3月 - 2027年12月", "核心模型设计与实现"],
        ["第三阶段", "2028年1月 - 2028年6月", "大规模实验验证"],
        ["第四阶段", "2028年7月 - 2028年8月", "系统集成与结题"],
    ]
    _build_table(story, schedule_data)

    # ── 七、经费预算 ────────────────────────────────
    story.append(_h1("七、经费预算"))
    story.append(_p(
        "本课题申请总经费30万元，具体预算明细如下表所示。"
    ))
    budget_data = [
        ["序号", "经费科目", "金额（万元）", "计算依据及说明"],
        ["1", "设备费", "8.0", "购置GPU计算卡、高性能存储设备"],
        ["2", "材料费", "3.0", "实验耗材、数据存储介质"],
        ["3", "测试化验加工费", "2.5", "云计算资源租赁"],
        ["4", "差旅费", "4.0", "国内外学术会议"],
        ["5", "会议费", "1.5", "学术研讨会"],
        ["6", "出版/文献/知识产权费", "2.0", "论文版面费"],
        ["7", "劳务费", "6.6", "研究生助研津贴"],
        ["8", "管理费", "2.4", "学校科研管理费（8%）"],
        ["", "合计", "30.0", ""],
    ]
    _build_table(story, budget_data)
    story.append(_p("注：管理费不超过总经费的10%，各项预算均按照学校财务制度执行。"))

    # ── 八、参考文献 ────────────────────────────────
    story.append(PageBreak())
    story.append(_h1("八、参考文献"))
    for i in range(1, 16):
        story.append(_p(f"[{i}] Author {i} A, Author B, Author C. Title of reference paper "
                        f"number {i} in the field of intelligent transportation systems "
                        f"and deep learning applications[J]. International Journal of "
                        f"Traffic Prediction, 2024, {i}0({i}): 1-{i}0."))

    # ── 九、附件清单 ────────────────────────────────
    story.append(_h1("九、附件清单"))
    for att in ["课题负责人学历学位证书复印件", "伦理审查证明", "合作协议", "依托单位推荐意见"]:
        story.append(_p(f"  {att}"))

    # ── 签字盖章页 ──────────────────────────────────
    story.append(PageBreak())
    for _ in range(6):
        story.append(Spacer(1, 30))
    story.append(_cover("签 字 盖 章 页"))
    story.append(Spacer(1, 30))
    story.append(_p("申请人签字：________________    日期：____________"))
    story.append(Spacer(1, 15))
    story.append(_p("课题负责人签字：________________    日期：____________"))
    story.append(Spacer(1, 15))
    story.append(_p("依托单位审核意见：（内容属实，同意申报）"))
    story.append(Spacer(1, 15))
    story.append(_p("单位负责人签字：________________    日期：____________"))
    story.append(Spacer(1, 15))
    story.append(_p("（单位盖章）"))

    # ── Build ────────────────────────────────────────
    doc.build(story)
    print(f"Test PDF saved to: {output_path.absolute()}")
    print(f"Pages: ~10+")


def _build_table(story, data: list[list[str]]):
    """Add a formatted table to the story."""
    t = Table(data, colWidths=[50, 180, 100, 180])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#D9E2F3")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("FONTNAME", (0, 0), (-1, -1), _CJK_FONT),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(t)
    story.append(Spacer(1, 12))


if __name__ == "__main__":
    generate_test_pdf()
