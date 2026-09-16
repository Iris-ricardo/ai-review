"""Local authentication and RBAC helpers for the demo deployment."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update

from app.core.config import get_settings
from app.models import User, UserSession
from app.models.base import SessionLocal


logger = logging.getLogger(__name__)


ROLE_LABELS = {
    "user": "普通用户",
    "rule_maintainer": "规则维护员",
    "rule_reviewer": "规则审核员",
    "admin": "管理员",
    "super_admin": "超级管理员",
}

ROLE_CAPABILITIES = {
    "user": {"dashboard.view", "review.create", "review.view_own"},
    "rule_maintainer": {
        "dashboard.view", "review.create", "review.view_own", "ruleset.view", "ruleset.edit",
        "ruleset.test", "ruleset.submit", "ruleset.delete_draft",
    },
    "rule_reviewer": {
        "dashboard.view", "review.create", "review.view_own", "review.view_all", "ruleset.view",
        "ruleset.test", "ruleset.approve", "ruleset.publish",
    },
    "admin": {
        "dashboard.view", "review.create", "review.view_own", "review.view_all", "review.batch", "ruleset.view",
        "ruleset.disable", "users.manage",
    },
    "super_admin": {
        "dashboard.view", "review.create", "review.view_own", "review.view_all", "review.batch", "ruleset.view",
        "ruleset.edit", "ruleset.test", "ruleset.submit", "ruleset.approve", "ruleset.publish",
        "ruleset.disable", "ruleset.delete_draft", "ruleset.delete",
        "users.manage", "system.manage",
    },
}

DEFAULT_USERS = [
    ("user", "user123456", "普通用户", "user"),
    ("maintainer", "maintainer123456", "规则维护员", "rule_maintainer"),
    ("reviewer", "reviewer123456", "规则审核员", "rule_reviewer"),
    ("admin", "admin123456", "管理员", "admin"),
    ("superadmin", "superadmin123456", "超级管理员", "super_admin"),
]

# When no deployment credential is configured, keep signed sessions usable for
# the lifetime of this process without deriving their key from public metadata.
_EPHEMERAL_AUTH_SECRET = secrets.token_bytes(32)


class AuthError(RuntimeError):
    pass


def _secret() -> bytes:
    settings = get_settings()
    value = settings.AUTH_SECRET or settings.ADMIN_TOKEN or settings.ACCESS_TOKEN
    return value.encode("utf-8") if value else _EPHEMERAL_AUTH_SECRET


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + padding).encode("ascii"))


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000
    ).hex()
    return f"pbkdf2_sha256${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt, expected = stored.split("$", 2)
    except ValueError:
        return False
    if scheme != "pbkdf2_sha256":
        return False
    actual = hash_password(password, salt).split("$", 2)[2]
    return hmac.compare_digest(actual, expected)


def capabilities_for(role: str) -> list[str]:
    return sorted(ROLE_CAPABILITIES.get(role, set()))


# B7b：强制改密期间该用户只剩自助改密能力（其余全部路由按能力点拒绝）
RESTRICTED_CAPABILITIES = ["account.change_password"]


def restricted_capabilities() -> list[str]:
    return list(RESTRICTED_CAPABILITIES)


def user_dict(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "role_label": ROLE_LABELS.get(user.role, user.role),
        "status": user.status,
        "must_change_password": bool(user.must_change_password),
        "capabilities": (
            restricted_capabilities()
            if user.must_change_password
            else capabilities_for(user.role)
        ),
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else "",
        "created_at": user.created_at.isoformat() if user.created_at else "",
    }


def ensure_default_users() -> None:
    """空库时的初始账号（R07：生产默认不创建源码可推导密码账号）。
    仅显式提供 AUTH_BOOTSTRAP_ADMIN_PASSWORD（首登强制改密）或 AUTH_CREATE_DEMO_USERS=true 才建号，
    否则仅告警且**不回落**公开默认口令；日志不打印口令；已有用户时直接返回，不覆盖、不重置。
    """
    settings = get_settings()
    with SessionLocal.begin() as session:
        count = int(session.scalar(select(func.count()).select_from(User)) or 0)
        if count:
            return
        if settings.AUTH_CREATE_DEMO_USERS:
            for username, password, display_name, role in DEFAULT_USERS:
                session.add(User(
                    id=uuid.uuid4().hex[:12],
                    username=username,
                    password_hash=hash_password(password),
                    display_name=display_name,
                    role=role,
                    status="active",
                    must_change_password=bool(
                        getattr(settings, "AUTH_FORCE_DEMO_PASSWORD_CHANGE", False)
                    ),
                ))
            logger.warning(
                "演示模式：空库已创建 %d 个固定口令演示账号（仅供本地演示，"
                "请勿用于正式部署；正式部署应设 AUTH_CREATE_DEMO_USERS=false）",
                len(DEFAULT_USERS),
            )
            return
        bootstrap_password = str(getattr(settings, "AUTH_BOOTSTRAP_ADMIN_PASSWORD", "")).strip()
        if bootstrap_password and bootstrap_password not in {
            "",
            "replace-with-...",
            "your-bootstrap-admin-password",
            "replace-with-a-random-bootstrap-admin-password",
        }:
            username = str(getattr(settings, "AUTH_BOOTSTRAP_ADMIN_USERNAME", "admin")).strip() or "admin"
            session.add(User(
                id=uuid.uuid4().hex[:12],
                username=username,
                password_hash=hash_password(bootstrap_password),
                display_name="初始管理员",
                role="super_admin",
                status="active",
                must_change_password=True,
            ))
            # 只记录账号名，不记录口令
            logger.warning(
                "空库初始化：已创建初始超级管理员 %r（首次登录强制改密）",
                username,
            )
            return
        logger.error(
            "空库且未创建任何账号：AUTH_CREATE_DEMO_USERS=false 且未提供有效的 "
            "AUTH_BOOTSTRAP_ADMIN_PASSWORD。请设置一次性初始管理员口令（首次登录"
            "强制改密），或仅在演示环境显式开启 AUTH_CREATE_DEMO_USERS=true。"
            "系统不会回落到任何公开默认口令。"
        )


def authenticate(username: str, password: str) -> dict[str, Any]:
    with SessionLocal.begin() as session:
        user = session.scalar(select(User).where(User.username == username.strip()))
        if user is None or user.status != "active" or not verify_password(password, user.password_hash):
            raise AuthError("用户名或密码错误")
        user.last_login_at = datetime.now()
        session.flush()
        return user_dict(user)


def create_token(user: dict[str, Any], expires_hours: int = 12, *, session_id: str | None = None) -> str:
    """签发 HMAC token；HTTP 登录必须走 issue_session_token（token 携带服务端会话 id）。
    不带 session_id 时仅用于服务层/测试的纯签名场景，不能通过 current_user 的会话校验。
    """
    payload = {
        "sub": user["id"],
        "username": user["username"],
        "role": user["role"],
        "exp": int((datetime.now() + timedelta(hours=expires_hours)).timestamp()),
    }
    if session_id:
        payload["sid"] = session_id
    body = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _b64(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{signature}"


def issue_session_token(user: dict[str, Any], expires_hours: int = 12) -> str:
    """登录：创建服务端会话并把 session_id 写入 token（B5a）。
    会话行落在 SQLite（user_sessions），logout/改密/停用/降级可撤销且服务重启不丢撤销状态；撤销后旧 token 立即失效。
    """
    sid = secrets.token_urlsafe(24)
    with SessionLocal.begin() as session:
        session.add(UserSession(
            id=sid,
            user_id=user["id"],
            expires_at=datetime.now() + timedelta(hours=expires_hours),
        ))
    return create_token(user, expires_hours=expires_hours, session_id=sid)


def session_is_active(session_id: str | None, user_id: str) -> bool:
    if not session_id:
        return False
    with SessionLocal() as session:
        row = session.get(UserSession, session_id)
        if row is None or row.user_id != user_id or row.revoked_at is not None:
            return False
        if row.expires_at and row.expires_at <= datetime.now():
            return False
        return True


def revoke_session(session_id: str | None) -> bool:
    """撤销当前会话（退出当前设备）。已撤销/不存在返回 False。"""
    if not session_id:
        return False
    with SessionLocal.begin() as session:
        row = session.get(UserSession, session_id)
        if row is not None and row.revoked_at is None:
            row.revoked_at = datetime.now()
            return True
        return False


def revoke_all_sessions(user_id: str) -> int:
    """撤销用户全部会话（改密/停用/降级/退出全部设备）。返回撤销条数。"""
    with SessionLocal.begin() as session:
        result = session.execute(
            update(UserSession)
            .where(
                UserSession.user_id == user_id,
                UserSession.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now())
        )
        return int(result.rowcount or 0)


def decode_token(token: str) -> dict[str, Any]:
    try:
        body, signature = token.split(".", 1)
    except ValueError as exc:
        raise AuthError("登录状态无效") from exc
    expected = _b64(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        raise AuthError("登录状态无效")
    payload = json.loads(_unb64(body))
    if int(payload.get("exp", 0)) < int(datetime.now().timestamp()):
        raise AuthError("登录已过期")
    return payload


def get_user(user_id: str) -> dict[str, Any] | None:
    with SessionLocal() as session:
        user = session.get(User, user_id)
        return user_dict(user) if user and user.status == "active" else None


def list_users() -> list[dict[str, Any]]:
    with SessionLocal() as session:
        users = session.scalars(select(User).order_by(User.username)).all()
        return [user_dict(user) for user in users]


def list_eligible_reviewers() -> list[dict[str, Any]]:
    with SessionLocal() as session:
        users = session.scalars(select(User).where(
            User.status == "active",
            User.role.in_(["rule_reviewer", "super_admin"]),
        ).order_by(User.username)).all()
        return [user_dict(user) for user in users]


def is_eligible_reviewer(username: str) -> bool:
    with SessionLocal() as session:
        return session.scalar(select(User.id).where(
            User.username == username,
            User.status == "active",
            User.role.in_(["rule_reviewer", "super_admin"]),
        )) is not None


def role_catalog() -> list[dict[str, Any]]:
    return [
        {
            "id": role,
            "label": ROLE_LABELS[role],
            "capabilities": capabilities_for(role),
        }
        for role in ROLE_LABELS
    ]


def create_user(data: dict[str, Any], *, actor_role: str) -> dict[str, Any]:
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    display_name = str(data.get("display_name", "")).strip() or username
    role = str(data.get("role", "user"))
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
        raise AuthError("用户名须为 3-32 位字母、数字、点、横线或下划线")
    if len(password) < 8:
        raise AuthError("密码至少需要 8 位")
    if role not in ROLE_LABELS:
        raise AuthError("无效的用户角色")
    if role == "super_admin" and actor_role != "super_admin":
        raise AuthError("只有超级管理员可以创建超级管理员")
    with SessionLocal.begin() as session:
        if session.scalar(select(User).where(User.username == username)):
            raise AuthError("用户名已存在")
        user = User(
            id=uuid.uuid4().hex[:12],
            username=username,
            password_hash=hash_password(password),
            display_name=display_name[:128],
            role=role,
            status="active",
        )
        session.add(user)
        session.flush()
        return user_dict(user)


def update_user(user_id: str, data: dict[str, Any], *, actor: dict[str, Any]) -> dict[str, Any]:
    with SessionLocal.begin() as session:
        user = session.get(User, user_id)
        if user is None:
            raise AuthError("用户不存在")
        if user.role == "super_admin" and actor["role"] != "super_admin":
            raise AuthError("只有超级管理员可以修改超级管理员")
        role = str(data.get("role", user.role))
        status = str(data.get("status", user.status))
        if role not in ROLE_LABELS:
            raise AuthError("无效的用户角色")
        if status not in {"active", "disabled"}:
            raise AuthError("无效的用户状态")
        if role == "super_admin" and actor["role"] != "super_admin":
            raise AuthError("只有超级管理员可以授予超级管理员角色")
        if user.id == actor["id"] and status == "disabled":
            raise AuthError("不能停用当前登录账号")
        removes_active_super_admin = (
            user.role == "super_admin"
            and user.status == "active"
            and (role != "super_admin" or status != "active")
        )
        if removes_active_super_admin:
            active_super_admins = int(session.scalar(
                select(func.count()).select_from(User).where(
                    User.role == "super_admin",
                    User.status == "active",
                )
            ) or 0)
            if active_super_admins <= 1:
                raise AuthError("必须保留至少一个启用的超级管理员")
        user.display_name = str(data.get("display_name", user.display_name)).strip()[:128] or user.username
        role_changed = role != user.role
        user.role = role
        user.status = status
        session.flush()
        result = user_dict(user)
    if role_changed or status == "disabled":
        # 停用/降权即时撤销全部会话：旧 token 立即不可用（B5a）
        revoke_all_sessions(user_id)
    return result


def reset_password(user_id: str, password: str, *, actor: dict[str, Any]) -> None:
    if len(password) < 8:
        raise AuthError("密码至少需要 8 位")
    with SessionLocal.begin() as session:
        user = session.get(User, user_id)
        if user is None:
            raise AuthError("用户不存在")
        if user.role == "super_admin" and actor["role"] != "super_admin":
            raise AuthError("只有超级管理员可以重置超级管理员密码")
        user.password_hash = hash_password(password)
        # B7b：管理员重置的密码视为临时密码，用户下次登录必须自行改密
        user.must_change_password = True
        session.flush()
    # 改密后撤销该用户全部会话：旧 token 立即失效（B5a）
    revoke_all_sessions(user_id)


def change_password(user_id: str, old_password: str, new_password: str) -> None:
    """用户自助改密（B7b）：先验证原密码，再设新密码并清除强制改密标记。"""
    if len(new_password) < 8:
        raise AuthError("新密码至少需要 8 位")
    with SessionLocal.begin() as session:
        user = session.get(User, user_id)
        if user is None:
            raise AuthError("用户不存在")
        if not verify_password(old_password, user.password_hash):
            raise AuthError("原密码错误")
        user.password_hash = hash_password(new_password)
        user.must_change_password = False
        session.flush()
    # 自助改密同样撤销全部会话：其它设备需重新登录（B5a 语义一致）
    revoke_all_sessions(user_id)
