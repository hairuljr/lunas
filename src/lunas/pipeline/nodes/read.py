"""Steps 1-3b: parse, extract, ground, and read a doubtful scan a second time.

An OCR read is read again by the vision model (once) when its weakest line scores below
`ocr_min_confidence` or its extraction fails the grounding check. Neither reader is
trusted over the other: where the first read's value was grounded in its own text and the
second read grounds a different value, the readers disagree and the invoice is held for a
person (`read_disagreement`). Otherwise the second read is used, with its own grounding.

Arithmetic failures are not a trigger: grounding already catches a misread amount (the
value the extraction returns is not printed), and an invoice whose printed numbers do not
add up is a vendor problem that a second reader would only confirm.
"""

import functools
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from lunas.config import Settings
from lunas.gapura.scanner import Finding, scan_document, screening_issues
from lunas.ingest import choose_backend, get_reader
from lunas.ingest.email import document_path, read_with
from lunas.ingest.recorded import DocumentRecorder
from lunas.ingest.vision import VisionReader
from lunas.llm import ChatClient, LLMRequestError, StructuredOutputError
from lunas.models import Backend, Invoice, Issue, ParsedDocument
from lunas.pipeline.checks import codes
from lunas.pipeline.nodes.extract import extract_invoice
from lunas.pipeline.nodes.grounding import ground_invoice
from lunas.telemetry import set_attributes, span

READ_DISAGREEMENT = "read_disagreement"
CRITICAL = ("invoice_number", "po_number", "subtotal", "tax", "total", "bank_account")


@dataclass
class Read:
    backend: Backend
    invoice: Invoice
    grounding: list[Issue]
    # The scanner's verdict on the raw text (`gapura.scanner`): empty, or one
    # document_flagged issue. Raw text stays here; only the issue goes on.
    screening: list[Issue] = field(default_factory=list)


def _normal(field: str, value: object) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return str(value.normalize())
    if field == "bank_account":
        return re.sub(r"\D", "", str(value))
    return re.sub(r"\s+", "", str(value)).upper()


def _ungrounded(grounding: list[Issue]) -> set[str]:
    """The fields the grounding check flagged (named in its messages)."""
    flagged = {field for field in CRITICAL for issue in grounding if f" {field} " in issue.message}
    if any(issue.code == "bank_account_not_extracted" for issue in grounding):
        flagged.add("bank_account")
    return flagged


def disagreements(first: Read, second: Read) -> list[str]:
    """Fields where both reads grounded a value and the values differ. A field one read
    did not return is no disagreement: the other read's value then goes on to the checks
    (a bank account that is not the one on file is still a mismatch there)."""
    doubtful = _ungrounded(first.grounding) | _ungrounded(second.grounding)
    return [
        field
        for field in CRITICAL
        if field not in doubtful
        and _normal(field, getattr(first.invoice, field)) is not None
        and _normal(field, getattr(second.invoice, field)) is not None
        and _normal(field, getattr(first.invoice, field))
        != _normal(field, getattr(second.invoice, field))
    ]


def _extract(document: ParsedDocument, backend: Backend, client: ChatClient) -> Read:
    invoice = extract_invoice(document, client)
    with span("grounding", {"app.ingest.backend": backend}) as current:
        grounding = ground_invoice(invoice, document.text)
        set_attributes(current, {"app.issue_codes": codes(grounding)})
    return Read(backend, invoice, grounding)


def first_backend(path: Path, settings: Settings, documents: DocumentRecorder | None) -> Backend:
    recorded = documents.first_backend(path) if documents else None
    return recorded or choose_backend(
        document_path(path), settings.ingest_mode, settings.min_chars_per_page
    )


def _parse(
    path: Path,
    backend: Backend,
    vision_client: ChatClient | None,
    documents: DocumentRecorder | None,
) -> ParsedDocument:
    reader = VisionReader(vision_client) if backend == "vision" else get_reader(backend)
    with span("parse") as current:
        read = functools.partial(read_with, reader)
        document = documents.read(path, backend, read) if documents else read(path)
        set_attributes(
            current,
            {
                "app.ingest.backend": backend,
                "app.ingest.pages": len(document.pages),
                "app.ingest.characters": len(document.text),
                "app.ingest.confidence": document.confidence,
            },
        )
    return document


def _screen(document: ParsedDocument, path: Path, known_emails: frozenset[str]) -> list[Finding]:
    # A replayed document may have no file behind it: then only its text is scanned.
    source = document_path(path) if path.exists() else None
    with span("screen") as current:
        findings = scan_document(document.text, source, known_emails)
        set_attributes(current, {"app.screen.findings": [f.check for f in findings]})
    return findings


def read_invoice(
    path: Path,
    client: ChatClient,
    vision_client: ChatClient | None,
    settings: Settings,
    documents: DocumentRecorder | None = None,
    known_emails: frozenset[str] = frozenset(),
) -> Read:
    """Raises StructuredOutputError or LLMRequestError if the first read cannot be
    extracted. A failed second read leaves the first read as it was. With `documents`,
    every read is recorded there, or replayed from there (`ingest.recorded`)."""
    backend = first_backend(path, settings, documents)
    document = _parse(path, backend, vision_client, documents)
    findings = _screen(document, path, known_emails)
    first = _extract(document, backend, client)
    first.screening = screening_issues(findings)
    doubtful = (document.confidence or 0.0) < settings.ocr_min_confidence or first.grounding
    if backend != "ocr" or vision_client is None or not doubtful:
        return first

    with span("second_read") as current:
        try:
            transcript = _parse(path, "vision", vision_client, documents)
            second = _extract(transcript, "vision", client)
            second.screening = screening_issues(
                list(dict.fromkeys(findings + _screen(transcript, path, known_emails)))
            )
        except (StructuredOutputError, LLMRequestError) as error:
            set_attributes(current, {"app.second_read.error": type(error).__name__})
            return first
        fields = disagreements(first, second)
        set_attributes(current, {"app.second_read.disagreements": fields})
    if fields:
        second.grounding = second.grounding + [
            Issue(
                code=READ_DISAGREEMENT,
                message="OCR and vision read different values for " + ", ".join(fields),
            )
        ]
    return second
