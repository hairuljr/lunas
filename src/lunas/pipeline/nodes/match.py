"""Step 5: 3-way match of an extracted invoice against the PO, goods receipts and vendor
master. Deterministic: every lookup and comparison here is code, never an LLM."""

import re
from decimal import Decimal

from lunas.erp import ErpData
from lunas.models import Invoice, Issue, Vendor


def normalize(text: str) -> str:
    """Case, punctuation and spacing do not distinguish two names or item descriptions."""
    return " ".join(re.sub(r"[^\w\s]", " ", text.casefold()).split())


def match_invoice(
    invoice: Invoice, erp: ErpData, tolerance: Decimal = Decimal("0.01")
) -> list[Issue]:
    if not invoice.po_number:
        return [Issue(code="po_missing", message="Invoice does not reference a purchase order")]
    po = erp.get_po(invoice.po_number)
    if po is None:
        return [Issue(code="po_not_found", message=f"PO {invoice.po_number} is not in the ERP")]

    issues: list[Issue] = []

    # The vendor is whoever the PO was issued to. The name on the invoice is only compared
    # against it, and the invoice's bank account is never a payment destination.
    vendor = erp.get_vendor(po.vendor_id)
    if vendor is None:
        issues.append(
            Issue(
                code="vendor_not_found",
                message=f"Vendor {po.vendor_id} of PO {po.po_number} is not in the vendor master",
            )
        )
    else:
        if normalize(invoice.vendor_name) != normalize(vendor.name):
            issues.append(
                Issue(
                    code="vendor_mismatch",
                    message=f"Invoice is from {invoice.vendor_name!r} but PO {po.po_number} "
                    f"was issued to {vendor.name!r}",
                )
            )
        issues += _check_bank_account(invoice.bank_account, vendor)

    if invoice.currency.strip().upper() != po.currency.strip().upper():
        issues.append(
            Issue(
                code="currency_mismatch",
                message=f"Invoice currency {invoice.currency} != PO currency {po.currency}",
            )
        )

    # A PO may list the same item on several lines (two deliveries, say), so ordered
    # quantities are summed per item, the same way received and invoiced quantities are.
    ordered: dict[str, Decimal] = {}
    prices: dict[str, set[Decimal]] = {}
    names: dict[str, str] = {}
    for line in po.lines:
        key = normalize(line.description)
        ordered[key] = ordered.get(key, Decimal(0)) + line.quantity
        prices.setdefault(key, set()).add(line.unit_price)
        names.setdefault(key, line.description)
    received: dict[str, Decimal] = {}
    for receipt in erp.get_goods_receipts(po.po_number):
        for line in receipt.lines:
            key = normalize(line.description)
            received[key] = received.get(key, Decimal(0)) + line.quantity

    # Quantities are summed per item so an invoice cannot split one PO line into several
    # lines that each look small enough.
    invoiced: dict[str, Decimal] = {}
    for index, item in enumerate(invoice.line_items, start=1):
        key = normalize(item.description)
        if key not in ordered:
            issues.append(
                Issue(
                    code="line_not_on_po",
                    message=f"Line {index}: {item.description!r} is not on PO {po.po_number}",
                )
            )
            continue
        first_line_of_item = key not in invoiced
        invoiced[key] = invoiced.get(key, Decimal(0)) + item.quantity
        if len(prices[key]) > 1:
            # Matching is by description only, so there is no telling which PO line this
            # invoice line bills. Accepting any of the prices would let the dearest one win.
            if first_line_of_item:
                issues.append(
                    Issue(
                        code="po_price_ambiguous",
                        message=f"PO {po.po_number} lists {names[key]!r} at several prices "
                        f"({', '.join(str(price) for price in sorted(prices[key]))})",
                    )
                )
            continue
        (po_price,) = prices[key]
        if abs(item.unit_price - po_price) > tolerance:
            issues.append(
                Issue(
                    code="price_mismatch",
                    message=f"Line {index}: unit price {item.unit_price} != PO price "
                    f"{po_price} for {item.description!r}",
                )
            )

    for key, quantity in invoiced.items():
        if quantity > ordered[key]:
            issues.append(
                Issue(
                    code="quantity_exceeds_po",
                    message=f"{names[key]!r}: invoiced {quantity} > ordered {ordered[key]}",
                )
            )
        if quantity > received.get(key, Decimal(0)):
            issues.append(
                Issue(
                    code="quantity_exceeds_received",
                    message=f"{names[key]!r}: invoiced {quantity} > received "
                    f"{received.get(key, Decimal(0))}",
                )
            )

    return issues


def _digits(account: str) -> str:
    return re.sub(r"\D", "", account)


def _check_bank_account(account: str | None, vendor: Vendor) -> list[Issue]:
    """The invoice's account is never paid; it is only evidence. A different account
    number is the fraud signal. A value with no digits at all is a reading problem."""
    if account is None or not account.strip():
        return []
    if not _digits(account):
        return [
            Issue(
                code="bank_account_unreadable",
                message=f"Invoice bank account {account!r} contains no account number",
            )
        ]
    if _digits(account) != _digits(vendor.bank_account):
        return [
            Issue(
                code="bank_account_mismatch",
                message=f"Invoice names bank account {account}, which is not the account "
                f"on file for {vendor.name}",
            )
        ]
    return []
