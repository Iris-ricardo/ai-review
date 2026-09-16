"""End-to-end API tests using FastAPI TestClient.

Full pipeline: upload -> review -> issues -> file download. Requires LibreOffice
for DOCX conversion."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "backend"))
sys.path.insert(0, str(PROJECT_ROOT))
from app.main import app
from app.api import routes
from app.core.config import get_settings

# 鈹€鈹€ Paths 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
EVAL_DIR = PROJECT_ROOT / "eval"
CLEAN_DOCX = EVAL_DIR / "samples" / "sample_proposal.docx"

pytestmark = pytest.mark.skipif(
    not CLEAN_DOCX.exists(),
    reason="sample_proposal.docx not found — run eval/generate_sample_docx.py",
)

client = TestClient(app)

LLM_FALLBACK_CHECKERS = {
    "anonymity_check",
    "title_content_match",
    "cross_consistency_semantic",
}


@pytest.fixture(autouse=True)
def disable_llm_api_key(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "")
    get_settings.cache_clear()


def _llm_fallback_issues(review: dict) -> list[dict]:
    return [
        issue for issue in review["issues"]
        if issue.get("layer") == "llm"
        and issue.get("severity") == "info"
        and "AI 检查未执行" in issue.get("message", "")
    ]


def _upload(file_path: Path) -> str:
    with open(file_path, "rb") as f:
        r = client.post("/api/v1/documents",
                        files={"file": (file_path.name, f)})
    assert r.status_code == 200, f"Upload failed: {r.text}"
    return r.json()["document_id"]


def _review(doc_id: str, ruleset: str = "campus", *, use_ai: bool = False) -> dict:
    r = client.post("/api/v1/reviews",
                    data={
                        "document_id": doc_id,
                        "ruleset_id": ruleset,
                        "use_ai": str(use_ai).lower(),
                        # R08：只要选择 AI，就必须同时给出材料外发授权
                        "privacy_consent": str(use_ai).lower(),
                    })
    assert r.status_code == 200, f"Review create failed: {r.text}"
    rev_id = r.json()["review_id"]
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        r2 = client.get(f"/api/v1/reviews/{rev_id}")
        assert r2.status_code == 200
        data = r2.json()
        if data.get("status") in {"done", "failed"}:
            return data
        time.sleep(0.05)
    raise AssertionError(f"Review {rev_id} did not finish before timeout")


def _issues(rev_id: str) -> dict:
    r = client.get(f"/api/v1/reviews/{rev_id}/issues")
    assert r.status_code == 200
    return r.json()


# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲
class TestE2EClean:
    """Clean sample has no errors but cannot fully pass without AI/manual checks."""

    @pytest.fixture(autouse=True)
    def _enable_egress_for_ai_cases(self, monkeypatch):
        """R08：这些用例验证“已授权但未配置密钥”的降级路径，需要显式打开外发开关。"""
        monkeypatch.setattr(routes.settings, "AI_EGRESS_ENABLED", True)

    def test_clean_conclusion_incomplete_without_ai_and_manual_confirmation(self):
        doc_id = _upload(CLEAN_DOCX)
        rev = _review(doc_id)
        assert rev["conclusion"] == "incomplete"

    def test_clean_zero_errors(self):
        doc_id = _upload(CLEAN_DOCX)
        rev = _review(doc_id)
        assert rev["errors"] == 0, f"Expected 0 errors, got {rev['errors']}"

    def test_clean_rule_layer_marks_manual_confirmation(self):
        doc_id = _upload(CLEAN_DOCX)
        rev = _review(doc_id)
        rule_issues = [issue for issue in rev["issues"] if issue.get("layer") == "rule"]
        manual = [
            issue for issue in rule_issues
            if issue.get("confidence") == "manual_required"
        ]
        assert {issue["checker"] for issue in manual} == {
            "page_number_check", "attachment_checklist", "signature_page"
        }

    def test_clean_has_three_llm_fallback_infos_without_key(self):
        doc_id = _upload(CLEAN_DOCX)
        rev = _review(doc_id, use_ai=True)
        fallback_issues = _llm_fallback_issues(rev)
        assert len(fallback_issues) == 3
        assert {issue["checker"] for issue in fallback_issues} == LLM_FALLBACK_CHECKERS

    def test_no_key_full_review_finishes_under_10_seconds(self):
        doc_id = _upload(CLEAN_DOCX)
        started_at = time.monotonic()
        rev = _review(doc_id, use_ai=True)
        elapsed = time.monotonic() - started_at
        assert elapsed < 10
        assert {issue["checker"] for issue in _llm_fallback_issues(rev)} == LLM_FALLBACK_CHECKERS

    def test_file_download_pdf(self):
        doc_id = _upload(CLEAN_DOCX)
        r = client.get(f"/api/v1/documents/{doc_id}/file")
        assert r.status_code == 200
        assert "pdf" in r.headers.get("content-type", "")


# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲
class TestE2ESabotage:
    """Sabotaged budget must trigger a budget_check error with page+bbox."""

    @pytest.fixture(scope="class")
    def sabotaged_review(self, tmp_path_factory):
        from eval.make_sabotage import make_sabotaged

        sabotaged_docx = tmp_path_factory.mktemp("e2e") / "sabotaged_budget.docx"
        make_sabotaged(output_path=sabotaged_docx)

        doc_id = _upload(sabotaged_docx)
        rev = _review(doc_id)
        return rev


    def test_conclusion_incomplete_but_preserves_detected_errors(self, sabotaged_review):
        assert sabotaged_review["conclusion"] == "incomplete"
        assert sabotaged_review["errors"] >= 1

    def test_budget_error_detected(self, sabotaged_review):
        budget_issues = [
            i for i in sabotaged_review["issues"]
            if i["checker"] == "budget_check" and i["severity"] == "error"
        ]
        assert len(budget_issues) >= 1, "No budget error found"

    def test_budget_error_has_page(self, sabotaged_review):
        budget_issues = [
            i for i in sabotaged_review["issues"]
            if i["checker"] == "budget_check"
        ]
        for bi in budget_issues:
            assert bi.get("page") is not None, \
                f"Budget issue missing page: {bi}"

    def test_budget_error_has_block_id(self, sabotaged_review):
        """block_id links to the budget table block 鈫?bbox available for PDF highlight."""
        budget_issues = [
            i for i in sabotaged_review["issues"]
            if i["checker"] == "budget_check"
        ]
        for bi in budget_issues:
            assert bi.get("block_id") is not None, \
                f"Budget issue missing block_id (no bbox): {bi}"

    def test_budget_error_has_bbox_4_element_array(self, sabotaged_review):
        """bbox must be a non-empty 4-element array [x0, y0, x1, y1]."""
        budget_issues = [
            i for i in sabotaged_review["issues"]
            if i["checker"] == "budget_check"
        ]
        for bi in budget_issues:
            bbox = bi.get("bbox")
            assert bbox is not None, f"Budget issue missing bbox: {bi}"
            assert isinstance(bbox, list), f"bbox not a list: {type(bbox)}"
            assert len(bbox) == 4, f"bbox not 4-element: {bbox}"
            assert all(isinstance(v, (int, float)) for v in bbox), \
                f"bbox contains non-numeric: {bbox}"
            # bbox must have non-zero area
            assert bbox[2] > bbox[0], f"bbox x1({bbox[2]}) <= x0({bbox[0]})"
            assert bbox[3] > bbox[1], f"bbox y1({bbox[3]}) <= y0({bbox[1]})"

    def test_budget_error_message_mentions_amounts(self, sabotaged_review):
        budget_issues = [
            i for i in sabotaged_review["issues"]
            if i["checker"] == "budget_check"
        ]
        for bi in budget_issues:
            assert "勾稽" in bi.get("message", ""), \
                f"Budget error message wrong: {bi.get('message')}"
            assert "30.0" in bi.get("message", ""), \
                f"Budget error should mention detail total 30.0"
            assert "25.0" in bi.get("message", ""), \
                f"Budget error should mention sabotaged total 25.0"


# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲
class TestE2ERulesets:
    """Ruleset listing endpoint."""

    def test_list_rulesets(self):
        r = client.get("/api/v1/rulesets")
        assert r.status_code == 200
        rulesets = r.json()["rulesets"]
        assert len(rulesets) >= 1
        ids = [rs["id"] for rs in rulesets]
        assert any("campus" in i for i in ids), f"campus not in {ids}"
        assert any("nsfc" in i for i in ids), f"nsfc not in {ids}"
        assert any("dachuang" in i for i in ids), f"dachuang not in {ids}"
        built_in = [
            ruleset for ruleset in rulesets
            if any(name in ruleset["id"] for name in ("campus", "nsfc", "dachuang"))
        ]
        assert all(ruleset["rule_count"] == 20 for ruleset in built_in)
