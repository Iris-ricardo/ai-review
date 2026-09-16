from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import fitz
import yaml
from fastapi.testclient import TestClient

from app.api import routes, rule_admin_routes
from app.main import app
from app.services import review_store, rule_admin_store
from app.services.rules.catalog import validate_ruleset
from app.services.task_runtime import TaskDeadlineExceeded


client = TestClient(app)


def _ruleset(ruleset_id: str) -> dict:
    return {
        "ruleset": ruleset_id,
        "name": "异常路径测试规则集",
        "description": "仅用于隔离测试数据库",
        "rules": [{
            "id": "E001",
            "type": "page_limit",
            "severity": "error",
            "description": "页数限制",
            "enabled": True,
            "params": {"max_pages": 2},
        }],
    }


def _document(path: Path, *, document_id: str | None = None) -> str:
    identifier = document_id or uuid.uuid4().hex[:12]
    review_store.create_document({
        "id": identifier,
        "filename": path.name,
        "file_path": str(path),
        "file_type": "pdf",
        "size": path.stat().st_size if path.exists() else 0,
        "mime_type": "application/pdf",
        "sha256": uuid.uuid4().hex * 2,
    })
    return identifier


def _pdf(path: Path) -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Synthetic proposal for API edge-case testing")
    document.save(path)
    document.close()


def test_login_missing_required_field_returns_structured_422():
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "superadmin"},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    assert detail[0]["loc"][-1] == "password"
    assert detail[0]["type"] == "missing"


def test_unpublished_managed_ruleset_is_rejected_before_review_or_batch_creation(
    tmp_path: Path,
    monkeypatch,
):
    ruleset_id = f"unpublished_{uuid.uuid4().hex[:8]}"
    rule_admin_store.create_ruleset(_ruleset(ruleset_id))
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-1.4\n%%EOF")
    document_id = _document(source)

    def forbidden(*args, **kwargs):
        raise AssertionError("work must not be created for an unpublished ruleset")

    monkeypatch.setattr(routes, "_start_review", forbidden)
    review = client.post(
        "/api/v1/reviews",
        data={
            "document_id": document_id,
            "ruleset_id": ruleset_id,
            "use_ai": "false",
        },
    )
    assert review.status_code == 409
    assert "no published version" in review.json()["detail"]

    monkeypatch.setattr(review_store, "create_batch", forbidden)
    batch = client.post(
        "/api/v1/batches",
        data={"ruleset_id": ruleset_id, "use_ai": "false"},
        files={"files": ("sample.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
    )
    assert batch.status_code == 409
    assert "no published version" in batch.json()["detail"]
    review_store.purge_documents([document_id])


def test_review_idempotency_is_atomic_with_concurrent_duplicate_requests(
    tmp_path: Path,
    monkeypatch,
):
    ruleset_id = f"atomic_{uuid.uuid4().hex[:8]}"
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "atomic.yaml").write_text(
        yaml.safe_dump(_ruleset(ruleset_id), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(routes.settings, "RULES_DIR", str(rules_dir))
    source = tmp_path / "atomic.pdf"
    source.write_bytes(b"%PDF-1.4\n%%EOF")
    document_id = _document(source)

    original_create = review_store.create_review
    create_count = 0
    count_lock = threading.Lock()

    def slow_create(*args, **kwargs):
        nonlocal create_count
        with count_lock:
            create_count += 1
        time.sleep(0.05)
        return original_create(*args, **kwargs)

    monkeypatch.setattr(review_store, "create_review", slow_create)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(
            lambda _: routes._new_review(
                document_id,
                ruleset_id,
                use_ai=False,
                idempotency_key="same-request",
            ),
            range(4),
        ))

    assert create_count == 1
    assert len({review_id for review_id, _ in results}) == 1
    assert sum(not reused for _, reused in results) == 1
    review_store.purge_documents([document_id])


def test_rule_admin_actor_context_is_isolated_between_threads():
    barrier = threading.Barrier(2)

    def observe(actor: str) -> str:
        token = rule_admin_store.set_current_actor(actor)
        try:
            barrier.wait(timeout=2)
            time.sleep(0.02)
            return rule_admin_store.actor_name()
        finally:
            rule_admin_store.reset_current_actor(token)

    with ThreadPoolExecutor(max_workers=2) as executor:
        actors = set(executor.map(observe, ("maintainer-a", "maintainer-b")))

    assert actors == {"maintainer-a", "maintainer-b"}
    assert rule_admin_store.actor_name() == "bootstrap-admin"


def test_rule_admin_rejects_bad_types_and_partial_test_cannot_unlock_release(
    tmp_path: Path,
):
    ruleset_id = f"payload_{uuid.uuid4().hex[:8]}"
    version = rule_admin_store.create_ruleset(_ruleset(ruleset_id))

    invalid_revision = client.put(
        f"/api/v1/rule-admin/versions/{version['id']}",
        json={"data": _ruleset(ruleset_id), "revision": "not-an-integer"},
    )
    assert invalid_revision.status_code == 422
    detail = invalid_revision.json()["detail"]
    assert isinstance(detail, list)
    assert {"type", "loc", "msg", "input"} <= set(detail[0])

    invalid_index = client.post(
        f"/api/v1/rule-admin/versions/{version['id']}/rules/reorder",
        json={
            "rule_id": "E001",
            "revision": version["revision"],
            "target_index": "first",
        },
    )
    assert invalid_index.status_code == 422

    invalid_rule_ids = client.post(
        f"/api/v1/rule-admin/versions/{version['id']}/test",
        json={"document_id": "unused", "rule_ids": None},
    )
    assert invalid_rule_ids.status_code == 422

    pdf_path = tmp_path / "partial.pdf"
    _pdf(pdf_path)
    document_id = _document(pdf_path)
    tested = client.post(
        f"/api/v1/rule-admin/versions/{version['id']}/test",
        json={
            "document_id": document_id,
            "use_ai": False,
            "rule_ids": ["E001"],
        },
    )
    assert tested.status_code == 200
    assert tested.json()["rules_executed"] == 1
    assert tested.json()["success"] is True
    assert tested.json()["release_eligible"] is False
    assert tested.json()["status"] == "partial"

    submitted = client.post(
        f"/api/v1/rule-admin/versions/{version['id']}/submit",
        json={"assigned_reviewer": "reviewer"},
    )
    assert submitted.status_code == 422
    assert "样例测试" in submitted.json()["detail"]
    review_store.purge_documents([document_id])


def test_rule_admin_test_maps_task_deadline_to_504(tmp_path: Path, monkeypatch):
    ruleset_id = f"deadline_{uuid.uuid4().hex[:8]}"
    version = rule_admin_store.create_ruleset(_ruleset(ruleset_id))
    source = tmp_path / "deadline.pdf"
    source.write_bytes(b"%PDF-1.4\n%%EOF")
    document_id = _document(source)

    def deadline(_path):
        raise TaskDeadlineExceeded("审查任务执行超时")

    monkeypatch.setattr(
        rule_admin_routes,
        "get_parser",
        lambda _path: SimpleNamespace(parse=deadline),
    )
    response = client.post(
        f"/api/v1/rule-admin/versions/{version['id']}/test",
        json={"document_id": document_id},
    )
    assert response.status_code == 504
    assert response.json()["detail"] == "审查任务执行超时"
    review_store.purge_documents([document_id])


def test_layout_validation_reports_bad_numeric_type_without_crashing():
    data = _ruleset(f"layout_{uuid.uuid4().hex[:8]}")
    data["rules"][0] = {
        "id": "E001",
        "type": "layout_check",
        "severity": "warning",
        "enabled": True,
        "params": {"body": {"min_size": "large", "max_size": 12}},
    }

    ruleset, errors, _ = validate_ruleset(data)

    assert ruleset is not None
    assert any(
        error["path"].endswith("body.min_size")
        and "数字" in error["message"]
        for error in errors
    )


def test_cleanup_hides_internal_paths_and_keeps_database_when_unlink_fails(
    tmp_path: Path,
    monkeypatch,
):
    upload_dir = tmp_path / "uploads"
    output_dir = tmp_path / "outputs"
    upload_dir.mkdir()
    output_dir.mkdir()
    monkeypatch.setattr(routes.settings, "UPLOAD_DIR", str(upload_dir))
    monkeypatch.setattr(routes.settings, "OUTPUT_DIR", str(output_dir))
    # 数据清理属旧管理接口：B2 起要求管理员凭据
    monkeypatch.setattr(routes.settings, "ADMIN_TOKEN", "test-admin-token")
    admin_headers = {"X-Admin-Token": "test-admin-token"}

    source = upload_dir / "old.pdf"
    source.write_bytes(b"%PDF-1.4\n%%EOF")
    document_id = _document(source)
    stored = review_store.create_review(
        document_id,
        "campus_general_v1",
        ruleset_snapshot="ruleset: campus_general_v1\nname: test\nrules: []\n",
        idempotency_key=uuid.uuid4().hex,
        use_ai=False,
    )
    review_store.update_review(
        stored["review_id"],
        status="done",
        progress=100,
        completed_at=review_store.now() - timedelta(days=60),
    )

    preview = client.post(
        "/api/v1/maintenance/cleanup?days=30",
        headers=admin_headers,
    )
    assert preview.status_code == 200
    candidate = next(
        item for item in preview.json()["candidates"]
        if item["document_id"] == document_id
    )
    assert "paths" not in candidate
    assert str(tmp_path) not in preview.text

    original_unlink = Path.unlink

    def fail_source(path: Path, *args, **kwargs):
        if path.resolve() == source.resolve():
            raise PermissionError("locked")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_source)
    executed = client.post(
        "/api/v1/maintenance/cleanup?days=30&dry_run=false",
        headers=admin_headers,
    )
    assert executed.status_code == 200
    assert executed.json()["failed_files"] == ["old.pdf"]
    assert executed.json()["skipped_documents"] >= 1
    assert str(tmp_path) not in executed.text
    assert review_store.get_document(document_id) is not None
    review_store.purge_documents([document_id])


def test_publish_reports_yaml_export_failure_without_rolling_back_database(
    tmp_path: Path,
    monkeypatch,
):
    ruleset_id = f"export_{uuid.uuid4().hex[:8]}"
    version = rule_admin_store.create_ruleset(
        _ruleset(ruleset_id),
        actor="maintainer",
    )
    rule_admin_store.record_test_run(
        ruleset_id,
        version["id"],
        "synthetic-document",
        version["sha256"],
        {"system_failures": 0, "rules_executed": 1, "issue_count": 0},
    )
    rule_admin_store.set_version_state(
        version["id"],
        "submit",
        actor="maintainer",
        assigned_reviewer="reviewer",
    )
    rule_admin_store.set_version_state(
        version["id"],
        "approve",
        actor="reviewer",
    )

    def fail_export(*args, **kwargs):
        raise OSError("read-only export directory")

    monkeypatch.setattr(rule_admin_store, "_sync_export", fail_export)
    published = rule_admin_store.publish(
        version["id"],
        tmp_path,
        actor="reviewer",
    )

    assert published["state"] == "published"
    assert published["export_synced"] is False
    assert "YAML" in published["export_warning"]
    assert rule_admin_store.get_version(version["id"])["state"] == "published"


def test_cors_preflight_allows_configured_local_frontend_only():
    allowed = client.options(
        "/api/v1/reviews",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "POST" in allowed.headers["access-control-allow-methods"]

    denied = client.options(
        "/api/v1/reviews",
        headers={
            "Origin": "https://untrusted.invalid",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert denied.status_code == 400
    assert "access-control-allow-origin" not in denied.headers
