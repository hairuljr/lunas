"""Step 4, duplicates: has this invoice been entered in the ERP before? Code, not an LLM."""

import re
from decimal import Decimal

from lunas.erp import ErpData
from lunas.models import Invoice, Issue


def find_duplicates(
    invoice: Invoice, erp: ErpData, tolerance: Decimal = Decimal("0.01")
) -> list[Issue]:
    vendor_id = _vendor_id(invoice, erp)
    if vendor_id is None:
        return []  # an unknown sender has no ledger to search; matching flags it
    history = erp.find_invoices(vendor_id)

    number = _number_key(invoice.invoice_number)
    for previous in history:
        if _number_key(previous.invoice_number) == number:
            return [
                Issue(
                    code="duplicate_invoice",
                    message=f"Invoice {invoice.invoice_number} is already in the ledger, "
                    f"dated {previous.invoice_date}",
                )
            ]

    # A resubmission under a new number: the same amount for the same order, or on the
    # same day. Not certain, so it is held for a person rather than rejected.
    for previous in history:
        same_order = invoice.po_number is not None and previous.po_number == invoice.po_number
        same_day = previous.invoice_date == invoice.invoice_date
        if abs(previous.total - invoice.total) <= tolerance and (same_order or same_day):
            return [
                Issue(
                    code="possible_duplicate",
                    message=f"Same total {invoice.total} as invoice {previous.invoice_number} "
                    f"of {previous.invoice_date}, "
                    + (f"for the same PO {invoice.po_number}" if same_order else "on the same day"),
                )
            ]
    return []


def _vendor_id(invoice: Invoice, erp: ErpData) -> str | None:
    """Whoever the PO was issued to; without one, the vendor whose name matches exactly."""
    po = erp.get_po(invoice.po_number) if invoice.po_number else None
    if po is not None:
        return po.vendor_id
    vendor = erp.find_vendor_by_name(invoice.vendor_name)
    return vendor.vendor_id if vendor else None


def _number_key(number: str) -> str:
    """INV/2026/10/0159 and inv-2026-10-0159 are the same invoice number."""
    return re.sub(r"[^0-9A-Z]", "", number.upper())
