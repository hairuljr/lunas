from datetime import date
from decimal import Decimal

from lunas.models import Invoice, LineItem
from lunas.pipeline.nodes.validate import validate_invoice


def make_invoice(**overrides) -> Invoice:
    fields = {
        "vendor_name": "PT Sumber Makmur",
        "invoice_number": "INV-001",
        "invoice_date": date(2026, 11, 1),
        "due_date": date(2026, 12, 1),
        "line_items": [
            LineItem(
                description="Kertas A4",
                quantity=Decimal(10),
                unit_price=Decimal(50000),
                amount=Decimal(500000),
            ),
            LineItem(
                description="Toner",
                quantity=Decimal(2),
                unit_price=Decimal(750000),
                amount=Decimal(1500000),
            ),
        ],
        "subtotal": Decimal(2000000),
        "tax": Decimal(220000),
        "total": Decimal(2220000),
    }
    return Invoice(**(fields | overrides))


def codes(invoice: Invoice) -> list[str]:
    return [issue.code for issue in validate_invoice(invoice)]


def test_clean_invoice_has_no_issues():
    assert codes(make_invoice()) == []


def test_wrong_total_is_flagged():
    assert codes(make_invoice(total=Decimal(2500000))) == ["total_mismatch"]


def test_wrong_subtotal_is_flagged():
    invoice = make_invoice(subtotal=Decimal(1900000), total=Decimal(2120000))
    assert codes(invoice) == ["subtotal_mismatch"]


def test_wrong_line_amount_is_flagged():
    invoice = make_invoice()
    invoice.line_items[0].amount = Decimal(600000)
    assert codes(invoice) == ["line_amount_mismatch", "subtotal_mismatch"]


def test_due_date_before_invoice_date_is_flagged():
    assert codes(make_invoice(due_date=date(2026, 10, 1))) == ["due_before_invoice_date"]


def test_rounding_within_tolerance_passes():
    assert codes(make_invoice(total=Decimal("2220000.01"))) == []
