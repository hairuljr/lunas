import math

from evals.node.transcription import cer, fields_found
from lunas.ingest.ocr import layout


def box(left, top, width, height, angle=0.0):
    """A text box's four corners, rotated about the origin by `angle` radians."""
    corners = [(left, top), (left + width, top), (left + width, top + height), (left, top + height)]
    c, s = math.cos(angle), math.sin(angle)
    return [[x * c - y * s, x * s + y * c] for x, y in corners]


def test_boxes_on_one_line_are_joined_left_to_right_with_their_columns():
    boxes = [box(500, 100, 140, 30), box(0, 102, 140, 30), box(0, 160, 200, 30)]
    text = layout(boxes, ["Total", "Subtotal", "Bank Negara"])

    first, second = text.splitlines()
    assert first.startswith("Subtotal") and first.endswith("Total")
    assert first.index("Total") > len("Subtotal") + 10  # the gap is kept as spaces
    assert second == "Bank Negara"


def test_a_skewed_page_is_levelled_before_lines_are_formed():
    # 3 degrees across 2000 px drifts about 100 px: more than a line height.
    angle = math.radians(3)
    boxes = [
        box(0, 100, 300, 30, angle),
        box(1700, 100, 300, 30, angle),
        box(0, 160, 300, 30, angle),
    ]
    assert layout(boxes, ["left", "right", "next"]).splitlines()[0].split() == ["left", "right"]


def test_empty_page():
    assert layout([], []) == ""


def test_cer_ignores_whitespace_layout():
    assert cer("Total   4.066.596", "Total 4.066.596") == 0.0
    assert cer("abcd", "abed") == 0.25


def test_critical_fields_match_numbers_whatever_their_separators():
    invoice = {
        "invoice_number": "INV/2026/10/0815",
        "po_number": "PO-2026-0001",
        "tax": "835835",
        "total": "8434335",
        "bank_account": "5270418896",
    }
    text = "No: INV/2026/10/ 0815 PO-2026-0001 Tax 835,835.00 Total 8.434.335 Acc 527041889"

    found = fields_found(invoice, text)

    assert found == {
        "invoice_number": True,
        "po_number": True,
        "tax": True,
        "total": True,
        "bank_account": False,
    }  # one digit short is not found
    assert fields_found({**invoice, "bank_account": None}, text)["bank_account"] is None
