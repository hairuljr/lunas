from pathlib import Path

import pytest

from data.generate import build_dataset, load_ground_truth, write_dataset
from lunas.erp import ErpData
from lunas.ingest import choose_backend, get_reader
from lunas.pipeline.nodes.decide import decide
from lunas.pipeline.nodes.match import match_invoice
from lunas.pipeline.nodes.validate import validate_invoice

SEED = 7
# Twelve invoices cover all ten vendors, so every layout and language is rendered.
INVOICES = 12


@pytest.fixture(scope="module")
def out(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("dataset")
    write_dataset(build_dataset(SEED, INVOICES), path, SEED)
    return path


def test_dataset_has_both_classes_and_every_layout(out: Path):
    records = load_ground_truth(out)

    assert len(records) == INVOICES
    assert sorted(path.name for path in (out / "invoices").iterdir()) == [
        f"inv-{number:03d}.pdf" for number in range(1, INVOICES + 1)
    ]
    assert [r.label for r in records].count("price_mismatch") == INVOICES // 3
    assert {(r.layout, r.language) for r in records} == {
        (layout, language)
        for layout in ("classic", "modern", "compact")
        for language in ("id", "en")
    }


def test_same_seed_gives_identical_files(out: Path, tmp_path: Path):
    write_dataset(build_dataset(SEED, INVOICES), tmp_path, SEED)

    files = sorted(path.relative_to(out) for path in out.rglob("*") if path.is_file())
    assert files == sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file())
    for file in files:
        assert (out / file).read_bytes() == (tmp_path / file).read_bytes(), file


def test_different_seed_gives_different_invoices():
    assert build_dataset(1, 3).records != build_dataset(2, 3).records


def test_every_pdf_is_digital_and_prints_its_ground_truth(out: Path):
    for record in load_ground_truth(out):
        path = out / record.file
        assert choose_backend(path) == "pdf_text"

        text = get_reader("pdf_text").read(path).text
        invoice = record.invoice
        printed_digits = "".join(character for character in text if character.isdigit())
        assert invoice.vendor_name in text
        assert invoice.invoice_number in text
        assert invoice.po_number in text
        assert invoice.bank_account in text
        assert str(int(invoice.total)) in printed_digits
        for item in invoice.line_items:
            assert item.description in text


def test_ground_truth_invoices_are_arithmetically_consistent(out: Path):
    for record in load_ground_truth(out):
        assert validate_invoice(record.invoice) == [], record.id


def test_labels_agree_with_match_and_decide(out: Path):
    """The labels are set by construction in the generator; the pipeline code must agree."""
    erp = ErpData.load(out)
    for record in load_ground_truth(out):
        issues = match_invoice(record.invoice, erp)

        assert sorted({issue.code for issue in issues}) == record.expected_issue_codes, record.id
        assert decide(record.invoice, issues).decision == record.expected_decision, record.id
