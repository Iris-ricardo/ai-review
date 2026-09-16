"""Guard contract for the four legacy admin endpoints (C3 fix, batch B2).
Fail closed: anonymous → 401, normal user → 403, empty ADMIN_TOKEN still needs super-admin session.
Super-admin session or valid X-Admin-Token → allowed; bad token → 401. Guarded: PUT /rulesets/{id},
POST /rulesets/from-guideline, /maintenance/cleanup (dry_run, deletes nothing), /system/llm-check."""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.api import routes
from app.main import app


def _login(client: TestClient, username: str, password: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _admin_headers(client: TestClient) -> dict[str, str]:
    return _login(client, "superadmin", "superadmin123456")


def _user_headers(client: TestClient) -> dict[str, str]:
    return _login(client, "user", "user123456")


@pytest.fixture(autouse=True)
def _tmp_rules_dir(monkeypatch, tmp_path):
    """把规则目录指到临时目录，避免守卫测试写入真实 rules/ 或管理库。"""
    rules_dir = tmp_path / "rules"
    monkeypatch.setattr(routes.settings, "RULES_DIR", str(rules_dir))
    return rules_dir


def _ruleset_yaml(ruleset_id: str) -> str:
    return "\n".join([
        f"ruleset: {ruleset_id}",
        "name: GuardTest",
        "rules:",
        "  - id: G001",
        "    type: page_limit",
        "    severity: warning",
        "    params:",
        "      max_pages: 5",
    ])


def _probe_put_ruleset(client: TestClient, headers, ruleset_id: str | None = None):
    ruleset_id = ruleset_id or f"guard_{uuid.uuid4().hex[:8]}"
    return client.put(
        f"/api/v1/rulesets/{ruleset_id}",
        content=_ruleset_yaml(ruleset_id),
        headers={"content-type": "text/plain", **(headers or {})},
    )


# ── PUT /rulesets/{id} ──────────────────────────────────────

def test_put_ruleset_rejects_anonymous_when_admin_token_empty():
    """ADMIN_TOKEN 为空也不能放行匿名写规则集（C3 核心回归）。"""
    client = TestClient(app)
    response = _probe_put_ruleset(client, None)
    assert response.status_code == 401


def test_put_ruleset_rejects_ordinary_user_session():
    client = TestClient(app)
    response = _probe_put_ruleset(client, _user_headers(client))
    assert response.status_code == 403


def test_put_ruleset_allows_super_admin_session_when_admin_token_empty():
    """ADMIN_TOKEN 为空时超级管理员会话仍可写（fail closed 而非全员禁止）。"""
    client = TestClient(app)
    response = _probe_put_ruleset(client, _admin_headers(client))
    assert response.status_code == 200


def test_put_ruleset_allows_valid_admin_token(monkeypatch):
    monkeypatch.setattr(routes.settings, "ADMIN_TOKEN", "guard-secret")
    client = TestClient(app)
    response = _probe_put_ruleset(
        client, {"X-Admin-Token": "guard-secret"}
    )
    assert response.status_code == 200


def test_put_ruleset_rejects_invalid_admin_token(monkeypatch):
    monkeypatch.setattr(routes.settings, "ADMIN_TOKEN", "guard-secret")
    client = TestClient(app)
    response = _probe_put_ruleset(
        client, {"X-Admin-Token": "wrong-secret"}
    )
    assert response.status_code == 401


# ── POST /maintenance/cleanup（仅 dry_run，不删除数据） ─────

def test_cleanup_rejects_anonymous():
    client = TestClient(app)
    response = client.post("/api/v1/maintenance/cleanup?days=30")
    assert response.status_code == 401


def test_cleanup_rejects_ordinary_user_session():
    client = TestClient(app)
    response = client.post(
        "/api/v1/maintenance/cleanup?days=30",
        headers=_user_headers(client),
    )
    assert response.status_code == 403


def test_cleanup_allows_super_admin_session_dry_run():
    client = TestClient(app)
    response = client.post(
        "/api/v1/maintenance/cleanup?days=30",
        headers=_admin_headers(client),
    )
    assert response.status_code == 200
    assert response.json()["dry_run"] is True


# ── POST /system/llm-check（无 LLM_API_KEY 时守卫通过后应 503） ──

def test_llm_check_rejects_anonymous():
    client = TestClient(app)
    response = client.post("/api/v1/system/llm-check")
    assert response.status_code == 401


def test_llm_check_rejects_ordinary_user_session():
    client = TestClient(app)
    response = client.post(
        "/api/v1/system/llm-check",
        headers=_user_headers(client),
    )
    assert response.status_code == 403


def test_llm_check_guard_passes_for_super_admin_without_key():
    """守卫通过后因未配置 LLM_API_KEY 返回 503——证明 401/403 守卫已放行。"""
    client = TestClient(app)
    response = client.post(
        "/api/v1/system/llm-check",
        headers=_admin_headers(client),
    )
    assert response.status_code == 503


# ── POST /rulesets/from-guideline ────────────────────────────

def test_from_guideline_rejects_anonymous_before_consent():
    client = TestClient(app)
    response = client.post(
        "/api/v1/rulesets/from-guideline",
        files={"file": ("guideline.pdf", b"%PDF-1.4\n", "application/pdf")},
    )
    assert response.status_code == 401


def test_from_guideline_allows_super_admin_with_mocked_extractor(monkeypatch):
    # R08：部署级外发开关默认关闭，本用例显式开启以验证“放行路径”
    monkeypatch.setattr(routes.settings, "AI_EGRESS_ENABLED", True)
    client = TestClient(app)

    def extract(path):
        return "ruleset: generated_v1\nrules: []\n", []

    monkeypatch.setattr(routes, "extract_draft_from_pdf", extract)
    response = client.post(
        "/api/v1/rulesets/from-guideline",
        headers=_admin_headers(client),
        data={"privacy_consent": "true"},
        files={"file": ("guideline.pdf", b"%PDF-1.4\n", "application/pdf")},
    )
    assert response.status_code == 200
