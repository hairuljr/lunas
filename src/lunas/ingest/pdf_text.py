from pathlib import Path

import pdfplumber

from lunas.models import Page, ParsedDocument


class PdfTextReader:
    """Reads the text layer and tables of a digital PDF. Free and fast; useless on scans."""

    def read(self, path: Path) -> ParsedDocument:
        with pdfplumber.open(path) as pdf:
            pages = [
                Page(
                    number=number,
                    text=page.extract_text() or "",
                    tables=page.extract_tables(),
                )
                for number, page in enumerate(pdf.pages, start=1)
            ]
        return ParsedDocument(pages=pages, backend="pdf_text", confidence=1.0)
