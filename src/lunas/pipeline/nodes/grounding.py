"""Step 3b: every value the decision rests on must be printed in the document it came from.

The extraction model reads untrusted text, and with OCR or vision a misread transcript.
If it returns an invoice number, PO number, amount or bank account that does not appear in
that text, the value was invented, altered, or read from a damaged line; either way the
checks after this one would be judging something the sender never printed. And a bank
account that is printed but came back empty would hide a changed account, because the
match reads None as "not printed".

Code only, run on every backend. Formatting is forgiven (spaces in numbers, thousands
separators, cents); the characters themselves are not.
"""

import re
from decimal import Decimal

from lunas.models import Invoice, Issue

NOT_IN_DOCUMENT = "field_not_in_document"
BANK_ACCOUNT_NOT_EXTRACTED = "bank_account_not_extracted"

_ACCOUNT_LABEL = re.compile(r"rekening|account\s*(no|number|#)|acc(t)?\.?\s*no|a/c", re.I)
# An account number as printed: at least 8 digits, maybe split by spaces, dots or dashes.
_ACCOUNT_DIGITS = re.compile(r"\d(?:[\s.\-]?\d){7,}")


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text).upper()


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def _numbers(text: str) -> set[str]:
    """Every number in the text without separators, and also without its cents."""
    found = set()
    for token in re.findall(r"\d[\d.,]*\d|\d", text):
        found.add(_digits(token))
        if re.search(r"[.,]\d{2}$", token):
            found.add(_digits(token[:-3]))
    return found


def _amount_digits(value: Decimal) -> str:
    if value == value.to_integral_value():
        return str(int(value))
    return _digits(f"{value:.2f}")


def ground_invoice(invoice: Invoice, text: str) -> list[Issue]:
    issues = []
    compact, numbers = _compact(text), _numbers(text)
    line_digits = [_digits(line) for line in text.splitlines()]

    def missing(field: str, value: object) -> None:
        issues.append(
            Issue(
                code=NOT_IN_DOCUMENT,
                message=f"Extracted {field} {value} does not appear in the document",
            )
        )

    for field in ("invoice_number", "po_number"):
        value = getattr(invoice, field)
        if value and _compact(value) not in compact:
            missing(field, value)
    for field in ("subtotal", "tax", "total"):
        value = getattr(invoice, field)
        if value is not None and _amount_digits(value) not in numbers:
            missing(field, value)

    if invoice.bank_account and invoice.bank_account.strip():
        account = _digits(invoice.bank_account)
        if account and not any(account in digits for digits in line_digits):
            missing("bank_account", invoice.bank_account)
    elif any(
        _ACCOUNT_LABEL.search(line) and _ACCOUNT_DIGITS.search(line) for line in text.splitlines()
    ):
        issues.append(
            Issue(
                code=BANK_ACCOUNT_NOT_EXTRACTED,
                message="The document prints a bank account number, but none was extracted",
            )
        )
    return issues
