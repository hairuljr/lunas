"""The week-1 slice of the workflow: parse -> extract -> validate -> match -> decide.

Plain function calls for now. The LangGraph graph with a Postgres checkpointer (SPEC §4)
arrives with the human-approval step, which is the first thing that needs to pause.
"""

from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pydantic import BaseModel

from lunas.config import Settings
from lunas.erp import ErpData
from lunas.ingest import choose_backend, get_reader
from lunas.llm import ChatClient, LLMRequestError, StructuredOutputError
from lunas.models import Backend, Decision, Invoice, Issue
from lunas.pipeline.nodes.decide import decide
from lunas.pipeline.nodes.extract import extract_invoice
from lunas.pipeline.nodes.match import match_invoice
from lunas.pipeline.nodes.validate import validate_invoice

# The model answered, but not with a valid invoice, even after the retry.
EXTRACTION_FAILED = "extraction_failed"
# The endpoint gave no answer at all. A property of the provider, not of the model.
LLM_UNAVAILABLE = "llm_unavailable"


class PipelineResult(BaseModel):
    file: str
    backend: Backend
    # None when extraction failed; the decision is then HOLD.
    invoice: Invoice | None
    issues: list[Issue]
    decision: Decision


def process_invoice(
    path: Path, erp: ErpData, client: ChatClient, settings: Settings
) -> PipelineResult:
    backend = choose_backend(path, settings.ingest_mode, settings.min_chars_per_page)
    document = get_reader(backend).read(path)

    try:
        invoice = extract_invoice(document, client)
    except (StructuredOutputError, LLMRequestError) as error:
        # An invoice that could not be read is never paid and never silently dropped, and
        # one failed request does not take the rest of the batch down with it.
        code = EXTRACTION_FAILED if isinstance(error, StructuredOutputError) else LLM_UNAVAILABLE
        return PipelineResult(
            file=path.name,
            backend=backend,
            invoice=None,
            issues=[Issue(code=code, message=str(error))],
            decision=Decision.HOLD,
        )

    issues = validate_invoice(invoice, settings.amount_tolerance)
    issues += match_invoice(invoice, erp, settings.amount_tolerance)
    result = decide(invoice, issues, settings.approval_threshold)
    return PipelineResult(
        file=path.name,
        backend=backend,
        invoice=invoice,
        issues=result.reasons,
        decision=result.decision,
    )


def process_invoices(
    paths: Iterable[Path], erp: ErpData, client: ChatClient, settings: Settings, workers: int = 4
) -> list[PipelineResult]:
    """Results come back in the order of `paths`. The work is I/O-bound LLM calls."""
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda path: process_invoice(path, erp, client, settings), paths))
