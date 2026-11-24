"""Parsed documents, recorded so the eval gate can replay a scan without the scan or OCR.

OCR runs on ONNX, whose output may differ between the arm64 machine the recordings are made
on and the x86 CI runner, and it needs the scans, which are generated and not committed.
Recording the ParsedDocument each reader returned lets the gate check everything after the
read (extraction replies, grounding, the second-read rules, checks, decision) on the text
it was measured on. It does not check the readers themselves: the transcription eval
(evals/node/transcription.py) measures those.
"""

from collections.abc import Callable
from pathlib import Path

from lunas.models import Backend, ParsedDocument


class DocumentMissingError(FileNotFoundError):
    """Replay-only was asked for a document read that was never recorded."""


class DocumentRecorder:
    """Records every read under `directory` as `<file stem>.<backend>.json`.
    With `replay_only`, reads come from there and no reader runs."""

    def __init__(self, directory: Path, replay_only: bool = False) -> None:
        self.directory = directory
        self.replay_only = replay_only
        self.used: set[Path] = set()

    def _file(self, path: Path, backend: Backend) -> Path:
        return self.directory / f"{path.stem}.{backend}.json"

    def first_backend(self, path: Path) -> Backend | None:
        """In replay, the backend the first read was recorded under (vision is only ever
        second). None while recording: the router chooses then."""
        if not self.replay_only:
            return None
        for backend in ("pdf_text", "ocr"):
            if self._file(path, backend).exists():
                return backend
        raise DocumentMissingError(f"No recorded read of {path.name} in {self.directory}")

    def read(self, path: Path, backend: Backend, read: Callable[[Path], ParsedDocument]):
        file = self._file(path, backend)
        self.used.add(file)
        if self.replay_only:
            if not file.exists():
                raise DocumentMissingError(f"No recorded {backend} read of {path.name}")
            return ParsedDocument.model_validate_json(file.read_text(encoding="utf-8"))
        document = read(path)
        self.directory.mkdir(parents=True, exist_ok=True)
        file.write_text(document.model_dump_json(indent=1) + "\n", encoding="utf-8")
        return document
