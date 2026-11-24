from pathlib import Path

import pytest

from lunas.ingest.recorded import DocumentMissingError, DocumentRecorder
from lunas.models import Page, ParsedDocument


def test_records_then_replays_without_the_file(tmp_path: Path) -> None:
    scan = tmp_path / "missing" / "inv-001.pdf"  # never opened
    document = ParsedDocument(pages=[Page(number=1, text="INV-1")], backend="ocr", confidence=0.9)
    recorder = DocumentRecorder(tmp_path / "docs")
    assert recorder.read(scan, "ocr", lambda _: document) == document

    replay = DocumentRecorder(tmp_path / "docs", replay_only=True)
    assert replay.first_backend(scan) == "ocr"
    assert replay.read(scan, "ocr", lambda _: pytest.fail("reader ran in replay")) == document
    with pytest.raises(DocumentMissingError):
        replay.read(scan, "vision", lambda _: document)
    with pytest.raises(DocumentMissingError):
        replay.first_backend(tmp_path / "inv-002.pdf")
