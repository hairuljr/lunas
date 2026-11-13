from pathlib import Path

import pdfplumber

from lunas.config import IngestMode
from lunas.ingest.base import Reader
from lunas.ingest.ocr import OcrReader
from lunas.ingest.pdf_text import PdfTextReader
from lunas.ingest.vision import VisionReader
from lunas.models import Backend

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}

_READERS: dict[Backend, type[Reader]] = {
    "pdf_text": PdfTextReader,
    "ocr": OcrReader,
    "vision": VisionReader,
}


def has_text_layer(path: Path, min_chars_per_page: int = 50) -> bool:
    """True only if every page has enough extractable text. One scanned page sends the
    whole document to OCR, so no page is silently read as empty."""
    with pdfplumber.open(path) as pdf:
        if not pdf.pages:
            return False
        return all(
            len((page.extract_text() or "").strip()) >= min_chars_per_page for page in pdf.pages
        )


def choose_backend(path: Path, mode: IngestMode = "auto", min_chars_per_page: int = 50) -> Backend:
    """Pick the first backend for a document. Deterministic: no LLM decides this.

    The fallback from OCR to vision (low OCR confidence, or failed arithmetic) happens
    later in the pipeline, because it depends on the result of reading the document.
    """
    suffix = path.suffix.lower()
    is_image = suffix in IMAGE_SUFFIXES
    if not is_image and suffix != ".pdf":
        raise ValueError(f"Unsupported file type: {suffix or path.name}")

    if mode == "pdf_text" and is_image:
        raise ValueError("pdf_text backend cannot read image files")
    if mode != "auto":
        return mode

    if is_image:
        return "ocr"
    return "pdf_text" if has_text_layer(path, min_chars_per_page) else "ocr"


def get_reader(backend: Backend) -> Reader:
    return _READERS[backend]()
