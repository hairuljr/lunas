"""Code-based scoring for the extraction and decision evals. No LLM judges here."""

from collections import Counter
from collections.abc import Sequence

from lunas.models import Decision, Invoice, LineItem

# The fields SPEC §2 holds to the extraction target.
KEY_FIELDS = (
    "vendor_name",
    "invoice_number",
    "invoice_date",
    "total",
    "tax",
    "po_number",
    "bank_account",
)
# Scored and reported too, outside the headline number. `notes` is free text and is not
# scored by exact match.
OTHER_FIELDS = ("due_date", "currency", "subtotal", "line_items")
FIELDS = KEY_FIELDS + OTHER_FIELDS


def _same(expected: object, actual: object) -> bool:
    """Exact match. For text, case and runs of whitespace are not part of the value; amounts
    compare as numbers (500000 == 500000.00) and dates as dates."""
    if isinstance(expected, str) and isinstance(actual, str):
        return " ".join(expected.split()).casefold() == " ".join(actual.split()).casefold()
    return expected == actual


def _same_lines(expected: list[LineItem], actual: list[LineItem]) -> bool:
    return len(expected) == len(actual) and all(
        _same(getattr(e, name), getattr(a, name))
        for e, a in zip(expected, actual, strict=True)
        for name in ("description", "quantity", "unit_price", "amount")
    )


def score_fields(expected: Invoice, actual: Invoice | None) -> dict[str, bool]:
    """Per-field exact match. A failed extraction scores every field as wrong."""
    if actual is None:
        return dict.fromkeys(FIELDS, False)
    scores = {
        name: _same(getattr(expected, name), getattr(actual, name))
        for name in FIELDS
        if name != "line_items"
    }
    scores["line_items"] = _same_lines(expected.line_items, actual.line_items)
    return {name: scores[name] for name in FIELDS}


def extraction_metrics(scores: Sequence[dict[str, bool]]) -> dict:
    documents = len(scores)
    correct = {name: sum(score[name] for score in scores) for name in FIELDS}
    key_correct = sum(correct[name] for name in KEY_FIELDS)
    return {
        "documents": documents,
        "key_field_accuracy": key_correct / (documents * len(KEY_FIELDS)),
        "documents_with_all_key_fields": sum(
            all(score[name] for name in KEY_FIELDS) for score in scores
        )
        / documents,
        "documents_fully_correct": sum(all(score.values()) for score in scores) / documents,
        "field_accuracy": {name: correct[name] / documents for name in FIELDS},
    }


def decision_metrics(expected: Sequence[Decision], actual: Sequence[Decision]) -> dict:
    pairs = list(zip(expected, actual, strict=True))
    must_not_pay = [got for want, got in pairs if want != Decision.APPROVE]
    confusion = Counter(f"{want.value}->{got.value}" for want, got in pairs)
    return {
        "documents": len(pairs),
        "accuracy": sum(want == got for want, got in pairs) / len(pairs),
        # The asymmetric one: an invoice that must not be paid was approved.
        "false_approve_rate": (
            sum(got == Decision.APPROVE for got in must_not_pay) / len(must_not_pay)
            if must_not_pay
            else None
        ),
        "confusion": dict(sorted(confusion.items())),
    }
