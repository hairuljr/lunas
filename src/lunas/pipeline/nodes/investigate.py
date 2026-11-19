"""Step 8, the exception investigation: what it must produce. The loop itself comes next.

The investigator diagnoses; it never decides. An invoice it looks at stays on hold until the
vendor corrects it or a person acts (SPEC §4, decision log 2026-11-19). Its output is:

- a cause per issue, from a fixed taxonomy, so its accuracy can be measured (SPEC §2:
  exception reason accuracy >= 90%);
- evidence: ids of records it fetched with its read-only tools, which code checks exist;
- an action, which code derives from the cause. The model does not choose it.

It is given typed fields and issues only, never the invoice text (SPEC §6).
"""

from enum import StrEnum

from pydantic import BaseModel


class ExceptionCause(StrEnum):
    OVERCHARGE = "overcharge"  # unit price above the PO, and the contract does not allow it
    CONTRACT_PRICE_NOT_YET_EFFECTIVE = "contract_price_not_yet_effective"  # billed too early
    PO_AMBIGUOUS = "po_ambiguous"  # the PO lists the item at more than one price
    SHORT_DELIVERY = "short_delivery"  # billed more than has been received so far
    OVER_BILLING = "over_billing"  # billed more than was ordered
    ITEM_NOT_ORDERED = "item_not_ordered"  # a line that is not on the PO
    RESUBMISSION = "resubmission"  # an invoice already paid, sent again under a new number
    SEPARATE_DELIVERY = "separate_delivery"  # same amount as an earlier invoice, but legitimate
    WRONG_TAX_RATE = "wrong_tax_rate"
    PAYMENT_TERMS_SHORTER = "payment_terms_shorter"  # asks to be paid sooner than agreed
    ARITHMETIC_ERROR = "arithmetic_error"  # line amounts, subtotal or total do not add up
    INVOICE_DATE_ERROR = "invoice_date_error"  # due date before the invoice date
    UNCLEAR = "unclear"  # the evidence does not settle it


class Action(StrEnum):
    REQUEST_CREDIT_NOTE = "request_credit_note"
    REQUEST_CORRECTED_INVOICE = "request_corrected_invoice"
    WAIT_FOR_GOODS_RECEIPT = "wait_for_goods_receipt"
    REJECT_AS_DUPLICATE = "reject_as_duplicate"  # a recommendation to the person; still held
    CONFIRM_SEPARATE_DELIVERY = "confirm_separate_delivery"
    PAY_ON_AGREED_TERMS = "pay_on_agreed_terms"
    FIX_PURCHASE_ORDER = "fix_purchase_order"
    MANUAL_REVIEW = "manual_review"


# The causes a diagnosis of each issue may choose from. Anything else is rejected by code.
POSSIBLE_CAUSES: dict[str, tuple[ExceptionCause, ...]] = {
    "price_mismatch": (
        ExceptionCause.OVERCHARGE,
        ExceptionCause.CONTRACT_PRICE_NOT_YET_EFFECTIVE,
    ),
    "po_price_ambiguous": (ExceptionCause.PO_AMBIGUOUS,),
    "quantity_exceeds_received": (ExceptionCause.SHORT_DELIVERY,),
    "quantity_exceeds_po": (ExceptionCause.OVER_BILLING,),
    "line_not_on_po": (ExceptionCause.ITEM_NOT_ORDERED,),
    "possible_duplicate": (ExceptionCause.RESUBMISSION, ExceptionCause.SEPARATE_DELIVERY),
    "tax_mismatch": (ExceptionCause.WRONG_TAX_RATE,),
    "payment_terms_mismatch": (ExceptionCause.PAYMENT_TERMS_SHORTER,),
    "line_amount_mismatch": (ExceptionCause.ARITHMETIC_ERROR,),
    "subtotal_mismatch": (ExceptionCause.ARITHMETIC_ERROR,),
    "total_mismatch": (ExceptionCause.ARITHMETIC_ERROR,),
    "due_before_invoice_date": (ExceptionCause.INVOICE_DATE_ERROR,),
}

# The action follows from the cause. Code, so the model's freedom ends at the diagnosis.
RECOMMENDED_ACTION: dict[ExceptionCause, Action] = {
    ExceptionCause.OVERCHARGE: Action.REQUEST_CREDIT_NOTE,
    ExceptionCause.CONTRACT_PRICE_NOT_YET_EFFECTIVE: Action.REQUEST_CORRECTED_INVOICE,
    ExceptionCause.PO_AMBIGUOUS: Action.FIX_PURCHASE_ORDER,
    ExceptionCause.SHORT_DELIVERY: Action.WAIT_FOR_GOODS_RECEIPT,
    ExceptionCause.OVER_BILLING: Action.REQUEST_CREDIT_NOTE,
    ExceptionCause.ITEM_NOT_ORDERED: Action.REQUEST_CORRECTED_INVOICE,
    ExceptionCause.RESUBMISSION: Action.REJECT_AS_DUPLICATE,
    ExceptionCause.SEPARATE_DELIVERY: Action.CONFIRM_SEPARATE_DELIVERY,
    ExceptionCause.WRONG_TAX_RATE: Action.REQUEST_CORRECTED_INVOICE,
    ExceptionCause.PAYMENT_TERMS_SHORTER: Action.PAY_ON_AGREED_TERMS,
    ExceptionCause.ARITHMETIC_ERROR: Action.REQUEST_CORRECTED_INVOICE,
    ExceptionCause.INVOICE_DATE_ERROR: Action.REQUEST_CORRECTED_INVOICE,
    ExceptionCause.UNCLEAR: Action.MANUAL_REVIEW,
}


class EvidenceKind(StrEnum):
    PURCHASE_ORDER = "purchase_order"
    GOODS_RECEIPT = "goods_receipt"
    LEDGER_INVOICE = "ledger_invoice"
    CONTRACT_CLAUSE = "contract_clause"
    VENDOR = "vendor"


class Evidence(BaseModel):
    kind: EvidenceKind
    # The record's id: a PO number, GR number, ledger invoice number, clause id or vendor id.
    # Code checks it is one the investigator actually fetched.
    ref: str
    # What in that record supports the cause, in one sentence.
    detail: str


class Diagnosis(BaseModel):
    issue_code: str
    line: int | None = None
    cause: ExceptionCause
    evidence: list[Evidence]
    explanation: str


class InvestigationReport(BaseModel):
    diagnoses: list[Diagnosis]
    turns: int

    def actions(self) -> list[Action]:
        return [RECOMMENDED_ACTION[d.cause] for d in self.diagnoses]


def allowed_cause(diagnosis: Diagnosis) -> bool:
    """A cause outside the issue's possible causes is not a diagnosis of that issue."""
    return diagnosis.cause == ExceptionCause.UNCLEAR or diagnosis.cause in POSSIBLE_CAUSES.get(
        diagnosis.issue_code, ()
    )
