"""样本审查分层自检（R10）：解析层 / 规则层 / 任务层，与正式审查一致；不写数据库、不启动服务、不调用真实模型。

解析层=DOCX→PDF 转换 + PDFParser + 逐页状态与原因；规则层=RuleEngine 确定性命中 + 人工确认项 + 逐规则状态；任务层复用正式流程 _validate_extracted_text / _validate_page_coverage / _page_quality_manual_issues / summarize_review 得出 validation / 结论 / review_complete（不复制一套会漂移的判断）。
调用方负责在导入 app 前完成环境隔离（RULES_DIR / UPLOAD_DIR / OUTPUT_DIR / LLM_API_KEY / OCR_ENABLED）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

DEFAULT_RULESET = "campus"


def load_ruleset_text(project_root: Path, ruleset: str) -> str:
    if ruleset.endswith((".yaml", ".yml")):
        path = Path(ruleset)
    else:
        path = project_root / "rules" / f"{ruleset}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"规则集不存在：{path}")
    return path.read_text(encoding="utf-8")


def evaluate_rules(ir, ruleset_text: str) -> dict[str, Any]:
    """规则层 + 任务层结算（与正式流程共用 summarize_review）。
    返回 {"issues", "statuses", "enabled", "summary"}；issues 已包含页质/降级产生的 manual_required 项。
    """
    from app.api import routes  # noqa: E402 —— 延迟导入，保持本模块可独立测试
    from app.services.rules.catalog import rule_requires_ai  # noqa: E402
    from app.services.rules.engine import RuleEngine  # noqa: E402

    engine = RuleEngine("snapshot.yaml", ruleset_text=ruleset_text)
    enabled = [rule for rule in engine.ruleset.rules if rule.enabled]
    statuses: dict[str, dict] = {
        rule.id: {
            "type": rule.type,
            "requires_ai": rule_requires_ai(rule),
            "description": rule.description,
            "status": "pending",
            "error": "",
        }
        for rule in enabled
    }

    def on_rule_result(index, total, rule, status, error) -> None:
        statuses[rule.id].update({"status": status, "error": error[:200]})

    issues = engine.run(
        ir,
        on_rule_result=on_rule_result,
        should_skip=lambda rule: rule_requires_ai(rule),
    )
    issues.extend(routes._page_quality_manual_issues(ir))
    summary = routes.summarize_review(issues, statuses, enabled)
    return {
        "issues": issues,
        "statuses": statuses,
        "enabled": enabled,
        "summary": summary,
    }


def _prepare_pdf(path: Path) -> tuple[Path, Path | None]:
    """返回 (可解析的 pdf 路径, 需要清理的临时目录)；DOCX 先复制到独立临时目录再转换，
    绝不在样本目录里留下 .pdf 副产物（否则会被输出目录安全管理当作“无关文件”长期保留）。
    """
    if path.suffix.lower() != ".docx":
        return path, None
    import shutil
    import tempfile

    from app.services.parser.converter import convert_docx_to_pdf

    tmp_dir = Path(tempfile.mkdtemp(prefix="corpus-layers-"))
    copy = tmp_dir / path.name
    shutil.copy2(path, copy)
    return convert_docx_to_pdf(copy), tmp_dir


def run_layers(path: Path | str, ruleset: str = DEFAULT_RULESET) -> dict[str, Any]:
    """对单个样本执行分层自检，返回结构化结果（不抛异常，解析失败也返回结果）。"""
    from app.schemas.ir import IR_SCHEMA_VERSION  # noqa: E402
    from app.services.parser import get_parser  # noqa: E402

    path = Path(path)
    project_root = Path(__file__).resolve().parents[2]
    payload: dict[str, Any] = {
        "file": path.name,
        "path": str(path),
        "ruleset": ruleset,
        "parse": {},
        "rules": {},
        "task": {},
    }

    tmp_dir: Path | None = None
    try:
        try:
            pdf, tmp_dir = _prepare_pdf(path)
        except Exception as exc:  # noqa: BLE001 —— 转换失败属解析层失败
            payload["parse"] = {"ok": False, "error": f"convert: {type(exc).__name__}: {exc}"}
            payload["task"] = {"validation": "parse_error", "validation_error": str(exc)}
            return payload

        try:
            ir = get_parser(str(pdf)).parse(str(pdf))
        except Exception as exc:  # noqa: BLE001 —— 损坏文档必须被正确拒绝
            payload["parse"] = {"ok": False, "error": f"parse: {type(exc).__name__}: {exc}"}
            payload["task"] = {"validation": "parse_error", "validation_error": str(exc)}
            return payload

        payload["parse"] = {
            "ok": True,
            "error": None,
            "pages": ir.meta.pages,
            "word_count": ir.meta.word_count,
            "schema_version": ir.meta.schema_version or IR_SCHEMA_VERSION,
            "ocr_pages": list(ir.meta.ocr_pages),
            "parse_warnings": list(getattr(ir.meta, "parse_warnings", []) or []),
            "page_status": [
                {
                    "page": meta.page_number,
                    "status": meta.status,
                    "reason": meta.reason,
                    "text_chars": meta.text_chars,
                    "image_area_ratio": meta.image_area_ratio,
                }
                for meta in ir.meta.page_meta
            ],
        }

        # 任务层：正式流程的完整性校验（未通过时正式任务会失败，不再进入规则层结算）
        from app.api import routes  # noqa: E402

        validation_error = None
        try:
            routes._validate_extracted_text(ir)
            routes._validate_page_coverage(ir)
        except RuntimeError as exc:
            validation_error = str(exc)
        except Exception as exc:  # noqa: BLE001
            validation_error = f"{type(exc).__name__}: {exc}"

        evaluation = evaluate_rules(ir, load_ruleset_text(project_root, ruleset))
        issues = evaluation["issues"]
        summary = evaluation["summary"]

        hits = [
            {
                "checker": issue.checker,
                "severity": issue.severity,
                "page": issue.page,
                "message": issue.message,
            }
            for issue in issues
            if issue.severity in {"error", "warning"}
            and issue.confidence != "manual_required"
        ]
        manual = [
            {
                "checker": issue.checker,
                "severity": issue.severity,
                "page": issue.page,
                "message": issue.message,
            }
            for issue in issues
            if issue.confidence == "manual_required"
        ]
        payload["rules"] = {
            "hits": hits,
            "manual": manual,
            "statuses": {
                key: value["status"] for key, value in evaluation["statuses"].items()
            },
            "enabled_rules": len(evaluation["enabled"]),
        }
        payload["task"] = {
            "validation": "failed" if validation_error else "ok",
            "validation_error": validation_error,
            **summary,
        }
        # 供进阶检查（如场景规则集）复用同一份 IR；不写入 manifest、不参与 JSON 序列化
        payload["_ir"] = ir
        return payload
    finally:
        if tmp_dir is not None:
            import shutil

            shutil.rmtree(tmp_dir, ignore_errors=True)


def hit_signature(payload: dict[str, Any]) -> dict[tuple[str, str], int]:
    """error/warning 确定性命中按 (checker, severity) 计数（与 manifest 口径一致）。"""
    counts: dict[tuple[str, str], int] = {}
    for hit in payload.get("rules", {}).get("hits", []):
        key = (hit["checker"], hit["severity"])
        counts[key] = counts.get(key, 0) + 1
    return counts


def page_status_map(payload: dict[str, Any]) -> dict[int, str]:
    return {
        item["page"]: item["status"]
        for item in payload.get("parse", {}).get("page_status", [])
    }
