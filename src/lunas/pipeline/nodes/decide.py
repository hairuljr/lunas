"""Step 9: the final decision. A fixed table over issue codes, never an LLM.

Unknown issue codes fall back to HOLD, so a new check can never cause an approval by
being forgotten here.
"""

from decimal import Decimal

from lunas.models import Decision, DecisionResult, Invoice, Issue

DEFAULT_APPROVAL_THRESHOLD = Decimal(50_000_000)

_ESCALATE = {
    # A different account or sender on a known PO is the classic payment-fraud pattern:
    # a human decides (SPEC §3).
    "bank_account_mismatch",
    "vendor_mismatch",
}
_REJECT = {
    # No PO, no pay.
    "po_missing",
    "po_not_found",
}
# Strongest first. Everything else that is an issue means HOLD.
_PRECEDENCE = ((Decision.ESCALATE, _ESCALATE), (Decision.REJECT, _REJECT))


def decide(
    invoice: Invoice,
    issues: list[Issue],
    approval_threshold: Decimal = DEFAULT_APPROVAL_THRESHOLD,
) -> DecisionResult:
    """`issues` is everything found by validation (step 4) and matching (step 5)."""
    codes = {issue.code for issue in issues}
    for decision, trigger in _PRECEDENCE:
        if codes & trigger:
            return DecisionResult(decision=decision, reasons=issues)
    if issues:
        return DecisionResult(decision=Decision.HOLD, reasons=issues)

    if invoice.total > approval_threshold:
        return DecisionResult(
            decision=Decision.ESCALATE,
            reasons=[
                Issue(
                    code="above_approval_threshold",
                    message=f"Total {invoice.total} is above the approval threshold "
                    f"{approval_threshold}",
                )
            ],
        )
    return DecisionResult(decision=Decision.APPROVE)
