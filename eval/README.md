# 评测工具

目录职责：

```text
eval/
  samples/                固定的合成 PDF/DOCX 样例
  results/                评测生成的报告（可用 EVAL_RESULTS_DIR 覆盖）
  metrics.py              指标口径实现（旧/实例/身份三口径，被 backend 测试直接覆盖）
  generate_sample_docx.py 生成标准 DOCX 样例
  generate_test_pdf.py    生成解析器测试 PDF
  inject_errors.py        向样例注入可控错误（含 GROUND_TRUTH 与 INSTANCE_ANNOTATIONS 标注）
  make_sabotage.py        生成预算错误样例
  run_eval.py             运行本地规则评测（20 样本端到端）
  smoke_llm.py            可选的真实模型冒烟测试（隔离运行，默认不产生费用）
```

## 一条命令复现（业务评测）

从**项目根目录**运行：

```powershell
.\.venv\Scripts\python.exe eval\run_eval.py
```

脚本自身完成：临时数据库/上传/输出/日志隔离、规则目录复制、隔离库临时用户真实登录、
`with TestClient` 完整生命周期、逐样本诊断留痕、结束后清理临时资源。
运行前后正式 `backend/review.db`、`uploads/`、`outputs/` 不变。

退出码约定：

| 退出码 | 含义 |
|---|---|
| 0 | 评测完整跑通、全部任务 done，且无阈值要求或阈值达标 |
| 1 | 基础设施失败（样本/依赖/认证/配置缺失或非法） |
| 2 | 评测跑完但存在样本的审查任务未正常 done |
| 3 | 全部任务 done，但实例口径指标未达到配置阈值 |

阈值（默认不启用，即不做硬拦截）：

```powershell
$env:EVAL_MIN_PRECISION = "0.80"
$env:EVAL_MIN_RECALL = "0.90"
.\.venv\Scripts\python.exe eval\run_eval.py
```

结果目录覆盖（默认 `eval/results`，运行前会把既有 report.md 备份到 `<目录>/backup/`）：

```powershell
$env:EVAL_RESULTS_DIR = "D:\path\to\acceptance-results"
```

## 指标口径

报告同时给出**三种口径**，它们回答的问题不同：

| 口径 | 统计与匹配 | 能证明什么 |
|---|---|---|
| 旧（集合去重，历史基线） | 单位 `(checker, severity)`，同规则同级别多个问题合并为 1 | 只证明「这类问题有没有被报出来」 |
| 实例（问题实例一对一，**当前阈值判定**） | 单位是问题实例，按 `(checker, severity)` 一对一贪心匹配，重复计入 FP/FN | 证明「同类同级的**条数**对不对」，**不证明找对了具体问题** |
| 身份（实例定位，R09 新增） | 在实例口径之上按标注身份匹配：预期实例带 `anchors`（证据锚点），预测实例的 evidence/message 必须命中全部锚点才算 `tp_identified` | 证明「**具体是哪一处**问题被找出来了」 |

身份口径的关键点：

- **同类同级但字段/位置不同不得互相顶替**：锚点没命中的预期记 FN，配不上的预测记 FP，
  于是「预期第一页预算合计错、实际第 99 页报同类同级问题」不会再被记成 TP。
- 锚点是**关键词全包含**，不是 message 全等 → 合理措辞变化仍能命中；
  页码差异只做提示（差 >1 页记入 `page_mismatches`），DOCX→PDF 版式位移不算错。
- 标注显式声明不可定位的实例（例如部分规则的副作用条目）按规则级配对，单独计入
  `tp_unverified`，**不与已核实命中混算**。
- 标注依据（每条锚点来自哪个注入动作、哪个检查器消息模板）逐条写在
  `inject_errors.annotation_evidence()`，并在每份 `results/diagnostics/sample_NN.json`
  的 `annotation_evidence` 中留痕；标注**只来自注入语义与检查器契约，不从实际输出反填**。
- 人工核对入口：单独生成一个带错样本时会同时写出两个标注文件，可直接对照样本正文复核
  （`*.ground_truth.json` 为历史扁平期望，`*.ground_truth_instances.json` 为身份标注 +
  逐条依据）：

  ```powershell
  .\.venv\Scripts\python.exe eval\inject_errors.py --errors 3,5,9 --out D:\tmp\injected.docx
  ```
- 三种口径并列输出，**差异来自统计与匹配规则，不得据此宣称检出能力提升**；
  阈值判定（退出码 3）仍沿用实例口径，未擅自更改验收标准。
- 只对「确定性违规(deterministic)」计算 Precision/Recall/F1；
  「需人工确认(manual_required)」与「降级(degraded)」不计入假阳性，单独计数（单位：问题实例条数）。
- 逐样本诊断（`results/diagnostics/sample_NN.json`）保存每个 TP/FP/FN 的
  样本、规则、预期与实际 issue（含页码、证据、消息），以及身份口径的
  `tp_identified` / `tp_unverified` / 页码位移明细。
- 报告记录：随机种子、干净样本 sha256、规则快照 sha256（隔离副本）、应用版本/构建标识、
  提示词版本、Python/PyMuPDF/LibreOffice 版本、注入器脚本 sha256、运行时间。

## 真实模型冒烟测试（可选，产生费用）

```powershell
.\.venv\Scripts\python.exe eval\smoke_llm.py
```

退出码：0 = 检出注入身份信息；1 = 基础设施缺失/失败（含未配置 LLM_API_KEY 时的跳过）；
2 = 任务 done 但未检出注入身份信息。脚本隔离运行（临时库/目录/规则副本/临时超管账号真实登录），
不触碰正式数据，不需要外围部署凭据。

## 环境要求

- 评测 DOCX→PDF 转换需要 LibreOffice：优先 `SOFFICE_PATH`，其次 PATH，
  最后是随项目打包的 `.tools\LibreOffice` 便携版（Windows）。
- 评测默认不调用真实模型；只有显式 `EVAL_USE_LLM=true` 且配置 `LLM_API_KEY` 才会调用（产生费用）。
- 扫描件 OCR 能力不参与本评测（依赖本机 tesseract，见主 README 的 OCR 说明）。

---

## 合成样本池（原 eval/corpus/README.md，内容照录）

## eval/corpus —— 可运行合成样本池（v6：campus + dachuang + nsfc）

## 一条命令生成样本文件

```powershell
cd 项目根
.\.venv\Scripts\python.exe eval\corpus\generate_samples.py                  # 默认 campus
.\.venv\Scripts\python.exe eval\corpus\generate_samples.py --ruleset all    # 三个规则集（39 份）
.\.venv\Scripts\python.exe eval\corpus\generate_samples.py --ruleset dachuang,nsfc
.\.venv\Scripts\python.exe eval\corpus\generate_samples.py --list           # 列出所有可选样本
# 挑着生成：--include / --exclude 用文件主名（逗号分隔可重复、不区分大小写）
.\.venv\Scripts\python.exe eval\corpus\generate_samples.py --include campus_clean,dachuang_clean
.\.venv\Scripts\python.exe eval\corpus\generate_samples.py --exclude nsfc_pdf_damaged
.\.venv\Scripts\python.exe eval\corpus\generate_samples.py --skip-check     # 只生成不自检
```

规则集模板由 `eval/corpus/build_templates.py` 生成并已逐份校准（campus 用既有校级模板；
dachuang 为专用短模板；nsfc 由校级模板派生：48 个月期限 + 申请代码 + 推荐信 + 承诺）：

```powershell
.\.venv\Scripts\python.exe eval\corpus\build_templates.py
.\.venv\Scripts\python.exe eval\corpus\selfcheck.py --docx eval\corpus\templates\dachuang_clean.docx --ruleset dachuang
.\.venv\Scripts\python.exe eval\corpus\selfcheck.py --docx eval\corpus\templates\nsfc_clean.docx --ruleset nsfc
```

## 规则集支持状态（诚实标注）

| 规则集 | 干净基线 | 规则命中样本 | 解析状态样本 | 合计 |
|---|---|---|---|---|
| campus | ✔ 16 页 / 7158 字 | 9 个注入器 + 6 个 mutation | 4 | 20 |
| dachuang | ✔ 4 页 / 1329 字（≤8 页、≤6000 字、12 个月） | 5 通用 + 2 数值 + 4 章节类 = 11 | 4 | 16 |
| nsfc | ✔ 18 页 / 7621 字（48 个月、申请代码、推荐信、承诺） | 5 通用 + 2 数值 + 5 章节/文献类 + word_limit = 13 | 4 | 18 |

- 通用 mutation（三个规则集）：`font_check`（改楷体）、`required_fields`（删该规则集必填字段：
  campus=填表日期 / dachuang=指导教师 / nsfc=申请代码）、`field_format`（电话非法）、
  `heading_numbering`（重复一级标题，附带未加粗）、`layout_margin`（左边距 10mm）。
- 数值类 mutation（dachuang/nsfc）：`date_check`（研究期限结束年份改到开始之前 →
  起止异常 + 期限不符 2 条）、`budget_check`（预算表材料费 +1 → 勾稽不平 1 条）。
- 章节/文献类 mutation：
  - campus：`reference_format`（前两条文献去类型标识与年份 → 2 条）；
  - dachuang：`required_sections`（删“三、创新点” → 缺失 + 编号跳号 2 条）、
    `section_order`（交换二/三 → 顺序不符 + 编号不连续 ×3 = 4 条）、
    `cross_field`（封面预算总额与预算表不一致 → 1 条）、`page_limit`（补页超 8 页 → 1 条）；
  - nsfc：`required_sections`（删“五、研究基础与工作条件” → 2 条）、
    `section_order`（交换一/二 → 顺序不符 + 编号不连续 ×2 = 3 条）、
    `figure_table_numbering`（删图2题注 → 1 条）、`cross_field`（封面插入 29.0 万元与
    预算表合计不符 → 1 条）、`reference_format`（同 campus → 2 条）。
- 解析状态样本 ×4（每个规则集）：空白页（`empty`）、扫描页（`scanned_no_text`）、
  扫描页+数字页码（`scanned_minimal_text`，R02 缺陷场景）、损坏 PDF（解析层拒绝）。
- **尚未覆盖**：dachuang 的 `reference_format`（其模板暂无“参考文献”节，需先补模板）；
  campus/dachuang 的 `word_limit`（两者都是**总字数**上限，追加文字必先撞页数上限；
  dachuang 实测 4700 字即触发 page_limit：见 `nsfc_word_limit_hit` 的对照说明）；
  nsfc 页数上限（40 页，实际不可达）；OCR 真机路径。
- `word_limit` 现有样本：`nsfc_word_limit_hit.docx` —— nsfc 的上限是**分章节**的
  （立项依据 ≤5000 字）且页数上限 40 页，因此可以孤立命中 1 条 warning。

- 产出目录：`eval/corpus/output/`（目录安全模型，见下节）
  - `campus_clean.docx` —— 干净样本（规则层 0 确定性命中；含人工确认项）
  - `*_hit.docx` ×14 —— 各规则类型“命中”样本
  - `pdf_*.pdf` ×4 —— 解析状态类样本（空白页 / 扫描页 / 扫描+页码 / 损坏文档）
  - `manifest.json` —— 每份文件的期望：`expected`（规则层 checker/severity 计数）、
    `expected_pages`（逐页状态）、`expected_outcome`（任务层 validation/结论/review_complete）、
    `expected_manual_min`、`expected_parse_error`；含 `selection` 字段记录本次选择
  - `selfcheck.txt` —— 分层自检结果（解析层 / 规则层 / 任务层 + 场景检查）
  - `.sample_pool_managed.json` —— 受管目录标记（证明该目录归本工具所有）
- 自定义目录：`--out D:\我的目录`
- 人工验证：直接打开 output 下的 docx/pdf，对照 manifest.json 期望逐份确认。

## 自检分层（R10）

样本自检不再只比较“命中了几个问题”，而是分三层，并与正式流程共用代码：

| 层 | 内容 | 复用来源 |
|---|---|---|
| 解析层 | DOCX→PDF、PDFParser、逐页状态与原因（`scanned_no_text` / `scanned_minimal_text` / `empty` / `ocr*` / `mixed_image_unread`） | `PDFParser` + `PageMeta` |
| 规则层 | RuleEngine 确定性命中（error/warning，manual 不计）+ 人工确认项 + 逐规则状态（failed/skipped/degraded） | `RuleEngine` |
| 任务层 | 完整性校验（字数、逐页覆盖）→ 结算（计数/结论/review_complete/ai_complete） | `routes._validate_extracted_text`、`_validate_page_coverage`、`_page_quality_manual_issues`、`routes.summarize_review`（**唯一实现**，正式流程同用） |

另有两条**场景检查**（复用干净样本 IR，不额外转换、不写库）：

1. 空规则集 → 结论必须 `incomplete`、`review_complete=False`（不得判 pass）；
2. 检查器抛异常 → 规则状态 `failed` 被如实记录、结论 `incomplete`、`review_complete=False`。

因此“样本池自检通过”的含义是：**该样本在解析、规则、正式结论三层都符合作者写下的期望**；
它与“单元测试通过”“正式任务符合预期”是三件不同的事（后者由 `backend/tests` 的 API 级用例覆盖）。

## 输出目录安全模型（R01）

本工具只清理“能证明属于自己”的产物，任何情况下都不会删除无关文件：

1. **归属判定**（`eval/corpus/output_safety.py`）：
   - 目录内有受管标记 `.sample_pool_managed.json` → 自有目录，可替换标记中列出的旧产物；
   - 无标记但目录内容可验证全部是本工具已知产物（manifest 声明与磁盘一致）→
     视为本工具旧版产物，允许接管一次并补写标记；
   - 目录不存在或为空 → 由本工具创建/接管；
   - **无标记且非空（含任何无法证明是本工具产物的文件）→ 拒绝运行，绝不删除**。
2. **危险目录拒绝**：磁盘根、用户主目录、桌面、项目根、外层工作区根
   （快照/验收文档所在）、真实业务数据目录（`backend/review.db`、`uploads`、`outputs`）
   一律拒绝，拒绝发生在任何文件操作之前；路径按真实路径判定（解析符号链接/junction）。
3. **失败保留旧成果**：所有构建与自检先写入目标目录内的同卷暂存子目录
   `.sample-pool-stage-*`；任何一步失败只删除本次暂存，上一份有效产物与标记原样保留，
   退出码非 0。发布采用逐文件 `os.replace`（同卷原子替换）。
4. **发布只替换旧受管产物**：目标位置存在“不在旧产物清单中”的同名文件（来源不明）→
   中止并报错，不覆盖。
5. **重复执行**：每轮替换上一轮成功发布的受管产物；目录内你自行放入的文件
   （如 `note.txt`）跨轮次保留，不会被清空。
6. **选择零样本**（`--exclude` 全量等）：直接提示并退出，不做任何改动。

失败恢复边界（诚实说明）：发布瞬间（删除旧产物后、逐文件替换完成前）若进程被
强杀，可能留下“新旧混合”目录；此时受管标记仍是上一轮清单，目录内会多出若干
未被标记的新文件。下一次运行会因“存在非受管同名文件”或“标记清单与实际不符”而
中止并提示 —— 不会静默覆盖或删除，人工确认后可重跑恢复。

样本可信来源：干净模板 = `eval/samples/sample_proposal.docx`（已校准：标题微软雅黑
加粗、附件行无行首数字、页边距满足 layout 下限）；hit 样本复用
`eval/inject_errors.py` 经验证的注入器或文档级 mutator，期望按实测校准（含同键
多实例与跨键副作用标注）。默认不调用真实模型。

自检环境说明（诚实标注）：`generate_samples.py` 与 `selfcheck.py` 固定
`OCR_ENABLED=0` —— 自检离线、确定性强：图像页（无数字文字）统一归为
`scanned_no_text`，不会触发外部 OCR 依赖；这不代表生产 OCR 路径已在本机验证
（生产 OCR 需 tesseract 语言包，未在本机核验）。

## 覆盖矩阵（campus 规则集，v4 明细；dachuang/nsfc 见上方支持状态表）

| 样本 | 类别 | 规则层期望（error/warning） | 任务层期望 | 说明 |
|---|---|---|---|---|
| campus_clean.docx | clean | 0 | ok / incomplete / 未完成 | 干净基线；含 ≥1 条人工确认项 |
| required_sections_hit.docx | hit | required_sections error + heading_numbering warning | ok / incomplete / 未完成 | 删“研究方法”节（真实副作用已标注） |
| page_limit_hit.docx | hit | page_limit error | ok / incomplete / 未完成 | 复制内容超 20 页上限 |
| budget_check_hit_total.docx | hit | budget_check error | ok / incomplete / 未完成 | 总额 30.0→25.0 勾稽不平 |
| budget_check_hit_ratio.docx | hit | budget_check error ×2 | ok / incomplete / 未完成 | 管理费 20%（2 条真命中） |
| figure_table_numbering_hit.docx | hit | figure_table_numbering warning | ok / incomplete / 未完成 | 删图2 题注 |
| section_order_hit.docx | hit | section_order warning + heading_numbering warning ×3 | ok / incomplete / 未完成 | 交换两节（真实副作用已标注） |
| cross_field_hit.docx | hit | cross_field_consistency warning | ok / incomplete / 未完成 | 封面题名错字 |
| date_check_hit.docx | hit | date_check warning ×2 | ok / incomplete / 未完成 | 调换起止时间（2 条真命中） |
| signature_page_hit.docx | hit | signature_page error | ok / incomplete / 未完成 | 删签字盖章页 |
| font_check_hit.docx | hit | font_check warning | ok / incomplete / 未完成 | 正文全改楷体 |
| required_fields_hit.docx | hit | required_fields error | ok / incomplete / 未完成 | 删封面“填表日期” |
| field_format_hit.docx | hit | field_format warning | ok / incomplete / 未完成 | 联系电话 12345 |
| heading_numbering_hit.docx | hit | heading_numbering warning + layout_check warning | ok / incomplete / 未完成 | 重复“二、…”（未加粗） |
| layout_margin_hit.docx | hit | layout_check warning | ok / incomplete / 未完成 | 左边距 31.7mm→10mm |
| reference_format_hit.docx | hit | reference_format warning ×2 | ok / incomplete / 未完成 | 前两条文献缺类型标识与年份（确定性分支） |
| pdf_blank_mid.pdf | parse_status | 0 | ok / incomplete / 未完成 | p3=`empty`（正常空白页，非失败） |
| pdf_scanned_mid.pdf | parse_status | 0 | **failed** | 整页扫描 → `scanned_no_text`，正式任务被拒绝 |
| pdf_scanned_pagenum.pdf | parse_status | 0 | **failed** | R02 缺陷场景：扫描页 + 数字页码“2” → `scanned_minimal_text` |
| pdf_damaged.pdf | parse_status | —（解析层拒绝） | **parse_error** | 带 PDF 头但正文非法；正确拒绝即通过 |
| 场景：空规则集 | scenario | — | incomplete / 未完成 | `no_executable_rules=True` 不得判 pass |
| 场景：规则异常 | scenario | 规则状态 failed | incomplete / 未完成 | 检查器抛错被如实记录 |

覆盖要点（诚实状态）：

- campus 规则集下附件清单/签章/页码会产生 `manual_required`，因此**所有正常文档的结论都是
  `incomplete`、`review_complete=False`**；这是“未完成有效审查不得判通过”的体现，不是缺陷。
- `page_number_check` / `attachment_checklist` 在 campus 为 **info 级**；`pdf_blank_mid.pdf`
  验证“正常空白页不产生任何 error/warning 误报”。
- `word_limit`（campus 上限 15000 字）：干净模板 7158 字/16 页，超限需再填 7800+ 字必先破
  20 页上限 → **无法在 campus 模板上孤立命中**，待 dachuang/nsfc 专用短模板补。
- `reference_format`（**R16 事实更正**）：检查器**已实现并注册** ——
  `backend/app/services/llm/checkers/__init__.py` 的 `ReferenceFormatChecker`。
  它虽然放在 `llm/` 目录下，但“出版年份 / 文献类型标识”判定是**确定性分支**，
  `semantic_review=false`（三个规则集都是 false）时不会调用模型、不外发材料；
  只有显式开启 `semantic_review` 才走 AI 复核。样本 `reference_format_hit.docx`
  即命中该确定性分支（2 条）。
- `anonymity / title_content / cross_consistency_semantic`（AI）默认剔除，不调用真实模型。
- OCR 开启路径未在本机验证（见上节“自检环境说明”）；真实 tesseract 运行时未跑。
- 损坏样本不能用“按比例截断”生成（实测部分截断会被 MuPDF 修复后成功打开），
  因此 `pdf_damaged.pdf` 使用确定的非法正文载荷。

## 下一步扩展（待确认顺序）

1. dachuang/nsfc 的章节/预算/日期类命中样本（按各自模板设计注入器）；
2. 给 dachuang/nsfc 模板补“参考文献”节，从而补其 `reference_format` 样本；
3. `word_limit` 孤立命中样本（需要“字数达标但页数不超”的专用模板）；
4. 边界/反例深化：layout 字号/行距边界、页眉页码 hit、OCR 真机路径。

恢复上下文（历史）：早期基线发现与解析器数字碎片修复等见 `pdf_parser.py`
纯数字碎片防护、`generate_sample_docx.py` 标题加粗校准、`inject_errors.py` 的
instances/extra 标注机制。
