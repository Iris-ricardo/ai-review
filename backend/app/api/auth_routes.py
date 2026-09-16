"""Authentication API and RBAC dependencies."""
from __future__ import annotations

import secrets
import threading
import time
from collections import defaultdict, deque
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.services import auth_service
from app.services.auth_service import AuthError


router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
settings = get_settings()

# 登录限速（账号维度）：滑动窗口，仅防同一账号被无限尝试；不落库、不锁定账号，
# 避免“简单锁定被滥用为拒绝服务”。IP+路径的全局限速仍在 main.py 中间件。
_login_lock = threading.Lock()
_login_events: dict[str, deque[float]] = defaultdict(deque)


def _login_allowed(username: str) -> bool:
    limit = max(1, int(settings.LOGIN_USER_RATE_LIMIT_PER_MINUTE))
    key = username.strip().lower()
    now_value = time.monotonic()
    with _login_lock:
        events = _login_events[key]
        while events and events[0] < now_value - 60:
            events.popleft()
        # 防批量用户名爆破导致的字典无限增长：超阈值时修剪陈旧键（B8）
        if len(_login_events) > 10000:
            stale_cutoff = now_value - 60
            stale = [
                candidate for candidate, deque_events in _login_events.items()
                if not deque_events or deque_events[0] < stale_cutoff
            ]
            for candidate in stale:
                del _login_events[candidate]
        if len(events) >= limit:
            return False
        events.append(now_value)
    return True


class LoginRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


def _bearer_token(authorization: str | None) -> str:
    value = authorization or ""
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return ""


def current_user(
    request: Request,
    authorization: str | None = Header(default=None),
    x_admin_token: str | None = Header(default=None),
) -> dict[str, Any]:
    token = _bearer_token(authorization)
    if token:
        try:
            payload = auth_service.decode_token(token)
            user = auth_service.get_user(str(payload.get("sub", "")))
            if user is None:
                raise AuthError("用户不存在或已停用")
            # B5a：登录签发的 token 必须携带服务端会话 id 且会话未被撤销。
            # 旧格式 token（无 sid）与已退出/被撤销的会话一律要求重新登录。
            if not auth_service.session_is_active(payload.get("sid"), user["id"]):
                raise AuthError("会话已失效，请重新登录")
            return {**user, "auth_method": "session"}
        except AuthError as exc:
            raise HTTPException(401, str(exc)) from exc
    if settings.ADMIN_TOKEN and x_admin_token and secrets.compare_digest(x_admin_token, settings.ADMIN_TOKEN):
        # 兼容部署凭据：显式标注 auth_method，便于管理守卫区分"部署凭据"与
        # "普通用户会话"，避免把 X-Admin-Token 伪装成用户身份（R06）。
        return {
            "id": "bootstrap-admin",
            "username": "bootstrap-admin",
            "display_name": "兼容管理员",
            "role": "super_admin",
            "role_label": "超级管理员",
            "status": "active",
            "must_change_password": False,
            "auth_method": "admin_token",
            "capabilities": auth_service.capabilities_for("super_admin"),
            "last_login_at": "",
            "created_at": "",
        }
    raise HTTPException(401, "需要登录")


def require_capability(capability: str):
    def dependency(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        if capability not in set(user.get("capabilities", [])):
            raise HTTPException(403, f"当前角色缺少权限：{capability}")
        return user
    return dependency


def local_or_current_user(
    request: Request,
    authorization: str | None = Header(default=None),
    x_admin_token: str | None = Header(default=None),
) -> dict[str, Any]:
    try:
        return current_user(request, authorization, x_admin_token)
    except HTTPException:
        if settings.AUTH_REQUIRED or settings.ADMIN_TOKEN:
            raise
        return {
            "id": "local-dev",
            "username": "local-dev",
            "display_name": "本地兼容用户",
            "role": "super_admin",
            "role_label": "超级管理员",
            "status": "active",
            "capabilities": auth_service.capabilities_for("super_admin"),
            "last_login_at": "",
            "created_at": "",
        }


def require_review_capability(capability: str):
    def dependency(user: dict[str, Any] = Depends(local_or_current_user)) -> dict[str, Any]:
        if capability not in set(user.get("capabilities", [])):
            raise HTTPException(403, f"当前角色缺少权限：{capability}")
        return user
    return dependency


@router.post("/login")
def login(payload: LoginRequest):
    # B5a：账号维度限速（窗口滑动、不锁定），与 main.py 的 IP+路径全局限速叠加
    if not _login_allowed(payload.username):
        raise HTTPException(
            429,
            "该账号登录尝试过于频繁，请稍后再试",
            headers={"Retry-After": "60"},
        )
    try:
        user = auth_service.authenticate(
            payload.username,
            payload.password,
        )
    except AuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    return {"token": auth_service.issue_session_token(user), "user": user}


@router.post("/logout")
def logout(authorization: str | None = Header(default=None)):
    """退出当前设备：撤销本 token 对应的服务端会话（B5a）。"""
    token = _bearer_token(authorization)
    if token:
        try:
            payload = auth_service.decode_token(token)
        except AuthError:
            payload = {}
        auth_service.revoke_session(payload.get("sid"))
    return {"ok": True}


@router.post("/logout-all")
def logout_all(user: dict[str, Any] = Depends(current_user)):
    """退出全部设备：撤销当前用户的所有会话（B5a）。"""
    auth_service.revoke_all_sessions(user["id"])
    return {"ok": True, "revoked": True}


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8, max_length=128)


@router.post("/change-password")
def change_own_password(
    payload: ChangePasswordRequest,
    user: dict[str, Any] = Depends(current_user),
):
    """自助改密（B7b）：验证原密码 → 设新密码 → 清除强制改密标记 → 撤销其它会话。"""
    try:
        auth_service.change_password(
            user["id"], payload.old_password, payload.new_password
        )
    except AuthError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"ok": True}


@router.get("/me")
def me(user: dict[str, Any] = Depends(current_user)):
    return {"user": user}


@router.get("/users")
def users(_: dict[str, Any] = Depends(require_capability("users.manage"))):
    return {"users": auth_service.list_users()}


@router.get("/roles")
def roles(_: dict[str, Any] = Depends(current_user)):
    return {"roles": auth_service.role_catalog()}


@router.get("/reviewers")
def reviewers(_: dict[str, Any] = Depends(require_capability("ruleset.submit"))):
    return {"reviewers": auth_service.list_eligible_reviewers()}


@router.post("/users", status_code=201)
def add_user(
    payload: dict[str, Any] = Body(...),
    actor: dict[str, Any] = Depends(require_capability("users.manage")),
):
    try:
        return {"user": auth_service.create_user(payload, actor_role=actor["role"])}
    except AuthError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.patch("/users/{user_id}")
def edit_user(
    user_id: str,
    payload: dict[str, Any] = Body(...),
    actor: dict[str, Any] = Depends(require_capability("users.manage")),
):
    try:
        return {"user": auth_service.update_user(user_id, payload, actor=actor)}
    except AuthError as exc:
        status = 404 if str(exc) == "用户不存在" else 422
        raise HTTPException(status, str(exc)) from exc


@router.post("/users/{user_id}/reset-password")
def change_user_password(
    user_id: str,
    payload: dict[str, Any] = Body(...),
    actor: dict[str, Any] = Depends(require_capability("users.manage")),
):
    try:
        auth_service.reset_password(user_id, str(payload.get("password", "")), actor=actor)
        return {"ok": True}
    except AuthError as exc:
        status = 404 if str(exc) == "用户不存在" else 422
        raise HTTPException(status, str(exc)) from exc
