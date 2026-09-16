"""Generate a realistic ~10-page sample project proposal DOCX for evaluation.

Usage: cd eval && python generate_sample_docx.py → eval/samples/sample_proposal.docx
"""
from __future__ import annotations

from pathlib import Path
from docx import Document
from docx.shared import Cm, Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.section import WD_ORIENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement


def set_cell_shading(cell, color: str):
    """Set cell background color."""
    shading = OxmlElement("w:shd")
    shading.set(qn("w:fill"), color)
    shading.set(qn("w:val"), "clear")
    cell._tc.get_or_add_tcPr().append(shading)


def add_heading_styled(doc, text: str, level: int = 1):
    """Add a heading with appropriate styling.

    黑体无粗体字形、bold 在 PDF 中丢失粗体标志 → 改用微软雅黑并显式加粗；字号按 campus 区间 h1 12-22 / h2 10-20 / h3 9-18。"""
    h = doc.add_heading(text, level=level)
    font_name = "微软雅黑"
    sizes = {1: 16, 2: 14, 3: 12}
    for run in h.runs:
        run.font.name = font_name
        run.font.size = Pt(sizes.get(level, 12))
        run.font.bold = True
        rPr = run._element.find(qn("w:rPr"))
        if rPr is None:
            rPr = OxmlElement("w:rPr")
            run._element.insert(0, rPr)
        rFonts_elem = rPr.find(qn("w:rFonts"))
        if rFonts_elem is None:
            rFonts_elem = OxmlElement("w:rFonts")
            rPr.insert(0, rFonts_elem)
        rFonts_elem.set(qn("w:eastAsia"), font_name)
    return h


def add_body_para(doc, text: str):
    """Add a body paragraph with 宋体 font."""
    p = doc.add_paragraph(text)
    pf = p.paragraph_format
    pf.first_line_indent = Cm(0.74)  # ~2 Chinese chars
    pf.line_spacing = 1.5
    pf.space_after = Pt(6)
    for run in p.runs:
        run.font.name = "宋体"
        run.font.size = Pt(12)
        rFonts = run._element.find(qn("w:rPr"))
        if rFonts is None:
            rFonts = OxmlElement("w:rPr")
            run._element.insert(0, rFonts)
        rFonts_elem = rFonts.find(qn("w:rFonts"))
        if rFonts_elem is None:
            rFonts_elem = OxmlElement("w:rFonts")
            rFonts.insert(0, rFonts_elem)
        rFonts_elem.set(qn("w:eastAsia"), "宋体")
    return p


def create_table(doc, headers: list[str], rows: list[list[str]], col_widths: list[float] | None = None):
    """Create a formatted table."""
    table = doc.add_table(rows=1 + len(rows), cols=len(headers))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER

    # Header row
    for i, h in enumerate(headers):
        cell = table.rows[0].cells[i]
        cell.text = h
        for p in cell.paragraphs:
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            for run in p.runs:
                run.font.bold = True
                run.font.size = Pt(10.5)
        set_cell_shading(cell, "D9E2F3")

    # Data rows
    for r, row_data in enumerate(rows):
        for c, val in enumerate(row_data):
            cell = table.rows[r + 1].cells[c]
            cell.text = val
            for p in cell.paragraphs:
                for run in p.runs:
                    run.font.size = Pt(10.5)

    doc.add_paragraph()  # spacing after table
    return table


def generate_sample(
    output_path: str = str(Path(__file__).parent / "samples" / "sample_proposal.docx"),
):
    """Generate a complete sample project proposal."""
    doc = Document()

    # ── Page setup ──────────────────────────────────
    section = doc.sections[0]
    section.page_width = Cm(21.0)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(2.54)
    section.bottom_margin = Cm(2.54)
    section.left_margin = Cm(3.17)
    section.right_margin = Cm(3.17)

    # ── Cover page ──────────────────────────────────
    for _ in range(6):
        doc.add_paragraph()

    title_p = doc.add_paragraph()
    title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title_p.add_run("校级科研课题申报书")
    run.font.name = "黑体"
    run.font.size = Pt(26)
    run.font.bold = True
    rPr = run._element.find(qn("w:rPr"))
    if rPr is None:
        rPr = OxmlElement("w:rPr")
        run._element.insert(0, rPr)
    rFonts_elem = rPr.find(qn("w:rFonts"))
    if rFonts_elem is None:
        rFonts_elem = OxmlElement("w:rFonts")
        rPr.insert(0, rFonts_elem)
    rFonts_elem.set(qn("w:eastAsia"), "黑体")

    doc.add_paragraph()

    info_lines = [
        "课题名称：基于深度学习的城市交通流预测方法研究",
        "研究期限：2026年9月 — 2028年8月",
        "申报单位：计算机科学与技术学院",
        "课题负责人：张三",
        "联系电话：13800138000",
        "电子邮箱：zhangsan@university.edu.cn",
        "填表日期：2026年6月30日",
    ]
    for line in info_lines:
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(line)
        run.font.name = "宋体"
        run.font.size = Pt(14)
        rPr = run._element.find(qn("w:rPr"))
        if rPr is None:
            rPr = OxmlElement("w:rPr")
            run._element.insert(0, rPr)
        rFonts_elem = rPr.find(qn("w:rFonts"))
        if rFonts_elem is None:
            rFonts_elem = OxmlElement("w:rFonts")
            rPr.insert(0, rFonts_elem)
        rFonts_elem.set(qn("w:eastAsia"), "宋体")

    doc.add_page_break()

    # ── Page 2: 一、立项依据 ────────────────────────
    add_heading_styled(doc, "一、立项依据与研究背景", level=1)
    add_body_para(doc,
        "城市交通拥堵问题日益严峻，准确预测交通流量是智能交通系统的核心环节。"
        "传统统计方法如ARIMA在处理复杂时空依赖关系时存在明显不足，而深度学习方法"
        "在此领域展现出巨大潜力。"
    )
    add_body_para(doc,
        "近年来，图卷积网络（GCN）和Transformer架构在处理时空数据方面取得了显著进展。"
        "然而，现有方法在城市路网拓扑结构动态变化、多尺度时空特征融合方面仍存在局限。"
        "本研究拟提出一种融合动态图卷积与自适应注意力机制的交通流预测新方法，"
        "以提升长期预测精度和计算效率。"
    )
    add_body_para(doc,
        "国内外相关研究主要分为三类：一是基于循环神经网络的序列预测方法，"
        "能捕捉时间依赖但忽略空间拓扑；二是基于图卷积的时空预测方法，"
        "能建模路网结构但多为静态图；三是基于注意力机制的纯Transformer方法，"
        "计算开销大且需要大量训练数据。本研究将结合前三类方法的优势，"
        "提出轻量化的动态时空预测框架。"
    )
    # Add more content to reach word count
    add_body_para(doc,
        "从实际应用价值来看，准确的交通流预测可以帮助交通管理部门提前制定管控策略，"
        "为出行者提供更优的路径规划建议，从而缓解拥堵、降低能耗、减少排放。"
        "据统计，城市交通拥堵每年造成的经济损失高达数千亿元，其中因预测不准确导致的"
        "无效调度和资源浪费占相当比例。因此，开展本研究具有重要的理论意义和现实价值。"
    )

    # ── Page 3: 二、研究目标与内容 ──────────────────
    doc.add_page_break()
    add_heading_styled(doc, "二、研究目标与内容", level=1)

    add_heading_styled(doc, "2.1 研究目标", level=2)
    add_body_para(doc,
        "本课题旨在构建一种新型的交通流预测模型，实现以下目标："
        "（1）提出动态邻接矩阵学习方法，自适应捕捉路网拓扑的时序演化；"
        "（2）设计多尺度时空注意力机制，融合全局与局部特征；"
        "（3）开发轻量化推理架构，满足实时预测需求；"
        "（4）在真实大规模数据集上验证模型的有效性和鲁棒性。"
    )

    add_heading_styled(doc, "2.2 研究内容", level=2)
    add_body_para(doc,
        "研究内容一：动态路网拓扑建模。针对现有方法使用静态邻接矩阵的问题，"
        "本研究将设计一种基于门控机制的动态图生成模块，在每个时间步自适应地学习"
        "节点间的连接强度，使模型能响应交通事故、道路封闭等突发事件引起的拓扑变化。"
        "关键技术包括图注意力网络的扩展变体和时间感知的边权重估计器。"
    )
    add_body_para(doc,
        "研究内容二：多尺度时空特征融合。交通流数据同时存在短时突变（如红绿灯周期）"
        "和长时趋势（如早晚高峰），单一尺度的特征提取难以兼顾。本研究将设计分层时间"
        "卷积与跨尺度注意力机制，分别在分钟级、小时级和日级尺度上提取特征并自适应融合，"
        "使模型对不同类型的交通模式变化都能做出准确响应。"
    )
    add_body_para(doc,
        "研究内容三：轻量化推理与部署优化。考虑到实际部署场景的计算资源限制，"
        "本研究将在保证预测精度的前提下，通过知识蒸馏、模型剪枝和量化技术，"
        "将模型参数量和推理延迟降至实用水平，并在边缘计算设备上进行验证。"
    )
    add_body_para(doc,
        "研究内容四：大规模实验验证。本研究将在METR-LA、PEMS-BAY等公开数据集"
        "以及本地城市交通数据上进行充分实验，与十余种基线方法进行全面对比，"
        "并通过消融实验和鲁棒性分析验证各模块的贡献。"
    )

    # ── Page 4: 三、研究方法与技术路线 ──────────────
    doc.add_page_break()
    add_heading_styled(doc, "三、研究方法与技术路线", level=1)
    add_body_para(doc,
        "本课题《基于深度学习的城市交通流预测方法研究》采用理论分析、模型设计、"
        "实验验证相结合的研究方法。"
        "首先，通过文献调研明确现有方法的不足；其次，基于图神经网络和注意力机制"
        "提出新的模型架构；然后，在公开数据集和本地数据上进行充分的对比实验；"
        "最后，对模型进行可解释性分析和鲁棒性测试。"
    )
    add_body_para(doc,
        "技术路线分为四个阶段：第一阶段（1-6个月）完成文献调研和需求分析，"
        "搭建数据预处理流水线；第二阶段（7-18个月）完成核心模型的设计、实现和调优，"
        "分模块进行单元测试和集成测试；第三阶段（19-30个月）开展大规模实验验证和"
        "对比分析，撰写高水平学术论文；第四阶段（31-36个月）进行系统集成与部署验证，"
        "整理科研成果并完成结题报告。"
    )
    add_body_para(doc,
        "开发环境采用Python 3.11 + PyTorch 2.0 + PyTorch Geometric，"
        "训练平台使用NVIDIA A100 GPU集群，代码采用Git进行版本管理，"
        "实验结果使用MLflow进行追踪管理，确保研究的可复现性。"
    )

    # ── Figure and table captions (standalone, <40 chars) + body refs ──
    def add_caption(doc, text: str):
        """Add a standalone figure/table caption line (<40 chars, centered)."""
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(text)
        run.font.name = "黑体"
        run.font.size = Pt(10.5)

    add_caption(doc, "图1 研究技术路线示意图")
    add_body_para(doc, "本研究整体技术路线如图1所示。")
    add_caption(doc, "图2 核心模型架构图")
    add_body_para(doc, "核心模型架构如图2所示，包含动态图生成模块、多尺度注意力模块和预测解码器三个部分。")
    add_caption(doc, "图3 模型训练损失收敛曲线")
    add_body_para(doc,
        "模型训练过程中损失函数收敛曲线如图3所示，从图中可以看出模型在约200个epoch后"
        "达到稳定收敛状态，验证集损失未见明显上升，说明模型未出现过拟合现象。"
    )
    add_caption(doc, "表1 模型参数配置表")
    add_body_para(doc, "模型各组件的参数配置如表1所示。")
    add_caption(doc, "表2 实验数据集统计信息")
    add_body_para(doc, "实验数据集统计信息如表2所示。")

    # ── Page 5: 四、预期成果与创新点 ──────────────
    doc.add_page_break()
    add_heading_styled(doc, "四、预期成果与创新点", level=1)

    add_heading_styled(doc, "4.1 预期成果", level=2)
    add_body_para(doc,
        "（1）理论成果：提出一套完整的动态时空交通流预测理论框架，"
        "在高水平国际期刊和会议上发表学术论文3-5篇。"
        "（2）技术成果：开发开源交通流预测工具包，提供完整的训练、评估和部署流程。"
        "（3）应用成果：在合作城市的交通管理系统中进行试点应用，验证实际效果。"
        "（4）人才培养：培养博士研究生1-2名，硕士研究生2-3名。"
    )

    add_heading_styled(doc, "4.2 创新点", level=2)
    add_body_para(doc,
        "创新点一：提出动态邻接矩阵学习方法，突破传统静态图卷积的局限，"
        "使模型能够自适应地捕捉路网拓扑的动态演化规律，这是本研究最核心的理论创新。"
    )
    add_body_para(doc,
        "创新点二：设计多尺度时空注意力融合机制，通过分层时间卷积与跨尺度注意力的协同，"
        "实现对不同时间粒度交通模式的统一建模，解决了现有方法多尺度特征融合不充分的问题。"
    )
    add_body_para(doc,
        "创新点三：提出面向边缘部署的模型轻量化方案，在保证预测精度的同时大幅降低计算开销，"
        "为深度学习交通预测模型的实际落地提供可行路径。"
    )

    # ── Page 6: 五、研究基础与条件 ──────────────
    doc.add_page_break()
    add_heading_styled(doc, "五、研究基础与工作条件", level=1)

    add_heading_styled(doc, "5.1 研究基础", level=2)
    add_body_para(doc,
        "课题负责人长期从事时空数据挖掘和智能交通系统研究，近五年来在IEEE TKDE、"
        "IEEE TITS、AAAI、KDD等高水平期刊和会议上发表相关论文20余篇，"
        "其中ESI高被引论文3篇。主持国家自然科学基金面上项目1项、青年项目1项，"
        "参与国家重点研发计划项目2项。在时空预测、图神经网络方面积累了丰富的研究经验。"
    )
    add_body_para(doc,
        "团队成员包括副教授1名、讲师2名、博士生3名、硕士生5名，形成了结构合理的"
        "研究梯队。团队成员在深度学习、计算机视觉、数据挖掘等领域各有所长，"
        "具备完成本课题所需的各项技术能力。"
    )

    add_heading_styled(doc, "5.2 工作条件", level=2)
    add_body_para(doc,
        "所在实验室拥有GPU计算集群（8×NVIDIA A100），高性能存储服务器，"
        "以及完善的深度学习开发环境。与本地交通管理部门建立了长期合作关系，"
        "可获取真实的交通流数据用于模型训练和验证。学校图书馆订购了IEEE、ACM、"
        "Elsevier等主要学术数据库，文献获取方便。"
    )

    # ── Page 7: 六、研究计划与进度安排 ──────────
    doc.add_page_break()
    add_heading_styled(doc, "六、研究计划与进度安排", level=1)
    add_body_para(doc,
        "本课题研究期限为2026年9月至2028年8月，共计24个月。"
        "各阶段具体安排如下表所示。"
    )

    schedule = [
        ["第一阶段", "2026年9月 - 2027年2月", "文献调研与需求分析，数据采集与预处理流水线搭建"],
        ["第二阶段", "2027年3月 - 2027年12月", "核心模型设计与实现，模块测试与初步验证"],
        ["第三阶段", "2028年1月 - 2028年6月", "大规模实验验证，学术论文撰写与投稿"],
        ["第四阶段", "2028年7月 - 2028年8月", "系统集成与部署验证，结题报告撰写"],
    ]
    create_table(doc, ["阶段", "时间", "主要任务"], schedule)

    # ── Page 8: 七、经费预算 ─────────────────────
    doc.add_page_break()
    add_heading_styled(doc, "七、经费预算", level=1)
    add_body_para(doc,
        "本课题申请总经费30万元，具体预算明细如下表所示。"
        "各项支出均严格按照学校科研经费管理办法编制，确保经费使用的合理性和规范性。"
    )

    budget_rows = [
        ["1", "设备费", "8.0", "购置GPU计算卡、高性能存储设备"],
        ["2", "材料费", "3.0", "实验耗材、数据存储介质"],
        ["3", "测试化验加工费", "2.5", "云计算资源租赁、数据标注外包"],
        ["4", "差旅费", "4.0", "国内外学术会议、合作单位调研"],
        ["5", "会议费", "1.5", "学术研讨会、项目评审会"],
        ["6", "出版/文献/知识产权费", "2.0", "论文版面费、专利申请费"],
        ["7", "劳务费", "6.6", "研究生助研津贴、专家咨询费"],
        ["8", "管理费", "2.4", "学校科研管理费（8%）"],
        ["", "合计", "30.0", ""],
    ]
    create_table(doc, ["序号", "经费科目", "金额（万元）", "计算依据及说明"], budget_rows)

    add_body_para(doc, "注：管理费不超过总经费的10%，各项预算均按照学校财务制度执行。")

    # ── Page 9: 八、参考文献 ─────────────────────
    doc.add_page_break()
    add_heading_styled(doc, "八、参考文献", level=1)

    references = [
        "[1] YU B, YIN H T, ZHU Z X. Spatio-temporal graph convolutional networks: "
        "a deep learning framework for traffic forecasting[C]//Proceedings of the 27th "
        "International Joint Conference on Artificial Intelligence. Stockholm: IJCAI, 2018: 3634-3640.",
        "[2] LI Y G, YU R, SHAHABI C, et al. Diffusion convolutional recurrent neural "
        "network: data-driven traffic forecasting[C]//International Conference on Learning "
        "Representations. Vancouver: ICLR, 2018.",
        "[3] WU Z H, PAN S R, LONG G D, et al. Graph WaveNet for deep spatial-temporal "
        "graph modeling[C]//Proceedings of the 28th International Joint Conference on "
        "Artificial Intelligence. Macao: IJCAI, 2019: 1907-1913.",
        "[4] GUO S N, LIN Y F, FENG N, et al. Attention based spatial-temporal graph "
        "convolutional networks for traffic flow forecasting[C]//Proceedings of the AAAI "
        "Conference on Artificial Intelligence. Honolulu: AAAI, 2019, 33(1): 922-929.",
        "[5] ZHENG C P, FAN X L, WANG C, et al. GMAN: a graph multi-attention network "
        "for traffic prediction[C]//Proceedings of the AAAI Conference on Artificial "
        "Intelligence. New York: AAAI, 2020, 34(1): 1234-1241.",
        "[6] VASWANI A, SHAZEER N, PARMAR N, et al. Attention is all you need[C]//"
        "Advances in Neural Information Processing Systems. Long Beach: NeurIPS, 2017: 5998-6008.",
        "[7] KIPF T N, WELLING M. Semi-supervised classification with graph "
        "convolutional networks[C]//International Conference on Learning Representations. "
        "Toulon: ICLR, 2017.",
        "[8] VELIČKOVIĆ P, CUCURULL G, CASANOVA A, et al. Graph attention networks"
        "[C]//International Conference on Learning Representations. Vancouver: ICLR, 2018.",
        "[9] FANG S H, ZHANG Q, MENG G Y, et al. GSTNet: global spatial-temporal network "
        "for traffic flow prediction[C]//Proceedings of the 28th International Joint "
        "Conference on Artificial Intelligence. Macao: IJCAI, 2019: 2286-2293.",
        "[10] ZHANG J P, ZHENG Y, QI D K. Deep spatio-temporal residual networks for "
        "citywide crowd flows prediction[C]//Proceedings of the AAAI Conference on "
        "Artificial Intelligence. San Francisco: AAAI, 2017: 1655-1661.",
        "[11] SONG C, LIN Y F, GUO S N, et al. Spatial-temporal synchronous graph "
        "convolutional networks: a new framework for spatial-temporal network data "
        "forecasting[C]//Proceedings of the AAAI Conference on Artificial Intelligence. "
        "New York: AAAI, 2020, 34(1): 914-921.",
        "[12] LI M, ZHU Z. Spatial-temporal fusion graph neural networks for traffic "
        "flow forecasting[C]//Proceedings of the AAAI Conference on Artificial "
        "Intelligence. Virtual: AAAI, 2021, 35(5): 4189-4196.",
        "[13] LAN S S, MA Y T, HUANG W, et al. DSTAGNN: dynamic spatial-temporal aware "
        "graph neural network for traffic flow forecasting[C]//Proceedings of the 39th "
        "International Conference on Machine Learning. Baltimore: PMLR, 2022: 11906-11922.",
        "[14] JIANG W W, LUO J. Graph neural network for traffic forecasting: a survey"
        "[J]. Expert Systems with Applications, 2022, 207: 117921.",
        "[15] YE J X, ZHAO J J, YE K J, et al. How to build a graph-based deep learning "
        "architecture in traffic domain: a survey[J]. IEEE Transactions on Intelligent "
        "Transportation Systems, 2022, 23(5): 3904-3924.",
        "[16] JIANG R, YIN D, WANG Z, et al. DL-Traff: survey and benchmark of deep "
        "learning models for urban traffic prediction[C]//Proceedings of the 30th ACM "
        "International Conference on Information and Knowledge Management. Virtual: "
        "ACM, 2021: 4515-4525.",
        "[17] CHEN W H, CHEN L, XIE Y, et al. Multi-range attentive bicomponent graph "
        "convolutional network for traffic forecasting[C]//Proceedings of the AAAI "
        "Conference on Artificial Intelligence. New York: AAAI, 2020, 34(4): 3529-3536.",
        "[18] PARK C, LEE C, BAHNG H, et al. ST-GRAT: a novel spatio-temporal graph "
        "attention network for accurately forecasting dynamically changing road speed"
        "[C]//Proceedings of the 29th ACM International Conference on Information and "
        "Knowledge Management. Virtual: ACM, 2020: 1215-1224.",
        "[19] WANG X Y, MA Y, WANG Y Q, et al. Traffic flow prediction via spatial "
        "temporal graph neural network[C]//Proceedings of The Web Conference 2020. "
        "Taipei: ACM, 2020: 1082-1092.",
        "[20] BAI L, YAO L, LI C, et al. Adaptive graph convolutional recurrent network "
        "for traffic forecasting[C]//Advances in Neural Information Processing Systems. "
        "Virtual: NeurIPS, 2020, 33: 17804-17815.",
    ]
    for ref in references:
        add_body_para(doc, ref)

    # ── Page 10: 九、附件清单 ────────────────────
    doc.add_page_break()
    add_heading_styled(doc, "九、附件清单", level=1)
    attachments = [
        "附件：课题负责人学历学位证书复印件",
        "附件：课题负责人近五年代表性成果清单",
        "附件：主要团队成员简历",
        "附件：合作协议（如有合作单位）",
        "附件：伦理审查证明",
        "附件：经费预算明细表（补充）",
        "附件：依托单位推荐意见",
    ]
    for att in attachments:
        add_body_para(doc, att)

    # ── Last page: 签字盖章页 ────────────────────
    doc.add_page_break()
    for _ in range(8):
        doc.add_paragraph()

    sig_p = doc.add_paragraph()
    sig_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = sig_p.add_run("签 字 盖 章 页")
    run.font.name = "黑体"
    run.font.size = Pt(18)
    run.font.bold = True

    doc.add_paragraph()
    doc.add_paragraph()

    sig_fields = [
        "申请人签字：________________    日期：____________",
        "",
        "课题负责人签字：________________    日期：____________",
        "",
        "依托单位审核意见：",
        "",
        "（内容属实，同意申报）",
        "",
        "单位负责人签字：________________    日期：____________",
        "",
        "（单位盖章）",
        "",
        "科研管理部门审核意见：",
        "",
        "（符合申报条件，同意推荐）",
        "",
        "部门负责人签字：________________    日期：____________",
    ]
    for line in sig_fields:
        p = doc.add_paragraph()
        if "签字" in line or "意见" in line or "公章" in line:
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        else:
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(line)
        run.font.name = "宋体"
        run.font.size = Pt(14)

    # ── Save ────────────────────────────────────────
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    print(f"Sample proposal saved to: {output_path.absolute()}")
    print(f"Pages: ~10 (estimated), Sections: 9 + cover + signature page")


if __name__ == "__main__":
    generate_sample()
