from pathlib import Path
from typing import Protocol

from lunas.models import ParsedDocument


class Reader(Protocol):
    def read(self, path: Path) -> ParsedDocument: ...
