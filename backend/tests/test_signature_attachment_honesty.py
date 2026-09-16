"""R03 回归测试：签字盖章/附件不得因“存在任意图片”而自动通过。

原缺陷：SignaturePageChecker 在关键字齐全时，只要末两页存在任意 image 块（校徽、插图、空白图都算）就返回零问题 —— 若该规则单独启用即形成 pass。
覆盖验收矩阵：关键字齐全 + 无图片/校徽/空白图片/普通插图/关键字缺失/末页扫描未完整读取，并区分“确定性缺失”与 manual_required。"""
from __future__ import annotations

import pytest

from app.api.routes import conclude_review
from app.schemas.ir import (
    PAGE_STATUS_OK,
    PAGE_STATUS_SCANNED_MINIMAL_TEXT,
    BBox,
    Block,
    DocMeta,
    DocumentIR,
    PageMeta,
)
from app.services.rules.checkers_meta import (
    AttachmentChecklistChecker,
    SignaturePageChecker,
)
from app.services.rules.rule_schema import RuleDef


def _ir(pages: int = 2, last_page_status: str = PAGE_STATUS_OK,
        last_page_text: str = "") -> DocumentIR:
    meta = DocMeta(
        pages=pages,
        page_meta=[
            PageMeta(page_number=i + 1, width=595, height=842,
                     status=(last_page_status if i + 1 == pages else PAGE_STATUS_OK))
            for i in range(pages)
        ],
    )
    blocks = [
        Block(id="b1", page=1, type="paragraph", text="正文内容"),
    ]
    if last_page_text:
        blocks.append(Block(id="b2", page=pages, type="paragraph", text=last_page_text))
    return DocumentIR(meta=meta, blocks=blocks)


def _add_image(ir: DocumentIR, page: int, block_id: str = "img1") -> None:
    ir.blocks.append(Block(
        id=block_id, page=page, type="image",
        bbox=BBox(x0=100, y0=600, x1=150, y1=650),
    ))


SIG_RULE = RuleDef(id="C011", type="signature_page", severity="error",
                   params={"keywords": ["签字", "盖章"]})


def _check(ir: DocumentIR, checker=None):
    return (checker or SignaturePageChecker()).check(SIG_RULE, ir)


def _sig_text() -> str:
    return "签字：张三    盖章：华南理工大学"


# ── 关键字齐全：有图片也必须保留人工确认项 ────────────────

def test_keywords_without_image_keeps_manual_required():
    ir = _ir(last_page_text=_sig_text())
    issues = _check(ir)
    assert len(issues) == 1
    assert issues[0].confidence == "manual_required"
    assert issues[0].severity == "info"


@pytest.mark.parametrize("label", ["校徽", "空白图片", "普通插图"])
def test_keywords_with_any_image_still_manual_required(label):
    """任意图片（含校徽/空白图/插图）都不能证明签章真实 → 不得零问题。"""
    ir = _ir(last_page_text=_sig_text())
    _add_image(ir, page=2)
    issues = _check(ir)
    assert issues, f"{label}：有图片时也必须返回人工确认项"
    assert all(issue.confidence == "manual_required" for issue in issues)
    # 不能因此判 pass
    assert conclude_review(
        no_executable_rules=False, incomplete_rules=0,
        manual_required=len(issues), errors=0,
    ) == "incomplete"


# ── 关键字缺失：确定性缺失 vs 未读取 ─────────────────────

def test_missing_keywords_on_readable_page_is_deterministic():
    ir = _ir(last_page_text="本页只有学院意见栏")
    issues = _check(ir)
    assert len(issues) == 1
    assert issues[0].severity == "error"            # 规则严重级
    assert issues[0].confidence == "deterministic"  # 可确定
    assert "缺少签字盖章关键字" in issues[0].message


def test_missing_keywords_when_last_page_unread_is_manual():
    """末页未读出时不得断言“关键字缺失”。"""
    ir = _ir(last_page_status=PAGE_STATUS_SCANNED_MINIMAL_TEXT)
    issues = _check(ir)
    assert len(issues) == 1
    assert issues[0].confidence == "manual_required"
    assert issues[0].severity == "info"
    assert "未完整读取" in issues[0].message


# ── 附件清单同类边界 ─────────────────────────────────────

ATT_RULE = RuleDef(id="C010", type="attachment_checklist", severity="info",
                   params={"expected": ["伦理审查证明"]})


def test_attachment_found_only_claims_manual_confirmation():
    ir = _ir(last_page_text="附件：伦理审查证明")
    issues = AttachmentChecklistChecker().check(ATT_RULE, ir)
    assert len(issues) == 1
    assert issues[0].confidence == "manual_required"
    assert "无法证明附件文件真实存在" in issues[0].message


def test_attachment_missing_on_readable_document_is_deterministic():
    ir = _ir(last_page_text="附件：无")
    issues = AttachmentChecklistChecker().check(ATT_RULE, ir)
    assert len(issues) == 1
    assert issues[0].confidence == "deterministic"


def test_attachment_missing_with_unread_page_is_manual():
    ir = _ir(last_page_status=PAGE_STATUS_SCANNED_MINIMAL_TEXT)
    issues = AttachmentChecklistChecker().check(ATT_RULE, ir)
    assert len(issues) == 1
    assert issues[0].confidence == "manual_required"
    assert "未完整读取" in issues[0].message


# ── 正式流程：关键字 + 任意图片不得判定通过 ────────────────

def _build_signature_pdf(path, with_logo: bool = True) -> str:
    import base64

    import fitz

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
        "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    doc = fitz.open()
    first = doc.new_page()
    first.insert_textbox(
        fitz.Rect(40, 40, 555, 700),
        "本项目围绕城市交通流预测方法开展研究，包含数据采集、模型训练与验证。" * 8,
        fontname="china-s", fontsize=11,
    )
    last = doc.new_page()
    last.insert_textbox(
        fitz.Rect(40, 40, 555, 200), "签字：张三    盖章：华南理工大学",
        fontname="china-s", fontsize=11,
    )
    if with_logo:
        last.insert_image(fitz.Rect(60, 400, 160, 500), stream=png)
    doc.save(path)
    doc.close()
    return str(path)


def test_signature_with_arbitrary_image_does_not_pass_review(tmp_path):
    """存在任意图片时，正式审查结论不得为 pass。"""
    import time

    from starlette.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    pdf_path = _build_signature_pdf(tmp_path / "signature_logo.pdf")
    with open(pdf_path, "rb") as handle:
        uploaded = client.post(
            "/api/v1/documents",
            files={"file": ("signature_logo.pdf", handle, "application/pdf")},
        )
    assert uploaded.status_code == 200, uploaded.text
    created = client.post(
        "/api/v1/reviews",
        data={
            "document_id": uploaded.json()["document_id"],
            "ruleset_id": "campus",
            "use_ai": "false",
        },
    )
    assert created.status_code == 200, created.text
    review_id = created.json()["review_id"]
    deadline = time.monotonic() + 60
    payload = None
    while time.monotonic() < deadline:
        payload = client.get(f"/api/v1/reviews/{review_id}").json()
        if payload.get("status") in {"done", "failed", "cancelled"}:
            break
        time.sleep(0.05)
    assert payload is not None and payload["status"] == "done", payload
    assert payload["conclusion"] != "pass"
    assert payload["review_complete"] is False
    manual = [
        issue for issue in payload["issues"]
        if issue.get("checker") == "signature_page"
        and issue.get("confidence") == "manual_required"
    ]
    assert manual, "存在图片时仍必须保留人工确认项"

