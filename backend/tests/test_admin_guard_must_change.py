"""R06 回归测试：强制改密期间的会话不得绕过管理/业务写接口（HTTP 层）。

原缺陷：``_require_admin`` 只看 ``role == "super_admin"``，强制改密的超级管理员（capabilities
只剩 account.change_password）仍能改规则集、生成指南、做模型检查、执行数据清理。"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import User
from app.models.base import SessionLocal
from app.services import auth_service, review_store

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


@pytest.fixture
def restricted_super_admin() -> dict:
    """一个 role=super_admin、但被强制改密的会话（能力只剩自助改密）。"""
    username = f"restricted_{uuid.uuid4().hex[:8]}"
    user = auth_service.create_user(
        {"username": username, "password": "initial_pass_123", "role": "super_admin"},
        actor_role="super_admin",
    )
    admin = _login("superadmin", "superadmin123456")
    reset = client.post(
        f"/api/v1/auth/users/{user['id']}/reset-password",
        headers=admin["headers"],
        json={"password": "temporary_abc123"},
    )
    assert reset.status_code == 200, reset.text
    session = _login(username, "temporary_abc123")
    assert session["user"]["must_change_password"] is True
    assert session["user"]["capabilities"] == ["account.change_password"]
    try:
        yield session
    finally:
        # 测试隔离：本用例临时创建的超级管理员必须停用，否则会改变
        # “最后一个启用的超级管理员”等既有用例的前提（共享会话数据库）。
        with SessionLocal.begin() as db:
            row = db.get(User, user["id"])
            if row is not None:
                row.status = "disabled"
                row.role = "admin"


DANGEROUS_CALLS = [
    ("PUT", "/api/v1/rulesets/campus_general_v1", {"content": "ruleset: x\nrules: []\n"}),
    ("POST", "/api/v1/system/llm-check", None),
    ("POST", "/api/v1/maintenance/cleanup?dry_run=true", None),
    ("POST", "/api/v1/rule-admin/rulesets", {"id": "probe", "name": "probe"}),
]


@pytest.mark.parametrize("method,path,payload", DANGEROUS_CALLS)
def test_restricted_super_admin_blocked_on_admin_endpoints(
    restricted_super_admin, method, path, payload
):
    headers = dict(restricted_super_admin["headers"])
    if method == "PUT":
        headers["Content-Type"] = "text/plain"
        response = client.put(path, headers=headers, content=payload["content"])
    elif payload is None:
        response = client.post(path, headers=headers)
    else:
        response = client.post(path, headers=headers, json=payload)
    assert response.status_code == 403, response.text


def test_restricted_super_admin_blocked_on_upload_and_review(restricted_super_admin):
    headers = restricted_super_admin["headers"]
    upload = client.post(
        "/api/v1/documents",
        headers=headers,
        files={"file": ("x.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
    )
    assert upload.status_code == 403
    created = client.post(
        "/api/v1/reviews",
        headers=headers,
        data={"document_id": "whatever", "ruleset_id": "campus", "use_ai": "false"},
    )
    assert created.status_code == 403
    batch = client.post(
        "/api/v1/batches",
        headers=headers,
        data={"ruleset_id": "campus", "use_ai": "false"},
        files=[("files", ("x.pdf", b"%PDF-1.4\n%%EOF", "application/pdf"))],
    )
    assert batch.status_code == 403


def test_restricted_super_admin_blocked_on_cancel(restricted_super_admin):
    """取消是写操作：受限会话不得通过 local_or_current_user 的宽松依赖绕过。"""
    owner = restricted_super_admin["user"]["id"]
    document_id = uuid.uuid4().hex[:12]
    review_store.create_document({
        "id": document_id,
        "filename": "cancel-sample.pdf",
        "file_path": "cancel-sample.pdf",
        "file_type": "pdf",
        "size": 14,
        "mime_type": "application/pdf",
        "sha256": uuid.uuid4().hex,
        "owner_id": owner,
    })
    queued = review_store.create_review(
        document_id,
        "campus_general_v1",
        ruleset_snapshot="ruleset: campus_general_v1\nname: t\nrules: []\n",
        idempotency_key=uuid.uuid4().hex,
        use_ai=False,
        owner_id=owner,
    )
    response = client.post(
        f"/api/v1/reviews/{queued['review_id']}/cancel",
        headers=restricted_super_admin["headers"],
    )
    assert response.status_code == 403, response.text


def test_restricted_super_admin_can_still_change_password_and_logout(
    restricted_super_admin,
):
    headers = restricted_super_admin["headers"]
    me = client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["user"]["must_change_password"] is True

    changed = client.post(
        "/api/v1/auth/change-password",
        headers=headers,
        json={"old_password": "temporary_abc123", "new_password": "brand_new_pass_456"},
    )
    assert changed.status_code == 200, changed.text

    logged_out = client.post("/api/v1/auth/logout", headers=headers)
    assert logged_out.status_code == 200


def test_cleanup_dry_run_not_executed_by_restricted_session(restricted_super_admin):
    """危险操作必须“未被执行”，而不仅仅是返回 403。"""
    before = review_store.retention_candidates(30)
    response = client.post(
        "/api/v1/maintenance/cleanup?dry_run=false",
        headers=restricted_super_admin["headers"],
    )
    assert response.status_code == 403
    after = review_store.retention_candidates(30)
    assert before == after


def test_normal_super_admin_still_passes_admin_guard():
    """未受限的超级管理员不受影响（回归护栏）。"""
    admin = _login("superadmin", "superadmin123456")
    assert admin["user"]["must_change_password"] is False
    response = client.post(
        "/api/v1/maintenance/cleanup?dry_run=true",
        headers=admin["headers"],
    )
    assert response.status_code == 200, response.text
