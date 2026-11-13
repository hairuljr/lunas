from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from lunas.ingest import choose_backend, get_reader

LINES = [
    "INVOICE INV-001",
    "PT Sumber Makmur",
    "Kertas A4   10 x 50.000 = 500.000",
    "Total 500.000",
]


@pytest.fixture
def digital_pdf(tmp_path: Path) -> Path:
    path = tmp_path / "digital.pdf"
    pdf = canvas.Canvas(str(path))
    for offset, line in enumerate(LINES):
        pdf.drawString(72, 760 - 20 * offset, line)
    pdf.save()
    return path


@pytest.fixture
def blank_pdf(tmp_path: Path) -> Path:
    """Stands in for a scan: a page with no text layer."""
    path = tmp_path / "scan.pdf"
    pdf = canvas.Canvas(str(path))
    pdf.rect(72, 600, 200, 100)
    pdf.save()
    return path


def test_auto_picks_pdf_text_for_digital_pdf(digital_pdf: Path):
    assert choose_backend(digital_pdf) == "pdf_text"


def test_auto_picks_ocr_for_pdf_without_text_layer(blank_pdf: Path):
    assert choose_backend(blank_pdf) == "ocr"


def test_auto_picks_ocr_for_images(tmp_path: Path):
    assert choose_backend(tmp_path / "invoice.PNG") == "ocr"


def test_explicit_mode_is_respected(digital_pdf: Path):
    assert choose_backend(digital_pdf, mode="vision") == "vision"


def test_pdf_text_mode_rejects_images(tmp_path: Path):
    with pytest.raises(ValueError, match="cannot read image"):
        choose_backend(tmp_path / "invoice.jpg", mode="pdf_text")


def test_unsupported_file_type_is_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="Unsupported file type"):
        choose_backend(tmp_path / "invoice.docx")


def test_pdf_text_reader_extracts_text(digital_pdf: Path):
    document = get_reader("pdf_text").read(digital_pdf)

    assert document.backend == "pdf_text"
    assert len(document.pages) == 1
    assert "INV-001" in document.text
    assert "PT Sumber Makmur" in document.text
