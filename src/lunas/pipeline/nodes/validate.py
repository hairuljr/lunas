"""Step 4: deterministic checks on an extracted invoice. No LLM does arithmetic."""

from decimal import Decimal

from lunas.config import DEFAULT_TAX_RATE
from lunas.models import Invoice, Issue

# Tax may be rounded to the whole currency unit, in either direction.
TAX_ROUNDING = Decimal(1)


def validate_invoice(
    invoice: Invoice, tolerance: Decimal = Decimal("0.01"), tax_rate: Decimal = DEFAULT_TAX_RATE
) -> list[Issue]:
    issues: list[Issue] = []

    def differs(actual: Decimal, expected: Decimal) -> bool:
        return abs(actual - expected) > tolerance

    for index, item in enumerate(invoice.line_items, start=1):
        expected = item.quantity * item.unit_price
        if differs(item.amount, expected):
            issues.append(
                Issue(
                    code="line_amount_mismatch",
                    message=f"Line {index}: amount {item.amount} != "
                    f"{item.quantity} x {item.unit_price} = {expected}",
                )
            )

    line_total = sum((item.amount for item in invoice.line_items), Decimal(0))
    if differs(invoice.subtotal, line_total):
        issues.append(
            Issue(
                code="subtotal_mismatch",
                message=f"Subtotal {invoice.subtotal} != sum of lines {line_total}",
            )
        )

    expected_tax = invoice.subtotal * tax_rate
    if abs(invoice.tax - expected_tax) > TAX_ROUNDING:
        issues.append(
            Issue(
                code="tax_mismatch",
                message=f"Tax {invoice.tax} is not {tax_rate:.0%} of subtotal {invoice.subtotal} "
                f"({expected_tax})",
            )
        )

    if differs(invoice.total, invoice.subtotal + invoice.tax):
        issues.append(
            Issue(
                code="total_mismatch",
                message=f"Total {invoice.total} != subtotal {invoice.subtotal} + tax {invoice.tax}",
            )
        )

    if invoice.due_date is not None and invoice.due_date < invoice.invoice_date:
        issues.append(
            Issue(
                code="due_before_invoice_date",
                message=f"Due date {invoice.due_date} is before invoice date "
                f"{invoice.invoice_date}",
            )
        )

    return issues
