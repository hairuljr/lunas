"""The only emails lunas sends vendors: fixed templates, filled in by the mail server.
A caller picks a template and a reason code, never the wording, so nothing an invoice says
can end up in an email."""

TEMPLATES = {
    "clarification": (
        "Invoice {invoice_number}: clarification needed",
        "Dear {vendor_name},\n\nWe could not process invoice {invoice_number} yet "
        "({reason}). Please reply with a corrected invoice or the supporting document.\n\n"
        "Accounts Payable, PT Lunas Nusantara Sejahtera",
    ),
    "payment_scheduled": (
        "Invoice {invoice_number}: payment scheduled",
        "Dear {vendor_name},\n\nPayment for invoice {invoice_number} has been scheduled "
        "to the account we hold on file for you.\n\n"
        "Accounts Payable, PT Lunas Nusantara Sejahtera",
    ),
}
REASONS = {
    "price": "a unit price differs from the purchase order",
    "quantity": "the quantity billed differs from what we received",
    "tax": "the tax amount is not the applicable rate",
    "terms": "the payment terms differ from our agreement",
    "purchase_order": "we could not find the purchase order",
    "other": "it needs a manual review",
}
