"""Steps 4 and 5: every deterministic check, in one place for the graph, the evals and the
generator tests."""

from lunas.config import Settings
from lunas.erp import ErpData
from lunas.models import Invoice, Issue
from lunas.pipeline.nodes.duplicates import find_duplicates
from lunas.pipeline.nodes.match import match_invoice
from lunas.pipeline.nodes.validate import validate_invoice
from lunas.telemetry import set_attributes, span

# The model answered, but not with a valid invoice, even after the retry.
EXTRACTION_FAILED = "extraction_failed"
# The endpoint gave no answer at all. A property of the provider, not of the model.
LLM_UNAVAILABLE = "llm_unavailable"


def codes(issues: list[Issue]) -> list[str]:
    return sorted({issue.code for issue in issues})


def check_invoice(invoice: Invoice, erp: ErpData, settings: Settings) -> list[Issue]:
    """Every issue the decision is made from. All code, no model."""
    with span("validate") as current:
        issues = validate_invoice(invoice, settings.amount_tolerance, settings.tax_rate)
        set_attributes(current, {"app.issue_codes": codes(issues)})
    with span("duplicates") as current:
        duplicates = find_duplicates(invoice, erp, settings.amount_tolerance)
        set_attributes(current, {"app.issue_codes": codes(duplicates)})
    with span("match", {"app.po_number": invoice.po_number}) as current:
        matched = match_invoice(invoice, erp, settings.amount_tolerance)
        set_attributes(current, {"app.issue_codes": codes(matched)})
    return issues + duplicates + matched
