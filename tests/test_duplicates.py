from datetime import date
from decimal import Decimal

from lunas.erp import ErpData
from lunas.models import Invoice, InvoiceRecord, PurchaseOrder, PurchaseOrderLine, Vendor
from lunas.pipeline.nodes.duplicates import find_duplicates

VENDOR = Vendor(
    vendor_id="V-001",
    name="PT Sumber Makmur",
    bank_name="Bank Central Asia",
    bank_account="5270418896",
    email="ar@sumbermakmur.example",
    payment_terms_days=30,
)
PAID = InvoiceRecord(
    vendor_id="V-001",
    invoice_number="INV/2026/10/0159",
    invoice_date=date(2026, 10, 19),
    total=Decimal(2220000),
    po_number="PO-001",
)


def make_erp(*history: InvoiceRecord) -> ErpData:
    po = PurchaseOrder(
        po_number="PO-001",
        vendor_id="V-001",
        order_date=date(2026, 10, 1),
        lines=[
            PurchaseOrderLine(description="Kertas A4", quantity=Decimal(10), unit_price=Decimal(1))
        ],
    )
    return ErpData([VENDOR], [po], [], list(history))


def make_invoice(**overrides) -> Invoice:
    fields = {
        "vendor_name": "PT Sumber Makmur",
        "invoice_number": "INV/2026/11/0201",
        "invoice_date": date(2026, 11, 2),
        "po_number": "PO-001",
        "line_items": [],
        "subtotal": Decimal(2000000),
        "tax": Decimal(220000),
        "total": Decimal(2220000),
    }
    return Invoice(**(fields | overrides))


def codes(invoice: Invoice, erp: ErpData | None = None) -> list[str]:
    return [issue.code for issue in find_duplicates(invoice, erp or make_erp(PAID))]


def test_new_invoice_number_with_a_new_order_is_not_a_duplicate():
    assert codes(make_invoice(po_number="PO-002", total=Decimal(5))) == []


def test_same_number_from_the_same_vendor_is_a_duplicate():
    assert codes(make_invoice(invoice_number="INV/2026/10/0159")) == ["duplicate_invoice"]


def test_invoice_number_compares_without_case_or_separators():
    assert codes(make_invoice(invoice_number="inv-2026-10-0159")) == ["duplicate_invoice"]


def test_same_total_for_the_same_order_under_a_new_number_is_a_possible_duplicate():
    assert codes(make_invoice()) == ["possible_duplicate"]


def test_same_total_on_the_same_day_is_a_possible_duplicate():
    invoice = make_invoice(po_number=None, invoice_date=date(2026, 10, 19))
    assert codes(invoice) == ["possible_duplicate"]


def test_same_order_with_a_different_total_is_not_a_duplicate():
    assert codes(make_invoice(total=Decimal(1110000))) == []


def test_without_a_po_the_vendor_is_found_by_name():
    invoice = make_invoice(po_number=None, invoice_number="INV/2026/10/0159")
    assert codes(invoice) == ["duplicate_invoice"]


def test_another_vendors_invoice_number_is_not_a_duplicate():
    invoice = make_invoice(vendor_name="PT Lain", po_number=None, invoice_number="INV/2026/10/0159")
    assert codes(invoice) == []


def test_empty_ledger_has_no_duplicates():
    assert codes(make_invoice(invoice_number="INV/2026/10/0159"), make_erp()) == []
