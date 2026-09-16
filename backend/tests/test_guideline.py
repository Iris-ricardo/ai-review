import sys
from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api import routes
from app.main import app
from app.services.llm.client import LLMUnavailableError
from app.services.llm.guideline import build_draft_yaml
from app.services.task_runtime import current_state


@pytest.fixture(autouse=True)
def _enable_egress(monkeypatch):
    """R08：指南生成属于材料外发，需要显式打开部署级开关（默认关闭）。"""
    monkeypatch.setattr(routes.settings, "AI_EGRESS_ENABLED", True)


def test_guideline_maps_all_supported_kinds_and_drops_invalid_and_dedupes():
    requirements = [
        {"kind": "page_limit", "detail": {"max_pages": 20}},
        {"kind": "word_limit", "detail": {"max_words": 15000}},
        {"kind": "required_section", "detail": {"sections": ["研究内容", "经费预算"]}},
        {"kind": "font", "detail": {"body_font": "宋体", "body_size": 12}},
        {"kind": "budget_ratio", "detail": {"item": "管理费", "max_ratio": 0.1}},
        {"kind": "duration", "detail": {"expected_months": 24}},
        {"kind": "attachment", "detail": {"expected": ["签字盖章页"]}},
        {"kind": "required_field", "detail": {"names": ["项目名称", "负责人"]}},
        {"kind": "field_format", "detail": {"name": "电子邮箱", "format": "email"}},
        {"kind": "heading_numbering", "detail": {"require_numbering": True}},
        {"kind": "layout", "detail": {
            "page_size": "A4",
            "orientation": "portrait",
            "margins_mm": {"left_min": 20, "right_min": 20},
        }},
        {"kind": "layout", "detail": {"page_size": "B5"}},
        {"kind": "field_format", "detail": {"name": "经费", "format": "currency"}},
        {"kind": "page_limit", "detail": {"max_pages": 20}},
        {"kind": "topic_scope", "detail": {"value": "人工智能"}},
    ]

    draft_yaml, dropped = build_draft_yaml(requirements)

    data = yaml.safe_load(draft_yaml)
    rules = data["rules"]
    assert [rule["type"] for rule in rules] == [
        "page_limit",
        "word_limit",
        "required_sections",
        "font_check",
        "budget_check",
        "date_check",
        "attachment_checklist",
        "required_fields",
        "field_format",
        "heading_numbering",
        "layout_check",
    ]
    assert rules[0]["params"] == {"max_pages": 20}
    assert rules[1]["params"] == {"max_words": 15000}
    assert rules[2]["params"] == {
        "sections": [{"title": "研究内容"}, {"title": "经费预算"}]
    }
    assert rules[3]["params"] == {"body_font": "宋体", "body_size": 12}
    assert rules[4]["params"] == {"max_ratio": {"管理费": 0.1}}
    assert rules[5]["params"] == {"expected_months": 24}
    assert rules[6]["params"] == {"expected": ["签字盖章页"]}
    assert rules[7]["params"] == {
        "fields": [{"name": "项目名称"}, {"name": "负责人"}]
    }
    assert rules[8]["params"] == {
        "fields": [{"name": "电子邮箱", "format": "email"}]
    }
    assert rules[9]["params"] == {
        "require_numbering": True,
        "allow_mixed_styles": False,
    }
    assert rules[10]["params"] == {
        "page": {"size": "A4", "orientation": "portrait"},
        "margins_mm": {"left_min": 20, "right_min": 20},
    }
    assert len(rules) == 11
    assert dropped == [
        {"kind": "layout", "detail": {"page_size": "B5"}},
        {"kind": "field_format", "detail": {"name": "经费", "format": "currency"}},
        {"kind": "topic_scope", "detail": {"value": "人工智能"}},
    ]


def test_from_guideline_api_returns_mocked_draft(tmp_path, monkeypatch):
    monkeypatch.setattr(routes.settings, "UPLOAD_DIR", str(tmp_path))
    # 指南生成属于旧管理接口：B2 起要求管理员凭据
    monkeypatch.setattr(routes.settings, "ADMIN_TOKEN", "test-admin-token")
    admin_headers = {"X-Admin-Token": "test-admin-token"}

    def extract(path):
        state = current_state()
        assert state is not None
        assert state.task_id.startswith("guideline-")
        return "ruleset: generated_from_guideline_v1\nrules: []\n", []

    monkeypatch.setattr(
        routes,
        "extract_draft_from_pdf",
        extract,
    )

    client = TestClient(app)
    response = client.post(
        "/api/v1/rulesets/from-guideline",
        headers=admin_headers,
        data={"privacy_consent": "true"},
        files={"file": ("guideline.pdf", b"%PDF-1.4\n", "application/pdf")},
    )

    assert response.status_code == 200
    assert response.json() == {
        "draft_yaml": "ruleset: generated_from_guideline_v1\nrules: []\n",
        "dropped": [],
    }


def test_from_guideline_requires_privacy_consent_before_upload(tmp_path, monkeypatch):
    monkeypatch.setattr(routes.settings, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(routes.settings, "ADMIN_TOKEN", "test-admin-token")
    admin_headers = {"X-Admin-Token": "test-admin-token"}
    called = False

    def extract(path):
        nonlocal called
        called = True
        return "", []

    monkeypatch.setattr(routes, "extract_draft_from_pdf", extract)
    response = TestClient(app).post(
        "/api/v1/rulesets/from-guideline",
        headers=admin_headers,
        files={"file": ("guideline.pdf", b"%PDF-1.4\n", "application/pdf")},
    )

    assert response.status_code == 422
    assert "授权" in response.json()["detail"]
    assert called is False
    assert not list(tmp_path.iterdir())


def test_from_guideline_api_returns_503_when_llm_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(routes.settings, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(routes.settings, "ADMIN_TOKEN", "test-admin-token")
    admin_headers = {"X-Admin-Token": "test-admin-token"}

    def fail(path):
        raise LLMUnavailableError("service down")

    monkeypatch.setattr(routes, "extract_draft_from_pdf", fail)

    client = TestClient(app)
    response = client.post(
        "/api/v1/rulesets/from-guideline",
        headers=admin_headers,
        data={"privacy_consent": "true"},
        files={"file": ("guideline.pdf", b"%PDF-1.4\n", "application/pdf")},
    )

    assert response.status_code == 503
    assert "LLM service unavailable" in response.json()["detail"]
