"""
Document parsers — convert PDF / DOCX files into the unified DocumentIR.
"""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

# 指纹函数必须先于子模块导入定义：pdf_parser/docx_parser 会引用它们。
_PARSER_FILES = (
    "pdf_parser.py",
    "docx_parser.py",
    "page_quality.py",
    "base.py",
    "__init__.py",
)


# 影响解析结果的运行时配置：任一项变化都必须让缓存 IR 与任务复用键失效（R11）。
# 否则「关闭 OCR 时解析出的无文本结果」在开启 OCR 后仍会被当成有效缓存复用。
_PARSE_CONFIG_KEYS = (
    "OCR_ENABLED",
    "OCR_LANGUAGE",
    "OCR_DPI",
    "OCR_MIN_PAGE_CHARS",
    "OCR_MAX_PAGES",
    "TESSDATA_PREFIX",
    "SOFFICE_PATH",
)


@lru_cache(maxsize=1)
def _parser_code_fingerprint() -> str:
    """解析器代码指纹：本包文件（含 IR 读写端）变化时旧缓存失效，仅哈希本包以免无关改动触发重解析。"""
    digest = hashlib.sha256()
    parser_dir = Path(__file__).resolve().parent
    for name in sorted(_PARSER_FILES):
        digest.update(name.encode("utf-8"))
        digest.update((parser_dir / name).read_bytes())
    return digest.hexdigest()[:16]


def parse_config_fingerprint() -> str:
    """解析相关运行时配置指纹；每次调用重新读取当前配置（不缓存），配置改动立即生效。"""
    from app.core.config import get_settings

    settings = get_settings()
    payload = "|".join(
        f"{key}={getattr(settings, key, '')!r}" for key in _PARSE_CONFIG_KEYS
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def parser_fingerprint() -> str:
    """完整解析指纹 = 解析器代码指纹 + 解析运行时配置指纹。"""
    return f"{_parser_code_fingerprint()}-{parse_config_fingerprint()}"


def cached_ir_is_current(ir) -> bool:
    """判断缓存的 IR 是否仍可复用：结构版本与解析器指纹都要匹配。
    旧版本/旧解析器产生的缓存返回 False → 调用方应重新解析。
    """
    from app.schemas.ir import IR_SCHEMA_VERSION

    meta = getattr(ir, "meta", None)
    if meta is None:
        return False
    return (
        meta.schema_version == IR_SCHEMA_VERSION
        and meta.parser_fingerprint == parser_fingerprint()
    )


from .base import BaseParser  # noqa: E402
from .pdf_parser import PDFParser  # noqa: E402
from .docx_parser import DocxParser  # noqa: E402

__all__ = [
    "BaseParser",
    "PDFParser",
    "DocxParser",
    "cached_ir_is_current",
    "parse_config_fingerprint",
    "parser_fingerprint",
]


def get_parser(filename: str) -> BaseParser:
    """Return a parser instance for the given file based on its extension."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext == "pdf":
        return PDFParser()
    elif ext == "docx":
        return DocxParser()
    else:
        raise ValueError(f"Unsupported file extension: .{ext}")
