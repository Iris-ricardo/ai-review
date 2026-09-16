"""Corpus self-check runner（样本池分层自检，R10）：对 .docx/.pdf 跑与正式审查一致的链路并分三层报告——解析层（LibreOffice 转换 + PDFParser + 逐页状态/原因）、规则层（RuleEngine 跳过 AI 规则 + 确定性命中 + 人工确认项）、任务层（正式流程完整性校验与结算 routes.summarize_review → validation / 结论 / review_complete）。

用法：python eval/corpus/selfcheck.py --docx <file> --ruleset campus [--expect-hit NAME | --expect-clean NAME | --expect-page 3:empty | --expect-task ok | --expect-conclusion incomplete | --expect-review-complete false | --expect-parse-error]。
退出码：0 全部期望满足；1 有期望不符（输出标明失败发生在解析/规则/任务哪一层）。"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

# Windows 控制台/重定向默认 cp1252：中文输出先重配 UTF-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = PROJECT_ROOT / "backend"
RULES_DIR = PROJECT_ROOT / "rules"
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 自检运行环境：只读规则目录、隔离转换产物（不需要数据库）、不调用外部模型/OCR
os.environ.setdefault("UPLOAD_DIR", str(tempfile.mkdtemp(prefix="corpus-tmp-")))
os.environ.setdefault("OUTPUT_DIR", str(tempfile.mkdtemp(prefix="corpus-tmp-")))
os.environ.setdefault("LLM_API_KEY", "")
os.environ.setdefault("ACCESS_TOKEN", "")
os.environ.setdefault("ADMIN_TOKEN", "")
os.environ.setdefault("RULES_DIR", str(RULES_DIR))
os.environ.setdefault("OCR_ENABLED", "0")  # 离线自检：图像页统一归为 scanned_no_text

from review_layers import page_status_map, run_layers  # noqa: E402


def summarize(payload: dict) -> None:
    parse = payload.get("parse", {})
    print(f"文件：{payload['file']}  规则集：{payload['ruleset']}")
    if not parse.get("ok"):
        print(f"[解析层] 失败（正确拒绝也算通过）：{parse.get('error')}")
        print(f"[任务层] validation={payload.get('task', {}).get('validation')}")
        return
    print(f"[解析层] 页数={parse['pages']} 字符总数={parse['word_count']} "
          f"schema={parse['schema_version']} OCR页={parse['ocr_pages']}")
    for item in parse["page_status"]:
        if item["status"] != "ok":
            print(f"    p{item['page']}: {item['status']} —— {item['reason']}")
    if parse.get("parse_warnings"):
        print(f"    解析降级：{parse['parse_warnings']}")

    rules = payload.get("rules", {})
    hits = rules.get("hits", [])
    manual = rules.get("manual", [])
    print(f"[规则层] 可执行规则={rules.get('enabled_rules', 0)} "
          f"确定性命中={len(hits)} 人工确认项={len(manual)}")
    for hit in hits:
        print(f"    [{hit['checker']}] {hit['severity']} p{hit['page']} {hit['message']}")
    failed = {key: value for key, value in rules.get("statuses", {}).items()
              if value in {"failed", "timed_out", "degraded", "skipped"}}
    if failed:
        print(f"    规则状态异常：{failed}")

    task = payload.get("task", {})
    print(f"[任务层] validation={task.get('validation')} "
          f"结论={task.get('conclusion')} review_complete={task.get('review_complete')} "
          f"errors={task.get('errors')} manual={task.get('manual_required_checks')} "
          f"incomplete_rules={task.get('incomplete_rules')}")
    if task.get("validation_error"):
        print(f"    完整性失败原因：{task['validation_error']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docx", required=True, help="样本 docx/pdf 路径")
    parser.add_argument("--ruleset", default="campus", help="规则集名或 YAML 路径")
    parser.add_argument("--expect-hit", action="append", default=[],
                        help="期望命中的 checker（可重复）")
    parser.add_argument("--expect-clean", action="append", default=[],
                        help="期望干净的 checker（可重复）")
    parser.add_argument("--expect-page", action="append", default=[],
                        help="期望逐页状态，格式 页码:状态（可重复）")
    parser.add_argument("--expect-task", choices=["ok", "failed", "parse_error"],
                        help="期望任务层 validation 结果")
    parser.add_argument("--expect-conclusion",
                        choices=["pass", "needs_revision", "incomplete"],
                        help="期望业务结论")
    parser.add_argument("--expect-review-complete", choices=["true", "false"],
                        help="期望 review_complete")
    parser.add_argument("--expect-parse-error", action="store_true",
                        help="期望文档在解析层被拒绝（损坏文档）")
    args = parser.parse_args()

    payload = run_layers(Path(args.docx), args.ruleset)
    summarize(payload)

    problems: list[str] = []
    parse = payload.get("parse", {})
    task = payload.get("task", {})
    hit_checkers = {
        hit["checker"] for hit in payload.get("rules", {}).get("hits", [])
    }

    if args.expect_parse_error:
        if parse.get("ok"):
            problems.append("[解析层] 期望解析失败，但解析成功")
    elif not parse.get("ok"):
        problems.append(f"[解析层] 解析失败：{parse.get('error')}")

    for checker in args.expect_hit:
        if checker not in hit_checkers:
            problems.append(f"[规则层] 期望命中但未命中：{checker}")
    for checker in args.expect_clean:
        if checker in hit_checkers:
            problems.append(f"[规则层] 期望干净但命中：{checker}")

    status_map = page_status_map(payload)
    for spec in args.expect_page:
        page_text, _, expected = spec.partition(":")
        try:
            page = int(page_text)
        except ValueError:
            problems.append(f"[参数] --expect-page 页码无效：{spec}")
            continue
        actual = status_map.get(page)
        if actual != expected:
            problems.append(
                f"[解析层] 第{page}页状态期望 {expected}，实际 {actual}"
            )

    if args.expect_task and task.get("validation") != args.expect_task:
        problems.append(
            f"[任务层] validation 期望 {args.expect_task}，实际 {task.get('validation')}"
        )
    if args.expect_conclusion and task.get("conclusion") != args.expect_conclusion:
        problems.append(
            f"[任务层] 结论期望 {args.expect_conclusion}，实际 {task.get('conclusion')}"
        )
    if args.expect_review_complete is not None:
        expected_flag = args.expect_review_complete == "true"
        if bool(task.get("review_complete")) is not expected_flag:
            problems.append(
                f"[任务层] review_complete 期望 {expected_flag}，"
                f"实际 {task.get('review_complete')}"
            )

    if problems:
        for problem in problems:
            print("FAIL:", problem)
        return 1
    print("OK：期望全部满足")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
