"""B7b: 强制改密门禁 + 自助改密（must_change_password）。

验收语义：管理员重置密码后 must_change=true，重新登录可用但能力受限（只留 account.change_password），审查等功能 403；
自助改密须验证原密码，成功后清标记并撤销全部会话需重新登录；普通用户可自愿改密（同样验证原密码并撤销其它会话）。"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import auth_service


client = TestClient(app)


def _login(username: str, password: str) -> dict:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    return {
        "headers": {"Authorization": f"Bearer {data['token']}"},
        "user": data["user"],
    }


def _create_user() -> tuple[str, str, str]:
    username = f"must_{uuid.uuid4().hex[:8]}"
    password = "initial_pass_123"
    user = auth_service.create_user(
        {"username": username, "password": password, "role": "user"},
        actor_role="super_admin",
    )
    return username, password, user["id"]


def test_admin_reset_forces_change_and_restricts_capabilities():
    username, password, user_id = _create_user()
    admin = _login("superadmin", "superadmin123456")

    reset = client.post(
        f"/api/v1/auth/users/{user_id}/reset-password",
        headers=admin["headers"],
        json={"password": "temporary_abc123"},
    )
    assert reset.status_code == 200

    # 重新登录：token 有效，但能力受限（必须改密）
    fresh = _login(username, "temporary_abc123")
    assert fresh["user"]["must_change_password"] is True
    assert fresh["user"]["capabilities"] == ["account.change_password"]

    # 受限会话不能创建审查（review.create 不在能力内）
    upload = client.post(
        "/api/v1/documents",
        headers=fresh["headers"],
        files={"file": ("a.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
    )
    assert upload.status_code == 403

    # 自助改密：原密码错误 → 422
    wrong = client.post(
        "/api/v1/auth/change-password",
        headers=fresh["headers"],
        json={"old_password": "not-the-password", "new_password": "brand_new_456"},
    )
    assert wrong.status_code == 422
    assert "原密码" in wrong.json()["detail"]

    # 自助改密成功 → 撤销全部会话 → 旧受限 token 立即失效
    changed = client.post(
        "/api/v1/auth/change-password",
        headers=fresh["headers"],
        json={"old_password": "temporary_abc123", "new_password": "brand_new_456"},
    )
    assert changed.status_code == 200
    assert client.get("/api/v1/auth/me", headers=fresh["headers"]).status_code == 401

    # 新密码登录：标记清除、能力恢复完整
    relogin = _login(username, "brand_new_456")
    assert relogin["user"]["must_change_password"] is False
    assert "review.create" in relogin["user"]["capabilities"]
    # 旧密码不可再用
    assert client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "temporary_abc123"},
    ).status_code == 401


def test_voluntary_change_password_verifies_old_password():
    username, password, user_id = _create_user()
    session = _login(username, password)
    assert session["user"]["must_change_password"] is False

    # 原密码错误不能改
    assert client.post(
        "/api/v1/auth/change-password",
        headers=session["headers"],
        json={"old_password": "wrong-old", "new_password": "voluntary_456"},
    ).status_code == 422

    ok = client.post(
        "/api/v1/auth/change-password",
        headers=session["headers"],
        json={"old_password": password, "new_password": "voluntary_456"},
    )
    assert ok.status_code == 200
    assert _login(username, "voluntary_456")["headers"]
    assert client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    ).status_code == 401


def test_me_exposes_must_change_flag_for_forced_user():
    username, password, user_id = _create_user()
    admin = _login("superadmin", "superadmin123456")
    client.post(
        f"/api/v1/auth/users/{user_id}/reset-password",
        headers=admin["headers"],
        json={"password": "temp_pass_12345"},
    )
    fresh = _login(username, "temp_pass_12345")
    me = client.get("/api/v1/auth/me", headers=fresh["headers"]).json()["user"]
    assert me["must_change_password"] is True
    assert me["capabilities"] == ["account.change_password"]
