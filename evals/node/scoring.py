"""Code-based scoring for the extraction and decision evals. No LLM judges here."""

import math
from collections import Counter
from collections.abc import Sequence
from typing import TYPE_CHECKING

from lunas.models import Decision, Invoice, LineItem

if TYPE_CHECKING:
    from data.generate import GroundTruth

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
OTHER_FIELDS = ("due_date", "payment_terms_days", "currency", "subtotal", "line_items")
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


def decision_metrics(expected: Sequence[Decision], actual: Sequence[Decision | None]) -> dict:
    """`None` is a run that ended without a decision. It counts as wrong, never as approve."""
    pairs = list(zip(expected, actual, strict=True))
    must_not_pay = [got for want, got in pairs if want != Decision.APPROVE]
    confusion = Counter(
        f"{want.value}->{got.value if got else 'no_decision'}" for want, got in pairs
    )
    return {
        "documents": len(pairs),
        "correct": sum(want == got for want, got in pairs),
        "accuracy": sum(want == got for want, got in pairs) / len(pairs),
        # The asymmetric one: an invoice that must not be paid was approved.
        "false_approve_rate": (
            sum(got == Decision.APPROVE for got in must_not_pay) / len(must_not_pay)
            if must_not_pay
            else None
        ),
        "confusion": dict(sorted(confusion.items())),
    }


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile. With 30 values, p95 is the 29th smallest."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q / 100 * len(ordered)) - 1)]


def by_label(
    records: Sequence[GroundTruth], decisions: Sequence[Decision | None]
) -> dict[str, dict]:
    """Decision metrics per class: an overall number hides a class the pipeline gets wrong."""
    groups: dict[str, tuple[list[Decision], list[Decision | None]]] = {}
    for record, decision in zip(records, decisions, strict=True):
        expected, actual = groups.setdefault(record.label, ([], []))
        expected.append(record.expected_decision)
        actual.append(decision)
    return {label: decision_metrics(*groups[label]) for label in sorted(groups)}


def percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:6.1%}"


def print_decisions(title: str, metrics: dict) -> None:
    """One report format for the pipeline and the baseline, so the two stay comparable."""
    print(f"\n{title}")
    print(f"  decision accuracy     {percent(metrics['accuracy'])}  (n={metrics['documents']})")
    print(f"  false-approve rate    {percent(metrics['false_approve_rate'])}")
    print(f"  expected->actual      {metrics['confusion']}")
    for label, row in metrics.get("by_label", {}).items():
        print(f"    {label:<20}{row['correct']:>4}/{row['documents']:<4}")
