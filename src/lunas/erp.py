"""Mock ERP data: vendor master, purchase orders, goods receipts.

Read from the JSON files written by data/generate.py for now. The same three lookups
become the `erp.*` MCP tools backed by Postgres later (SPEC §5).
"""

import json
from pathlib import Path

from pydantic import TypeAdapter

from lunas.models import GoodsReceipt, PurchaseOrder, Vendor

VENDORS_FILE = "vendors.json"
PURCHASE_ORDERS_FILE = "purchase_orders.json"
GOODS_RECEIPTS_FILE = "goods_receipts.json"


class ErpData:
    def __init__(
        self,
        vendors: list[Vendor],
        purchase_orders: list[PurchaseOrder],
        goods_receipts: list[GoodsReceipt],
    ) -> None:
        self._vendors = {vendor.vendor_id: vendor for vendor in vendors}
        self._purchase_orders = {po.po_number: po for po in purchase_orders}
        self._goods_receipts: dict[str, list[GoodsReceipt]] = {}
        for receipt in goods_receipts:
            self._goods_receipts.setdefault(receipt.po_number, []).append(receipt)

    @classmethod
    def load(cls, directory: Path) -> ErpData:
        def read[T](name: str, kind: type[T]) -> list[T]:
            raw = json.loads((directory / name).read_text(encoding="utf-8"))
            return TypeAdapter(list[kind]).validate_python(raw)

        return cls(
            vendors=read(VENDORS_FILE, Vendor),
            purchase_orders=read(PURCHASE_ORDERS_FILE, PurchaseOrder),
            goods_receipts=read(GOODS_RECEIPTS_FILE, GoodsReceipt),
        )

    def get_vendor(self, vendor_id: str) -> Vendor | None:
        return self._vendors.get(vendor_id)

    def get_po(self, po_number: str) -> PurchaseOrder | None:
        return self._purchase_orders.get(po_number)

    def get_goods_receipts(self, po_number: str) -> list[GoodsReceipt]:
        return self._goods_receipts.get(po_number, [])
