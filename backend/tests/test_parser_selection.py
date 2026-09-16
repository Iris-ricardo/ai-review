"""
Unit tests for parser factory function.
"""
import pytest

from app.services.parser import get_parser, PDFParser, DocxParser


class TestParserSelection:
    def test_get_pdf_parser(self):
        parser = get_parser("document.pdf")
        assert isinstance(parser, PDFParser)

    def test_get_docx_parser(self):
        parser = get_parser("document.docx")
        assert isinstance(parser, DocxParser)

    def test_get_docx_parser_uppercase(self):
        parser = get_parser("DOCUMENT.DOCX")
        assert isinstance(parser, DocxParser)

    def test_unsupported_extension(self):
        with pytest.raises(ValueError, match="Unsupported"):
            get_parser("document.txt")

    def test_no_extension(self):
        with pytest.raises(ValueError, match="Unsupported"):
            get_parser("noextension")
