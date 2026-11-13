from datetime import date
from decimal import Decimal

from evals.node.scoring import (
    FIELDS,
    KEY_FIELDS,
    decision_metrics,
    extraction_metrics,
    score_fields,
)
from lunas.models import Decision, Invoice, LineItem


def make_invoice(**overrides) -> Invoice:
    fields = {
        "vendor_name": "PT Sumber Makmur",
        "invoice_number": "INV-001",
        "invoice_date": date(2026, 11, 1),
        "due_date": date(2026, 12, 1),
        "po_number": "PO-001",
        "line_items": [
            LineItem(
                description="Kertas A4",
                quantity=Decimal(10),
                unit_price=Decimal(50000),
                amount=Decimal(500000),
            )
        ],
        "subtotal": Decimal(500000),
        "tax": Decimal(55000),
        "total": Decimal(555000),
        "bank_account": "5270418896",
    }
    return Invoice(**(fields | overrides))


def wrong_fields(actual: Invoice | None) -> list[str]:
    return [name for name, ok in score_fields(make_invoice(), actual).items() if not ok]


def test_identical_invoice_scores_every_field():
    assert wrong_fields(make_invoice()) == []


def test_amounts_compare_as_numbers_and_text_ignores_case_and_spacing():
    actual = make_invoice(total=Decimal("555000.00"), vendor_name="pt  sumber makmur")
    assert wrong_fields(actual) == []


def test_wrong_values_are_scored_per_field():
    actual = make_invoice(total=Decimal(555001), invoice_date=date(2026, 1, 11), po_number=None)
    assert wrong_fields(actual) == ["invoice_date", "total", "po_number"]


def test_invented_due_date_is_wrong_when_none_is_printed():
    expected = make_invoice(due_date=None)
    assert score_fields(expected, make_invoice())["due_date"] is False


def test_line_items_need_every_value_and_the_same_count():
    changed = make_invoice()
    changed.line_items[0].unit_price = Decimal(55000)
    assert wrong_fields(changed) == ["line_items"]
    assert wrong_fields(make_invoice(line_items=[])) == ["line_items"]


def test_notes_are_not_scored():
    assert wrong_fields(make_invoice(notes="Terima kasih")) == []


def test_failed_extraction_scores_every_field_wrong():
    assert wrong_fields(None) == list(FIELDS)


def test_extraction_metrics():
    perfect = score_fields(make_invoice(), make_invoice())
    one_key_field_wrong = score_fields(make_invoice(), make_invoice(total=Decimal(1)))
    one_other_field_wrong = score_fields(make_invoice(), make_invoice(due_date=None))

    metrics = extraction_metrics([perfect, one_key_field_wrong, one_other_field_wrong])

    assert metrics["documents"] == 3
    assert metrics["key_field_accuracy"] == (3 * len(KEY_FIELDS) - 1) / (3 * len(KEY_FIELDS))
    assert metrics["documents_with_all_key_fields"] == 2 / 3
    assert metrics["documents_fully_correct"] == 1 / 3
    assert metrics["field_accuracy"]["total"] == 2 / 3
    assert metrics["field_accuracy"]["vendor_name"] == 1.0


def test_decision_metrics_count_false_approvals_over_invoices_that_must_not_be_paid():
    expected = [Decision.APPROVE, Decision.APPROVE, Decision.HOLD, Decision.HOLD]
    actual = [Decision.APPROVE, Decision.HOLD, Decision.APPROVE, Decision.HOLD]

    metrics = decision_metrics(expected, actual)

    assert metrics["accuracy"] == 0.5
    assert metrics["false_approve_rate"] == 0.5
    assert metrics["confusion"] == {
        "approve->approve": 1,
        "approve->hold": 1,
        "hold->approve": 1,
        "hold->hold": 1,
    }


def test_false_approve_rate_is_undefined_without_unpayable_invoices():
    metrics = decision_metrics([Decision.APPROVE], [Decision.APPROVE])
    assert metrics["false_approve_rate"] is None
