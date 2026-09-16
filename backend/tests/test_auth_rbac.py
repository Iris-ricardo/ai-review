from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.api import auth_routes, rule_admin_routes
from app.core.config import PROJECT_INSTANCE_FINGERPRINT
from app.main import app
from app.services import auth_service, review_store
from app.services.auth_service import AuthError


def _login(client: TestClient, username: str, password: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200
    token = response.json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _ruleset(ruleset_id: str) -> dict:
    return {
        "ruleset": ruleset_id,
        "name": "RBAC 测试规则集",
        "description": "只用于权限测试",
        "rules": [{
            "id": "R001",
            "type": "page_limit",
            "severity": "warning",
            "description": "页数限制",
            "enabled": True,
            "basis": {"document": "测试依据"},
            "params": {"max_pages": 5},
        }],
    }


def test_session_secret_does_not_fall_back_to_public_fingerprint(monkeypatch):
    settings = auth_service.get_settings()
    monkeypatch.setattr(settings, "AUTH_SECRET", "")
    monkeypatch.setattr(settings, "ADMIN_TOKEN", "")
    monkeypatch.setattr(settings, "ACCESS_TOKEN", "")

    payload = {
        "sub": "forged-super-admin",
        "username": "forged",
        "role": "super_admin",
        "exp": int(time.time()) + 3600,
    }
    body = auth_service._b64(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    )
    fingerprint_signature = auth_service._b64(hmac.new(
        PROJECT_INSTANCE_FINGERPRINT.encode("utf-8"),
        body.encode("ascii"),
        hashlib.sha256,
    ).digest())

    with pytest.raises(AuthError, match="登录状态无效"):
        auth_service.decode_token(f"{body}.{fingerprint_signature}")

    legitimate = auth_service.create_token({
        "id": "ephemeral-user",
        "username": "ephemeral",
        "role": "user",
    })
    assert auth_service.decode_token(legitimate)["sub"] == "ephemeral-user"


def test_last_active_super_admin_cannot_be_demoted_or_disabled(monkeypatch):
    review_store.init_store()
    auth_service.ensure_default_users()
    client = TestClient(app)
    super_admin = next(
        user for user in auth_service.list_users()
        if user["username"] == "superadmin"
    )
    super_admin_headers = _login(client, "superadmin", "superadmin123456")

    demoted = client.patch(
        f"/api/v1/auth/users/{super_admin['id']}",
        headers=super_admin_headers,
        json={"role": "admin"},
    )
    assert demoted.status_code == 422
    assert demoted.json()["detail"] == "必须保留至少一个启用的超级管理员"

    monkeypatch.setattr(auth_routes.settings, "ADMIN_TOKEN", "test-bootstrap-token")
    disabled = client.patch(
        f"/api/v1/auth/users/{super_admin['id']}",
        headers={"X-Admin-Token": "test-bootstrap-token"},
        json={"status": "disabled"},
    )
    assert disabled.status_code == 422
    assert disabled.json()["detail"] == "必须保留至少一个启用的超级管理员"
    unchanged = auth_service.get_user(super_admin["id"])
    assert unchanged is not None
    assert unchanged["role"] == "super_admin"
    assert unchanged["status"] == "active"


def test_login_is_rate_limited_and_rate_state_is_isolated(monkeypatch):
    with main_module._rate_lock:
        main_module._rate_events.clear()
    monkeypatch.setattr(main_module.settings, "RATE_LIMIT_PER_MINUTE", 1)
    client = TestClient(app)
    try:
        first = client.post(
            "/api/v1/auth/login",
            json={"username": "missing", "password": "incorrect"},
        )
        limited = client.post(
            "/api/v1/auth/login",
            json={"username": "missing", "password": "incorrect"},
        )

        assert first.status_code == 401
        assert limited.status_code == 429
        assert limited.headers["Retry-After"] == "60"
        assert limited.json()["detail"] == "请求过于频繁，请稍后再试"
    finally:
        with main_module._rate_lock:
            main_module._rate_events.clear()


def test_local_login_and_ruleset_capabilities(monkeypatch):
    monkeypatch.setattr(auth_routes.settings, "ADMIN_TOKEN", "")
    monkeypatch.setattr(rule_admin_routes.settings, "ADMIN_TOKEN", "")
    review_store.init_store()
    auth_service.ensure_default_users()
    client = TestClient(app)
    ruleset_id = f"rbac_{uuid.uuid4().hex[:8]}"

    user_headers = _login(client, "user", "user123456")
    maintainer_headers = _login(client, "maintainer", "maintainer123456")
    reviewer_headers = _login(client, "reviewer", "reviewer123456")

    assert client.get("/api/v1/auth/me", headers=maintainer_headers).json()["user"]["role"] == "rule_maintainer"
    assert client.get("/api/v1/rule-admin/checkers", headers=user_headers).status_code == 403

    blocked = client.post(
        "/api/v1/rule-admin/rulesets",
        json={"data": _ruleset(f"{ruleset_id}_blocked")},
        headers=reviewer_headers,
    )
    assert blocked.status_code == 403

    created = client.post(
        "/api/v1/rule-admin/rulesets",
        json={"data": _ruleset(ruleset_id)},
        headers=maintainer_headers,
    )
    assert created.status_code == 200

    detail = client.get(
        f"/api/v1/rule-admin/rulesets/{ruleset_id}",
        headers=maintainer_headers,
    )
    assert detail.status_code == 200
    assert detail.json()["audit"][0]["actor"] == "maintainer"


def test_review_documents_are_isolated_by_owner():
    review_store.init_store()
    auth_service.ensure_default_users()
    client = TestClient(app)
    user_headers = _login(client, "user", "user123456")
    maintainer_headers = _login(client, "maintainer", "maintainer123456")
    reviewer_headers = _login(client, "reviewer", "reviewer123456")

    uploaded = client.post(
        "/api/v1/documents",
        headers=user_headers,
        files={"file": ("owner-test.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
    )
    assert uploaded.status_code == 200
    document_id = uploaded.json()["document_id"]
    assert client.get(f"/api/v1/documents/{document_id}", headers=user_headers).status_code == 200
    assert client.get(f"/api/v1/documents/{document_id}", headers=maintainer_headers).status_code == 404
    assert client.get(f"/api/v1/documents/{document_id}", headers=reviewer_headers).status_code == 200


def test_admin_can_manage_users_but_cannot_grant_super_admin():
    review_store.init_store()
    auth_service.ensure_default_users()
    client = TestClient(app)
    admin_headers = _login(client, "admin", "admin123456")
    username = f"member_{uuid.uuid4().hex[:8]}"

    created = client.post(
        "/api/v1/auth/users",
        headers=admin_headers,
        json={
            "username": username,
            "display_name": "RBAC member",
            "password": "member123456",
            "role": "user",
        },
    )
    assert created.status_code == 201
    user_id = created.json()["user"]["id"]
    updated = client.patch(
        f"/api/v1/auth/users/{user_id}",
        headers=admin_headers,
        json={"role": "rule_maintainer", "status": "disabled"},
    )
    assert updated.status_code == 200
    assert updated.json()["user"]["role"] == "rule_maintainer"
    assert updated.json()["user"]["status"] == "disabled"
    assert client.post(
        "/api/v1/auth/users",
        headers=admin_headers,
        json={
            "username": f"super_{uuid.uuid4().hex[:8]}",
            "display_name": "blocked",
            "password": "member123456",
            "role": "super_admin",
        },
    ).status_code == 422


def test_strict_ruleset_four_eyes_and_assigned_reviewer_flow(tmp_path, monkeypatch):
    review_store.init_store()
    auth_service.ensure_default_users()
    client = TestClient(app)
    monkeypatch.setattr(rule_admin_routes.settings, "RULES_DIR", str(tmp_path / "rules"))
    maintainer = _login(client, "maintainer", "maintainer123456")
    reviewer = _login(client, "reviewer", "reviewer123456")
    admin = _login(client, "admin", "admin123456")
    super_admin = _login(client, "superadmin", "superadmin123456")
    ruleset_id = f"four_eyes_{uuid.uuid4().hex[:8]}"

    created = client.post(
        "/api/v1/rule-admin/rulesets",
        json={"data": _ruleset(ruleset_id)},
        headers=maintainer,
    )
    assert created.status_code == 200
    version = created.json()["version"]
    assert version["created_by"] == "maintainer"
    assert client.post(
        f"/api/v1/rule-admin/versions/{version['id']}/submit",
        json={"assigned_reviewer": "reviewer"}, headers=maintainer,
    ).status_code == 422

    from app.services import rule_admin_store
    rule_admin_store.record_test_run(
        ruleset_id, version["id"], "synthetic", version["sha256"],
        {"system_failures": 0, "rules_executed": 1, "issue_count": 0},
    )
    submitted = client.post(
        f"/api/v1/rule-admin/versions/{version['id']}/submit",
        json={"assigned_reviewer": "reviewer", "note": "ready"}, headers=maintainer,
    )
    assert submitted.status_code == 200
    submitted_version = submitted.json()["version"]
    assert submitted_version["submitted_by"] == "maintainer"
    assert submitted_version["assigned_reviewer"] == "reviewer"

    assert client.post(
        f"/api/v1/rule-admin/versions/{version['id']}/approve",
        json={}, headers=maintainer,
    ).status_code == 403
    assert client.post(
        f"/api/v1/rule-admin/versions/{version['id']}/approve",
        json={}, headers=super_admin,
    ).status_code == 422
    approved = client.post(
        f"/api/v1/rule-admin/versions/{version['id']}/approve",
        json={"note": "checked"}, headers=reviewer,
    )
    assert approved.status_code == 200
    assert approved.json()["version"]["state"] == "approved"
    assert approved.json()["version"]["approved_by"] == "reviewer"

    assert client.post(
        f"/api/v1/rule-admin/versions/{version['id']}/publish",
        json={}, headers=admin,
    ).status_code == 403
    published = client.post(
        f"/api/v1/rule-admin/versions/{version['id']}/publish",
        json={}, headers=reviewer,
    )
    assert published.status_code == 200
    assert published.json()["version"]["published_by"] == "reviewer"


def test_rejection_requires_reason_and_returns_version_to_maintainer():
    review_store.init_store()
    auth_service.ensure_default_users()
    client = TestClient(app)
    maintainer = _login(client, "maintainer", "maintainer123456")
    reviewer = _login(client, "reviewer", "reviewer123456")
    ruleset_id = f"reject_flow_{uuid.uuid4().hex[:8]}"
    version = client.post(
        "/api/v1/rule-admin/rulesets",
        json={"data": _ruleset(ruleset_id)}, headers=maintainer,
    ).json()["version"]
    from app.services import rule_admin_store
    rule_admin_store.record_test_run(
        ruleset_id, version["id"], "synthetic", version["sha256"],
        {"system_failures": 0, "rules_executed": 1, "issue_count": 0},
    )
    client.post(
        f"/api/v1/rule-admin/versions/{version['id']}/submit",
        json={"assigned_reviewer": "reviewer"}, headers=maintainer,
    )
    assert client.post(
        f"/api/v1/rule-admin/versions/{version['id']}/reject",
        json={}, headers=reviewer,
    ).status_code == 422
    rejected = client.post(
        f"/api/v1/rule-admin/versions/{version['id']}/reject",
        json={"note": "请补充政策依据"}, headers=reviewer,
    )
    assert rejected.status_code == 200
    assert rejected.json()["version"]["state"] == "rejected"
    assert rejected.json()["version"]["rejection_reason"] == "请补充政策依据"
    saved = client.put(
        f"/api/v1/rule-admin/versions/{version['id']}",
        json={"data": _ruleset(ruleset_id), "revision": rejected.json()["version"]["revision"]},
        headers=maintainer,
    )
    assert saved.status_code == 200
    assert saved.json()["version"]["state"] == "draft"
