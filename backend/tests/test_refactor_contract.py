from __future__ import annotations

import threading
import time
import uuid
import importlib
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient
from sqlalchemy import create_engine, inspect

from app.api import routes
from app.main import app
from app.schemas.ir import Block, DocumentIR
from app.services import review_store
from app.services.bounded_executor import BoundedTaskExecutor
from app.services.rules.engine import RuleEngine
from app.services.task_runtime import (
    TaskCancelled,
    TaskDeadlineExceeded,
    checkpoint,
    consume_llm_call,
    task_scope,
)


client = TestClient(app)


def _cached_document() -> str:
    document_id = uuid.uuid4().hex[:12]
    data = {
        "id": document_id,
        "filename": "synthetic.pdf",
        "file_path": "unused.pdf",
        "file_type": "pdf",
        "size": 10,
        "sha256": uuid.uuid4().hex * 2,
    }
    review_store.create_document(data)
    routes._documents[document_id] = data
    return document_id


def test_unknown_ruleset_is_rejected_instead_of_falling_back():
    document_id = _cached_document()
    response = client.post(
        "/api/v1/reviews",
        data={
            "document_id": document_id,
            "ruleset_id": "definitely_missing",
            "use_ai": "false",
        },
    )
    assert response.status_code == 404


def test_issues_endpoint_rejects_incomplete_review():
    document_id = uuid.uuid4().hex[:12]
    review_store.create_document({
        "id": document_id,
        "filename": "incomplete.pdf",
        "file_path": "unused.pdf",
        "file_type": "pdf",
        "size": 1,
        "sha256": uuid.uuid4().hex * 2,
    })
    stored = review_store.create_review(
        document_id,
        "campus_general_v1",
        ruleset_snapshot="ruleset: empty\nname: Empty\nrules: []\n",
        idempotency_key=uuid.uuid4().hex,
        use_ai=False,
    )
    response = client.get(f"/api/v1/reviews/{stored['review_id']}/issues")
    assert response.status_code == 409
    review_store.update_review(
        stored["review_id"],
        status="cancelled",
        completed_at=review_store.now(),
    )


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("fake.pdf", b"this is not a pdf"),
        ("fake.docx", b"this is not a zip"),
    ],
)
def test_upload_rejects_extension_spoofing(filename: str, content: bytes):
    response = client.post(
        "/api/v1/documents",
        files={"file": (filename, content)},
    )
    assert response.status_code == 400


def test_duplicate_active_review_is_reused(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    document_id = _cached_document()

    def slow_review(review_id, doc_id, ruleset_id):
        started.set()
        release.wait(timeout=5)
        routes._reviews[review_id].update({"status": "done", "progress": 100})

    monkeypatch.setattr(routes, "_execute_review", slow_review)
    try:
        first = client.post(
            "/api/v1/reviews",
            data={"document_id": document_id, "ruleset_id": "campus", "use_ai": "false"},
        )
        assert first.status_code == 200
        assert started.wait(timeout=1)
        second = client.post(
            "/api/v1/reviews",
            data={"document_id": document_id, "ruleset_id": "campus", "use_ai": "false"},
        )
        assert second.status_code == 200
        assert second.json()["review_id"] == first.json()["review_id"]
        assert second.json()["reused"] is True
    finally:
        release.set()


def test_bounded_executor_cooperatively_cancels_running_task():
    executor = BoundedTaskExecutor(max_workers=1, queue_capacity=0, timeout_seconds=10)
    started = threading.Event()

    def work():
        started.set()
        while True:
            checkpoint()
            time.sleep(0.01)

    future = executor.submit("cancel-me", work)
    assert started.wait(timeout=1)
    assert executor.cancel("cancel-me") is True
    with pytest.raises(TaskCancelled):
        future.result(timeout=2)
    executor.shutdown()


def test_bounded_executor_enforces_total_deadline():
    executor = BoundedTaskExecutor(max_workers=1, queue_capacity=0, timeout_seconds=1)

    def work():
        while True:
            checkpoint()
            time.sleep(0.02)

    future = executor.submit("timeout-me", work)
    with pytest.raises(TaskDeadlineExceeded):
        future.result(timeout=3)
    executor.shutdown()


def test_task_llm_call_budget_is_hard_bounded():
    with task_scope("budget", threading.Event(), 10):
        assert consume_llm_call(2) is True
        assert consume_llm_call(2) is True
        assert consume_llm_call(2) is False


def test_generated_required_sections_rule_executes(tmp_path: Path):
    ruleset = tmp_path / "generated.yaml"
    ruleset.write_text(
        "\n".join([
            "ruleset: generated",
            "name: generated",
            "rules:",
            "  - id: G001",
            "    type: required_sections",
            "    severity: error",
            "    params:",
            "      sections:",
            "        - title: 研究内容",
        ]),
        encoding="utf-8",
    )
    ir = DocumentIR(blocks=[Block(id="b1", page=1, type="paragraph", text="正文")])
    issues = RuleEngine(ruleset).run(ir)
    assert len(issues) == 1
    assert issues[0].rule_id == "G001"
    assert issues[0].message.startswith("缺少必填章节")


def test_review_status_survives_compatibility_cache_clear(tmp_path: Path):
    document_id = uuid.uuid4().hex[:12]
    source = tmp_path / "persisted.pdf"
    source.write_bytes(b"%PDF-1.4\n%%EOF")
    review_store.create_document({
        "id": document_id,
        "filename": source.name,
        "file_path": str(source),
        "file_type": "pdf",
        "size": source.stat().st_size,
        "sha256": uuid.uuid4().hex * 2,
    })
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
        result_json={"conclusion": "incomplete", "issues": []},
        completed_at=review_store.now(),
    )
    routes._reviews.pop(stored["review_id"], None)
    response = client.get(f"/api/v1/reviews/{stored['review_id']}")
    assert response.status_code == 200
    assert response.json()["status"] == "done"
    assert response.json()["conclusion"] == "incomplete"
    review_store.purge_documents([document_id])


def test_privacy_consent_required_before_external_ai(monkeypatch):
    document_id = _cached_document()
    monkeypatch.setattr(
        routes,
        "get_settings",
        lambda: SimpleNamespace(LLM_API_KEY="configured"),
    )
    response = client.post(
        "/api/v1/reviews",
        data={
            "document_id": document_id,
            "ruleset_id": "campus",
            "use_ai": "true",
            "privacy_consent": "false",
        },
    )
    assert response.status_code == 422
    assert "授权" in response.json()["detail"]


def test_optional_access_token_protects_api(monkeypatch):
    main_module = importlib.import_module("app.main")
    monkeypatch.setattr(main_module.settings, "ACCESS_TOKEN", "test-access-token")
    unauthorized = client.get("/api/v1/rulesets")
    authorized = client.get(
        "/api/v1/rulesets",
        headers={"X-Access-Token": "test-access-token"},
    )
    assert unauthorized.status_code == 401
    assert authorized.status_code == 200


def test_mutation_rate_limit_returns_429(monkeypatch):
    main_module = importlib.import_module("app.main")
    monkeypatch.setattr(main_module.settings, "RATE_LIMIT_PER_MINUTE", 1)
    main_module._rate_events.clear()
    try:
        first = client.post(
            "/api/v1/documents",
            files={"file": ("fake.pdf", b"invalid")},
        )
        second = client.post(
            "/api/v1/documents",
            files={"file": ("fake.pdf", b"invalid")},
        )
        assert first.status_code == 400
        assert second.status_code == 429
        assert second.headers["retry-after"] == "60"
    finally:
        main_module._rate_events.clear()


def test_additive_migration_upgrades_prototype_sqlite(tmp_path: Path, monkeypatch):
    database = tmp_path / "prototype.db"
    old_engine = create_engine(f"sqlite:///{database}")
    with old_engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE documents ("
            "id VARCHAR(32) PRIMARY KEY, filename VARCHAR(512) NOT NULL, "
            "original_path VARCHAR(1024) NOT NULL, file_type VARCHAR(10) NOT NULL, "
            "file_size INTEGER, created_at DATETIME)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE review_tasks ("
            "id VARCHAR(32) PRIMARY KEY, document_id VARCHAR(32) NOT NULL, "
            "ruleset_id VARCHAR(64) NOT NULL, status VARCHAR(20), progress INTEGER, "
            "result_json JSON, error_message TEXT, created_at DATETIME, completed_at DATETIME)"
        )
    monkeypatch.setattr(review_store, "engine", old_engine)
    review_store.init_store()
    columns = {item["name"] for item in inspect(old_engine).get_columns("review_tasks")}
    assert {"stage", "current_rule", "ruleset_snapshot", "last_activity_at"} <= columns
    document_columns = {item["name"] for item in inspect(old_engine).get_columns("documents")}
    assert {"sha256", "converted_path", "ir_json"} <= document_columns


def test_retention_cleanup_defaults_to_dry_run(tmp_path: Path, monkeypatch):
    # 数据清理属旧管理接口：B2 起要求管理员凭据
    monkeypatch.setattr(routes.settings, "ADMIN_TOKEN", "test-admin-token")
    admin_headers = {"X-Admin-Token": "test-admin-token"}
    document_id = uuid.uuid4().hex[:12]
    source = tmp_path / "old.pdf"
    source.write_bytes(b"%PDF-1.4\n%%EOF")
    review_store.create_document({
        "id": document_id,
        "filename": source.name,
        "file_path": str(source),
        "file_type": "pdf",
        "size": source.stat().st_size,
        "sha256": uuid.uuid4().hex * 2,
    })
    stored = review_store.create_review(
        document_id,
        "campus_general_v1",
        ruleset_snapshot="ruleset: campus_general_v1\nname: test\nrules: []\n",
        idempotency_key=uuid.uuid4().hex,
        use_ai=False,
    )
    old = review_store.now() - timedelta(days=60)
    review_store.update_review(
        stored["review_id"],
        status="done",
        progress=100,
        completed_at=old,
    )
    response = client.post(
        "/api/v1/maintenance/cleanup?days=30",
        headers=admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["dry_run"] is True
    assert any(
        item["document_id"] == document_id
        for item in response.json()["candidates"]
    )
    assert review_store.get_document(document_id) is not None
    review_store.purge_documents([document_id])


def test_documents_can_be_listed_for_sample_selection(tmp_path: Path):
    document_id = uuid.uuid4().hex[:12]
    source = tmp_path / "sample-for-rules.pdf"
    source.write_bytes(b"%PDF-1.4\n%%EOF")
    review_store.create_document({
        "id": document_id,
        "filename": source.name,
        "file_path": str(source),
        "file_type": "pdf",
        "size": source.stat().st_size,
        "sha256": uuid.uuid4().hex * 2,
    })
    try:
        response = client.get("/api/v1/documents?limit=10")
        assert response.status_code == 200
        documents = response.json()["documents"]
        assert any(
            item["document_id"] == document_id
            and item["filename"] == source.name
            and item["size"] == source.stat().st_size
            for item in documents
        )
    finally:
        review_store.purge_documents([document_id])


def test_empty_ruleset_snapshot_yields_incomplete_not_pass():
    """历史快照/遗留数据若是空规则集，执行后不得误判 pass，应为 incomplete。"""
    sample_pdf = (
        Path(__file__).resolve().parent.parent.parent
        / "eval" / "samples" / "sample_proposal.pdf"
    )
    if not sample_pdf.exists():
        pytest.skip("sample_proposal.pdf not found")
    document_id = uuid.uuid4().hex[:12]
    review_store.create_document({
        "id": document_id,
        "filename": sample_pdf.name,
        "file_path": str(sample_pdf),
        "file_type": "pdf",
        "size": sample_pdf.stat().st_size,
        "sha256": uuid.uuid4().hex * 2,
    })
    stored = review_store.create_review(
        document_id,
        "campus_general_v1",
        ruleset_snapshot="ruleset: empty\nname: Empty\nrules: []\n",
        idempotency_key=uuid.uuid4().hex,
        use_ai=False,
    )
    try:
        routes._execute_review(stored["review_id"], document_id, "campus_general_v1")
        review = review_store.get_review(stored["review_id"])
        assert review["status"] == "done"
        assert review["conclusion"] == "incomplete"
        assert review["no_executable_rules"] is True
        assert review["review_complete"] is False
    finally:
        review_store.purge_documents([document_id])
