"""The state behind the mock servers: the ERP records, and what was paid, posted and sent."""

import threading
from decimal import Decimal

from pydantic import BaseModel

from lunas.erp import ErpData


class PaymentOrder(BaseModel):
    invoice_number: str
    vendor_id: str
    bank_account: str
    amount: Decimal
    idempotency_key: str
    approved_by: str | None = None


class LedgerEntry(BaseModel):
    invoice_number: str
    vendor_id: str
    decision: str
    amount: Decimal
    idempotency_key: str


class SentEmail(BaseModel):
    to: str
    subject: str
    body: str


class Backoffice:
    """Thread-safe: an eval runs invoices in parallel against one backoffice."""

    def __init__(self, erp: ErpData) -> None:
        self.erp = erp
        self.payments: list[PaymentOrder] = []
        self.ledger: list[LedgerEntry] = []
        self.outbox: list[SentEmail] = []
        self._lock = threading.Lock()

    def _once(self, items: list, item):
        """Idempotent on the key: a repeat returns the first record, and adds nothing."""
        with self._lock:
            for existing in items:
                if existing.idempotency_key == item.idempotency_key:
                    return existing, False
            items.append(item)
            return item, True

    def pay(self, order: PaymentOrder) -> tuple[PaymentOrder, bool]:
        return self._once(self.payments, order)

    def post(self, entry: LedgerEntry) -> tuple[LedgerEntry, bool]:
        return self._once(self.ledger, entry)

    def send(self, email: SentEmail) -> None:
        with self._lock:
            self.outbox.append(email)
