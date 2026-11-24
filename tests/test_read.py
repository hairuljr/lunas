from decimal import Decimal

from PIL import Image

from lunas.ingest.vision import VisionReader
from lunas.llm.client import _parts
from lunas.models import Invoice, Issue
from lunas.pipeline.nodes.grounding import BANK_ACCOUNT_NOT_EXTRACTED, NOT_IN_DOCUMENT
from lunas.pipeline.nodes.read import Read, disagreements


def invoice(**changes) -> Invoice:
    fields = {
        "vendor_name": "PT Bumi Cahaya Alat Kantor",
        "invoice_number": "INV-BCA-00657",
        "invoice_date": "2026-09-25",
        "po_number": "PO-2026-0031",
        "currency": "IDR",
        "line_items": [],
        "subtotal": Decimal(3663600),
        "tax": Decimal(402996),
        "total": Decimal(4066596),
        "bank_account": "5069057507",
    }
    return Invoice(**fields | changes)


def test_two_grounded_reads_of_different_values_disagree():
    ocr = Read("ocr", invoice(bank_account="506905750"), [])  # a stamp ate a digit
    vision = Read("vision", invoice(), [])

    assert disagreements(ocr, vision) == ["bank_account"]


def test_a_field_one_reader_could_not_ground_is_not_a_disagreement():
    flagged = Issue(code=NOT_IN_DOCUMENT, message="Extracted total 4066597 does not appear")
    ocr = Read(
        "ocr",
        invoice(total=Decimal(4066597), bank_account=None),
        [
            flagged,
            Issue(code=BANK_ACCOUNT_NOT_EXTRACTED, message="printed, not extracted"),
        ],
    )

    assert disagreements(ocr, Read("vision", invoice(), [])) == []


def test_formatting_is_not_a_disagreement():
    ocr = Read("ocr", invoice(bank_account="5069 0575 07", total=Decimal("4066596.00")), [])
    assert disagreements(ocr, Read("vision", invoice(), [])) == []


class Transcriber:
    model = "vision-model"

    def __init__(self) -> None:
        self.sent = []

    def complete(self, messages, schema=None) -> str:
        self.sent.append(messages)
        return "FAKTUR\nTotal 4.066.596\n"


def test_vision_reader_sends_each_page_as_an_image_and_returns_text(tmp_path):
    path = tmp_path / "scan.png"
    Image.new("L", (400, 600), 255).save(path)
    client = Transcriber()

    document = VisionReader(client).read(path)

    assert document.backend == "vision" and document.text == "FAKTUR\nTotal 4.066.596"
    user = client.sent[0][1]["content"]
    assert user[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    # Trace content keeps the text parts and never the image bytes.
    parts = _parts(client.sent[0])[1]["parts"]
    assert parts[1] == {"type": "blob", "modality": "image", "content": "<omitted>"}


def test_po_numbers_are_looked_up_ignoring_case_and_spaces():
    from lunas.erp import po_key

    assert po_key("pO-2026- 0071") == po_key("PO-2026-0071") == "PO-2026-0071"
    assert po_key("PO-2026-0O71") != po_key("PO-2026-0071")  # a misread character is not
