"""Synthetic accounts-payable dataset: vendor master, purchase orders, goods receipts,
digital invoice PDFs and per-invoice ground truth.

    uv run python data/generate.py [--out data/out] [--seed 20261112] [--invoices 30]

The same seed always produces byte-identical files. Labels come from how each invoice was
constructed, never from running the pipeline, so they can be used to grade it.

Eight classes (SPEC §8): clean, price mismatch, partial delivery, duplicate, wrong tax, no
PO, changed bank account, short payment terms. Each class is split half dev, half test:
iterate on dev; test is for reported numbers only.
"""

import argparse
import json
import random
import shutil
from collections import Counter
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

from lunas.config import DEFAULT_TAX_RATE
from lunas.erp import (
    GOODS_RECEIPTS_FILE,
    INVOICE_HISTORY_FILE,
    PURCHASE_ORDERS_FILE,
    VENDORS_FILE,
)
from lunas.models import (
    Contract,
    ContractClause,
    Decision,
    GoodsReceipt,
    GoodsReceiptLine,
    Invoice,
    InvoiceRecord,
    LineItem,
    PurchaseOrder,
    PurchaseOrderLine,
    Vendor,
)
from lunas.retrieval.questions import PRICE_QUESTION

DEFAULT_SEED = 20261112
DEFAULT_INVOICES = 300
GROUND_TRUTH_FILE = "ground_truth.json"
MANIFEST_FILE = "manifest.json"
INVOICE_DIR = "invoices"
CONTRACTS_FILE = "contracts.json"
CONTRACT_DIR = "contracts"
RETRIEVAL_CASES_FILE = "retrieval_cases.json"

TAX_RATE = DEFAULT_TAX_RATE
# The wrong_tax class bills this instead, and prints it.
WRONG_TAX_RATE = Decimal("0.12")
# Paid invoices per vendor already in the ledger before the dataset starts, so the duplicate
# check always has history to search, not only the originals of the duplicates.
HISTORY_PER_VENDOR = 3
# Every invoice stays below the human-approval threshold (50,000,000 in policies/default.yaml),
# so `clean` always means `approve`. Line value is capped so the worst case still fits:
# 4 lines x 9,000,000 x 1.15 price bump x 1.11 tax < 50,000,000.
MAX_LINE_VALUE = 9_000_000
MAX_INVOICE_TOTAL = Decimal(50_000_000)
FIRST_ORDER_DATE = date(2026, 9, 1)

Language = Literal["id", "en"]
Layout = Literal["classic", "modern", "compact"]
Label = Literal[
    "clean",
    "price_mismatch",
    "quantity_mismatch",
    "duplicate",
    "wrong_tax",
    "no_po",
    "bank_account_change",
    "payment_terms",
    "price_amendment",
]
Split = Literal["dev", "test"]

# Relative share of each class. Clean invoices are the majority, as in real AP traffic;
# every exception class gets the same share so each one is measurable.
CLASS_WEIGHTS: dict[Label, int] = {
    "clean": 8,
    "price_mismatch": 2,
    "quantity_mismatch": 2,
    "duplicate": 2,
    "wrong_tax": 2,
    "no_po": 2,
    "bank_account_change": 2,
    "payment_terms": 2,
    "price_amendment": 2,
}

# What the pipeline must conclude for each class (and variant), and from which issues.
EXPECTED: dict[tuple[Label, str | None], tuple[Decision, list[str]]] = {
    ("clean", None): (Decision.APPROVE, []),
    ("price_mismatch", None): (Decision.HOLD, ["price_mismatch"]),
    ("quantity_mismatch", None): (Decision.HOLD, ["quantity_exceeds_received"]),
    ("duplicate", "exact"): (Decision.REJECT, ["duplicate_invoice"]),
    ("duplicate", "renumbered"): (Decision.HOLD, ["possible_duplicate"]),
    ("wrong_tax", None): (Decision.HOLD, ["tax_mismatch"]),
    ("no_po", "missing"): (Decision.REJECT, ["po_missing"]),
    ("no_po", "unknown"): (Decision.REJECT, ["po_not_found"]),
    ("bank_account_change", None): (Decision.ESCALATE, ["bank_account_mismatch"]),
    ("payment_terms", None): (Decision.HOLD, ["payment_terms_mismatch"]),
    # The invoice bills the price of the vendor's contract addendum, not the PO price. The
    # checks see a price mismatch; the contract check must clear it once the addendum is
    # in effect, and only then.
    ("price_amendment", "in_effect"): (Decision.APPROVE, []),
    ("price_amendment", "too_early"): (Decision.HOLD, ["price_mismatch"]),
}
# Issues the deterministic checks raise that the contract check is expected to clear.
CLEARED_BY_CONTRACT: dict[tuple[Label, str | None], list[str]] = {
    ("price_amendment", "in_effect"): ["price_mismatch"],
}

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

# The ten vendors above are written by hand. The rest of the vendor master is generated
# from these pools with a fixed seed, so it is the same on every run and for every --seed.
VENDOR_COUNT = 40
_COMPANY_WORDS = [
    "Cahaya",
    "Mandiri",
    "Sentosa",
    "Karya",
    "Prima",
    "Utama",
    "Bumi",
    "Sinar",
    "Lestari",
    "Gemilang",
    "Harapan",
    "Tunggal",
    "Sakti",
    "Mulia",
    "Perkasa",
    "Cemerlang",
    "Bintang",
]
_ENGLISH_WORDS = [
    "Summit",
    "Harbor",
    "Pacific",
    "Evergreen",
    "Golden",
    "Northern",
    "Atlas",
    "Pioneer",
    "Crescent",
    "Meridian",
    "Orchid",
    "Sterling",
    "Coral",
    "Horizon",
    "Keystone",
    "Island",
]
_TRADE: dict[str, tuple[str, str]] = {
    "office": ("Alat Kantor", "Office Supply"),
    "it": ("Teknologi", "Systems"),
    "cleaning": ("Kebersihan", "Hygiene"),
    "packaging": ("Kemasan", "Packaging"),
    "pantry": ("Boga", "Foods"),
}
_STREETS = [
    "Jl. Diponegoro",
    "Jl. Pahlawan",
    "Jl. Ahmad Yani",
    "Jl. Merdeka",
    "Jl. Veteran",
    "Jl. Sisingamangaraja",
    "Jl. Gajah Mada",
    "Jl. Thamrin",
    "Jl. Pemuda",
    "Jl. Siliwangi",
]
_CITIES = [
    ("Jakarta Pusat", 10110),
    ("Bandung", 40115),
    ("Surabaya", 60271),
    ("Medan", 20112),
    ("Semarang", 50134),
    ("Makassar", 90111),
    ("Denpasar", 80113),
    ("Malang", 65111),
    ("Bekasi", 17141),
    ("Tangerang", 15111),
    ("Palembang", 30111),
    ("Bogor", 16121),
]
_BANKS = [
    "Bank Central Asia",
    "Bank Mandiri",
    "Bank Negara Indonesia",
    "Bank Rakyat Indonesia",
    "Bank CIMB Niaga",
    "Bank Permata",
    "Bank Syariah Indonesia",
    "Bank Danamon",
]
_NUMBER_FORMATS = [
    "INV/{{yyyy}}/{{mm}}/{{seq:04d}}", "INV-{code}-{{seq:05d}}",
    "{{seq:04d}}/{code}/{{roman}}/{{yyyy}}", "{code}-{{yy}}-{{seq:05d}}",
    "{code}/{{yyyy}}/{{seq:05d}}", "{code}{{yy}}{{mm}}{{seq:04d}}",
]  # fmt: skip


def _generated_vendors(count: int) -> list[VendorSpec]:
    rng = random.Random("lunas-vendors")
    taken = {spec.name for spec in VENDORS}
    specs = []
    while len(specs) < count:
        index = len(specs)
        language: Language = "id" if index % 2 == 0 else "en"
        category = list(_TRADE)[index % len(_TRADE)]
        trade = _TRADE[category][_LANGUAGE_INDEX[language]]
        if language == "id":
            words = rng.sample(_COMPANY_WORDS, 2)
            name = f"{rng.choice(['PT', 'CV'])} {words[0]} {words[1]} {trade}"
        else:
            name = f"{rng.choice(_ENGLISH_WORDS)} {trade} {rng.choice(['Ltd', 'Co', 'Group'])}"
        if name in taken:
            continue
        taken.add(name)
        city, postcode = rng.choice(_CITIES)
        code = "".join(word[0] for word in name.replace("PT ", "").replace("CV ", "").split())[:3]
        slug = "".join(ch for ch in name.lower() if ch.isalnum())[:18]
        specs.append(
            VendorSpec(
                name=name,
                language=language,
                layout=("classic", "modern", "compact")[index % 3],
                category=category,
                address=(f"{rng.choice(_STREETS)} No. {rng.randint(1, 250)}", f"{city} {postcode}"),
                tax_id=f"{rng.randint(11, 99)}.{rng.randint(100, 999)}.{rng.randint(100, 999)}."
                f"{rng.randint(0, 9)}-{rng.randint(100, 999)}.000",
                bank_name=rng.choice(_BANKS),
                bank_account="".join(str(rng.randint(0, 9)) for _ in range(rng.randint(10, 13))),
                email=f"finance@{slug}.example",
                payment_terms_days=rng.choice([14, 30, 45]),
                invoice_number_format=rng.choice(_NUMBER_FORMATS).format(code=code.upper()),
            )
        )
    return specs


VENDORS += _generated_vendors(VENDOR_COUNT - len(VENDORS))

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

# The bank_account_change class: a new account number on the invoice, and this note.
BANK_CHANGE_NOTES: dict[Language, str] = {
    "id": "Mohon diperhatikan: rekening pembayaran kami telah berubah. Silakan transfer ke "
    "rekening baru yang tercantum di atas.",
    "en": "Please note that our bank details have changed. Kindly transfer to the new "
    "account shown above.",
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
        "tax": "PPN {rate}%",
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
        "tax": "VAT {rate}%",
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
    # How the class was built, where it is built more than one way.
    # no_po: missing | unknown. duplicate: exact | renumbered.
    variant: str | None = None
    split: Split
    language: Language
    layout: Layout
    invoice: Invoice
    expected_decision: Decision
    expected_issue_codes: list[str]
    # What the deterministic checks alone find, before the contract check.
    expected_check_codes: list[str]
    # The contract clause a correct contract check cites, where one applies.
    expected_citation: str | None = None


@dataclass(frozen=True)
class Dataset:
    vendors: list[Vendor]
    purchase_orders: list[PurchaseOrder]
    goods_receipts: list[GoodsReceipt]
    invoice_history: list[InvoiceRecord]
    # Parallel lists: records[i] was issued by specs[i].
    records: list[GroundTruth]
    specs: list[VendorSpec]
    contracts: list[Contract]
    retrieval_cases: list[RetrievalCase]


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


def _short_delivery(rng: random.Random, ordered: list[Decimal]) -> list[Decimal]:
    """One or two lines arrive short. A line with nothing received is left off the receipt."""
    received = list(ordered)
    for index in rng.sample(range(len(ordered)), k=rng.randint(1, min(2, len(ordered)))):
        received[index] = Decimal(rng.randint(0, int(ordered[index]) - 1))
    return received


def _new_account(rng: random.Random, current: str) -> str:
    while (account := "".join(str(rng.randint(0, 9)) for _ in current)) == current:
        pass
    return account


def _class_counts(invoices: int) -> dict[Label, int]:
    """Split `invoices` by CLASS_WEIGHTS, largest remainder first. Deterministic."""
    total = sum(CLASS_WEIGHTS.values())
    exact = {label: invoices * weight / total for label, weight in CLASS_WEIGHTS.items()}
    counts = {label: int(share) for label, share in exact.items()}
    by_remainder = sorted(exact, key=lambda label: exact[label] - counts[label], reverse=True)
    for label in by_remainder[: invoices - sum(counts.values())]:
        counts[label] += 1
    return counts


def _assign_splits(rng: random.Random, labels: list[Label]) -> list[Split]:
    """Half of every class to dev and half to test, so both splits hold every class. With
    an odd count, dev gets the extra one."""
    splits: list[Split] = ["dev"] * len(labels)
    for label in CLASS_WEIGHTS:
        indices = [index for index, other in enumerate(labels) if other == label]
        rng.shuffle(indices)
        for index in indices[1::2]:
            splits[index] = "test"
    return splits


def _past_invoices(rng: random.Random, starts: list[int]) -> list[InvoiceRecord]:
    """Invoices each vendor sent before the dataset begins, all paid. Their numbers come
    before every number in the dataset, so none of them is a duplicate by number."""
    records = []
    for index, spec in enumerate(VENDORS):
        for back in range(1, HISTORY_PER_VENDOR + 1):
            issued = FIRST_ORDER_DATE - timedelta(days=30 * back + rng.randint(0, 9))
            records.append(
                InvoiceRecord(
                    vendor_id=_vendor_id(index),
                    invoice_number=_invoice_number(spec, issued, starts[index] - 10 * back),
                    invoice_date=issued,
                    total=Decimal(rng.randint(5, 300) * 111_000),
                    po_number=f"PO-{issued.year}-H{index + 1:02d}{back}",
                )
            )
    return records


def build_dataset(seed: int = DEFAULT_SEED, invoices: int = DEFAULT_INVOICES) -> Dataset:
    """One PO, goods receipt and invoice per case, each case built to be one class."""
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

    labels = [label for label, count in _class_counts(invoices).items() for _ in range(count)]
    rng.shuffle(labels)
    splits = _assign_splits(rng, labels)
    starts = [rng.randint(40, 900) for _ in VENDORS]
    sequences = list(starts)
    history = _past_invoices(rng, starts)
    contracts, retrieval_cases, amendments = build_contracts(seed, vendors)
    amended_vendors = [i for i, v in enumerate(vendors) if v.vendor_id in amendments]
    amendment_cases = 0

    purchase_orders, goods_receipts, records, specs = [], [], [], []
    for index, (label, split) in enumerate(zip(labels, splits, strict=True)):
        # Round-robin over vendors so every layout and language appears in a small dataset.
        # A price_amendment case needs a vendor whose contract has an addendum.
        vendor_index = index % len(VENDORS)
        amendment: Amendment | None = None
        if label == "price_amendment":
            vendor_index = amended_vendors[amendment_cases % len(amended_vendors)]
            amendment = amendments[vendors[vendor_index].vendor_id]
            amendment_cases += 1
        spec = VENDORS[vendor_index]
        number = index + 1
        variant: str | None = None

        order_date = FIRST_ORDER_DATE + timedelta(days=rng.randint(0, 45))
        received_date = order_date + timedelta(days=rng.randint(3, 10))
        invoice_date = received_date + timedelta(days=rng.randint(0, 5))
        po_lines = _po_lines(rng, spec)
        if amendment is not None:
            # Ordered at the old price; invoiced at the addendum's price, either after the
            # addendum took effect or before it did.
            variant = rng.choice(["in_effect", "too_early"])
            if variant == "in_effect":
                invoice_date = amendment.effective + timedelta(days=rng.randint(1, 20))
            else:
                invoice_date = amendment.effective - timedelta(days=rng.randint(3, 15))
            received_date = invoice_date - timedelta(days=rng.randint(0, 3))
            order_date = received_date - timedelta(days=rng.randint(5, 15))
            po_lines = [
                line for line in po_lines if line.description != amendment.item
            ] or po_lines[1:]
            old_price = Decimal(int(amendment.base_price * rng.uniform(0.9, 1.0)) // 100 * 100)
            quantity = rng.randint(1, max(1, min(20, MAX_LINE_VALUE // int(amendment.price))))
            po_lines.insert(
                0,
                PurchaseOrderLine(
                    description=amendment.item,
                    quantity=Decimal(quantity),
                    unit_price=old_price,
                ),
            )
            po_lines = po_lines[:4]

        po_number: str | None = f"PO-{order_date.year}-{number:04d}"
        if label == "no_po":
            # Neither variant has a PO in the ERP: one prints none, one cites a number
            # that was never issued.
            variant = rng.choice(["missing", "unknown"])
            po_number = None if variant == "missing" else f"PO-{order_date.year}-{9000 + number}"
        else:
            purchase_orders.append(
                PurchaseOrder(
                    po_number=po_number,
                    vendor_id=_vendor_id(vendor_index),
                    order_date=order_date,
                    lines=po_lines,
                )
            )
            received = [line.quantity for line in po_lines]
            if label == "quantity_mismatch":
                received = _short_delivery(rng, received)
            receipt_lines = [
                GoodsReceiptLine(description=line.description, quantity=quantity)
                for line, quantity in zip(po_lines, received, strict=True)
                if quantity > 0
            ]
            if receipt_lines:
                goods_receipts.append(
                    GoodsReceipt(
                        gr_number=f"GR-{received_date.year}-{number:04d}",
                        po_number=po_number,
                        received_date=received_date,
                        lines=receipt_lines,
                    )
                )

        # The invoice always bills what was ordered: a short delivery is billed in full.
        prices = [line.unit_price for line in po_lines]
        if label == "price_mismatch":
            prices = _overcharge(rng, prices)
        if amendment is not None:
            prices[0] = amendment.price
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
        tax = subtotal * tax_rate(label)
        assert tax == tax.to_integral_value(), "prices must keep tax in whole rupiah"
        tax = tax.to_integral_value()
        total = subtotal + tax
        assert total < MAX_INVOICE_TOTAL, "invoice must stay below the approval threshold"

        terms = spec.payment_terms_days
        if label == "payment_terms":
            terms = rng.choice([days for days in (7, 10) if days < spec.payment_terms_days])
        bank_account = spec.bank_account
        notes = rng.choice(NOTES[spec.language])
        if label == "bank_account_change":
            bank_account = _new_account(rng, spec.bank_account)
            notes = BANK_CHANGE_NOTES[spec.language]

        sequences[vendor_index] += rng.randint(1, 9)
        original_number = _invoice_number(spec, invoice_date, sequences[vendor_index])
        printed_number, printed_date = original_number, invoice_date
        if label == "duplicate":
            # The original is already in the ledger, paid. The PDF is either the same
            # invoice again, or the same bill under a new number and date.
            history.append(
                InvoiceRecord(
                    vendor_id=_vendor_id(vendor_index),
                    invoice_number=original_number,
                    invoice_date=invoice_date,
                    total=total,
                    po_number=po_number,
                )
            )
            variant = rng.choice(["exact", "renumbered"])
            if variant == "renumbered":
                sequences[vendor_index] += rng.randint(1, 9)
                printed_date = invoice_date + timedelta(days=rng.randint(7, 20))
                printed_number = _invoice_number(spec, printed_date, sequences[vendor_index])

        invoice = Invoice(
            vendor_name=spec.name,
            invoice_number=printed_number,
            invoice_date=printed_date,
            # The compact layout prints payment terms instead of a due date. Each field is
            # set only where the document prints it: a reader that derives one from the
            # other has computed it, not read it.
            due_date=None if spec.layout == "compact" else printed_date + timedelta(days=terms),
            payment_terms_days=terms if spec.layout == "compact" else None,
            po_number=po_number,
            line_items=line_items,
            subtotal=subtotal,
            tax=tax,
            total=total,
            bank_account=bank_account,
            notes=notes,
        )

        decision, issue_codes = EXPECTED[(label, variant)]
        records.append(
            GroundTruth(
                id=f"inv-{number:03d}",
                file=f"{INVOICE_DIR}/inv-{number:03d}.pdf",
                label=label,
                variant=variant,
                split=split,
                language=spec.language,
                layout=spec.layout,
                invoice=invoice,
                expected_decision=decision,
                expected_issue_codes=issue_codes,
                expected_check_codes=sorted(
                    issue_codes + CLEARED_BY_CONTRACT.get((label, variant), [])
                ),
                expected_citation=amendment.clause_id if amendment else None,
            )
        )
        specs.append(spec)

    return Dataset(
        vendors,
        purchase_orders,
        goods_receipts,
        history,
        records,
        specs,
        contracts,
        retrieval_cases,
    )


# --- contracts ------------------------------------------------------------------------

# Every contract has the same articles in the vendor's language. Each paragraph has a kind,
# which is never shown to a model: it only labels which paragraphs answer which question.
_CATEGORY_NAMES: dict[str, tuple[str, str]] = {
    "office": ("perlengkapan kantor", "office supplies"),
    "it": ("perangkat dan lisensi teknologi informasi", "IT equipment and licences"),
    "cleaning": ("perlengkapan dan jasa kebersihan", "cleaning supplies and services"),
    "packaging": ("bahan kemasan", "packaging materials"),
    "pantry": ("kebutuhan pantri dan katering", "pantry supplies and catering"),
}
_ARTICLES: list[tuple[tuple[str, str], list[tuple[str, str, str]]]] = [
    (("Ruang Lingkup", "Scope"), [
        ("scope",
         "PIHAK KEDUA menyediakan {category} kepada PIHAK PERTAMA berdasarkan Purchase Order "
         "yang diterbitkan oleh PIHAK PERTAMA.",
         "The Supplier shall supply {category} to the Buyer against purchase orders issued "
         "by the Buyer."),
        ("scope",
         "Setiap Purchase Order merupakan bagian yang tidak terpisahkan dari Perjanjian ini.",
         "Each purchase order forms an integral part of this Agreement."),
    ]),
    (("Harga", "Prices"), [
        ("price_fixed",
         "Harga satuan barang tercantum dalam Lampiran A dan berlaku tetap selama jangka "
         "waktu Perjanjian.",
         "Unit prices are set out in Schedule A and remain fixed for the term of this "
         "Agreement."),
        ("price_change",
         "Perubahan harga satuan hanya berlaku melalui addendum tertulis yang ditandatangani "
         "kedua pihak dan tidak berlaku surut.",
         "A change to a unit price takes effect only through a written addendum signed by "
         "both parties, and never retroactively."),
        ("price_excludes_tax",
         "Harga belum termasuk Pajak Pertambahan Nilai.",
         "Prices exclude value added tax."),
    ]),
    (("Pajak", "Tax"), [
        ("tax",
         "Setiap faktur dikenakan PPN sebesar {tax}% dari subtotal sebagai Dasar Pengenaan "
         "Pajak.",
         "Each invoice carries VAT at {tax}% of the subtotal, which is the tax base."),
    ]),
    (("Penagihan", "Invoicing"), [
        ("invoice_po",
         "Faktur wajib mencantumkan nomor Purchase Order yang menjadi dasar penagihan.",
         "Every invoice must state the number of the purchase order it bills."),
        ("invoice_received",
         "PIHAK KEDUA hanya dapat menagih barang yang telah diterima PIHAK PERTAMA dan "
         "tercatat dalam berita acara penerimaan barang.",
         "The Supplier may invoice only goods that the Buyer has received and recorded in "
         "a goods receipt."),
        ("invoice_once",
         "Satu pengiriman hanya boleh ditagih satu kali; faktur yang diajukan ulang dengan "
         "nomor yang sama atau berbeda akan ditolak.",
         "Each delivery may be invoiced once only; an invoice resubmitted under the same or "
         "a different number will be rejected."),
    ]),
    (("Pembayaran", "Payment"), [
        ("payment_terms",
         "PIHAK PERTAMA membayar faktur yang lengkap dan benar paling lambat {terms} hari "
         "kalender sejak tanggal faktur.",
         "The Buyer pays a complete and correct invoice within {terms} calendar days of the "
         "invoice date."),
        ("payment_account",
         "Pembayaran ditransfer ke rekening PIHAK KEDUA di {bank} nomor {account} atas nama "
         "{vendor}.",
         "Payment is made by transfer to the Supplier's account at {bank}, number {account}, "
         "in the name of {vendor}."),
        ("late_payment",
         "Atas keterlambatan pembayaran, PIHAK KEDUA berhak atas denda {late}% per bulan dari "
         "jumlah yang terlambat.",
         "Late payment accrues interest of {late}% per month on the overdue amount."),
    ]),
    (("Perubahan Rekening", "Change of Bank Details"), [
        ("bank_change",
         "Perubahan rekening tujuan pembayaran hanya berlaku setelah PIHAK KEDUA mengirimkan "
         "surat resmi yang ditandatangani direksi dan PIHAK PERTAMA memverifikasinya melalui "
         "telepon ke nomor yang terdaftar.",
         "A change of the payment account takes effect only after the Supplier sends a "
         "formal letter signed by a director and the Buyer verifies it by telephone on the "
         "registered number."),
        ("bank_change",
         "Permintaan perubahan rekening yang hanya disampaikan melalui faktur atau email "
         "tidak berlaku.",
         "A request to change the account made only on an invoice or by email has no "
         "effect."),
    ]),
    (("Pengiriman", "Delivery"), [
        ("delivery",
         "PIHAK KEDUA mengirimkan barang paling lambat {delivery} hari kalender sejak "
         "Purchase Order diterima.",
         "The Supplier delivers within {delivery} calendar days of receiving a purchase "
         "order."),
        ("delivery_partial",
         "Pengiriman sebagian diperbolehkan; sisa barang dikirim dan ditagih setelah "
         "diterima.",
         "Partial deliveries are allowed; the remainder is delivered and invoiced once "
         "received."),
    ]),
    (("Jangka Waktu", "Term"), [
        ("term",
         "Perjanjian ini berlaku sejak {start} sampai dengan {end}.",
         "This Agreement runs from {start} to {end}."),
        ("term",
         "Perjanjian dapat diperpanjang dengan kesepakatan tertulis kedua pihak.",
         "The Agreement may be extended by written agreement of both parties."),
    ]),
    (("Pengakhiran", "Termination"), [
        ("termination",
         "Masing-masing pihak dapat mengakhiri Perjanjian dengan pemberitahuan tertulis "
         "{notice} hari sebelumnya.",
         "Either party may terminate this Agreement on {notice} days' written notice."),
    ]),
    (("Hukum yang Berlaku", "Governing Law"), [
        ("law",
         "Perjanjian ini tunduk pada hukum Republik Indonesia; sengketa diselesaikan di "
         "Pengadilan Negeri Jakarta Selatan.",
         "This Agreement is governed by the laws of the Republic of Indonesia; disputes go "
         "to the South Jakarta District Court."),
    ]),
]  # fmt: skip
_AMENDMENT = (
    ("Addendum", "Addendum"),
    "price_amendment",
    "Addendum No. 1 tanggal {signed}: terhitung sejak {effective}, harga satuan {item} "
    "diubah menjadi {price} per unit.",
    "Addendum No. 1 dated {signed}: from {effective}, the unit price of {item} is changed "
    "to {price} per unit.",
)
# Questions that cite a clause by number, which embeddings handle poorly and keyword search
# handles well. One per contract and language, about a randomly chosen clause.
CLAUSE_REFERENCE_QUESTION = (
    "Apa bunyi Pasal {number} dalam kontrak dengan {vendor}?",
    "What does clause {number} of the contract with {vendor} say?",
)
# Share of contracts with a price addendum. Week 2 adds the invoice class that relies on it.
AMENDMENT_SHARE = 0.35

# Questions a contract check asks, in both languages, and the paragraph kinds that answer
# them. The vendor's name is in the question so that search without a vendor filter is a
# fair, harder test.
RETRIEVAL_QUESTIONS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "price_change": (
        PRICE_QUESTION["id"],
        PRICE_QUESTION["en"],
        ("price_fixed", "price_change", "price_amendment"),
    ),
    "payment_terms": (
        "Berapa hari termin pembayaran faktur dari {vendor}?",
        "How many days do we have to pay an invoice from {vendor}?",
        ("payment_terms",),
    ),
    "payment_account": (
        "Ke rekening mana pembayaran untuk {vendor} ditransfer?",
        "Which bank account do we pay {vendor} into?",
        ("payment_account",),
    ),
    "bank_change": (
        "Faktur {vendor} meminta transfer ke rekening baru. Apa syaratnya?",
        "An invoice from {vendor} asks us to pay a new bank account. What is required?",
        ("bank_change",),
    ),
    "tax": (
        "Berapa tarif PPN pada faktur {vendor}?",
        "What VAT rate applies to invoices from {vendor}?",
        ("tax",),
    ),
    "partial_delivery": (
        "Bolehkah {vendor} menagih barang yang belum diterima seluruhnya?",
        "May {vendor} invoice goods that have not all been received?",
        ("invoice_received", "delivery_partial"),
    ),
    "duplicate": (
        "{vendor} mengirim ulang faktur yang sudah dibayar. Apa kata kontrak?",
        "{vendor} resent an invoice we already paid. What does the contract say?",
        ("invoice_once",),
    ),
    "po_required": (
        "Apakah faktur dari {vendor} wajib mencantumkan nomor PO?",
        "Must invoices from {vendor} quote a purchase order number?",
        ("invoice_po",),
    ),
}


class RetrievalCase(BaseModel):
    """A question about one vendor's contract and the clauses that answer it."""

    id: str
    vendor_id: str
    # By vendor: a vendor's contract is either iterated on (dev) or held out (test).
    split: Split
    language: Language
    kind: str
    query: str
    relevant: list[str]


@dataclass(frozen=True)
class Amendment:
    """A contract addendum that sets a new unit price for one item from a date."""

    clause_id: str
    item: str  # in the contract's language, as on the PO
    base_price: int  # the catalogue price the old contract price drifts around
    price: Decimal
    effective: date


def build_contracts(
    seed: int, vendors: list[Vendor]
) -> tuple[list[Contract], list[RetrievalCase], dict[str, Amendment]]:
    rng = random.Random(f"contracts-{seed}")
    vendor_ids = [vendor.vendor_id for vendor in vendors]
    rng.shuffle(vendor_ids)
    test_vendors = set(vendor_ids[1::2])

    contracts: list[Contract] = []
    cases: list[RetrievalCase] = []
    amendments: dict[str, Amendment] = {}
    for vendor, spec in zip(vendors, VENDORS, strict=True):
        lang = _LANGUAGE_INDEX[spec.language]
        start = date(2026, 1, 1)
        end = date(2027, 12, 31)
        items = CATALOG[spec.category]
        amended = rng.choice(items) if rng.random() < AMENDMENT_SHARE else None
        asked_item = (amended or rng.choice(items))[lang]
        values = {
            "category": _CATEGORY_NAMES[spec.category][lang],
            "tax": f"{TAX_RATE * 100:.0f}",
            "terms": spec.payment_terms_days,
            "bank": spec.bank_name,
            "account": spec.bank_account,
            "vendor": spec.name,
            "late": rng.choice(["1", "1,5" if spec.language == "id" else "1.5", "2"]),
            "delivery": rng.choice([5, 7, 14]),
            "start": format_date(start, spec.language, "classic"),
            "end": format_date(end, spec.language, "classic"),
            "notice": rng.choice([30, 60, 90]),
        }
        articles = list(_ARTICLES)
        if amended:
            signed = date(2026, 8, rng.randint(1, 28))
            effective = date(2026, 9, 1) + timedelta(days=rng.randint(0, 40))
            price = Decimal(int(amended[2] * 1.08) // 100 * 100)
            text = _AMENDMENT[2 + lang].format(
                signed=format_date(signed, spec.language, "classic"),
                effective=format_date(effective, spec.language, "classic"),
                item=amended[lang],
                price=f"Rp {format_money(price, 'id', 'classic')}"
                if spec.language == "id"
                else f"IDR {format_money(price, 'en', 'classic')}",
            )
            articles.append((_AMENDMENT[0], [(_AMENDMENT[1], text, text)]))
            amendments[vendor.vendor_id] = Amendment(
                clause_id=f"KTR-{vendor.vendor_id}#{len(articles)}.1",
                item=amended[lang],
                base_price=amended[2],
                price=price,
                effective=effective,
            )

        contract_id = f"KTR-{vendor.vendor_id}"
        clauses, kinds = [], {}
        for article_number, (titles, paragraphs) in enumerate(articles, start=1):
            for paragraph_number, (kind, text_id, text_en) in enumerate(paragraphs, start=1):
                number = f"{article_number}.{paragraph_number}"
                clause_id = f"{contract_id}#{number}"
                kinds[clause_id] = kind
                clauses.append(
                    ContractClause(
                        clause_id=clause_id,
                        contract_id=contract_id,
                        vendor_id=vendor.vendor_id,
                        number=number,
                        article=str(article_number),
                        article_title=titles[lang],
                        text=(text_id, text_en)[lang].format(**values),
                    )
                )
        title = (
            f"Perjanjian Pengadaan antara {BUYER_NAME} dan {spec.name}"
            if spec.language == "id"
            else f"Supply Agreement between {BUYER_NAME} and {spec.name}"
        )
        contracts.append(
            Contract(
                contract_id=contract_id,
                vendor_id=vendor.vendor_id,
                vendor_name=spec.name,
                language=spec.language,
                title=title,
                effective_date=start,
                end_date=end,
                clauses=clauses,
            )
        )

        split: Split = "test" if vendor.vendor_id in test_vendors else "dev"
        for kind, (question_id, question_en, answers) in RETRIEVAL_QUESTIONS.items():
            relevant = [clause_id for clause_id, k in kinds.items() if k in answers]
            for language, question in (("id", question_id), ("en", question_en)):
                cases.append(
                    RetrievalCase(
                        id=f"{vendor.vendor_id}-{kind}-{language}",
                        vendor_id=vendor.vendor_id,
                        split=split,
                        language=language,
                        kind=kind,
                        query=question.format(vendor=spec.name, item=asked_item),
                        relevant=relevant,
                    )
                )
        cited = rng.choice(clauses)
        for language, question in zip(("id", "en"), CLAUSE_REFERENCE_QUESTION, strict=True):
            cases.append(
                RetrievalCase(
                    id=f"{vendor.vendor_id}-clause_reference-{language}",
                    vendor_id=vendor.vendor_id,
                    split=split,
                    language=language,
                    kind="clause_reference",
                    query=question.format(number=cited.number, vendor=spec.name),
                    relevant=[cited.clause_id],
                )
            )
    return contracts, cases, amendments


def render_contract(contract: Contract) -> str:
    """The contract as a person would read it."""
    lines = [f"# {contract.title}", "", f"`{contract.contract_id}`", ""]
    article = None
    for clause in contract.clauses:
        if clause.article != article:
            article = clause.article
            word = "Pasal" if contract.language == "id" else "Article"
            lines += ["", f"## {word} {clause.article}. {clause.article_title}", ""]
        lines.append(f"{clause.number}. {clause.text}")
    return "\n".join(lines).strip() + "\n"


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


def tax_rate(label: Label) -> Decimal:
    """The rate an invoice of this class is billed at, and prints."""
    return WRONG_TAX_RATE if label == "wrong_tax" else TAX_RATE


def _tax_label(record: GroundTruth) -> str:
    return LABELS[record.language]["tax"].format(rate=f"{tax_rate(record.label) * 100:.0f}")


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
    meta_lines = [
        f"{text['invoice_number']}: {invoice.invoice_number}",
        f"{text['date']}: {when(invoice.invoice_date)}",
        f"{text['due_date']}: {when(invoice.due_date)}",
    ]
    if invoice.po_number:
        meta_lines.append(f"{text['po_number']}: {invoice.po_number}")
    meta = Paragraph(
        f"<b><font size=18>{text['title']}</font></b><br/><br/>" + "<br/>".join(meta_lines),
        right,
    )
    header = Table([[letterhead, meta]], colWidths=[95 * mm, 79 * mm])
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))

    items = Table(
        [[text[k] for k in ("no", "description", "quantity", "unit_price", "amount")]]
        + _item_rows(record)
        + [
            ["", "", "", text["subtotal"], money(invoice.subtotal)],
            ["", "", "", _tax_label(record), money(invoice.tax)],
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
            f"{text['account']}: {invoice.bank_account}<br/>{text['holder']}: {spec.name}",
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

    headers = [text["invoice_number"], text["date"], text["due_date"]]
    values = [invoice.invoice_number, when(invoice.invoice_date), when(invoice.due_date)]
    widths = [60 * mm, 57 * mm, 57 * mm]
    if invoice.po_number:
        headers.append(text["po_number"])
        values.append(invoice.po_number)
        widths = [52 * mm, 38 * mm, 38 * mm, 46 * mm]
    meta = Table([headers, values], colWidths=widths)
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
            [_tax_label(record), money(invoice.tax)],
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
                    f"{invoice.bank_account} &middot; {text['holder']} {spec.name}",
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
        *([field(text["po_number"], invoice.po_number)] if invoice.po_number else []),
        field(text["terms"], text["terms_value"].format(days=invoice.payment_terms_days)),
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
        f"{_tax_label(record):>66}{money(invoice.tax):>20}",
        f"{text['total']:>66}{money(invoice.total):>20}",
        rule,
        text["payment"],
        field(text["bank"], spec.bank_name),
        field(text["account"], invoice.bank_account or ""),
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
        (INVOICE_HISTORY_FILE, dataset.invoice_history),
        (GROUND_TRUTH_FILE, dataset.records),
        (CONTRACTS_FILE, dataset.contracts),
        (RETRIEVAL_CASES_FILE, dataset.retrieval_cases),
    ):
        _dump(out / name, [model.model_dump(mode="json") for model in models])

    contract_dir = out / CONTRACT_DIR
    if contract_dir.exists():
        shutil.rmtree(contract_dir)
    contract_dir.mkdir()
    for contract in dataset.contracts:
        (contract_dir / f"{contract.contract_id}.md").write_text(
            render_contract(contract), encoding="utf-8"
        )

    for record, spec in zip(dataset.records, dataset.specs, strict=True):
        _RENDERERS[record.layout](out / record.file, record, spec)

    labels = Counter(record.label for record in dataset.records)
    splits = Counter(record.split for record in dataset.records)
    _dump(
        out / MANIFEST_FILE,
        {
            "seed": seed,
            "invoices": len(dataset.records),
            "labels": {label: labels[label] for label in CLASS_WEIGHTS},
            "splits": dict(sorted(splits.items())),
        },
    )


# What an eval may be asked to run on. Test is for reported numbers only.
SPLIT_CHOICES = ("dev", "test", "all")


def load_ground_truth(out: Path, split: str = "all") -> list[GroundTruth]:
    raw = json.loads((out / GROUND_TRUTH_FILE).read_text(encoding="utf-8"))
    records = [GroundTruth.model_validate(item) for item in raw]
    return [record for record in records if split in ("all", record.split)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path(__file__).parent / "out")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--invoices", type=int, default=DEFAULT_INVOICES)
    args = parser.parse_args()

    dataset = build_dataset(args.seed, args.invoices)
    write_dataset(dataset, args.out, args.seed)
    labels = Counter(record.label for record in dataset.records)
    summary = ", ".join(f"{labels[label]} {label}" for label in CLASS_WEIGHTS if labels[label])
    print(f"Wrote {len(dataset.records)} invoices ({summary}) to {args.out}")


if __name__ == "__main__":
    main()
