"""逐页读取质量判定（R02）：不用“该页数字字符数 > 0”证明已读取——扫描页只印一个数字页码就会被放行，
正文其实没读到。可解释信号（数字字符数、图像覆盖比例、未覆盖大图区域、OCR 结果）→ 逐页状态与原因，
供 IR 与正式流程决定“拒绝通过 / 人工确认 / 正常”。约束：含图片不判失败、不靠提高字符阈值掩盖问题、
OCR 未报错 ≠ 读出正文（字数不足按未读取）、混合页可疑不伪装、纯函数便于逐类验收。"""
from __future__ import annotations

from dataclasses import dataclass

from app.schemas.ir import (
    PAGE_STATUS_EMPTY,
    PAGE_STATUS_MIXED_IMAGE_UNREAD,
    PAGE_STATUS_OCR,
    PAGE_STATUS_OCR_FAILED,
    PAGE_STATUS_OCR_INSUFFICIENT,
    PAGE_STATUS_OCR_SKIPPED_LIMIT,
    PAGE_STATUS_OK,
    PAGE_STATUS_SCANNED_MINIMAL_TEXT,
    PAGE_STATUS_SCANNED_NO_TEXT,
)

# ── OCR 执行状态 ────────────────────────────────────
OCR_NOT_ATTEMPTED = "not_attempted"   # 本页无需 OCR
OCR_DISABLED = "disabled"             # 需 OCR 但未启用
OCR_SUCCESS = "success"               # 读出足够正文
OCR_INSUFFICIENT = "insufficient"     # 未读出有效正文
OCR_FAILED = "failed"                 # OCR 抛错
OCR_SKIPPED_LIMIT = "skipped_limit"   # 超页数上限未执行

# ── 判定阈值（可解释、可单测）───────────────────────────
# 整页扫描判定：图像覆盖 ≥ 60% 且数字文字 < 200 字。
# 正常“正文 + 校徽/插图”远低于该覆盖比例，因此不会被误判。
SCANNED_IMAGE_RATIO = 0.6
SCANNED_MAX_DIGITAL_CHARS = 200
# 可疑大图区域：单张图像覆盖 ≥ 50% 页面、区域内没有任何数字文字，
# 且整页仍有正文（≥ 200 字）→ 无法确认该区域是否需要 OCR。
UNREAD_REGION_RATIO = 0.5
UNCERTAIN_MIN_DIGITAL_CHARS = 200


@dataclass(frozen=True)
class PageSignals:
    """单页的解析信号（全部来自真实抽取结果，便于逐条断言）。"""

    page_number: int
    digital_chars: int = 0
    image_count: int = 0
    image_area_ratio: float = 0.0
    has_unread_image_region: bool = False
    ocr_state: str = OCR_NOT_ATTEMPTED
    ocr_chars: int = 0


def classify_page(
    signals: PageSignals,
    *,
    scanned_ratio: float = SCANNED_IMAGE_RATIO,
    scanned_max_chars: int = SCANNED_MAX_DIGITAL_CHARS,
    unread_region_ratio: float = UNREAD_REGION_RATIO,
    uncertain_min_chars: int = UNCERTAIN_MIN_DIGITAL_CHARS,
    ocr_min_chars: int = 1,
) -> tuple[str, str]:
    """返回 (status, reason)；status 取值见 app.schemas.ir 常量。
    ocr_min_chars：OCR 结果被视为“读出正文”的最小字数，由调用方传入部署配置
    OCR_MIN_PAGE_CHARS（避免此处再读配置）。
    """
    if signals.ocr_state == OCR_SUCCESS:
        if signals.ocr_chars < ocr_min_chars:
            return (
                PAGE_STATUS_OCR_INSUFFICIENT,
                f"OCR 仅读出 {signals.ocr_chars} 字（少于 {ocr_min_chars} 字），"
                "未能确认正文已被读取",
            )
        return PAGE_STATUS_OCR, f"本页使用 OCR 提取（{signals.ocr_chars} 字）"

    if signals.ocr_state == OCR_INSUFFICIENT:
        return (
            PAGE_STATUS_OCR_INSUFFICIENT,
            f"OCR 执行后仅得到 {signals.ocr_chars} 字，未读出有效正文",
        )

    if signals.ocr_state == OCR_FAILED:
        return PAGE_STATUS_OCR_FAILED, "OCR 尝试失败（缺少识别运行时或语言包）"

    if signals.ocr_state == OCR_SKIPPED_LIMIT:
        return (
            PAGE_STATUS_OCR_SKIPPED_LIMIT,
            "达到单文档 OCR 页数上限，本页未执行 OCR",
        )

    if signals.image_count == 0 and signals.digital_chars == 0:
        return PAGE_STATUS_EMPTY, "正常空白页（非失败）"

    # 只有图像、没有任何数字文字：该页内容未被读取（与旧版行为一致）
    if signals.image_count > 0 and signals.digital_chars == 0:
        return (
            PAGE_STATUS_SCANNED_NO_TEXT,
            "页面含图像但无数字文字，且未被 OCR 覆盖（读取不完整）",
        )

    # 图像占主体、仅有极少量数字文字（典型：整页扫描 + 数字页码/页眉）
    if (
        signals.image_area_ratio >= scanned_ratio
        and signals.digital_chars < scanned_max_chars
    ):
        return (
            PAGE_STATUS_SCANNED_MINIMAL_TEXT,
            f"页面主体为图像（覆盖 {signals.image_area_ratio:.0%}），"
            f"仅有 {signals.digital_chars} 个数字字符（如扫描页页码），正文未被读取",
        )

    if (
        signals.has_unread_image_region
        and signals.image_area_ratio >= unread_region_ratio
        and signals.digital_chars >= uncertain_min_chars
    ):
        return (
            PAGE_STATUS_MIXED_IMAGE_UNREAD,
            "页面存在大块图像区域且区域内无数字文字，无法确认该区域内容是否已读取",
        )

    note = ""
    if signals.image_count:
        note = f"（含 {signals.image_count} 个图像块，覆盖 {signals.image_area_ratio:.0%}）"
    return PAGE_STATUS_OK, f"数字文本页{note}"
