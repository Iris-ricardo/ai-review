"""Versioned ruleset administration repository for the local deployment."""
from __future__ import annotations

import hashlib
import logging
import uuid
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func, select

from app.models import ManagedRuleSet, RuleAdminAudit, RuleSetVersion, RuleTestRun
from app.models.base import SessionLocal
from app.services.rules.catalog import validate_ruleset


EDITABLE_STATES = {"draft", "rejected"}
WORKING_STATES = EDITABLE_STATES | {"submitted", "approved"}
_current_actor: ContextVar[str] = ContextVar("rule_admin_actor", default="bootstrap-admin")
logger = logging.getLogger(__name__)


class RuleAdminError(RuntimeError):
    pass


class RevisionConflict(RuleAdminError):
    pass


def set_current_actor(actor: str):
    return _current_actor.set(actor or "bootstrap-admin")


def reset_current_actor(token) -> None:
    _current_actor.reset(token)


def actor_name(actor: str = "") -> str:
    return actor or _current_actor.get()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dump(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=120)


def _version_dict(model: RuleSetVersion, *, include_content: bool = True) -> dict[str, Any]:
    result = {
        "id": model.id,
        "ruleset_id": model.ruleset_id,
        "version_number": model.version_number,
        "revision": model.revision,
        "state": model.state,
        "sha256": model.content_sha256,
        "change_note": model.change_note,
        "validation": model.validation_json or {},
        "created_by": model.created_by,
        "assigned_reviewer": model.assigned_reviewer,
        "submitted_by": model.submitted_by,
        "submitted_at": model.submitted_at.isoformat() if model.submitted_at else "",
        "approved_by": model.approved_by,
        "approved_at": model.approved_at.isoformat() if model.approved_at else "",
        "approval_note": model.approval_note,
        "rejected_by": model.rejected_by,
        "rejected_at": model.rejected_at.isoformat() if model.rejected_at else "",
        "rejection_reason": model.rejection_reason,
        "published_by": model.published_by,
        "created_at": model.created_at.isoformat() if model.created_at else "",
        "updated_at": model.updated_at.isoformat() if model.updated_at else "",
        "published_at": model.published_at.isoformat() if model.published_at else "",
    }
    if include_content:
        result["yaml"] = model.yaml_text
        result["data"] = yaml.safe_load(model.yaml_text) or {}
    return result


def _test_run_dict(model: RuleTestRun | None) -> dict[str, Any] | None:
    if model is None:
        return None
    result = dict(model.result_json or {})
    return {
        "id": model.id,
        "status": model.status,
        "document_id": model.document_id,
        "content_sha256": model.content_sha256,
        "created_at": model.created_at.isoformat() if model.created_at else "",
        **result,
    }


def _audit(session, ruleset_id: str, action: str, version_id: str | None = None, detail: dict | None = None) -> None:
    session.add(RuleAdminAudit(
        ruleset_id=ruleset_id,
        version_id=version_id,
        action=action,
        actor=_current_actor.get(),
        detail_json=detail or {},
    ))


def ensure_seeded(rules_dir: str | Path) -> None:
    """Idempotently import YAML files as published version 1."""
    directory = Path(rules_dir).resolve()
    if not directory.exists():
        return
    with SessionLocal.begin() as session:
        existing_ids = set(session.scalars(select(ManagedRuleSet.id)).all())
        for path in sorted(directory.glob("*.yaml")):
            try:
                text = path.read_text(encoding="utf-8")
                data = yaml.safe_load(text) or {}
                ruleset_id = str(data.get("ruleset") or path.stem)
                ruleset, errors, warnings = validate_ruleset(data)
            except Exception:
                continue
            if ruleset_id in existing_ids or ruleset is None or errors:
                continue
            catalog = ManagedRuleSet(
                id=ruleset_id,
                name=ruleset.name,
                description=ruleset.description,
                status="active",
            )
            session.add(catalog)
            session.flush()
            version = RuleSetVersion(
                id=uuid.uuid4().hex[:12],
                ruleset_id=ruleset_id,
                version_number=1,
                revision=1,
                state="published",
                yaml_text=text,
                content_sha256=_sha(text),
                validation_json={"valid": True, "errors": [], "warnings": warnings},
                change_note=f"从 {path.name} 初始化导入",
                published_at=datetime.now(),
            )
            session.add(version)
            session.flush()
            catalog.active_version_id = version.id
            _audit(session, ruleset_id, "seed", version.id, {"source": path.name})
            existing_ids.add(ruleset_id)


def list_rulesets() -> list[dict[str, Any]]:
    with SessionLocal() as session:
        items = session.scalars(select(ManagedRuleSet).order_by(ManagedRuleSet.id)).all()
        results = []
        for item in items:
            active = session.get(RuleSetVersion, item.active_version_id) if item.active_version_id else None
            draft = session.scalar(
                select(RuleSetVersion).where(
                    RuleSetVersion.ruleset_id == item.id,
                    RuleSetVersion.state.in_(list(WORKING_STATES)),
                ).order_by(RuleSetVersion.version_number.desc())
            )
            rule_count = len((yaml.safe_load(active.yaml_text) or {}).get("rules", [])) if active else 0
            results.append({
                "id": item.id,
                "name": item.name,
                "description": item.description,
                "status": item.status,
                "rule_count": rule_count,
                "active_version": _version_dict(active, include_content=False) if active else None,
                "working_version": _version_dict(draft, include_content=False) if draft else None,
            })
        return results


def get_ruleset(ruleset_id: str) -> dict[str, Any] | None:
    with SessionLocal() as session:
        item = session.get(ManagedRuleSet, ruleset_id)
        if item is None:
            return None
        versions = session.scalars(
            select(RuleSetVersion).where(RuleSetVersion.ruleset_id == ruleset_id)
            .order_by(RuleSetVersion.version_number.desc())
        ).all()
        active = next((version for version in versions if version.id == item.active_version_id), None)
        working = next((version for version in versions if version.state in WORKING_STATES), None)
        working_data = _version_dict(working) if working else None
        if working_data is not None:
            latest_test = session.scalar(select(RuleTestRun).where(
                RuleTestRun.version_id == working.id,
                RuleTestRun.content_sha256 == working.content_sha256,
            ).order_by(RuleTestRun.created_at.desc()))
            working_data["latest_test"] = _test_run_dict(latest_test)
        return {
            "id": item.id,
            "name": item.name,
            "description": item.description,
            "status": item.status,
            "active_version": _version_dict(active) if active else None,
            "working_version": working_data,
            "versions": [_version_dict(version, include_content=False) for version in versions],
        }


def get_version(version_id: str) -> dict[str, Any] | None:
    with SessionLocal() as session:
        model = session.get(RuleSetVersion, version_id)
        if model is None:
            return None
        result = _version_dict(model)
        latest_test = session.scalar(select(RuleTestRun).where(
            RuleTestRun.version_id == model.id,
            RuleTestRun.content_sha256 == model.content_sha256,
        ).order_by(RuleTestRun.created_at.desc()))
        result["latest_test"] = _test_run_dict(latest_test)
        return result


def get_published_yaml(ruleset_id: str, *, allow_disabled: bool = False) -> str | None:
    with SessionLocal() as session:
        item = session.get(ManagedRuleSet, ruleset_id)
        if item is None or not item.active_version_id:
            return None
        if item.status == "disabled" and not allow_disabled:
            return None
        version = session.get(RuleSetVersion, item.active_version_id)
        return version.yaml_text if version else None


def create_ruleset(data: dict[str, Any], change_note: str = "新建规则集", *, actor: str = "") -> dict[str, Any]:
    ruleset, errors, warnings = validate_ruleset(data)
    if ruleset is None or errors:
        raise RuleAdminError(str(errors))
    text = _dump(ruleset.model_dump(exclude_defaults=True))
    with SessionLocal.begin() as session:
        if session.get(ManagedRuleSet, ruleset.ruleset):
            raise RuleAdminError("规则集 ID 已存在")
        item = ManagedRuleSet(
            id=ruleset.ruleset, name=ruleset.name, description=ruleset.description,
            status="draft",
        )
        version = RuleSetVersion(
            id=uuid.uuid4().hex[:12], ruleset_id=ruleset.ruleset,
            version_number=1, revision=1, state="draft", yaml_text=text,
            content_sha256=_sha(text), change_note=change_note,
            validation_json={"valid": True, "errors": [], "warnings": warnings},
            created_by=actor_name(actor),
        )
        session.add_all([item, version])
        _audit(session, ruleset.ruleset, "create", version.id)
        session.flush()
        return _version_dict(version)


def save_legacy_draft(data: dict[str, Any], text: str) -> dict[str, Any]:
    """Store the legacy YAML PUT as a draft without bypassing release gates."""
    ruleset, errors, warnings = validate_ruleset(data)
    if ruleset is None or errors:
        raise RuleAdminError(str(errors))
    with SessionLocal.begin() as session:
        item = session.get(ManagedRuleSet, ruleset.ruleset)
        if item is None:
            item = ManagedRuleSet(
                id=ruleset.ruleset, name=ruleset.name,
                description=ruleset.description, status="draft",
            )
            session.add(item)
            next_number = 1
        else:
            existing = session.scalar(select(RuleSetVersion).where(
                RuleSetVersion.ruleset_id == ruleset.ruleset,
                RuleSetVersion.state.in_(list(WORKING_STATES)),
            ).order_by(RuleSetVersion.version_number.desc()))
            if existing is not None:
                if existing.state not in EDITABLE_STATES:
                    raise RuleAdminError("已提交版本不可通过兼容接口改写，请先驳回")
                existing.yaml_text = text
                existing.content_sha256 = _sha(text)
                existing.revision += 1
                existing.change_note = "通过兼容 YAML 接口保存草稿"
                existing.validation_json = {
                    "valid": True, "errors": [], "warnings": warnings,
                }
                _audit(session, ruleset.ruleset, "legacy_draft_save", existing.id)
                session.flush()
                return _version_dict(existing)
            next_number = int(session.scalar(select(func.max(RuleSetVersion.version_number)).where(
                RuleSetVersion.ruleset_id == ruleset.ruleset
            )) or 0) + 1
        version = RuleSetVersion(
            id=uuid.uuid4().hex[:12], ruleset_id=ruleset.ruleset,
            version_number=next_number, revision=1, state="draft",
            yaml_text=text, content_sha256=_sha(text),
            change_note="通过兼容 YAML 接口保存草稿",
            validation_json={"valid": True, "errors": [], "warnings": warnings},
        )
        session.add(version)
        _audit(session, ruleset.ruleset, "legacy_draft_create", version.id)
        session.flush()
        return _version_dict(version)


def clone_version(ruleset_id: str, source_version_id: str | None = None, change_note: str = "克隆为新草稿", *, actor: str = "") -> dict[str, Any]:
    with SessionLocal.begin() as session:
        item = session.get(ManagedRuleSet, ruleset_id)
        if item is None:
            raise RuleAdminError("规则集不存在")
        existing = session.scalar(select(RuleSetVersion).where(
            RuleSetVersion.ruleset_id == ruleset_id,
            RuleSetVersion.state.in_(list(WORKING_STATES)),
        ))
        if existing:
            raise RuleAdminError("已有未完成的工作版本")
        source_id = source_version_id or item.active_version_id
        source = session.get(RuleSetVersion, source_id) if source_id else None
        if source is None or source.ruleset_id != ruleset_id:
            raise RuleAdminError("源版本不存在")
        next_number = int(session.scalar(select(func.max(RuleSetVersion.version_number)).where(
            RuleSetVersion.ruleset_id == ruleset_id
        )) or 0) + 1
        version = RuleSetVersion(
            id=uuid.uuid4().hex[:12], ruleset_id=ruleset_id,
            version_number=next_number, revision=1, state="draft",
            yaml_text=source.yaml_text, content_sha256=source.content_sha256,
            change_note=change_note, validation_json=source.validation_json,
            created_by=actor_name(actor),
        )
        session.add(version)
        _audit(session, ruleset_id, "clone", version.id, {"source_version_id": source.id})
        session.flush()
        return _version_dict(version)


def save_draft(version_id: str, data: dict[str, Any], expected_revision: int, change_note: str = "") -> dict[str, Any]:
    ruleset, errors, warnings = validate_ruleset(data)
    if ruleset is None or errors:
        raise RuleAdminError(str(errors))
    text = _dump(ruleset.model_dump(exclude_defaults=True))
    with SessionLocal.begin() as session:
        version = session.get(RuleSetVersion, version_id)
        if version is None:
            raise RuleAdminError("版本不存在")
        if version.state not in EDITABLE_STATES:
            raise RuleAdminError("只有草稿或已驳回版本可以修改")
        if version.revision != expected_revision:
            raise RevisionConflict(f"版本已被其他操作更新，当前 revision={version.revision}")
        if ruleset.ruleset != version.ruleset_id:
            raise RuleAdminError("规则集 ID 不能修改")
        version.yaml_text = text
        version.content_sha256 = _sha(text)
        version.revision += 1
        version.state = "draft"
        version.change_note = change_note or version.change_note
        version.validation_json = {"valid": True, "errors": [], "warnings": warnings}
        _audit(session, version.ruleset_id, "save_draft", version.id, {"revision": version.revision})
        session.flush()
        return _version_dict(version)


def set_version_state(
    version_id: str,
    action: str,
    note: str = "",
    *,
    actor: str = "",
    assigned_reviewer: str = "",
) -> dict[str, Any]:
    if action not in {"submit", "approve", "reject"}:
        raise RuleAdminError("不支持的版本操作")
    who = actor_name(actor)
    with SessionLocal.begin() as session:
        version = session.get(RuleSetVersion, version_id)
        if version is None:
            raise RuleAdminError("版本不存在")

        if action == "submit":
            if version.state != "draft":
                raise RuleAdminError("只有草稿版本可以提交审核")
            if not assigned_reviewer:
                raise RuleAdminError("提交审核时必须指定审核员")
            if assigned_reviewer == who:
                raise RuleAdminError("提交人不能把审核任务指派给自己")
            tested = session.scalar(select(RuleTestRun.id).where(
                RuleTestRun.version_id == version.id,
                RuleTestRun.content_sha256 == version.content_sha256,
                RuleTestRun.status == "done",
            ).limit(1))
            if tested is None:
                raise RuleAdminError("提交审核前必须使用当前版本运行并通过一次样例测试")
            version.state = "submitted"
            version.assigned_reviewer = assigned_reviewer
            version.submitted_by = who
            version.submitted_at = datetime.now()
            version.approved_by = ""
            version.approved_at = None
            version.approval_note = ""
            version.rejected_by = ""
            version.rejected_at = None
            version.rejection_reason = ""

        elif action == "approve":
            if version.state != "submitted":
                raise RuleAdminError("只有待审核版本可以审核通过")
            if who != version.assigned_reviewer:
                raise RuleAdminError("只有被指派的审核员可以处理此版本")
            if who in {version.submitted_by, version.created_by}:
                raise RuleAdminError("创建人或提交人不能审核自己的版本")
            version.state = "approved"
            version.approved_by = who
            version.approved_at = datetime.now()
            version.approval_note = note

        else:
            if version.state != "submitted":
                raise RuleAdminError("只有待审核版本可以驳回")
            if who != version.assigned_reviewer:
                raise RuleAdminError("只有被指派的审核员可以处理此版本")
            if who in {version.submitted_by, version.created_by}:
                raise RuleAdminError("创建人或提交人不能审核自己的版本")
            if not note.strip():
                raise RuleAdminError("驳回时必须填写整改原因")
            version.state = "rejected"
            version.rejected_by = who
            version.rejected_at = datetime.now()
            version.rejection_reason = note.strip()

        version.change_note = note or version.change_note
        _audit(session, version.ruleset_id, action, version.id, {
            "note": note,
            "assigned_reviewer": version.assigned_reviewer,
            "submitted_by": version.submitted_by,
        })
        session.flush()
        return _version_dict(version)


def publish(
    version_id: str,
    rules_dir: str | Path,
    note: str = "",
    *,
    actor: str = "",
) -> dict[str, Any]:
    who = actor_name(actor)
    with SessionLocal.begin() as session:
        version = session.get(RuleSetVersion, version_id)
        if version is None:
            raise RuleAdminError("版本不存在")
        if version.state != "approved":
            raise RuleAdminError("版本必须先由指定审核员审核通过，才能发布")
        if who != version.assigned_reviewer:
            raise RuleAdminError("只有完成审核的指定审核员可以发布此版本")
        if who in {version.submitted_by, version.created_by}:
            raise RuleAdminError("创建人或提交人不能发布自己的版本")
        data = yaml.safe_load(version.yaml_text) or {}
        ruleset, errors, warnings = validate_ruleset(data)
        if errors:
            raise RuleAdminError(str(errors))
        if ruleset is None or not any(rule.enabled for rule in ruleset.rules):
            raise RuleAdminError("发布版本必须至少包含一条已启用规则")
        tested = session.scalar(select(RuleTestRun.id).where(
            RuleTestRun.ruleset_id == version.ruleset_id,
            RuleTestRun.content_sha256 == version.content_sha256,
            RuleTestRun.status == "done",
        ).limit(1))
        if tested is None:
            raise RuleAdminError("发布前必须使用当前内容运行一次样例测试")
        version.state = "published"
        version.published_at = datetime.now()
        version.published_by = who
        version.change_note = note or version.change_note
        version.validation_json = {"valid": True, "errors": [], "warnings": warnings}
        item = session.get(ManagedRuleSet, version.ruleset_id)
        previous = session.get(RuleSetVersion, item.active_version_id) if item.active_version_id else None
        if previous is not None and previous.id != version.id:
            previous.state = "retired"
        item.active_version_id = version.id
        item.status = "active"
        item.name = str(data.get("name", ""))
        item.description = str(data.get("description", ""))
        _audit(session, version.ruleset_id, "publish", version.id, {"sha256": version.content_sha256})
        session.flush()
        result = _version_dict(version)
        export_ruleset_id = version.ruleset_id
        export_text = version.yaml_text
    try:
        _sync_export(Path(rules_dir), export_ruleset_id, export_text)
    except OSError:
        logger.exception(
            "Published ruleset %s but failed to update the YAML export",
            export_ruleset_id,
        )
        result["export_synced"] = False
        result["export_warning"] = "发布已生效，但 YAML 导出同步失败，请检查目录权限后重试导出"
    else:
        result["export_synced"] = True
    return result


def restore(ruleset_id: str, source_version_id: str, note: str = "恢复历史版本为草稿", *, actor: str = "") -> dict[str, Any]:
    draft = clone_version(ruleset_id, source_version_id, note, actor=actor)
    with SessionLocal.begin() as session:
        _audit(
            session, ruleset_id, "restore_to_draft", draft["id"],
            {"source_version_id": source_version_id},
        )
    return draft


def set_ruleset_status(ruleset_id: str, disabled: bool) -> dict[str, Any]:
    with SessionLocal.begin() as session:
        item = session.get(ManagedRuleSet, ruleset_id)
        if item is None:
            raise RuleAdminError("规则集不存在")
        if not item.active_version_id and not disabled:
            raise RuleAdminError("尚无发布版本，无法恢复")
        item.status = "disabled" if disabled else "active"
        _audit(session, ruleset_id, "disable" if disabled else "enable", item.active_version_id)
        session.flush()
        return {"id": item.id, "status": item.status}


def delete_ruleset(ruleset_id: str) -> dict[str, Any]:
    """Delete a ruleset only while it has never been published."""
    with SessionLocal.begin() as session:
        item = session.get(ManagedRuleSet, ruleset_id)
        if item is None:
            raise RuleAdminError("规则集不存在")
        if item.active_version_id:
            raise RuleAdminError("已发布规则集不能彻底删除，请使用停用规则集")
        versions = session.scalars(select(RuleSetVersion).where(
            RuleSetVersion.ruleset_id == ruleset_id
        )).all()
        if any(version.state not in EDITABLE_STATES for version in versions):
            raise RuleAdminError("存在不可编辑版本，不能彻底删除规则集")
        for version in versions:
            session.delete(version)
        test_runs = session.scalars(select(RuleTestRun).where(
            RuleTestRun.ruleset_id == ruleset_id
        )).all()
        for test_run in test_runs:
            session.delete(test_run)
        session.flush()
        _audit(session, ruleset_id, "delete_ruleset")
        session.delete(item)
        return {"id": ruleset_id, "deleted": True}


def delete_draft(version_id: str) -> None:
    with SessionLocal.begin() as session:
        version = session.get(RuleSetVersion, version_id)
        if version is None:
            raise RuleAdminError("版本不存在")
        if version.state not in EDITABLE_STATES:
            raise RuleAdminError("只能删除草稿或已驳回版本")
        _audit(session, version.ruleset_id, "delete_draft", version.id)
        session.delete(version)


def list_audit(ruleset_id: str) -> list[dict[str, Any]]:
    with SessionLocal() as session:
        rows = session.scalars(select(RuleAdminAudit).where(
            RuleAdminAudit.ruleset_id == ruleset_id
        ).order_by(RuleAdminAudit.created_at.desc()).limit(100)).all()
        return [{
            "id": row.id, "version_id": row.version_id, "action": row.action,
            "actor": row.actor, "detail": row.detail_json or {},
            "created_at": row.created_at.isoformat() if row.created_at else "",
        } for row in rows]


def record_test_run(ruleset_id: str, version_id: str, document_id: str, sha256: str, result: dict) -> dict:
    if result.get("success") is False or int(result.get("system_failures", 0)) > 0:
        status = "failed"
    elif result.get("release_eligible") is False:
        status = "partial"
    else:
        status = "done"
    with SessionLocal.begin() as session:
        model = RuleTestRun(
            id=uuid.uuid4().hex[:12], ruleset_id=ruleset_id,
            version_id=version_id, document_id=document_id,
            content_sha256=sha256,
            status=status,
            result_json=result,
        )
        session.add(model)
        _audit(session, ruleset_id, "test", version_id, {"test_run_id": model.id})
        session.flush()
        return {
            "id": model.id,
            "status": model.status,
            **result,
            "content_sha256": sha256,
        }


def _sync_export(directory: Path, ruleset_id: str, text: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    target = None
    for path in directory.glob("*.yaml"):
        try:
            if (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("ruleset") == ruleset_id:
                target = path
                break
        except Exception:
            continue
    target = target or directory / f"{ruleset_id}.yaml"
    temporary = target.with_suffix(".yaml.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(target)
