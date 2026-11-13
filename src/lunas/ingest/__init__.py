"""Document ingestion: one Reader interface, three backends (SPEC.md §4a)."""

from lunas.ingest.base import Reader
from lunas.ingest.router import choose_backend, get_reader, has_text_layer

__all__ = ["Reader", "choose_backend", "get_reader", "has_text_layer"]
