"""
Core ORM models: Document, ReviewTask, ReviewIssue.
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


def _gen_uuid() -> str:
    return uuid.uuid4().hex[:12]


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_gen_uuid)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    original_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    file_type: Mapped[str] = mapped_column(String(10), nullable=False)  # pdf / docx
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    mime_type: Mapped[str] = mapped_column(String(128), default="")
    sha256: Mapped[str] = mapped_column(String(64), default="", index=True)
    owner_id: Mapped[str] = mapped_column(String(32), nullable=False, default="", index=True)
    converted_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    ir_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    tasks: Mapped[list["ReviewTask"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class ReviewTask(Base):
    __tablename__ = "review_tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_gen_uuid)
    document_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("documents.id"), nullable=False
    )
    ruleset_id: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(32), nullable=False, default="", index=True)
    status: Mapped[str] = mapped_column(
        String(20), default="queued", index=True
    )  # queued | running | done | failed | cancelled | timed_out
    progress: Mapped[int] = mapped_column(Integer, default=0)  # 0-100
    stage: Mapped[str] = mapped_column(String(64), default="queued")
    current_rule: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    rules_completed: Mapped[int] = mapped_column(Integer, default=0)
    rules_total: Mapped[int] = mapped_column(Integer, default=0)
    rule_status_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    ruleset_snapshot: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), default="", index=True)
    use_ai: Mapped[bool] = mapped_column(Boolean, default=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    degraded_checks: Mapped[int] = mapped_column(Integer, default=0)
    result_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    total_issues: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    warnings: Mapped[int] = mapped_column(Integer, default=0)
    infos: Mapped[int] = mapped_column(Integer, default=0)
    duration_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_activity_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    document: Mapped["Document"] = relationship(back_populates="tasks")
    issues: Mapped[list["ReviewIssue"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )


class ReviewIssue(Base):
    __tablename__ = "review_issues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("review_tasks.id"), nullable=False
    )
    rule_id: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(10), nullable=False)  # error|warning|info
    page: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    block_id: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    evidence: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    suggestion: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    confidence: Mapped[str] = mapped_column(
        String(20), default="deterministic"
    )  # deterministic | ai
    layer: Mapped[str] = mapped_column(
        String(20), default="rule"
    )  # rule | semantic
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    task: Mapped["ReviewTask"] = relationship(back_populates="issues")


class ReviewBatch(Base):
    __tablename__ = "review_batches"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_gen_uuid)
    ruleset_id: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(32), nullable=False, default="", index=True)
    status: Mapped[str] = mapped_column(String(20), default="running")
    total: Mapped[int] = mapped_column(Integer, default=0)
    done_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    items: Mapped[list["ReviewBatchItem"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )


class ReviewBatchItem(Base):
    __tablename__ = "review_batch_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("review_batches.id"), nullable=False, index=True
    )
    document_id: Mapped[Optional[str]] = mapped_column(
        String(32), ForeignKey("documents.id"), nullable=True
    )
    review_id: Mapped[Optional[str]] = mapped_column(
        String(32), ForeignKey("review_tasks.id"), nullable=True
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    batch: Mapped["ReviewBatch"] = relationship(back_populates="items")


class ManagedRuleSet(Base):
    """Stable identity and lifecycle state for an administrator-managed ruleset."""

    __tablename__ = "managed_rulesets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    active_version_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class RuleSetVersion(Base):
    """A mutable draft or immutable submitted/published ruleset version."""

    __tablename__ = "ruleset_versions"
    __table_args__ = (UniqueConstraint("ruleset_id", "version_number"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_gen_uuid)
    ruleset_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("managed_rulesets.id"), nullable=False, index=True
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    yaml_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    change_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    validation_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="bootstrap-admin")
    assigned_reviewer: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    submitted_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    approved_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    approval_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rejected_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    rejected_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    rejection_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    published_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class RuleAdminAudit(Base):
    __tablename__ = "rule_admin_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ruleset_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False, default="bootstrap-admin")
    detail_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class RuleTestRun(Base):
    __tablename__ = "rule_test_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_gen_uuid)
    ruleset_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    document_id: Mapped[str] = mapped_column(String(32), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="done")
    result_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class EgressConsent(Base):
    """AI 材料外发授权/策略审计（B5b）：记录授权主体、用途、授权与否、策略版本与时间，不含密钥与正文。

    单次调用级发送范围/字符数仍在 llm client 日志（未落库，剩余差距见交付报告）。
    """

    __tablename__ = "egress_consents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    actor_id: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    actor_name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    consent_granted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ai_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class User(Base):
    """Local user account for the demonstration RBAC layer."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_gen_uuid)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="user")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    # B7b：首次登录/管理员重置后必须改密；为 True 时该用户的会话能力受限，
    # 仅允许自助改密相关操作，直到改密完成。
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    sessions: Mapped[list["UserSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class UserSession(Base):
    """服务端会话（B5a）：token 携带 session_id，撤销即删除/标记，重启不丢失。
    logout=撤销当前会话（退出当前设备），logout-all=撤销全部；改密/停用/降级时撤销该用户全部会话。
    """

    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id"), nullable=False, index=True
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="sessions")
