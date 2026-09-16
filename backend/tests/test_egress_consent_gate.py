"""R08 回归测试：AI 材料外发必须持有任务级授权（并在每次真实发送前复核）。

全程 mock，不产生真实网络请求；核心断言是**禁止场景下 provider 请求次数为 0**。原缺陷：仅“当前配置了
LLM_API_KEY”时才要求 privacy_consent（无密钥排队、补密钥恢复即可无授权外发）；审计在提交后才写库、执行/恢复阶段不校验授权记录。"""
from __future__ import annotations

import threading
import uuid
from pathlib import Path

import fitz
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import review_store
from app.services.llm import client as llm_client
from app.services.task_runtime import (
    authorize_egress,
    egress_is_authorized,
    task_scope,
)

client = TestClient(app)


class _ProviderSpy:
    """替换真实 provider 调用，只计数并返回可控内容。"""

    def __init__(self, responses: list[str] | None = None):
        self.calls: list[list[dict]] = []
        self.responses = responses or ['{"issues": []}']
        self._index = 0

    def __call__(self, messages: list[dict]) -> str:
        self.calls.append(messages)
        response = self.responses[min(self._index, len(self.responses) - 1)]
        self._index += 1
        return response


@pytest.fixture
def provider(monkeypatch) -> _ProviderSpy:
    spy = _ProviderSpy()
    monkeypatch.setattr(llm_client, "_post_chat", spy)
    return spy


def _sample_pdf(tmp_path) -> str:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(
        fitz.Rect(40, 40, 555, 500),
        "项目申报书\n"
        "课题名称：基于深度学习的城市交通流预测方法研究\n"
        "研究期限：2026年9月至2028年8月，共计36个月\n"
        "申报单位：计算机科学与技术学院\n"
        "课题负责人：张三\n"
        "联系电话：13800138000\n"
        "电子邮箱：zhangsan@university.edu.cn\n"
        "填表日期：2026年6月30日",
        fontname="china-s",
        fontsize=11,
    )
    body = doc.new_page()
    body.insert_textbox(
        fitz.Rect(40, 40, 555, 780),
        "一、立项依据与研究背景\n"
        + "本项目围绕城市交通流预测方法开展研究，包含数据采集、模型训练与验证。" * 6
        + "\n二、研究目标与内容\n"
        + "研究目标为提升预测精度，研究内容包括动态图建模与多尺度融合。" * 4
        + "\n三、经费预算\n本课题申请总经费30万元，其中管理费2.4万元。",
        fontname="china-s",
        fontsize=11,
    )
    path = tmp_path / "sample.pdf"
    doc.save(path)
    doc.close()
    return str(path)


def _upload(path: str) -> str:
    with open(path, "rb") as handle:
        response = client.post(
            "/api/v1/documents",
            files={"file": ("sample.pdf", handle, "application/pdf")},
        )
    assert response.status_code == 200, response.text
    return response.json()["document_id"]


def _create_review(document_id: str, *, use_ai: bool, consent: bool) -> dict:
    response = client.post(
        "/api/v1/reviews",
        data={
            "document_id": document_id,
            "ruleset_id": "campus",
            "use_ai": str(use_ai).lower(),
            "privacy_consent": str(consent).lower(),
        },
    )
    return {"status_code": response.status_code, "payload": response.json() if response.content else {}}


def _wait_review(review_id: str, timeout: float = 60.0) -> dict:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = client.get(f"/api/v1/reviews/{review_id}").json()
        if payload.get("status") in {"done", "failed", "cancelled"}:
            return payload
        time.sleep(0.05)
    raise AssertionError("review did not finish")


def _enable_egress(monkeypatch):
    """显式打开部署级外发开关（否则默认拒绝，见 config.AI_EGRESS_ENABLED=False）。"""
    from app.api import routes

    monkeypatch.setattr(routes.settings, "AI_EGRESS_ENABLED", True)
    monkeypatch.setattr(routes.settings, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(llm_client, "get_settings", lambda: routes.settings)
    return routes


def test_use_ai_without_consent_rejected_even_without_api_key(tmp_path, provider, monkeypatch):
    """未配置密钥也必须先授权（不再以“有没有密钥”决定是否要授权）。"""
    routes = _enable_egress(monkeypatch)
    monkeypatch.setattr(routes.settings, "LLM_API_KEY", "")  # 关键：没有密钥也一样要授权
    document_id = _upload(_sample_pdf(tmp_path))
    response = _create_review(document_id, use_ai=True, consent=False)
    assert response["status_code"] == 422, response
    assert provider.calls == []


def test_consent_but_egress_disabled_at_creation_is_rejected(tmp_path, provider, monkeypatch):
    """部署级禁用时，创建入口直接拒绝（不产生任何请求）。"""
    from app.api import routes

    monkeypatch.setattr(routes.settings, "AI_EGRESS_ENABLED", False)
    document_id = _upload(_sample_pdf(tmp_path))
    created = _create_review(document_id, use_ai=True, consent=True)
    assert created["status_code"] == 422, created
    assert provider.calls == []


def test_queued_task_then_egress_disabled_makes_zero_requests(tmp_path, provider, monkeypatch):
    """排队期间/执行前关闭外发 → 执行时一次都不发（不会在下一次发送时绕过）。"""
    routes = _enable_egress(monkeypatch)
    document_id = _upload(_sample_pdf(tmp_path))
    stored = review_store.create_review(
        document_id,
        "campus_general_v1",
        ruleset_snapshot=(
            Path(__file__).resolve().parents[2] / "rules" / "campus.yaml"
        ).read_text(encoding="utf-8"),
        idempotency_key=uuid.uuid4().hex,
        use_ai=True,
    )
    review_store.record_egress_consent(
        purpose="single_review",
        subject_id=stored["review_id"],
        consent_granted=True,
        ai_enabled=True,
        policy_version="test",
    )
    monkeypatch.setattr(routes.settings, "AI_EGRESS_ENABLED", False)  # 执行前关闭
    routes._submit_existing(stored["review_id"], document_id, "campus_general_v1")
    review = _wait_review(stored["review_id"])
    assert review["status"] == "done"
    assert review["conclusion"] != "pass"
    assert provider.calls == []


def test_authorized_and_enabled_path_reaches_provider(tmp_path, provider, monkeypatch):
    """允许路径：授权记录 + 部署开关打开 → AI 规则真正执行（调用被 mock 计数）。"""
    _enable_egress(monkeypatch)
    document_id = _upload(_sample_pdf(tmp_path))
    created = _create_review(document_id, use_ai=True, consent=True)
    assert created["status_code"] == 200, created
    review = _wait_review(created["payload"]["review_id"])
    assert review["status"] == "done"
    assert provider.calls, "授权且启用时应当发生外发（被 mock 计数）"


def test_historical_task_without_consent_record_makes_zero_requests(tmp_path, provider, monkeypatch):
    """历史任务（无授权记录）按保守策略：不推定同意，零外发。"""
    routes = _enable_egress(monkeypatch)
    document_id = _upload(_sample_pdf(tmp_path))
    stored = review_store.create_review(
        document_id,
        "campus_general_v1",
        ruleset_snapshot=(
            Path(__file__).resolve().parents[2] / "rules" / "campus.yaml"
        ).read_text(encoding="utf-8"),
        idempotency_key=uuid.uuid4().hex,
        use_ai=True,
    )
    routes._submit_existing(stored["review_id"], document_id, "campus_general_v1")
    review = _wait_review(stored["review_id"])
    assert review["status"] == "done"
    assert provider.calls == []
    assert review["conclusion"] != "pass"


def test_consent_audit_failure_prevents_execution(tmp_path, provider, monkeypatch):
    """授权写入失败 → 任务不执行（无外发），并明确失败。"""
    routes = _enable_egress(monkeypatch)
    document_id = _upload(_sample_pdf(tmp_path))

    def _boom(**_kwargs):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(routes.review_store, "record_egress_consent", _boom)
    response = _create_review(document_id, use_ai=True, consent=True)
    assert response["status_code"] == 500, response
    assert provider.calls == []


def test_client_denies_egress_outside_authorized_scope(provider, monkeypatch):
    """统一出口默认拒绝：无任务作用域或未授权作用域内调用都被拦下。"""
    _enable_egress(monkeypatch)
    with pytest.raises(llm_client.EgressNotAuthorizedError):
        llm_client.call_json("system", "user material")
    assert provider.calls == []

    with task_scope("t-unauthorized", threading.Event(), 10):
        assert egress_is_authorized() is False
        with pytest.raises(llm_client.EgressNotAuthorizedError):
            llm_client.call_json("system", "user material")
        assert provider.calls == []

        authorize_egress(True)
        assert egress_is_authorized() is True
        llm_client.call_json("system", "user material")
        assert len(provider.calls) == 1

        # 策略在作用域内被关闭 → 后续请求（含重试）立刻被拦
        authorize_egress(False)
        with pytest.raises(llm_client.EgressNotAuthorizedError):
            llm_client.call_json("system", "user material")
        assert len(provider.calls) == 1


def test_deployment_switch_off_blocks_retries_and_json_repair(provider, monkeypatch):
    """部署开关关闭时，重试与 JSON 修复请求同样不会发出。"""
    routes = _enable_egress(monkeypatch)
    with task_scope("t-repair", threading.Event(), 10, egress_authorized=True):
        monkeypatch.setattr(routes.settings, "AI_EGRESS_ENABLED", False)
        with pytest.raises(llm_client.LLMUnavailableError):
            llm_client.call_json("system", "material")
    assert provider.calls == []


def test_connectivity_probe_sends_no_material_and_needs_no_document_consent(
    provider, monkeypatch
):
    """连通性检查不套用文档授权身份，但受部署开关约束，且提示词由客户端硬编码。"""
    routes = _enable_egress(monkeypatch)
    monkeypatch.setattr(routes.settings, "AI_EGRESS_ENABLED", False)
    with pytest.raises(llm_client.LLMUnavailableError):
        llm_client.probe_connectivity()
    assert provider.calls == []

    monkeypatch.setattr(routes.settings, "AI_EGRESS_ENABLED", True)
    result = llm_client.probe_connectivity()
    assert isinstance(result, dict)
    assert len(provider.calls) == 1
    sent = provider.calls[0]
    assert all("material" not in message["content"] for message in sent)
    assert "连通性" in sent[0]["content"]
