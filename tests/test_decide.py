from datetime import date
from decimal import Decimal

import pytest

from lunas.models import Decision, Invoice, Issue
from lunas.pipeline.nodes.decide import decide


def make_invoice(total: int = 2220000) -> Invoice:
    return Invoice(
        vendor_name="PT Sumber Makmur",
        invoice_number="INV-001",
        invoice_date=date(2026, 11, 1),
        line_items=[],
        subtotal=Decimal(0),
        tax=Decimal(0),
        total=Decimal(total),
    )


def issues(*codes: str) -> list[Issue]:
    return [Issue(code=code, message=code) for code in codes]


def test_no_issues_is_approved():
    result = decide(make_invoice(), [])

    assert result.decision == Decision.APPROVE
    assert result.reasons == []


@pytest.mark.parametrize(
    ("codes", "expected"),
    [
        (["price_mismatch"], Decision.HOLD),
        (["quantity_exceeds_received"], Decision.HOLD),
        (["total_mismatch"], Decision.HOLD),
        (["po_missing"], Decision.REJECT),
        (["po_not_found"], Decision.REJECT),
        (["bank_account_mismatch"], Decision.ESCALATE),
        (["vendor_mismatch"], Decision.ESCALATE),
        (["price_mismatch", "bank_account_mismatch"], Decision.ESCALATE),
    ],
)
def test_decision_table(codes: list[str], expected: Decision):
    result = decide(make_invoice(), issues(*codes))

    assert result.decision == expected
    assert [issue.code for issue in result.reasons] == codes


def test_unknown_issue_code_is_never_approved():
    assert decide(make_invoice(), issues("a_check_added_later")).decision == Decision.HOLD


def test_clean_invoice_above_threshold_goes_to_a_human():
    result = decide(make_invoice(total=50_000_001), [])

    assert result.decision == Decision.ESCALATE
    assert [issue.code for issue in result.reasons] == ["above_approval_threshold"]


def test_clean_invoice_at_threshold_is_approved():
    assert decide(make_invoice(total=50_000_000), []).decision == Decision.APPROVE


def test_threshold_does_not_override_a_hold():
    result = decide(make_invoice(total=90_000_000), issues("price_mismatch"))

    assert result.decision == Decision.HOLD
