"""Independent acceptance tests for the visual ruleset administration platform."""
from __future__ import annotations

import importlib
import uuid
from pathlib import Path

import fitz
import yaml
from fastapi.testclient import TestClient

from app.api import rule_admin_routes
from app.main import app
from app.services import review_store, rule_admin_store
from app.services.rules.catalog import validate_ruleset


def _ruleset(ruleset_id: str, *, max_pages: int = 2) -> dict:
    return {
        "ruleset": ruleset_id,
        "name": "独立验收规则集",
        "description": "仅写入 pytest 隔离数据库",
        "rules": [{
            "id": "A001",
            "type": "page_limit",
            "severity": "error",
            "description": "页数限制",
            "enabled": True,
            "tags": ["验收"],
            "basis": {"document": "验收指南", "clause": "1.1"},
            "review_note": "人工复核附件页",
            "params": {"max_pages": max_pages, "section_limits": {}},
        }],
    }


def test_admin_token_optional_and_access_token_remains_outer_boundary(monkeypatch):
    client = TestClient(app)
    main_module = importlib.import_module("app.main")
    ruleset_id = f"auth_{uuid.uuid4().hex[:8]}"

    monkeypatch.setattr(rule_admin_routes.settings, "ADMIN_TOKEN", "")
    assert client.get("/api/v1/rule-admin/checkers").status_code == 200
    assert client.post(
        "/api/v1/rule-admin/rulesets", json={"data": _ruleset(ruleset_id)}
    ).status_code == 200

    monkeypatch.setattr(rule_admin_routes.settings, "ADMIN_TOKEN", "admin-secret")
    monkeypatch.setattr(main_module.settings, "ACCESS_TOKEN", "access-secret")
    assert client.get("/api/v1/rule-admin/checkers").status_code == 401
    assert client.get(
        "/api/v1/rule-admin/checkers",
        headers={"X-Access-Token": "access-secret"},
    ).status_code == 401
    assert client.get(
        "/api/v1/rule-admin/checkers",
        headers={
            "X-Access-Token": "access-secret",
            "X-Admin-Token": "admin-secret",
        },
    ).status_code == 200


def test_all_builtin_rulesets_survive_structured_yaml_round_trip():
    rules_dir = Path(__file__).resolve().parents[2] / "rules"
    for path in sorted(rules_dir.glob("*.yaml")):
        original_data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        original, errors, _ = validate_ruleset(original_data)
        assert original is not None and errors == [], path.name

        canonical = yaml.safe_dump(
            original.model_dump(exclude_defaults=True),
            allow_unicode=True,
            sort_keys=False,
        )
        restored, restored_errors, _ = validate_ruleset(yaml.safe_load(canonical) or {})
        assert restored is not None and restored_errors == [], path.name
        assert restored.model_dump() == original.model_dump(), path.name


def test_reference_semantic_review_is_not_called_when_ai_is_disabled(
    tmp_path, monkeypatch
):
    ruleset_id = f"privacy_{uuid.uuid4().hex[:8]}"
    data = _ruleset(ruleset_id)
    data["rules"][0] = {
        "id": "A001",
        "type": "reference_format",
        "severity": "warning",
        "description": "参考文献复核",
        "enabled": True,
        "params": {"semantic_review": True, "max_chunk_chars": 2000},
    }
    version = rule_admin_store.create_ruleset(data)

    pdf_path = tmp_path / "privacy.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Synthetic proposal reference section for acceptance testing")
    document.save(pdf_path)
    document.close()
    document_id = f"privacy-doc-{uuid.uuid4().hex[:8]}"
    review_store.create_document({
        "id": document_id,
        "filename": pdf_path.name,
        "file_path": str(pdf_path),
        "file_type": "pdf",
        "size": pdf_path.stat().st_size,
        "mime_type": "application/pdf",
        "sha256": uuid.uuid4().hex,
    })

    llm_checkers = importlib.import_module("app.services.llm.checkers")

    def forbidden_call(*_args, **_kwargs):
        raise AssertionError("AI must not be called when use_ai is false")

    monkeypatch.setattr(llm_checkers, "call_json", forbidden_call)
    response = TestClient(app).post(
        f"/api/v1/rule-admin/versions/{version['id']}/test",
        json={"document_id": document_id, "use_ai": False},
    )
    assert response.status_code == 200
    assert response.json()["rules_executed"] == 0
    assert response.json()["success"] is False
    assert response.json()["release_eligible"] is False
    assert response.json()["status"] == "failed"


def test_content_change_invalidates_prior_test_release_gate():
    ruleset_id = f"sha_{uuid.uuid4().hex[:8]}"
    version = rule_admin_store.create_ruleset(_ruleset(ruleset_id, max_pages=2))
    rule_admin_store.record_test_run(
        ruleset_id,
        version["id"],
        "synthetic-document",
        version["sha256"],
        {"system_failures": 0, "rules_executed": 1, "issue_count": 0},
    )
    updated = rule_admin_store.save_draft(
        version["id"],
        _ruleset(ruleset_id, max_pages=5),
        version["revision"],
    )
    try:
        rule_admin_store.set_version_state(
            updated["id"], "submit", actor="maintainer", assigned_reviewer="reviewer"
        )
    except rule_admin_store.RuleAdminError as exc:
        assert "样例测试" in str(exc)
    else:
        raise AssertionError("A stale test result must not unlock publication")
