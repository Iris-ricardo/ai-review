"""Durable repository for documents, reviews and batches."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, select, update

from app.models import (
    Document,
    EgressConsent,
    ReviewBatch,
    ReviewBatchItem,
    ReviewIssue,
    ReviewTask,
)
from app.models.base import Base, SessionLocal, engine


TERMINAL_STATUSES = {"done", "failed", "cancelled", "timed_out"}
ACTIVE_STATUSES = {"queued", "running", "cancelling"}


def now() -> datetime:
    return datetime.now()


def record_egress_consent(
    *,
    purpose: str,
    subject_id: str = "",
    actor_id: str = "",
    actor_name: str = "",
    consent_granted: bool = False,
    ai_enabled: bool = True,
    policy_version: str = "",
) -> None:
    """记录一次 AI 入口的授权/策略审计（B5b）：主体、用途、授权、版本、时间。"""
    with SessionLocal.begin() as session:
        session.add(EgressConsent(
            purpose=purpose,
            subject_id=subject_id,
            actor_id=actor_id,
            actor_name=actor_name,
            consent_granted=consent_granted,
            ai_enabled=ai_enabled,
            policy_version=policy_version,
        ))


def egress_consent_granted(subject_id: str, purpose: str = "single_review") -> bool:
    """该任务是否已持久化“同意材料外发”的授权记录（R08）。

    无记录（含历史任务）→ False，按**保守策略**处理：不推定用户同意，AI 规则一律跳过（结论 incomplete）。"""
    if not subject_id:
        return False
    with SessionLocal() as session:
        exists = session.scalar(
            select(EgressConsent.id)
            .where(EgressConsent.subject_id == subject_id)
            .where(EgressConsent.purpose == purpose)
            .where(EgressConsent.consent_granted == True)  # noqa: E712 —— SQL 表达式
            .limit(1)
        )
    return exists is not None


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def init_store() -> None:
    Base.metadata.create_all(bind=engine)
    _migrate_sqlite_schema()


def _migrate_sqlite_schema() -> None:
    """Add columns introduced by the durable-task refactor.

    ``create_all`` never alters existing tables, so the migration is additive and an existing review.db opens without deleting user data."""
    if engine.dialect.name != "sqlite":
        return
    columns: dict[str, dict[str, str]] = {
        "documents": {
            "mime_type": "VARCHAR(128) NOT NULL DEFAULT ''",
            "sha256": "VARCHAR(64) NOT NULL DEFAULT ''",
            "converted_path": "VARCHAR(1024)",
            "ir_json": "JSON",
            "owner_id": "VARCHAR(32) NOT NULL DEFAULT ''",
        },
        "review_tasks": {
            "stage": "VARCHAR(64) NOT NULL DEFAULT 'queued'",
            "current_rule": "VARCHAR(64)",
            "rules_completed": "INTEGER NOT NULL DEFAULT 0",
            "rules_total": "INTEGER NOT NULL DEFAULT 0",
            "rule_status_json": "JSON",
            "ruleset_snapshot": "TEXT",
            "idempotency_key": "VARCHAR(128) NOT NULL DEFAULT ''",
            "use_ai": "BOOLEAN NOT NULL DEFAULT 1",
            "cancel_requested": "BOOLEAN NOT NULL DEFAULT 0",
            "degraded_checks": "INTEGER NOT NULL DEFAULT 0",
            "total_issues": "INTEGER NOT NULL DEFAULT 0",
            "errors": "INTEGER NOT NULL DEFAULT 0",
            "warnings": "INTEGER NOT NULL DEFAULT 0",
            "infos": "INTEGER NOT NULL DEFAULT 0",
            "duration_seconds": "FLOAT",
            "started_at": "DATETIME",
            "last_activity_at": "DATETIME",
            "owner_id": "VARCHAR(32) NOT NULL DEFAULT ''",
        },
        "review_batches": {
            "owner_id": "VARCHAR(32) NOT NULL DEFAULT ''",
        },
        "users": {
            "must_change_password": "BOOLEAN NOT NULL DEFAULT 0",
        },
        "ruleset_versions": {
            "assigned_reviewer": "VARCHAR(64) NOT NULL DEFAULT ''",
            "submitted_by": "VARCHAR(64) NOT NULL DEFAULT ''",
            "submitted_at": "DATETIME",
            "approved_by": "VARCHAR(64) NOT NULL DEFAULT ''",
            "approved_at": "DATETIME",
            "approval_note": "TEXT NOT NULL DEFAULT ''",
            "rejected_by": "VARCHAR(64) NOT NULL DEFAULT ''",
            "rejected_at": "DATETIME",
            "rejection_reason": "TEXT NOT NULL DEFAULT ''",
            "published_by": "VARCHAR(64) NOT NULL DEFAULT ''",
        },
    }
    with engine.begin() as connection:
        for table, definitions in columns.items():
            existing = {
                row[1]
                for row in connection.exec_driver_sql(
                    f'PRAGMA table_info("{table}")'
                ).fetchall()
            }
            for name, ddl in definitions.items():
                if name not in existing:
                    connection.exec_driver_sql(
                        f'ALTER TABLE "{table}" ADD COLUMN "{name}" {ddl}'
                    )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_documents_sha256 ON documents (sha256)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_review_tasks_idempotency_key "
            "ON review_tasks (idempotency_key)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_documents_owner_id ON documents (owner_id)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_review_tasks_owner_id ON review_tasks (owner_id)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_review_batches_owner_id ON review_batches (owner_id)"
        )
        # Legacy submitted versions had no assigned reviewer and cannot satisfy
        # the strict approval workflow. Return them to editable draft state.
        connection.exec_driver_sql(
            "UPDATE ruleset_versions SET state='draft' "
            "WHERE state='submitted' AND assigned_reviewer=''"
        )


def create_document(data: dict[str, Any]) -> dict[str, Any]:
    with SessionLocal.begin() as session:
        model = Document(
            id=data.get("id") or new_id(),
            filename=data["filename"],
            original_path=data["file_path"],
            file_type=data["file_type"],
            file_size=int(data.get("size", 0)),
            mime_type=data.get("mime_type", ""),
            sha256=data.get("sha256", ""),
            owner_id=data.get("owner_id", ""),
        )
        session.add(model)
        session.flush()
        return document_dict(model)


def get_document(document_id: str) -> dict[str, Any] | None:
    with SessionLocal() as session:
        model = session.get(Document, document_id)
        return document_dict(model) if model else None


def list_documents(limit: int = 50, *, owner_id: str | None = None) -> list[dict[str, Any]]:
    safe_limit = max(1, min(limit, 200))
    with SessionLocal() as session:
        query = select(Document)
        if owner_id is not None:
            query = query.where(Document.owner_id == owner_id)
        models = session.scalars(query.order_by(Document.created_at.desc()).limit(safe_limit)).all()
        return [document_dict(model) for model in models]


def find_document_by_sha256(sha256: str, *, owner_id: str | None = None) -> dict[str, Any] | None:
    if not sha256:
        return None
    with SessionLocal() as session:
        query = select(Document).where(Document.sha256 == sha256)
        if owner_id is not None:
            query = query.where(Document.owner_id == owner_id)
        model = session.scalar(query.order_by(Document.created_at.desc()))
        return document_dict(model) if model else None


def set_converted_path(document_id: str, converted_path: str) -> None:
    with SessionLocal.begin() as session:
        model = session.get(Document, document_id)
        if model:
            model.converted_path = converted_path


def get_document_ir(document_id: str) -> dict[str, Any] | None:
    with SessionLocal() as session:
        model = session.get(Document, document_id)
        return model.ir_json if model else None


def set_document_ir(document_id: str, ir_json: dict[str, Any]) -> None:
    with SessionLocal.begin() as session:
        model = session.get(Document, document_id)
        if model:
            model.ir_json = ir_json


def document_dict(model: Document) -> dict[str, Any]:
    return {
        "id": model.id,
        "filename": model.filename,
        "file_path": model.original_path,
        "file_type": model.file_type,
        "size": model.file_size,
        "mime_type": model.mime_type,
        "sha256": model.sha256,
        "owner_id": model.owner_id,
        "converted_path": model.converted_path,
        "created_at": format_dt(model.created_at),
    }


def create_review(
    document_id: str,
    ruleset_id: str,
    *,
    ruleset_snapshot: str,
    idempotency_key: str,
    use_ai: bool,
    owner_id: str = "",
) -> dict[str, Any]:
    with SessionLocal.begin() as session:
        model = ReviewTask(
            id=new_id(),
            document_id=document_id,
            ruleset_id=ruleset_id,
            status="queued",
            progress=0,
            stage="queued",
            ruleset_snapshot=ruleset_snapshot,
            idempotency_key=idempotency_key,
            use_ai=use_ai,
            owner_id=owner_id,
            last_activity_at=now(),
            rule_status_json={},
        )
        session.add(model)
        session.flush()
        return review_dict(model)


def find_active_review(idempotency_key: str, *, owner_id: str | None = None) -> dict[str, Any] | None:
    if not idempotency_key:
        return None
    with SessionLocal() as session:
        query = select(ReviewTask).where(
            ReviewTask.idempotency_key == idempotency_key,
            ReviewTask.status.in_(ACTIVE_STATUSES),
        )
        if owner_id is not None:
            query = query.where(ReviewTask.owner_id == owner_id)
        model = session.scalar(query.order_by(ReviewTask.created_at.desc()))
        return review_dict(model) if model else None


def get_review(review_id: str) -> dict[str, Any] | None:
    with SessionLocal() as session:
        model = session.get(ReviewTask, review_id)
        return review_dict(model) if model else None


def get_ruleset_snapshot(review_id: str) -> str | None:
    with SessionLocal() as session:
        model = session.get(ReviewTask, review_id)
        return model.ruleset_snapshot if model else None


def list_reviews(limit: int = 50, *, owner_id: str | None = None) -> list[dict[str, Any]]:
    safe_limit = max(1, min(limit, 200))
    with SessionLocal() as session:
        query = select(ReviewTask)
        if owner_id is not None:
            query = query.where(ReviewTask.owner_id == owner_id)
        models = session.scalars(query.order_by(ReviewTask.created_at.desc()).limit(safe_limit)).all()
        return [review_dict(model) for model in models]


def update_review(
    review_id: str, *, allow_terminal: bool = False, **updates: Any
) -> dict[str, Any] | None:
    """更新任务字段；**已终态的行默认不可改写**（R05）。

    行不存在 → None；已终态 → 不修改并返回当前（数据库事实）行，避免迟到的进度/心跳/规则回调污染终态展示；需显式改写时传 allow_terminal=True；落终态必须走 ``finalize``。"""
    with SessionLocal.begin() as session:
        model = session.get(ReviewTask, review_id)
        if model is None:
            return None
        if model.status in TERMINAL_STATUSES and not allow_terminal:
            return review_dict(model)
        for key, value in updates.items():
            if hasattr(model, key):
                setattr(model, key, value)
        model.last_activity_at = now()
        session.flush()
        return review_dict(model)


# ── 终态迁移：B3/R05 原子化 + 统一胜出规则 ──
# 终态只写一次、先提交者胜出；cancel_requested=1 只允许落 cancelled；queued→running 必须走 mark_running，禁止直接 UPDATE 终态。
TERMINAL_WINNING_RULE = (
    "终态先提交者胜出；cancel_requested=1 的行只允许落 cancelled"
)


def mark_running(review_id: str) -> dict[str, Any] | None:
    """原子地把“活动且未请求取消”的行置为 running（B3）。

    返回 None 表示该行已取消/请求取消/已终态，调用方应结束执行——修复“排队期间取消的任务被再次拉起”的复活窗口。"""
    with SessionLocal.begin() as session:
        result = session.execute(
            update(ReviewTask)
            .where(ReviewTask.id == review_id)
            .where(ReviewTask.status.in_(ACTIVE_STATUSES))
            .where(ReviewTask.cancel_requested == False)
            .values(
                status="running",
                stage="parsing",
                progress=5,
                started_at=now(),
                last_activity_at=now(),
            )
        )
        if (result.rowcount or 0) == 0:
            return None
        model = session.get(ReviewTask, review_id)
        return review_dict(model) if model else None


def finalize(review_id: str, status: str, **updates: Any) -> bool:
    """原子终态写入；被胜出规则拒绝或行已终态时返回 False。

    status 必须是终态之一（done/failed/cancelled/timed_out）。"""
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"finalize 只接受终态，收到 {status!r}")
    columns = ReviewTask.__table__.columns.keys()
    clean_updates = {key: value for key, value in updates.items() if key in columns}
    clause = and_(
        ReviewTask.id == review_id,
        ~ReviewTask.status.in_(TERMINAL_STATUSES),
    )
    if status != "cancelled":
        # 取消请求先提交时，完成/失败/超时都不能覆盖它（终态先提交者仍胜出）
        clause = and_(clause, ReviewTask.cancel_requested == False)
    with SessionLocal.begin() as session:
        result = session.execute(
            update(ReviewTask)
            .where(clause)
            .values(status=status, last_activity_at=now(), **clean_updates)
        )
        return (result.rowcount or 0) > 0


def request_cancel(review_id: str) -> dict[str, Any] | None:
    """原子地请求取消（R05：条件更新，取代“先读后无条件写”）。

    胜出规则与 ``finalize`` 一致：行不存在 → None；已终态 → 取消无效，不写 cancel_requested 并返回当前终态行；非终态 → 单条 UPDATE 落 cancel_requested=1/status=cancelling。这样“读取时还 running、写入前已 done”不会命中条件更新，不出现 done → cancelling 回退。"""
    with SessionLocal.begin() as session:
        result = session.execute(
            update(ReviewTask)
            .where(ReviewTask.id == review_id)
            .where(~ReviewTask.status.in_(TERMINAL_STATUSES))
            .values(
                cancel_requested=True,
                status="cancelling",
                stage="cancelling",
                last_activity_at=now(),
            )
        )
        model = session.get(ReviewTask, review_id)
        if model is None:
            return None
        if (result.rowcount or 0) == 0:
            # 已被终态先提交者落定：取消请求不生效（也不留 cancel_requested 标记）
            return review_dict(model)
        session.refresh(model)
        return review_dict(model)


def incomplete_reviews() -> list[dict[str, Any]]:
    with SessionLocal() as session:
        models = session.scalars(
            select(ReviewTask).where(ReviewTask.status.in_(ACTIVE_STATUSES))
        ).all()
        return [review_dict(model) for model in models]


def retention_candidates(days: int) -> list[dict[str, Any]]:
    cutoff = now() - timedelta(days=max(1, days))
    candidates: list[dict[str, Any]] = []
    with SessionLocal() as session:
        documents = session.scalars(select(Document)).unique().all()
        for document in documents:
            tasks = list(document.tasks)
            if any(task.status not in TERMINAL_STATUSES for task in tasks):
                continue
            timestamps = [
                task.completed_at or task.created_at
                for task in tasks
                if task.completed_at or task.created_at
            ]
            last_used = max(timestamps) if timestamps else document.created_at
            if last_used is None or last_used >= cutoff:
                continue
            candidates.append({
                "document_id": document.id,
                "review_ids": [task.id for task in tasks],
                "paths": [
                    value for value in (document.original_path, document.converted_path)
                    if value
                ],
                "last_used_at": format_dt(last_used),
            })
    return candidates


def purge_documents(document_ids: list[str]) -> dict[str, int]:
    safe_ids = sorted(set(document_ids))
    if not safe_ids:
        return {"documents": 0, "reviews": 0, "batch_items": 0}
    with SessionLocal.begin() as session:
        review_ids = list(session.scalars(
            select(ReviewTask.id).where(ReviewTask.document_id.in_(safe_ids))
        ))
        batch_result = session.execute(
            delete(ReviewBatchItem).where(
                (ReviewBatchItem.document_id.in_(safe_ids))
                | (ReviewBatchItem.review_id.in_(review_ids))
            )
        )
        if review_ids:
            session.execute(delete(ReviewIssue).where(ReviewIssue.task_id.in_(review_ids)))
            review_result = session.execute(
                delete(ReviewTask).where(ReviewTask.id.in_(review_ids))
            )
        else:
            review_result = None
        document_result = session.execute(
            delete(Document).where(Document.id.in_(safe_ids))
        )
        empty_batches = list(session.scalars(
            select(ReviewBatch.id).where(
                ~ReviewBatch.items.any()
            )
        ))
        if empty_batches:
            session.execute(delete(ReviewBatch).where(ReviewBatch.id.in_(empty_batches)))
        return {
            "documents": int(document_result.rowcount or 0),
            "reviews": int(review_result.rowcount or 0) if review_result is not None else 0,
            "batch_items": int(batch_result.rowcount or 0),
        }


def create_batch(ruleset_id: str, total: int, *, owner_id: str = "") -> dict[str, Any]:
    with SessionLocal.begin() as session:
        model = ReviewBatch(id=new_id(), ruleset_id=ruleset_id, total=total, owner_id=owner_id)
        session.add(model)
        session.flush()
        return batch_dict(model, [])


def add_batch_item(
    batch_id: str,
    *,
    filename: str,
    document_id: str | None,
    review_id: str | None,
    status: str,
    error: str | None = None,
) -> None:
    with SessionLocal.begin() as session:
        session.add(ReviewBatchItem(
            batch_id=batch_id,
            filename=filename,
            document_id=document_id,
            review_id=review_id,
            status=status,
            error_message=error,
        ))


def get_batch(batch_id: str) -> dict[str, Any] | None:
    with SessionLocal.begin() as session:
        model = session.get(ReviewBatch, batch_id)
        if model is None:
            return None
        items = list(model.items)
        done_count = 0
        for item in items:
            if item.review_id:
                review = session.get(ReviewTask, item.review_id)
                if review is not None:
                    item.status = review.status
                    if review.status in TERMINAL_STATUSES:
                        done_count += 1
            elif item.status in TERMINAL_STATUSES:
                done_count += 1
        model.done_count = done_count
        if done_count == model.total:
            model.status = "done"
            model.completed_at = model.completed_at or now()
        session.flush()
        return batch_dict(model, items, session=session)


def review_dict(model: ReviewTask) -> dict[str, Any]:
    data: dict[str, Any] = {
        "review_id": model.id,
        "owner_id": model.owner_id,
        "status": model.status,
        "progress": model.progress,
        "stage": model.stage,
        "current_rule": model.current_rule,
        "rules_completed": model.rules_completed,
        "rules_total": model.rules_total,
        "rule_statuses": model.rule_status_json or {},
        "document_id": model.document_id,
        "ruleset_id": model.ruleset_id,
        "use_ai": model.use_ai,
        "cancel_requested": model.cancel_requested,
        "degraded_checks": model.degraded_checks,
        "created_at": format_dt(model.created_at),
        "started_at": format_dt(model.started_at),
        "last_activity_at": format_dt(model.last_activity_at),
        "completed_at": format_dt(model.completed_at),
        "error": model.error_message or "",
        "total_issues": model.total_issues,
        "errors": model.errors,
        "warnings": model.warnings,
        "infos": model.infos,
        "duration_seconds": model.duration_seconds,
    }
    if model.result_json:
        data.update(model.result_json)
    return data


def batch_dict(
    model: ReviewBatch,
    items: list[ReviewBatchItem],
    *,
    session=None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for item in items:
        review = session.get(ReviewTask, item.review_id) if session and item.review_id else None
        rows.append({
            "filename": item.filename,
            "document_id": item.document_id,
            "review_id": item.review_id,
            "status": review.status if review else item.status,
            "progress": review.progress if review else (100 if item.status in TERMINAL_STATUSES else 0),
            "current_rule": review.current_rule if review else None,
            "errors": review.errors if review else 0,
            "warnings": review.warnings if review else 0,
            "infos": review.infos if review else 0,
            "conclusion": (review.result_json or {}).get("conclusion", "unknown") if review else "unknown",
            "review_time": format_dt(review.completed_at) if review else "",
            "error": (review.error_message or "") if review else (item.error_message or ""),
        })
    return {
        "batch_id": model.id,
        "owner_id": model.owner_id,
        "ruleset_id": model.ruleset_id,
        "status": model.status,
        "done_count": model.done_count,
        "total": model.total,
        "created_at": format_dt(model.created_at),
        "completed_at": format_dt(model.completed_at),
        "files": rows,
    }


def format_dt(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return value.isoformat(timespec="seconds")
