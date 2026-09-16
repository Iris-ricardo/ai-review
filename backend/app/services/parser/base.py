"""
Abstract base class for document parsers.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from app.schemas.ir import DocumentIR


class BaseParser(ABC):
    """All document parsers must implement ``parse`` → ``DocumentIR``."""

    @abstractmethod
    def parse(self, file_path: str | Path) -> DocumentIR:
        """Parse a document file and return the unified IR."""
        ...
