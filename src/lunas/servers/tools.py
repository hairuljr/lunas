"""The three mock MCP servers. Tool descriptions are pinned by gapura
(`policies/pins.json`): changing one here quarantines the tool until the pins are updated
on purpose (`python -m lunas.gapura.pins --update`)."""

import json

from mcp.server.mcpserver import MCPServer

from lunas.servers.backoffice import Backoffice, LedgerEntry, PaymentOrder, SentEmail
from lunas.text import digits, parse_amount
from lunas.vendor_mail import REASONS, TEMPLATES


def _error(message: str) -> str:
    return json.dumps({"error": message})


def build_erp(office: Backoffice) -> MCPServer:
    server = MCPServer("erp", instructions="Read the ERP records; post decisions to the ledger.")
    erp = office.erp

    @server.tool()
    def get_vendor(vendor_id: str) -> str:
        """Look up a vendor in the vendor master: name, bank account, email, payment terms."""
        vendor = erp.get_vendor(vendor_id)
        return vendor.model_dump_json() if vendor else _error("vendor not found")

    @server.tool()
    def get_po(po_number: str) -> str:
        """Look up a purchase order: vendor, lines, quantities, unit prices, payment terms."""
        po = erp.get_po(po_number)
        return po.model_dump_json() if po else _error("purchase order not found")

    @server.tool()
    def get_goods_receipts(po_number: str) -> str:
        """List the goods receipts recorded against a purchase order."""
        receipts = erp.get_goods_receipts(po_number)
        return json.dumps([receipt.model_dump(mode="json") for receipt in receipts])

    @server.tool()
    def find_invoices(vendor_id: str) -> str:
        """List the invoices from a vendor already entered in the ledger and paid."""
        history = erp.find_invoices(vendor_id)
        return json.dumps([record.model_dump(mode="json") for record in history])

    @server.tool()
    def post_ledger_entry(
        invoice_number: str, vendor_id: str, decision: str, amount: str, idempotency_key: str
    ) -> str:
        """Record the decision taken on an invoice in the ledger. Idempotent on the key."""
        value = parse_amount(amount)
        if value is None:
            return _error("amount is not a number")
        entry, created = office.post(
            LedgerEntry(
                invoice_number=invoice_number,
                vendor_id=vendor_id,
                decision=decision,
                amount=value,
                idempotency_key=idempotency_key,
            )
        )
        return json.dumps({"status": "posted" if created else "already_posted"})

    return server


def build_payments(office: Backoffice) -> MCPServer:
    server = MCPServer("payments", instructions="Schedule vendor payments.")

    @server.tool()
    def schedule_payment(
        invoice_number: str,
        vendor_id: str,
        bank_account: str,
        amount: str,
        idempotency_key: str,
        approved_by: str = "",
    ) -> str:
        """Schedule a transfer of `amount` IDR to `bank_account` for an invoice. Idempotent on
        the key: the same key never pays twice."""
        value = parse_amount(amount)
        if value is None:
            return _error("amount is not a number")
        order, created = office.pay(
            PaymentOrder(
                invoice_number=invoice_number,
                vendor_id=vendor_id,
                bank_account=digits(bank_account),
                amount=value,
                idempotency_key=idempotency_key,
                approved_by=approved_by or None,
            )
        )
        return json.dumps({"status": "scheduled" if created else "already_scheduled"})

    return server


def build_mail(office: Backoffice) -> MCPServer:
    server = MCPServer("mail", instructions="Send email to vendors.")

    @server.tool()
    def send_vendor_email(vendor_id: str, template: str, invoice_number: str, reason: str) -> str:
        """Send a vendor one of our fixed templates ('clarification' or 'payment_scheduled')
        at the email address in the vendor master. `reason` is one of: price, quantity, tax,
        terms, purchase_order, other."""
        vendor = office.erp.get_vendor(vendor_id)
        if vendor is None:
            return _error("vendor not found")
        if template not in TEMPLATES or reason not in REASONS:
            return _error("unknown template or reason")
        subject, body = TEMPLATES[template]
        fields = {
            "invoice_number": invoice_number,
            "vendor_name": vendor.name,
            "reason": REASONS[reason],
        }
        office.send(
            SentEmail(to=vendor.email, subject=subject.format(**fields), body=body.format(**fields))
        )
        return json.dumps({"status": "sent", "to": vendor.email})

    @server.tool()
    def send_email(to: str, subject: str, body: str) -> str:
        """Send an email with any recipient, subject and body."""
        office.send(SentEmail(to=to, subject=subject, body=body))
        return json.dumps({"status": "sent"})

    return server


def build_servers(office: Backoffice) -> dict[str, MCPServer]:
    return {
        "erp": build_erp(office),
        "payments": build_payments(office),
        "mail": build_mail(office),
    }
