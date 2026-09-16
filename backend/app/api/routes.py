"""HTTP API for durable document review tasks."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import yaml
from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import FileResponse
from openpyxl import Workbook
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.api.auth_routes import (
    current_user,
    local_or_current_user,
    require_review_capability,
)
from app.services import auth_service, review_store
from app.services import rule_admin_store
from app.services.bounded_executor import BoundedTaskExecutor, TaskQueueFull
from app.services.llm.client import (
    PROMPT_VERSION,
    LLMFormatError,
    LLMUnavailableError,
    call_json,
    probe_connectivity,
)
from app.services.llm.guideline import extract_draft_from_pdf
from app.services.parser import cached_ir_is_current, get_parser, parser_fingerprint
from app.services.parser.converter import convert_docx_to_pdf
from app.services.report.annotator import generate_annotated_pdf
from app.services.report.reporter import generate_review_report
from app.services.rules.base_checker import list_registered_checker_types
from app.services.rules.catalog import rule_requires_ai, validate_ruleset
from app.services.rules.engine import RuleEngine
from app.services.rules.rule_schema import RuleDef, RuleIssue, RuleSet
from app.schemas.ir import (
    UNCERTAIN_PAGE_STATUSES,
    UNREAD_PAGE_STATUSES,
    DocumentIR,
)
from app.services.task_runtime import (
    TaskCancelled,
    TaskDeadlineExceeded,
    authorize_egress,
    checkpoint,
    task_scope,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1")
settings = get_settings()
_configured_rules_dir = Path(settings.RULES_DIR).resolve()

# Compatibility caches are intentionally not the source of truth.  They keep
# the original test/plugin surface working while production reads are durable.
_documents: Dict[str, dict] = {}
_reviews: Dict[str, dict] = {}
_batches: Dict[str, dict] = {}

_task_executor: BoundedTaskExecutor | None = None
_retired_executor: BoundedTaskExecutor | None = None
_review_futures: Dict[str, object] = {}
_batch_futures: Dict[str, object] = {}
_services_started = False
_watchdog_thread: threading.Thread | None = None
_shutdown_event = threading.Event()
_review_creation_lock = threading.Lock()

AI_RULE_TYPES = {
    "anonymity_check",
    "title_content_match",
    "cross_consistency_semantic",
}
TERMINAL_STATUSES = review_store.TERMINAL_STATUSES


def _require_executor() -> BoundedTaskExecutor:
    """返回当前执行器；未启动（或已关闭）时明确报错，绝不复用已关闭实例。"""
    executor = _task_executor
    if executor is None:
        raise RuntimeError("审查执行器未运行：请先启动服务（startup_services）")
    return executor


def active_task_count() -> int:
    """健康检查用：未启动/已关闭时返回 0，不抛异常。"""
    executor = _task_executor
    return executor.active_count() if executor is not None else 0


def _cancel_in_executor(review_id: str) -> bool:
    """尽力通知执行器取消；执行器不存在时返回 False（调用方据此收敛终态）。"""
    executor = _task_executor
    if executor is None:
        return False
    return executor.cancel(review_id)


def _can_view_all(user: dict) -> bool:
    return "review.view_all" in set(user.get("capabilities", []))


def _require_owned(resource: dict | None, user: dict, kind: str) -> dict:
    if resource is None:
        raise HTTPException(404, f"{kind} not found")
    if not _can_view_all(user) and resource.get("owner_id") != user.get("id"):
        # Return 404 instead of leaking the existence of another user's data.
        raise HTTPException(404, f"{kind} not found")
    return resource


def _rules_dir() -> Path:
    return Path(settings.RULES_DIR).resolve()


def _load_ruleset_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _find_ruleset_path(ruleset_id: str) -> Path | None:
    rules_dir = _rules_dir()
    for path in sorted(rules_dir.glob("*.yaml")):
        try:
            data = _load_ruleset_yaml(path)
        except Exception as exc:
            logger.warning("Failed to load %s: %s", path, exc)
            continue
        if data.get("ruleset") == ruleset_id or path.stem == ruleset_id:
            return path
    return None


def _resolve_ruleset_path(ruleset_id: str) -> Path:
    path = _find_ruleset_path(ruleset_id)
    if path is None:
        raise FileNotFoundError(f"Ruleset not found: {ruleset_id}")
    return path


def _resolve_ruleset_snapshot(ruleset_id: str) -> str:
    """Return only the current published snapshot for managed rulesets."""
    managed = rule_admin_store.get_ruleset(ruleset_id)
    if managed is not None:
        if managed.get("status") == "disabled":
            raise PermissionError(f"Ruleset disabled: {ruleset_id}")
        snapshot = rule_admin_store.get_published_yaml(ruleset_id)
        if snapshot:
            return snapshot
        raise FileNotFoundError(f"Ruleset has no published version: {ruleset_id}")
    return _resolve_ruleset_path(ruleset_id).read_text(encoding="utf-8")


def _ensure_ruleset_reviewable(ruleset_id: str) -> str:
    managed = rule_admin_store.get_ruleset(ruleset_id)
    try:
        return _resolve_ruleset_snapshot(ruleset_id)
    except PermissionError as exc:
        raise HTTPException(409, str(exc)) from exc
    except FileNotFoundError as exc:
        status = 409 if managed is not None else 404
        raise HTTPException(status, str(exc)) from exc


def _path_for_ruleset_save(ruleset_id: str) -> Path:
    existing = _find_ruleset_path(ruleset_id)
    if existing is not None:
        return existing
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", ruleset_id):
        raise HTTPException(400, "规则集 ID 只能包含字母、数字、下划线和连字符")
    target = (_rules_dir() / f"{ruleset_id}.yaml").resolve()
    if target.parent != _rules_dir():
        raise HTTPException(400, "Invalid ruleset id")
    return target


def _validate_ruleset_yaml(yaml_text: str) -> RuleSet:
    try:
        data = yaml.safe_load(yaml_text) or {}
        ruleset, errors, _ = validate_ruleset(data)
    except Exception as exc:
        raise HTTPException(422, f"Invalid ruleset YAML: {exc}") from exc
    if ruleset is None or errors:
        raise HTTPException(422, f"Invalid ruleset YAML: {errors}")
    registered = set(list_registered_checker_types())
    unknown = sorted({rule.type for rule in ruleset.rules if rule.type not in registered})
    if unknown:
        raise HTTPException(422, f"Unknown rule type(s): {', '.join(unknown)}")
    return ruleset


def _require_admin(user: dict = Depends(current_user)) -> None:
    """管理接口守卫（C3/R06）：超级管理员**且**具备 system.manage **且**不处于强制改密。
    强制改密会话只保留 account.change_password 能力（RESTRICTED_CAPABILITIES），不因 role 放行；
    ``X-Admin-Token`` 由 current_user 解析并标注 auth_method="admin_token"（部署凭据）。
    """
    if user.get("must_change_password"):
        raise HTTPException(403, "首次登录必须修改密码后才能使用管理功能")
    if user.get("role") != "super_admin":
        raise HTTPException(403, "需要超级管理员权限")
    if "system.manage" not in set(user.get("capabilities", [])):
        raise HTTPException(403, "当前会话不具备系统管理能力")


@router.get("/rulesets")
def list_rulesets():
    if _rules_dir() == _configured_rules_dir:
        rule_admin_store.ensure_seeded(settings.RULES_DIR)
        managed = [
            item for item in rule_admin_store.list_rulesets()
            if item.get("status") == "active" and item.get("active_version")
        ]
        return {"rulesets": [{
            "id": item["id"],
            "name": item["name"],
            "description": item["description"],
            "rule_count": item["rule_count"],
        } for item in managed]}
    results = []
    if _rules_dir().exists():
        for path in sorted(_rules_dir().glob("*.yaml")):
            try:
                data = _load_ruleset_yaml(path)
                results.append({
                    "id": data.get("ruleset", path.stem),
                    "name": data.get("name", path.stem),
                    "description": data.get("description", ""),
                    "rule_count": len(data.get("rules", [])),
                })
            except Exception as exc:
                logger.warning("Failed to load %s: %s", path, exc)
    return {"rulesets": results}


@router.get("/rulesets/{ruleset_id}")
def get_ruleset(ruleset_id: str):
    if _rules_dir() == _configured_rules_dir:
        rule_admin_store.ensure_seeded(settings.RULES_DIR)
        managed = rule_admin_store.get_ruleset(ruleset_id)
        if managed is not None:
            active = managed.get("active_version")
            if managed.get("status") != "active" or not active:
                raise HTTPException(404, "Ruleset not found")
            return {
                "id": managed["id"],
                "name": managed["name"],
                "description": managed["description"],
                "rule_count": len(active["data"].get("rules", [])),
            }
    path = _find_ruleset_path(ruleset_id)
    if path is None:
        raise HTTPException(404, "Ruleset not found")
    yaml_text = path.read_text(encoding="utf-8")
    data = _load_ruleset_yaml(path)
    return {
        "id": data.get("ruleset", path.stem),
        "name": data.get("name", path.stem),
        "description": data.get("description", ""),
        "rule_count": len(data.get("rules", [])),
        "yaml": yaml_text,
    }


@router.put("/rulesets/{ruleset_id}", dependencies=[Depends(_require_admin)])
def save_ruleset(
    ruleset_id: str,
    yaml_text: str = Body(..., media_type="text/plain"),
):
    ruleset = _validate_ruleset_yaml(yaml_text)
    if ruleset.ruleset != ruleset_id:
        raise HTTPException(
            422,
            "URL 中的规则集 ID 必须与 YAML 的 ruleset 字段一致",
        )
    content_hash = hashlib.sha256(yaml_text.encode("utf-8")).hexdigest()
    if _rules_dir() == _configured_rules_dir:
        try:
            version = rule_admin_store.save_legacy_draft(
                ruleset.model_dump(), yaml_text
            )
        except rule_admin_store.RuleAdminError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {
            "id": ruleset.ruleset,
            "name": ruleset.name,
            "description": ruleset.description,
            "rule_count": len(ruleset.rules),
            "sha256": content_hash,
            "state": version["state"],
            "version_id": version["id"],
            "published": False,
        }
    path = _path_for_ruleset_save(ruleset_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        history_dir = _rules_dir() / ".history" / ruleset_id
        history_dir.mkdir(parents=True, exist_ok=True)
        old_text = path.read_text(encoding="utf-8")
        old_hash = hashlib.sha256(old_text.encode("utf-8")).hexdigest()
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        (history_dir / f"{timestamp}-{old_hash[:12]}.yaml").write_text(
            old_text,
            encoding="utf-8",
        )
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(yaml_text, encoding="utf-8")
    os.replace(temp, path)
    return {
        "id": ruleset.ruleset,
        "name": ruleset.name,
        "description": ruleset.description,
        "rule_count": len(ruleset.rules),
        "sha256": content_hash,
    }


@router.post("/rulesets/from-guideline", dependencies=[Depends(_require_admin)])
async def create_ruleset_from_guideline(
    file: UploadFile = File(...),
    privacy_consent: bool = Form(False),
):
    if not privacy_consent:
        raise HTTPException(422, "使用外部 AI 前必须确认指南材料发送授权")
    if not _egress_allowed():
        raise HTTPException(
            422,
            "外部 AI 外发已被部署级策略禁用；指南生成需要外部 AI，请联系管理员",
        )
    # B5b：指南生成入口的授权审计
    review_store.record_egress_consent(
        purpose="guideline",
        consent_granted=True,
        ai_enabled=True,
        policy_version=_ai_policy_version(),
    )
    uploaded = await _save_upload(file, allowed_extensions={"pdf"})

    def execute() -> tuple[str, list[dict]]:
        with task_scope(
            f"guideline-{uuid.uuid4().hex[:12]}",
            threading.Event(),
            settings.TASK_TIMEOUT_SECONDS,
            # R08：指南生成走的是"上传的指南材料"，其授权已在入口处校验并落库
            # （privacy_consent + 部署外发开关），因此这里显式打开材料外发授权。
            egress_authorized=True,
        ):
            return extract_draft_from_pdf(uploaded["file_path"])

    try:
        draft_yaml, dropped = await run_in_threadpool(execute)
    except TaskDeadlineExceeded as exc:
        raise HTTPException(504, str(exc)) from exc
    except (LLMUnavailableError, LLMFormatError) as exc:
        raise HTTPException(503, f"LLM service unavailable: {exc}") from exc
    return {"draft_yaml": draft_yaml, "dropped": dropped}


def _safe_filename(filename: str) -> str:
    normalized = filename.replace("\\", "/")
    name = Path(normalized).name.strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name[:180] or "unnamed"


async def _save_upload(
    file: UploadFile,
    *,
    allowed_extensions: set[str] | None = None,
    owner_id: str = "",
) -> dict:
    if not file.filename:
        raise HTTPException(400, "No filename")
    filename = _safe_filename(file.filename)
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    allowed = allowed_extensions or {"pdf", "docx"}
    if ext not in allowed:
        raise HTTPException(400, f"Unsupported: .{ext}")

    upload_dir = Path(settings.UPLOAD_DIR).resolve()
    upload_dir.mkdir(parents=True, exist_ok=True)
    document_id = uuid.uuid4().hex[:12]
    part_path = upload_dir / f".{document_id}.part"
    final_path = upload_dir / f"{document_id}_{filename}"
    max_bytes = max(1, settings.MAX_UPLOAD_SIZE_MB) * 1024 * 1024
    digest = hashlib.sha256()
    size = 0
    header = b""
    try:
        with open(part_path, "wb") as handle:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                if not header:
                    header = chunk[:16]
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(
                        413,
                        f"文件超过 {settings.MAX_UPLOAD_SIZE_MB}MB 限制",
                    )
                digest.update(chunk)
                handle.write(chunk)
        if size == 0:
            raise HTTPException(400, "上传文件为空")
        _validate_file_signature(part_path, ext, header)
        existing = review_store.find_document_by_sha256(
            digest.hexdigest(), owner_id=owner_id or None
        )
        if existing and Path(existing["file_path"]).exists():
            part_path.unlink(missing_ok=True)
            _documents[existing["id"]] = existing
            return existing
        os.replace(part_path, final_path)
    except Exception:
        part_path.unlink(missing_ok=True)
        raise

    data = {
        "id": document_id,
        "filename": filename,
        "file_path": str(final_path),
        "file_type": ext,
        "size": size,
        "mime_type": "application/pdf" if ext == "pdf" else (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        "sha256": digest.hexdigest(),
        "owner_id": owner_id,
    }
    stored = review_store.create_document(data)
    _documents[document_id] = stored
    return stored


def _validate_file_signature(path: Path, ext: str, header: bytes) -> None:
    if ext == "pdf":
        if not header.startswith(b"%PDF-"):
            raise HTTPException(400, "文件扩展名为 PDF，但内容不是有效 PDF")
        return
    if not header.startswith(b"PK"):
        raise HTTPException(400, "文件扩展名为 DOCX，但内容不是有效 DOCX")
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > 20000:
                raise HTTPException(400, "DOCX 压缩包文件条目数量异常")
            names = {info.filename for info in members}
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise HTTPException(400, "DOCX 缺少必要的文档结构")
            expanded = sum(info.file_size for info in members)
            if expanded > settings.MAX_UPLOAD_SIZE_MB * 4 * 1024 * 1024:
                raise HTTPException(400, "DOCX 解压后体积异常")
            compressed = sum(max(1, info.compress_size) for info in members)
            if expanded > 20 * compressed and expanded > 20 * 1024 * 1024:
                raise HTTPException(400, "DOCX 压缩比异常")
    except zipfile.BadZipFile as exc:
        raise HTTPException(400, "DOCX 压缩包损坏") from exc


@router.post("/documents")
async def upload_document(
    file: UploadFile = File(...),
    user: dict = Depends(require_review_capability("review.create")),
):
    stored = await _save_upload(file, owner_id=user["id"])
    return {
        "document_id": stored["id"],
        "filename": stored["filename"],
        "file_type": stored["file_type"],
        "size": stored["size"],
        "sha256": stored["sha256"],
    }


@router.get("/documents")
def list_documents(
    limit: int = Query(50, ge=1, le=200),
    user: dict = Depends(local_or_current_user),
):
    return {
        "documents": [{
            "document_id": item["id"],
            "filename": item["filename"],
            "file_type": item["file_type"],
            "size": item["size"],
            "mime_type": item.get("mime_type", ""),
            "sha256": item.get("sha256", ""),
            "created_at": item.get("created_at", ""),
        } for item in review_store.list_documents(
            limit, owner_id=None if _can_view_all(user) else user["id"]
        )]
    }


def _get_document(document_id: str) -> dict | None:
    return _documents.get(document_id) or review_store.get_document(document_id)


@router.get("/documents/{document_id}")
def get_document(document_id: str, user: dict = Depends(local_or_current_user)):
    document = _require_owned(_get_document(document_id), user, "Document")
    return {
        "document_id": document["id"],
        "filename": document["filename"],
        "file_type": document["file_type"],
        "size": document["size"],
        "mime_type": document.get("mime_type", ""),
        "sha256": document.get("sha256", ""),
        "created_at": document.get("created_at", ""),
    }


def _document_pdf(document: dict) -> Path:
    source = Path(document["file_path"])
    if document["file_type"] == "pdf":
        return source
    converted = document.get("converted_path")
    if converted and Path(converted).exists():
        return Path(converted)
    pdf_path = convert_docx_to_pdf(source)
    review_store.set_converted_path(document["id"], str(pdf_path))
    document["converted_path"] = str(pdf_path)
    return pdf_path


@router.get("/documents/{document_id}/file")
def download_document_file(document_id: str, user: dict = Depends(local_or_current_user)):
    document = _require_owned(_get_document(document_id), user, "Document")
    try:
        pdf_path = _document_pdf(document)
    except Exception as exc:
        raise HTTPException(500, f"Conversion failed: {exc}") from exc
    return FileResponse(
        str(pdf_path),
        media_type="application/pdf",
        filename=f"{Path(document['filename']).stem}.pdf",
    )


def _public_review(review: dict) -> dict:
    data = dict(review)
    review_id = data.pop("review_id", data.pop("id", ""))
    return {"review_id": review_id, **data}


def _get_review(review_id: str) -> dict | None:
    # Direct cache mutations are retained for compatibility tests only.
    cached = _reviews.get(review_id)
    if cached is not None:
        return {"review_id": review_id, **cached}
    return review_store.get_review(review_id)


def _update_review(review_id: str, **updates) -> dict | None:
    """更新非终态任务字段并同步缓存（R05）。
    store 对已终态行原样返回（不改写），迟到的进度/心跳/规则回调不会污染终态展示。
    """
    stored = review_store.update_review(review_id, **updates)
    if stored:
        _reviews[review_id] = {
            key: value for key, value in stored.items() if key != "review_id"
        }
        return stored
    # 行不存在：不凭请求参数制造缓存状态
    return _get_review(review_id)


def _finalize_review(review_id: str, status: str, **updates) -> bool:
    """终态写入的单一出口（B3）：优先写目标终态，被取消请求拒绝时改判 cancelled。
    store.finalize 规则：行已终态不覆盖（first-writer-wins）；cancel_requested=1 时 done/failed/timed_out
    被拒、仅 cancelled 可落定，返回 True 表示行已终态；它是 Core 批量 UPDATE，写入后须回读同步 _reviews 缓存。
    """
    if review_store.finalize(review_id, status, **updates):
        stored = review_store.get_review(review_id)
        if stored:
            _reviews[review_id] = {
                key: value for key, value in stored.items() if key != "review_id"
            }
        return True
    current = _get_review(review_id) or {}
    if (
        status != "cancelled"
        and current.get("status") not in TERMINAL_STATUSES
        and current.get("cancel_requested")
    ):
        if review_store.finalize(review_id, "cancelled"):
            stored = review_store.get_review(review_id)
            if stored:
                _reviews[review_id] = {
                    key: value for key, value in stored.items() if key != "review_id"
                }
            return True
    return False


def _new_review(
    document_id: str,
    ruleset_id: str,
    *,
    use_ai: bool = True,
    idempotency_key: str | None = None,
    owner_id: str = "",
) -> tuple[str, bool]:
    document = _get_document(document_id)
    if document is None:
        raise FileNotFoundError(f"Document {document_id} not found")
    snapshot = _resolve_ruleset_snapshot(ruleset_id)
    snapshot_data = yaml.safe_load(snapshot) or {}
    canonical_id = snapshot_data.get("ruleset", ruleset_id)
    automatic_key = hashlib.sha256(
        (
            f"{owner_id}|{document.get('sha256') or document_id}|{canonical_id}|"
            f"{hashlib.sha256(snapshot.encode('utf-8')).hexdigest()}|{int(use_ai)}|"
            # R11：解析运行时配置（OCR 开关/语言/DPI/页数预算）变化后不得复用旧任务
            f"{parser_fingerprint()}"
        ).encode("utf-8")
    ).hexdigest()
    key = (
        hashlib.sha256(f"{automatic_key}|{idempotency_key}".encode("utf-8")).hexdigest()
        if idempotency_key else automatic_key
    )
    with _review_creation_lock:
        existing = review_store.find_active_review(key, owner_id=owner_id or None)
        if existing:
            return existing["review_id"], True
        stored = review_store.create_review(
            document_id,
            canonical_id,
            ruleset_snapshot=snapshot,
            idempotency_key=key,
            use_ai=use_ai,
            owner_id=owner_id,
        )
        review_id = stored["review_id"]
        _reviews[review_id] = {
            key: value for key, value in stored.items() if key != "review_id"
        }
    return review_id, False


def _validate_extracted_text(ir) -> None:
    text_chars = sum(len(block.text.strip()) for block in ir.blocks if block.text.strip())
    if text_chars < settings.MIN_EXTRACTED_TEXT_CHARS:
        raise RuntimeError(
            "文档未提取到足够文字，可能是扫描版 PDF；请先进行 OCR 后重新上传"
        )


def _validate_page_coverage(ir) -> None:
    """整篇字数足够不等于每页都被读取（B4/C9/R02）。
    UNREAD_PAGE_STATUSES 中的逐页状态（扫描页无文字或仅有页码、OCR 未读出/失败/超上限）都表示
    该页正文未被读取：缺页会漏掉规则错误，必须阻止审查通过，不能当作正常空白页静默放行。
    """
    unread = [
        meta for meta in ir.meta.page_meta
        if meta.status in UNREAD_PAGE_STATUSES
    ]
    if unread:
        detail = "；".join(
            f"第{meta.page_number}页（{meta.status}：{meta.reason or '未读取'}）"
            for meta in unread
        )
        raise RuntimeError(
            f"以下页面未能提取到正文：{detail}。"
            "请先对文档进行 OCR（或改用可选中文字的版本）后重新上传"
        )


def _page_quality_manual_issues(ir) -> list:
    """读取可疑页与文档级解析降级 → manual_required，使结论为 incomplete。
    R02：大块图像区域内无文字、无法可靠判断的页面不得判为通过；R04：表格等结构解析降级不得伪装成“没有该内容”。
    """
    issues: list[RuleIssue] = []
    for meta in ir.meta.page_meta:
        if meta.status in UNCERTAIN_PAGE_STATUSES:
            issues.append(RuleIssue(
                rule_id="parser",
                severity="info",
                message=(
                    f"第{meta.page_number}页存在大块图像区域且区域内无数字文字，"
                    "无法确认该区域内容是否已读取"
                ),
                suggestion="请人工核对该页图像区域的内容是否已纳入审查",
                confidence="manual_required",
                layer="system",
                checker="parser_coverage",
                page=meta.page_number,
            ))
    for warning in list(getattr(ir.meta, "parse_warnings", []) or []):
        issues.append(RuleIssue(
            rule_id="parser",
            severity="info",
            message=f"文档存在解析降级：{warning}",
            suggestion="请人工核对相关表格或区域的实际内容",
            confidence="manual_required",
            layer="system",
            checker="parser_coverage",
        ))
    return issues


def _rule_weight(rule: RuleDef) -> int:
    return 5 if rule_requires_ai(rule) else 1


def _egress_allowed() -> bool:
    """部署级 AI 外发总开关（B5b）；False 时用户勾选也不能覆盖。"""
    return bool(getattr(settings, "AI_EGRESS_ENABLED", True))


def _ai_policy_version() -> str:
    return f"{settings.APP_VERSION}|{PROMPT_VERSION}"


def _is_degraded_issue(issue) -> bool:
    return (
        issue.layer == "llm"
        and issue.severity == "info"
        and issue.message.startswith("AI 检查未执行")
    )


def conclude_review(
    *,
    no_executable_rules: bool,
    incomplete_rules: int,
    manual_required: int,
    errors: int,
) -> str:
    """业务结论决策表（六）：任务状态与业务结论严格分离。
    优先级：incomplete（无可执行规则 / 检查不完整 / 需人工确认）> needs_revision（确定性 error）> pass；
    任何“未完成有效审查”都不能被判为通过，存在确定性错误时也不把 incomplete 伪装成 needs_revision。
    """
    if no_executable_rules or incomplete_rules or manual_required:
        return "incomplete"
    if errors:
        return "needs_revision"
    return "pass"


def summarize_review(issues: list, statuses: dict, enabled_rules: list) -> dict:
    """正式流程与样本自检共用的结算逻辑（R10：唯一实现，避免两套口径漂移）。
    输入规则问题列表（含页质/降级 manual 项）、逐规则状态与启用规则；输出计数、完整性、
    结论与 review_complete/ai_complete；副作用：无可执行规则时向 issues 追加 system 说明。
    """
    errors = sum(issue.severity == "error" for issue in issues)
    warnings = sum(issue.severity == "warning" for issue in issues)
    infos = sum(issue.severity == "info" for issue in issues)
    degraded = sum(_is_degraded_issue(issue) for issue in issues)
    manual_required = sum(
        issue.confidence == "manual_required" for issue in issues
    )
    skipped_checks = sum(
        item["status"] == "skipped" for item in statuses.values()
    )
    failed_checks = sum(
        item["status"] in {"failed", "timed_out"}
        for item in statuses.values()
    )
    incomplete_rules = sum(
        item["status"] in {"failed", "degraded", "skipped"}
        for item in statuses.values()
    )
    # 没有可执行规则时不能判定通过（防御历史快照/遗留数据绕过发布校验）
    no_executable_rules = not enabled_rules
    if no_executable_rules:
        issues.append(RuleIssue(
            rule_id="system",
            severity="info",
            message="规则集没有启用的规则，无法完成有效的形式审查",
            suggestion="请在规则管理中启用至少一条规则后重新提交",
            confidence="system",
            layer="system",
            checker="system",
        ))
    conclusion = conclude_review(
        no_executable_rules=no_executable_rules,
        incomplete_rules=incomplete_rules,
        manual_required=manual_required,
        errors=errors,
    )
    return {
        "conclusion": conclusion,
        "no_executable_rules": no_executable_rules,
        "review_complete": (
            not no_executable_rules
            and not incomplete_rules
            and not manual_required
        ),
        "ai_complete": not any(
            item.get("requires_ai", item["type"] in AI_RULE_TYPES)
            and item["status"] in {"failed", "degraded", "skipped", "timed_out"}
            for item in statuses.values()
        ),
        "total_issues": len(issues),
        "errors": errors,
        "warnings": warnings,
        "infos": infos,
        "degraded_checks": degraded,
        "manual_required_checks": manual_required,
        "skipped_checks": skipped_checks,
        "failed_checks": failed_checks,
        "incomplete_rules": incomplete_rules,
    }


def _execute_review(review_id: str, document_id: str, ruleset_id: str) -> None:
    document = _get_document(document_id)
    started = time.monotonic()
    try:
        checkpoint()
        if document is None:
            raise RuntimeError(f"Document {document_id} not found")
        # 原子地把 queued→running：若排队期间被取消/请求取消则直接结束，
        # 不再用 running+cancel_requested=False 覆写已取消的行（B3 复活窗口修复）
        running = review_store.mark_running(review_id)
        if running is None:
            return
        _reviews[review_id] = {
            key: value for key, value in running.items() if key != "review_id"
        }
        pdf_path = _document_pdf(document)
        checkpoint()
        # B4/C8：解析结果缓存必须带结构版本与解析器指纹校验；
        # 旧结构/旧解析器的缓存放行重新解析，而不是默认永久复用。
        ir = None
        cached_ir = review_store.get_document_ir(document_id)
        if cached_ir:
            candidate = DocumentIR.model_validate(cached_ir)
            if cached_ir_is_current(candidate):
                ir = candidate
        if ir is None:
            ir = get_parser(str(pdf_path)).parse(str(pdf_path))
            review_store.set_document_ir(document_id, ir.model_dump())
        _validate_extracted_text(ir)
        _validate_page_coverage(ir)
        checkpoint()

        snapshot = review_store.get_ruleset_snapshot(review_id)
        if not snapshot:
            snapshot = _resolve_ruleset_snapshot(ruleset_id)
        engine = RuleEngine("snapshot.yaml", ruleset_text=snapshot)
        use_ai = bool((_get_review(review_id) or {}).get("use_ai", True))
        statuses = {
            rule.id: {
                "type": rule.type,
                "requires_ai": rule_requires_ai(rule),
                "description": rule.description,
                "status": "pending",
                "error": "",
            }
            for rule in engine.ruleset.rules if rule.enabled
        }
        enabled_rules = [rule for rule in engine.ruleset.rules if rule.enabled]
        total_weight = sum(_rule_weight(rule) for rule in enabled_rules) or 1
        completed_weight = 0
        rule_started: dict[str, float] = {}
        _update_review(
            review_id,
            stage="rules",
            progress=30,
            rules_total=len(enabled_rules),
            rules_completed=0,
            rule_status_json=statuses,
        )

        def on_rule_start(index: int, total: int, rule: RuleDef) -> None:
            rule_started[rule.id] = time.monotonic()
            statuses[rule.id]["status"] = "running"
            _update_review(
                review_id,
                stage="ai_check" if rule_requires_ai(rule) else "rule_check",
                current_rule=rule.id,
                rule_status_json={key: dict(value) for key, value in statuses.items()},
            )

        def on_rule_result(
            index: int,
            total: int,
            rule: RuleDef,
            status: str,
            error: str,
        ) -> None:
            nonlocal completed_weight
            completed_weight += _rule_weight(rule)
            statuses[rule.id].update({
                "status": status,
                "error": error[:200],
                "duration_seconds": round(
                    time.monotonic() - rule_started.get(rule.id, time.monotonic()), 2
                ),
            })
            progress = 30 + int(60 * completed_weight / total_weight)
            _update_review(
                review_id,
                progress=min(progress, 90),
                rules_completed=index,
                rule_status_json={key: dict(value) for key, value in statuses.items()},
            )

        # R08：执行期复核「部署策略 + 该任务持久化的授权记录」，任一不满足即跳过 AI 规则
        # （结论 incomplete）且不打开任务级外发授权；历史任务无授权记录按未授权保守处理。
        consent_ok = review_store.egress_consent_granted(review_id, "single_review")
        ai_active = bool(use_ai) and _egress_allowed() and consent_ok
        if use_ai and not consent_ok:
            logger.warning(
                "Review %s: AI 规则跳过——缺少该任务的材料外发授权记录", review_id
            )
        authorize_egress(ai_active)
        issues = engine.run(
            ir,
            on_rule_start=on_rule_start,
            on_rule_result=on_rule_result,
            should_skip=lambda rule: (
                rule_requires_ai(rule) and not ai_active
            ),
        )
        checkpoint()
        _update_review(review_id, stage="finalizing", current_rule=None, progress=92)

        # R02/R04：读取可疑页与解析降级转为 manual_required，保证结论 incomplete
        issues.extend(_page_quality_manual_issues(ir))

        block_map = {block.id: block for block in ir.blocks}
        for issue in issues:
            if issue.block_id and issue.block_id in block_map:
                block = block_map[issue.block_id]
                if issue.page is None:
                    issue.page = block.page
                if issue.bbox is None:
                    issue.bbox = [
                        block.bbox.x0,
                        block.bbox.y0,
                        block.bbox.x1,
                        block.bbox.y1,
                    ]

        summary = summarize_review(issues, statuses, enabled_rules)
        elapsed = time.monotonic() - started
        result = {
            **summary,
            "duration_seconds": round(elapsed, 1),
            "issues": [issue.model_dump() for issue in issues],
            # R02：逐页状态与原因随结果持久化，前端/报告可据此解释“为何不通过”
            "page_quality": [
                {
                    "page": meta.page_number,
                    "status": meta.status,
                    "reason": meta.reason,
                    "text_chars": meta.text_chars,
                    "image_area_ratio": meta.image_area_ratio,
                    "ocr_attempted": meta.ocr_attempted,
                }
                for meta in ir.meta.page_meta
            ],
            "execution": {
                "app_version": settings.APP_VERSION,
                "build_id": settings.APP_BUILD_ID,
                "ruleset_sha256": hashlib.sha256(snapshot.encode("utf-8")).hexdigest(),
                "use_ai": use_ai,
                "llm_model": settings.LLM_MODEL if use_ai else "",
                "llm_base_url": settings.LLM_BASE_URL if use_ai else "",
                "prompt_version": PROMPT_VERSION if use_ai else "",
                "max_llm_calls": settings.LLM_MAX_CALLS_PER_TASK if use_ai else 0,
            },
        }
        # 终态写入走原子出口：若用户在此窗口内取消，done 会被拒并改判 cancelled
        _finalize_review(
            review_id,
            "done",
            stage="completed",
            progress=100,
            current_rule=None,
            result_json=result,
            error_message=None,
            total_issues=summary["total_issues"],
            errors=summary["errors"],
            warnings=summary["warnings"],
            infos=summary["infos"],
            duration_seconds=round(elapsed, 1),
            degraded_checks=summary["degraded_checks"],
            completed_at=review_store.now(),
            rule_status_json=statuses,
        )
    except TaskCancelled as exc:
        _finalize_review(
            review_id,
            "cancelled",
            stage="cancelled",
            progress=100,
            current_rule=None,
            error_message=str(exc),
            completed_at=review_store.now(),
        )
    except TaskDeadlineExceeded as exc:
        # 若同一时刻用户已请求取消，finalize 会拒绝 timed_out 并改判 cancelled
        _finalize_review(
            review_id,
            "timed_out",
            stage="timed_out",
            progress=100,
            current_rule=None,
            error_message=str(exc),
            completed_at=review_store.now(),
        )
    except Exception as exc:
        logger.exception("Review failed: %s", review_id)
        _finalize_review(
            review_id,
            "failed",
            stage="failed",
            progress=100,
            current_rule=None,
            error_message=str(exc),
            completed_at=review_store.now(),
        )


def _heartbeat(review_id: str, detail: str) -> None:
    state = _get_review(review_id) or {}
    if state.get("status") not in TERMINAL_STATUSES:
        _update_review(review_id, stage=detail)


def _submit_existing(review_id: str, document_id: str, ruleset_id: str) -> None:
    try:
        future = _require_executor().submit(
            review_id,
            _execute_review,
            review_id,
            document_id,
            ruleset_id,
            heartbeat=lambda detail: _heartbeat(review_id, detail),
        )
    except TaskQueueFull as exc:
        _finalize_review(
            review_id,
            "failed",
            stage="queue_full",
            progress=100,
            error_message=str(exc),
            completed_at=review_store.now(),
        )
        raise
    _review_futures[review_id] = future
    future.add_done_callback(lambda _: _review_futures.pop(review_id, None))


def _start_review(
    document_id: str,
    ruleset_id: str,
    *,
    use_ai: bool = True,
    idempotency_key: str | None = None,
    owner_id: str = "",
    privacy_consent: bool = False,
    actor_id: str = "",
    actor_name: str = "",
) -> tuple[str, bool]:
    """创建（或幂等复用）审查任务，并在**调度之前**落库授权审计（R08）：授权记录与任务绑定、先于执行
    写入，写入失败则任务不会被调度（直接判失败，不产生外发），消除“已执行但审计写入失败”的窗口。
    """
    review_id, reused = _new_review(
        document_id,
        ruleset_id,
        use_ai=use_ai,
        idempotency_key=idempotency_key,
        owner_id=owner_id,
    )
    if reused:
        return review_id, reused
    try:
        # B5b/R08：授权审计先落库（主体、用途、授权、策略版本、时间；不含材料与密钥）
        review_store.record_egress_consent(
            purpose="single_review",
            subject_id=review_id,
            actor_id=actor_id or owner_id,
            actor_name=actor_name,
            consent_granted=bool(privacy_consent),
            ai_enabled=bool(use_ai),
            policy_version=_ai_policy_version(),
        )
    except Exception as exc:  # noqa: BLE001 —— 审计失败绝不静默继续
        logger.exception("Egress consent audit failed for %s", review_id)
        _finalize_review(
            review_id,
            "failed",
            stage="consent_audit_failed",
            progress=100,
            error_message="授权记录写入失败，任务未执行",
            completed_at=review_store.now(),
        )
        raise HTTPException(500, "授权记录写入失败，任务未执行") from exc
    _submit_existing(review_id, document_id, ruleset_id)
    return review_id, reused


@router.post("/reviews")
async def create_review(
    document_id: str = Form(...),
    ruleset_id: str = Form("campus_general_v1"),
    use_ai: bool = Form(True),
    privacy_consent: bool = Form(False),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: dict = Depends(require_review_capability("review.create")),
):
    _require_owned(_get_document(document_id), user, "Document")
    _ensure_ruleset_reviewable(ruleset_id)
    # R08：只要请求使用 AI，就必须有用户授权——与"当前是否配置了密钥"无关，
    # 避免"先无密钥排队、之后补密钥再外发"的绕过路径。
    if use_ai and not privacy_consent:
        raise HTTPException(422, "使用外部 AI 前必须确认材料发送授权")
    if use_ai and not _egress_allowed():
        raise HTTPException(
            422,
            "外部 AI 外发已被部署级策略禁用；请关闭 AI 选项（结论将为 incomplete）或联系管理员",
        )
    try:
        review_id, reused = _start_review(
            document_id,
            ruleset_id,
            use_ai=use_ai,
            idempotency_key=idempotency_key,
            owner_id=user["id"],
            privacy_consent=privacy_consent,
            actor_id=user.get("id", ""),
            actor_name=user.get("username", ""),
        )
    except PermissionError as exc:
        raise HTTPException(409, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(409, str(exc)) from exc
    except TaskQueueFull as exc:
        raise HTTPException(429, str(exc)) from exc
    review = _get_review(review_id) or {}
    return {
        "review_id": review_id,
        "status": review.get("status", "queued"),
        "reused": reused,
    }


@router.get("/reviews")
def list_review_tasks(
    limit: int = Query(50, ge=1, le=200),
    user: dict = Depends(local_or_current_user),
):
    return {"reviews": review_store.list_reviews(
        limit, owner_id=None if _can_view_all(user) else user["id"]
    )}


@router.get("/reviews/{review_id}")
def get_review(review_id: str, user: dict = Depends(local_or_current_user)):
    review = _require_owned(_get_review(review_id), user, "Review")
    return _public_review(review)


@router.post("/reviews/{review_id}/cancel")
def cancel_review(review_id: str, user: dict = Depends(require_review_capability("review.create"))):
    """请求取消（R05）：以数据库原子条件更新为准，取消是合作式的。
    胜出规则同 store.finalize：终态先提交者胜出，cancel_requested 先提交时只允许落 cancelled；
    执行器已不再持有该任务时直接收敛为终态 cancelled，避免行永久停留在 cancelling。
    """
    _require_owned(_get_review(review_id), user, "Review")
    # 前置状态以数据库事实为准（进程内缓存可能陈旧）
    pre = review_store.get_review(review_id)
    if pre is None:
        raise HTTPException(404, "Review not found")
    _reviews[review_id] = {
        key: value for key, value in pre.items() if key != "review_id"
    }
    if pre.get("status") in TERMINAL_STATUSES:
        return _public_review(pre)
    was_queued = pre.get("status") == "queued"

    requested = review_store.request_cancel(review_id)
    if requested is None:
        raise HTTPException(404, "Review not found")
    _reviews[review_id] = {
        key: value for key, value in requested.items() if key != "review_id"
    }
    if requested.get("status") in TERMINAL_STATUSES:
        # 读取与请求之间已被“终态先提交者”落定：取消无效，返回真实终态
        return _public_review(requested)

    handled = _cancel_in_executor(review_id)
    if was_queued or not handled:
        # 排队中取消，或执行器已无该任务（线程已结束/尚未登记）→ 收敛为 cancelled
        _finalize_review(
            review_id,
            "cancelled",
            stage="cancelled",
            progress=100,
            error_message="审查任务已取消",
            completed_at=review_store.now(),
        )
    return _public_review(_get_review(review_id) or requested)


@router.get("/reviews/{review_id}/issues")
def get_review_issues(review_id: str, user: dict = Depends(local_or_current_user)):
    review = _require_owned(_get_review(review_id), user, "Review")
    if review.get("status") != "done":
        raise HTTPException(409, f"Review is {review.get('status', 'not complete')}")
    return {
        "review_id": review_id,
        "status": review["status"],
        "issues": review.get("issues", []),
        "conclusion": review.get("conclusion", "incomplete"),
        "ai_complete": review.get("ai_complete", False),
    }


def _review_source_pdf(review: dict) -> Path:
    document = _get_document(review.get("document_id", ""))
    if not document:
        raise HTTPException(404, "Review document not found")
    return _document_pdf(document)


@router.get("/reviews/{review_id}/annotated.pdf")
def download_annotated_pdf(review_id: str, user: dict = Depends(local_or_current_user)):
    review = _require_owned(_get_review(review_id), user, "Review")
    if review.get("status") != "done":
        raise HTTPException(409, "Review is not complete")
    output = Path(settings.OUTPUT_DIR).resolve() / f"{review_id}_annotated.pdf"
    generate_annotated_pdf(_review_source_pdf(review), review.get("issues", []), output)
    return FileResponse(str(output), media_type="application/pdf", filename=output.name)


@router.get("/reviews/{review_id}/report.pdf")
def download_review_report(review_id: str, user: dict = Depends(local_or_current_user)):
    review = _require_owned(_get_review(review_id), user, "Review")
    if review.get("status") != "done":
        raise HTTPException(409, "Review is not complete")
    document = _get_document(review.get("document_id", ""))
    if not document:
        raise HTTPException(404, "Review document not found")
    snapshot = review_store.get_ruleset_snapshot(review_id) or ""
    try:
        ruleset_data = yaml.safe_load(snapshot) or {}
    except yaml.YAMLError:
        ruleset_data = {}
    output = Path(settings.OUTPUT_DIR).resolve() / f"{review_id}_report.pdf"
    generate_review_report(
        review,
        document_name=document["filename"],
        ruleset_name=ruleset_data.get("name", review.get("ruleset_id", "")),
        output_path=output,
    )
    return FileResponse(str(output), media_type="application/pdf", filename=output.name)


@router.post("/maintenance/cleanup", dependencies=[Depends(_require_admin)])
def cleanup_retained_data(
    dry_run: bool = Query(True),
    days: int | None = Query(None, ge=1, le=3650),
):
    retention_days = days or settings.DATA_RETENTION_DAYS
    candidates = review_store.retention_candidates(retention_days)
    summary = {
        "dry_run": dry_run,
        "retention_days": retention_days,
        "candidate_documents": len(candidates),
        "candidate_reviews": sum(len(item["review_ids"]) for item in candidates),
    }
    if dry_run:
        return {
            **summary,
            "candidates": [
                {
                    "document_id": item["document_id"],
                    "review_ids": item["review_ids"],
                    "last_used_at": item["last_used_at"],
                }
                for item in candidates
            ],
        }

    upload_root = Path(settings.UPLOAD_DIR).resolve()
    output_root = Path(settings.OUTPUT_DIR).resolve()
    removed_files = 0
    failed_files: list[str] = []
    purgeable_document_ids: list[str] = []
    for item in candidates:
        paths = [Path(value) for value in item["paths"]]
        paths.extend(
            output_root / f"{review_id}{suffix}"
            for review_id in item["review_ids"]
            for suffix in ("_annotated.pdf", "_report.pdf")
        )
        resolved_paths = [path.resolve() for path in paths]
        unsafe_paths = [
            resolved for resolved in resolved_paths
            if not (
                resolved.is_relative_to(upload_root)
                or resolved.is_relative_to(output_root)
            )
        ]
        if unsafe_paths:
            failed_files.extend(path.name for path in unsafe_paths)
            continue
        candidate_failed = False
        for resolved in resolved_paths:
            try:
                if resolved.exists():
                    resolved.unlink()
                    removed_files += 1
            except OSError:
                logger.exception("Failed to remove retained file: %s", resolved)
                failed_files.append(resolved.name)
                candidate_failed = True
        if not candidate_failed:
            purgeable_document_ids.append(item["document_id"])
    deleted = review_store.purge_documents(
        purgeable_document_ids
    )
    for item in candidates:
        if item["document_id"] not in purgeable_document_ids:
            continue
        _documents.pop(item["document_id"], None)
        for review_id in item["review_ids"]:
            _reviews.pop(review_id, None)
    return {
        **summary,
        "removed_files": removed_files,
        "failed_files": failed_files,
        "skipped_documents": len(candidates) - len(purgeable_document_ids),
        **deleted,
    }


@router.post("/system/llm-check", dependencies=[Depends(_require_admin)])
def check_llm_connection():
    if not settings.LLM_API_KEY:
        raise HTTPException(503, "未配置 LLM_API_KEY")
    started = time.monotonic()
    try:
        # R08 要求 7：连通性检查不发送任何材料（提示词在 client 内部硬编码），
        # 因此不需要任务级材料授权，但仍然要求已配置密钥且部署外发开关开启。
        response = probe_connectivity()
    except (LLMUnavailableError, LLMFormatError) as exc:
        raise HTTPException(503, f"模型连通性检查失败：{exc}") from exc
    if response.get("ok") is not True:
        raise HTTPException(503, "模型已响应，但没有遵循 JSON 连通性协议")
    return {
        "ok": True,
        "model": settings.LLM_MODEL,
        "latency_seconds": round(time.monotonic() - started, 2),
    }


@router.post("/batches")
async def create_batch(
    files: List[UploadFile] = File(...),
    ruleset_id: str = Form(...),
    use_ai: bool = Form(True),
    privacy_consent: bool = Form(False),
    user: dict = Depends(require_review_capability("review.batch")),
):
    if not files:
        raise HTTPException(400, "At least one file is required")
    if len(files) > 50:
        raise HTTPException(400, "A batch may contain at most 50 files")
    _ensure_ruleset_reviewable(ruleset_id)
    # R08：批量入口同样与"当前是否配置密钥"无关——用 AI 就必须先授权
    if use_ai and not privacy_consent:
        raise HTTPException(422, "使用外部 AI 前必须确认材料发送授权")
    if use_ai and not _egress_allowed():
        raise HTTPException(
            422,
            "外部 AI 外发已被部署级策略禁用；请关闭 AI 选项（结论将为 incomplete）或联系管理员",
        )

    batch = review_store.create_batch(ruleset_id, len(files), owner_id=user["id"])
    batch_id = batch["batch_id"]
    # B5b：批量入口的授权审计（一次记录整批的授权与策略版本）
    review_store.record_egress_consent(
        purpose="batch_review",
        subject_id=batch_id,
        actor_id=user.get("id", ""),
        actor_name=user.get("username", ""),
        consent_granted=bool(privacy_consent),
        ai_enabled=bool(use_ai),
        policy_version=_ai_policy_version(),
    )
    for upload in files:
        filename = _safe_filename(upload.filename or "unnamed")
        try:
            document = await _save_upload(upload, owner_id=user["id"])
            # R08：批量里的每个任务同样在**调度之前**落库单任务授权记录
            review_id, reused = _start_review(
                document["id"],
                ruleset_id,
                use_ai=use_ai,
                owner_id=user["id"],
                privacy_consent=privacy_consent,
                actor_id=user.get("id", ""),
                actor_name=user.get("username", ""),
            )
            review_store.add_batch_item(
                batch_id,
                filename=filename,
                document_id=document["id"],
                review_id=review_id,
                status="queued",
            )
        except Exception as exc:
            logger.exception("Batch file submission failed: %s", filename)
            review_store.add_batch_item(
                batch_id,
                filename=filename,
                document_id=None,
                review_id=None,
                status="failed",
                error=str(exc),
            )
    return {"batch_id": batch_id}


@router.get("/batches/{batch_id}")
def get_batch(batch_id: str, user: dict = Depends(local_or_current_user)):
    batch = _require_owned(review_store.get_batch(batch_id), user, "Batch")
    return batch


@router.get("/batches/{batch_id}/summary.xlsx")
def download_batch_summary(batch_id: str, user: dict = Depends(local_or_current_user)):
    batch = _require_owned(review_store.get_batch(batch_id), user, "Batch")
    if batch["done_count"] != batch["total"]:
        raise HTTPException(409, "Batch is not complete")
    output = Path(settings.OUTPUT_DIR).resolve() / f"{batch_id}_summary.xlsx"
    output.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "审查汇总"
    sheet.append(["文件名", "结论", "error数", "warning数", "info数", "审查时间"])
    for entry in batch["files"]:
        sheet.append([
            entry["filename"],
            entry["conclusion"],
            entry["errors"],
            entry["warnings"],
            entry["infos"],
            entry["review_time"],
        ])
    sheet.freeze_panes = "A2"
    for column, width in {
        "A": 36, "B": 16, "C": 12, "D": 12, "E": 12, "F": 22
    }.items():
        sheet.column_dimensions[column].width = width
    workbook.save(output)
    workbook.close()
    return FileResponse(
        str(output),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=output.name,
    )


def startup_services() -> None:
    global _services_started, _watchdog_thread
    if _services_started:
        return
    Path(settings.UPLOAD_DIR).resolve().mkdir(parents=True, exist_ok=True)
    Path(settings.OUTPUT_DIR).resolve().mkdir(parents=True, exist_ok=True)
    review_store.init_store()
    auth_service.ensure_default_users()
    rule_admin_store.ensure_seeded(settings.RULES_DIR)
    _shutdown_event.clear()
    _services_started = True
    for review in review_store.incomplete_reviews():
        review_id = review["review_id"]
        if review.get("cancel_requested"):
            _finalize_review(
                review_id,
                "cancelled",
                stage="cancelled",
                progress=100,
                completed_at=review_store.now(),
            )
            continue
        _reviews[review_id] = {
            key: value for key, value in review.items() if key != "review_id"
        }
        _update_review(
            review_id,
            status="queued",
            stage="recovered",
            progress=min(int(review.get("progress", 0)), 90),
        )
        try:
            _submit_existing(
                review_id,
                review["document_id"],
                review["ruleset_id"],
            )
        except TaskQueueFull:
            logger.error("Could not recover review %s: queue full", review_id)
    _watchdog_thread = threading.Thread(
        target=_watchdog_loop,
        name="review-watchdog",
        daemon=True,
    )
    _watchdog_thread.start()


def _reap_stale_reviews(now_value: datetime) -> None:
    """看门狗单轮清理（R05）：
    - running 超过心跳阈值 → timed_out（若已 cancel_requested，finalize 按胜出规则改判 cancelled）；
    - cancelling 超过心跳阈值 → 收敛为 cancelled，避免“取消成功但执行器已不存在”的行永久停留。
    """
    for review in review_store.incomplete_reviews():
        status = review.get("status")
        if status not in {"running", "cancelling"}:
            continue
        value = review.get("last_activity_at")
        if not value:
            continue
        last_activity = datetime.fromisoformat(value)
        if last_activity.tzinfo is None:
            last_activity = last_activity.replace(tzinfo=now_value.tzinfo)
        idle = (now_value - last_activity).total_seconds()
        if idle <= settings.TASK_HEARTBEAT_TIMEOUT_SECONDS:
            continue
        review_id = review["review_id"]
        if status == "cancelling":
            logger.error(
                "Review %s stuck in cancelling for %.1fs; converging to cancelled",
                review_id,
                idle,
            )
            _finalize_review(
                review_id,
                "cancelled",
                stage="cancelled",
                progress=100,
                error_message="取消请求长时间未完成，已收敛为已取消",
                completed_at=review_store.now(),
            )
            _cancel_in_executor(review_id)
            continue
        logger.error(
            "Review %s exceeded heartbeat timeout after %.1fs",
            review_id,
            idle,
        )
        _finalize_review(
            review_id,
            "timed_out",
            stage="timed_out",
            progress=100,
            current_rule=None,
            error_message="任务长时间没有活动，已自动终止",
            completed_at=review_store.now(),
        )
        _cancel_in_executor(review_id)


def _watchdog_loop(shutdown_event: threading.Event | None = None) -> None:
    """看门狗循环（R12：绑定启动时传入的事件，避免重启后误读新事件）。"""
    event = shutdown_event if shutdown_event is not None else _shutdown_event
    while not event.wait(10):
        try:
            _reap_stale_reviews(datetime.now().astimezone())
        except Exception:
            logger.exception("Review watchdog failed")


def startup_services(*, allow_retired_tasks: bool = False) -> None:
    """启动服务（幂等；同一进程内关闭后可再次启动，R12）：每次启动都用**新的** BoundedTaskExecutor
    与 shutdown event，绝不复用已关闭的执行器；旧执行器仍有未结束任务时默认拒绝重叠启动（避免新旧实例同时操作同一批数据）；
    确认安全可传 allow_retired_tasks=True，启动失败则释放已创建的执行器/看门狗后原样抛出，不留半启动状态。
    """
    global _services_started, _watchdog_thread, _task_executor
    global _shutdown_event, _retired_executor
    if _services_started:
        return
    retired = _retired_executor
    if retired is not None and retired.active_count() > 0:
        if not allow_retired_tasks:
            raise RuntimeError(
                "上一个执行器仍有任务在运行，拒绝重叠启动；请等待任务结束，"
                "或确认安全后传 allow_retired_tasks=True"
            )
        logger.warning(
            "startup_services: %s 个旧任务仍在运行（allow_retired_tasks=True）",
            retired.active_count(),
        )
    Path(settings.UPLOAD_DIR).resolve().mkdir(parents=True, exist_ok=True)
    Path(settings.OUTPUT_DIR).resolve().mkdir(parents=True, exist_ok=True)

    executor = BoundedTaskExecutor(
        max_workers=settings.TASK_MAX_WORKERS,
        queue_capacity=settings.TASK_QUEUE_CAPACITY,
        timeout_seconds=settings.TASK_TIMEOUT_SECONDS,
    )
    shutdown_event = threading.Event()
    watchdog: threading.Thread | None = None
    try:
        review_store.init_store()
        auth_service.ensure_default_users()
        rule_admin_store.ensure_seeded(settings.RULES_DIR)
        # 缓存不是事实来源：重启时清掉上一轮的内存态，再由数据库恢复
        _reviews.clear()
        _shutdown_event = shutdown_event
        _task_executor = executor
        for review in review_store.incomplete_reviews():
            review_id = review["review_id"]
            if review.get("cancel_requested"):
                _finalize_review(
                    review_id,
                    "cancelled",
                    stage="cancelled",
                    progress=100,
                    completed_at=review_store.now(),
                )
                continue
            _reviews[review_id] = {
                key: value for key, value in review.items() if key != "review_id"
            }
            _update_review(
                review_id,
                status="queued",
                stage="recovered",
                progress=min(int(review.get("progress", 0)), 90),
            )
            try:
                _submit_existing(
                    review_id,
                    review["document_id"],
                    review["ruleset_id"],
                )
            except TaskQueueFull:
                logger.error("Could not recover review %s: queue full", review_id)
        watchdog = threading.Thread(
            target=_watchdog_loop,
            args=(shutdown_event,),
            name="review-watchdog",
            daemon=True,
        )
        watchdog.start()
        _watchdog_thread = watchdog
        _services_started = True
    except Exception:
        shutdown_event.set()
        executor.shutdown(wait=False)
        if watchdog is not None and watchdog.is_alive():
            watchdog.join(timeout=2)
        _task_executor = None
        _watchdog_thread = None
        _services_started = False
        raise


def shutdown_services() -> None:
    """停止后台线程与执行器（幂等；可在同一进程内再次 startup_services）。
    旧执行器仅保留在 _retired_executor 供重启前检查残留任务，绝不再被复用（新提交只走新实例）。
    """
    global _services_started, _watchdog_thread, _task_executor, _retired_executor
    _shutdown_event.set()
    executor = _task_executor
    _task_executor = None
    if executor is not None:
        executor.shutdown(wait=False)
        _retired_executor = executor
    thread = _watchdog_thread
    _watchdog_thread = None
    if (
        thread is not None
        and thread.is_alive()
        and thread is not threading.current_thread()
    ):
        thread.join(timeout=2)
        if thread.is_alive():
            logger.warning("看门狗线程未在 2s 内退出，可能存在残留任务")
    _services_started = False


# 注意：不在模块导入时启动服务。服务生命周期统一由 main.py 的 lifespan 管理，
# 测试通过 conftest.py 的会话级 fixture 显式启动，避免「导入即恢复真实任务、
# 启动工作线程或修改数据库」的副作用。
