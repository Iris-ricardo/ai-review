"""Contract tests for the versioned visual ruleset administration API."""
from __future__ import annotations

import uuid
from pathlib import Path

import fitz
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api import routes, rule_admin_routes
from app.main import app
from app.models import ManagedRuleSet, ReviewTask, RuleSetVersion
from app.models.base import SessionLocal
from app.services import review_store, rule_admin_store
from app.services.rules.base_checker import list_registered_checker_types
from app.services.rules.catalog import checker_catalog, rule_requires_ai, validate_ruleset
from app.services.rules.rule_schema import RuleDef


def _data(ruleset_id: str, max_pages: int = 2, name: str = "可视化测试规则集") -> dict:
    return {
        "ruleset": ruleset_id,
        "name": name,
        "description": "只存在于隔离测试目录",
        "rules": [{
            "id": "T001",
            "type": "page_limit",
            "severity": "error",
            "description": "页数限制",
            "enabled": True,
            "basis": {"document": "测试指南", "clause": "1.1"},
            "params": {"max_pages": max_pages},
        }],
    }


def _review_count() -> int:
    with SessionLocal() as session:
        return int(session.scalar(select(func.count()).select_from(ReviewTask)) or 0)


def _login_headers(client: TestClient, username: str, password: str) -> dict[str, str]:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_catalog_exactly_matches_registered_checkers():
    assert {item["type"] for item in checker_catalog()} == set(list_registered_checker_types())
    assert all(item["schema"]["type"] == "object" for item in checker_catalog())


def test_optional_semantic_reference_review_requires_ai_consent():
    local_rule = RuleDef(
        id="REF1", type="reference_format", severity="warning",
        params={"semantic_review": False},
    )
    semantic_rule = RuleDef(
        id="REF2", type="reference_format", severity="warning",
        params={"semantic_review": True},
    )
    assert rule_requires_ai(local_rule) is False
    assert rule_requires_ai(semantic_rule) is True


def test_delete_ruleset_only_before_publication(monkeypatch):
    monkeypatch.setattr(rule_admin_routes.settings, "ADMIN_TOKEN", "secret")
    client = TestClient(app)
    headers = {"X-Admin-Token": "secret"}
    draft_ruleset_id = f"delete_draft_{uuid.uuid4().hex[:8]}"
    published_ruleset_id = f"delete_published_{uuid.uuid4().hex[:8]}"

    created = client.post(
        "/api/v1/rule-admin/rulesets",
        json={"data": _data(draft_ruleset_id)},
        headers=headers,
    )
    assert created.status_code == 200
    deleted = client.delete(
        f"/api/v1/rule-admin/rulesets/{draft_ruleset_id}",
        headers=headers,
    )
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert client.get(
        f"/api/v1/rule-admin/rulesets/{draft_ruleset_id}",
        headers=headers,
    ).status_code == 404

    published = client.post(
        "/api/v1/rule-admin/rulesets",
        json={"data": _data(published_ruleset_id)},
        headers=headers,
    )
    assert published.status_code == 200
    version_id = published.json()["version"]["id"]
    with SessionLocal.begin() as session:
        version = session.get(RuleSetVersion, version_id)
        version.state = "published"
        item = session.get(ManagedRuleSet, published_ruleset_id)
        item.active_version_id = version_id
        item.status = "active"
    blocked = client.delete(
        f"/api/v1/rule-admin/rulesets/{published_ruleset_id}",
        headers=headers,
    )
    assert blocked.status_code == 422
    assert "不能彻底删除" in blocked.json()["detail"]


def test_all_builtin_rulesets_pass_management_catalog_validation():
    rules_dir = Path(__file__).resolve().parents[2] / "rules"
    validated = {}
    for path in sorted(rules_dir.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        ruleset, errors, _ = validate_ruleset(data)
        assert ruleset is not None, path.name
        assert errors == [], f"{path.name}: {errors}"
        validated[ruleset.ruleset] = len(ruleset.rules)
    assert validated == {
        "campus_general_v1": 20,
        "dachuang_2026_v1": 20,
        "nsfc_general_v1": 20,
    }


def test_versioned_lifecycle_isolated_from_public_catalog_and_reviews(tmp_path, monkeypatch):
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    monkeypatch.setattr(routes.settings, "RULES_DIR", str(rules_dir))
    monkeypatch.setattr(rule_admin_routes.settings, "RULES_DIR", str(rules_dir))
    monkeypatch.setattr(routes, "_configured_rules_dir", rules_dir.resolve())
    monkeypatch.setattr(rule_admin_routes.settings, "ADMIN_TOKEN", "secret")
    monkeypatch.setattr(routes.settings, "ADMIN_TOKEN", "secret")
    client = TestClient(app)
    ruleset_id = f"visual_{uuid.uuid4().hex[:8]}"
    headers = {"X-Admin-Token": "secret"}

    assert client.get("/api/v1/rule-admin/rulesets").status_code == 401
    assert client.get("/api/v1/rule-admin/checkers").status_code == 401
    assert client.get("/api/v1/rule-admin/rulesets", headers=headers).status_code == 200
    unauthorized = client.post("/api/v1/rule-admin/rulesets", json={"data": _data(ruleset_id)})
    assert unauthorized.status_code == 401
    created = client.post(
        "/api/v1/rule-admin/rulesets", json={"data": _data(ruleset_id)}, headers=headers
    )
    assert created.status_code == 200
    draft = created.json()["version"]
    assert draft["state"] == "draft"
    assert draft["data"]["rules"][0]["basis"]["clause"] == "1.1"
    assert client.get(f"/api/v1/rulesets/{ruleset_id}").status_code == 404
    assert not list(rules_dir.glob("*.yaml"))

    conflict = client.put(
        f"/api/v1/rule-admin/versions/{draft['id']}",
        json={"data": _data(ruleset_id), "revision": 99}, headers=headers,
    )
    assert conflict.status_code == 409
    gated = client.post(
        f"/api/v1/rule-admin/versions/{draft['id']}/submit",
        json={"assigned_reviewer": "reviewer"}, headers=headers,
    )
    assert gated.status_code == 422

    pdf_path = tmp_path / "sample.pdf"
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "Synthetic proposal for deterministic page check")
    pdf.save(pdf_path)
    pdf.close()
    document_id = f"rule-test-{uuid.uuid4().hex[:8]}"
    review_store.create_document({
        "id": document_id, "filename": pdf_path.name, "file_path": str(pdf_path),
        "file_type": "pdf", "size": pdf_path.stat().st_size,
        "mime_type": "application/pdf", "sha256": uuid.uuid4().hex,
    })
    before = _review_count()
    no_consent = client.post(
        f"/api/v1/rule-admin/versions/{draft['id']}/test",
        json={"document_id": document_id, "use_ai": True}, headers=headers,
    )
    assert no_consent.status_code == 422
    tested = client.post(
        f"/api/v1/rule-admin/versions/{draft['id']}/test",
        json={"document_id": document_id, "use_ai": False}, headers=headers,
    )
    assert tested.status_code == 200
    assert tested.json()["status"] == "done"
    assert tested.json()["success"] is True
    assert tested.json()["system_failures"] == 0
    assert tested.json()["rules_executed"] == 1
    assert _review_count() == before

    submitted = client.post(
        f"/api/v1/rule-admin/versions/{draft['id']}/submit",
        json={"assigned_reviewer": "reviewer"}, headers=headers,
    )
    assert submitted.status_code == 200
    reviewer_headers = _login_headers(client, "reviewer", "reviewer123456")
    approved = client.post(
        f"/api/v1/rule-admin/versions/{draft['id']}/approve",
        json={"note": "contract approval"}, headers=reviewer_headers,
    )
    assert approved.status_code == 200

    published = client.post(
        f"/api/v1/rule-admin/versions/{draft['id']}/publish", json={}, headers=reviewer_headers
    )
    assert published.status_code == 200
    v1 = published.json()["version"]
    public = client.get(f"/api/v1/rulesets/{ruleset_id}")
    assert public.status_code == 200
    assert public.json()["rule_count"] == 1
    assert "yaml" not in public.json()

    cloned = client.post(
        f"/api/v1/rule-admin/rulesets/{ruleset_id}/clone",
        json={"source_version_id": v1["id"]}, headers=headers,
    ).json()["version"]
    updated = client.put(
        f"/api/v1/rule-admin/versions/{cloned['id']}",
        json={
            "data": _data(ruleset_id, 5, "仅在草稿中的新名称"),
            "revision": cloned["revision"],
        },
        headers=headers,
    ).json()["version"]
    assert client.get(f"/api/v1/rulesets/{ruleset_id}").json()["name"] == "可视化测试规则集"
    client.post(
        f"/api/v1/rule-admin/versions/{updated['id']}/test",
        json={"document_id": document_id}, headers=headers,
    )
    submitted_v2 = client.post(
        f"/api/v1/rule-admin/versions/{updated['id']}/submit",
        json={"assigned_reviewer": "reviewer"}, headers=headers
    )
    assert submitted_v2.status_code == 200
    assert client.post(
        f"/api/v1/rule-admin/versions/{updated['id']}/approve",
        json={}, headers=reviewer_headers,
    ).status_code == 200
    v2 = client.post(
        f"/api/v1/rule-admin/versions/{updated['id']}/publish", json={}, headers=reviewer_headers
    ).json()["version"]
    assert v2["version_number"] > v1["version_number"]
    assert client.get(f"/api/v1/rulesets/{ruleset_id}").json()["name"] == "仅在草稿中的新名称"
    assert rule_admin_store.get_version(v1["id"])["state"] == "retired"
    assert rule_admin_store.get_version(v1["id"])["data"]["rules"][0]["params"]["max_pages"] == 2

    restored = client.post(
        f"/api/v1/rule-admin/rulesets/{ruleset_id}/restore",
        json={"source_version_id": v1["id"]}, headers=headers,
    )
    assert restored.status_code == 200
    restored_draft = restored.json()["version"]
    assert restored_draft["state"] == "draft"
    assert restored_draft["version_number"] > v2["version_number"]
    assert restored_draft["data"]["rules"][0]["params"]["max_pages"] == 2
    still_v2 = client.get(f"/api/v1/rulesets/{ruleset_id}")
    assert still_v2.json()["name"] == "仅在草稿中的新名称"
    client.post(
        f"/api/v1/rule-admin/versions/{restored_draft['id']}/test",
        json={"document_id": document_id}, headers=headers,
    )
    client.post(
        f"/api/v1/rule-admin/versions/{restored_draft['id']}/submit",
        json={"assigned_reviewer": "reviewer"}, headers=headers,
    )
    client.post(
        f"/api/v1/rule-admin/versions/{restored_draft['id']}/approve",
        json={}, headers=reviewer_headers,
    )
    v3 = client.post(
        f"/api/v1/rule-admin/versions/{restored_draft['id']}/publish",
        json={}, headers=reviewer_headers,
    ).json()["version"]
    assert v3["state"] == "published"

    disabled = client.post(
        f"/api/v1/rule-admin/rulesets/{ruleset_id}/disable", json={}, headers=headers
    )
    assert disabled.status_code == 200
    assert client.get(f"/api/v1/rulesets/{ruleset_id}").status_code == 404
    client.post(f"/api/v1/rule-admin/rulesets/{ruleset_id}/enable", json={}, headers=headers)
    assert client.get(f"/api/v1/rulesets/{ruleset_id}").status_code == 200

    review_store.purge_documents([document_id])


def test_legacy_put_creates_draft_without_changing_public_catalog(tmp_path, monkeypatch):
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    monkeypatch.setattr(routes.settings, "RULES_DIR", str(rules_dir))
    monkeypatch.setattr(routes, "_configured_rules_dir", rules_dir.resolve())
    # 旧管理接口自 B2 起要求管理员凭据
    monkeypatch.setattr(routes.settings, "ADMIN_TOKEN", "test-admin-token")
    admin_headers = {"X-Admin-Token": "test-admin-token"}
    ruleset_id = f"legacy_{uuid.uuid4().hex[:8]}"
    text = yaml.safe_dump(_data(ruleset_id), allow_unicode=True, sort_keys=False)

    response = TestClient(app).put(
        f"/api/v1/rulesets/{ruleset_id}", content=text,
        headers={"content-type": "text/plain", **admin_headers},
    )

    assert response.status_code == 200
    assert response.json()["state"] == "draft"
    assert response.json()["published"] is False
    assert TestClient(app).get(f"/api/v1/rulesets/{ruleset_id}").status_code == 404
    assert not list(rules_dir.glob("*.yaml"))


def test_publish_rejects_ruleset_without_enabled_rules(tmp_path):
    ruleset_id = f"disabled_{uuid.uuid4().hex[:8]}"
    data = _data(ruleset_id)
    data["rules"][0]["enabled"] = False
    version = rule_admin_store.create_ruleset(data)
    rule_admin_store.record_test_run(
        ruleset_id, version["id"], "synthetic", version["sha256"],
        {"system_failures": 0, "rules_executed": 0, "issue_count": 0},
    )
    rule_admin_store.set_version_state(
        version["id"], "submit", actor="maintainer", assigned_reviewer="reviewer"
    )
    rule_admin_store.set_version_state(version["id"], "approve", actor="reviewer")

    with pytest.raises(rule_admin_store.RuleAdminError, match="至少包含一条已启用规则"):
        rule_admin_store.publish(version["id"], tmp_path, actor="reviewer")


def test_seed_import_is_idempotent(tmp_path):
    ruleset_id = f"seed_{uuid.uuid4().hex[:8]}"
    path = tmp_path / "seed.yaml"
    path.write_text(yaml.safe_dump(_data(ruleset_id), allow_unicode=True, sort_keys=False), encoding="utf-8")
    rule_admin_store.ensure_seeded(tmp_path)
    rule_admin_store.ensure_seeded(tmp_path)
    with SessionLocal() as session:
        versions = session.scalars(select(RuleSetVersion).where(
            RuleSetVersion.ruleset_id == ruleset_id
        )).all()
    assert len(versions) == 1
