"""Step 10: act on the final decision, through gapura. Code only; no model is involved.

Every argument is built here from typed, checked fields: the payee is the PO's vendor, the
destination is that vendor's account in the vendor master (SPEC §5, never the invoice's),
the amount is the invoice total the checks matched. Emails are fixed templates. The
gateway still checks every call (role `act`), so a bug here cannot pay a wrong account.
"""

from pydantic import BaseModel

from lunas.erp import ErpData
from lunas.gapura.gateway import Gateway, Status
from lunas.models import Decision, Invoice, Issue
from lunas.pipeline.nodes.route import SECURITY

# Issues a vendor can fix with a corrected invoice: these get a clarification email.
# Security findings (route.SECURITY) never do: telling a fraudster what was caught helps
# the fraudster.
_CLARIFY = {
    "price_mismatch": "price",
    "quantity_exceeds_received": "quantity",
    "quantity_exceeds_po": "quantity",
    "tax_mismatch": "tax",
    "payment_terms_mismatch": "terms",
}


class ActionTaken(BaseModel):
    tool: str
    status: Status
    reasons: list[str] = []


def act(
    invoice: Invoice,
    decision: Decision,
    issues: list[Issue],
    erp: ErpData,
    gateway: Gateway,
    approved_by: str | None = None,
) -> list[ActionTaken]:
    po = erp.get_po(invoice.po_number) if invoice.po_number else None
    vendor = erp.get_vendor(po.vendor_id) if po else None
    if vendor is None or not invoice.invoice_number:
        return []  # nothing in our records to act on: no PO, so no payee
    session = gateway.session("act")
    key = f"{vendor.vendor_id}:{invoice.invoice_number}"
    calls = [
        (
            "erp.post_ledger_entry",
            {
                "invoice_number": invoice.invoice_number,
                "vendor_id": vendor.vendor_id,
                "decision": decision.value,
                "amount": str(invoice.total or 0),
                "idempotency_key": f"ledger:{key}",
            },
        )
    ]
    if decision == Decision.APPROVE:
        calls.append(
            (
                "payments.schedule_payment",
                {
                    "invoice_number": invoice.invoice_number,
                    "vendor_id": vendor.vendor_id,
                    "bank_account": vendor.bank_account,
                    "amount": str(invoice.total),
                    "idempotency_key": f"pay:{key}",
                    "approved_by": approved_by or "",
                },
            )
        )
    codes = {issue.code for issue in issues}
    reason = next((_CLARIFY[c] for c in sorted(codes) if c in _CLARIFY), None)
    if decision == Decision.HOLD and reason and not codes & SECURITY:
        calls.append(
            (
                "mail.send_vendor_email",
                {
                    "vendor_id": vendor.vendor_id,
                    "template": "clarification",
                    "invoice_number": invoice.invoice_number,
                    "reason": reason,
                },
            )
        )
    actions = []
    for tool, args in calls:
        result = gateway.call(session, tool, args, approved_by=approved_by)
        actions.append(ActionTaken(tool=tool, status=result.status, reasons=list(result.reasons)))
    return actions
