"""
Basic tests for FastAPI application health endpoint.
"""
import mimetypes
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from starlette.testclient import TestClient
from app.main import app
from app.api import routes
from app.services import review_store


class TestHealthCheck:
    def test_health_endpoint(self):
        client = TestClient(app)
        response = client.get("/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "version" in data

    def test_module_worker_uses_javascript_mime_type(self):
        assert mimetypes.guess_type("pdf.worker.min.mjs")[0] == "application/javascript"

    def test_review_submission_does_not_block_health(self, monkeypatch):
        started = threading.Event()
        release = threading.Event()
        document_id = "async-test-doc"
        document = {
            "id": document_id,
            "filename": "synthetic.pdf",
            "file_path": "unused.pdf",
            "file_type": "pdf",
            "size": 0,
            "sha256": "a" * 64,
        }
        if review_store.get_document(document_id) is None:
            review_store.create_document(document)
        routes._documents[document_id] = document

        def slow_review(review_id, doc_id, ruleset_id):
            started.set()
            release.wait(timeout=5)
            routes._reviews[review_id].update({"status": "done", "progress": 100})

        monkeypatch.setattr(routes, "_execute_review", slow_review)
        client = TestClient(app)
        try:
            submitted_at = time.monotonic()
            response = client.post(
                "/api/v1/reviews",
                data={
                    "document_id": document_id,
                    "ruleset_id": "campus",
                    "use_ai": "false",
                },
            )
            submit_elapsed = time.monotonic() - submitted_at

            assert response.status_code == 200
            assert response.json()["status"] in {"queued", "running"}
            assert submit_elapsed < 1
            assert started.wait(timeout=1)

            health_started = time.monotonic()
            health = client.get("/api/health")
            assert health.status_code == 200
            assert time.monotonic() - health_started < 1
        finally:
            release.set()
            routes._documents.pop(document_id, None)
            review_store.purge_documents([document_id])


class TestRulesetDetail:
    def test_get_ruleset_returns_yaml(self, tmp_path, monkeypatch):
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        yaml_text = "\n".join([
            "ruleset: campus_general_v1",
            "name: Campus",
            "rules:",
            "  - id: C001",
            "    type: page_limit",
            "    severity: error",
            "    params:",
            "      max_pages: 20",
        ])
        (rules_dir / "campus.yaml").write_text(yaml_text, encoding="utf-8")
        monkeypatch.setattr(routes.settings, "RULES_DIR", str(rules_dir))

        client = TestClient(app)
        response = client.get("/api/v1/rulesets/campus_general_v1")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "campus_general_v1"
        assert data["rule_count"] == 1
        assert data["yaml"] == yaml_text

    def test_put_ruleset_saves_yaml_and_rejects_invalid_yaml(self, tmp_path, monkeypatch):
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        monkeypatch.setattr(routes.settings, "RULES_DIR", str(rules_dir))
        # 旧管理接口自 B2 起要求管理员凭据：配置兼容 ADMIN_TOKEN 并随请求携带
        monkeypatch.setattr(routes.settings, "ADMIN_TOKEN", "test-admin-token")
        admin_headers = {
            "content-type": "text/plain",
            "X-Admin-Token": "test-admin-token",
        }
        yaml_text = "\n".join([
            "ruleset: generated_v1",
            "name: Generated",
            "rules:",
            "  - id: G001",
            "    type: page_limit",
            "    severity: error",
            "    params:",
            "      max_pages: 10",
        ])

        client = TestClient(app)
        response = client.put(
            "/api/v1/rulesets/generated_v1",
            content=yaml_text,
            headers=admin_headers,
        )

        assert response.status_code == 200
        assert (rules_dir / "generated_v1.yaml").read_text(encoding="utf-8") == yaml_text
        assert response.json()["rule_count"] == 1

        updated_yaml = yaml_text.replace("name: Generated", "name: Generated Updated")
        updated = client.put(
            "/api/v1/rulesets/generated_v1",
            content=updated_yaml,
            headers=admin_headers,
        )
        assert updated.status_code == 200
        history = list((rules_dir / ".history" / "generated_v1").glob("*.yaml"))
        assert len(history) == 1
        assert history[0].read_text(encoding="utf-8") == yaml_text

        invalid = client.put(
            "/api/v1/rulesets/bad_v1",
            content="ruleset: [",
            headers=admin_headers,
        )
        assert invalid.status_code == 422

        mismatched = client.put(
            "/api/v1/rulesets/url_id",
            content=yaml_text,
            headers=admin_headers,
        )
        assert mismatched.status_code == 422
