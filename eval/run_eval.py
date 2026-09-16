"""运行隔离的 20 样本 M4 端到端评测（FastAPI TestClient）；退出码：0=跑通且全部 done 且阈值达标，
1=基础设施失败（样本/依赖/认证/配置缺失或非法），2=有样本任务未正常 done，3=全部 done 但未达 EVAL_MIN_PRECISION/EVAL_MIN_RECALL。
隔离：import app 前设独立数据库/上传/输出/日志环境变量，不触碰正式 review.db、uploads、outputs，不花真实密钥；保持 AUTH_REQUIRED=true 并用隔离库临时用户真实登录，LLM 默认禁用。
口径：报告并列旧口径（(checker,severity) 集合去重）、实例口径（一对一）与身份口径（R09 锚点）；阈值判定用实例口径，结果目录可用 EVAL_RESULTS_DIR 覆盖，运行前把既有 report.md 备份到 <目录>/backup/。"""
from __future__ import annotations

import hashlib
import json
import os
import random
import secrets
import shutil
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

# ── 路径常量 ──────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
EVAL_DIR = Path(__file__).resolve().parent
SAMPLES_DIR = EVAL_DIR / "samples"
# 结果目录可用 EVAL_RESULTS_DIR 覆盖（例如验收时写到独立位置，不覆盖仓库内历史报告）
RESULTS_DIR = Path(os.environ.get("EVAL_RESULTS_DIR") or str(EVAL_DIR / "results"))
REPORT_PATH = RESULTS_DIR / "report.md"
DIAG_DIR = RESULTS_DIR / "diagnostics"

SOURCE_DOCX = SAMPLES_DIR / "sample_proposal.docx"
SEED = 20260713

# ── 编码：Windows 控制台默认 cp1252，日志里的「→」等字符会触发 UnicodeEncodeError ──
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

# ── 隔离配置：必须在 import app（会初始化 settings/engine/startup_services）之前 ──
_runtime = Path(tempfile.mkdtemp(prefix="eval-runtime-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_runtime / 'review.db').as_posix()}"
os.environ["UPLOAD_DIR"] = str(_runtime / "uploads")
os.environ["OUTPUT_DIR"] = str(_runtime / "outputs")
# 规则目录复制进隔离运行时：评测不读取真实 rules 目录，外部改动不影响评测结果
shutil.copytree(PROJECT_ROOT / "rules", _runtime / "rules")
os.environ["RULES_DIR"] = str(_runtime / "rules")
os.environ["LLM_API_KEY"] = ""          # 默认禁用真实模型，无外部请求
os.environ["ACCESS_TOKEN"] = ""         # 不依赖外围访问凭据
os.environ["ADMIN_TOKEN"] = ""
os.environ["AUTH_SECRET"] = secrets.token_hex(32)
os.environ["AUTH_REQUIRED"] = "true"    # 保持认证开启
os.environ["DEBUG"] = "false"
os.environ["LOG_FILE"] = str(_runtime / "eval.log")

# 只有显式 EVAL_USE_LLM=true 才启用真实模型（默认关闭，避免费用）
_use_llm = os.environ.get("EVAL_USE_LLM", "").lower() in {"1", "true", "yes"}

sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(PROJECT_ROOT))

from starlette.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.services import auth_service  # noqa: E402
from eval.inject_errors import (  # noqa: E402
    GROUND_TRUTH,
    annotation_evidence,
    expected_instances_for,
    expected_issues_for,
    inject_errors,
)
from eval.metrics import (  # noqa: E402
    checker_counts,
    classify_issue as _classify_issue,
    match_identified,
    match_issues,
    precision_recall_f1 as _metrics,
    set_based_counts,
)


class InfrastructureError(RuntimeError):
    """样本、依赖或认证等环境缺失导致的失败。"""


def _sample_errors(rng: random.Random) -> list[list[int]]:
    samples: list[list[int]] = []
    all_errors = list(range(1, 11))

    def draw(required: int | None = None) -> list[int]:
        while True:
            count = rng.randint(2, 5)
            selected = set(rng.sample(all_errors, count))
            if required is not None:
                selected.add(required)
                while len(selected) > 5:
                    selected.remove(rng.choice(sorted(selected - {required})))
            # 错误1（删研究方法）与 6/7（图表/章节顺序）会互相干扰，避免组合
            if 1 in selected and ({6, 7} & selected):
                continue
            # 错误3（总额改25）与 4（管理费→20%）会共享“勾稽不平”症状，
            # 实例级标注需两两互斥才能保持每注入器期望唯一（B-corp 校准）。
            if 3 in selected and 4 in selected:
                continue
            # 错误1 删除“研究方法”整节会同时删掉正文里标题的第二处引用，
            # 使 #8（改标题一个字）失去可比对对象 → 组合下期望不可达，需互斥。
            if 1 in selected and 8 in selected:
                continue
            # 错误1 删节会减少总页数，可能使 #2（页数超限）不再超限 → 期望不可达。
            if 1 in selected and 2 in selected:
                continue
            return sorted(selected)

    for required in all_errors:
        samples.append(draw(required))
    for _ in range(10):
        samples.append(draw())
    return samples


def _make_client(client: TestClient) -> dict[str, str]:
    """在隔离库内创建随机密码的临时用户并真实登录，返回带 Bearer 的请求头。"""
    username = f"eval_{secrets.token_hex(6)}"
    password = secrets.token_hex(16)
    auth_service.create_user(
        {"username": username, "password": password, "role": "user"},
        actor_role="super_admin",
    )
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    if resp.status_code != 200:
        raise InfrastructureError(f"临时用户登录失败：HTTP {resp.status_code}")
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def run_evaluation(
    client: TestClient,
    *,
    min_precision: float | None = None,
    min_recall: float | None = None,
) -> tuple[str, int, dict]:
    """跑完整评测，返回 (报告文本, 未正常完成的任务数, 指标摘要)。"""
    if not SOURCE_DOCX.exists():
        raise InfrastructureError(
            f"干净样本不存在：{SOURCE_DOCX}，请先运行 generate_sample_docx.py"
        )

    # 认证开启时，未携带凭据的请求应被拒绝
    with SOURCE_DOCX.open("rb") as fh:
        unauth = client.post(
            "/api/v1/documents",
            files={
                "file": (
                    SOURCE_DOCX.name,
                    fh,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )
    if unauth.status_code != 401:
        raise InfrastructureError(f"未认证请求应返回 401，实际 {unauth.status_code}")

    headers = _make_client(client)

    rng = random.Random(SEED)
    samples = _sample_errors(rng)
    overall_old = defaultdict(int)
    overall_new = defaultdict(int)
    overall_identified = defaultdict(int)
    per_checker_old: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    per_checker_new: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    per_checker_identified: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    page_mismatch_total: list[dict] = []
    manual_required_count = 0
    degraded_count = 0
    durations: list[float] = []
    task_failures: list[int] = []

    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    for index, errors in enumerate(samples, start=1):
        sample_path = _runtime / f"sample_{index:02d}.docx"
        # 注入错误并生成带错样本；期望按注入编号实例级展开（含 instances 标注）
        _, _ = inject_errors(SOURCE_DOCX, errors, sample_path)
        expected_issues = expected_issues_for(errors, include_anonymity=_use_llm)
        # R09：身份口径期望（带目标字段与证据锚点），与实例口径期望实例数一致
        expected_instances = expected_instances_for(errors, include_anonymity=_use_llm)

        started_at = time.monotonic()
        with sample_path.open("rb") as stream:
            upload = client.post(
                "/api/v1/documents",
                files={
                    "file": (
                        sample_path.name,
                        stream,
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                },
                headers=headers,
            )
        if upload.status_code != 200:
            raise InfrastructureError(f"样本 {index} 上传失败：HTTP {upload.status_code}")
        review = client.post(
            "/api/v1/reviews",
            data={
                "document_id": upload.json()["document_id"],
                "ruleset_id": "campus_general_v1",
                "use_ai": str(_use_llm).lower(),
                "privacy_consent": str(_use_llm).lower(),
            },
            headers=headers,
        )
        if review.status_code != 200:
            raise InfrastructureError(f"样本 {index} 创建审查失败：HTTP {review.status_code}")
        review_id = review.json()["review_id"]

        deadline = time.monotonic() + 900
        payload: dict = {}
        while time.monotonic() < deadline:
            result = client.get(f"/api/v1/reviews/{review_id}", headers=headers)
            result.raise_for_status()
            payload = result.json()
            if payload.get("status") in {"done", "failed", "cancelled", "timed_out"}:
                break
            time.sleep(0.2)
        if payload.get("status") != "done":
            task_failures.append(index)
            continue
        durations.append(time.monotonic() - started_at)

        # ── 三类口径拆分预测（保留实例级清单用于一对一匹配与诊断留痕） ──
        predicted_deterministic: list[dict] = []
        manual_required_keys: set[tuple[str, str]] = set()
        degraded_keys: set[tuple[str, str]] = set()
        for issue in payload.get("issues", []):
            kind = _classify_issue(issue)
            if kind == "manual_required":
                manual_required_keys.add(
                    (issue.get("checker", ""), issue.get("severity", ""))
                )
                manual_required_count += 1
            elif kind == "degraded":
                degraded_keys.add(
                    (issue.get("checker", ""), issue.get("severity", ""))
                )
                degraded_count += 1
            else:
                predicted_deterministic.append(issue)

        # 旧口径（集合去重）、实例口径（问题实例一对一）与身份口径（R09 定位）并行统计
        counts_old = set_based_counts(predicted_deterministic, expected_issues)
        match = match_issues(predicted_deterministic, expected_issues)
        counts_new = match["counts"]
        identified = match_identified(predicted_deterministic, expected_instances)
        counts_identified = identified["counts"]
        for key, value in counts_old.items():
            overall_old[key] += value
        for key, value in counts_new.items():
            overall_new[key] += value
        for key, value in counts_identified.items():
            overall_identified[key] += value
        page_mismatch_total.extend(identified.get("page_mismatches", []))

        predicted_checkers = {
            item.get("checker", "") for item in predicted_deterministic
        }
        expected_checkers = {item["checker"] for item in expected_issues}
        for checker in sorted(predicted_checkers | expected_checkers):
            old_counts = set_based_counts(
                [i for i in predicted_deterministic if i.get("checker", "") == checker],
                [e for e in expected_issues if e["checker"] == checker],
            )
            for key, value in old_counts.items():
                per_checker_old[checker][key] += value
        for checker, counts in checker_counts(match).items():
            for key, value in counts.items():
                per_checker_new[checker][key] += value
        for expected_item, _actual in identified["tp_identified"]:
            per_checker_identified[expected_item["checker"]]["tp_identified"] += 1
        for expected_item, _actual in identified["tp_unverified"]:
            per_checker_identified[expected_item["checker"]]["tp_unverified"] += 1
        for actual in identified["fp"]:
            per_checker_identified[actual.get("checker", "")]["fp"] += 1
        for expected_item in identified["fn"]:
            per_checker_identified[expected_item["checker"]]["fn"] += 1

        # ── 逐样本诊断留痕（含每个 TP/FP/FN 的预期与实际明细） ──
        diag = {
            "sample": index,
            "injected_errors": errors,
            "expected": sorted([f"{e['checker']}/{e['severity']}" for e in expected_issues]),
            "deterministic": sorted(
                f"{i.get('checker', '')}/{i.get('severity', '')}"
                for i in predicted_deterministic
            ),
            "manual_required": sorted(f"{c}/{s}" for c, s in manual_required_keys),
            "degraded": sorted(f"{c}/{s}" for c, s in degraded_keys),
            "metric_scope": {
                "set_based_legacy": counts_old,
                "instance_one_to_one": counts_new,
                "identity_located": counts_identified,
            },
            "identity_details": {
                "tp_identified": [
                    {
                        "target": exp.get("target", ""),
                        "anchors": exp.get("anchors", []),
                        "expected": f"{exp['checker']}/{exp['severity']}",
                        "actual_evidence": (act.get("evidence") or act.get("message") or "")[:200],
                    }
                    for exp, act in identified["tp_identified"]
                ],
                "tp_unverified": [
                    {
                        "target": exp.get("target", ""),
                        "expected": f"{exp['checker']}/{exp['severity']}",
                        "actual": f"{act.get('checker', '')}/{act.get('severity', '')}",
                    }
                    for exp, act in identified["tp_unverified"]
                ],
                "fp_details": [
                    {
                        "actual": f"{act.get('checker', '')}/{act.get('severity', '')}",
                        "evidence": (act.get("evidence") or act.get("message") or "")[:200],
                    }
                    for act in identified["fp"]
                ],
                "fn_details": [
                    {
                        "expected": f"{exp['checker']}/{exp['severity']}",
                        "target": exp.get("target", ""),
                        "anchors": exp.get("anchors", []),
                    }
                    for exp in identified["fn"]
                ],
                "page_mismatches": identified.get("page_mismatches", []),
            },
            "annotation_evidence": annotation_evidence(errors),
            "tp_details": [
                {"expected": exp, "actual": act} for exp, act in match["tp"]
            ],
            "fp_details": [{"actual": act} for act in match["fp"]],
            "fn_details": [{"expected": exp} for exp in match["fn"]],
            "conclusion": payload.get("conclusion"),
        }
        (DIAG_DIR / f"sample_{index:02d}.json").write_text(
            json.dumps(diag, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    precision_old, recall_old, f1_old, note_old = _metrics(overall_old)
    precision_new, recall_new, f1_new, note_new = _metrics(overall_new)
    precision_id, recall_id, f1_id, note_id = _metrics(overall_identified)
    source_sha = hashlib.sha256(SOURCE_DOCX.read_bytes()).hexdigest()[:12]
    ruleset_sha = hashlib.sha256(
        (Path(os.environ["RULES_DIR"]) / "campus.yaml").read_bytes()
    ).hexdigest()[:12]

    # ── 执行环境与代码版本留痕（用于复现审计） ──
    import importlib.metadata as _meta
    import platform
    from datetime import datetime

    try:
        pymupdf_version = _meta.version("PyMuPDF")
    except Exception:
        pymupdf_version = "unknown"
    from app.core.config import get_settings as _get_settings
    from app.services.llm.client import PROMPT_VERSION
    from app.services.parser.converter import _find_libreoffice as _find_soffice

    app_settings = _get_settings()
    app_version = f"{app_settings.APP_VERSION} (build {app_settings.APP_BUILD_ID})"
    injector_sha = hashlib.sha256(
        (EVAL_DIR / "inject_errors.py").read_bytes()
    ).hexdigest()[:12]
    soffice_path = _find_soffice()
    if soffice_path:
        try:
            import subprocess
            proc = subprocess.run(
                [soffice_path, "--version"],
                capture_output=True,
                timeout=10,
                encoding="utf-8",
                errors="replace",
                creationflags=(
                    subprocess.CREATE_NO_WINDOW
                    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW")
                    else 0
                ),
            )
            soffice_version = (
                (proc.stdout or "").strip().splitlines()[0]
                if proc.stdout and proc.stdout.strip() else "unknown"
            )
        except Exception:
            soffice_version = "unknown"
    else:
        soffice_version = "未找到（DOCX 转换将失败）"

    threshold_met: bool | None = None
    threshold_lines: list[str] = []
    if min_precision is not None or min_recall is not None:
        req_precision = min_precision if min_precision is not None else 0.0
        req_recall = min_recall if min_recall is not None else 0.0
        if note_new:
            threshold_met = False
        else:
            threshold_met = precision_new >= req_precision and recall_new >= req_recall
        threshold_lines = [
            "",
            f"- 达标阈值（新口径）：Precision ≥ {req_precision}，Recall ≥ {req_recall}",
            f"- 阈值结果：{'达标' if threshold_met else '未达标'}"
            + (f"（{note_new}）" if note_new else ""),
        ]

    mode_note = (
        "LLM 已启用（EVAL_USE_LLM=true），注入器⑤纳入统计。"
        if _use_llm
        else "未启用 LLM（默认），注入器⑤（anonymity_check）从统计中剔除，无外部模型请求。"
    )
    lines = [
        "# M4 形式审查评测报告",
        "",
        f"- 样本数：{len(samples)}（每份随机注入 2-5 种错误）",
        f"- 模式：{mode_note}",
        f"- 随机种子：{SEED}",
        f"- 干净样本 sha256：{source_sha[:12]}",
        f"- 规则快照（campus.yaml）sha256：{ruleset_sha[:12]}（隔离运行时副本）",
        f"- 应用版本：{app_version}",
        f"- 提示词版本：{PROMPT_VERSION}",
        f"- Python：{platform.python_version()}",
        f"- PyMuPDF 版本：{pymupdf_version}",
        f"- LibreOffice：{soffice_version}",
        f"- 注入器 inject_errors.py sha256：{injector_sha}",
        f"- 认证：AUTH_REQUIRED=true，隔离库临时用户真实登录（无外围凭据）",
        f"- 运行时间：{datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"- 平均单份耗时：{sum(durations) / len(durations):.2f} 秒（{len(durations)} 份完成）" if durations else "- 平均单份耗时：无完成样本",
        "",
        "## 指标口径说明",
        "",
        "- 只对「确定性违规(deterministic)」计算 Precision/Recall/F1。",
        "- 「需人工确认(manual_required)」与「降级(degraded)」不计入假阳性，单独计数（单位：问题实例条数）。",
        "- 旧口径（历史基线）：统计单位为 (checker, severity) 集合去重，同规则同级别多个问题合并为 1。",
        "- 新口径（当前阈值判定）：统计单位为问题实例，按 (checker, severity) 一对一贪心匹配，重复问题计入 FP/FN。",
        "- 身份口径（R09 新增）：在实例口径之上按**标注身份**匹配——预期实例带 `anchors`"
        "（证据锚点，取自注入语义与检查器消息模板，逐条依据见 diagnostics 的"
        " `annotation_evidence`）；只有预测实例的 evidence/message 命中全部锚点才算"
        " `tp_identified`（位置/字段已核实），命中不了的预期记 FN、配不上的预测记 FP，"
        "**同类同级但字段/位置不同不得互相顶替**。未带锚点的实例按规则级配对，"
        "单独计入 `tp_unverified`，不与已核实命中混算。",
        "- 页码只作提示（差 >1 页记入 `page_mismatches`），不作为配对条件：DOCX→PDF"
        " 转换与版式变化会造成合理位移。",
        "- **口径变化不等于算法变好**：三行数字的差异来自统计与匹配规则，不得用来宣称"
        " 检出能力提升；阈值判定仍沿用实例口径（未擅自更改验收标准）。",
        "",
        "## 总体指标（确定性违规，三种口径对比）",
        "",
        "| 口径 | Precision | Recall | F1 | TP | FP | FN |",
        "|---|---:|---:|---:|---:|---:|---:|",
        f"| 旧（集合去重） | {precision_old:.4f} | {recall_old:.4f} | {f1_old:.4f} | {overall_old['tp']} | {overall_old['fp']} | {overall_old['fn']} |",
        f"| 新（问题实例一对一） | {precision_new:.4f} | {recall_new:.4f} | {f1_new:.4f} | {overall_new['tp']} | {overall_new['fp']} | {overall_new['fn']} |",
        f"| 身份（实例定位，R09） | {precision_id:.4f} | {recall_id:.4f} | {f1_id:.4f} | "
        f"{overall_identified['tp']} | {overall_identified['fp']} | {overall_identified['fn']} |",
        "",
        f"- 身份口径细分：位置/字段已核实命中 `tp_identified` = {overall_identified['tp_identified']}，"
        f"仅规则级命中 `tp_unverified` = {overall_identified['tp_unverified']}，"
        f"页码位移提示 {len(page_mismatch_total)} 条。",
        "",
        f"> 旧口径：{note_old}" if note_old else "",
        f"> 新口径：{note_new}" if note_new else "",
        f"> 身份口径：{note_id}" if note_id else "",
        *threshold_lines,
        "",
        f"- 需人工确认项：{manual_required_count} 条",
        f"- 降级项（AI 未执行等）：{degraded_count} 条",
        f"- 未正常完成的任务：{len(task_failures)} 个" + (f"（样本 {task_failures}）" if task_failures else ""),
        "",
        "## 分 Checker 指标（新口径：问题实例一对一）",
        "",
        "| Checker | Precision | Recall | F1 | TP | FP | FN |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    evaluated_checkers = {item["checker"] for item in GROUND_TRUTH.values()}
    if not _use_llm:
        evaluated_checkers.discard("anonymity_check")
    evaluated_checkers.update(per_checker_new)
    evaluated_checkers.update(per_checker_old)
    for checker in sorted(evaluated_checkers):
        counts = per_checker_new[checker]
        p, r, checker_f1, _ = _metrics(counts)
        lines.append(
            f"| {checker} | {p:.4f} | {r:.4f} | {checker_f1:.4f} | "
            f"{counts.get('tp', 0)} | {counts.get('fp', 0)} | {counts.get('fn', 0)} |"
        )
    lines += [
        "",
        "## 分 Checker 指标（旧口径：集合去重）",
        "",
        "| Checker | Precision | Recall | F1 | TP | FP | FN |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for checker in sorted(evaluated_checkers):
        counts = per_checker_old[checker]
        p, r, checker_f1, _ = _metrics(counts)
        lines.append(
            f"| {checker} | {p:.4f} | {r:.4f} | {checker_f1:.4f} | "
            f"{counts.get('tp', 0)} | {counts.get('fp', 0)} | {counts.get('fn', 0)} |"
        )
    lines += [
        "",
        "## 分 Checker 指标（身份口径：实例定位，R09）",
        "",
        "| Checker | 已核实命中 | 仅规则级命中 | FP | FN |",
        "|---|---:|---:|---:|---:|",
    ]
    for checker in sorted(set(evaluated_checkers) | set(per_checker_identified)):
        counts = per_checker_identified[checker]
        lines.append(
            f"| {checker} | {counts.get('tp_identified', 0)} | "
            f"{counts.get('tp_unverified', 0)} | {counts.get('fp', 0)} | {counts.get('fn', 0)} |"
        )
    lines += [
        "",
        "> 「已核实命中」= 预测实例的 evidence/message 命中该实例的全部证据锚点；",
        "> 「仅规则级命中」= 该实例的标注未提供可核实锚点（或标注声明不可定位），只核对规则与级别。",
        "> FP/FN 的含义与实例口径一致，但身份口径下「同类同级但位置/字段不符」会同时产生 FP 与 FN。",
    ]
    report = "\n".join(lines) + "\n"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    if REPORT_PATH.exists():
        # 保留上一次报告，防止评测覆盖丢失历史基线
        backup_dir = RESULTS_DIR / "backup"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(REPORT_PATH, backup_dir / f"report.{stamp}.md")
    REPORT_PATH.write_text(report, encoding="utf-8")
    summary = {
        "precision_old": precision_old,
        "recall_old": recall_old,
        "precision_new": precision_new,
        "recall_new": recall_new,
        "precision_identified": precision_id,
        "recall_identified": recall_id,
        "tp_identified": overall_identified["tp_identified"],
        "tp_unverified": overall_identified["tp_unverified"],
        "page_mismatches": len(page_mismatch_total),
        "note_new": note_new,
        "threshold_met": threshold_met,
    }
    return report, len(task_failures), summary


def _threshold(name: str) -> float | None:
    """解析 0~1 的阈值环境变量；未设置返回 None，非法值按基础设施失败处理。"""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        raise InfrastructureError(f"{name} 必须是 0~1 的数字，当前值为 {raw!r}")
    if not 0.0 <= value <= 1.0:
        raise InfrastructureError(f"{name} 必须在 0~1 之间，当前值为 {raw!r}")
    return value


def main() -> int:
    exit_code = 0
    report = ""
    summary: dict = {}
    try:
        min_precision = _threshold("EVAL_MIN_PRECISION")
        min_recall = _threshold("EVAL_MIN_RECALL")
        with TestClient(app) as client:  # 完整生命周期：进入触发 startup，退出触发 shutdown
            report, task_failures, summary = run_evaluation(
                client,
                min_precision=min_precision,
                min_recall=min_recall,
            )
        if task_failures:
            # 评测跑通，但存在任务未正常 done → 退出码 2
            exit_code = 2
        elif summary.get("threshold_met") is False:
            # 全部任务 done，但新口径指标未达到配置的阈值 → 退出码 3
            print(
                "[指标未达标] 新口径 "
                f"Precision={summary.get('precision_new'):.4f}, "
                f"Recall={summary.get('recall_new'):.4f}"
            )
            exit_code = 3
    except InfrastructureError as exc:
        print(f"[基础设施失败] {exc}")
        exit_code = 1
    except Exception as exc:
        print(f"[未预期失败] {exc}")
        exit_code = 1
    finally:
        # 先关闭数据库连接，再清理本次临时资源
        try:
            from app.models.base import engine
            engine.dispose()
        except Exception:
            pass
        shutil.rmtree(_runtime, ignore_errors=True)

    if report:
        sys.stdout.write(report)
    print(f"\n报告已写入：{REPORT_PATH}")
    print(f"逐样本诊断已写入：{DIAG_DIR}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
