"""B5b: AI 外发部署级急停（C12）与授权审计落库（C11）验收测试。

未授权/AI 关闭/禁止外发/紧急停用时，外部模型请求次数必须为 0；consent 记录主体、用途、
时间、策略版本（不记录密钥/正文）。"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.api import routes
from app.main import app
from app.services.llm import client as llm_client_module
from app.services.llm.client import LLMUnavailableError
from app.services.llm.client import call_json as real_call_json
from app.services.review_store import record_egress_consent  # noqa: F401 (table ref)


def _session_client():
    client = TestClient(app)
    login = client.post(
        "/api/v1/auth/login",
        json={"username": "superadmin", "password": "superadmin123456"},
    )
    assert login.status_code == 200
    return client, {"Authorization": f"Bearer {login.json()['token']}"}


def _upload_pdf(client, headers, name="egress.pdf") -> str:
    response = client.post(
        "/api/v1/documents",
        headers=headers,
        files={"file": (name, b"%PDF-1.4\n%%EOF", "application/pdf")},
    )
    assert response.status_code == 200, response.text
    return response.json()["document_id"]


def test_call_json_gate_blocks_before_any_transport_when_egress_disabled(monkeypatch):
    """统一出口策略：禁用时连发送函数都不会被调用（mock 捕获请求次数 = 0）。"""
    monkeypatch.setattr(routes.settings, "AI_EGRESS_ENABLED", False)
    monkeypatch.setattr(routes.settings, "LLM_API_KEY", "sk-fake")

    calls: list = []

    def recorder(*args, **kwargs):
        calls.append(args)
        return "{}"

    monkeypatch.setattr(llm_client_module, "_post_chat", recorder)
    with pytest.raises(LLMUnavailableError, match="禁用"):
        real_call_json("system", "user")
    assert calls == []


def test_create_review_rejected_when_egress_disabled(monkeypatch):
    monkeypatch.setattr(routes.settings, "AI_EGRESS_ENABLED", False)
    client, headers = _session_client()
    doc_id = _upload_pdf(client, headers)
    response = client.post(
        "/api/v1/reviews",
        data={
            "document_id": doc_id,
            "ruleset_id": "campus_general_v1",
            "use_ai": "true",
            "privacy_consent": "true",
        },
        headers=headers,
    )
    assert response.status_code == 422
    assert "禁用" in response.json()["detail"]


def test_batch_rejected_when_egress_disabled(monkeypatch):
    monkeypatch.setattr(routes.settings, "AI_EGRESS_ENABLED", False)
    client, headers = _session_client()
    response = client.post(
        "/api/v1/batches",
        data={
            "ruleset_id": "campus_general_v1",
            "use_ai": "true",
            "privacy_consent": "true",
        },
        files=[
            ("files", ("a.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")),
            ("files", ("b.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")),
        ],
        headers=headers,
    )
    assert response.status_code == 422
    assert "禁用" in response.json()["detail"]


def test_guideline_rejected_when_egress_disabled(monkeypatch):
    monkeypatch.setattr(routes.settings, "AI_EGRESS_ENABLED", False)
    monkeypatch.setattr(routes.settings, "ADMIN_TOKEN", "egress-guard-secret")
    client = TestClient(app)
    response = client.post(
        "/api/v1/rulesets/from-guideline",
        headers={"X-Admin-Token": "egress-guard-secret"},
        data={"privacy_consent": "true"},
        files={"file": ("guideline.pdf", b"%PDF-1.4\n", "application/pdf")},
    )
    assert response.status_code == 422
    assert "禁用" in response.json()["detail"]


def test_llm_check_reports_egress_disabled(monkeypatch):
    monkeypatch.setattr(routes.settings, "AI_EGRESS_ENABLED", False)
    monkeypatch.setattr(routes.settings, "LLM_API_KEY", "sk-fake")
    client, headers = _session_client()
    response = client.post("/api/v1/system/llm-check", headers=headers)
    assert response.status_code == 503
    assert "禁用" in response.json()["detail"]


def test_single_review_records_egress_consent_audit(monkeypatch):
    """授权审计落库：主体/用途/授权/版本/时间；AI 关闭也会留痕。"""
    monkeypatch.setattr(routes.settings, "AI_EGRESS_ENABLED", True)
    client, headers = _session_client()
    doc_id = _upload_pdf(client, headers, "audit.pdf")

    created = client.post(
        "/api/v1/reviews",
        data={
            "document_id": doc_id,
            "ruleset_id": "campus_general_v1",
            "use_ai": "false",
            "privacy_consent": "false",
        },
        headers=headers,
    )
    assert created.status_code == 200
    review_id = created.json()["review_id"]

    rows = _consent_rows()
    matches = [r for r in rows if r["subject_id"] == review_id]
    assert len(matches) == 1
    record = matches[0]
    assert record["purpose"] == "single_review"
    assert record["actor_name"] == "superadmin"
    assert record["ai_enabled"] is False
    assert record["consent_granted"] is False
    assert record["policy_version"]


def _consent_rows():
    from sqlalchemy import select

    from app.models import EgressConsent
    from app.models.base import SessionLocal

    with SessionLocal() as session:
        return [
            {
                "purpose": row.purpose,
                "subject_id": row.subject_id,
                "actor_id": row.actor_id,
                "actor_name": row.actor_name,
                "consent_granted": row.consent_granted,
                "ai_enabled": row.ai_enabled,
                "policy_version": row.policy_version,
                "created_at": row.created_at,
            }
            for row in session.scalars(
                select(EgressConsent).order_by(EgressConsent.id.desc()).limit(50)
            )
        ]
