from __future__ import annotations

import sys
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.schemas.ir import Block, DocumentIR
from app.services.llm.evidence import verify


def _ir_with_texts(texts: list[str]) -> DocumentIR:
    return DocumentIR(
        blocks=[
            Block(id=f"b{i}", page=i + 1, type="paragraph", text=text)
            for i, text in enumerate(texts)
        ]
    )


def test_verify_exact_match():
    ir = _ir_with_texts(["项目名称：智慧交通研究"])

    block = verify("智慧交通研究", ir)

    assert block is not None
    assert block.id == "b0"


def test_verify_match_with_whitespace_difference():
    ir = _ir_with_texts(["申请人\n张三 来自 华南理工大学"])

    block = verify("申请人张三来自华南理工大学", ir)

    assert block is not None
    assert block.id == "b0"


def test_verify_fuzzy_match_at_boundary():
    block_text = "abcdefghijklmnopqrst"
    evidence = "abcdefghijklmnopqXYZ"
    assert SequenceMatcher(None, evidence, block_text).ratio() == 0.85
    ir = _ir_with_texts([block_text])

    block = verify(evidence, ir)

    assert block is not None
    assert block.id == "b0"


def test_verify_returns_none_when_no_match():
    ir = _ir_with_texts(["研究内容与技术路线"])

    block = verify("不存在的伪造证据", ir)

    assert block is None
