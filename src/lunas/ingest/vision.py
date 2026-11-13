from pathlib import Path

from lunas.models import ParsedDocument


class VisionReader:
    """Vision LLM that transcribes pages to text and tables. It does not fill Invoice fields,
    so extraction stays one node evaluated the same way for every backend.
    Planned for week 2 (SPEC.md §12)."""

    def read(self, path: Path) -> ParsedDocument:
        raise NotImplementedError("Vision backend is not implemented yet")
