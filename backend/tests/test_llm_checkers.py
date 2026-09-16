from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.schemas.ir import BBox, Block, DocumentIR
from app.services.llm import checkers as llm_checkers
from app.services.llm.checkers import (
    AnonymityCheckChecker,
    CrossConsistencySemanticChecker,
    ReferenceFormatChecker,
    TitleContentMatchChecker,
)
from app.services.llm.client import LLMFormatError, LLMUnavailableError
from app.services.rules.rule_schema import RuleDef


def _block(block_id: str, page: int, block_type: str, text: str, level: int = 0) -> Block:
    y = float(page * 10 + int(block_id[1:]))
    return Block(
        id=block_id,
        page=page,
        type=block_type,
        level=level,
        text=text,
        bbox=BBox(x0=10, y0=y, x1=200, y1=y + 8),
    )


def _rule(rule_id: str, rule_type: str, severity: str = "warning", **params) -> RuleDef:
    return RuleDef(id=rule_id, type=rule_type, severity=severity, params=params)


def _mock_call(monkeypatch, evidence: str):
    def fake_call_json(system: str, user: str) -> dict:
        return {
            "issues": [{
                "severity": "warning",
                "evidence": evidence,
                "message": "mock issue",
                "suggestion": "mock suggestion",
            }]
        }

    monkeypatch.setattr(llm_checkers, "call_json", fake_call_json)


def _mock_call_error(monkeypatch, error: Exception):
    def fake_call_json(system: str, user: str) -> dict:
        raise error

    monkeypatch.setattr(llm_checkers, "call_json", fake_call_json)


def test_anonymity_check_accepts_verified_evidence(monkeypatch):
    _mock_call(monkeypatch, "申请人张三")
    ir = DocumentIR(blocks=[
        _block("b0", 1, "paragraph", "封面"),
        _block("b1", 2, "paragraph", "申请人张三，来自华南理工大学"),
    ])

    issues = AnonymityCheckChecker().check(
        _rule("L001", "anonymity_check", "error"), ir
    )

    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert issues[0].checker == "anonymity_check"
    assert issues[0].layer == "llm"
    assert issues[0].block_id == "b1"


def test_anonymity_check_discards_fake_evidence(monkeypatch):
    _mock_call(monkeypatch, "不存在的申请人李四")
    ir = DocumentIR(blocks=[_block("b1", 2, "paragraph", "申请人张三，来自华南理工大学")])

    issues = AnonymityCheckChecker().check(_rule("L001", "anonymity_check"), ir)

    assert issues == []


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (LLMUnavailableError("LLM_API_KEY is not configured"), "AI 检查未执行（未配置 LLM_API_KEY）"),
        (LLMFormatError("bad json"), "AI 检查未执行（返回格式错误）"),
    ],
)
def test_anonymity_check_degrades_with_specific_messages(monkeypatch, caplog, error, message):
    _mock_call_error(monkeypatch, error)
    ir = DocumentIR(blocks=[_block("b1", 2, "paragraph", "正文段落")])

    issues = AnonymityCheckChecker().check(_rule("L001", "anonymity_check"), ir)

    assert len(issues) == 1
    assert issues[0].message == message
    assert any(record.exc_info for record in caplog.records)


def test_anonymity_check_degrades_call_failure_with_exception_details(monkeypatch, caplog):
    root = TimeoutError("connect timed out")
    error = LLMUnavailableError("LLM service unavailable")
    error.__cause__ = root
    _mock_call_error(monkeypatch, error)
    ir = DocumentIR(blocks=[_block("b1", 2, "paragraph", "正文段落")])

    issues = AnonymityCheckChecker().check(_rule("L001", "anonymity_check"), ir)

    assert len(issues) == 1
    assert issues[0].message == "AI 检查未执行（调用失败：TimeoutError: connect timed out）"
    assert any(record.exc_info for record in caplog.records)


def test_title_content_match_accepts_verified_evidence(monkeypatch):
    _mock_call(monkeypatch, "本节通篇介绍背景")
    ir = DocumentIR(blocks=[
        _block("b0", 1, "heading", "研究内容", level=1),
        _block("b1", 1, "paragraph", "本节通篇介绍背景，与研究内容明显不符。"),
    ])

    issues = TitleContentMatchChecker().check(_rule("L002", "title_content_match"), ir)

    assert len(issues) == 1
    assert issues[0].severity == "warning"
    assert issues[0].checker == "title_content_match"
    assert issues[0].block_id == "b1"


def test_title_content_match_discards_fake_evidence(monkeypatch):
    _mock_call(monkeypatch, "完全伪造的章节内容")
    ir = DocumentIR(blocks=[
        _block("b0", 1, "heading", "研究内容", level=1),
        _block("b1", 1, "paragraph", "本节通篇介绍背景，与研究内容明显不符。"),
    ])

    issues = TitleContentMatchChecker().check(_rule("L002", "title_content_match"), ir)

    assert issues == []


def test_title_content_match_batches_multiple_sections(monkeypatch):
    calls = []

    def fake_call_json(system: str, user: str) -> dict:
        calls.append(user)
        return {"issues": []}

    monkeypatch.setattr(llm_checkers, "call_json", fake_call_json)
    ir = DocumentIR(blocks=[
        _block("b0", 1, "heading", "研究背景", level=1),
        _block("b1", 1, "paragraph", "背景正文"),
        _block("b2", 2, "heading", "研究内容", level=1),
        _block("b3", 2, "paragraph", "内容正文"),
    ])

    issues = TitleContentMatchChecker().check(
        _rule("L002", "title_content_match"), ir
    )

    assert issues == []
    assert len(calls) == 1
    assert "研究背景" in calls[0]
    assert "研究内容" in calls[0]


def test_cross_consistency_semantic_accepts_verified_evidence(monkeypatch):
    _mock_call(monkeypatch, "经费总额为30万元。")
    ir = DocumentIR(blocks=[
        _block("b0", 1, "paragraph", "项目经费总额为25万元。"),
        _block("b1", 2, "paragraph", "经费总额为30万元。"),
    ])

    issues = CrossConsistencySemanticChecker().check(
        _rule("L003", "cross_consistency_semantic", "error"), ir
    )

    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert issues[0].checker == "cross_consistency_semantic"
    assert issues[0].block_id == "b1"


def test_cross_consistency_semantic_discards_fake_evidence(monkeypatch):
    _mock_call(monkeypatch, "经费总额为999万元。")
    ir = DocumentIR(blocks=[
        _block("b0", 1, "paragraph", "项目经费总额为25万元。"),
        _block("b1", 2, "paragraph", "经费总额为30万元。"),
    ])

    issues = CrossConsistencySemanticChecker().check(_rule("L003", "cross_consistency_semantic"), ir)

    assert issues == []


def test_reference_format_detects_clear_missing_fields_without_llm(monkeypatch):
    ir = DocumentIR(blocks=[
        _block("b0", 3, "heading", "参考文献", level=1),
        _block("b1", 3, "paragraph", "[1] 张三. 论文标题\n[2] 李四. 另一论文"),
    ])

    issues = ReferenceFormatChecker().check(_rule("L004", "reference_format"), ir)

    assert len(issues) == 2
    assert all(issue.severity == "warning" for issue in issues)
    assert all(issue.checker == "reference_format" for issue in issues)
    assert all(issue.confidence == "deterministic" for issue in issues)


def test_reference_format_passes_well_formed_entries_without_llm(monkeypatch):
    ir = DocumentIR(blocks=[
        _block("b0", 3, "heading", "参考文献", level=1),
        _block(
            "b1",
            3,
            "paragraph",
            "[1] 张三. 论文标题[J]. 学报, 2024.\n[2] 李四. 另一论文[J]. 学报, 2025.",
        ),
    ])

    issues = ReferenceFormatChecker().check(_rule("L004", "reference_format"), ir)

    assert issues == []
