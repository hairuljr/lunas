"""Step 4: deterministic checks on an extracted invoice. No LLM does arithmetic."""

from decimal import Decimal

from lunas.models import Invoice, Issue


def validate_invoice(invoice: Invoice, tolerance: Decimal = Decimal("0.01")) -> list[Issue]:
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
