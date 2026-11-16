"""OpenTelemetry spans. The audit log arrives with gapura (SPEC §11)."""

from lunas.telemetry.tracing import (
    JsonlSpanExporter,
    capture_content,
    configure_tracing,
    mark_error,
    set_attributes,
    shutdown_tracing,
    span,
)

__all__ = [
    "JsonlSpanExporter",
    "capture_content",
    "configure_tracing",
    "mark_error",
    "set_attributes",
    "shutdown_tracing",
    "span",
]
