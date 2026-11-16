"""OpenTelemetry tracing: one trace per invoice, one span per node, one per model call.

Spans follow the OTel GenAI semantic conventions (Development status, checked against
opentelemetry-semantic-conventions 0.66b2); everything of our own lives under `app.*`.
Tracing is off unless an exporter is configured, and every span helper is then a no-op, so
the pipeline never depends on it.

    process_invoice                 root, one per invoice
    ├─ parse                        ingest backend, pages
    ├─ extract                      prompt name and version
    │  └─ chat <model>              one per model call, so the corrective retry is a second
    ├─ validate                     issue codes
    ├─ match                        issue codes
    └─ decide                       decision
"""

import json
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from importlib.metadata import version
from pathlib import Path
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from lunas.config import Settings

_NOOP = trace.NoOpTracerProvider()
_provider: TracerProvider | None = None
_capture_content = False


class JsonlSpanExporter(SpanExporter):
    """One span per line. Readable without any backend, and easy to diff or grep."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("a", encoding="utf-8")
        self._lock = threading.Lock()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        with self._lock:
            for span in spans:
                self._file.write(json.dumps(json.loads(span.to_json(indent=None))) + "\n")
            self._file.flush()
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self._file.close()


def configure_tracing(settings: Settings, exporters: Sequence[SpanExporter] = ()) -> bool:
    """Start tracing if an OTLP endpoint, a trace file or an explicit exporter is given.
    Returns whether tracing is on. Call `shutdown_tracing` before the process exits."""
    global _provider, _capture_content
    shutdown_tracing()
    batch: list[SpanExporter] = []
    if settings.otlp_endpoint:
        endpoint = settings.otlp_endpoint.rstrip("/")
        if not endpoint.endswith("/v1/traces"):
            endpoint += "/v1/traces"
        batch.append(OTLPSpanExporter(endpoint=endpoint))
    if settings.trace_file is not None:
        batch.append(JsonlSpanExporter(settings.trace_file))
    if not batch and not exporters:
        return False

    resource = Resource.create({"service.name": "lunas", "service.version": version("lunas")})
    _provider = TracerProvider(resource=resource)
    for exporter in batch:
        _provider.add_span_processor(BatchSpanProcessor(exporter))
    for exporter in exporters:  # tests: synchronous, so spans are there when the call returns
        _provider.add_span_processor(SimpleSpanProcessor(exporter))
    _capture_content = settings.trace_content
    return True


def shutdown_tracing() -> None:
    """Flush and stop. A CLI or eval that exits without this loses its last spans."""
    global _provider
    if _provider is not None:
        _provider.shutdown()
        _provider = None


def capture_content() -> bool:
    """Whether prompts and replies may be put on spans. Off by default: an invoice is
    someone else's document, and this repo only ever sends synthetic ones."""
    return _capture_content


Attributes = dict[str, Any]


@contextmanager
def span(
    name: str, attributes: Attributes | None = None, kind: SpanKind = SpanKind.INTERNAL
) -> Iterator[Span]:
    """A span that records an escaping exception as its error and re-raises it."""
    tracer = (_provider or _NOOP).get_tracer("lunas")
    with tracer.start_as_current_span(
        name, kind=kind, attributes=_present(attributes or {}), record_exception=False
    ) as current:
        try:
            yield current
        except Exception as error:
            mark_error(current, error)
            raise


def mark_error(current: Span, error: Exception) -> None:
    current.set_attribute("error.type", type(error).__name__)
    current.set_status(Status(StatusCode.ERROR, str(error)[:500]))


def set_attributes(current: Span, attributes: Attributes) -> None:
    current.set_attributes(_present(attributes))


def _present(attributes: Attributes) -> Attributes:
    """OTel rejects None values; an attribute we do not know is simply not set."""
    return {key: value for key, value in attributes.items() if value is not None}
