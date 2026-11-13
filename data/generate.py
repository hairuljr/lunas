"""Synthetic accounts-payable dataset: vendor master, purchase orders, goods receipts,
digital invoice PDFs and per-invoice ground truth.

    uv run python data/generate.py [--out data/out] [--seed 20261112] [--invoices 30]

The same seed always produces byte-identical files. Labels come from how each invoice was
constructed, never from running the pipeline, so they can be used to grade it.

Week-1 scope (SPEC §12): two classes, `clean` and `price_mismatch`.
"""

import argparse
import json
import random
import shutil
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from lunas.erp import GOODS_RECEIPTS_FILE, PURCHASE_ORDERS_FILE, VENDORS_FILE
from lunas.models import (
    Decision,
    GoodsReceipt,
    GoodsReceiptLine,
    Invoice,
    LineItem,
    PurchaseOrder,
    PurchaseOrderLine,
    Vendor,
)

DEFAULT_SEED = 20261112
DEFAULT_INVOICES = 30
GROUND_TRUTH_FILE = "ground_truth.json"
MANIFEST_FILE = "manifest.json"
INVOICE_DIR = "invoices"

TAX_RATE = Decimal("0.11")
# Every invoice stays below the human-approval threshold (50,000,000 in policies/default.yaml),
# so `clean` always means `approve`. Line value is capped so the worst case still fits:
# 4 lines x 9,000,000 x 1.15 price bump x 1.11 tax < 50,000,000.
MAX_LINE_VALUE = 9_000_000
MAX_INVOICE_TOTAL = Decimal(50_000_000)
FIRST_ORDER_DATE = date(2026, 9, 1)

Language = Literal["id", "en"]
Layout = Literal["classic", "modern", "compact"]
Label = Literal["clean", "price_mismatch"]

BUYER_NAME = "PT Lunas Nusantara Sejahtera"
BUYER_ADDRESS = ["Jl. Jenderal Sudirman Kav. 52-53", "Jakarta Selatan 12190"]


@dataclass(frozen=True)
class VendorSpec:
    name: str
    language: Language
    layout: Layout
    category: str
    address: tuple[str, str]
    tax_id: str
    bank_name: str
    bank_account: str
    email: str
    payment_terms_days: int
    invoice_number_format: str


# (Indonesian description, English description, base unit price in IDR).
# Descriptions stay under 34 characters so they fit the fixed-width `compact` layout.
CATALOG: dict[str, list[tuple[str, str, int]]] = {
    "office": [
        ("Kertas HVS A4 80gsm (rim)", "A4 Copy Paper 80gsm (ream)", 52_000),
        ("Toner Printer Laser Hitam", "Black Laser Printer Toner", 780_000),
        ("Map Arsip Ordner (lusin)", "Lever Arch Files (dozen)", 264_000),
        ("Pulpen Gel Hitam (kotak)", "Black Gel Pens (box)", 48_000),
        ("Kursi Kantor Ergonomis", "Ergonomic Office Chair", 1_850_000),
        ("Papan Tulis Putih 120x240", "Whiteboard 120x240", 1_250_000),
    ],
    "it": [
        ("Monitor LED 24 inci", "24-inch LED Monitor", 2_150_000),
        ("Keyboard Mekanikal USB", "USB Mechanical Keyboard", 640_000),
        ("Kabel Jaringan Cat6 (dus)", "Cat6 Network Cable (box)", 1_420_000),
        ("SSD Eksternal 1TB", "External SSD 1TB", 1_680_000),
        ("Router Nirkabel Dual-Band", "Dual-Band Wireless Router", 1_190_000),
        ("Lisensi Antivirus 1 Tahun", "Antivirus License 1 Year", 385_000),
    ],
    "cleaning": [
        ("Cairan Pembersih Lantai 5L", "Floor Cleaner 5L", 96_000),
        ("Sabun Cuci Tangan 5L", "Hand Soap 5L", 118_000),
        ("Tisu Gulung (karton)", "Toilet Tissue Rolls (carton)", 312_000),
        ("Kantong Sampah 90x120 (pak)", "Trash Bags 90x120 (pack)", 67_000),
        ("Pengharum Ruangan Otomatis", "Automatic Air Freshener", 245_000),
        ("Jasa Kebersihan Bulanan", "Monthly Cleaning Service", 7_600_000),
    ],
    "packaging": [
        ("Kardus Double Wall 40x40x40", "Double Wall Carton 40x40x40", 14_500),
        ("Lakban Bening 48mm (dus)", "Clear Tape 48mm (box)", 432_000),
        ("Bubble Wrap 1,25m x 50m", "Bubble Wrap 1.25m x 50m", 285_000),
        ("Palet Kayu 110x110", "Wooden Pallet 110x110", 175_000),
        ("Stretch Film 500mm (rol)", "Stretch Film 500mm (roll)", 89_000),
        ("Label Pengiriman A6 (rol)", "Shipping Labels A6 (roll)", 73_000),
    ],
    "pantry": [
        ("Air Mineral Galon 19L", "Mineral Water 19L Gallon", 21_000),
        ("Kopi Bubuk Arabika 1kg", "Ground Arabica Coffee 1kg", 198_000),
        ("Gula Pasir 1kg", "Granulated Sugar 1kg", 18_500),
        ("Teh Celup (kotak isi 100)", "Tea Bags (box of 100)", 42_000),
        ("Katering Rapat (paket)", "Meeting Catering (package)", 1_350_000),
        ("Mesin Kopi Otomatis", "Automatic Coffee Machine", 6_400_000),
    ],
}
_LANGUAGE_INDEX = {"id": 0, "en": 1}

VENDORS: list[VendorSpec] = [
    VendorSpec(
        "PT Sumber Makmur Abadi", "id", "classic", "office",
        ("Jl. Gatot Subroto No. 88", "Jakarta Selatan 12930"), "01.234.567.8-012.000",
        "Bank Central Asia", "5270418896", "ar@sumbermakmur.example", 30,
        "INV/{yyyy}/{mm}/{seq:04d}",
    ),
    VendorSpec(
        "Nusantara Tech Supplies Ltd", "en", "classic", "it",
        ("Jl. HR Rasuna Said Kav. C-22", "Jakarta Selatan 12940"), "02.845.119.3-063.000",
        "Bank Mandiri", "1240007719356", "billing@nusantaratech.example", 30,
        "INV-{seq:06d}",
    ),
    VendorSpec(
        "CV Bersih Sejahtera", "id", "modern", "cleaning",
        ("Jl. Raya Darmo No. 17", "Surabaya 60264"), "03.377.902.5-609.000",
        "Bank Negara Indonesia", "0389915527", "keuangan@bersihsejahtera.example", 14,
        "{seq:04d}/INV/BSJ/{roman}/{yyyy}",
    ),
    VendorSpec(
        "Archipelago Packaging Co", "en", "modern", "packaging",
        ("Kawasan Industri Jababeka Blok F-9", "Bekasi 17530"), "04.610.284.7-431.000",
        "Bank Rakyat Indonesia", "033201009876305", "accounts@archipelagopack.example", 45,
        "SI-{yy}-{seq:05d}",
    ),
    VendorSpec(
        "PT Dapur Kantor Nusantara", "id", "compact", "pantry",
        ("Jl. Asia Afrika No. 112", "Bandung 40261"), "05.129.846.0-423.000",
        "Bank Central Asia", "0841736502", "tagihan@dapurkantor.example", 14,
        "FK-{yy}{mm}-{seq:04d}",
    ),
    VendorSpec(
        "Java Office Furnishings", "en", "compact", "office",
        ("Jl. Pemuda No. 150", "Semarang 50132"), "06.553.071.2-511.000",
        "Bank CIMB Niaga", "800114673900", "finance@javaoffice.example", 30,
        "JOF/{yyyy}/{seq:05d}",
    ),
    VendorSpec(
        "PT Mitra Data Solusindo", "id", "modern", "it",
        ("Jl. TB Simatupang No. 41", "Jakarta Selatan 12550"), "07.902.318.4-017.000",
        "Bank Mandiri", "1030005542871", "finance@mitradata.example", 30,
        "MDS-INV-{yyyy}{mm}-{seq:03d}",
    ),
    VendorSpec(
        "PT Kemas Prima Indonesia", "id", "compact", "packaging",
        ("Jl. Raya Serpong KM 7", "Tangerang Selatan 15310"), "08.266.740.9-411.000",
        "Bank Permata", "4102583697", "ar@kemasprima.example", 45,
        "{seq:05d}/KPI/{yyyy}",
    ),
    VendorSpec(
        "Borneo Hygiene Services", "en", "classic", "cleaning",
        ("Jl. Jenderal Ahmad Yani No. 5", "Balikpapan 76113"), "09.718.455.6-721.000",
        "Bank Negara Indonesia", "0712264490", "billing@borneohygiene.example", 14,
        "BHS{yy}{seq:05d}",
    ),
    VendorSpec(
        "CV Sari Rasa Catering", "id", "classic", "pantry",
        ("Jl. Malioboro No. 60", "Yogyakarta 55213"), "10.045.893.1-541.000",
        "Bank Syariah Indonesia", "7153309842", "admin@sarirasa.example", 14,
        "INV.{seq:04d}.{mm}.{yyyy}",
    ),
]  # fmt: skip

NOTES: dict[Language, list[str | None]] = {
    "id": [
        None,
        None,
        "Terima kasih atas kerja sama Anda.",
        "Mohon cantumkan nomor faktur pada berita transfer.",
        "Barang yang sudah diterima tidak dapat dikembalikan.",
    ],
    "en": [
        None,
        None,
        "Thank you for your business.",
        "Please quote the invoice number on your remittance.",
        "Late payments are subject to a 2% monthly charge.",
    ],
}

LABELS: dict[Language, dict[str, str]] = {
    "id": {
        "title": "FAKTUR",
        "invoice_number": "No. Faktur",
        "date": "Tanggal",
        "due_date": "Jatuh Tempo",
        "po_number": "No. PO",
        "terms": "Termin Pembayaran",
        "terms_value": "{days} hari",
        "currency": "Mata Uang",
        "currency_note": "Semua nilai dalam Rupiah (IDR).",
        "from": "Dari",
        "bill_to": "Kepada",
        "tax_id": "NPWP",
        "no": "No",
        "description": "Deskripsi",
        "quantity": "Qty",
        "unit_price": "Harga Satuan",
        "amount": "Jumlah",
        "subtotal": "Subtotal",
        "tax": "PPN 11%",
        "total": "Total",
        "payment": "Pembayaran",
        "bank": "Bank",
        "account": "No. Rekening",
        "holder": "Atas Nama",
        "notes": "Catatan",
    },
    "en": {
        "title": "INVOICE",
        "invoice_number": "Invoice No.",
        "date": "Invoice Date",
        "due_date": "Due Date",
        "po_number": "PO Number",
        "terms": "Payment Terms",
        "terms_value": "Net {days} days",
        "currency": "Currency",
        "currency_note": "All amounts are in Indonesian Rupiah (IDR).",
        "from": "From",
        "bill_to": "Bill To",
        "tax_id": "Tax ID",
        "no": "No",
        "description": "Description",
        "quantity": "Qty",
        "unit_price": "Unit Price",
        "amount": "Amount",
        "subtotal": "Subtotal",
        "tax": "VAT 11%",
        "total": "Total",
        "payment": "Payment Details",
        "bank": "Bank",
        "account": "Account No.",
        "holder": "Account Name",
        "notes": "Notes",
    },
}

MONTHS: dict[Language, list[str]] = {
    "id": [
        "Januari", "Februari", "Maret", "April", "Mei", "Juni",
        "Juli", "Agustus", "September", "Oktober", "November", "Desember",
    ],
    "en": [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ],
}  # fmt: skip
ROMAN_MONTHS = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII"]


class GroundTruth(BaseModel):
    id: str
    file: str
    label: Label
    language: Language
    layout: Layout
    invoice: Invoice
    expected_decision: Decision
    expected_issue_codes: list[str]


@dataclass(frozen=True)
class Dataset:
    vendors: list[Vendor]
    purchase_orders: list[PurchaseOrder]
    goods_receipts: list[GoodsReceipt]
    # Parallel lists: records[i] was issued by specs[i].
    records: list[GroundTruth]
    specs: list[VendorSpec]


# --- dataset construction -------------------------------------------------------------


def _vendor_id(index: int) -> str:
    return f"V-{index + 1:03d}"


def _invoice_number(spec: VendorSpec, issued: date, seq: int) -> str:
    return spec.invoice_number_format.format(
        yyyy=issued.year,
        yy=issued.year % 100,
        mm=f"{issued.month:02d}",
        roman=ROMAN_MONTHS[issued.month - 1],
        seq=seq,
    )


def _po_lines(rng: random.Random, spec: VendorSpec) -> list[PurchaseOrderLine]:
    items = rng.sample(CATALOG[spec.category], k=rng.randint(1, 4))
    lines = []
    for item in items:
        # Contract prices drift around the list price; whole hundreds keep 11% tax integral.
        unit_price = int(item[2] * rng.uniform(0.9, 1.1)) // 100 * 100
        quantity = rng.randint(1, max(1, min(20, MAX_LINE_VALUE // unit_price)))
        lines.append(
            PurchaseOrderLine(
                description=item[_LANGUAGE_INDEX[spec.language]],
                quantity=Decimal(quantity),
                unit_price=Decimal(unit_price),
            )
        )
    return lines


def _overcharge(rng: random.Random, prices: list[Decimal]) -> list[Decimal]:
    """Raise the unit price of one or two lines by 3-15%, in whole hundreds."""
    bumped = list(prices)
    for index in rng.sample(range(len(prices)), k=rng.randint(1, min(2, len(prices)))):
        bump = max(100, int(prices[index] * Decimal(str(rng.uniform(0.03, 0.15)))) // 100 * 100)
        bumped[index] = prices[index] + bump
    return bumped


def build_dataset(seed: int = DEFAULT_SEED, invoices: int = DEFAULT_INVOICES) -> Dataset:
    """One PO, one full goods receipt and one invoice per case; a third are overcharged."""
    rng = random.Random(seed)
    vendors = [
        Vendor(
            vendor_id=_vendor_id(index),
            name=spec.name,
            bank_name=spec.bank_name,
            bank_account=spec.bank_account,
            email=spec.email,
            payment_terms_days=spec.payment_terms_days,
        )
        for index, spec in enumerate(VENDORS)
    ]

    labels: list[Label] = ["price_mismatch"] * (invoices // 3)
    labels += ["clean"] * (invoices - len(labels))
    rng.shuffle(labels)
    sequences = [rng.randint(40, 900) for _ in VENDORS]

    purchase_orders, goods_receipts, records, specs = [], [], [], []
    for index, label in enumerate(labels):
        # Round-robin over vendors so every layout and language appears in a small dataset.
        vendor_index = index % len(VENDORS)
        spec = VENDORS[vendor_index]
        number = index + 1

        order_date = FIRST_ORDER_DATE + timedelta(days=rng.randint(0, 45))
        received_date = order_date + timedelta(days=rng.randint(3, 10))
        invoice_date = received_date + timedelta(days=rng.randint(0, 5))
        po_number = f"PO-{order_date.year}-{number:04d}"
        po_lines = _po_lines(rng, spec)

        purchase_orders.append(
            PurchaseOrder(
                po_number=po_number,
                vendor_id=_vendor_id(vendor_index),
                order_date=order_date,
                lines=po_lines,
            )
        )
        goods_receipts.append(
            GoodsReceipt(
                gr_number=f"GR-{received_date.year}-{number:04d}",
                po_number=po_number,
                received_date=received_date,
                lines=[
                    GoodsReceiptLine(description=line.description, quantity=line.quantity)
                    for line in po_lines
                ],
            )
        )

        prices = [line.unit_price for line in po_lines]
        if label == "price_mismatch":
            prices = _overcharge(rng, prices)
        line_items = [
            LineItem(
                description=line.description,
                quantity=line.quantity,
                unit_price=price,
                amount=line.quantity * price,
            )
            for line, price in zip(po_lines, prices, strict=True)
        ]
        subtotal = sum((item.amount for item in line_items), Decimal(0))
        tax = subtotal * TAX_RATE
        assert tax == tax.to_integral_value(), "prices must keep tax in whole rupiah"
        tax = tax.to_integral_value()
        total = subtotal + tax
        assert total < MAX_INVOICE_TOTAL, "invoice must stay below the approval threshold"

        sequences[vendor_index] += rng.randint(1, 9)
        records.append(
            GroundTruth(
                id=f"inv-{number:03d}",
                file=f"{INVOICE_DIR}/inv-{number:03d}.pdf",
                label=label,
                language=spec.language,
                layout=spec.layout,
                invoice=Invoice(
                    vendor_name=spec.name,
                    invoice_number=_invoice_number(spec, invoice_date, sequences[vendor_index]),
                    invoice_date=invoice_date,
                    # The compact layout prints payment terms only, so no due date exists
                    # to extract: a reader that fills one in has computed it.
                    due_date=None
                    if spec.layout == "compact"
                    else invoice_date + timedelta(days=spec.payment_terms_days),
                    po_number=po_number,
                    line_items=line_items,
                    subtotal=subtotal,
                    tax=tax,
                    total=total,
                    bank_account=spec.bank_account,
                    notes=rng.choice(NOTES[spec.language]),
                ),
                expected_decision=Decision.APPROVE if label == "clean" else Decision.HOLD,
                expected_issue_codes=[] if label == "clean" else ["price_mismatch"],
            )
        )
        specs.append(spec)

    return Dataset(vendors, purchase_orders, goods_receipts, records, specs)


# --- formatting -----------------------------------------------------------------------


def _group(value: Decimal, separator: str) -> str:
    return f"{int(value):,}".replace(",", separator)


def format_money(value: Decimal, language: Language, layout: Layout) -> str:
    if layout == "modern":
        return f"Rp {_group(value, '.')},00" if language == "id" else f"IDR {_group(value, ',')}"
    return _group(value, ".") if language == "id" else f"{_group(value, ',')}.00"


def format_date(value: date, language: Language, layout: Layout) -> str:
    if layout == "compact":
        return value.isoformat()
    month = MONTHS[language][value.month - 1]
    if layout == "modern":
        return (
            value.strftime("%d/%m/%Y")
            if language == "id"
            else f"{value.day} {month[:3]} {value.year}"
        )
    return (
        f"{value.day} {month} {value.year}"
        if language == "id"
        else f"{month} {value.day}, {value.year}"
    )


# --- PDF layouts ----------------------------------------------------------------------


def _document(path: Path) -> SimpleDocTemplate:
    # invariant=1 drops the creation timestamp and random ID: same input, same bytes.
    return SimpleDocTemplate(
        str(path), pagesize=A4, invariant=1, title="Invoice",
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
    )  # fmt: skip


def _item_rows(record: GroundTruth) -> list[list[str]]:
    def money(value: Decimal) -> str:
        return format_money(value, record.language, record.layout)

    return [
        [
            str(number),
            item.description,
            str(int(item.quantity)),
            money(item.unit_price),
            money(item.amount),
        ]
        for number, item in enumerate(record.invoice.line_items, start=1)
    ]


def _render_classic(path: Path, record: GroundTruth, spec: VendorSpec) -> None:
    """Vendor letterhead on the left, bordered item table, totals under the table."""
    invoice, text = record.invoice, LABELS[record.language]
    styles = getSampleStyleSheet()
    normal = styles["Normal"]
    right = ParagraphStyle("right", parent=normal, alignment=TA_RIGHT)

    def money(value: Decimal) -> str:
        return format_money(value, record.language, record.layout)

    def when(value: date) -> str:
        return format_date(value, record.language, record.layout)

    letterhead = Paragraph(
        f"<b><font size=14>{spec.name}</font></b><br/>{spec.address[0]}<br/>{spec.address[1]}"
        f"<br/>{text['tax_id']}: {spec.tax_id}",
        normal,
    )
    meta = Paragraph(
        f"<b><font size=18>{text['title']}</font></b><br/><br/>"
        f"{text['invoice_number']}: {invoice.invoice_number}<br/>"
        f"{text['date']}: {when(invoice.invoice_date)}<br/>"
        f"{text['due_date']}: {when(invoice.due_date)}<br/>"
        f"{text['po_number']}: {invoice.po_number}",
        right,
    )
    header = Table([[letterhead, meta]], colWidths=[95 * mm, 79 * mm])
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))

    items = Table(
        [[text[k] for k in ("no", "description", "quantity", "unit_price", "amount")]]
        + _item_rows(record)
        + [
            ["", "", "", text["subtotal"], money(invoice.subtotal)],
            ["", "", "", text["tax"], money(invoice.tax)],
            ["", "", "", text["total"], money(invoice.total)],
        ],
        colWidths=[10 * mm, 76 * mm, 16 * mm, 36 * mm, 36 * mm],
    )
    last_item = len(invoice.line_items)
    items.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, last_item), 0.5, colors.grey),
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
                ("FONTNAME", (3, -1), (-1, -1), "Helvetica-Bold"),
            ]
        )
    )

    story = [
        header,
        Spacer(1, 8 * mm),
        Paragraph(
            f"<b>{text['bill_to']}:</b><br/>{BUYER_NAME}<br/>{'<br/>'.join(BUYER_ADDRESS)}",
            normal,
        ),
        Spacer(1, 6 * mm),
        items,
        Spacer(1, 3 * mm),
        Paragraph(f"<i>{text['currency_note']}</i>", normal),
        Spacer(1, 6 * mm),
        Paragraph(
            f"<b>{text['payment']}</b><br/>{text['bank']}: {spec.bank_name}<br/>"
            f"{text['account']}: {spec.bank_account}<br/>{text['holder']}: {spec.name}",
            normal,
        ),
    ]
    if invoice.notes:
        story += [Spacer(1, 5 * mm), Paragraph(f"<b>{text['notes']}:</b> {invoice.notes}", normal)]
    _document(path).build(story)


def _render_modern(path: Path, record: GroundTruth, spec: VendorSpec) -> None:
    """Centered title, From/To columns, a meta strip, borderless items, payment box at the end."""
    invoice, text = record.invoice, LABELS[record.language]
    styles = getSampleStyleSheet()
    normal = styles["Normal"]
    title = ParagraphStyle("title", parent=normal, alignment=TA_CENTER, fontSize=20, leading=26)

    def money(value: Decimal) -> str:
        return format_money(value, record.language, record.layout)

    def when(value: date) -> str:
        return format_date(value, record.language, record.layout)

    parties = Table(
        [
            [
                Paragraph(
                    f"<font color='grey'>{text['from']}</font><br/><b>{spec.name}</b><br/>"
                    f"{spec.address[0]}<br/>{spec.address[1]}<br/>{text['tax_id']} {spec.tax_id}",
                    normal,
                ),
                Paragraph(
                    f"<font color='grey'>{text['bill_to']}</font><br/><b>{BUYER_NAME}</b><br/>"
                    + "<br/>".join(BUYER_ADDRESS),
                    normal,
                ),
            ]
        ],
        colWidths=[87 * mm, 87 * mm],
    )
    parties.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))

    meta = Table(
        [
            [text[k] for k in ("invoice_number", "date", "due_date", "po_number")],
            [
                invoice.invoice_number,
                when(invoice.invoice_date),
                when(invoice.due_date),
                invoice.po_number,
            ],
        ],
        colWidths=[52 * mm, 38 * mm, 38 * mm, 46 * mm],
    )
    meta.setStyle(
        TableStyle(
            [
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.grey),
                ("FONTSIZE", (0, 0), (-1, 0), 8),
                ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
                ("LINEABOVE", (0, 0), (-1, 0), 0.8, colors.black),
                ("LINEBELOW", (0, 1), (-1, 1), 0.8, colors.black),
            ]
        )
    )

    items = Table(
        [[text[k] for k in ("no", "description", "quantity", "unit_price", "amount")]]
        + _item_rows(record),
        colWidths=[10 * mm, 72 * mm, 14 * mm, 39 * mm, 39 * mm],
    )
    items.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.black),
                ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
            ]
        )
    )
    totals = Table(
        [
            [text["subtotal"], money(invoice.subtotal)],
            [text["tax"], money(invoice.tax)],
            [text["total"], money(invoice.total)],
        ],
        colWidths=[40 * mm, 44 * mm],
        hAlign="RIGHT",
    )
    totals.setStyle(
        TableStyle(
            [
                ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
                ("FONTNAME", (0, 2), (-1, 2), "Helvetica-Bold"),
                ("LINEABOVE", (0, 2), (-1, 2), 0.8, colors.black),
            ]
        )
    )
    payment = Table(
        [
            [
                Paragraph(
                    f"<b>{text['payment']}</b><br/>{spec.bank_name} &middot; {text['account']} "
                    f"{spec.bank_account} &middot; {text['holder']} {spec.name}",
                    normal,
                )
            ]
        ],
        colWidths=[174 * mm],
    )
    payment.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, colors.grey)]))

    story = [
        Paragraph(f"<b>{text['title']}</b>", title),
        Spacer(1, 6 * mm),
        parties,
        Spacer(1, 6 * mm),
        meta,
        Spacer(1, 8 * mm),
        items,
        Spacer(1, 4 * mm),
        totals,
        Spacer(1, 8 * mm),
        payment,
    ]
    if invoice.notes:
        story += [Spacer(1, 4 * mm), Paragraph(f"{text['notes']}: {invoice.notes}", normal)]
    _document(path).build(story)


def _render_compact(path: Path, record: GroundTruth, spec: VendorSpec) -> None:
    """Fixed-width ERP printout: key/value header, whitespace-aligned columns, no due date."""
    invoice, text = record.invoice, LABELS[record.language]
    rule = "-" * 86

    def money(value: Decimal) -> str:
        return format_money(value, record.language, record.layout)

    def field(label: str, value: str) -> str:
        return f"{label:<18}: {value}"

    lines = [
        spec.name,
        *spec.address,
        f"{text['tax_id']}: {spec.tax_id}",
        rule,
        text["title"],
        field(text["invoice_number"], invoice.invoice_number),
        field(text["date"], format_date(invoice.invoice_date, record.language, record.layout)),
        field(text["po_number"], invoice.po_number or ""),
        field(text["terms"], text["terms_value"].format(days=spec.payment_terms_days)),
        field(text["currency"], invoice.currency),
        field(text["bill_to"], BUYER_NAME),
        rule,
        f"{text['no']:<4}{text['description']:<36}{text['quantity']:>6}"
        f"{text['unit_price']:>20}{text['amount']:>20}",
        rule,
    ]
    for number, item in enumerate(invoice.line_items, start=1):
        lines.append(
            f"{number:<4}{item.description:<36}{int(item.quantity):>6}"
            f"{money(item.unit_price):>20}{money(item.amount):>20}"
        )
    lines += [
        rule,
        f"{text['subtotal']:>66}{money(invoice.subtotal):>20}",
        f"{text['tax']:>66}{money(invoice.tax):>20}",
        f"{text['total']:>66}{money(invoice.total):>20}",
        rule,
        text["payment"],
        field(text["bank"], spec.bank_name),
        field(text["account"], spec.bank_account),
        field(text["holder"], spec.name),
    ]
    if invoice.notes:
        lines += ["", f"{text['notes']}: {invoice.notes}"]

    pdf = Canvas(str(path), pagesize=A4, invariant=1)
    pdf.setTitle("Invoice")
    pdf.setFont("Courier", 9)
    y = A4[1] - 20 * mm
    for line in lines:
        pdf.drawString(18 * mm, y, line)
        y -= 12
    pdf.save()


_RENDERERS = {"classic": _render_classic, "modern": _render_modern, "compact": _render_compact}


# --- output ---------------------------------------------------------------------------


def _dump(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_dataset(dataset: Dataset, out: Path, seed: int) -> None:
    invoice_dir = out / INVOICE_DIR
    if invoice_dir.exists():
        shutil.rmtree(invoice_dir)  # a smaller rerun must not leave stale PDFs behind
    invoice_dir.mkdir(parents=True)

    for name, models in (
        (VENDORS_FILE, dataset.vendors),
        (PURCHASE_ORDERS_FILE, dataset.purchase_orders),
        (GOODS_RECEIPTS_FILE, dataset.goods_receipts),
        (GROUND_TRUTH_FILE, dataset.records),
    ):
        _dump(out / name, [model.model_dump(mode="json") for model in models])

    for record, spec in zip(dataset.records, dataset.specs, strict=True):
        _RENDERERS[record.layout](out / record.file, record, spec)

    counts: dict[str, int] = {}
    for record in dataset.records:
        counts[record.label] = counts.get(record.label, 0) + 1
    _dump(out / MANIFEST_FILE, {"seed": seed, "invoices": len(dataset.records), "labels": counts})


def load_ground_truth(out: Path) -> list[GroundTruth]:
    raw = json.loads((out / GROUND_TRUTH_FILE).read_text(encoding="utf-8"))
    return [GroundTruth.model_validate(item) for item in raw]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path(__file__).parent / "out")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--invoices", type=int, default=DEFAULT_INVOICES)
    args = parser.parse_args()

    dataset = build_dataset(args.seed, args.invoices)
    write_dataset(dataset, args.out, args.seed)
    labels = sorted({record.label for record in dataset.records})
    summary = ", ".join(
        f"{sum(r.label == label for r in dataset.records)} {label}" for label in labels
    )
    print(f"Wrote {len(dataset.records)} invoices ({summary}) to {args.out}")


if __name__ == "__main__":
    main()
