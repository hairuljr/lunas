from decimal import Decimal

from lunas.models import Invoice
from lunas.pipeline.nodes.grounding import (
    BANK_ACCOUNT_NOT_EXTRACTED,
    NOT_IN_DOCUMENT,
    ground_invoice,
)

TEXT = """FAKTUR
No. Faktur : INV/2026/10/0815
No. PO : PO-2026-0001
Subtotal 7.598.500
PPN 11% 835.835
Total 8.434.335
No. Rekening : 527 041 8896
"""


def invoice(**changes) -> Invoice:
    fields = {
        "vendor_name": "PT Sumber Makmur Abadi",
        "invoice_number": "INV/2026/10/0815",
        "invoice_date": "2026-10-15",
        "po_number": "PO-2026-0001",
        "currency": "IDR",
        "line_items": [],
        "subtotal": Decimal(7598500),
        "tax": Decimal(835835),
        "total": Decimal(8434335),
        "bank_account": "5270418896",
    }
    return Invoice(**fields | changes)


def test_values_printed_in_any_format_are_grounded():
    assert ground_invoice(invoice(), TEXT) == []
    english = TEXT.replace("8.434.335", "IDR 8,434,335.00")
    assert ground_invoice(invoice(), english) == []


def test_a_value_the_document_does_not_print_is_flagged():
    issues = ground_invoice(invoice(total=Decimal(8434336), bank_account="5270418897"), TEXT)

    assert [i.code for i in issues] == [NOT_IN_DOCUMENT, NOT_IN_DOCUMENT]
    assert "total" in issues[0].message and "bank_account" in issues[1].message


def test_an_account_printed_but_not_extracted_is_flagged():
    issues = ground_invoice(invoice(bank_account=None), TEXT)
    assert [i.code for i in issues] == [BANK_ACCOUNT_NOT_EXTRACTED]


def test_no_account_printed_and_none_extracted_is_fine():
    text = TEXT.replace("No. Rekening : 527 041 8896", "Catatan: transfer ke rekening di atas.")
    assert ground_invoice(invoice(bank_account=None), text) == []
