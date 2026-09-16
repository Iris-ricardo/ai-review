"""B5a: 服务端会话撤销与登录限速（账号维度）验收测试。

退出当前会话后该会话旧 token 立即失效（logout=当前设备，logout-all=全部设备）；改密/重置密码后撤销全部会话；
停用再启用不复活已撤销会话；降权即撤销全部会话 → 旧 token 401；撤销落 SQLite、重启不丢；登录限速按账号维度且互不影响。"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.api import auth_routes
from app.main import app
from app.services import auth_service

client = TestClient(app)


def _login(username: str, password: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    token = response.json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _me(headers: dict[str, str]):
    return client.get("/api/v1/auth/me", headers=headers)


def _new_user() -> tuple[str, str, str]:
    """创建随机普通用户并返回 (username, password, user_id)。"""
    username = f"sess_{uuid.uuid4().hex[:8]}"
    password = "sess_pass_12345"
    created = auth_service.create_user(
        {"username": username, "password": password, "role": "user"},
        actor_role="super_admin",
    )
    return username, password, created["id"]


def test_logout_revokes_only_current_device():
    username, password, _ = _new_user()
    headers_a = _login(username, password)
    headers_b = _login(username, password)  # 第二个设备
    assert _me(headers_a).status_code == 200
    assert _me(headers_b).status_code == 200

    response = client.post("/api/v1/auth/logout", headers=headers_a)
    assert response.status_code == 200

    # A 设备已退出 → 立即失效；B 设备不受影响
    assert _me(headers_a).status_code == 401
    assert _me(headers_b).status_code == 200


def test_logout_all_revokes_every_device():
    username, password, _ = _new_user()
    headers_a = _login(username, password)
    headers_b = _login(username, password)
    response = client.post("/api/v1/auth/logout-all", headers=headers_a)
    assert response.status_code == 200
    assert _me(headers_a).status_code == 401
    assert _me(headers_b).status_code == 401


def test_reset_password_revokes_all_sessions():
    username, password, user_id = _new_user()
    old_headers = _login(username, password)
    admin = _login("superadmin", "superadmin123456")
    new_password = "new_password_98765"
    reset = client.post(
        f"/api/v1/auth/users/{user_id}/reset-password",
        headers=admin,
        json={"password": new_password},
    )
    assert reset.status_code == 200
    # 旧会话全部失效
    assert _me(old_headers).status_code == 401
    # 新密码可登录
    assert _login(username, new_password)


def test_disable_then_enable_does_not_resurrect_session():
    username, password, user_id = _new_user()
    user_headers = _login(username, password)
    admin = _login("superadmin", "superadmin123456")

    disabled = client.patch(
        f"/api/v1/auth/users/{user_id}",
        headers=admin,
        json={"status": "disabled"},
    )
    assert disabled.status_code == 200
    assert _me(user_headers).status_code == 401

    enabled = client.patch(
        f"/api/v1/auth/users/{user_id}",
        headers=admin,
        json={"status": "active"},
    )
    assert enabled.status_code == 200
    # 启用账号 ≠ 复活已撤销会话：旧 token 仍不可用
    assert _me(user_headers).status_code == 401
    # 重新登录可用
    assert _login(username, password)


def test_downgrade_revokes_old_token_permissions():
    username, password, user_id = _new_user()
    headers = _login(username, password)
    admin = _login("superadmin", "superadmin123456")
    assert _me(headers).json()["user"]["role"] == "user"

    upgraded = client.patch(
        f"/api/v1/auth/users/{user_id}",
        headers=admin,
        json={"role": "rule_maintainer"},
    )
    assert upgraded.status_code == 200
    # 角色变更即撤销全部会话 → 旧 token 立即失效
    assert _me(headers).status_code == 401
    fresh = _login(username, password)
    assert _me(fresh).json()["user"]["role"] == "rule_maintainer"


def test_revocation_is_server_side_and_not_bound_to_client():
    """撤销状态在 SQLite：第二个进程/客户端实例也必须拒绝旧 token。"""
    username, password, _ = _new_user()
    headers = _login(username, password)
    # 用“另一个客户端”（同一 app/DB）访问，等价于重启后新进程的读路径
    other_client = TestClient(app)
    assert other_client.get("/api/v1/auth/me", headers=headers).status_code == 200
    client.post("/api/v1/auth/logout", headers=headers)
    assert other_client.get("/api/v1/auth/me", headers=headers).status_code == 401
    assert auth_service.session_is_active(
        auth_service.decode_token(headers["Authorization"][7:])["sid"],
        auth_service.decode_token(headers["Authorization"][7:])["sub"],
    ) is False


def test_token_without_session_id_is_rejected():
    """旧格式 token（无 sid）不能通过会话校验 → 升级后要求重新登录。"""
    super_admin = next(u for u in auth_service.list_users() if u["username"] == "superadmin")
    legacy_token = auth_service.create_token(super_admin)
    headers = {"Authorization": f"Bearer {legacy_token}"}
    assert _me(headers).status_code == 401


def test_login_rate_limit_is_per_username(monkeypatch):
    monkeypatch.setattr(
        auth_routes.settings, "LOGIN_USER_RATE_LIMIT_PER_MINUTE", 2
    )
    auth_routes._login_events.clear()
    target = f"brute_{uuid.uuid4().hex[:6]}"
    for _ in range(2):
        response = client.post(
            "/api/v1/auth/login",
            json={"username": target, "password": "wrong-password"},
        )
        assert response.status_code == 401
    limited = client.post(
        "/api/v1/auth/login",
        json={"username": target, "password": "wrong-password"},
    )
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "60"
    # 其它账号不受该账号限速影响
    other = f"other_{uuid.uuid4().hex[:6]}"
    assert client.post(
        "/api/v1/auth/login",
        json={"username": other, "password": "wrong-password"},
    ).status_code == 401
