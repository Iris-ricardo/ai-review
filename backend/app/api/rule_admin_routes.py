"""Administrator API for structured, versioned ruleset management."""
from __future__ import annotations

import threading
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.api.auth_routes import require_review_capability
from app.core.config import get_settings
from app.services import auth_service, review_store, rule_admin_store
from app.services.parser import get_parser
from app.services.parser.converter import LibreOfficeNotFound, convert_docx_to_pdf
from app.services.rule_admin_store import RevisionConflict, RuleAdminError
from app.services.rules.catalog import checker_catalog, rule_requires_ai, validate_ruleset
from app.services.rules.engine import RuleEngine
from app.services.task_runtime import TaskDeadlineExceeded, task_scope


router = APIRouter(prefix="/api/v1/rule-admin", tags=["rule-admin"])
settings = get_settings()


class SaveVersionRequest(BaseModel):
    data: dict[str, Any] = Field(default_factory=dict)
    revision: int = Field(default=0, ge=0, strict=True)
    change_note: str = ""


class SaveYamlRequest(BaseModel):
    yaml: str = ""
    revision: int = Field(default=0, ge=0, strict=True)
    change_note: str = "高级 YAML 编辑"


class RuleMutationRequest(BaseModel):
    rule_id: str = Field(min_length=1)
    revision: int = Field(default=0, ge=0, strict=True)
    change_note: str = ""
    enabled: bool = Field(default=True, strict=True)
    new_rule_id: str | None = None
    target_index: int | None = Field(default=None, ge=0, strict=True)


class TestVersionRequest(BaseModel):
    document_id: str = Field(min_length=1)
    use_ai: bool = Field(default=False, strict=True)
    privacy_consent: bool = Field(default=False, strict=True)
    rule_ids: list[str] = Field(default_factory=list)


def require_rule_capability(capability: str):
    capability_dependency = require_review_capability(capability)

    async def dependency(
        user: dict[str, Any] = Depends(capability_dependency),
    ) -> AsyncGenerator[dict[str, Any], None]:
        actor = str(user.get("username") or user.get("id") or "bootstrap-admin")
        token = rule_admin_store.set_current_actor(actor)
        try:
            yield user
        finally:
            rule_admin_store.reset_current_actor(token)

    return dependency


def _seed() -> None:
    rule_admin_store.ensure_seeded(settings.RULES_DIR)


def _handle_error(exc: RuleAdminError) -> HTTPException:
    return HTTPException(409 if isinstance(exc, RevisionConflict) else 422, str(exc))


@router.get("/checkers", dependencies=[Depends(require_rule_capability("ruleset.view"))])
def list_checkers():
    return {"checkers": checker_catalog()}


@router.get("/rulesets", dependencies=[Depends(require_rule_capability("ruleset.view"))])
def list_managed_rulesets():
    _seed()
    return {"rulesets": rule_admin_store.list_rulesets()}


@router.post("/rulesets")
def create_managed_ruleset(
    payload: dict[str, Any] = Body(...),
    user: dict[str, Any] = Depends(require_rule_capability("ruleset.edit")),
):
    _seed()
    try:
        version = rule_admin_store.create_ruleset(
            payload.get("data") or payload,
            str(payload.get("change_note", "新建规则集")),
            actor=str(user["username"]),
        )
    except RuleAdminError as exc:
        raise _handle_error(exc) from exc
    return {"version": version}


@router.get("/rulesets/{ruleset_id}", dependencies=[Depends(require_rule_capability("ruleset.view"))])
def get_managed_ruleset(ruleset_id: str):
    _seed()
    result = rule_admin_store.get_ruleset(ruleset_id)
    if result is None:
        raise HTTPException(404, "规则集不存在")
    result["audit"] = rule_admin_store.list_audit(ruleset_id)
    return result


@router.post("/rulesets/{ruleset_id}/clone")
def clone_managed_ruleset(
    ruleset_id: str,
    payload: dict[str, Any] = Body(default={}),
    user: dict[str, Any] = Depends(require_rule_capability("ruleset.edit")),
):
    try:
        version = rule_admin_store.clone_version(
            ruleset_id,
            payload.get("source_version_id"),
            str(payload.get("change_note", "克隆为新草稿")),
            actor=str(user["username"]),
        )
        return {"version": version}
    except RuleAdminError as exc:
        raise _handle_error(exc) from exc


@router.post("/rulesets/{ruleset_id}/disable", dependencies=[Depends(require_rule_capability("ruleset.disable"))])
def disable_managed_ruleset(ruleset_id: str):
    try:
        return rule_admin_store.set_ruleset_status(ruleset_id, True)
    except RuleAdminError as exc:
        raise _handle_error(exc) from exc


@router.post("/rulesets/{ruleset_id}/enable", dependencies=[Depends(require_rule_capability("ruleset.disable"))])
def enable_managed_ruleset(ruleset_id: str):
    try:
        return rule_admin_store.set_ruleset_status(ruleset_id, False)
    except RuleAdminError as exc:
        raise _handle_error(exc) from exc


@router.delete("/rulesets/{ruleset_id}", dependencies=[Depends(require_rule_capability("ruleset.delete"))])
def delete_managed_ruleset(ruleset_id: str):
    try:
        return rule_admin_store.delete_ruleset(ruleset_id)
    except RuleAdminError as exc:
        raise _handle_error(exc) from exc


@router.post("/rulesets/{ruleset_id}/restore")
def restore_managed_ruleset(
    ruleset_id: str,
    payload: dict[str, Any] = Body(...),
    user: dict[str, Any] = Depends(require_rule_capability("ruleset.edit")),
):
    try:
        version = rule_admin_store.restore(
            ruleset_id,
            str(payload.get("source_version_id", "")),
            str(payload.get("change_note", "回滚历史版本")),
            actor=str(user["username"]),
        )
        return {"version": version}
    except RuleAdminError as exc:
        raise _handle_error(exc) from exc


@router.post("/validate", dependencies=[Depends(require_rule_capability("ruleset.edit"))])
def validate_managed_ruleset(payload: dict[str, Any] = Body(...)):
    data = payload.get("data") or payload
    ruleset, errors, warnings = validate_ruleset(data)
    return {
        "valid": ruleset is not None and not errors,
        "errors": errors,
        "warnings": warnings,
        "normalized": ruleset.model_dump() if ruleset else None,
    }


@router.get("/versions/{version_id}", dependencies=[Depends(require_rule_capability("ruleset.view"))])
def get_managed_version(version_id: str):
    version = rule_admin_store.get_version(version_id)
    if version is None:
        raise HTTPException(404, "版本不存在")
    return version


@router.put("/versions/{version_id}", dependencies=[Depends(require_rule_capability("ruleset.edit"))])
def save_managed_version(version_id: str, payload: SaveVersionRequest):
    try:
        version = rule_admin_store.save_draft(
            version_id,
            payload.data,
            payload.revision,
            payload.change_note,
        )
        return {"version": version}
    except RuleAdminError as exc:
        raise _handle_error(exc) from exc


@router.put("/versions/{version_id}/yaml", dependencies=[Depends(require_rule_capability("ruleset.edit"))])
def save_managed_yaml(version_id: str, payload: SaveYamlRequest):
    try:
        data = yaml.safe_load(payload.yaml) or {}
    except yaml.YAMLError as exc:
        raise HTTPException(422, f"YAML 无效：{exc}") from exc
    try:
        version = rule_admin_store.save_draft(
            version_id, data, payload.revision, payload.change_note,
        )
        return {"version": version}
    except RuleAdminError as exc:
        raise _handle_error(exc) from exc


@router.delete("/versions/{version_id}", dependencies=[Depends(require_rule_capability("ruleset.delete_draft"))])
def delete_managed_version(version_id: str):
    try:
        rule_admin_store.delete_draft(version_id)
        return {"deleted": True}
    except RuleAdminError as exc:
        raise _handle_error(exc) from exc


@router.post("/versions/{version_id}/submit")
def submit_managed_version(
    version_id: str,
    payload: dict[str, Any] = Body(default={}),
    user: dict[str, Any] = Depends(require_rule_capability("ruleset.submit")),
):
    try:
        reviewer = str(payload.get("assigned_reviewer", "")).strip()
        if not auth_service.is_eligible_reviewer(reviewer):
            raise RuleAdminError("指定审核员不存在、已停用或不具备审核员身份")
        return {"version": rule_admin_store.set_version_state(
            version_id, "submit", str(payload.get("note", "")),
            actor=str(user["username"]), assigned_reviewer=reviewer,
        )}
    except RuleAdminError as exc:
        raise _handle_error(exc) from exc


@router.post("/versions/{version_id}/approve")
def approve_managed_version(
    version_id: str,
    payload: dict[str, Any] = Body(default={}),
    user: dict[str, Any] = Depends(require_rule_capability("ruleset.approve")),
):
    try:
        return {"version": rule_admin_store.set_version_state(
            version_id, "approve", str(payload.get("note", "")),
            actor=str(user["username"]),
        )}
    except RuleAdminError as exc:
        raise _handle_error(exc) from exc


@router.post("/versions/{version_id}/reject")
def reject_managed_version(
    version_id: str,
    payload: dict[str, Any] = Body(default={}),
    user: dict[str, Any] = Depends(require_rule_capability("ruleset.approve")),
):
    try:
        return {"version": rule_admin_store.set_version_state(
            version_id, "reject", str(payload.get("note", "")),
            actor=str(user["username"]),
        )}
    except RuleAdminError as exc:
        raise _handle_error(exc) from exc


@router.post("/versions/{version_id}/publish")
def publish_managed_version(
    version_id: str,
    payload: dict[str, Any] = Body(default={}),
    user: dict[str, Any] = Depends(require_rule_capability("ruleset.publish")),
):
    try:
        return {"version": rule_admin_store.publish(
            version_id, settings.RULES_DIR, str(payload.get("note", "")),
            actor=str(user["username"]),
        )}
    except RuleAdminError as exc:
        raise _handle_error(exc) from exc


def _mutate_rule(
    version_id: str,
    payload: RuleMutationRequest,
    operation: str,
) -> dict[str, Any]:
    version = rule_admin_store.get_version(version_id)
    if version is None:
        raise HTTPException(404, "版本不存在")
    data = version["data"]
    rules = data.setdefault("rules", [])
    rule_id = payload.rule_id
    index = next((idx for idx, rule in enumerate(rules) if rule.get("id") == rule_id), -1)
    if index < 0:
        raise HTTPException(404, "规则不存在")
    if operation == "toggle":
        rules[index]["enabled"] = payload.enabled
    elif operation == "copy":
        copied = dict(rules[index])
        copied["params"] = dict(copied.get("params", {}))
        copied["id"] = str(payload.new_rule_id or "").strip()
        if not copied["id"]:
            raise HTTPException(422, "必须指定新规则 ID")
        rules.insert(index + 1, copied)
    elif operation == "reorder":
        target = index if payload.target_index is None else payload.target_index
        if not 0 <= target < len(rules):
            raise HTTPException(422, "目标位置超出范围")
        rule = rules.pop(index)
        rules.insert(target, rule)
    try:
        return rule_admin_store.save_draft(
            version_id, data, payload.revision, payload.change_note or operation
        )
    except RuleAdminError as exc:
        raise _handle_error(exc) from exc


@router.post("/versions/{version_id}/rules/toggle", dependencies=[Depends(require_rule_capability("ruleset.edit"))])
def toggle_rule(version_id: str, payload: RuleMutationRequest):
    return {"version": _mutate_rule(version_id, payload, "toggle")}


@router.post("/versions/{version_id}/rules/copy", dependencies=[Depends(require_rule_capability("ruleset.edit"))])
def copy_rule(version_id: str, payload: RuleMutationRequest):
    return {"version": _mutate_rule(version_id, payload, "copy")}


@router.post("/versions/{version_id}/rules/reorder", dependencies=[Depends(require_rule_capability("ruleset.edit"))])
def reorder_rule(version_id: str, payload: RuleMutationRequest):
    return {"version": _mutate_rule(version_id, payload, "reorder")}


@router.post("/versions/{version_id}/test")
async def test_managed_version(
    version_id: str,
    payload: TestVersionRequest,
    user: dict[str, Any] = Depends(require_rule_capability("ruleset.test")),
):
    version = rule_admin_store.get_version(version_id)
    if version is None:
        raise HTTPException(404, "版本不存在")
    document_id = payload.document_id
    if payload.use_ai and not payload.privacy_consent:
        raise HTTPException(422, "使用外部 AI 前必须确认样例材料发送授权")
    if payload.use_ai:
        # B5b：试跑同样受部署级外发总开关约束并记录授权审计
        from app.api.routes import _ai_policy_version, _egress_allowed
        if not _egress_allowed():
            raise HTTPException(
                422,
                "外部 AI 外发已被部署级策略禁用；请关闭 AI 选项或联系管理员",
            )
        review_store.record_egress_consent(
            purpose="rule_test",
            subject_id=version_id,
            actor_id=user.get("id", ""),
            actor_name=user.get("username", ""),
            consent_granted=bool(payload.privacy_consent),
            ai_enabled=True,
            policy_version=_ai_policy_version(),
        )
    document = review_store.get_document(document_id)
    if document is None:
        raise HTTPException(404, "样例文档不存在，请先通过上传页面上传")
    if (
        "review.view_all" not in set(user.get("capabilities", []))
        and document.get("owner_id")
        and document.get("owner_id") != user.get("id")
    ):
        raise HTTPException(404, "样例文档不存在")

    def execute() -> dict[str, Any]:
        with task_scope(
            f"rule-test-{uuid.uuid4().hex[:12]}",
            threading.Event(),
            settings.TASK_TIMEOUT_SECONDS,
            # R08：试跑若开启 AI，则上面的入口已完成授权校验并落库审计；
            # 这里显式打开材料外发授权，AI 规则之外的路径仍保持默认拒绝。
            egress_authorized=bool(payload.use_ai),
        ):
            source = Path(document["file_path"])
            if document["file_type"] == "docx":
                converted = document.get("converted_path")
                source = (
                    Path(converted)
                    if converted and Path(converted).exists()
                    else convert_docx_to_pdf(source)
                )
            ir = get_parser(str(source)).parse(str(source))
            engine = RuleEngine("admin-test.yaml", ruleset_text=version["yaml"])
            selected = set(payload.rule_ids)
            issues = engine.run(
                ir,
                should_skip=lambda rule: (
                    rule_requires_ai(rule) and not payload.use_ai
                ) or (bool(selected) and rule.id not in selected),
            )
            rules_executed = len([
                rule for rule in engine.ruleset.rules
                if rule.enabled and (not selected or rule.id in selected)
                and (payload.use_ai or not rule_requires_ai(rule))
            ])
            system_failures = sum(issue.layer == "system" for issue in issues)
            degraded_checks = sum(
                issue.layer == "llm"
                and issue.severity == "info"
                and issue.message.startswith("AI 检查未执行")
                for issue in issues
            )
            success = (
                rules_executed > 0
                and system_failures == 0
                and not (payload.use_ai and degraded_checks > 0)
            )
            return {
                "rules_executed": rules_executed,
                "issue_count": len(issues),
                "errors": sum(issue.severity == "error" for issue in issues),
                "warnings": sum(issue.severity == "warning" for issue in issues),
                "system_failures": system_failures,
                "degraded_checks": degraded_checks,
                "success": success,
                "release_eligible": success and not selected,
                "issues": [issue.model_dump() for issue in issues],
            }

    try:
        result = await run_in_threadpool(execute)
    except LibreOfficeNotFound as exc:
        raise HTTPException(
            503,
            "服务端未安装 LibreOffice，无法试跑 DOCX 样例（PDF 样例不受影响）。"
            "Windows 可执行 choco install libreoffice-fresh，"
            "或用 SOFFICE_PATH 指定 soffice 路径后重试。",
        ) from exc
    except TaskDeadlineExceeded as exc:
        raise HTTPException(504, str(exc)) from exc
    return rule_admin_store.record_test_run(
        version["ruleset_id"], version_id, document_id, version["sha256"], result
    )
