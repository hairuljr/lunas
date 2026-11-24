"""Step 7: which way an invoice goes after the checks. Code, not a model.

    clean        -> decide (approve, or escalate above the approval threshold)
    hard_reject  -> decide (reject): a fixed rule, nothing to investigate, no tokens spent
    human        -> a person: payment details or identity are in question, or the issue is
                    one the investigation cannot work on. The agent never touches these.
    investigate  -> the investigation loop (step 8): a discrepancy to diagnose

The route never changes the decision; it only decides who looks next. Precedence follows
the decision table: human before hard_reject before investigate, so an invoice with a
changed bank account and a price difference goes to a person, not to the agent.
"""

from enum import StrEnum

from pydantic import BaseModel

from lunas.models import Issue


class Route(StrEnum):
    CLEAN = "clean"
    HARD_REJECT = "hard_reject"
    HUMAN = "human"
    INVESTIGATE = "investigate"


HARD_REJECT = frozenset({"po_missing", "po_not_found", "duplicate_invoice"})
# Security risks first: the payment destination or the sender is in question.
SECURITY = frozenset({"bank_account_mismatch", "vendor_mismatch"})
# Not a security risk, but nothing the investigation tools can resolve: an account number
# that could not be read, an extracted value the document does not print, a vendor or
# currency outside the master data, or no typed invoice at all.
NOT_INVESTIGABLE = frozenset(
    {
        "bank_account_unreadable",
        # The extraction does not match the printed document (step 3b): re-read it by hand.
        "field_not_in_document",
        "bank_account_not_extracted",
        "vendor_not_found",
        "currency_mismatch",
        "extraction_failed",
        "llm_unavailable",
    }
)
INVESTIGABLE = frozenset(
    {
        "price_mismatch",
        "po_price_ambiguous",
        "quantity_exceeds_received",
        "quantity_exceeds_po",
        "line_not_on_po",
        "possible_duplicate",
        "tax_mismatch",
        "payment_terms_mismatch",
        "line_amount_mismatch",
        "subtotal_mismatch",
        "total_mismatch",
        "due_before_invoice_date",
    }
)
KNOWN = HARD_REJECT | SECURITY | NOT_INVESTIGABLE | INVESTIGABLE


class RouteResult(BaseModel):
    route: Route
    # The issue codes that sent it this way.
    because: list[str]


def route_invoice(issues: list[Issue]) -> RouteResult:
    codes = {issue.code for issue in issues}
    # A code this table does not know goes to a person, never to the agent or to approval.
    for route, trigger in (
        (Route.HUMAN, SECURITY | NOT_INVESTIGABLE),
        (Route.HUMAN, codes - KNOWN),
        (Route.HARD_REJECT, HARD_REJECT),
        (Route.INVESTIGATE, INVESTIGABLE),
    ):
        if hit := codes & trigger:
            return RouteResult(route=route, because=sorted(hit))
    return RouteResult(route=Route.CLEAN, because=[])
