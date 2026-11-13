from datetime import date
from decimal import Decimal

from lunas.erp import ErpData
from lunas.models import (
    GoodsReceipt,
    GoodsReceiptLine,
    Invoice,
    LineItem,
    PurchaseOrder,
    PurchaseOrderLine,
    Vendor,
)
from lunas.pipeline.nodes.match import match_invoice

PAPER = "Kertas A4"
TONER = "Toner"


def make_erp(received: dict[str, int] | None = None) -> ErpData:
    received = {PAPER: 10, TONER: 2} if received is None else received
    return ErpData(
        vendors=[
            Vendor(
                vendor_id="V-001",
                name="PT Sumber Makmur",
                bank_name="Bank Central Asia",
                bank_account="5270418896",
                email="ar@sumbermakmur.example",
                payment_terms_days=30,
            )
        ],
        purchase_orders=[
            PurchaseOrder(
                po_number="PO-001",
                vendor_id="V-001",
                order_date=date(2026, 10, 1),
                lines=[
                    PurchaseOrderLine(
                        description=PAPER, quantity=Decimal(10), unit_price=Decimal(50000)
                    ),
                    PurchaseOrderLine(
                        description=TONER, quantity=Decimal(2), unit_price=Decimal(750000)
                    ),
                ],
            )
        ],
        goods_receipts=[
            GoodsReceipt(
                gr_number="GR-001",
                po_number="PO-001",
                received_date=date(2026, 10, 8),
                lines=[
                    GoodsReceiptLine(description=name, quantity=Decimal(quantity))
                    for name, quantity in received.items()
                ],
            )
        ],
    )


def line(description: str, quantity: int, unit_price: int) -> LineItem:
    return LineItem(
        description=description,
        quantity=Decimal(quantity),
        unit_price=Decimal(unit_price),
        amount=Decimal(quantity * unit_price),
    )


def make_invoice(**overrides) -> Invoice:
    fields = {
        "vendor_name": "PT Sumber Makmur",
        "invoice_number": "INV-001",
        "invoice_date": date(2026, 11, 1),
        "po_number": "PO-001",
        "line_items": [line(PAPER, 10, 50000), line(TONER, 2, 750000)],
        "subtotal": Decimal(2000000),
        "tax": Decimal(220000),
        "total": Decimal(2220000),
        "bank_account": "5270418896",
    }
    return Invoice(**(fields | overrides))


def codes(invoice: Invoice, erp: ErpData | None = None) -> list[str]:
    return [issue.code for issue in match_invoice(invoice, erp or make_erp())]


def test_matching_invoice_has_no_issues():
    assert codes(make_invoice()) == []


def test_missing_po_reference_is_flagged():
    assert codes(make_invoice(po_number=None)) == ["po_missing"]


def test_unknown_po_is_flagged():
    assert codes(make_invoice(po_number="PO-999")) == ["po_not_found"]


def test_higher_unit_price_is_flagged():
    invoice = make_invoice(line_items=[line(PAPER, 10, 55000), line(TONER, 2, 750000)])
    assert codes(invoice) == ["price_mismatch"]


def test_lower_unit_price_is_flagged():
    invoice = make_invoice(line_items=[line(PAPER, 10, 45000), line(TONER, 2, 750000)])
    assert codes(invoice) == ["price_mismatch"]


def test_price_within_tolerance_passes():
    items = [line(PAPER, 10, 50000), line(TONER, 2, 750000)]
    items[0].unit_price = Decimal("50000.01")
    assert codes(make_invoice(line_items=items)) == []


def test_quantity_above_po_is_flagged():
    invoice = make_invoice(line_items=[line(PAPER, 12, 50000), line(TONER, 2, 750000)])
    assert codes(invoice) == ["quantity_exceeds_po", "quantity_exceeds_received"]


def test_quantity_above_received_is_flagged():
    erp = make_erp(received={PAPER: 6, TONER: 2})
    assert codes(make_invoice(), erp) == ["quantity_exceeds_received"]


def test_po_line_split_across_invoice_lines_is_summed():
    invoice = make_invoice(
        line_items=[line(PAPER, 6, 50000), line(PAPER, 6, 50000), line(TONER, 2, 750000)]
    )
    assert codes(invoice) == ["quantity_exceeds_po", "quantity_exceeds_received"]


def test_item_not_on_po_is_flagged():
    invoice = make_invoice(line_items=[line(PAPER, 10, 50000), line("Biaya Admin", 1, 100000)])
    assert codes(invoice) == ["line_not_on_po"]


def test_description_match_ignores_case_and_punctuation():
    invoice = make_invoice(line_items=[line("KERTAS  A4.", 10, 50000), line(TONER, 2, 750000)])
    assert codes(invoice) == []


def test_different_vendor_name_is_flagged():
    assert codes(make_invoice(vendor_name="PT Sumber Makmur Jaya")) == ["vendor_mismatch"]


def test_vendor_name_match_ignores_case_and_punctuation():
    assert codes(make_invoice(vendor_name="PT. SUMBER MAKMUR")) == []


def test_bank_account_not_on_file_is_flagged():
    assert codes(make_invoice(bank_account="9990001112")) == ["bank_account_mismatch"]


def test_blank_bank_account_counts_as_not_printed():
    assert codes(make_invoice(bank_account="  ")) == []


def test_bank_account_without_digits_is_unreadable_not_a_changed_account():
    assert codes(make_invoice(bank_account="N/A")) == ["bank_account_unreadable"]


def test_bank_account_match_ignores_formatting():
    assert codes(make_invoice(bank_account="527-041-8896")) == []


def test_invoice_without_bank_account_passes():
    assert codes(make_invoice(bank_account=None)) == []


def test_currency_match_ignores_case_and_spacing():
    assert codes(make_invoice(currency=" idr ")) == []


def test_different_currency_is_flagged():
    assert codes(make_invoice(currency="USD")) == ["currency_mismatch"]


def split_po_erp(second_price: int) -> ErpData:
    """The paper is ordered on two PO lines of 5, as for two deliveries."""
    erp = make_erp()
    po = erp.get_po("PO-001")
    po.lines = [
        PurchaseOrderLine(description=PAPER, quantity=Decimal(5), unit_price=Decimal(50000)),
        PurchaseOrderLine(description=PAPER, quantity=Decimal(5), unit_price=Decimal(second_price)),
        po.lines[1],
    ]
    return erp


def test_po_lines_for_the_same_item_are_summed():
    assert codes(make_invoice(), split_po_erp(second_price=50000)) == []


def test_po_lines_for_the_same_item_still_cap_the_quantity():
    invoice = make_invoice(line_items=[line(PAPER, 11, 50000), line(TONER, 2, 750000)])
    assert codes(invoice, split_po_erp(second_price=50000)) == [
        "quantity_exceeds_po",
        "quantity_exceeds_received",
    ]


def test_same_item_at_two_po_prices_is_flagged_once_instead_of_guessing():
    invoice = make_invoice(
        line_items=[line(PAPER, 5, 50000), line(PAPER, 5, 60000), line(TONER, 2, 750000)]
    )
    assert codes(invoice, split_po_erp(second_price=60000)) == ["po_price_ambiguous"]
