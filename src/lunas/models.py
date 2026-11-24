from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel

Backend = Literal["pdf_text", "ocr", "vision"]
Table = list[list[str | None]]


class Page(BaseModel):
    number: int
    text: str
    tables: list[Table] = []


class ParsedDocument(BaseModel):
    """Output of every ingest backend. Its content is untrusted: it was written by the sender."""

    pages: list[Page]
    backend: Backend
    confidence: float | None = None

    @property
    def text(self) -> str:
        return "\n\n".join(page.text for page in self.pages)


class LineItem(BaseModel):
    description: str
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal


class Invoice(BaseModel):
    vendor_name: str
    invoice_number: str
    invoice_date: date
    due_date: date | None = None
    # Only when the document prints a term ("Net 30 days", "Termin 14 hari"). Never derived
    # from the due date, and the due date is never derived from it.
    payment_terms_days: int | None = None
    po_number: str | None = None
    currency: str = "IDR"
    line_items: list[LineItem]
    subtotal: Decimal
    tax: Decimal
    total: Decimal
    # Informational only. Payments always go to the account in the vendor master.
    bank_account: str | None = None
    # Untrusted free text. Passed downstream as data, never as instructions.
    notes: str | None = None


class Decision(StrEnum):
    APPROVE = "approve"
    HOLD = "hold"
    REJECT = "reject"
    ESCALATE = "escalate"


class Issue(BaseModel):
    code: str
    message: str
    # The invoice line it is about (1-based), where it is about one line.
    line: int | None = None


class DecisionResult(BaseModel):
    decision: Decision
    # The issues that produced the decision. Empty for a clean approve.
    reasons: list[Issue] = []


class Vendor(BaseModel):
    vendor_id: str
    name: str
    bank_name: str
    # Digits only. The single source of truth for where payments go.
    bank_account: str
    email: str
    payment_terms_days: int


class PurchaseOrderLine(BaseModel):
    description: str
    quantity: Decimal
    unit_price: Decimal


class PurchaseOrder(BaseModel):
    po_number: str
    vendor_id: str
    order_date: date
    currency: str = "IDR"
    lines: list[PurchaseOrderLine]


class GoodsReceiptLine(BaseModel):
    description: str
    quantity: Decimal


class GoodsReceipt(BaseModel):
    gr_number: str
    po_number: str
    received_date: date
    lines: list[GoodsReceiptLine]


class InvoiceRecord(BaseModel):
    """An invoice already entered in the ERP ledger and paid. What the duplicate check
    searches."""

    vendor_id: str
    invoice_number: str
    invoice_date: date
    total: Decimal
    po_number: str | None = None


class ContractClause(BaseModel):
    """One numbered paragraph of a contract: the unit that retrieval indexes and that a
    finding cites. Its article (the parent) is what gets passed to a model as context."""

    clause_id: str  # "KTR-V-001#5.1"
    contract_id: str
    vendor_id: str
    number: str  # "5.1"
    article: str  # "5"
    article_title: str
    text: str


class Contract(BaseModel):
    """Our signed agreement with a vendor. Internal and trusted, unlike an invoice."""

    contract_id: str
    vendor_id: str
    vendor_name: str
    language: str
    title: str
    effective_date: date
    end_date: date
    clauses: list[ContractClause]
