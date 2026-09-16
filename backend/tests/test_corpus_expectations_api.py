"""R10：样本池期望 ↔ 正式任务结果（API 级对照）。

样本池自检（解析/规则/任务三层）只走正式代码的纯函数；本测试用真实 HTTP 审查链路（上传→排队→执行→
终态）验证同一份样本的**用户可见结果**与 `eval/corpus/output/manifest.json` 期望一致；样本池未生成时跳过。"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.main import app

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = PROJECT_ROOT / "eval" / "corpus" / "output"
MANIFEST = OUTPUT_DIR / "manifest.json"

pytestmark = pytest.mark.skipif(
    not MANIFEST.exists(),
    reason="样本池尚未生成：先运行 eval/corpus/generate_samples.py",
)

client = TestClient(app)


def _manifest_entry(filename: str) -> dict:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for entry in data.get("samples", []):
        if entry.get("file") == filename:
            return entry
    pytest.skip(f"manifest 中没有样本 {filename}")


def _run_review(filename: str, ruleset_id: str) -> dict:
    sample = OUTPUT_DIR / filename
    assert sample.exists(), f"样本文件缺失：{sample}"
    with open(sample, "rb") as handle:
        uploaded = client.post(
            "/api/v1/documents",
            files={"file": (sample.name, handle)},
        )
    assert uploaded.status_code == 200, uploaded.text
    created = client.post(
        "/api/v1/reviews",
        data={
            "document_id": uploaded.json()["document_id"],
            "ruleset_id": ruleset_id,
            "use_ai": "false",
        },
    )
    assert created.status_code == 200, created.text
    review_id = created.json()["review_id"]
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        payload = client.get(f"/api/v1/reviews/{review_id}").json()
        if payload.get("status") in {"done", "failed", "cancelled"}:
            return payload
        time.sleep(0.05)
    raise AssertionError("正式审查未在 180s 内结束")


def _manifest_rulesets() -> list[str]:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return list(data.get("rulesets") or sorted({
        entry.get("ruleset", "campus") for entry in data.get("samples", [])
    }))


def _hit_signature(review: dict) -> dict[tuple[str, str], int]:
    actual: dict[tuple[str, str], int] = {}
    for issue in review.get("issues", []):
        if issue.get("severity") in {"error", "warning"} and \
                issue.get("confidence") != "manual_required":
            key = (issue["checker"], issue["severity"])
            actual[key] = actual.get(key, 0) + 1
    return actual


def test_layout_margin_sample_matches_manifest_in_formal_pipeline():
    """命中样本：正式任务的命中集合与任务层结论必须与 manifest 一致。"""
    entry = _manifest_entry("layout_margin_hit.docx")
    review = _run_review("layout_margin_hit.docx", "campus")
    assert review["status"] == "done", review

    expected_hits: dict[tuple[str, str], int] = {}
    for item in entry.get("expected", []):
        key = (item["checker"], item["severity"])
        expected_hits[key] = expected_hits.get(key, 0) + 1
    assert _hit_signature(review) == expected_hits

    outcome = entry.get("expected_outcome", {})
    assert review["conclusion"] == outcome["conclusion"]
    assert review["review_complete"] is outcome["review_complete"]


def test_clean_sample_keeps_manual_items_and_never_passes():
    """干净样本：正式任务不得判 pass，且必须保留人工确认项。"""
    entry = _manifest_entry("campus_clean.docx")
    review = _run_review("campus_clean.docx", "campus")
    assert review["status"] == "done", review
    assert review["conclusion"] != "pass"
    assert review["review_complete"] is False
    assert review["manual_required_checks"] >= entry.get("expected_manual_min", 1)
    assert review["errors"] == 0  # 干净样本无确定性错误


@pytest.mark.parametrize("ruleset", [
    ruleset for ruleset in ("dachuang", "nsfc")
])
def test_extension_rulesets_clean_sample_matches_manifest(ruleset: str):
    """dachuang/nsfc 扩展：干净样本在正式流程中同样 0 错误、结论 incomplete。"""
    if ruleset not in _manifest_rulesets():
        pytest.skip(f"manifest 未包含规则集 {ruleset}（用 --ruleset all 生成）")
    entry = _manifest_entry(f"{ruleset}_clean.docx")
    review = _run_review(f"{ruleset}_clean.docx", ruleset)
    assert review["status"] == "done", review
    assert _hit_signature(review) == {}, f"{ruleset} 干净样本不应有确定性命中"
    assert review["errors"] == 0
    assert review["conclusion"] != "pass"
    assert review["review_complete"] is False
    assert review["manual_required_checks"] >= entry.get("expected_manual_min", 1)
