from pathlib import Path

from lunas.models import ParsedDocument


class OcrReader:
    """Local OCR for scans and images. Planned for week 2 (SPEC.md §12)."""

    def read(self, path: Path) -> ParsedDocument:
        raise NotImplementedError("OCR backend is not implemented yet")
