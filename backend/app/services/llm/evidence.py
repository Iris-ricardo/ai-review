from __future__ import annotations

from difflib import SequenceMatcher
from typing import Iterable, Optional

from app.schemas.ir import Block, DocumentIR


def verify(
    evidence: str,
    ir: DocumentIR,
    allowed_blocks: Iterable[Block] | None = None,
) -> Optional[Block]:
    normalized_evidence = _normalize(evidence)
    if not normalized_evidence:
        return None

    best_block: Block | None = None
    best_ratio = 0.0
    candidates = list(allowed_blocks) if allowed_blocks is not None else ir.blocks
    for block in candidates:
        normalized_text = _normalize(block.text)
        if not normalized_text:
            continue
        if normalized_evidence in normalized_text:
            return block
        ratio = SequenceMatcher(None, normalized_evidence, normalized_text).ratio()
        if ratio >= 0.85 and ratio > best_ratio:
            best_ratio = ratio
            best_block = block
    return best_block


def _normalize(text: str) -> str:
    return "".join(ch for ch in text if not ch.isspace())
