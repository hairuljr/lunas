from collections import Counter
from pathlib import Path

import pytest

from data.generate import (
    CLASS_WEIGHTS,
    DEFAULT_INVOICES,
    EXPECTED,
    LABELS,
    VENDOR_COUNT,
    _class_counts,
    build_dataset,
    load_ground_truth,
    tax_rate,
    write_dataset,
)
from lunas.config import Settings
from lunas.erp import ErpData
from lunas.ingest import choose_backend, get_reader
from lunas.models import Decision, Issue
from lunas.pipeline.nodes.contract import amounts_in
from lunas.pipeline.nodes.decide import decide
from lunas.pipeline.nodes.investigate import POSSIBLE_CAUSES
from lunas.pipeline.nodes.route import Route, route_invoice
from lunas.pipeline.nodes.validate import validate_invoice
from lunas.pipeline.run import check_invoice

SEED = 7
# Sixteen invoices hold every class and all ten vendors, so every layout and language.
INVOICES = 16


@pytest.fixture(scope="module")
def out(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("dataset")
    write_dataset(build_dataset(SEED, INVOICES), path, SEED)
    return path


def test_dataset_has_every_class_layout_and_language(out: Path):
    records = load_ground_truth(out)

    assert len(records) == INVOICES
    assert sorted(path.name for path in (out / "invoices").iterdir()) == [
        f"inv-{number:03d}.pdf" for number in range(1, INVOICES + 1)
    ]
    assert {r.label for r in records} == set(CLASS_WEIGHTS)
    assert {(r.layout, r.language) for r in records} == {
        (layout, language)
        for layout in ("classic", "modern", "compact")
        for language in ("id", "en")
    }


@pytest.mark.parametrize("invoices", [11, 16, 300, 301])
def test_class_counts_add_up_and_give_every_class_a_share(invoices: int):
    counts = _class_counts(invoices)

    assert sum(counts.values()) == invoices
    assert min(counts.values()) >= 1
    assert max(counts, key=counts.__getitem__) == "clean"


def test_every_class_is_split_evenly_between_dev_and_test():
    records = build_dataset(SEED, 300).records
    per_class = Counter((record.label, record.split) for record in records)

    for label in CLASS_WEIGHTS:
        dev, test = per_class[(label, "dev")], per_class[(label, "test")]
        assert dev - test in (0, 1), label


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
        assert invoice.vendor_name in text, record.id
        assert invoice.invoice_number in text, record.id
        assert invoice.bank_account in text, record.id
        assert str(int(invoice.total)) in printed_digits, record.id
        for item in invoice.line_items:
            assert item.description in text, record.id
        # A field the ground truth leaves empty must not be printed either.
        po_label = LABELS[record.language]["po_number"]
        assert (po_label in text) == (invoice.po_number is not None), record.id
        if invoice.po_number:
            assert invoice.po_number in text, record.id
        terms = LABELS[record.language]["terms_value"]
        if invoice.payment_terms_days is not None:
            assert terms.format(days=invoice.payment_terms_days) in text, record.id
        assert f"{tax_rate(record.label) * 100:.0f}%" in text, record.id


def test_only_the_wrong_tax_class_breaks_the_arithmetic(out: Path):
    for record in load_ground_truth(out):
        codes = [issue.code for issue in validate_invoice(record.invoice)]
        assert codes == (["tax_mismatch"] if record.label == "wrong_tax" else []), record.id


def test_every_duplicate_has_its_original_in_the_ledger(out: Path):
    erp = ErpData.load(out)
    duplicates = [r for r in load_ground_truth(out) if r.label == "duplicate"]
    assert duplicates

    for record in duplicates:
        po = erp.get_po(record.invoice.po_number)
        (original,) = [
            previous
            for previous in erp.find_invoices(po.vendor_id)
            if previous.po_number == record.invoice.po_number
        ]
        same_number = original.invoice_number == record.invoice.invoice_number
        assert same_number == (record.variant == "exact"), record.id
        assert original.total == record.invoice.total, record.id


@pytest.mark.parametrize("invoices", [INVOICES, DEFAULT_INVOICES])
def test_labels_agree_with_the_checks_and_the_decision(invoices: int):
    """The labels are set by construction in the generator; the pipeline code must agree,
    for every class and variant at the default size too."""
    dataset = build_dataset(SEED, invoices)
    erp = ErpData(
        dataset.vendors, dataset.purchase_orders, dataset.goods_receipts, dataset.invoice_history
    )
    settings = Settings(_env_file=None)
    if invoices == DEFAULT_INVOICES:
        assert {(r.label, r.variant) for r in dataset.records} == set(EXPECTED)

    for record in dataset.records:
        issues = check_invoice(record.invoice, erp, settings)
        codes = sorted({issue.code for issue in issues})
        assert codes == record.expected_check_codes, record.id

        decision = decide(record.invoice, issues).decision
        if record.expected_check_codes == record.expected_issue_codes:
            assert decision == record.expected_decision, record.id
        else:
            # Only the contract check can clear these; without it the invoice is held.
            assert (record.label, decision) == ("price_amendment", Decision.HOLD), record.id


def test_every_vendor_has_a_contract_that_matches_the_vendor_master():
    dataset = build_dataset(SEED, 3)
    vendors = {vendor.vendor_id: vendor for vendor in dataset.vendors}

    assert len(dataset.contracts) == len(dataset.vendors) == VENDOR_COUNT
    for contract in dataset.contracts:
        vendor = vendors[contract.vendor_id]
        text = " ".join(clause.text for clause in contract.clauses)
        assert vendor.bank_account in text, contract.contract_id
        assert f"{vendor.payment_terms_days} " in text, contract.contract_id
        assert len({clause.clause_id for clause in contract.clauses}) == len(contract.clauses)


def test_retrieval_cases_point_at_real_clauses_and_split_by_vendor():
    dataset = build_dataset(SEED, 3)
    clause_ids = {clause.clause_id for c in dataset.contracts for clause in c.clauses}
    split_of: dict[str, set[str]] = {}

    for case in dataset.retrieval_cases:
        assert case.relevant, case.id
        assert set(case.relevant) <= clause_ids, case.id
        assert all(key.startswith(f"KTR-{case.vendor_id}#") for key in case.relevant), case.id
        split_of.setdefault(case.vendor_id, set()).add(case.split)

    assert all(len(splits) == 1 for splits in split_of.values())
    assert {next(iter(s)) for s in split_of.values()} == {"dev", "test"}


def test_price_amendment_bills_the_addendum_price_around_its_effective_date():
    dataset = build_dataset(SEED, DEFAULT_INVOICES)
    clauses = {c.clause_id: c for contract in dataset.contracts for c in contract.clauses}
    cases = [r for r in dataset.records if r.label == "price_amendment"]
    assert {r.variant for r in cases} == {"in_effect", "too_early"}

    for record in cases:
        clause = clauses[record.expected_citation]
        line = record.invoice.line_items[0]
        assert line.description in clause.text, record.id
        assert line.unit_price in amounts_in(clause.text), record.id


def test_expected_routes_and_causes_agree_with_the_router_and_the_taxonomy():
    for record in build_dataset(SEED, DEFAULT_INVOICES).records:
        issues = [Issue(code=code, message=code) for code in record.expected_issue_codes]

        assert route_invoice(issues).route == record.expected_route, record.id
        if record.expected_route == Route.INVESTIGATE:
            assert len(record.expected_causes) == len(issues), record.id
            for issue, cause in zip(issues, record.expected_causes, strict=True):
                assert cause in POSSIBLE_CAUSES[issue.code], record.id
        else:
            assert record.expected_causes == [], record.id
